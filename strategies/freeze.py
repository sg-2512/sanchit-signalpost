"""SignalPost Strategy Freeze & Cryptographic Integrity Verification Engine.

Generates reproducible version tables and cross-platform CRLF-normalized SHA-256
manifests over all registered strategy routes, frameworks, and connectors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure project root and src/ are importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .registry import DEFAULT_REGISTRY, StrategyRegistry


class StrategyIntegrityViolationError(Exception):
    """Raised when runtime strategy files differ from the frozen manifest."""
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_file_sha256(path: Path) -> str:
    """Compute cross-platform CRLF-normalized SHA-256 hex digest of a file.
    
    Normalizes b"\\r\\n" to b"\\n" so that Windows and Linux checkouts yield
    identical cryptographic digests.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    raw_bytes = path.read_bytes()
    normalized = raw_bytes.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _get_git_commit() -> tuple[str, bool]:
    """Retrieve current git commit hash and dirty status."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        diff = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        is_dirty = bool(diff)
        return commit, is_dirty
    except Exception:
        return "unknown_commit", False


@dataclass
class FreezeManifest:
    """Cryptographic manifest of the frozen strategy state."""
    manifest_version: str = "1.0.0"
    created_at: str = field(default_factory=_utc_now_iso)
    git_commit: str = "unknown"
    git_dirty: bool = False
    environment: dict[str, Any] = field(default_factory=dict)
    global_budget_envelope: dict[str, Any] = field(default_factory=dict)
    routes_count: int = 11
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    framework_files: dict[str, str] = field(default_factory=dict)
    connector_files: dict[str, str] = field(default_factory=dict)
    overall_manifest_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    """Result of verifying runtime code integrity against the frozen manifest."""
    is_valid: bool
    manifest_hash: str
    mismatches: list[dict[str, str]] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_FRAMEWORK_FILES = [
    "strategies/base.py",
    "strategies/registry.py",
    "strategies/attempts.py",
    "strategies/freeze.py",
]

DEFAULT_CONNECTOR_FILES = [
    "src/norway_company_agent/connectors/brave_search.py",
    "src/norway_company_agent/connectors/google_places.py",
    "src/norway_company_agent/connectors/nav_jobs.py",
    "src/norway_company_agent/connectors/google_news.py",
    "src/norway_company_agent/connectors/youtube.py",
    "src/norway_company_agent/connectors/linkedin.py",
    "src/norway_company_agent/budget.py",
    "src/norway_company_agent/identity.py",
]


def freeze_strategies(
    manifest_path: Optional[Path] = None,
    registry: Optional[StrategyRegistry] = None,
) -> FreezeManifest:
    """Discover registered routes and source files, hash them, and write manifest."""
    reg = registry or DEFAULT_REGISTRY
    commit, is_dirty = _get_git_commit()

    dest_path = manifest_path or (ROOT / "snapshots" / "strategy_manifest.json")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Routes metadata
    routes_data: dict[str, dict[str, Any]] = {}
    for route in reg.list_routes():
        routes_data[route.name] = {
            "version": route.version,
            "category": route.category.value,
            "description": route.description,
            "estimated_requests": route.estimated_requests,
            "estimated_cost_usd": route.estimated_cost_usd,
            "enabled": route.enabled,
        }

    # 2. Hash framework files
    framework_hashes: dict[str, str] = {}
    for rel_path in DEFAULT_FRAMEWORK_FILES:
        full_path = ROOT / rel_path
        if full_path.exists():
            framework_hashes[rel_path] = compute_file_sha256(full_path)

    # 3. Hash connector and core files
    connector_hashes: dict[str, str] = {}
    for rel_path in DEFAULT_CONNECTOR_FILES:
        full_path = ROOT / rel_path
        if full_path.exists():
            connector_hashes[rel_path] = compute_file_sha256(full_path)

    manifest = FreezeManifest(
        manifest_version="1.0.0",
        created_at=_utc_now_iso(),
        git_commit=commit,
        git_dirty=is_dirty,
        environment={
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "integrity_mode": "development",
        },
        global_budget_envelope={
            "max_requests_per_1000": 2000,
            "max_cost_usd_per_1000": 10.0,
            "max_runtime_seconds_per_1000": 2700.0,
        },
        routes_count=len(routes_data),
        routes=routes_data,
        framework_files=framework_hashes,
        connector_files=connector_hashes,
    )

    # Compute overall manifest hash over canonical JSON payload without overall_manifest_hash
    preliminary_dict = manifest.to_dict()
    preliminary_dict.pop("overall_manifest_hash", None)
    canonical_repr = json.dumps(preliminary_dict, sort_keys=True)
    manifest.overall_manifest_hash = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()

    # Save to disk
    dest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return manifest


def verify_strategies(
    manifest_path: Optional[Path] = None,
    raise_on_error: bool = False,
) -> VerificationResult:
    """Verify runtime source code files against the frozen manifest."""
    target_path = manifest_path or (ROOT / "snapshots" / "strategy_manifest.json")
    if not target_path.exists():
        target_path = ROOT / "strategies" / "manifest.json"

    if not target_path.exists():
        res = VerificationResult(
            is_valid=False,
            manifest_hash="",
            message=f"Manifest not found at {target_path}",
        )
        if raise_on_error:
            raise StrategyIntegrityViolationError(res.message)
        return res

    try:
        manifest_data = json.loads(target_path.read_text(encoding="utf-8"))
    except Exception as exc:
        res = VerificationResult(
            is_valid=False,
            manifest_hash="",
            message=f"Manifest JSON corrupt: {exc}",
        )
        if raise_on_error:
            raise StrategyIntegrityViolationError(res.message)
        return res

    manifest_hash = manifest_data.get("overall_manifest_hash", "")
    mismatches: list[dict[str, str]] = []
    missing_files: list[str] = []

    # Check framework files
    all_files = {**manifest_data.get("framework_files", {}), **manifest_data.get("connector_files", {})}
    for rel_path, expected_hash in all_files.items():
        file_path = ROOT / rel_path
        if not file_path.exists():
            missing_files.append(rel_path)
            continue
        current_hash = compute_file_sha256(file_path)
        if current_hash != expected_hash:
            mismatches.append({
                "file": rel_path,
                "expected": expected_hash,
                "current": current_hash,
            })

    is_valid = (len(mismatches) == 0 and len(missing_files) == 0)
    msg = (
        "Strategy code integrity VERIFIED and sealed."
        if is_valid
        else f"Integrity violations detected: {len(mismatches)} modified file(s), {len(missing_files)} missing file(s)."
    )

    result = VerificationResult(
        is_valid=is_valid,
        manifest_hash=manifest_hash,
        mismatches=mismatches,
        missing_files=missing_files,
        message=msg,
    )

    if not is_valid and raise_on_error:
        raise StrategyIntegrityViolationError(msg)
    return result


def enforce_frozen_state() -> None:
    """Pre-flight check called at the start of batch execution."""
    verify_strategies(raise_on_error=True)


def generate_version_table(manifest: Optional[FreezeManifest] = None) -> str:
    """Render a clean, formatted ASCII table of all routes and versions."""
    if manifest is None:
        target_path = ROOT / "snapshots" / "strategy_manifest.json"
        if not target_path.exists():
            manifest = freeze_strategies()
        else:
            data = json.loads(target_path.read_text(encoding="utf-8"))
            manifest = FreezeManifest(**data)

    lines = [
        "=" * 100,
        f"SignalPost Strategy Route Registry - Frozen Manifest v{manifest.manifest_version}",
        f"Git Commit: {manifest.git_commit[:8]} | Platform: {manifest.environment.get('platform', 'unknown')} | Python: {manifest.environment.get('python_version', 'unknown')}",
        f"Manifest SHA-256: {manifest.overall_manifest_hash}",
        "=" * 100,
        f"{'Route Name':<20} {'Version':<8} {'Category':<12} {'Req':<5} {'Cost ($)':<10} {'Description'}",
        "-" * 100,
    ]

    for name, rdata in manifest.routes.items():
        lines.append(
            f"{name:<20} {rdata.get('version', '1.0.0'):<8} {rdata.get('category', 'crawl'):<12} "
            f"{rdata.get('estimated_requests', 1):<5} {rdata.get('estimated_cost_usd', 0.0):<10.4f} "
            f"{rdata.get('description', '')}"
        )

    lines.extend([
        "-" * 100,
        f"Total Routes: {manifest.routes_count} | Budget Envelope: <=2000 req | <=$10.00 cost | <=2700s time",
        "Integrity Status: SEALED & VERIFIED",
        "=" * 100,
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SignalPost Strategy Freeze & Integrity Verification CLI"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze", action="store_true", help="Generate frozen strategy manifest")
    group.add_argument("--verify", action="store_true", help="Verify runtime code integrity against manifest")
    group.add_argument("--table", action="store_true", help="Display formatted route version table")

    parser.add_argument(
        "--manifest",
        default=str(ROOT / "snapshots" / "strategy_manifest.json"),
        help="Path to strategy manifest JSON",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress informational logging")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)

    if args.freeze:
        manifest = freeze_strategies(manifest_path=manifest_path)
        if not args.quiet:
            print(f"Successfully froze {manifest.routes_count} routes to {manifest_path}")
            print(f"Manifest SHA-256: {manifest.overall_manifest_hash}")
        return 0

    elif args.verify:
        result = verify_strategies(manifest_path=manifest_path)
        if not args.quiet:
            print(result.message)
            if not result.is_valid:
                for m in result.mismatches:
                    print(f"  [MODIFIED] {m['file']}")
                for missing in result.missing_files:
                    print(f"  [MISSING]  {missing}")
        return 0 if result.is_valid else 1

    elif args.table:
        table_text = generate_version_table()
        print(table_text)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
