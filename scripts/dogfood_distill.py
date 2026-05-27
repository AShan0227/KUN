"""Dogfood KUN's long-task pipeline: distill Claude Code engineering capabilities.

Connects to a running KUN FastAPI server's WebSocket endpoint, sends the
5-phase distillation task, streams all events to a timestamped log file
plus stdout, exits when the 'done' event arrives.

Usage:
    # Terminal 1: start KUN
    .venv/bin/uvicorn kun.api.main:app --host 0.0.0.0 --port 8000

    # Terminal 2: fire the dogfood task
    .venv/bin/python scripts/dogfood_distill.py

Output:
    docs/dev_logs/dogfood-run-<timestamp>.log — full event stream
    stdout — key event kinds + final answer
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

# ===================== Task description =====================

DOGFOOD_TASK = """\
深度蒸馏 Claude Code 的工程能力, 融合鲲自己优势, 做出鲲专属升级.

任务范围 (5 阶段, 全部完成才算 done):

Phase A · 蒸馏: 读全部 docs/dev_logs/ 文件 (LT-progress / LT-retrospective /
PH1-user-testing-report / L1-L6 progress + retrospective + phase-0). 识别
Claude Code 在写代码过程中表现出的工程招数 (TodoWrite 拆任务 / 并行多 Agent /
grep verify before assume / 测试驱动 / commit 纪律 ≤ 1000 行 / 错误立即修不藏 /
ADR-025 dev log 沉淀 / 决策点停下问 / Read 行号定位不全文 cat / Bash 工具克制
等). 按能力类型归类输出新文件 docs/claude-code-engineering-capabilities.md.

Phase B · 对比: 把 Phase A 招数与现有 seeds/methodologies/ (27 张方法论, 跑
ls 看) 对比, 找出鲲缺失的 ≥ 5 张 + 半有的 ≥ 5 张. 输出新文件
docs/capability-gap-analysis.md.

Phase C · 写新 seeds: 每条 gap 写一张 yaml seed (含 topic / title /
description / trigger / action / anti_pattern / evidence / confidence /
applicability / related_methodologies / distilled_from / created_at). 至少 5
张, 全部 yaml.safe_load 通过.

Phase D · RSI 验证: 用 Selector + External Supervisor + Gate 跑 mini 实验,
≥ 3 张 seed 进 runtime_capabilities (state=merged 或 enabled).

Phase E · 融会贯通: 新 seeds × 鲲已有硬能力 (RSI / Anti-drift / Adapter Router /
RecursivePlanner / 七角色并行) 出 ≥ 3 个 hybrid 增强提案. 输出新文件
docs/hybrid-capability-proposals.md.

成功标准 (全部满足才算 done):
- 5 phase 全完成
- 文件产物齐 (3 个新 md + ≥ 5 张新 yaml)
- runtime_capabilities 新增 ≥ 3 行
- 现有 1582 测试通过 (跑 pytest 验证)
- 现有 27 seeds 全部保留 (只增不改, 数量只可增加)
- dev_logs 全部保持只读 (任务期间所有 dev_logs 文件 SHA 不变)

约束 (out_of_scope, 严格不做):
- 现有 service / 模块的重构
- schema / RLS / 基础设施的改动
- Claude Code 非工程能力 (对话风格 / UI 偏好等)

不变量 (全程保持):
- 现有测试持续通过
- 现有 seeds 持续可用 (数量 ≥ 27)
- 现有 dev_logs 全部为只读
- 任务全程产出新文件 only, 已有文件 SHA 保持

预估: 5 phase 多步骤复杂任务, 估计 1-2 小时, 复杂度 complex, risk medium.
"""


# ===================== Driver =====================


WS_URL = "ws://localhost:8000/ws?tenant_id=u-dogfood"
LOG_DIR = Path(__file__).resolve().parent.parent / "docs" / "dev_logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _emit_to_log(fh: object, payload: dict) -> None:
    """Append one JSONL event to the run log."""
    payload_with_ts = {"_recv_ts": datetime.now(UTC).isoformat(), **payload}
    line = json.dumps(payload_with_ts, ensure_ascii=False) + "\n"
    fh.write(line)  # type: ignore[attr-defined]
    fh.flush()  # type: ignore[attr-defined]


def _emit_to_stdout(payload: dict, *, started_at: float) -> None:
    """Print a compact one-line view of each event."""
    kind = payload.get("type") or payload.get("kind") or "?"
    data = payload.get("data") or {}
    elapsed = time.perf_counter() - started_at

    if kind == "answer":
        content = data.get("content", "") or payload.get("content", "")
        print(f"\n[{elapsed:7.1f}s] ✅ ANSWER:\n{content}\n")
        return
    if kind == "done":
        result = data.get("result") or {}
        status = result.get("status", "?")
        print(f"[{elapsed:7.1f}s] 🏁 DONE status={status}")
        return
    if kind == "error":
        msg = payload.get("message") or data.get("message", "")
        print(f"[{elapsed:7.1f}s] ❌ ERROR: {msg}")
        return

    # Compact data preview
    preview_keys = ["stage", "task_id", "status", "step_idx", "verdict",
                    "tier", "node_count", "leaves", "ratio"]
    preview = {k: data[k] for k in preview_keys if k in data}
    summary = ", ".join(f"{k}={v}" for k, v in preview.items())
    print(f"[{elapsed:7.1f}s] {kind:30s} {summary}")


async def main() -> int:
    log_path = LOG_DIR / f"dogfood-run-{_now_iso()}.log"
    print(f"📄 Event log: {log_path}")
    print(f"🌐 WS URL:    {WS_URL}")
    print("⚙️  Starting dogfood task — this will take 1-2 hours.\n")
    print("Press Ctrl+C to interrupt (KUN keeps state in checkpoints).\n")

    started_at = time.perf_counter()
    done_received = False

    try:
        async with websockets.connect(WS_URL, max_size=10 * 1024 * 1024) as ws:
            with log_path.open("w", encoding="utf-8") as logf:
                # Log task input
                logf.write(
                    json.dumps(
                        {
                            "_recv_ts": datetime.now(UTC).isoformat(),
                            "_meta": "task_input",
                            "task_text": DOGFOOD_TASK,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

                # Send the task
                await ws.send(
                    json.dumps(
                        {
                            "type": "user_message",
                            "content": DOGFOOD_TASK,
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
    except websockets.ConnectionClosed as e:
        print(f"\n❌ WS closed unexpectedly: code={e.code} reason={e.reason}")
        return 1
    except Exception as e:
        print(f"\n❌ Driver crashed: {type(e).__name__}: {e}")
        return 1

    elapsed = time.perf_counter() - started_at
    if done_received:
        print(f"\n✅ Completed in {elapsed/60:.1f} min")
        print(f"   Full event log: {log_path}")
        return 0
    else:
        print(f"\n⚠️  Stream ended without 'done' event after {elapsed/60:.1f} min")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
