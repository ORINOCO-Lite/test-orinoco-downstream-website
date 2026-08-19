from __future__ import annotations

from copy import deepcopy
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "orinoco_curation_prototype_v1",
    ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
)
assert SPEC is not None and SPEC.loader is not None
CORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)

ADAPTERS = ("zotero", "dump-research-info")


def candidate(
    adapter_id: str,
    *,
    record_id: str = "SOURCE-1",
    claim_kind: str = "create-record",
    material_title: str = "Candidate title",
    policy_version: int = 1,
    path: str | None = None,
    pid: str | None = None,
    baseline: dict[str, object] | None = None,
    blockers: tuple[str, ...] = (),
):
    pid = pid or f"record:{adapter_id}:{record_id.lower()}"
    path = path or f"XYZProject/{adapter_id}-{record_id.lower()}.yaml"
    return CORE.make_candidate(
        adapter_id=adapter_id,
        source_namespace=f"fixture:{adapter_id}",
        source_record_id=record_id,
        claim_kind=claim_kind,
        material={"title": material_title, "source_record_id": record_id},
        relevant_policy={"matching_policy_version": policy_version},
        proposed_path=path,
        proposed_record={
            "pid": pid,
            "schema_type": "xyzri:XYZProject",
            "name": material_title,
            "pav:importedBy": f"adapter:{adapter_id}",
            "pav:importedFrom": f"fixture:{adapter_id}/{record_id}",
        },
        baseline_record=baseline,
        blockers=blockers,
    )


def decision(
    candidate_value,
    disposition: str,
    *,
    supersedes_decision_id: str | None = None,
    reviewer: str = "reviewer@example.invalid",
    decided_on: str = "2026-08-18",
    rationale: str | None = None,
    evidence: tuple[str, ...] = ("fixture:milestone-5-behavior-vector",),
    **conditional: object,
):
    claim_revision_id = CORE.claim_revision_identity(
        candidate_value.candidate_id,
        candidate_value.material_fingerprint,
        candidate_value.relevant_policy_fingerprint,
    )
    result: dict[str, object] = {
        "claim_revision_id": claim_revision_id,
        "supersedes_decision_id": supersedes_decision_id,
        "candidate_id": candidate_value.candidate_id,
        "adapter_id": candidate_value.adapter_id,
        "source_namespace": candidate_value.source_namespace,
        "source_record_id": candidate_value.source_record_id,
        "claim_kind": candidate_value.claim_kind,
        "material_fingerprint": candidate_value.material_fingerprint,
        "relevant_policy_fingerprint": (candidate_value.relevant_policy_fingerprint),
        "disposition": disposition,
        "reviewer": reviewer,
        "decided_on": decided_on,
        "rationale": rationale or f"Synthetic {disposition} behavior vector.",
        "evidence": list(evidence),
    }
    result.update(conditional)
    result["decision_id"] = CORE.decision_identity(
        claim_revision_id=claim_revision_id,
        supersedes_decision_id=supersedes_decision_id,
        disposition=disposition,
        reviewer=reviewer,
        decided_on=decided_on,
        rationale=str(result["rationale"]),
        evidence=evidence,
        target_record_id=conditional.get("target_record_id"),
        return_when=conditional.get("return_when"),
        scope=conditional.get("scope"),
        replacement_candidate_id=conditional.get("replacement_candidate_id"),
    )
    return result


def decision_book(
    inventory: dict[str, object],
    decisions: list[dict[str, object]],
    *,
    current_ids: list[str] | None = None,
    historical_transactions: list[dict[str, object]] | None = None,
):
    if current_ids is None:
        revisions = {
            (
                entry["candidate_id"],
                entry["material_fingerprint"],
                entry["relevant_policy_fingerprint"],
            )
            for entry in inventory["candidates"]
        }
        current_ids = [
            str(raw["decision_id"])
            for raw in decisions
            if (
                raw["candidate_id"],
                raw["material_fingerprint"],
                raw["relevant_policy_fingerprint"],
            )
            in revisions
        ]
    transactions = list(historical_transactions or [])
    transactions.append(
        {
            "inventory_id": inventory["inventory_id"],
            "decision_ids": current_ids,
        }
    )
    document = {
        "format": CORE.DECISIONS_FORMAT,
        "decisions": decisions,
        "transactions": transactions,
    }
    return CORE.parse_decisions(yaml.safe_dump(document, sort_keys=False))


