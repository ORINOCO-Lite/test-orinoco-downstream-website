from __future__ import annotations

import importlib.util
import importlib
import tempfile
import sys
import tomllib
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, TOOLS.as_posix())


def load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_template_ownership", TOOLS / "verify_template_ownership.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DownstreamContractTests(unittest.TestCase):
    def test_repository_has_no_submodule_contract(self) -> None:
        self.assertFalse((ROOT / ".gitmodules").exists())

    def test_every_checked_path_has_unambiguous_ownership(self) -> None:
        failures = load_verifier().verify(ROOT)
        self.assertEqual([], failures, "\n".join(failures))

    def test_site_owned_paths_are_present(self) -> None:
        for path in (
            "metadata/records",
            "metadata/reference",
            "metadata/provenance",
            "editorial",
            "assets",
            "site",
            "integrations",
            "extensions",
            "generated/manifests",
        ):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_dir())

    def test_offline_acceptance_is_site_owned(self) -> None:
        verifier = load_verifier()
        ownership = verifier.load_yaml(ROOT / "template-ownership.yml")
        classes = verifier.ownership_classes(ownership)
        self.assertEqual(
            ["consumer_tests"],
            verifier.classify("tests/offline/test_no_network.py", classes),
        )
        self.assertEqual(
            ["initialized_site_owned"],
            verifier.classify("orinoco-site-bundle.json", classes),
        )

    def test_engine_configuration_uses_the_supported_path_contract(self) -> None:
        configuration = yaml.safe_load((ROOT / "orinoco.yaml").read_text(encoding="utf-8"))
        self.assertEqual(1, configuration["contract_version"])
        self.assertEqual("metadata/records", configuration["paths"]["canonical"])
        self.assertNotIn("records", configuration["paths"])

        # Run the actual engine loader when this downstream suite is executing
        # in the locked Orinoco environment. The structural assertions above
        # remain active in content-neutral template-source tests, where the
        # unpublished fail-closed wheel is intentionally unavailable.
        try:
            config_module = importlib.import_module("orinoco_lite.config")
        except ModuleNotFoundError:
            return
        loader = next(
            (
                getattr(config_module, name)
                for name in (
                    "load_config_path",
                    "load_config",
                    "load_site_config",
                    "load_configuration",
                )
                if hasattr(config_module, name)
            ),
            None,
        )
        if loader is None:
            self.fail("orinoco_lite.config exposes no supported configuration loader")
        loaded = loader(ROOT / "orinoco.yaml")
        self.assertIsNotNone(loaded)

    def test_complete_gate_includes_browser_and_deterministic_acceptance(self) -> None:
        pixi = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
        self.assertEqual("==0.154.5", pixi["dependencies"]["hugo"])
        tasks = pixi["tasks"]
        self.assertEqual("orinoco projection update", tasks["projection-update"])
        self.assertEqual("orinoco projection verify", tasks["projection-verify"])
        self.assertEqual("orinoco assets hydrate", tasks["assets-hydrate"])
        self.assertEqual("orinoco assets verify", tasks["assets-verify"])
        self.assertEqual(
            ["assets-hydrate"],
            tasks["assets-prepare-online"]["depends-on"],
        )
        self.assertEqual(
            "orinoco assets verify",
            tasks["assets-prepare-online"]["cmd"],
        )
        self.assertIn("projection-verify", tasks["test-all"]["depends-on"])
        self.assertIn("assets-prepare-online", tasks["test-all"]["depends-on"])
        self.assertNotIn("assets-verify", tasks["test-all"]["depends-on"])
        self.assertIn("test-browser", tasks["test-all"]["depends-on"])
        self.assertIn("verify-deterministic", tasks["test-all"]["depends-on"])
        self.assertIn("verify-hugo", tasks["test-all"]["depends-on"])

    def test_runtime_state_is_wholly_ignored(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".orinoco/", ignore)
        self.assertNotIn("!.orinoco/downloads/", ignore)

    def test_engine_digest_uses_pixi_direct_url_schema(self) -> None:
        pixi = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
        engine = yaml.safe_load((ROOT / "orinoco.lock").read_text(encoding="utf-8"))[
            "engine"
        ]
        requirement = pixi["pypi-dependencies"]["orinoco-lite"]["url"]
        self.assertEqual(
            f"{engine['url']}#sha256={engine['sha256']}", requirement
        )
        lock = yaml.safe_load((ROOT / "pixi.lock").read_text(encoding="utf-8"))
        matches = [
            package
            for package in lock["packages"]
            if package.get("name") == "orinoco-lite"
        ]
        self.assertEqual(1, len(matches))
        self.assertEqual(engine["version"], matches[0]["version"])
        self.assertEqual("direct+" + requirement, matches[0]["pypi"])

    def test_workflows_use_the_lock_generator_pixi_version(self) -> None:
        workflows = ROOT / ".github" / "workflows"
        for name in ("pages.yml", "update-orinoco.yml", "validate.yml"):
            text = (workflows / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertIn("pixi-version: v0.73.0", text)

    def test_consumer_workflow_jobs_are_inert_in_template_repository(self) -> None:
        workflows = ROOT / ".github" / "workflows"
        for name in ("pages.yml", "update-orinoco.yml", "validate.yml"):
            document = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
            for job_name, job in document["jobs"].items():
                with self.subTest(workflow=name, job=job_name):
                    self.assertIn(
                        "github.repository != 'con/orinoco-lite-template'",
                        str(job.get("if", "")),
                    )

    def test_update_workflow_commit_has_required_agent_attribution(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "update-orinoco.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        self.assertIn("\n  workflow_dispatch:\n", workflow_text)
        self.assertNotIn("\n  schedule:\n", workflow_text)
        self.assertNotIn("cron:", workflow_text)
        pull_request = workflow["jobs"]["update"]["steps"][-1]
        self.assertEqual(
            "chore(deps): update Orinoco framework\n\n"
            "Co-Authored-By: Codex CLI 0.143.0 / GPT 5.6-sol "
            "<codex@openai.com>\n",
            pull_request["with"]["commit-message"],
        )
        self.assertTrue(
            pull_request["with"]["body"].startswith(
                "**AI-generated draft — not reviewed by John**\n"
            )
        )

    def test_deterministic_comparator_uses_exact_inventory_and_digests(self) -> None:
        comparator = load_tool("verify_deterministic_build")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = workspace / "first"
            second = workspace / "second"
            first.mkdir()
            second.mkdir()
            (first / "index.html").write_bytes(b"same\n")
            (second / "index.html").write_bytes(b"same\n")
            manifest, differences = comparator.compare(first, second)
            self.assertEqual([], differences)
            self.assertEqual(1, manifest["file_count"])
            self.assertEqual(64, len(manifest["tree_sha256"]))
            (second / "index.html").write_bytes(b"different\n")
            _, differences = comparator.compare(first, second)
            self.assertEqual(["index.html"], differences)


if __name__ == "__main__":
    unittest.main()
