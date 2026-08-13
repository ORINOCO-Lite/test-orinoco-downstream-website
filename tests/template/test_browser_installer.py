from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_installer():
    path = ROOT / "tools" / "install_browser_tests.py"
    spec = importlib.util.spec_from_file_location("install_browser_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BrowserInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = load_installer()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="orinoco-browser-installer-test-"
        )
        self.browser = Path(self.temporary.name) / "browser"
        self.browser.mkdir()
        for name in self.installer.TRACKED_FILES:
            shutil.copy2(ROOT / "tests" / "browser" / name, self.browser / name)
        self.before = self.installer.tracked_snapshot(self.browser)
        self.commands: list[list[str]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_installed(self, version: str, *, core_version: str | None = None) -> None:
        for package, relative in self.installer.INSTALLED_PACKAGES.items():
            path = self.browser / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            observed = (
                core_version
                if package == "playwright-core" and core_version is not None
                else version
            )
            path.write_text(
                json.dumps({"name": package, "version": observed}),
                encoding="utf-8",
            )

    def successful_runner(self, command, cwd: Path) -> int:
        self.assertEqual(self.browser.resolve(), cwd)
        command = list(command)
        self.commands.append(command)
        version = (
            self.installer.MACOS_14_PLAYWRIGHT
            if command[:2] == ["npm", "install"]
            else "1.62.1"
        )
        self.write_installed(version)
        return 0

    def test_platform_selection_is_exact_and_fail_closed(self) -> None:
        self.assertEqual(
            "1.61.1",
            self.installer.selected_playwright_version("Darwin", "14.7.2"),
        )
        for system, version in (
            ("Linux", ""),
            ("Darwin", "13.7.8"),
            ("Darwin", "15.0"),
        ):
            with self.subTest(system=system, version=version):
                self.assertIsNone(
                    self.installer.selected_playwright_version(system, version)
                )
        for version in ("", "14-beta", "Darwin 14"):
            with self.subTest(malformed=version):
                with self.assertRaises(self.installer.BrowserInstallError):
                    self.installer.selected_playwright_version("Darwin", version)

    def test_other_platform_runs_only_locked_npm_ci(self) -> None:
        before_digest = self.installer.snapshot_digest(self.before)

        version = self.installer.install_browser_tests(
            self.browser,
            system="Linux",
            macos_version="",
            runner=self.successful_runner,
        )

        self.assertEqual("1.62.1", version)
        self.assertEqual([["npm", "ci"]], self.commands)
        after = self.installer.tracked_snapshot(self.browser)
        self.assertEqual(self.before, after)
        self.assertEqual(before_digest, self.installer.snapshot_digest(after))

    def test_macos_14_runs_the_exact_compatibility_overlay(self) -> None:
        version = self.installer.install_browser_tests(
            self.browser,
            system="Darwin",
            macos_version="14.7.2",
            runner=self.successful_runner,
        )

        self.assertEqual("1.61.1", version)
        self.assertEqual(
            [
                ["npm", "ci"],
                [
                    "npm",
                    "install",
                    "--no-save",
                    "--package-lock=false",
                    "--ignore-scripts",
                    "@playwright/test@1.61.1",
                ],
            ],
            self.commands,
        )
        self.installer.verify_installed_versions(self.browser, "1.61.1")
        self.assertEqual(
            self.before,
            self.installer.tracked_snapshot(self.browser),
        )

    def test_tracked_mutation_is_restored_and_rejected(self) -> None:
        def mutating_runner(command, cwd: Path) -> int:
            self.write_installed("1.62.1")
            (cwd / "package-lock.json").write_bytes(b"mutated\n")
            return 0

        with self.assertRaisesRegex(
            self.installer.BrowserInstallError,
            "restored: package-lock.json",
        ):
            self.installer.install_browser_tests(
                self.browser,
                system="Linux",
                macos_version="",
                runner=mutating_runner,
            )
        after = self.installer.tracked_snapshot(self.browser)
        self.assertEqual(self.before, after)
        self.assertEqual(
            self.installer.snapshot_digest(self.before),
            self.installer.snapshot_digest(after),
        )

    def test_checked_version_drift_fails_closed(self) -> None:
        manifest = json.loads((self.browser / "package.json").read_text())
        manifest["devDependencies"]["@playwright/test"] = "1.63.0"
        (self.browser / "package.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(
            self.installer.BrowserInstallError,
            "must remain on Playwright 1.62.1; found 1.63.0",
        ):
            self.installer.install_browser_tests(
                self.browser,
                system="Linux",
                macos_version="",
                runner=self.successful_runner,
            )
        self.assertEqual([], self.commands)

    def test_command_and_version_failures_stop_visibly(self) -> None:
        with self.assertRaisesRegex(
            self.installer.BrowserInstallError,
            "npm ci failed with status 9",
        ):
            self.installer.install_browser_tests(
                self.browser,
                system="Linux",
                macos_version="",
                runner=lambda command, cwd: 9,
            )

        def wrong_version_runner(command, cwd: Path) -> int:
            self.write_installed("1.62.1", core_version="9.9.9")
            return 0

        with self.assertRaisesRegex(
            self.installer.BrowserInstallError,
            "playwright-core=9.9.9",
        ):
            self.installer.install_browser_tests(
                self.browser,
                system="Linux",
                macos_version="",
                runner=wrong_version_runner,
            )
        self.assertEqual(
            self.before,
            self.installer.tracked_snapshot(self.browser),
        )


if __name__ == "__main__":
    unittest.main()
