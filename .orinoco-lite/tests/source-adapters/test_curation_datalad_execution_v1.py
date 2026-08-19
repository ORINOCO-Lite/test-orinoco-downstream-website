from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from datalad.support.json_py import load_stream


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "orinoco_curation_prototype_v1_datalad_acceptance",
    ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
)
assert SPEC is not None and SPEC.loader is not None
CORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)

GENERATOR = "generate_synthetic_evidence.py"
INVENTORY = "synthetic-curation/inventory.yaml"
DECISIONS = "synthetic-curation/decisions.yaml"
REPORT = "synthetic-curation/report.yaml"
ARTIFACTS = (INVENTORY, DECISIONS, REPORT)


def run_checked(
    arguments: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for inherited in (
        "PIXI_ENVIRONMENT_NAME",
        "PIXI_ENVIRONMENT_PLATFORMS",
        "PIXI_IN_SHELL",
        "PIXI_PROJECT_MANIFEST",
        "PIXI_PROJECT_NAME",
        "PIXI_PROJECT_ROOT",
        "PIXI_PROJECT_VERSION",
        "PIXI_PROMPT",
    ):
        environment.pop(inherited, None)
    environment.update(
        {
            "DATALAD_LOG_LEVEL": "warning",
            "GIT_AUTHOR_DATE": "2026-08-18T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-18T00:00:00Z",
            "LC_ALL": "C",
            "NO_COLOR": "1",
        }
    )
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(arguments)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(repo: Path, *arguments: str, check: bool = True) -> str:
    return run_checked(
        ["git", *arguments],
        cwd=repo,
        check=check,
    ).stdout.strip()


def synthetic_payloads(empty_tree_digest: str) -> dict[str, dict[str, object]]:
    candidate = CORE.make_candidate(
        adapter_id="synthetic-acceptance",
        source_namespace="fixture:datalad-squash",
        source_record_id="SYNTHETIC-ONLY",
        claim_kind="create-record",
        material={"title": "Synthetic acceptance candidate"},
        relevant_policy={"policy": "synthetic-only-v1"},
        proposed_path="Synthetic/never-materialized.yaml",
        proposed_record={
            "pid": "synthetic:never-materialized",
            "name": "Synthetic acceptance candidate",
        },
        baseline_record=None,
    )
    inventory = CORE.build_inventory(
        candidate.adapter_id,
        [candidate],
        context=CORE.EvaluationContext(as_of=date(2026, 8, 18)),
        inputs={
            "source": {
                "kind": "synthetic-acceptance-fixture",
                "revision": "sha256:" + "1" * 64,
            },
            "policy": {"coordinate": "synthetic-policy-v1"},
        },
    )
    claim_revision_id = CORE.claim_revision_identity(
        candidate.candidate_id,
        candidate.material_fingerprint,
        candidate.relevant_policy_fingerprint,
    )
    reviewer = "synthetic-reviewer@example.invalid"
    rationale = "Reject a synthetic-only candidate for merge-evidence acceptance."
    evidence = ["synthetic:local-datalad-squash-acceptance"]
    decision_id = CORE.decision_identity(
        claim_revision_id=claim_revision_id,
        supersedes_decision_id=None,
        disposition="reject",
        reviewer=reviewer,
        decided_on="2026-08-18",
        rationale=rationale,
        evidence=evidence,
    )
    decision = {
        "decision_id": decision_id,
        "claim_revision_id": claim_revision_id,
        "supersedes_decision_id": None,
        "candidate_id": candidate.candidate_id,
        "adapter_id": candidate.adapter_id,
        "source_namespace": candidate.source_namespace,
        "source_record_id": candidate.source_record_id,
        "claim_kind": candidate.claim_kind,
        "material_fingerprint": candidate.material_fingerprint,
        "relevant_policy_fingerprint": candidate.relevant_policy_fingerprint,
        "disposition": "reject",
        "reviewer": reviewer,
        "decided_on": "2026-08-18",
        "rationale": rationale,
        "evidence": evidence,
    }
    decisions = {
        "format": CORE.DECISIONS_FORMAT,
        "decisions": [decision],
        "transactions": [
            {
                "inventory_id": inventory["inventory_id"],
                "decision_ids": [decision_id],
            }
        ],
    }
    CORE.parse_inventory(inventory)
    CORE.parse_decisions(json.dumps(decisions))
    report = {
        "format": CORE.RECONCILIATION_FORMAT,
        "inventory_id": inventory["inventory_id"],
        "adapter_id": candidate.adapter_id,
        "before_digest": empty_tree_digest,
        "after_digest": empty_tree_digest,
        "changed": False,
        "outcomes": [
            {
                "candidate_id": candidate.candidate_id,
                "disposition": "reject",
                "metadata_action": "leave-canonical-unchanged",
            }
        ],
    }
    return {
        INVENTORY: inventory,
        DECISIONS: decisions,
        REPORT: report,
    }


def generator_source(payloads: dict[str, dict[str, object]]) -> str:
    encoded = json.dumps(payloads, sort_keys=True, separators=(",", ":"))
    return (
        "from pathlib import Path\n"
        "import json\n\n"
        f"PAYLOADS = json.loads({encoded!r})\n"
        "for relative, payload in sorted(PAYLOADS.items()):\n"
        "    target = Path(relative)\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    target.write_text(\n"
        "        json.dumps(payload, indent=2, sort_keys=True) + '\\n',\n"
        "        encoding='utf-8',\n"
        "    )\n"
    )


def aggregate_digest(repo: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        content = (repo / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class DataLadExecutionAndSquashAcceptance(unittest.TestCase):
    def test_sidecar_and_curation_artifacts_survive_squash_without_run_commit(
        self,
    ) -> None:
        pixi = shutil.which("pixi")
        self.assertIsNotNone(pixi, "Pixi is required for DataLad acceptance")
        assert pixi is not None

        with tempfile.TemporaryDirectory(prefix=".m5-datalad-", dir=ROOT) as tmp:
            repo = Path(tmp)
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Synthetic Acceptance")
            git(repo, "config", "user.email", "synthetic@example.invalid")
            git(repo, "config", "commit.gpgsign", "false")
            git(repo, "config", "core.hooksPath", "/dev/null")
            git(repo, "config", "datalad.run.record-directory", ".datalad/runinfo")

            payloads = synthetic_payloads(
                CORE.metadata_tree_digest(repo / "canonical-records")
            )
            (repo / GENERATOR).write_text(
                generator_source(payloads),
                encoding="utf-8",
            )
            git(repo, "add", GENERATOR)
            git(repo, "commit", "--no-verify", "-m", "test: add synthetic generator")
            base_commit = git(repo, "rev-parse", "HEAD^{commit}")

            self.assertFalse((repo / ".datalad/config").exists())
            self.assertFalse((repo / ".git/annex").exists())
            self.assertFalse((repo / ".gitmodules").exists())
            git(repo, "switch", "-c", "synthetic-review")

            run_checked(
                [
                    pixi,
                    "run",
                    "datalad",
                    "run",
                    "--explicit",
                    "--sidecar",
                    "yes",
                    "-m",
                    "record synthetic curation execution evidence",
                    "-i",
                    GENERATOR,
                    "-o",
                    INVENTORY,
                    "-o",
                    DECISIONS,
                    "-o",
                    REPORT,
                    "--",
                    "python",
                    GENERATOR,
                ],
                cwd=repo,
            )
            run_commit = git(repo, "rev-parse", "HEAD^{commit}")
            self.assertNotEqual(run_commit, base_commit)
            self.assertEqual(git(repo, "status", "--porcelain=v1"), "")
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD"), "2")

            sidecars = git(
                repo,
                "ls-tree",
                "-r",
                "--name-only",
                "HEAD",
                ".datalad/runinfo",
            ).splitlines()
            self.assertEqual(len(sidecars), 1)
            sidecar = sidecars[0]
            run_message = git(repo, "show", "-s", "--format=%B", run_commit)
            self.assertIn(Path(sidecar).name, run_message)

            run_records = list(load_stream(str(repo / sidecar), compressed=True))
            self.assertEqual(len(run_records), 1)
            run_record = run_records[0]
            self.assertEqual(run_record["cmd"], f"python {GENERATOR}")
            self.assertEqual(run_record["exit"], 0)
            self.assertEqual(run_record["pwd"], ".")
            self.assertNotIn("dsid", run_record)
            self.assertEqual(run_record["inputs"], [GENERATOR])
            self.assertEqual(set(run_record["outputs"]), set(ARTIFACTS))

            evidence_paths = [*ARTIFACTS, sidecar]
            before_hashes = {
                relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
                for relative in evidence_paths
            }
            before_digest = aggregate_digest(repo, evidence_paths)
            run_tree = git(repo, "rev-parse", f"{run_commit}^{{tree}}")

            git(repo, "switch", "main")
            git(repo, "merge", "--squash", "synthetic-review")
            git(
                repo,
                "commit",
                "--no-verify",
                "-m",
                "test: squash synthetic curation evidence",
            )
            squash_commit = git(repo, "rev-parse", "HEAD^{commit}")
            squash_tree = git(repo, "rev-parse", "HEAD^{tree}")
            self.assertEqual(squash_tree, run_tree)
            self.assertNotEqual(squash_commit, run_commit)
            self.assertEqual(
                git(repo, "rev-list", "--parents", "-n", "1", squash_commit).split(),
                [squash_commit, base_commit],
            )
            ancestor_check = run_checked(
                ["git", "merge-base", "--is-ancestor", run_commit, squash_commit],
                cwd=repo,
                check=False,
            )
            self.assertEqual(ancestor_check.returncode, 1)

            git(repo, "branch", "-D", "synthetic-review")
            self.assertEqual(
                git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "--contains",
                    run_commit,
                ),
                "",
            )
            git(repo, "reflog", "expire", "--expire=now", "--all")
            git(repo, "gc", "--prune=now")
            pruned = run_checked(
                ["git", "cat-file", "-e", f"{run_commit}^{{commit}}"],
                cwd=repo,
                check=False,
            )
            self.assertNotEqual(pruned.returncode, 0)

            after_hashes = {
                relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
                for relative in evidence_paths
            }
            self.assertEqual(after_hashes, before_hashes)
            self.assertEqual(aggregate_digest(repo, evidence_paths), before_digest)
            self.assertEqual(git(repo, "rev-list", "--count", "main"), "2")
            self.assertFalse((repo / ".datalad/config").exists())
            self.assertFalse((repo / ".git/annex").exists())
            self.assertFalse((repo / ".gitmodules").exists())
            tracked_modes = {
                line.split(maxsplit=1)[0]
                for line in git(repo, "ls-files", "--stage").splitlines()
            }
            self.assertNotIn("160000", tracked_modes)
            self.assertFalse((repo / "canonical-records").exists())
            self.assertEqual(
                list(load_stream(str(repo / sidecar), compressed=True)),
                [run_record],
            )

            parsed_inventory = CORE.load_inventory(repo / INVENTORY)
            parsed_decisions = CORE.load_decisions(repo / DECISIONS)
            report = json.loads((repo / REPORT).read_text(encoding="utf-8"))
            self.assertEqual(parsed_inventory.inventory_id, report["inventory_id"])
            self.assertIn(
                parsed_inventory.inventory_id,
                parsed_decisions.transactions,
            )
            self.assertFalse(report["changed"])

            print(
                "SYNTHETIC_DATALAD_SQUASH_EVIDENCE "
                + json.dumps(
                    {
                        "artifact_count": len(ARTIFACTS),
                        "canonical_change_count": 0,
                        "evidence_sha256": before_digest,
                        "git_tree_oid": squash_tree,
                        "main_commit_count": 2,
                        "ordinary_git_without_annex": True,
                        "run_commit_pruned": True,
                        "sidecar_count": len(sidecars),
                        "sidecar_sha256": before_hashes[sidecar],
                        "tracked_evidence_count": len(evidence_paths),
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
