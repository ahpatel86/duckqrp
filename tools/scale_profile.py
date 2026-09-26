"""Per-stage scaling profile: run a study at two data sizes and flag
any stage that grows faster than the data.

This is how the covariate join was found. Every test passes at fixture
scale, where a 15x-vs-4x difference is under a second; only a scaling
comparison makes it visible.

    python tools/scale_profile.py STUDY SMALL_DIR LARGE_DIR --factor 4

A stage whose ratio materially exceeds the data factor has both sides of
a join growing with the extract. That is the shape worth fixing: the
answer scales linearly, so the work should too.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qrp import Engine, load_study, run          # noqa: E402
from qrp.events import StageFinished             # noqa: E402


def profile(study_path: str, data: str) -> dict[str, float]:
    times: dict[str, float] = {}

    def sink(ev):
        if isinstance(ev, StageFinished):
            times[ev.name] = ev.seconds

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        study = load_study(study_path)
    eng = Engine(verbose=False, on_event=sink)
    try:
        run(study, data, engine=eng, verbose=False)
    finally:
        eng.close()
    return times


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("study")
    ap.add_argument("small")
    ap.add_argument("large")
    ap.add_argument("--factor", type=float, default=4.0,
                    help="how many times larger the large dataset is")
    ap.add_argument("--floor", type=float, default=0.05,
                    help="ignore stages faster than this at the small size")
    args = ap.parse_args()

    small = profile(args.study, args.small)
    large = profile(args.study, args.large)

    print(f"{'stage':<30}{'small':>8}{'large':>8}{'ratio':>8}  flag")
    flagged = 0
    for name in sorted(large, key=lambda k: -large[k]):
        a, b = small.get(name, 0.0), large[name]
        if a < args.floor:
            print(f"{name:<30}{a:>8.2f}{b:>8.2f}{'-':>8}")
            continue
        ratio = b / a
        # 25% headroom over the data factor before calling it a problem
        bad = ratio > args.factor * 1.25
        flagged += bad
        print(f"{name:<30}{a:>8.2f}{b:>8.2f}{ratio:>8.2f}"
              f"  {'SUPERLINEAR' if bad else ''}")
    print()
    print(f"{flagged} stage(s) growing faster than the data.")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
