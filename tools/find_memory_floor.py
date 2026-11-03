"""
Find the minimum memory limit at which a study completes, and name the
stage that fails below it.

Why this exists
---------------
The POV1 memory fix came out of one observation: at 384MB the run always
died in the same stage, and that stage turned out to be a 10-way join
whose peak was the SUM of ten concurrent hash tables rather than the
largest of them. Splitting it dropped the floor from ~420MB to under
160MB with no runtime cost.

The fix is worth having. The *method* is worth more, because POV1 was
only found by being the stage that happened to fail first. Other stages
may have the same shape and will surface only at a scale I could not
test — a 300GB SCDM table on real hardware, for instance.

So: run this against your own data before sizing a VM.

    python tools/find_memory_floor.py --study s.json --indata scdm/

It binary-searches the limit, reports the failing stage at each step, and
prints the RAM-versus-spill trade-off curve. Each attempt runs in a
subprocess, so an out-of-memory failure cannot poison the search.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ATTEMPT = r'''
import json, os, sys, time, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, {src!r})
from qrp import Engine, load_study, run
from qrp.events import StageStarted

study_path, indata, mb, db, spill = sys.argv[1:6]
for suffix in ("", ".wal"):
    p = db + suffix
    if db != ":memory:" and os.path.exists(p):
        os.remove(p)

current = [None]
def sink(ev):
    if isinstance(ev, StageStarted):
        current[0] = ev.name

t0 = time.time()
result = {{"limit_mb": int(mb)}}
try:
    eng = Engine(database=db, memory_limit=mb + "MB", temp_directory=spill,
                 threads=1, verbose=False, on_event=sink)
    run(load_study(study_path), indata, engine=eng, verbose=False)
    result.update(ok=True, seconds=round(time.time() - t0, 1),
                  spill_mb=round(eng.peak_spill_bytes / 1e6),
                  peak_mb=round(eng.peak_memory_bytes / 1e6))
    eng.close()
except Exception as exc:
    result.update(ok=False, seconds=round(time.time() - t0, 1),
                  stage=current[0], error=type(exc).__name__)
print("RESULT " + json.dumps(result))
'''


def attempt(src: str, study: str, indata: str, mb: int,
            db: str, spill: str, timeout: int) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(ATTEMPT.format(src=src))
        script = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, script, study, indata, str(mb), db, spill],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"limit_mb": mb, "ok": False, "error": "Timeout", "stage": None}
    finally:
        Path(script).unlink(missing_ok=True)

    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {"limit_mb": mb, "ok": False, "error": "NoOutput",
            "stage": None, "stderr": proc.stderr[-300:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", required=True)
    ap.add_argument("--indata", required=True)
    ap.add_argument("--low", type=int, default=64, help="MB, assumed to fail")
    ap.add_argument("--high", type=int, default=8192, help="MB, assumed to work")
    ap.add_argument("--tolerance", type=int, default=32, help="MB")
    ap.add_argument("--db", default="/tmp/floor.duckdb")
    ap.add_argument("--spill", default="/tmp/floor_spill")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--curve", action="store_true",
                    help="also map the RAM/spill trade-off above the floor")
    a = ap.parse_args()

    src = str(Path(__file__).resolve().parents[1] / "src")
    Path(a.spill).mkdir(parents=True, exist_ok=True)

    size = sum(f.stat().st_size
               for f in Path(a.indata).rglob("*.parquet")) / 1e6
    print(f"input: {size:,.0f} MB")
    print(f"searching between {a.low} and {a.high} MB\n")

    top = attempt(src, a.study, a.indata, a.high, a.db, a.spill, a.timeout)
    if not top.get("ok"):
        print(f"  FAILS even at {a.high}MB "
              f"(stage: {top.get('stage')}, {top.get('error')})")
        print("  raise --high, or this study needs more than that.")
        return
    print(f"  {a.high:>6}MB  ok    {top['seconds']:>7.1f}s  "
          f"spill {top.get('spill_mb', 0):>6,}MB")

    lo, hi, best = a.low, a.high, top
    while hi - lo > a.tolerance:
        mid = (lo + hi) // 2
        r = attempt(src, a.study, a.indata, mid, a.db, a.spill, a.timeout)
        if r.get("ok"):
            print(f"  {mid:>6}MB  ok    {r['seconds']:>7.1f}s  "
                  f"spill {r.get('spill_mb', 0):>6,}MB")
            hi, best = mid, r
        else:
            print(f"  {mid:>6}MB  FAIL  {r.get('error')} "
                  f"in stage: {r.get('stage')}")
            lo = mid

    print()
    print(f"minimum memory: ~{hi} MB  ({hi / size:.2f}x the input)")
    print(f"peak spill at that limit: {best.get('spill_mb', 0):,} MB "
          f"({best.get('spill_mb', 0) / size:.1f}x the input)")
    print()
    print("Size the machine above this, not at it — and give the spill "
          "directory\nroom for the figure above, on a fast disk.")

    if a.curve:
        print("\nRAM / spill trade-off:")
        print(f"  {'limit':>8}{'time':>10}{'spilled':>12}")
        m = hi
        while m <= a.high:
            r = attempt(src, a.study, a.indata, m, a.db, a.spill, a.timeout)
            if r.get("ok"):
                print(f"  {m:>6}MB{r['seconds']:>9.1f}s"
                      f"{r.get('spill_mb', 0):>10,}MB")
            m *= 2


if __name__ == "__main__":
    main()
