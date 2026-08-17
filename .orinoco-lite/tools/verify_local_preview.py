#!/usr/bin/env python3
"""Prove that one host-neutral local artifact works on both loopback names."""

from __future__ import annotations

import argparse
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


LOOPBACK_ORIGIN = re.compile(
    rb"https?://(?:127\.0\.0\.1|localhost|\[::1\])"
    rb"(?::[0-9]+)?(?=[/?#\s\"']|$)",
    re.IGNORECASE,
)
REFERENCE_ATTRIBUTES = {"href", "src", "action", "poster"}
IGNORED_SCHEMES = {"data", "javascript", "mailto", "tel"}


class LocalPreviewError(RuntimeError):
    """Raised when a local static artifact is not host-neutral and complete."""


class ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name.lower() in REFERENCE_ATTRIBUTES and value:
                self.references.append(value)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def audit_host_neutral(site: Path) -> int:
    """Reject symlinks and every baked loopback origin in the artifact."""

    if not site.is_dir() or not (site / "index.html").is_file():
        raise LocalPreviewError(f"local static site is absent at {site}")
    files = 0
    for path in sorted(site.rglob("*")):
        relative = path.relative_to(site).as_posix()
        if path.is_symlink():
            raise LocalPreviewError(
                f"local static site contains a symbolic link: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise LocalPreviewError(
                f"local static site contains a non-regular file: {relative}"
            )
        files += 1
        if LOOPBACK_ORIGIN.search(path.read_bytes()):
            raise LocalPreviewError(
                f"local static site embeds a loopback origin: {relative}"
            )
    if not files:
        raise LocalPreviewError(f"local static site is empty: {site}")
    return files


def fetch(url: str) -> bytes:
    try:
        with urlopen(url, timeout=10) as response:
            if response.status != 200:
                raise LocalPreviewError(
                    f"local preview returned HTTP {response.status}: {url}"
                )
            return response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise LocalPreviewError(f"local preview request failed: {url}: {error}") from error


def verify_origin(origin: str) -> int:
    """Fetch the entry point and every same-origin reference it declares."""

    index = fetch(origin)
    try:
        text = index.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LocalPreviewError(f"local preview index is not UTF-8: {origin}") from error
    parser = ReferenceParser()
    parser.feed(text)
    origin_netloc = urlsplit(origin).netloc
    references: set[str] = set()
    for reference in parser.references:
        parsed = urlsplit(reference)
        if parsed.scheme.lower() in IGNORED_SCHEMES:
            continue
        target = urljoin(origin, reference)
        target_url = urlsplit(target)
        if target_url.scheme not in {"http", "https"}:
            continue
        if target_url.netloc != origin_netloc:
            continue
        references.add(target)
    if not references:
        raise LocalPreviewError(
            f"local preview index exposes no same-origin references: {origin}"
        )
    for target in sorted(references):
        fetch(target)
    return len(references)


def verify_local_preview(site: Path) -> dict[str, object]:
    site = site.resolve()
    files = audit_host_neutral(site)
    handler = partial(QuietHandler, directory=str(site))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    origins = [
        f"http://127.0.0.1:{port}/",
        f"http://localhost:{port}/",
    ]
    try:
        references = {
            urlsplit(origin).hostname or origin: verify_origin(origin)
            for origin in origins
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    return {
        "files": files,
        "hostnames": sorted(references),
        "references": references,
        "version": 1,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("site", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = verify_local_preview(args.site)
    except LocalPreviewError as error:
        print(f"local preview verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
