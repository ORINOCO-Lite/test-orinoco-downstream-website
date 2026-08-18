from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
IMPORTED_BY = "urn:orinoco-lite:test:source-adapter"
IMPORTED_FROM = "https://example.invalid/source/pav-acceptance"
PAV = {
    "pav:importedBy": {
        "annotation_tag": "pav:importedBy",
        "annotation_value": IMPORTED_BY,
    },
    "pav:importedFrom": {
        "annotation_tag": "pav:importedFrom",
        "annotation_value": IMPORTED_FROM,
    },
}
COMPACT_PAV = {
    "pav:importedBy": IMPORTED_BY,
    "pav:importedFrom": IMPORTED_FROM,
}
EXPECTED_PROJECTION = {
    "records": 199,
    "pages": 185,
    "graph_nodes": 186,
    "graph_edges": 467,
}

try:
    from rdflib import Graph, URIRef
    from rdflib.compare import isomorphic

    from orinoco_lite.config import load_workspace, load_workspace_lock
    from orinoco_lite.errors import DriverError, OrinocoError
    from orinoco_lite.projection import (
        _native_fingerprint,
        render_projection,
        validate_semantics,
    )
    from orinoco_lite.runtime import verify_runtime_directory
    from orinoco_lite.schema_conversion import build_format_converters

    RUNTIME_ACCEPTANCE_AVAILABLE = True
except ModuleNotFoundError:
    # The adapter-only Pixi environment deliberately does not install the
    # released engine. The root consumer environment runs this acceptance.
    RUNTIME_ACCEPTANCE_AVAILABLE = False


def record_with_annotations(annotations: dict[str, object]) -> dict[str, object]:
    """Return one schema probe covering every currently proven PAV location."""

    return {
        "pid": "xyzrins:publications/pav-acceptance-probe",
        "schema_type": "xyzri:XYZPublication",
        "title": "PAV acceptance probe",
        "annotations": deepcopy(annotations),
        "attributed_to": [
            {
                "object": "xyzrins:persons/pav-acceptance-probe",
                "schema_type": "dlthings:Attribution",
                "annotations": deepcopy(annotations),
            }
        ],
        "attributes": [
            {
                "predicate": "dcterms:title",
                "value": "PAV acceptance probe",
                "schema_type": "dlthings:AttributeSpecification",
                "annotations": deepcopy(annotations),
            }
        ],
        "identifiers": [
            {
                "notation": "pav-acceptance-probe",
                "schema_type": "dlthings:Identifier",
                "annotations": deepcopy(annotations),
            }
        ],
        "generated_by": [
            {
                "object": "xyzrins:projects/pav-acceptance-probe",
                "schema_type": "dlthings:Generation",
                "annotations": deepcopy(annotations),
            }
        ],
    }


