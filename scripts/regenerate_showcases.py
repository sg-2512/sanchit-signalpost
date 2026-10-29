#!/usr/bin/env python3
"""
Regenerate showcase HTML files using the updated showcase.html template.
"""
from pathlib import Path
import json
import shutil
from src.norway_company_agent.showcase import write_showcase_html

dirs_to_update = [
    Path("out/eval-1000-perfect"),
    Path("out/eval-100-test-perfect"),
    Path("out/eval-1000-roadmap"),
    Path("out/fresh-1000-live"),
]

for d in dirs_to_update:
    prof_file = d / "profiles.jsonl"
    obs_file = d / "all-observations.jsonl"
    if not prof_file.exists():
        print(f"Skipping {d}: profiles.jsonl missing")
        continue

    print(f"Regenerating showcase for {d}...")
    profiles = [json.loads(line) for line in prof_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    observations = []
    if obs_file.exists():
        observations = [json.loads(line) for line in obs_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    target_showcase = d / "showcase.html"
    target_index = d / "index.html"

    write_showcase_html(profiles, observations, output_path=target_showcase)
    shutil.copyfile(target_showcase, target_index)
    print(f"Successfully wrote {target_showcase} and {target_index}")

# Also update root out/showcase.html if eval-1000-perfect exists
root_showcase = Path("out/showcase.html")
eval_1000_showcase = Path("out/eval-1000-perfect/showcase.html")
if eval_1000_showcase.exists():
    shutil.copyfile(eval_1000_showcase, root_showcase)
    print(f"Updated {root_showcase}")
