"""LT.C — TaskCheckpointService 单测 (持久化 + resume)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.executor.checkpoint import (
    CheckpointStatus,
    TaskCheckpoint,
    TaskCheckpointService,
)


class _FakeStore:
    """In-memory writer + reader + status_marker, simulating DB."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.status_marks: list[tuple[str, str, CheckpointStatus]] = []
        self.write_fail = False

    async def writer(self, row: dict[str, Any]) -> None:
        if self.write_fail:
            raise RuntimeError("simulated DB write failure")
        self.rows.append(dict(row))

    async def reader(self, tenant_id: str, task_id: str) -> TaskCheckpoint | None:
        """Return latest active row for (tenant, task), or None."""
        candidates = [
            r
            for r in self.rows
            if r["tenant_id"] == tenant_id
            and r["task_id"] == task_id
            and r["status"] == "active"
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r["sequence"])
        # rebuild TaskCheckpoint from row
        return TaskCheckpoint(
            checkpoint_id=latest["checkpoint_id"],
            task_id=latest["task_id"],
            step_idx=latest["step_idx"],
            sequence=latest["sequence"],
            conversation_snapshot=list(latest["conversation_snapshot"]),
            working_state=dict(latest["working_state"]),
            artifact_refs=list(latest["artifact_refs"]),
            goal_anchor_id=latest["goal_anchor_id"],
            last_self_report=latest["last_self_report"],
            cost_usd_so_far=latest["cost_usd_so_far"],
            tokens_used_so_far=latest["tokens_used_so_far"],
            status=latest["status"],
            rationale=latest["rationale"],
            created_at=latest["created_at"],
        )

    async def status_marker(
        self, tenant_id: str, checkpoint_id: str, new_status: CheckpointStatus
    ) -> None:
        self.status_marks.append((tenant_id, checkpoint_id, new_status))
        # also update in-memory row so reader reflects it
        for r in self.rows:
            if r["tenant_id"] == tenant_id and r["checkpoint_id"] == checkpoint_id:
                r["status"] = new_status


def _make_service(store: _FakeStore | None = None) -> tuple[TaskCheckpointService, _FakeStore]:
    s = store or _FakeStore()
    return (
        TaskCheckpointService(
            writer=s.writer, reader=s.reader, status_marker=s.status_marker
        ),
        s,
    )


# ---- TaskCheckpoint dataclass ----


def test_to_row_payload_contains_required_fields() -> None:
    cp = TaskCheckpoint(
        checkpoint_id="tcp-1",
        task_id="tk-1",
        step_idx=0,
        sequence=0,
        conversation_snapshot=[{"role": "user", "content": "hi"}],
        working_state={"step": "started"},
        artifact_refs=["s3://b/k1"],
    )
    row = cp.to_row_payload(tenant_id="t-acme")
    assert row["tenant_id"] == "t-acme"
    assert row["checkpoint_id"] == "tcp-1"
    assert row["task_id"] == "tk-1"
    assert row["step_idx"] == 0
    assert row["sequence"] == 0
    assert row["conversation_snapshot"] == [{"role": "user", "content": "hi"}]
    assert row["working_state"] == {"step": "started"}
    assert row["artifact_refs"] == ["s3://b/k1"]
    assert row["status"] == "active"
    assert row["goal_anchor_id"] is None
    assert row["last_self_report"] is None
    assert row["cost_usd_so_far"] == 0.0
    assert row["tokens_used_so_far"] == 0


def test_to_row_payload_copies_mutable_fields() -> None:
    """payload 不应 alias frozen dataclass 内部 list/dict."""
    convo = [{"role": "user", "content": "x"}]
    state = {"counter": 0}
    refs = ["a"]
    cp = TaskCheckpoint(
        checkpoint_id="tcp-1",
        task_id="tk-1",
        step_idx=0,
        sequence=0,
        conversation_snapshot=convo,
        working_state=state,
        artifact_refs=refs,
    )
    row = cp.to_row_payload(tenant_id="t-1")
    # 修改 row 的内容不应影响原 conversation_snapshot
    row["conversation_snapshot"].append({"x": "y"})
    row["working_state"]["new"] = "v"
    row["artifact_refs"].append("z")
    assert len(cp.conversation_snapshot) == 1
    assert "new" not in cp.working_state
    assert "z" not in cp.artifact_refs


