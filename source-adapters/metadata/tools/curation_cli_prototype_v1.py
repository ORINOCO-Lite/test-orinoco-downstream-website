#!/usr/bin/env python3
"""Run the site-owned Milestone 5 curation transaction prototype."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date
import errno
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import ModuleType
from typing import Callable, Mapping, Sequence

import yaml


EXACT_ENGINE_VERSION = "0.1.12"
ADAPTER_ALIASES = {
    "zotero": "zotero",
    "dump": "dump-research-info",
    "dump-research-info": "dump-research-info",
}
DECISION_DISPOSITIONS = (
    "accept",
    "reject",
    "link",
    "defer",
    "permanent-exclude",
    "supersede",
)
DECISION_DETAILS = {
    "target_record_id",
    "return_when",
    "scope",
    "replacement_candidate_id",
}
REPORT_RESERVATION_FORMAT = "orinoco-lite-curation-report-reservation-v1"
PROVIDER_PATHS = {
    "zotero": Path("source-adapters/zotero/curation_prototype_v1.py"),
    "dump-research-info": Path(
        "source-adapters/dump-research-info/curation_prototype_v1.py"
    ),
}
StageValidator = Callable[[Path], None]


@dataclass
class ReportReservation:
    path: Path
    descriptor: int
    marker: bytes
    operation: str
    token: str
    active: bool = True


class CurationCliError(RuntimeError):
    """Report an unsafe path or incomplete curation transaction request."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _adapter(value: str) -> str:
    try:
        return ADAPTER_ALIASES[value]
    except KeyError as error:
        raise CurationCliError(f"Unsupported source adapter: {value}") from error


def _repository_root(value: Path) -> Path:
    root = value.resolve()
    if not root.is_dir():
        raise CurationCliError(f"Repository root does not exist: {value}")
    return root


