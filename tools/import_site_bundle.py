#!/usr/bin/env python3
"""Import a complete reviewed site bundle into site-owned paths only."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from template_contract import (
    SITE_OWNED_CLASSES,
    ContractError,
    classify,
    find_root,
    load_yaml,
    ownership_classes,
    sha256_file,
)


BUNDLE_FORMAT = "orinoco-site-bundle-v1"
IMPORTABLE_CLASSES = SITE_OWNED_CLASSES | {"generated"}
FORBIDDEN_NAMES = {".git", ".gitmodules", ".env", "credentials"}
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = (
        datetime.fromtimestamp(int(epoch), timezone.utc)
        if epoch is not None
        else datetime.now(timezone.utc)
    )
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read bundle manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("format") != BUNDLE_FORMAT:
        raise ContractError(f"bundle manifest must use format {BUNDLE_FORMAT}")
    return value


def source_files(
    bundle: Path,
    classes: dict[str, list[str]],
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle)
        if relative.as_posix() == "orinoco-site-bundle.json":
            continue
        if relative.parts and relative.parts[0] == ".github":
            raise ContractError(f"forbidden root workflow path: {relative}")
        if any(part in FORBIDDEN_NAMES for part in relative.parts):
            raise ContractError(f"forbidden repository or credential path: {relative}")
        if path.is_symlink():
            raise ContractError(f"symbolic links are not accepted in bundles: {relative}")
        if path.is_dir():
            continue
        ownership = classify(relative.as_posix(), classes)
        if len(ownership) != 1 or ownership[0] not in IMPORTABLE_CLASSES:
            detail = ", ".join(ownership) if ownership else "unclassified"
            raise ContractError(
                f"bundle path is not importable site content: {relative} ({detail})"
            )
        if path.name == ".gitkeep":
            continue
        result[relative.as_posix()] = path
    if not result:
        raise ContractError("bundle contains no importable site-owned files")
    return result


def resolve_provenance(
    args: argparse.Namespace, manifest: dict[str, Any] | None
) -> tuple[str, str, str]:
    source = manifest.get("source", {}) if manifest else {}
    if not isinstance(source, dict):
        raise ContractError("bundle source provenance must be a mapping")
    repository = args.source_repository or source.get("repository")
    commit = args.source_commit or source.get("commit")
    scope = args.scope or source.get("scope")
    if not isinstance(repository, str) or not repository:
        raise ContractError("source repository is required")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ContractError("source commit must be a full lower-case 40-hex SHA")
    if not isinstance(scope, str) or not scope:
        raise ContractError("source scope is required (use 'full' for a complete site)")
    return repository, commit, scope


def verify_manifest(
    files: dict[str, Path],
    manifest: dict[str, Any] | None,
    classes: dict[str, list[str]],
) -> None:
    if manifest is None:
        return
    declared = manifest.get("files")
    if not isinstance(declared, dict) or not all(
        isinstance(path, str) and isinstance(digest, str)
        for path, digest in declared.items()
    ):
        raise ContractError("bundle manifest files must map paths to SHA-256 digests")
    if set(declared) != set(files):
        missing = sorted(set(files) - set(declared))
        extra = sorted(set(declared) - set(files))
        raise ContractError(
            f"bundle manifest inventory differs; missing={missing}, extra={extra}"
        )
    mismatches = [
        path for path, source in files.items() if sha256_file(source) != declared[path]
    ]
    if mismatches:
        raise ContractError(f"bundle digest mismatch: {', '.join(mismatches)}")

    classifications = manifest.get("classifications")
    if classifications is not None:
        if not isinstance(classifications, dict) or set(classifications) != set(files):
            raise ContractError(
                "bundle manifest classifications must cover the exact declared inventory"
            )
        classification_mismatches = [
            path
            for path in files
            if classifications.get(path) not in classify(path, classes)
            or classifications.get(path) not in IMPORTABLE_CLASSES
        ]
        if classification_mismatches:
            raise ContractError(
                "bundle classification mismatch: "
                + ", ".join(classification_mismatches)
            )

    sizes = manifest.get("sizes")
    observed_sizes = {path: source.stat().st_size for path, source in files.items()}
    if sizes is not None:
        if not isinstance(sizes, dict) or sizes != observed_sizes:
            raise ContractError("bundle manifest sizes differ from the declared files")

    summary = manifest.get("summary")
    if summary is not None:
        if not isinstance(summary, dict):
            raise ContractError("bundle manifest summary must be a mapping")
        expected_summary: dict[str, Any] = {
            "bytes": sum(observed_sizes.values()),
            "files": len(files),
        }
        if classifications is not None:
            expected_summary["classes"] = {
                name: sum(value == name for value in classifications.values())
                for name in sorted(set(classifications.values()))
            }
        for key, expected in expected_summary.items():
            if summary.get(key) != expected:
                raise ContractError(
                    f"bundle manifest summary.{key} must be {expected!r}, "
                    f"not {summary.get(key)!r}"
                )


def destination_conflicts(root: Path, files: dict[str, Path]) -> list[str]:
    conflicts: list[str] = []
    for relative in files:
        destination = root / relative
        if destination.exists() and destination.name != ".gitkeep":
            conflicts.append(relative)
    return conflicts


def remove_placeholder(destination: Path) -> None:
    placeholder = destination.parent / ".gitkeep"
    if placeholder.is_file():
        placeholder.unlink()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("bundle", type=Path)
    result.add_argument("--source-repository")
    result.add_argument("--source-commit")
    result.add_argument("--scope")
    result.add_argument(
        "--replace",
        action="store_true",
        help="replace same-path site-owned files; never prunes unrelated files",
    )
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--root", type=Path)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = find_root(args.root)
    bundle = args.bundle.resolve()
    if not bundle.is_dir():
        raise ContractError(f"bundle directory does not exist: {bundle}")
    manifest_path = bundle / "orinoco-site-bundle.json"
    manifest = load_manifest(manifest_path) if manifest_path.is_file() else None
    ownership = load_yaml(root / "template-ownership.yml")
    classes = ownership_classes(ownership)
    files = source_files(bundle, classes)
    verify_manifest(files, manifest, classes)
    repository, commit, scope = resolve_provenance(args, manifest)

    conflicts = destination_conflicts(root, files)
    if manifest is not None and (root / manifest_path.name).exists():
        conflicts.append(manifest_path.name)
        conflicts.sort()
    if conflicts and not args.replace:
        raise ContractError(
            "refusing to overwrite existing site-owned files; pass --replace after "
            f"review: {', '.join(conflicts)}"
        )

    imported = [
        {
            "path": relative,
            "sha256": sha256_file(source),
            "size": source.stat().st_size,
        }
        for relative, source in files.items()
    ]
    ledger = {
        "ledger_version": 1,
        "operation": "full-site-import",
        "created_at": timestamp(),
        "source": {
            "repository": repository,
            "commit": commit,
            "scope": scope,
            "manifest": manifest_path.name if manifest else None,
            "manifest_sha256": sha256_file(manifest_path) if manifest else None,
            "declared_files": len(files),
        },
        "replace_existing": bool(args.replace),
        "files": imported,
    }
    if args.dry_run:
        return ledger

    for relative, source in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
        remove_placeholder(destination)

    if manifest is not None:
        manifest_destination = root / manifest_path.name
        shutil.copyfile(manifest_path, manifest_destination)
        shutil.copymode(manifest_path, manifest_destination)

    provenance = root / "metadata" / "provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    ledger_path = provenance / f"site-import-{commit[:12]}.json"
    serialized = json.dumps(ledger, indent=2, sort_keys=True) + "\n"
    if ledger_path.exists() and ledger_path.read_text(encoding="utf-8") != serialized:
        raise ContractError(f"import ledger already exists with different content: {ledger_path}")
    ledger_path.write_text(serialized, encoding="utf-8")
    remove_placeholder(ledger_path)
    return ledger


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        ledger = run(args)
    except ContractError as error:
        print(f"site import failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "dry-run" if args.dry_run else "imported",
                "files": len(ledger["files"]),
                "source": ledger["source"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
