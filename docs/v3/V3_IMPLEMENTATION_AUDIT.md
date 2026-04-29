# KUN V3 实装审计

这份文档只回答一个问题：V3 的能力到底有没有进入主流程。

## V3-1 守望决策层

- 调用方：`Orchestrator.stream()` 在 planning 前调用 `WatchtowerDecisionPlane.decide()`。
- 影响决策：修改 `execution_mode`、`context_limit`、`required_skills`、metric dimensions。
- 消费者：planner、context packer、state ledger、scorecard。
- 测试：`tests/unit/test_watchtower_decision_plane.py`。
- 诚实边界：策略包目前是规则版，不是 LLM 自动学习版。

## V3-2 State Ledger

- 调用方：`Orchestrator.stream()` 在 task created / decision / plan / running / step / pause / finish 写入。
- 影响决策：不直接改执行，只提供同一份当前状态给人和 LLM。
- 消费者：`/api/blackboard/state`、`/api/blackboard/state-ledger`、`/api/blackboard/full/{task_id}`、前端首页。
- 测试：`tests/unit/test_state_ledger.py`、`tests/unit/test_wave7.py`。
- 诚实边界：当前是热视图，不替代持久 DB 账本。

## V3-3 Hermes 全链路

- 调用方：`Orchestrator._execute_step()`、`run_agent_loop()`。
- 影响决策：LLM 看到 Hermes 结构化任务包；skill 输入输出经过 Hermes 适配。
- 消费者：LLM 执行 prompt、skill dispatcher、adapter registry。
- 测试：`tests/unit/test_hermes_full_chain_adapter.py`。
- 诚实边界：Hermes 不执行真实外部动作，真实动作归 World Gateway。

## V3-4 三层记忆写回

- 调用方：`Orchestrator.stream()` 在 watchtower decision、step completed、task finalization 调用 `MemoryWriteback`。
- 影响决策：写入 Context AssetStore，后续 ContextPacker 可检索。
- 消费者：`ContextPacker`。
- 测试：`tests/unit/test_v3_memory_scoring_gateway.py`。
- 诚实边界：第一版写入 AssetStore；长期持久化、蒸馏、遗忘还没全部做完。

## V3-5 World Gateway

- 调用方：`execute_approved_action_once()`。
- 影响决策：所有已审批 side-effect action 先进入 World Gateway，生成审计包。
- 消费者：pending action executor、NUO action panel。
- 测试：`tests/unit/test_v3_memory_scoring_gateway.py`、`tests/unit/test_action_executor.py`。
- 诚实边界：没有真实外部 handler 时不会假装已外发，payload 里标 `external_dispatched=false`。

## V3-6 统一评分系统

- 调用方：`Orchestrator.stream()` finalization。
- 影响决策：scorecard 进入事件、WebSocket side channel，并作为 capability writeback 的 rubric 来源。
- 消费者：capability card、NUO/前端 side channel、记忆写回。
- 测试：`tests/unit/test_v3_memory_scoring_gateway.py`。
- 诚实边界：第一版是确定性评分；用户满意度先用中性默认值，后续要接真实反馈。

## V3-7 主交互入口

- 调用方：`frontend/src/app/page.tsx`。
- 影响决策：不直接改后端决策，但让用户看到任务状态、成本、风险、待确认。
- 消费者：用户。
- 测试：前端 typecheck / lint。
- 诚实边界：节点图、高级拖拽编辑没有放到主入口。

## V3-8 伪功能审计

- 调用方：开发流程。
- 影响决策：所有 V3 后续模块都必须填写“调用方、影响、消费者、测试、边界”。
- 消费者：开发者、reviewer、后续 Claude/Codex 审查。
- 测试：本文件本身不是自动化测试；它是 review 门禁。
- 诚实边界：静态自动扫描还没做，先用人工审计清单防止继续堆空壳。

## V3-9 能力边界账本

- 调用方：`/nuo/health/summary`、`/nuo/health/delivery-status`。
- 影响决策：不直接执行任务；它把“可测 / 半闭环 / 仅审计 / 未就绪”变成产品可见状态，避免把审计网关说成真实执行器。
- 消费者：NUO 管家页、用户、开发者、reviewer。
- 测试：`tests/unit/test_delivery_status.py`、`tests/integration/test_api_routes_boot.py`。
- 诚实边界：这是当前人工维护的能力账本；后续要让 PROMISES / git / runtime telemetry 自动生成或校验它。
