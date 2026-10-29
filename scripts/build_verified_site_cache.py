#!/usr/bin/env python3
"""Create a declared, revalidation-only cache of exact legal-entity websites."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows: list[dict] = []
    for line in Path(args.profiles).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        profile = json.loads(line)
        website = (profile.get("evidence") or {}).get("website") or {}
        value = website.get("value") or {}
        identity = value.get("identity_assessment") or {}
        url = value.get("final_url") or profile.get("website")
        if website.get("status") != "available" or not identity.get("publishable") or not url:
            continue
        rows.append({
            "organisation_number": str(profile.get("organisation_number") or ""),
            "url": str(url),
            "identity_method": identity.get("method"),
            "verified_at": website.get("retrieved_at"),
            "source_content_sha256": website.get("content_sha256"),
            "cache_policy": "revalidation_only",
        })
    rows.sort(key=lambda row: row["organisation_number"])
    digest = hashlib.sha256("\n".join(json.dumps(row, sort_keys=True) for row in rows).encode()).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"entries": len(rows), "sha256": digest, "output": str(output), "policy": "revalidation_only"}, indent=2))


if __name__ == "__main__":
    main()
