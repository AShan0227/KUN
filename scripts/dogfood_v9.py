"""Dogfood v9 — V7 §16 production-loop real long-task validation.

V7 Phase X.B's last big proof: a real long-running task driven through KUN's
WebSocket API, exercising ALL 4 X.B production wirings (mission_alignment_reviews
/ lifecycle_transitions / auditor_reports / ensemble_calls) under realistic
cross-family ensemble (gpt-5.5 + Qwen2.5-14b local).

Pre-conditions (verified by this script):
  - KUN API on http://localhost:8000 (uvicorn kun.api.main:app)
  - Ollama 0.24 + qwen2.5:14b-instruct-q4_K_M
  - PG at alembic head 0017
  - Env: KUN_V7_ENSEMBLE_ENABLED=true, KUN_V7_ENSEMBLE_TIERS=top,
    KUN_V7_ENSEMBLE_LOCAL_MODEL_ID=qwen2.5:14b-instruct-q4_K_M

Task focus (scoped for ~15-30 min, ~$0.5-1):
  Use self-reflect to read V7 doc + write a design-coherence report to
  docs/dist-output/dogfood-v9-design-coherence.md. Drives multi-step LLM
  loop → many ensemble_calls rows → some plan_review → maybe gate.

Output:
  docs/dev_logs/dogfood-v9-run-<timestamp>.log — full JSONL event stream
  stdout — compact per-event one-liner

Real cost (gpt-5.5 only — Qwen is free):
  ~30-100 LLM calls × ~$0.001-0.005 each = $0.05-0.50 total. Cap by max_steps.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

# ===================== Task description =====================

DOGFOOD_V9_TASK = """\
任务: 评估 KUN V7 §11.4 (multi-LLM ensemble) 和 §15 (capability lifecycle 9 阶段)
两个章节的设计是否互相支撑.

工具说明 (重要):
- 用 self-reflect skill 读写, 不要用 file-io
- 读: `<skill name="self-reflect">{"op":"read","path":"docs/v7/KUN-V7.md","offset":N,"limit":M}</skill>`
- 列目录: `<skill name="self-reflect">{"op":"list","path":"docs/v7"}</skill>`
- 写: `<skill name="self-reflect">{"op":"write","path":"dogfood-v9-design-coherence.md","content":"..."}</skill>` (path 省 docs/dist-output/ 前缀)

任务步骤:

1. self-reflect list `docs/v7/` 确认 KUN-V7.md 存在.

2. self-reflect read 大概找到 §11.4 multi-LLM ensemble 章节 (可以先读 offset=1 limit=100 看目录,
   再 offset=N limit=200 读相关段落). 记录: ensemble_invoke API 设计, 跨 family 强约束,
   consensus 策略 3 档, divergence_score 计算方法.

3. self-reflect read 大概找到 §15 capability lifecycle 9 阶段 (再次 offset+limit 分块).
   记录: 9 个阶段 (observation → candidate → replay → holdout → shadow → canary →
   production → monitor → rollback/retire), 严格验收 5 阶段, 三类 evidence, user_approval.

4. 对比两个设计是否互相支撑. 重点:
   - ensemble 的 divergence_score 高时, lifecycle 是否会回退/不晋级? (是 → 互相支撑)
   - lifecycle CANARY → PRODUCTION 是否需要 ensemble 验证作为 evidence 之一?
   - 这两套机制是分立的, 还是有 hook 让 ensemble 数据流入 lifecycle gate?

5. self-reflect write `dogfood-v9-design-coherence.md` 到 docs/dist-output/, 内容:
   - 摘要 (≤ 100 字)
   - §11.4 ensemble 设计要点 (≤ 150 字)
   - §15 lifecycle 设计要点 (≤ 150 字)
   - 互相支撑的点 (≥ 1 个具体例子)
   - 互相冲突或缺连接的点 (≥ 1 个具体例子, 如有)
   - 建议 (≥ 1 个具体改进)
   - 总长 400-700 字之间

成功标准:
- 步骤 1-5 全部完成
- docs/dist-output/dogfood-v9-design-coherence.md 真存在, 字数 400-700
- 评估有具体引用 (引用 §11.4 / §15 的具体内容, 不是泛泛而谈)

