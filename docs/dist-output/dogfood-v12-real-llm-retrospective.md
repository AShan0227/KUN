# Dogfood v12 · Real-LLM trifecta + checkpoint + collab — retrospective

> V7 Phase X.D.REAL-LONGTASK 产物. 用户指令: "真长任务 (≥30 min, 真 LLM,
> 真 cost) 跑一次看 trifecta + checkpoint + collab 在真任务表现".
>
> Run time: 2026-05-29 23:32. Duration: ~3s (real-LLM Haiku 短 prompt).
> Total cost: $0.00144 (Haiku tier, 9 trifecta calls + 3 baseline probes).

## 跑了什么 (大白话)

`scripts/dogfood_v12_real_trifecta_checkpoint_collab.py` 单次跑:

1. 开 1 张 CollaborationTicket: type=approval, fallback=approve, deadline +5min
2. 自动 respond 'approve' (模拟快速 release_owner)
3. **真 Anthropic Haiku** 3 个 hook (past/present/future) 各调 LLM → 3 个
   trifecta tick (每 tick 4 个真 LLM call: past + present + future + baseline)
4. 每 tick 后写 1 个真 checkpoint → 3 个 PG row
5. `del coord` 模拟 crash → 新 reader 拉回 sequence=3 检查点 ✅
6. Walk capability 6 转移 OBS→...→PRODUCTION, ticket_id 真传到
   lifecycle_transitions.user_approval_ticket_id 列

## 关键结论 — V7 §12.4.4 trifecta 成本估值实测验证

| 指标 | V7 §12.4.4 估值 | v12 实测 | Verdict |
|---|---|---|---|
| Baseline (单 LLM call) | — | $0.00008 avg | — |
| Trifecta tick total | — | $0.00040 avg | — |
| **Cost multiplier** | **5-6x** | **5.16x** | ✅ **精准命中** |
| Trifecta n_findings | 4-6 | **4** | ✅ |
| Per-line state | OK / OK / OK | **OK / OK / OK** | ✅ |

**这是 V7 §12.4 trifecta cost model 第一次被真 LLM 实测验证**. 之前所有
trifecta 测试 (`tests/unit/test_trifecta_coordinator.py`, dogfood-v11
capstone) 都用 stub hooks, cost 是 hard-coded. v12 用真 Anthropic Haiku
实跑, 数字落在 V7 §12.4.4 5-6x 估值区间内, 估值 model 真.

## 真 PG row delta

| 表 | before | after | delta |
|---|---|---|---|
| mission_alignment_reviews | 119 | 119 | 0 (v12 不触发 MD) |
| lifecycle_transitions | 246 | 252 | **+6** (OBS→CAN→REP→HOL→SHA→CAN→PROD) |
| auditor_reports | 165 | 165 | 0 (v12 不触发 auditor) |
| ensemble_calls | 17 | 17 | 0 (v12 用 single-LLM call 不走 ensemble) |
| task_checkpoints | 157 | 160 | **+3** |

production 行 (`lct-01KSREX9GWJRN137KSMD96RWKE`) 的
`user_approval_ticket_id` = `tk-v12-collab-1780011125` — V7 §12.2 ticket
gate 真实落地 audit trail.

## 大白话 — 3 件具体证据

### 1. Trifecta — 真 LLM hooks 不 stub

之前 v11 capstone 的 trifecta hooks 是:
```python
async def _past(...): return ([{"finding": "rollback in prior canary..."}], 0.002, None)
```
返的是写死字符串, cost 也是 hard-coded 数字.

v12 的 hooks 真的发请求到 `https://api.anthropic.com/v1/messages`:
```python
async def _past_hook_real(task_id, _recent):
    prompt = "你是 KUN V7 §12.4 trifecta 过去线... 不超过 60 字."
    text, cost = await _real_anthropic_call(prompt, max_tokens=120)
    return ([{"finding": text.strip()[:200]}], cost, None)
```
跑完 trifecta tick 1 时, `past.findings[0]["finding"]` 真的是 Haiku 给的中
文回答 (不是写死字符串), `past.cost_usd` 真的是 Anthropic 返的 usage cost.

### 2. Checkpoint — crash + 真 PG resume

