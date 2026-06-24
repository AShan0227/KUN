# 怎么试一下：让 KUN 自己进化给你看

> 本文档是 Phase 1 (L0-L5) 完成后的**验收测试手册**。目标：让你 30 分钟内亲眼看到
> 鲲在做 self-improvement，而不是只看到一堆单测通过。

---

## 前置 (5 分钟)

```bash
# 1. 把项目跑起来
./scripts/bootstrap.sh   # 起 docker-compose 10 容器 + 跑单测

# 2. 确认 1160 个单测全绿
uv run pytest tests/unit -p no:warnings 2>&1 | tail -3
# 期望: 1160 passed
```

如果 bootstrap 失败，先看 [`docs/DEPLOY.md`](./DEPLOY.md) 排查。

---

## 试验 1 · 看 Director 给一个长任务 pinned anchor（5 分钟）

**这试什么**：长任务模式下，KUN 会把 GoalAnchor pin 到 system prompt 顶部，并加 anti-sycophancy 段。

```bash
uv run python -c "
from kun.agents.director.anchor import GoalAnchor

anchor = GoalAnchor(
    task_id='t-demo-1',
    goal_statement='把 kun/agents/executor 拆成 5 个独立模块, 不破坏现有调用',
    success_criteria=[
        'tests/unit 全绿',
        '5 个独立子模块, 每个 <300 行',
        '现有 import kun.agents.executor.xxx 全部不变',
    ],
    invariants=['不动 data 脊柱表 schema', '不影响监督线接入'],
    out_of_scope=['顺手重写 Director', '改 task 入口契约'],
)
print('=== Goal Anchor (会被 pin 到 prompt 顶部) ===')
print(anchor.render_for_system_prompt())
"
```

**期望看到**：
- ` ═══ GOAL ANCHOR (immutable, pinned) ═══` 视觉边界
- `goal_statement` / `success_criteria` / `invariants` / `out_of_scope` 四块
- 顶部明确标 immutable, do not override

---

## 试验 2 · 看 Supervisor 检测异常 + 写 strategy_search_request（5 分钟）

**这试什么**：喂 4 类异常事件，看 Supervisor 阈值检测 + emitter 真发出 search_request。

```bash
uv run python -c "
import asyncio
from kun.agents.supervisor.service import SupervisorService

emitted = []
async def fake_emitter(req):
    emitted.append(req)

async def main():
    svc = SupervisorService(emitter=fake_emitter)
    # 喂 3 次同一 task_type 失败
    for _ in range(3):
        await svc.observe('task.failed', {'tenant_id': 'demo', 'task_type': 'coding.refactor'})
    # 喂 1 次 task.done 高 duration ratio → duration_outlier
    await svc.observe('task.done', {
        'tenant_id': 'demo',
        'task_type': 'coding.refactor',
        'duration_sec': 30.0,
        'avg_duration_sec_for_task_type': 3.0,
    })
    print(f'触发了 {len(emitted)} 个 strategy_search_request')
    for r in emitted:
        print(f'  - {r[\"anomaly_kind\"]} on {r[\"target_module\"]} → priority={r[\"priority\"]} severity={r[\"severity\"]} triggered_by={r[\"triggered_by\"]}')

asyncio.run(main())
"
```

**期望看到**：
- 至少 2 个 `anomaly_threshold` 单异常 request (task_failure_spike + duration_outlier)
- 至少 1 个 `anomaly_cluster` 系统性 request (module_systemic) — 同 target_module 多 anomaly_kind 触发的聚类

---

## 试验 3 · 看 Strategist 提候选 + Explorer Pool 三模式（5 分钟）

**这试什么**：anomaly request 喂给 Strategist，看它产 Conservative/Aggressive/Performance 3 个候选。

```bash
uv run python -c "
import asyncio
from kun.agents.strategist.service import StrategistService

async def main():
    svc = StrategistService()
    candidates = await svc.propose_candidates({
        'anomaly_kind': 'llm_fallback_spike',
        'target_module': 'llm.router',
        'evidence': [{'fallback_provider': 'openai', 'primary_provider': 'anthropic'}],
    })
    print(f'产了 {len(candidates)} 个 candidates:')
    for c in candidates:
        print(f'  - [{c.explorer_mode}] target_level={c.target_level} sampling={c.sampling_rate} rollout={c.rollout_mode}')
        print(f'    change_spec: {c.change_spec}')
        print(f'    rationale: {c.rationale}')

asyncio.run(main())
"
```

