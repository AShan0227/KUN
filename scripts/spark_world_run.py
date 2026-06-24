"""X.J supervisor harness — run 鲲 (LongTaskOrchestrator) on the real
Spark World children's-game task, with Claude as external supervisor.

This is NOT a stubbed dogfood. It wires the **production adapters**
(make_llm_invoker + make_tool_executor + LongTaskRuntimeBundle) into
LongTaskOrchestrator and drives the main execution loop with a REAL LLM,
which writes real files / runs real shell in a sandboxed workspace.

Sandbox: KUN_SKILL_EXEC_ROOTS + KUN_SKILL_FILE_ROOT are pointed at
~/Desktop/SparkWorld so 鲲's shell-exec / file-io skills build there.

Supervision: every OrchestratorEvent is printed + appended to
~/Desktop/SparkWorld/logs/kun-run-<ts>.jsonl so the supervisor (Claude)
and the user can watch what 鲲 does, catch drift, and optimize.

Modes:
  smoke — 4-step env-validation (no code written), proves the real-LLM
          main-loop + shell/file skills work in the workspace. Cheap.
  m1    — build/extend the 颜色岛 MVP (longer, real cost).

Run: .venv/bin/python scripts/spark_world_run.py smoke
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKSPACE = Path("~/Desktop/SparkWorld").expanduser()


def _setup_env() -> None:
    import os

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass
    # 鲲's executable skills build in the Spark World workspace
    os.environ["KUN_SKILL_EXEC_ROOTS"] = str(WORKSPACE)
    os.environ["KUN_SKILL_FILE_ROOT"] = str(WORKSPACE)
    # Provider selection (audit F156: this comment must match the env set below).
    # History — two paths proved flaky during dogfood:
    #  - claude CLI OAuth (`claude -p` subprocess): hung 180s × 3 → RetryError → a
    #    silent stub fallback that echoed the prompt (a no-op masquerading as success);
    #  - earlier codex_only runs hit a TLS-handshake EOF to chatgpt.com.
    # Current decision: the user's Anthropic subscription is rate-limited and Codex
    # (gpt-5.5) has separate quota, so we PIN ALL tiers to the Codex MCP provider.
    # KUN_CODEX_ONLY=1 routes everything to Codex MCP (gpt-5.5); requires codex CLI
    # logged in. We intentionally do NOT disable the codex CLI path here
    # (KUN_DISABLE_CODEX_CLI=0) and clear any stale KUN_DISABLE_CLI_OAUTH override.
    # (Process-scoped run script: these env writes are intentionally not restored.)
    os.environ["KUN_CODEX_ONLY"] = "1"
    os.environ["KUN_DISABLE_CODEX_CLI"] = "0"
    os.environ.pop("KUN_DISABLE_CLI_OAUTH", None)
    # Turn on the 4 opt-in long-task mechanisms (trifecta/methodology/critique/discipline)
    os.environ.setdefault("KUN_V7_TRIFECTA_ENABLED", "true")
    os.environ.setdefault("KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED", "true")
    os.environ.setdefault("KUN_V7_TRIFECTA_EVERY_N_STEPS", "4")
    os.environ.setdefault("KUN_V7_METHODOLOGY_INJECT_ENABLED", "true")
    os.environ.setdefault("KUN_V7_CRITIQUE_EVERY_N_STEPS", "4")
    os.environ.setdefault("KUN_V7_DISCIPLINE_ENFORCER_ENABLED", "true")


# 鲲 conveys tools as a text directive; the LLM emits <skill name="...">{json}</skill>
SKILL_DIRECTIVE = """\
你有以下工具, 用 XML 调用 (一次可多个): <skill name="工具名">{JSON 参数}</skill>

- shell-exec: 跑 shell 命令。{"command":"...", "timeout_sec":120}
  cwd 默认就是工作区根 (~/Desktop/SparkWorld), 相对路径即可。可跑 node/npm/ls/cat/git。
- file-io: 读写文件。{"op":"read","path":"产品执行方案.md"} / {"op":"write","path":"src/x.js","content":"..."} / {"op":"list","path":"src"}
  path 相对工作区根。write 会自动建父目录。
- grep-verify: 在写代码前先查证, 不要凭记忆。{"pattern":"...", "path":"src"}
- web-search: 查外部资料/开源/文档。{"query":"..."}

写大文件/代码: 优先用 shell-exec heredoc (cat > path <<'EOF' ... EOF) 更稳;
file-io 适合小文件和读取 —— 把大段代码塞进 JSON content 容易触发 bad_json。

