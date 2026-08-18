#!/usr/bin/env python3
"""Build deterministic dump-research-info candidates for M5 curation."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path, PurePosixPath
import re
import sys
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import quote


ADAPTER_ID = "dump-research-info"
ADAPTER_AGENT = "urn:orinoco-lite:source-adapter:dump-research-info:prototype-v1"
CLAIM_KIND = "record-import"
PROTOTYPE_VERSION = 1
SOURCE_DIRECTORY = "data/con_site"
SOURCE_NAMESPACE = "https://github.com/con/dump-research-info"
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
PAV_IMPORTED_BY = "pav:importedBy"
PAV_IMPORTED_FROM = "pav:importedFrom"


class DumpResearchInfoCurationError(RuntimeError):
    """Report an invalid source checkout or unsafe candidate proposal."""


def load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise DumpResearchInfoCurationError(f"Cannot load prototype dependency {path}")
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
        "orinoco_dump_research_info_adapter_for_curation",
        root / "source-adapters/dump-research-info/metadata_adapter.py",
    )
    return curation, adapter


def literal_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DumpResearchInfoCurationError(
            "source_path must be a literal repository-relative path"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or "\\" in value
        or "\0" in value
        or value == "."
    ):
        raise DumpResearchInfoCurationError(
            "source_path must be a literal repository-relative POSIX path"
        )
    return value


def exact_commit(value: object) -> str:
    if not isinstance(value, str) or FULL_COMMIT.fullmatch(value) is None:
        raise DumpResearchInfoCurationError(
            "expected_source_commit must be an exact lower-case 40-hex commit"
        )
    return value


def source_checkout(
    root: Path,
    literal_path: str,
    expected_commit: str,
    adapter: ModuleType,
) -> Path:
    checkout = (root / Path(literal_path)).resolve()
    if not checkout.is_dir():
        raise DumpResearchInfoCurationError(
            f"Source checkout does not exist: {literal_path}"
        )
    top_level = Path(
        adapter.git_output(checkout, "rev-parse", "--show-toplevel")
    ).resolve()
    if top_level != checkout:
        raise DumpResearchInfoCurationError(
            "source_path must identify the source repository root"
        )
    observed_commit = adapter.git_commit(checkout)
    if observed_commit != expected_commit:
        raise DumpResearchInfoCurationError(
            "Source checkout moved: expected "
            f"{expected_commit}, found {observed_commit}"
        )
    if adapter.git_dirty(checkout):
        raise DumpResearchInfoCurationError(
            "Source checkout must be clean, including untracked files"
        )
    return checkout


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
        raise DumpResearchInfoCurationError(
            f"{record.get('pid')}: annotations must be a mapping"
        )
    for tag, annotation in expanded_pav(imported_by, imported_from).items():
        if tag in annotations and annotations[tag] != annotation:
            raise DumpResearchInfoCurationError(
                f"{record.get('pid')}: proposal would overwrite {tag}"
            )
        annotations[tag] = annotation
    return proposed


def source_record_coordinate(class_name: str, source_pid: str) -> str:
    """Identify the source record independently of an execution revision."""

    return (
        f"{SOURCE_NAMESPACE}/blob/main/{SOURCE_DIRECTORY}/"
        f"{quote(class_name, safe='')}.json#record={quote(source_pid, safe='')}"
    )


def relevant_policy() -> dict[str, object]:
    return {
        "prototype_version": PROTOTYPE_VERSION,
        "source_directory": SOURCE_DIRECTORY,
        "identity_policy": "exact-pid-or-reviewed-identifier-v1",
        "canonical_path_policy": "metadata-adapter-record-stem-v1",
        "unresolved_relation_policy": "preserve-and-block-v1",
    }


def build_candidates(
    root: Path,
    output: Path,
    *,
    source_path: str,
    expected_source_commit: str,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    """Build record-level candidates from one exact clean source checkout."""

    root = root.resolve()
    output = output.resolve()
    literal_path = literal_relative_path(source_path)
    expected_commit = exact_commit(expected_source_commit)
    curation, metadata_adapter = load_dependencies(root)
    checkout = source_checkout(
        root,
        literal_path,
        expected_commit,
        metadata_adapter,
    )
    report, plans = metadata_adapter.plan_materialization(
        checkout,
        root,
        output,
        source_directory=SOURCE_DIRECTORY,
    )

    if metadata_adapter.git_commit(
        checkout
    ) != expected_commit or metadata_adapter.git_dirty(checkout):
        raise DumpResearchInfoCurationError(
            "Source checkout changed while candidates were being built"
        )
    inputs = report.get("inputs")
    if (
        not isinstance(inputs, dict)
        or inputs.get("dump_research_info_commit") != expected_commit
        or inputs.get("dump_research_info_dirty") is not False
    ):
        raise DumpResearchInfoCurationError(
            "Materialization plan did not retain the exact clean source coordinate"
        )

    known_pids = set(metadata_adapter.load_yaml_records(root))
    for _class_name, _source_pid, _target, desired, _current in plans:
        pid = desired.get("pid")
        if not isinstance(pid, str) or not pid:
            raise DumpResearchInfoCurationError(
                "Materialization plan contains a record without a PID"
            )
        known_pids.add(pid)

    imported_by = ADAPTER_AGENT
    records_root = (root / "metadata/records").resolve()
    candidates = []
    all_blockers: set[str] = set()
    for class_name, source_pid, target, desired, current in plans:
        try:
            proposed_path = target.resolve().relative_to(records_root).as_posix()
        except ValueError as error:
            raise DumpResearchInfoCurationError(
                f"Canonical proposal target escapes metadata/records: {target}"
            ) from error
        unresolved = sorted(metadata_adapter.referenced_pids(desired) - known_pids)
        blockers = tuple(f"unresolved-relation:{pid}" for pid in unresolved)
        all_blockers.update(blockers)
        proposed_record = annotate_record(
            desired,
            imported_by=imported_by,
            imported_from=source_record_coordinate(class_name, source_pid),
        )
        candidates.append(
            curation.make_candidate(
                adapter_id=ADAPTER_ID,
                source_namespace=SOURCE_NAMESPACE,
                source_record_id=f"{class_name}:{source_pid}",
                claim_kind=CLAIM_KIND,
                material={
                    "source_class": class_name,
                    "source_record_id": source_pid,
                    "transformed_record": desired,
                    "unresolved_relation_targets": unresolved,
                },
                relevant_policy=relevant_policy(),
                proposed_path=proposed_path,
                proposed_record=proposed_record,
                baseline_record=current,
                blockers=blockers,
            )
        )

    candidates.sort(key=lambda candidate: candidate.candidate_id)
    return {
        "adapter_id": ADAPTER_ID,
        "source": {
            "kind": "exact-clean-git-checkout",
            "path": literal_path,
            "commit": expected_commit,
            "tree": inputs.get("dump_research_info_tree"),
            "source_directory": SOURCE_DIRECTORY,
        },
        "context": {"source_run_id": source_run_id},
        "implementation": {
            "agent": ADAPTER_AGENT,
            "provider_sha256": metadata_adapter.sha256(Path(__file__).read_bytes()),
            "transformer_sha256": metadata_adapter.sha256(
                Path(metadata_adapter.__file__).read_bytes()
            ),
        },
        "policy": relevant_policy(),
        "blockers": sorted(all_blockers),
        "candidates": candidates,
    }
