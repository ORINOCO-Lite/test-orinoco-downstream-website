#!/usr/bin/env python3
"""Emit or check the exact, non-mutating full-site bundle inventory."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys
from typing import Any

import yaml


BUNDLE_FORMAT = "orinoco-site-bundle-v1"
SOURCE = {
    "repository": "https://github.com/con/centerforopenneuroscience.org.git",
    "commit": "26907c487efaa2c31bba9d02398aa201ab6f774b",
    "scope": "full",
}
IMPORTABLE = {
    "consumer_tests",
    "extensions",
    "generated",
    "initialized_site_owned",
    "site_policy",
}
IGNORED_PARTS = {
    ".git",
    ".pixi",
    ".orinoco",
    "__pycache__",
    "build",
    "node_modules",
    "playwright-report",
    "test-results",
}
FORBIDDEN_ANYWHERE = {".git", ".gitmodules", ".env", "credentials"}
MANIFEST_NAME = "orinoco-site-bundle.json"
ANNEX_KEY_PATTERN = (
    rb"(?:MD5E|SHA256E)-s[0-9]+--[0-9a-f]{32,64}"
    rb"(?:\.[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
ANNEX_POINTER_PATTERN = re.compile(
    rb"^(?:(?:\.\./)+)?/?(?:\.git/)?annex/objects/"
    rb"(?:(?:[^/\r\n]+/)+)?"
    rb"(?P<key>" + ANNEX_KEY_PATTERN + rb")"
    rb"(?:/(?P=key))?\r?\n?$"
)


class InventoryError(RuntimeError):
    """Report an unsafe or inexact bundle inventory."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def annex_pointer_key(path: Path) -> str | None:
    """Return the annex key when a regular file is pointer-form text."""

    if not path.is_file() or path.stat().st_size > 4096:
        return None
    match = ANNEX_POINTER_PATTERN.fullmatch(path.read_bytes())
    return match.group("key").decode("ascii") if match is not None else None


def reject_annex_pointer(path: Path, normalized: str) -> None:
    key = annex_pointer_key(path)
    if key is not None:
        raise InventoryError(
            f"bundle contains a git-annex pointer-form regular file: "
            f"{normalized} ({key})"
        )


