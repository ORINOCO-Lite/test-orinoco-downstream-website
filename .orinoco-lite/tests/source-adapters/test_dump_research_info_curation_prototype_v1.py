from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE_PATH = "../dump-research-info"
RELOCATED_SOURCE_PATH = "../relocated-dump-research-info"


def load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def commit_all(root: Path, message: str, *, initialize: bool = False) -> str:
    if initialize:
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(root)],
            check=True,
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
            message,
        ],
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD^{commit}"],
        text=True,
    ).strip()


def write_source_records(path: Path, *, existing_email: str) -> None:
    records = [
        {
            "pid": "person:existing",
            "schema_type": "xyzri:XYZPerson",
            "name": "Existing Person",
            "email": existing_email,
        },
        {
            "pid": "person:source-only",
            "schema_type": "xyzri:XYZPerson",
            "name": "Source Only",
            "identifiers": [
                {
                    "notation": "source-only",
                    "schema_type": "dlthings:Identifier",
                }
            ],
            "associated_with": [
                {"object": "organization:planned"},
                {"object": "organization:missing"},
            ],
        },
    ]
    path.write_text(
        json.dumps(records, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.with_name("XYZOrganization.json").write_text(
        json.dumps(
            [
                {
                    "pid": "organization:planned",
                    "schema_type": "xyzri:XYZOrganization",
                    "name": "Planned Organization",
                }
            ],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def write_fixture(root: Path) -> tuple[Path, Path, ModuleType, str]:
    source = root / "dump-research-info"
    source_data = source / "data/con_site"
    source_data.mkdir(parents=True)
    write_source_records(
        source_data / "XYZPerson.json",
        existing_email="source@example.invalid",
    )
    source_commit = commit_all(source, "source fixture", initialize=True)

    downstream = root / "downstream"
    adapter_directory = downstream / "source-adapters/dump-research-info"
    core_directory = downstream / "source-adapters/metadata/tools"
    records = downstream / "metadata/records/XYZPerson"
    adapter_directory.mkdir(parents=True)
    core_directory.mkdir(parents=True)
    records.mkdir(parents=True)
    shutil.copy2(
        ROOT / "source-adapters/dump-research-info/metadata_adapter.py",
        adapter_directory / "metadata_adapter.py",
    )
    shutil.copy2(
        ROOT / "source-adapters/dump-research-info/curation_prototype_v1.py",
        adapter_directory / "curation_prototype_v1.py",
    )
    shutil.copy2(
        ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
        core_directory / "curation_prototype_v1.py",
    )
    (downstream / ".gitignore").write_text("build/\n", encoding="utf-8")
    (records / "existing.yaml").write_text(
        "pid: person:existing\nschema_type: xyzri:XYZPerson\nname: Existing Person\n",
        encoding="utf-8",
    )
    commit_all(downstream, "downstream fixture", initialize=True)
    provider = load_module(
        "orinoco_dump_research_info_curation_test",
        adapter_directory / "curation_prototype_v1.py",
    )
    return source, downstream, provider, source_commit


def candidate_signatures(
    result: dict[str, object],
) -> dict[str, tuple[str, str, str]]:
    return {
        candidate.source_record_id: (
            candidate.candidate_id,
            candidate.material_fingerprint,
            candidate.relevant_policy_fingerprint,
        )
        for candidate in result["candidates"]
    }


class DumpResearchInfoCurationPrototypeTests(unittest.TestCase):
    def test_candidates_are_exact_deterministic_and_block_unresolved_relations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, downstream, provider, source_commit = write_fixture(Path(temporary))

            first = provider.build_candidates(
                downstream,
                downstream / "build/curation/first",
                source_path=SOURCE_PATH,
                expected_source_commit=source_commit,
                source_run_id="source-run-one",
            )
            second = provider.build_candidates(
                downstream,
                downstream / "build/curation/second",
                source_path=SOURCE_PATH,
                expected_source_commit=source_commit,
                source_run_id="source-run-two",
            )

            self.assertEqual(candidate_signatures(first), candidate_signatures(second))
            self.assertNotEqual(first["context"], second["context"])
            self.assertEqual(source_commit, first["source"]["commit"])
            self.assertEqual(
                provider.SOURCE_NAMESPACE,
                first["candidates"][0].source_namespace,
            )
            candidates = {
                candidate.source_record_id: candidate
                for candidate in first["candidates"]
            }
            existing = candidates["XYZPerson:person:existing"]
            source_only = candidates["XYZPerson:person:source-only"]
            self.assertEqual("XYZPerson/existing.yaml", existing.proposed_path)
            self.assertEqual(
                "XYZPerson/person-source-only.yaml",
                source_only.proposed_path,
            )
            self.assertEqual(
                {
                    "pid": "person:existing",
                    "schema_type": "xyzri:XYZPerson",
                    "name": "Existing Person",
                },
                existing.baseline_record,
            )
            self.assertIsNone(source_only.baseline_record)
            self.assertEqual(
                [
                    {"object": "organization:planned"},
                    {"object": "organization:missing"},
                ],
                source_only.proposed_record["associated_with"],
            )
            blocker = "unresolved-relation:organization:missing"
            self.assertEqual((blocker,), source_only.blockers)
            self.assertEqual([], list(existing.blockers))
            self.assertEqual([blocker], first["blockers"])
            annotations = source_only.proposed_record["annotations"]
            self.assertEqual(
                {
                    "annotation_tag": "pav:importedBy",
                    "annotation_value": provider.ADAPTER_AGENT,
                },
                annotations["pav:importedBy"],
            )
            self.assertEqual(
                {
                    "annotation_tag": "pav:importedFrom",
                    "annotation_value": (
                        "https://github.com/con/dump-research-info/blob/main/"
                        "data/con_site/XYZPerson.json"
                        "#record=person%3Asource-only"
                    ),
                },
                annotations["pav:importedFrom"],
            )
            self.assertEqual(
                annotations,
                source_only.proposed_record["identifiers"][0]["annotations"],
            )

            relocated = source.parent / "relocated-dump-research-info"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "-q",
                    "--no-hardlinks",
                    str(source),
                    str(relocated),
                ],
                check=True,
            )
            relocated_result = provider.build_candidates(
                downstream,
                downstream / "build/curation/relocated",
                source_path=RELOCATED_SOURCE_PATH,
                expected_source_commit=source_commit,
                source_run_id="source-run-relocated",
            )
            self.assertEqual(
                candidate_signatures(first),
                candidate_signatures(relocated_result),
            )
            self.assertEqual(
                [candidate.proposed_record for candidate in first["candidates"]],
                [
                    candidate.proposed_record
                    for candidate in relocated_result["candidates"]
                ],
            )
            self.assertNotEqual(
                first["source"]["path"],
                relocated_result["source"]["path"],
            )
            self.assertEqual(
                first["implementation"], relocated_result["implementation"]
            )

            (source / "non-material-note.txt").write_text(
                "This does not change a source record.\n",
                encoding="utf-8",
            )
            context_only_commit = commit_all(source, "source context only")
            context_only = provider.build_candidates(
                downstream,
                downstream / "build/curation/context-only",
                source_path=SOURCE_PATH,
                expected_source_commit=context_only_commit,
                source_run_id="source-run-three",
            )
            self.assertEqual(
                candidate_signatures(first),
                candidate_signatures(context_only),
            )
            self.assertEqual(
                [candidate.proposed_record for candidate in first["candidates"]],
                [candidate.proposed_record for candidate in context_only["candidates"]],
            )

            write_source_records(
                source / "data/con_site/XYZPerson.json",
                existing_email="material-change@example.invalid",
            )
            material_commit = commit_all(source, "change source record")
            changed = provider.build_candidates(
                downstream,
                downstream / "build/curation/material-change",
                source_path=SOURCE_PATH,
                expected_source_commit=material_commit,
                source_run_id="source-run-four",
            )
            changed_existing = {
                candidate.source_record_id: candidate
                for candidate in changed["candidates"]
            }["XYZPerson:person:existing"]
            self.assertEqual(existing.candidate_id, changed_existing.candidate_id)
            self.assertNotEqual(
                existing.material_fingerprint,
                changed_existing.material_fingerprint,
            )
            self.assertEqual(
                existing.relevant_policy_fingerprint,
                changed_existing.relevant_policy_fingerprint,
            )

    def test_imported_structured_assertions_receive_expanded_pav(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _source, _downstream, provider, _source_commit = write_fixture(
                Path(temporary)
            )
            record = {
                "pid": "publication:assertions",
                "schema_type": "xyzri:XYZPublication",
                "title": "Imported assertions",
                "attributed_to": [
                    {
                        "object": "person:author",
                        "schema_type": "dlthings:Attribution",
                    }
                ],
                "attributes": [
                    {
                        "predicate": "dcterms:issued",
                        "value": "2026",
                        "schema_type": "dlthings:AttributeSpecification",
                    }
                ],
                "identifiers": [
                    {
                        "notation": "assertions",
                        "schema_type": "dlthings:Identifier",
                    }
                ],
                "generated_by": [
                    {
                        "object": "project:source",
                        "schema_type": "dlthings:Generation",
                    }
                ],
                "associated_with": [{"object": "organization:source"}],
            }
            annotated = provider.annotate_record(
                record,
                imported_by=provider.ADAPTER_AGENT,
                imported_from="https://example.invalid/source/assertions",
            )

            self.assertNotIn("annotations", record)
            expected = annotated["annotations"]
            for field in provider.PAV_ASSERTION_FIELDS:
                with self.subTest(field=field):
                    self.assertEqual(
                        annotated[field][0]["annotations"],
                        expected,
                    )
            self.assertNotIn("annotations", annotated["associated_with"][0])

            conflicting = {
                **record,
                "identifiers": [
                    {
                        "notation": "assertions",
                        "schema_type": "dlthings:Identifier",
                        "annotations": {
                            "pav:importedFrom": {
                                "annotation_tag": "pav:importedFrom",
                                "annotation_value": "https://example.invalid/other",
                            }
                        },
                    }
                ],
            }
            with self.assertRaisesRegex(
                provider.DumpResearchInfoCurationError,
                "proposal would overwrite pav:importedFrom",
            ):
                provider.annotate_record(
                    conflicting,
                    imported_by=provider.ADAPTER_AGENT,
                    imported_from="https://example.invalid/source/assertions",
                )

    def test_source_coordinate_must_be_relative_exact_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, downstream, provider, source_commit = write_fixture(Path(temporary))

            with self.assertRaisesRegex(
                provider.DumpResearchInfoCurationError,
                "repository-relative",
            ):
                provider.build_candidates(
                    downstream,
                    downstream / "build/curation/absolute",
                    source_path=source.resolve().as_posix(),
                    expected_source_commit=source_commit,
                )
            with self.assertRaisesRegex(
                provider.DumpResearchInfoCurationError,
                "exact lower-case 40-hex",
            ):
                provider.build_candidates(
                    downstream,
                    downstream / "build/curation/symbolic",
                    source_path=SOURCE_PATH,
                    expected_source_commit="main",
                )
            with self.assertRaisesRegex(
                provider.DumpResearchInfoCurationError,
                "Source checkout moved",
            ):
                provider.build_candidates(
                    downstream,
                    downstream / "build/curation/moved",
                    source_path=SOURCE_PATH,
                    expected_source_commit="0" * 40,
                )

            (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                provider.DumpResearchInfoCurationError,
                "must be clean",
            ):
                provider.build_candidates(
                    downstream,
                    downstream / "build/curation/dirty",
                    source_path=SOURCE_PATH,
                    expected_source_commit=source_commit,
                )


if __name__ == "__main__":
    unittest.main()
