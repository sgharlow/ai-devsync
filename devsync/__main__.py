"""`python -m devsync` -- works even when the console script is not on PATH."""
import sys

from devsync.cli import main

sys.exit(main())
