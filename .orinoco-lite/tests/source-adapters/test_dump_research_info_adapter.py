from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[3]


def load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


review_host = load_module(
    "orinoco_metadata_review", ROOT / "source-adapters/metadata/tools/review.py"
)
adapter = load_module(
    "orinoco_dump_research_info_adapter",
    ROOT / "source-adapters/dump-research-info/metadata_adapter.py",
)


def commit_repository(root: Path, message: str) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Adapter Test",
            "-c",
            "user.email=adapter@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD^{commit}"], text=True
    ).strip()


def write_fixture(root: Path) -> tuple[Path, Path]:
    source = root / "dump-research-info"
    source_data = source / "data/con_site"
    source_data.mkdir(parents=True)
    (source_data / "XYZPerson.json").write_text(
        json.dumps(
            [
                {
                    "pid": "person:existing",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Existing Person",
                    "email": "new@example.invalid",
                },
                {
                    "pid": "person:source-only",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Source Only",
                    "associated_with": [{"object": "organization:missing"}],
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (source_data / "XYZPublication.json").write_text(
        json.dumps(
            [
                {
                    "pid": "publication:legacy",
                    "schema_type": "xyzri:XYZPublication",
                    "title": "Publication",
                    "identifiers": [{"notation": "10.1000/example"}],
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    commit_repository(source, "source fixture")

    downstream = root / "downstream"
    people = downstream / "metadata/records/XYZPerson"
    publications = downstream / "metadata/records/XYZPublication"
    people.mkdir(parents=True)
    publications.mkdir(parents=True)
    (downstream / ".gitignore").write_text("build/\n", encoding="utf-8")
    (people / "existing.yaml").write_text(
        "pid: person:existing\n"
        "schema_type: xyzri:XYZPerson\n"
        "name: Existing Person\n",
        encoding="utf-8",
    )
    (publications / "publication.yaml").write_text(
        "pid: publication:current\n"
        "schema_type: xyzri:XYZPublication\n"
        "title: Publication\n"
        "identifiers:\n"
        "  - notation: 10.1000/example\n",
        encoding="utf-8",
    )
    commit_repository(downstream, "downstream fixture")
    return source, downstream


def metadata_snapshot(downstream: Path) -> dict[Path, bytes]:
    root = downstream / "metadata/records"
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*.yaml")
    }


class DumpResearchInfoAdapterTests(unittest.TestCase):
    def test_documented_provenance_boundary_is_direct_datalad_run(self) -> None:
        readme = (ROOT / "source-adapters/dump-research-info/README.md").read_text()
        manifest = (ROOT / "source-adapters/metadata/pixi.toml").read_text()

        self.assertIn("pixi run datalad run --explicit", readme)
        self.assertIn("(\n  set -eu", readme)
        self.assertIn(
            '"python source-adapters/dump-research-info/metadata_adapter.py \\\n',
            readme,
        )
        self.assertIn("--materialize", readme)
        self.assertIn("-o metadata/records", readme)
        self.assertNotIn(
            "-o source-adapters/dump-research-info/source/con-site-gap", readme
        )
        self.assertIn(
            "SOURCE=../orinoco-lite-dev/submodules/dump-research-info", readme
        )
        self.assertIn("--source '$SOURCE'", readme)
        self.assertNotIn("--source /", readme)
        self.assertNotIn("datalad-run-dump-research-info", readme)
        self.assertNotIn("datalad-run-dump-research-info", manifest)
        self.assertNotIn("materialize-dump-research-info", manifest)
        self.assertNotIn("git-annex", manifest)
        self.assertIn("downstream's committed root Pixi lock", readme)
        self.assertNotIn("source-owned", readme)
        self.assertNotIn("authoritative", readme)

    def test_extract_is_deterministic_and_reports_gap_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            first = root / "outputs/first"
            second = root / "outputs/second"

            report = adapter.extract(source, downstream, first)
            adapter.extract(source, downstream, second)

            self.assertFalse(report["canonical_promotion"])
            self.assertEqual(
                report["summary"],
                {
                    "source_records": 3,
                    "matched_records": 2,
                    "source_only_records": 1,
                    "source_only_by_class": {"XYZPerson": 1},
                    "enrichment_records": 1,
                    "ambiguous_records": 0,
                    "unresolved_relation_targets": 1,
                    "matched_without_delta": 1,
                },
            )
            self.assertEqual(
                json.loads((first / "candidates/XYZPerson.json").read_text())[0][
                    "pid"
                ],
                "person:source-only",
            )
            enrichment = json.loads(
                (first / "enrichment/XYZPerson.json").read_text()
            )
            self.assertEqual(
                enrichment[0]["missing_fields"], {"email": "new@example.invalid"}
            )
            self.assertEqual(
                {
                    path.relative_to(first): path.read_bytes()
                    for path in first.rglob("*")
                    if path.is_file()
                },
                {
                    path.relative_to(second): path.read_bytes()
                    for path in second.rglob("*")
                    if path.is_file()
                },
            )

    def test_materialize_writes_transformed_source_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            source_path = source / "data/con_site/XYZPerson.json"
            source_records = json.loads(source_path.read_text())
            source_records[1]["associated_with"][0]["object"] = "person:existing"
            source_path.write_text(
                json.dumps(source_records) + "\n", encoding="utf-8"
            )
            diagnostics = downstream / "build/metadata-review/materialize"
            existing_path = downstream / "metadata/records/XYZPerson/existing.yaml"
            existing_path.write_text(
                "pid: person:existing\n"
                "schema_type: xyzri:XYZPerson\n"
                "name: Stale Name\n"
                "email: stale@example.invalid\n"
                "downstream_only: remove me\n",
                encoding="utf-8",
            )

            result = adapter.materialize(source, downstream, diagnostics)

            self.assertEqual(
                result["summary"],
                {
                    "added_records": 1,
                    "updated_records": 2,
                    "unchanged_records": 0,
                    "written_records": 3,
                },
            )
            added = downstream / "metadata/records/XYZPerson/person-source-only.yaml"
            self.assertEqual(
                adapter.yaml.safe_load(added.read_text()),
                {
                    "pid": "person:source-only",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Source Only",
                    "associated_with": [{"object": "person:existing"}],
                },
            )
            existing = adapter.yaml.safe_load(existing_path.read_text())
            self.assertEqual(
                existing,
                {
                    "pid": "person:existing",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Existing Person",
                    "email": "new@example.invalid",
                },
            )
            self.assertEqual(
                result["updated"],
                [
                    {
                        "path": "metadata/records/XYZPerson/existing.yaml",
                        "changed_value_paths": [
                            "/downstream_only",
                            "/email",
                            "/name",
                        ],
                    },
                    {
                        "path": "metadata/records/XYZPublication/publication.yaml",
                        "changed_value_paths": ["/identifiers"],
                    },
                ],
            )
            self.assertTrue((diagnostics / "materialization.json").is_file())

            repeated = adapter.materialize(source, downstream, diagnostics)
            self.assertEqual(
                repeated["summary"],
                {
                    "added_records": 0,
                    "updated_records": 0,
                    "unchanged_records": 3,
                    "written_records": 0,
                },
            )

    def test_materialize_preserves_unresolved_relations_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            diagnostics = downstream / "build/metadata-review/materialize"

            result = adapter.materialize(source, downstream, diagnostics)

            added = adapter.yaml.safe_load(
                (
                    downstream
                    / "metadata/records/XYZPerson/person-source-only.yaml"
                ).read_text()
            )
            self.assertEqual(
                added["associated_with"],
                [
                    {
                        "object": "organization:missing",
                    }
                ],
            )
            self.assertNotIn("skipped_unresolved_values", result)
            self.assertTrue((diagnostics / "materialization.json").is_file())

    def test_materialize_rejects_two_sources_for_one_final_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            source_path = source / "data/con_site/XYZPerson.json"
            source_records = json.loads(source_path.read_text())
            source_records[1]["identifiers"] = [{"notation": "person:existing"}]
            source_path.write_text(
                json.dumps(source_records) + "\n", encoding="utf-8"
            )
            before = metadata_snapshot(downstream)

            with self.assertRaisesRegex(
                adapter.DumpResearchInfoAdapterError,
                "same final PID person:existing",
            ):
                adapter.materialize(
                    source,
                    downstream,
                    downstream / "build/metadata-review/materialize",
                )

            self.assertEqual(metadata_snapshot(downstream), before)

    def test_materialize_rejects_two_sources_for_one_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            source_path = source / "data/con_site/XYZPerson.json"
            source_records = json.loads(source_path.read_text())
            source_records.append(
                {
                    "pid": "person/source-only",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Colliding Source",
                }
            )
            source_path.write_text(
                json.dumps(source_records) + "\n", encoding="utf-8"
            )
            before = metadata_snapshot(downstream)

            with self.assertRaisesRegex(
                adapter.DumpResearchInfoAdapterError,
                "same final path",
            ):
                adapter.materialize(
                    source,
                    downstream,
                    downstream / "build/metadata-review/materialize",
                )

            self.assertEqual(metadata_snapshot(downstream), before)

    def test_materialize_rejects_exact_pid_in_another_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            source_path = source / "data/con_site/XYZPerson.json"
            source_records = json.loads(source_path.read_text())
            source_records[0]["pid"] = "publication:current"
            source_path.write_text(
                json.dumps(source_records) + "\n", encoding="utf-8"
            )
            before = metadata_snapshot(downstream)

            with self.assertRaisesRegex(
                adapter.DumpResearchInfoAdapterError,
                "exact downstream PID is in XYZPublication",
            ):
                adapter.materialize(
                    source,
                    downstream,
                    downstream / "build/metadata-review/materialize",
                )

            self.assertEqual(metadata_snapshot(downstream), before)

    def test_change_paths_reports_additions_replacements_and_removals(self) -> None:
        self.assertEqual(
            adapter.change_paths(
                {"keep": 1, "replace": "old", "remove": True},
                {"keep": 1, "replace": "new", "add": True},
            ),
            ["/add", "/remove", "/replace"],
        )

    def test_host_selection_stages_only_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            source, root = write_fixture(fixture)
            adapter_path = (
                root / "source-adapters/dump-research-info/metadata_adapter.py"
            )
            adapter_path.parent.mkdir(parents=True)
            shutil.copy2(Path(adapter.__file__), adapter_path)
            config = root / "source-adapters/metadata/sources.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "contract_version = 1\n"
                "[[sources]]\n"
                "id = \"dump-research-info\"\n"
                "adapter = \"source-adapters/dump-research-info/metadata_adapter.py\"\n"
                "enabled_by_default = false\n"
                "source_directory = \"data/con_site\"\n"
                "evidence_root = "
                "\"source-adapters/dump-research-info/source/con-site-gap\"\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Adapter Test",
                    "-c",
                    "user.email=adapter@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "adapter fixture",
                ],
                check=True,
            )
            evidence = root / "source-adapters/dump-research-info/source/con-site-gap"

            report = review_host.run(
                "review",
                root=root,
                config=config,
                build=root / "build/metadata-review",
                selected_sources=["dump-research-info"],
                source_inputs={"dump-research-info": str(source)},
            )
            self.assertFalse(evidence.exists())
            self.assertEqual(
                report["sources"][0]["canonical_diff"]["summary"]["added"], 1
            )

            with self.assertRaisesRegex(
                review_host.MetadataReviewError, "under datalad run"
            ):
                review_host.run(
                    "refresh-evidence",
                    root=root,
                    config=config,
                    build=root / "build/metadata-review",
                    selected_sources=["dump-research-info"],
                    source_inputs={"dump-research-info": str(source)},
                )
            self.assertFalse(evidence.exists())
            self.assertFalse(
                (root / "metadata/records/XYZPerson/source-only.yaml").exists()
            )

    def test_standalone_output_must_belong_to_datalad_run_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            outside = root.parent / "outside-adapter-output"
            old_cwd = Path.cwd()
            try:
                # The standalone contract refuses an output outside the
                # current DataLad run dataset before touching that path.
                os.chdir(root)
                with self.assertRaisesRegex(
                    adapter.DumpResearchInfoAdapterError, "strict descendant"
                ):
                    adapter.main(
                        [
                            "--source",
                            str(source),
                            "--downstream",
                            str(downstream),
                            "--output",
                            str(outside),
                        ]
                    )
            finally:
                os.chdir(old_cwd)
