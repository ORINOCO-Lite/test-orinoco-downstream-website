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

    def test_triggers_inputs_and_permissions_are_fail_closed(self) -> None:
        self.assertEqual(
            {"workflow_dispatch", "issue_comment"}, set(self.contract["on"])
        )
        inputs = self.contract["on"]["workflow_dispatch"]["inputs"]
        acknowledgment = inputs["acknowledge_public_review_data"]
        self.assertEqual("true", acknowledgment["required"])
        self.assertEqual("false", acknowledgment["default"])
        self.assertEqual("boolean", acknowledgment["type"])
        self.assertIn("reviewer identity", acknowledgment["description"])
        self.assertIn("decisions", acknowledgment["description"])
        self.assertEqual("main", inputs["source_refspec"]["default"])
        self.assertNotIn("as_of", inputs)
        self.assertNotIn("source_commit", inputs)
        self.assertEqual({}, self.contract["permissions"])
        self.assertEqual(
            {
                "actions": "read",
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

    def test_actions_are_pinned_and_tokens_are_not_persisted(self) -> None:
        references = re.findall(r"^\s*uses:\s*([^\s#]+)", self.text, re.MULTILINE)
        self.assertEqual(7, len(references))
        self.assertEqual(
            {
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "prefix-dev/setup-pixi@f00437f565399d418b0acc85936d12c1fb668347",
            },
            set(references),
        )
        for reference in references:
            self.assertRegex(reference, r"@[0-9a-f]{40}$")
        self.assertEqual(5, self.text.count("persist-credentials: false"))
        self.assertNotIn("persist-credentials: true", self.text)

    def test_proposal_uses_exact_source_and_ephemeral_plan_state(self) -> None:
        self.assertIn("Read immutable workflow-run coordinates", self.text)
        self.assertIn('run["run_started_at"]', self.text)
        self.assertNotIn("github.run_started_at", self.text)
        self.assertEqual(2, self.text.count("repository: con/dump-research-info"))
        self.assertIn('--source-revision "$SOURCE_COMMIT"', self.text)
        self.assertIn("--expected-library-version", self.text)
        plan = self.propose_steps["Plan the metadata diff and render the review form"][
            "run"
        ]
        self.assertIn("plan", plan)
        self.assertIn('--scratch "$scratch"', plan)
        self.assertIn('--body "$RUNNER_TEMP/pr-body.md"', plan)
        self.assertIn('--base-sha "$base_sha"', plan)
        self.assertIn('--as-of "$AS_OF"', plan)
        self.assertIn('> "$RUNNER_TEMP/plan.json"', plan)
        self.assertIn("zero undecided changes", plan)

    def test_proposal_preserves_one_native_datalad_metadata_commit(self) -> None:
        names = [step["name"] for step in self.propose["steps"]]
        branch = names.index(
            "Create the curation branch before recording metadata provenance"
        )
        datalad = names.index("Add proposed metadata with one native DataLad commit")
        push = names.index("Push the native proposal commit")
        pull = names.index("Open one draft PR with the review form in its body")
        self.assertLess(branch, datalad)
        self.assertLess(datalad, push)
        self.assertLess(push, pull)
        self.assertEqual(1, self.text.count("datalad run --explicit"))
        self.assertNotIn("--sidecar", self.text)
        stage = self.propose_steps[
            "Add proposed metadata with one native DataLad commit"
        ]["run"]
        self.assertIn("stage-proposal", stage)
        self.assertIn("-o metadata/records", stage)
        self.assertIn('git rev-list --count "${BASE_SHA}..HEAD"', stage)
        self.assertIn("proposal commit changed a path outside metadata/records", stage)
        self.assertNotIn("git reset", stage)
        self.assertNotIn("git commit --amend", stage)

    def test_proposal_opens_one_draft_pr_with_body_form_and_diff(self) -> None:
        step = self.propose_steps["Open one draft PR with the review form in its body"][
            "run"
        ]
        self.assertEqual(1, self.text.count("gh pr create"))
        self.assertIn('--body-file "$RUNNER_TEMP/pr-body.md"', step)
        self.assertIn("--draft", step)
        self.assertIn("--label curation-review", step)
        self.assertIn("**AI-generated draft — not reviewed by John**", self.text)
        self.assertIn("<!-- orinoco-lite-curation-form-v1 ", self.text)
        self.assertNotIn("peter-evans/create-pull-request", self.text)
        self.assertNotIn("source-adapters/${ADAPTER}/transactions", self.text)
        self.assertNotIn(".datalad/runinfo", self.text)
        label = self.propose_steps["Provision the curation review label"]["run"]
        self.assertEqual(2, label.count("labels/curation-review"))
        self.assertIn("gh label create curation-review", label)

    def test_comment_is_one_complete_exact_form_submission(self) -> None:
        recognition = self.comment_steps["Recognize only the exact submit command"][
            "run"
        ]
        self.assertIn(
            'event["comment"]["body"].strip() == "/curation submit"',
            recognition,
        )
        self.assertNotIn("/curation finalize", self.text)
        self.assertNotIn("```yaml", self.text)
        self.assertNotIn("queue:", self.text)
        self.assertEqual("false", self.comment["concurrency"]["cancel-in-progress"])
        names = [step["name"] for step in self.comment["steps"]]
        preflight = names.index("Preflight the draft PR and reviewer authority")
        self.assertLess(
            preflight, names.index("Check out trusted current default-branch code")
        )
        self.assertLess(
            preflight, names.index("Install the trusted locked Pixi environment")
        )
        self.assertIn(
            'permission.get("permission") not in {"write", "admin"}', self.text
        )
        self.assertIn('pull.get("draft")', self.text)
        self.assertIn('"automation/curation/"', self.text)
        self.assertIn('"curation-review" not in labels', self.text)

    def test_untrusted_pr_is_data_only_and_form_source_is_reproduced(self) -> None:
        names = [step["name"] for step in self.comment["steps"]]
        restrict = names.index("Restrict the untrusted PR diff before using its data")
        install = names.index("Install the trusted locked Pixi environment")
        inspect = names.index("Inspect the completed form with trusted code")
        apply = names.index("Apply all form decisions with trusted code")
        self.assertLess(restrict, install)
        self.assertLess(restrict, inspect)
        self.assertLess(inspect, apply)
        self.assertIn("path: trusted", self.text)
        self.assertIn("path: review", self.text)
        self.assertIn("proposal contains a path outside metadata/records", self.text)
        self.assertIn("proposal path has a forbidden mode", self.text)
        self.assertIn("initial curation PR must contain one DataLad commit", self.text)
        self.assertIn("inspect-form", self.text)
        self.assertIn("ref: ${{ steps.form.outputs.source_revision }}", self.text)
        apply_run = self.comment_steps["Apply all form decisions with trusted code"][
            "run"
        ]
        self.assertIn("trusted/source-adapters/metadata/tools/", apply_run)
        self.assertIn('--trusted-root "$GITHUB_WORKSPACE/trusted"', apply_run)
        self.assertIn('--review-root "$GITHUB_WORKSPACE/review"', apply_run)
        self.assertIn('--reviewer "https://github.com/${ACTOR}"', apply_run)
        self.assertIn('--reviewed-at "$COMMENT_CREATED_AT"', apply_run)
        self.assertNotIn("working-directory: review", self.text)

    def test_apply_writes_only_metadata_and_compact_decision_cache(self) -> None:
        self.assertIn(
            '"curation-decisions.yaml"',
            self.comment_steps["Apply all form decisions with trusted code"]["run"],
        )
        validation = self.comment_steps[
            "Validate the reconciled metadata with the locked runtime"
        ]["run"]
        self.assertIn("apply changed a path outside metadata and its cache", validation)
        self.assertIn("apply did not add or update the decision cache", validation)
        self.assertIn("pixi run --manifest-path trusted/pixi.toml orinoco", validation)
        self.assertIn('--root "$GITHUB_WORKSPACE/review" projection update', validation)
        self.assertIn('--root "$GITHUB_WORKSPACE/review" validate', validation)
        commit = self.comment_steps["Commit the decisions and reconciled metadata"][
            "run"
        ]
        self.assertIn('git -C review add -- metadata/records "$CACHE_PATH"', commit)
        self.assertIn('-m "chore(curation): record reviewed source metadata"', commit)
        self.assertEqual(1, self.text.count("datalad run --explicit"))

    def test_form_and_head_use_compare_and_swap_before_one_push(self) -> None:
        names = [step["name"] for step in self.comment["steps"]]
        recheck = names.index("Recheck the PR body before the commit and guarded push")
        commit = names.index("Commit the decisions and reconciled metadata")
        push = names.index("Push the review commit with an exact head lease")
        self.assertLess(recheck, commit)
        self.assertLess(commit, push)
        self.assertIn('body.encode("utf-8") != captured', self.text)
        self.assertIn("curation PR coordinates changed during submission", self.text)
        self.assertEqual(
            1,
            self.text.count('--force-with-lease="refs/heads/${HEAD_REF}:${HEAD_SHA}"'),
        )
        self.assertIn('merge-base --is-ancestor "$HEAD_SHA" HEAD', self.text)

    def test_review_dispatches_validation_but_never_publishes(self) -> None:
        self.assertEqual(1, self.text.count("gh workflow run validate.yml"))
        self.assertIn("Post a compact attributed submission summary", self.text)
        self.assertIn("Decision provenance:", self.text)
        self.assertNotIn("gh pr review", self.text)
        self.assertNotIn("gh pr merge", self.text)
        self.assertNotIn("auto-merge", self.text)
        self.assertNotIn("actions/deploy-pages", self.text)
        self.assertNotIn("gh workflow run pages", self.text)

    def test_obsolete_hosted_attestation_artifacts_are_absent(self) -> None:
        for obsolete in (
            "review-manifest",
            "attestation",
            "render-review",
            "render-proposal-receipt",
            "render-ledger-attestation",
            "list-attestation-run-ids",
            "validate-guard",
            "validate-reproposal",
            "apply-comment",
            "validate-complete",
            "curation-decision-event-v1",
            "DataLad-Run-Record:",
        ):
            self.assertNotIn(obsolete, self.text)


if __name__ == "__main__":
    unittest.main()