# ---- save ----


@pytest.mark.asyncio
async def test_save_assigns_sequence_and_id() -> None:
    service, store = _make_service()
    cp = await service.save(
        tenant_id="t-1",
        task_id="tk-1",
        step_idx=0,
        conversation_snapshot=[],
    )
    assert cp.sequence == 0
    assert cp.checkpoint_id.startswith("tcp-")
    assert len(store.rows) == 1
    assert store.rows[0]["sequence"] == 0


@pytest.mark.asyncio
async def test_save_sequence_monotonic_per_task() -> None:
    service, _ = _make_service()
    cp0 = await service.save(tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[])
    cp1 = await service.save(tenant_id="t-1", task_id="tk-1", step_idx=1, conversation_snapshot=[])
    cp2 = await service.save(tenant_id="t-1", task_id="tk-1", step_idx=2, conversation_snapshot=[])
    assert (cp0.sequence, cp1.sequence, cp2.sequence) == (0, 1, 2)


@pytest.mark.asyncio
async def test_save_sequence_independent_per_task() -> None:
    service, _ = _make_service()
    a = await service.save(tenant_id="t-1", task_id="tk-A", step_idx=0, conversation_snapshot=[])
    b = await service.save(tenant_id="t-1", task_id="tk-B", step_idx=0, conversation_snapshot=[])
    a2 = await service.save(tenant_id="t-1", task_id="tk-A", step_idx=1, conversation_snapshot=[])
    assert a.sequence == 0
    assert b.sequence == 0  # 不同 task, 独立 counter
    assert a2.sequence == 1


@pytest.mark.asyncio
async def test_save_writer_raise_propagates() -> None:
    """ADR-024: 状态机推进失败必须 raise."""
    service, store = _make_service()
    store.write_fail = True
    with pytest.raises(RuntimeError, match="DB write"):
        await service.save(
            tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[]
        )


@pytest.mark.asyncio
async def test_save_carries_anchor_and_self_report() -> None:
    service, store = _make_service()
    cp = await service.save(
        tenant_id="t-1",
        task_id="tk-1",
        step_idx=0,
        conversation_snapshot=[{"role": "user", "content": "hi"}],
        goal_anchor_id="ga-x",
        last_self_report={"on_anchor": True, "drift_risk": "low"},
        cost_usd_so_far=0.12,
        tokens_used_so_far=1500,
    )
    assert cp.goal_anchor_id == "ga-x"
    assert cp.last_self_report == {"on_anchor": True, "drift_risk": "low"}
    assert cp.cost_usd_so_far == 0.12
    assert cp.tokens_used_so_far == 1500
    assert store.rows[0]["goal_anchor_id"] == "ga-x"


# ---- resume ----


@pytest.mark.asyncio
async def test_resume_returns_none_when_no_checkpoint() -> None:
    service, _ = _make_service()
    cp = await service.resume(tenant_id="t-1", task_id="tk-1")
    assert cp is None


@pytest.mark.asyncio
async def test_resume_returns_latest_active() -> None:
    service, _ = _make_service()
    await service.save(tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[{"r": 0}])
    await service.save(tenant_id="t-1", task_id="tk-1", step_idx=1, conversation_snapshot=[{"r": 1}])
    cp = await service.resume(tenant_id="t-1", task_id="tk-1")
    assert cp is not None
    assert cp.sequence == 1
    assert cp.step_idx == 1


