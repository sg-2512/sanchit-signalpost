#!/usr/bin/env python3
"""Validate the source-rights registry before freezing a submission."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "source-rights.json"


def main() -> int:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    rows = data.get("connectors") or []
    strategies = [str(row.get("strategy") or "") for row in rows]
    required = {"strategy", "source", "access", "publication", "identity", "evidence"}
    invalid = [row.get("strategy") for row in rows if not required.issubset(row)]
    duplicates = sorted({name for name in strategies if name and strategies.count(name) > 1})
    allowed = {"official_safe", "conditional", "experimental", "discovery_only"}
    invalid_publication = [row.get("strategy") for row in rows if row.get("publication") not in allowed]
    report = {
        "schema_version": data.get("schema_version"),
        "declared_connectors": len(rows),
        "duplicates": duplicates,
        "missing_required_fields": invalid,
        "invalid_publication_states": invalid_publication,
        "passed": not duplicates and not invalid and not invalid_publication,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
