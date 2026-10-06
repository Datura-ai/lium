"""PyInstaller entry point for the Lium CLI."""

import multiprocessing

from lium.cli.cli import main

# Prevent spawned helper processes from re-entering Click as if this binary were
# a generic Python interpreter when running under PyInstaller.
multiprocessing.freeze_support()


if __name__ == "__main__":
    main()
