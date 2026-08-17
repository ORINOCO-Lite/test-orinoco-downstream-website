#!/usr/bin/env python3
"""Record validation results without changing update coordinates or content hashes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from template_contract import ContractError, find_root


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--status", choices=("passed", "failed"), required=True)
    result.add_argument("--command", action="append", default=[])
    result.add_argument("--root", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = find_root(args.root)
        path = root / ".orinoco-lite" / "state" / "framework-update.json"
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(f"cannot read update ledger {path}: {error}") from error
        if not isinstance(ledger, dict) or ledger.get("ledger_version") != 2:
            raise ContractError("update ledger must use ledger_version 2")
        ledger["validation"] = {
            "status": args.status,
            "commands": args.command,
        }
        if args.status == "failed":
            ledger["status"] = "failed-validation"
        path.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except ContractError as error:
        print(f"cannot finalize update ledger: {error}", file=sys.stderr)
        return 2
    print(f"update validation recorded as {args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