纪律: 先查证再动手; 改完用 shell-exec 跑起来验证; 不要假设, 用工具确认。
完成当前目标后, 直接给出 final answer (不要再调用工具)。"""


def _build_ref(mode: str) -> Any:
    from kun.agents.director.anchor import GoalAnchor
    from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec

    if mode == "smoke":
        goal_detail = (
            "环境校验任务 (不要写任何代码)。你在工作区 ~/Desktop/SparkWorld 里, "
            "shell-exec 的 cwd 默认就是这里, file-io 的 path 相对这里。三步: "
            "(1) shell-exec 跑 `node -v && npm -v && ls -la` 看环境和现有文件; "
            "(2) file-io read `产品执行方案.md` (这是项目 GoalAnchor) 了解目标; "
            "(3) 一句话 final answer: 总结项目现状 + 你认为 M1 下一步该做什么。"
        )
        goal_statement = "校验 鲲 能在 SparkWorld 工作区用真 LLM + shell/file 工具跑通"
        criteria = ["shell-exec 真跑出 node 版本和文件列表", "读到 产品执行方案.md", "给出现状总结"]
    elif mode == "plan":
        goal_detail = (
            "把火花世界的产品方案做到极致 —— 这一轮**只做方案, 不写任何游戏代码**。"
            "输入材料都在工作区里: docs/source/ 下有 3 份原始产品思考 "
            "(火火兔的火花🔥 / 火花新进展 / 火火兔商务思考), 产品执行方案.md 是一份很薄的初稿。\n"
            "步骤: (1) file-io 逐份读 docs/source/ 三份 + 产品执行方案.md, 吃透愿景与约束; "
            "(2) web-search 深度调研外部参考并记录来源: 儿童 AI 安全/COPPA、AI 世界导演/生成式交互、"
            "端侧 STT + 小模型成本架构、Toca Boca / Sago Mini / Khanmigo / Roblox 等前沿儿童产品打法、"
            "相关论文与可复用开源引擎; (3) 改代码/断言前一律 grep-verify, 不靠记忆; "
            "(4) 综合写一份 CTO / 产品总监级的完整方案到 docs/产品方案-vNext.md, 必须覆盖 8 大块: "
            "①完整世界架构 (10 世界 × 3 年龄段重返机制) ②AI 世界导演设计 (三层 AI + 安全转译 + 高容错通感) "
            "③Spark 成长系统 ④商业模式 (Spark Pass) ⑤技术架构 (端侧 STT+LLM + 成本/缓存/沙箱) "
            "⑥分阶段开发路线图 ⑦风险与监管 ⑧验收标准。"
            "要求: 深、具体、可执行, 不要泛泛而谈; 每节先想清楚再落笔; 标注外部来源。"
            "完成后 final answer 给出方案大纲 + 文件路径 + 关键决策摘要。"
        )
        goal_statement = "火花世界产品方案做到极致 (CTO/PM 级完整 spec)"
        criteria = [
            "读全 docs/source/ 3 份源文档",
            "web-search 真调研外部参考并标来源",
            "产出 docs/产品方案-vNext.md, 8 大块全覆盖、深且可执行",
        ]
    else:  # m1
        goal_detail = (
            "在工作区 ~/Desktop/SparkWorld 里推进火花世界 M1 (颜色岛 MVP)。"
            "工作区已有脚手架: src/ (Phaser 颜色岛场景/小火花/导演客户端) + server/ "
            "(导演服务) + public/。**尽量复用现有代码, 别从零重写**; 目标是让 demo 真的能在"
            "浏览器打开、颜色岛核心环 (点彩虹树→拖色种→湖变色) 可玩。"
            "先 file-io read `产品执行方案.md` 看 GoalAnchor 和硬不变量 I1-I6, "
            "再 shell-exec `ls -R src public server` + 读关键文件看已有代码, grep-verify 关键实现。"
            "然后: 用 `npm install` 确保依赖在, `npm start` 起服务并 curl http://localhost:8787/health 验证, "
            "修掉任何让 demo 跑不起来的问题。每改一处都 shell-exec 验证。"
            "完成后 final answer 报告: 改了什么 + demo 是否能跑起来 + 验证证据。"
            " 重要: 起长驻服务 (npm start) 必须后台跑, 不要前台阻塞 shell-exec —— 用 "
            "`nohup npm start > logs/server.log 2>&1 & sleep 3` 然后 curl, 跑完用 "
            "`pkill -f 'node server' || true` 收尾。"
        )
        goal_statement = "火花世界 M1 颜色岛 MVP 能在浏览器跑起来"
        criteria = ["npm start 起服务成功", "/health 返回 ok", "颜色岛核心环可玩"]

    owner = Owner(tenant_id="spark-world", user_id="founder")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint(goal_statement, owner),
        task_type="coding.javascript.game",
        risk_level="low",
        complexity="complex" if mode == "m1" else "simple",
        estimated_steps=40 if mode == "m1" else 4,
        estimated_duration_sec=1800.0 if mode == "m1" else 60.0,
        owner=owner,
        success_criteria_short=goal_statement,
    )
    spec = TaskSpec(goal_detail=goal_detail, success_metrics=criteria, subtasks_hint=[])
    ref = TaskRef(meta=meta, spec=spec)
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement=goal_statement,
        success_criteria=criteria,
        out_of_scope=["Unity 移植", "世界 2-10", "真实支付", "首版不做账号系统"],
        invariants=[
            "I1 无失败态: 永不出现 game over / 次数用完 / 你错了",
            "I2 小火花永不说听不懂: 任何输入都转成正向世界反馈",
            "I3 危险表达走安全转译 (爆炸→烟花)",
            "I6 AI 是世界导演不是聊天框",
        ],
    )
    return ref


class _StubSupervisor:
    async def analyze_observation(self, **_kw: Any) -> Any:
        class _Obs:
            verdict = "ok"
            rationale = "supervisor stub (Claude supervises at event level)"
            recommended_action = None

        return _Obs()


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    max_steps_override = int(sys.argv[2]) if len(sys.argv) > 2 else None
    _setup_env()

    # Register builtin skill executors (shell-exec/file-io/...) — they
    # self-register on import; a standalone runner must trigger the autoload
    # or 鲲's tool calls dispatch to "unknown skill".
    from kun.skills.dispatcher import autoload_builtins

    autoload_builtins()

    from kun.engineering.long_task_orchestrator import LongTaskOrchestrator
    from kun.engineering.long_task_runtime_bundle import LongTaskRuntimeBundle
    from kun.integration.llm_invoker import make_llm_invoker
    from kun.integration.tool_executor import make_tool_executor
    from kun.interface.llm.base import TaskProfile
    from kun.interface.llm.router import get_router

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "logs").mkdir(exist_ok=True)
    log_path = WORKSPACE / "logs" / f"kun-run-{datetime.now(UTC):%Y%m%d-%H%M%S}-{mode}.jsonl"

    print(f"{'=' * 72}\n  X.J — 鲲 executes Spark World  (mode={mode})\n{'=' * 72}")
    print(f"  workspace : {WORKSPACE}")
    print(f"  event log : {log_path}")

    router = get_router()
    profile = TaskProfile(task_type="coding.javascript.game", risk_level="low", needs_reasoning=True)
    invoker = make_llm_invoker(
        router,
        purpose="execution",
        profile=profile,
        temperature=0.4,
        max_tokens=8192 if mode in ("m1", "plan") else 1024,
    )

    class _Store:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def writer(self, row: dict[str, Any]) -> None:
            self.rows.append(dict(row))

        async def reader(self, _t: str, _tk: str) -> Any:
            return None

        async def marker(self, _t: str, _cp: str, _st: Any) -> None:
            return None

    store = _Store()
    bundle = LongTaskRuntimeBundle.from_env_defaults(
        llm_router=router, external_supervisor=_StubSupervisor()
    )
    print("  mechanisms:", dict(sorted(bundle.enabled_flags.items())))

    orch = LongTaskOrchestrator(
        llm_invoker=invoker,
        tool_executor=make_tool_executor(),
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=_StubSupervisor(),
        enable_recursive_planner=False,
        max_steps=max_steps_override or {"smoke": 4, "plan": 80}.get(mode, 60),
        # KUN runs on CLI + local models (OAuth/local) — cost is not metered per
        # token, so we don't gate on budget (user). max_steps is the runaway guard.
        max_budget_usd=1000.0,
        **bundle.as_orchestrator_kwargs(),
    )

    events: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    async def _sink(ev: Any) -> None:
        rec = {"t": round(time.perf_counter() - t0, 1), "kind": ev.kind, "data": dict(ev.data)}
        events.append(rec)
        with log_path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        d = rec["data"]
        brief = {k: d[k] for k in ("step", "tool", "tool_name", "status", "overall_score", "call_count") if k in d}
        print(f"  [{rec['t']:>6.1f}s] {ev.kind}  {brief if brief else ''}")
        if ev.kind in ("long_task.step", "long_task.tool_result") and "content" in d:
            print(f"            ↳ {str(d['content'])[:160]}")

    ref = _build_ref(mode)
    print(f"\n  goal: {ref.spec.goal_detail[:120]}...\n{'-' * 72}")
    outcome = await orch.run_long_task(ref, on_event=_sink, extra_system_segments=[SKILL_DIRECTIVE])

    dt = time.perf_counter() - t0
    lr = outcome.loop_result
    print(f"\n{'=' * 72}\n  RUN SUMMARY\n{'=' * 72}")
    print(f"  status        : {lr.status}")
    print(f"  steps taken   : {getattr(lr, 'steps_taken', '?')}")
    print(f"  events        : {outcome.events_emitted}")
    print(f"  cost (USD)    : {getattr(lr, 'total_cost_usd', 0.0):.4f}")
    print(f"  wallclock     : {dt:.1f}s")
    print(f"  final answer  :\n{'-' * 72}\n{(lr.final_text or '')[:1200]}\n{'-' * 72}")
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"  event kinds   : {kinds}")
    print(f"  full log      : {log_path}")
    return 0 if lr.status in ("final", "done") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
