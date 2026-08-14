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


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


review_host = load_module(
    "orinoco_metadata_review", ROOT / "integrations/metadata/tools/review.py"
)
adapter = load_module(
    "orinoco_dump_research_info_adapter",
    ROOT / "integrations/dump-research-info/metadata_adapter.py",
)
run_with_datalad = load_module(
    "orinoco_dump_research_info_datalad_runner",
    ROOT / "integrations/dump-research-info/run_with_datalad.py",
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
                    "schema_type": "dlthings:Person",
                    "name": "Existing Person",
                    "email": "new@example.invalid",
                },
                {
                    "pid": "person:source-only",
                    "schema_type": "dlthings:Person",
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
                    "schema_type": "dlthings:Publication",
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
    reference = downstream / "metadata/reference"
    people.mkdir(parents=True)
    publications.mkdir(parents=True)
    reference.mkdir(parents=True)
    (downstream / ".gitignore").write_text("build/\n", encoding="utf-8")
    (people / "existing.yaml").write_text(
        "pid: person:existing\n"
        "schema_type: dlthings:Person\n"
        "name: Existing Person\n",
        encoding="utf-8",
    )
    (publications / "publication.yaml").write_text(
        "pid: publication:current\n"
        "schema_type: dlthings:Publication\n"
        "title: Publication\n"
        "identifiers:\n"
        "  - notation: 10.1000/example\n",
        encoding="utf-8",
    )
    commit_repository(downstream, "downstream fixture")
    return source, downstream


class DumpResearchInfoAdapterTests(unittest.TestCase):
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

    def test_host_selection_stages_only_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            source, root = write_fixture(fixture)
            plugin = root / "integrations/dump-research-info/metadata_adapter.py"
            plugin.parent.mkdir(parents=True)
            shutil.copy2(Path(adapter.__file__), plugin)
            config = root / "integrations/metadata/sources.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "contract_version = 1\n"
                "[[sources]]\n"
                "id = \"dump-research-info\"\n"
                "plugin = \"integrations/dump-research-info/metadata_adapter.py\"\n"
                "enabled_by_default = false\n"
                "source_directory = \"data/con_site\"\n"
                "evidence_root = "
                "\"integrations/dump-research-info/source/con-site-gap\"\n",
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
            evidence = root / "integrations/dump-research-info/source/con-site-gap"

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
                review_host.MetadataReviewError, "datalad-run-dump-research-info"
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

    def test_datalad_runner_records_exact_revisions_and_only_evidence_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, downstream = write_fixture(Path(temporary))
            arguments = run_with_datalad.datalad_arguments(source, root=downstream)
            source_commit = adapter.git_commit(source)
            downstream_commit = adapter.git_commit(downstream)

            self.assertEqual(arguments[:3], ["datalad", "run", "--explicit"])
            self.assertEqual(
                arguments[arguments.index("-o") + 1],
                "integrations/dump-research-info/source/con-site-gap",
            )
            command = arguments[-1]
            self.assertIn(f"--expected-source-commit {source_commit}", command)
            self.assertIn(f"--downstream-revision {downstream_commit}", command)
            self.assertIn(
                "--output integrations/dump-research-info/source/con-site-gap",
                command,
            )

    def test_standalone_output_must_belong_to_datalad_run_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, downstream = write_fixture(root)
            outside = root.parent / "outside-adapter-output"
            old_cwd = Path.cwd()
            try:
                # The standalone contract refuses an output not owned by the
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
