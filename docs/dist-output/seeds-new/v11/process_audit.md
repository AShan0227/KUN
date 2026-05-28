# V7 Phase X.C v11 capstone — 3 张 seed 的 process audit

> V7 §12.3 强制证据之二 (capability_candidate 进 Replay 必须的三类证据之一).
>
> 本 audit 不评估 seed 内容好坏 (那是 strategy_replay_report 的事), 而是回答:
> **为什么 KUN 之前没有这些工程方法论, 这反映了 KUN 哪里的工程缺口**。
> 决议每张 seed: MERGE / KNOWN_LIMIT / DEFER。

---

## 3 张 v11 seed 反映的工程缺口

### 缺口 1: 把"接生产链路"的验收标准从隐性升成显性

**candidate 涉及**: `production_loop_real_pg_e2e_chain.yaml`

**原链路问题**:

- V7 §16 明文写"凡是不能进入真实生产链路的功能, 都不算完成", 但**完成
  与否的判定标准从未具象化为可机器复制的 test pattern**
- X.B 早期我用 fake `_CaptureSession` 跑测试全过, 写"接到 X / production-ready",
  但实际生产路径有 3 个 wiring 错没暴露 (cross-loop asyncpg / 漏 mount
  cockpit router / ensemble enum mismatch)
- "接生产"这条 hard rule 之前是 process / culture 层面, 不是 test 层面.
  没有自动可执行的 acceptance bar, 完成度判断主观, claim 容易 oversell

**audit 结论**: 这是 KUN **协议级 hard rule 缺机器复制的 acceptance test 落地**.
seed 把这条 rule 具象化为"1 个 e2e test 串 N 个真子系统真 PG 落行+读回",
任何 wiring 错都炸在 read-back assert. 这是 V7 §16 hard rule **从声明升级
成 acceptance bar** 的工程意义.

**MERGE 推荐**: ✅ 通过, 直接合 `seeds/methodologies/`.

---

### 缺口 2: 协议级 invariant 只在 service 层 enforce, DB 层不兜底

**candidate 涉及**: `service_layer_invariant_plus_db_check_belt_and_suspenders.yaml`

**原链路问题**:

- V7 §12.2 (production flip 必须 user_approval) / §12.3 (replay 必须三证据)
  / §16.6 (P0 必不 release) 是协议级 hard rule
- 但早期实装时**只在 service 层 raise**, DB CHECK 没加 — caller 绕过
  service (admin script / 测试 fixture / 未来新 service) 就静默漏
- X.B.MF-5 的 13 个真 PG CHECK violation 测试是事后补的 — 揭示了 4 张表
  之前的 CHECK constraint **从没在真 PG 跑过** (alembic 写了, 但 fake
  session 永远不会触发, 谁知道 CHECK syntax 对不对)
- 反模式被 `dogfood-v11` 的 `consensus_strategy='majority'` vs DB enum
  `'majority_vote'` 不匹配抓住 — service 写一种, DB CHECK 写另一种, 任一
  side 漂都失效

**audit 结论**: 这是 KUN **协议级不变量"双源真理"同步缺工程纪律**. 应该:
(1) service 层 raise 业务 Error (caller 早收反馈);
(2) DB CHECK 兜底 (admin script / 其他 service 绕过 service 仍兜得住);
(3) 测试 2 套都覆盖 — fake session 测 service raise, 真 PG 测 DB CHECK syntax.

**MERGE 推荐**: ✅ 通过, 直接合 `seeds/methodologies/`.

---

### 缺口 3: 把"人审"从 bool flag 升成 V7 §11 标准 IO

**candidate 涉及**: `human_in_loop_gate_via_collab_ticket.yaml`

**原链路问题**:

- V7 §11 给了 CollaborationTicket 标准 IO (有 deadline + fallback_policy +
  escalation + responder + idempotent terminal status), 但**实装时几次
  都用 `approved: bool` 简化**
- bool flag 路径丢了: SLA fallback / 责任人记录 / audit trail / replay
  attack 防护. dogfood v9 跑长任务时 trifecta 没启用 (env 默认 OFF), 一个
  原因就是没人审过它的 cost — 但人审没 ticket 化, 没 SLA, 推不动
- 真实生产链路里的"决策点" (V7 §10.3.3 三档) 在 X.B 阶段是 KUN 内部
  classifier 判, 缺**agent 自己主动 raise 人审 + ticket gate 下游 action**
  的标准路径

**audit 结论**: 这是 KUN **协议设计与实装精度不对齐**. V7 §11 设计已经把
人审做成 first-class CollaborationTicket, 但实装时常省略走 ticket. seed
明示 "user_approval_ticket_id 必须落到下游 service 的列 (lifecycle_transitions.
user_approval_ticket_id) 做 audit trail", 把协议 IO 强制落地成代码契约.

**MERGE 推荐**: ✅ 通过, 直接合 `seeds/methodologies/`.

---

## 整体 audit 结论

3 张 seed 都是**真实 dogfood-v11 capstone 工作中现场抽出**, 不是事后想
象的工程指南:

| seed | 反映 KUN 哪种 gap | 证据强度 |
|---|---|---|
| `production_loop_real_pg_e2e_chain.yaml` | hard rule 缺 acceptance test 落地 | dogfood v10 找出 3 个 wiring 错 + dogfood v11 capstone 串 7 件成 |
| `service_layer_invariant_plus_db_check_belt_and_suspenders.yaml` | 协议 invariant 双源真理同步 | X.B.MF-5 13 测 + v11 中 enum 不匹配现场抓 |
| `human_in_loop_gate_via_collab_ticket.yaml` | 协议 IO 实装精度 | COLLAB-E2E 9 测 + v11 capstone 真 ticket gate prod flip 落 PG |

3 张都通过 process audit:

| seed | 决议 | 原因 |
|---|---|---|
| `production_loop_real_pg_e2e_chain.yaml` | **MERGE** | hard rule 工程落地, 适用所有未来 X.* phase |
| `service_layer_invariant_plus_db_check_belt_and_suspenders.yaml` | **MERGE** | 适用所有未来 alembic migration / 协议 invariant |
| `human_in_loop_gate_via_collab_ticket.yaml` | **MERGE** | 适用所有未来 "不可逆 action" gate |

---

## V7 工程意义

audit 暴露了 KUN 设计的 3 个真实 gap, 与 dogfood v8 process audit 抽的
gap (A-E 5 个) 高度互补:

- v8 gap 偏**协议设计盲区** (cache awareness, decision raise, todo state
  machine, silence detection, agent spawner)
- v11 gap 偏**协议实装精度** (acceptance test, invariant 同步, IO 强制落地)

两批合并起来形成 V7 §16 production-loop 闭环工程纪律的完整图. 应该写入:

- V7 §25 差异附录 (实装精度 vs 协议设计的 gap)
- `seeds/methodologies/` (3 张 v11 seed 直接合)
- 后续 phase 实施前必读 (acceptance test pattern + 双保险 + ticket-gate)
