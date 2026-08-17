#!/usr/bin/env python3
"""Install the locked browser suite with a macOS 14 WebKit compatibility overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


MACOS_14_PLAYWRIGHT = "1.61.1"
LOCKED_PLAYWRIGHT = "1.62.1"
EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TRACKED_FILES = ("package.json", "package-lock.json")
INSTALLED_PACKAGES = {
    "@playwright/test": Path("node_modules/@playwright/test/package.json"),
    "playwright": Path("node_modules/playwright/package.json"),
    "playwright-core": Path("node_modules/playwright-core/package.json"),
}
Runner = Callable[[Sequence[str], Path], int]


class BrowserInstallError(RuntimeError):
    """Raised when the browser dependency boundary cannot be proven."""


def load_json_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrowserInstallError(f"{label} is not valid JSON") from error
    if not isinstance(document, dict):
        raise BrowserInstallError(f"{label} must contain a JSON object")
    return document


def tracked_snapshot(browser_directory: Path) -> dict[str, bytes]:
    """Read the consumer-owned npm inputs without following symbolic links."""

    snapshot: dict[str, bytes] = {}
    for relative in TRACKED_FILES:
        path = browser_directory / relative
        if path.is_symlink() or not path.is_file():
            raise BrowserInstallError(
                f"browser dependency input must be a regular file: {relative}"
            )
        snapshot[relative] = path.read_bytes()
    return snapshot


def snapshot_digest(snapshot: dict[str, bytes]) -> str:
    """Return a stable digest over names and exact tracked bytes."""

    digest = hashlib.sha256()
    for relative in sorted(snapshot):
        name = relative.encode("utf-8")
        content = snapshot[relative]
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def declared_playwright_version(snapshot: dict[str, bytes]) -> str:
    """Prove that manifest and lock agree on one exact Playwright version."""

    manifest = load_json_bytes(snapshot["package.json"], "package.json")
    lock = load_json_bytes(snapshot["package-lock.json"], "package-lock.json")
    dev_dependencies = manifest.get("devDependencies")
    if not isinstance(dev_dependencies, dict):
        raise BrowserInstallError("package.json devDependencies must be an object")
    version = dev_dependencies.get("@playwright/test")
    if not isinstance(version, str) or EXACT_VERSION.fullmatch(version) is None:
        raise BrowserInstallError(
            "package.json must pin @playwright/test to an exact three-part version"
        )
    if version != LOCKED_PLAYWRIGHT:
        raise BrowserInstallError(
            f"checked browser inputs must remain on Playwright {LOCKED_PLAYWRIGHT}; "
            f"found {version}"
        )

    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise BrowserInstallError("package-lock.json packages must be an object")
    root = packages.get("")
    if not isinstance(root, dict) or not isinstance(
        root.get("devDependencies"), dict
    ):
        raise BrowserInstallError(
            "package-lock.json must declare root devDependencies"
        )
    if root["devDependencies"].get("@playwright/test") != version:
        raise BrowserInstallError(
            "package.json and package-lock.json disagree on @playwright/test"
        )
    for package_name, relative in INSTALLED_PACKAGES.items():
        entry = packages.get(relative.parent.as_posix())
        if not isinstance(entry, dict) or entry.get("version") != version:
            raise BrowserInstallError(
                f"package-lock.json does not pin {package_name} to {version}"
            )
    return version


def selected_playwright_version(
    system: str | None = None,
    macos_version: str | None = None,
) -> str | None:
    """Select the macOS 14 overlay, or defer to the checked lock elsewhere."""

    system = platform.system() if system is None else system
    if system != "Darwin":
        return None
    macos_version = platform.mac_ver()[0] if macos_version is None else macos_version
    match = re.fullmatch(r"(?P<major>[0-9]+)(?:\.[0-9]+){0,2}", macos_version)
    if match is None:
        raise BrowserInstallError(
            f"cannot determine the Darwin macOS major version: {macos_version!r}"
        )
    if int(match.group("major")) == 14:
        return MACOS_14_PLAYWRIGHT
    return None


def subprocess_runner(command: Sequence[str], cwd: Path) -> int:
    return subprocess.run(list(command), cwd=cwd, check=False).returncode


def run_checked(command: Sequence[str], cwd: Path, runner: Runner) -> None:
    status = runner(command, cwd)
    if status:
        raise BrowserInstallError(
            f"{' '.join(command)} failed with status {status}"
        )


def installed_version(browser_directory: Path, package: str, relative: Path) -> str:
    path = browser_directory / relative
    if path.is_symlink() or not path.is_file():
        raise BrowserInstallError(f"installed {package} metadata is missing")
    document = load_json_bytes(path.read_bytes(), relative.as_posix())
    version = document.get("version")
    if not isinstance(version, str):
        raise BrowserInstallError(f"installed {package} version is missing")
    return version


def verify_installed_versions(browser_directory: Path, expected: str) -> None:
    failures = [
        f"{package}={observed}"
        for package, relative in INSTALLED_PACKAGES.items()
        if (observed := installed_version(browser_directory, package, relative))
        != expected
    ]
    if failures:
        raise BrowserInstallError(
            f"expected Playwright {expected}; installed " + ", ".join(failures)
        )


def changed_tracked_files(
    browser_directory: Path, before: dict[str, bytes]
) -> list[str]:
    changed: list[str] = []
    for relative, content in before.items():
        path = browser_directory / relative
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            changed.append(relative)
    return sorted(changed)


def restore_tracked_files(browser_directory: Path, before: dict[str, bytes]) -> None:
    for relative, content in before.items():
        path = browser_directory / relative
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def install_browser_tests(
    browser_directory: Path,
    *,
    system: str | None = None,
    macos_version: str | None = None,
    runner: Runner = subprocess_runner,
) -> str:
    """Install and verify Playwright while preserving consumer-owned inputs."""

    browser_directory = browser_directory.resolve()
    before = tracked_snapshot(browser_directory)
    before_digest = snapshot_digest(before)
    declared = declared_playwright_version(before)
    overlay = selected_playwright_version(system, macos_version)
    expected = overlay or declared
    failure: BrowserInstallError | None = None
    try:
        run_checked(["npm", "ci"], browser_directory, runner)
        if overlay is not None:
            run_checked(
                [
                    "npm",
                    "install",
                    "--no-save",
                    "--package-lock=false",
                    "--ignore-scripts",
                    f"@playwright/test@{overlay}",
                ],
                browser_directory,
                runner,
            )
        verify_installed_versions(browser_directory, expected)
    except BrowserInstallError as error:
        failure = error

    changed = changed_tracked_files(browser_directory, before)
    if changed:
        restore_tracked_files(browser_directory, before)
        mutation = BrowserInstallError(
            "npm changed consumer-owned browser inputs; restored: "
            + ", ".join(changed)
        )
        if failure is not None:
            raise mutation from failure
        raise mutation
    after = tracked_snapshot(browser_directory)
    if snapshot_digest(after) != before_digest:
        raise BrowserInstallError("browser dependency input digest changed")
    if failure is not None:
        raise failure
    return expected


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--browser-directory",
        type=Path,
        default=Path(".orinoco-lite/tests/browser"),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        version = install_browser_tests(args.browser_directory)
    except BrowserInstallError as error:
        print(f"browser dependency installation failed: {error}", file=sys.stderr)
        return 2
    print(f"browser dependencies verified with Playwright {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
