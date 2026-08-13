"""Build the Pages artifact with an explicit, validated base URL."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def _base_url() -> str:
    value = os.environ.get("ORINOCO_BASE_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(
            "ORINOCO_BASE_URL must be an absolute HTTP(S) URL; "
            "configure project Pages before running build-pages"
        )
    if parsed.query or parsed.fragment:
        raise SystemExit("ORINOCO_BASE_URL must not contain a query or fragment")
    return value.rstrip("/") + "/"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    destination = Path(args[0]) if args else Path("build/pages")
    subprocess.run(
        [
            "orinoco",
            "build",
            "--destination",
            str(destination),
            "--base-url",
            _base_url(),
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
