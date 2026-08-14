#!/usr/bin/env python3
"""Create the dump-research-info evidence commit with ``datalad run``."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
from typing import Sequence


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path("integrations/dump-research-info/source/con-site-gap")


class DataLadRunError(RuntimeError):
    """Report an unsafe or unreproducible provenance run."""


def git_output(root: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments],
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise DataLadRunError(error.stderr.strip() or "Git inspection failed") from error


def require_clean(root: Path, label: str) -> None:
    if git_output(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise DataLadRunError(f"{label} checkout must be clean before datalad run")


def recorded_command(
    source: Path,
    source_commit: str,
    downstream_commit: str,
    *,
    root: Path = ROOT,
) -> str:
    source_argument = Path(os.path.relpath(source, root)).as_posix()
    return shlex.join(
        [
            "./integrations/metadata/metadata-review",
            "extract-dump-research-info",
            "--",
            "--source",
            source_argument,
            "--expected-source-commit",
            source_commit,
            "--downstream-revision",
            downstream_commit,
            "--output",
            EVIDENCE.as_posix(),
        ]
    )


def datalad_arguments(source: Path, *, root: Path = ROOT) -> list[str]:
    source = source.expanduser().resolve()
    if not source.is_dir() or source.is_symlink():
        raise DataLadRunError(f"Source is not an ordinary checkout: {source}")
    require_clean(root, "Downstream")
    require_clean(source, "dump-research-info")
    source_commit = git_output(source, "rev-parse", "HEAD^{commit}")
    downstream_commit = git_output(root, "rev-parse", "HEAD^{commit}")
    command = recorded_command(
        source, source_commit, downstream_commit, root=root
    )
    return [
        "datalad",
        "run",
        "--explicit",
        "-m",
        f"review dump-research-info con_site at {source_commit[:12]}",
        "-i",
        "integrations/dump-research-info/metadata_adapter.py",
        "-i",
        "integrations/dump-research-info/run_with_datalad.py",
        "-i",
        "integrations/metadata",
        "-i",
        "metadata/records",
        "-i",
        "metadata/reference",
        "-o",
        EVIDENCE.as_posix(),
        command,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    command = datalad_arguments(args.source)
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except FileNotFoundError as error:
        raise DataLadRunError(
            "datalad is unavailable; run this task through metadata-review's Pixi lock"
        ) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
