#!/usr/bin/env python3
"""Extract review candidates absent from downstream canonical metadata."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

import yaml


DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
PLACEHOLDER = re.compile(r"reporter\.nih\.gov/search/x[12]x[12]x[12]x[12]x[12]", re.IGNORECASE)
RELATION_KEYS = {"associated_with", "attributed_to", "generated_by", "part_of"}


class ExtractionError(RuntimeError):
    """Report malformed source or downstream metadata."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_commit(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD^{commit}"], text=True
    ).strip()


def load_source(root: Path) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    seen: set[str] = set()
    for path in sorted(root.glob("XYZ*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ExtractionError(f"Source class is not a list: {path}")
        records: dict[str, dict[str, object]] = {}
        for record in payload:
            if not isinstance(record, dict) or not isinstance(record.get("pid"), str):
                raise ExtractionError(f"Source record has no PID: {path}")
            pid = str(record["pid"])
            if pid in seen:
                raise ExtractionError(f"Source PID is duplicated: {pid}")
            seen.add(pid)
            records[pid] = record
        result[path.stem] = records
    return result


def load_yaml_records(downstream_root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for scope, relative_root in (
        ("canonical", Path("metadata/records")),
        ("reference", Path("metadata/reference")),
    ):
        root = downstream_root / relative_root
        for path in sorted(root.rglob("*.yaml")):
            if path.name.startswith("."):
                continue
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("pid"), str):
                raise ExtractionError(f"Downstream record has no PID: {path}")
            pid = str(payload["pid"])
            if pid in result:
                raise ExtractionError(f"Downstream PID is duplicated: {pid}")
            result[pid] = {
                "class": path.parent.name,
                "path": path.relative_to(downstream_root).as_posix(),
                "record": payload,
                "scope": scope,
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
    downstream_values = {canonical_json(normalize_value(value)) for value in downstream}
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


def render_markdown(report: Mapping[str, object]) -> str:
    summary = report["summary"]
    lines = [
        "# CON site metadata gap",
        "",
        "This is review evidence, not an approved canonical metadata update.",
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
    for class_name, count in sorted(summary["source_only_by_class"].items()):
        lines.append(f"- `{class_name}`: {count}")
    lines.extend(["", "## Review warnings", ""])
    warnings = report["warnings"]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "Inspect `report.json`, `candidates/`, and `enrichment/` before",
            "copying any value into the downstream repository.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--downstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source.resolve()
    downstream_root = args.downstream.resolve()
    output = args.output.resolve()
    dataset_root = Path.cwd().resolve()
    if output == dataset_root or dataset_root not in output.parents:
        raise ExtractionError("Output must be a strict descendant of the run dataset")
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ExtractionError(f"Output is not a safe directory: {output}")
    if output.exists():
        shutil.rmtree(output)
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

    output.mkdir(parents=True, exist_ok=True)
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

    source_only_count = sum(len(records) for records in source_only.values())
    enrichment_count = sum(len(records) for records in enrichment.values())
    report: dict[str, Any] = {
        "format": "orinoco-con-site-metadata-gap",
        "version": 1,
        "canonical_promotion": False,
        "inputs": {
            "dump_research_info_commit": git_commit(source_root),
            "downstream_commit": git_commit(downstream_root),
            "extractor_sha256": sha256(Path(__file__).read_bytes()),
            "source_directory": "data/con_site",
        },
        "summary": {
            "source_records": sum(len(records) for records in source_records.values()),
            "matched_records": len(matches),
            "source_only_records": source_only_count,
            "source_only_by_class": {
                class_name: len(records)
                for class_name, records in sorted(source_only.items())
            },
            "enrichment_records": enrichment_count,
            "ambiguous_records": len(ambiguous),
            "unresolved_relation_targets": len(unresolved_targets),
        },
        "matches": matches,
        "ambiguous": ambiguous,
        "unresolved_relation_targets": sorted(unresolved_targets),
        "warnings": sorted(warnings),
        "artifacts": {
            "candidate_tree_sha256": sha256(
                b"".join(
                    path.relative_to(output).as_posix().encode()
                    + b"\0"
                    + path.read_bytes()
                    + b"\0"
                    for path in sorted(candidates_root.glob("*.json"))
                )
            ),
            "enrichment_tree_sha256": sha256(
                b"".join(
                    path.relative_to(output).as_posix().encode()
                    + b"\0"
                    + path.read_bytes()
                    + b"\0"
                    for path in sorted(enrichment_root.glob("*.json"))
                )
            ),
        },
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
