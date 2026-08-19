from __future__ import annotations

from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


CORE = load_module(
    "orinoco_curation_cli_test_core",
    ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
)
CLI = load_module(
    "orinoco_curation_cli_prototype_v1",
    ROOT / "source-adapters/metadata/tools/curation_cli_prototype_v1.py",
)


def candidate(
    adapter_id: str,
    *,
    record_id: str = "SOURCE-1",
    name: str = "Proposed",
):
    return CORE.make_candidate(
        adapter_id=adapter_id,
        source_namespace=f"fixture:{adapter_id}",
        source_record_id=record_id,
        claim_kind="record-import",
        material={"source_record_id": record_id, "name": name},
        relevant_policy={"prototype_version": 1},
        proposed_path=f"XYZProject/{adapter_id}-{record_id.lower()}.yaml",
        proposed_record={
            "pid": f"record:{adapter_id}:{record_id.lower()}",
            "schema_type": "xyzri:XYZProject",
            "name": name,
        },
    )


def write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def empty_decisions() -> dict[str, object]:
    return {
        "format": CORE.DECISIONS_FORMAT,
        "decisions": [],
        "transactions": [],
    }


def reviewed_decisions(
    inventory: dict[str, object],
    value,
    *,
    disposition: str = "accept",
) -> dict[str, object]:
    claim_revision_id = CORE.claim_revision_identity(
        value.candidate_id,
        value.material_fingerprint,
        value.relevant_policy_fingerprint,
    )
    event = {
        "claim_revision_id": claim_revision_id,
        "supersedes_decision_id": None,
        "disposition": disposition,
        "reviewer": "reviewer@example.invalid",
        "decided_on": "2026-08-18",
        "rationale": "Focused CLI reconciliation fixture.",
        "evidence": ["fixture:curation-cli-prototype-v1"],
    }
    decision_id = CORE.decision_identity(**event)
    return {
        "format": CORE.DECISIONS_FORMAT,
        "decisions": [
            {
                "decision_id": decision_id,
                **event,
                "candidate_id": value.candidate_id,
                "adapter_id": value.adapter_id,
                "source_namespace": value.source_namespace,
                "source_record_id": value.source_record_id,
                "claim_kind": value.claim_kind,
                "material_fingerprint": value.material_fingerprint,
                "relevant_policy_fingerprint": (value.relevant_policy_fingerprint),
            }
        ],
        "transactions": [
            {
                "inventory_id": inventory["inventory_id"],
                "decision_ids": [decision_id],
            }
        ],
    }


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def reviewed_fixture(
    root: Path,
    adapter_id: str = "zotero",
    *,
    disposition: str = "accept",
):
    records = root / "metadata/records"
    write_yaml(
        records / "XYZProject/baseline.yaml",
        {
            "pid": "record:baseline",
            "schema_type": "xyzri:XYZProject",
            "name": "Baseline",
        },
    )
    value = candidate(adapter_id)
    inventory = CORE.build_inventory(
        adapter_id,
        [value],
        inputs={
            "source": {"kind": "fixture", "revision": "exact"},
            "policy": {"version": 1},
        },
    )
    inventory_path = (
        root / f"source-adapters/{adapter_id}/transactions/reviewed-inventory.yaml"
    )
    decisions_path = (
        root / f"source-adapters/{adapter_id}/policy/reviewed-decisions.yaml"
    )
    CORE.write_inventory(inventory_path, inventory, decisions_path=decisions_path)
    write_yaml(
        decisions_path,
        reviewed_decisions(inventory, value, disposition=disposition),
    )
    return records, value, inventory_path, decisions_path


