#!/usr/bin/env python3
"""Build and apply a compact, pull-request-native curation review.

The helper performs no GitHub or Git operations.  The workflow checks out a
trusted base and a pull-request tree, while this module builds source-adapter
candidates, renders the PR-description form, verifies the proposed metadata,
and records the submitted human decisions.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import html
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from types import ModuleType
from typing import Iterator, Mapping, Sequence

import yaml


FORM_FORMAT = "orinoco-lite-github-curation-form-v1"
CACHE_FORMAT = "orinoco-lite-curation-decisions-v1"
ATTRIBUTION = "**AI-generated draft — not reviewed by John**"
SUBMIT_COMMAND = "/curation submit"
ADAPTERS = ("dump-research-info", "zotero")
SOURCE_NAMESPACE = "https://github.com/con/dump-research-info"
_FORM_MARKER = "orinoco-lite-curation-form-v1"
_RECORD_MARKER = "orinoco-lite-curation-record-v1"
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORM_RE = re.compile(rf"<!--\s*{_FORM_MARKER}\s+(?P<payload>[A-Za-z0-9_-]+)\s*-->")
_RECORD_RE = re.compile(rf"<!--\s*{_RECORD_MARKER}\s+(?P<payload>[A-Za-z0-9_-]+)\s*-->")
_CHOICE_RE = re.compile(
    r"(?m)^- \[(?P<checked>[ xX])\] (?P<choice>Accept|Reject|Defer)[ \t]*$"
)


class CurationGitHubError(RuntimeError):
    """Reject ambiguous, stale, or unsafe hosted review state."""


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


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
class CandidateBuild:
    adapter: str
    source: Mapping[str, object]
    candidates: tuple[object, ...]


def _line(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurationGitHubError(f"{label} must be a non-empty string")
    if "\n" in value or "\r" in value or "\0" in value:
        raise CurationGitHubError(f"{label} must be one line")
    return value


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


def _b64_encode(value: object) -> str:
    return base64.urlsafe_b64encode(_canonical_bytes(value)).decode().rstrip("=")


def _b64_decode(value: str, label: str) -> object:
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise CurationGitHubError(f"Malformed {label}") from error
    if _b64_encode(decoded) != value:
        raise CurationGitHubError(f"Non-canonical {label}")
    return decoded


def _strict_yaml(text: str, label: str) -> object:
    try:
        if any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            for token in yaml.scan(text)
        ):
            raise CurationGitHubError(f"{label} may not use aliases")
        return yaml.load(text, Loader=_StrictSafeLoader)
    except CurationGitHubError:
        raise
    except yaml.YAMLError as error:
        raise CurationGitHubError(f"Malformed {label}: {error}") from error


def _yaml_bytes(value: object, *, sort_keys: bool = False) -> bytes:
    try:
        return yaml.safe_dump(
            value,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=sort_keys,
            width=1000,
        ).encode("utf-8")
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise CurationGitHubError(
            f"Value is not deterministic YAML: {error}"
        ) from error


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise CurationGitHubError(f"Cannot load trusted module: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _root(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise CurationGitHubError(f"{label} may not be a symbolic link")
    result = path.resolve()
    if not result.is_dir():
        raise CurationGitHubError(f"{label} is not a directory: {path}")
    return result


@contextmanager
def _provider_output(scratch: Path) -> Iterator[Path]:
    if scratch.is_symlink():
        raise CurationGitHubError("scratch may not be a symbolic link")
    scratch.mkdir(parents=True, exist_ok=True)
    if not scratch.is_dir() or scratch.is_symlink():
        raise CurationGitHubError("scratch must be a real directory")
    with tempfile.TemporaryDirectory(prefix="curation-provider-", dir=scratch) as name:
        yield Path(name)


def _load_provider(root: Path, adapter: str) -> ModuleType:
    return _load_module(
        f"orinoco_hosted_{adapter.replace('-', '_')}_provider_v1",
        root / "source-adapters" / adapter / "curation_prototype_v1.py",
    )


def build_candidates(
    root: Path,
    adapter: str,
    scratch: Path,
    *,
    source_path: str | None = None,
    source_revision: str | None = None,
    expected_library_version: int | None = None,
) -> CandidateBuild:
    """Build candidates from one trusted root and exact source coordinate."""

    root = _root(root, "root")
    if adapter not in ADAPTERS:
        raise CurationGitHubError(f"Unsupported adapter: {adapter}")
    provider = _load_provider(root, adapter)
    # Provider tooling may print progress.  Keep this command's stdout as one
    # machine-readable JSON object for the workflow.
    with _provider_output(scratch) as output, redirect_stdout(StringIO()):
        if adapter == "dump-research-info":
            if source_path is None or source_revision is None:
                raise CurationGitHubError(
                    "dump-research-info requires --source-path and --source-revision"
                )
            if expected_library_version is not None:
                raise CurationGitHubError(
                    "dump-research-info does not use --expected-library-version"
                )
            if _SHA40.fullmatch(source_revision) is None:
                raise CurationGitHubError("source revision must be a 40-hex commit")
            result = provider.build_candidates(
                root,
                output,
                source_path=source_path,
                expected_source_commit=source_revision,
            )
        else:
            if source_path is not None or source_revision is not None:
                raise CurationGitHubError("zotero does not use dump source arguments")
            if (
                isinstance(expected_library_version, bool)
                or not isinstance(expected_library_version, int)
                or expected_library_version < 1
            ):
                raise CurationGitHubError(
                    "zotero requires a positive --expected-library-version"
                )
            result = provider.build_candidates(
                root,
                output,
                expected_library_version=expected_library_version,
            )
    if not isinstance(result, dict) or result.get("adapter_id") != adapter:
        raise CurationGitHubError("Provider returned the wrong adapter")
    source = result.get("source")
    candidates = result.get("candidates")
    if not isinstance(source, dict) or not isinstance(candidates, list):
        raise CurationGitHubError("Provider result is incomplete")
    seen_source: set[str] = set()
    seen_path: set[str] = set()
    seen_pid: set[str] = set()
    for candidate in candidates:
        source_id = _line(
            getattr(candidate, "source_record_id", None), "source_record_id"
        )
        proposed_path = _candidate_path(candidate)
        record = _candidate_record(candidate)
        pid = _line(record.get("pid"), "proposed record pid")
        if source_id in seen_source:
            raise CurationGitHubError(f"Duplicate source record: {source_id}")
        if proposed_path in seen_path:
            raise CurationGitHubError(f"Duplicate proposal target: {proposed_path}")
        if pid in seen_pid:
            raise CurationGitHubError(f"Duplicate proposed record PID: {pid}")
        seen_source.add(source_id)
        seen_path.add(proposed_path)
        seen_pid.add(pid)
    return CandidateBuild(adapter, result["source"], tuple(candidates))


def _candidate_path(candidate: object) -> str:
    value = _line(getattr(candidate, "proposed_path", None), "proposed_path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix != ".yaml"
    ):
        raise CurationGitHubError(f"Unsafe proposal path: {value}")
    return value


def _candidate_record(candidate: object) -> dict[str, object]:
    value = getattr(candidate, "proposed_record", None)
    if not isinstance(value, Mapping):
        raise CurationGitHubError("Candidate proposed_record must be a mapping")
    result = json.loads(_canonical_bytes(dict(value)))
    if not isinstance(result, dict):  # pragma: no cover - guaranteed above
        raise AssertionError
    return result


def claim_sha256(candidate: object) -> str:
    """Combine material and relevant-policy identity into one cache digest."""

    payload = {
        "material": _line(
            getattr(candidate, "material_fingerprint", None),
            "material_fingerprint",
        ),
        "policy": _line(
            getattr(candidate, "relevant_policy_fingerprint", None),
            "relevant_policy_fingerprint",
        ),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _empty_cache(adapter: str) -> dict[str, object]:
    return {
        "format": CACHE_FORMAT,
        "adapter": adapter,
        "reviews": {},
        "decisions": {},
    }


def cache_path(root: Path, adapter: str) -> Path:
    return root / "source-adapters" / adapter / "policy" / "curation-decisions.yaml"


def load_cache(root: Path, adapter: str) -> dict[str, object]:
    """Read and strictly validate the compact current-decision cache."""

    path = cache_path(root, adapter)
    if not path.exists():
        return _empty_cache(adapter)
    if path.is_symlink() or not path.is_file():
        raise CurationGitHubError(f"Decision cache must be a regular file: {path}")
    value = _strict_yaml(path.read_text(encoding="utf-8"), "decision cache")
    if not isinstance(value, dict) or set(value) != {
        "format",
        "adapter",
        "reviews",
        "decisions",
    }:
        raise CurationGitHubError("Decision cache has missing or unexpected fields")
    if value["format"] != CACHE_FORMAT or value["adapter"] != adapter:
        raise CurationGitHubError("Decision cache format or adapter is invalid")
    reviews = value["reviews"]
    decisions = value["decisions"]
    if not isinstance(reviews, dict) or not isinstance(decisions, dict):
        raise CurationGitHubError(
            "Decision cache reviews and decisions must be mappings"
        )
    for reference, review in reviews.items():
        _line(reference, "review reference")
        if not isinstance(review, dict) or set(review) != {
            "source",
            "reviewer",
            "reviewed_at",
            "pull_request",
        }:
            raise CurationGitHubError(f"Review {reference!r} is invalid")
        if not isinstance(review["source"], dict) or not review["source"]:
            raise CurationGitHubError(f"Review {reference!r} source is invalid")
        _line(review["reviewer"], "reviewer")
        _reviewed_at(review["reviewed_at"])
        _line(review["pull_request"], "pull_request")
    seen_source: set[str] = set()
    used_reviews: set[str] = set()
    for pid, decision in decisions.items():
        _line(pid, "decision pid")
        if not isinstance(decision, dict) or set(decision) != {
            "source_record_id",
            "claim_sha256",
            "disposition",
            "review",
        }:
            raise CurationGitHubError(f"Decision {pid!r} is invalid")
        source_id = _line(decision["source_record_id"], "source_record_id")
        if source_id in seen_source:
            raise CurationGitHubError(f"Duplicate cached source record: {source_id}")
        seen_source.add(source_id)
        if _SHA256.fullmatch(str(decision["claim_sha256"])) is None:
            raise CurationGitHubError(f"Decision {pid!r} claim_sha256 is invalid")
        if decision["disposition"] not in {"accept", "reject", "defer"}:
            raise CurationGitHubError(f"Decision {pid!r} disposition is invalid")
        reference = _line(decision["review"], "decision review")
        if reference not in reviews:
            raise CurationGitHubError(f"Decision {pid!r} references a missing review")
        used_reviews.add(reference)
    if used_reviews != set(reviews):
        raise CurationGitHubError("Decision cache contains an unused review batch")
    return json.loads(_canonical_bytes(value))


def _decision_by_source(cache: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    decisions = cache["decisions"]
    assert isinstance(decisions, dict)
    return {
        str(decision["source_record_id"]): decision
        for decision in decisions.values()
        if isinstance(decision, Mapping)
    }


def pending_candidates(
    candidates: Sequence[object], cache: Mapping[str, object]
) -> tuple[object, ...]:
    """Return claims needing review under the compact cache semantics."""

    existing = _decision_by_source(cache)
    pending: list[object] = []
    for candidate in candidates:
        if getattr(candidate, "baseline_record", None) == getattr(
            candidate, "proposed_record", None
        ):
            continue
        source_id = str(getattr(candidate, "source_record_id"))
        decision = existing.get(source_id)
        if decision is None or decision.get("claim_sha256") != claim_sha256(candidate):
            pending.append(candidate)
            continue
        disposition = decision.get("disposition")
        if disposition == "reject":
            continue
        if disposition == "accept" and getattr(
            candidate, "baseline_record", None
        ) == getattr(candidate, "proposed_record", None):
            continue
        # A deferral has no return-condition UI, so the next proposal asks again.
        pending.append(candidate)
    return tuple(sorted(pending, key=_candidate_sort_key))


def _plain_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = " ".join(value.split())
    return rendered or None


def _candidate_label(candidate: object) -> str:
    record = _candidate_record(candidate)
    for field in ("display_label", "formatted_name", "title", "name", "short_name"):
        label = _plain_label(record.get(field))
        if label is not None:
            return label
    return str(record["pid"])


def _candidate_sort_key(candidate: object) -> tuple[str, str, str]:
    record = _candidate_record(candidate)
    return (
        _candidate_label(candidate).casefold(),
        str(record["pid"]),
        str(getattr(candidate, "source_record_id")),
    )


def _marker_payload(
    build: CandidateBuild, *, base_sha: str, as_of: str
) -> dict[str, object]:
    if _SHA40.fullmatch(base_sha) is None:
        raise CurationGitHubError("base_sha must be a 40-hex commit")
    try:
        if date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise CurationGitHubError("as_of must be an ISO calendar date") from error
    if build.adapter == "dump-research-info":
        revision = build.source.get("commit")
        if not isinstance(revision, str) or _SHA40.fullmatch(revision) is None:
            raise CurationGitHubError("Dump provider did not retain its revision")
        source = {"revision": revision}
    else:
        version = build.source.get("library_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise CurationGitHubError("Zotero provider did not retain its version")
        source = {"library_version": version}
    return {
        "format": FORM_FORMAT,
        "adapter": build.adapter,
        "base_sha": base_sha,
        "as_of": as_of,
        "source": source,
    }


def render_form(
    build: CandidateBuild,
    candidates: Sequence[object],
    *,
    base_sha: str,
    as_of: str,
) -> str:
    """Render a friendly task-list form for the PR description."""

    marker = _marker_payload(build, base_sha=base_sha, as_of=as_of)
    lines = [
        ATTRIBUTION,
        "",
        f"<!-- {_FORM_MARKER} {_b64_encode(marker)} -->",
        "",
        "# Metadata curation review",
        "",
        "Review the actual YAML changes in **Files changed**. Use the task-list",
        "controls in this PR description to check exactly one option for every",
        f"record. Then comment `{SUBMIT_COMMAND}` on the PR.",
        "",
        "The workflow will add one commit containing the submitted decisions and",
        "the resulting accepted metadata. It will not merge the PR.",
        "",
    ]
    if not candidates:
        lines.extend(("No records currently require review.", ""))
    for candidate in sorted(candidates, key=_candidate_sort_key):
        source_id = str(getattr(candidate, "source_record_id"))
        record = _candidate_record(candidate)
        label = html.escape(_candidate_label(candidate), quote=False)
        pid = html.escape(str(record["pid"]), quote=False)
        lines.extend(
            (
                f"<!-- {_RECORD_MARKER} {_b64_encode(source_id)} -->",
                f"## {label}",
                "",
                f"Canonical ID: <code>{pid}</code>",
                "",
            )
        )
        if source_id != str(record["pid"]):
            lines.extend(
                (
                    f"Source ID: <code>{html.escape(source_id, quote=False)}</code>",
                    "",
                )
            )
        blockers = tuple(getattr(candidate, "blockers", ()))
        if blockers:
            rendered = "; ".join(
                html.escape(str(item), quote=False) for item in blockers
            )
            lines.extend((f"Accept is unavailable: {rendered}.", ""))
        else:
            lines.append("- [ ] Accept")
        lines.extend(("- [ ] Reject", "- [ ] Defer", ""))
    return "\n".join(lines)


def inspect_form(body: str) -> dict[str, object]:
    matches = list(_FORM_RE.finditer(body))
    if len(matches) != 1:
        raise CurationGitHubError("PR description must contain one curation marker")
    value = _b64_decode(matches[0].group("payload"), "curation form marker")
    if not isinstance(value, dict) or set(value) != {
        "format",
        "adapter",
        "base_sha",
        "as_of",
        "source",
    }:
        raise CurationGitHubError("Curation form marker fields are invalid")
    if value["format"] != FORM_FORMAT or value["adapter"] not in ADAPTERS:
        raise CurationGitHubError("Curation form marker is unsupported")
    if _SHA40.fullmatch(str(value["base_sha"])) is None:
        raise CurationGitHubError("Curation form base_sha is invalid")
    try:
        if date.fromisoformat(str(value["as_of"])).isoformat() != value["as_of"]:
            raise ValueError
    except ValueError as error:
        raise CurationGitHubError("Curation form as_of is invalid") from error
    source = value["source"]
    if not isinstance(source, dict):
        raise CurationGitHubError("Curation form source is invalid")
    if value["adapter"] == "dump-research-info":
        if (
            set(source) != {"revision"}
            or _SHA40.fullmatch(str(source.get("revision"))) is None
        ):
            raise CurationGitHubError("Curation form dump source is invalid")
    elif set(source) != {"library_version"} or (
        isinstance(source.get("library_version"), bool)
        or not isinstance(source.get("library_version"), int)
        or source["library_version"] < 1
    ):
        raise CurationGitHubError("Curation form Zotero source is invalid")
    return value


def parse_choices(body: str, candidates: Sequence[object]) -> dict[str, str]:
    """Parse exactly one checked task per expected source record."""

    expected = {str(getattr(item, "source_record_id")): item for item in candidates}
    matches = list(_RECORD_RE.finditer(body))
    observed: dict[str, str] = {}
    for index, match in enumerate(matches):
        source_id = _b64_decode(match.group("payload"), "record marker")
        if not isinstance(source_id, str) or source_id not in expected:
            raise CurationGitHubError("PR description contains an unexpected record")
        if source_id in observed:
            raise CurationGitHubError(f"Duplicate form record: {source_id}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        segment = body[match.end() : end]
        selected = [
            choice.group("choice").lower()
            for choice in _CHOICE_RE.finditer(segment)
            if choice.group("checked").lower() == "x"
        ]
        if len(selected) != 1:
            raise CurationGitHubError(
                f"{source_id} must have exactly one checked disposition"
            )
        disposition = selected[0]
        if disposition == "accept" and getattr(expected[source_id], "blockers", ()):
            raise CurationGitHubError(f"Blocked record cannot be accepted: {source_id}")
        observed[source_id] = disposition
    if set(observed) != set(expected):
        missing = sorted(set(expected) - set(observed))
        raise CurationGitHubError(
            "PR description is missing review records: " + ", ".join(missing)
        )
    return observed


def _assert_regular_tree(root: Path, label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise CurationGitHubError(f"{label} must be a real directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CurationGitHubError(f"{label} contains a symbolic link: {path}")
        if not path.is_dir() and not path.is_file():
            raise CurationGitHubError(f"{label} contains a special file: {path}")


def _tree_bytes(root: Path, label: str) -> dict[str, bytes]:
    _assert_regular_tree(root, label)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _expected_proposal_tree(
    trusted_root: Path, candidates: Sequence[object]
) -> dict[str, bytes]:
    records = trusted_root / "metadata" / "records"
    expected = _tree_bytes(records, "trusted metadata tree")
    for candidate in candidates:
        path = _candidate_path(candidate)
        baseline = getattr(candidate, "baseline_record", None)
        if baseline is None and path in expected:
            raise CurationGitHubError(f"New proposal would overwrite {path}")
        if baseline is not None:
            if path not in expected:
                raise CurationGitHubError(f"Proposal baseline is missing: {path}")
            loaded = _strict_yaml(expected[path].decode("utf-8"), f"baseline {path}")
            if loaded != dict(baseline):
                raise CurationGitHubError(f"Proposal baseline changed: {path}")
        expected[path] = _yaml_bytes(_candidate_record(candidate))
    return expected


def verify_proposal_tree(
    trusted_root: Path, review_root: Path, candidates: Sequence[object]
) -> None:
    expected = _expected_proposal_tree(trusted_root, candidates)
    observed = _tree_bytes(review_root / "metadata" / "records", "review metadata tree")
    if observed != expected:
        unexpected = sorted(set(observed) - set(expected))
        missing = sorted(set(expected) - set(observed))
        changed = sorted(
            path
            for path in set(expected) & set(observed)
            if expected[path] != observed[path]
        )
        details = []
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        if missing:
            details.append("missing: " + ", ".join(missing))
        if changed:
            details.append("changed: " + ", ".join(changed))
        raise CurationGitHubError(
            "Review metadata is not the exact proposal (" + "; ".join(details) + ")"
        )


def _safe_target(records: Path, relative: str) -> Path:
    _assert_regular_tree(records, "metadata tree")
    target = records / Path(relative)
    current = records
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise CurationGitHubError(f"Unsafe proposal parent: {current}")
        current.mkdir(exist_ok=True)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise CurationGitHubError(f"Unsafe proposal target: {target}")
    return target


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CurationGitHubError(f"Output must be a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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


def stage_records(root: Path, candidates: Sequence[object]) -> int:
    records = root / "metadata" / "records"
    changed = 0
    for candidate in candidates:
        target = _safe_target(records, _candidate_path(candidate))
        payload = _yaml_bytes(_candidate_record(candidate))
        if target.exists() and target.read_bytes() == payload:
            continue
        _atomic_write(target, payload)
        changed += 1
    return changed


def _reviewed_at(value: object) -> str:
    rendered = _line(value, "reviewed_at")
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as error:
        raise CurationGitHubError("reviewed_at must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CurationGitHubError("reviewed_at must be in UTC")
    normalized = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    if rendered != normalized:
        raise CurationGitHubError(
            "reviewed_at must use canonical UTC seconds (YYYY-MM-DDTHH:MM:SSZ)"
        )
    return normalized


def _source_provenance(build: CandidateBuild) -> dict[str, object]:
    if build.adapter == "dump-research-info":
        return {
            "uri": SOURCE_NAMESPACE,
            "revision": str(build.source["commit"]),
        }
    return {
        "uri": f"https://api.zotero.org/groups/{build.source['group_id']}",
        "library_version": int(build.source["library_version"]),
    }


def source_coordinate(build: CandidateBuild) -> str:
    """Render one compact, immutable source coordinate for Git audit trailers."""

    source = _source_provenance(build)
    if build.adapter == "dump-research-info":
        return f"{source['uri']}@{source['revision']}"
    return f"{source['uri']}@library-version:{source['library_version']}"


def updated_cache(
    cache: Mapping[str, object],
    build: CandidateBuild,
    candidates: Sequence[object],
    choices: Mapping[str, str],
    *,
    reviewer: str,
    reviewed_at: str,
    review_url: str,
    pull_request_number: int,
) -> dict[str, object]:
    if isinstance(pull_request_number, bool) or pull_request_number < 1:
        raise CurationGitHubError("pull_request_number must be positive")
    reference = f"pr-{pull_request_number}"
    decisions = json.loads(_canonical_bytes(cache["decisions"]))
    reviews = json.loads(_canonical_bytes(cache["reviews"]))
    if reference in reviews:
        raise CurationGitHubError(f"Review reference already exists: {reference}")
    reviewed_sources = {str(getattr(item, "source_record_id")) for item in candidates}
    decisions = {
        pid: decision
        for pid, decision in decisions.items()
        if decision["source_record_id"] not in reviewed_sources
    }
    for candidate in candidates:
        source_id = str(getattr(candidate, "source_record_id"))
        pid = str(_candidate_record(candidate)["pid"])
        if pid in decisions:
            raise CurationGitHubError(
                f"Canonical PID belongs to another cached source record: {pid}"
            )
        decisions[pid] = {
            "source_record_id": source_id,
            "claim_sha256": claim_sha256(candidate),
            "disposition": choices[source_id],
            "review": reference,
        }
    used = {decision["review"] for decision in decisions.values()}
    reviews = {key: value for key, value in reviews.items() if key in used}
    reviews[reference] = {
        "source": _source_provenance(build),
        "reviewer": _line(reviewer, "reviewer"),
        "reviewed_at": _reviewed_at(reviewed_at),
        "pull_request": _line(review_url, "review_url"),
    }
    return {
        "format": CACHE_FORMAT,
        "adapter": build.adapter,
        "reviews": {key: reviews[key] for key in sorted(reviews)},
        "decisions": {key: decisions[key] for key in sorted(decisions)},
    }


def _cache_unchanged(trusted_root: Path, review_root: Path, adapter: str) -> None:
    trusted = cache_path(trusted_root, adapter)
    review = cache_path(review_root, adapter)
    trusted_payload = trusted.read_bytes() if trusted.exists() else None
    review_payload = review.read_bytes() if review.exists() else None
    if trusted_payload != review_payload:
        raise CurationGitHubError("Decision cache changed before human submission")


def apply_review(
    trusted_root: Path,
    review_root: Path,
    build: CandidateBuild,
    candidates: Sequence[object],
    body: str,
    *,
    reviewer: str,
    reviewed_at: str,
    review_url: str,
    pull_request_number: int,
) -> dict[str, object]:
    """Verify, reconcile, and persist one submitted review batch."""

    verify_proposal_tree(trusted_root, review_root, candidates)
    _cache_unchanged(trusted_root, review_root, build.adapter)
    choices = parse_choices(body, candidates)
    records = review_root / "metadata" / "records"
    changed = 0
    for candidate in candidates:
        source_id = str(getattr(candidate, "source_record_id"))
        if choices[source_id] == "accept":
            continue
        relative = _candidate_path(candidate)
        target = _safe_target(records, relative)
        baseline = trusted_root / "metadata" / "records" / relative
        if baseline.exists():
            payload = baseline.read_bytes()
            if target.read_bytes() != payload:
                _atomic_write(target, payload)
                changed += 1
        else:
            target.unlink()
            changed += 1
    cache = updated_cache(
        load_cache(trusted_root, build.adapter),
        build,
        candidates,
        choices,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        review_url=review_url,
        pull_request_number=pull_request_number,
    )
    destination = cache_path(review_root, build.adapter)
    if destination.parent.is_symlink():
        raise CurationGitHubError("Decision-cache directory may not be a symlink")
    _atomic_write(destination, _yaml_bytes(cache))
    counts = {
        disposition: sum(value == disposition for value in choices.values())
        for disposition in ("accept", "reject", "defer")
    }
    return {
        "adapter": build.adapter,
        "source_coordinate": source_coordinate(build),
        "count": len(candidates),
        "accepted": counts["accept"],
        "rejected": counts["reject"],
        "deferred": counts["defer"],
        "restored_records": changed,
        "cache_path": destination.relative_to(review_root).as_posix(),
    }


def _read_body(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CurationGitHubError(f"PR body must be a regular file: {path}")
    return path.read_text(encoding="utf-8")


def _add_source_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--source-path")
    command.add_argument("--source-revision")
    command.add_argument("--expected-library-version", type=int)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--root", type=Path, required=True)
    plan.add_argument("--adapter", choices=ADAPTERS, required=True)
    plan.add_argument("--scratch", type=Path, required=True)
    plan.add_argument("--body", type=Path, required=True)
    plan.add_argument("--base-sha", required=True)
    plan.add_argument("--as-of", required=True)
    _add_source_arguments(plan)

    stage = commands.add_parser("stage-proposal")
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--adapter", choices=ADAPTERS, required=True)
    stage.add_argument("--scratch", type=Path, required=True)
    stage.add_argument("--base-sha", required=True)
    stage.add_argument("--as-of", required=True)
    _add_source_arguments(stage)

    inspect = commands.add_parser("inspect-form")
    inspect.add_argument("--body", type=Path, required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--trusted-root", type=Path, required=True)
    apply.add_argument("--review-root", type=Path, required=True)
    apply.add_argument("--body", type=Path, required=True)
    apply.add_argument("--scratch", type=Path, required=True)
    apply.add_argument("--reviewer", required=True)
    apply.add_argument("--reviewed-at", required=True)
    apply.add_argument("--review-url", required=True)
    apply.add_argument("--pull-request-number", type=int, required=True)
    apply.add_argument("--adapter", choices=ADAPTERS)
    _add_source_arguments(apply)
    return result


def _build_from_args(arguments, *, root: Path, adapter: str) -> CandidateBuild:
    return build_candidates(
        root,
        adapter,
        arguments.scratch,
        source_path=arguments.source_path,
        source_revision=arguments.source_revision,
        expected_library_version=arguments.expected_library_version,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "inspect-form":
            output = inspect_form(_read_body(arguments.body))
        elif arguments.command == "plan":
            root = _root(arguments.root, "root")
            build = _build_from_args(arguments, root=root, adapter=arguments.adapter)
            candidates = pending_candidates(
                build.candidates, load_cache(root, build.adapter)
            )
            body = render_form(
                build,
                candidates,
                base_sha=arguments.base_sha,
                as_of=arguments.as_of,
            )
            _atomic_write(arguments.body, body.encode("utf-8"))
            output = {
                "adapter": build.adapter,
                "candidate_count": len(candidates),
                "body": str(arguments.body),
            }
        elif arguments.command == "stage-proposal":
            root = _root(arguments.root, "root")
            build = _build_from_args(arguments, root=root, adapter=arguments.adapter)
            candidates = pending_candidates(
                build.candidates, load_cache(root, build.adapter)
            )
            _marker_payload(
                build,
                base_sha=arguments.base_sha,
                as_of=arguments.as_of,
            )
            output = {
                "adapter": build.adapter,
                "candidate_count": len(candidates),
                "changed_record_count": stage_records(root, candidates),
            }
        elif arguments.command == "apply":
            body = _read_body(arguments.body)
            marker = inspect_form(body)
            adapter = str(marker["adapter"])
            if arguments.adapter is not None and arguments.adapter != adapter:
                raise CurationGitHubError("Explicit adapter differs from the PR form")
            if adapter == "dump-research-info":
                marker_revision = marker["source"]["revision"]
                if (
                    arguments.source_revision is not None
                    and arguments.source_revision != marker_revision
                ):
                    raise CurationGitHubError("Dump source differs from the PR form")
                arguments.source_revision = marker_revision
            else:
                marker_version = marker["source"]["library_version"]
                if (
                    arguments.expected_library_version is not None
                    and arguments.expected_library_version != marker_version
                ):
                    raise CurationGitHubError("Zotero source differs from the PR form")
                arguments.expected_library_version = marker_version
            trusted = _root(arguments.trusted_root, "trusted_root")
            review = _root(arguments.review_root, "review_root")
            build = _build_from_args(arguments, root=trusted, adapter=adapter)
            candidates = pending_candidates(
                build.candidates, load_cache(trusted, adapter)
            )
            output = apply_review(
                trusted,
                review,
                build,
                candidates,
                body,
                reviewer=arguments.reviewer,
                reviewed_at=arguments.reviewed_at,
                review_url=arguments.review_url,
                pull_request_number=arguments.pull_request_number,
            )
        else:  # pragma: no cover - argparse enforces commands
            raise AssertionError(arguments.command)
    except (CurationGitHubError, OSError, UnicodeError, ValueError) as error:
        print(f"curation GitHub helper failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
