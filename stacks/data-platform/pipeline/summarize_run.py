#!/usr/bin/env python3
"""Parse dbt test run_results.json and emit machine-readable summary for host wrapper."""

import json
import sys
from pathlib import Path

results_path = Path(sys.argv[1])
rc = int(sys.argv[2])

summary = {"dbt_test_rc": rc, "pass": 0, "warn": 0, "fail": 0, "error": 0, "warn_tests": [], "fail_tests": []}
if results_path.exists():
    data = json.loads(results_path.read_text(encoding="utf-8"))
    for r in data.get("results") or []:
        st = r.get("status", "")
        uid = r.get("unique_id", "")
        short_name = uid.split(".")[-1] if uid else uid
        if st == "pass":
            summary["pass"] += 1
        elif st == "warn":
            summary["warn"] += 1
            summary["warn_tests"].append(short_name)
        elif st == "fail":
            summary["fail"] += 1
            summary["fail_tests"].append(short_name)
        else:
            summary["error"] += 1
            summary["fail_tests"].append(short_name)

print("DBT_TEST_SUMMARY_JSON=" + json.dumps(summary))
