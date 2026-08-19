from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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
    "orinoco_hosted_curation_test_core",
    ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
)
GITHUB = load_module(
    "orinoco_hosted_curation_test_helper",
    ROOT / "source-adapters/metadata/tools/curation_github_prototype_v1.py",
)

REVISION = "a" * 40
BASE_SHA = "b" * 40


def record(pid: str, value: str, *, pav: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "pid": pid,
        "schema_type": "xyzri:XYZOrganization",
        "display_label": f"Friendly {pid.rsplit('/', 1)[-1].title()}",
        "name": value,
    }
    if pav:
        result["annotations"] = {
            "pav:importedBy": {
                "annotation_tag": "pav:importedBy",
                "annotation_value": "urn:orinoco-lite:source-adapter:test",
            },
            "pav:importedFrom": {
                "annotation_tag": "pav:importedFrom",
                "annotation_value": "https://source.invalid/record",
            },
        }
    return result


def candidate(
    source_id: str,
    *,
    pid: str | None = None,
    baseline: dict[str, object] | None = None,
    material: str = "one",
    blocked: bool = False,
):
    pid = pid or f"xyzrins:records/{source_id.lower()}"
    proposed = record(pid, "new", pav=True)
    return CORE.make_candidate(
        adapter_id="dump-research-info",
        source_namespace="https://github.com/con/dump-research-info",
        source_record_id=source_id,
        claim_kind="record-import",
        material={"value": material},
        relevant_policy={"version": 1},
        proposed_path=f"XYZOrganization/{source_id.lower()}.yaml",
        proposed_record=proposed,
        baseline_record=baseline,
        blockers=("unresolved relation",) if blocked else (),
    )


def build(*candidates) -> object:
    return GITHUB.CandidateBuild(
        "dump-research-info",
        {"commit": REVISION},
        tuple(candidates),
    )


def form(*candidates) -> str:
    return GITHUB.render_form(
        build(*candidates),
        candidates,
        base_sha=BASE_SHA,
        as_of="2026-08-19",
    )


def select(body: str, choices: dict[str, str]) -> str:
    matches = list(GITHUB._RECORD_RE.finditer(body))
    for index in range(len(matches) - 1, -1, -1):
        match = matches[index]
        source_id = GITHUB._b64_decode(match.group("payload"), "test marker")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        segment = body[match.end() : end]
        choice = choices[str(source_id)].title()
        needle = f"- [ ] {choice}"
        if needle not in segment:
            segment = f"\n- [x] {choice}" + segment
        else:
            segment = segment.replace(needle, f"- [x] {choice}", 1)
        body = body[: match.end()] + segment + body[end:]
    return body


def empty_cache() -> dict[str, object]:
    return {
        "format": GITHUB.CACHE_FORMAT,
        "adapter": "dump-research-info",
        "reviews": {},
        "decisions": {},
    }


def write_baseline(root: Path, item) -> None:
    records = root / "metadata/records"
    records.mkdir(parents=True, exist_ok=True)
    baseline = item.baseline_record
    if baseline is not None:
        path = records / item.proposed_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(GITHUB._yaml_bytes(dict(baseline)))


class FormTests(unittest.TestCase):
    def test_form_is_friendly_and_contains_no_candidate_digest(self):
        normal = candidate(
            "XYZOrganization:CON",
            pid="xyzrins:records/con",
            baseline=record("xyzrins:records/con", "old"),
        )
        blocked = candidate("XYZProject:BLOCKED", blocked=True)

        body = form(normal, blocked)

        self.assertEqual(body.splitlines()[0], GITHUB.ATTRIBUTION)
        self.assertIn("Use the task-list", body)
        self.assertNotIn("edit this PR description", body)
        self.assertIn("Files changed", body)
        self.assertIn("check exactly one", body)
        self.assertIn("`/curation submit`", body)
        self.assertIn("Friendly Con", body)
        self.assertIn("Canonical ID: <code>xyzrins:records/con</code>", body)
        self.assertIn("Source ID: <code>XYZOrganization:CON</code>", body)
        self.assertNotIn(normal.candidate_id, body)
        self.assertNotIn(normal.material_fingerprint, body)
        self.assertNotIn(normal.relevant_policy_fingerprint, body)
        blocked_start = body.index(GITHUB._b64_encode(blocked.source_record_id))
        blocked_card = body[blocked_start:]
        self.assertIn("Accept is unavailable", blocked_card)
        self.assertNotIn("- [ ] Accept", blocked_card)
        self.assertEqual(
            GITHUB.inspect_form(body),
            {
                "format": GITHUB.FORM_FORMAT,
                "adapter": "dump-research-info",
                "base_sha": BASE_SHA,
                "as_of": "2026-08-19",
                "source": {"revision": REVISION},
            },
        )

    def test_form_requires_exactly_one_choice_for_every_record(self):
        first = candidate("FIRST")
        second = candidate("SECOND")
        body = form(first, second)
        submitted = select(body, {"FIRST": "accept", "SECOND": "reject"})
        self.assertEqual(
            GITHUB.parse_choices(submitted, (first, second)),
            {"FIRST": "accept", "SECOND": "reject"},
        )

        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "exactly one"):
            GITHUB.parse_choices(body, (first, second))

        multiple = submitted.replace("- [ ] Defer", "- [x] Defer", 1)
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "exactly one"):
            GITHUB.parse_choices(multiple, (first, second))

        one_card = submitted[
            : submitted.index(GITHUB._b64_encode(second.source_record_id))
        ]
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "missing review"):
            GITHUB.parse_choices(one_card, (first, second))

    def test_blocked_record_cannot_be_accepted(self):
        blocked = candidate("BLOCKED", blocked=True)
        body = select(form(blocked), {"BLOCKED": "accept"})
        with self.assertRaisesRegex(GITHUB.CurationGitHubError, "cannot be accepted"):
            GITHUB.parse_choices(body, (blocked,))


