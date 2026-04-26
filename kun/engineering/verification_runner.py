"""任务成功验证 runner (BATCH4 C3 / T53).

TODO: orchestrator wire in M3.2 — 标记 done 前调用 VerificationRunner.verify().
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.orm import PendingActionRow
from kun.datamodel.verification_spec import VerificationResult, VerificationSpec

CommandRunner = Callable[[list[str], Path, float], subprocess.CompletedProcess[str]]
URLChecker = Callable[[str, float], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class HumanApprovalRecord:
    approval_id: str
    tenant_id: str
    task_id: str
    status: str
    approver: str | None = None
    approved_at: str | None = None
    approver_email: str | None = None


class HumanApprovalStore(Protocol):
    async def get(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        task_id: str | None = None,
    ) -> HumanApprovalRecord | None: ...


class PendingActionApprovalStore:
    """从 pending_actions 表读取人工审批状态."""

    async def get(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        task_id: str | None = None,
    ) -> HumanApprovalRecord | None:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = select(PendingActionRow).where(
                PendingActionRow.tenant_id == tenant_id,
                PendingActionRow.action_id == approval_id,
            )
            if task_id:
                stmt = stmt.where(PendingActionRow.task_ref == task_id)
            row = (await session.execute(stmt)).scalar_one_or_none()

        if row is None:
            return None
        payload = row.payload or {}
        return HumanApprovalRecord(
            approval_id=row.action_id,
            tenant_id=row.tenant_id,
            task_id=row.task_ref,
            status=row.status,
            approver=_optional_str(payload.get("approver") or payload.get("decision_by")),
            approved_at=row.decided_at.isoformat() if row.decided_at else None,
            approver_email=_optional_str(payload.get("approver_email")),
        )


class VerificationRunner:
    """按 VerificationSpec 跑确定性验证."""

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        command_runner: CommandRunner | None = None,
        url_checker: URLChecker | None = None,
        approval_store: HumanApprovalStore | None = None,
    ) -> None:
        self._cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._command_runner = command_runner or _default_command_runner
        self._url_checker = url_checker or _default_url_checker
        self._approval_store = approval_store or PendingActionApprovalStore()

    async def verify(self, spec: VerificationSpec, artifact_ref: str) -> VerificationResult:
        """跑验证. 失败时返回 passed=False, 不抛给调用方."""

        try:
            match spec.kind:
                case "exact_output":
                    return self._verify_exact_output(spec, artifact_ref)
                case "test_pass":
                    return await self._verify_command(
                        spec,
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            str(spec.spec.get("test_file_path") or artifact_ref),
                            "-q",
                        ],
                    )
                case "lint_pass":
                    return await self._verify_command(
                        spec,
                        [
                            sys.executable,
                            "-m",
                            "ruff",
                            "check",
                            str(spec.spec.get("target") or artifact_ref),
                        ],
                    )
                case "url_check":
                    return await self._verify_url(spec, artifact_ref)
                case "human_approval":
                    return await self._verify_human_approval(spec)
                case "hash_match":
                    return self._verify_hash_match(spec, artifact_ref)
                case "schema_validate":
                    return self._verify_schema_validate(spec, artifact_ref)
        except Exception as exc:
            return VerificationResult(kind=spec.kind, passed=False, error_msg=str(exc))

    def _verify_exact_output(self, spec: VerificationSpec, artifact_ref: str) -> VerificationResult:
        text = _read_text(self._resolve_path(artifact_ref))
        expected = spec.spec.get("expected")
        contains = spec.spec.get("contains")
        if isinstance(expected, str):
            passed = text == expected
            return VerificationResult(
                kind=spec.kind,
                passed=passed,
                error_msg=None if passed else "artifact text did not equal expected output",
            )
        if isinstance(contains, str):
            passed = contains in text
            return VerificationResult(
                kind=spec.kind,
                passed=passed,
                error_msg=None if passed else "artifact text did not contain expected fragment",
            )
        return VerificationResult(
            kind=spec.kind,
            passed=False,
            error_msg="exact_output requires spec.expected or spec.contains",
        )

    async def _verify_command(
        self,
        spec: VerificationSpec,
        command: list[str],
    ) -> VerificationResult:
        process = await asyncio.to_thread(
            self._command_runner,
            command,
            self._cwd,
            float(spec.timeout_sec),
        )
        output = f"{process.stdout}\n{process.stderr}".strip()
        passed = process.returncode == 0
        return VerificationResult(
            kind=spec.kind,
            passed=passed,
            evidence_url=f"local-command:{' '.join(command)}",
            error_msg=None if passed else output or f"command failed with {process.returncode}",
            details={"returncode": process.returncode, "output": output},
        )

    async def _verify_url(self, spec: VerificationSpec, artifact_ref: str) -> VerificationResult:
        url = str(spec.spec.get("url") or artifact_ref)
        allowed_statuses = _as_int_set(spec.spec.get("allowed_statuses"), default={200})
        status_code, body = await self._url_checker(url, float(spec.timeout_sec))
        passed = status_code in allowed_statuses
        return VerificationResult(
            kind=spec.kind,
            passed=passed,
            evidence_url=url,
            error_msg=None if passed else f"unexpected status code {status_code}",
            details={"status_code": status_code, "body_preview": body[:200]},
        )

    async def _verify_human_approval(self, spec: VerificationSpec) -> VerificationResult:
        tenant_id = _required_str(spec.spec.get("tenant_id"))
        approval_id = _required_str(spec.spec.get("approval_id") or spec.spec.get("action_id"))
        task_id = _optional_str(spec.spec.get("task_id"))
        if not tenant_id or not approval_id:
            return VerificationResult(
                kind=spec.kind,
                passed=False,
                error_msg="human_approval requires persisted tenant_id and approval_id/action_id",
            )

        record = await self._approval_store.get(
            tenant_id=tenant_id,
            approval_id=approval_id,
            task_id=task_id,
        )
        if record is None:
            return VerificationResult(
                kind=spec.kind,
                passed=False,
                error_msg="human approval record is missing",
                details={"tenant_id": tenant_id, "approval_id": approval_id, "task_id": task_id},
            )

        approved = record.status in {"approved", "executed"}
        return VerificationResult(
            kind=spec.kind,
            passed=approved,
            evidence_url=f"human-approval:{record.approval_id}",
            error_msg=None if approved else f"human approval status is {record.status}",
            details={
                "tenant_id": record.tenant_id,
                "task_id": record.task_id,
                "approval_id": record.approval_id,
                "status": record.status,
                "approver": record.approver,
                "approved_at": record.approved_at,
                "approver_email": record.approver_email,
            },
        )

    def _verify_hash_match(self, spec: VerificationSpec, artifact_ref: str) -> VerificationResult:
        expected = spec.spec.get("expected_sha256") or spec.spec.get("sha256")
        if not isinstance(expected, str):
            return VerificationResult(
                kind=spec.kind,
                passed=False,
                error_msg="hash_match requires spec.expected_sha256",
            )

        actual = _sha256_file(self._resolve_path(artifact_ref))
        normalized_expected = expected.removeprefix("sha256:")
        passed = actual == normalized_expected
        return VerificationResult(
            kind=spec.kind,
            passed=passed,
            error_msg=None if passed else "sha256 mismatch",
            details={"actual_sha256": actual, "expected_sha256": normalized_expected},
        )

    def _verify_schema_validate(
        self, spec: VerificationSpec, artifact_ref: str
    ) -> VerificationResult:
        payload = _read_json_payload(self._resolve_path(artifact_ref), artifact_ref)
        if not isinstance(payload, dict):
            return VerificationResult(
                kind=spec.kind,
                passed=False,
                error_msg="schema_validate artifact must be a JSON object",
            )

        required_keys = _as_str_list(spec.spec.get("required_keys"))
        missing = [key for key in required_keys if key not in payload]
        exact_values = spec.spec.get("exact_values")
        mismatched = _mismatched_values(
            payload, exact_values if isinstance(exact_values, dict) else {}
        )
        passed = not missing and not mismatched
        return VerificationResult(
            kind=spec.kind,
            passed=passed,
            error_msg=None if passed else "schema validation failed",
            details={"missing_keys": missing, "mismatched_values": mismatched},
        )

    def _resolve_path(self, artifact_ref: str) -> Path:
        path = Path(artifact_ref)
        return path if path.is_absolute() else self._cwd / path


def _default_command_runner(
    command: list[str],
    cwd: Path,
    timeout_sec: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


async def _default_url_checker(url: str, timeout_sec: float) -> tuple[int, str]:
    _assert_public_http_url(url)
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.get(url)
    return response.status_code, response.text


def _assert_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme is not allowed: {parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL host is required")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"SSRF blocked: {url}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError(f"SSRF blocked: {url}")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_payload(path: Path, artifact_ref: str) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(artifact_ref)


def _as_int_set(value: Any, *, default: set[int]) -> set[int]:
    if value is None:
        return default
    if isinstance(value, list):
        return {int(item) for item in value}
    return {int(value)}


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _mismatched_values(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    mismatched: dict[str, dict[str, Any]] = {}
    for key, expected_value in expected.items():
        if payload.get(key) != expected_value:
            mismatched[key] = {"expected": expected_value, "actual": payload.get(key)}
    return mismatched


def _required_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
