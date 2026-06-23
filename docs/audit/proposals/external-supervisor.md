# External Supervisor 落地 · 合并方案（F030/F031/F032，关联 F047）

> 状态：needs-design（ADR-023 的独立进程 / fail-close / 监督强制力都涉及架构与进程模型决策）
> 关联：F047 已在 decisions.md ADR-023 加"3 硬约束未实现"注记；本方案给落地路径。

## 0. 现状（已核实）

ADR-023 宣称 External Supervisor 是「独立进程 + 本地模型 + 双模式(实时 Mode A / 复盘 Mode B) +
自嗨检测 + fail-close 三硬约束」。实际：

- **F030 · 独立进程 + fail-close 未落地**：`kun/external_supervisor/runner.py` 自述是 L2.4 占位 stub
  ——"启动 service + 健康检查一次 + 等 SIGTERM"，NATS 订阅 + Mode A/B 路由标 "L2.5 实装"(未做)。
  全仓无 `multiprocessing.Process`；`KUN_SUPERVISOR_MODEL` 从不读取；无 model-family 校验。
  `external_supervisor_enabled` 默认 `False`(`config.py:70`)。
- **F031 · Mode/Pool 死代码**：`modes.py` 的 `mode_a_gate_review` / `mode_b_task_debrief` /
  `self_aggrandizement_check` 与 `pool.py` 的 `ExternalSupervisorPool`(`_DEFAULT_MODES`)生产 0 调用方
  ——只有单测和 `integration/external_supervisor_critique.py` 的 prompt 渲染引用。Gate 的"监督否决权"
  因此形同虚设。
- **F032 · 裁决 advisory-only**：`long_task_orchestrator` **确实**有 critique hook——当
  `external_supervisor_service` 注入且 cadence 命中时，每 N 步直接调监督并按 verdict emit
  `long_task.critique` / `long_task.drift_alarm`。但 (a) 默认关闭；(b) **只发事件，无任何消费者据此
  暂停/终止/降级主线**——alarming 是观测信号，不是控制信号。主线照跑。

## 1. 落地选项（监督机制本体）

两条路，按"独立性强度 vs 工程成本"权衡：

### 选项 A · 同进程 fail-close 守卫（低成本，先上）
- 保持监督在主进程内(`ExternalSupervisorService` 已 in-process 接入 long_task)，但**补齐 ADR-023 的
  3 条启动校验**改为"配置守卫"：`external_supervisor_enabled=True` 时，启动检查 `KUN_SUPERVISOR_MODEL`
  已配 + 与主线 model family 不同，否则 **fail-close 拒绝启动**(而非静默跑同模型)。
- 把 `self_aggrandizement_check`(自嗨检测)接进 critique hook 的每步/任务尾，真消费其 verdict。
- 取舍：拿到"不同模型监督 + 自嗨检测 + 强制力"，但**不是真·进程隔离**(监督与主线共享进程/故障域)。
  诚实地把 ADR-023 标注为"Phase 1: in-process guard"。

### 选项 B · 真·独立进程（ADR-023 原意，高成本）
- `runner.py` 实装为独立进程(`python -m kun.external_supervisor`)：用 `multiprocessing.Process` 或
  独立容器(compose 加 `external-supervisor` service)，订阅 NATS `kun.task.*` / `kun.external_supervisor.requests`，
  路由到 Mode A/B + 自嗨检测，写 `evidence_ledger`。
- 取舍：真隔离 + 零边际成本本地模型持续监管，但要做进程生命周期、NATS 往返、背压、容器编排。
- 依赖 watchtower NATS 链路(F029)与 events outbox 已通。

**建议**：先做 A(几天，立即拿到强制力 + fail-close + 自嗨检测)，把 B 作为后续 epic；A 落地时同步修订
ADR-023 把"独立进程"标为 Phase 2 目标态。

## 2. 监督裁决如何获得强制力（F032 的核心）

当前 `long_task.drift_alarm` / critique verdict 只 emit 不阻断。接线点：`long_task_orchestrator`
的主循环——监督返回 `block`/`high_drift` verdict 时：

1. **硬阻断**：抛出受控的 `SupervisionHalt` → 主循环捕获 → 任务转 `failed`/`paused`，写终结事件(复用
   F027 修好的 canonical done 路径)。
2. **降级**：drift 时强制下一步走更保守策略(降低 effort / 强制 plan-review / 要求人审 ticket)。
3. **Gate 否决权**：Gate.admit 把"监督 block 裁决"作为一条硬 gate(R-级规则)，监督 block ⇒ 不准入。
- 配置开关 `KUN_SUPERVISOR_ENFORCE`(advisory|enforce)，默认 advisory，灰度到 enforce。

## 3. 分步落地 + 验证 + 风险

| 步 | 做什么 | 验证 | 风险 |
|----|--------|------|------|
| 1 | 选项 A 的启动 fail-close 守卫(F030) | 单测：enabled+缺 model / 同 family → 启动抛错；正常配置 → 通过 | 误挡合法启动 → 守卫只在 enabled 时生效 |
| 2 | critique hook 默认接入 + 消费 self_aggrandizement(F031) | 注入 fake 监督返回自嗨信号 → 断言 emit + 记录 | 监督 LLM 不可用 → fail-open 还是 fail-close 要明确(建议 enforce 模式 fail-close) |
| 3 | drift/block 裁决获得强制力(F032) | 集成测试：监督 block → 任务 paused/failed(advisory 模式仍只 emit) | 误杀正常任务 → 默认 advisory，灰度 enforce |
| 4 | (选项 B) 独立进程 runner + NATS 订阅 | 起独立进程 → 发请求 → 写 evidence_ledger | 进程/编排复杂度；依赖 F029 |

## 4. 与其它方案的耦合
- 监督消费 Executor 自评(F021)与防漂移闭环在 RSI 主链方案(rsi-mainline-wiring.md 第 6 步)。
- NATS 订阅依赖 watchtower NATS 规则加载(F029)。
- fail-close 守卫落地后，回填 decisions.md ADR-023 的状态注记(目前 F047 标"未实现")。

## 5. 覆盖 findings
F030, F031, F032（标 needs-design 指向本文件）；F047 已在 ADR-023 注记。