class CacheTests(unittest.TestCase):
    def _cache(self, items, choices):
        return GITHUB.updated_cache(
            empty_cache(),
            build(*items),
            items,
            choices,
            reviewer="https://github.com/reviewer",
            reviewed_at="2026-08-19T12:34:56Z",
            review_url="https://github.com/con/site/pull/22",
            pull_request_number=22,
        )

    def test_cache_is_compact_deterministic_and_keyed_by_canonical_pid(self):
        first = candidate("SOURCE-A", pid="xyzrins:records/readable-a")
        second = candidate("SOURCE-B", pid="xyzrins:records/readable-b")
        choices = {"SOURCE-A": "accept", "SOURCE-B": "reject"}

        one = self._cache((first, second), choices)
        two = self._cache((first, second), choices)

        self.assertEqual(one, two)
        self.assertEqual(
            set(one["decisions"]),
            {"xyzrins:records/readable-a", "xyzrins:records/readable-b"},
        )
        self.assertEqual(set(one["reviews"]), {"pr-22"})
        decision = one["decisions"]["xyzrins:records/readable-a"]
        self.assertEqual(
            set(decision),
            {"source_record_id", "claim_sha256", "disposition", "review"},
        )
        self.assertEqual(decision["source_record_id"], "SOURCE-A")
        self.assertEqual(len(decision["claim_sha256"]), 64)
        rendered = GITHUB._yaml_bytes(one)
        self.assertLess(len(rendered), 1000)
        self.assertNotIn(b"material_fingerprint", rendered)
        self.assertNotIn(b"candidate_id", rendered)

    def test_current_decision_replaces_prior_pid_for_same_source(self):
        old = candidate("SOURCE", pid="xyzrins:records/old")
        cache = self._cache((old,), {"SOURCE": "reject"})
        new = candidate("SOURCE", pid="xyzrins:records/new", material="two")

        updated = GITHUB.updated_cache(
            cache,
            build(new),
            (new,),
            {"SOURCE": "accept"},
            reviewer="https://github.com/another",
            reviewed_at="2026-08-20T01:02:03Z",
            review_url="https://github.com/con/site/pull/23",
            pull_request_number=23,
        )

        self.assertEqual(set(updated["decisions"]), {"xyzrins:records/new"})
        self.assertEqual(set(updated["reviews"]), {"pr-23"})

    def test_cache_suppression_and_change_invalidation(self):
        accept = candidate("ACCEPT")
        reject = candidate("REJECT")
        defer = candidate("DEFER")
        cache = self._cache(
            (accept, reject, defer),
            {"ACCEPT": "accept", "REJECT": "reject", "DEFER": "defer"},
        )
        accepted_current = candidate(
            "ACCEPT", baseline=deepcopy(dict(accept.proposed_record))
        )
        rejected_same = candidate("REJECT")
        deferred_same = candidate("DEFER")

        pending = GITHUB.pending_candidates(
            (accepted_current, rejected_same, deferred_same), cache
        )
        self.assertEqual([item.source_record_id for item in pending], ["DEFER"])

        changed = candidate("REJECT", material="changed")
        pending = GITHUB.pending_candidates((changed,), cache)
        self.assertEqual(pending, (changed,))

    def test_exact_noop_never_needs_review(self):
        item = candidate("NOOP")
        noop = candidate("NOOP", baseline=deepcopy(dict(item.proposed_record)))
        self.assertEqual(GITHUB.pending_candidates((noop,), empty_cache()), ())