class CurationCliPrototypeTests(unittest.TestCase):
    def test_propose_prepares_a_fresh_provider_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "metadata/records").mkdir(parents=True)
            value = candidate("zotero")
            output = root / "build/curation/zotero/fresh-run"

            def build_candidates(_root, output_arg, **_kwargs):
                self.assertEqual(output_arg, output)
                self.assertTrue(output_arg.is_dir())
                return {
                    "adapter_id": "zotero",
                    "source": {"kind": "frozen-fixture", "revision": "exact"},
                    "policy": {"version": 1},
                    "implementation": {"provider_sha256": "a" * 64},
                    "candidates": [value],
                }

            self.assertFalse(output.exists())
            CLI.propose(
                root,
                adapter="zotero",
                inventory_path=(
                    "source-adapters/zotero/transactions/fresh-output.yaml"
                ),
                provider_output="build/curation/zotero/fresh-run",
                as_of=date(2026, 8, 18),
                expected_library_version=739,
                core=CORE,
                provider=SimpleNamespace(build_candidates=build_candidates),
            )
            self.assertTrue(output.is_dir())

    def test_propose_for_each_provider_preserves_decisions_and_records_inputs(
        self,
    ) -> None:
        vectors = (
            (
                "zotero",
                {
                    "expected_library_version": 739,
                    "source_path": None,
                    "expected_source_commit": None,
                    "source_run_id": None,
                },
                {"expected_library_version": 739},
            ),
            (
                "dump-research-info",
                {
                    "expected_library_version": None,
                    "source_path": "../dump-research-info",
                    "expected_source_commit": "a" * 40,
                    "source_run_id": "non-material-run-id",
                },
                {
                    "source_path": "../dump-research-info",
                    "expected_source_commit": "a" * 40,
                    "source_run_id": "non-material-run-id",
                },
            ),
        )
        for adapter_id, options, expected_kwargs in vectors:
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "metadata/records").mkdir(parents=True)
                value = candidate(adapter_id)
                decisions_path = (
                    root / f"source-adapters/{adapter_id}/policy/prior-decisions.yaml"
                )
                write_yaml(decisions_path, empty_decisions())
                decision_bytes = decisions_path.read_bytes()
                source_evidence = {
                    "kind": f"frozen-{adapter_id}-fixture",
                    "revision": "exact",
                }
                policy_evidence = {
                    "path": f"source-adapters/{adapter_id}/policy/site-policy.yaml",
                    "sha256": "b" * 64,
                }
                implementation_evidence = {
                    "agent": f"urn:orinoco-lite:source-adapter:{adapter_id}",
                    "provider_sha256": "c" * 64,
                    "transformer_sha256": "d" * 64,
                }
                calls: list[tuple[Path, Path, dict[str, object]]] = []

                def build_candidates(root_arg, output_arg, **kwargs):
                    calls.append((root_arg, output_arg, kwargs))
                    output_arg.mkdir(parents=True, exist_ok=True)
                    return {
                        "adapter_id": adapter_id,
                        "source": source_evidence,
                        "policy": policy_evidence,
                        "implementation": implementation_evidence,
                        "context": {"source_run_id": "ignored-context"},
                        "candidates": [value],
                    }

                inventory_path = (
                    f"source-adapters/{adapter_id}/transactions/proposal.yaml"
                )
                provider_output = f"build/curation/{adapter_id}/provider"
                inventory = CLI.propose(
                    root,
                    adapter=adapter_id,
                    inventory_path=inventory_path,
                    decisions_path=decisions_path,
                    provider_output=provider_output,
                    as_of=date(2026, 8, 18),
                    resolved_policy_questions=("M5-Q-two", "M5-Q-one"),
                    core=CORE,
                    provider=SimpleNamespace(build_candidates=build_candidates),
                    **options,
                )

                self.assertEqual(decisions_path.read_bytes(), decision_bytes)
                self.assertEqual(
                    inventory["inputs"],
                    {
                        "source": source_evidence,
                        "policy": policy_evidence,
                        "implementation": implementation_evidence,
                        "evaluation_context": {
                            "as_of": "2026-08-18",
                            "resolved_policy_questions": [
                                "M5-Q-one",
                                "M5-Q-two",
                            ],
                        },
                    },
                )
                self.assertNotIn("context", inventory["inputs"])
                parsed = CORE.load_inventory(root / inventory_path)
                self.assertEqual(parsed.inputs, inventory["inputs"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], root.resolve())
                self.assertEqual(calls[0][2], expected_kwargs)

    def test_all_authority_and_evidence_paths_are_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata/records").mkdir(parents=True)
            decisions = root / "source-adapters/zotero/policy/decisions.yaml"
            write_yaml(decisions, empty_decisions())
            calls: list[str] = []

            def build_candidates(*_args, **_kwargs):
                calls.append("provider")
                return {}

            provider = SimpleNamespace(build_candidates=build_candidates)
            attempts = (
                {
                    "inventory_path": (
                        "source-adapters/zotero/transactions/../policy/escape.yaml"
                    ),
                    "decisions_path": decisions,
                    "provider_output": "build/curation/zotero/provider",
                },
                {
                    "inventory_path": (
                        "source-adapters/zotero/transactions/proposal.yaml"
                    ),
                    "decisions_path": (
                        "source-adapters/zotero/transactions/decisions.yaml"
                    ),
                    "provider_output": "build/curation/zotero/provider",
                },
                {
                    "inventory_path": (
                        "source-adapters/zotero/transactions/proposal.yaml"
                    ),
                    "decisions_path": decisions,
                    "provider_output": "source-adapters/zotero/policy/provider",
                },
            )
            for attempt in attempts:
                with (
                    self.subTest(attempt=attempt),
                    self.assertRaises(CLI.CurationCliError),
                ):
                    CLI.propose(
                        root,
                        adapter="zotero",
                        as_of=date(2026, 8, 18),
                        expected_library_version=739,
                        core=CORE,
                        provider=provider,
                        **attempt,
                    )
            self.assertEqual(calls, [])

    def test_proposal_inventory_is_append_only_and_identical_reruns_noop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "metadata/records").mkdir(parents=True)
            decisions = root / "source-adapters/zotero/policy/decisions.yaml"
            write_yaml(decisions, empty_decisions())
            decision_bytes = decisions.read_bytes()
            selected = {"candidate": candidate("zotero")}

            def build_candidates(_root, output, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                return {
                    "adapter_id": "zotero",
                    "source": {"kind": "frozen-fixture", "revision": "exact"},
                    "policy": {"version": 1},
                    "implementation": {
                        "provider_sha256": "a" * 64,
                        "transformer_sha256": "b" * 64,
                    },
                    "candidates": [selected["candidate"]],
                }

            provider = SimpleNamespace(build_candidates=build_candidates)
            inventory = "source-adapters/zotero/transactions/append-only-inventory.yaml"

            def propose() -> dict[str, object]:
                return CLI.propose(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    provider_output="build/curation/zotero/append-only",
                    as_of=date(2026, 8, 18),
                    expected_library_version=739,
                    core=CORE,
                    provider=provider,
                )

            first = propose()
            inventory_path = root / inventory
            inventory_bytes = inventory_path.read_bytes()
            self.assertEqual(propose(), first)
            self.assertEqual(inventory_path.read_bytes(), inventory_bytes)

            selected["candidate"] = candidate("zotero", name="Changed proposal")
            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "Append-only inventory path.*different content",
            ):
                propose()
            self.assertEqual(inventory_path.read_bytes(), inventory_bytes)
            self.assertEqual(decisions.read_bytes(), decision_bytes)

    def test_inventory_publication_scratch_never_enters_tracked_transactions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "metadata/records").mkdir(parents=True)
            decisions = root / "source-adapters/zotero/policy/decisions.yaml"
            write_yaml(decisions, empty_decisions())
            value = candidate("zotero")

            def build_candidates(_root, output, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                return {
                    "adapter_id": "zotero",
                    "source": {"kind": "frozen-fixture", "revision": "exact"},
                    "policy": {"version": 1},
                    "implementation": {"provider_sha256": "a" * 64},
                    "candidates": [value],
                }

            scratch_paths: list[Path] = []

            class CoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def write_inventory(self, path, *args, **kwargs):
                    scratch_paths.append(path)
                    return CORE.write_inventory(path, *args, **kwargs)

            inventory = root / (
                "source-adapters/zotero/transactions/scratch-location.yaml"
            )
            CLI.propose(
                root,
                adapter="zotero",
                inventory_path=inventory,
                decisions_path=decisions,
                provider_output="build/curation/zotero/scratch-location",
                as_of=date(2026, 8, 18),
                expected_library_version=739,
                core=CoreProxy(),
                provider=SimpleNamespace(build_candidates=build_candidates),
            )

            self.assertEqual(len(scratch_paths), 1)
            self.assertTrue(
                scratch_paths[0].is_relative_to(
                    root / "build/curation/zotero/scratch-location"
                )
            )
            self.assertFalse(scratch_paths[0].exists())
            self.assertEqual(
                sorted(path.name for path in inventory.parent.iterdir()),
                [inventory.name],
            )

    def test_staged_validation_is_mandatory_and_failure_preserves_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, value, inventory, decisions = reviewed_fixture(root)
            before = tree_bytes(records)
            inventory_bytes = inventory.read_bytes()
            decision_bytes = decisions.read_bytes()
            report = "source-adapters/zotero/transactions/reconciliation-report.yaml"

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "requires a staged-tree validator",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=None,
                    core=CORE,
                )

            observed_stages: list[Path] = []

            def reject_schema(staged: Path) -> None:
                observed_stages.append(staged)
                self.assertTrue((staged / value.proposed_path).is_file())
                raise RuntimeError("locked schema rejected the staged tree")

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "locked schema rejected",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=reject_schema,
                    core=CORE,
                )

            self.assertEqual(len(observed_stages), 1)
            self.assertEqual(tree_bytes(records), before)
            self.assertEqual(inventory.read_bytes(), inventory_bytes)
            self.assertEqual(decisions.read_bytes(), decision_bytes)
            self.assertFalse((root / report).exists())
            self.assertEqual(
                list((root / "metadata").glob(".curation-prototype-v1-stage-*")),
                [],
            )

    def test_authority_change_at_commit_boundary_rolls_back_without_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, _value, inventory, decisions = reviewed_fixture(root)
            before = tree_bytes(records)
            report = "source-adapters/zotero/transactions/authority-race.yaml"

            def mutate_authority(_staged: Path) -> None:
                decisions.write_bytes(
                    decisions.read_bytes() + b"\n# changed externally\n"
                )

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "decisions changed during the transaction",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=mutate_authority,
                    core=CORE,
                )

            self.assertEqual(tree_bytes(records), before)
            self.assertFalse((root / report).exists())
            self.assertEqual(
                list((root / "metadata").glob(".curation-prototype-v1-stage-*")),
                [],
            )

    def test_existing_reports_cannot_be_overwritten_by_mutating_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, _value, inventory, decisions = reviewed_fixture(root)
            before = tree_bytes(records)
            report = root / "source-adapters/zotero/transactions/existing.yaml"
            report_bytes = b"format: retained-review-evidence\n"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_bytes(report_bytes)

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "Append-only report path already exists",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=lambda _path: None,
                    core=CORE,
                )
            self.assertEqual(tree_bytes(records), before)
            self.assertEqual(report.read_bytes(), report_bytes)

            stage = root / "metadata/.curation-prototype-v1-stage-report-guard"
            (stage / "records").mkdir(parents=True)
            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "Append-only report path already exists",
            ):
                CLI.recover(
                    root,
                    adapter="zotero",
                    report_path=report,
                    core=CORE,
                )
            self.assertTrue(stage.is_dir())
            self.assertEqual(report.read_bytes(), report_bytes)

    def test_noop_reconciliation_report_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, _value, inventory, decisions = reviewed_fixture(
                root,
                disposition="reject",
            )
            before = tree_bytes(records)
            report = "source-adapters/zotero/transactions/rejected.yaml"

            first = CLI.reconcile(
                root,
                adapter="zotero",
                inventory_path=inventory,
                decisions_path=decisions,
                report_path=report,
                validate_staged=lambda _path: None,
                core=CORE,
            )
            report_path = root / report
            report_bytes = report_path.read_bytes()
            self.assertFalse(first["changed"])
            self.assertEqual(tree_bytes(records), before)

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "Append-only report path already exists",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=lambda _path: None,
                    core=CORE,
                )
            self.assertEqual(report_path.read_bytes(), report_bytes)
            self.assertEqual(tree_bytes(records), before)

    def test_report_reservation_blocks_a_post_guard_competing_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, value, inventory, decisions = reviewed_fixture(root)
            report = "source-adapters/zotero/transactions/reserved.yaml"
            report_path = root / report
            competing_attempts: list[str] = []

            class CoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def reconcile_inventory(self, *args, before_commit, **kwargs):
                    def competing_guard(planned_report) -> None:
                        before_commit(planned_report)
                        try:
                            descriptor = os.open(
                                report_path,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                            )
                        except FileExistsError:
                            competing_attempts.append("blocked")
                        else:
                            os.close(descriptor)
                            raise AssertionError(
                                "competing report creation was not excluded"
                            )

                    return CORE.reconcile_inventory(
                        *args,
                        before_commit=competing_guard,
                        **kwargs,
                    )

            reconciled = CLI.reconcile(
                root,
                adapter="zotero",
                inventory_path=inventory,
                decisions_path=decisions,
                report_path=report,
                validate_staged=lambda _path: None,
                core=CoreProxy(),
            )
            self.assertEqual(competing_attempts, ["blocked"])
            self.assertTrue(reconciled["changed"])
            self.assertTrue((records / value.proposed_path).is_file())
            self.assertEqual(
                yaml.safe_load(report_path.read_text(encoding="utf-8"))["inventory_id"],
                reconciled["inventory_id"],
            )

    def test_post_commit_report_failure_preserves_a_recoverable_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, value, inventory, decisions = reviewed_fixture(root)
            report = "source-adapters/zotero/transactions/prepared-journal.yaml"
            report_path = root / report
            original_commit = CLI._commit_report

            def fail_report_install(_reservation, _report) -> None:
                raise RuntimeError("injected final report installation failure")

            CLI._commit_report = fail_report_install
            try:
                with self.assertRaisesRegex(
                    CLI.CurationCliError,
                    "prepared reconciliation report could not be finalized",
                ):
                    CLI.reconcile(
                        root,
                        adapter="zotero",
                        inventory_path=inventory,
                        decisions_path=decisions,
                        report_path=report,
                        validate_staged=lambda _path: None,
                        core=CORE,
                    )
            finally:
                CLI._commit_report = original_commit

            self.assertTrue((records / value.proposed_path).is_file())
            marker = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["state"], "prepared")
            self.assertEqual(
                CORE.metadata_tree_digest(records),
                marker["report"]["after_digest"],
            )

            recovery = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=CORE,
            )
            self.assertEqual(
                recovery["recovery"],
                "finalized-committed-reconciliation-report",
            )
            finalized = yaml.safe_load(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                finalized["inventory_id"], marker["report"]["inventory_id"]
            )

    def test_normal_and_recovered_reports_have_identical_canonical_bytes(self) -> None:
        report = "source-adapters/zotero/transactions/canonical-bytes.yaml"
        with (
            tempfile.TemporaryDirectory() as normal_tmp,
            tempfile.TemporaryDirectory() as recovered_tmp,
        ):
            normal_root = Path(normal_tmp)
            _records, _value, inventory, decisions = reviewed_fixture(normal_root)
            CLI.reconcile(
                normal_root,
                adapter="zotero",
                inventory_path=inventory,
                decisions_path=decisions,
                report_path=report,
                validate_staged=lambda _path: None,
                core=CORE,
            )
            normal_bytes = (normal_root / report).read_bytes()

            recovered_root = Path(recovered_tmp)
            _records, _value, inventory, decisions = reviewed_fixture(recovered_root)
            report_path = recovered_root / report
            original_commit = CLI._commit_report

            def fail_report_install(_reservation, _report) -> None:
                raise RuntimeError("injected final report installation failure")

            CLI._commit_report = fail_report_install
            try:
                with self.assertRaisesRegex(
                    CLI.CurationCliError,
                    "prepared reconciliation report could not be finalized",
                ):
                    CLI.reconcile(
                        recovered_root,
                        adapter="zotero",
                        inventory_path=inventory,
                        decisions_path=decisions,
                        report_path=report,
                        validate_staged=lambda _path: None,
                        core=CORE,
                    )
            finally:
                CLI._commit_report = original_commit

            marker = json.loads(report_path.read_text(encoding="utf-8"))
            lock_observed: list[bool] = []

            class LockedCoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def metadata_tree_digest(self, records):
                    lock_observed.append(
                        CORE._reconciliation_lock_path(records).is_file()
                    )
                    return CORE.metadata_tree_digest(records)

            CLI.recover_report_reservation(
                recovered_root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=LockedCoreProxy(),
            )
            self.assertEqual(lock_observed, [True])
            self.assertEqual(report_path.read_bytes(), normal_bytes)

    def test_post_core_authority_drift_preserves_the_prepared_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, value, inventory, decisions = reviewed_fixture(root)
            decision_bytes = decisions.read_bytes()
            report = "source-adapters/zotero/transactions/post-core-drift.yaml"
            report_path = root / report

            class CoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def reconcile_inventory(self, *args, after_commit, **kwargs):
                    def drift_before_report(planned_report) -> None:
                        decisions.write_bytes(
                            decision_bytes + b"\n# post-core authority drift\n"
                        )
                        after_commit(planned_report)

                    return CORE.reconcile_inventory(
                        *args,
                        after_commit=drift_before_report,
                        **kwargs,
                    )

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "prepared reconciliation report could not be finalized",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=lambda _path: None,
                    core=CoreProxy(),
                )

            self.assertTrue((records / value.proposed_path).is_file())
            marker = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["state"], "prepared")
            decisions.write_bytes(decision_bytes)
            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "finalized-committed-reconciliation-report",
            )

    def test_exception_after_prepare_preserves_an_aborted_report_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, _value, inventory, decisions = reviewed_fixture(root)
            before = tree_bytes(records)
            report = "source-adapters/zotero/transactions/aborted-prepared.yaml"
            report_path = root / report

            class CoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def reconcile_inventory(self, *args, before_commit, **kwargs):
                    def fail_after_prepare(planned_report) -> None:
                        before_commit(planned_report)
                        raise RuntimeError("injected failure after report preparation")

                    return CORE.reconcile_inventory(
                        *args,
                        before_commit=fail_after_prepare,
                        **kwargs,
                    )

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "prepared reconciliation report could not be finalized",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=report,
                    validate_staged=lambda _path: None,
                    core=CoreProxy(),
                )
            self.assertEqual(tree_bytes(records), before)
            marker = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["state"], "prepared")

            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "discarded-aborted-reconciliation-report",
            )
            self.assertFalse(report_path.exists())

    def test_unprepared_report_reservation_can_only_be_verified_and_cleared(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "metadata/records").mkdir(parents=True)
            report = root / "source-adapters/zotero/transactions/unprepared.yaml"
            reservation = CLI._reserve_report(report, operation="reconcile")
            token = reservation.token
            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "recover-report-reservation.*token",
            ):
                CLI._reserve_report(report, operation="reconcile")
            CLI._preserve_report_reservation(reservation)

            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=token,
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "discarded-unprepared-report-reservation",
            )
            self.assertFalse(report.exists())

    def test_unpublished_fsynced_reservation_temp_has_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "metadata/records").mkdir(parents=True)
            report = root / "source-adapters/zotero/transactions/unpublished.yaml"
            report.parent.mkdir(parents=True)
            token = "a" * 32
            temporary = CLI._reservation_temp_path(report, token)
            marker = CLI._reservation_marker(
                operation="reconcile",
                token=token,
                state="reserved",
                report=None,
            )
            descriptor = CLI._write_fsynced_temp(
                temporary,
                marker,
                read_write=True,
            )
            os.close(descriptor)

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "Unpublished report-reservation files require.*"
                "recover-report-reservation",
            ):
                CLI._reserve_report(report, operation="reconcile")

            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=token,
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "discarded-unpublished-report-reservation",
            )
            self.assertFalse(report.exists())
            self.assertFalse(temporary.exists())

    def test_recovery_is_explicit_before_a_reported_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, value, inventory, decisions = reviewed_fixture(root)
            stage = root / "metadata/.curation-prototype-v1-stage-explicit"
            write_yaml(
                stage / "records/XYZProject/never-activated.yaml",
                {
                    "pid": "record:never-activated",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Never activated",
                },
            )
            reconcile_report = "source-adapters/zotero/transactions/reconciled.yaml"

            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "requires recover_interrupted",
            ):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=reconcile_report,
                    validate_staged=lambda _path: None,
                    core=CORE,
                )
            self.assertTrue(stage.is_dir())
            self.assertFalse((root / reconcile_report).exists())

            recovery_report = "source-adapters/zotero/transactions/recovery.yaml"
            recovered = CLI.recover(
                root,
                adapter="zotero",
                report_path=recovery_report,
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "discarded-never-activated-stage",
            )
            self.assertFalse(stage.exists())
            self.assertEqual(
                yaml.safe_load((root / recovery_report).read_text(encoding="utf-8"))[
                    "adapter_id"
                ],
                "zotero",
            )

            report = CLI.reconcile(
                root,
                adapter="zotero",
                inventory_path=inventory,
                decisions_path=decisions,
                report_path=reconcile_report,
                validate_staged=lambda _path: None,
                core=CORE,
            )
            self.assertTrue(report["changed"])
            self.assertTrue((records / value.proposed_path).is_file())
            self.assertEqual(
                yaml.safe_load((root / reconcile_report).read_text(encoding="utf-8"))[
                    "inventory_id"
                ],
                report["inventory_id"],
            )

    def test_missing_canonical_root_directs_propose_and_reconcile_to_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            records, _value, inventory, decisions = reviewed_fixture(root)
            stage = root / "metadata/.curation-prototype-v1-stage-missing-root"
            write_yaml(
                stage / "records/XYZProject/staged.yaml",
                {
                    "pid": "record:staged",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Staged",
                },
            )
            backup = stage.with_name(f"{stage.name}-backup")
            os.replace(records, backup)
            provider_calls: list[str] = []

            def build_candidates(*_args, **_kwargs):
                provider_calls.append("called")
                raise AssertionError("provider must not run before recovery")

            recovery_direction = (
                "recover --adapter zotero --report "
                "source-adapters/zotero/transactions/<recovery-report>.yaml"
            )
            with self.assertRaisesRegex(CLI.CurationCliError, recovery_direction):
                CLI.propose(
                    root,
                    adapter="zotero",
                    inventory_path=(
                        "source-adapters/zotero/transactions/missing-root.yaml"
                    ),
                    decisions_path=decisions,
                    provider_output="build/curation/zotero/missing-root",
                    as_of=date(2026, 8, 18),
                    expected_library_version=739,
                    core=CORE,
                    provider=SimpleNamespace(build_candidates=build_candidates),
                )

            reconciliation_report = (
                "source-adapters/zotero/transactions/missing-root-reconcile.yaml"
            )
            with self.assertRaisesRegex(CLI.CurationCliError, recovery_direction):
                CLI.reconcile(
                    root,
                    adapter="zotero",
                    inventory_path=inventory,
                    decisions_path=decisions,
                    report_path=reconciliation_report,
                    validate_staged=lambda _path: None,
                    core=CORE,
                )

            self.assertEqual(provider_calls, [])
            self.assertFalse((root / reconciliation_report).exists())
            recovery_report = (
                "source-adapters/zotero/transactions/missing-root-recovery.yaml"
            )
            recovered = CLI.recover(
                root,
                adapter="zotero",
                report_path=recovery_report,
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "restored-pre-transaction-backup",
            )
            self.assertTrue(records.is_dir())
            self.assertFalse(stage.exists())
            self.assertFalse(backup.exists())

    def test_recovery_exception_after_prepare_discards_only_exact_before_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            records = root / "metadata/records"
            write_yaml(
                records / "XYZProject/baseline.yaml",
                {
                    "pid": "record:baseline",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Baseline",
                },
            )
            stage = root / "metadata/.curation-prototype-v1-stage-prepared"
            (stage / "records").mkdir(parents=True)
            report = "source-adapters/zotero/transactions/aborted-recovery-report.yaml"
            report_path = root / report

            class CoreProxy:
                def __getattr__(self, name):
                    return getattr(CORE, name)

                def recover_interrupted(
                    self,
                    *args,
                    before_commit,
                    after_commit,
                    **kwargs,
                ):
                    def fail_after_prepare(planned_report) -> None:
                        before_commit(planned_report)
                        raise RuntimeError(
                            "injected failure after recovery report preparation"
                        )

                    return CORE.recover_interrupted(
                        *args,
                        before_commit=fail_after_prepare,
                        after_commit=after_commit,
                        **kwargs,
                    )

            with self.assertRaisesRegex(
                CLI.CurationCliError,
                "prepared interrupted-recovery report could not be finalized",
            ):
                CLI.recover(
                    root,
                    adapter="zotero",
                    report_path=report,
                    core=CoreProxy(),
                )

            marker = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["state"], "prepared")
            self.assertEqual(marker["report"]["artifacts_before"], [stage.name])
            self.assertTrue(stage.is_dir())

            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "discarded-aborted-interrupted-recovery-report",
            )
            self.assertFalse(report_path.exists())
            self.assertTrue(stage.is_dir())

            completed = CLI.recover(
                root,
                adapter="zotero",
                report_path=report,
                core=CORE,
            )
            self.assertEqual(
                completed["recovery"],
                "discarded-never-activated-stage",
            )
            self.assertFalse(stage.exists())

    @unittest.skipUnless(os.name == "posix", "stale-lock recovery is POSIX-only")
    def test_stale_lock_recovery_is_a_separate_explicit_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            records = root / "metadata/records"
            write_yaml(
                records / "XYZProject/baseline.yaml",
                {
                    "pid": "record:baseline",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Baseline",
                },
            )
            stage = root / "metadata/.curation-prototype-v1-stage-stale-lock"
            (stage / "records").mkdir(parents=True)
            lock = CORE._reconciliation_lock_path(records)
            lock.write_text(
                json.dumps(
                    {
                        "format": CORE.LOCK_FORMAT,
                        "pid": 999_999_999,
                        "records_dir": str(records.absolute()),
                        "token": "focused-stale-lock-fixture",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            interrupted_report = (
                "source-adapters/zotero/transactions/interrupted-recovery.yaml"
            )
            with self.assertRaisesRegex(
                CORE.CurationPrototypeError,
                "recovery is locked",
            ):
                CLI.recover(
                    root,
                    adapter="zotero",
                    report_path=interrupted_report,
                    core=CORE,
                )
            self.assertTrue(lock.is_file())
            self.assertFalse((root / interrupted_report).exists())

            lock_report = "source-adapters/zotero/transactions/lock-recovery.yaml"
            recovered_lock = CLI.recover_lock(
                root,
                adapter="zotero",
                report_path=lock_report,
                core=CORE,
            )
            self.assertEqual(
                recovered_lock["recovery"],
                "removed-stale-reconciliation-lock",
            )
            self.assertFalse(lock.exists())

            recovered_stage = CLI.recover(
                root,
                adapter="zotero",
                report_path=interrupted_report,
                core=CORE,
            )
            self.assertEqual(
                recovered_stage["recovery"],
                "discarded-never-activated-stage",
            )
            self.assertFalse(stage.exists())

    @unittest.skipUnless(os.name == "posix", "stale-lock recovery is POSIX-only")
    def test_stale_lock_removal_prepares_a_recoverable_report_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            records = root / "metadata/records"
            write_yaml(
                records / "XYZProject/baseline.yaml",
                {
                    "pid": "record:baseline",
                    "schema_type": "xyzri:XYZProject",
                    "name": "Baseline",
                },
            )
            lock = CORE._reconciliation_lock_path(records)
            lock.write_text(
                json.dumps(
                    {
                        "format": CORE.LOCK_FORMAT,
                        "pid": 999_999_999,
                        "records_dir": str(records.absolute()),
                        "token": "prepared-stale-lock-fixture",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            report = "source-adapters/zotero/transactions/prepared-lock.yaml"
            report_path = root / report
            original_commit = CLI._commit_report

            def fail_report_install(_reservation, _report) -> None:
                raise RuntimeError("injected lock report installation failure")

            CLI._commit_report = fail_report_install
            try:
                with self.assertRaisesRegex(
                    CLI.CurationCliError,
                    "prepared stale-lock recovery report could not be finalized",
                ):
                    CLI.recover_lock(
                        root,
                        adapter="zotero",
                        report_path=report,
                        core=CORE,
                    )
            finally:
                CLI._commit_report = original_commit

            self.assertFalse(lock.exists())
            marker = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["state"], "prepared")
            recovered = CLI.recover_report_reservation(
                root,
                adapter="zotero",
                report_path=report,
                token=marker["token"],
                core=CORE,
            )
            self.assertEqual(
                recovered["recovery"],
                "finalized-stale-lock-recovery-report",
            )
            self.assertEqual(
                yaml.safe_load(report_path.read_text(encoding="utf-8"))["recovery"],
                "removed-stale-reconciliation-lock",
            )

    def test_decision_renderer_emits_a_core_valid_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _records, value, inventory, _decisions = reviewed_fixture(root)
            rendered = CLI.render_inventory_decision(
                root,
                adapter="zotero",
                inventory_path=inventory,
                candidate_id=value.candidate_id,
                supersedes_decision_id=None,
                disposition="reject",
                reviewer="reviewer@example.invalid",
                decided_on=date(2026, 8, 18),
                rationale="Rendered focused review event.",
                evidence=("fixture:rendered-decision-event",),
                core=CORE,
            )
            document = {
                "format": CORE.DECISIONS_FORMAT,
                "decisions": [rendered],
                "transactions": [
                    {
                        "inventory_id": CORE.load_inventory(inventory).inventory_id,
                        "decision_ids": [rendered["decision_id"]],
                    }
                ],
            }
            parsed = CORE.parse_decisions(yaml.safe_dump(document, sort_keys=False))

            self.assertIn(rendered["decision_id"], parsed.decisions)
            self.assertEqual(
                rendered["claim_revision_id"],
                CORE.claim_revision_identity(
                    value.candidate_id,
                    value.material_fingerprint,
                    value.relevant_policy_fingerprint,
                ),
            )

    def test_locked_validator_fails_closed_outside_the_consumer_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata/records").mkdir(parents=True)
            with self.assertRaises(CLI.CurationCliError):
                CLI.locked_staged_validator(root, CORE)


if __name__ == "__main__":
    unittest.main()
