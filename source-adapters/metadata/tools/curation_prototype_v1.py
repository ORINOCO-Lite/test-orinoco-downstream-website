#!/usr/bin/env python3
"""Implementation-neutral prototype for durable source-adapter curation.

This module is deliberately labelled as a prototype.  It demonstrates common
behaviour for two site-owned adapters without declaring a stable Python API,
storage location, or serialization contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import tempfile
from types import MappingProxyType
from typing import Callable, Iterable, Iterator, Mapping, Sequence

import yaml


INVENTORY_FORMAT = "orinoco-lite-candidate-inventory-prototype-v1"
DECISIONS_FORMAT = "orinoco-lite-curation-decisions-prototype-v1"
RECONCILIATION_FORMAT = "orinoco-lite-reconciliation-report-prototype-v1"
FINGERPRINT_PREFIX = "sha256:"
CANDIDATE_PREFIX = "curation-candidate-v1:"
CLAIM_REVISION_PREFIX = "curation-claim-revision-v1:"
DECISION_PREFIX = "curation-decision-event-v1:"
INVENTORY_PREFIX = "curation-inventory-v1:"
STAGE_PREFIX = ".curation-prototype-v1-stage-"
LOCK_PREFIX = ".curation-prototype-v1-lock-"
LOCK_FORMAT = "orinoco-lite-curation-lock-prototype-v1"
EVALUATION_CONTEXT_INPUT = "evaluation_context"
DISPOSITIONS = frozenset(
    {
        "accept",
        "reject",
        "link",
        "defer",
        "permanent-exclude",
        "supersede",
    }
)


class CurationPrototypeError(RuntimeError):
    """Fail closed when prototype curation state is unsafe or inconsistent."""


class _ReadOnlyJsonMapping(Mapping[str, object]):
    """Expose strict JSON values by detached copy through a read-only mapping."""

    def __init__(self, value: Mapping[str, object]) -> None:
        self._keys = tuple(value)
        self._encoded = MappingProxyType(
            {key: _canonical_bytes(value[key]) for key in self._keys}
        )

    def __getitem__(self, key: str) -> object:
        return json.loads(self._encoded[key])

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def _require_nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurationPrototypeError(f"{label} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise CurationPrototypeError(f"{label} must be a single line")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise CurationPrototypeError(
            f"Fingerprint input is not deterministic JSON data: {error}"
        ) from error
    return rendered.encode("utf-8")


def fingerprint(value: object) -> str:
    """Return a deterministic fingerprint for adapter-selected JSON data."""

    return FINGERPRINT_PREFIX + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalize_inputs(value: object, path: str = "inputs") -> object:
    """Return strict JSON input evidence while excluding run-local identity."""

    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        if not all(isinstance(key, str) and key for key in value):
            raise CurationPrototypeError(f"{path} keys must be non-empty strings")
        for key in sorted(value):
            item = value[key]
            run_key = key.lower().replace("-", "_")
            if run_key == "run_id" or run_key.endswith("_run_id"):
                raise CurationPrototypeError(
                    f"{path}.{key} is run-local and cannot enter inventory inputs"
                )
            normalized[key] = _normalize_inputs(item, f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        return [
            _normalize_inputs(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (bool, int, float, str)):
        _canonical_bytes(value)
        return value
    raise CurationPrototypeError(f"{path} must contain only strict JSON data")


def _validate_fingerprint(value: object, label: str) -> str:
    result = _require_nonempty(value, label)
    expected_length = len(FINGERPRINT_PREFIX) + 64
    if len(result) != expected_length or not result.startswith(FINGERPRINT_PREFIX):
        raise CurationPrototypeError(f"{label} must be a sha256 fingerprint")
    try:
        int(result[len(FINGERPRINT_PREFIX) :], 16)
    except ValueError as error:
        raise CurationPrototypeError(f"{label} must be a sha256 fingerprint") from error
    return result


def candidate_identity(
    adapter_id: str,
    source_namespace: str,
    source_record_id: str,
    claim_kind: str,
) -> str:
    """Identify a claim without run-local or change-detection inputs."""

    identity = {
        "adapter_id": _require_nonempty(adapter_id, "adapter_id"),
        "claim_kind": _require_nonempty(claim_kind, "claim_kind"),
        "source_namespace": _require_nonempty(source_namespace, "source_namespace"),
        "source_record_id": _require_nonempty(source_record_id, "source_record_id"),
    }
    return CANDIDATE_PREFIX + hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def claim_revision_identity(
    candidate_id: str,
    material_fingerprint: str,
    relevant_policy_fingerprint: str,
) -> str:
    """Identify one exact material-and-policy revision of a stable claim."""

    revision = {
        "candidate_id": _require_nonempty(candidate_id, "candidate_id"),
        "material_fingerprint": _validate_fingerprint(
            material_fingerprint, "material_fingerprint"
        ),
        "relevant_policy_fingerprint": _validate_fingerprint(
            relevant_policy_fingerprint, "relevant_policy_fingerprint"
        ),
    }
    return (
        CLAIM_REVISION_PREFIX + hashlib.sha256(_canonical_bytes(revision)).hexdigest()
    )


def _validate_prefixed_identity(value: object, prefix: str, label: str) -> str:
    identifier = _require_nonempty(value, label)
    if len(identifier) != len(prefix) + 64 or not identifier.startswith(prefix):
        raise CurationPrototypeError(f"{label} has an unsupported identity format")
    try:
        int(identifier[len(prefix) :], 16)
    except ValueError as error:
        raise CurationPrototypeError(
            f"{label} has an unsupported identity format"
        ) from error
    return identifier


def decision_identity(
    *,
    claim_revision_id: str,
    supersedes_decision_id: str | None,
    disposition: str,
    reviewer: str,
    decided_on: str,
    rationale: str,
    evidence: Sequence[str],
    target_record_id: str | None = None,
    return_when: Mapping[str, object] | None = None,
    scope: Mapping[str, object] | None = None,
    replacement_candidate_id: str | None = None,
) -> str:
    """Identify one durable review event, distinct from its claim revision."""

    claim_revision_id = _validate_prefixed_identity(
        claim_revision_id, CLAIM_REVISION_PREFIX, "claim_revision_id"
    )
    if supersedes_decision_id is not None:
        supersedes_decision_id = _validate_prefixed_identity(
            supersedes_decision_id,
            DECISION_PREFIX,
            "supersedes_decision_id",
        )
    if disposition not in DISPOSITIONS:
        raise CurationPrototypeError(f"Unsupported disposition {disposition!r}")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        raise CurationPrototypeError("decision evidence must be a non-empty sequence")
    normalized_evidence = [
        _require_nonempty(item, "decision evidence entry") for item in evidence
    ]
    if not normalized_evidence or len(normalized_evidence) != len(
        set(normalized_evidence)
    ):
        raise CurationPrototypeError(
            "decision evidence must be non-empty and contain unique entries"
        )

    conditional_values = {
        "target_record_id": target_record_id,
        "return_when": return_when,
        "scope": scope,
        "replacement_candidate_id": replacement_candidate_id,
    }
    conditional_name = {
        "link": "target_record_id",
        "defer": "return_when",
        "permanent-exclude": "scope",
        "supersede": "replacement_candidate_id",
    }.get(disposition)
    for name, value in conditional_values.items():
        if (name == conditional_name) != (value is not None):
            qualifier = "requires" if name == conditional_name else "does not allow"
            raise CurationPrototypeError(f"{disposition} {qualifier} {name}")

    event: dict[str, object] = {
        "claim_revision_id": claim_revision_id,
        "supersedes_decision_id": supersedes_decision_id,
        "disposition": disposition,
        "reviewer": _require_nonempty(reviewer, "decision reviewer"),
        "decided_on": _require_nonempty(decided_on, "decision decided_on"),
        "rationale": _require_nonempty(rationale, "decision rationale"),
        "evidence": normalized_evidence,
    }
    if conditional_name is not None:
        event[conditional_name] = conditional_values[conditional_name]
    return DECISION_PREFIX + hashlib.sha256(_canonical_bytes(event)).hexdigest()


def _validate_proposed_path(value: object) -> str:
    path = _require_nonempty(value, "proposed_path")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or path != pure.as_posix()
        or "\\" in path
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix not in {".yaml", ".yml"}
    ):
        raise CurationPrototypeError(
            "proposed_path must be a normalized relative YAML path"
        )
    return path


def _record(
    value: object, label: str, *, nullable: bool = False
) -> dict[str, object] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict):
        qualifier = " or null" if nullable else ""
        raise CurationPrototypeError(f"{label} must be a mapping{qualifier}")
    pid = value.get("pid")
    _require_nonempty(pid, f"{label}.pid")
    # Round-tripping through canonical JSON also rejects non-portable values.
    _canonical_bytes(value)
    return json.loads(_canonical_bytes(value))


def _parse_blockers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CurationPrototypeError("candidate blockers must be a list")
    return tuple(_require_nonempty(item, "candidate blocker") for item in value)


@dataclass(frozen=True)
class Candidate:
    """One source claim and the metadata operation proposed for it."""

    adapter_id: str
    source_namespace: str
    source_record_id: str
    claim_kind: str
    material_fingerprint: str
    relevant_policy_fingerprint: str
    proposed_path: str
    proposed_record: Mapping[str, object]
    baseline_record: Mapping[str, object] | None = None
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.adapter_id, "adapter_id")
        _require_nonempty(self.source_namespace, "source_namespace")
        _require_nonempty(self.source_record_id, "source_record_id")
        _require_nonempty(self.claim_kind, "claim_kind")
        _validate_fingerprint(self.material_fingerprint, "material_fingerprint")
        _validate_fingerprint(
            self.relevant_policy_fingerprint, "relevant_policy_fingerprint"
        )
        _validate_proposed_path(self.proposed_path)
        _record(dict(self.proposed_record), "proposed_record")
        if self.baseline_record is not None:
            _record(dict(self.baseline_record), "baseline_record")
        if not isinstance(self.blockers, tuple):
            raise CurationPrototypeError("blockers must be a tuple")
        for blocker in self.blockers:
            _require_nonempty(blocker, "candidate blocker")
        if len(self.blockers) != len(set(self.blockers)):
            raise CurationPrototypeError("candidate blockers must be unique")

    @property
    def candidate_id(self) -> str:
        return candidate_identity(
            self.adapter_id,
            self.source_namespace,
            self.source_record_id,
            self.claim_kind,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "adapter_id": self.adapter_id,
            "source_namespace": self.source_namespace,
            "source_record_id": self.source_record_id,
            "claim_kind": self.claim_kind,
            "material_fingerprint": self.material_fingerprint,
            "relevant_policy_fingerprint": self.relevant_policy_fingerprint,
            "proposed_path": self.proposed_path,
            "proposed_record": json.loads(_canonical_bytes(self.proposed_record)),
            "baseline_record": (
                None
                if self.baseline_record is None
                else json.loads(_canonical_bytes(self.baseline_record))
            ),
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Candidate:
        if not isinstance(value, dict):
            raise CurationPrototypeError("Inventory candidate must be a mapping")
        fields = {
            "candidate_id",
            "adapter_id",
            "source_namespace",
            "source_record_id",
            "claim_kind",
            "material_fingerprint",
            "relevant_policy_fingerprint",
            "proposed_path",
            "proposed_record",
            "baseline_record",
            "blockers",
        }
        if set(value) != fields:
            raise CurationPrototypeError(
                "Inventory candidate has missing or unexpected fields"
            )
        candidate = cls(
            adapter_id=_require_nonempty(value["adapter_id"], "adapter_id"),
            source_namespace=_require_nonempty(
                value["source_namespace"], "source_namespace"
            ),
            source_record_id=_require_nonempty(
                value["source_record_id"], "source_record_id"
            ),
            claim_kind=_require_nonempty(value["claim_kind"], "claim_kind"),
            material_fingerprint=_validate_fingerprint(
                value["material_fingerprint"], "material_fingerprint"
            ),
            relevant_policy_fingerprint=_validate_fingerprint(
                value["relevant_policy_fingerprint"],
                "relevant_policy_fingerprint",
            ),
            proposed_path=_validate_proposed_path(value["proposed_path"]),
            proposed_record=_record(value["proposed_record"], "proposed_record") or {},
            baseline_record=_record(
                value["baseline_record"], "baseline_record", nullable=True
            ),
            blockers=_parse_blockers(value["blockers"]),
        )
        if value["candidate_id"] != candidate.candidate_id:
            raise CurationPrototypeError(
                "Inventory candidate_id does not match its stable identity"
            )
        return candidate


def make_candidate(
    *,
    adapter_id: str,
    source_namespace: str,
    source_record_id: str,
    claim_kind: str,
    material: object,
    relevant_policy: object,
    proposed_path: str,
    proposed_record: Mapping[str, object],
    baseline_record: Mapping[str, object] | None = None,
    blockers: Sequence[str] = (),
) -> Candidate:
    """Build a candidate from adapter-selected material and policy inputs."""

    normalized_path = _validate_proposed_path(proposed_path)
    normalized_record = _record(dict(proposed_record), "proposed_record") or {}
    return Candidate(
        adapter_id=adapter_id,
        source_namespace=source_namespace,
        source_record_id=source_record_id,
        claim_kind=claim_kind,
        material_fingerprint=fingerprint(
            {
                "adapter_material": material,
                "materialized_proposal": {
                    "path": normalized_path,
                    "record": normalized_record,
                },
            }
        ),
        relevant_policy_fingerprint=fingerprint(relevant_policy),
        proposed_path=normalized_path,
        proposed_record=normalized_record,
        baseline_record=baseline_record,
        blockers=tuple(blockers),
    )


@dataclass(frozen=True)
class EvaluationContext:
    """Explicit non-source facts that can satisfy a deferral return condition."""

    as_of: date | None = None
    resolved_policy_questions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    decision_id: str
    claim_revision_id: str
    supersedes_decision_id: str | None
    candidate_id: str
    adapter_id: str
    source_namespace: str
    source_record_id: str
    claim_kind: str
    material_fingerprint: str
    relevant_policy_fingerprint: str
    disposition: str
    reviewer: str
    decided_on: date
    rationale: str
    evidence: tuple[str, ...]
    target_record_id: str | None = None
    return_when: Mapping[str, object] | None = None
    scope: Mapping[str, object] | None = None
    replacement_candidate_id: str | None = None

    @property
    def identity_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.adapter_id,
            self.source_namespace,
            self.source_record_id,
            self.claim_kind,
        )

    def matches(self, candidate: Candidate) -> bool:
        return self.identity_tuple == (
            candidate.adapter_id,
            candidate.source_namespace,
            candidate.source_record_id,
            candidate.claim_kind,
        )


@dataclass(frozen=True)
class DecisionBook:
    decisions: Mapping[str, Decision]
    transactions: Mapping[str, tuple[str, ...]]
    active_decisions: Mapping[str, str] = field(default_factory=dict)

    def revisions(self, candidate_id: str) -> tuple[Decision, ...]:
        active_id = self.active_decisions.get(candidate_id)
        if active_id is None:
            return ()
        reversed_history: list[Decision] = []
        current = self.decisions[active_id]
        while True:
            reversed_history.append(current)
            if current.supersedes_decision_id is None:
                break
            current = self.decisions[current.supersedes_decision_id]
        return tuple(reversed(reversed_history))

    def active(self, candidate_id: str) -> Decision | None:
        active_id = self.active_decisions.get(candidate_id)
        return None if active_id is None else self.decisions[active_id]

    def active_values(self) -> tuple[Decision, ...]:
        return tuple(
            self.decisions[identifier]
            for _, identifier in sorted(self.active_decisions.items())
        )

    def exact(self, candidate: Candidate) -> Decision | None:
        active = self.active(candidate.candidate_id)
        if active is None:
            return None
        identifier = claim_revision_identity(
            candidate.candidate_id,
            candidate.material_fingerprint,
            candidate.relevant_policy_fingerprint,
        )
        return active if active.claim_revision_id == identifier else None


def _parse_date(value: object, label: str) -> date:
    rendered = _require_nonempty(value, label)
    try:
        result = date.fromisoformat(rendered)
    except ValueError as error:
        raise CurationPrototypeError(f"{label} must be an ISO calendar date") from error
    if result.isoformat() != rendered:
        raise CurationPrototypeError(f"{label} must be an ISO calendar date")
    return result


def _parse_evidence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CurationPrototypeError("decision evidence must be a non-empty list")
    evidence = tuple(
        _require_nonempty(item, "decision evidence entry") for item in value
    )
    if len(evidence) != len(set(evidence)):
        raise CurationPrototypeError("decision evidence entries must be unique")
    return evidence


def _parse_return_when(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CurationPrototypeError("defer return_when must be a mapping")
    kind = value.get("kind")
    if kind in {"material-change", "relevant-policy-change"}:
        if set(value) != {"kind"}:
            raise CurationPrototypeError(
                f"defer {kind} return_when has unexpected fields"
            )
        return {"kind": kind}
    if kind == "on-or-after":
        if set(value) != {"kind", "date"}:
            raise CurationPrototypeError(
                "defer on-or-after return_when requires only date"
            )
        return {
            "kind": kind,
            "date": _parse_date(value["date"], "return date").isoformat(),
        }
    if kind == "policy-question-resolved":
        if set(value) != {"kind", "question"}:
            raise CurationPrototypeError(
                "defer policy-question-resolved requires only question"
            )
        return {
            "kind": kind,
            "question": _require_nonempty(value["question"], "policy question"),
        }
    raise CurationPrototypeError("defer return_when kind is unsupported")


def _parse_scope(value: object, decision: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CurationPrototypeError("permanent-exclude scope must be a mapping")
    fields = {
        "adapter_id",
        "source_namespace",
        "source_record_id",
        "claim_kind",
        "material_changes",
        "relevant_policy_changes",
    }
    if set(value) != fields:
        raise CurationPrototypeError(
            "permanent-exclude scope has missing or unexpected fields"
        )
    result: dict[str, object] = {}
    for name in ("adapter_id", "source_namespace", "source_record_id", "claim_kind"):
        result[name] = _require_nonempty(value[name], f"scope.{name}")
    for name in ("material_changes", "relevant_policy_changes"):
        if not isinstance(value[name], bool):
            raise CurationPrototypeError(f"scope.{name} must be boolean")
        result[name] = value[name]
    if result["adapter_id"] not in {decision["adapter_id"], "*"}:
        raise CurationPrototypeError("permanent-exclude scope excludes its decision")
    if result["source_namespace"] not in {decision["source_namespace"], "*"}:
        raise CurationPrototypeError("permanent-exclude scope excludes its decision")
    if result["source_record_id"] not in {decision["source_record_id"], "*"}:
        raise CurationPrototypeError("permanent-exclude scope excludes its decision")
    if result["claim_kind"] not in {decision["claim_kind"], "*"}:
        raise CurationPrototypeError("permanent-exclude scope excludes its decision")
    if result["material_changes"] is not True:
        raise CurationPrototypeError(
            "permanent-exclude scope must explicitly include material changes"
        )
    return result


def _parse_decision(value: object) -> Decision:
    if not isinstance(value, dict):
        raise CurationPrototypeError("Every decision must be a mapping")
    base_fields = {
        "decision_id",
        "claim_revision_id",
        "supersedes_decision_id",
        "candidate_id",
        "adapter_id",
        "source_namespace",
        "source_record_id",
        "claim_kind",
        "material_fingerprint",
        "relevant_policy_fingerprint",
        "disposition",
        "reviewer",
        "decided_on",
        "rationale",
        "evidence",
    }
    disposition = value.get("disposition")
    conditional = {
        "link": {"target_record_id"},
        "defer": {"return_when"},
        "permanent-exclude": {"scope"},
        "supersede": {"replacement_candidate_id"},
    }.get(disposition, set())
    if disposition not in DISPOSITIONS:
        raise CurationPrototypeError(f"Unsupported disposition {disposition!r}")
    if set(value) != base_fields | conditional:
        raise CurationPrototypeError(
            f"{disposition} decision has missing or unexpected fields"
        )

    adapter_id = _require_nonempty(value["adapter_id"], "decision adapter_id")
    namespace = _require_nonempty(
        value["source_namespace"], "decision source_namespace"
    )
    record_id = _require_nonempty(
        value["source_record_id"], "decision source_record_id"
    )
    claim_kind = _require_nonempty(value["claim_kind"], "decision claim_kind")
    expected_id = candidate_identity(adapter_id, namespace, record_id, claim_kind)
    candidate_id = _require_nonempty(value["candidate_id"], "decision candidate_id")
    if candidate_id != expected_id:
        raise CurationPrototypeError(
            "Decision candidate_id does not match its stable identity"
        )
    material_fingerprint = _validate_fingerprint(
        value["material_fingerprint"], "decision material_fingerprint"
    )
    relevant_policy_fingerprint = _validate_fingerprint(
        value["relevant_policy_fingerprint"],
        "decision relevant_policy_fingerprint",
    )
    expected_claim_revision_id = claim_revision_identity(
        candidate_id, material_fingerprint, relevant_policy_fingerprint
    )
    claim_revision_id = _validate_prefixed_identity(
        value["claim_revision_id"],
        CLAIM_REVISION_PREFIX,
        "decision claim_revision_id",
    )
    if claim_revision_id != expected_claim_revision_id:
        raise CurationPrototypeError(
            "Decision claim_revision_id does not match its exact claim revision"
        )
    supersedes_decision_id = value["supersedes_decision_id"]
    if supersedes_decision_id is not None:
        supersedes_decision_id = _validate_prefixed_identity(
            supersedes_decision_id,
            DECISION_PREFIX,
            "decision supersedes_decision_id",
        )

    reviewer = _require_nonempty(value["reviewer"], "decision reviewer")
    decided_on = _parse_date(value["decided_on"], "decision decided_on")
    rationale = _require_nonempty(value["rationale"], "decision rationale")
    evidence = _parse_evidence(value["evidence"])

    target_record_id = None
    return_when = None
    scope = None
    replacement_candidate_id = None
    if disposition == "link":
        target_record_id = _require_nonempty(
            value["target_record_id"], "link target_record_id"
        )
    elif disposition == "defer":
        return_when = _parse_return_when(value["return_when"])
    elif disposition == "permanent-exclude":
        scope = _parse_scope(value["scope"], value)
    elif disposition == "supersede":
        replacement_candidate_id = _require_nonempty(
            value["replacement_candidate_id"], "replacement_candidate_id"
        )
        if replacement_candidate_id == candidate_id:
            raise CurationPrototypeError("A candidate cannot supersede itself")

    expected_decision_id = decision_identity(
        claim_revision_id=claim_revision_id,
        supersedes_decision_id=supersedes_decision_id,
        disposition=disposition,
        reviewer=reviewer,
        decided_on=decided_on.isoformat(),
        rationale=rationale,
        evidence=evidence,
        target_record_id=target_record_id,
        return_when=return_when,
        scope=scope,
        replacement_candidate_id=replacement_candidate_id,
    )
    decision_id = _validate_prefixed_identity(
        value["decision_id"], DECISION_PREFIX, "decision decision_id"
    )
    if decision_id != expected_decision_id:
        raise CurationPrototypeError(
            "Decision decision_id does not match its durable review event"
        )
    if supersedes_decision_id == decision_id:
        raise CurationPrototypeError("A decision revision cannot supersede itself")

    return Decision(
        decision_id=decision_id,
        claim_revision_id=claim_revision_id,
        supersedes_decision_id=supersedes_decision_id,
        candidate_id=candidate_id,
        adapter_id=adapter_id,
        source_namespace=namespace,
        source_record_id=record_id,
        claim_kind=claim_kind,
        material_fingerprint=material_fingerprint,
        relevant_policy_fingerprint=relevant_policy_fingerprint,
        disposition=disposition,
        reviewer=reviewer,
        decided_on=decided_on,
        rationale=rationale,
        evidence=evidence,
        target_record_id=target_record_id,
        return_when=return_when,
        scope=scope,
        replacement_candidate_id=replacement_candidate_id,
    )


def _scope_matches(scope: Mapping[str, object], decision: Decision) -> bool:
    values = (
        ("adapter_id", decision.adapter_id),
        ("source_namespace", decision.source_namespace),
        ("source_record_id", decision.source_record_id),
        ("claim_kind", decision.claim_kind),
    )
    return all(scope[name] in {actual, "*"} for name, actual in values)


def _validate_decision_relations(decisions: Mapping[str, Decision]) -> None:
    permanent = [
        decision
        for decision in decisions.values()
        if decision.disposition == "permanent-exclude"
    ]
    for exclusion in permanent:
        assert exclusion.scope is not None
        for other in decisions.values():
            if other.decision_id == exclusion.decision_id:
                continue
            relevant_policy_in_scope = (
                exclusion.scope["relevant_policy_changes"] is True
                or exclusion.relevant_policy_fingerprint
                == other.relevant_policy_fingerprint
            )
            if (
                _scope_matches(exclusion.scope, other)
                and relevant_policy_in_scope
                and other.disposition != "permanent-exclude"
            ):
                raise CurationPrototypeError(
                    "Contradictory decisions overlap a permanent-exclude scope"
                )

    edges: dict[str, set[str]] = {}
    for decision in decisions.values():
        if decision.disposition == "supersede":
            assert decision.replacement_candidate_id is not None
            edges.setdefault(decision.candidate_id, set()).add(
                decision.replacement_candidate_id
            )

    def visit(candidate_id: str, active: set[str], complete: set[str]) -> None:
        if candidate_id in active:
            raise CurationPrototypeError("Contradictory supersede cycle")
        if candidate_id in complete:
            return
        active.add(candidate_id)
        for replacement in edges.get(candidate_id, set()):
            visit(replacement, active, complete)
        active.remove(candidate_id)
        complete.add(candidate_id)

    complete: set[str] = set()
    for candidate_id in sorted(edges):
        visit(candidate_id, set(), complete)


def _validate_decision_history(
    decisions: Mapping[str, Decision],
) -> dict[str, str]:
    grouped: dict[str, dict[str, Decision]] = {}
    for identifier, decision in decisions.items():
        grouped.setdefault(decision.candidate_id, {})[identifier] = decision

    active: dict[str, str] = {}
    for candidate_id, revisions in grouped.items():
        children: dict[str, str] = {}
        roots: list[str] = []
        for identifier, revision in revisions.items():
            previous = revision.supersedes_decision_id
            if previous is None:
                roots.append(identifier)
                continue
            prior = revisions.get(previous)
            if prior is None:
                raise CurationPrototypeError(
                    f"Decision history for {candidate_id} references another identity or a missing revision"
                )
            if previous in children:
                raise CurationPrototypeError(
                    f"Decision history for {candidate_id} branches at {previous}"
                )
            if revision.decided_on < prior.decided_on:
                raise CurationPrototypeError(
                    f"Decision history date moves backward for {candidate_id}"
                )
            children[previous] = identifier
        if len(roots) != 1:
            raise CurationPrototypeError(
                f"Decision history for {candidate_id} must have one root"
            )
        visited: set[str] = set()
        current = roots[0]
        while True:
            if current in visited:
                raise CurationPrototypeError(
                    f"Decision history cycle for {candidate_id}"
                )
            visited.add(current)
            next_revision = children.get(current)
            if next_revision is None:
                break
            current = next_revision
        if visited != set(revisions):
            raise CurationPrototypeError(
                f"Decision history for {candidate_id} is disconnected or cyclic"
            )
        active[candidate_id] = current
    return active


def parse_decisions(text: str) -> DecisionBook:
    """Parse and strictly validate prototype-v1 durable decision YAML."""

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CurationPrototypeError(f"Malformed decision YAML: {error}") from error
    if not isinstance(document, dict):
        raise CurationPrototypeError("Decision document must be a mapping")
    if set(document) != {"format", "decisions", "transactions"}:
        raise CurationPrototypeError(
            "Decision document has missing or unexpected top-level fields"
        )
    if document["format"] != DECISIONS_FORMAT:
        raise CurationPrototypeError("Decision document format is unsupported")
    if not isinstance(document["decisions"], list):
        raise CurationPrototypeError("Decision document decisions must be a list")
    decisions: dict[str, Decision] = {}
    for raw in document["decisions"]:
        decision = _parse_decision(raw)
        if decision.decision_id in decisions:
            raise CurationPrototypeError(
                f"Duplicate durable decision event {decision.decision_id}"
            )
        decisions[decision.decision_id] = decision
    active_decisions = _validate_decision_history(decisions)
    _validate_decision_relations(
        {identifier: decisions[identifier] for identifier in active_decisions.values()}
    )

    if not isinstance(document["transactions"], list):
        raise CurationPrototypeError("Decision transactions must be a list")
    transactions: dict[str, tuple[str, ...]] = {}
    for raw in document["transactions"]:
        if not isinstance(raw, dict) or set(raw) != {
            "inventory_id",
            "decision_ids",
        }:
            raise CurationPrototypeError(
                "Every decision transaction requires only inventory_id and decision_ids"
            )
        inventory_id = _require_nonempty(
            raw["inventory_id"], "transaction inventory_id"
        )
        if not inventory_id.startswith(INVENTORY_PREFIX):
            raise CurationPrototypeError(
                "transaction inventory_id is not a prototype-v1 inventory id"
            )
        decision_ids = raw["decision_ids"]
        if not isinstance(decision_ids, list):
            raise CurationPrototypeError("transaction decision_ids must be a list")
        normalized = tuple(
            _require_nonempty(item, "transaction decision_id") for item in decision_ids
        )
        if len(normalized) != len(set(normalized)):
            raise CurationPrototypeError(
                f"Duplicate decision in transaction {inventory_id}"
            )
        if inventory_id in transactions:
            raise CurationPrototypeError(
                f"Duplicate decision transaction for {inventory_id}"
            )
        transactions[inventory_id] = normalized

    anchored = {
        decision_id
        for transaction in transactions.values()
        for decision_id in transaction
    }
    unknown = sorted(anchored - set(decisions))
    if unknown:
        raise CurationPrototypeError(
            "Decision transactions reference unknown revisions: " + ", ".join(unknown)
        )
    unbound = sorted(set(decisions) - anchored)
    if unbound:
        raise CurationPrototypeError(
            "Unbound decision revisions have no retained transaction: "
            + ", ".join(unbound)
        )
    return DecisionBook(
        decisions=decisions,
        transactions=transactions,
        active_decisions=active_decisions,
    )


def load_decisions(path: Path) -> DecisionBook:
    try:
        return parse_decisions(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CurationPrototypeError(
            f"Cannot read decisions {path}: {error}"
        ) from error


@dataclass(frozen=True)
class MetadataIndex:
    records_by_id: Mapping[str, tuple[str, ...]]
    records_by_path: Mapping[str, Mapping[str, object]]

    @classmethod
    def from_directory(
        cls, root: Path, *, require_unique_pids: bool = False
    ) -> MetadataIndex:
        records_by_id: dict[str, list[str]] = {}
        records_by_path: dict[str, Mapping[str, object]] = {}
        _reject_symlink_tree(root)
        if not root.exists():
            return cls({}, {})
        for path in sorted(
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file() and candidate.suffix in {".yaml", ".yml"}
        ):
            relative = path.relative_to(root).as_posix()
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as error:
                raise CurationPrototypeError(
                    f"Cannot index metadata record {relative}: {error}"
                ) from error
            if not isinstance(value, dict):
                raise CurationPrototypeError(
                    f"Metadata YAML must be a mapping: {relative}"
                )
            records_by_path[relative] = value
            pid = value.get("pid")
            if isinstance(pid, str) and pid:
                records_by_id.setdefault(pid, []).append(relative)
        duplicate_pids = {
            pid: paths for pid, paths in records_by_id.items() if len(paths) > 1
        }
        if require_unique_pids and duplicate_pids:
            details = "; ".join(
                f"{pid}: {', '.join(paths)}"
                for pid, paths in sorted(duplicate_pids.items())
            )
            raise CurationPrototypeError(f"Duplicate metadata PIDs: {details}")
        return cls(
            records_by_id={
                pid: tuple(paths) for pid, paths in sorted(records_by_id.items())
            },
            records_by_path=records_by_path,
        )

    def require_unique(self, record_id: str) -> str:
        paths = self.records_by_id.get(record_id, ())
        if not paths:
            raise CurationPrototypeError(f"Link target {record_id!r} does not exist")
        if len(paths) != 1:
            raise CurationPrototypeError(
                f"Link target {record_id!r} is ambiguous: {', '.join(paths)}"
            )
        return paths[0]


def _reject_symlink_tree(root: Path) -> None:
    if root.is_symlink():
        raise CurationPrototypeError(f"Metadata root cannot be a symlink: {root}")
    if not root.exists():
        return
    if not root.is_dir():
        raise CurationPrototypeError(f"Metadata root is not a directory: {root}")
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted([*directories, *filenames]):
            path = current_path / name
            if path.is_symlink():
                raise CurationPrototypeError(
                    f"Metadata tree cannot contain symlinks: {path}"
                )


@dataclass(frozen=True)
class Evaluation:
    outcome: str
    reason: str
    prior_disposition: str | None


def _fingerprint_changes(candidate: Candidate, decision: Decision) -> tuple[bool, bool]:
    return (
        candidate.material_fingerprint != decision.material_fingerprint,
        candidate.relevant_policy_fingerprint != decision.relevant_policy_fingerprint,
    )


def _defer_trigger(
    candidate: Candidate,
    decision: Decision,
    context: EvaluationContext,
) -> str | None:
    assert decision.return_when is not None
    material_changed, policy_changed = _fingerprint_changes(candidate, decision)
    kind = decision.return_when["kind"]
    if kind == "material-change" and material_changed:
        return "defer-return-material-change"
    if kind == "relevant-policy-change" and policy_changed:
        return "defer-return-relevant-policy-change"
    if kind == "on-or-after":
        return_date = date.fromisoformat(str(decision.return_when["date"]))
        if context.as_of is not None and context.as_of >= return_date:
            return "defer-return-on-or-after"
    if kind == "policy-question-resolved":
        question = str(decision.return_when["question"])
        if question in context.resolved_policy_questions:
            return "defer-return-policy-question-resolved"
    return None


def _scope_contains_candidate(
    scope: Mapping[str, object], candidate: Candidate
) -> bool:
    values = (
        ("adapter_id", candidate.adapter_id),
        ("source_namespace", candidate.source_namespace),
        ("source_record_id", candidate.source_record_id),
        ("claim_kind", candidate.claim_kind),
    )
    return all(scope[name] in {actual, "*"} for name, actual in values)


def _permanent_applies(decision: Decision, candidate: Candidate) -> bool:
    if decision.disposition != "permanent-exclude":
        return False
    assert decision.scope is not None
    if not _scope_contains_candidate(decision.scope, candidate):
        return False
    material_changed, policy_changed = _fingerprint_changes(candidate, decision)
    if material_changed and decision.scope["material_changes"] is not True:
        return False
    if policy_changed and decision.scope["relevant_policy_changes"] is not True:
        return False
    return True


def evaluate_candidate(
    candidate: Candidate,
    decisions: DecisionBook,
    *,
    context: EvaluationContext | None = None,
    metadata_index: MetadataIndex | None = None,
    all_candidates: Mapping[str, Candidate] | None = None,
) -> Evaluation:
    """Decide whether a source claim is suppressed or must return for review."""

    context = context or EvaluationContext()
    all_candidates = all_candidates or {candidate.candidate_id: candidate}
    exact = decisions.exact(candidate)
    scoped_permanent = [
        decision
        for decision in decisions.active_values()
        if _permanent_applies(decision, candidate)
    ]
    if len(scoped_permanent) > 1:
        raise CurationPrototypeError(
            f"Ambiguous permanent-exclude scopes for {candidate.candidate_id}"
        )
    if (
        exact is not None
        and scoped_permanent
        and exact.decision_id != scoped_permanent[0].decision_id
    ):
        raise CurationPrototypeError(
            f"Contradictory exact and permanent decisions for {candidate.candidate_id}"
        )
    active_revision = decisions.active(candidate.candidate_id)
    decision = exact
    if decision is None and scoped_permanent:
        decision = scoped_permanent[0]
    if decision is None and active_revision is not None:
        decision = active_revision
    if decision is None:
        return Evaluation("review", "no-prior-decision", None)
    if decision.candidate_id == candidate.candidate_id and not decision.matches(
        candidate
    ):
        raise CurationPrototypeError(
            f"Decision identity does not match candidate {candidate.candidate_id}"
        )

    material_changed, policy_changed = _fingerprint_changes(candidate, decision)
    if decision.disposition == "permanent-exclude":
        assert decision.scope is not None
        if not _scope_contains_candidate(decision.scope, candidate):
            raise CurationPrototypeError(
                "permanent-exclude scope does not contain its candidate"
            )
        if material_changed and decision.scope["material_changes"] is not True:
            return Evaluation("review", "stale-material", decision.disposition)
        if policy_changed and decision.scope["relevant_policy_changes"] is not True:
            return Evaluation("review", "stale-relevant-policy", decision.disposition)
        return Evaluation(
            "suppress", "permanent-exclusion-in-scope", decision.disposition
        )

    if decision.disposition == "defer":
        trigger = _defer_trigger(candidate, decision, context)
        if trigger is not None:
            return Evaluation("review", trigger, decision.disposition)
        return Evaluation("suppress", "deferred-condition-unmet", decision.disposition)
    if material_changed:
        return Evaluation("review", "stale-material", decision.disposition)
    if policy_changed:
        return Evaluation("review", "stale-relevant-policy", decision.disposition)

    if decision.disposition == "accept":
        if metadata_index is None:
            return Evaluation(
                "review", "accepted-state-unverified", decision.disposition
            )
        current = metadata_index.records_by_path.get(candidate.proposed_path)
        if current != candidate.proposed_record:
            return Evaluation("review", "accepted-state-drift", decision.disposition)
        return Evaluation("suppress", "accepted-unchanged", decision.disposition)
    if decision.disposition == "reject":
        return Evaluation("suppress", "rejected-unchanged", decision.disposition)
    if decision.disposition == "link":
        if metadata_index is None:
            raise CurationPrototypeError("Link evaluation requires a metadata index")
        assert decision.target_record_id is not None
        metadata_index.require_unique(decision.target_record_id)
        return Evaluation("suppress", "linked-to-existing", decision.disposition)
    if decision.disposition == "supersede":
        replacement_id = decision.replacement_candidate_id
        assert replacement_id is not None
        replacement = all_candidates.get(replacement_id)
        replacement_decision = (
            None if replacement is None else decisions.exact(replacement)
        )
        if replacement is None or replacement_decision is None:
            raise CurationPrototypeError(
                f"Supersede replacement {replacement_id} is not active"
            )
        replacement_changes = _fingerprint_changes(replacement, replacement_decision)
        if replacement_changes != (
            False,
            False,
        ) or replacement_decision.disposition not in {
            "accept",
            "link",
        }:
            raise CurationPrototypeError(
                f"Supersede replacement {replacement_id} is not accepted or linked"
            )
        if replacement_decision.disposition == "link":
            if metadata_index is None:
                raise CurationPrototypeError(
                    "Linked supersede replacement requires a metadata index"
                )
            assert replacement_decision.target_record_id is not None
            metadata_index.require_unique(replacement_decision.target_record_id)
        return Evaluation(
            "suppress", "superseded-by-active-candidate", decision.disposition
        )
    raise AssertionError(f"Unhandled disposition {decision.disposition}")


@dataclass(frozen=True)
class Inventory:
    adapter_id: str
    inventory_id: str
    inputs: Mapping[str, object]
    candidates: tuple[Candidate, ...]
    review: Mapping[str, Mapping[str, object]]

    def to_mapping(self) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for candidate in self.candidates:
            entry = candidate.to_mapping()
            entry["review"] = dict(self.review[candidate.candidate_id])
            entries.append(entry)
        return {
            "format": INVENTORY_FORMAT,
            "adapter_id": self.adapter_id,
            "inputs": dict(self.inputs),
            "inventory_id": self.inventory_id,
            "candidates": entries,
        }


def _inventory_id(document_without_id: Mapping[str, object]) -> str:
    return (
        INVENTORY_PREFIX
        + hashlib.sha256(_canonical_bytes(document_without_id)).hexdigest()
    )


def _normalize_evaluation_context(
    context: EvaluationContext | None,
) -> tuple[EvaluationContext, dict[str, object]]:
    normalized = context or EvaluationContext()
    if normalized.as_of is not None and type(normalized.as_of) is not date:
        raise CurationPrototypeError("evaluation context as_of must be a date or null")
    if not isinstance(normalized.resolved_policy_questions, frozenset):
        raise CurationPrototypeError(
            "evaluation context resolved_policy_questions must be a frozenset"
        )
    questions = sorted(
        _require_nonempty(question, "resolved policy question")
        for question in normalized.resolved_policy_questions
    )
    return normalized, {
        "as_of": None if normalized.as_of is None else normalized.as_of.isoformat(),
        "resolved_policy_questions": questions,
    }


def build_inventory(
    adapter_id: str,
    candidates: Iterable[Candidate],
    decisions: DecisionBook | None = None,
    *,
    context: EvaluationContext | None = None,
    metadata_dir: Path | None = None,
    inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the deterministic inventory of claims that currently need review."""

    adapter_id = _require_nonempty(adapter_id, "inventory adapter_id")
    ordered = sorted(candidates, key=lambda item: item.candidate_id)
    by_id: dict[str, Candidate] = {}
    for candidate in ordered:
        if candidate.adapter_id != adapter_id:
            raise CurationPrototypeError(
                "Inventory cannot mix candidates from different adapters"
            )
        if candidate.candidate_id in by_id:
            raise CurationPrototypeError(
                f"Duplicate candidate {candidate.candidate_id}"
            )
        by_id[candidate.candidate_id] = candidate
    decisions = decisions or DecisionBook({}, {})
    raw_inputs = dict(inputs or {})
    if EVALUATION_CONTEXT_INPUT in raw_inputs:
        raise CurationPrototypeError(
            f"inventory inputs reserve {EVALUATION_CONTEXT_INPUT!r} for the core"
        )
    evaluation_context, serialized_context = _normalize_evaluation_context(context)
    raw_inputs[EVALUATION_CONTEXT_INPUT] = serialized_context
    normalized_inputs = _normalize_inputs(raw_inputs)
    assert isinstance(normalized_inputs, dict)
    metadata_index = (
        MetadataIndex.from_directory(metadata_dir) if metadata_dir is not None else None
    )
    entries: list[dict[str, object]] = []
    for candidate in ordered:
        evaluation = evaluate_candidate(
            candidate,
            decisions,
            context=evaluation_context,
            metadata_index=metadata_index,
            all_candidates=by_id,
        )
        if evaluation.outcome != "review":
            continue
        entry = candidate.to_mapping()
        entry["review"] = {
            "reason": evaluation.reason,
            "prior_disposition": evaluation.prior_disposition,
        }
        entries.append(entry)
    unsigned = {
        "format": INVENTORY_FORMAT,
        "adapter_id": adapter_id,
        "inputs": normalized_inputs,
        "candidates": entries,
    }
    return {
        "format": INVENTORY_FORMAT,
        "adapter_id": adapter_id,
        "inputs": normalized_inputs,
        "inventory_id": _inventory_id(unsigned),
        "candidates": entries,
    }


