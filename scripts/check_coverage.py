"""Fail unless the test run covered the SDK as the project requires.

Run after ``coverage run -m pytest`` and ``coverage json``::

    uv run coverage run -m pytest
    uv run coverage json
    uv run python scripts/check_coverage.py

* Hand-written runtime code (the HTTP plumbing, errors, pagination, streams, webhooks,
  the clients): line coverage and branch coverage are each at least 95 %.
* Generated code (``types.py`` and the two resource modules): every statement is run,
  which means every method of every operation was called at least once (the tests call
  each one through the sync and the async client).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLD = 95.0
GENERATED = {
    "src/billionsend/types.py",
    "src/billionsend/resources/_sync.py",
    "src/billionsend/resources/_async.py",
}


def percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def main(path: str = "coverage.json") -> int:
    files = json.loads(Path(path).read_text(encoding="utf-8"))["files"]
    failures: list[str] = []
    totals = {"lines": [0, 0], "branches": [0, 0]}
    for name, data in sorted(files.items()):
        summary = data["summary"]
        lines = (summary["covered_lines"], summary["num_statements"])
        branches = (summary.get("covered_branches", 0), summary.get("num_branches", 0))
        if name in GENERATED:
            if summary["missing_lines"]:
                failures.append(f"{name}: {summary['missing_lines']} generated lines never ran")
            continue
        for kind, (covered, total) in (("lines", lines), ("branches", branches)):
            totals[kind][0] += covered
            totals[kind][1] += total
        print(f"{name:45} lines {percent(*lines):6.1f}%  branches {percent(*branches):6.1f}%")
        for kind, (covered, total) in (("line", lines), ("branch", branches)):
            if percent(covered, total) < THRESHOLD:
                failures.append(
                    f"{name}: {kind} coverage {percent(covered, total):.1f}% is under {THRESHOLD}%"
                )
    for kind, (covered, total) in totals.items():
        print(f"hand-written {kind}: {percent(covered, total):.1f}% ({covered}/{total})")
        if percent(covered, total) < THRESHOLD:
            failures.append(f"hand-written {kind} coverage is under {THRESHOLD}%")
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