def path_matches(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    normalized_pattern = pattern.strip("/")
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return normalized == prefix or normalized.startswith(prefix + "/")
    return fnmatch.fnmatchcase(normalized, normalized_pattern)


def ownership_classes(path: Path) -> dict[str, list[str]]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise InventoryError(f"cannot read ownership contract {path}: {error}") from error
    if not isinstance(value, dict) or value.get("contract_version") != 1:
        raise InventoryError("ownership contract must use contract_version 1")
    declared = value.get("classes")
    if not isinstance(declared, dict):
        raise InventoryError("ownership contract classes must be a mapping")
    result: dict[str, list[str]] = {}
    for name, details in declared.items():
        patterns = details.get("paths") if isinstance(details, dict) else None
        if not isinstance(name, str) or not isinstance(patterns, list) or not all(
            isinstance(item, str) and item for item in patterns
        ):
            raise InventoryError(f"invalid ownership class: {name!r}")
        result[name] = patterns
    missing = IMPORTABLE - set(result)
    if missing:
        raise InventoryError(f"ownership contract omits importable classes: {sorted(missing)}")
    return result


def classify(path: str, classes: dict[str, list[str]]) -> str:
    matches = [
        name
        for name, patterns in classes.items()
        if any(path_matches(path, pattern) for pattern in patterns)
    ]
    if len(matches) != 1 or matches[0] not in IMPORTABLE:
        detail = ", ".join(matches) if matches else "unclassified"
        raise InventoryError(f"bundle path has no unique importable owner: {path} ({detail})")
    return matches[0]


def inventory(root: Path, ownership: Path) -> dict[str, Any]:
    classes = ownership_classes(ownership)
    files: dict[str, str] = {}
    classifications: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        normalized = relative.as_posix()
        if normalized == MANIFEST_NAME or path.name == ".gitkeep":
            continue
        if (
            any(part in FORBIDDEN_ANYWHERE for part in relative.parts)
            or relative.parts[0] == ".github"
        ):
            raise InventoryError(f"forbidden bundle path: {normalized}")
        if path.is_symlink():
            raise InventoryError(f"bundle contains a symbolic link: {normalized}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InventoryError(f"bundle contains a non-regular file: {normalized}")
        reject_annex_pointer(path, normalized)
        classification = classify(normalized, classes)
        files[normalized] = sha256(path)
        classifications[normalized] = classification
        sizes[normalized] = path.stat().st_size
    if not files:
        raise InventoryError("bundle contains no importable files")
    counts = {
        name: sum(value == name for value in classifications.values())
        for name in sorted(set(classifications.values()))
    }
    return {
        "format": BUNDLE_FORMAT,
        "source": SOURCE,
        "files": files,
        "classifications": classifications,
        "sizes": sizes,
        "summary": {
            "bytes": sum(sizes.values()),
            "classes": counts,
            "files": len(files),
        },
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read bundle manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise InventoryError("bundle manifest must be a JSON object")
    return value


def verify_declared_inventory(
    root: Path,
    ownership: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify the immutable source inventory inside an instantiated consumer.

    Copier-owned facade files, engine outputs added after import, and the import
    ledger are intentionally outside the source bundle. They therefore do not
    make the preserved source manifest stale, while every declared source path
    remains required and byte-exact.
    """

    if manifest.get("format") != BUNDLE_FORMAT:
        raise InventoryError(f"bundle manifest must use format {BUNDLE_FORMAT}")
    if manifest.get("source") != SOURCE:
        raise InventoryError("bundle manifest source provenance differs")

    files = manifest.get("files")
    classifications = manifest.get("classifications")
    sizes = manifest.get("sizes")
    summary = manifest.get("summary")
    if not isinstance(files, dict) or not all(
        isinstance(path, str)
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        for path, digest in files.items()
    ):
        raise InventoryError("bundle manifest files must map paths to SHA-256 digests")
    if not isinstance(classifications, dict) or not all(
        isinstance(path, str) and isinstance(owner, str)
        for path, owner in classifications.items()
    ):
        raise InventoryError("bundle manifest classifications must be a mapping")
    if not isinstance(sizes, dict) or not all(
        isinstance(path, str)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        for path, size in sizes.items()
    ):
        raise InventoryError("bundle manifest sizes must be non-negative integers")
    if set(files) != set(classifications) or set(files) != set(sizes):
        raise InventoryError(
            "bundle manifest files, classifications, and sizes differ"
        )

    classes = ownership_classes(ownership)
    observed_class_counts = {
        name: sum(owner == name for owner in classifications.values())
        for name in sorted(set(classifications.values()))
    }
    expected_summary = {
        "bytes": sum(sizes.values()),
        "classes": observed_class_counts,
        "files": len(files),
    }
    if summary != expected_summary:
        raise InventoryError("bundle manifest summary differs from its declarations")

    for normalized in sorted(files):
        relative = PurePosixPath(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or any(part in FORBIDDEN_ANYWHERE for part in relative.parts)
            or relative.parts[0] == ".github"
            or normalized == MANIFEST_NAME
        ):
            raise InventoryError(f"unsafe declared bundle path: {normalized}")
        owner = classify(normalized, classes)
        if classifications[normalized] != owner:
            raise InventoryError(
                f"bundle classification mismatch for {normalized}: "
                f"{classifications[normalized]} != {owner}"
            )
        path = root.joinpath(*relative.parts)
        if path.is_symlink():
            raise InventoryError(f"declared bundle path is a symbolic link: {normalized}")
        if not path.is_file():
            raise InventoryError(f"declared bundle file is missing: {normalized}")
        reject_annex_pointer(path, normalized)
        if path.stat().st_size != sizes[normalized]:
            raise InventoryError(f"declared bundle size mismatch: {normalized}")
        if sha256(path) != files[normalized]:
            raise InventoryError(f"declared bundle digest mismatch: {normalized}")
    return manifest


def serialized(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    result.add_argument("--ownership", type=Path)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--emit", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    ownership = (
        args.ownership.resolve()
        if args.ownership is not None
        else (
            root / "template-ownership.yml"
            if (root / "template-ownership.yml").is_file()
            else root / "tests/parity/site-bundle-ownership.yml"
        )
    )
    try:
        if args.emit:
            observed = inventory(root, ownership)
            rendered = serialized(observed)
            sys.stdout.write(rendered)
            return 0
        manifest = root / MANIFEST_NAME
        observed = verify_declared_inventory(
            root,
            ownership,
            load_manifest(manifest),
        )
    except (InventoryError, OSError) as error:
        print(f"site bundle inventory failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "bytes": observed["summary"]["bytes"],
                "files": observed["summary"]["files"],
                "source": observed["source"],
                "status": "current",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
