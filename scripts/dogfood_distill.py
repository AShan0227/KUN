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

工具说明 (重要, 仔细读):
你的所有 read / list / write 操作必须通过 **self-reflect** skill, 不要用
file-io (它的 sandbox 是 /tmp/kun-skills, 跟仓库无关). self-reflect 设计就是
给这种"鲲读自己 + 写蒸馏产物"的任务用的:

  - 读: docs/ / seeds/ / kun/ / tests/ / scripts/ / alembic/ 都可以读, 用
    `<skill name="self-reflect">{"op":"read","path":"docs/dev_logs/LT-progress.md"}</skill>`
    大文件加 offset+limit (1-based 行号, 限制行数), e.g.
    `<skill name="self-reflect">{"op":"read","path":"X","offset":1,"limit":200}</skill>`
  - 列目录: `<skill name="self-reflect">{"op":"list","path":"docs/dev_logs"}</skill>`
  - 写: 只能写到 `docs/dist-output/` 下面, 任何其他路径会被拒. 用
    `<skill name="self-reflect">{"op":"write","path":"capability-map.md","content":"# ..."}</skill>`
    (path 可省 `docs/dist-output/` 前缀)
  - 不能 delete (本任务不需要, 现有文件保持 SHA 不变是硬约束)

任务范围 (5 阶段, 全部完成才算 done):

Phase A · 蒸馏: 用 self-reflect list docs/dev_logs/, 然后 read 关键文件
(LT-progress / LT-retrospective / PH1-user-testing-report / L1-L6 progress /
phase-0-documentation). 大文件用 offset+limit 分块读. 识别 Claude Code 在写
代码过程中表现出的工程招数 (TodoWrite 拆任务 / 并行多 Agent / grep verify
before assume / 测试驱动 / commit 纪律 ≤ 1000 行 / 错误立即修不藏 / ADR-025
dev log 沉淀 / 决策点停下问 / Read 行号定位不全文 cat / Bash 工具克制 等).
按能力类型归类, 用 self-reflect write 输出新文件
`claude-code-engineering-capabilities.md` 到 docs/dist-output/.

Phase B · 对比: 用 self-reflect list seeds/methodologies/ 列出现有 27 张
方法论, read 每张的 topic+title+description. 对比 Phase A 招数, 找出鲲缺
失的 ≥ 5 张 + 半有的 ≥ 5 张. write 输出新文件 `capability-gap-analysis.md`
到 docs/dist-output/.

Phase C · 写新 seeds: 每条 gap 写一张 yaml seed (含 topic / title /
description / trigger / action / anti_pattern / evidence / confidence /
applicability / related_methodologies / distilled_from / created_at). 至少 5
张, 全部 yaml.safe_load 通过. 写到 docs/dist-output/seeds-new/ 下 (每张一个
.yaml 文件). 鲲监督方后续会把这些 yaml 集成进 seeds/methodologies/.

Phase D · RSI 验证: 输出 docs/dist-output/rsi-mini-experiment-plan.md 描述
每张新 seed 应该怎么走 Selector + External Supervisor + Gate 的 mini 实验
(不需要真跑, 描述清晰可执行即可). ≥ 3 张 seed 的实验方案.

Phase E · 融会贯通: 新 seeds × 鲲已有硬能力 (RSI / Anti-drift / Adapter
Router / RecursivePlanner / 七角色并行) 出 ≥ 3 个 hybrid 增强提案. write
`hybrid-capability-proposals.md` 到 docs/dist-output/.

成功标准 (全部满足才算 done):
- 5 phase 全完成
- self-reflect write 真产出 ≥ 3 个 md 文件 + ≥ 5 个 yaml 文件 (在 docs/dist-output/)
- 现有 dev_logs / seeds / kun / tests 全部保持 SHA 不变 (self-reflect 设计就
  保证这点 — 它白名单读 + 单写出目录)

约束 (严格不做):
- 不改现有 service / 模块代码
- 不改 schema / RLS / 基础设施
- 不蒸馏 Claude Code 非工程能力 (对话风格 / UI 偏好等)

预估: 5 phase 多步骤复杂任务, 30-90 min, 复杂度 complex, risk medium.
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
