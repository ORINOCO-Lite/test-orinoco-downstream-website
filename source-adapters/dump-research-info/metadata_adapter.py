#!/usr/bin/env python3
"""Review legacy ``dump-research-info/data/con_site`` metadata."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

import yaml


ADAPTER_API_VERSION = 1
DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
PLACEHOLDER = re.compile(
    r"reporter\.nih\.gov/search/x[12]x[12]x[12]x[12]x[12]", re.IGNORECASE
)
RELATION_KEYS = {"associated_with", "attributed_to", "generated_by", "part_of"}


class DumpResearchInfoAdapterError(RuntimeError):
    """Report malformed source, downstream metadata, or output state."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_output(path: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), *arguments],
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "git command failed"
        raise DumpResearchInfoAdapterError(
            f"Cannot inspect Git checkout {path}: {detail}"
        ) from error


def git_commit(path: Path) -> str:
    return git_output(path, "rev-parse", "HEAD^{commit}")


def git_tree(path: Path, relative: str) -> str:
    return git_output(path, "rev-parse", f"HEAD:{relative}")


def git_dirty(path: Path) -> bool:
    return bool(git_output(path, "status", "--porcelain=v1", "--untracked-files=all"))


def paths_differ_from_commit(
    path: Path, commit: str, relative_paths: Sequence[str]
) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "diff", "--quiet", commit, "--", *relative_paths],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise DumpResearchInfoAdapterError(
            f"Cannot compare downstream metadata with commit {commit}"
        )
    return result.returncode == 1


def load_source(root: Path) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    seen: set[str] = set()
    paths = sorted(root.glob("XYZ*.json"))
    if not paths:
        raise DumpResearchInfoAdapterError(
            f"No XYZ class files exist in source directory {root}"
        )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DumpResearchInfoAdapterError(
                f"Cannot read source class {path}: {error}"
            ) from error
        if not isinstance(payload, list):
            raise DumpResearchInfoAdapterError(f"Source class is not a list: {path}")
        records: dict[str, dict[str, object]] = {}
        for record in payload:
            if not isinstance(record, dict) or not isinstance(record.get("pid"), str):
                raise DumpResearchInfoAdapterError(
                    f"Source record has no PID: {path}"
                )
            pid = str(record["pid"])
            if pid in seen:
                raise DumpResearchInfoAdapterError(f"Source PID is duplicated: {pid}")
            seen.add(pid)
            records[pid] = record
        result[path.stem] = records
    return result


def load_yaml_records(downstream_root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    root = downstream_root / "metadata/records"
    if not root.is_dir():
        raise DumpResearchInfoAdapterError(
            f"Downstream metadata root does not exist: {root}"
        )
    for path in sorted(root.rglob("*.yaml")):
        if path.name.startswith("."):
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise DumpResearchInfoAdapterError(
                f"Cannot read downstream record {path}: {error}"
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("pid"), str):
            raise DumpResearchInfoAdapterError(
                f"Downstream record has no PID: {path}"
            )
        pid = str(payload["pid"])
        if pid in result:
            raise DumpResearchInfoAdapterError(f"Downstream PID is duplicated: {pid}")
        result[pid] = {
            "class": path.parent.name,
            "path": path.relative_to(downstream_root).as_posix(),
            "record": payload,
            "scope": "records",
        }
    return result


def normalized_doi(value: object) -> str | None:
    text = str(value).strip().lower().rstrip("/")
    if "doi.org/" in text:
        text = text.split("doi.org/", 1)[1]
    if text.startswith("doi:"):
        text = text[4:]
    return text if DOI.fullmatch(text) else None


def identity_tokens(record: Mapping[str, object]) -> set[str]:
    values = [record.get("pid")]
    identifiers = record.get("identifiers", [])
    if isinstance(identifiers, list):
        values.extend(
            identifier.get("notation")
            for identifier in identifiers
            if isinstance(identifier, dict)
        )
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        tokens.add(f"value:{text.casefold().rstrip('/')}")
        if doi := normalized_doi(text):
            tokens.add(f"doi:{doi}")
    return tokens


def normalize_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: normalize_value(child)
            for key, child in sorted(value.items())
            if key != "schema_type"
        }
    if isinstance(value, list):
        return [normalize_value(child) for child in value]
    return value


def list_additions(source: list[object], downstream: list[object]) -> list[object]:
    downstream_values = {
        canonical_json(normalize_value(value)) for value in downstream
    }
    return [
        value
        for value in source
        if canonical_json(normalize_value(value)) not in downstream_values
    ]


