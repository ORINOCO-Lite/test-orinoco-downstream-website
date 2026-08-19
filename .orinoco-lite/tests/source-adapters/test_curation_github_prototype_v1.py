from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
import lzma
from pathlib import Path
import re
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
    "orinoco_curation_github_test_core",
    ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
)
CLI = load_module(
    "orinoco_curation_github_test_cli",
    ROOT / "source-adapters/metadata/tools/curation_cli_prototype_v1.py",
)
GITHUB = load_module(
    "orinoco_curation_github_prototype_v1",
    ROOT / "source-adapters/metadata/tools/curation_github_prototype_v1.py",
)


def candidate(record_id: str, *, blocked: bool = False):
    return CORE.make_candidate(
        adapter_id="dump-research-info",
        source_namespace="fixture:dump",
        source_record_id=record_id,
        claim_kind="record-import",
        material={"source_record_id": record_id},
        relevant_policy={"prototype": 1},
        proposed_path=f"XYZOrganization/{record_id.lower()}.yaml",
        baseline_record=(
            None
            if record_id == "NEW"
            else {
                "pid": f"record:{record_id.lower()}",
                "schema_type": "xyzri:XYZOrganization",
                "name": "Old",
                "removed": True,
                "nested": {"same": 1, "value": "old"},
            }
        ),
        proposed_record={
            "pid": f"record:{record_id.lower()}",
            "schema_type": "xyzri:XYZOrganization",
            "name": "New",
            "added": True,
            "nested": {"same": 1, "value": "new"},
        },
        blockers=("fixture blocker",) if blocked else (),
    )


def empty_document() -> dict[str, object]:
    return {
        "format": CORE.DECISIONS_FORMAT,
        "decisions": [],
        "transactions": [],
    }


def book(document: dict[str, object] | None = None):
    return CORE.parse_decisions(
        yaml.safe_dump(document or empty_document(), sort_keys=False)
    )


def inventory(*values):
    return CORE.parse_inventory(
        CORE.build_inventory(
            "dump-research-info",
            values,
            context=CORE.EvaluationContext(as_of=date(2026, 8, 19)),
            inputs={
                "source": {"commit": "a" * 40},
                "policy": {"sha256": "b" * 64},
            },
        )
    )


def bundle(value, decisions=None):
    return GITHUB.build_review_bundle(
        CORE,
        value,
        decisions or book(),
        inventory_path=(
            "source-adapters/dump-research-info/transactions/github-123.yaml"
        ),
        decisions_path=(
            "source-adapters/dump-research-info/policy/curation-decisions.yaml"
        ),
        manifest_path=(
            "source-adapters/dump-research-info/transactions/"
            "github-123.review-manifest.yaml"
        ),
        review_path=(
            "source-adapters/dump-research-info/transactions/github-123.review.md"
        ),
        base_sha="1" * 40,
        head_sha="1" * 40,
        public_data_actor="github-user:7@reviewer",
        public_data_at="2026-08-19T13:14:15Z",
        public_data_run_url="https://github.com/con/site/actions/runs/123",
    )


def event(body: str) -> dict[str, object]:
    return {
        "action": "created",
        "issue": {"number": 42, "pull_request": {"url": "https://api.invalid"}},
        "repository": {"full_name": "con/site", "default_branch": "main"},
        "sender": {"id": 7, "login": "reviewer"},
        "comment": {
            "id": 99,
            "body": body,
            "created_at": "2026-08-19T23:30:00Z",
            "html_url": "https://github.com/con/site/issues/42#issuecomment-99",
            "user": {"id": 7, "login": "reviewer"},
        },
    }


def pull_request(marker: str) -> dict[str, object]:
    return {
        "number": 42,
        "state": "open",
        "body": f"Hosted review.\n\n{marker}\n",
        "user": {
            "id": GITHUB.PROPOSAL_BOT_ID,
            "login": GITHUB.PROPOSAL_BOT_LOGIN,
        },
        "base": {
            "ref": "main",
            "sha": "1" * 40,
            "repo": {"full_name": "con/site"},
        },
        "head": {
            "ref": "automation/curation/dump-research-info-123",
            "sha": "3" * 40,
            "repo": {"full_name": "con/site"},
        },
        "labels": [{"name": "curation-review"}],
    }


def proposal_run() -> dict[str, object]:
    return {
        "id": 123,
        "run_attempt": 1,
        "html_url": "https://github.com/con/site/actions/runs/123",
        "event": "workflow_dispatch",
        "path": ".github/workflows/curation-review.yml",
        "head_sha": "1" * 40,
        "head_branch": "main",
        "created_at": "2026-08-19T13:14:15Z",
        "updated_at": "2026-08-19T13:20:00Z",
        "status": "completed",
        "conclusion": "success",
        "actor": {"id": 7, "login": "reviewer"},
        "repository": {"full_name": "con/site"},
        "head_repository": {"full_name": "con/site"},
    }


def comment_run(
    *,
    run_id: int = 456,
    run_attempt: int = 1,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "html_url": f"https://github.com/con/site/actions/runs/{run_id}",
        "event": "issue_comment",
        "path": ".github/workflows/curation-review.yml",
        "head_sha": "1" * 40,
        "head_branch": "main",
        "created_at": "2026-08-19T23:30:01Z",
        "updated_at": "2026-08-19T23:40:00Z",
        "status": status,
        "conclusion": conclusion,
        "actor": {"id": 7, "login": "reviewer"},
        "repository": {"full_name": "con/site"},
        "head_repository": {"full_name": "con/site"},
    }


def bot_comment(
    body: str,
    *,
    comment_id: int,
    created_at: str,
    updated_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": body,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "html_url": (
            f"https://github.com/con/site/issues/42#issuecomment-{comment_id}"
        ),
        "user": {
            "id": GITHUB.PROPOSAL_BOT_ID,
            "login": GITHUB.PROPOSAL_BOT_LOGIN,
        },
    }


def submission_text(
    inventory_id: str,
    decisions: list[dict[str, object]],
) -> str:
    payload = yaml.safe_dump(
        {"inventory_id": inventory_id, "decisions": decisions},
        sort_keys=False,
    ).rstrip()
    return f"/curation submit\n```yaml\n{payload}\n```\n"


