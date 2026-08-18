#!/usr/bin/env python3
"""Build deterministic Zotero candidates for the M5 curation prototype."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import quote

import yaml


ADAPTER_ID = "zotero"
ADAPTER_AGENT = "urn:orinoco-lite:source-adapter:zotero:prototype-v1"
CLAIM_KIND = "record-import"
PROTOTYPE_VERSION = 1
ZOTERO_NOTATION = re.compile(r"^zotero:group:(\d+):item:([A-Z0-9]+)$")
PAV_IMPORTED_BY = "pav:importedBy"
PAV_IMPORTED_FROM = "pav:importedFrom"


class ZoteroCurationError(RuntimeError):
    """Report an invalid frozen source, policy, or proposal target."""


def load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ZoteroCurationError(f"Cannot load prototype dependency {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def load_dependencies(root: Path) -> tuple[ModuleType, ModuleType]:
    curation = load_module(
        "orinoco_curation_prototype_v1",
        root / "source-adapters/metadata/tools/curation_prototype_v1.py",
    )
    adapter = load_module(
        "orinoco_zotero_metadata_adapter_for_curation",
        root / "source-adapters/zotero/metadata_adapter.py",
    )
    return curation, adapter


def load_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ZoteroCurationError(f"Cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ZoteroCurationError(f"{label} must be a mapping: {path}")
    return value


def load_publications(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ZoteroCurationError(
            f"Cannot read Zotero candidates {path}: {error}"
        ) from error
    if not isinstance(value, list) or not all(
        isinstance(record, dict) for record in value
    ):
        raise ZoteroCurationError(
            "Zotero publication candidates must be an array of objects"
        )
    return value


def canonical_records(root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    records_root = root / "metadata/records"
    for path in sorted(records_root.rglob("*.yaml")):
        if path.name == ".dumpthings.yaml":
            continue
        record = load_mapping(path, label="canonical record")
        pid = record.get("pid")
        if not isinstance(pid, str) or not pid or pid in result:
            raise ZoteroCurationError(
                f"Canonical record PID is invalid or duplicated: {path}"
            )
        result[pid] = (path, record)
    return result


def source_identity(record: Mapping[str, Any], group_id: int) -> tuple[str, str]:
    """Return the stable Zotero item-set identity and its source locator.

    DOI-derived publication PIDs are material proposal data, not source
    identity: correcting a DOI must reopen the same claim rather than minting
    an unrelated candidate.  Duplicate items intentionally form one composite
    identity from their sorted, immutable Zotero item keys.  A change to that
    key set is therefore an identity change, while field changes within the
    same item set are material revisions of one claim.
    """

    matches: list[tuple[str, str]] = []
    identifiers = record.get("identifiers", [])
    if not isinstance(identifiers, list):
        raise ZoteroCurationError(f"{record.get('pid')}: identifiers must be a list")
    for identifier in identifiers:
        if not isinstance(identifier, dict):
            continue
        notation = identifier.get("notation")
        match = ZOTERO_NOTATION.fullmatch(str(notation))
        if match is not None:
            matches.append((match.group(1), match.group(2)))
    if not matches or any(group != str(group_id) for group, _key in matches):
        raise ZoteroCurationError(
            f"{record.get('pid')}: expected Zotero identifiers for group {group_id}"
        )
    keys = sorted({key for _group, key in matches})
    source_record_id = (
        f"item:{keys[0]}" if len(keys) == 1 else f"items:{','.join(keys)}"
    )
    if len(keys) == 1:
        imported_from = f"https://api.zotero.org/groups/{group_id}/items/{keys[0]}"
    else:
        imported_from = (
            f"https://api.zotero.org/groups/{group_id}/items?itemKey="
            f"{quote(','.join(keys), safe='')}"
        )
    return source_record_id, imported_from


def relation_targets(record: Mapping[str, Any], field: str) -> set[str]:
    values = record.get(field, [])
    if not isinstance(values, list):
        values = [values]
    targets: set[str] = set()
    for value in values:
        target = value.get("object") if isinstance(value, dict) else value
        if isinstance(target, str) and target:
            targets.add(target)
    return targets


def selected_policy(
    source_record: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Select only policy entries capable of changing this source claim."""

    source_pid = str(source_record["pid"])
    attribution_targets = relation_targets(source_record, "attributed_to")
    about_targets = relation_targets(source_record, "about")
    generation_targets = relation_targets(source_record, "generated_by")
    allowed_attribution = set(policy.get("allowed_attribution_targets", []))
    omitted_attribution = policy.get("omitted_attribution_targets", {})
    allowed_about = set(policy.get("allowed_about_targets", []))
    omitted_generation = policy.get("omitted_generation_objects", {})
    curated = policy.get("curated_generations", {})
    allowed_curated = set(policy.get("allowed_curated_generation_targets", []))
    if not isinstance(omitted_attribution, dict):
        raise ZoteroCurationError("omitted_attribution_targets must be a mapping")
    if not isinstance(omitted_generation, dict):
        raise ZoteroCurationError("omitted_generation_objects must be a mapping")
    if not isinstance(curated, dict):
        raise ZoteroCurationError("curated_generations must be a mapping")

    attribution = {
        target: (
            {"outcome": "allow"}
            if target in allowed_attribution
            else {"outcome": "omit", "rationale": omitted_attribution[target]}
        )
        for target in sorted(attribution_targets)
    }
    about = {
        target: {"outcome": "allow" if target in allowed_about else "unclassified"}
        for target in sorted(about_targets)
    }
    generation = {
        target: (
            {"outcome": "omit", "rationale": omitted_generation[target]}
            if target in omitted_generation
            else {"outcome": "retain"}
        )
        for target in sorted(generation_targets)
    }
    curated_for_record = deepcopy(curated.get(source_pid, []))
    curated_targets = relation_targets(
        {"generated_by": curated_for_record}, "generated_by"
    )
    return {
        "prototype_version": PROTOTYPE_VERSION,
        "source_policy_format_version": policy.get("format_version"),
        "pid_override": deepcopy(policy.get("pid_overrides", {}).get(source_pid)),
        "attribution_targets": attribution,
        "about_targets": about,
        "generation_targets": generation,
        "curated_generations": curated_for_record,
        "curated_target_authority": {
            target: target in allowed_curated for target in sorted(curated_targets)
        },
    }


