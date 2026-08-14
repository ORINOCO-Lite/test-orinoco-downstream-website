from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import textwrap
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


review = load_module(
    "orinoco_metadata_review", ROOT / "integrations/metadata/tools/review.py"
)
zotero = load_module(
    "orinoco_zotero_metadata_adapter",
    ROOT / "integrations/zotero/metadata_adapter.py",
)


EMPTY_DIFF = {
    "summary": {
        "added": 0,
        "removed": 0,
        "changed": 0,
        "unchanged": 0,
        "different": False,
    },
    "added": [],
    "removed": [],
    "changed": [],
}


class MetadataReviewHostTests(unittest.TestCase):
    def test_semantic_diff_reports_identity_and_field_changes(self) -> None:
        before = {
            "gone": {"pid": "gone"},
            "same": {"pid": "same", "value": 1},
            "edit": {"pid": "edit", "value": {"old": True}},
        }
        after = {
            "new": {"pid": "new"},
            "same": {"pid": "same", "value": 1},
            "edit": {"pid": "edit", "value": {"new": True}},
        }

        difference = review.semantic_diff(before, after)

        self.assertEqual(
            difference["summary"],
            {
                "added": 1,
                "removed": 1,
                "changed": 1,
                "unchanged": 1,
                "different": True,
            },
        )
        self.assertEqual(difference["added"][0]["id"], "new")
        self.assertEqual(difference["removed"][0]["id"], "gone")
        self.assertEqual(
            [change["path"] for change in difference["changed"][0]["changes"]],
            ["/value/new", "/value/old"],
        )

    def test_review_is_read_only_and_refresh_replaces_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "integrations" / "fake" / "adapter.py"
            plugin.parent.mkdir(parents=True)
            plugin.write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path
                    PLUGIN_API_VERSION = 1
                    def review(context):
                        root = Path(context["root"])
                        output = Path(context["output"])
                        staged = output / "snapshot.json"
                        staged.write_text('{"version": 2}\\n')
                        return {
                            "adapter_api_version": 1,
                            "source_id": "fake",
                            "canonical_promotion": False,
                            "source": {"reviewed_version": 1, "live_version": 2},
                            "source_diff": {empty},
                            "candidate_diff": {empty},
                            "canonical_diff": {empty},
                            "artifacts": {},
                            "evidence_updates": [{
                                "operation": "replace",
                                "staged": staged.relative_to(root).as_posix(),
                                "destination": "integrations/fake/source/snapshot.json",
                            }],
                        }
                    """
                ).replace("{empty}", repr(EMPTY_DIFF)),
                encoding="utf-8",
            )
            config = root / "sources.toml"
            config.write_text(
                'contract_version = 1\n[[sources]]\nid = "fake"\n'
                'plugin = "integrations/fake/adapter.py"\n',
                encoding="utf-8",
            )
            build = root / "build" / "metadata-review"
            destination = root / "integrations/fake/source/snapshot.json"

            report = review.run("review", root=root, config=config, build=build)
            self.assertFalse(destination.exists())
            self.assertFalse(report["canonical_promotion"])

            review.run("refresh-evidence", root=root, config=config, build=build)
            self.assertEqual(json.loads(destination.read_text()), {"version": 2})
            tracked = json.loads(
                (root / "generated/manifests/metadata-review.json").read_text()
            )
            self.assertFalse(tracked["canonical_promotion"])
            self.assertNotIn("evidence_updates", tracked["sources"][0])

    def test_evidence_destination_cannot_target_canonical_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(review.MetadataReviewError, "outside"):
                review.allowed_destination(
                    root, "fake", "metadata/records/XYZPerson/person.yaml"
                )

    def test_refresh_is_blocked_before_mutation_when_review_has_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "integrations/fake/adapter.py"
            plugin.parent.mkdir(parents=True)
            plugin.write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path
                    PLUGIN_API_VERSION = 1
                    def review(context):
                        root = Path(context["root"])
                        output = Path(context["output"])
                        staged = output / "snapshot.json"
                        staged.write_text('{"version": 2}\\n')
                        return {
                            "adapter_api_version": 1,
                            "source_id": "fake",
                            "canonical_promotion": False,
                            "source": {},
                            "source_diff": {empty},
                            "candidate_diff": {empty},
                            "canonical_diff": {empty},
                            "blockers": ["policy changed"],
                            "artifacts": {},
                            "evidence_updates": [{
                                "operation": "replace",
                                "staged": staged.relative_to(root).as_posix(),
                                "destination": "integrations/fake/source/snapshot.json",
                            }],
                        }
                    """
                ).replace("{empty}", repr(EMPTY_DIFF)),
                encoding="utf-8",
            )
            config = root / "sources.toml"
            config.write_text(
                'contract_version = 1\n[[sources]]\nid = "fake"\n'
                'plugin = "integrations/fake/adapter.py"\n',
                encoding="utf-8",
            )
            destination = root / "integrations/fake/source/snapshot.json"

            with self.assertRaisesRegex(review.MetadataReviewError, "policy changed"):
                review.run(
                    "refresh-evidence",
                    root=root,
                    config=config,
                    build=root / "build/metadata-review",
                )

            self.assertFalse(destination.exists())