**期望看到**：
- 3 个 candidates，explorer_mode 分别 conservative / aggressive / performance
- 各自不同 sampling_rate (0.3 / 0.5 / 1.0) 和 rollout_mode (canary / canary / shadow)
- 每个有 rationale 解释为什么这么改

---

## 试验 4 · 看 Forward / Backward 自动决策（5 分钟）

**这试什么**：如果 24h 内有 enabled capability 命中 target_module，Strategist 选 backward rollback 而非 forward 新探索。

```bash
uv run python -c "
import asyncio
from datetime import UTC, datetime, timedelta
from kun.agents.strategist.service import StrategistService

async def main():
    cap = {
        'capability_id': 'cp-recent',
        'promoted_at': datetime.now(UTC) - timedelta(hours=2),
        'enabled': True,
        'change_summary': 'tier upgrade triggered 2h ago',
    }
    async def history_reader(target_module): return [cap]

    svc = StrategistService(capability_history_reader=history_reader)
    candidates = await svc.propose_candidates({
        'anomaly_kind': 'llm_fallback_spike',
        'target_module': 'llm.router',
        'evidence': [{'fallback_provider': 'openai'}],
    })
    print(f'Decision: 选了 {len(candidates)} 个 candidate(s)')
    for c in candidates:
        print(f'  [{c.explorer_mode}] {c.change_spec[\"kind\"]} — rollout_mode={c.rollout_mode}')

asyncio.run(main())
"
```

**期望看到**：1 个 `backward` candidate, `change_spec.kind == capability_rollback`, `rollout_mode=direct`。
**对比试验 3**（无 history）：3 个 forward candidates。

---

## 试验 5 · 看 Gate 4 条规则准入门禁（5 分钟）

**这试什么**：Gate.admit 拒绝低 pass_rate / alarming debrief / 自指 target，写一行 capability row。

```bash
uv run python -c "
import asyncio
from kun.agents.gate.service import GateService

async def main():
    written = []
    async def writer(payload): written.append(payload)
    svc = GateService(capability_writer=writer)

    # Case 1: 正常 approve
    d1 = await svc.admit(
        {'experiment_id': 'er-1', 'target_module': 'llm.router', 'change_spec': {'kind': 'tier_upgrade'}, 'rationale': 'normal'},
        test_report={'pass_rate': 0.95, 'passed_count': 19, 'total_count': 20},
        debrief={'verdict': 'ok', 'evidence_quality_score': 0.8},
    )
    print(f'Case 1 (normal): {d1.verdict} — promotion_state={d1.promotion_state}')

    # Case 2: 低 pass_rate → reject
    d2 = await svc.admit(
        {'experiment_id': 'er-2', 'target_module': 'llm.router', 'change_spec': {'kind': 'x'}},
        test_report={'pass_rate': 0.6, 'passed_count': 6, 'total_count': 10},
    )
    print(f'Case 2 (low pass_rate): {d2.verdict} — {[r for r in d2.reasons if \"pass_rate\" in r]}')

    # Case 3: 自指 target_module → awaiting_human_review
    d3 = await svc.admit(
        {'experiment_id': 'er-3', 'target_module': 'strategist.service', 'change_spec': {'kind': 'x'}, 'rationale': 'self-mod'},
        test_report={'pass_rate': 0.95, 'passed_count': 19, 'total_count': 20},
        debrief={'verdict': 'ok', 'evidence_quality_score': 0.8},
    )
    print(f'Case 3 (self-referential): {d3.verdict} — writes row? {d3.capability_row_payload is not None}')
    if d3.capability_row_payload:
        print(f'  metadata.promotion_block_self_referential={d3.capability_row_payload[\"metadata\"].get(\"promotion_block_self_referential\")}')

    print(f'\\n总共写了 {len(written)} 行 capability row.')

asyncio.run(main())
"
```

