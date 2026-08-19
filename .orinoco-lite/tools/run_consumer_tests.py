#!/usr/bin/env python3
"""Run optional site-owned Python tests without requiring placeholders."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "directory",
        nargs="?",
        default=".orinoco-lite/tests",
        help="Directory containing site-owned tests",
    )
    result.add_argument("--pattern", default="test_*.py")
    return result


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    args = parser().parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Consumer test directory is missing: {directory}", file=sys.stderr)
        return 2

    suite = unittest.TestLoader().discover(
        directory.as_posix(),
        pattern=args.pattern,
    )
    count = suite.countTestCases()
    if count == 0:
        print("No site-owned Python tests are present yet.")
        return 0

    print(f"Running {count} site-owned Python tests.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
