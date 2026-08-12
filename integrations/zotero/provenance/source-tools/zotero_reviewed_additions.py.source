#!/usr/bin/env python3
"""Apply human-reviewed Zotero additions and verify them in a public snapshot."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import secrets
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml


API_ROOT = "https://api.zotero.org"
API_HEADERS = {
    "User-Agent": "CON-metadata-maintenance/0.1",
    "Zotero-API-Version": "3",
}


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi.removeprefix(prefix)
    return doi


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any = None,
) -> Any:
    request_headers = {**API_HEADERS, **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Zotero API request failed with HTTP {error.code}: {detail}"
        ) from error


def load_review(path: Path) -> dict[str, Any]:
    review = yaml.safe_load(path.read_text(encoding="utf-8"))
    if review.get("format_version") != 1:
        raise ValueError(f"Unsupported review format in {path}")
    if not review.get("group_id"):
        raise ValueError(f"Missing Zotero group ID in {path}")

    seen_dois: set[str] = set()
    for addition in review.get("additions", []):
        if addition.get("status") not in {"draft", "approved"}:
            raise ValueError(f"Invalid review status for {addition.get('id')!r}")
        item = addition.get("item", {})
        doi = normalize_doi(item.get("DOI"))
        if not addition.get("id") or not doi or doi in seen_dois:
            raise ValueError(f"Invalid or duplicate reviewed DOI: {doi!r}")
        seen_dois.add(doi)
        if addition["status"] == "approved":
            if not addition.get("approved_by") or not addition.get("approved_on"):
                raise ValueError(f"Approved addition {addition['id']!r} lacks reviewer")
            date.fromisoformat(str(addition["approved_on"]))
            if not addition.get("rationale") or not addition.get("collection"):
                raise ValueError(
                    f"Approved addition {addition['id']!r} lacks rationale or collection"
                )
    return review


def fetch_all(path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    start = 0
    while True:
        parameters = {**(query or {}), "limit": 100, "start": start}
        page = request_json(f"{API_ROOT}{path}?{urlencode(parameters)}")
        if not isinstance(page, list):
            raise RuntimeError(f"Expected a list response from Zotero path {path}")
        values.extend(page)
        if len(page) < 100:
            return values
        start += len(page)


def collection_key(group_id: str, name: str) -> str:
    collections = fetch_all(f"/groups/{group_id}/collections")
    matches = [
        entry["key"] for entry in collections if entry.get("data", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one Zotero collection named {name!r}, found {len(matches)}"
        )
    return matches[0]


def live_doi_matches(group_id: str, doi: str) -> list[dict[str, Any]]:
    candidates = fetch_all(
        f"/groups/{group_id}/items/top",
        {"q": doi, "qmode": "everything"},
    )
    return [
        entry
        for entry in candidates
        if normalize_doi(entry.get("data", {}).get("DOI")) == doi
    ]


def apply_reviewed(review: dict[str, Any], api_key_file: Path) -> None:
    api_key = api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError(f"Empty Zotero API key file: {api_key_file}")
    group_id = str(review["group_id"])

    for addition in review.get("additions", []):
        if addition["status"] != "approved":
            print(f"Skipped unapproved Zotero addition: {addition['id']}")
            continue
        doi = normalize_doi(addition["item"]["DOI"])
        target_collection = collection_key(group_id, addition["collection"])
        existing = live_doi_matches(group_id, doi)
        if len(existing) > 1:
            raise RuntimeError(f"Multiple live Zotero items have DOI {doi}")
        if existing:
            item = existing[0]
            if target_collection not in item.get("data", {}).get("collections", []):
                raise RuntimeError(
                    f"Existing Zotero item {item['key']} is not in "
                    f"{addition['collection']!r}; refusing an unreviewed reclassification"
                )
            print(f"Already present: {doi} ({item['key']})")
            continue

        item_type = addition["item"].get("itemType")
        template = request_json(
            f"{API_ROOT}/items/new?{urlencode({'itemType': item_type})}"
        )
        unknown_fields = sorted(set(addition["item"]) - set(template))
        if unknown_fields:
            raise ValueError(
                f"Unsupported Zotero fields for {addition['id']}: {unknown_fields}"
            )
        template.update(addition["item"])
        template["collections"] = [target_collection]
        response = request_json(
            f"{API_ROOT}/groups/{group_id}/items",
            method="POST",
            headers={
                "Zotero-API-Key": api_key,
                "Zotero-Write-Token": secrets.token_hex(16),
            },
            payload=[template],
        )
        failed = response.get("failed", {})
        if failed:
            raise RuntimeError(f"Zotero rejected {addition['id']}: {failed}")
        saved = response.get("successful", {}).get("0")
        if not saved:
            raise RuntimeError(f"Zotero did not confirm creation of {addition['id']}")
        item_key = saved.get("key") if isinstance(saved, dict) else str(saved)
        print(f"Added: {doi} ({item_key}) -> {addition['collection']}")


def check_snapshot(review: dict[str, Any], snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    collections = snapshot.get("collections", [])
    items = snapshot.get("items", [])

    for addition in review.get("additions", []):
        if addition["status"] != "approved":
            continue
        collection_matches = [
            entry["key"]
            for entry in collections
            if entry.get("data", {}).get("name") == addition["collection"]
        ]
        if len(collection_matches) != 1:
            raise RuntimeError(
                f"Snapshot does not contain exactly one {addition['collection']!r} collection"
            )
        doi = normalize_doi(addition["item"]["DOI"])
        item_matches = [
            entry
            for entry in items
            if normalize_doi(entry.get("data", {}).get("DOI")) == doi
        ]
        if len(item_matches) != 1:
            raise RuntimeError(
                f"Snapshot contains {len(item_matches)} items with reviewed DOI {doi}"
            )
        if collection_matches[0] not in item_matches[0].get("data", {}).get(
            "collections", []
        ):
            raise RuntimeError(
                f"Reviewed DOI {doi} is not in {addition['collection']!r}"
            )
    approved_count = sum(
        addition["status"] == "approved" for addition in review.get("additions", [])
    )
    print(f"Verified {approved_count} reviewed Zotero additions in {snapshot_path}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check-snapshot", action="store_true")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--snapshot", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    review = load_review(args.review)
    if args.apply:
        if args.api_key_file is None:
            raise SystemExit("--api-key-file is required with --apply")
        apply_reviewed(review, args.api_key_file)
    else:
        if args.snapshot is None:
            raise SystemExit("--snapshot is required with --check-snapshot")
        check_snapshot(review, args.snapshot)


if __name__ == "__main__":
    main()