def file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@unittest.skipUnless(
    RUNTIME_ACCEPTANCE_AVAILABLE,
    "requires the locked root Orinoco Lite environment",
)
class PavAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = load_workspace(ROOT)
        cls.lock = load_workspace_lock(cls.workspace)
        if cls.lock.engine_version != "0.1.12":
            raise AssertionError("acceptance requires locked engine 0.1.12")
        if importlib.metadata.version("orinoco-lite") != "0.1.12":
            raise AssertionError("acceptance requires installed engine 0.1.12")

        configured = os.environ.get("ORINOCO_RUNTIME_ROOT")
        candidates = []
        if configured:
            candidates.append(Path(configured))
        candidates.append(
            ROOT
            / ".orinoco/runtime"
            / f"{cls.lock.runtime.version}-{cls.lock.runtime.sha256[:12]}"
        )
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            try:
                report = verify_runtime_directory(
                    candidate,
                    expected_release=cls.lock.runtime.version,
                    expected_manifest_sha256=cls.lock.runtime.manifest_sha256,
                )
            except OrinocoError:
                continue
            cls.runtime = report.root
            break
        else:
            raise unittest.SkipTest(
                "locked runtime is not installed; run `pixi run verify-runtime` "
                "before this offline acceptance"
            )

        if cls.lock.runtime.version != "0.1.12":
            raise AssertionError("acceptance requires locked runtime 0.1.12")
        schema = cls.runtime / "schema/demo-research-information/unreleased.yaml"
        cls.to_ttl, cls.to_json = build_format_converters(schema)

    def workspace_for_records(self, records: Path):
        relative = records.relative_to(ROOT).as_posix()
        return replace(
            self.workspace,
            paths={**self.workspace.paths, "records": relative},
        )

    def write_records(
        self,
        records: Path,
        values: list[dict[str, object]],
    ) -> None:
        records.mkdir(parents=True, exist_ok=True)
        for index, value in enumerate(values):
            path = records / f"record-{index}.yaml"
            path.write_text(
                yaml.safe_dump(value, sort_keys=False),
                encoding="utf-8",
            )

    def test_expanded_keyed_pav_round_trips_at_proven_locations(self) -> None:
        record = record_with_annotations(PAV)

        first_ttl = self.to_ttl.convert(record, "XYZPublication")
        restored = self.to_json.convert(first_ttl, "XYZPublication")
        second_ttl = self.to_ttl.convert(restored, "XYZPublication")

        self.assertEqual(restored["annotations"], PAV)
        for field in (
            "attributed_to",
            "attributes",
            "identifiers",
            "generated_by",
        ):
            with self.subTest(field=field):
                self.assertEqual(restored[field][0]["annotations"], PAV)

        first_graph = Graph().parse(data=first_ttl, format="turtle")
        second_graph = Graph().parse(data=second_ttl, format="turtle")
        self.assertTrue(isomorphic(first_graph, second_graph))
        annotation_tag = URIRef(
            "https://concepts.datalad.org/s/things/v2/annotation_tag"
        )
        for tag in ("importedBy", "importedFrom"):
            with self.subTest(tag=tag):
                tagged = list(
                    first_graph.triples(
                        (
                            None,
                            annotation_tag,
                            URIRef(f"http://purl.org/pav/{tag}"),
                        )
                    )
                )
                self.assertEqual(len(tagged), 5)

    def test_compact_nested_pav_normalizes_and_fails_closed(self) -> None:
        for field in (
            "attributed_to",
            "attributes",
            "identifiers",
            "generated_by",
        ):
            with self.subTest(field=field):
                record = record_with_annotations(PAV)
                record[field][0]["annotations"] = deepcopy(COMPACT_PAV)
                restored = self.to_json.convert(
                    self.to_ttl.convert(record, "XYZPublication"),
                    "XYZPublication",
                )
                self.assertEqual(restored[field][0]["annotations"], PAV)
                self.assertNotEqual(
                    _native_fingerprint(record),
                    _native_fingerprint(restored),
                )

        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        for field in ("attributed_to", "attributes"):
            with self.subTest(native_validation=field), tempfile.TemporaryDirectory(
                prefix="pav-native-",
                dir=build,
            ) as temporary:
                records = Path(temporary) / "records"
                probe = record_with_annotations(PAV)
                probe.pop("annotations")
                for other in (
                    "attributed_to",
                    "attributes",
                    "identifiers",
                    "generated_by",
                ):
                    if other != field:
                        probe.pop(other)
                probe[field][0]["annotations"] = deepcopy(COMPACT_PAV)
                self.write_records(
                    records,
                    [
                        {
                            "pid": "xyzrins:.",
                            "schema_type": "xyzri:XYZProject",
                            "title": "PAV acceptance root",
                        },
                        {
                            "pid": "xyzrins:persons/pav-acceptance-probe",
                            "schema_type": "xyzri:XYZPerson",
                            "given_name": "PAV",
                            "family_name": "Probe",
                        },
                        probe,
                    ],
                )
                with self.assertRaisesRegex(
                    DriverError,
                    "schema round trip changed native semantics",
                ):
                    validate_semantics(
                        self.workspace_for_records(records),
                        self.runtime,
                    )

    def test_top_level_pav_projection_is_exact_deterministic_and_private(self) -> None:
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="pav-projection-",
            dir=build,
        ) as temporary:
            temporary_root = Path(temporary)
            records = temporary_root / "records"
            shutil.copytree(ROOT / "metadata/records", records)
            workspace = self.workspace_for_records(records)

            baseline = temporary_root / "baseline"
            baseline_report = render_projection(
                workspace,
                self.runtime,
                baseline,
            )

            source = records / "XYZPublication/datalad-joss-2021.yaml"
            record = yaml.safe_load(source.read_text(encoding="utf-8"))
            record["annotations"] = deepcopy(PAV)
            source.write_text(
                yaml.safe_dump(record, sort_keys=False),
                encoding="utf-8",
            )

            first = temporary_root / "first"
            second = temporary_root / "second"
            first_report = render_projection(workspace, self.runtime, first)
            second_report = render_projection(workspace, self.runtime, second)

            self.assertEqual(baseline_report, EXPECTED_PROJECTION)
            self.assertEqual(first_report, EXPECTED_PROJECTION)
            self.assertEqual(second_report, EXPECTED_PROJECTION)
            self.assertEqual(file_bytes(first), file_bytes(second))
            self.assertEqual(
                file_bytes(baseline / "content"),
                file_bytes(first / "content"),
            )
            self.assertEqual(
                (baseline / "static/graph.json").read_bytes(),
                (first / "static/graph.json").read_bytes(),
            )

            lines = (first / "records.jsonl").read_text(
                encoding="utf-8"
            ).splitlines(keepends=True)
            self.assertEqual(len(lines), 199)
            matching = [
                line
                for line in lines
                if json.loads(line).get("pid") == record["pid"]
            ]
            self.assertEqual(
                matching,
                [
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                ],
            )
            self.assertEqual(json.loads(matching[0])["annotations"], PAV)

            graph = json.loads(
                (first / "static/graph.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(graph["nodes"]), 186)
            self.assertEqual(len(graph["edges"]), 467)
            public = b"".join(file_bytes(first / "content").values())
            public += (first / "static/graph.json").read_bytes()
            for marker in (
                b"pav:importedBy",
                b"pav:importedFrom",
                IMPORTED_BY.encode(),
                IMPORTED_FROM.encode(),
            ):
                with self.subTest(marker=marker):
                    self.assertNotIn(marker, public)


if __name__ == "__main__":
    unittest.main()
