"""PyInstaller entry point for the browser native messaging host."""

import sys

from idm.native_host import main

if __name__ == "__main__":
    sys.exit(main())