def field_delta(
    source: Mapping[str, object], downstream: Mapping[str, object]
) -> dict[str, object]:
    missing: dict[str, object] = {}
    differing: dict[str, object] = {}
    for field, source_value in sorted(source.items()):
        if field in {"pid", "schema_type"}:
            continue
        if field not in downstream or downstream[field] in (None, "", []):
            missing[field] = source_value
            continue
        downstream_value = downstream[field]
        if normalize_value(source_value) == normalize_value(downstream_value):
            continue
        difference: dict[str, object] = {
            "source": source_value,
            "downstream": downstream_value,
        }
        if isinstance(source_value, list) and isinstance(downstream_value, list):
            additions = list_additions(source_value, downstream_value)
            if additions:
                difference["source_only_entries"] = additions
        differing[field] = difference
    return {"missing_fields": missing, "differing_fields": differing}


def referenced_pids(value: object, field: str | None = None) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        if field in RELATION_KEYS and isinstance(value.get("object"), str):
            result.add(str(value["object"]))
        for key, child in value.items():
            result.update(referenced_pids(child, key))
    elif isinstance(value, list):
        for child in value:
            if field == "part_of" and isinstance(child, str):
                result.add(child)
            else:
                result.update(referenced_pids(child, field))
    return result


def match_record(
    source_class: str,
    source: Mapping[str, object],
    downstream: Mapping[str, Mapping[str, object]],
    token_index: Mapping[str, set[str]],
) -> tuple[str | None, str, list[str]]:
    pid = str(source["pid"])
    if pid in downstream:
        return pid, "exact-pid", [pid]
    matches: set[str] = set()
    for token in identity_tokens(source):
        matches.update(token_index.get(token, set()))
    same_class = {
        candidate
        for candidate in matches
        if downstream[candidate]["class"] == source_class
    }
    if len(same_class) == 1:
        return next(iter(same_class)), "identifier", sorted(same_class)
    if same_class:
        return None, "ambiguous-identifier", sorted(same_class)
    return None, "unmatched", []


