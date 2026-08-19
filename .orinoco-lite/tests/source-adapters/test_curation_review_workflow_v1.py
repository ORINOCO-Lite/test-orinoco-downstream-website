from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/curation-review.yml"


class CurationReviewWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.contract = yaml.load(cls.text, Loader=yaml.BaseLoader)
        cls.propose = cls.contract["jobs"]["propose"]
        cls.comment = cls.contract["jobs"]["comment"]
        cls.propose_steps = {step["name"]: step for step in cls.propose["steps"]}
        cls.comment_steps = {step["name"]: step for step in cls.comment["steps"]}

    def test_triggers_acknowledgment_and_permissions_are_fail_closed(self) -> None:
        self.assertEqual(
            {"workflow_dispatch", "issue_comment"}, set(self.contract["on"])
        )
        acknowledgment = self.contract["on"]["workflow_dispatch"]["inputs"][
            "acknowledge_public_review_data"
        ]
        self.assertEqual(
            {"description", "required", "default", "type"}, set(acknowledgment)
        )
        self.assertEqual("true", acknowledgment["required"])
        self.assertEqual("false", acknowledgment["default"])
        self.assertEqual("boolean", acknowledgment["type"])
        source_refspec = self.contract["on"]["workflow_dispatch"]["inputs"][
            "source_refspec"
        ]
        self.assertEqual("main", source_refspec["default"])
        self.assertEqual("false", source_refspec["required"])
        self.assertEqual("string", source_refspec["type"])
        self.assertNotIn("as_of", self.contract["on"]["workflow_dispatch"]["inputs"])
        self.assertNotIn(
            "source_commit", self.contract["on"]["workflow_dispatch"]["inputs"]
        )
        self.assertIn("Read the immutable workflow-run coordinates", self.text)
        self.assertIn('run["run_started_at"]', self.text)
        self.assertIn("${{ steps.run.outputs.as_of }}", self.text)
        self.assertNotIn("github.run_started_at", self.text)
        self.assertIn(
            "Resolve the selected source refspec to an immutable commit", self.text
        )
        self.assertNotIn("${{ inputs.as_of }}", self.text)
        self.assertNotIn("${{ inputs.source_commit }}", self.text)
        self.assertIn(
            "source-adapters/${{ inputs.adapter }}/transactions/**", self.text
        )
        self.assertNotIn("source-adapters/${{ inputs.adapter }}/policy/**", self.text)
        self.assertIn('--public-data-actor "$PUBLIC_DATA_ACTOR"', self.text)
        self.assertEqual({}, self.contract["permissions"])
        self.assertEqual(
            {
                "actions": "write",
                "contents": "write",
                "issues": "write",
                "pull-requests": "write",
            },
            self.propose["permissions"],
        )
        self.assertEqual(
            {
                "actions": "write",
                "contents": "write",
                "issues": "write",
                "pull-requests": "read",
            },
            self.comment["permissions"],
        )
        self.assertIn("github.run_attempt == 1", self.propose["if"])
        self.assertIn(
            "github.ref_name == github.event.repository.default_branch",
            self.propose["if"],
        )
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("workflow_run", self.text)

    def test_actions_are_fully_pinned_and_checkouts_do_not_persist_tokens(self) -> None:
        references = re.findall(r"^\s*uses:\s*([^\s#]+)", self.text, re.MULTILINE)
        self.assertEqual(10, len(references))
        self.assertEqual(
            {
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "prefix-dev/setup-pixi@f00437f565399d418b0acc85936d12c1fb668347",
                "peter-evans/create-pull-request@98357b18bf14b5342f975ff684046ec3b2a07725",
            },
            set(references),
        )
        for reference in references:
            self.assertRegex(reference, r"@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", self.text)
        self.assertNotIn("persist-credentials: true", self.text)

    def test_proposal_is_exact_draft_only_and_posts_a_bot_receipt(self) -> None:
        self.assertEqual(2, self.text.count("repository: con/dump-research-info"))
        self.assertNotIn("source_repository:", self.text)
        self.assertNotIn("source_run_id:", self.text)
        self.assertIn("build/curation/sources/dump-research-info", self.text)
        self.assertIn("draft: true", self.text)
        self.assertIn("**AI-generated draft — not reviewed by John**", self.text)
        self.assertIn("The exact source proposal contained zero candidates.", self.text)
        self.assertIn("if: steps.proposal.outputs.has_candidates == 'true'", self.text)
        self.assertIn("render-proposal-receipt", self.text)
        self.assertIn("path: proposal-review", self.text)
        self.assertIn("path: proposal-trusted", self.text)
        self.assertEqual(
            "Post the proposal receipt as the final proposal mutation",
            self.propose["steps"][-1]["name"],
        )
        label = self.propose_steps["Provision the stable curation review label"]["run"]
        self.assertIn('gh api "repos/${REPOSITORY}/labels/curation-review"', label)
        self.assertNotIn("gh label view", label)
        self.assertIn("gh label create curation-review", label)
        self.assertNotIn("--force", label)
        self.assertEqual(2, self.text.count("gh workflow run validate.yml"))

    def test_comments_use_event_file_and_authorize_before_expensive_setup(self) -> None:
        self.assertNotIn("github.event.comment.body", self.text)
        self.assertIn('os.environ["GITHUB_EVENT_PATH"]', self.text)
        names = [step["name"] for step in self.comment["steps"]]
        preflight = names.index(
            "Preflight immutable PR and reviewer authority before setup"
        )
        self.assertLess(preflight, names.index("Check out trusted default-branch code"))
        self.assertLess(
            preflight, names.index("Install the trusted locked Pixi environment")
        )
        self.assertIn(
            'permission.get("permission") not in {"write", "admin"}', self.text
        )
        self.assertEqual("max", self.comment["concurrency"]["queue"])
        self.assertEqual("false", self.comment["concurrency"]["cancel-in-progress"])
        self.assertIn('body.strip() == "/curation finalize"', self.text)

    def test_attestation_chain_uses_exact_attempt_evidence_and_precedes_cas(
        self,
    ) -> None:
        self.assertIn("list-attestation-run-ids", self.text)
        self.assertIn("actions/runs/${run_id}/attempts/${run_attempt}", self.text)
        self.assertEqual(
            2,
            self.text.count("actions/runs/${RUN_ID}/attempts/${RUN_ATTEMPT}"),
        )
        self.assertIn("actions/runs/${run_id}/attempts/1", self.text)
        self.assertEqual(
            4, self.text.count('--comments-json "$RUNNER_TEMP/comments.json"')
        )
        self.assertEqual(
            3,
            self.text.count(
                '--attestation-runs-json "$RUNNER_TEMP/attestation-runs.json"'
            ),
        )
        names = [step["name"] for step in self.comment["steps"]]
        self.assertLess(
            names.index("Enforce the read-only trust and authorization guard"),
            names.index("Record the explicit submitted decisions"),
        )
        self.assertLess(
            names.index("Commit changed decisions locally for pre-push attestation"),
            names.index("Render the decision-ledger edge from trusted code"),
        )
        self.assertLess(
            names.index("Render the decision-ledger edge from trusted code"),
            names.index("Post the decision attestation before changing the PR ref"),
        )
        self.assertLess(
            names.index("Post the decision attestation before changing the PR ref"),
            names.index("Push the pre-attested decision edge to the same PR"),
        )
        status_if = self.comment_steps["Post an attributed success status"]["if"]
        self.assertIn("steps.submit.outputs.changed != 'true'", status_if)

    def test_trusted_helpers_guard_every_mutation_with_exact_contract(self) -> None:
        for command in (
            "select-manifest",
            "validate-pr-tree",
            "validate-guard",
            "validate-reproposal",
            "apply-comment",
            "validate-complete",
            "render-ledger-attestation",
        ):
            self.assertIn(command, self.text)
        self.assertIn('--hosted-command "$HOSTED_COMMAND"', self.text)
        self.assertGreaterEqual(
            self.text.count('--default-branch "$DEFAULT_BRANCH"'), 4
        )
        self.assertIn(
            "Reproduce and byte-compare the proposal from trusted state", self.text
        )
        self.assertIn("This curation transaction is already finalized.", self.text)
        self.assertIn(
            "Validate the reconciled tree before the terminal push", self.text
        )
        for step in self.comment["steps"]:
            if "run" in step:
                self.assertNotIn(
                    "${{ steps.transaction.outputs.",
                    step["run"],
                    step.get("name", "unnamed step"),
                )
        for name in (
            "Enforce the read-only trust and authorization guard",
            "Record the explicit submitted decisions",
            "Commit changed decisions locally for pre-push attestation",
            "Render the decision-ledger edge from trusted code",
            "Reconcile under retained DataLad execution evidence",
        ):
            self.assertNotIn("GH_TOKEN", self.comment_steps[name].get("env", {}))

    def test_datalad_evidence_is_regular_compressed_and_replayable(self) -> None:
        self.assertIn("datalad run --explicit --sidecar yes", self.text)
        self.assertEqual(
            2, self.text.count("load_stream(sys.argv[1], compressed=True)")
        )
        self.assertIn('record.get("outputs") != [sys.argv[2]]', self.text)
        self.assertIn(
            'record.get("outputs") != ["metadata/records", sys.argv[2]]',
            self.text,
        )
        self.assertIn("DataLad-Run-Record:", self.text)
        self.assertIn("git commit --amend", self.text)
        self.assertIn(
            '-m "chore(curation): reconcile reviewed source proposal"', self.text
        )

    def test_pushes_are_exact_lease_guarded_and_never_publish(self) -> None:
        self.assertEqual(
            2,
            self.text.count('--force-with-lease="refs/heads/${HEAD_REF}:${HEAD_SHA}"'),
        )
        self.assertEqual(
            2, self.text.count('git merge-base --is-ancestor "$HEAD_SHA" HEAD')
        )
        self.assertNotIn("run: pixi run test-all", self.text)
        self.assertNotIn("gh pr merge", self.text)
        self.assertNotIn("auto-merge", self.text)
        self.assertNotIn("actions/deploy-pages", self.text)
        self.assertNotIn("gh workflow run pages", self.text)


if __name__ == "__main__":
    unittest.main()
