"""Bug Root-Cause Case Library 单测 (alembic 0013) — RCDH fast-path 案例库.

Coverage:
  - trace_signature stable + 不同 input 不同输出 + top 3 internal frame 优先
  - lookup_case 命中 → 返 BugCase + hit_count++
  - lookup_case 未命中 → None
  - lookup_case reader 异常 → None (不破坏调用方完整诊断回退)
  - record_case 新建 → 返 case_id
  - record_case 同 signature 重复 → upsert hit_count
  - record_case writer 异常 → raise (状态机推进失败必须可见)
  - record_case + lookup_case round-trip
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.governance.bug_case_library import (
    BugCase,
    lookup_case,
    record_case,
    trace_signature,
)

# ---- _FakeStore: in-memory writer + reader, simulating DB ----


class _FakeStore:
    """In-memory reader + writer; upsert by (tenant_id, trace_signature)."""

    def __init__(self) -> None:
        # 内部: key = (tenant_id, signature) → row dict
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        # writer 调用次数 (用于检查是否落库)
        self.write_count = 0
        # 模拟 reader / writer 异常
        self.reader_should_raise = False
        self.writer_should_raise = False

    async def reader(self, tenant_id: str, signature: str) -> dict[str, Any] | None:
        if self.reader_should_raise:
            raise RuntimeError("simulated DB read failure")
        row = self.rows.get((tenant_id, signature))
        if row is None:
            return None
        return dict(row)

    async def writer(self, row: dict[str, Any]) -> None:
        if self.writer_should_raise:
            raise RuntimeError("simulated DB write failure")
        self.write_count += 1
        key = (row["tenant_id"], row["trace_signature"])
        self.rows[key] = dict(row)


# ---- trace_signature ----


def test_trace_signature_stable_same_input_same_output() -> None:
    """同 input 同输出 (deterministic)."""
    err = "AssertionError"
    lines = [
        'File "kun/foo/bar.py", line 10, in baz',
        'File "kun/x/y.py", line 20, in qux',
        'File "tests/test_z.py", line 5, in test_baz',
    ]
    sig1 = trace_signature(err, lines)
    sig2 = trace_signature(err, lines)
    assert sig1 == sig2
    assert sig1.startswith("AssertionError|")
    assert "hash:" in sig1


def test_trace_signature_different_inputs_different_outputs() -> None:
    """不同 input 不同输出 (碰撞概率 ≈ 2^-32 in 8 hex)."""
    sig_a = trace_signature("AssertionError", ['File "kun/a.py", line 1, in foo'])
    sig_b = trace_signature("AssertionError", ['File "kun/b.py", line 1, in foo'])
    sig_c = trace_signature("KeyError", ['File "kun/a.py", line 1, in foo'])
    assert sig_a != sig_b
    assert sig_a != sig_c
    assert sig_b != sig_c


def test_trace_signature_picks_top_3_internal_frames_before_site_packages() -> None:
    """top 3 frame 取 kun.* 优先, .venv/site-packages 后."""
    # 故意把 site-packages 放前面; signature 应优先 internal
    lines = [
        'File "/path/to/.venv/lib/python3.12/site-packages/pkg/x.py", line 99, in ext',
        'File "kun/foo/bar.py", line 10, in baz_inner',
        'File "/usr/lib/python3.12/builtin.py", line 1, in builtin_fn',
        'File "kun/x/y.py", line 20, in y_func',
        'File "kun/z/w.py", line 30, in w_func',
    ]
    sig = trace_signature("RuntimeError", lines)
    # 内部 frame ident 应该在签名里
    assert "kun/foo/bar.py:baz_inner" in sig
    assert "kun/x/y.py:y_func" in sig
    assert "kun/z/w.py:w_func" in sig
    # site-packages 不应在 top-3 中 (因为有 3 个 internal)
    assert "site-packages" not in sig
    assert "/usr/lib/python" not in sig


def test_trace_signature_with_only_external_frames_uses_external() -> None:
    """没 internal frame 时退化到 external (好过无 signature)."""
    lines = [
        'File "/.venv/lib/python3.12/site-packages/x/y.py", line 1, in ext_a',
        'File "/.venv/lib/python3.12/site-packages/x/z.py", line 2, in ext_b',
    ]
    sig = trace_signature("ValueError", lines)
    assert sig.startswith("ValueError|")
    # external frame ident 应该 carry 上来 (无 internal 退化)
    assert "ext_a" in sig or "ext_b" in sig


def test_trace_signature_with_no_recognizable_frames_still_stable() -> None:
    """无可识别 frame 时仍返稳定 signature (只 error_type + hash)."""
    sig1 = trace_signature("CustomError", ["random text", "no frames here"])
    sig2 = trace_signature("CustomError", ["random text", "no frames here"])
    assert sig1 == sig2
    assert sig1.startswith("CustomError|")


def test_trace_signature_handles_pytest_style_frames() -> None:
    """pytest 风格 frame (tests/x.py::test_y) 也被 ID."""
    lines = ["tests/unit/test_foo.py::test_bar FAILED"]
    sig = trace_signature("AssertionError", lines)
    assert "tests/unit/test_foo.py::test_bar" in sig


def test_trace_signature_handles_dotted_module_path() -> None:
    """Dotted module path (kun.foo.bar) 也被 ID."""
    lines = ["error in kun.governance.rcdh module"]
    sig = trace_signature("ImportError", lines)
    assert "kun.governance.rcdh" in sig


# ---- lookup_case ----


@pytest.mark.asyncio
async def test_lookup_case_miss_returns_none() -> None:
    """未命中 → None."""
    store = _FakeStore()
    out = await lookup_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=['File "kun/foo.py", line 1, in bar'],
        reader=store.reader,
    )
    assert out is None


@pytest.mark.asyncio
async def test_lookup_case_no_reader_returns_none() -> None:
    """No reader provided → None (案例库 best-effort)."""
    out = await lookup_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=["x"],
        reader=None,
    )
    assert out is None


@pytest.mark.asyncio
async def test_lookup_case_hit_returns_bugcase_and_increments_hit_count() -> None:
    """命中 → 返 BugCase, hit_count++ + last_hit_at 更新."""
    store = _FakeStore()
    # 先 record 一条
    case_id = await record_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=['File "kun/foo.py", line 1, in bar'],
        root_cause_kind="double_responsibility",
        fix_pattern="拆分模块职责",
        writer=store.writer,
    )
    initial_writes = store.write_count

    # 然后 lookup 应该命中
    out = await lookup_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=['File "kun/foo.py", line 1, in bar'],
        reader=store.reader,
        writer=store.writer,
    )
    assert out is not None
    assert isinstance(out, BugCase)
    assert out.case_id == case_id
    assert out.root_cause_kind == "double_responsibility"
    assert out.hit_count == 2  # 初始 1, lookup +1
    # writer 应该被调一次 (hit_count update)
    assert store.write_count == initial_writes + 1


@pytest.mark.asyncio
async def test_lookup_case_reader_exception_returns_none() -> None:
    """reader 异常 → None (不破坏调用方完整诊断回退)."""
    store = _FakeStore()
    store.reader_should_raise = True
    out = await lookup_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=["x"],
        reader=store.reader,
    )
    assert out is None


# ---- record_case ----


@pytest.mark.asyncio
async def test_record_case_creates_new_returns_case_id() -> None:
    """新建 → 返 case_id (bc- prefix)."""
    store = _FakeStore()
    case_id = await record_case(
        tenant_id="t-acme",
        error_type="KeyError",
        trace_lines=['File "kun/x.py", line 1, in y'],
        root_cause_kind="missing_migration",
        fix_pattern="alembic upgrade head + 加 default 值",
        writer=store.writer,
    )
    assert case_id.startswith("bc-")
    assert store.write_count == 1
    # 表中有一条 row
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_record_case_writer_exception_raises() -> None:
    """writer 异常 → raise (状态机推进失败必须可见, ADR-024 contract)."""
    store = _FakeStore()
    store.writer_should_raise = True
    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        await record_case(
            tenant_id="t-acme",
            error_type="AssertionError",
            trace_lines=["x"],
            root_cause_kind="race_condition",
            fix_pattern="加锁",
            writer=store.writer,
        )


@pytest.mark.asyncio
async def test_record_case_no_writer_raises() -> None:
    """No writer provided → raise (不允许静默不落库)."""
    with pytest.raises(RuntimeError, match="requires writer"):
        await record_case(
            tenant_id="t-acme",
            error_type="AssertionError",
            trace_lines=["x"],
            root_cause_kind="race_condition",
            fix_pattern="加锁",
            writer=None,
        )


@pytest.mark.asyncio
async def test_record_case_same_signature_upserts_hit_count() -> None:
    """同 signature 重复 record → upsert (case_id 保持, hit_count +1)."""
    store = _FakeStore()
    args = {
        "tenant_id": "t-acme",
        "error_type": "AssertionError",
        "trace_lines": ['File "kun/foo.py", line 1, in bar'],
        "writer": store.writer,
        "reader": store.reader,
    }
    case_id_1 = await record_case(
        **args,
        root_cause_kind="double_responsibility",
        fix_pattern="拆分职责",
    )
    case_id_2 = await record_case(
        **args,
        root_cause_kind="double_responsibility",
        fix_pattern="拆分职责 (refined)",
    )
    assert case_id_1 == case_id_2  # 同 case_id
    # 表中仍只一条 row (upsert)
    assert len(store.rows) == 1
    # hit_count = 2 (初始 1, 第二次 +1)
    row = next(iter(store.rows.values()))
    assert row["hit_count"] == 2
    # fix_pattern 用最新的
    assert row["fix_pattern"] == "拆分职责 (refined)"


@pytest.mark.asyncio
async def test_record_and_lookup_round_trip() -> None:
    """record_case + lookup_case round-trip — record → lookup 必命中."""
    store = _FakeStore()

    trace = [
        'File "kun/agents/executor/service.py", line 42, in handle',
        'File "kun/governance/rcdh.py", line 100, in run_diagnostic',
        'File "tests/test_e2e.py", line 8, in test_flow',
    ]
    case_id = await record_case(
        tenant_id="t-acme",
        error_type="ValueError",
        trace_lines=trace,
        root_cause_kind="tenant_isolation_leak",
        fix_pattern="set_tenant_id() before query\n--\n用 session_scope context manager",
        evidence_dx_id="dx-abc123",
        writer=store.writer,
    )

    # Round-trip lookup
    found = await lookup_case(
        tenant_id="t-acme",
        error_type="ValueError",
        trace_lines=trace,
        reader=store.reader,
        writer=store.writer,
    )
    assert found is not None
    assert found.case_id == case_id
    assert found.root_cause_kind == "tenant_isolation_leak"
    assert "session_scope" in found.fix_pattern
    assert found.evidence_dx_id == "dx-abc123"
    assert found.hit_count == 2  # 初始 1 + lookup +1


@pytest.mark.asyncio
async def test_lookup_case_tenant_isolation() -> None:
    """不同 tenant 同 signature 互不命中 (ADR-007 隔离 by tenant_id)."""
    store = _FakeStore()
    trace = ['File "kun/foo.py", line 1, in bar']
    await record_case(
        tenant_id="t-acme",
        error_type="AssertionError",
        trace_lines=trace,
        root_cause_kind="race_condition",
        fix_pattern="加锁",
        writer=store.writer,
    )
    # 同 signature 但不同 tenant — 必须 miss
    out = await lookup_case(
        tenant_id="t-other-tenant",
        error_type="AssertionError",
        trace_lines=trace,
        reader=store.reader,
        writer=store.writer,
    )
    assert out is None


# ---- BugCase dataclass sanity ----


def test_bugcase_to_row_payload_round_trip() -> None:
    """BugCase ↔ row dict round-trip (to_row_payload + from_row)."""
    cp = BugCase(
        case_id="bc-1",
        trace_signature="AssertionError|kun.foo.bar|hash:deadbeef",
        error_type="AssertionError",
        root_cause_kind="double_responsibility",
        fix_pattern="拆分模块",
        evidence_dx_id="dx-1",
        hit_count=3,
    )
    row = cp.to_row_payload(tenant_id="t-acme")
    assert row["tenant_id"] == "t-acme"
    assert row["case_id"] == "bc-1"
    assert row["trace_signature"].startswith("AssertionError|")
    assert row["hit_count"] == 3

    # round-trip back
    rebuilt = BugCase.from_row(row)
    assert rebuilt.case_id == cp.case_id
    assert rebuilt.trace_signature == cp.trace_signature
    assert rebuilt.hit_count == cp.hit_count
    assert rebuilt.fix_pattern == cp.fix_pattern
