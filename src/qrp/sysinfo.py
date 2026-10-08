"""
Host capability detection, so the UI can suggest sensible defaults.

The point is not to be clever. It is that "Memory" as a dropdown of
fixed values is the wrong shape for this question: a user on a 128 GB
server and a user on an 8 GB laptop need different answers, and neither
should have to guess what DuckDB will do with the number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def total_memory_bytes() -> int:
    """Physical RAM, honouring a cgroup limit if one applies.

    Containers and job schedulers cap memory below what /proc/meminfo
    reports. Reading the cgroup limit first avoids confidently
    suggesting 64GB inside a 4GB container.
    """
    for path in (
        "/sys/fs/cgroup/memory.max",                        # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",      # cgroup v1
    ):
        try:
            raw = open(path).read().strip()
            if raw and raw != "max":
                val = int(raw)
                if 0 < val < (1 << 62):
                    return val
        except Exception:
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        pass
    try:  # macOS / Windows
        import psutil

        return psutil.virtual_memory().total
    except Exception:
        return 0


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def human(n: int) -> str:
    if n <= 0:
        return "unknown"
    for unit, div in (("TB", 1000**4), ("GB", 1000**3), ("MB", 1000**2)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return f"{n}B"


# Ceiling on the default. Measured on a real extract (174k patients,
# 35.2M rows): above a 1 GB limit, more memory buys about 2% — 4.94 s at
# 1 GB against 4.82 s at 3.1 GiB. 8 GB is generous headroom for a study
# several times larger, and still a number a DP can reason about.
#
# The point is predictability. DuckDB's own default is 80% of physical
# RAM, so the same package takes 3 GB on a laptop and ~102 GB on a
# 128 GB server — silently, and on shared hardware.
DEFAULT_MEMORY_CEILING_GB = 8


def suggest_memory_limit(total: int | None = None) -> str:
    """The default memory limit: 8 GB, or less on a smaller host.

    Capped BOTH ways. The ceiling keeps a big shared server from having
    80% of its RAM taken by a job that does not need it; the fraction
    keeps a small host from being handed a limit it cannot honour, where
    DuckDB would accept the setting and then fail partway through.
    """
    total = total if total is not None else total_memory_bytes()
    if total <= 0:
        return "auto"
    gb = total / 1000**3
    frac = 0.60 if gb <= 8 else 0.70 if gb <= 32 else 0.75
    return f"{max(1, min(DEFAULT_MEMORY_CEILING_GB, int(gb * frac)))}GB"


def memory_choices(total: int | None = None) -> list[str]:
    """Offer only values the host can actually satisfy."""
    total = total if total is not None else total_memory_bytes()
    gb = int(total / 1000**3) if total else 0
    ladder = [1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256]
    out = [f"{g}GB" for g in ladder if not gb or g <= gb]
    return ["auto", *out] if out else ["auto"]


@dataclass(frozen=True)
class HostInfo:
    memory_bytes: int
    cpus: int

    @classmethod
    def detect(cls) -> "HostInfo":
        return cls(total_memory_bytes(), cpu_count())

    def summary(self) -> str:
        return (f"host: {human(self.memory_bytes)} RAM, {self.cpus} CPU"
                f"{'s' if self.cpus != 1 else ''}  ·  "
                f"suggested limit {suggest_memory_limit(self.memory_bytes)}")