@pytest.mark.asyncio
async def test_resume_advances_sequence_counter() -> None:
    """resume 后 save 下一次时 sequence 应该接续, 不重复."""
    service, store = _make_service()
    await service.save(tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[])
    await service.save(tenant_id="t-1", task_id="tk-1", step_idx=1, conversation_snapshot=[])
    # 模拟进程重启 — 用新 service 实例 + 同 store
    new_service = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.status_marker
    )
    resumed = await new_service.resume(tenant_id="t-1", task_id="tk-1")
    assert resumed is not None
    assert resumed.sequence == 1
    # 接下来 save 应该 sequence=2
    cp = await new_service.save(
        tenant_id="t-1", task_id="tk-1", step_idx=2, conversation_snapshot=[]
    )
    assert cp.sequence == 2


@pytest.mark.asyncio
async def test_resume_anchor_mismatch_marks_failed_resume() -> None:
    """anchor 变化 (e.g. 重新跑) → 旧 checkpoint 不能 resume."""
    service, store = _make_service()
    await service.save(
        tenant_id="t-1",
        task_id="tk-1",
        step_idx=0,
        conversation_snapshot=[],
        goal_anchor_id="ga-old",
    )
    resumed = await service.resume(
        tenant_id="t-1",
        task_id="tk-1",
        current_anchor_id="ga-new",
    )
    assert resumed is None
    # status_marker 被调
    assert len(store.status_marks) == 1
    tenant, _cp_id, status = store.status_marks[0]
    assert tenant == "t-1"
    assert status == "failed_resume"
    # 再次 resume 同 task → 应该没 active checkpoint
    resumed2 = await service.resume(
        tenant_id="t-1", task_id="tk-1", current_anchor_id="ga-new"
    )
    assert resumed2 is None


@pytest.mark.asyncio
async def test_resume_anchor_match_returns_checkpoint() -> None:
    service, _ = _make_service()
    await service.save(
        tenant_id="t-1",
        task_id="tk-1",
        step_idx=0,
        conversation_snapshot=[],
        goal_anchor_id="ga-x",
    )
    cp = await service.resume(
        tenant_id="t-1", task_id="tk-1", current_anchor_id="ga-x"
    )
    assert cp is not None
    assert cp.goal_anchor_id == "ga-x"


@pytest.mark.asyncio
async def test_resume_current_anchor_none_does_not_block() -> None:
    """current_anchor_id=None 时不做匹配 check (灵活)."""
    service, _ = _make_service()
    await service.save(
        tenant_id="t-1",
        task_id="tk-1",
        step_idx=0,
        conversation_snapshot=[],
        goal_anchor_id="ga-x",
    )
    cp = await service.resume(tenant_id="t-1", task_id="tk-1")
    assert cp is not None


# ---- finalize ----


@pytest.mark.asyncio
async def test_finalize_marks_final() -> None:
    service, store = _make_service()
    cp = await service.save(tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[])
    await service.finalize(tenant_id="t-1", checkpoint_id=cp.checkpoint_id)
    assert ("t-1", cp.checkpoint_id, "final") in store.status_marks


@pytest.mark.asyncio
async def test_finalize_requires_status_marker() -> None:
    """没注入 status_marker 时 finalize 应明确报."""

    async def writer(_: Any) -> None:
        return None

    async def reader(_a: str, _b: str) -> None:
        return None

    service = TaskCheckpointService(writer=writer, reader=reader)
    with pytest.raises(RuntimeError, match="status_marker"):
        await service.finalize(tenant_id="t-1", checkpoint_id="tcp-x")


# ---- tenant isolation (via fake store) ----


@pytest.mark.asyncio
async def test_resume_respects_tenant_isolation() -> None:
    service, _ = _make_service()
    await service.save(
        tenant_id="t-1", task_id="tk-1", step_idx=0, conversation_snapshot=[]
    )
    cp_other = await service.resume(tenant_id="t-other", task_id="tk-1")
    assert cp_other is None
