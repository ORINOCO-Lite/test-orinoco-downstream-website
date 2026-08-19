#!/usr/bin/env python3
"""Render and apply the site-owned GitHub curation review prototype.

The module deliberately performs no network or Git operations.  A hosted
workflow supplies GitHub API responses as JSON and runs this trusted copy of
the helper against a separate pull-request worktree.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from types import MappingProxyType, ModuleType
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import yaml


FORMAT = "orinoco-lite-github-curation-review-prototype-v1"
FINGERPRINT_PREFIX = "sha256:"
MAX_COMMENT_BYTES = 50 * 1024
MAX_CANDIDATES = 500
MAX_REVIEW_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_AUTHORITY_JSON_BYTES = 64 * 1024 * 1024
MAX_ATTESTATION_COMMENTS = 1000
MAX_ATTESTATION_RUNS = 1000
MAX_ATTESTATION_BODY_BYTES = 128 * 1024
COMMAND = "/curation submit"
FINALIZE_COMMAND = "/curation finalize"
BRANCH_PREFIX = "automation/curation/"
REVIEW_LABEL = "curation-review"
PROPOSAL_BOT_ID = 41_898_282
PROPOSAL_BOT_LOGIN = "github-actions[bot]"
ALIAS_PREFIXES = MappingProxyType(
    {
        "dump-research-info": "DRI",
        "zotero": "ZOT",
    }
)
TRUSTED_ROOT = Path(__file__).resolve().parents[3]
_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")
_DECISION_RE = re.compile(r"curation-decision-event-v1:[0-9a-f]{64}\Z")
_LOGIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|[A-Za-z0-9][A-Za-z0-9-]{0,33}\[bot\])"
_ACTOR_RE = re.compile(rf"github-user:([0-9]+)@({_LOGIN})\Z")
_MARKER_START = "<!-- orinoco-lite-curation-review "
_ATTESTATION_START = "<!-- orinoco-lite-curation-attestation-v1 "
_ATTESTATION_RE = re.compile(
    r"<!-- orinoco-lite-curation-attestation-v1 " r"(?P<payload>[A-Za-z0-9_-]+) -->"
)
PROPOSAL_RECEIPT_FORMAT = "orinoco-lite-curation-proposal-receipt-v1"
LEDGER_ATTESTATION_FORMAT = "orinoco-lite-curation-ledger-attestation-v1"
_COMMENT_RE = re.compile(
    r"\A[ \t]*/curation submit[ \t]*\r?\n"
    r"[ \t]*```yaml[ \t]*\r?\n"
    r"(?P<yaml>.*?)\r?\n[ \t]*```[ \t]*\r?\n?\Z",
    re.DOTALL,
)


class CurationGitHubError(RuntimeError):
    """Reject an unsafe, stale, or ambiguous hosted review operation."""


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects duplicate mapping keys."""


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise CurationGitHubError("YAML mapping keys must be scalar") from error
        if duplicate:
            raise CurationGitHubError(f"Duplicate YAML mapping key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class Submission:
    alias: str
    expected_decision: str | None
    disposition: str
    rationale: str
    evidence: tuple[str, ...]
    details: Mapping[str, object]


@dataclass(frozen=True)
class CommentProvenance:
    reviewer: str
    decided_on: date
    comment_url: str
    comment_id: int
    login: str


def _nonempty_line(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurationGitHubError(f"{label} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise CurationGitHubError(f"{label} must be a single line")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CurationGitHubError(f"{label} must be a positive integer")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CurationGitHubError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise CurationGitHubError(f"{label} keys must be strings")
    return value


def _only_fields(value: Mapping[str, object], fields: set[str], label: str) -> None:
    observed = set(value)
    if observed != fields:
        missing = sorted(fields - observed)
        unexpected = sorted(observed - fields)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise CurationGitHubError(f"{label} fields are invalid ({'; '.join(details)})")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CurationGitHubError(
            f"Value is not deterministic JSON: {error}"
        ) from error


def _digest(value: bytes) -> str:
    return FINGERPRINT_PREFIX + hashlib.sha256(value).hexdigest()


def _deep_copy(value: object) -> object:
    return json.loads(_canonical_bytes(value))


def _strict_yaml(text: str, label: str) -> object:
    try:
        tokens = tuple(yaml.scan(text))
        if any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            for token in tokens
        ):
            raise CurationGitHubError(f"{label} may not use YAML anchors or aliases")
        return yaml.load(text, Loader=_StrictSafeLoader)
    except CurationGitHubError:
        raise
    except yaml.YAMLError as error:
        raise CurationGitHubError(f"Malformed {label}: {error}") from error


def _json_value_file(path: Path, label: str) -> object:
    def pairs(items):
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CurationGitHubError(f"Duplicate JSON field {key!r} in {label}")
            result[key] = value
        return result

    try:
        payload = path.read_bytes()
        if len(payload) > MAX_AUTHORITY_JSON_BYTES:
            raise CurationGitHubError(f"{label} exceeds the 64 MiB hosted limit")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except OSError as error:
        raise CurationGitHubError(f"Cannot read {label}: {path}") from error
    except UnicodeError as error:
        raise CurationGitHubError(f"Malformed {label}: expected UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise CurationGitHubError(f"Malformed {label}: {error}") from error
    return value


def _json_file(path: Path, label: str) -> Mapping[str, object]:
    return _mapping(_json_value_file(path, label), label)


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise CurationGitHubError(f"Cannot load trusted helper: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def trusted_modules() -> tuple[ModuleType, ModuleType]:
    """Load the core and CLI beside this trusted script, never from PR data."""

    core = _load_module(
        "orinoco_github_curation_trusted_core_v1",
        TRUSTED_ROOT / "source-adapters/metadata/tools/curation_prototype_v1.py",
    )
    cli = _load_module(
        "orinoco_github_curation_trusted_cli_v1",
        TRUSTED_ROOT / "source-adapters/metadata/tools/curation_cli_prototype_v1.py",
    )
    return core, cli


def _trusted_provider(adapter_id: str, core: ModuleType) -> ModuleType:
    provider = _load_module(
        f"orinoco_github_trusted_{adapter_id.replace('-', '_')}_provider_v1",
        TRUSTED_ROOT / "source-adapters" / adapter_id / "curation_prototype_v1.py",
    )
    adapter = _load_module(
        f"orinoco_github_trusted_{adapter_id.replace('-', '_')}_adapter_v1",
        TRUSTED_ROOT / "source-adapters" / adapter_id / "metadata_adapter.py",
    )
    provider.load_dependencies = lambda _root: (core, adapter)
    if adapter_id == "zotero":
        previous_ingest = sys.modules.get("zotero_ingest")
        ingest = _load_module(
            "zotero_ingest",
            TRUSTED_ROOT / "source-adapters/zotero/tools/zotero_ingest.py",
        )
        try:
            site_export = _load_module(
                "orinoco_github_trusted_zotero_site_export_v1",
                TRUSTED_ROOT / "source-adapters/zotero/tools/zotero_site_export.py",
            )
        finally:
            if previous_ingest is None:
                sys.modules.pop("zotero_ingest", None)
            else:
                sys.modules["zotero_ingest"] = previous_ingest
        adapter.load_tools = lambda _root: (ingest, site_export)
    return provider


def _pointer(parts: Sequence[str]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def semantic_diff(baseline: object, proposed: object) -> list[dict[str, object]]:
    """Return a complete, deterministic structural diff using JSON pointers."""

    changes: list[dict[str, object]] = []

    def compare(before: object, after: object, parts: tuple[str, ...]) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                next_parts = (*parts, key)
                if key not in before:
                    changes.append(
                        {
                            "operation": "add",
                            "path": _pointer(next_parts),
                            "proposed": _deep_copy(after[key]),
                        }
                    )
                elif key not in after:
                    changes.append(
                        {
                            "operation": "remove",
                            "path": _pointer(next_parts),
                            "baseline": _deep_copy(before[key]),
                        }
                    )
                else:
                    compare(before[key], after[key], next_parts)
            return
        if isinstance(before, list) and isinstance(after, list):
            for index in range(max(len(before), len(after))):
                next_parts = (*parts, str(index))
                if index >= len(before):
                    changes.append(
                        {
                            "operation": "add",
                            "path": _pointer(next_parts),
                            "proposed": _deep_copy(after[index]),
                        }
                    )
                elif index >= len(after):
                    changes.append(
                        {
                            "operation": "remove",
                            "path": _pointer(next_parts),
                            "baseline": _deep_copy(before[index]),
                        }
                    )
                else:
                    compare(before[index], after[index], next_parts)
            return
        if type(before) is not type(after) or before != after:
            changes.append(
                {
                    "operation": "replace",
                    "path": _pointer(parts),
                    "baseline": _deep_copy(before),
                    "proposed": _deep_copy(after),
                }
            )

    compare(baseline, proposed, ())
    return changes


def stable_aliases(inventory) -> dict[str, str]:
    try:
        prefix = ALIAS_PREFIXES[inventory.adapter_id]
    except KeyError as error:
        raise CurationGitHubError(
            f"No stable alias prefix for adapter {inventory.adapter_id!r}"
        ) from error
    width = max(3, len(str(len(inventory.candidates))))
    return {
        candidate.candidate_id: f"{prefix}-{index:0{width}d}"
        for index, candidate in enumerate(inventory.candidates, start=1)
    }


def _candidate_entries(
    core: ModuleType, inventory, decisions
) -> list[dict[str, object]]:
    aliases = stable_aliases(inventory)
    entries: list[dict[str, object]] = []
    for candidate in inventory.candidates:
        current = decisions.active(candidate.candidate_id)
        entries.append(
            {
                "alias": aliases[candidate.candidate_id],
                "candidate_id": candidate.candidate_id,
                "claim_revision_id": core.claim_revision_identity(
                    candidate.candidate_id,
                    candidate.material_fingerprint,
                    candidate.relevant_policy_fingerprint,
                ),
                "expected_decision": (None if current is None else current.decision_id),
                "source_namespace": candidate.source_namespace,
                "source_record_id": candidate.source_record_id,
                "claim_kind": candidate.claim_kind,
                "proposed_path": candidate.proposed_path,
                "review": _deep_copy(inventory.review[candidate.candidate_id]),
                "blockers": list(candidate.blockers),
                "baseline_record": (
                    None
                    if candidate.baseline_record is None
                    else _deep_copy(candidate.baseline_record)
                ),
                "proposed_record": _deep_copy(candidate.proposed_record),
                "semantic_diff": semantic_diff(
                    candidate.baseline_record, candidate.proposed_record
                ),
            }
        )
    return entries


def _yaml_block(value: object) -> list[str]:
    rendered = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
        width=1000,
    ).rstrip("\n")
    longest = max((len(run) for run in re.findall(r"`+", rendered)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}yaml", rendered, fence]


def _batch_form(
    inventory_id: object, entries: Sequence[Mapping[str, object]]
) -> list[str]:
    payload = {
        "inventory_id": inventory_id,
        "decisions": [
            {
                "candidate": entry["alias"],
                "expected_decision": entry["expected_decision"],
                "disposition": "REPLACE_ME",
                "rationale": "REPLACE_WITH_ONE_LINE_RATIONALE",
                "evidence": ["REPLACE_WITH_EVIDENCE_URL_OR_REFERENCE"],
                "details": {},
            }
            for entry in entries
        ],
    }
    rendered = yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).rstrip("\n")
    return ["````text", COMMAND, "```yaml", rendered, "```", "````"]


def render_review_markdown(manifest: Mapping[str, object]) -> str:
    """Render all immutable candidate cards recorded by a strict manifest."""

    acknowledgment = _mapping(
        manifest["public_data_acknowledgment"], "public_data_acknowledgment"
    )
    candidates = manifest["candidates"]
    if not isinstance(candidates, list):
        raise CurationGitHubError("Manifest candidates must be a list")
    lines = [
        "# Curation review",
        "",
        "> This pull request intentionally exposes the source proposal for hosted",
        "> review. Do not copy personal data outside the approved review context.",
        "",
        f"Inventory: `{manifest['inventory_id']}`",
        f"Adapter: `{manifest['adapter_id']}`",
        f"Public-data acknowledgment: `{acknowledgment['actor']}` at ",
        f"`{acknowledgment['acknowledged_at']}` ([workflow run]({acknowledgment['run_url']})).",
        "",
        "Copy a batch form into a PR comment, replace every placeholder, and",
        "submit the whole fenced command. Forms contain at most 20 candidates.",
        "",
    ]
    for start in range(0, len(candidates), 20):
        batch = candidates[start : start + 20]
        lines.extend(
            [
                f"## Batch form {start // 20 + 1}",
                "",
                *_batch_form(manifest["inventory_id"], batch),
                "",
            ]
        )
    for entry_value in candidates:
        entry = _mapping(entry_value, "manifest candidate")
        status = (
            "**BLOCKED — `accept` is forbidden until every blocker is resolved.**"
            if entry["blockers"]
            else "Ready for a disposition."
        )
        lines.extend(
            [
                f"## {entry['alias']}",
                "",
                f"Review status: {status}",
                "",
                f"- Candidate: `{entry['candidate_id']}`",
                f"- Claim revision: `{entry['claim_revision_id']}`",
                f"- Expected decision: `{entry['expected_decision']}`",
                f"- Source record: `{entry['source_record_id']}`",
                f"- Claim kind: `{entry['claim_kind']}`",
                f"- Proposed path: `{entry['proposed_path']}`",
                "",
                "### Blockers",
                "",
                *_yaml_block(entry["blockers"]),
                "",
                "### Full semantic diff",
                "",
                *_yaml_block(entry["semantic_diff"]),
                "",
                "### Full baseline record",
                "",
                *_yaml_block(entry["baseline_record"]),
                "",
                "### Full proposed record",
                "",
                *_yaml_block(entry["proposed_record"]),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _normalized_relative(root: Path, path: Path, label: str) -> str:
    root = root.resolve()
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise CurationGitHubError(f"{label} escapes the PR worktree") from error
    pure = PurePosixPath(relative)
    if relative != pure.as_posix() or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise CurationGitHubError(
            f"{label} must be a normalized worktree-relative path"
        )
    if _SAFE_PATH_RE.fullmatch(relative) is None:
        raise CurationGitHubError(
            f"{label} contains whitespace, control, or shell metacharacters"
        )
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise CurationGitHubError(f"{label} may not traverse a symlink")
    return relative


def _scoped_path(
    root: Path,
    adapter: str,
    value: str | Path,
    *,
    area: str,
    suffix: str,
    label: str,
    must_exist: bool,
) -> Path:
    root = root.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    relative = _normalized_relative(root, path, label)
    expected = PurePosixPath("source-adapters") / adapter / area
    try:
        PurePosixPath(relative).relative_to(expected)
    except ValueError as error:
        raise CurationGitHubError(
            f"{label} must be inside {expected.as_posix()}"
        ) from error
    path = root / relative
    if path.suffix != suffix:
        raise CurationGitHubError(f"{label} must use the {suffix} suffix")
    if must_exist and not path.is_file():
        raise CurationGitHubError(f"{label} does not exist: {relative}")
    if path.exists() and not path.is_file():
        raise CurationGitHubError(f"{label} is not a regular file: {relative}")
    return path


def _sha(value: object, label: str) -> str:
    value = _nonempty_line(value, label)
    if _SHA_RE.fullmatch(value) is None:
        raise CurationGitHubError(f"{label} must be a lowercase full Git object id")
    return value


def _timestamp(value: object, label: str) -> str:
    value = _nonempty_line(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CurationGitHubError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise CurationGitHubError(f"{label} must include a timezone")
    return value


def _https_url(value: object, label: str) -> str:
    value = _nonempty_line(value, label)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
        raise CurationGitHubError(f"{label} must be an HTTPS URL without credentials")
    return value


def _actions_run_coordinate(value: object) -> tuple[str, str]:
    url = _https_url(value, "public_data_run_url")
    parsed = urlsplit(url)
    match = re.fullmatch(r"/([^/]+/[^/]+)/actions/runs/([1-9][0-9]*)", parsed.path)
    if (
        parsed.netloc.lower() != "github.com"
        or match is None
        or parsed.query
        or parsed.fragment
    ):
        raise CurationGitHubError(
            "public_data_run_url must identify one immutable GitHub Actions run"
        )
    return match.group(1), match.group(2)


def _actor(value: object, label: str) -> str:
    value = _nonempty_line(value, label)
    if _ACTOR_RE.fullmatch(value) is None:
        raise CurationGitHubError(f"{label} must use github-user:<numeric-id>@<login>")
    return value


def _canonical_bundle_paths(adapter_id: str, run_id: str) -> dict[str, str]:
    stem = f"github-{run_id}"
    transaction_root = f"source-adapters/{adapter_id}/transactions"
    return {
        "inventory": f"{transaction_root}/{stem}.yaml",
        "decisions": (f"source-adapters/{adapter_id}/policy/curation-decisions.yaml"),
        "manifest": f"{transaction_root}/{stem}.review-manifest.yaml",
        "review": f"{transaction_root}/{stem}.review.md",
    }


def build_review_bundle(
    core: ModuleType,
    inventory,
    decisions,
    *,
    inventory_path: str,
    decisions_path: str,
    manifest_path: str,
    review_path: str,
    base_sha: str,
    head_sha: str,
    public_data_actor: str,
    public_data_at: str,
    public_data_run_url: str,
) -> tuple[dict[str, object], str]:
    """Build a deterministic manifest and complete Markdown review surface."""

    if len(inventory.candidates) > MAX_CANDIDATES:
        raise CurationGitHubError(
            f"Inventory exceeds the {MAX_CANDIDATES}-candidate hosted review limit"
        )
    base_sha = _sha(base_sha, "base_sha")
    head_sha = _sha(head_sha, "head_sha")
    if head_sha != base_sha:
        raise CurationGitHubError(
            "Proposal base and head must both be the trusted default-branch head"
        )
    repository, run_id = _actions_run_coordinate(public_data_run_url)
    supplied_paths = {
        "inventory": inventory_path,
        "decisions": decisions_path,
        "manifest": manifest_path,
        "review": review_path,
    }
    if supplied_paths != _canonical_bundle_paths(inventory.adapter_id, run_id):
        raise CurationGitHubError(
            "Hosted bundle paths must use the canonical github-<run_id> grammar"
        )
    acknowledgment = {
        "actor": _actor(public_data_actor, "public_data_actor"),
        "acknowledged_at": _timestamp(public_data_at, "public_data_at"),
        "run_url": _https_url(public_data_run_url, "public_data_run_url"),
    }
    unsigned: dict[str, object] = {
        "format": FORMAT,
        "inventory_id": inventory.inventory_id,
        "adapter_id": inventory.adapter_id,
        "paths": supplied_paths,
        "proposal_coordinates": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "repository": repository,
            "run_id": run_id,
            "source_inputs": _deep_copy(inventory.inputs),
        },
        "public_data_acknowledgment": acknowledgment,
        "candidates": _candidate_entries(core, inventory, decisions),
    }
    review = render_review_markdown(unsigned)
    if len(review.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise CurationGitHubError("Rendered review exceeds the 8 MiB hosted limit")
    unsigned["review_sha256"] = _digest(review.encode("utf-8"))
    manifest = {**unsigned, "bundle_digest": _digest(_canonical_bytes(unsigned))}
    if (
        len(yaml.safe_dump(manifest, sort_keys=False).encode("utf-8"))
        > MAX_MANIFEST_BYTES
    ):
        raise CurationGitHubError("Rendered manifest exceeds the 16 MiB hosted limit")
    return manifest, review


def pr_body_marker(manifest: Mapping[str, object]) -> str:
    return (
        f'{_MARKER_START}inventory_id="{manifest["inventory_id"]}" '
        f'bundle_digest="{manifest["bundle_digest"]}" -->'
    )


def _attestation_marker(payload: Mapping[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(_canonical_bytes(payload)).rstrip(b"=")
    return f"{_ATTESTATION_START}{encoded.decode('ascii')} -->"


def _decode_attestation_payload(encoded: str) -> Mapping[str, object]:
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = _mapping(json.loads(decoded), "attestation payload")
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise CurationGitHubError("Attestation marker payload is malformed") from error
    if _attestation_marker(payload) != f"{_ATTESTATION_START}{encoded} -->":
        raise CurationGitHubError("Attestation marker payload is not canonical JSON")
    return payload


def _attestation_body(summary: str, payload: Mapping[str, object]) -> str:
    return (
        "**AI-generated draft — not reviewed by John**\n\n"
        f"{summary}\n\n{_attestation_marker(payload)}\n"
    )


def _file_binding(root: Path, relative: str, label: str) -> dict[str, str]:
    path = root / relative
    normalized = _normalized_relative(root, path, label)
    if path.is_symlink() or not path.is_file():
        raise CurationGitHubError(f"{label} is not a regular file")
    return {"path": normalized, "sha256": _digest(path.read_bytes())}


def _source_receipt_fields(manifest: Mapping[str, object]) -> tuple[str, str]:
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    inputs = _mapping(coordinates["source_inputs"], "source_inputs")
    evaluation = _mapping(inputs.get("evaluation_context"), "evaluation_context")
    as_of = _nonempty_line(evaluation.get("as_of"), "evaluation_context.as_of")
    try:
        date.fromisoformat(as_of)
    except ValueError as error:
        raise CurationGitHubError(
            "evaluation_context.as_of is not an ISO date"
        ) from error
    source = _mapping(inputs.get("source"), "source_inputs.source")
    return as_of, _digest(_canonical_bytes(source))


def proposal_receipt_payload(
    root: Path,
    manifest: Mapping[str, object],
    pull_request: Mapping[str, object],
    sidecar_path: str,
    *,
    workflow_run_attempt: int,
) -> dict[str, object]:
    """Bind the immutable proposal bytes to its first bot-created PR head."""

    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    paths = _mapping(manifest["paths"], "manifest paths")
    number = pull_request.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise CurationGitHubError("Pull-request number is invalid")
    head = _mapping(pull_request.get("head"), "pull_request.head")
    proposal_head = _sha(head.get("sha"), "pull_request.head.sha")
    workflow_run_attempt = _positive_integer(
        workflow_run_attempt, "workflow_run_attempt"
    )
    if workflow_run_attempt != 1:
        raise CurationGitHubError("Proposal receipt requires workflow run attempt 1")
    sidecar_path = _nonempty_line(sidecar_path, "proposal sidecar path")
    if re.fullmatch(r"\.datalad/runinfo/[0-9a-f]{32}", sidecar_path) is None:
        raise CurationGitHubError("Proposal sidecar path is not content-addressed")
    as_of, source_sha256 = _source_receipt_fields(manifest)
    return {
        "format": PROPOSAL_RECEIPT_FORMAT,
        "repository": str(coordinates["repository"]),
        "pull_request": number,
        "workflow_run_id": str(coordinates["run_id"]),
        "workflow_run_attempt": workflow_run_attempt,
        "base_sha": str(coordinates["base_sha"]),
        "proposal_head_sha": proposal_head,
        "adapter_id": str(manifest["adapter_id"]),
        "inventory_id": str(manifest["inventory_id"]),
        "bundle_digest": str(manifest["bundle_digest"]),
        "manifest": str(paths["manifest"]),
        "as_of": as_of,
        "source_sha256": source_sha256,
        "files": {
            "inventory": _file_binding(
                root, str(paths["inventory"]), "receipt inventory"
            ),
            "manifest": _file_binding(root, str(paths["manifest"]), "receipt manifest"),
            "review": _file_binding(root, str(paths["review"]), "receipt review"),
            "sidecar": _file_binding(root, sidecar_path, "receipt sidecar"),
        },
    }


def ledger_attestation_payload(
    root: Path,
    manifest: Mapping[str, object],
    pull_request: Mapping[str, object],
    *,
    workflow_run_id: str,
    workflow_run_attempt: int,
    parent_head_sha: str,
    target_head_sha: str,
) -> dict[str, object]:
    """Bind one bot-produced ledger commit to its authenticated parent."""

    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    paths = _mapping(manifest["paths"], "manifest paths")
    number = pull_request.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise CurationGitHubError("Pull-request number is invalid")
    run_id = _nonempty_line(workflow_run_id, "workflow_run_id")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise CurationGitHubError("workflow_run_id must be a positive integer")
    workflow_run_attempt = _positive_integer(
        workflow_run_attempt, "workflow_run_attempt"
    )
    parent = _sha(parent_head_sha, "parent_head_sha")
    target = _sha(target_head_sha, "target_head_sha")
    if parent == target:
        raise CurationGitHubError("Ledger attestation must bind a new commit")
    head = _mapping(pull_request.get("head"), "pull_request.head")
    if _sha(head.get("sha"), "pull_request.head.sha") != parent:
        raise CurationGitHubError("Attestation parent is not the current PR head")
    decision_path = root / str(paths["decisions"])
    _normalized_relative(root, decision_path, "attested decision ledger")
    if decision_path.is_symlink() or not decision_path.is_file():
        raise CurationGitHubError("Attested decision ledger does not exist")
    return {
        "format": LEDGER_ATTESTATION_FORMAT,
        "repository": str(coordinates["repository"]),
        "pull_request": number,
        "workflow_run_id": run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "parent_head_sha": parent,
        "target_head_sha": target,
        "adapter_id": str(manifest["adapter_id"]),
        "inventory_id": str(manifest["inventory_id"]),
        "bundle_digest": str(manifest["bundle_digest"]),
        "manifest": str(paths["manifest"]),
        "decisions": str(paths["decisions"]),
        "ledger_sha256": _digest(decision_path.read_bytes()),
    }


def validate_allowed_changes(
    manifest: Mapping[str, object], changes: object, *, phase: str
) -> tuple[str, ...]:
    """Allow only regular, phase-appropriate curation transaction files."""

    if phase not in {"initial", "reviewed", "reconciled"}:
        raise CurationGitHubError(f"Unsupported curation PR phase: {phase}")

    document = _mapping(changes, "changed paths")
    _only_fields(document, {"files"}, "changed paths")
    files = document["files"]
    if not isinstance(files, list):
        raise CurationGitHubError("changed paths files must be a list")
    paths = _mapping(manifest["paths"], "manifest paths")
    bundle_allowed = {
        str(paths["inventory"]),
        str(paths["manifest"]),
        str(paths["review"]),
    }
    if phase in {"reviewed", "reconciled"}:
        bundle_allowed.add(str(paths["decisions"]))
    required = {
        str(paths["inventory"]),
        str(paths["manifest"]),
        str(paths["review"]),
    }
    if phase in {"reviewed", "reconciled"}:
        required.add(str(paths["decisions"]))
    observed: list[str] = []
    statuses: dict[str, str] = {}
    runinfo_paths: list[str] = []
    report_numbers: list[int] = []
    inventory_stem = Path(str(paths["inventory"])).stem
    report_pattern = re.compile(
        rf"source-adapters/{re.escape(str(manifest['adapter_id']))}/transactions/"
        rf"{re.escape(inventory_stem)}\.reconciliation-([0-9]{{3}})\.yaml\Z"
    )
    candidate_paths = {
        f"metadata/records/{entry['proposed_path']}" for entry in manifest["candidates"]
    }
    for index, raw in enumerate(files):
        item = _mapping(raw, f"changed paths files[{index}]")
        _only_fields(item, {"path", "status", "mode"}, f"changed paths files[{index}]")
        path = _nonempty_line(item["path"], f"changed paths files[{index}].path")
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or path != pure.as_posix()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise CurationGitHubError(f"Changed path is unsafe: {path}")
        if path in observed:
            raise CurationGitHubError(f"Changed path is duplicated: {path}")
        runinfo = re.fullmatch(r"\.datalad/runinfo/[0-9a-f]{32}", path) is not None
        report_match = report_pattern.fullmatch(path)
        phase_extra = runinfo or (
            phase == "reconciled"
            and (path in candidate_paths or report_match is not None)
        )
        if path not in bundle_allowed and not phase_extra:
            raise CurationGitHubError(f"Unexpected path in curation PR: {path}")
        if item["status"] not in {"added", "modified"}:
            raise CurationGitHubError(f"Changed path has unsafe status: {path}")
        if item["mode"] != "100644":
            raise CurationGitHubError(f"Changed path has unsafe file mode: {path}")
        observed.append(path)
        statuses[path] = str(item["status"])
        if runinfo:
            if item["status"] != "added":
                raise CurationGitHubError(
                    f"DataLad run sidecar must be newly added: {path}"
                )
            runinfo_paths.append(path)
        if report_match is not None:
            if item["status"] != "added":
                raise CurationGitHubError(
                    f"Reconciliation report must be append-only: {path}"
                )
            report_numbers.append(int(report_match.group(1)))
    missing = sorted(required - set(observed))
    if missing:
        raise CurationGitHubError(
            "Curation PR omits required proposal paths: " + ", ".join(missing)
        )
    for path in (str(paths["inventory"]), str(paths["manifest"]), str(paths["review"])):
        if statuses[path] != "added":
            raise CurationGitHubError(
                f"Proposal bundle path must be newly added: {path}"
            )
    if phase == "reconciled":
        if report_numbers != [1]:
            raise CurationGitHubError(
                "Terminal reconciliation requires exactly report 001"
            )
        expected_sidecars = 2
    elif report_numbers:
        raise CurationGitHubError("Reconciliation reports are not allowed yet")
    else:
        expected_sidecars = 1
    if len(runinfo_paths) != expected_sidecars:
        raise CurationGitHubError(
            f"{phase} curation PR requires exactly {expected_sidecars} "
            "content-addressed DataLad run sidecar(s)"
        )
    return tuple(observed)


def validate_datalad_sidecars(
    root: Path,
    changed_paths: Sequence[str],
    *,
    manifest: Mapping[str, object],
    phase: str,
) -> None:
    """Validate content-addressed, successful DataLad run evidence."""

    changed = set(changed_paths)
    sidecars = sorted(path for path in changed if path.startswith(".datalad/runinfo/"))
    observed_outputs: list[tuple[str, ...]] = []
    for relative in sidecars:
        path = root / relative
        _normalized_relative(root, path, "DataLad run sidecar")
        if path.is_symlink() or not path.is_file():
            raise CurationGitHubError("DataLad run sidecar is not a regular file")
        try:
            with lzma.open(path, "rt", encoding="utf-8") as stream:
                records = [json.loads(line) for line in stream if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError, lzma.LZMAError) as error:
            raise CurationGitHubError(
                f"DataLad run sidecar is malformed: {relative}"
            ) from error
        if len(records) != 1 or not isinstance(records[0], dict):
            raise CurationGitHubError(
                f"DataLad sidecar must contain exactly one run record: {relative}"
            )
        record = records[0]
        serialized = json.dumps(record, indent=1, sort_keys=True, ensure_ascii=False)
        record_id = hashlib.md5(  # nosec - DataLad's content-address format.
            serialized.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        if path.name != record_id:
            raise CurationGitHubError(
                f"DataLad sidecar filename is not its content address: {relative}"
            )
        if record.get("exit") != 0 or record.get("pwd") != ".":
            raise CurationGitHubError(
                f"DataLad sidecar does not record a successful root run: {relative}"
            )
        outputs = record.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise CurationGitHubError(
                f"DataLad sidecar has no explicit outputs: {relative}"
            )
        normalized_outputs: list[str] = []
        for output in outputs:
            output = _nonempty_line(output, "DataLad run output")
            if _SAFE_PATH_RE.fullmatch(output) is None:
                raise CurationGitHubError(
                    f"DataLad sidecar output is not in the allowed PR diff: {output}"
                )
            if output == "metadata/records":
                normalized_outputs.append(output)
                continue
            if output not in changed:
                raise CurationGitHubError(
                    f"DataLad sidecar output is not in the allowed PR diff: {output}"
                )
            normalized_outputs.append(output)
        if len(normalized_outputs) != len(set(normalized_outputs)):
            raise CurationGitHubError("DataLad sidecar outputs are duplicated")
        observed_outputs.append(tuple(normalized_outputs))

    paths = _mapping(manifest["paths"], "manifest paths")
    proposal_outputs = (str(paths["inventory"]),)
    if phase == "reconciled":
        inventory_path = Path(str(paths["inventory"]))
        report = inventory_path.with_name(
            f"{inventory_path.stem}.reconciliation-001.yaml"
        ).as_posix()
        expected_outputs = sorted([proposal_outputs, ("metadata/records", report)])
    else:
        expected_outputs = [proposal_outputs]
    if sorted(observed_outputs) != expected_outputs:
        raise CurationGitHubError(
            f"{phase} DataLad sidecar outputs do not match the trusted run contract"
        )


def _regular_tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise CurationGitHubError(f"Canonical metadata tree is unavailable: {root}")
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CurationGitHubError(
                f"Canonical metadata tree contains a symlink: {path}"
            )
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def validate_reconciled_outputs(
    root: Path,
    core: ModuleType,
    inventory,
    decisions,
    manifest: Mapping[str, object],
) -> None:
    """Replay the one terminal reconciliation from trusted base authority."""

    paths = _mapping(manifest["paths"], "manifest paths")
    report_path = root / str(paths["inventory"])
    report_path = report_path.with_name(f"{report_path.stem}.reconciliation-001.yaml")
    _normalized_relative(root, report_path, "reconciliation report")
    if report_path.is_symlink() or not report_path.is_file():
        raise CurationGitHubError("Terminal reconciliation report 001 is missing")
    report = _mapping(
        _strict_yaml(report_path.read_text(encoding="utf-8"), "reconciliation report"),
        "reconciliation report",
    )
    authority_records = TRUSTED_ROOT.resolve() / "metadata/records"
    scratch_parent = TRUSTED_ROOT.resolve() / "build/curation/reconciled-validation"
    _normalized_relative(TRUSTED_ROOT.resolve(), scratch_parent, "replay scratch")
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=scratch_parent, prefix=".terminal-replay-"
    ) as temporary:
        replay_records = Path(temporary) / "records"
        shutil.copytree(authority_records, replay_records)
        replayed = core.reconcile_inventory(
            inventory,
            decisions,
            replay_records,
            validate_staged=lambda _path: None,
        )
        if replayed != dict(report):
            raise CurationGitHubError(
                "Terminal reconciliation report is not a trusted replay result"
            )
        if _regular_tree(replay_records) != _regular_tree(root / "metadata/records"):
            raise CurationGitHubError(
                "Reconciled canonical metadata differs from the trusted replay"
            )


def _manifest_candidate_expected(
    core: ModuleType, inventory, entry: Mapping[str, object]
) -> dict[str, object]:
    """Rebuild immutable display fields while carrying the initial CAS hint.

    ``expected_decision`` is only a retained, historical form hint.  Submission
    authority always comes from the active decision book at apply time.
    """

    candidates = {
        candidate.candidate_id: candidate for candidate in inventory.candidates
    }
    candidate_id = entry.get("candidate_id")
    try:
        candidate = candidates[candidate_id]
    except (KeyError, TypeError) as error:
        raise CurationGitHubError(
            f"Manifest references an unknown candidate: {candidate_id!r}"
        ) from error
    aliases = stable_aliases(inventory)
    return {
        "alias": aliases[candidate.candidate_id],
        "candidate_id": candidate.candidate_id,
        "claim_revision_id": core.claim_revision_identity(
            candidate.candidate_id,
            candidate.material_fingerprint,
            candidate.relevant_policy_fingerprint,
        ),
        "expected_decision": entry.get("expected_decision"),
        "source_namespace": candidate.source_namespace,
        "source_record_id": candidate.source_record_id,
        "claim_kind": candidate.claim_kind,
        "proposed_path": candidate.proposed_path,
        "review": _deep_copy(inventory.review[candidate.candidate_id]),
        "blockers": list(candidate.blockers),
        "baseline_record": (
            None
            if candidate.baseline_record is None
            else _deep_copy(candidate.baseline_record)
        ),
        "proposed_record": _deep_copy(candidate.proposed_record),
        "semantic_diff": semantic_diff(
            candidate.baseline_record, candidate.proposed_record
        ),
    }


def validate_manifest(
    core: ModuleType,
    inventory,
    decisions,
    manifest: Mapping[str, object],
    review: str,
) -> None:
    """Validate immutable display data, aliases, coordinates, and review bytes."""

    if len(inventory.candidates) > MAX_CANDIDATES:
        raise CurationGitHubError("Inventory exceeds the hosted candidate limit")
    if len(review.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise CurationGitHubError("Review Markdown exceeds the hosted size limit")
    if (
        len(yaml.safe_dump(dict(manifest), sort_keys=False).encode("utf-8"))
        > MAX_MANIFEST_BYTES
    ):
        raise CurationGitHubError("Manifest exceeds the hosted size limit")
    _only_fields(
        manifest,
        {
            "format",
            "inventory_id",
            "adapter_id",
            "paths",
            "proposal_coordinates",
            "public_data_acknowledgment",
            "candidates",
            "review_sha256",
            "bundle_digest",
        },
        "manifest",
    )
    if manifest["format"] != FORMAT:
        raise CurationGitHubError("Manifest format is unsupported")
    if manifest["inventory_id"] != inventory.inventory_id:
        raise CurationGitHubError("Manifest inventory id is stale")
    if manifest["adapter_id"] != inventory.adapter_id:
        raise CurationGitHubError("Manifest adapter is stale")
    paths = _mapping(manifest["paths"], "manifest paths")
    _only_fields(
        paths, {"inventory", "decisions", "manifest", "review"}, "manifest paths"
    )
    for name, value in paths.items():
        rendered_path = _nonempty_line(value, f"manifest paths.{name}")
        pure = PurePosixPath(rendered_path)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise CurationGitHubError(f"manifest paths.{name} is unsafe")
        if _SAFE_PATH_RE.fullmatch(rendered_path) is None:
            raise CurationGitHubError(
                f"manifest paths.{name} contains shell metacharacters"
            )
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    _only_fields(
        coordinates,
        {"base_sha", "head_sha", "repository", "run_id", "source_inputs"},
        "proposal_coordinates",
    )
    base_sha = _sha(coordinates["base_sha"], "proposal_coordinates.base_sha")
    head_sha = _sha(coordinates["head_sha"], "proposal_coordinates.head_sha")
    if head_sha != base_sha:
        raise CurationGitHubError("Manifest proposal coordinates do not share a base")
    repository = _nonempty_line(
        coordinates["repository"], "proposal_coordinates.repository"
    )
    run_id = _nonempty_line(coordinates["run_id"], "proposal_coordinates.run_id")
    if _canonical_bytes(coordinates["source_inputs"]) != _canonical_bytes(
        inventory.inputs
    ):
        raise CurationGitHubError("Manifest source inputs are stale")
    acknowledgment = _mapping(
        manifest["public_data_acknowledgment"], "public_data_acknowledgment"
    )
    _only_fields(
        acknowledgment,
        {"actor", "acknowledged_at", "run_url"},
        "public_data_acknowledgment",
    )
    _actor(acknowledgment["actor"], "public_data_acknowledgment.actor")
    _timestamp(
        acknowledgment["acknowledged_at"],
        "public_data_acknowledgment.acknowledged_at",
    )
    run_repository, observed_run_id = _actions_run_coordinate(acknowledgment["run_url"])
    if (repository, run_id) != (run_repository, observed_run_id):
        raise CurationGitHubError(
            "Manifest proposal coordinates differ from the acknowledged run"
        )
    if dict(paths) != _canonical_bundle_paths(inventory.adapter_id, run_id):
        raise CurationGitHubError("Manifest bundle paths are not canonical")
    raw_entries = manifest["candidates"]
    if not isinstance(raw_entries, list) or len(raw_entries) != len(
        inventory.candidates
    ):
        raise CurationGitHubError("Manifest candidates do not match the inventory")
    candidate_ids: list[str] = []
    for raw in raw_entries:
        entry = _mapping(raw, "manifest candidate")
        expected = _manifest_candidate_expected(core, inventory, entry)
        if dict(entry) != expected:
            raise CurationGitHubError(
                f"Manifest display data changed for {entry.get('candidate_id')!r}"
            )
        candidate_ids.append(str(entry["candidate_id"]))
        expected_decision = entry["expected_decision"]
        if expected_decision is not None:
            if (
                not isinstance(expected_decision, str)
                or _DECISION_RE.fullmatch(expected_decision) is None
            ):
                raise CurationGitHubError("Manifest expected decision is malformed")
            revisions = decisions.revisions(str(entry["candidate_id"]))
            if expected_decision not in {
                revision.decision_id for revision in revisions
            }:
                raise CurationGitHubError(
                    "Manifest expected decision is not retained in candidate history"
                )
    if candidate_ids != [item.candidate_id for item in inventory.candidates]:
        raise CurationGitHubError("Manifest candidates are not in inventory order")
    expected_review = render_review_markdown(manifest)
    if review != expected_review:
        raise CurationGitHubError("Review Markdown differs from its immutable cards")
    if manifest["review_sha256"] != _digest(review.encode("utf-8")):
        raise CurationGitHubError("Review Markdown digest is invalid")
    unsigned = dict(manifest)
    bundle_digest = unsigned.pop("bundle_digest")
    if bundle_digest != _digest(_canonical_bytes(unsigned)):
        raise CurationGitHubError("Manifest bundle digest is invalid")


def parse_submission_comment(body: str) -> tuple[str, tuple[Submission, ...]]:
    """Parse one complete, strictly fenced batch submission comment."""

    if not isinstance(body, str):
        raise CurationGitHubError("Comment body must be text")
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise CurationGitHubError("Comment exceeds the 50 KiB curation limit")
    match = _COMMENT_RE.fullmatch(body)
    if match is None:
        raise CurationGitHubError(
            "Comment must contain only /curation submit and one fenced YAML document"
        )
    document = _mapping(
        _strict_yaml(match.group("yaml"), "submission YAML"), "submission"
    )
    _only_fields(document, {"inventory_id", "decisions"}, "submission")
    inventory_id = _nonempty_line(document["inventory_id"], "inventory_id")
    raw_decisions = document["decisions"]
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise CurationGitHubError("decisions must be a non-empty list")
    submissions: list[Submission] = []
    seen: set[str] = set()
    detail_fields = {
        "accept": set(),
        "reject": set(),
        "link": {"target_record_id"},
        "defer": {"return_when"},
        "permanent-exclude": {"scope"},
        "supersede": {"replacement_candidate_id"},
    }
    for index, raw in enumerate(raw_decisions):
        item = _mapping(raw, f"decisions[{index}]")
        _only_fields(
            item,
            {
                "candidate",
                "expected_decision",
                "disposition",
                "rationale",
                "evidence",
                "details",
            },
            f"decisions[{index}]",
        )
        alias = _nonempty_line(item["candidate"], f"decisions[{index}].candidate")
        if alias in seen:
            raise CurationGitHubError(f"Duplicate candidate alias in batch: {alias}")
        seen.add(alias)
        expected = item["expected_decision"]
        if expected is not None and (
            not isinstance(expected, str) or _DECISION_RE.fullmatch(expected) is None
        ):
            raise CurationGitHubError(
                f"decisions[{index}].expected_decision is not null or a decision id"
            )
        disposition = _nonempty_line(
            item["disposition"], f"decisions[{index}].disposition"
        )
        if disposition not in detail_fields:
            raise CurationGitHubError(f"Unsupported disposition: {disposition}")
        rationale = _nonempty_line(item["rationale"], f"decisions[{index}].rationale")
        raw_evidence = item["evidence"]
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise CurationGitHubError(
                f"decisions[{index}].evidence must be a non-empty list"
            )
        evidence = tuple(
            _nonempty_line(value, f"decisions[{index}].evidence")
            for value in raw_evidence
        )
        if len(evidence) != len(set(evidence)):
            raise CurationGitHubError(
                f"decisions[{index}].evidence entries must be unique"
            )
        details = _mapping(item["details"], f"decisions[{index}].details")
        _only_fields(
            details,
            detail_fields[disposition],
            f"decisions[{index}].details",
        )
        submissions.append(
            Submission(
                alias=alias,
                expected_decision=expected,
                disposition=disposition,
                rationale=rationale,
                evidence=evidence,
                details=_deep_copy(details),
            )
        )
    return inventory_id, tuple(submissions)


def derive_comment_provenance(event: Mapping[str, object]) -> CommentProvenance:
    comment = _mapping(event.get("comment"), "event.comment")
    user = _mapping(comment.get("user"), "event.comment.user")
    identifier = user.get("id")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier < 1
    ):
        raise CurationGitHubError("Comment user id must be a positive integer")
    login = _nonempty_line(user.get("login"), "comment user login")
    reviewer = _actor(f"github-user:{identifier}@{login}", "derived reviewer")
    created_at = _timestamp(comment.get("created_at"), "comment.created_at")
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    decided_on = parsed.astimezone(timezone.utc).date()
    comment_id = comment.get("id")
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id < 1
    ):
        raise CurationGitHubError("Comment id must be a positive integer")
    comment_url = _https_url(comment.get("html_url"), "comment.html_url")
    parsed_url = urlsplit(comment_url)
    if (
        parsed_url.netloc.lower() != "github.com"
        or parsed_url.fragment != f"issuecomment-{comment_id}"
    ):
        raise CurationGitHubError("Comment URL is not the immutable GitHub comment URL")
    return CommentProvenance(
        reviewer=reviewer,
        decided_on=decided_on,
        comment_url=comment_url,
        comment_id=comment_id,
        login=login,
    )


def _github_user(value: object, label: str) -> tuple[int, str]:
    user = _mapping(value, label)
    identifier = user.get("id")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier < 1
    ):
        raise CurationGitHubError(f"{label}.id must be a positive integer")
    login = _nonempty_line(user.get("login"), f"{label}.login")
    _actor(f"github-user:{identifier}@{login}", label)
    return identifier, login


def validate_proposal_run(
    manifest: Mapping[str, object],
    run: Mapping[str, object],
    *,
    default_branch: str,
    require_success: bool = True,
    allow_unsuccessful: bool = False,
) -> None:
    """Bind self-recorded acknowledgment fields to immutable Actions state."""

    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    acknowledgment = _mapping(
        manifest["public_data_acknowledgment"], "public_data_acknowledgment"
    )
    repository = _mapping(run.get("repository"), "proposal run repository")
    head_repository = _mapping(
        run.get("head_repository"), "proposal run head_repository"
    )
    actor_id, actor_login = _github_user(run.get("actor"), "proposal run actor")
    if _positive_integer(run.get("run_attempt"), "proposal run.run_attempt") != 1:
        raise CurationGitHubError("Proposal workflow must use run attempt 1")
    if str(run.get("id")) != coordinates["run_id"]:
        raise CurationGitHubError("Proposal run id differs from manifest")
    if run.get("html_url") != acknowledgment["run_url"]:
        raise CurationGitHubError("Proposal run URL differs from acknowledgment")
    if run.get("event") != "workflow_dispatch":
        raise CurationGitHubError("Proposal must originate from workflow_dispatch")
    if run.get("path") != ".github/workflows/curation-review.yml":
        raise CurationGitHubError("Proposal used an unexpected trusted workflow")
    if (
        repository.get("full_name") != coordinates["repository"]
        or head_repository.get("full_name") != coordinates["repository"]
    ):
        raise CurationGitHubError("Proposal run repository is inconsistent")
    if (
        run.get("head_sha") != coordinates["base_sha"]
        or run.get("head_branch") != default_branch
    ):
        raise CurationGitHubError(
            "Proposal run did not start at the trusted default head"
        )
    if acknowledgment["actor"] != f"github-user:{actor_id}@{actor_login}":
        raise CurationGitHubError("Public-data acknowledgment actor is unauthenticated")
    if run.get("created_at") != acknowledgment["acknowledged_at"]:
        raise CurationGitHubError("Public-data acknowledgment time is unauthenticated")
    if require_success:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise CurationGitHubError(
                "Proposal workflow run is not completed successfully"
            )
    else:
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status not in {"queued", "in_progress", "completed"}:
            raise CurationGitHubError("Proposal workflow run state is invalid")
        if status == "completed":
            if not isinstance(conclusion, str) or not conclusion:
                raise CurationGitHubError("Completed proposal run lacks a conclusion")
            if not allow_unsuccessful and conclusion != "success":
                raise CurationGitHubError("Proposal workflow run failed")
        elif conclusion is not None:
            raise CurationGitHubError("Incomplete proposal run has a conclusion")


def _validate_attestation_payload_shape(payload: Mapping[str, object]) -> str:
    format_name = payload.get("format")
    common = {
        "format",
        "repository",
        "pull_request",
        "workflow_run_id",
        "workflow_run_attempt",
        "adapter_id",
        "inventory_id",
        "bundle_digest",
        "manifest",
    }
    if format_name == PROPOSAL_RECEIPT_FORMAT:
        _only_fields(
            payload,
            common
            | {
                "base_sha",
                "proposal_head_sha",
                "as_of",
                "source_sha256",
                "files",
            },
            "proposal receipt",
        )
        _sha(payload["base_sha"], "proposal receipt base_sha")
        _sha(payload["proposal_head_sha"], "proposal receipt proposal_head_sha")
        _nonempty_line(payload["as_of"], "proposal receipt as_of")
        files = _mapping(payload["files"], "proposal receipt files")
        _only_fields(
            files, {"inventory", "manifest", "review", "sidecar"}, "receipt files"
        )
        for name, raw in files.items():
            binding = _mapping(raw, f"receipt files.{name}")
            _only_fields(binding, {"path", "sha256"}, f"receipt files.{name}")
            path = _nonempty_line(binding["path"], f"receipt files.{name}.path")
            if _SAFE_PATH_RE.fullmatch(path) is None:
                raise CurationGitHubError(f"receipt files.{name}.path is unsafe")
            digest = _nonempty_line(binding["sha256"], f"receipt files.{name}.sha256")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise CurationGitHubError(f"receipt files.{name}.sha256 is invalid")
        digest_fields = ("source_sha256", "bundle_digest")
    elif format_name == LEDGER_ATTESTATION_FORMAT:
        _only_fields(
            payload,
            common
            | {
                "parent_head_sha",
                "target_head_sha",
                "decisions",
                "ledger_sha256",
            },
            "ledger attestation",
        )
        parent = _sha(payload["parent_head_sha"], "ledger parent_head_sha")
        target = _sha(payload["target_head_sha"], "ledger target_head_sha")
        if parent == target:
            raise CurationGitHubError("Ledger attestation contains a self-edge")
        decisions = _nonempty_line(payload["decisions"], "ledger decisions")
        if _SAFE_PATH_RE.fullmatch(decisions) is None:
            raise CurationGitHubError("Ledger attestation decision path is unsafe")
        digest_fields = ("ledger_sha256", "bundle_digest")
    else:
        raise CurationGitHubError("Attestation marker format is unsupported")
    repository = _nonempty_line(payload["repository"], "attestation repository")
    if re.fullmatch(r"[^/\s]+/[^/\s]+", repository) is None:
        raise CurationGitHubError("Attestation repository is invalid")
    number = payload["pull_request"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise CurationGitHubError("Attestation pull-request number is invalid")
    run_id = _nonempty_line(payload["workflow_run_id"], "workflow_run_id")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise CurationGitHubError("Attestation workflow_run_id is invalid")
    _positive_integer(
        payload["workflow_run_attempt"], "Attestation workflow_run_attempt"
    )
    _nonempty_line(payload["adapter_id"], "attestation adapter_id")
    _nonempty_line(payload["inventory_id"], "attestation inventory_id")
    manifest_path = _nonempty_line(payload["manifest"], "attestation manifest")
    if _SAFE_PATH_RE.fullmatch(manifest_path) is None:
        raise CurationGitHubError("Attestation manifest path is unsafe")
    for field in digest_fields:
        value = _nonempty_line(payload[field], f"attestation {field}")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise CurationGitHubError(f"Attestation {field} is invalid")
    return str(format_name)


def parse_attestation_comments(
    comments: object,
    *,
    repository: str,
    pull_request_number: int,
) -> tuple[dict[str, object], ...]:
    """Strictly extract bot-authored canonical markers, independent of API order."""

    if not isinstance(comments, list):
        raise CurationGitHubError("Attestation comments JSON must be a raw list")
    seen_comment_ids: set[int] = set()
    seen_payloads: set[bytes] = set()
    observed: list[dict[str, object]] = []
    for index, raw in enumerate(comments):
        comment = _mapping(raw, f"attestation comments[{index}]")
        comment_id = comment.get("id")
        if (
            isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or comment_id < 1
            or comment_id in seen_comment_ids
        ):
            raise CurationGitHubError("Attestation comment ids must be unique integers")
        seen_comment_ids.add(comment_id)
        user = comment.get("user")
        if not isinstance(user, Mapping) or (user.get("id"), user.get("login")) != (
            PROPOSAL_BOT_ID,
            PROPOSAL_BOT_LOGIN,
        ):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            raise CurationGitHubError("Bot attestation comment body must be text")
        if len(body.encode("utf-8")) > MAX_ATTESTATION_BODY_BYTES:
            raise CurationGitHubError("Attestation comment exceeds the 128 KiB limit")
        marker_matches = tuple(_ATTESTATION_RE.finditer(body))
        if not marker_matches and _ATTESTATION_START not in body:
            continue
        if (
            len(marker_matches) != 1
            or body.count(_ATTESTATION_START) != 1
            or next((line for line in body.splitlines() if line.strip()), None)
            != "**AI-generated draft — not reviewed by John**"
        ):
            raise CurationGitHubError("Bot attestation comment marker is malformed")
        payload = _decode_attestation_payload(marker_matches[0].group("payload"))
        _validate_attestation_payload_shape(payload)
        canonical = _canonical_bytes(payload)
        if canonical in seen_payloads:
            raise CurationGitHubError("Attestation payload is duplicated")
        seen_payloads.add(canonical)
        if len(seen_payloads) > MAX_ATTESTATION_COMMENTS:
            raise CurationGitHubError(
                "Attestation comments exceed the 1000-marker limit"
            )
        provenance = derive_comment_provenance({"comment": comment})
        parsed_url = urlsplit(provenance.comment_url)
        if parsed_url.path not in {
            f"/{repository}/issues/{pull_request_number}",
            f"/{repository}/pull/{pull_request_number}",
        }:
            raise CurationGitHubError("Attestation comment belongs to another PR")
        created_at = _timestamp(
            comment.get("created_at"), "attestation comment.created_at"
        )
        updated_at = _timestamp(
            comment.get("updated_at"), "attestation comment.updated_at"
        )
        if updated_at != created_at:
            raise CurationGitHubError("Bot attestation comment was edited")
        observed.append(
            {
                "comment_id": comment_id,
                "comment_url": provenance.comment_url,
                "created_at": created_at,
                "payload": payload,
            }
        )
    return tuple(sorted(observed, key=lambda item: int(item["comment_id"])))


def attestation_run_ids(
    comments: object, *, repository: str, pull_request_number: int
) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            {
                (
                    str(
                        _mapping(item["payload"], "attestation payload")[
                            "workflow_run_id"
                        ]
                    ),
                    int(
                        _mapping(item["payload"], "attestation payload")[
                            "workflow_run_attempt"
                        ]
                    ),
                )
                for item in parse_attestation_comments(
                    comments,
                    repository=repository,
                    pull_request_number=pull_request_number,
                )
            },
            key=lambda item: (int(item[0]), item[1]),
        )
    )


def _attestation_run_index(
    runs: object,
) -> dict[tuple[str, int], Mapping[str, object]]:
    if not isinstance(runs, list):
        raise CurationGitHubError("Attestation runs JSON must be a raw list")
    if len(runs) > MAX_ATTESTATION_RUNS:
        raise CurationGitHubError("Attestation runs exceed the 1000-run limit")
    result: dict[tuple[str, int], Mapping[str, object]] = {}
    for index, raw in enumerate(runs):
        run = _mapping(raw, f"attestation runs[{index}]")
        identifier = run.get("id")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier < 1
        ):
            raise CurationGitHubError("Attestation run id must be a positive integer")
        attempt = _positive_integer(
            run.get("run_attempt"), "Attestation run.run_attempt"
        )
        key = (str(identifier), attempt)
        if key in result:
            raise CurationGitHubError("Attestation run attempt is duplicated")
        result[key] = run
    return result


def _validate_attestation_run(
    run: Mapping[str, object],
    *,
    run_id: str,
    run_attempt: int,
    repository: str,
    default_branch: str,
    trusted_default_sha: str,
    event_name: str,
    comment_created_at: str,
) -> bool:
    if str(run.get("id")) != run_id:
        raise CurationGitHubError("Attestation run id response is inconsistent")
    if run.get("run_attempt") != run_attempt:
        raise CurationGitHubError("Attestation run attempt response is inconsistent")
    if run.get("html_url") != f"https://github.com/{repository}/actions/runs/{run_id}":
        raise CurationGitHubError("Attestation run URL is inconsistent")
    if run.get("event") != event_name:
        raise CurationGitHubError("Attestation run event is inconsistent")
    if run.get("path") != ".github/workflows/curation-review.yml":
        raise CurationGitHubError("Attestation used an unexpected workflow")
    run_repository = _mapping(run.get("repository"), "attestation run.repository")
    head_repository = _mapping(
        run.get("head_repository"), "attestation run.head_repository"
    )
    if (
        run_repository.get("full_name") != repository
        or head_repository.get("full_name") != repository
        or run.get("head_branch") != default_branch
        or run.get("head_sha") != trusted_default_sha
    ):
        raise CurationGitHubError(
            "Attestation run repository or trusted head is invalid"
        )
    created = datetime.fromisoformat(
        _timestamp(run.get("created_at"), "attestation run.created_at").replace(
            "Z", "+00:00"
        )
    )
    updated = datetime.fromisoformat(
        _timestamp(run.get("updated_at"), "attestation run.updated_at").replace(
            "Z", "+00:00"
        )
    )
    comment_time = datetime.fromisoformat(comment_created_at.replace("Z", "+00:00"))
    if not created <= comment_time <= updated:
        raise CurationGitHubError("Attestation comment falls outside its workflow run")
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status == "completed":
        if not isinstance(conclusion, str) or not conclusion:
            raise CurationGitHubError("Completed attestation run lacks a conclusion")
        return conclusion == "success"
    if status not in {"queued", "in_progress"} or conclusion is not None:
        raise CurationGitHubError("Attestation run state is invalid")
    return False


def validate_hosted_guard(
    event: Mapping[str, object],
    pull_request: Mapping[str, object],
    permission: Mapping[str, object],
    *,
    expected_marker: str,
    expected_base_sha: str,
    expected_branch: str,
    expected_repository: str,
    command: str = "submit",
) -> CommentProvenance:
    """Purely validate the hosted event, PR state, and actor permission."""

    if event.get("action") != "created":
        raise CurationGitHubError("Only issue_comment.created events are accepted")
    issue = _mapping(event.get("issue"), "event.issue")
    if not isinstance(issue.get("pull_request"), Mapping):
        raise CurationGitHubError(
            "Curation comments are accepted only on pull requests"
        )
    repository = _mapping(event.get("repository"), "event.repository")
    repository_name = _nonempty_line(
        repository.get("full_name"), "repository full_name"
    )
    if repository_name != expected_repository:
        raise CurationGitHubError("Event repository differs from proposal coordinates")
    default_branch = _nonempty_line(
        repository.get("default_branch"), "repository default_branch"
    )
    if pull_request.get("state") != "open":
        raise CurationGitHubError("Curation pull request must be open")
    issue_number = issue.get("number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        raise CurationGitHubError("Issue number is invalid")
    if pull_request.get("number") != issue_number:
        raise CurationGitHubError("Event and pull-request numbers differ")
    creator = _github_user(pull_request.get("user"), "pull_request.user")
    if creator != (PROPOSAL_BOT_ID, PROPOSAL_BOT_LOGIN):
        raise CurationGitHubError("Curation PR was not created by the proposal bot")
    base = _mapping(pull_request.get("base"), "pull_request.base")
    head = _mapping(pull_request.get("head"), "pull_request.head")
    base_repo = _mapping(base.get("repo"), "pull_request.base.repo")
    head_repo = _mapping(head.get("repo"), "pull_request.head.repo")
    if base.get("ref") != default_branch:
        raise CurationGitHubError("Curation PR must target the default branch")
    if (
        base_repo.get("full_name") != repository_name
        or head_repo.get("full_name") != repository_name
    ):
        raise CurationGitHubError(
            "Curation PR base and head must be in the same repository"
        )
    if base.get("sha") != expected_base_sha:
        raise CurationGitHubError(
            "Pull-request base is not the trusted proposal base commit"
        )
    head_ref = _nonempty_line(head.get("ref"), "pull_request.head.ref")
    if head_ref != expected_branch:
        raise CurationGitHubError(
            f"Curation PR branch must be the proposal branch {expected_branch}"
        )
    labels = pull_request.get("labels")
    if not isinstance(labels, list):
        raise CurationGitHubError("Pull-request labels must be a list")
    label_names = {
        label.get("name")
        for label in labels
        if isinstance(label, Mapping) and isinstance(label.get("name"), str)
    }
    if REVIEW_LABEL not in label_names:
        raise CurationGitHubError(f"Curation PR requires the {REVIEW_LABEL} label")
    provenance = derive_comment_provenance(event)
    author_id = int(provenance.reviewer.split(":", 1)[1].split("@", 1)[0])
    sender = _github_user(event.get("sender"), "event.sender")
    if sender != (author_id, provenance.login):
        raise CurationGitHubError("Event sender and comment author differ")
    if permission.get("permission") not in {"write", "admin"}:
        raise CurationGitHubError("Comment author requires write or admin permission")
    permission_user = _github_user(permission.get("user"), "permission.user")
    if permission_user != (author_id, provenance.login):
        raise CurationGitHubError("Permission response belongs to another user")
    parsed_comment_url = urlsplit(provenance.comment_url)
    expected_paths = {
        f"/{repository_name}/issues/{issue_number}",
        f"/{repository_name}/pull/{issue_number}",
    }
    if parsed_comment_url.path not in expected_paths:
        raise CurationGitHubError(
            "Comment URL does not belong to the event repository and pull request"
        )
    comment = _mapping(event.get("comment"), "event.comment")
    if command == "submit":
        parse_submission_comment(comment.get("body"))
    elif command == "finalize":
        if comment.get("body") != FINALIZE_COMMAND:
            raise CurationGitHubError(
                "Finalize comment body must be exactly /curation finalize"
            )
    else:
        raise CurationGitHubError(f"Unsupported hosted command: {command}")
    body = pull_request.get("body")
    if not isinstance(body, str):
        raise CurationGitHubError("Pull-request body is missing the review marker")
    marker_count = body.count(expected_marker)
    all_marker_count = body.count(_MARKER_START)
    if marker_count != 1 or all_marker_count != 1:
        raise CurationGitHubError(
            "Pull-request body has a missing or ambiguous review marker"
        )
    return provenance


def _decision_document(
    core: ModuleType, text: str | None
) -> tuple[dict[str, object], object]:
    if text is None:
        document = {
            "format": core.DECISIONS_FORMAT,
            "decisions": [],
            "transactions": [],
        }
    else:
        document = _mapping(_strict_yaml(text, "decision YAML"), "decision document")
        document = _deep_copy(document)
    book = core.parse_decisions(yaml.safe_dump(document, sort_keys=False))
    return document, book


def apply_submission(
    core: ModuleType,
    cli: ModuleType,
    inventory,
    decision_document: Mapping[str, object],
    decision_book,
    *,
    inventory_id: str,
    submissions: Sequence[Submission],
    provenance: CommentProvenance,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], tuple[str, ...], bool]:
    """Apply a reviewed batch in memory and return validated decision YAML data."""

    if (
        inventory_id != inventory.inventory_id
        or manifest["inventory_id"] != inventory_id
    ):
        raise CurationGitHubError(
            "Submission inventory id does not match the PR bundle"
        )
    entries = {str(entry["alias"]): entry for entry in manifest["candidates"]}
    candidates = {
        candidate.candidate_id: candidate for candidate in inventory.candidates
    }
    document = _deep_copy(decision_document)
    assert isinstance(document, dict)
    raw_decisions = document["decisions"]
    raw_transactions = document["transactions"]
    assert isinstance(raw_decisions, list) and isinstance(raw_transactions, list)
    existing_raw = {
        str(item["decision_id"]): item
        for item in raw_decisions
        if isinstance(item, dict) and "decision_id" in item
    }
    transactions = {
        str(item["inventory_id"]): item
        for item in raw_transactions
        if isinstance(item, dict) and "inventory_id" in item
    }
    transaction = transactions.get(inventory.inventory_id)
    if transaction is None:
        transaction = {"inventory_id": inventory.inventory_id, "decision_ids": []}
        raw_transactions.append(transaction)
    decision_ids = transaction["decision_ids"]
    if not isinstance(decision_ids, list):
        raise CurationGitHubError("Inventory transaction decision_ids must be a list")
    transaction_by_candidate: dict[str, str] = {}
    for identifier in decision_ids:
        decision = decision_book.decisions.get(identifier)
        if decision is None:
            raise CurationGitHubError(
                "Inventory transaction contains an unknown decision"
            )
        transaction_by_candidate[decision.candidate_id] = identifier
    applied: list[str] = []
    changed = False
    for submission in submissions:
        try:
            entry = entries[submission.alias]
        except KeyError as error:
            raise CurationGitHubError(
                f"Unknown candidate alias for this inventory: {submission.alias}"
            ) from error
        candidate = candidates[str(entry["candidate_id"])]
        if submission.disposition == "accept" and candidate.blockers:
            raise CurationGitHubError(
                f"Blocked candidate {submission.alias} cannot be accepted: "
                + ", ".join(candidate.blockers)
            )
        evidence = list(submission.evidence)
        if provenance.comment_url not in evidence:
            evidence.append(provenance.comment_url)
        event = cli.render_decision(
            core,
            candidate,
            supersedes_decision_id=submission.expected_decision,
            disposition=submission.disposition,
            reviewer=provenance.reviewer,
            decided_on=provenance.decided_on,
            rationale=submission.rationale,
            evidence=evidence,
            details=submission.details,
        )
        current = decision_book.active(candidate.candidate_id)
        current_id = None if current is None else current.decision_id
        event_id = str(event["decision_id"])
        replay = (
            current_id == event_id
            and existing_raw.get(event_id) == event
            and transaction_by_candidate.get(candidate.candidate_id) == event_id
        )
        if replay:
            applied.append(event_id)
            continue
        if current_id != submission.expected_decision:
            raise CurationGitHubError(
                f"Stale expected_decision for {submission.alias}: "
                f"expected {current_id!r}"
            )
        if event_id in existing_raw:
            raise CurationGitHubError(
                "Decision identity already exists with another state"
            )
        raw_decisions.append(event)
        existing_raw[event_id] = event
        previous_transaction_id = transaction_by_candidate.get(candidate.candidate_id)
        if previous_transaction_id is not None:
            decision_ids.remove(previous_transaction_id)
        decision_ids.append(event_id)
        transaction_by_candidate[candidate.candidate_id] = event_id
        applied.append(event_id)
        changed = True
        # Reparse after each event so later validation observes the exact active chain.
        decision_book = core.parse_decisions(yaml.safe_dump(document, sort_keys=False))
    core.parse_decisions(yaml.safe_dump(document, sort_keys=False))
    return document, tuple(applied), changed


def _atomic_yaml(
    path: Path, value: Mapping[str, object], expected: bytes | None
) -> None:
    payload = yaml.safe_dump(
        dict(value),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.github-curation.lock")
    try:
        lock_descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise CurationGitHubError(f"Another review update holds {lock.name}") from error
    try:
        os.close(lock_descriptor)
        observed = path.read_bytes() if path.exists() else None
        if observed != expected:
            raise CurationGitHubError("Decision file changed during comment processing")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        lock.unlink(missing_ok=True)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    if path.exists() and path.read_bytes() == payload:
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_bundle(
    root: Path,
    adapter: str,
    manifest_value: str | Path,
    review_value: str | Path,
    *,
    core: ModuleType,
) -> tuple[Mapping[str, object], str, object, object, Path, Path]:
    manifest_path = _scoped_path(
        root,
        adapter,
        manifest_value,
        area="transactions",
        suffix=".yaml",
        label="manifest",
        must_exist=True,
    )
    if not manifest_path.name.endswith(".review-manifest.yaml"):
        raise CurationGitHubError(
            "manifest filename must end with .review-manifest.yaml"
        )
    review_path = _scoped_path(
        root,
        adapter,
        review_value,
        area="transactions",
        suffix=".md",
        label="review",
        must_exist=True,
    )
    if not review_path.name.endswith(".review.md"):
        raise CurationGitHubError("review filename must end with .review.md")
    manifest = _mapping(
        _strict_yaml(manifest_path.read_text(encoding="utf-8"), "manifest YAML"),
        "manifest",
    )
    paths = _mapping(manifest.get("paths"), "manifest paths")
    if paths.get("manifest") != _normalized_relative(
        root, manifest_path, "manifest"
    ) or paths.get("review") != _normalized_relative(root, review_path, "review"):
        raise CurationGitHubError("Manifest path binding is stale")
    inventory_path = _scoped_path(
        root,
        adapter,
        str(paths.get("inventory")),
        area="transactions",
        suffix=".yaml",
        label="inventory",
        must_exist=True,
    )
    decisions_path = _scoped_path(
        root,
        adapter,
        str(paths.get("decisions")),
        area="policy",
        suffix=".yaml",
        label="decisions",
        must_exist=False,
    )
    inventory = core.load_inventory(inventory_path)
    decision_text = (
        decisions_path.read_text(encoding="utf-8") if decisions_path.exists() else None
    )
    _, decisions = _decision_document(core, decision_text)
    review = review_path.read_text(encoding="utf-8")
    validate_manifest(core, inventory, decisions, manifest, review)
    return manifest, review, inventory, decisions, inventory_path, decisions_path


def select_review_bundle(
    root: Path,
    pull_request: Mapping[str, object],
    changes: object,
    *,
    core: ModuleType,
    phase: str,
) -> dict[str, object]:
    """Select the one manifest named by the PR marker and allowed diff."""

    root = root.resolve()
    changed = _mapping(changes, "changed paths")
    files = changed.get("files")
    if not isinstance(files, list):
        raise CurationGitHubError("changed paths files must be a list")
    manifest_paths = sorted(
        str(item.get("path"))
        for item in files
        if isinstance(item, Mapping)
        and isinstance(item.get("path"), str)
        and str(item["path"]).endswith(".review-manifest.yaml")
    )
    if len(manifest_paths) != 1:
        raise CurationGitHubError("Curation PR must change exactly one review manifest")
    parts = PurePosixPath(manifest_paths[0]).parts
    if len(parts) < 4 or parts[0] != "source-adapters" or parts[2] != "transactions":
        raise CurationGitHubError("Changed review manifest is outside adapter scope")
    adapter = parts[1]
    if adapter not in ALIAS_PREFIXES:
        raise CurationGitHubError("Changed review manifest uses an unsupported adapter")
    manifest_path = _scoped_path(
        root,
        adapter,
        manifest_paths[0],
        area="transactions",
        suffix=".yaml",
        label="manifest",
        must_exist=True,
    )
    raw_manifest = _mapping(
        _strict_yaml(manifest_path.read_text(encoding="utf-8"), "manifest YAML"),
        "manifest",
    )
    paths = _mapping(raw_manifest.get("paths"), "manifest paths")
    manifest, _, inventory, _, _, _ = _load_bundle(
        root,
        adapter,
        manifest_paths[0],
        str(paths.get("review")),
        core=core,
    )
    selected_paths = validate_allowed_changes(manifest, changes, phase=phase)
    validate_datalad_sidecars(root, selected_paths, manifest=manifest, phase=phase)
    body = pull_request.get("body")
    marker = pr_body_marker(manifest)
    if (
        not isinstance(body, str)
        or body.count(marker) != 1
        or body.count(_MARKER_START) != 1
    ):
        raise CurationGitHubError(
            "Pull-request body does not select the changed review manifest"
        )
    return {
        "adapter_id": adapter,
        "bundle_digest": manifest["bundle_digest"],
        "decisions": paths["decisions"],
        "inventory": paths["inventory"],
        "inventory_id": inventory.inventory_id,
        "manifest": paths["manifest"],
        "pr_body_marker": marker,
        "review": paths["review"],
    }


def _trusted_base_decisions(
    core: ModuleType, manifest: Mapping[str, object], adapter_id: str
):
    """Load proposal-time decision authority only from the trusted base tree."""

    paths = _mapping(manifest["paths"], "manifest paths")
    decision_path = _scoped_path(
        TRUSTED_ROOT,
        adapter_id,
        str(paths["decisions"]),
        area="policy",
        suffix=".yaml",
        label="trusted base decisions",
        must_exist=False,
    )
    if decision_path.exists():
        decisions = core.load_decisions(decision_path)
    else:
        _, decisions = _decision_document(core, None)
    for raw in manifest["candidates"]:
        entry = _mapping(raw, "manifest candidate")
        active = decisions.active(str(entry["candidate_id"]))
        active_id = None if active is None else active.decision_id
        if entry["expected_decision"] != active_id:
            raise CurationGitHubError(
                "Manifest expected decision differs from trusted base authority"
            )
    return decisions


def validate_attestation_chain(
    root: Path,
    core: ModuleType,
    manifest: Mapping[str, object],
    pull_request: Mapping[str, object],
    changes: object,
    comments: object,
    runs: object,
    *,
    default_branch: str,
    trusted_default_sha: str,
) -> dict[str, object]:
    """Authenticate proposal bytes and the unique successful ledger-head chain."""

    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    paths = _mapping(manifest["paths"], "manifest paths")
    repository = str(coordinates["repository"])
    number = pull_request.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise CurationGitHubError("Pull-request number is invalid")
    trusted_default_sha = _sha(trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError("Attestation base differs from trusted default")
    head = _mapping(pull_request.get("head"), "pull_request.head")
    current_head = _sha(head.get("sha"), "pull_request.head.sha")
    markers = parse_attestation_comments(
        comments, repository=repository, pull_request_number=number
    )
    if not markers:
        raise CurationGitHubError("Curation PR lacks its bot proposal receipt")
    run_index = _attestation_run_index(runs)
    marker_run_keys = {
        (
            str(_mapping(item["payload"], "attestation payload")["workflow_run_id"]),
            int(
                _mapping(item["payload"], "attestation payload")["workflow_run_attempt"]
            ),
        )
        for item in markers
    }
    if set(run_index) != marker_run_keys:
        raise CurationGitHubError(
            "Attestation run responses do not exactly cover marker run attempts"
        )
    receipts = [
        item
        for item in markers
        if _mapping(item["payload"], "attestation payload").get("format")
        == PROPOSAL_RECEIPT_FORMAT
    ]
    if len(receipts) != 1:
        raise CurationGitHubError("Curation PR requires exactly one proposal receipt")
    receipt_item = receipts[0]
    receipt = _mapping(receipt_item["payload"], "proposal receipt")
    if receipt.get("workflow_run_id") != coordinates["run_id"]:
        raise CurationGitHubError("Proposal receipt names another workflow run")
    receipt_files = _mapping(receipt["files"], "proposal receipt files")
    sidecar_binding = _mapping(receipt_files["sidecar"], "proposal receipt sidecar")
    sidecar_path = str(sidecar_binding["path"])
    changed_document = _mapping(changes, "changed paths")
    changed_files = changed_document.get("files")
    if not isinstance(changed_files, list):
        raise CurationGitHubError("changed paths files must be a list")
    changed_names = {
        str(item.get("path"))
        for item in changed_files
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    if sidecar_path not in changed_names:
        raise CurationGitHubError("Proposal receipt sidecar is absent from the PR diff")
    receipt_pr = dict(pull_request)
    receipt_pr["head"] = {
        **dict(head),
        "sha": _sha(receipt["proposal_head_sha"], "receipt proposal_head_sha"),
    }
    expected_receipt = proposal_receipt_payload(
        root,
        manifest,
        receipt_pr,
        sidecar_path,
        workflow_run_attempt=int(receipt["workflow_run_attempt"]),
    )
    if dict(receipt) != expected_receipt:
        raise CurationGitHubError(
            "Proposal receipt differs from immutable proposal bytes"
        )
    receipt_run_key = (
        str(receipt["workflow_run_id"]),
        int(receipt["workflow_run_attempt"]),
    )
    proposal_run = run_index[receipt_run_key]
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=default_branch,
        require_success=False,
        allow_unsuccessful=True,
    )
    _validate_attestation_run(
        proposal_run,
        run_id=receipt_run_key[0],
        run_attempt=receipt_run_key[1],
        repository=repository,
        default_branch=default_branch,
        trusted_default_sha=trusted_default_sha,
        event_name="workflow_dispatch",
        comment_created_at=str(receipt_item["created_at"]),
    )

    expected_common = {
        "repository": repository,
        "pull_request": number,
        "adapter_id": manifest["adapter_id"],
        "inventory_id": manifest["inventory_id"],
        "bundle_digest": manifest["bundle_digest"],
        "manifest": paths["manifest"],
    }
    raw_edges: list[tuple[Mapping[str, object], bool]] = []
    ledger_run_keys: set[tuple[str, int]] = set()
    for item in markers:
        payload = _mapping(item["payload"], "attestation payload")
        if payload["format"] != LEDGER_ATTESTATION_FORMAT:
            continue
        for field, expected in expected_common.items():
            if payload.get(field) != expected:
                raise CurationGitHubError(
                    f"Ledger attestation {field} differs from the selected bundle"
                )
        if payload.get("decisions") != paths["decisions"]:
            raise CurationGitHubError("Ledger attestation decision path is stale")
        run_id = str(payload["workflow_run_id"])
        run_attempt = int(payload["workflow_run_attempt"])
        run_key = (run_id, run_attempt)
        if run_key in ledger_run_keys:
            raise CurationGitHubError(
                "One workflow run attempt produced multiple attestations"
            )
        ledger_run_keys.add(run_key)
        successful = _validate_attestation_run(
            run_index[run_key],
            run_id=run_id,
            run_attempt=run_attempt,
            repository=repository,
            default_branch=default_branch,
            trusted_default_sha=trusted_default_sha,
            event_name="issue_comment",
            comment_created_at=str(item["created_at"]),
        )
        raw_edges.append((payload, successful))

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for edge, successful in raw_edges:
        key = (str(edge["parent_head_sha"]), str(edge["target_head_sha"]))
        group = grouped.setdefault(
            key,
            {"payload": edge, "successful": False, "digests": set()},
        )
        digests = group["digests"]
        assert isinstance(digests, set)
        digests.add(str(edge["ledger_sha256"]))
        group["successful"] = bool(group["successful"]) or successful
    for group in grouped.values():
        digests = group["digests"]
        assert isinstance(digests, set)
        if len(digests) != 1:
            raise CurationGitHubError(
                "Parallel ledger attestations disagree about ledger content"
            )

    adjacency: dict[str, list[tuple[str, str]]] = {}
    for key in grouped:
        adjacency.setdefault(key[0], []).append(key)
    for edges in adjacency.values():
        edges.sort()
    root_head = str(receipt["proposal_head_sha"])
    paths_to_current: list[list[tuple[str, str]]] = []
    stack: list[tuple[str, list[tuple[str, str]], frozenset[str]]] = [
        (root_head, [], frozenset({root_head}))
    ]
    while stack:
        head_value, path, seen = stack.pop()
        if head_value == current_head:
            paths_to_current.append(path)
            if len(paths_to_current) > 1:
                break
            continue
        for edge_key in reversed(adjacency.get(head_value, [])):
            target = edge_key[1]
            if target not in seen:
                stack.append((target, [*path, edge_key], seen | {target}))
    if len(paths_to_current) != 1:
        raise CurationGitHubError(
            "Current PR head does not have exactly one authenticated attestation path"
        )
    installed_path = paths_to_current[0]
    installed_edges = set(installed_path)
    successful_edges = {
        key for key, group in grouped.items() if bool(group["successful"])
    }
    if not successful_edges.issubset(installed_edges):
        raise CurationGitHubError(
            "Successful attestation history is not on the current installed path"
        )
    visited = [
        _mapping(grouped[key]["payload"], "ledger attestation")
        for key in installed_path
    ]
    tip = current_head

    _trusted_base_decisions(core, manifest, str(manifest["adapter_id"]))
    trusted_path = _scoped_path(
        TRUSTED_ROOT,
        str(manifest["adapter_id"]),
        str(paths["decisions"]),
        area="policy",
        suffix=".yaml",
        label="trusted base decisions",
        must_exist=False,
    )
    current_path = _scoped_path(
        root,
        str(manifest["adapter_id"]),
        str(paths["decisions"]),
        area="policy",
        suffix=".yaml",
        label="PR decisions",
        must_exist=False,
    )
    trusted_bytes = trusted_path.read_bytes() if trusted_path.exists() else None
    current_bytes = current_path.read_bytes() if current_path.exists() else None
    if visited:
        if current_bytes is None or visited[-1]["ledger_sha256"] != _digest(
            current_bytes
        ):
            raise CurationGitHubError(
                "Current decision ledger differs from its installed attestation"
            )
        authority = "installed-ledger-attestation"
        ledger_digest = _digest(current_bytes)
    else:
        if current_bytes != trusted_bytes:
            raise CurationGitHubError(
                "First submission decision ledger is not byte-identical to trusted base"
            )
        authority = "trusted-base-ledger"
        ledger_digest = None if current_bytes is None else _digest(current_bytes)
    return {
        "attestation_authority": authority,
        "attestation_tip": tip,
        "ledger_sha256": ledger_digest,
        "proposal_head_sha": receipt["proposal_head_sha"],
        "installed_ledger_attestations": len(visited),
        "successful_ledger_attestations": len(
            successful_edges.intersection(installed_edges)
        ),
    }


def regenerate_inventory(
    authority_root: Path,
    core: ModuleType,
    cli: ModuleType,
    provider: ModuleType,
    inventory,
    proposal_decisions,
    manifest: Mapping[str, object],
    *,
    output: Path,
) -> dict[str, object]:
    """Regenerate an inventory using trusted code and manifest-bound inputs."""

    inputs = _mapping(
        _mapping(manifest["proposal_coordinates"], "proposal_coordinates")[
            "source_inputs"
        ],
        "source_inputs",
    )
    _only_fields(
        inputs,
        {"evaluation_context", "implementation", "policy", "source"},
        "source_inputs",
    )
    evaluation = _mapping(inputs["evaluation_context"], "evaluation_context")
    _only_fields(
        evaluation,
        {"as_of", "resolved_policy_questions"},
        "evaluation_context",
    )
    as_of_value = _nonempty_line(evaluation["as_of"], "evaluation_context.as_of")
    try:
        as_of = date.fromisoformat(as_of_value)
    except ValueError as error:
        raise CurationGitHubError(
            "evaluation_context.as_of is not an ISO date"
        ) from error
    questions = evaluation["resolved_policy_questions"]
    if not isinstance(questions, list) or not all(
        isinstance(item, str) and item for item in questions
    ):
        raise CurationGitHubError(
            "evaluation_context.resolved_policy_questions must be a string list"
        )
    source = _mapping(inputs["source"], "source_inputs.source")
    expected_library_version = None
    source_path = None
    expected_source_commit = None
    if inventory.adapter_id == "dump-research-info":
        source_path = _nonempty_line(source.get("path"), "source_inputs.source.path")
        expected_source_commit = _sha(
            source.get("commit"), "source_inputs.source.commit"
        )
        if len(expected_source_commit) != 40:
            raise CurationGitHubError("Dump source commit must contain 40 hex digits")
    elif inventory.adapter_id == "zotero":
        expected_library_version = source.get("library_version")
        if isinstance(expected_library_version, bool) or not isinstance(
            expected_library_version, int
        ):
            raise CurationGitHubError(
                "Zotero source library_version must be an integer"
            )
    else:
        raise CurationGitHubError("Inventory adapter is unsupported")
    result = cli._provider_result(
        provider,
        adapter_id=inventory.adapter_id,
        root=authority_root,
        output=output,
        expected_library_version=expected_library_version,
        source_path=source_path,
        expected_source_commit=expected_source_commit,
        source_run_id=None,
    )
    return core.build_inventory(
        inventory.adapter_id,
        result["candidates"],
        proposal_decisions,
        context=core.EvaluationContext(
            as_of=as_of,
            resolved_policy_questions=frozenset(questions),
        ),
        metadata_dir=authority_root / "metadata/records",
        inputs={
            "source": dict(result["source"]),
            "policy": dict(result["policy"]),
            "implementation": dict(result["implementation"]),
        },
    )


def validate_reproposal(
    root: Path,
    core: ModuleType,
    cli: ModuleType,
    inventory,
    decisions,
    manifest: Mapping[str, object],
    inventory_path: Path,
    *,
    provider: ModuleType | None = None,
) -> dict[str, object]:
    """Regenerate with trusted code and byte-compare the authoritative inventory."""

    authority_root = TRUSTED_ROOT.resolve()
    proposal_decisions = _trusted_base_decisions(core, manifest, inventory.adapter_id)
    provider = provider or _trusted_provider(inventory.adapter_id, core)
    scratch_parent = authority_root / "build/curation" / inventory.adapter_id
    _normalized_relative(authority_root, scratch_parent, "re-proposal scratch")
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=scratch_parent, prefix=".trusted-reproposal-"
    ) as temporary:
        regenerated = regenerate_inventory(
            authority_root,
            core,
            cli,
            provider,
            inventory,
            proposal_decisions,
            manifest,
            output=Path(temporary),
        )
    expected = core._dump_yaml(regenerated).encode("utf-8")
    observed = inventory_path.read_bytes()
    if expected != observed:
        raise CurationGitHubError(
            "Trusted re-proposal is not byte-identical to the authoritative inventory"
        )
    if regenerated != inventory.to_mapping():
        raise CurationGitHubError("Trusted re-proposal mapping differs from inventory")
    return {
        "byte_count": len(observed),
        "inventory_id": inventory.inventory_id,
        "inventory_sha256": _digest(observed),
        "reproposal_valid": True,
    }


def render_review_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    adapter = arguments.adapter
    inventory_path = _scoped_path(
        root,
        adapter,
        arguments.inventory,
        area="transactions",
        suffix=".yaml",
        label="inventory",
        must_exist=True,
    )
    decisions_path = _scoped_path(
        root,
        adapter,
        arguments.decisions,
        area="policy",
        suffix=".yaml",
        label="decisions",
        must_exist=False,
    )
    manifest_path = _scoped_path(
        root,
        adapter,
        arguments.manifest,
        area="transactions",
        suffix=".yaml",
        label="manifest",
        must_exist=False,
    )
    if not manifest_path.name.endswith(".review-manifest.yaml"):
        raise CurationGitHubError(
            "manifest filename must end with .review-manifest.yaml"
        )
    review_path = _scoped_path(
        root,
        adapter,
        arguments.review,
        area="transactions",
        suffix=".md",
        label="review",
        must_exist=False,
    )
    if not review_path.name.endswith(".review.md"):
        raise CurationGitHubError("review filename must end with .review.md")
    inventory = core.load_inventory(inventory_path)
    if inventory.adapter_id != adapter:
        raise CurationGitHubError("Inventory belongs to another adapter")
    decision_text = (
        decisions_path.read_text(encoding="utf-8") if decisions_path.exists() else None
    )
    _, decisions = _decision_document(core, decision_text)
    manifest, review = build_review_bundle(
        core,
        inventory,
        decisions,
        inventory_path=_normalized_relative(root, inventory_path, "inventory"),
        decisions_path=_normalized_relative(root, decisions_path, "decisions"),
        manifest_path=_normalized_relative(root, manifest_path, "manifest"),
        review_path=_normalized_relative(root, review_path, "review"),
        base_sha=arguments.base_sha,
        head_sha=arguments.head_sha,
        public_data_actor=arguments.public_data_actor,
        public_data_at=arguments.public_data_at,
        public_data_run_url=arguments.public_data_run_url,
    )
    manifest_text = yaml.safe_dump(
        manifest, sort_keys=False, allow_unicode=True, width=1000
    )
    _atomic_text(review_path, review)
    _atomic_text(manifest_path, manifest_text)
    return {
        "adapter_id": adapter,
        "bundle_digest": manifest["bundle_digest"],
        "candidate_count": len(inventory.candidates),
        "inventory_id": inventory.inventory_id,
        "manifest": _normalized_relative(root, manifest_path, "manifest"),
        "no_op": not inventory.candidates,
        "pr_body_marker": pr_body_marker(manifest),
        "review": _normalized_relative(root, review_path, "review"),
    }


def render_proposal_receipt_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    selected = select_review_bundle(
        root, pull_request, changes, core=core, phase="initial"
    )
    if selected["adapter_id"] != arguments.adapter or selected["manifest"] != str(
        arguments.manifest
    ):
        raise CurationGitHubError("Explicit receipt bundle is not PR-selected")
    manifest, _, _, _, _, _ = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError("Proposal receipt base differs from trusted default")
    proposal_run = _json_file(arguments.proposal_run_json, "proposal-run JSON")
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=arguments.default_branch,
        require_success=False,
    )
    head = _mapping(pull_request.get("head"), "pull_request.head")
    base = _mapping(pull_request.get("base"), "pull_request.base")
    head_repo = _mapping(head.get("repo"), "pull_request.head.repo")
    base_repo = _mapping(base.get("repo"), "pull_request.base.repo")
    expected_branch = f"{BRANCH_PREFIX}{manifest['adapter_id']}-{coordinates['run_id']}"
    if (
        pull_request.get("state") != "open"
        or _github_user(pull_request.get("user"), "pull_request.user")
        != (PROPOSAL_BOT_ID, PROPOSAL_BOT_LOGIN)
        or head.get("ref") != expected_branch
        or base.get("ref") != arguments.default_branch
        or base.get("sha") != trusted_default_sha
        or head_repo.get("full_name") != coordinates["repository"]
        or base_repo.get("full_name") != coordinates["repository"]
    ):
        raise CurationGitHubError(
            "Proposal receipt pull-request coordinates are invalid"
        )
    changed = _mapping(changes, "changed paths")
    sidecars = [
        str(item.get("path"))
        for item in changed["files"]
        if isinstance(item, Mapping)
        and isinstance(item.get("path"), str)
        and str(item["path"]).startswith(".datalad/runinfo/")
    ]
    if len(sidecars) != 1:
        raise CurationGitHubError("Proposal receipt requires one DataLad sidecar")
    payload = proposal_receipt_payload(
        root,
        manifest,
        pull_request,
        sidecars[0],
        workflow_run_attempt=_positive_integer(
            proposal_run.get("run_attempt"), "proposal run.run_attempt"
        ),
    )
    marker = _attestation_marker(payload)
    return {
        "body": _attestation_body(
            f"Proposal receipt for `{payload['proposal_head_sha']}`.", payload
        ),
        "marker": marker,
        "proposal_head_sha": payload["proposal_head_sha"],
        "run_id": payload["workflow_run_id"],
        "run_attempt": payload["workflow_run_attempt"],
        "sidecar_sha256": payload["files"]["sidecar"]["sha256"],
    }


def render_ledger_attestation_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    manifest, _, _, _, _, _ = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError(
            "Ledger attestation base differs from trusted default"
        )
    run = _json_file(arguments.workflow_run_json, "workflow-run JSON")
    run_id = str(_positive_integer(run.get("id"), "workflow run.id"))
    run_attempt = _positive_integer(run.get("run_attempt"), "workflow run.run_attempt")
    run_created_at = _timestamp(run.get("created_at"), "workflow run.created_at")
    _validate_attestation_run(
        run,
        run_id=run_id,
        run_attempt=run_attempt,
        repository=str(coordinates["repository"]),
        default_branch=arguments.default_branch,
        trusted_default_sha=trusted_default_sha,
        event_name="issue_comment",
        comment_created_at=run_created_at,
    )
    payload = ledger_attestation_payload(
        root,
        manifest,
        pull_request,
        workflow_run_id=run_id,
        workflow_run_attempt=run_attempt,
        parent_head_sha=arguments.parent_head_sha,
        target_head_sha=arguments.target_head_sha,
    )
    marker = _attestation_marker(payload)
    return {
        "body": _attestation_body(
            f"Decision ledger checkpoint for `{payload['target_head_sha']}`.",
            payload,
        ),
        "ledger_sha256": payload["ledger_sha256"],
        "marker": marker,
        "parent_head_sha": payload["parent_head_sha"],
        "run_id": payload["workflow_run_id"],
        "run_attempt": payload["workflow_run_attempt"],
        "target_head_sha": payload["target_head_sha"],
    }


def list_attestation_run_ids_files(arguments) -> dict[str, object]:
    comments = _json_value_file(arguments.comments_json, "attestation comments JSON")
    run_attempts = attestation_run_ids(
        comments,
        repository=arguments.repository,
        pull_request_number=arguments.pull_request_number,
    )
    return {
        "attestation_count": len(run_attempts),
        "run_ids": sorted({run_id for run_id, _ in run_attempts}, key=int),
        "runs": [
            {"run_id": run_id, "run_attempt": attempt}
            for run_id, attempt in run_attempts
        ],
    }


def validate_guard_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    if arguments.phase not in {"initial", "reviewed"}:
        raise CurationGitHubError(
            "Finalization is terminal; close and restart the curation transaction"
        )
    manifest, _, inventory, _, _, _ = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    if arguments.hosted_command == "finalize" and arguments.phase != "reviewed":
        raise CurationGitHubError(
            "Finalization requires a reviewed transaction and is terminal"
        )
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    selected = select_review_bundle(
        root, pull_request, changes, core=core, phase=arguments.phase
    )
    if selected["manifest"] != _normalized_relative(
        root,
        _scoped_path(
            root,
            arguments.adapter,
            arguments.manifest,
            area="transactions",
            suffix=".yaml",
            label="manifest",
            must_exist=True,
        ),
        "manifest",
    ):
        raise CurationGitHubError("Explicit manifest is not the PR-selected manifest")
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError(
            "Manifest proposal base differs from the trusted default checkout"
        )
    event = _json_file(arguments.event_json, "event JSON")
    event_repository = _mapping(event.get("repository"), "event.repository")
    if event_repository.get("default_branch") != arguments.default_branch:
        raise CurationGitHubError("Event default branch differs from CLI authority")
    proposal_run = _json_file(arguments.proposal_run_json, "proposal-run JSON")
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=arguments.default_branch,
        require_success=False,
        allow_unsuccessful=True,
    )
    attestation = validate_attestation_chain(
        root,
        core,
        manifest,
        pull_request,
        changes,
        _json_value_file(arguments.comments_json, "attestation comments JSON"),
        _json_value_file(arguments.attestation_runs_json, "attestation runs JSON"),
        default_branch=arguments.default_branch,
        trusted_default_sha=trusted_default_sha,
    )
    permission = _json_file(arguments.permission_json, "permission JSON")
    provenance = validate_hosted_guard(
        event,
        pull_request,
        permission,
        expected_marker=pr_body_marker(manifest),
        expected_base_sha=trusted_default_sha,
        expected_branch=(f"{BRANCH_PREFIX}{arguments.adapter}-{coordinates['run_id']}"),
        expected_repository=str(coordinates["repository"]),
        command=arguments.hosted_command,
    )
    return {
        **attestation,
        "actor": provenance.reviewer,
        "comment_url": provenance.comment_url,
        "command": arguments.hosted_command,
        "guard_valid": True,
        "inventory_id": inventory.inventory_id,
        "phase": arguments.phase,
    }


def apply_comment_files(
    arguments, core: ModuleType, cli: ModuleType
) -> dict[str, object]:
    root = arguments.root.resolve()
    if arguments.phase not in {"initial", "reviewed"}:
        raise CurationGitHubError(
            "Finalization is terminal; close and restart the curation transaction"
        )
    manifest, _, inventory, decisions, inventory_path, decisions_path = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    if _normalized_relative(root, inventory_path, "inventory") != str(
        arguments.inventory
    ) or _normalized_relative(root, decisions_path, "decisions") != str(
        arguments.decisions
    ):
        raise CurationGitHubError(
            "CLI inventory or decisions path differs from manifest"
        )
    event = _json_file(arguments.event_json, "event JSON")
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    permission = _json_file(arguments.permission_json, "permission JSON")
    event_repository = _mapping(event.get("repository"), "event.repository")
    proposal_run = _json_file(arguments.proposal_run_json, "proposal-run JSON")
    if event_repository.get("default_branch") != arguments.default_branch:
        raise CurationGitHubError("Event default branch differs from CLI authority")
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=arguments.default_branch,
        require_success=False,
        allow_unsuccessful=True,
    )
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    selected = select_review_bundle(
        root, pull_request, changes, core=core, phase=arguments.phase
    )
    if selected["manifest"] != _normalized_relative(
        root, root / str(arguments.manifest), "manifest"
    ):
        raise CurationGitHubError("Explicit manifest is not the PR-selected manifest")
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError(
            "Manifest proposal base differs from the trusted default checkout"
        )
    attestation = validate_attestation_chain(
        root,
        core,
        manifest,
        pull_request,
        changes,
        _json_value_file(arguments.comments_json, "attestation comments JSON"),
        _json_value_file(arguments.attestation_runs_json, "attestation runs JSON"),
        default_branch=arguments.default_branch,
        trusted_default_sha=trusted_default_sha,
    )
    provenance = validate_hosted_guard(
        event,
        pull_request,
        permission,
        expected_marker=pr_body_marker(manifest),
        expected_base_sha=trusted_default_sha,
        expected_branch=(f"{BRANCH_PREFIX}{arguments.adapter}-{coordinates['run_id']}"),
        expected_repository=str(coordinates["repository"]),
        command="submit",
    )
    comment = _mapping(event.get("comment"), "event.comment")
    inventory_id, submissions = parse_submission_comment(comment.get("body"))
    before = decisions_path.read_bytes() if decisions_path.exists() else None
    document, applied, changed = apply_submission(
        core,
        cli,
        inventory,
        _decision_document(core, None if before is None else before.decode("utf-8"))[0],
        decisions,
        inventory_id=inventory_id,
        submissions=submissions,
        provenance=provenance,
        manifest=manifest,
    )
    if changed:
        _atomic_yaml(decisions_path, document, before)
    updated = core.parse_decisions(yaml.safe_dump(document, sort_keys=False))
    transaction = updated.transactions.get(inventory.inventory_id, ())
    reviewed_candidates = {
        updated.decisions[identifier].candidate_id for identifier in transaction
    }
    aliases = {
        str(entry["candidate_id"]): str(entry["alias"])
        for entry in manifest["candidates"]
    }
    remaining = [
        aliases[item.candidate_id]
        for item in inventory.candidates
        if item.candidate_id not in reviewed_candidates
    ]
    correction_items = [
        {
            "candidate": aliases[updated.decisions[identifier].candidate_id],
            "expected_decision": identifier,
        }
        for identifier in applied
    ]
    return {
        **attestation,
        "applied_decision_ids": list(applied),
        "candidate_count": len(inventory.candidates),
        "changed": changed,
        "comment_url": provenance.comment_url,
        "complete": not remaining,
        "correction_items": correction_items,
        "inventory_id": inventory.inventory_id,
        "remaining_aliases": remaining,
        "reviewed_count": len(reviewed_candidates),
    }


def validate_complete_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    if arguments.phase != "reviewed":
        raise CurationGitHubError(
            "Finalization is terminal; close and restart the curation transaction"
        )
    manifest, _, inventory, decisions, inventory_path, decisions_path = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    if _normalized_relative(root, inventory_path, "inventory") != str(
        arguments.inventory
    ) or _normalized_relative(root, decisions_path, "decisions") != str(
        arguments.decisions
    ):
        raise CurationGitHubError(
            "CLI inventory or decisions path differs from manifest"
        )
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    selected = select_review_bundle(
        root, pull_request, changes, core=core, phase=arguments.phase
    )
    if selected["manifest"] != _normalized_relative(
        root, root / str(arguments.manifest), "manifest"
    ):
        raise CurationGitHubError("Explicit manifest is not the PR-selected manifest")
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError(
            "Manifest proposal base differs from the trusted default checkout"
        )
    base = _mapping(pull_request.get("base"), "pull_request.base")
    if base.get("sha") != trusted_default_sha:
        raise CurationGitHubError("Pull-request base differs from trusted default")
    proposal_run = _json_file(arguments.proposal_run_json, "proposal-run JSON")
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=arguments.default_branch,
        require_success=False,
        allow_unsuccessful=True,
    )
    attestation = validate_attestation_chain(
        root,
        core,
        manifest,
        pull_request,
        changes,
        _json_value_file(arguments.comments_json, "attestation comments JSON"),
        _json_value_file(arguments.attestation_runs_json, "attestation runs JSON"),
        default_branch=arguments.default_branch,
        trusted_default_sha=trusted_default_sha,
    )
    current = core._validate_current_transaction(inventory, decisions)
    metadata_index = core.MetadataIndex.from_directory(
        root / "metadata/records", require_unique_pids=True
    )
    core._validate_current_relations(inventory, current, metadata_index)
    ordered_ids = [
        current[item.candidate_id].decision_id for item in inventory.candidates
    ]
    return {
        **attestation,
        "candidate_count": len(inventory.candidates),
        "complete": True,
        "decision_ids": ordered_ids,
        "inventory_id": inventory.inventory_id,
    }


def select_manifest_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    return select_review_bundle(
        root, pull_request, changes, core=core, phase=arguments.phase
    )


def validate_pr_tree_files(arguments, core: ModuleType) -> dict[str, object]:
    root = arguments.root.resolve()
    pull_request = _json_file(arguments.pr_json, "pull-request JSON")
    changes = _json_value_file(arguments.changed_paths_json, "changed-paths JSON")
    selected = select_review_bundle(
        root, pull_request, changes, core=core, phase=arguments.phase
    )
    manifest_path = root / str(selected["manifest"])
    manifest = _mapping(
        _strict_yaml(manifest_path.read_text(encoding="utf-8"), "manifest YAML"),
        "manifest",
    )
    paths = _mapping(manifest["paths"], "manifest paths")
    loaded_manifest, _, inventory, decisions, _, _ = _load_bundle(
        root,
        str(manifest["adapter_id"]),
        str(paths["manifest"]),
        str(paths["review"]),
        core=core,
    )
    if dict(loaded_manifest) != dict(manifest):
        raise CurationGitHubError("PR-selected manifest changed during validation")
    coordinates = _mapping(manifest["proposal_coordinates"], "proposal_coordinates")
    proposal_run = _json_file(arguments.proposal_run_json, "proposal-run JSON")
    validate_proposal_run(
        manifest,
        proposal_run,
        default_branch=arguments.default_branch,
        require_success=False,
        allow_unsuccessful=True,
    )
    trusted_default_sha = _sha(arguments.trusted_default_sha, "trusted_default_sha")
    if coordinates["base_sha"] != trusted_default_sha:
        raise CurationGitHubError(
            "Proposal base differs from the trusted default checkout"
        )
    if pull_request.get("state") != "open":
        raise CurationGitHubError("Curation pull request must be open")
    if _github_user(pull_request.get("user"), "pull_request.user") != (
        PROPOSAL_BOT_ID,
        PROPOSAL_BOT_LOGIN,
    ):
        raise CurationGitHubError("Curation PR was not created by the proposal bot")
    base = _mapping(pull_request.get("base"), "pull_request.base")
    head = _mapping(pull_request.get("head"), "pull_request.head")
    base_repo = _mapping(base.get("repo"), "pull_request.base.repo")
    head_repo = _mapping(head.get("repo"), "pull_request.head.repo")
    repository = str(coordinates["repository"])
    if (
        base.get("ref") != arguments.default_branch
        or base.get("sha") != trusted_default_sha
        or base_repo.get("full_name") != repository
        or head_repo.get("full_name") != repository
    ):
        raise CurationGitHubError("Pull-request repository or trusted base is invalid")
    expected_branch = f"{BRANCH_PREFIX}{manifest['adapter_id']}-{coordinates['run_id']}"
    if head.get("ref") != expected_branch:
        raise CurationGitHubError("Pull-request branch is not the proposal run branch")
    if arguments.phase == "reconciled":
        validate_reconciled_outputs(root, core, inventory, decisions, manifest)
    return {
        **selected,
        "phase": arguments.phase,
        "pr_tree_valid": True,
        "trusted_default_sha": trusted_default_sha,
    }


def validate_reproposal_files(
    arguments, core: ModuleType, cli: ModuleType
) -> dict[str, object]:
    root = arguments.root.resolve()
    tree = validate_pr_tree_files(arguments, core)
    if tree["adapter_id"] != arguments.adapter or tree["manifest"] != str(
        arguments.manifest
    ):
        raise CurationGitHubError(
            "Explicit re-proposal bundle is not the PR-selected manifest"
        )
    manifest, _, inventory, decisions, inventory_path, _ = _load_bundle(
        root, arguments.adapter, arguments.manifest, arguments.review, core=core
    )
    result = validate_reproposal(
        root,
        core,
        cli,
        inventory,
        decisions,
        manifest,
        inventory_path,
    )
    return {**result, "pr_tree_valid": True, "phase": arguments.phase}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render-review")
    render.add_argument("--root", type=Path, default=Path.cwd())
    render.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    render.add_argument("--inventory", required=True)
    render.add_argument("--decisions", required=True)
    render.add_argument("--manifest", required=True)
    render.add_argument("--review", required=True)
    render.add_argument("--base-sha", required=True)
    render.add_argument("--head-sha", required=True)
    render.add_argument("--public-data-actor", required=True)
    render.add_argument("--public-data-at", required=True)
    render.add_argument("--public-data-run-url", required=True)

    receipt = commands.add_parser("render-proposal-receipt")
    receipt.add_argument("--root", type=Path, default=Path.cwd())
    receipt.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    receipt.add_argument("--manifest", required=True)
    receipt.add_argument("--review", required=True)
    receipt.add_argument("--pr-json", type=Path, required=True)
    receipt.add_argument("--changed-paths-json", type=Path, required=True)
    receipt.add_argument("--proposal-run-json", type=Path, required=True)
    receipt.add_argument("--default-branch", required=True)
    receipt.add_argument("--trusted-default-sha", required=True)

    ledger = commands.add_parser("render-ledger-attestation")
    ledger.add_argument("--root", type=Path, default=Path.cwd())
    ledger.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    ledger.add_argument("--manifest", required=True)
    ledger.add_argument("--review", required=True)
    ledger.add_argument("--pr-json", type=Path, required=True)
    ledger.add_argument("--workflow-run-json", type=Path, required=True)
    ledger.add_argument("--parent-head-sha", required=True)
    ledger.add_argument("--target-head-sha", required=True)
    ledger.add_argument("--default-branch", required=True)
    ledger.add_argument("--trusted-default-sha", required=True)

    run_ids = commands.add_parser("list-attestation-run-ids")
    run_ids.add_argument("--comments-json", type=Path, required=True)
    run_ids.add_argument("--repository", required=True)
    run_ids.add_argument("--pull-request-number", type=int, required=True)

    guard = commands.add_parser("validate-guard")
    guard.add_argument("--root", type=Path, default=Path.cwd())
    guard.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    guard.add_argument("--manifest", required=True)
    guard.add_argument("--review", required=True)
    guard.add_argument("--event-json", type=Path, required=True)
    guard.add_argument("--pr-json", type=Path, required=True)
    guard.add_argument("--permission-json", type=Path, required=True)
    guard.add_argument("--proposal-run-json", type=Path, required=True)
    guard.add_argument("--comments-json", type=Path, required=True)
    guard.add_argument("--attestation-runs-json", type=Path, required=True)
    guard.add_argument("--changed-paths-json", type=Path, required=True)
    guard.add_argument("--phase", choices=("initial", "reviewed"), required=True)
    guard.add_argument(
        "--hosted-command",
        choices=("submit", "finalize"),
        required=True,
    )
    guard.add_argument("--trusted-default-sha", required=True)
    guard.add_argument("--default-branch", required=True)

    apply = commands.add_parser("apply-comment")
    apply.add_argument("--root", type=Path, default=Path.cwd())
    apply.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    apply.add_argument("--inventory", required=True)
    apply.add_argument("--decisions", required=True)
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--review", required=True)
    apply.add_argument("--event-json", type=Path, required=True)
    apply.add_argument("--pr-json", type=Path, required=True)
    apply.add_argument("--permission-json", type=Path, required=True)
    apply.add_argument("--proposal-run-json", type=Path, required=True)
    apply.add_argument("--comments-json", type=Path, required=True)
    apply.add_argument("--attestation-runs-json", type=Path, required=True)
    apply.add_argument("--changed-paths-json", type=Path, required=True)
    apply.add_argument("--phase", choices=("initial", "reviewed"), required=True)
    apply.add_argument("--trusted-default-sha", required=True)
    apply.add_argument("--default-branch", required=True)

    complete = commands.add_parser("validate-complete")
    complete.add_argument("--root", type=Path, default=Path.cwd())
    complete.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    complete.add_argument("--inventory", required=True)
    complete.add_argument("--decisions", required=True)
    complete.add_argument("--manifest", required=True)
    complete.add_argument("--review", required=True)
    complete.add_argument("--pr-json", type=Path, required=True)
    complete.add_argument("--changed-paths-json", type=Path, required=True)
    complete.add_argument("--proposal-run-json", type=Path, required=True)
    complete.add_argument("--comments-json", type=Path, required=True)
    complete.add_argument("--attestation-runs-json", type=Path, required=True)
    complete.add_argument("--phase", choices=("reviewed",), required=True)
    complete.add_argument("--default-branch", required=True)
    complete.add_argument("--trusted-default-sha", required=True)

    select = commands.add_parser("select-manifest")
    select.add_argument("--root", type=Path, default=Path.cwd())
    select.add_argument("--pr-json", type=Path, required=True)
    select.add_argument("--changed-paths-json", type=Path, required=True)
    select.add_argument(
        "--phase",
        choices=("initial", "reviewed", "reconciled"),
        required=True,
    )

    tree = commands.add_parser("validate-pr-tree")
    tree.add_argument("--root", type=Path, default=Path.cwd())
    tree.add_argument("--pr-json", type=Path, required=True)
    tree.add_argument("--changed-paths-json", type=Path, required=True)
    tree.add_argument("--proposal-run-json", type=Path, required=True)
    tree.add_argument(
        "--phase",
        choices=("initial", "reviewed", "reconciled"),
        required=True,
    )
    tree.add_argument("--default-branch", required=True)
    tree.add_argument("--trusted-default-sha", required=True)

    reproposal = commands.add_parser("validate-reproposal")
    reproposal.add_argument("--root", type=Path, default=Path.cwd())
    reproposal.add_argument("--adapter", choices=tuple(ALIAS_PREFIXES), required=True)
    reproposal.add_argument("--manifest", required=True)
    reproposal.add_argument("--review", required=True)
    reproposal.add_argument("--pr-json", type=Path, required=True)
    reproposal.add_argument("--changed-paths-json", type=Path, required=True)
    reproposal.add_argument("--proposal-run-json", type=Path, required=True)
    reproposal.add_argument(
        "--phase",
        choices=("initial", "reviewed", "reconciled"),
        required=True,
    )
    reproposal.add_argument("--default-branch", required=True)
    reproposal.add_argument("--trusted-default-sha", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        core, cli = trusted_modules()
        if arguments.command == "render-review":
            output = render_review_files(arguments, core)
        elif arguments.command == "render-proposal-receipt":
            output = render_proposal_receipt_files(arguments, core)
        elif arguments.command == "render-ledger-attestation":
            output = render_ledger_attestation_files(arguments, core)
        elif arguments.command == "list-attestation-run-ids":
            output = list_attestation_run_ids_files(arguments)
        elif arguments.command == "validate-guard":
            output = validate_guard_files(arguments, core)
        elif arguments.command == "apply-comment":
            output = apply_comment_files(arguments, core, cli)
        elif arguments.command == "validate-complete":
            output = validate_complete_files(arguments, core)
        elif arguments.command == "select-manifest":
            output = select_manifest_files(arguments, core)
        elif arguments.command == "validate-pr-tree":
            output = validate_pr_tree_files(arguments, core)
        elif arguments.command == "validate-reproposal":
            output = validate_reproposal_files(arguments, core, cli)
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(arguments.command)
    except (CurationGitHubError, OSError, UnicodeError, ValueError) as error:
        print(f"curation GitHub prototype failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