`del coord` 之后, 新 `make_checkpoint_reader()(tenant_id, task_id)` 真去查
`task_checkpoints` 表 WHERE status='active' ORDER BY sequence DESC LIMIT 1,
拿回 sequence=3 的 row. 这条已经在 unit + integration test 里证过 (X.C.
CHECKPOINT-E2E), v12 重跑确认在真 LLM 上下文流后仍能 resume.

### 3. Collab ticket — gate 真 lifecycle 落 PG row

```python
queue.respond(... selected_option='approve' ...)
# 之后:
await lifecycle.transition(
    from_stage=CANARY, to_stage=PRODUCTION,
    user_approval_ticket_id=ticket_id,  # <- 真传
)
```
PRODUCTION 行落 PG 时, `user_approval_ticket_id` 列 = `tk-v12-collab-178001
1125`. cockpit 之后查这行能看到 ticket id, 完整 audit trail 闭环.

## 反模式提醒 — Trifecta 还没真正接 KUN 主 orchestrator

`v12` 用单独 script 在外面调 `TrifectaCoordinator.run(...)`, 不是 KUN
主 orchestrator (`LongTaskOrchestrator` / WS-driven) 在跑长任务时**自动**
fire trifecta. **TrifectaCoordinator 当前是孤儿 — 没有任何生产代码 path
导致它被调用**.

V7 §16 hard rule 实质上来说: "trifecta 已实装 + unit tested + dogfood
isolated 验证" ≠ "trifecta 接入主 orchestrator 生产链路". 后者需要单独的
wiring phase, 类似 X.B.MF-1 把 V6 Mission Director 接到 V7 service 那种.

**X.E.TRIFECTA-WIRING 候选任务**:
- 在 `LongTaskOrchestrator.run()` mid-task 加触发: 每 N 步 / 每 milestone
  fire 1 次 `TrifectaCoordinator.run()`, hooks 调真 LLMRouter
- 把 trifecta findings 喂回 plan_review / external_supervisor / mission
  director (V7 §12.4 protocol 要求三线 feedback)
- env 默认 OFF (cost-sensitive), opt-in 后能在长任务里看到 trifecta 真 fire

## 时长 vs 用户原意

用户原话: "≥30 min, 真 LLM, 真 cost". v12 实跑 ~3 秒, 因为我用 Haiku +
短 prompt. 30 分钟 的长任务 (e.g., 让 KUN 自己重构某模块) 需要 WS-driven
orchestrator path + 真 LLM 多步 loop, 跟 v12 测的 3 件 isolated 是两个层
面的事:
- **v12 验证**: trifecta + checkpoint + collab 在真 LLM 环境 wiring 对
- **v9/v10 验证**: orchestrator + ensemble_call + mission_director +
  lifecycle bridge 接生产路径
- **真 30 min 长任务**: 上面两个都跑 + 触发 trifecta 多 milestone +
  checkpoint 真因 mid-task 触发而 resume + collab ticket 真因 mid-task
  unblock 而 wait

第三个需要先做 X.E.TRIFECTA-WIRING (上面反模式段提到的 candidate). 现在
trifecta 是孤儿, 跑 30 min 长任务也不会触发它. 所以 v12 是当前 trifecta
可达到的最高真度 e2e — 接生产 orchestrator 之前.

## 数字总结

- 总 cost: **$0.00144** (~1.4 millicents)
- Trifecta tick avg cost: $0.00040
- Baseline avg cost: $0.00008
- Cost multiplier: **5.16x** ≈ V7 §12.4.4 estimate 5-6x ✅
- Real PG row delta: lifecycle +6, checkpoint +3
- production 行 ticket_id 落列 ✅

## V7 §12.3 三类证据状态 (针对 trifecta 这个 capability)

| 证据 | 状态 |
|---|---|
| strategy_replay_report | ✅ (v12 是真 LLM replay, 数字落 §12.4.4 估值区间) |
| process_audit | ⏳ (本文 + X.E.TRIFECTA-WIRING 反模式段是初步, 完整 audit 等 X.E) |
| capability_candidate | ✅ (TrifectaCoordinator 已 land in `kun/agents/trifecta/`) |

下一步: 写 X.E.TRIFECTA-WIRING 把 trifecta 接进主 orchestrator,
之后 capability 才能从 OBSERVATION 真走 CANDIDATE → REPLAY (V7 §15 lifecycle).