def parse_inventory(value: object) -> Inventory:
    """Validate an in-memory or YAML prototype-v1 candidate inventory."""

    if isinstance(value, str):
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError as error:
            raise CurationPrototypeError(
                f"Malformed inventory YAML: {error}"
            ) from error
    if not isinstance(value, dict) or set(value) != {
        "format",
        "adapter_id",
        "inputs",
        "inventory_id",
        "candidates",
    }:
        raise CurationPrototypeError(
            "Inventory has missing or unexpected top-level fields"
        )
    if value["format"] != INVENTORY_FORMAT:
        raise CurationPrototypeError("Inventory format is unsupported")
    adapter_id = _require_nonempty(value["adapter_id"], "inventory adapter_id")
    normalized_inputs = _normalize_inputs(value["inputs"])
    if not isinstance(normalized_inputs, dict):
        raise CurationPrototypeError("Inventory inputs must be a mapping")
    if not isinstance(value["candidates"], list):
        raise CurationPrototypeError("Inventory candidates must be a list")

    candidates: list[Candidate] = []
    reviews: dict[str, Mapping[str, object]] = {}
    seen: set[str] = set()
    for raw in value["candidates"]:
        if not isinstance(raw, dict) or "review" not in raw:
            raise CurationPrototypeError(
                "Every inventory candidate requires review context"
            )
        raw_candidate = dict(raw)
        review = raw_candidate.pop("review")
        if not isinstance(review, dict) or set(review) != {
            "reason",
            "prior_disposition",
        }:
            raise CurationPrototypeError(
                "Inventory review context has missing or unexpected fields"
            )
        _require_nonempty(review["reason"], "inventory review reason")
        prior = review["prior_disposition"]
        if prior is not None and prior not in DISPOSITIONS:
            raise CurationPrototypeError("Inventory prior_disposition is unsupported")
        candidate = Candidate.from_mapping(raw_candidate)
        if candidate.adapter_id != adapter_id:
            raise CurationPrototypeError(
                "Inventory candidate adapter does not match inventory adapter"
            )
        if candidate.candidate_id in seen:
            raise CurationPrototypeError(
                f"Duplicate inventory candidate {candidate.candidate_id}"
            )
        seen.add(candidate.candidate_id)
        candidates.append(candidate)
        reviews[candidate.candidate_id] = review
    if [item.candidate_id for item in candidates] != sorted(seen):
        raise CurationPrototypeError(
            "Inventory candidates are not deterministically sorted"
        )

    unsigned = {
        "format": INVENTORY_FORMAT,
        "adapter_id": adapter_id,
        "inputs": normalized_inputs,
        "candidates": value["candidates"],
    }
    expected_id = _inventory_id(unsigned)
    if value["inventory_id"] != expected_id:
        raise CurationPrototypeError("Inventory id does not match its content")
    return Inventory(
        adapter_id=adapter_id,
        inventory_id=expected_id,
        inputs=normalized_inputs,
        candidates=tuple(candidates),
        review=reviews,
    )