def submission(
    alias: str,
    *,
    expected: str | None = None,
    disposition: str = "reject",
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "candidate": alias,
        "expected_decision": expected,
        "disposition": disposition,
        "rationale": "Reviewed in the hosted pull request.",
        "evidence": ["https://example.invalid/source-evidence"],
        "details": details or {},
    }


def write_sidecar(root: Path, outputs: list[str]) -> str:
    record = {
        "cmd": "trusted curation proposal",
        "exit": 0,
        "inputs": [],
        "outputs": outputs,
        "pwd": ".",
    }
    serialized = json.dumps(record, indent=1, sort_keys=True, ensure_ascii=False)
    record_id = hashlib.md5(
        serialized.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    relative = f".datalad/runinfo/{record_id}"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    return relative


class ReviewBundleTests(unittest.TestCase):
    def test_bundle_is_deterministic_complete_and_uses_stable_aliases(self) -> None:
        value = inventory(candidate("SECOND", blocked=True), candidate("FIRST"))
        first_manifest, first_review = bundle(value)
        second_manifest, second_review = bundle(value)

        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_review, second_review)
        self.assertEqual(
            [entry["alias"] for entry in first_manifest["candidates"]],
            ["DRI-001", "DRI-002"],
        )
        for entry in first_manifest["candidates"]:
            self.assertIn("baseline_record", entry)
            self.assertIn("proposed_record", entry)
            self.assertIn("semantic_diff", entry)
            self.assertIn(f"## {entry['alias']}", first_review)
        operations = {
            change["operation"]
            for entry in first_manifest["candidates"]
            for change in entry["semantic_diff"]
        }
        self.assertEqual(operations, {"add", "remove", "replace"})
        forms = re.findall(
            r"/curation submit\n```yaml\n(.*?)\n```", first_review, re.DOTALL
        )
        form_aliases = [
            item["candidate"]
            for rendered in forms
            for item in yaml.safe_load(rendered)["decisions"]
        ]
        self.assertEqual(form_aliases, ["DRI-001", "DRI-002"])
        self.assertIn("BLOCKED — `accept` is forbidden", first_review)
        self.assertNotIn("disposition: reject", first_review)
        GITHUB.validate_manifest(CORE, value, book(), first_manifest, first_review)

    def test_render_exposes_empty_inventory_as_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            value = inventory()
            inventory_relative = (
                "source-adapters/dump-research-info/transactions/github-123.yaml"
            )
            decisions_relative = (
                "source-adapters/dump-research-info/policy/curation-decisions.yaml"
            )
            manifest_relative = (
                "source-adapters/dump-research-info/transactions/"
                "github-123.review-manifest.yaml"
            )
            review_relative = (
                "source-adapters/dump-research-info/transactions/"
                "github-123.review.md"
            )
            CORE.write_inventory(root / inventory_relative, value.to_mapping())
            rendered = GITHUB.render_review_files(
                SimpleNamespace(
                    root=root,
                    adapter="dump-research-info",
                    inventory=inventory_relative,
                    decisions=decisions_relative,
                    manifest=manifest_relative,
                    review=review_relative,
                    base_sha="1" * 40,
                    head_sha="1" * 40,
                    public_data_actor="github-user:7@reviewer",
                    public_data_at="2026-08-19T13:14:15Z",
                    public_data_run_url=(
                        "https://github.com/con/site/actions/runs/123"
                    ),
                ),
                CORE,
            )
            self.assertEqual(rendered["candidate_count"], 0)
            self.assertTrue(rendered["no_op"])

    def test_manifest_rejects_changed_display_acknowledgment_and_review(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, review = bundle(value)
        changed_display = deepcopy(manifest)
        changed_display["candidates"][0]["proposed_path"] = "changed.yaml"
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "display data"):
            GITHUB.validate_manifest(CORE, value, book(), changed_display, review)

        changed_ack = deepcopy(manifest)
        changed_ack["public_data_acknowledgment"]["actor"] = "reviewer"
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "github-user"):
            GITHUB.validate_manifest(CORE, value, book(), changed_ack, review)

        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Markdown differs"):
            GITHUB.validate_manifest(
                CORE, value, book(), manifest, review + "changed\n"
            )

        alternate_ledger = deepcopy(manifest)
        alternate_ledger["paths"][
            "decisions"
        ] = "source-adapters/dump-research-info/policy/alternate.yaml"
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "not canonical"):
            GITHUB.validate_manifest(CORE, value, book(), alternate_ledger, review)
        for unsafe in ("$(touch-pwned).yaml", "`id`.yaml", "quote'file.yaml"):
            injected = deepcopy(manifest)
            injected["paths"]["inventory"] = (
                "source-adapters/dump-research-info/transactions/" + unsafe
            )
            with self.subTest(path=unsafe), self.assertRaisesRegex(
                GITHUB.CurationGitHubError, "metacharacters"
            ):
                GITHUB.validate_manifest(CORE, value, book(), injected, review)

    def test_semantic_diff_uses_escaped_pointers_and_reports_root_change(self) -> None:
        self.assertEqual(
            GITHUB.semantic_diff({"a/b": {"~key": 1}}, {"a/b": {"~key": 2}}),
            [
                {
                    "operation": "replace",
                    "path": "/a~1b/~0key",
                    "baseline": 1,
                    "proposed": 2,
                }
            ],
        )
        self.assertEqual(GITHUB.semantic_diff(None, {"pid": "new"})[0]["path"], "/")


