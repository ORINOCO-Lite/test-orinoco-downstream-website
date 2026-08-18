#!/usr/bin/env python3
"""Prepare the checked browser runtime in bounded, observable phases."""

from __future__ import annotations

import argparse
import math
import os
import platform
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


DEFAULT_DEPENDENCY_TIMEOUT_SECONDS = 300
DEFAULT_BROWSER_TIMEOUT_SECONDS = 600
DEFAULT_TERMINATION_GRACE_SECONDS = 5
CHECKED_BROWSERS = ("chromium", "webkit")
Runner = Callable[[Sequence[str], Path, float, str], int]


class BrowserRuntimeError(RuntimeError):
    """Raised when the checked browser runtime cannot be prepared."""


def terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    """Terminate the phase and its descendants, escalating after a short grace."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def bounded_subprocess_runner(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    label: str,
    *,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> int:
    """Run one visible phase and stop its whole process group on timeout."""

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        terminate_process_group(process, termination_grace_seconds)
        raise BrowserRuntimeError(
            f"{label} exceeded its {timeout_seconds:g}-second timeout"
        ) from error


def run_phase(
    label: str,
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    runner: Runner,
) -> None:
    command_text = " ".join(command)
    print(
        f"[browser-runtime] starting {label} "
        f"(timeout: {timeout_seconds:g}s): {command_text}",
        flush=True,
    )
    try:
        status = runner(command, cwd, timeout_seconds, label)
    except BrowserRuntimeError:
        raise
    except Exception as error:
        raise BrowserRuntimeError(f"{label} could not start: {error}") from error
    if status:
        raise BrowserRuntimeError(f"{label} failed with status {status}")
    print(f"[browser-runtime] completed {label}", flush=True)


def browser_context(
    browser_directory: Path,
    project_directory: Path | None = None,
) -> tuple[Path, str]:
    """Resolve the project cwd and npm prefix without moving the cache root."""

    project_directory = (
        Path.cwd() if project_directory is None else project_directory
    ).resolve()
    unresolved_browser_directory = (
        browser_directory
        if browser_directory.is_absolute()
        else project_directory / browser_directory
    )
    if unresolved_browser_directory.is_symlink():
        raise BrowserRuntimeError(
            "browser test directory must not be a symbolic link: "
            f"{unresolved_browser_directory}"
        )
    browser_directory = unresolved_browser_directory.resolve()
    if not browser_directory.is_dir():
        raise BrowserRuntimeError(
            f"browser test directory must be a regular directory: {browser_directory}"
        )
    try:
        browser_prefix = browser_directory.relative_to(project_directory).as_posix()
    except ValueError:
        browser_prefix = browser_directory.as_posix()
    return project_directory, browser_prefix


def checked_timeout(seconds: float) -> float:
    if not math.isfinite(seconds) or seconds <= 0:
        raise BrowserRuntimeError(
            "phase timeout must be a finite number greater than zero"
        )
    return seconds


def prepare_browser_binaries(
    browser_directory: Path,
    *,
    project_directory: Path | None = None,
    timeout_seconds: float = DEFAULT_BROWSER_TIMEOUT_SECONDS,
    runner: Runner = bounded_subprocess_runner,
) -> None:
    """Prepare Chromium headless-shell and WebKit before either test suite."""

    timeout_seconds = checked_timeout(timeout_seconds)
    project_directory, browser_prefix = browser_context(
        browser_directory, project_directory
    )
    run_phase(
        "Chromium headless-shell and WebKit download",
        [
            "npx",
            "--prefix",
            browser_prefix,
            "playwright",
            "install",
            "--only-shell",
            *CHECKED_BROWSERS,
        ],
        project_directory,
        timeout_seconds,
        runner,
    )


def prepare_linux_host_dependencies(
    browser_directory: Path,
    *,
    project_directory: Path | None = None,
    system: str | None = None,
    timeout_seconds: float = DEFAULT_DEPENDENCY_TIMEOUT_SECONDS,
    runner: Runner = bounded_subprocess_runner,
) -> None:
    """Prepare WebKit host libraries after Chromium has finished."""

    timeout_seconds = checked_timeout(timeout_seconds)
    project_directory, browser_prefix = browser_context(
        browser_directory, project_directory
    )

    system = platform.system() if system is None else system
    if system == "Linux":
        run_phase(
            "Linux WebKit host dependencies",
            [
                "npx",
                "--prefix",
                browser_prefix,
                "playwright",
                "install-deps",
                "webkit",
            ],
            project_directory,
            timeout_seconds,
            runner,
        )
    elif system == "Darwin":
        print(
            "[browser-runtime] Linux WebKit host dependencies skipped on Darwin",
            flush=True,
        )
    else:
        raise BrowserRuntimeError(f"unsupported browser test platform: {system}")


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return seconds


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "phase",
        choices=("browsers", "linux-host-dependencies"),
    )
    result.add_argument(
        "--browser-directory",
        type=Path,
        default=Path(".orinoco-lite/tests/browser"),
    )
    result.add_argument(
        "--dependency-timeout-seconds",
        type=positive_seconds,
        default=DEFAULT_DEPENDENCY_TIMEOUT_SECONDS,
    )
    result.add_argument(
        "--browser-timeout-seconds",
        type=positive_seconds,
        default=DEFAULT_BROWSER_TIMEOUT_SECONDS,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.phase == "browsers":
            prepare_browser_binaries(
                args.browser_directory,
                timeout_seconds=args.browser_timeout_seconds,
            )
        else:
            prepare_linux_host_dependencies(
                args.browser_directory,
                timeout_seconds=args.dependency_timeout_seconds,
            )
    except BrowserRuntimeError as error:
        print(f"browser runtime preparation failed: {error}", file=sys.stderr)
        return 2
    print("browser runtime preparation complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
