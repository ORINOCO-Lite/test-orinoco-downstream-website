from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
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


provider = load_module(
    "orinoco_zotero_curation_prototype_v1_tests",
    ROOT / "source-adapters/zotero/curation_prototype_v1.py",
)


def metadata_snapshot() -> dict[Path, bytes]:
    records = ROOT / "metadata/records"
    return {
        path.relative_to(records): path.read_bytes()
        for path in sorted(records.rglob("*.yaml"))
    }


class ZoteroCurationPrototypeTests(unittest.TestCase):
    def test_item_identity_survives_doi_and_material_revision(self) -> None:
        core, _adapter = provider.load_dependencies(ROOT)
        source = {
            "pid": "https://doi.org/10.0000/original",
            "identifiers": [
                {
                    "notation": "zotero:group:6197458:item:STABLE01",
                    "schema_type": "dlthings:Identifier",
                },
                {
                    "notation": "10.0000/original",
                    "schema_type": "dlthings:DOI",
                },
            ],
        }
        revised = deepcopy(source)
        revised["pid"] = "https://doi.org/10.0000/corrected"
        revised["identifiers"][1]["notation"] = "10.0000/corrected"

        source_id, locator = provider.source_identity(source, 6197458)
        revised_id, revised_locator = provider.source_identity(revised, 6197458)
        self.assertEqual(source_id, "item:STABLE01")
        self.assertEqual(revised_id, source_id)
        self.assertEqual(revised_locator, locator)

        def candidate(record: dict[str, object]):
            return core.make_candidate(
                adapter_id=provider.ADAPTER_ID,
                source_namespace="zotero:group:6197458",
                source_record_id=provider.source_identity(record, 6197458)[0],
                claim_kind=provider.CLAIM_KIND,
                material={"source_record": record},
                relevant_policy={"prototype_version": 1},
                proposed_path="XYZPublication/doi-candidate.yaml",
                proposed_record=record,
            )

        original_candidate = candidate(source)
        revised_candidate = candidate(revised)
        self.assertEqual(
            revised_candidate.candidate_id,
            original_candidate.candidate_id,
        )
        self.assertNotEqual(
            revised_candidate.material_fingerprint,
            original_candidate.material_fingerprint,
        )

        duplicate = deepcopy(source)
        duplicate["identifiers"].insert(
            0,
            {
                "notation": "zotero:group:6197458:item:ANOTHER2",
                "schema_type": "dlthings:Identifier",
            },
        )
        duplicate_id, duplicate_locator = provider.source_identity(duplicate, 6197458)
        self.assertEqual(duplicate_id, "items:ANOTHER2,STABLE01")
        self.assertEqual(
            duplicate_locator,
            "https://api.zotero.org/groups/6197458/items?"
            "itemKey=ANOTHER2%2CSTABLE01",
        )

    def test_frozen_snapshot_builds_deterministic_nonmutating_candidates(self) -> None:
        before = metadata_snapshot()
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as first_directory:
            with tempfile.TemporaryDirectory(dir=build) as second_directory:
                first = provider.build_candidates(
                    ROOT,
                    Path(first_directory),
                    expected_library_version=451,
                )
                second = provider.build_candidates(
                    ROOT,
                    Path(second_directory),
                    expected_library_version=451,
                )

        self.assertEqual(
            [candidate.to_mapping() for candidate in first["candidates"]],
            [candidate.to_mapping() for candidate in second["candidates"]],
        )
        self.assertEqual(len(first["candidates"]), 126)
        self.assertEqual(first["source"]["group_id"], 6197458)
        self.assertEqual(first["source"]["library_version"], 451)
        self.assertEqual(
            first["source"]["snapshot_sha256"],
            "e824d6e007aeed49c36caa84d60d0458a882425b6fbdd69a18505ac8dbc6b28c",
        )
        self.assertEqual(
            first["source"]["publication_candidates_sha256"],
            "a844c0300c3ee876692279210b587b522b31ffd180b933143eba41f3802dedc7",
        )
        self.assertEqual(
            first["policy"]["sha256"],
            "e64b10d9f33d32baa495fe198330cd5a68539cac39e8fed5685df4224eea3a6c",
        )
        self.assertEqual(first["implementation"]["agent"], provider.ADAPTER_AGENT)
        for name in (
            "provider_sha256",
            "transformer_sha256",
            "ingest_sha256",
            "site_export_sha256",
        ):
            with self.subTest(implementation=name):
                self.assertRegex(first["implementation"][name], r"^[0-9a-f]{64}$")
        self.assertEqual(metadata_snapshot(), before)

        candidate_ids: set[str] = set()
        for candidate in first["candidates"]:
            self.assertTrue(candidate.proposed_path.startswith("XYZPublication/"))
            self.assertNotIn("metadata/records", candidate.proposed_path)
            self.assertEqual(candidate.blockers, ())
            self.assertNotIn(candidate.candidate_id, candidate_ids)
            candidate_ids.add(candidate.candidate_id)
            annotations = candidate.proposed_record["annotations"]
            self.assertEqual(
                annotations["pav:importedFrom"]["annotation_tag"],
                "pav:importedFrom",
            )
            self.assertRegex(
                annotations["pav:importedFrom"]["annotation_value"],
                r"^https://api\.zotero\.org/groups/6197458/items"
                r"(?:/[A-Z0-9]+|\?itemKey=[A-Z0-9]+(?:%2C[A-Z0-9]+)+)$",
            )
            self.assertEqual(
                annotations["pav:importedBy"]["annotation_tag"],
                "pav:importedBy",
            )
            self.assertEqual(
                annotations["pav:importedBy"]["annotation_value"],
                provider.ADAPTER_AGENT,
            )

    def test_snapshot_version_mismatch_fails_before_proposal(self) -> None:
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            with self.assertRaisesRegex(
                provider.ZoteroCurationError,
                "Zotero fixture moved: expected 452, found 451",
            ):
                provider.build_candidates(
                    ROOT,
                    Path(directory),
                    expected_library_version=452,
                )
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