def load_inventory(path: Path) -> Inventory:
    try:
        return parse_inventory(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CurationPrototypeError(
            f"Cannot read inventory {path}: {error}"
        ) from error


def _dump_yaml(value: object) -> str:
    return yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )


def write_inventory(
    path: Path,
    inventory: Mapping[str, object],
    *,
    decisions_path: Path | None = None,
) -> None:
    """Atomically write only proposal state, never durable decision state."""

    parse_inventory(dict(inventory))
    path = path.resolve()
    if decisions_path is not None and path == decisions_path.resolve():
        raise CurationPrototypeError(
            "Proposal inventory path cannot be the durable decisions path"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_dump_yaml(inventory))
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_current_transaction(
    inventory: Inventory, decisions: DecisionBook
) -> dict[str, Decision]:
    transaction = decisions.transactions.get(inventory.inventory_id)
    if transaction is None:
        raise CurationPrototypeError(
            f"Missing decision transaction for {inventory.inventory_id}"
        )
    expected_by_candidate = {
        candidate.candidate_id: claim_revision_identity(
            candidate.candidate_id,
            candidate.material_fingerprint,
            candidate.relevant_policy_fingerprint,
        )
        for candidate in inventory.candidates
    }
    try:
        supplied = [decisions.decisions[identifier] for identifier in transaction]
    except KeyError as error:
        raise CurationPrototypeError(
            f"Transaction references unknown decision event {error.args[0]}"
        ) from error
    current: dict[str, Decision] = {}
    selected_ids: set[str] = set()
    missing: list[str] = []
    for candidate in inventory.candidates:
        claim_revision_id = expected_by_candidate[candidate.candidate_id]
        exact_events = [
            decision
            for decision in supplied
            if decision.candidate_id == candidate.candidate_id
            and decision.claim_revision_id == claim_revision_id
        ]
        if not exact_events:
            missing.append(claim_revision_id)
            continue
        if len(exact_events) != 1:
            raise CurationPrototypeError(
                "Ambiguous transaction decision events for exact claim revision "
                f"{claim_revision_id}"
            )
        decision = exact_events[0]
        current[candidate.candidate_id] = decision
        selected_ids.add(decision.decision_id)
    if missing:
        raise CurationPrototypeError(
            "Missing transaction decision revisions: " + ", ".join(missing)
        )
    unexpected = sorted(set(transaction) - selected_ids)
    if unexpected:
        raise CurationPrototypeError(
            "Unexpected transaction decision revisions: " + ", ".join(unexpected)
        )
    for candidate in inventory.candidates:
        decision = current[candidate.candidate_id]
        active_decision = decisions.active(candidate.candidate_id)
        if (
            active_decision is None
            or active_decision.decision_id != decision.decision_id
        ):
            raise CurationPrototypeError(
                f"Transaction decision is historical and inactive for {candidate.candidate_id}"
            )
        if not decision.matches(candidate):
            raise CurationPrototypeError(
                f"Transaction decision identity is stale for {candidate.candidate_id}"
            )
        if (
            decision.material_fingerprint != candidate.material_fingerprint
            or decision.relevant_policy_fingerprint
            != candidate.relevant_policy_fingerprint
        ):
            raise CurationPrototypeError(
                f"Transaction decision fingerprints are stale for {candidate.candidate_id}"
            )
    return current


