"""Bug Root-Cause Case Library — RCDH fast-path 案例库 (alembic 0013).

RCDH 4 级诊断每个 trace 从头分析. 但 Claude Code 一眼能看懂 trace 是因为
见过几万次类似 bug. 这个模块把"上次见过相同 signature 的 trace"工程化
成 O(1) lookup — 命中直接返 fix_pattern, 没命中再走 RCDH 完整诊断.

设计要点 (ADR-024 frozen_dataclass 模式):
  - BugCase frozen dataclass — 跨 process 序列化友好
  - to_row_payload(tenant_id) → dict for ORM writer (service 不导 sqlalchemy)
  - reader / writer 都是 callback (DI), 测试用 fake, prod 接真 session_scope
  - trace_signature 稳定: 同 (error_type, top-3 frames) 得同 signature

trace_signature 算法:
  1. error_type 放最前 (e.g. "AssertionError")
  2. top 3 frame: kun.* / kun/* 优先 (in-codebase frames), site-packages / .venv 后
  3. SHA256(joined) hex 前 8 位附在末尾防长字符串
  → "AssertionError|kun.foo.bar|kun.x.y|test_z.py:test_baz|hash:abc12345"

命中语义:
  - lookup_case: reader(tenant_id, signature) → row | None.
    命中 → 同步 hit_count++ + last_hit_at = now (通过 writer 调).
    reader 异常 → 返 None (案例库 best-effort, 不破坏调用方完整诊断回退).
  - record_case: writer(row) 插入或 upsert.
    writer 异常 → raise (状态机推进失败必须可见, ADR-024 contract).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.governance.bug_case_library")


# Reader: 给 (tenant_id, signature) 返回 row dict | None.
BugCaseReader = Callable[[str, str], Awaitable["dict[str, Any] | None"]]

# Writer: 拿 row dict, 落库 (insert / upsert).
BugCaseWriter = Callable[[dict[str, Any]], Awaitable[None]]


# ---- frame 内外部判定 ----

_INTERNAL_PREFIXES = ("kun.", "kun/")
_EXTERNAL_MARKERS = (
    ".venv/",
    "site-packages/",
    "/dist-packages/",
    "/usr/lib/python",
    "/Library/Frameworks/Python.framework/",
)

# 提取 frame 标识符 — 尽可能稳定的形式
# 支持 "File \"x.py\", line N, in foo" / "kun.foo.bar.baz" / "x.py:test_y"
_FILE_FRAME_RE = re.compile(
    r'File "([^"]+)", line \d+, in (\w+)'
)
_DOTTED_RE = re.compile(r"\bkun(?:\.[a-zA-Z_][a-zA-Z0-9_]*){1,6}\b")
_PATH_RE = re.compile(r"\bkun(?:/[a-zA-Z_][a-zA-Z0-9_]*){1,6}\b")
_PYTEST_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_/.]*\.py)::([a-zA-Z_][a-zA-Z0-9_]*)\b")
_PYTEST_COLON_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_/.]*\.py):([a-zA-Z_][a-zA-Z0-9_]*)\b")


def _frame_is_internal(frame: str) -> bool:
    """frame 是否是 kun 内部 frame (vs site-packages / stdlib)."""
    if any(marker in frame for marker in _EXTERNAL_MARKERS):
        return False
    return any(frame.startswith(prefix) or prefix in frame for prefix in _INTERNAL_PREFIXES)


def _extract_frame_ident(line: str) -> str | None:
    """从一条 trace 行抽取 stable frame 标识符. None if 无可识别 frame."""
    line = line.strip()
    if not line:
        return None

    # 1. 标准 traceback: File "x.py", line N, in func
    m = _FILE_FRAME_RE.search(line)
    if m:
        file_path, func_name = m.group(1), m.group(2)
        # 规范化: kun 内部 frame 用模块路径, 外部用 file:func
        if any(marker in file_path for marker in _EXTERNAL_MARKERS):
            return f"{file_path}:{func_name}"
        # 提取 kun.foo.bar 风格 if possible
        return f"{file_path}:{func_name}"

    # 2. pytest 风格: tests/x.py::test_y
    m = _PYTEST_RE.search(line)
    if m:
        return f"{m.group(1)}::{m.group(2)}"
    m = _PYTEST_COLON_RE.search(line)
    if m:
        return f"{m.group(1)}:{m.group(2)}"

    # 3. dotted module path: kun.foo.bar.baz
    m = _DOTTED_RE.search(line)
    if m:
        return m.group(0)

    # 4. slash path: kun/foo/bar
    m = _PATH_RE.search(line)
    if m:
        return m.group(0)

    return None


def _is_internal_frame(frame_ident: str) -> bool:
    """frame 标识符是否是 kun 内部 (排在 site-packages 前)."""
    if any(marker in frame_ident for marker in _EXTERNAL_MARKERS):
        return False
    return frame_ident.startswith("kun.") or frame_ident.startswith("kun/")


def trace_signature(error_type: str, trace_lines: list[str]) -> str:
    """Generate stable signature for a trace.

    error_type + top 3 内部 frame (kun.* 优先, .venv/site-packages 后) 用 '|' 连
    + SHA256 prefix (8 hex chars) 防 long string.

    返回如:
      "AssertionError|kun.foo.bar|kun.x.y|test_z.py:test_baz|hash:abc12345"

    同 input 同输出 (deterministic). 不同 input 不同输出 (碰撞概率 ≈ 2^-32 in 8 hex).
    """
    err = (error_type or "UnknownError").strip()

    # 1. 抽取所有 frame_ident
    idents: list[str] = []
    for line in trace_lines:
        if not isinstance(line, str):
            continue
        ident = _extract_frame_ident(line)
        if ident is not None:
            idents.append(ident)

    # 2. 内部 frame 优先: kun.* / kun/* 先, 外部后. 保持原顺序作为 tiebreak.
    internal: list[str] = []
    external: list[str] = []
    for ident in idents:
        if _is_internal_frame(ident):
            internal.append(ident)
        else:
            external.append(ident)

    # 3. 取 top 3 (优先内部)
    top_frames = (internal + external)[:3]

    # 4. 组合 base + hash
    base = "|".join([err, *top_frames])
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{base}|hash:{digest}"


# ---- BugCase dataclass ----


@dataclass(frozen=True)
class BugCase:
    """Bug 根因案例 — RCDH fast-path lookup 结果."""

    case_id: str
    trace_signature: str
    error_type: str
    root_cause_kind: str
    fix_pattern: str
    evidence_dx_id: str | None
    hit_count: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_hit_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        """转 ORM 可写 dict (service 不导 sqlalchemy)."""
        return {
            "tenant_id": tenant_id,
            "case_id": self.case_id,
            "trace_signature": self.trace_signature,
            "error_type": self.error_type,
            "root_cause_kind": self.root_cause_kind,
            "fix_pattern": self.fix_pattern,
            "evidence_dx_id": self.evidence_dx_id,
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BugCase:
        """从 ORM row dict rebuild BugCase (reader 路径)."""
        return cls(
            case_id=row["case_id"],
            trace_signature=row["trace_signature"],
            error_type=row["error_type"],
            root_cause_kind=row["root_cause_kind"],
            fix_pattern=row["fix_pattern"],
            evidence_dx_id=row.get("evidence_dx_id"),
            hit_count=int(row.get("hit_count", 1)),
            created_at=row.get("created_at", datetime.now(UTC)),
            last_hit_at=row.get("last_hit_at", datetime.now(UTC)),
        )


# ---- Service ops ----


async def lookup_case(
    *,
    tenant_id: str,
    error_type: str,
    trace_lines: list[str],
    reader: BugCaseReader | None = None,
    writer: BugCaseWriter | None = None,
) -> BugCase | None:
    """Look up case by trace signature. O(1) reader hit by (tenant, signature).

    Args:
        tenant_id:    租户 ID (ADR-007 隔离).
        error_type:   异常类型 (e.g. "AssertionError").
        trace_lines:  trace 行列表 (任意格式 — 标准 Traceback / pytest / 模块路径).
        reader:       async fn(tenant_id, signature) → row dict | None.
                      None → 返回 None (无 reader = 无案例库, best-effort).
        writer:       async fn(row dict) → None — 命中后递增 hit_count + last_hit_at.
                      None → 命中后不更新 (best-effort statistic).

    Returns:
        BugCase if 命中 (hit_count 已 +1), else None.

    异常处理:
        reader 异常 → log warning, 返 None (案例库失败不破坏 RCDH 完整诊断回退).
        writer 异常 (hit_count update) → log warning, 仍返 BugCase (统计 best-effort).
    """
    if reader is None:
        return None

    sig = trace_signature(error_type, trace_lines)

    try:
        row = await reader(tenant_id, sig)
    except Exception as e:
        log.warning(
            "bug_case_library.lookup_reader_failed",
            tenant_id=tenant_id,
            signature_preview=sig[:64],
            error=str(e),
        )
        return None

    if row is None:
        log.debug(
            "bug_case_library.miss",
            tenant_id=tenant_id,
            signature_preview=sig[:64],
        )
        return None

    case = BugCase.from_row(row)

    # 同步 hit_count + last_hit_at — best-effort 更新 (失败仅 log)
    new_hit_count = case.hit_count + 1
    new_last_hit = datetime.now(UTC)
    updated = BugCase(
        case_id=case.case_id,
        trace_signature=case.trace_signature,
        error_type=case.error_type,
        root_cause_kind=case.root_cause_kind,
        fix_pattern=case.fix_pattern,
        evidence_dx_id=case.evidence_dx_id,
        hit_count=new_hit_count,
        created_at=case.created_at,
        last_hit_at=new_last_hit,
    )
    if writer is not None:
        try:
            await writer(updated.to_row_payload(tenant_id))
        except Exception as e:
            log.warning(
                "bug_case_library.lookup_hit_count_update_failed",
                tenant_id=tenant_id,
                case_id=case.case_id,
                error=str(e),
            )

    log.info(
        "bug_case_library.hit",
        tenant_id=tenant_id,
        case_id=case.case_id,
        hit_count=new_hit_count,
        root_cause_kind=case.root_cause_kind,
    )
    return updated


async def record_case(
    *,
    tenant_id: str,
    error_type: str,
    trace_lines: list[str],
    root_cause_kind: str,
    fix_pattern: str,
    evidence_dx_id: str | None = None,
    reader: BugCaseReader | None = None,
    writer: BugCaseWriter | None = None,
) -> str:
    """Insert or upsert (on signature conflict update hit_count). Returns case_id.

    Args:
        tenant_id:        租户 ID.
        error_type:       异常类型.
        trace_lines:      trace 行列表 (用于 signature 计算).
        root_cause_kind:  e.g. 'double_responsibility' / 'race_condition' /
                          'missing_migration' / 'keyword_false_positive' /
                          'tenant_isolation_leak' / ...
        fix_pattern:      中英描述如何修. 多个修法 \\n--\\n 分割.
        evidence_dx_id:   关联 diagnostic_records 行 ID (可选).
        reader:           async fn(tenant_id, signature) → row | None.
                          有 reader → 先查同 signature 已存在 → 走 upsert 路径.
        writer:           async fn(row) → None — 必填. None → raise.

    Returns:
        case_id (新建 or 既有).

    异常处理:
        writer 异常 → raise (状态机推进失败必须可见, ADR-024 contract).
    """
    if writer is None:
        raise RuntimeError(
            "record_case requires writer; none provided "
            "(state-machine advance must not silently no-op)"
        )

    sig = trace_signature(error_type, trace_lines)

    # 如有 reader, 先尝试上次的 case (signature 已存在 → upsert 增 hit_count)
    existing_row: dict[str, Any] | None = None
    if reader is not None:
        try:
            existing_row = await reader(tenant_id, sig)
        except Exception as e:
            log.warning(
                "bug_case_library.record_reader_failed_fallthrough_insert",
                tenant_id=tenant_id,
                signature_preview=sig[:64],
                error=str(e),
            )
            existing_row = None

    if existing_row is not None:
        # Upsert path: 用既有 case_id, 增 hit_count + 更新 last_hit_at, 用新 fix_pattern
        case_id = existing_row["case_id"]
        new_hit_count = int(existing_row.get("hit_count", 1)) + 1
        upserted = BugCase(
            case_id=case_id,
            trace_signature=sig,
            error_type=error_type,
            root_cause_kind=root_cause_kind,
            fix_pattern=fix_pattern,
            evidence_dx_id=evidence_dx_id or existing_row.get("evidence_dx_id"),
            hit_count=new_hit_count,
            created_at=existing_row.get("created_at", datetime.now(UTC)),
            last_hit_at=datetime.now(UTC),
        )
        await writer(upserted.to_row_payload(tenant_id))  # raise 上抛
        log.info(
            "bug_case_library.upserted",
            tenant_id=tenant_id,
            case_id=case_id,
            hit_count=new_hit_count,
            root_cause_kind=root_cause_kind,
        )
        return case_id

    # Insert path: 新建
    case_id = new_id("bug_case")
    fresh = BugCase(
        case_id=case_id,
        trace_signature=sig,
        error_type=error_type,
        root_cause_kind=root_cause_kind,
        fix_pattern=fix_pattern,
        evidence_dx_id=evidence_dx_id,
        hit_count=1,
    )
    await writer(fresh.to_row_payload(tenant_id))  # raise 上抛
    log.info(
        "bug_case_library.recorded",
        tenant_id=tenant_id,
        case_id=case_id,
        root_cause_kind=root_cause_kind,
        signature_preview=sig[:64],
    )
    return case_id


__all__ = [
    "BugCase",
    "BugCaseReader",
    "BugCaseWriter",
    "lookup_case",
    "record_case",
    "trace_signature",
]