class ApplyTests(unittest.TestCase):
    def test_stage_rejects_symlinked_metadata_tree(self):
        item = candidate("LINK")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records = root / "metadata/records"
            records.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (records / "XYZOrganization").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(GITHUB.CurationGitHubError, "symbolic link"):
                GITHUB.stage_records(root, (item,))

    def test_apply_retains_pav_and_restores_reject_and_defer(self):
        accepted = candidate(
            "ACCEPT",
            baseline=record("xyzrins:records/accept", "old"),
        )
        rejected = candidate(
            "REJECT",
            baseline=record("xyzrins:records/reject", "old"),
        )
        deferred = candidate("DEFER")
        items = (accepted, rejected, deferred)

        with tempfile.TemporaryDirectory() as name:
            trusted = Path(name) / "trusted"
            review = Path(name) / "review"
            for item in items:
                write_baseline(trusted, item)
            shutil.copytree(trusted, review)
            GITHUB.stage_records(review, items)
            body = select(
                form(*items),
                {"ACCEPT": "accept", "REJECT": "reject", "DEFER": "defer"},
            )

            result = GITHUB.apply_review(
                trusted,
                review,
                build(*items),
                items,
                body,
                reviewer="https://github.com/reviewer",
                reviewed_at="2026-08-19T12:34:56Z",
                review_url="https://github.com/con/site/pull/22",
                pull_request_number=22,
            )

            accepted_record = yaml.safe_load(
                (review / "metadata/records" / accepted.proposed_path).read_text()
            )
            self.assertIn("pav:importedBy", accepted_record["annotations"])
            self.assertIn("pav:importedFrom", accepted_record["annotations"])
            self.assertEqual(
                (review / "metadata/records" / rejected.proposed_path).read_bytes(),
                (trusted / "metadata/records" / rejected.proposed_path).read_bytes(),
            )
            self.assertFalse(
                (review / "metadata/records" / deferred.proposed_path).exists()
            )
            cache = GITHUB.load_cache(review, "dump-research-info")
            self.assertEqual(
                {
                    pid: decision["disposition"]
                    for pid, decision in cache["decisions"].items()
                },
                {
                    "xyzrins:records/accept": "accept",
                    "xyzrins:records/reject": "reject",
                    "xyzrins:records/defer": "defer",
                },
            )
            self.assertEqual(result["count"], 3)
            self.assertEqual(
                result["cache_path"],
                "source-adapters/dump-research-info/policy/curation-decisions.yaml",
            )

    def test_apply_rejects_unexpected_metadata(self):
        item = candidate("EXPECTED")
        with tempfile.TemporaryDirectory() as name:
            trusted = Path(name) / "trusted"
            review = Path(name) / "review"
            write_baseline(trusted, item)
            shutil.copytree(trusted, review)
            GITHUB.stage_records(review, (item,))
            unexpected = review / "metadata/records/XYZOrganization/unexpected.yaml"
            unexpected.write_text("pid: unexpected\n", encoding="utf-8")

            with self.assertRaisesRegex(GITHUB.CurationGitHubError, "exact proposal"):
                GITHUB.apply_review(
                    trusted,
                    review,
                    build(item),
                    (item,),
                    select(form(item), {"EXPECTED": "accept"}),
                    reviewer="https://github.com/reviewer",
                    reviewed_at="2026-08-19T12:34:56Z",
                    review_url="https://github.com/con/site/pull/22",
                    pull_request_number=22,
                )


class CliBoundaryTests(unittest.TestCase):
    def test_stage_requires_base_and_as_of_coordinates(self):
        arguments = GITHUB.parser().parse_args(
            [
                "stage-proposal",
                "--root",
                ".",
                "--adapter",
                "dump-research-info",
                "--scratch",
                "scratch",
                "--base-sha",
                BASE_SHA,
                "--as-of",
                "2026-08-19",
                "--source-path",
                "source",
                "--source-revision",
                REVISION,
            ]
        )
        self.assertEqual(arguments.base_sha, BASE_SHA)
        self.assertEqual(arguments.as_of, "2026-08-19")

        with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
            GITHUB.parser().parse_args(
                [
                    "stage-proposal",
                    "--root",
                    ".",
                    "--adapter",
                    "dump-research-info",
                    "--scratch",
                    "scratch",
                ]
            )

    def test_apply_derives_source_revision_from_form_marker(self):
        item = candidate("CLI")
        body = form(item)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            trusted = root / "trusted"
            review = root / "review"
            trusted.mkdir()
            review.mkdir()
            body_path = root / "body.md"
            body_path.write_text(body, encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_build(*args, **kwargs):
                captured.update(kwargs)
                return build(item)

            with (
                mock.patch.object(GITHUB, "build_candidates", side_effect=fake_build),
                mock.patch.object(GITHUB, "load_cache", return_value=empty_cache()),
                mock.patch.object(GITHUB, "pending_candidates", return_value=(item,)),
                mock.patch.object(
                    GITHUB,
                    "apply_review",
                    return_value={"count": 1, "cache_path": "cache.yaml"},
                ),
                redirect_stdout(StringIO()) as output,
            ):
                status = GITHUB.main(
                    [
                        "apply",
                        "--trusted-root",
                        str(trusted),
                        "--review-root",
                        str(review),
                        "--body",
                        str(body_path),
                        "--scratch",
                        str(root / "scratch"),
                        "--reviewer",
                        "https://github.com/reviewer",
                        "--reviewed-at",
                        "2026-08-19T12:34:56Z",
                        "--review-url",
                        "https://github.com/con/site/pull/22",
                        "--pull-request-number",
                        "22",
                        "--source-path",
                        "source",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual(captured["source_revision"], REVISION)
            self.assertEqual(json.loads(output.getvalue())["count"], 1)


if __name__ == "__main__":
    unittest.main()
