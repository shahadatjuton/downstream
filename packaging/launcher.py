"""PyInstaller entry point: launches the GUI (or CLI when URLs are passed)."""

import multiprocessing
import sys

from idm.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
