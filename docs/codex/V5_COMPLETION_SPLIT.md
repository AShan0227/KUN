# KUN V5 Completion Split

> 目标：先把“能进入正式真实任务测试”的开发补齐，再配置真实工具并跑 dogfood。
> 本文是给另一台电脑 `slyvan` 和当前 Codex 主线的并行开发任务书。

## 0. 总原则

- 不准把没跑通的能力说成 ready。
- 不准写“看起来有功能但没有参与真实协作”的伪闭环。
- 所有判断点必须尽量进入 `DecisionTicket` / `StateLedger` / `resource_credit_stats`。
- 简单任务快跑，复杂任务深跑，高风险任务稳跑。
- 启只负责探索和实验，不直接改生产策略。
- 傩是系统管家，必须能体检 agent、context、memory、skill、WorldGateway、工程链路和外部协作。
- 真实外部动作默认人工确认，除非是明确低风险、可回滚、已配置、已审计的动作。

## 1. 开工方式

两台电脑都从当前分支开始：

```bash
git fetch origin
git checkout codex/v4-system-loop
git pull --ff-only origin codex/v4-system-loop
uv sync --extra dev
uv run --extra dev pytest -q
uv run --extra dev ruff check kun tests
uv run --extra dev mypy kun
```

`slyvan` 新开分支：

```bash
git checkout -b codex/slyvan-v5-completion
```

当前主 session 后续新开分支：

```bash
git checkout -b codex/v5-decision-evolution-closure
```

