#!/usr/bin/env python3
"""Run optional site-owned Python tests without requiring placeholders."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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
    result.add_argument(
        "--root",
        type=Path,
        help="Consumer root; normally discovered from the test directory",
    )
    return result


def find_consumer_root(directory: Path, explicit: Path | None = None) -> Path:
    """Find the ordinary consumer whose pinned runtime tests must use."""

    candidates = (explicit.resolve(),) if explicit is not None else (
        directory.resolve(),
        *directory.resolve().parents,
    )
    for candidate in candidates:
        if (candidate / "orinoco.yaml").is_file() and (
            candidate / "orinoco.lock"
        ).is_file():
            return candidate
    raise RuntimeError(
        "consumer tests require a root containing orinoco.yaml and orinoco.lock"
    )


def verified_runtime_root(root: Path) -> Path:
    """Resolve the exact released runtime before importing site-owned tests."""

    executable = shutil.which("orinoco")
    if executable is None:
        raise RuntimeError("the locked orinoco executable is unavailable")
    environment = dict(os.environ)
    environment.pop("ORINOCO_RUNTIME_ROOT", None)
    completed = subprocess.run(
        [executable, "--root", os.fspath(root), "runtime", "verify", "--json"],
        cwd=root,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "the pinned released runtime failed verification"
            + (f": {detail}" if detail else "")
        )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("runtime verification returned invalid JSON") from error
    runtime_value = report.get("root") if isinstance(report, dict) else None
    if not isinstance(runtime_value, str) or not runtime_value:
        raise RuntimeError("runtime verification did not identify its exact root")
    runtime = Path(runtime_value)
    if (
        not runtime.is_absolute()
        or not runtime.is_dir()
        or not (runtime / "runtime-manifest.json").is_file()
    ):
        raise RuntimeError("runtime verification identified an invalid root")
    return runtime.resolve()


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    args = parser().parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Consumer test directory is missing: {directory}", file=sys.stderr)
        return 2

    if not any(path.is_file() for path in directory.rglob(args.pattern)):
        print("No site-owned Python tests are present yet.")
        return 0

    try:
        root = find_consumer_root(directory, args.root)
        runtime = verified_runtime_root(root)
    except RuntimeError as error:
        print(f"Consumer test runtime failed: {error}", file=sys.stderr)
        return 2
    os.environ["ORINOCO_RUNTIME_ROOT"] = os.fspath(runtime)

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
