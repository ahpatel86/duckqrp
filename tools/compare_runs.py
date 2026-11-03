"""
Compare run logs.

This is the payoff for the .jsonl half of the log: "which stage got
slower since last week" answered from files, without re-running
anything. Point it at a log directory.

    python tools/compare_runs.py logs/
    python tools/compare_runs.py logs/ --last 5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    header, stages, finished = {}, {}, {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = row.get("type")
        if kind == "Header":
            header = row
        elif kind == "StageFinished":
            stages[row["name"]] = row["seconds"]
        elif kind == "RunFinished":
            finished = row
    return {"path": path, "header": header, "stages": stages,
            "finished": finished}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir")
    ap.add_argument("--last", type=int, default=3)
    a = ap.parse_args()

    files = sorted(Path(a.log_dir).glob("*.jsonl"))[-a.last:]
    if not files:
        print(f"no .jsonl logs in {a.log_dir}")
        return
    runs = [load(f) for f in files]

    print(f"{'':<30}" + "".join(f"{r['path'].stem[-15:]:>17}" for r in runs))
    print("-" * (30 + 17 * len(runs)))

    names: list[str] = []
    for r in runs:
        for n in r["stages"]:
            if n not in names:
                names.append(n)

    for n in names:
        row = f"{n:<30}"
        base = runs[0]["stages"].get(n)
        for r in runs:
            v = r["stages"].get(n)
            if v is None:
                row += f"{'—':>17}"
            elif base and r is not runs[0]:
                delta = (v - base) / base * 100
                mark = "+" if delta > 0 else ""
                row += f"{v:>10.2f}s {mark}{delta:>4.0f}%"
            else:
                row += f"{v:>16.2f}s"
        print(row)

    print("-" * (30 + 17 * len(runs)))
    for label, key, fmt in (
        ("TOTAL", "seconds", lambda v: f"{v:>16.2f}s"),
        ("peak RAM", "peak_memory_bytes", lambda v: f"{v / 1e6:>14,.0f}MB"),
        ("spilled", "peak_spill_bytes", lambda v: f"{v / 1e6:>14,.0f}MB"),
    ):
        row = f"{label:<30}"
        for r in runs:
            v = r["finished"].get(key, 0) or 0
            row += fmt(v)
        print(row)

    row = f"{'outcome':<30}"
    for r in runs:
        f_ = r["finished"]
        state = ("cancelled" if f_.get("cancelled")
                 else "ok" if f_.get("ok") else "FAILED")
        row += f"{state:>17}"
    print(row)


if __name__ == "__main__":
    main()