def _validate_current_relations(
    inventory: Inventory,
    current: Mapping[str, Decision],
    metadata_index: MetadataIndex,
) -> None:
    candidates = {item.candidate_id: item for item in inventory.candidates}
    paths: dict[str, str] = {}
    for candidate in inventory.candidates:
        previous = paths.get(candidate.proposed_path)
        if previous is not None:
            raise CurationPrototypeError(
                "Transaction candidates conflict at proposed path "
                f"{candidate.proposed_path}: {previous}, {candidate.candidate_id}"
            )
        paths[candidate.proposed_path] = candidate.candidate_id

        decision = current[candidate.candidate_id]
        path_present = candidate.proposed_path in metadata_index.records_by_path
        path_record = metadata_index.records_by_path.get(candidate.proposed_path)
        baseline_matches = (
            not path_present
            if candidate.baseline_record is None
            else path_present and path_record == candidate.baseline_record
        )
        already_accepted = (
            decision.disposition == "accept"
            and path_present
            and path_record == candidate.proposed_record
        )
        if not baseline_matches and not already_accepted:
            raise CurationPrototypeError(
                "Stale candidate baseline at "
                f"{candidate.proposed_path} for {candidate.candidate_id}"
            )
        if decision.disposition == "accept" and candidate.blockers:
            raise CurationPrototypeError(
                f"Blocked candidate {candidate.candidate_id} cannot be accepted: "
                + ", ".join(candidate.blockers)
            )
        if decision.disposition == "link":
            assert decision.target_record_id is not None
            metadata_index.require_unique(decision.target_record_id)
        elif decision.disposition == "permanent-exclude":
            assert decision.scope is not None
            if not _scope_contains_candidate(decision.scope, candidate):
                raise CurationPrototypeError(
                    "permanent-exclude scope does not contain its current candidate"
                )
        elif decision.disposition == "supersede":
            replacement_id = decision.replacement_candidate_id
            assert replacement_id is not None
            replacement = candidates.get(replacement_id)
            replacement_decision = current.get(replacement_id)
            if replacement is None or replacement_decision is None:
                raise CurationPrototypeError(
                    f"Supersede replacement {replacement_id} is not in the transaction"
                )
            if replacement_decision.disposition not in {"accept", "link"}:
                raise CurationPrototypeError(
                    f"Supersede replacement {replacement_id} is not accepted or linked"
                )
            if replacement_decision.disposition == "link":
                assert replacement_decision.target_record_id is not None
                metadata_index.require_unique(replacement_decision.target_record_id)