class ZoteroAdapterContractTests(unittest.TestCase):
    def test_snapshot_map_namespaces_collection_and_item_keys(self) -> None:
        snapshot = {
            "collections": [{"data": {"key": "SAME", "name": "Articles"}}],
            "items": [{"data": {"key": "SAME", "title": "Article"}}],
        }
        self.assertEqual(
            set(zotero.snapshot_map(snapshot)), {"collection:SAME", "item:SAME"}
        )

    def test_candidate_map_rejects_cross_class_pid_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for class_name in ("XYZDataset", "XYZPublication"):
                (root / f"{class_name}.json").write_text(
                    json.dumps([{"pid": "duplicate"}]), encoding="utf-8"
                )
            with self.assertRaisesRegex(zotero.ZoteroAdapterError, "duplicated"):
                zotero.candidate_map(root)

    def test_canonical_export_skips_only_the_record_root_control_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "metadata/records"
            (records / "XYZPerson").mkdir(parents=True)
            (records / ".dumpthings.yaml").write_text("type: file\n", encoding="utf-8")
            (records / "XYZPerson/person.yaml").write_text(
                "pid: person\nschema_type: dlthings:Person\n", encoding="utf-8"
            )
            destination = root / "build/index"
            index = zotero.export_canonical_json(root, destination)
            self.assertEqual(set(index), {"XYZPerson"})
            self.assertEqual(json.loads(index["XYZPerson"].read_text())[0]["pid"], "person")

    def test_noncanonical_mapping_identities_are_explicit_and_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical_people = root / "canonical/XYZPerson.json"
            canonical_organizations = root / "canonical/XYZOrganization.json"
            canonical_people.parent.mkdir(parents=True)
            canonical_people.write_text(
                json.dumps([{"pid": "xyzrins:persons/published"}]), encoding="utf-8"
            )
            canonical_organizations.write_text("[]", encoding="utf-8")
            identities = root / "review-identities.yaml"
            identities.write_text(
                textwrap.dedent(
                    """
                    format_version: 1
                    identities:
                    - pid: xyzrins:persons/source-only
                      aliases: [Source Only, Source Alias]
                    """
                ),
                encoding="utf-8",
            )

            closure = zotero.export_noncanonical_mapping_identities(
                identities,
                {
                    "XYZPerson": canonical_people,
                    "XYZOrganization": canonical_organizations,
                },
                root / "build/closure",
            )

            self.assertEqual(closure["identities"], ["xyzrins:persons/source-only"])
            self.assertEqual(
                json.loads(closure["people_path"].read_text()),
                [
                    {"display_label": "Source Only", "pid": "xyzrins:persons/source-only"},
                    {"display_label": "Source Alias", "pid": "xyzrins:persons/source-only"},
                ],
            )
            self.assertFalse((root / "metadata/records").exists())


if __name__ == "__main__":
    unittest.main()
