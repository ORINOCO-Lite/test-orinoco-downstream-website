from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.parity.site_bundle_inventory import IGNORED_PARTS, annex_pointer_key


ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "generated" / "manifests"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FullFidelityConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = load_json(MANIFESTS / "full-fidelity.json")
        cls.source_import = load_json(MANIFESTS / "source-import.json")
        cls.framework_import = load_json(MANIFESTS / "framework-import.json")
        cls.projection_import = load_json(
            MANIFESTS / "projection-input-import.json"
        )
        cls.theme_import = load_json(MANIFESTS / "theme-import.json")
        cls.zotero_import = load_json(MANIFESTS / "zotero-import.json")
        cls.traceability = load_json(ROOT / "tests" / "traceability.json")

    def assert_manifest_files(self, entries: list[dict]) -> None:
        for entry in entries:
            path = ROOT / entry["path"]
            self.assertTrue(path.is_file(), entry["path"])
            self.assertFalse(path.is_symlink(), entry["path"])
            self.assertEqual(sha256(path), entry["sha256"], entry["path"])
            self.assertEqual(path.stat().st_size, entry["size"], entry["path"])

    def test_complete_snapshot_has_no_selection_filter(self) -> None:
        self.assertEqual(
            self.contract["sources"]["site"]["commit"],
            "26907c487efaa2c31bba9d02398aa201ab6f774b",
        )
        self.assertEqual(self.contract["selection"]["mode"], "all")
        self.assertIsNone(self.contract["selection"]["filter"])
        active = load_json(
            ROOT / self.contract["selection"]["active_provenance"]
        )
        self.assertEqual(
            active["selection_policy"], {"filter": None, "mode": "all"}
        )
        self.assertEqual(active["source"]["scope"], "full")
        self.assertIn(
            "orinoco-site-bundle.json", active["active_contracts"]
        )
        self.assertEqual(
            active["historical_evidence"][0]["path"],
            "metadata/provenance/selection.yaml",
        )
        self.assertEqual(
            active["historical_evidence"][0]["status"],
            "superseded-historical-evidence-only",
        )
        historical = self.contract["selection"]["historical_selection_ledger"]
        self.assertEqual(historical["policy_status"], "historical-evidence-only")
        self.assertIn(
            "not an active consumer filter", historical["explanation"]
        )
        self.assertIn(
            "not a selection policy",
            (ROOT / "metadata/provenance/README.md").read_text(
                encoding="utf-8"
            ),
        )
        self.assertNotIn(
            "selection.yaml",
            (ROOT / "tests/parity/site_bundle_inventory.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_exact_canonical_and_reference_inventory(self) -> None:
        canonical = self.contract["canonical"]["records"]
        references = self.contract["reference"]["records"]
        self.assertEqual(len(canonical), 186)
        self.assertEqual(len(references), 13)
        self.assertEqual(
            self.contract["canonical"]["class_counts"],
            {
                "XYZInstrument": 1,
                "XYZOrganization": 1,
                "XYZPerson": 33,
                "XYZProject": 24,
                "XYZPublication": 126,
                "XYZTopic": 1,
            },
        )
        all_pids = [entry["pid"] for entry in canonical + references]
        self.assertEqual(len(all_pids), len(set(all_pids)))
        self.assert_manifest_files(canonical)
        self.assert_manifest_files(references)

    def test_editorial_and_provenance_are_complete(self) -> None:
        editorial = self.contract["editorial"]["files"]
        ledgers = self.contract["provenance"]["ledgers"]
        self.assertEqual(len(editorial), 10)
        self.assertEqual(len(ledgers), 7)
        self.assert_manifest_files(editorial)
        self.assert_manifest_files(ledgers)
        self.assertEqual(
            {Path(entry["path"]).name for entry in ledgers},
            {
                "assets.yaml",
                "editorial.yaml",
                "people.yaml",
                "projection.yaml",
                "projects.yaml",
                "publications.yaml",
                "selection.yaml",
            },
        )

    def test_committed_projection_is_complete_and_closed(self) -> None:
        projection = self.contract["projection"]
        self.assertEqual(projection["record_count"], 199)
        self.assertEqual(projection["page_count"], 185)
        self.assertEqual(projection["graph_nodes"], 186)
        self.assertEqual(projection["graph_edges"], 467)
        self.assertEqual(len(projection["files"]), 189)
        self.assert_manifest_files(projection["files"])

        records = {
            json.loads(line)["pid"]
            for line in (
                ROOT / "generated/projection/records.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        expected = {
            item["pid"]
            for key in ("canonical", "reference")
            for item in self.contract[key]["records"]
        }
        self.assertEqual(records, expected)

        graph = load_json(ROOT / "generated/projection/static/graph.json")
        graph_pids = {node["id"] for node in graph["nodes"]}
        canonical_pids = {
            item["pid"] for item in self.contract["canonical"]["records"]
        }
        self.assertEqual(graph_pids, canonical_pids)
        self.assertNotIn("PsyInf", {node["label"] for node in graph["nodes"]})
        self.assertNotIn("FZJ", {node["label"] for node in graph["nodes"]})

        inputs = self.contract["projection_inputs"]
        self.assertEqual(inputs["selection_filter"], None)
        self.assertEqual(
            inputs["accepted_site_commit"],
            "26907c487efaa2c31bba9d02398aa201ab6f774b",
        )
        self.assertEqual(
            self.projection_import["counts"],
            {
                "byte_identical_files": 11,
                "files": 12,
                "transformed_files": 1,
            },
        )
        for entry in self.projection_import["entries"]:
            target = ROOT / entry["target_path"]
            self.assertTrue(target.is_file(), entry["target_path"])
            self.assertEqual(sha256(target), entry["target_sha256"])
            if entry["disposition"] != "flattened-v2-contract":
                self.assertEqual(
                    entry["source_sha256"], entry["target_sha256"]
                )
        templates = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "site/projection-templates").glob("*.j2")
        }
        self.assertEqual(
            templates,
            {
                "site/projection-templates/dataset.md.j2",
                "site/projection-templates/homepage.md.j2",
                "site/projection-templates/instrument.md.j2",
                "site/projection-templates/objective.md.j2",
                "site/projection-templates/page.md.j2",
                "site/projection-templates/person.md.j2",
                "site/projection-templates/project.md.j2",
                "site/projection-templates/publication.md.j2",
                "site/projection-templates/topic.md.j2",
            },
        )
        projection_contract = (
            ROOT / "site/projection.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("version: 2", projection_contract)
        self.assertIn(
            "producer: site/projection-tools/pool2graph.py",
            projection_contract,
        )
        self.assertNotIn("profiles/con", projection_contract)
        projection = yaml.safe_load(projection_contract)
        self.assertEqual(
            projection["routing"], {"strip_prefix": "xyzrins:"}
        )
        reverse = [
            {"from": "generated_by", "to": "generated"},
            {"from": "part_of", "to": "parts"},
        ]
        project_inline = [
            "associated_with",
            "associated_with::roles",
            "influenced_by",
            "influenced_by::roles",
            "identifiers::creator",
            "part_of",
        ]
        self.assertEqual(
            projection["homepage"],
            {
                "pid": "xyzrins:.",
                "template": (
                    "site/projection-templates/homepage.md.j2"
                ),
                "reverse_injections": reverse,
                "inline": project_inline,
            },
        )
        pages = projection["pages"]
        self.assertEqual(
            pages["xyzri:XYZPerson"]["select"],
            {
                "linked_from": {
                    "pid": "xyzrins:.",
                    "field": "associated_with",
                }
            },
        )
        self.assertEqual(
            pages["xyzri:XYZProject"]["select"],
            {
                "links_to": {
                    "field": "part_of",
                    "pid": "xyzrins:.",
                    "recursive": True,
                }
            },
        )
        self.assertEqual(
            pages["xyzri:XYZProject"]["reverse_injections"], reverse
        )
        self.assertEqual(
            pages["xyzri:XYZProject"]["inline"], project_inline
        )
        expected_inline = {
            "xyzri:XYZDataset": [
                "about",
                "attributed_to",
                "kind",
                "rules",
                "characterized_by",
            ],
            "xyzri:XYZObjective": ["part_of", "depends_on"],
            "xyzri:XYZTopic": ["part_of"],
            "xyzri:XYZPerson": [
                "delegated_by",
                "delegated_by::roles",
                "identifiers::creator",
            ],
            "xyzri:XYZPublication": ["about", "attributed_to"],
            "xyzri:XYZInstrument": [
                "about",
                "attributed_to",
                "kind",
                "rules",
            ],
        }
        for schema_type, inlines in expected_inline.items():
            self.assertEqual(pages[schema_type]["inline"], inlines)
        semantics = self.projection_import["selection_semantics"]
        self.assertEqual(
            semantics["source_commit"],
            "a9ac9d5abc3898fd13d9b8392008f0c323c8dcd8",
        )
        self.assertEqual(
            semantics["source_path"],
            ".forgejo/workflows/update-from-pool.yaml",
        )
        self.assertEqual(
            semantics["representation"],
            "type-neutral-declarative-v2",
        )
        historical = (
            ROOT
            / "generated/projection/provenance/milestone-3-SHA256SUMS"
        )
        self.assertTrue(historical.is_file())
        active_digest = ROOT / "generated/projection/SHA256SUMS"
        if active_digest.exists():
            digest_text = active_digest.read_text(encoding="utf-8")
            self.assertTrue(
                digest_text.startswith(
                    "# orinoco-lite projection manifest v2\n"
                )
            )
            self.assertNotIn("profiles/con", digest_text)

    def test_every_source_overlay_entry_has_one_explicit_disposition(self) -> None:
        entries = self.source_import["entries"]
        self.assertEqual(len(entries), 501)
        self.assertEqual(
            self.source_import["selection"], {"filter": None, "mode": "all"}
        )
        self.assertEqual(
            self.source_import["mapping_counts"],
            {
                "byte_identical": 482,
                "digest_hydration_contracts": 16,
                "flattened_theme_imports": 1,
                "layout_transforms": 2,
                "total": 501,
            },
        )
        self.assertEqual(
            len({entry["source_path"] for entry in entries}), len(entries)
        )
        for entry in entries:
            target = ROOT / entry["target_path"]
            if entry["disposition"] == "flattened-theme-import":
                self.assertEqual(entry["source_mode"], "160000")
                self.assertEqual(
                    entry["source_object"],
                    "3623fa505ee42fee899844d94a4ff7f5a1ae9096",
                )
                self.assertTrue(target.is_dir())
                self.assertEqual(
                    entry["target_manifest"],
                    "generated/manifests/theme-import.json",
                )
            elif entry["disposition"] == "digest-hydration-contract":
                self.assertEqual(entry["source_mode"], "120000")
                self.assertFalse(entry["target_present"])
                self.assertFalse(target.is_symlink(), entry["target_path"])
            else:
                self.assertTrue(target.is_file(), entry["target_path"])
                self.assertEqual(
                    sha256(target), entry["target_sha256"], entry["target_path"]
                )
                if entry["disposition"] == "byte-identical-copy":
                    self.assertEqual(
                        entry["source_sha256"], entry["target_sha256"]
                    )

    def test_accepted_framework_presentation_is_exact_and_consumer_owned(self) -> None:
        self.assertEqual(
            self.framework_import["source"]["commit"],
            "26907c487efaa2c31bba9d02398aa201ab6f774b",
        )
        self.assertEqual(
            self.framework_import["source"]["scope"],
            ["config/_default", "archetypes", "layouts", "assets", "static"],
        )
        self.assertEqual(
            self.framework_import["counts"],
            {
                "annex_payload_materializations": 13,
                "byte_identical_files": 59,
                "bytes": 1_378_923,
                "files": 72,
                "modes": {"100644": 72},
            },
        )
        self.assertEqual(
            self.framework_import["materialization"],
            {
                "downstream_requirement": (
                    "ordinary verified bytes; git-annex is not required"
                ),
                "source": {
                    "commit": (
                        "6c8b9a5b7260dc20dfe1453dd863b353e8f90f06"
                    ),
                    "repository": (
                        "https://github.com/leej3/www-from-model.git"
                    ),
                    "role": "allowed-hydrated-read-only-mirror",
                },
                "verification": [
                    "source Git blob SHA-256",
                    "MD5E annex key payload size",
                    "MD5E annex key payload MD5",
                    "ordinary-Git target SHA-256",
                ],
            },
        )
        self.assertFalse(
            self.framework_import["license_boundary"]["runtime_redistribution"]
        )
        materialized = []
        for entry in self.framework_import["entries"]:
            path = ROOT / entry["target_path"]
            self.assertTrue(path.is_file(), entry["target_path"])
            self.assertFalse(path.is_symlink(), entry["target_path"])
            self.assertEqual(path.stat().st_size, entry["size"])
            self.assertEqual(sha256(path), entry["target_sha256"])
            if entry["disposition"] == "verified-annex-payload-materialization":
                materialized.append(entry)
                key = entry["annex_key"]
                backend, key_details = key.split("-s", 1)
                size_text, digest_with_extension = key_details.split("--", 1)
                payload_md5 = digest_with_extension.split(".", 1)[0]
                self.assertEqual(backend, "MD5E")
                self.assertEqual(entry["payload_size"], int(size_text))
                self.assertEqual(entry["payload_size"], entry["size"])
                self.assertEqual(entry["payload_md5"], payload_md5)
                self.assertEqual(
                    hashlib.md5(path.read_bytes()).hexdigest(), payload_md5
                )
                self.assertEqual(entry["target_storage"], "ordinary-git")
                self.assertNotEqual(
                    entry["source_sha256"], entry["target_sha256"]
                )
                source_pointer = f"/annex/objects/{key}\n".encode("ascii")
                self.assertEqual(len(source_pointer), entry["source_size"])
                self.assertEqual(
                    hashlib.sha256(source_pointer).hexdigest(),
                    entry["source_sha256"],
                )
                git_object = (
                    f"blob {len(source_pointer)}\0".encode("ascii")
                    + source_pointer
                )
                self.assertEqual(
                    hashlib.sha1(git_object).hexdigest(), entry["source_blob"]
                )
                self.assertEqual(
                    entry["source_representation"],
                    "git-annex-pointer-blob",
                )
                self.assertEqual(
                    entry["materialization_source"],
                    {
                        **self.framework_import["materialization"]["source"],
                        "source_path": entry["source_path"],
                    },
                )
                if path.suffix == ".png":
                    self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            else:
                self.assertEqual(
                    entry["disposition"],
                    "byte-identical-consumer-presentation",
                )
                self.assertEqual(
                    entry["source_sha256"], entry["target_sha256"]
                )
        self.assertEqual(len(materialized), 13)
        self.assertEqual(sum(entry["payload_size"] for entry in materialized), 1_258_562)
        css = ROOT / "site/framework/assets/css/compiled/main.css"
        self.assertTrue(
            css.read_bytes().startswith(
                b"/*! Congo v2.13.0 | MIT License | https://github.com/jpanther/congo */"
            )
        )
        graph_js = ROOT / "site/framework/static/graph.js"
        self.assertTrue(graph_js.read_bytes().startswith(b"(function(){"))
        self.assertIn(b'document.createElement("link")', graph_js.read_bytes())
        graph = load_json(ROOT / "site/framework/static/graph.json")
        self.assertIsInstance(graph["nodes"], list)
        self.assertIsInstance(graph["edges"], list)

        pointer_files = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if set(relative.parts) & IGNORED_PARTS:
                continue
            if path.is_file() and not path.is_symlink():
                key = annex_pointer_key(path)
                if key is not None:
                    pointer_files.append((relative.as_posix(), key))
        self.assertEqual(pointer_files, [])
        module = (ROOT / "site/config/module.toml").read_text(encoding="utf-8")
        for path in (
            "site/framework/assets",
            "site/framework/layouts",
            "site/framework/static",
        ):
            self.assertIn(f'source = "{path}"', module)

    def test_built_framework_runtime_contains_payloads_not_pointers(self) -> None:
        build_root = ROOT / "build"
        build_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="framework-runtime-", dir=build_root
        ) as temporary:
            destination = Path(temporary)
            result = subprocess.run(
                [
                    "orinoco",
                    "build",
                    "--destination",
                    str(destination),
                    "--base-url",
                    "http://127.0.0.1:8765/",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stderr or result.stdout,
            )

            pointer_files = []
            for path in destination.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    key = annex_pointer_key(path)
                    if key is not None:
                        pointer_files.append(
                            (path.relative_to(destination).as_posix(), key)
                        )
            self.assertEqual(pointer_files, [])

            stylesheets = list(
                (destination / "css").glob("main.bundle.min.*.css")
            )
            self.assertEqual(len(stylesheets), 1)
            compiled_css = stylesheets[0].read_bytes()
            self.assertGreater(len(compiled_css), 50_000)
            self.assertTrue(compiled_css.startswith(b":root{"))
            self.assertIn(b"--tw-border-spacing-x:0", compiled_css)

            target_digests = {
                Path(entry["target_path"]).name: entry["target_sha256"]
                for entry in self.framework_import["entries"]
                if entry["disposition"]
                == "verified-annex-payload-materialization"
            }
            for name in (
                "meerkat_person.png",
                "meerkat_project.png",
                "meerkat_topic.png",
            ):
                image = destination / "img" / name
                self.assertTrue(image.is_file(), name)
                self.assertTrue(image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(sha256(image), target_digests[name])

    def test_pinned_theme_is_flattened_with_distinct_mit_provenance(self) -> None:
        self.assertEqual(
            self.theme_import["source"],
            {
                "commit": "3623fa505ee42fee899844d94a4ff7f5a1ae9096",
                "repository": "https://github.com/leej3/congo.git",
                "scope": ["."],
            },
        )
        self.assertEqual(
            self.theme_import["counts"],
            {
                "bytes": 13_535_424,
                "files": 467,
                "modes": {"100644": 466, "100755": 1},
            },
        )
        self.assertEqual(self.theme_import["license"]["spdx"], "MIT")
        for entry in self.theme_import["entries"]:
            path = ROOT / entry["target_path"]
            self.assertTrue(path.is_file(), entry["target_path"])
            self.assertFalse(path.is_symlink(), entry["target_path"])
            self.assertEqual(sha256(path), entry["source_sha256"])
        declared = {
            entry["target_path"] for entry in self.theme_import["entries"]
        }
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "site/framework/themes/congo").rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, declared)
        license_path = ROOT / self.theme_import["license"]["path"]
        self.assertEqual(
            sha256(license_path), self.theme_import["license"]["sha256"]
        )

    def test_assets_use_digest_hydration_without_git_annex(self) -> None:
        assets = self.contract["assets"]["declarations"]
        self.assertEqual(len(assets), 71)
        self.assertEqual(
            Counter(entry["storage"] for entry in assets),
            Counter({"git": 55, "git-annex": 16}),
        )
        self.assertEqual(sum(entry["size"] for entry in assets), 57_407_085)
        for entry in assets:
            path = ROOT / entry["path"]
            self.assertFalse(path.is_symlink(), entry["path"])
            if entry["storage"] == "git":
                self.assertTrue(path.is_file(), entry["path"])
                self.assertEqual(sha256(path), entry["sha256"])
            else:
                retrieval = entry["retrieval"]
                self.assertEqual(retrieval["mode"], "read-only")
                self.assertTrue(retrieval["object_url"].startswith("https://"))
                self.assertIsNotNone(entry["annex_key"])
                if path.exists():
                    self.assertEqual(sha256(path), entry["sha256"])
        self.assertNotIn(
            "profiles/con",
            (ROOT / "assets/manifest.yaml").read_text(encoding="utf-8"),
        )

    def test_zotero_evidence_is_exact_and_active_tools_are_read_only(self) -> None:
        snapshot = self.zotero_import["snapshot"]
        self.assertEqual(snapshot["group_id"], 6197458)
        self.assertEqual(snapshot["library_version"], 451)
        self.assertEqual(snapshot["collections"], 5)
        self.assertEqual(snapshot["top_level_items"], 197)
        self.assertEqual(
            snapshot["candidate_counts"],
            {
                "XYZDataset": 4,
                "XYZInstrument": 3,
                "XYZPublication": 126,
                "XYZPublicationVenue": 20,
            },
        )
        self.assertFalse(self.zotero_import["runtime_dependency"])
        self.assertFalse(self.zotero_import["write_to_zotero_supported"])
        for entry in self.zotero_import["entries"]:
            path = ROOT / entry["target_path"]
            self.assertTrue(path.is_file(), entry["target_path"])
            self.assertEqual(sha256(path), entry["source_sha256"])
        tools = ROOT / "integrations/zotero/tools"
        self.assertEqual(
            {path.name for path in tools.glob("*.py")},
            {"zotero_ingest.py", "zotero_site_export.py"},
        )
        for path in tools.glob("*.py"):
            compile(path.read_bytes(), str(path), "exec")

    def test_every_existing_test_definition_is_traced_without_drop(self) -> None:
        summary = self.traceability["summary"]
        self.assertEqual(
            summary,
            {
                "editor_component_definitions": 8,
                "parent_python_methods": 106,
                "playwright_consumer_ported_definitions": 2,
                "playwright_consumer_ported_executions": 4,
                "playwright_definitions": 5,
                "playwright_engine_retained_definitions": 3,
                "playwright_engine_retained_executions": 5,
                "playwright_project_executions": 9,
                "unmapped": 0,
                "zotero_consumer_ported_methods": 23,
                "zotero_engine_retained_methods": 19,
                "zotero_python_methods": 42,
            },
        )
        collections = (
            self.traceability["parent_python"],
            self.traceability["playwright_definitions"],
            self.traceability["playwright_executions"],
            self.traceability["zotero_python"],
            self.traceability["editor_component"],
        )
        for entries in collections:
            for entry in entries:
                self.assertEqual(entry["status"], "mapped-no-drop")
                self.assertIn("successor_owner", entry)
        for entry in self.traceability["zotero_python"]:
            self.assertTrue((ROOT / entry["source_copy"]).is_file())
            if entry["execution"] == "active-consumer-test":
                successor_path = entry["successor_test_id"].split("::", 1)[0]
                self.assertTrue((ROOT / successor_path).is_file())
        active_definitions = [
            entry
            for entry in self.traceability["playwright_definitions"]
            if entry.get("execution") == "active-consumer-browser-test"
        ]
        self.assertEqual(len(active_definitions), 2)
        for entry in active_definitions:
            successor_path = entry["successor_test_id"].split("::", 1)[0]
            self.assertTrue((ROOT / successor_path).is_file())
        active_executions = [
            entry
            for entry in self.traceability["playwright_executions"]
            if entry.get("execution") == "active-consumer-browser-test"
        ]
        self.assertEqual(len(active_executions), 4)
        self.assertEqual(
            {entry["project"] for entry in active_executions},
            {"chromium", "webkit"},
        )

    def test_active_configuration_uses_only_flattened_paths(self) -> None:
        active = [
            ROOT / "assets/manifest.yaml",
            ROOT / "site/presentation.yaml",
            ROOT / "site/projection.yaml",
            *sorted((ROOT / "site/config").glob("*.toml")),
        ]
        for path in active:
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("profiles/con", content, str(path))
            self.assertNotIn("submodules/", content, str(path))
        self.assertNotIn(
            "https://centerforopenneuroscience.org/",
            (ROOT / "site/config/hugo.toml").read_text(encoding="utf-8"),
        )

    def test_single_repository_has_no_submodule_or_custom_domain_contract(self) -> None:
        self.assertFalse((ROOT / ".gitmodules").exists())
        self.assertFalse((ROOT / "CNAME").exists())
        self.assertFalse((ROOT / "site/static/CNAME").exists())
        ignored_runtime_parts = {
            ".pixi",
            ".orinoco",
            "__pycache__",
            "build",
            "node_modules",
            "playwright-report",
            "test-results",
        }
        symlinks = [
            path
            for path in ROOT.rglob("*")
            if path.is_symlink()
            and not (
                set(path.relative_to(ROOT).parts)
                & ignored_runtime_parts
            )
        ]
        self.assertEqual(symlinks, [])
        if (ROOT / ".git").exists():
            index = subprocess.check_output(
                ["git", "-C", str(ROOT), "ls-files", "--stage"], text=True
            )
            self.assertFalse(
                any(line.startswith("160000 ") for line in index.splitlines())
            )

    def test_test_site_notice_is_not_rendered(self) -> None:
        self.assertFalse(
            (ROOT / "site/layouts/_partials/extend-footer.html").exists()
        )
        index = (ROOT / "build/site/index.html").read_text(encoding="utf-8")
        self.assertNotIn("TEST SITE", index)
        self.assertNotIn("not the production Center for Open Neuroscience", index)

    def test_offline_acceptance_uses_an_os_level_network_deny_profile(self) -> None:
        profile = (
            ROOT / "tests/offline/macos-network-deny.sb"
        ).read_text(encoding="utf-8")
        self.assertIn("(deny network*)", profile)
        instructions = (
            ROOT / "tests/offline/README.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(instructions.split())
        for operation in (
            "validation",
            "projection verification",
            "update",
            "site builds",
            "editor",
        ):
            self.assertIn(operation, normalized)
        self.assertIn("network namespace", normalized)
        self.assertIn("Warmed-cache offline", normalized)
        self.assertIn("all sixteen payloads", normalized)
        self.assertIn("size and SHA-256 digest", normalized)
        self.assertIn("pixi run assets-hydrate", normalized)
        self.assertIn("pixi run assets-verify", normalized)
        self.assertIn("complete cold-offline operation", normalized)
        self.assertIn("M4-I002", normalized)

    def test_complete_site_bundle_inventory_is_current(self) -> None:
        manifest = load_json(ROOT / "orinoco-site-bundle.json")
        import_ledger = load_json(
            ROOT / "metadata/provenance/site-import-26907c487efa.json"
        )
        self.assertEqual(manifest["format"], "orinoco-site-bundle-v1")
        self.assertEqual(len(manifest["files"]), 1_092)
        self.assertEqual(manifest["summary"]["files"], 1_092)
        self.assertEqual(
            manifest["summary"]["classes"],
            {
                "consumer_tests": 20,
                "generated": 205,
                "initialized_site_owned": 867,
            },
        )
        self.assertEqual(
            manifest["source"],
            {
                "repository": (
                    "https://github.com/con/centerforopenneuroscience.org.git"
                ),
                "commit": "26907c487efaa2c31bba9d02398aa201ab6f774b",
                "scope": "full",
            },
        )
        self.assertEqual(import_ledger["source"]["declared_files"], 1_092)
        self.assertEqual(
            import_ledger["source"]["manifest"],
            "orinoco-site-bundle.json",
        )
        self.assertEqual(
            import_ledger["source"]["manifest_sha256"],
            sha256(ROOT / "orinoco-site-bundle.json"),
        )
        self.assertEqual(
            import_ledger["files"],
            [
                {
                    "path": path,
                    "sha256": manifest["files"][path],
                    "size": manifest["sizes"][path],
                }
                for path in sorted(manifest["files"])
            ],
        )
        self.assertEqual(
            import_ledger["corrections"],
            [
                {
                    "files": 13,
                    "kind": "framework-annex-payload-materialization",
                    "materialization_source": {
                        "commit": (
                            "6c8b9a5b7260dc20dfe1453dd863b353e8f90f06"
                        ),
                        "repository": (
                            "https://github.com/leej3/www-from-model.git"
                        ),
                        "role": "allowed-hydrated-read-only-mirror",
                    },
                    "reason": (
                        "accepted framework source blobs contained git-annex "
                        "pointer paths instead of runtime payload bytes"
                    ),
                    "verification": [
                        "source Git blob SHA-256",
                        "MD5E annex key payload size",
                        "MD5E annex key payload MD5",
                        "ordinary-Git target SHA-256",
                    ],
                }
            ],
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tests/parity/site_bundle_inventory.py"),
                "--check",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        with tempfile.TemporaryDirectory(prefix="orinoco-bundle-check-") as temporary:
            fixture = Path(temporary)
            shutil.copy2(
                ROOT / "orinoco-site-bundle.json",
                fixture / "orinoco-site-bundle.json",
            )
            for relative in manifest["files"]:
                source = ROOT / relative
                destination = fixture / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            # These files are created by Copier/import after the immutable
            # source bundle is captured and must not redefine that inventory.
            (fixture / ".copier-answers.yml").write_text(
                "_commit: v0.1.0\n",
                encoding="utf-8",
            )
            (
                fixture
                / "metadata/provenance/site-import-26907c487efa.json"
            ).write_text("{}\n", encoding="utf-8")

            checker = fixture / "tests/parity/site_bundle_inventory.py"
            command = [
                sys.executable,
                str(checker),
                "--root",
                str(fixture),
                "--check",
            ]
            pristine = subprocess.run(
                command,
                cwd=fixture,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                pristine.returncode,
                0,
                pristine.stderr or pristine.stdout,
            )

            sample = fixture / "metadata/provenance/selection.yaml"
            original = sample.read_bytes()
            sample.write_bytes(original + b"\n")
            changed = subprocess.run(
                command,
                cwd=fixture,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn("mismatch", changed.stderr)

            sample.write_bytes(original)
            sample.write_text(
                "/annex/objects/"
                "MD5E-s1--d41d8cd98f00b204e9800998ecf8427e.bin\n",
                encoding="utf-8",
            )
            pointer = subprocess.run(
                command,
                cwd=fixture,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(pointer.returncode, 0)
            self.assertIn("git-annex pointer-form", pointer.stderr)

            sample.write_bytes(original)
            sample.unlink()
            missing = subprocess.run(
                command,
                cwd=fixture,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("missing", missing.stderr)


if __name__ == "__main__":
    unittest.main()
