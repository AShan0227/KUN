"""VerificationSpec / VerificationRunner 测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from kun.datamodel.verification_spec import VerificationSpec
from kun.engineering.verification_runner import (
    HumanApprovalRecord,
    HumanApprovalStore,
    VerificationRunner,
    _assert_public_http_url,
)


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cmd"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class FakeApprovalStore(HumanApprovalStore):
    def __init__(self, records: dict[str, HumanApprovalRecord]) -> None:
        self.records = records
        self.seen: list[tuple[str, str, str | None]] = []

    async def get(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        task_id: str | None = None,
    ) -> HumanApprovalRecord | None:
        self.seen.append((tenant_id, approval_id, task_id))
        record = self.records.get(approval_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        if task_id and record.task_id != task_id:
            return None
        return record


@pytest.mark.asyncio
async def test_exact_output_passes_when_text_matches(tmp_path: Path) -> None:
    artifact = tmp_path / "answer.txt"
    artifact.write_text("done\n", encoding="utf-8")
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(kind="exact_output", spec={"expected": "done\n"}),
        "answer.txt",
    )

    assert result.passed is True


@pytest.mark.asyncio
async def test_exact_output_fails_when_text_differs(tmp_path: Path) -> None:
    artifact = tmp_path / "answer.txt"
    artifact.write_text("fake\n", encoding="utf-8")
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(kind="exact_output", spec={"expected": "done\n"}),
        "answer.txt",
    )

    assert result.passed is False
    assert "equal expected" in str(result.error_msg)


@pytest.mark.asyncio
async def test_test_pass_runs_pytest_command(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def command_runner(
        command: list[str],
        cwd: Path,
        timeout_sec: float,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        assert cwd == tmp_path
        assert timeout_sec == 30.0
        return _completed(stdout="1 passed")

    runner = VerificationRunner(cwd=tmp_path, command_runner=command_runner)

    result = await runner.verify(
        VerificationSpec(kind="test_pass", spec={}, timeout_sec=30),
        "tests/unit/test_x.py",
    )

    assert result.passed is True
    assert seen[0][-2:] == ["tests/unit/test_x.py", "-q"]


@pytest.mark.asyncio
async def test_test_pass_fails_on_nonzero_returncode(tmp_path: Path) -> None:
    runner = VerificationRunner(
        cwd=tmp_path,
        command_runner=lambda *_args: _completed(stdout="1 failed", returncode=1),
    )

    result = await runner.verify(VerificationSpec(kind="test_pass"), "tests/unit/test_x.py")

    assert result.passed is False
    assert "1 failed" in str(result.error_msg)


@pytest.mark.asyncio
async def test_lint_pass_uses_ruff_command(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def command_runner(
        command: list[str],
        _cwd: Path,
        _timeout_sec: float,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed()

    runner = VerificationRunner(cwd=tmp_path, command_runner=command_runner)

    result = await runner.verify(VerificationSpec(kind="lint_pass"), "kun")

    assert result.passed is True
    assert seen[0][-3:] == ["ruff", "check", "kun"]


@pytest.mark.asyncio
async def test_url_check_passes_allowed_status() -> None:
    async def url_checker(url: str, timeout_sec: float) -> tuple[int, str]:
        assert url == "https://example.test/health"
        assert timeout_sec == 5.0
        return 204, "ok"

    runner = VerificationRunner(url_checker=url_checker)

    result = await runner.verify(
        VerificationSpec(
            kind="url_check",
            spec={"url": "https://example.test/health", "allowed_statuses": [200, 204]},
            timeout_sec=5,
        ),
        "",
    )

    assert result.passed is True
    assert result.details["status_code"] == 204


@pytest.mark.asyncio
async def test_url_check_fails_unexpected_status() -> None:
    async def url_checker(_url: str, _timeout_sec: float) -> tuple[int, str]:
        return 500, "bad"

    runner = VerificationRunner(url_checker=url_checker)

    result = await runner.verify(VerificationSpec(kind="url_check"), "https://example.test")

    assert result.passed is False
    assert "unexpected status" in str(result.error_msg)


@pytest.mark.asyncio
async def test_url_check_blocks_localhost_ssrf() -> None:
    with pytest.raises(ValueError, match="SSRF blocked"):
        _assert_public_http_url("http://localhost:8000/health")


@pytest.mark.asyncio
async def test_url_check_blocks_private_ip_ssrf() -> None:
    with pytest.raises(ValueError, match="SSRF blocked"):
        _assert_public_http_url("http://169.254.169.254/latest/meta-data")


@pytest.mark.asyncio
async def test_human_approval_reads_persisted_approval_record() -> None:
    store = FakeApprovalStore(
        {
            "act-1": HumanApprovalRecord(
                approval_id="act-1",
                tenant_id="tenant-1",
                task_id="task-1",
                status="approved",
                approver="sylvan",
                approved_at="2026-04-26T10:00:00+00:00",
                approver_email="sylvan@example.com",
            )
        }
    )
    runner = VerificationRunner(approval_store=store)

    ok = await runner.verify(
        VerificationSpec(
            kind="human_approval",
            spec={"tenant_id": "tenant-1", "task_id": "task-1", "approval_id": "act-1"},
        ),
        "",
    )

    assert ok.passed is True
    assert ok.evidence_url == "human-approval:act-1"
    assert ok.details["approver"] == "sylvan"
    assert ok.details["approved_at"] == "2026-04-26T10:00:00+00:00"
    assert ok.details["approver_email"] == "sylvan@example.com"
    assert store.seen == [("tenant-1", "act-1", "task-1")]


@pytest.mark.asyncio
async def test_human_approval_rejects_missing_persisted_fields() -> None:
    runner = VerificationRunner(approval_store=FakeApprovalStore({}))

    result = await runner.verify(
        VerificationSpec(kind="human_approval", spec={"approved": True}), ""
    )

    assert result.passed is False
    assert "persisted tenant_id" in str(result.error_msg)


@pytest.mark.asyncio
async def test_human_approval_fails_when_persisted_status_not_approved() -> None:
    store = FakeApprovalStore(
        {
            "act-2": HumanApprovalRecord(
                approval_id="act-2",
                tenant_id="tenant-1",
                task_id="task-1",
                status="rejected",
            )
        }
    )
    runner = VerificationRunner(approval_store=store)

    result = await runner.verify(
        VerificationSpec(
            kind="human_approval",
            spec={"tenant_id": "tenant-1", "task_id": "task-1", "approval_id": "act-2"},
        ),
        "",
    )

    assert result.passed is False
    assert "rejected" in str(result.error_msg)


@pytest.mark.asyncio
async def test_hash_match_passes_for_expected_sha256(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable", encoding="utf-8")
    digest = hashlib.sha256(b"stable").hexdigest()
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(kind="hash_match", spec={"expected_sha256": f"sha256:{digest}"}),
        "artifact.txt",
    )

    assert result.passed is True


@pytest.mark.asyncio
async def test_hash_match_fails_on_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("changed", encoding="utf-8")
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(kind="hash_match", spec={"expected_sha256": "0" * 64}),
        "artifact.txt",
    )

    assert result.passed is False
    assert "mismatch" in str(result.error_msg)


@pytest.mark.asyncio
async def test_schema_validate_passes_required_keys_and_values(tmp_path: Path) -> None:
    artifact = tmp_path / "payload.json"
    artifact.write_text(json.dumps({"status": "done", "count": 3}), encoding="utf-8")
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(
            kind="schema_validate",
            spec={"required_keys": ["status", "count"], "exact_values": {"status": "done"}},
        ),
        "payload.json",
    )

    assert result.passed is True


@pytest.mark.asyncio
async def test_schema_validate_fails_missing_key(tmp_path: Path) -> None:
    artifact = tmp_path / "payload.json"
    artifact.write_text(json.dumps({"status": "done"}), encoding="utf-8")
    runner = VerificationRunner(cwd=tmp_path)

    result = await runner.verify(
        VerificationSpec(kind="schema_validate", spec={"required_keys": ["status", "count"]}),
        "payload.json",
    )

    assert result.passed is False
    assert result.details["missing_keys"] == ["count"]