class SubmissionParserTests(unittest.TestCase):
    def test_strict_batch_parser_accepts_conditional_details(self) -> None:
        inventory_id = "curation-inventory-v1:" + "a" * 64
        body = submission_text(
            inventory_id,
            [
                submission("DRI-001"),
                submission(
                    "DRI-002",
                    disposition="defer",
                    details={"return_when": {"kind": "material-change"}},
                ),
            ],
        )
        observed_id, observed = GITHUB.parse_submission_comment(body)
        self.assertEqual(observed_id, inventory_id)
        self.assertEqual([item.alias for item in observed], ["DRI-001", "DRI-002"])
        self.assertEqual(
            observed[1].details, {"return_when": {"kind": "material-change"}}
        )

    def test_parser_rejects_identity_fields_unknown_fields_and_unfenced_text(
        self,
    ) -> None:
        inventory_id = "curation-inventory-v1:" + "a" * 64
        value = submission("DRI-001")
        value["reviewer"] = "forged"
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "unexpected reviewer"):
            GITHUB.parse_submission_comment(submission_text(inventory_id, [value]))
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "contain only"):
            GITHUB.parse_submission_comment(
                "Please apply this.\n"
                + submission_text(inventory_id, [submission("DRI-001")])
            )

    def test_parser_rejects_duplicate_keys_aliases_and_oversized_comments(self) -> None:
        duplicate = (
            "/curation submit\n```yaml\n"
            "inventory_id: curation-inventory-v1:"
            + "a" * 64
            + "\ninventory_id: duplicate\ndecisions: []\n```\n"
        )
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Duplicate YAML"):
            GITHUB.parse_submission_comment(duplicate)

        anchored = (
            "/curation submit\n```yaml\n"
            "inventory_id: curation-inventory-v1:"
            + "a" * 64
            + "\ndecisions: &items []\n```\n"
        )
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "anchors"):
            GITHUB.parse_submission_comment(anchored)
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "50 KiB"):
            GITHUB.parse_submission_comment("x" * (GITHUB.MAX_COMMENT_BYTES + 1))


