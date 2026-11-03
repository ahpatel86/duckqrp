"""Benchmark the pipeline across dataset scales."""

from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qrp import Engine, load_study, run


def dataset_size(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*.parquet")) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", default="study/demo_type2.json")
    ap.add_argument("--data-root", default="/tmp/qrp_data")
    ap.add_argument("--scales", nargs="+", default=["100k"])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()

    study = load_study(args.study)
    rows = []
    for scale in args.scales:
        data = Path(args.data_root) / scale
        if not data.exists():
            print(f"skip {scale}: not generated")
            continue
        mb = dataset_size(data)
        times = []
        final_rows = 0
        for _ in range(args.repeat):
            gc.collect()
            eng = Engine(threads=args.threads, verbose=False)
            t0 = time.perf_counter()
            run(study, data, engine=eng, verbose=False)
            times.append(time.perf_counter() - t0)
            final_rows = eng.count("cohort_final")
            eng.close()
        best = min(times)
        rows.append((scale, mb, best, sum(times) / len(times), final_rows))
        print(f"{scale:>8}  {mb:8.1f} MB  best {best:7.2f}s  "
              f"mean {sum(times)/len(times):7.2f}s  "
              f"cohort_final {final_rows:,}")

    if rows:
        print()
        print(f"{'scale':>8}{'MB':>10}{'best s':>10}{'MB/s':>10}{'rows':>14}")
        print("-" * 52)
        for scale, mb, best, _mean, n in rows:
            print(f"{scale:>8}{mb:10.1f}{best:10.2f}{mb / best:10.1f}{n:>14,}")


if __name__ == "__main__":
    main()