def _absolute(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # ``TemporaryDirectory`` is spelled through /var on macOS while its
    # canonical location is under /private/var.  Canonicalize before scope
    # comparisons so an in-scope absolute path cannot fail only because of
    # that operating-system alias.
    return Path(os.path.abspath(path)).resolve(strict=False)


def _reject_symlinks(root: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise CurationCliError(
            f"{label} escapes the repository root: {path}"
        ) from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CurationCliError(f"{label} may not traverse a symlink: {current}")


def _scoped_yaml_path(
    root: Path,
    adapter_id: str,
    value: str | Path,
    *,
    area: str,
    label: str,
    must_exist: bool,
) -> Path:
    if area not in {"transactions", "policy"}:
        raise AssertionError(f"Unhandled curation area: {area}")
    literal_scope = root / "source-adapters" / adapter_id / area
    _reject_symlinks(root, literal_scope, label=f"{area} area")
    scope = _absolute(root, literal_scope)
    path = _absolute(root, value)
    try:
        path.relative_to(scope)
    except ValueError as error:
        raise CurationCliError(
            f"{label} must be inside source-adapters/{adapter_id}/{area}: {value}"
        ) from error
    try:
        path.resolve(strict=False).relative_to(scope.resolve(strict=False))
    except ValueError as error:
        raise CurationCliError(
            f"{label} resolves outside its allowed area: {value}"
        ) from error
    _reject_symlinks(root, path, label=label)
    if path.suffix != ".yaml":
        raise CurationCliError(f"{label} must use the .yaml suffix: {value}")
    if must_exist:
        if not path.is_file():
            raise CurationCliError(f"{label} does not exist: {value}")
    elif path.exists() and not path.is_file():
        raise CurationCliError(f"{label} is not a regular file: {value}")
    return path


def _provider_output_path(root: Path, adapter_id: str, value: str | Path) -> Path:
    literal_scope = root / "build/curation" / adapter_id
    _reject_symlinks(root, literal_scope, label="provider output area")
    scope = _absolute(root, literal_scope)
    path = _absolute(root, value)
    try:
        path.relative_to(scope)
        path.resolve(strict=False).relative_to(scope.resolve(strict=False))
    except ValueError as error:
        raise CurationCliError(
            f"provider output must be inside build/curation/{adapter_id}: {value}"
        ) from error
    _reject_symlinks(root, path, label="provider output")
    if path.exists() and not path.is_dir():
        raise CurationCliError(f"provider output is not a directory: {value}")
    return path


def _records_root(
    root: Path,
    *,
    must_exist: bool = True,
    core: ModuleType | None = None,
    adapter_id: str | None = None,
) -> Path:
    records = root / "metadata/records"
    _reject_symlinks(root, records, label="canonical metadata root")
    if must_exist and not records.is_dir():
        stage_prefix = getattr(core, "STAGE_PREFIX", None)
        artifacts = (
            tuple(
                sorted(
                    path
                    for path in records.parent.glob(f"{stage_prefix}*")
                    if path.exists() or path.is_symlink()
                )
            )
            if isinstance(stage_prefix, str) and stage_prefix
            else ()
        )
        if artifacts and adapter_id is not None:
            artifact_names = ", ".join(path.name for path in artifacts)
            raise CurationCliError(
                "Canonical metadata root is missing while an interrupted "
                f"reconciliation remains ({artifact_names}); run `recover --adapter "
                f"{adapter_id} --report source-adapters/{adapter_id}/transactions/"
                "<recovery-report>.yaml` before propose or reconcile"
            )
        raise CurationCliError(f"Canonical metadata root is missing: {records}")
    if records.exists() and not records.is_dir():
        raise CurationCliError(f"Canonical metadata root is not a directory: {records}")
    return records


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise CurationCliError(f"Cannot load curation module: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _load_core(root: Path) -> ModuleType:
    return _load_module(
        "orinoco_site_curation_core_prototype_v1",
        root / "source-adapters/metadata/tools/curation_prototype_v1.py",
    )


def _load_provider(root: Path, adapter_id: str) -> ModuleType:
    return _load_module(
        f"orinoco_site_{adapter_id.replace('-', '_')}_curation_prototype_v1",
        root / PROVIDER_PATHS[adapter_id],
    )


def _empty_decisions(core: ModuleType):
    return core.parse_decisions(
        yaml.safe_dump(
            {
                "format": core.DECISIONS_FORMAT,
                "decisions": [],
                "transactions": [],
            },
            sort_keys=False,
        )
    )


def _read_authority(path: Path, *, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise CurationCliError(f"Cannot read {label} {path}: {error}") from error


def _require_unchanged(path: Path, expected: bytes, *, label: str) -> None:
    observed = _read_authority(path, label=label)
    if observed != expected:
        raise CurationCliError(f"{label} changed during the transaction: {path}")


def _evaluation_context(
    core: ModuleType,
    *,
    as_of: date,
    resolved_policy_questions: Sequence[str],
):
    if type(as_of) is not date:
        raise CurationCliError("propose requires an explicit ISO as-of date")
    questions = frozenset(resolved_policy_questions)
    if len(questions) != len(resolved_policy_questions):
        raise CurationCliError("resolved policy questions must be unique")
    return core.EvaluationContext(
        as_of=as_of,
        resolved_policy_questions=questions,
    )


def _provider_result(
    provider: ModuleType,
    *,
    adapter_id: str,
    root: Path,
    output: Path,
    expected_library_version: int | None,
    source_path: str | None,
    expected_source_commit: str | None,
    source_run_id: str | None,
) -> Mapping[str, object]:
    if adapter_id == "zotero":
        if expected_library_version is None:
            raise CurationCliError("zotero propose requires --expected-library-version")
        if (
            source_path is not None
            or expected_source_commit is not None
            or source_run_id is not None
        ):
            raise CurationCliError("zotero propose does not accept dump source options")
        result = provider.build_candidates(
            root,
            output,
            expected_library_version=expected_library_version,
        )
    else:
        if expected_library_version is not None:
            raise CurationCliError(
                "dump-research-info propose does not accept a Zotero library version"
            )
        if source_path is None or expected_source_commit is None:
            raise CurationCliError(
                "dump-research-info propose requires --source-path and "
                "--expected-source-commit"
            )
        result = provider.build_candidates(
            root,
            output,
            source_path=source_path,
            expected_source_commit=expected_source_commit,
            source_run_id=source_run_id,
        )
    if not isinstance(result, Mapping):
        raise CurationCliError("Candidate provider returned a non-mapping result")
    if result.get("adapter_id") != adapter_id:
        raise CurationCliError("Candidate provider returned the wrong adapter id")
    if not isinstance(result.get("source"), Mapping):
        raise CurationCliError(
            "Candidate provider omitted deterministic source evidence"
        )
    if not isinstance(result.get("policy"), Mapping):
        raise CurationCliError(
            "Candidate provider omitted deterministic policy evidence"
        )
    if not isinstance(result.get("implementation"), Mapping):
        raise CurationCliError(
            "Candidate provider omitted deterministic implementation evidence"
        )
    candidates = result.get("candidates")
    if not isinstance(candidates, (list, tuple)):
        raise CurationCliError("Candidate provider returned an invalid candidate list")
    return result


def _write_inventory_append_only(
    core: ModuleType,
    path: Path,
    inventory: Mapping[str, object],
    *,
    decisions_path: Path | None,
    scratch_dir: Path,
    before_create: Callable[[], None] | None = None,
) -> None:
    """Atomically create one inventory path or preserve an identical one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    if path.parent.stat().st_dev != scratch_dir.stat().st_dev:
        raise CurationCliError(
            "Inventory publication scratch and transaction directory must be on "
            "the same filesystem"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=scratch_dir,
        prefix=f".{path.name}.",
        suffix=".yaml",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        core.write_inventory(
            temporary,
            inventory,
            decisions_path=decisions_path,
        )
        if temporary.is_symlink() or not temporary.is_file():
            raise CurationCliError(
                f"Inventory renderer did not create a regular file: {temporary}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        rendered_descriptor = os.open(temporary, flags)
        try:
            os.fsync(rendered_descriptor)
        finally:
            os.close(rendered_descriptor)
        if before_create is not None:
            before_create()
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = core.load_inventory(path).to_mapping()
            except Exception as error:
                raise CurationCliError(
                    f"Append-only inventory path already contains invalid content: {path}"
                ) from error
            if existing != dict(inventory):
                raise CurationCliError(
                    f"Append-only inventory path already contains different content: {path}"
                )
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise CurationCliError(
                    "Inventory publication cannot cross filesystem boundaries"
                ) from error
            raise
    finally:
        temporary.unlink(missing_ok=True)


def propose(
    root: Path,
    *,
    adapter: str,
    inventory_path: str | Path,
    provider_output: str | Path,
    as_of: date,
    resolved_policy_questions: Sequence[str] = (),
    decisions_path: str | Path | None = None,
    expected_library_version: int | None = None,
    source_path: str | None = None,
    expected_source_commit: str | None = None,
    source_run_id: str | None = None,
    core: ModuleType | None = None,
    provider: ModuleType | None = None,
) -> dict[str, object]:
    """Generate one deterministic proposal without creating decision state."""

    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    inventory = _scoped_yaml_path(
        root,
        adapter_id,
        inventory_path,
        area="transactions",
        label="inventory path",
        must_exist=False,
    )
    output = _provider_output_path(root, adapter_id, provider_output)
    decision_file = None
    decision_bytes = None
    if decisions_path is not None:
        decision_file = _scoped_yaml_path(
            root,
            adapter_id,
            decisions_path,
            area="policy",
            label="decisions path",
            must_exist=True,
        )
        decision_bytes = _read_authority(decision_file, label="decisions")

    core = core or _load_core(root)
    records = _records_root(
        root,
        core=core,
        adapter_id=adapter_id,
    )
    provider = provider or _load_provider(root, adapter_id)
    decisions = (
        core.load_decisions(decision_file)
        if decision_file is not None
        else _empty_decisions(core)
    )
    context = _evaluation_context(
        core,
        as_of=as_of,
        resolved_policy_questions=resolved_policy_questions,
    )
    result = _provider_result(
        provider,
        adapter_id=adapter_id,
        root=root,
        output=output,
        expected_library_version=expected_library_version,
        source_path=source_path,
        expected_source_commit=expected_source_commit,
        source_run_id=source_run_id,
    )
    if decision_file is not None and decision_bytes is not None:
        _require_unchanged(decision_file, decision_bytes, label="decisions")

    built = core.build_inventory(
        adapter_id,
        result["candidates"],
        decisions,
        context=context,
        metadata_dir=records,
        inputs={
            "source": dict(result["source"]),
            "policy": dict(result["policy"]),
            "implementation": dict(result["implementation"]),
        },
    )

    def before_create() -> None:
        if decision_file is not None and decision_bytes is not None:
            _require_unchanged(decision_file, decision_bytes, label="decisions")

    scratch_dir = output / ".inventory-publication"
    _reject_symlinks(root, output, label="provider output")
    _reject_symlinks(root, scratch_dir, label="inventory publication scratch")
    _write_inventory_append_only(
        core,
        inventory,
        built,
        decisions_path=decision_file,
        scratch_dir=scratch_dir,
        before_create=before_create,
    )
    if decision_file is not None and decision_bytes is not None:
        _require_unchanged(decision_file, decision_bytes, label="decisions")
    return built


def _prepare_atomic_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CurationCliError(f"Report path is not a regular file: {path}")


def _reservation_marker(
    *,
    operation: str,
    token: str,
    state: str,
    report: Mapping[str, object] | None,
    pid: int | None = None,
) -> bytes:
    if state not in {"reserved", "prepared"}:
        raise CurationCliError(f"Unsupported report reservation state: {state}")
    if (state == "prepared") != (report is not None):
        raise CurationCliError("Prepared report reservation state is inconsistent")
    report_payload = None if report is None else _canonical_json_bytes(report)
    return (
        json.dumps(
            {
                "format": REPORT_RESERVATION_FORMAT,
                "operation": operation,
                "pid": os.getpid() if pid is None else pid,
                "token": token,
                "state": state,
                "report": None if report is None else dict(report),
                "report_sha256": (
                    None
                    if report_payload is None
                    else hashlib.sha256(report_payload).hexdigest()
                ),
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _parse_reservation_marker(marker: bytes) -> dict[str, object]:
    try:
        value = json.loads(marker)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CurationCliError("Report reservation marker is malformed") from error
    fields = {
        "format",
        "operation",
        "pid",
        "token",
        "state",
        "report",
        "report_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise CurationCliError("Report reservation marker has unexpected fields")
    if value["format"] != REPORT_RESERVATION_FORMAT:
        raise CurationCliError("Report reservation marker format is unsupported")
    if not isinstance(value["operation"], str) or not value["operation"]:
        raise CurationCliError("Report reservation operation is invalid")
    if (
        not isinstance(value["pid"], int)
        or isinstance(value["pid"], bool)
        or value["pid"] <= 0
    ):
        raise CurationCliError("Report reservation pid is invalid")
    if not isinstance(value["token"], str) or not value["token"]:
        raise CurationCliError("Report reservation token is invalid")
    if value["state"] == "reserved":
        if value["report"] is not None or value["report_sha256"] is not None:
            raise CurationCliError("Unprepared report reservation contains report data")
    elif value["state"] == "prepared":
        if not isinstance(value["report"], dict):
            raise CurationCliError("Prepared report reservation has no report mapping")
        payload = _canonical_json_bytes(value["report"])
        if value["report_sha256"] != hashlib.sha256(payload).hexdigest():
            raise CurationCliError("Prepared report reservation digest is invalid")
    else:
        raise CurationCliError("Report reservation state is unsupported")
    return value


def _reservation_temp_path(path: Path, token: str) -> Path:
    return path.with_name(f".{path.name}.{token}.reservation.tmp")


def _report_temp_path(path: Path, token: str) -> Path:
    return path.with_name(f".{path.name}.{token}.tmp")


def _report_temp_siblings(path: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {
                *path.parent.glob(f".{path.name}.*.reservation.tmp"),
                *path.parent.glob(f".{path.name}.*.tmp"),
            }
        )
    )


def _write_fsynced_temp(path: Path, payload: bytes, *, read_write: bool) -> int:
    flags = (os.O_RDWR if read_write else os.O_WRONLY) | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return descriptor


def _raise_existing_report(path: Path) -> None:
    try:
        marker = _parse_reservation_marker(path.read_bytes())
    except (OSError, CurationCliError):
        raise CurationCliError(f"Append-only report path already exists: {path}")
    raise CurationCliError(
        "Report path is already reserved; run recover-report-reservation with "
        f"token {marker['token']}: {path}"
    )


def _reserve_report(path: Path, *, operation: str) -> ReportReservation:
    """Claim a report name before any canonical state can be mutated."""

    _prepare_atomic_output(path)
    token = secrets.token_hex(16)
    marker = _reservation_marker(
        operation=operation,
        token=token,
        state="reserved",
        report=None,
    )
    temporary = _reservation_temp_path(path, token)
    if path.exists():
        _raise_existing_report(path)
    orphaned = _report_temp_siblings(path)
    if orphaned:
        raise CurationCliError(
            "Unpublished report-reservation files require "
            "recover-report-reservation: " + ", ".join(item.name for item in orphaned)
        )
    descriptor = _write_fsynced_temp(temporary, marker, read_write=True)
    try:
        os.link(temporary, path)
    except FileExistsError as error:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        try:
            _raise_existing_report(path)
        except CurationCliError as existing_error:
            raise existing_error from error
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink()
    return ReportReservation(
        path=path,
        descriptor=descriptor,
        marker=marker,
        operation=operation,
        token=token,
    )


def _require_report_reservation(reservation: ReportReservation) -> None:
    if not reservation.active:
        raise CurationCliError("Report reservation is no longer active")
    try:
        path_status = os.stat(reservation.path, follow_symlinks=False)
        descriptor_status = os.fstat(reservation.descriptor)
        os.lseek(reservation.descriptor, 0, os.SEEK_SET)
        observed = os.read(reservation.descriptor, len(reservation.marker) + 1)
    except OSError as error:
        raise CurationCliError(
            f"Report reservation is no longer readable: {reservation.path}"
        ) from error
    if (
        path_status.st_dev != descriptor_status.st_dev
        or path_status.st_ino != descriptor_status.st_ino
        or observed != reservation.marker
        or descriptor_status.st_size != len(reservation.marker)
    ):
        raise CurationCliError(
            f"Report reservation changed during the transaction: {reservation.path}"
        )


def _discard_report_reservation(reservation: ReportReservation) -> None:
    if not reservation.active:
        return
    try:
        _require_report_reservation(reservation)
        reservation.path.unlink()
    finally:
        os.close(reservation.descriptor)
        reservation.active = False


def _preserve_report_reservation(reservation: ReportReservation) -> None:
    if not reservation.active:
        return
    try:
        _require_report_reservation(reservation)
    finally:
        os.close(reservation.descriptor)
        reservation.active = False


def _replace_reservation_marker(
    reservation: ReportReservation,
    marker: bytes,
) -> None:
    temporary = _reservation_temp_path(reservation.path, reservation.token)
    descriptor = _write_fsynced_temp(temporary, marker, read_write=False)
    os.close(descriptor)
    try:
        _require_report_reservation(reservation)
        os.replace(temporary, reservation.path)
        os.close(reservation.descriptor)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        reservation.descriptor = os.open(reservation.path, flags)
        reservation.marker = marker
        _require_report_reservation(reservation)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_report_reservation(
    reservation: ReportReservation,
    report: Mapping[str, object],
) -> None:
    marker = _reservation_marker(
        operation=reservation.operation,
        token=reservation.token,
        state="prepared",
        report=report,
    )
    _replace_reservation_marker(reservation, marker)


def _report_payload(report: Mapping[str, object]) -> bytes:
    try:
        json.dumps(
            report,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        rendered = yaml.safe_dump(
            dict(report),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
            width=1000,
        )
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise CurationCliError(
            f"Reconciliation report is not deterministic: {error}"
        ) from error
    return rendered.encode("utf-8")


def _commit_report(
    reservation: ReportReservation,
    report: Mapping[str, object],
) -> None:
    payload = _report_payload(report)
    prepared = _parse_reservation_marker(reservation.marker)
    if prepared["state"] != "prepared" or prepared["report"] != dict(report):
        raise CurationCliError(
            "Prepared report reservation does not match the final report"
        )
    temporary = _report_temp_path(reservation.path, reservation.token)
    descriptor = _write_fsynced_temp(temporary, payload, read_write=False)
    os.close(descriptor)
    try:
        _require_report_reservation(reservation)
        # The destination is the still-open inode created by _reserve_report,
        # never a pre-existing report.  Replacing that private reservation makes
        # the complete report visible in one filesystem operation.
        os.replace(temporary, reservation.path)
        reservation.active = False
        os.close(reservation.descriptor)
        if _read_authority(reservation.path, label="report") != payload:
            raise CurationCliError(
                f"Atomic report installation could not be verified: {reservation.path}"
            )
    finally:
        temporary.unlink(missing_ok=True)


def locked_staged_validator(root: Path, core: ModuleType) -> StageValidator:
    """Build the mandatory locked-0.1.12 structural and semantic validator."""

    root = _repository_root(root)
    try:
        from orinoco_lite.config import load_workspace, load_workspace_lock
        from orinoco_lite.errors import OrinocoError
        from orinoco_lite.projection import validate_semantics
        from orinoco_lite.runtime import verify_runtime_directory
    except ModuleNotFoundError as error:
        raise CurationCliError(
            "The locked Orinoco Lite engine is unavailable; run from the root "
            "consumer environment"
        ) from error

    try:
        workspace = load_workspace(root)
        lock = load_workspace_lock(workspace)
        installed = importlib.metadata.version("orinoco-lite")
    except Exception as error:
        raise CurationCliError(
            f"Cannot load the locked consumer environment: {error}"
        ) from error
    if lock.engine_version != EXACT_ENGINE_VERSION or installed != EXACT_ENGINE_VERSION:
        raise CurationCliError(
            "Reconciliation requires locked and installed orinoco-lite 0.1.12"
        )
    if lock.runtime.version != EXACT_ENGINE_VERSION:
        raise CurationCliError("Reconciliation requires locked runtime 0.1.12")

    candidates: list[Path] = []
    configured = os.environ.get("ORINOCO_RUNTIME_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.append(
        root / ".orinoco/runtime" / f"{lock.runtime.version}-{lock.runtime.sha256[:12]}"
    )
    runtime = None
    failures: list[str] = []
    for candidate in candidates:
        if not candidate.is_dir():
            failures.append(f"missing {candidate}")
            continue
        try:
            runtime = verify_runtime_directory(
                candidate,
                expected_release=lock.runtime.version,
                expected_manifest_sha256=lock.runtime.manifest_sha256,
            ).root
            break
        except OrinocoError as error:
            failures.append(f"invalid {candidate}: {error}")
    if runtime is None:
        detail = "; ".join(failures)
        raise CurationCliError(
            "The exact locked runtime is unavailable; run `pixi run verify-runtime` "
            f"before reconciliation ({detail})"
        )

    def validate(staged_records: Path) -> None:
        staged_records = staged_records.resolve()
        try:
            relative = staged_records.relative_to(root).as_posix()
        except ValueError as error:
            raise CurationCliError(
                "Staged metadata root is outside the consumer repository"
            ) from error
        core.MetadataIndex.from_directory(
            staged_records,
            require_unique_pids=True,
        )
        staged_workspace = replace(
            workspace,
            paths={**workspace.paths, "records": relative},
        )
        validate_semantics(staged_workspace, runtime)

    return validate


def reconcile(
    root: Path,
    *,
    adapter: str,
    inventory_path: str | Path,
    decisions_path: str | Path,
    report_path: str | Path,
    validate_staged: StageValidator,
    core: ModuleType | None = None,
) -> dict[str, object]:
    """Reconcile one explicit reviewed pair and atomically record its report."""

    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    inventory_file = _scoped_yaml_path(
        root,
        adapter_id,
        inventory_path,
        area="transactions",
        label="inventory path",
        must_exist=True,
    )
    decision_file = _scoped_yaml_path(
        root,
        adapter_id,
        decisions_path,
        area="policy",
        label="decisions path",
        must_exist=True,
    )
    report_file = _scoped_yaml_path(
        root,
        adapter_id,
        report_path,
        area="transactions",
        label="report path",
        must_exist=False,
    )
    if report_file == inventory_file:
        raise CurationCliError("Report path cannot overwrite the reviewed inventory")
    if not callable(validate_staged):
        raise CurationCliError("Reconciliation requires a staged-tree validator")

    inventory_bytes = _read_authority(inventory_file, label="inventory")
    decision_bytes = _read_authority(decision_file, label="decisions")
    core = core or _load_core(root)
    records = _records_root(
        root,
        core=core,
        adapter_id=adapter_id,
    )
    inventory = core.load_inventory(inventory_file)
    if inventory.adapter_id != adapter_id:
        raise CurationCliError("Reviewed inventory belongs to a different adapter")
    decisions = core.load_decisions(decision_file)
    reservation = _reserve_report(report_file, operation="reconcile")

    planned: dict[str, object] | None = None

    def before_commit(planned_report: Mapping[str, object]) -> None:
        nonlocal planned
        _require_unchanged(inventory_file, inventory_bytes, label="inventory")
        _require_unchanged(decision_file, decision_bytes, label="decisions")
        _require_report_reservation(reservation)
        planned = dict(planned_report)
        _prepare_report_reservation(reservation, planned)

    def after_commit(planned_report: Mapping[str, object]) -> None:
        final_report = dict(planned_report)
        if planned != final_report:
            raise CurationCliError(
                "Core reconciliation report changed across the commit boundary"
            )
        _require_unchanged(inventory_file, inventory_bytes, label="inventory")
        _require_unchanged(decision_file, decision_bytes, label="decisions")
        _commit_report(reservation, final_report)

    try:
        report = core.reconcile_inventory(
            inventory,
            decisions,
            records,
            validate_staged=validate_staged,
            before_commit=before_commit,
            after_commit=after_commit,
        )
        if planned != report or reservation.active:
            raise CurationCliError(
                "Core reconciliation did not finalize its prepared report"
            )
    except Exception as error:
        if not reservation.active:
            raise
        marker_state = _parse_reservation_marker(reservation.marker)["state"]
        if marker_state == "prepared":
            _preserve_report_reservation(reservation)
            raise CurationCliError(
                "A prepared reconciliation report could not be finalized; run "
                "recover-report-reservation to resolve it against canonical metadata"
            ) from error
        _discard_report_reservation(reservation)
        raise
    return report


def recover(
    root: Path,
    *,
    adapter: str,
    report_path: str | Path,
    core: ModuleType | None = None,
) -> dict[str, object]:
    """Explicitly recover exactly one interrupted canonical-tree transaction."""

    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    report_file = _scoped_yaml_path(
        root,
        adapter_id,
        report_path,
        area="transactions",
        label="report path",
        must_exist=False,
    )
    records = _records_root(root, must_exist=False)
    core = core or _load_core(root)
    reservation = _reserve_report(report_file, operation="recover-interrupted")
    planned: dict[str, object] | None = None

    def report_with_adapter(value: Mapping[str, object]) -> dict[str, object]:
        return {**dict(value), "adapter_id": adapter_id}

    def before_commit(planned_report: Mapping[str, object]) -> None:
        nonlocal planned
        _require_report_reservation(reservation)
        planned = report_with_adapter(planned_report)
        _prepare_report_reservation(reservation, planned)

    def after_commit(planned_report: Mapping[str, object]) -> None:
        final_report = report_with_adapter(planned_report)
        if planned != final_report:
            raise CurationCliError(
                "Core interrupted-recovery report changed across the commit boundary"
            )
        _commit_report(reservation, final_report)

    try:
        recovered = core.recover_interrupted(
            records,
            before_commit=before_commit,
            after_commit=after_commit,
        )
        report = report_with_adapter(recovered)
        if planned != report or reservation.active:
            raise CurationCliError(
                "Core interrupted recovery did not finalize its prepared report"
            )
    except Exception as error:
        if not reservation.active:
            raise
        marker_state = _parse_reservation_marker(reservation.marker)["state"]
        if marker_state == "prepared":
            _preserve_report_reservation(reservation)
            raise CurationCliError(
                "A prepared interrupted-recovery report could not be finalized; "
                "run recover-report-reservation"
            ) from error
        _discard_report_reservation(reservation)
        raise
    return report


def recover_lock(
    root: Path,
    *,
    adapter: str,
    report_path: str | Path,
    core: ModuleType | None = None,
) -> dict[str, object]:
    """Explicitly remove one core-verified stale reconciliation lock."""

    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    report_file = _scoped_yaml_path(
        root,
        adapter_id,
        report_path,
        area="transactions",
        label="report path",
        must_exist=False,
    )
    records = _records_root(root, must_exist=False)
    core = core or _load_core(root)
    reservation = _reserve_report(report_file, operation="recover-lock")
    planned: dict[str, object] | None = None

    def report_with_adapter(value: Mapping[str, object]) -> dict[str, object]:
        return {**dict(value), "adapter_id": adapter_id}

    def before_commit(planned_report: Mapping[str, object]) -> None:
        nonlocal planned
        _require_report_reservation(reservation)
        planned = report_with_adapter(planned_report)
        _prepare_report_reservation(reservation, planned)

    def after_commit(planned_report: Mapping[str, object]) -> None:
        final_report = report_with_adapter(planned_report)
        if planned != final_report:
            raise CurationCliError(
                "Core stale-lock report changed across the commit boundary"
            )
        _commit_report(reservation, final_report)

    try:
        recovered = core.recover_stale_lock(
            records,
            before_commit=before_commit,
            after_commit=after_commit,
        )
        report = report_with_adapter(recovered)
        if planned != report or reservation.active:
            raise CurationCliError(
                "Core stale-lock recovery did not finalize its prepared report"
            )
    except Exception as error:
        if not reservation.active:
            raise
        marker_state = _parse_reservation_marker(reservation.marker)["state"]
        if marker_state == "prepared":
            _preserve_report_reservation(reservation)
            raise CurationCliError(
                "A prepared stale-lock recovery report could not be finalized; "
                "run recover-report-reservation"
            ) from error
        _discard_report_reservation(reservation)
        raise
    return report


def _open_report_reservation(path: Path) -> tuple[ReportReservation, dict[str, object]]:
    marker = _read_authority(path, label="report reservation")
    value = _parse_reservation_marker(marker)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    reservation = ReportReservation(
        path=path,
        descriptor=descriptor,
        marker=marker,
        operation=str(value["operation"]),
        token=str(value["token"]),
    )
    try:
        _require_report_reservation(reservation)
    except Exception:
        os.close(descriptor)
        reservation.active = False
        raise
    return reservation, value


def _require_reservation_owner_inactive(pid: int) -> None:
    if pid == os.getpid():
        return
    if os.name != "posix":
        raise CurationCliError(
            "Report reservation owner checks require POSIX or the originating process"
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise CurationCliError(
            f"Report reservation owner pid {pid} cannot be verified"
        ) from error
    raise CurationCliError(f"Report reservation owner pid {pid} is still running")


def _require_no_interrupted_transaction(core: ModuleType, records: Path) -> None:
    artifacts = _interrupted_transaction_artifacts(core, records)
    if artifacts:
        raise CurationCliError(
            "Recover the interrupted metadata transaction before the report "
            "reservation: " + ", ".join(sorted(path.name for path in artifacts))
        )


def _interrupted_transaction_artifacts(
    core: ModuleType,
    records: Path,
) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in records.parent.glob(f"{core.STAGE_PREFIX}*")
            if path.exists() or path.is_symlink()
        )
    )


def _validate_reservation_token(token: str) -> None:
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise CurationCliError(
            "Report reservation token must be 32 lower-case hex characters"
        )


def _reap_bound_report_temps(path: Path, token: str) -> None:
    reservation_temp = _reservation_temp_path(path, token)
    report_temp = _report_temp_path(path, token)
    expected = {reservation_temp, report_temp}
    unexpected = set(_report_temp_siblings(path)) - expected
    if unexpected:
        raise CurationCliError(
            "Ambiguous report-reservation temporary files: "
            + ", ".join(sorted(item.name for item in unexpected))
        )
    for temporary in expected:
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
            raise CurationCliError(
                f"Report-reservation temporary path is unsafe: {temporary}"
            )
        temporary.unlink(missing_ok=True)


def recover_report_reservation(
    root: Path,
    *,
    adapter: str,
    report_path: str | Path,
    token: str,
    core: ModuleType | None = None,
) -> dict[str, object]:
    """Finalize or discard one verified, inactive report reservation."""

    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    _validate_reservation_token(token)
    report_file = _scoped_yaml_path(
        root,
        adapter_id,
        report_path,
        area="transactions",
        label="report reservation path",
        must_exist=False,
    )
    records = _records_root(root, must_exist=False)
    core = core or _load_core(root)
    if not report_file.exists():
        reservation_temp = _reservation_temp_path(report_file, token)
        report_temp = _report_temp_path(report_file, token)
        siblings = set(_report_temp_siblings(report_file))
        if report_temp in siblings:
            raise CurationCliError(
                "An unpublished final-report temporary file requires manual review"
            )
        if siblings != {reservation_temp}:
            raise CurationCliError(
                "No unique unpublished report reservation matches that path and token"
            )
        if reservation_temp.is_symlink() or not reservation_temp.is_file():
            raise CurationCliError(
                f"Unpublished report reservation is unsafe: {reservation_temp}"
            )
        temporary_marker = _read_authority(
            reservation_temp,
            label="unpublished report reservation",
        )
        temporary_value = _parse_reservation_marker(temporary_marker)
        if temporary_value["token"] != token or temporary_value["state"] != "reserved":
            raise CurationCliError(
                "Unpublished report reservation does not match a safe initial claim"
            )
        _require_reservation_owner_inactive(int(temporary_value["pid"]))
        with core.canonical_transaction_guard(records):
            if report_file.exists():
                raise CurationCliError(
                    "Report reservation appeared while recovery acquired its lock"
                )
            if (
                set(_report_temp_siblings(report_file)) != {reservation_temp}
                or _read_authority(
                    reservation_temp,
                    label="unpublished report reservation",
                )
                != temporary_marker
            ):
                raise CurationCliError(
                    "Unpublished report reservation changed before recovery"
                )
            _reap_bound_report_temps(report_file, token)
        return {
            "recovery": "discarded-unpublished-report-reservation",
            "adapter_id": adapter_id,
            "report": report_file.name,
        }

    reservation, marker = _open_report_reservation(report_file)
    try:
        if token != marker["token"]:
            raise CurationCliError("Report reservation token does not match")
        _require_reservation_owner_inactive(int(marker["pid"]))
        with core.canonical_transaction_guard(records):
            _require_report_reservation(reservation)
            marker = _parse_reservation_marker(reservation.marker)
            if token != marker["token"]:
                raise CurationCliError("Report reservation changed before recovery")
            operation = str(marker["operation"])
            state = str(marker["state"])
            if state == "reserved":
                # The mutation callback cannot return until the prepared marker
                # replaces this reserved marker. Any bound sibling is therefore
                # unpublished scratch, not evidence of a committed mutation.
                _reap_bound_report_temps(report_file, token)
                _discard_report_reservation(reservation)
                return {
                    "recovery": "discarded-unprepared-report-reservation",
                    "adapter_id": adapter_id,
                    "report": report_file.name,
                }

            report = marker["report"]
            assert isinstance(report, dict)
            if report.get("adapter_id") != adapter_id:
                raise CurationCliError(
                    "Prepared report reservation belongs to a different adapter"
                )
            if operation == "reconcile":
                _require_no_interrupted_transaction(core, records)
                before_digest = report.get("before_digest")
                after_digest = report.get("after_digest")
                current_digest = core.metadata_tree_digest(records)
                if current_digest == after_digest:
                    _reap_bound_report_temps(report_file, token)
                    _commit_report(reservation, report)
                    action = "finalized-committed-reconciliation-report"
                elif current_digest == before_digest:
                    _reap_bound_report_temps(report_file, token)
                    _discard_report_reservation(reservation)
                    action = "discarded-aborted-reconciliation-report"
                else:
                    raise CurationCliError(
                        "Canonical metadata matches neither prepared report digest"
                    )
            elif operation == "recover-interrupted":
                artifacts_before = report.get("artifacts_before")
                if not isinstance(artifacts_before, list) or not all(
                    isinstance(item, str) and item for item in artifacts_before
                ):
                    raise CurationCliError(
                        "Prepared recovery report has no exact pre-mutation artifacts"
                    )
                current_digest = core.metadata_tree_digest(records)
                current_artifacts = [
                    path.name
                    for path in _interrupted_transaction_artifacts(core, records)
                ]
                if (
                    current_digest == report.get("after_digest")
                    and not current_artifacts
                ):
                    _reap_bound_report_temps(report_file, token)
                    _commit_report(reservation, report)
                    action = "finalized-interrupted-recovery-report"
                elif (
                    current_digest == report.get("before_digest")
                    and current_artifacts == artifacts_before
                ):
                    _reap_bound_report_temps(report_file, token)
                    _discard_report_reservation(reservation)
                    action = "discarded-aborted-interrupted-recovery-report"
                else:
                    raise CurationCliError(
                        "Canonical metadata and recovery artifacts match neither "
                        "prepared recovery boundary"
                    )
            elif operation == "recover-lock":
                # Acquiring this guard proves the stale lock named by the report
                # was removed; the guard's own lock deliberately has the same path.
                _reap_bound_report_temps(report_file, token)
                _commit_report(reservation, report)
                action = "finalized-stale-lock-recovery-report"
            else:
                raise CurationCliError(
                    f"Unsupported report reservation operation: {operation}"
                )
            return {
                "recovery": action,
                "adapter_id": adapter_id,
                "report": report_file.name,
            }
    except Exception:
        _preserve_report_reservation(reservation)
        raise


def render_decision(
    core: ModuleType,
    candidate,
    *,
    supersedes_decision_id: str | None,
    disposition: str,
    reviewer: str,
    decided_on: date,
    rationale: str,
    evidence: Sequence[str],
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Render one complete decision event with core-derived identities."""

    if type(decided_on) is not date:
        raise CurationCliError("decision event requires an explicit ISO decision date")
    details = dict(details or {})
    unexpected = sorted(set(details) - DECISION_DETAILS)
    if unexpected:
        raise CurationCliError(
            "Decision details contain unsupported fields: " + ", ".join(unexpected)
        )
    claim_revision_id = core.claim_revision_identity(
        candidate.candidate_id,
        candidate.material_fingerprint,
        candidate.relevant_policy_fingerprint,
    )
    event: dict[str, object] = {
        "claim_revision_id": claim_revision_id,
        "supersedes_decision_id": supersedes_decision_id,
        "disposition": disposition,
        "reviewer": reviewer,
        "decided_on": decided_on.isoformat(),
        "rationale": rationale,
        "evidence": list(evidence),
        **details,
    }
    decision_id = core.decision_identity(**event)
    return {
        "decision_id": decision_id,
        **event,
        "candidate_id": candidate.candidate_id,
        "adapter_id": candidate.adapter_id,
        "source_namespace": candidate.source_namespace,
        "source_record_id": candidate.source_record_id,
        "claim_kind": candidate.claim_kind,
        "material_fingerprint": candidate.material_fingerprint,
        "relevant_policy_fingerprint": candidate.relevant_policy_fingerprint,
    }


def render_inventory_decision(
    root: Path,
    *,
    adapter: str,
    inventory_path: str | Path,
    candidate_id: str,
    supersedes_decision_id: str | None,
    disposition: str,
    reviewer: str,
    decided_on: date,
    rationale: str,
    evidence: Sequence[str],
    details: Mapping[str, object] | None = None,
    core: ModuleType | None = None,
) -> dict[str, object]:
    root = _repository_root(root)
    adapter_id = _adapter(adapter)
    inventory_file = _scoped_yaml_path(
        root,
        adapter_id,
        inventory_path,
        area="transactions",
        label="inventory path",
        must_exist=True,
    )
    core = core or _load_core(root)
    inventory = core.load_inventory(inventory_file)
    if inventory.adapter_id != adapter_id:
        raise CurationCliError("Reviewed inventory belongs to a different adapter")
    candidates = {
        candidate.candidate_id: candidate for candidate in inventory.candidates
    }
    try:
        candidate = candidates[candidate_id]
    except KeyError as error:
        raise CurationCliError(
            f"Candidate is not present in the reviewed inventory: {candidate_id}"
        ) from error
    return render_decision(
        core,
        candidate,
        supersedes_decision_id=supersedes_decision_id,
        disposition=disposition,
        reviewer=reviewer,
        decided_on=decided_on,
        rationale=rationale,
        evidence=evidence,
        details=details,
    )


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO calendar date") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected an ISO calendar date")
    return parsed


def _yaml_mapping(value: str) -> dict[str, object]:
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as error:
        raise argparse.ArgumentTypeError(f"expected a YAML mapping: {error}") from error
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected a YAML mapping")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    propose_parser = commands.add_parser("propose")
    propose_parser.add_argument("--root", type=Path, default=Path.cwd())
    propose_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    propose_parser.add_argument("--inventory", required=True)
    propose_parser.add_argument("--decisions")
    propose_parser.add_argument("--provider-output", required=True)
    propose_parser.add_argument("--as-of", type=_iso_date, required=True)
    propose_parser.add_argument(
        "--resolved-policy-question",
        action="append",
        default=[],
    )
    propose_parser.add_argument("--expected-library-version", type=int)
    propose_parser.add_argument("--source-path")
    propose_parser.add_argument("--expected-source-commit")
    propose_parser.add_argument("--source-run-id")

    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--root", type=Path, default=Path.cwd())
    reconcile_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    reconcile_parser.add_argument("--inventory", required=True)
    reconcile_parser.add_argument("--decisions", required=True)
    reconcile_parser.add_argument("--report", required=True)

    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--root", type=Path, default=Path.cwd())
    recover_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    recover_parser.add_argument("--report", required=True)

    recover_lock_parser = commands.add_parser("recover-lock")
    recover_lock_parser.add_argument("--root", type=Path, default=Path.cwd())
    recover_lock_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    recover_lock_parser.add_argument("--report", required=True)

    recover_report_parser = commands.add_parser("recover-report-reservation")
    recover_report_parser.add_argument("--root", type=Path, default=Path.cwd())
    recover_report_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    recover_report_parser.add_argument("--report", required=True)
    recover_report_parser.add_argument("--token", required=True)

    render_parser = commands.add_parser("render-decision")
    render_parser.add_argument("--root", type=Path, default=Path.cwd())
    render_parser.add_argument(
        "--adapter", choices=tuple(ADAPTER_ALIASES), required=True
    )
    render_parser.add_argument("--inventory", required=True)
    render_parser.add_argument("--candidate-id", required=True)
    render_parser.add_argument("--supersedes-decision-id")
    render_parser.add_argument(
        "--disposition",
        choices=DECISION_DISPOSITIONS,
        required=True,
    )
    render_parser.add_argument("--reviewer", required=True)
    render_parser.add_argument("--decided-on", type=_iso_date, required=True)
    render_parser.add_argument("--rationale", required=True)
    render_parser.add_argument("--evidence", action="append", required=True)
    render_parser.add_argument("--details-yaml", type=_yaml_mapping, default={})
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "propose":
            inventory = propose(
                arguments.root,
                adapter=arguments.adapter,
                inventory_path=arguments.inventory,
                decisions_path=arguments.decisions,
                provider_output=arguments.provider_output,
                as_of=arguments.as_of,
                resolved_policy_questions=arguments.resolved_policy_question,
                expected_library_version=arguments.expected_library_version,
                source_path=arguments.source_path,
                expected_source_commit=arguments.expected_source_commit,
                source_run_id=arguments.source_run_id,
            )
            print(inventory["inventory_id"])
        elif arguments.command == "reconcile":
            core = _load_core(_repository_root(arguments.root))
            validator = locked_staged_validator(arguments.root, core)
            report = reconcile(
                arguments.root,
                adapter=arguments.adapter,
                inventory_path=arguments.inventory,
                decisions_path=arguments.decisions,
                report_path=arguments.report,
                validate_staged=validator,
                core=core,
            )
            print(report["inventory_id"])
        elif arguments.command == "recover":
            report = recover(
                arguments.root,
                adapter=arguments.adapter,
                report_path=arguments.report,
            )
            print(report["recovery"])
        elif arguments.command == "recover-lock":
            report = recover_lock(
                arguments.root,
                adapter=arguments.adapter,
                report_path=arguments.report,
            )
            print(report["recovery"])
        elif arguments.command == "render-decision":
            decision = render_inventory_decision(
                arguments.root,
                adapter=arguments.adapter,
                inventory_path=arguments.inventory,
                candidate_id=arguments.candidate_id,
                supersedes_decision_id=arguments.supersedes_decision_id,
                disposition=arguments.disposition,
                reviewer=arguments.reviewer,
                decided_on=arguments.decided_on,
                rationale=arguments.rationale,
                evidence=arguments.evidence,
                details=arguments.details_yaml,
            )
            print(yaml.safe_dump(decision, sort_keys=False), end="")
        elif arguments.command == "recover-report-reservation":
            report = recover_report_reservation(
                arguments.root,
                adapter=arguments.adapter,
                report_path=arguments.report,
                token=arguments.token,
            )
            print(report["recovery"])
        else:
            raise AssertionError(f"Unhandled command: {arguments.command}")
    except Exception as error:
        print(f"curation transaction failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
