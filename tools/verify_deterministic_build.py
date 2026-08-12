#!/usr/bin/env python3
"""Compare two independently generated static trees byte for byte."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any


class DeterminismError(RuntimeError):
    """Raised when a build tree cannot satisfy the deterministic contract."""


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Return an exact path, mode, size, and SHA-256 inventory."""

    if not root.is_dir():
        raise DeterminismError(f"build directory does not exist: {root}")
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise DeterminismError(f"symbolic links are forbidden in static output: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DeterminismError(f"non-regular static output: {relative}")
        information = path.stat()
        result[relative] = {
            "mode": stat.S_IMODE(information.st_mode),
            "size": information.st_size,
            "sha256": digest(path),
        }
    if not result:
        raise DeterminismError(f"build directory is empty: {root}")
    return result


def compare(first: Path, second: Path) -> tuple[dict[str, Any], list[str]]:
    first_inventory = inventory(first)
    second_inventory = inventory(second)
    paths = sorted(first_inventory.keys() | second_inventory.keys())
    differences = [
        path
        for path in paths
        if first_inventory.get(path) != second_inventory.get(path)
    ]
    canonical = json.dumps(
        first_inventory, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "algorithm": "sha256",
        "files": first_inventory,
        "file_count": len(first_inventory),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
        "comparison": {
            "first": first.as_posix(),
            "second": second.as_posix(),
            "identical": not differences,
            "differences": differences,
        },
    }
    return manifest, differences


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("first", type=Path)
    result.add_argument("second", type=Path)
    result.add_argument("--manifest", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest, differences = compare(args.first, args.second)
    except DeterminismError as error:
        print(f"deterministic build verification failed: {error}", file=sys.stderr)
        return 2
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if differences:
        print("static builds differ:", file=sys.stderr)
        for path in differences:
            print(f"- {path}", file=sys.stderr)
        return 1
    print(
        f"deterministic static build verified: {manifest['file_count']} files, "
        f"tree {manifest['tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