**期望看到**：
- Case 1 approve, promotion_state=merged
- Case 2 reject, reason 含 `test_pass_rate=0.60<0.9`
- Case 3 awaiting_human_review, 仍写一行 row, metadata 标 `promotion_block_self_referential=True`

---

## 试验 6 · 端到端 RSI 闭环（10 分钟，真接 Postgres）

**这试什么**：从异常事件流入开始，看 Supervisor + Strategist + Gate 三件套真把 capability 写进 `runtime_capabilities` 表。

> 这试验**真接 docker-compose 起的 Postgres**，而不是 fake emitter。

```bash
# 0. 确认 docker-compose 起着
docker compose -f docker-compose.dev.yml ps | grep postgres
# 期望: kun-dev-postgres-1 ... Up

# 1. 跑端到端脚本
uv run python scripts/e2e_rsi_demo.py
```

**期望看到**（实测 2026-05-27 已通过）：
- Supervisor 检测到 3 次连续 llm.fallback.triggered + 1 次 duration_outlier → 写 2 个 strategy_search_request 到真 Postgres
- Strategist 产 3 个 candidates (Explorer Pool 3 模式)
- Gate 全部 approve → 写 3 行 runtime_capabilities (sampling 0.3/0.5/1.0, state=merged)
- Promotion queue sweeper 跑一次 → 0 expired / 0 stale (因为刚写入)

**直接查表确认**（注意：RLS 默认隐藏，psql 要用 superuser `kun` 而不是 `kun_app`）：
```bash
docker exec kun-dev-postgres-1 psql -U kun -d kun -c "SELECT request_id, target_module, priority, status FROM strategy_search_requests WHERE tenant_id='u-e2e-demo';"

docker exec kun-dev-postgres-1 psql -U kun -d kun -c "SELECT capability_id, target_module, promotion_state, enabled, sampling_rate FROM runtime_capabilities WHERE tenant_id='u-e2e-demo';"
```

**预期输出**：
```
strategy_search_requests: 2 rows (anthropic / executor.coding.refactor)
runtime_capabilities:     3 rows (llm.router × 3, merged, enabled=f)
```

**清理**: demo 脚本启动时自动 cleanup 上次 demo 数据, 重复跑无残留.

---

## 7 个其他验证点（按需）

- **方法论蒸馏**: `uv run python -c "from kun.engineering.methodology_distill import distill; r = distill(); print(f'novel: {len(r.novel_candidates)}, sources: {len(r.sources)}')"` — 应该看到 ~192 个 novel candidates from 11 文件
- **Input Classifier**: `uv run python -c "from kun.agents.director.input_classifier import classify_input; print(classify_input('stop now').category)"` — 应该输出 `interrupt`
- **RCDH 4 级诊断**: `uv run python -c "import asyncio; from kun.governance.rcdh import run_diagnostic; r = asyncio.run(run_diagnostic('feature flag disabled in llm.router', triggered_by_event_id='e1')); print(f'root_cause_level={r.root_cause_level}, action={r.recommended_action}')"` — 应该是 L1 activate
- **Anomaly Cluster**: 试验 2 已经覆盖 (`anomaly_cluster` request)
- **Priority Channel**: `uv run python -c "from kun.governance.priority_channel import classify_request_priority; print(classify_request_priority({'triggered_by': 'anomaly_cluster'}).tier)"` — 应该是 `urgent`
- **Exploration Penalty**: 看测试 `tests/unit/test_l4_exploration_penalty.py::test_strategist_filters_via_penalty`
- **External Supervisor 自嗨检测**: 看测试 `tests/unit/test_external_supervisor_modes.py::test_self_aggrandizement_calls_llm_when_signal_hit`

---

## 如果某步骤失败

1. 先看 docker-compose 是否真起来：`docker compose -f docker-compose.dev.yml ps`
2. 看 alembic 是否跑过：`uv run alembic current` (应该是 `0011` 或更新)
3. 看 .env 配置：`cat .env | grep KUN_PG`
4. 查 KUN log：`docker compose -f docker-compose.dev.yml logs postgres | tail -20`

跑通 6 个试验 = Phase 1 真接通，可以进 Phase 2 商业化。

---

*最后更新：2026-05-27*