def tree_sha256(root: Path) -> str:
    return sha256(
        b"".join(
            path.relative_to(root).as_posix().encode()
            + b"\0"
            + path.read_bytes()
            + b"\0"
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    )


def render_markdown(report: Mapping[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# dump-research-info CON metadata review",
        "",
        "This is source-review evidence, not an approved site-metadata update.",
        "",
        "## Summary",
        "",
        f"- Source records: {summary['source_records']}",
        f"- Matched downstream records: {summary['matched_records']}",
        f"- Source-only candidate records: {summary['source_only_records']}",
        f"- Matched records with possible enrichment: {summary['enrichment_records']}",
        f"- Ambiguous identities: {summary['ambiguous_records']}",
        f"- Unresolved relation targets: {summary['unresolved_relation_targets']}",
        "",
        "## Source-only candidates by class",
        "",
    ]
    class_counts = summary["source_only_by_class"]
    assert isinstance(class_counts, dict)
    if class_counts:
        for class_name, count in sorted(class_counts.items()):
            lines.append(f"- `{class_name}`: {count}")
    else:
        lines.append("- None")
    lines.extend(["", "## Review warnings", ""])
    warnings = report["warnings"]
    assert isinstance(warnings, list)
    lines.extend(f"- {warning}" for warning in warnings or ["None"])
    lines.extend(
        [
            "",
            "Inspect `report.json`, `candidates/`, and `enrichment/` before",
            "copying any value into downstream site metadata.",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_output(output: Path) -> None:
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise DumpResearchInfoAdapterError(f"Output is not a safe directory: {output}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def flatten_source(
    source_records: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, dict[str, object]]:
    return {
        f"{class_name}:{pid}": dict(record)
        for class_name, records in sorted(source_records.items())
        for pid, record in sorted(records.items())
    }


def extract(
    source_checkout: Path,
    downstream_root: Path,
    output: Path,
    *,
    source_directory: str = "data/con_site",
    downstream_revision: str | None = None,
) -> dict[str, object]:
    source_checkout = source_checkout.resolve()
    downstream_root = downstream_root.resolve()
    output = output.resolve()
    source_root = source_checkout / source_directory
    if not source_root.is_dir():
        raise DumpResearchInfoAdapterError(
            f"Source directory does not exist: {source_root}"
        )
    prepare_output(output)

    source_records = load_source(source_root)
    downstream = load_yaml_records(downstream_root)
    token_index: dict[str, set[str]] = defaultdict(set)
    for pid, entry in downstream.items():
        record = entry["record"]
        assert isinstance(record, dict)
        for token in identity_tokens(record):
            token_index[token].add(pid)

    all_downstream_pids = set(downstream)
    source_only: dict[str, list[dict[str, object]]] = defaultdict(list)
    enrichment: dict[str, list[dict[str, object]]] = defaultdict(list)
    matches: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    unresolved_targets: set[str] = set()
    warnings: set[str] = set()

    for class_name, records in sorted(source_records.items()):
        for source_pid, source_record in sorted(records.items()):
            target_pid, method, candidates = match_record(
                class_name, source_record, downstream, token_index
            )
            if method == "ambiguous-identifier":
                ambiguous.append(
                    {
                        "source_class": class_name,
                        "source_pid": source_pid,
                        "candidate_pids": candidates,
                    }
                )
                continue
            if target_pid is None:
                source_only[class_name].append(source_record)
                missing_targets = referenced_pids(source_record) - all_downstream_pids
                unresolved_targets.update(missing_targets)
                if PLACEHOLDER.search(json.dumps(source_record, sort_keys=True)):
                    warnings.add(
                        f"{source_pid} contains an explicitly documented placeholder URL"
                    )
                continue
            target = downstream[target_pid]
            target_record = target["record"]
            assert isinstance(target_record, dict)
            delta = field_delta(source_record, target_record)
            matches.append(
                {
                    "source_class": class_name,
                    "source_pid": source_pid,
                    "downstream_pid": target_pid,
                    "downstream_path": target["path"],
                    "downstream_scope": target["scope"],
                    "match_method": method,
                }
            )
            if delta["missing_fields"] or delta["differing_fields"]:
                enrichment[class_name].append(
                    {
                        "source_pid": source_pid,
                        "downstream_pid": target_pid,
                        "downstream_path": target["path"],
                        **delta,
                    }
                )

    candidates_root = output / "candidates"
    enrichment_root = output / "enrichment"
    candidates_root.mkdir()
    enrichment_root.mkdir()
    for class_name, records in sorted(source_only.items()):
        (candidates_root / f"{class_name}.json").write_text(
            json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for class_name, records in sorted(enrichment.items()):
        (enrichment_root / f"{class_name}.json").write_text(
            json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    source_map = flatten_source(source_records)
    (output / "source-index.json").write_text(
        json.dumps(source_map, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    source_only_count = sum(len(records) for records in source_only.values())
    enrichment_count = sum(len(records) for records in enrichment.values())
    effective_downstream_revision = downstream_revision or git_commit(downstream_root)
    metadata_dirty = paths_differ_from_commit(
        downstream_root,
        effective_downstream_revision,
        ["metadata/records"],
    )
    report: dict[str, Any] = {
        "format": "orinoco-dump-research-info-review",
        "version": 1,
        "canonical_promotion": False,
        "inputs": {
            "dump_research_info_commit": git_commit(source_checkout),
            "dump_research_info_tree": git_tree(source_checkout, source_directory),
            "dump_research_info_dirty": git_dirty(source_checkout),
            "downstream_commit": effective_downstream_revision,
            "downstream_metadata_dirty": metadata_dirty,
            "adapter_sha256": sha256(Path(__file__).read_bytes()),
            "source_directory": source_directory,
            "source_records_sha256": sha256(canonical_json(source_map)),
        },
        "summary": {
            "source_records": len(source_map),
            "matched_records": len(matches),
            "source_only_records": source_only_count,
            "source_only_by_class": {
                class_name: len(records)
                for class_name, records in sorted(source_only.items())
            },
            "enrichment_records": enrichment_count,
            "ambiguous_records": len(ambiguous),
            "unresolved_relation_targets": len(unresolved_targets),
            "matched_without_delta": len(matches) - enrichment_count,
        },
        "matches": matches,
        "ambiguous": ambiguous,
        "unresolved_relation_targets": sorted(unresolved_targets),
        "warnings": sorted(warnings),
        "artifacts": {
            "candidate_tree_sha256": tree_sha256(candidates_root),
            "enrichment_tree_sha256": tree_sha256(enrichment_root),
        },
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def load_json_map(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DumpResearchInfoAdapterError(
            f"Cannot read evidence {path}: {error}"
        ) from error
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, dict)
        for key, value in payload.items()
    ):
        raise DumpResearchInfoAdapterError(f"Evidence is not a record map: {path}")
    return payload


def candidate_map(root: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    if not root.is_dir():
        return records
    for path in sorted(root.glob("XYZ*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DumpResearchInfoAdapterError(
                f"Cannot read candidate evidence {path}: {error}"
            ) from error
        if not isinstance(payload, list):
            raise DumpResearchInfoAdapterError(
                f"Candidate evidence is not a list: {path}"
            )
        for record in payload:
            if not isinstance(record, dict) or not isinstance(record.get("pid"), str):
                raise DumpResearchInfoAdapterError(
                    f"Candidate evidence has no PID: {path}"
                )
            identity = f"{path.stem}:{record['pid']}"
            if identity in records:
                raise DumpResearchInfoAdapterError(
                    f"Candidate evidence identity is duplicated: {identity}"
                )
            records[identity] = record
    return records


def canonical_gap_diff(output: Path, report: Mapping[str, object]) -> dict[str, object]:
    added = [
        {"id": identity, "record": record}
        for identity, record in sorted(candidate_map(output / "candidates").items())
    ]
    changed: list[dict[str, object]] = []
    for path in sorted((output / "enrichment").glob("XYZ*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload:
            changed.append({"id": record["downstream_pid"], "review": record})
    summary = report["summary"]
    assert isinstance(summary, dict)
    return {
        "status": "review-required",
        "summary": {
            "added": len(added),
            "removed": 0,
            "changed": len(changed),
            "unchanged": summary["matched_without_delta"],
            "different": bool(added or changed),
        },
        "added": added,
        "removed": [],
        "changed": changed,
    }


def review(context: Mapping[str, object]) -> dict[str, object]:
    from orinoco_metadata_review import semantic_diff

    root = Path(str(context["root"])).resolve()
    output_root = Path(str(context["output"])).resolve()
    config = context.get("config")
    if not isinstance(config, dict):
        raise DumpResearchInfoAdapterError("Adapter config must be a mapping")
    source_input = context.get("source_input")
    if not isinstance(source_input, str) or not source_input:
        raise DumpResearchInfoAdapterError(
            "A DataLad-provided checkout is required via "
            "--source-input dump-research-info=/path/to/dump-research-info"
        )
    source_checkout = Path(source_input).expanduser().resolve()
    if not source_checkout.is_dir() or source_checkout.is_symlink():
        raise DumpResearchInfoAdapterError(
            f"Source input is not an ordinary checkout directory: {source_checkout}"
        )
    source_directory = str(config.get("source_directory", "data/con_site"))
    evidence_root = (root / str(config["evidence_root"])).resolve()
    output = output_root / "review"
    report = extract(
        source_checkout,
        root,
        output,
        source_directory=source_directory,
    )

    reviewed_source = load_json_map(evidence_root / "source-index.json")
    live_source = load_json_map(output / "source-index.json")
    reviewed_candidates = candidate_map(evidence_root / "candidates")
    live_candidates = candidate_map(output / "candidates")
    reviewed_report_path = evidence_root / "report.json"
    reviewed_commit: str | None = None
    if reviewed_report_path.is_file():
        reviewed_report = json.loads(reviewed_report_path.read_text(encoding="utf-8"))
        reviewed_inputs = reviewed_report.get("inputs", {})
        if isinstance(reviewed_inputs, dict):
            value = reviewed_inputs.get("dump_research_info_commit")
            if isinstance(value, str):
                reviewed_commit = value
    inputs = report["inputs"]
    assert isinstance(inputs, dict)
    return {
        "adapter_api_version": ADAPTER_API_VERSION,
        "source_id": "dump-research-info",
        "canonical_promotion": False,
        "source": {
            "kind": "git-checkout",
            "source_directory": source_directory,
            "reviewed_version": reviewed_commit,
            "live_version": inputs["dump_research_info_commit"],
            "live_tree": inputs["dump_research_info_tree"],
            "input_dirty": inputs["dump_research_info_dirty"],
        },
        "source_diff": semantic_diff(reviewed_source, live_source),
        "candidate_diff": semantic_diff(reviewed_candidates, live_candidates),
        "canonical_diff": canonical_gap_diff(output, report),
        "blockers": (
            []
            if context.get("mode") == "review"
            else [
                "dump-research-info evidence must be generated by running the "
                "extract-dump-research-info command under datalad run"
            ]
        ),
        "warnings": report["warnings"],
        "summary": report["summary"],
        "artifacts": {
            "review": output.relative_to(root).as_posix(),
            "report": (output / "report.json").relative_to(root).as_posix(),
        },
        "evidence_updates": [],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--downstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-directory", default="data/con_site")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--downstream-revision")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    run_root = Path.cwd().resolve()
    if output == run_root or run_root not in output.parents:
        raise DumpResearchInfoAdapterError(
            "Standalone output must be a strict descendant of the DataLad run dataset"
        )
    observed_source_commit = git_commit(args.source.resolve())
    if (
        args.expected_source_commit is not None
        and observed_source_commit != args.expected_source_commit
    ):
        raise DumpResearchInfoAdapterError(
            "Source checkout moved: expected "
            f"{args.expected_source_commit}, found {observed_source_commit}"
        )
    if args.expected_source_commit is not None and git_dirty(args.source.resolve()):
        raise DumpResearchInfoAdapterError("Source checkout has uncommitted changes")
    if args.downstream_revision is not None:
        git_output(
            args.downstream.resolve(),
            "rev-parse",
            f"{args.downstream_revision}^{{commit}}",
        )
    report = extract(
        args.source,
        args.downstream,
        output,
        source_directory=args.source_directory,
        downstream_revision=args.downstream_revision,
    )
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