def expanded_pav(imported_by: str, imported_from: str) -> dict[str, dict[str, str]]:
    return {
        PAV_IMPORTED_BY: {
            "annotation_tag": PAV_IMPORTED_BY,
            "annotation_value": imported_by,
        },
        PAV_IMPORTED_FROM: {
            "annotation_tag": PAV_IMPORTED_FROM,
            "annotation_value": imported_from,
        },
    }


def annotate_record(
    record: Mapping[str, Any], *, imported_by: str, imported_from: str
) -> dict[str, Any]:
    proposed = deepcopy(dict(record))
    annotations = proposed.setdefault("annotations", {})
    if not isinstance(annotations, dict):
        raise ZoteroCurationError(f"{record.get('pid')}: annotations must be a mapping")
    for tag, annotation in expanded_pav(imported_by, imported_from).items():
        if tag in annotations and annotations[tag] != annotation:
            raise ZoteroCurationError(
                f"{record.get('pid')}: proposal would overwrite {tag}"
            )
        annotations[tag] = annotation
    return proposed


def build_candidates(
    root: Path,
    output: Path,
    *,
    expected_library_version: int,
) -> dict[str, Any]:
    """Build record-level candidates from the exact committed Zotero fixture."""

    root = root.resolve()
    output = output.resolve()
    curation, metadata_adapter = load_dependencies(root)
    snapshot_path = root / "source-adapters/zotero/source/snapshot.json"
    publications_path = (
        root / "source-adapters/zotero/source/candidates/XYZPublication.json"
    )
    policy_path = root / "source-adapters/zotero/policy/site-policy.yaml"
    snapshot = metadata_adapter.load_json(snapshot_path)
    if not isinstance(snapshot, dict):
        raise ZoteroCurationError("The committed Zotero snapshot must be an object")
    ingest, site_export = metadata_adapter.load_tools(root)
    ingest.validate_snapshot(snapshot)
    source = snapshot.get("source")
    if not isinstance(source, dict):
        raise ZoteroCurationError(
            "The committed Zotero snapshot has no source metadata"
        )
    library_version = source.get("library_version")
    if library_version != expected_library_version:
        raise ZoteroCurationError(
            f"Zotero fixture moved: expected {expected_library_version}, found {library_version}"
        )
    group_id = source.get("group_id")
    if not isinstance(group_id, int):
        raise ZoteroCurationError("The committed Zotero group identifier is invalid")

    policy = load_mapping(policy_path, label="Zotero site policy")
    source_publications = load_publications(publications_path)
    source_by_pid = {str(record.get("pid")): record for record in source_publications}
    if len(source_by_pid) != len(source_publications) or "None" in source_by_pid:
        raise ZoteroCurationError(
            "Zotero publication candidate PIDs are invalid or duplicated"
        )

    rendered_root, rendered_report_path = metadata_adapter.export_site_publications(
        site_export,
        publications_path,
        snapshot_path,
        policy_path,
        output,
    )
    rendered = metadata_adapter.yaml_map(rendered_root)
    rendered_report = metadata_adapter.load_json(rendered_report_path)
    if not isinstance(rendered_report, dict):
        raise ZoteroCurationError("Zotero site export report must be an object")
    pid_map = rendered_report.get("pid_map")
    if not isinstance(pid_map, list):
        raise ZoteroCurationError("Zotero site export report has no PID map")

    records_root = root / "metadata/records"
    canonical = canonical_records(root)
    imported_by = ADAPTER_AGENT
    candidates = []
    for mapping in pid_map:
        if not isinstance(mapping, dict):
            raise ZoteroCurationError("Zotero PID map contains a non-object entry")
        source_pid = mapping.get("source_pid")
        site_pid = mapping.get("site_pid")
        if not isinstance(source_pid, str) or not isinstance(site_pid, str):
            raise ZoteroCurationError("Zotero PID map entry is incomplete")
        source_record = source_by_pid.get(source_pid)
        rendered_record = rendered.get(site_pid)
        if source_record is None or rendered_record is None:
            raise ZoteroCurationError(f"Zotero PID map cannot resolve {source_pid}")
        source_record_id, imported_from = source_identity(source_record, group_id)
        baseline = canonical.get(site_pid)
        if baseline is None:
            rendered_path = next(
                path
                for path in sorted(rendered_root.glob("*.yaml"))
                if load_mapping(path, label="rendered publication").get("pid")
                == site_pid
            )
            proposed_path = Path("XYZPublication") / rendered_path.name
            baseline_record = None
        else:
            canonical_path, baseline_record = baseline
            proposed_path = canonical_path.relative_to(records_root)
        proposed_record = annotate_record(
            rendered_record,
            imported_by=imported_by,
            imported_from=imported_from,
        )
        candidates.append(
            curation.make_candidate(
                adapter_id=ADAPTER_ID,
                source_namespace=f"zotero:group:{group_id}",
                source_record_id=source_record_id,
                claim_kind=CLAIM_KIND,
                material={
                    "source_record": source_record,
                    "rendered_record": rendered_record,
                },
                relevant_policy=selected_policy(source_record, policy),
                proposed_path=proposed_path.as_posix(),
                proposed_record=proposed_record,
                baseline_record=baseline_record,
            )
        )

    candidates.sort(key=lambda candidate: candidate.candidate_id)
    return {
        "adapter_id": ADAPTER_ID,
        "source": {
            "kind": "frozen-zotero-snapshot",
            "group_id": group_id,
            "library_version": library_version,
            "content_sha256": source.get("content_sha256"),
            "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "publication_candidates_sha256": hashlib.sha256(
                publications_path.read_bytes()
            ).hexdigest(),
        },
        "policy": {
            "path": policy_path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "prototype_version": PROTOTYPE_VERSION,
        },
        "implementation": {
            "agent": ADAPTER_AGENT,
            "provider_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "transformer_sha256": hashlib.sha256(
                Path(metadata_adapter.__file__).read_bytes()
            ).hexdigest(),
            "ingest_sha256": hashlib.sha256(
                Path(ingest.__file__).read_bytes()
            ).hexdigest(),
            "site_export_sha256": hashlib.sha256(
                Path(site_export.__file__).read_bytes()
            ).hexdigest(),
        },
        "candidates": candidates,
    }
