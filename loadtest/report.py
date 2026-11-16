"""Turn Locust's CSV into a short table, and fail if any request failed.

python loadtest/report.py results/locust_stats.csv
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path


def report(path: Path) -> tuple[str, bool]:
    rows = list(csv.DictReader(path.open()))
    total = next(r for r in rows if r["Name"] == "Aggregated")
    lines = [
        f"**{int(total['Request Count']):,} requests, {int(total['Failure Count'])} failed, "
        f"{float(total['Requests/s']):.1f} a second; median {total['50%']} ms, 95th percentile {total['95%']} ms.**",
        "",
        "| Request | Count | Failed | Median ms | 95th percentile ms |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        if r["Name"] != "Aggregated":
            lines.append(f"| {r['Type']} {r['Name']} | {r['Request Count']} | {r['Failure Count']} | {r['50%']} | {r['95%']} |")
    return "\n".join(lines) + "\n", int(total["Failure Count"]) == 0


def main(argv: list[str]) -> int:
    text, ok = report(Path(argv[0]))
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Portal load test\n\n" + text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