def write_record(root: Path, relative: str, value: dict[str, object]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class SharedAdapterBehaviorVectors(unittest.TestCase):
    def test_same_core_exposes_undecided_candidates_for_both_adapters(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id):
                value = candidate(adapter_id)
                inventory = CORE.build_inventory(adapter_id, [value])
                parsed = CORE.parse_inventory(inventory)

                self.assertEqual(parsed.adapter_id, adapter_id)
                self.assertEqual(len(parsed.candidates), 1)
                self.assertEqual(
                    parsed.review[value.candidate_id]["reason"],
                    "no-prior-decision",
                )
                self.assertEqual(inventory["format"], CORE.INVENTORY_FORMAT)

    def test_candidate_identity_ignores_run_ids_and_change_fingerprints(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id):
                first = candidate(adapter_id, material_title="First", policy_version=1)
                # Run ids are deliberately not accepted by make_candidate and remain
                # execution provenance outside the stable candidate identity.
                run_a = "run-2026-08-18-a"
                run_b = "run-2026-08-18-b"
                second = candidate(
                    adapter_id, material_title="Second", policy_version=2
                )

                self.assertNotEqual(run_a, run_b)
                self.assertEqual(first.candidate_id, second.candidate_id)
                self.assertNotEqual(
                    first.material_fingerprint, second.material_fingerprint
                )
                self.assertNotEqual(
                    first.relevant_policy_fingerprint,
                    second.relevant_policy_fingerprint,
                )

    def test_material_fingerprint_binds_path_and_materialized_proposal(self) -> None:
        original = candidate("zotero")
        changed_path = candidate("zotero", path="XYZProject/a-different-target.yaml")
        changed_record = CORE.make_candidate(
            adapter_id=original.adapter_id,
            source_namespace=original.source_namespace,
            source_record_id=original.source_record_id,
            claim_kind=original.claim_kind,
            material={"title": "Candidate title", "source_record_id": "SOURCE-1"},
            relevant_policy={"matching_policy_version": 1},
            proposed_path=original.proposed_path,
            proposed_record={
                **original.proposed_record,
                "extra": "materialized change",
            },
        )

        self.assertEqual(original.candidate_id, changed_path.candidate_id)
        self.assertEqual(original.candidate_id, changed_record.candidate_id)
        self.assertNotEqual(
            original.material_fingerprint, changed_path.material_fingerprint
        )
        self.assertNotEqual(
            original.material_fingerprint, changed_record.material_fingerprint
        )

    def test_accept_reconciles_and_positive_state_deduplicates(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                records = Path(tmp) / "metadata/records"
                records.mkdir(parents=True)
                value = candidate(adapter_id)
                inventory = CORE.build_inventory(adapter_id, [value])
                book = decision_book(inventory, [decision(value, "accept")])

                first = CORE.reconcile_inventory(inventory, book, records)
                second = CORE.reconcile_inventory(inventory, book, records)
                next_inventory = CORE.build_inventory(
                    adapter_id, [value], book, metadata_dir=records
                )

                self.assertTrue(first["changed"])
                self.assertFalse(second["changed"])
                self.assertEqual(next_inventory["candidates"], [])
                accepted = yaml.safe_load(
                    (records / value.proposed_path).read_text(encoding="utf-8")
                )
                self.assertEqual(accepted, value.proposed_record)

    def test_unchanged_rejection_survives_nonmaterial_and_unrelated_changes(
        self,
    ) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id):
                value = candidate(adapter_id)
                inventory = CORE.build_inventory(adapter_id, [value])
                book = decision_book(inventory, [decision(value, "reject")])

                nonmaterial_source_note = "changed acquisition timestamp"
                unrelated_policy = {"presentation_policy_version": 99}
                rerun = candidate(adapter_id)

                self.assertTrue(nonmaterial_source_note)
                self.assertTrue(unrelated_policy)
                self.assertEqual(
                    CORE.build_inventory(adapter_id, [rerun], book)["candidates"],
                    [],
                )

    def test_material_and_relevant_policy_changes_reopen_review(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id):
                original = candidate(adapter_id)
                initial = CORE.build_inventory(adapter_id, [original])
                book = decision_book(initial, [decision(original, "reject")])
                changed_material = candidate(
                    adapter_id, material_title="Materially changed title"
                )
                changed_policy = candidate(adapter_id, policy_version=2)

                material_inventory = CORE.build_inventory(
                    adapter_id, [changed_material], book
                )
                policy_inventory = CORE.build_inventory(
                    adapter_id, [changed_policy], book
                )

                self.assertEqual(
                    material_inventory["candidates"][0]["review"]["reason"],
                    "stale-material",
                )
                self.assertEqual(
                    policy_inventory["candidates"][0]["review"]["reason"],
                    "stale-relevant-policy",
                )
                self.assertEqual(
                    material_inventory["candidates"][0]["review"]["prior_disposition"],
                    "reject",
                )

    def test_deferral_returns_only_on_its_declared_condition(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id):
                value = candidate(adapter_id)
                initial = CORE.build_inventory(adapter_id, [value])
                book = decision_book(
                    initial,
                    [
                        decision(
                            value,
                            "defer",
                            return_when={
                                "kind": "on-or-after",
                                "date": "2026-09-01",
                            },
                        )
                    ],
                )

                before = CORE.build_inventory(
                    adapter_id,
                    [value],
                    book,
                    context=CORE.EvaluationContext(as_of=date(2026, 8, 31)),
                )
                changed_before_date = CORE.build_inventory(
                    adapter_id,
                    [
                        candidate(
                            adapter_id,
                            material_title="Changed before declared return date",
                            policy_version=2,
                        )
                    ],
                    book,
                    context=CORE.EvaluationContext(as_of=date(2026, 8, 31)),
                )
                on_date = CORE.build_inventory(
                    adapter_id,
                    [value],
                    book,
                    context=CORE.EvaluationContext(as_of=date(2026, 9, 1)),
                )

                self.assertEqual(before["candidates"], [])
                self.assertEqual(changed_before_date["candidates"], [])
                self.assertEqual(
                    on_date["candidates"][0]["review"]["reason"],
                    "defer-return-on-or-after",
                )

    def test_material_and_named_policy_deferral_conditions(self) -> None:
        value = candidate("zotero")
        initial = CORE.build_inventory("zotero", [value])
        material_book = decision_book(
            initial,
            [
                decision(
                    value,
                    "defer",
                    return_when={"kind": "material-change"},
                )
            ],
        )
        changed = candidate("zotero", material_title="Changed")
        self.assertEqual(
            CORE.build_inventory("zotero", [changed], material_book)["candidates"][0][
                "review"
            ]["reason"],
            "defer-return-material-change",
        )

        policy_book = decision_book(
            initial,
            [
                decision(
                    value,
                    "defer",
                    return_when={
                        "kind": "policy-question-resolved",
                        "question": "M5-Q-content-policy",
                    },
                )
            ],
        )
        unresolved = CORE.build_inventory("zotero", [value], policy_book)
        resolved = CORE.build_inventory(
            "zotero",
            [value],
            policy_book,
            context=CORE.EvaluationContext(
                as_of=date(2026, 8, 18),
                resolved_policy_questions=frozenset({"M5-Q-content-policy"}),
            ),
        )
        self.assertEqual(unresolved["candidates"], [])
        self.assertEqual(
            resolved["candidates"][0]["review"]["reason"],
            "defer-return-policy-question-resolved",
        )

    def test_permanent_exclusion_requires_and_obeys_explicit_scope(self) -> None:
        value = candidate("dump-research-info")
        initial = CORE.build_inventory("dump-research-info", [value])
        without_scope = decision(
            value,
            "permanent-exclude",
            scope={
                "adapter_id": value.adapter_id,
                "source_namespace": value.source_namespace,
                "source_record_id": value.source_record_id,
                "claim_kind": value.claim_kind,
                "material_changes": True,
                "relevant_policy_changes": True,
            },
        )
        without_scope.pop("scope")
        with self.assertRaisesRegex(
            CORE.CurationPrototypeError, "missing or unexpected"
        ):
            decision_book(initial, [without_scope])

        scoped = decision(
            value,
            "permanent-exclude",
            scope={
                "adapter_id": value.adapter_id,
                "source_namespace": value.source_namespace,
                "source_record_id": value.source_record_id,
                "claim_kind": value.claim_kind,
                "material_changes": True,
                "relevant_policy_changes": False,
            },
        )
        book = decision_book(initial, [scoped])
        material_change = candidate(
            "dump-research-info", material_title="Changed forever-excluded claim"
        )
        policy_change = candidate("dump-research-info", policy_version=2)

        self.assertEqual(
            CORE.build_inventory("dump-research-info", [material_change], book)[
                "candidates"
            ],
            [],
        )
        self.assertEqual(
            CORE.build_inventory("dump-research-info", [policy_change], book)[
                "candidates"
            ][0]["review"]["reason"],
            "stale-relevant-policy",
        )

    def test_wildcard_permanent_scope_matches_future_candidates_uniquely(self) -> None:
        seed = candidate("zotero", record_id="SEED")
        initial = CORE.build_inventory("zotero", [seed])
        exclusion = decision(
            seed,
            "permanent-exclude",
            scope={
                "adapter_id": seed.adapter_id,
                "source_namespace": seed.source_namespace,
                "source_record_id": "*",
                "claim_kind": seed.claim_kind,
                "material_changes": True,
                "relevant_policy_changes": True,
            },
        )
        book = decision_book(initial, [exclusion])
        future = candidate("zotero", record_id="FUTURE")
        different_claim = candidate(
            "zotero", record_id="FUTURE", claim_kind="enrichment"
        )

        self.assertEqual(
            CORE.build_inventory("zotero", [future], book)["candidates"], []
        )
        self.assertEqual(
            len(CORE.build_inventory("zotero", [different_claim], book)["candidates"]),
            1,
        )

    def test_overlapping_active_permanent_scopes_fail_as_ambiguous(self) -> None:
        first = candidate("zotero", record_id="FIRST")
        second = candidate("zotero", record_id="SECOND")
        inventory = CORE.build_inventory("zotero", [first, second])

        def broad_exclusion(value):
            return decision(
                value,
                "permanent-exclude",
                scope={
                    "adapter_id": value.adapter_id,
                    "source_namespace": value.source_namespace,
                    "source_record_id": "*",
                    "claim_kind": value.claim_kind,
                    "material_changes": True,
                    "relevant_policy_changes": True,
                },
            )

        book = decision_book(
            inventory, [broad_exclusion(first), broad_exclusion(second)]
        )
        future = candidate("zotero", record_id="FUTURE")
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Ambiguous"):
            CORE.build_inventory("zotero", [future], book)


class DecisionValidationVectors(unittest.TestCase):
    def test_malformed_duplicate_and_unknown_decisions_fail_closed(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Malformed"):
            CORE.parse_decisions("format: [unterminated")

        duplicate = decision(value, "reject")
        with self.assertRaisesRegex(
            CORE.CurationPrototypeError, "Duplicate durable decision event"
        ):
            decision_book(inventory, [duplicate, deepcopy(duplicate)])

        unknown = decision(value, "reject")
        unknown["disposition"] = "silently-ignore"
        with self.assertRaisesRegex(
            CORE.CurationPrototypeError, "Unsupported disposition"
        ):
            decision_book(inventory, [unknown])

        malformed_inventory = decision(value, "reject")
        malformed_document = {
            "format": CORE.DECISIONS_FORMAT,
            "decisions": [malformed_inventory],
            "transactions": [
                {
                    "inventory_id": f"{CORE.INVENTORY_PREFIX}truncated",
                    "decision_ids": [malformed_inventory["decision_id"]],
                }
            ],
        }
        with self.assertRaisesRegex(
            CORE.CurationPrototypeError,
            "transaction inventory_id has an unsupported identity format",
        ):
            CORE.parse_decisions(yaml.safe_dump(malformed_document, sort_keys=False))

    def test_missing_audit_fields_invalid_dates_and_empty_evidence_fail(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        for mutation, diagnostic in (
            (lambda raw: raw.pop("reviewer"), "missing or unexpected"),
            (lambda raw: raw.update(decided_on="August 18"), "ISO calendar"),
            (lambda raw: raw.update(rationale=""), "rationale"),
            (lambda raw: raw.update(evidence=[]), "evidence"),
        ):
            with self.subTest(diagnostic=diagnostic):
                raw = decision(value, "reject")
                mutation(raw)
                with self.assertRaisesRegex(CORE.CurationPrototypeError, diagnostic):
                    decision_book(inventory, [raw])

    def test_contradictory_permanent_scope_and_supersede_cycle_fail(self) -> None:
        broad = candidate("zotero", record_id="SHARED", claim_kind="create")
        other = candidate("zotero", record_id="SHARED", claim_kind="enrich")
        inventory = CORE.build_inventory("zotero", [broad, other])
        exclusion = decision(
            broad,
            "permanent-exclude",
            scope={
                "adapter_id": "zotero",
                "source_namespace": broad.source_namespace,
                "source_record_id": "SHARED",
                "claim_kind": "*",
                "material_changes": True,
                "relevant_policy_changes": True,
            },
        )
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Contradictory"):
            decision_book(inventory, [exclusion, decision(other, "accept")])

        first = candidate("zotero", record_id="FIRST")
        second = candidate("zotero", record_id="SECOND")
        cycle_inventory = CORE.build_inventory("zotero", [first, second])
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "cycle"):
            decision_book(
                cycle_inventory,
                [
                    decision(
                        first,
                        "supersede",
                        replacement_candidate_id=second.candidate_id,
                    ),
                    decision(
                        second,
                        "supersede",
                        replacement_candidate_id=first.candidate_id,
                    ),
                ],
            )

    def test_revision_chain_retains_dormant_history_without_reactivation(self) -> None:
        original = candidate("zotero", material_title="Original")
        original_inventory = CORE.build_inventory("zotero", [original])
        old = decision(original, "reject")
        old_book = decision_book(original_inventory, [old])
        changed = candidate("zotero", material_title="Changed")
        changed_inventory = CORE.build_inventory("zotero", [changed], old_book)
        new = decision(
            changed,
            "reject",
            supersedes_decision_id=str(old["decision_id"]),
        )
        book = decision_book(
            changed_inventory,
            [old, new],
            historical_transactions=[
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [old["decision_id"]],
                }
            ],
        )

        self.assertEqual(len(book.revisions(original.candidate_id)), 2)
        self.assertEqual(
            book.active(original.candidate_id).decision_id,
            new["decision_id"],
        )
        self.assertEqual(
            CORE.build_inventory("zotero", [changed], book)["candidates"], []
        )
        reverted = CORE.build_inventory("zotero", [original], book)
        self.assertEqual(
            reverted["candidates"][0]["review"]["reason"], "stale-material"
        )

    def test_same_inventory_correction_selects_only_the_active_tip(self) -> None:
        value = candidate("dump-research-info")
        inventory = CORE.build_inventory("dump-research-info", [value])
        first = decision(value, "reject", rationale="Initial review was mistaken.")
        corrected = decision(
            value,
            "accept",
            supersedes_decision_id=str(first["decision_id"]),
            rationale="Corrected after reviewing the full source evidence.",
        )

        book = decision_book(
            inventory,
            [first, corrected],
            current_ids=[str(corrected["decision_id"])],
        )

        self.assertEqual(
            book.transactions[inventory["inventory_id"]],
            (corrected["decision_id"],),
        )
        self.assertEqual(
            [item.decision_id for item in book.revisions(value.candidate_id)],
            [first["decision_id"], corrected["decision_id"]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            CORE.reconcile_inventory(inventory, book, records)
            self.assertTrue((records / value.proposed_path).exists())

    def test_selected_tip_requires_complete_and_exclusive_ancestry(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        first = decision(value, "reject")
        corrected = decision(
            value,
            "accept",
            supersedes_decision_id=str(first["decision_id"]),
        )

        missing_history = {
            "format": CORE.DECISIONS_FORMAT,
            "decisions": [corrected],
            "transactions": [
                {
                    "inventory_id": inventory["inventory_id"],
                    "decision_ids": [corrected["decision_id"]],
                }
            ],
        }
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "missing revision"):
            CORE.parse_decisions(yaml.safe_dump(missing_history, sort_keys=False))

        unanchored_value = candidate("zotero", record_id="UNANCHORED")
        unanchored = decision(unanchored_value, "reject")
        unanchored_history = {
            "format": CORE.DECISIONS_FORMAT,
            "decisions": [first, corrected, unanchored],
            "transactions": [
                {
                    "inventory_id": inventory["inventory_id"],
                    "decision_ids": [corrected["decision_id"]],
                }
            ],
        }
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Unbound"):
            CORE.parse_decisions(yaml.safe_dump(unanchored_history, sort_keys=False))

        multiple_tips = deepcopy(unanchored_history)
        multiple_tips["decisions"] = [first, corrected]
        multiple_tips["transactions"][0]["decision_ids"] = [
            first["decision_id"],
            corrected["decision_id"],
        ]
        with self.assertRaisesRegex(
            CORE.CurationPrototypeError, "selects multiple tips"
        ):
            CORE.parse_decisions(yaml.safe_dump(multiple_tips, sort_keys=False))

    def test_reverted_claim_revision_gets_a_new_review_event_and_reconciles(
        self,
    ) -> None:
        original = candidate("zotero", material_title="Version one")
        original_inventory = CORE.build_inventory("zotero", [original])
        first = decision(original, "reject")
        first_book = decision_book(original_inventory, [first])

        changed = candidate("zotero", material_title="Version two")
        changed_inventory = CORE.build_inventory("zotero", [changed], first_book)
        second = decision(
            changed,
            "reject",
            supersedes_decision_id=str(first["decision_id"]),
        )
        second_book = decision_book(
            changed_inventory,
            [first, second],
            historical_transactions=[
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [first["decision_id"]],
                }
            ],
        )

        reverted_inventory = CORE.build_inventory("zotero", [original], second_book)
        third = decision(
            original,
            "accept",
            supersedes_decision_id=str(second["decision_id"]),
            rationale="Re-reviewed the source after it reverted to version one.",
        )
        book = decision_book(
            reverted_inventory,
            [first, second, third],
            current_ids=[str(third["decision_id"])],
            historical_transactions=[
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [first["decision_id"]],
                },
                {
                    "inventory_id": changed_inventory["inventory_id"],
                    "decision_ids": [second["decision_id"]],
                },
            ],
        )

        revisions = book.revisions(original.candidate_id)
        self.assertEqual(len(revisions), 3)
        self.assertEqual(revisions[0].claim_revision_id, revisions[2].claim_revision_id)
        self.assertEqual(book.exact(original).decision_id, third["decision_id"])
        self.assertEqual(len({item.decision_id for item in revisions}), 3)
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            CORE.reconcile_inventory(reverted_inventory, book, records)
            accepted = yaml.safe_load(
                (records / original.proposed_path).read_text(encoding="utf-8")
            )
            self.assertEqual(accepted, original.proposed_record)

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "historical and inactive"
            ):
                CORE.reconcile_inventory(original_inventory, book, records)

    def test_triggered_deferral_can_be_superseded_without_fingerprint_change(
        self,
    ) -> None:
        value = candidate("dump-research-info")
        initial_inventory = CORE.build_inventory("dump-research-info", [value])
        deferred = decision(
            value,
            "defer",
            return_when={"kind": "on-or-after", "date": "2026-09-01"},
        )
        deferred_book = decision_book(initial_inventory, [deferred])
        returned_inventory = CORE.build_inventory(
            "dump-research-info",
            [value],
            deferred_book,
            context=CORE.EvaluationContext(as_of=date(2026, 9, 1)),
        )
        accepted = decision(
            value,
            "accept",
            supersedes_decision_id=str(deferred["decision_id"]),
            decided_on="2026-09-01",
        )
        book = decision_book(
            returned_inventory,
            [deferred, accepted],
            current_ids=[str(accepted["decision_id"])],
            historical_transactions=[
                {
                    "inventory_id": initial_inventory["inventory_id"],
                    "decision_ids": [deferred["decision_id"]],
                }
            ],
        )

        self.assertEqual(deferred["claim_revision_id"], accepted["claim_revision_id"])
        self.assertNotEqual(deferred["decision_id"], accepted["decision_id"])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            CORE.reconcile_inventory(returned_inventory, book, records)
            self.assertTrue((records / value.proposed_path).exists())

    def test_active_permanent_revision_can_follow_historical_rejection(self) -> None:
        original = candidate("zotero", material_title="Original")
        original_inventory = CORE.build_inventory("zotero", [original])
        old = decision(original, "reject")
        old_book = decision_book(original_inventory, [old])
        changed = candidate("zotero", material_title="Changed")
        changed_inventory = CORE.build_inventory("zotero", [changed], old_book)
        permanent = decision(
            changed,
            "permanent-exclude",
            supersedes_decision_id=str(old["decision_id"]),
            scope={
                "adapter_id": changed.adapter_id,
                "source_namespace": changed.source_namespace,
                "source_record_id": changed.source_record_id,
                "claim_kind": changed.claim_kind,
                "material_changes": True,
                "relevant_policy_changes": True,
            },
        )
        book = decision_book(
            changed_inventory,
            [old, permanent],
            historical_transactions=[
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [old["decision_id"]],
                }
            ],
        )

        self.assertEqual(
            book.active(original.candidate_id).disposition,
            "permanent-exclude",
        )

    def test_old_inventory_cannot_replay_a_superseded_acceptance(self) -> None:
        original = candidate("zotero", material_title="Original")
        original_inventory = CORE.build_inventory("zotero", [original])
        old_accept = decision(original, "accept")
        old_book = decision_book(original_inventory, [old_accept])
        changed = candidate("zotero", material_title="Changed")
        changed_inventory = CORE.build_inventory("zotero", [changed], old_book)
        current_reject = decision(
            changed,
            "reject",
            supersedes_decision_id=str(old_accept["decision_id"]),
        )
        book = decision_book(
            changed_inventory,
            [old_accept, current_reject],
            historical_transactions=[
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [old_accept["decision_id"]],
                }
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "historical and inactive"
            ):
                CORE.reconcile_inventory(original_inventory, book, records)
            self.assertEqual(tree_bytes(records), {})

    def test_branching_history_and_unbound_decisions_fail_closed(self) -> None:
        original = candidate("zotero", material_title="Original")
        original_inventory = CORE.build_inventory("zotero", [original])
        old = decision(original, "reject")
        first = candidate("zotero", material_title="First branch")
        second = candidate("zotero", material_title="Second branch")
        first_decision = decision(
            first,
            "reject",
            supersedes_decision_id=str(old["decision_id"]),
        )
        second_decision = decision(
            second,
            "reject",
            supersedes_decision_id=str(old["decision_id"]),
        )
        first_inventory = CORE.build_inventory("zotero", [first])
        second_inventory = CORE.build_inventory("zotero", [second])
        document = {
            "format": CORE.DECISIONS_FORMAT,
            "decisions": [old, first_decision, second_decision],
            "transactions": [
                {
                    "inventory_id": original_inventory["inventory_id"],
                    "decision_ids": [old["decision_id"]],
                },
                {
                    "inventory_id": first_inventory["inventory_id"],
                    "decision_ids": [first_decision["decision_id"]],
                },
                {
                    "inventory_id": second_inventory["inventory_id"],
                    "decision_ids": [second_decision["decision_id"]],
                },
            ],
        }
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "branches"):
            CORE.parse_decisions(yaml.safe_dump(document, sort_keys=False))

        unbound = {
            "format": CORE.DECISIONS_FORMAT,
            "decisions": [old],
            "transactions": [],
        }
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Unbound"):
            CORE.parse_decisions(yaml.safe_dump(unbound, sort_keys=False))

    def test_stale_and_missing_transaction_decisions_fail_closed(self) -> None:
        original = candidate("zotero")
        changed = candidate("zotero", material_title="Changed")
        changed_inventory = CORE.build_inventory("zotero", [changed])
        stale_book = decision_book(
            changed_inventory,
            [decision(original, "reject")],
            current_ids=[decision(original, "reject")["decision_id"]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "Missing transaction decision revisions"
            ):
                CORE.reconcile_inventory(changed_inventory, stale_book, records)

            missing_book = decision_book(
                changed_inventory,
                [],
                current_ids=[],
            )
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "Missing transaction decision revisions",
            ):
                CORE.reconcile_inventory(changed_inventory, missing_book, records)

    def test_unexpected_transaction_decision_fails_but_dormant_history_remains(
        self,
    ) -> None:
        current = candidate("dump-research-info", record_id="CURRENT")
        dormant = candidate("dump-research-info", record_id="DISAPPEARED")
        inventory = CORE.build_inventory("dump-research-info", [current])
        decisions = [decision(current, "reject"), decision(dormant, "reject")]
        unexpected = decision_book(
            inventory,
            decisions,
            current_ids=[raw["decision_id"] for raw in decisions],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "Unexpected transaction decision revisions",
            ):
                CORE.reconcile_inventory(inventory, unexpected, records)

            legitimate = decision_book(
                inventory,
                decisions,
                current_ids=[decisions[0]["decision_id"]],
                historical_transactions=[
                    {
                        "inventory_id": CORE.INVENTORY_PREFIX + "0" * 64,
                        "decision_ids": [decisions[1]["decision_id"]],
                    }
                ],
            )
            CORE.reconcile_inventory(inventory, legitimate, records)
            self.assertIn(decisions[1]["decision_id"], legitimate.decisions)

    def test_absence_or_abandonment_is_never_a_decision(self) -> None:
        value = candidate("zotero")
        first_proposal = CORE.build_inventory("zotero", [value])
        abandoned_rerun = CORE.build_inventory("zotero", [value])

        self.assertEqual(first_proposal, abandoned_rerun)
        self.assertEqual(
            abandoned_rerun["candidates"][0]["review"]["reason"],
            "no-prior-decision",
        )
        empty_book = CORE.parse_decisions(
            yaml.safe_dump(
                {
                    "format": CORE.DECISIONS_FORMAT,
                    "decisions": [],
                    "transactions": [],
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "Missing decision transaction"
            ):
                CORE.reconcile_inventory(
                    first_proposal, empty_book, Path(tmp) / "records"
                )


class ReconciliationVectors(unittest.TestCase):
    def test_link_to_unique_existing_record_prevents_duplicate_creation(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                records = Path(tmp) / "records"
                target = {
                    "pid": "record:existing",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Existing",
                }
                write_record(records, "XYZProject/existing.yaml", target)
                value = candidate(adapter_id, pid="record:would-be-duplicate")
                inventory = CORE.build_inventory(adapter_id, [value])
                book = decision_book(
                    inventory,
                    [
                        decision(
                            value,
                            "link",
                            target_record_id="record:existing",
                        )
                    ],
                )

                CORE.reconcile_inventory(inventory, book, records)

                self.assertEqual(
                    yaml.safe_load((records / "XYZProject/existing.yaml").read_text()),
                    target,
                )
                self.assertFalse((records / value.proposed_path).exists())

    def test_missing_and_ambiguous_link_targets_fail_closed(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(
            inventory,
            [decision(value, "link", target_record_id="record:target")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            with self.assertRaisesRegex(CORE.CurationPrototypeError, "does not exist"):
                CORE.reconcile_inventory(inventory, book, records)

            target = {"pid": "record:target", "name": "Target"}
            write_record(records, "XYZProject/one.yaml", target)
            write_record(records, "XYZProject/two.yaml", target)
            before = tree_bytes(records)
            with self.assertRaisesRegex(CORE.CurationPrototypeError, "ambiguous"):
                CORE.reconcile_inventory(inventory, book, records)
            self.assertEqual(tree_bytes(records), before)

    def test_supersede_leaves_only_the_intended_active_record(self) -> None:
        old = candidate("zotero", record_id="OLD", pid="record:old")
        replacement = candidate("zotero", record_id="NEW", pid="record:replacement")
        inventory = CORE.build_inventory("zotero", [old, replacement])
        book = decision_book(
            inventory,
            [
                decision(
                    old,
                    "supersede",
                    replacement_candidate_id=replacement.candidate_id,
                ),
                decision(replacement, "accept"),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            CORE.reconcile_inventory(inventory, book, records)

            self.assertFalse((records / old.proposed_path).exists())
            self.assertTrue((records / replacement.proposed_path).exists())
            self.assertEqual(
                CORE.build_inventory(
                    "zotero", [old, replacement], book, metadata_dir=records
                )["candidates"],
                [],
            )

    def test_all_rejected_is_a_durable_decision_only_transition(self) -> None:
        values = [
            candidate("dump-research-info", record_id="ONE"),
            candidate("dump-research-info", record_id="TWO"),
        ]
        inventory = CORE.build_inventory("dump-research-info", values)
        raw_decisions = [decision(value, "reject") for value in values]
        book = decision_book(inventory, raw_decisions)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "metadata/records"
            records.mkdir(parents=True)
            decisions_path = root / "source-adapters/policy/decisions.yaml"
            decisions_path.parent.mkdir(parents=True)
            decision_bytes = yaml.safe_dump(
                {
                    "format": CORE.DECISIONS_FORMAT,
                    "decisions": raw_decisions,
                    "transactions": [
                        {
                            "inventory_id": inventory["inventory_id"],
                            "decision_ids": [
                                raw["decision_id"] for raw in raw_decisions
                            ],
                        }
                    ],
                },
                sort_keys=False,
            ).encode()
            decisions_path.write_bytes(decision_bytes)

            planned_reports: list[object] = []
            finalized_reports: list[object] = []
            report = CORE.reconcile_inventory(
                inventory,
                book,
                records,
                before_commit=planned_reports.append,
                after_commit=finalized_reports.append,
            )

            self.assertFalse(report["changed"])
            self.assertEqual(len(planned_reports), 1)
            self.assertEqual(len(finalized_reports), 1)
            self.assertEqual(dict(planned_reports[0]), report)
            self.assertEqual(dict(finalized_reports[0]), report)
            self.assertEqual(tree_bytes(records), {})
            self.assertEqual(decisions_path.read_bytes(), decision_bytes)

    def test_blocked_candidate_cannot_be_accepted(self) -> None:
        value = candidate(
            "dump-research-info",
            blockers=("unresolved-relation:organization:missing",),
        )
        inventory = CORE.build_inventory("dump-research-info", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            with self.assertRaisesRegex(CORE.CurationPrototypeError, "Blocked"):
                CORE.reconcile_inventory(inventory, book, records)
            self.assertEqual(tree_bytes(records), {})

    def test_stale_baseline_rejects_concurrent_canonical_edits(self) -> None:
        baseline = {
            "pid": "record:existing",
            "schema_type": "xyzri:XYZProject",
            "name": "Reviewed baseline",
        }
        value = candidate(
            "zotero",
            path="XYZProject/existing.yaml",
            pid="record:existing",
            baseline=baseline,
        )
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            concurrent = {**baseline, "name": "Concurrent human edit"}
            write_record(records, value.proposed_path, concurrent)
            before = tree_bytes(records)

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "Stale candidate baseline"
            ):
                CORE.reconcile_inventory(inventory, book, records)

            self.assertEqual(tree_bytes(records), before)

    def test_nonaccept_disposition_is_an_exact_canonical_noop(self) -> None:
        baseline = {
            "pid": "record:existing",
            "schema_type": "xyzri:XYZProject",
            "name": "Baseline",
        }
        value = candidate(
            "dump-research-info",
            path="XYZProject/existing.yaml",
            pid="record:existing",
            baseline=baseline,
        )
        inventory = CORE.build_inventory("dump-research-info", [value])
        book = decision_book(inventory, [decision(value, "reject")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            path = records / value.proposed_path
            path.parent.mkdir(parents=True)
            exact = (
                "# preserve human formatting\n"
                "pid: record:existing\n"
                "schema_type: xyzri:XYZProject\n"
                "name: Baseline\n"
            ).encode()
            path.write_bytes(exact)

            report = CORE.reconcile_inventory(inventory, book, records)

            self.assertFalse(report["changed"])
            self.assertEqual(path.read_bytes(), exact)
            self.assertEqual(
                report["outcomes"][0]["metadata_action"],
                "leave-canonical-unchanged",
            )

    def test_duplicate_pid_in_staged_tree_fails_before_activation(self) -> None:
        value = candidate("zotero", pid="record:duplicate")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            write_record(
                records,
                "XYZProject/existing.yaml",
                {"pid": "record:duplicate", "name": "Existing authority"},
            )
            before = tree_bytes(records)

            with self.assertRaisesRegex(CORE.CurationPrototypeError, "Duplicate"):
                CORE.reconcile_inventory(inventory, book, records)

            self.assertEqual(tree_bytes(records), before)

    def test_symlinked_root_and_descendant_fail_before_staging(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            symlink_root = root / "root-link"
            symlink_root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(CORE.CurationPrototypeError, "symlink"):
                CORE.reconcile_inventory(inventory, book, symlink_root)

            records = root / "records"
            records.mkdir()
            (records / "XYZProject").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(CORE.CurationPrototypeError, "symlink"):
                CORE.reconcile_inventory(inventory, book, records)
            self.assertEqual(tree_bytes(outside), {})

    def test_staged_schema_callback_failure_keeps_canonical_tree(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            baseline = {"pid": "record:baseline", "name": "Baseline"}
            write_record(records, "XYZProject/baseline.yaml", baseline)
            before = tree_bytes(records)

            def reject_staged(staged: Path) -> None:
                self.assertTrue((staged / value.proposed_path).exists())
                raise ValueError("locked schema rejected staged tree")

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "Staged metadata validation failed"
            ):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    validate_staged=reject_staged,
                )

            self.assertEqual(tree_bytes(records), before)

    def test_staged_validation_cannot_overwrite_a_concurrent_canonical_edit(
        self,
    ) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            baseline_path = "XYZProject/baseline.yaml"
            write_record(
                records,
                baseline_path,
                {"pid": "record:baseline", "name": "Initial authority"},
            )
            concurrent = {"pid": "record:baseline", "name": "Concurrent edit"}

            def mutate_canonical_during_validation(staged: Path) -> None:
                self.assertTrue((staged / value.proposed_path).exists())
                write_record(records, baseline_path, concurrent)

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "Canonical metadata changed during staged validation",
            ):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    validate_staged=mutate_canonical_during_validation,
                )

            self.assertEqual(
                yaml.safe_load((records / baseline_path).read_text(encoding="utf-8")),
                concurrent,
            )
            self.assertFalse((records / value.proposed_path).exists())

    def test_existing_reconciliation_lock_fails_closed(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            recovery_stage = Path(tmp) / f"{CORE.STAGE_PREFIX}locked-recovery"
            recovery_stage.mkdir()
            lock_path, token = CORE._acquire_reconciliation_lock(records)
            try:
                with self.assertRaisesRegex(
                    CORE.CurationPrototypeError, "Reconciliation lock already exists"
                ):
                    CORE.reconcile_inventory(inventory, book, records)
                self.assertEqual(tree_bytes(records), {})
                self.assertTrue(lock_path.exists())
                with self.assertRaisesRegex(
                    CORE.CurationPrototypeError, "Reconciliation lock already exists"
                ):
                    CORE.recover_interrupted(records)
                self.assertTrue(recovery_stage.exists())
            finally:
                CORE._release_reconciliation_lock(records, lock_path, token)
                shutil.rmtree(recovery_stage)

    @unittest.skipUnless(os.name == "posix", "stale pid probing is POSIX-only")
    def test_stale_lock_recovery_prepares_and_finalizes_one_exact_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = (Path(tmp) / "records").absolute()
            records.mkdir()
            lock_path = CORE._reconciliation_lock_path(records)
            lock_path.write_text(
                json.dumps(
                    {
                        "format": CORE.LOCK_FORMAT,
                        "pid": 999_999_999,
                        "records_dir": str(records),
                        "token": "synthetic-stale-owner",
                    }
                ),
                encoding="utf-8",
            )
            planned: list[object] = []
            finalized: list[object] = []

            def prepare(report) -> None:
                self.assertTrue(lock_path.exists())
                planned.append(report)

            def finalize(report) -> None:
                self.assertFalse(lock_path.exists())
                finalized.append(report)

            report = CORE.recover_stale_lock(
                records,
                before_commit=prepare,
                after_commit=finalize,
            )

            self.assertEqual(dict(planned[0]), report)
            self.assertEqual(dict(finalized[0]), report)
            self.assertFalse(lock_path.exists())

    def test_activation_backup_digest_is_a_second_compare_and_swap(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            baseline_path = "XYZProject/baseline.yaml"
            write_record(
                records,
                baseline_path,
                {"pid": "record:baseline", "name": "Initial authority"},
            )
            concurrent = {"pid": "record:baseline", "name": "Last-window edit"}

            def mutate_at_activation(boundary: str) -> None:
                if boundary == "before-activate":
                    write_record(records, baseline_path, concurrent)

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "changed during reconciliation before activation",
            ):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    fault_hook=mutate_at_activation,
                )

            self.assertEqual(
                yaml.safe_load((records / baseline_path).read_text(encoding="utf-8")),
                concurrent,
            )
            self.assertFalse((records / value.proposed_path).exists())

    def test_activation_never_trusts_a_foreign_backup_path(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            baseline = {"pid": "record:baseline", "name": "Baseline"}
            write_record(records, "XYZProject/baseline.yaml", baseline)
            before = tree_bytes(records)
            foreign = {"pid": "record:foreign", "name": "Foreign authority"}
            occupied_stage: Path | None = None
            occupied_backup: Path | None = None

            def occupy_backup(boundary: str) -> None:
                nonlocal occupied_backup, occupied_stage
                if boundary != "before-activate":
                    return
                stages = [
                    path
                    for path in root.glob(f"{CORE.STAGE_PREFIX}*")
                    if (path / "records").is_dir()
                ]
                self.assertEqual(len(stages), 1)
                occupied_stage = stages[0]
                occupied_backup = root / f"{occupied_stage.name}-backup"
                write_record(
                    occupied_backup,
                    "XYZProject/foreign.yaml",
                    foreign,
                )

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "backup path is already occupied",
            ):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    fault_hook=occupy_backup,
                )

            self.assertEqual(tree_bytes(records), before)
            self.assertIsNotNone(occupied_stage)
            assert occupied_stage is not None
            self.assertFalse(occupied_stage.exists())
            self.assertIsNotNone(occupied_backup)
            assert occupied_backup is not None
            self.assertEqual(
                yaml.safe_load(
                    (occupied_backup / "XYZProject/foreign.yaml").read_text(
                        encoding="utf-8"
                    )
                ),
                foreign,
            )
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "no unambiguous pre-transaction metadata authority",
            ):
                CORE.recover_interrupted(records)
            self.assertEqual(tree_bytes(records), before)
            self.assertEqual(
                yaml.safe_load(
                    (occupied_backup / "XYZProject/foreign.yaml").read_text(
                        encoding="utf-8"
                    )
                ),
                foreign,
            )

    def test_guard_or_watcher_cannot_change_the_planned_staged_tree(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        for mutation_boundary in ("guard", "installed"):
            with self.subTest(
                boundary=mutation_boundary
            ), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                records = root / "records"
                baseline = {"pid": "record:baseline", "name": "Baseline"}
                write_record(records, "XYZProject/baseline.yaml", baseline)
                before = tree_bytes(records)

                def mutate_guard(_planned_report) -> None:
                    if mutation_boundary != "guard":
                        return
                    stages = [
                        path
                        for path in root.glob(f"{CORE.STAGE_PREFIX}*")
                        if (path / "records").is_dir()
                    ]
                    self.assertEqual(len(stages), 1)
                    write_record(
                        stages[0] / "records",
                        value.proposed_path,
                        {**value.proposed_record, "name": "Guard mutation"},
                    )

                def mutate_installed(boundary: str) -> None:
                    if (
                        mutation_boundary == "installed"
                        and boundary == "after-activate"
                    ):
                        write_record(
                            records,
                            value.proposed_path,
                            {**value.proposed_record, "name": "Watcher mutation"},
                        )

                diagnostic = (
                    "Staged metadata changed"
                    if mutation_boundary == "guard"
                    else "Installed metadata does not match"
                )
                with self.assertRaisesRegex(CORE.CurationPrototypeError, diagnostic):
                    CORE.reconcile_inventory(
                        inventory,
                        book,
                        records,
                        before_commit=mutate_guard,
                        fault_hook=mutate_installed,
                    )

                self.assertEqual(tree_bytes(records), before)
                self.assertFalse((records / value.proposed_path).exists())

    def test_final_commit_guard_failure_rolls_back_before_install(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            baseline = {"pid": "record:baseline", "name": "Baseline"}
            write_record(records, "XYZProject/baseline.yaml", baseline)
            before = tree_bytes(records)

            def reject_changed_authority(planned_report) -> None:
                self.assertTrue(planned_report["changed"])
                self.assertEqual(planned_report["format"], CORE.RECONCILIATION_FORMAT)
                with self.assertRaises(TypeError):
                    planned_report["changed"] = False
                detached_outcomes = planned_report["outcomes"]
                detached_outcomes.clear()
                self.assertEqual(len(planned_report["outcomes"]), 1)
                raise RuntimeError("inventory bytes changed")

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "Final commit guard failed"
            ):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    before_commit=reject_changed_authority,
                )

            self.assertEqual(tree_bytes(records), before)
            self.assertFalse((records / value.proposed_path).exists())
            self.assertEqual(list(Path(tmp).glob(f"{CORE.STAGE_PREFIX}*")), [])

    def test_interrupted_activation_rolls_back_the_complete_tree(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        for boundary in (
            "before-activate",
            "after-backup",
            "after-activate",
            "before-backup-cleanup",
        ):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                records = Path(tmp) / "records"
                baseline = {"pid": "record:baseline", "name": "Baseline"}
                write_record(records, "XYZProject/baseline.yaml", baseline)
                before = tree_bytes(records)

                def interrupt(stage: str) -> None:
                    if stage == boundary:
                        raise RuntimeError("synthetic interruption")

                with self.assertRaisesRegex(CORE.CurationPrototypeError, "rolled back"):
                    CORE.reconcile_inventory(
                        inventory, book, records, fault_hook=interrupt
                    )

                self.assertEqual(tree_bytes(records), before)
                self.assertFalse((records / value.proposed_path).exists())
                self.assertEqual(list(Path(tmp).glob(f"{CORE.STAGE_PREFIX}*")), [])

    def test_orphan_backup_blocks_rerun_and_has_explicit_recovery(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            baseline = {"pid": "record:baseline", "name": "Baseline"}
            write_record(records, "XYZProject/baseline.yaml", baseline)
            before = tree_bytes(records)
            base = f"{CORE.STAGE_PREFIX}hard-exit"
            stage = root / base
            backup = root / f"{base}-backup"
            records.rename(backup)
            (stage / "records").mkdir(parents=True)

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "recover_interrupted"
            ):
                CORE.reconcile_inventory(inventory, book, records)

            planned_recoveries: list[object] = []
            finalized_recoveries: list[object] = []
            recovery = CORE.recover_interrupted(
                records,
                before_commit=planned_recoveries.append,
                after_commit=finalized_recoveries.append,
            )

            self.assertEqual(recovery["recovery"], "restored-pre-transaction-backup")
            self.assertEqual(
                recovery["artifacts_before"],
                sorted([stage.name, backup.name]),
            )
            self.assertEqual(dict(planned_recoveries[0]), recovery)
            self.assertEqual(dict(finalized_recoveries[0]), recovery)
            self.assertEqual(recovery["after_digest"], recovery["tree_digest"])
            self.assertEqual(tree_bytes(records), before)
            self.assertFalse(stage.exists())
            self.assertFalse(backup.exists())
            CORE.reconcile_inventory(inventory, book, records)
            self.assertTrue((records / value.proposed_path).exists())

    def test_crash_during_obsolete_backup_cleanup_keeps_installed_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            installed = {"pid": "record:installed", "name": "Installed authority"}
            write_record(records, "XYZProject/installed.yaml", installed)
            before = tree_bytes(records)
            stage = root / f"{CORE.STAGE_PREFIX}cleanup-crash"
            write_record(
                stage / "obsolete-backup",
                "XYZProject/partial.yaml",
                {"pid": "record:partial", "name": "Partial obsolete backup"},
            )

            recovery = CORE.recover_interrupted(records)

            self.assertEqual(recovery["recovery"], "discarded-never-activated-stage")
            self.assertEqual(recovery["records_dir"], "records")
            self.assertEqual(recovery["artifacts_before"], [stage.name])
            self.assertEqual(
                recovery["artifacts_removed"], [f"{CORE.STAGE_PREFIX}cleanup-crash"]
            )
            self.assertEqual(tree_bytes(records), before)
            self.assertFalse(stage.exists())

    def test_rollback_failure_retains_recoverable_backup(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            baseline = {"pid": "record:baseline", "name": "Baseline"}
            write_record(records, "XYZProject/baseline.yaml", baseline)
            before = tree_bytes(records)

            def fail_activation_and_rollback(stage: str) -> None:
                if stage in {"after-backup", "before-rollback"}:
                    raise RuntimeError(f"synthetic {stage} failure")

            with self.assertRaisesRegex(CORE.CurationPrototypeError, "recovery backup"):
                CORE.reconcile_inventory(
                    inventory,
                    book,
                    records,
                    fault_hook=fail_activation_and_rollback,
                )

            backups = list(root.glob(f"{CORE.STAGE_PREFIX}*-backup"))
            self.assertEqual(len(backups), 1)
            self.assertFalse(records.exists())
            self.assertEqual(tree_bytes(backups[0]), before)

            CORE.recover_interrupted(records)
            self.assertEqual(tree_bytes(records), before)

    def test_pav_assertion_provenance_survives_yaml_round_trip(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "accept")])
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            CORE.reconcile_inventory(inventory, book, records)
            accepted = yaml.safe_load(
                (records / value.proposed_path).read_text(encoding="utf-8")
            )

            self.assertEqual(accepted["pav:importedBy"], "adapter:zotero")
            self.assertEqual(accepted["pav:importedFrom"], "fixture:zotero/SOURCE-1")


class StaticRepositoryBoundaryVectors(unittest.TestCase):
    def test_proposal_rerun_cannot_overwrite_durable_decisions(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions_path = root / "source-adapters/zotero/policy/decisions.yaml"
            decisions_path.parent.mkdir(parents=True)
            decisions_path.write_text("human-authored: true\n", encoding="utf-8")
            before = decisions_path.read_bytes()
            inventory_path = root / "build/proposal/inventory.yaml"

            CORE.write_inventory(
                inventory_path, inventory, decisions_path=decisions_path
            )
            CORE.write_inventory(
                inventory_path, inventory, decisions_path=decisions_path
            )

            self.assertEqual(decisions_path.read_bytes(), before)
            self.assertEqual(
                CORE.load_inventory(inventory_path).inventory_id,
                inventory["inventory_id"],
            )
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError, "cannot be the durable decisions path"
            ):
                CORE.write_inventory(
                    decisions_path, inventory, decisions_path=decisions_path
                )

    def test_inventory_is_deterministic_and_cache_independent(self) -> None:
        for adapter_id in ADAPTERS:
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                value = candidate(adapter_id)
                first = CORE.build_inventory(adapter_id, [value])
                cache = Path(tmp) / "build/source-adapters/cache"
                cache.mkdir(parents=True)
                (cache / "diagnostic.json").write_text("{}\n")
                shutil.rmtree(cache)
                second = CORE.build_inventory(adapter_id, [value])

                self.assertEqual(first, second)
                self.assertEqual(
                    yaml.safe_dump(first, sort_keys=False),
                    yaml.safe_dump(second, sort_keys=False),
                )

    def test_inventory_inputs_are_deterministic_bound_and_run_id_free(self) -> None:
        value = candidate("zotero")
        first = CORE.build_inventory(
            "zotero",
            [value],
            inputs={
                "source": {
                    "library_version": 451,
                    "snapshot_sha256": "a" * 64,
                },
                "policy": {"coordinate": "site-policy-v1"},
            },
        )
        reordered = CORE.build_inventory(
            "zotero",
            [value],
            inputs={
                "policy": {"coordinate": "site-policy-v1"},
                "source": {
                    "snapshot_sha256": "a" * 64,
                    "library_version": 451,
                },
            },
        )

        self.assertEqual(first, reordered)
        self.assertEqual(CORE.parse_inventory(first).inputs, first["inputs"])
        self.assertEqual(
            first["inputs"][CORE.EVALUATION_CONTEXT_INPUT],
            {"as_of": None, "resolved_policy_questions": []},
        )
        tampered = deepcopy(first)
        tampered["inputs"]["source"]["library_version"] = 452
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "Inventory id"):
            CORE.parse_inventory(tampered)
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "run-local"):
            CORE.build_inventory(
                "zotero",
                [value],
                inputs={"source_run_id": "run-123"},
            )

        contextual = CORE.build_inventory(
            "zotero",
            [value],
            context=CORE.EvaluationContext(
                as_of=date(2026, 9, 1),
                resolved_policy_questions=frozenset({"Q-B", "Q-A"}),
            ),
        )
        self.assertEqual(
            contextual["inputs"][CORE.EVALUATION_CONTEXT_INPUT],
            {
                "as_of": "2026-09-01",
                "resolved_policy_questions": ["Q-A", "Q-B"],
            },
        )
        self.assertNotEqual(first["inventory_id"], contextual["inventory_id"])
        with self.assertRaisesRegex(CORE.CurationPrototypeError, "reserve"):
            CORE.build_inventory(
                "zotero",
                [value],
                inputs={CORE.EVALUATION_CONTEXT_INPUT: {"as_of": "forged"}},
            )

    def test_decisions_and_inventories_are_not_canonical_metadata(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        book = decision_book(inventory, [decision(value, "reject")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "metadata/records"
            records.mkdir(parents=True)
            policy = root / "source-adapters/zotero/policy"
            policy.mkdir(parents=True)
            CORE.write_inventory(policy / "inventory.yaml", inventory)

            CORE.reconcile_inventory(inventory, book, records)

            self.assertEqual(tree_bytes(records), {})
            self.assertTrue((policy / "inventory.yaml").exists())

    def test_framework_update_surface_does_not_touch_site_decisions(self) -> None:
        value = candidate("dump-research-info")
        inventory = CORE.build_inventory("dump-research-info", [value])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            framework = root / ".orinoco-lite/runtime-version"
            framework.parent.mkdir(parents=True)
            framework.write_text("old\n")
            decisions_path = (
                root / "source-adapters/dump-research-info/policy/decisions.yaml"
            )
            decisions_path.parent.mkdir(parents=True)
            decisions_path.write_text("site-owned: true\n")
            before = decisions_path.read_bytes()

            framework.write_text("new\n")
            CORE.write_inventory(root / "build/inventory.yaml", inventory)

            self.assertEqual(decisions_path.read_bytes(), before)
            self.assertEqual(framework.read_text(), "new\n")

    def test_adapter_overlap_remains_visible_with_distinct_stable_ids(self) -> None:
        zotero = candidate(
            "zotero",
            record_id="OVERLAP",
            pid="record:shared",
            path="XYZProject/shared.yaml",
        )
        dump = candidate(
            "dump-research-info",
            record_id="OVERLAP",
            pid="record:shared",
            path="XYZProject/shared.yaml",
        )

        zotero_inventory = CORE.build_inventory("zotero", [zotero])
        dump_inventory = CORE.build_inventory("dump-research-info", [dump])

        self.assertNotEqual(zotero.candidate_id, dump.candidate_id)
        self.assertEqual(len(zotero_inventory["candidates"]), 1)
        self.assertEqual(len(dump_inventory["candidates"]), 1)
        self.assertEqual(
            zotero_inventory["candidates"][0]["proposed_path"],
            dump_inventory["candidates"][0]["proposed_path"],
        )

    def test_one_branch_can_hold_proposal_decision_and_final_state(self) -> None:
        value = candidate("zotero")
        inventory = CORE.build_inventory("zotero", [value])
        raw = decision(value, "accept")
        with tempfile.TemporaryDirectory() as tmp:
            branch = Path(tmp)
            inventory_path = branch / "build/review/inventory.yaml"
            decisions_path = branch / "source-adapters/zotero/policy/decisions.yaml"
            records = branch / "metadata/records"
            records.mkdir(parents=True)
            decisions_path.parent.mkdir(parents=True)
            document = {
                "format": CORE.DECISIONS_FORMAT,
                "decisions": [raw],
                "transactions": [
                    {
                        "inventory_id": inventory["inventory_id"],
                        "decision_ids": [raw["decision_id"]],
                    }
                ],
            }
            decisions_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            CORE.write_inventory(
                inventory_path, inventory, decisions_path=decisions_path
            )

            CORE.reconcile_inventory(
                CORE.load_inventory(inventory_path),
                CORE.load_decisions(decisions_path),
                records,
            )

            self.assertTrue(inventory_path.exists())
            self.assertTrue(decisions_path.exists())
            self.assertTrue((records / value.proposed_path).exists())


if __name__ == "__main__":
    unittest.main()
