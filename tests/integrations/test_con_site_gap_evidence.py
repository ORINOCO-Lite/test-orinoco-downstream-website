from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "integrations/metadata/evidence/con-site-gap"


def tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.relative_to(EVIDENCE).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class ConSiteGapEvidenceTests(unittest.TestCase):
    def test_evidence_is_provenance_bound_and_never_canonical(self) -> None:
        report = json.loads((EVIDENCE / "report.json").read_text())
        provenance = json.loads((EVIDENCE / "provenance.json").read_text())

        self.assertFalse(report["canonical_promotion"])
        self.assertFalse(provenance["canonical_promotion"])
        self.assertEqual(
            report["inputs"]["dump_research_info_commit"],
            provenance["inputs"]["dump_research_info_commit"],
        )
        self.assertEqual(
            report["inputs"]["downstream_commit"],
            provenance["inputs"]["downstream_commit"],
        )
        self.assertEqual(
            hashlib.sha256((EVIDENCE / "extractor.py").read_bytes()).hexdigest(),
            provenance["inputs"]["extractor_sha256"],
        )
        self.assertEqual(
            tree_digest(EVIDENCE / "candidates"),
            provenance["outputs"]["candidate_tree_sha256"],
        )
        self.assertEqual(
            tree_digest(EVIDENCE / "enrichment"),
            provenance["outputs"]["enrichment_tree_sha256"],
        )
        self.assertEqual(
            provenance["outputs"]["git_tree"],
            provenance["rerun"]["output_git_tree"],
        )

    def test_candidate_inventory_matches_the_review_summary(self) -> None:
        report = json.loads((EVIDENCE / "report.json").read_text())
        observed = {
            path.stem: len(json.loads(path.read_text()))
            for path in sorted((EVIDENCE / "candidates").glob("*.json"))
        }

        self.assertEqual(observed, report["summary"]["source_only_by_class"])
        self.assertEqual(sum(observed.values()), 19)
        self.assertEqual(report["summary"]["matched_records"], 60)
        self.assertEqual(report["summary"]["ambiguous_records"], 0)
        self.assertEqual(
            report["unresolved_relation_targets"],
            [
                "xyzrins:persons/brock-wester",
                "xyzrins:persons/russell-poldrack",
            ],
        )

    def test_evidence_contains_only_ordinary_files(self) -> None:
        for path in EVIDENCE.rglob("*"):
            if path.is_dir():
                continue
            self.assertTrue(path.is_file(), path)
            self.assertFalse(path.is_symlink(), path)


if __name__ == "__main__":
    unittest.main()