class HostedGuardTests(unittest.TestCase):
    def test_guard_derives_identity_date_and_immutable_url(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, _ = bundle(value)
        marker = GITHUB.pr_body_marker(manifest)
        alias = manifest["candidates"][0]["alias"]
        body = submission_text(value.inventory_id, [submission(alias)])
        observed = GITHUB.validate_hosted_guard(
            event(body),
            pull_request(marker),
            {
                "permission": "write",
                "user": {"id": 7, "login": "reviewer"},
            },
            expected_marker=marker,
            expected_base_sha="1" * 40,
            expected_branch="automation/curation/dump-research-info-123",
            expected_repository="con/site",
        )
        self.assertEqual(observed.reviewer, "github-user:7@reviewer")
        self.assertEqual(observed.decided_on, date(2026, 8, 19))
        self.assertEqual(
            observed.comment_url,
            "https://github.com/con/site/issues/42#issuecomment-99",
        )

    def test_guard_rejects_each_hosted_boundary(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, _ = bundle(value)
        marker = GITHUB.pr_body_marker(manifest)
        alias = manifest["candidates"][0]["alias"]
        good_event = event(submission_text(value.inventory_id, [submission(alias)]))
        good_pr = pull_request(marker)
        good_permission = {
            "permission": "admin",
            "user": {"id": 7, "login": "reviewer"},
        }
        mutations = (
            (
                "edited event",
                {**good_event, "action": "edited"},
                good_pr,
                good_permission,
            ),
            ("closed PR", good_event, {**good_pr, "state": "closed"}, good_permission),
            (
                "fork",
                good_event,
                {
                    **good_pr,
                    "head": {**good_pr["head"], "repo": {"full_name": "fork/site"}},
                },
                good_permission,
            ),
            (
                "wrong base",
                good_event,
                {**good_pr, "base": {**good_pr["base"], "ref": "release"}},
                good_permission,
            ),
            (
                "wrong branch",
                good_event,
                {**good_pr, "head": {**good_pr["head"], "ref": "feature/review"}},
                good_permission,
            ),
            ("no label", good_event, {**good_pr, "labels": []}, good_permission),
            (
                "read actor",
                good_event,
                good_pr,
                {
                    "permission": "read",
                    "user": {"id": 7, "login": "reviewer"},
                },
            ),
            ("wrong marker", good_event, {**good_pr, "body": "none"}, good_permission),
        )
        for label, event_value, pr_value, permission_value in mutations:
            with self.subTest(label=label), self.assertRaises(
                GITHUB.CurationGitHubError
            ):
                GITHUB.validate_hosted_guard(
                    event_value,
                    pr_value,
                    permission_value,
                    expected_marker=marker,
                    expected_base_sha="1" * 40,
                    expected_branch=("automation/curation/dump-research-info-123"),
                    expected_repository="con/site",
                )

    def test_guard_requires_exact_finalize_and_all_identity_bindings(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, _ = bundle(value)
        marker = GITHUB.pr_body_marker(manifest)
        permission = {
            "permission": "write",
            "user": {"id": 7, "login": "reviewer"},
        }

        def validate(event_value, permission_value=permission):
            return GITHUB.validate_hosted_guard(
                event_value,
                pull_request(marker),
                permission_value,
                expected_marker=marker,
                expected_base_sha="1" * 40,
                expected_branch="automation/curation/dump-research-info-123",
                expected_repository="con/site",
                command="finalize",
            )

        self.assertEqual(validate(event("/curation finalize")).login, "reviewer")
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "exactly"):
            validate(event("/curation finalize\n"))
        missing_sender = event("/curation finalize")
        missing_sender.pop("sender")
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "event.sender"):
            validate(missing_sender)
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "permission.user"):
            validate(event("/curation finalize"), {"permission": "write"})

    def test_proposal_run_authenticates_acknowledgment(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, _ = bundle(value)
        GITHUB.validate_proposal_run(manifest, proposal_run(), default_branch="main")
        mutations = (
            {"event": "push"},
            {"head_sha": "2" * 40},
            {"actor": {"id": 8, "login": "attacker"}},
            {"created_at": "2026-08-19T13:14:16Z"},
            {"conclusion": "failure"},
            {"path": ".github/workflows/untrusted.yml"},
        )
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(
                GITHUB.CurationGitHubError
            ):
                GITHUB.validate_proposal_run(
                    manifest, {**proposal_run(), **change}, default_branch="main"
                )


class ApplySubmissionTests(unittest.TestCase):
    def test_apply_derives_provenance_updates_transaction_and_replays(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, _ = bundle(value)
        alias = manifest["candidates"][0]["alias"]
        body = submission_text(value.inventory_id, [submission(alias)])
        provenance = GITHUB.derive_comment_provenance(event(body))
        inventory_id, submissions = GITHUB.parse_submission_comment(body)

        document, decision_ids, changed = GITHUB.apply_submission(
            CORE,
            CLI,
            value,
            empty_document(),
            book(),
            inventory_id=inventory_id,
            submissions=submissions,
            provenance=provenance,
            manifest=manifest,
        )
        self.assertTrue(changed)
        self.assertEqual(
            document["transactions"][0]["decision_ids"], list(decision_ids)
        )
        rendered = document["decisions"][0]
        self.assertEqual(rendered["reviewer"], "github-user:7@reviewer")
        self.assertEqual(rendered["decided_on"], "2026-08-19")
        self.assertEqual(rendered["evidence"][-1], provenance.comment_url)
        parsed = book(document)

        replay, replay_ids, replay_changed = GITHUB.apply_submission(
            CORE,
            CLI,
            value,
            document,
            parsed,
            inventory_id=inventory_id,
            submissions=submissions,
            provenance=provenance,
            manifest=manifest,
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replay_ids, decision_ids)
        self.assertEqual(replay, document)

    def test_apply_rejects_blocked_accept_unknown_alias_and_stale_expected(
        self,
    ) -> None:
        value = inventory(candidate("BLOCKED", blocked=True))
        manifest, _ = bundle(value)
        alias = manifest["candidates"][0]["alias"]
        provenance = GITHUB.derive_comment_provenance(event("unused"))

        def apply(item):
            return GITHUB.apply_submission(
                CORE,
                CLI,
                value,
                empty_document(),
                book(),
                inventory_id=value.inventory_id,
                submissions=(item,),
                provenance=provenance,
                manifest=manifest,
            )

        blocked = GITHUB.Submission(
            alias=alias,
            expected_decision=None,
            disposition="accept",
            rationale="Looks good.",
            evidence=("fixture:evidence",),
            details={},
        )
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Blocked candidate"):
            apply(blocked)
        unknown = GITHUB.Submission(
            alias="DRI-999",
            expected_decision=None,
            disposition="reject",
            rationale="Not applicable.",
            evidence=("fixture:evidence",),
            details={},
        )
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Unknown candidate"):
            apply(unknown)
        stale = GITHUB.Submission(
            alias=alias,
            expected_decision="curation-decision-event-v1:" + "a" * 64,
            disposition="reject",
            rationale="Not applicable.",
            evidence=("fixture:evidence",),
            details={},
        )
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Stale"):
            apply(stale)

    def test_batch_can_be_completed_across_multiple_comments(self) -> None:
        first = candidate("FIRST")
        second = candidate("SECOND")
        value = inventory(first, second)
        manifest, _ = bundle(value)
        provenance = GITHUB.derive_comment_provenance(event("unused"))
        document = empty_document()
        current_book = book()
        for entry in manifest["candidates"]:
            item = GITHUB.Submission(
                alias=entry["alias"],
                expected_decision=None,
                disposition="reject",
                rationale=f"Reviewed {entry['alias']}.",
                evidence=(f"fixture:{entry['alias']}",),
                details={},
            )
            document, _, changed = GITHUB.apply_submission(
                CORE,
                CLI,
                value,
                document,
                current_book,
                inventory_id=value.inventory_id,
                submissions=(item,),
                provenance=provenance,
                manifest=manifest,
            )
            self.assertTrue(changed)
            current_book = book(document)
        selected = CORE._validate_current_transaction(value, current_book)
        self.assertEqual(set(selected), {first.candidate_id, second.candidate_id})

    def test_correction_replaces_selected_tip_and_retains_its_ancestry(self) -> None:
        value = inventory(candidate("FIRST"))
        manifest, review = bundle(value)
        alias = manifest["candidates"][0]["alias"]
        provenance = GITHUB.derive_comment_provenance(event("unused"))
        first = GITHUB.Submission(
            alias=alias,
            expected_decision=None,
            disposition="reject",
            rationale="Initial hosted decision.",
            evidence=("fixture:initial",),
            details={},
        )
        document, first_ids, _ = GITHUB.apply_submission(
            CORE,
            CLI,
            value,
            empty_document(),
            book(),
            inventory_id=value.inventory_id,
            submissions=(first,),
            provenance=provenance,
            manifest=manifest,
        )
        first_book = book(document)
        correction = GITHUB.Submission(
            alias=alias,
            expected_decision=first_ids[0],
            disposition="defer",
            rationale="Corrected after another hosted review pass.",
            evidence=("fixture:correction",),
            details={"return_when": {"kind": "material-change"}},
        )
        corrected, corrected_ids, _ = GITHUB.apply_submission(
            CORE,
            CLI,
            value,
            document,
            first_book,
            inventory_id=value.inventory_id,
            submissions=(correction,),
            provenance=provenance,
            manifest=manifest,
        )
        corrected_book = book(corrected)
        transaction = corrected["transactions"][0]["decision_ids"]
        self.assertEqual(transaction, [corrected_ids[0]])
        self.assertEqual(
            [
                item.decision_id
                for item in corrected_book.revisions(value.candidates[0].candidate_id)
            ],
            [first_ids[0], corrected_ids[0]],
        )
        GITHUB.validate_manifest(CORE, value, corrected_book, manifest, review)


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_enforces_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.yaml"
            path.write_text("before\n", encoding="utf-8")
            with self.assertRaisesRegex(GITHUB.CurationGitHubError, "changed"):
                GITHUB._atomic_yaml(path, empty_document(), b"stale\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(list(path.parent.glob("*.lock")), [])


class ReproposalTests(unittest.TestCase):
    def test_trusted_reproposal_is_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pr_root = root / "pr"
            authority_root = root / "trusted"
            pr_root.mkdir()
            authority_root.mkdir()
            value = candidate("FIRST")
            source = {
                "kind": "exact-clean-git-checkout",
                "path": "build/curation/sources/dump-research-info",
                "commit": "a" * 40,
            }
            policy = {"prototype_version": 1}
            implementation = {
                "agent": "fixture:trusted",
                "provider_sha256": "b" * 64,
            }
            built = CORE.build_inventory(
                "dump-research-info",
                [value],
                context=CORE.EvaluationContext(as_of=date(2026, 8, 19)),
                metadata_dir=None,
                inputs={
                    "source": source,
                    "policy": policy,
                    "implementation": implementation,
                },
            )
            parsed = CORE.parse_inventory(built)
            inventory_path = pr_root / (
                "source-adapters/dump-research-info/transactions/github-123.yaml"
            )
            CORE.write_inventory(inventory_path, built)
            trusted_record = authority_root / "metadata/records" / value.proposed_path
            trusted_record.parent.mkdir(parents=True)
            trusted_record.write_text(
                yaml.safe_dump(dict(value.baseline_record), sort_keys=False),
                encoding="utf-8",
            )
            manifest, _ = bundle(parsed)

            def build_candidates(_root, _output, **kwargs):
                self.assertEqual(
                    kwargs,
                    {
                        "source_path": source["path"],
                        "expected_source_commit": source["commit"],
                        "source_run_id": None,
                    },
                )
                return {
                    "adapter_id": "dump-research-info",
                    "source": source,
                    "policy": policy,
                    "implementation": implementation,
                    "candidates": [value],
                }

            previous_trusted_root = GITHUB.TRUSTED_ROOT
            GITHUB.TRUSTED_ROOT = authority_root
            try:
                result = GITHUB.validate_reproposal(
                    pr_root,
                    CORE,
                    CLI,
                    parsed,
                    book(),
                    manifest,
                    inventory_path,
                    provider=SimpleNamespace(build_candidates=build_candidates),
                )
            finally:
                GITHUB.TRUSTED_ROOT = previous_trusted_root
            self.assertTrue(result["reproposal_valid"])
            inventory_path.write_bytes(inventory_path.read_bytes() + b"\n")
            GITHUB.TRUSTED_ROOT = authority_root
            try:
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "byte-identical"
                ):
                    GITHUB.validate_reproposal(
                        pr_root,
                        CORE,
                        CLI,
                        parsed,
                        book(),
                        manifest,
                        inventory_path,
                        provider=SimpleNamespace(build_candidates=build_candidates),
                    )
            finally:
                GITHUB.TRUSTED_ROOT = previous_trusted_root

    def test_manifest_hint_cannot_override_trusted_base_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            authority_root = Path(temporary).resolve()
            value = candidate("FIRST")
            parsed = inventory(value)
            manifest, _ = bundle(parsed)
            event_value = CLI.render_decision(
                CORE,
                value,
                supersedes_decision_id=None,
                disposition="reject",
                reviewer="github-user:7@reviewer",
                decided_on=date(2026, 8, 18),
                rationale="Trusted base rejection.",
                evidence=("fixture:trusted-base",),
                details={},
            )
            trusted_document = {
                "format": CORE.DECISIONS_FORMAT,
                "decisions": [event_value],
                "transactions": [
                    {
                        "inventory_id": parsed.inventory_id,
                        "decision_ids": [event_value["decision_id"]],
                    }
                ],
            }
            decision_path = authority_root / manifest["paths"]["decisions"]
            decision_path.parent.mkdir(parents=True)
            decision_path.write_text(
                yaml.safe_dump(trusted_document, sort_keys=False), encoding="utf-8"
            )
            previous_trusted_root = GITHUB.TRUSTED_ROOT
            GITHUB.TRUSTED_ROOT = authority_root
            try:
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "trusted base authority"
                ):
                    GITHUB._trusted_base_decisions(CORE, manifest, "dump-research-info")
            finally:
                GITHUB.TRUSTED_ROOT = previous_trusted_root

    def test_zotero_trusted_loader_overrides_and_restores_hostile_import(self) -> None:
        hostile = SimpleNamespace(marker="hostile")
        previous = sys.modules.get("zotero_ingest")
        sys.modules["zotero_ingest"] = hostile
        try:
            provider = GITHUB._trusted_provider("zotero", CORE)
            _, adapter = provider.load_dependencies(Path("/untrusted"))
            ingest, site_export = adapter.load_tools(Path("/untrusted"))
            self.assertEqual(
                Path(ingest.__file__).resolve(),
                (ROOT / "source-adapters/zotero/tools/zotero_ingest.py").resolve(),
            )
            self.assertEqual(
                Path(site_export.__file__).resolve(),
                (ROOT / "source-adapters/zotero/tools/zotero_site_export.py").resolve(),
            )
            self.assertIs(sys.modules["zotero_ingest"], hostile)
        finally:
            if previous is None:
                sys.modules.pop("zotero_ingest", None)
            else:
                sys.modules["zotero_ingest"] = previous


class AttestationChainTests(unittest.TestCase):
    def test_receipt_ledger_chain_attempts_and_rollback_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "pr"
            authority = parent / "trusted"
            root.mkdir()
            authority.mkdir()
            value = inventory(candidate("FIRST"))
            manifest, review = bundle(value)
            paths = manifest["paths"]
            CORE.write_inventory(root / paths["inventory"], value.to_mapping())
            manifest_path = root / paths["manifest"]
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
            )
            (root / paths["review"]).write_text(review, encoding="utf-8")
            sidecar = write_sidecar(root, [paths["inventory"]])
            changes = {
                "files": [
                    {"path": paths["inventory"], "status": "added", "mode": "100644"},
                    {"path": paths["manifest"], "status": "added", "mode": "100644"},
                    {"path": paths["review"], "status": "added", "mode": "100644"},
                    {"path": sidecar, "status": "added", "mode": "100644"},
                ]
            }
            pr_value = pull_request(GITHUB.pr_body_marker(manifest))
            receipt_payload = GITHUB.proposal_receipt_payload(
                root,
                manifest,
                pr_value,
                sidecar,
                workflow_run_attempt=1,
            )
            receipt = bot_comment(
                GITHUB._attestation_body("Proposal receipt.", receipt_payload),
                comment_id=100,
                created_at="2026-08-19T13:18:00Z",
            )
            previous_trusted_root = GITHUB.TRUSTED_ROOT
            GITHUB.TRUSTED_ROOT = authority
            try:
                base = GITHUB.validate_attestation_chain(
                    root,
                    CORE,
                    manifest,
                    pr_value,
                    changes,
                    [
                        receipt,
                        {"id": 99, "body": None, "user": None},
                    ],
                    [proposal_run()],
                    default_branch="main",
                    trusted_default_sha="1" * 40,
                )
                self.assertEqual(base["attestation_authority"], "trusted-base-ledger")

                forged_user = deepcopy(receipt)
                forged_user["user"] = {"id": 7, "login": "reviewer"}
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "lacks its bot proposal receipt"
                ):
                    GITHUB.validate_attestation_chain(
                        root,
                        CORE,
                        manifest,
                        pr_value,
                        changes,
                        [forged_user],
                        [proposal_run()],
                        default_branch="main",
                        trusted_default_sha="1" * 40,
                    )
                edited = deepcopy(receipt)
                edited["updated_at"] = "2026-08-19T13:19:00Z"
                with self.assertRaisesRegex(GITHUB.CurationGitHubError, "edited"):
                    GITHUB.parse_attestation_comments(
                        [edited], repository="con/site", pull_request_number=42
                    )
                malformed = deepcopy(receipt)
                malformed["body"] += GITHUB._ATTESTATION_START
                with self.assertRaisesRegex(GITHUB.CurationGitHubError, "malformed"):
                    GITHUB.parse_attestation_comments(
                        [malformed], repository="con/site", pull_request_number=42
                    )
                duplicate = deepcopy(receipt)
                duplicate["id"] = 102
                duplicate["html_url"] = (
                    "https://github.com/con/site/issues/42#issuecomment-102"
                )
                with self.assertRaisesRegex(GITHUB.CurationGitHubError, "duplicated"):
                    GITHUB.parse_attestation_comments(
                        [duplicate, receipt],
                        repository="con/site",
                        pull_request_number=42,
                    )

                decision_document, _, _ = GITHUB.apply_submission(
                    CORE,
                    CLI,
                    value,
                    empty_document(),
                    book(),
                    inventory_id=value.inventory_id,
                    submissions=(
                        GITHUB.Submission(
                            alias=manifest["candidates"][0]["alias"],
                            expected_decision=None,
                            disposition="reject",
                            rationale="Authenticated hosted decision.",
                            evidence=("fixture:attestation",),
                            details={},
                        ),
                    ),
                    provenance=GITHUB.derive_comment_provenance(event("unused")),
                    manifest=manifest,
                )
                decision_path = root / paths["decisions"]
                decision_path.parent.mkdir(parents=True)
                decision_path.write_text(
                    yaml.safe_dump(decision_document, sort_keys=False),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "byte-identical"
                ):
                    GITHUB.validate_attestation_chain(
                        root,
                        CORE,
                        manifest,
                        pr_value,
                        changes,
                        [receipt],
                        [proposal_run()],
                        default_branch="main",
                        trusted_default_sha="1" * 40,
                    )

                first_payload = GITHUB.ledger_attestation_payload(
                    root,
                    manifest,
                    pr_value,
                    workflow_run_id="456",
                    workflow_run_attempt=1,
                    parent_head_sha="3" * 40,
                    target_head_sha="4" * 40,
                )
                first_marker = bot_comment(
                    GITHUB._attestation_body("Ledger checkpoint.", first_payload),
                    comment_id=103,
                    created_at="2026-08-19T23:35:00Z",
                )
                pr_value["head"]["sha"] = "4" * 40
                authenticated = GITHUB.validate_attestation_chain(
                    root,
                    CORE,
                    manifest,
                    pr_value,
                    changes,
                    [first_marker, receipt],
                    [comment_run(), proposal_run()],
                    default_branch="main",
                    trusted_default_sha="1" * 40,
                )
                self.assertEqual(
                    authenticated["attestation_authority"],
                    "installed-ledger-attestation",
                )

                failed_payload = GITHUB.ledger_attestation_payload(
                    root,
                    manifest,
                    pr_value,
                    workflow_run_id="457",
                    workflow_run_attempt=1,
                    parent_head_sha="4" * 40,
                    target_head_sha="5" * 40,
                )
                failed_marker = bot_comment(
                    GITHUB._attestation_body("Failed intent.", failed_payload),
                    comment_id=104,
                    created_at="2026-08-19T23:35:00Z",
                )
                failed_run = comment_run(run_id=457, conclusion="failure")
                GITHUB.validate_attestation_chain(
                    root,
                    CORE,
                    manifest,
                    pr_value,
                    changes,
                    [failed_marker, receipt, first_marker],
                    [failed_run, proposal_run(), comment_run()],
                    default_branch="main",
                    trusted_default_sha="1" * 40,
                )
                pr_value["head"]["sha"] = "5" * 40
                installed_failed_run = GITHUB.validate_attestation_chain(
                    root,
                    CORE,
                    manifest,
                    pr_value,
                    changes,
                    [receipt, first_marker, failed_marker],
                    [proposal_run(), comment_run(), failed_run],
                    default_branch="main",
                    trusted_default_sha="1" * 40,
                )
                self.assertEqual(
                    installed_failed_run["installed_ledger_attestations"], 2
                )
                pr_value["head"]["sha"] = "3" * 40
                with self.assertRaisesRegex(GITHUB.CurationGitHubError, "Successful"):
                    GITHUB.validate_attestation_chain(
                        root,
                        CORE,
                        manifest,
                        pr_value,
                        changes,
                        [receipt, first_marker],
                        [proposal_run(), comment_run()],
                        default_branch="main",
                        trusted_default_sha="1" * 40,
                    )

                pr_value["head"]["sha"] = "4" * 40
                rerun_payload = GITHUB.ledger_attestation_payload(
                    root,
                    manifest,
                    pr_value,
                    workflow_run_id="457",
                    workflow_run_attempt=2,
                    parent_head_sha="4" * 40,
                    target_head_sha="6" * 40,
                )
                rerun_marker = bot_comment(
                    GITHUB._attestation_body("Successful rerun.", rerun_payload),
                    comment_id=105,
                    created_at="2026-08-19T23:36:00Z",
                )
                pr_value["head"]["sha"] = "6" * 40
                rerun = comment_run(run_id=457, run_attempt=2)
                retried = GITHUB.validate_attestation_chain(
                    root,
                    CORE,
                    manifest,
                    pr_value,
                    changes,
                    [rerun_marker, receipt, failed_marker, first_marker],
                    [rerun, proposal_run(), failed_run, comment_run()],
                    default_branch="main",
                    trusted_default_sha="1" * 40,
                )
                self.assertEqual(retried["successful_ledger_attestations"], 2)

                decision_path.write_bytes(decision_path.read_bytes() + b"\n")
                with self.assertRaisesRegex(GITHUB.CurationGitHubError, "differs"):
                    GITHUB.validate_attestation_chain(
                        root,
                        CORE,
                        manifest,
                        pr_value,
                        changes,
                        [receipt, first_marker, failed_marker, rerun_marker],
                        [proposal_run(), comment_run(), failed_run, rerun],
                        default_branch="main",
                        trusted_default_sha="1" * 40,
                    )
            finally:
                GITHUB.TRUSTED_ROOT = previous_trusted_root


