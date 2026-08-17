#!/usr/bin/env python3
"""Verify the exact Hugo version and Extended feature contract."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys


VERSION = re.compile(
    r"\bhugo v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:-(?P<revision>[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*))?"
    r"(?P<variants>(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)*)"
    r"(?=\s|$)",
    re.IGNORECASE,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", required=True)
    result.add_argument("--extended", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    executable = shutil.which("hugo")
    if executable is None:
        print("Hugo is unavailable", file=sys.stderr)
        return 2
    result = subprocess.run(
        [executable, "version"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    match = VERSION.search(result.stdout)
    if result.returncode or match is None:
        print(f"cannot parse Hugo version: {result.stdout.strip()}", file=sys.stderr)
        return 2
    failures: list[str] = []
    if match.group("version") != args.version:
        failures.append(
            f"expected Hugo {args.version}, found {match.group('version')}"
        )
    variants = {
        item.lower()
        for item in match.group("variants").split("+")
        if item
    }
    if args.extended and "extended" not in variants:
        failures.append("Hugo Extended is required")
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
