from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_verifier():
    path = ROOT / "tools" / "verify_local_preview.py"
    spec = importlib.util.spec_from_file_location("verify_local_preview", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = load_verifier()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="orinoco-local-preview-test-"
        )
        self.site = Path(self.temporary.name) / "site"
        (self.site / "about").mkdir(parents=True)
        (self.site / "css").mkdir()
        (self.site / "js").mkdir()
        (self.site / "index.html").write_text(
            "<html><head>"
            '<link rel="canonical" href="/">'
            '<link rel="stylesheet" href="/css/site.css">'
            '<script src="/js/site.js"></script>'
            "</head><body>"
            '<a href="/about/">About</a>'
            "</body></html>",
            encoding="utf-8",
        )
        (self.site / "about" / "index.html").write_text(
            '<a href="/">Home</a>\n', encoding="utf-8"
        )
        (self.site / "css" / "site.css").write_text(
            "body { color: black; }\n", encoding="utf-8"
        )
        (self.site / "js" / "site.js").write_text(
            "window.preview = true;\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_one_artifact_works_through_both_loopback_names(self) -> None:
        report = self.verifier.verify_local_preview(self.site)

        self.assertEqual(["127.0.0.1", "localhost"], report["hostnames"])
        self.assertEqual(4, report["files"])
        self.assertEqual(
            report["references"]["127.0.0.1"],
            report["references"]["localhost"],
        )
        self.assertGreaterEqual(report["references"]["localhost"], 4)

    def test_baked_loopback_origins_fail_before_serving(self) -> None:
        index = self.site / "index.html"
        for origin in (
            "http://127.0.0.1:8765/",
            "http://localhost:8765/",
            "http://[::1]:8765/",
        ):
            with self.subTest(origin=origin):
                index.write_text(f'<a href="{origin}">bad</a>\n', encoding="utf-8")
                with self.assertRaisesRegex(
                    self.verifier.LocalPreviewError,
                    "embeds a loopback origin: index.html",
                ):
                    self.verifier.verify_local_preview(self.site)

    def test_missing_same_origin_resource_fails_visibly(self) -> None:
        (self.site / "index.html").write_text(
            '<link rel="stylesheet" href="/missing.css">\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.verifier.LocalPreviewError,
            "local preview request failed",
        ):
            self.verifier.verify_local_preview(self.site)


if __name__ == "__main__":
    unittest.main()