class ReconciledTreeTests(unittest.TestCase):
    def test_terminal_tree_replays_report_and_candidate_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            authority_root = root / "trusted"
            pr_root = root / "pr"
            authority_root.mkdir()
            pr_root.mkdir()
            proposed = candidate("FIRST")
            parsed_inventory = inventory(proposed)
            manifest, review = bundle(parsed_inventory)
            paths = manifest["paths"]

            inventory_path = pr_root / paths["inventory"]
            CORE.write_inventory(inventory_path, parsed_inventory.to_mapping())
            manifest_path = pr_root / paths["manifest"]
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
            )
            review_path = pr_root / paths["review"]
            review_path.write_text(review, encoding="utf-8")

            authority_record = (
                authority_root / "metadata/records" / proposed.proposed_path
            )
            authority_record.parent.mkdir(parents=True)
            authority_record.write_text(
                CORE._dump_yaml(dict(proposed.baseline_record)), encoding="utf-8"
            )
            pr_record = pr_root / "metadata/records" / proposed.proposed_path
            pr_record.parent.mkdir(parents=True)
            pr_record.write_bytes(authority_record.read_bytes())

            alias = manifest["candidates"][0]["alias"]
            document, _, _ = GITHUB.apply_submission(
                CORE,
                CLI,
                parsed_inventory,
                empty_document(),
                book(),
                inventory_id=parsed_inventory.inventory_id,
                submissions=(
                    GITHUB.Submission(
                        alias=alias,
                        expected_decision=None,
                        disposition="accept",
                        rationale="Accepted in the hosted review.",
                        evidence=("fixture:hosted-review",),
                        details={},
                    ),
                ),
                provenance=GITHUB.derive_comment_provenance(event("unused")),
                manifest=manifest,
            )
            decisions_path = pr_root / paths["decisions"]
            decisions_path.parent.mkdir(parents=True)
            decisions_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            report = CORE.reconcile_inventory(
                parsed_inventory,
                book(document),
                pr_root / "metadata/records",
                validate_staged=lambda _path: None,
            )
            report_relative = (
                "source-adapters/dump-research-info/transactions/"
                "github-123.reconciliation-001.yaml"
            )
            report_path = pr_root / report_relative
            report_path.write_text(CORE._dump_yaml(report), encoding="utf-8")

            proposal_sidecar = write_sidecar(pr_root, [paths["inventory"]])
            reconcile_sidecar = write_sidecar(
                pr_root, ["metadata/records", report_relative]
            )
            changed = {
                "files": [
                    {"path": paths["inventory"], "status": "added", "mode": "100644"},
                    {"path": paths["manifest"], "status": "added", "mode": "100644"},
                    {"path": paths["review"], "status": "added", "mode": "100644"},
                    {"path": paths["decisions"], "status": "added", "mode": "100644"},
                    {
                        "path": f"metadata/records/{proposed.proposed_path}",
                        "status": "modified",
                        "mode": "100644",
                    },
                    {"path": report_relative, "status": "added", "mode": "100644"},
                    {"path": proposal_sidecar, "status": "added", "mode": "100644"},
                    {"path": reconcile_sidecar, "status": "added", "mode": "100644"},
                ]
            }
            pr_json = pr_root / "pr.json"
            changed_json = pr_root / "changed.json"
            proposal_run_json = pr_root / "proposal-run.json"
            pr_json.write_text(
                json.dumps(pull_request(GITHUB.pr_body_marker(manifest))),
                encoding="utf-8",
            )
            changed_json.write_text(json.dumps(changed), encoding="utf-8")
            proposal_run_json.write_text(json.dumps(proposal_run()), encoding="utf-8")
            arguments = SimpleNamespace(
                root=pr_root,
                pr_json=pr_json,
                changed_paths_json=changed_json,
                proposal_run_json=proposal_run_json,
                phase="reconciled",
                default_branch="main",
                trusted_default_sha="1" * 40,
            )
            previous_trusted_root = GITHUB.TRUSTED_ROOT
            GITHUB.TRUSTED_ROOT = authority_root
            try:
                validated = GITHUB.validate_pr_tree_files(arguments, CORE)
                self.assertTrue(validated["pr_tree_valid"])

                corrupted_report = deepcopy(report)
                corrupted_report["changed"] = not corrupted_report["changed"]
                report_path.write_text(
                    CORE._dump_yaml(corrupted_report), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "trusted replay result"
                ):
                    GITHUB.validate_pr_tree_files(arguments, CORE)

                report_path.write_text(CORE._dump_yaml(report), encoding="utf-8")
                corrupted_record = dict(proposed.proposed_record)
                corrupted_record["name"] = "Forged"
                pr_record.write_text(
                    CORE._dump_yaml(corrupted_record), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    GITHUB.CurationGitHubError, "trusted replay"
                ):
                    GITHUB.validate_pr_tree_files(arguments, CORE)
            finally:
                GITHUB.TRUSTED_ROOT = previous_trusted_root


