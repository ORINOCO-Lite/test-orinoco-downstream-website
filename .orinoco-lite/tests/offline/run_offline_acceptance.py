#!/usr/bin/env python3
"""Prepare and run the warmed-cache acceptance gate behind an OS network deny."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[3]
STATE_RELATIVE = Path("build/offline/acceptance-state.json")
BUNDLE_RELATIVE = Path("build/offline/editor-review.json")
CATALOG_RELATIVE = Path("build/pages/edit/data/record-sources.json")
CONTRACT_RELATIVE = Path(".orinoco-lite/tests/browser/consumer-contract.json")
PROFILE_RELATIVE = Path(".orinoco-lite/tests/offline/macos-network-deny.sb")
STATE_FORMAT = "orinoco-warmed-cache-offline-acceptance"
STATE_VERSION = 1
EDITOR_REPORT_FORMAT = "orinoco-editor-apply-report"
EDITOR_REPORT_VERSION = 1
GIVEN_NAME = re.compile(
    r'(?P<prefix><[^>\s]+/given_name>\s+)'
    r'"(?P<value>(?:[^"\\]|\\.)*)"(?P<suffix>\s+\.)'
)

ONLINE_TASKS = (
    "verify-runtime",
    "assets-hydrate",
    "assets-verify",
    "build-browser-pages",
)
DENIED_TASKS = (
    "assets-verify",
    "validate",
    "projection-verify",
    "projection-update",
    "projection-verify",
    "build",
    "build-repeat",
)


class AcceptanceError(RuntimeError):
    """Raised when the offline acceptance contract cannot be proven."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    rendered = [str(item) for item in command]
    print(f"+ {shlex.join(rendered)}", flush=True)
    result = subprocess.run(
        rendered,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    if result.returncode:
        raise AcceptanceError(
            f"command failed with status {result.returncode}: {shlex.join(rendered)}"
        )
    return result


def _git(root: Path, *arguments: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace") if binary else result.stderr
        raise AcceptanceError(f"git {' '.join(arguments)} failed: {detail.strip()}")
    return result.stdout


def _head(root: Path) -> str:
    value = _git(root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise AcceptanceError("consumer HEAD did not resolve to a full commit")
    return value


def _require_clean_tracked(root: Path) -> None:
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    )
    if status:
        raise AcceptanceError(
            "offline acceptance requires a clean tracked worktree; "
            f"found:\n{status.rstrip()}"
        )


def _worktree_entry(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink():
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        return {
            "kind": "symlink",
            "mode": stat.S_IMODE(path.lstat().st_mode),
            "sha256": hashlib.sha256(b"symlink\0" + target).hexdigest(),
        }
    if not path.is_file():
        raise AcceptanceError(
            f"tracked path is absent or not a regular file: {relative}"
        )
    return {
        "kind": "file",
        "mode": stat.S_IMODE(path.stat().st_mode),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def tracked_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """Return exact index and worktree evidence for every tracked path."""

    output: bytes = _git(root, "ls-files", "--stage", "-z", binary=True)
    snapshot: dict[str, dict[str, Any]] = {}
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, separator, raw_path = raw.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            raise AcceptanceError("consumer index contains an unsupported staged entry")
        relative = os.fsdecode(raw_path)
        evidence = _worktree_entry(root, relative)
        evidence.update(
            {
                "index_mode": fields[0].decode("ascii"),
                "index_object": fields[1].decode("ascii"),
            }
        )
        snapshot[relative] = evidence
    if not snapshot:
        raise AcceptanceError("consumer repository has no tracked paths")
    return snapshot


def _assert_snapshot(
    root: Path,
    expected: dict[str, Any],
    *,
    label: str,
) -> None:
    observed = tracked_snapshot(root)
    changed = sorted(
        path
        for path in set(expected) | set(observed)
        if expected.get(path) != observed.get(path)
    )
    if changed:
        sample = ", ".join(changed[:20])
        raise AcceptanceError(
            f"tracked bytes changed {label}: {sample}"
            + (" ..." if len(changed) > 20 else "")
        )
    _require_clean_tracked(root)


def _json_report(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AcceptanceError("editor dry-run did not emit a JSON report")


def _assert_editor_report(report: dict[str, Any], source_path: str) -> None:
    expected = {
        "applied": False,
        "changed_paths": [source_path],
        "format": EDITOR_REPORT_FORMAT,
        "validated_records": 1,
        "version": EDITOR_REPORT_VERSION,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise AcceptanceError(
                f"editor dry-run report has {key}={report.get(key)!r}; "
                f"expected {value!r}"
            )
    difference = report.get("diff")
    if not isinstance(difference, str) or f"b/{source_path}" not in difference:
        raise AcceptanceError("editor dry-run did not report the bound canonical diff")


def construct_editor_bundle(root: Path, *, head: str) -> tuple[Path, str]:
    """Create a one-record fixture from the built catalog and consumer contract."""

    contract = _read_json(root / CONTRACT_RELATIVE)
    catalog = _read_json(root / CATALOG_RELATIVE)
    test_record = contract.get("test_record")
    review_contract = contract.get("review_bundle")
    if not isinstance(test_record, dict) or not isinstance(review_contract, dict):
        raise AcceptanceError("browser contract omits the editor fixture coordinates")
    pid = test_record.get("pid")
    source_path = test_record.get("source_path")
    edited_name = test_record.get("edited_given_name")
    if not all(
        isinstance(item, str) and item
        for item in (pid, source_path, edited_name)
    ):
        raise AcceptanceError("browser contract editor fixture is incomplete")
    if catalog.get("source_commit") != head:
        raise AcceptanceError("built editor catalog is not bound to the prepared HEAD")
    records = catalog.get("records")
    if not isinstance(records, list):
        raise AcceptanceError("built editor catalog has no records list")
    matches = [
        item
        for item in records
        if isinstance(item, dict) and item.get("pid") == pid
    ]
    if len(matches) != 1:
        raise AcceptanceError(
            f"built editor catalog does not contain exactly one {pid}"
        )
    source = matches[0]
    if source.get("path") != source_path:
        raise AcceptanceError(
            "built editor catalog source path differs from the contract"
        )
    canonical = root / source_path
    if not canonical.is_file() or canonical.is_symlink():
        raise AcceptanceError("editor fixture source is not a regular canonical file")
    source_sha256 = hashlib.sha256(canonical.read_bytes()).hexdigest()
    if source.get("sha256") != source_sha256:
        raise AcceptanceError("built editor catalog source digest is stale")
    rdf = source.get("rdf_turtle")
    schema_type = source.get("schema_type")
    if not isinstance(rdf, str) or not isinstance(schema_type, str):
        raise AcceptanceError("built editor catalog record lacks RDF or schema type")
    edited_rdf, count = GIVEN_NAME.subn(
        lambda match: (
            match.group("prefix")
            + json.dumps(edited_name, ensure_ascii=False)
            + match.group("suffix")
        ),
        rdf,
    )
    if count != 1 or edited_rdf == rdf:
        raise AcceptanceError("could not make exactly one bound given-name edit in RDF")
    bundle = {
        "format": review_contract.get("format"),
        "records": [
            {
                "pid": pid,
                "rdf_turtle": edited_rdf,
                "schema_type": schema_type,
                "source_path": source_path,
                "source_sha256": source_sha256,
            }
        ],
        "source_commit": head,
        "version": review_contract.get("version"),
    }
    path = root / BUNDLE_RELATIVE
    _write_json(path, bundle)
    return path, source_path


def _pixi(
    pixi: str,
    root: Path,
    *arguments: str | Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return _run([pixi, "run", "--as-is", *arguments], cwd=root, capture=capture)


def prepare(root: Path, state_path: Path) -> None:
    """Warm all accepted caches and record the exact tracked-byte baseline."""

    _require_root(root)
    _require_clean_tracked(root)
    before = tracked_snapshot(root)
    head = _head(root)
    pixi = shutil.which("pixi")
    if pixi is None:
        raise AcceptanceError("Pixi is unavailable")
    pixi = str(Path(pixi).resolve())
    _run([pixi, "install", "--frozen"], cwd=root)
    for task in ONLINE_TASKS:
        _pixi(pixi, root, task)
    bundle, source_path = construct_editor_bundle(root, head=head)
    report_result = _pixi(
        pixi,
        root,
        "apply-editor-bundle",
        "--",
        bundle,
        capture=True,
    )
    _assert_editor_report(_json_report(report_result.stdout), source_path)
    _assert_snapshot(root, before, label="during online preparation")
    state = {
        "bundle": bundle.relative_to(root).as_posix(),
        "format": STATE_FORMAT,
        "head": head,
        "pixi": pixi,
        "platform": platform.system(),
        "source_path": source_path,
        "tracked": before,
        "version": STATE_VERSION,
    }
    _write_json(state_path, state)
    print(f"online preparation complete: {state_path.relative_to(root)}")


def _load_state(root: Path, path: Path) -> dict[str, Any]:
    state = _read_json(path)
    if state.get("format") != STATE_FORMAT or state.get("version") != STATE_VERSION:
        raise AcceptanceError("offline acceptance state has an unsupported format")
    if state.get("head") != _head(root):
        raise AcceptanceError("consumer HEAD changed after online preparation")
    if state.get("platform") != platform.system():
        raise AcceptanceError("online preparation was created on a different platform")
    tracked = state.get("tracked")
    if not isinstance(tracked, dict):
        raise AcceptanceError(
            "offline acceptance state omits the tracked-byte snapshot"
        )
    _assert_snapshot(root, tracked, label="after online preparation")
    bundle = state.get("bundle")
    if not isinstance(bundle, str) or bundle != BUNDLE_RELATIVE.as_posix():
        raise AcceptanceError(
            "offline acceptance state names an unexpected editor bundle"
        )
    if not (root / bundle).is_file() or (root / bundle).is_symlink():
        raise AcceptanceError("prepared editor bundle is absent or not a regular file")
    pixi = state.get("pixi")
    if (
        not isinstance(pixi, str)
        or not Path(pixi).is_file()
        or not os.access(pixi, os.X_OK)
    ):
        raise AcceptanceError("prepared Pixi executable is unavailable")
    if not isinstance(state.get("source_path"), str):
        raise AcceptanceError("offline acceptance state omits the editor source path")
    return state


def _require_root(root: Path) -> None:
    if not (root / "orinoco.yaml").is_file() or not (root / ".git").exists():
        raise AcceptanceError(f"not an Orinoco consumer Git checkout: {root}")


def _prove_macos_network_deny() -> None:
    if platform.system() != "Darwin":
        raise AcceptanceError("the macOS denied phase is running on the wrong platform")
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            probe.sendto(b"orinoco-offline-probe", ("127.0.0.1", 9))
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EPERM}:
                raise AcceptanceError(
                    f"macOS network probe failed for a non-policy reason: {error}"
                ) from error
        else:
            raise AcceptanceError(
                "macOS network probe succeeded; sandbox-exec denial is not active"
            )
    finally:
        probe.close()


def validate_linux_namespace(
    host_namespace: str,
    current_namespace: str,
    interfaces: set[str],
    routes: Sequence[str],
) -> None:
    """Fail unless this is a new network namespace with no external interface."""

    if not host_namespace or current_namespace == host_namespace:
        raise AcceptanceError(
            "Linux denied phase did not enter a new network namespace"
        )
    if interfaces != {"lo"}:
        raise AcceptanceError(
            "Linux denied namespace must expose only loopback; found "
            + ", ".join(sorted(interfaces))
        )
    active_routes = [line for line in routes[1:] if line.strip()]
    if active_routes:
        raise AcceptanceError("Linux denied namespace unexpectedly contains a route")


def _prove_linux_network_deny(host_namespace: str | None) -> None:
    if platform.system() != "Linux":
        raise AcceptanceError("the Linux denied phase is running on the wrong platform")
    if host_namespace is None:
        raise AcceptanceError(
            "Linux denied phase is missing its host namespace witness"
        )
    try:
        current = os.readlink("/proc/self/ns/net")
        interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
        routes = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AcceptanceError(
            f"cannot inspect the Linux network namespace: {error}"
        ) from error
    validate_linux_namespace(host_namespace, current, interfaces, routes)


def denied_phase(
    root: Path,
    state_path: Path,
    *,
    boundary: str,
    host_namespace: str | None,
) -> None:
    """Run every claimed offline operation after proving the OS boundary."""

    if boundary == "macos-sandbox":
        _prove_macos_network_deny()
    elif boundary == "linux-unshare":
        _prove_linux_network_deny(host_namespace)
    else:
        raise AcceptanceError(f"unsupported network boundary: {boundary}")
    state = _load_state(root, state_path)
    pixi = state["pixi"]
    for task in DENIED_TASKS:
        _pixi(pixi, root, task)
    _pixi(pixi, root, "--skip-deps", "verify-deterministic")
    editor = _pixi(
        pixi,
        root,
        "apply-editor-bundle",
        "--",
        root / state["bundle"],
        capture=True,
    )
    _assert_editor_report(_json_report(editor.stdout), state["source_path"])
    _assert_snapshot(root, state["tracked"], label="during denied-network acceptance")
    print(
        json.dumps(
            {
                "boundary": boundary,
                "head": state["head"],
                "status": "passed",
                "tracked_files": len(state["tracked"]),
            },
            sort_keys=True,
        )
    )


def _denied_arguments(
    script: Path,
    root: Path,
    state_path: Path,
    boundary: str,
    host_namespace: str | None = None,
) -> list[str]:
    arguments = [
        str(script),
        "--root",
        str(root),
        "--state",
        str(state_path),
        "_denied",
        "--boundary",
        boundary,
    ]
    if host_namespace is not None:
        arguments.extend(["--host-namespace", host_namespace])
    return arguments


def deny(root: Path, state_path: Path) -> None:
    """Launch the prepared gate through the supported platform's OS boundary."""

    _require_root(root)
    _load_state(root, state_path)
    script = Path(__file__).resolve()
    system = platform.system()
    if system == "Darwin":
        sandbox = shutil.which("sandbox-exec")
        profile = root / PROFILE_RELATIVE
        if sandbox is None or not profile.is_file():
            raise AcceptanceError(
                "macOS sandbox-exec or its deny profile is unavailable"
            )
        command = [
            sandbox,
            "-f",
            str(profile),
            sys.executable,
            *_denied_arguments(
                script,
                root,
                state_path,
                "macos-sandbox",
            ),
        ]
    elif system == "Linux":
        unshare = shutil.which("unshare")
        if unshare is None:
            raise AcceptanceError(
                "Linux unshare is unavailable; refusing a proxy simulation"
            )
        try:
            host_namespace = os.readlink("/proc/self/ns/net")
        except OSError as error:
            raise AcceptanceError(
                f"cannot record the host network namespace: {error}"
            ) from error
        prefix: list[str] = []
        if os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if sudo is None:
                raise AcceptanceError(
                    "passwordless sudo is required for Linux unshare; "
                    "refusing a proxy simulation"
                )
            prefix = [sudo, "-n"]
        environment = shutil.which("env")
        if environment is None:
            raise AcceptanceError("env is unavailable for the Linux denied child")
        command = [
            *prefix,
            unshare,
            "--net",
            f"--setgid={os.getgid()}",
            f"--setuid={os.getuid()}",
            "--",
            environment,
            f"HOME={Path.home()}",
            f"PATH={os.environ.get('PATH', '')}",
            sys.executable,
            *_denied_arguments(
                script,
                root,
                state_path,
                "linux-unshare",
                host_namespace,
            ),
        ]
    else:
        raise AcceptanceError(
            f"unsupported platform {system!r}; no OS-level network deny is defined"
        )
    _run(command, cwd=root)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=ROOT)
    result.add_argument("--state", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="online frozen install and cache preparation")
    commands.add_parser("deny", help="run prepared commands behind an OS network deny")
    internal = commands.add_parser("_denied", help="internal OS-boundary child")
    internal.add_argument(
        "--boundary",
        choices=("macos-sandbox", "linux-unshare"),
        required=True,
    )
    internal.add_argument("--host-namespace")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    state_path = (args.state or (root / STATE_RELATIVE)).resolve()
    try:
        if root not in state_path.parents:
            raise AcceptanceError("offline state must remain below the consumer root")
        if args.command == "prepare":
            prepare(root, state_path)
        elif args.command == "deny":
            deny(root, state_path)
        else:
            denied_phase(
                root,
                state_path,
                boundary=args.boundary,
                host_namespace=args.host_namespace,
            )
    except AcceptanceError as error:
        print(f"offline acceptance failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
