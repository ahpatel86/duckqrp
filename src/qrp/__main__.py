"""`python -m qrp` entry point.

Propagates the CLI's return code so a failed run exits non-zero. This
was returning 0 on failure, which would silently break any wrapper
script or scheduler that checks the exit status — the kind of bug that
turns a loud failure into a quiet wrong answer.
"""

import sys

from .cli import main

sys.exit(main())