class CliWorkflowTests(unittest.TestCase):
    def test_separate_pr_data_worktree_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            value = candidate("FIRST")
            parsed_inventory = inventory(value)
            inventory_relative = (
                "source-adapters/dump-research-info/transactions/github-123.yaml"
            )
            decisions_relative = (
                "source-adapters/dump-research-info/policy/curation-decisions.yaml"
            )
            manifest_relative = (
                "source-adapters/dump-research-info/transactions/"
                "github-123.review-manifest.yaml"
            )
            review_relative = (
                "source-adapters/dump-research-info/transactions/github-123.review.md"
            )
            inventory_path = root / inventory_relative
            decisions_path = root / decisions_relative
            CORE.write_inventory(
                inventory_path,
                parsed_inventory.to_mapping(),
                decisions_path=decisions_path,
            )
            records_path = root / "metadata/records" / value.proposed_path
            records_path.parent.mkdir(parents=True)
            records_path.write_text(
                yaml.safe_dump(dict(value.baseline_record), sort_keys=False),
                encoding="utf-8",
            )
            rendered = GITHUB.render_review_files(
                SimpleNamespace(
                    root=root,
                    adapter="dump-research-info",
                    inventory=inventory_relative,
                    decisions=decisions_relative,
                    manifest=manifest_relative,
                    review=review_relative,
                    base_sha="1" * 40,
                    head_sha="1" * 40,
                    public_data_actor="github-user:7@reviewer",
                    public_data_at="2026-08-19T13:14:15Z",
                    public_data_run_url=(
                        "https://github.com/con/site/actions/runs/123"
                    ),
                ),
                CORE,
            )
            manifest_bytes = (root / manifest_relative).read_bytes()
            review_bytes = (root / review_relative).read_bytes()
            manifest = yaml.safe_load(manifest_bytes)
            alias = manifest["candidates"][0]["alias"]
            comment = submission_text(
                parsed_inventory.inventory_id, [submission(alias)]
            )
            event_path = root / "event.json"
            pr_path = root / "pr.json"
            permission_path = root / "permission.json"
            proposal_run_path = root / "proposal-run.json"
            changes_path = root / "changes.json"
            comments_path = root / "comments.json"
            attestation_runs_path = root / "attestation-runs.json"
            runinfo_relative = write_sidecar(root, [inventory_relative])
            initial_changes = {
                "files": [
                    {"path": inventory_relative, "status": "added", "mode": "100644"},
                    {"path": manifest_relative, "status": "added", "mode": "100644"},
                    {"path": review_relative, "status": "added", "mode": "100644"},
                    {"path": runinfo_relative, "status": "added", "mode": "100644"},
                ]
            }
            pr_value = pull_request(rendered["pr_body_marker"])
            receipt_payload = GITHUB.proposal_receipt_payload(
                root,
                manifest,
                pr_value,
                runinfo_relative,
                workflow_run_attempt=1,
            )
            receipt = bot_comment(
                GITHUB._attestation_body("Proposal receipt.", receipt_payload),
                comment_id=100,
                created_at="2026-08-19T13:18:00Z",
            )
            event_path.write_text(json.dumps(event(comment)), encoding="utf-8")
            pr_path.write_text(json.dumps(pr_value), encoding="utf-8")
            permission_path.write_text(
                json.dumps(
                    {
                        "permission": "write",
                        "user": {"id": 7, "login": "reviewer"},
                    }
                ),
                encoding="utf-8",
            )
            proposal_evidence = proposal_run()
            proposal_evidence["conclusion"] = "failure"
            proposal_run_path.write_text(
                json.dumps(proposal_evidence), encoding="utf-8"
            )
            changes_path.write_text(json.dumps(initial_changes), encoding="utf-8")
            comments_path.write_text(json.dumps([receipt]), encoding="utf-8")
            attestation_runs_path.write_text(
                json.dumps([proposal_evidence]), encoding="utf-8"
            )
            arguments = SimpleNamespace(
                root=root,
                adapter="dump-research-info",
                inventory=inventory_relative,
                decisions=decisions_relative,
                manifest=manifest_relative,
                review=review_relative,
                event_json=event_path,
                pr_json=pr_path,
                permission_json=permission_path,
                proposal_run_json=proposal_run_path,
                comments_json=comments_path,
                attestation_runs_json=attestation_runs_path,
                changed_paths_json=changes_path,
                phase="initial",
                trusted_default_sha="1" * 40,
                default_branch="main",
                hosted_command="submit",
            )
            self.assertTrue(
                GITHUB.validate_pr_tree_files(arguments, CORE)["pr_tree_valid"]
            )
            self.assertTrue(GITHUB.validate_guard_files(arguments, CORE)["guard_valid"])
            applied = GITHUB.apply_comment_files(arguments, CORE, CLI)
            self.assertTrue(applied["changed"])
            self.assertEqual((root / manifest_relative).read_bytes(), manifest_bytes)
            self.assertEqual((root / review_relative).read_bytes(), review_bytes)
            reviewed_changes = deepcopy(initial_changes)
            reviewed_changes["files"].append(
                {"path": decisions_relative, "status": "added", "mode": "100644"}
            )
            ledger_payload = GITHUB.ledger_attestation_payload(
                root,
                manifest,
                pr_value,
                workflow_run_id="456",
                workflow_run_attempt=1,
                parent_head_sha="3" * 40,
                target_head_sha="4" * 40,
            )
            ledger_comment = bot_comment(
                GITHUB._attestation_body("Decision ledger checkpoint.", ledger_payload),
                comment_id=101,
                created_at="2026-08-19T23:35:00Z",
            )
            pr_value["head"]["sha"] = "4" * 40
            pr_path.write_text(json.dumps(pr_value), encoding="utf-8")
            comments_path.write_text(
                json.dumps([ledger_comment, receipt]), encoding="utf-8"
            )
            attestation_runs_path.write_text(
                json.dumps([comment_run(), proposal_evidence]), encoding="utf-8"
            )
            changes_path.write_text(json.dumps(reviewed_changes), encoding="utf-8")
            arguments.phase = "reviewed"
            completed = GITHUB.validate_complete_files(arguments, CORE)
            self.assertTrue(completed["complete"])
            self.assertEqual(completed["candidate_count"], 1)
            arguments.phase = "reconciled"
            with self.assertRaisesRegex(
                GITHUB.CurationGitHubError, "Finalization is terminal"
            ):
                GITHUB.apply_comment_files(arguments, CORE, CLI)
            with self.assertRaisesRegex(
                GITHUB.CurationGitHubError, "Finalization is terminal"
            ):
                GITHUB.validate_complete_files(arguments, CORE)
            self.assertNotEqual(GITHUB.TRUSTED_ROOT, root)


if __name__ == "__main__":
    unittest.main()