def _tree_digest(root: Path) -> str:
    _reject_symlink_tree(root)
    entries: list[dict[str, str]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    return fingerprint(entries)


def metadata_tree_digest(records_dir: Path) -> str:
    """Return the exact canonical-tree digest used by reconciliation reports."""

    return _tree_digest(records_dir.absolute())


def _write_record(root: Path, relative: str, value: Mapping[str, object]) -> None:
    destination = root / PurePosixPath(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_dump_yaml(dict(value)), encoding="utf-8")


FaultHook = Callable[[str], None]
StageValidator = Callable[[Path], None]
CommitGuard = Callable[[Mapping[str, object]], None]


def _reconciliation_lock_path(records_dir: Path) -> Path:
    records_dir = records_dir.absolute()
    suffix = hashlib.sha256(records_dir.name.encode("utf-8")).hexdigest()[:16]
    return records_dir.parent / f"{LOCK_PREFIX}{suffix}.json"


def _read_reconciliation_lock(lock_path: Path) -> dict[str, object]:
    if lock_path.is_symlink():
        raise CurationPrototypeError(
            f"Reconciliation lock cannot be a symlink: {lock_path}"
        )
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CurationPrototypeError(
            f"Reconciliation lock is malformed and requires manual recovery: {lock_path}"
        ) from error
    if not isinstance(value, dict) or set(value) != {
        "format",
        "pid",
        "records_dir",
        "token",
    }:
        raise CurationPrototypeError(
            f"Reconciliation lock is malformed and requires manual recovery: {lock_path}"
        )
    if value["format"] != LOCK_FORMAT:
        raise CurationPrototypeError(
            f"Reconciliation lock format is unsupported: {lock_path}"
        )
    if (
        not isinstance(value["pid"], int)
        or isinstance(value["pid"], bool)
        or value["pid"] <= 0
    ):
        raise CurationPrototypeError(
            f"Reconciliation lock has an invalid pid: {lock_path}"
        )
    _require_nonempty(value["records_dir"], "lock records_dir")
    _require_nonempty(value["token"], "lock token")
    return value


def _acquire_reconciliation_lock(records_dir: Path) -> tuple[Path, str]:
    records_dir = records_dir.absolute()
    records_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _reconciliation_lock_path(records_dir)
    token = secrets.token_hex(16)
    value = {
        "format": LOCK_FORMAT,
        "pid": os.getpid(),
        "records_dir": str(records_dir),
        "token": token,
    }
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        try:
            owner = _read_reconciliation_lock(lock_path)
            detail = f"pid {owner['pid']}"
        except CurationPrototypeError:
            detail = "unreadable owner metadata"
        raise CurationPrototypeError(
            "Reconciliation lock already exists; recovery is locked "
            f"({detail}): {lock_path}. Refuse concurrent work; call "
            "recover_stale_lock(records_dir) only after verifying its owner is gone."
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise
    return lock_path, token


def _release_reconciliation_lock(
    records_dir: Path, lock_path: Path, token: str
) -> None:
    value = _read_reconciliation_lock(lock_path)
    if value["records_dir"] != str(records_dir.absolute()) or value["token"] != token:
        raise CurationPrototypeError(
            f"Reconciliation lock ownership changed; refusing to remove {lock_path}"
        )
    lock_path.unlink()


@contextmanager
def canonical_transaction_guard(records_dir: Path) -> Iterator[None]:
    """Hold the prototype's exclusive canonical-metadata transaction lock."""

    records_dir = records_dir.absolute()
    lock_path, token = _acquire_reconciliation_lock(records_dir)
    try:
        yield
    finally:
        _release_reconciliation_lock(records_dir, lock_path, token)


def recover_stale_lock(
    records_dir: Path,
    *,
    before_commit: CommitGuard | None = None,
    after_commit: CommitGuard | None = None,
) -> dict[str, object]:
    """Remove one well-formed lock only after its recorded process has exited."""

    records_dir = records_dir.absolute()
    lock_path = _reconciliation_lock_path(records_dir)
    if not lock_path.exists() and not lock_path.is_symlink():
        raise CurationPrototypeError("No reconciliation lock exists to recover")
    value = _read_reconciliation_lock(lock_path)
    if value["records_dir"] != str(records_dir):
        raise CurationPrototypeError(
            "Reconciliation lock names a different canonical metadata root"
        )
    if os.name != "posix":
        raise CurationPrototypeError(
            "Automatic stale-lock recovery is only supported on POSIX; "
            "manual recovery is required on this platform"
        )
    pid = int(value["pid"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise CurationPrototypeError(
            f"Reconciliation lock owner pid {pid} cannot be proven stale"
        ) from error
    else:
        raise CurationPrototypeError(
            f"Reconciliation lock owner pid {pid} is still running"
        )
    planned_report: Mapping[str, object] = _ReadOnlyJsonMapping(
        {
            "format": RECONCILIATION_FORMAT,
            "recovery": "removed-stale-reconciliation-lock",
            "records_dir": records_dir.name,
            "artifact_removed": lock_path.name,
        }
    )
    _run_commit_guard(before_commit, planned_report)
    # Re-read immediately before deletion so a replacement lock is never removed.
    if _read_reconciliation_lock(lock_path) != value:
        raise CurationPrototypeError(
            "Reconciliation lock changed during stale-lock recovery"
        )
    lock_path.unlink()
    _run_after_commit(after_commit, planned_report)
    return dict(planned_report)


def _recovery_artifacts(records_dir: Path) -> tuple[Path, ...]:
    records_dir = records_dir.absolute()
    return tuple(
        sorted(
            path
            for path in records_dir.parent.glob(f"{STAGE_PREFIX}*")
            if path.exists() or path.is_symlink()
        )
    )


def _fail_on_recovery_backup(records_dir: Path) -> None:
    artifacts = _recovery_artifacts(records_dir)
    if artifacts:
        raise CurationPrototypeError(
            "Interrupted reconciliation requires recover_interrupted() before rerun: "
            + ", ".join(str(path) for path in artifacts)
        )


def _artifact_base(path: Path) -> str:
    for suffix in (
        "-backup-failed-install",
        "-recovery-displaced",
        "-backup",
    ):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.name


def _remove_recovery_artifact(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _recover_interrupted_locked(
    records_dir: Path,
    *,
    before_commit: CommitGuard | None,
    after_commit: CommitGuard | None,
) -> dict[str, object]:
    """Restore pre-transaction authority after one interrupted tree swap.

    A backup is always the pre-transaction authority.  A lone stage can be
    discarded only while the untouched canonical tree still exists.  Ambiguous
    groups and states without either authority fail closed.
    """

    records_dir = records_dir.absolute()
    artifacts = _recovery_artifacts(records_dir)
    groups = {_artifact_base(path) for path in artifacts}
    if len(groups) != 1:
        raise CurationPrototypeError(
            "Recovery requires exactly one interrupted reconciliation group"
        )
    base = next(iter(groups))
    stage = records_dir.parent / base
    backup = records_dir.parent / f"{base}-backup"
    failed_install = records_dir.parent / f"{base}-backup-failed-install"
    displaced = records_dir.parent / f"{base}-recovery-displaced"
    recognized = {stage, backup, failed_install, displaced}
    if set(artifacts) - recognized:
        raise CurationPrototypeError(
            "Recovery found unrecognized reconciliation artifacts"
        )

    before_digest = _tree_digest(records_dir)
    removed: list[str]
    backup_authority = False
    if (
        backup.exists()
        and not backup.is_symlink()
        and stage.exists()
        and not stage.is_symlink()
    ):
        MetadataIndex.from_directory(backup, require_unique_pids=True)
        if records_dir.is_symlink():
            raise CurationPrototypeError(
                "Recovery refuses a symlinked canonical metadata root"
            )
        if records_dir.exists() and (displaced.exists() or displaced.is_symlink()):
            raise CurationPrototypeError("Recovery displacement path already exists")
        removed = [
            artifact.name
            for artifact in (failed_install, displaced)
            if artifact.exists() or artifact.is_symlink()
        ]
        if records_dir.exists():
            removed.append(displaced.name)
        removed.append(stage.name)
        after_digest = _tree_digest(backup)
        recovery = "restored-pre-transaction-backup"
        backup_authority = True
    elif (
        stage.exists()
        and not stage.is_symlink()
        and records_dir.exists()
        and not (backup.exists() or backup.is_symlink())
    ):
        MetadataIndex.from_directory(records_dir, require_unique_pids=True)
        removed = [
            artifact.name
            for artifact in (failed_install, displaced)
            if artifact.exists() or artifact.is_symlink()
        ]
        removed.append(stage.name)
        after_digest = before_digest
        recovery = "discarded-never-activated-stage"
    else:
        raise CurationPrototypeError(
            "Recovery has no unambiguous pre-transaction metadata authority"
        )

    planned_report: Mapping[str, object] = _ReadOnlyJsonMapping(
        {
            "format": RECONCILIATION_FORMAT,
            "recovery": recovery,
            "records_dir": records_dir.name,
            "artifacts_before": [artifact.name for artifact in artifacts],
            "artifacts_removed": removed,
            "before_digest": before_digest,
            "after_digest": after_digest,
            "changed": before_digest != after_digest,
            "tree_digest": after_digest,
        }
    )
    _run_commit_guard(before_commit, planned_report)
    if _recovery_artifacts(records_dir) != artifacts:
        raise CurationPrototypeError(
            "Recovery artifacts changed at the final commit boundary"
        )
    if _tree_digest(records_dir) != before_digest:
        raise CurationPrototypeError(
            "Canonical metadata changed at the recovery commit boundary"
        )

    if backup_authority:
        if _tree_digest(backup) != after_digest:
            raise CurationPrototypeError(
                "Recovery backup changed at the final commit boundary"
            )
        moved_current = False
        try:
            if records_dir.exists():
                os.replace(records_dir, displaced)
                moved_current = True
            os.replace(backup, records_dir)
        except Exception as error:
            if moved_current and displaced.exists() and not records_dir.exists():
                os.replace(displaced, records_dir)
            raise CurationPrototypeError(
                f"Recovery could not restore the pre-transaction backup: {error}"
            ) from error
        for artifact in (failed_install, displaced):
            if artifact.exists() or artifact.is_symlink():
                os.replace(artifact, stage / f"obsolete-{artifact.name}")
        _remove_recovery_artifact(stage)
    else:
        for artifact in (failed_install, displaced):
            if artifact.exists() or artifact.is_symlink():
                os.replace(artifact, stage / f"obsolete-{artifact.name}")
        _remove_recovery_artifact(stage)

    MetadataIndex.from_directory(records_dir, require_unique_pids=True)
    if _tree_digest(records_dir) != after_digest or _recovery_artifacts(records_dir):
        raise CurationPrototypeError(
            "Recovered metadata does not match the planned recovery report"
        )
    _run_after_commit(after_commit, planned_report)
    return dict(planned_report)


def recover_interrupted(
    records_dir: Path,
    *,
    before_commit: CommitGuard | None = None,
    after_commit: CommitGuard | None = None,
) -> dict[str, object]:
    """Recover one interrupted swap while holding the reconciliation lock."""

    records_dir = records_dir.absolute()
    with canonical_transaction_guard(records_dir):
        return _recover_interrupted_locked(
            records_dir,
            before_commit=before_commit,
            after_commit=after_commit,
        )


recover_interrupted_reconciliation = recover_interrupted


def _run_commit_guard(
    before_commit: CommitGuard | None,
    planned_report: Mapping[str, object],
) -> None:
    if before_commit is None:
        return
    try:
        before_commit(planned_report)
    except Exception as guard_error:
        raise CurationPrototypeError(
            f"Final commit guard failed: {guard_error}"
        ) from guard_error


def _run_after_commit(
    after_commit: CommitGuard | None,
    planned_report: Mapping[str, object],
) -> None:
    if after_commit is None:
        return
    try:
        after_commit(planned_report)
    except Exception as callback_error:
        raise CurationPrototypeError(
            f"Post-commit callback failed: {callback_error}"
        ) from callback_error


def _activate_staged_tree(
    records_dir: Path,
    staged_records: Path,
    backup: Path,
    fault_hook: FaultHook | None,
    expected_before_digest: str,
    before_commit: CommitGuard | None,
    planned_report: Mapping[str, object],
) -> None:
    had_original = records_dir.exists()
    backed_up = False
    installed = False
    failed_install = backup.with_name(backup.name + "-failed-install")
    stage_root = staged_records.parent
    obsolete_backup = stage_root / "obsolete-backup"
    obsolete_failed_install = stage_root / "obsolete-failed-install"
    try:
        if fault_hook is not None:
            fault_hook("before-activate")
        if had_original:
            if backup.exists() or backup.is_symlink():
                raise CurationPrototypeError(
                    "Canonical metadata backup path is already occupied"
                )
            os.replace(records_dir, backup)
            backed_up = True
            moved_digest = _tree_digest(backup)
        else:
            moved_digest = _tree_digest(records_dir)
        if moved_digest != expected_before_digest:
            raise CurationPrototypeError(
                "Canonical metadata changed during reconciliation before activation"
            )
        if fault_hook is not None:
            fault_hook("after-backup")
        _run_commit_guard(before_commit, planned_report)
        if had_original:
            if not backup.exists() or backup.is_symlink():
                raise CurationPrototypeError(
                    "Canonical metadata backup disappeared before staged installation"
                )
            if _tree_digest(backup) != expected_before_digest:
                raise CurationPrototypeError(
                    "Canonical metadata backup changed before staged installation"
                )
        expected_after_digest = str(planned_report["after_digest"])
        if _tree_digest(staged_records) != expected_after_digest:
            raise CurationPrototypeError(
                "Staged metadata changed after its planned digest was recorded"
            )
        if records_dir.exists() or records_dir.is_symlink():
            raise CurationPrototypeError(
                "Canonical metadata path was recreated before staged installation"
            )
        os.replace(staged_records, records_dir)
        installed = True
        if fault_hook is not None:
            fault_hook("after-activate")
        if _tree_digest(records_dir) != expected_after_digest:
            raise CurationPrototypeError(
                "Installed metadata does not match its planned digest"
            )
        if backup.exists():
            if fault_hook is not None:
                fault_hook("before-backup-cleanup")
            if _tree_digest(records_dir) != expected_after_digest:
                raise CurationPrototypeError(
                    "Installed metadata does not match its planned digest"
                )
            os.replace(backup, obsolete_backup)
    except Exception as error:
        try:
            if fault_hook is not None:
                fault_hook("before-rollback")
            if installed and records_dir.exists():
                os.replace(records_dir, failed_install)
            if backed_up:
                if not backup.exists() or backup.is_symlink():
                    raise CurationPrototypeError(
                        "Canonical backup is unavailable for rollback"
                    )
                os.replace(backup, records_dir)
            elif not had_original and installed and records_dir.exists():
                os.replace(records_dir, failed_install)
            if failed_install.exists():
                os.replace(failed_install, obsolete_failed_install)
        except Exception as rollback_error:
            raise CurationPrototypeError(
                "Reconciliation failed and rollback also failed: "
                f"{error}; rollback: {rollback_error}; recovery backup: {backup}"
            ) from rollback_error
        if not backed_up and stage_root.exists():
            _remove_recovery_artifact(stage_root)
        raise CurationPrototypeError(
            f"Reconciliation activation failed and was rolled back: {error}"
        ) from error


def _reconcile_inventory_locked(
    inventory: Inventory | Mapping[str, object] | str,
    decisions: DecisionBook,
    records_dir: Path,
    *,
    context: EvaluationContext | None = None,
    fault_hook: FaultHook | None = None,
    validate_staged: StageValidator | None = None,
    before_commit: CommitGuard | None = None,
    after_commit: CommitGuard | None = None,
) -> dict[str, object]:
    del context  # Reserved for a later prototype; decisions bind the exact inventory.
    _fail_on_recovery_backup(records_dir)
    parsed = (
        inventory if isinstance(inventory, Inventory) else parse_inventory(inventory)
    )
    current = _validate_current_transaction(parsed, decisions)
    before_digest = _tree_digest(records_dir)
    metadata_index = MetadataIndex.from_directory(records_dir)
    _validate_current_relations(parsed, current, metadata_index)
    if _tree_digest(records_dir) != before_digest:
        raise CurationPrototypeError(
            "Canonical metadata changed during transaction validation"
        )

    records_dir = records_dir.absolute()
    records_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=records_dir.parent))
    staged_records = stage_root / "records"
    backup = records_dir.parent / (stage_root.name + "-backup")
    outcomes: list[dict[str, str]] = []
    try:
        if records_dir.exists():
            shutil.copytree(records_dir, staged_records, symlinks=True)
            _reject_symlink_tree(staged_records)
        else:
            staged_records.mkdir()
        for candidate in parsed.candidates:
            decision = current[candidate.candidate_id]
            if decision.disposition == "accept":
                if (
                    metadata_index.records_by_path.get(candidate.proposed_path)
                    == candidate.proposed_record
                ):
                    action = "already-accepted"
                else:
                    _write_record(
                        staged_records,
                        candidate.proposed_path,
                        candidate.proposed_record,
                    )
                    action = "write-proposal"
            else:
                action = "leave-canonical-unchanged"
            outcomes.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "disposition": decision.disposition,
                    "metadata_action": action,
                }
            )
        MetadataIndex.from_directory(staged_records, require_unique_pids=True)
        if validate_staged is not None:
            try:
                validate_staged(staged_records)
            except Exception as error:
                raise CurationPrototypeError(
                    f"Staged metadata validation failed: {error}"
                ) from error
        MetadataIndex.from_directory(staged_records, require_unique_pids=True)
        staged_digest = _tree_digest(staged_records)
        if _tree_digest(records_dir) != before_digest:
            raise CurationPrototypeError(
                "Canonical metadata changed during staged validation"
            )
        planned_report: Mapping[str, object] = _ReadOnlyJsonMapping(
            {
                "format": RECONCILIATION_FORMAT,
                "inventory_id": parsed.inventory_id,
                "adapter_id": parsed.adapter_id,
                "before_digest": before_digest,
                "after_digest": staged_digest,
                "changed": before_digest != staged_digest,
                "outcomes": outcomes,
            }
        )
        if staged_digest != before_digest:
            _activate_staged_tree(
                records_dir,
                staged_records,
                backup,
                fault_hook,
                before_digest,
                before_commit,
                planned_report,
            )
        else:
            _run_commit_guard(before_commit, planned_report)
    finally:
        failed_install = backup.with_name(backup.name + "-failed-install")
        recovery_sibling_exists = any(
            path.exists() or path.is_symlink() for path in (backup, failed_install)
        )
        if stage_root.exists() and not recovery_sibling_exists:
            shutil.rmtree(stage_root)

    after_digest = _tree_digest(records_dir)
    if after_digest != planned_report["after_digest"]:
        raise CurationPrototypeError(
            "Canonical metadata changed after the planned reconciliation boundary"
        )
    _run_after_commit(after_commit, planned_report)
    return dict(planned_report)


def reconcile_inventory(
    inventory: Inventory | Mapping[str, object] | str,
    decisions: DecisionBook,
    records_dir: Path,
    *,
    context: EvaluationContext | None = None,
    fault_hook: FaultHook | None = None,
    validate_staged: StageValidator | None = None,
    before_commit: CommitGuard | None = None,
    after_commit: CommitGuard | None = None,
) -> dict[str, object]:
    """Apply one complete decision transaction through an exclusive tree swap.

    ``before_commit`` receives the read-only exact planned report.  For a
    changing transaction it runs after canonical backup and final digest CAS,
    but before staged installation; for a no-op it runs after the final digest
    check.  Any exception before installation rolls the canonical tree back.
    ``after_commit`` receives the same report after final digest verification
    and before the exclusive canonical lock is released.
    """

    records_dir = records_dir.absolute()
    with canonical_transaction_guard(records_dir):
        return _reconcile_inventory_locked(
            inventory,
            decisions,
            records_dir,
            context=context,
            fault_hook=fault_hook,
            validate_staged=validate_staged,
            before_commit=before_commit,
            after_commit=after_commit,
        )