约束:
- 中文, 句子清晰
- 不改 KUN 代码 / V7 doc
- 不在 docs/dist-output/ 之外写
- 不调 shell (用 self-reflect 就够)
"""


# ===================== Driver =====================

WS_URL = "ws://localhost:8000/ws?tenant_id=u-sylvan"
LOG_DIR = Path(__file__).resolve().parent.parent / "docs" / "dev_logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _emit_to_log(fh, payload: dict) -> None:
    payload_with_ts = {"_recv_ts": datetime.now(UTC).isoformat(), **payload}
    fh.write(json.dumps(payload_with_ts, ensure_ascii=False) + "\n")
    fh.flush()


def _emit_to_stdout(payload: dict, *, started_at: float) -> None:
    kind = payload.get("type") or payload.get("kind") or "?"
    data = payload.get("data") or {}
    elapsed = time.perf_counter() - started_at

    if kind == "answer":
        content = data.get("content", "") or payload.get("content", "")
        print(f"\n[{elapsed:7.1f}s] ANSWER:\n{content}\n")
        return
    if kind == "done":
        result = data.get("result") or {}
        status = result.get("status", "?")
        print(f"[{elapsed:7.1f}s] DONE status={status}")
        return
    if kind == "error":
        msg = payload.get("message") or data.get("message", "")
        print(f"[{elapsed:7.1f}s] ERROR: {msg}")
        return

    preview_keys = ["stage", "task_id", "status", "step_idx", "verdict", "tier"]
    preview = {k: data[k] for k in preview_keys if k in data}
    summary = ", ".join(f"{k}={v}" for k, v in preview.items())
    print(f"[{elapsed:7.1f}s] {kind:30s} {summary}")


async def _count_rows(table_name: str) -> int:
    """Direct DB count of a Phase X.B table (bypasses cockpit reader)."""
    from kun.core.db import get_admin_sessionmaker
    from kun.core.orm import (
        AuditorReportRow,
        EnsembleCallRow,
        LifecycleTransitionRow,
        MissionAlignmentReviewRow,
    )
    from sqlalchemy import func, select

    table_to_orm = {
        "mission_alignment_reviews": MissionAlignmentReviewRow,
        "lifecycle_transitions": LifecycleTransitionRow,
        "auditor_reports": AuditorReportRow,
        "ensemble_calls": EnsembleCallRow,
    }
    sm = get_admin_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            select(func.count()).select_from(table_to_orm[table_name])
        )
        return int(result.scalar() or 0)


async def _snapshot_xb_tables() -> dict[str, int]:
    return {
        t: await _count_rows(t)
        for t in (
            "mission_alignment_reviews",
            "lifecycle_transitions",
            "auditor_reports",
            "ensemble_calls",
        )
    }


async def main() -> int:
    log_path = LOG_DIR / f"dogfood-v9-run-{_now_iso()}.log"
    print(f"📄 Event log: {log_path}")
    print(f"🌐 WS URL:    {WS_URL}")
    print("⚙️  ENV:")
    for k in (
        "KUN_V7_ENSEMBLE_ENABLED",
        "KUN_V7_ENSEMBLE_TIERS",
        "KUN_V7_ENSEMBLE_LOCAL_MODEL_ID",
    ):
        print(f"    {k}={os.environ.get(k, '<unset>')}")

    # Pre-flight: snapshot X.B tables
    print("\n📊 Pre-flight: X.B table snapshot")
    before = await _snapshot_xb_tables()
    for t, n in before.items():
        print(f"    {t:35s} rows={n}")

    print("\n⚙️  Starting dogfood v9 task — expected 15-30 min.")
    print("    Press Ctrl+C to interrupt (KUN keeps state in checkpoints).\n")

    started_at = time.perf_counter()
    done_received = False

    try:
        async with websockets.connect(WS_URL, max_size=10 * 1024 * 1024) as ws:
            with log_path.open("w", encoding="utf-8") as logf:
                # Record task input
                logf.write(
                    json.dumps(
                        {
                            "_recv_ts": datetime.now(UTC).isoformat(),
                            "_meta": "task_input",
                            "task_text": DOGFOOD_V9_TASK,
                            "before_snapshot": before,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

                await ws.send(
                    json.dumps(
                        {
                            "type": "user_message",
                            "content": DOGFOOD_V9_TASK,
                            "output_kind": "developer",
                        }
                    )
                )
                print("📤 Task sent. Streaming events...\n")

                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"⚠️  non-JSON frame: {raw[:200]}")
                        continue

                    _emit_to_log(logf, payload)
                    _emit_to_stdout(payload, started_at=started_at)

                    if payload.get("type") == "done" or payload.get("kind") == "done":
                        done_received = True
                        break

    except KeyboardInterrupt:
        print("\n⏹️  Interrupted. Checkpoint should preserve state.")
        return 130
    except Exception as e:
        print(f"\n❌ Driver error: {type(e).__name__}: {e}")
        return 1

    elapsed = time.perf_counter() - started_at

    # Post-flight: snapshot again, compute deltas
    print("\n\n📊 Post-flight: X.B table snapshot + delta")
    after = await _snapshot_xb_tables()
    deltas: dict[str, int] = {}
    for t, n_after in after.items():
        n_before = before[t]
        delta = n_after - n_before
        deltas[t] = delta
        marker = " ← +" + str(delta) if delta > 0 else ""
        print(f"    {t:35s} before={n_before} after={n_after}{marker}")

    # Append delta info to log
    with log_path.open("a", encoding="utf-8") as logf:
        logf.write(
            json.dumps(
                {
                    "_recv_ts": datetime.now(UTC).isoformat(),
                    "_meta": "completion_summary",
                    "elapsed_sec": elapsed,
                    "done_received": done_received,
                    "before_snapshot": before,
                    "after_snapshot": after,
                    "deltas": deltas,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    print(f"\n{'=' * 70}")
    if done_received:
        print(f"  ✅ Completed in {elapsed / 60:.1f} min")
        print(f"  📄 Full event log: {log_path}")
        print(
            "  🔥 Hot wirings: "
            + ", ".join(
                f"{t}+{d}" for t, d in deltas.items() if d > 0
            )
            or "  (no table grew — possible regression)"
        )
        # Check the artifact too
        artifact = (
            Path(__file__).resolve().parent.parent  # noqa: ASYNC240
            / "docs"
            / "dist-output"
            / "dogfood-v9-design-coherence.md"
        )
        if artifact.exists():
            sz = artifact.stat().st_size
            print(f"  📝 Artifact: {artifact} ({sz} bytes)")
        else:
            print(f"  ⚠️  Artifact NOT found: {artifact}")
        return 0
    print(f"  ⚠️  Stream ended without 'done' after {elapsed / 60:.1f} min")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