每个阶段都要：

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check kun tests
uv run --extra dev mypy kun
uv run kun ops delivery-status --json
```

## 2. 分工比例

`slyvan` 负责约 60%：执行底座、外部世界、长周期任务、编译器、代码能力。

当前主 session 负责约 40%：守望决策层、启自进化闭环、MoE 记忆策略、傩体检验收、最终整合。

## 3. SLYVAN 线：60% 任务

### S1. StateLedger 升级为长期事件账本

目标：

- 让 StateLedger 不只是当前快照，而是能从 `EventRow` 可靠回放任务/Mission/外部动作/审批/信用的长期故事。
- 支持按 `task_id`、`mission_id`、`event_type`、时间窗口查询。
- 输出用户能懂的 timeline，也输出 LLM 能读的结构化 ledger。

建议文件范围：

- `kun/core/state_ledger.py`
- `kun/api/blackboard.py`
- `kun/api/blackboard_data_sources.py`
- `kun/engineering/mission_control.py`
- `kun/datamodel/events.py`
- `tests/unit/test_state_ledger.py`
- `tests/unit/test_blackboard*.py`

验收：

- `/api/blackboard/state-ledger/{task_id}/history` 能返回长期历史。
- Mission story 能看到事件、决策票据、成本、风险、审批和恢复。
- `delivery-status` 不能再把 StateLedger 只说成“当前快照第一版”。

### S2. 长周期 Mission 自动运营闭环

目标：

- Mission 能计划、推进、失败续跑、卡住暂停、人工解锁、复盘、继续下一步。
- 普通任务 continuation 和 Mission continuation 的状态要统一展示。
- 不要求真的跨周 dogfood，但必须提供可重复的长周期压缩测试脚本。

建议文件范围：

- `kun/engineering/mission_control.py`
- `kun/engineering/mission_worker.py`
- `kun/engineering/mission_reaper.py`
- `kun/engineering/cron_scheduler.py`
- `kun/api/missions.py`
- `kun/ops/dogfood.py`
- `tests/unit/test_mission_*`
- `tests/unit/test_ops_preflight.py`

验收：

- 有一个 `uv run kun ops dogfood --include-db-long-horizon-drill` 能证明多轮 Mission 推进、失败恢复、复盘、继续。
- 达到 max attempts 后必须 blocked，不准无限重试。
- 恢复动作必须写 EventRow / StateLedger。

### S3. WorldGateway 真实 handler 开发补齐

目标：

- 现有 `email.send` / `browser.execute` / `enterprise_api.post` 继续保持默认关闭，但开发面要完整。
- 补齐真实 handler 的 smoke、配置诊断、补偿描述、幂等保护、审批上下文校验。
- 支付、公开发布、部署/回滚不直接做真实执行，可以继续做 plan，但要把“为什么不能真实执行”写入 capability/status。

建议文件范围：

- `kun/world/*`
- `kun/engineering/action_executor.py`
- `kun/api/nuo/action_panel.py`
- `kun/ops/preflight.py`
- `kun/ops/secret_audit.py`
- `tests/unit/test_action_executor.py`
- `tests/unit/test_world_*`

验收：

- 没配置时 fail-closed。
- 半配置时 preflight block。
- 配置齐但没有审批上下文时 block。
- 低风险 draft 类动作可 dogfood。

### S4. Compiler 编译器层补强

目标：

- KUN 编译器不是 MarkItDown 包装器，而是“输入 -> 可决策资产”的标准层。
- 补 Office/OCR/音频这类重后端的适配器接口和诚实状态，不要求本地一定安装全部依赖。
- MarkItDown 后端继续可选，未安装/未开启必须明确 unavailable。

建议文件范围：

- `kun/compiler/*`
- `kun/interface/input_translator.py`
- `kun/interface/hermes.py`
- `kun/api/compiler.py`
- `tests/unit/test_compiler*`
- `tests/unit/test_input_translator.py`

验收：

- 每种后端都有 `supported / disabled / unavailable / failed` 状态。
- 外部资料进入资产池前都有 review package。
- 傩能发现低质量编译资产并建议重编译。

### S5. CodeCapability 强化为可测试工程能力

目标：

- 保持默认 dry-run。
- 增强 patch proposal、review、rollback、sandbox check。
- 把成功路径沉淀为 review-only skill draft。
- 不做“大范围自动改仓库”的假能力。

建议文件范围：

- `kun/skills/code_capability/*`
- `kun/skills/builtin/code_*`
- `kun/api/code_capability.py` 或现有相关 API
- `kun/engineering/credit_assignment.py`
- `tests/unit/test_code_capability*`

验收：

- 单文件改动可 dry-run、可预览 diff、可执行检查、可回滚。
- sandbox 不够强时必须标 partial，不准说 production safe。
- 成功代码路径写入 resource credit 和 Qi review-only 信号。

### S6. SLYVAN 交付要求

- 每完成一个 S 任务，单独 commit。
- 分支推到 GitHub：

```bash
git push origin codex/slyvan-v5-completion
```

- 不直接合 main。
- 最后给主 session 一份总结：
  - 做了什么；
  - 哪些真实跑通；
  - 哪些仍 partial；
  - 跑过哪些命令；
  - 是否改了 delivery-status。

## 4. 主 session 线：40% 任务

### M1. Watchtower Decision Plane 统一策略票据

目标：

- 把 `ExecutionMode`、`ValueGate`、`ProtocolRegistry`、`TaskRouter`、`PreDeliverGate`、模型路由、WorldGateway policy 的关键判断尽量统一成可审计票据。
- 已经有 DecisionTicket 的地方继续检查覆盖。
- 没票据的关键事件必须被傩发现。

验收：

- 傩治理审计能报告“关键判断缺票据”。
- 决策票据能进入 StateLedger 和 credit。

### M2. MoE 记忆和策略调用层

目标：

- 任务进来先判断用不用记忆，再判断用哪类记忆。
- 按任务类型稀疏激活：记忆层、skill、方法论、评估标准、风险规则。
- 广告/编程/教育/商业决策/外部动作要有不同策略。

验收：

- `MemoryInvocationPolicy` 有清晰 task family。
- ContextPacker 消费 memory policy。
- 成功/失败信用会反向影响下一次策略。

### M3. 启的自进化闭环

目标：

- 启用本地/便宜模型做大量历史任务 replay。
- 强模型只复审高价值候选。
- 生成 StrategyPack 草稿、review package、rollout plan。
- 仍然不直接改生产。

验收：

- idle-batch 里能看到本地评估、强模型复审、lab replay、tree search 的状态。
- 输出进入 Qi problem queue / methodology draft / experiment draft。

### M4. 傩作为全系统管家

目标：

- 傩诊断不只管 agent，还管 context、memory、skill、WorldGateway、Qi、scheduler、DecisionTicket、compiler。
- 定期体检能发现：
  - 记忆爆炸/低价值/重复；
  - 外部 handler 风险；
  - 决策票据缺失；
  - 多车道抢资源；
  - Qi 草案风险；
  - compiler 低质量资产。

验收：

- `NUO system health` 覆盖这些模块。
- 高风险只报告，不自动执行。
- 低风险治理 action 有明确 apply 入口。

### M5. 最终验收和交接

目标：

- 合并 slyvan 分支前做 code review。
- 更新 `docs/v5/KUN-V5.md` 和 delivery-status。
- 最终跑全量检查。

验收命令：

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check kun tests
uv run --extra dev mypy kun
uv run kun ops delivery-status --json
uv run kun ops readiness --include-dogfood --skip-alembic
```

## 5. 哪些不能靠开发伪装完成

这些只能等配置/真实环境/真实任务：

- 真实 SMTP 账号和发信域名。
- 真实浏览器执行目标和白名单。
- 真实企业 API 和认证。
- OAuth / MFA / 设备风控。
- 云 KMS / 托管 Secret Manager。
- 真实生产数据库 + 对象存储备份恢复演练。
- 跨周真实产品运营 dogfood。
- 账单闭环和法律审查。

代码里可以做接口、审计、preflight、smoke，但不能把这些说成已经生产 ready。

## 6. 交接给 slyvan 的一句话

你负责 V5 的“执行能力和长周期底座”60%：StateLedger 长期账本、Mission 长周期闭环、WorldGateway 真实 handler 开发面、Compiler 后端接口、CodeCapability 工程能力。所有真实外部动作默认关闭和人工确认；所有未配置/未验证能力必须诚实标 partial。完成后推 `codex/slyvan-v5-completion`，不要合 main。
