# KUN V3 下一轮并行分工

更新时间：2026-04-29

当前集成基线：

- 分支：`codex/v3-world-mission-integration`
- 口径：先不合 `main`，先在这个集成分支上继续拆子分支。
- 已完成：WorldGateway 最小低风险 handler + Mission continuation runner 第一版。
- 诚实边界：Mission 现在是“启动续跑执行任务并写回 checkpoint”，不是原 TaskRow 原地恢复。

## 总规则

1. 每个 session 从 `codex/v3-world-mission-integration` 拉新分支。
2. 不要直接改对方负责的核心模块。
3. 写了新能力，必须写清楚：调用方、消费者、测试、诚实边界。
4. 能力没有被真实调用，就不能在文档或 UI 里写“已完成”。
5. 任何真实外部动作必须经过 WorldGateway，不允许在普通 tool / skill 里绕开。

## Session A：主 session，也就是我这边

分支建议：`codex/v3-world-status-ux`

### A1. WorldGateway 能力边界继续收口

目标：让“外部世界动作”更清楚、更可审计、更不容易误用。

主要文件：

- `kun/world/gateway.py`
- `kun/engineering/action_executor.py`
- `kun/api/nuo/action_panel.py`
- `kun/engineering/delivery_status.py`
- `frontend/src/app/nuo/page.tsx`
- `frontend/src/app/page.tsx`
- `tests/unit/test_v3_memory_scoring_gateway.py`
- `tests/unit/test_action_executor.py`
- `tests/unit/test_delivery_status.py`

要做：

1. 把 handler descriptor 展开为更明确的“能做 / 不能做 / 需要权限 / 是否外发”。
2. NUO 和首页显示更直白：草稿、dry-run、真实写文件、缺 handler。
3. 不支持的 action_type 不要只是一句 requires_handler，要给下一步建议。
4. 将 WorldGateway 的执行结果继续写入 StateLedger / delivery status，让用户能看懂“这次到底有没有真实影响外部世界”。

验收：

- 不支持的外部动作不会被误说成已执行。
- 支持的低风险动作能看到 artifact / diff / draft / dry-run 包。
- 测试覆盖 preview、execute、失败、requires_handler。

### A2. 主体验做减法

目标：首页不要变成技术面板，只显示用户能懂的四件事。

四件事：

- 当前在干什么。
- 花了多少 / 预算如何。
- 风险在哪里。
- 哪里需要我确认。

主要文件：

- `frontend/src/app/page.tsx`
- `kun/api/blackboard_data_sources.py`
- `kun/core/state_ledger.py`

验收：

- 首页能一眼看到 Mission、活跃任务、待确认动作。
- 技术细节默认不铺满主屏。
- 不做节点图主入口。

### A3. 能力边界账本持续校准

目标：NUO 里说的“ready / partial / audit_only / not_ready”和代码真实状态一致。

主要文件：

- `kun/engineering/delivery_status.py`
- `docs/v3/V3_IMPLEMENTATION_AUDIT.md`
- `docs/v3/V3_DELIVERY_PLAN.md`

验收：

- 每次接通一条真实链路，都同步更新能力边界。
- 每个 partial 都明确 missing 和 next_steps。

## Session B：另一个 session

分支建议：`codex/v3-mission-reaper-scheduler`

### B1. Mission failure reaper

目标：Mission 不要卡死在 queued / running。

主要文件：

- `kun/engineering/mission_control.py`
- `kun/engineering/mission_worker.py`
- `kun/datamodel/events.py`
- `tests/unit/test_mission_control.py`
- `tests/unit/test_mission_worker.py`

要做：

1. 扫描长时间没有更新的 mission task。
2. 对 queued 卡死：允许重新进入 resume request，或者标记 blocked 等人处理。
3. 对 running 卡死：按阈值标记 failed / blocked，并写 checkpoint。
4. 发事件，比如 `mission.task.reaped`、`mission.task.blocked`。
5. 不要碰 WorldGateway handler。

验收：

- 单测覆盖 queued stale、running stale、未超时不处理、超过 max_attempts 后 blocked。
- reaper 的结果会更新 MissionTaskRow、RuntimeStateRow、MissionRow。
- 事件里写清 reason 和 stale_seconds。

### B2. Mission 定时调度收口

目标：现在手动点“推进一次”可以用，但后台也要能按节奏跑。

主要文件：

- `kun/api/main.py`
- `kun/api/runtime.py`
- `kun/engineering/mission_worker.py`
- `kun/engineering/mission_control.py`
- `tests/unit/test_api_runtime.py`

要做：

1. 确认 `mission_resume_every_minute` 使用的是接了 Orchestrator 的 worker。
2. 加 reaper 定时任务，例如 `mission_reaper_every_minute`。
3. 定时任务不能吞异常后静默失败，要写事件或日志。
4. 配置开关：本地默认可开，生产可控。

验收：

- runtime 安装后能取到同一个 MissionResumeWorker。
- scheduler job 调用 worker / reaper 时使用正确 tenant。
- 异常有可观测输出，不假装成功。

### B3. Mission 级预算和 checkpoint 汇总

目标：长期任务不是只知道“跑没跑”，还要知道花了多少、做到哪一步。

主要文件：

- `kun/engineering/mission_worker.py`
- `kun/engineering/mission_control.py`
- `kun/datamodel/mission.py`
- `frontend/src/app/page.tsx`（只允许轻量显示，不做大 UI）

要做：

1. 从 continuation outcome 汇总 cost / tokens / duration。
2. MissionSnapshot 里暴露轻量统计，或者在 checkpoint 里保留汇总。
3. 首页长期目标卡能看到简单进度，不要做复杂详情页。

验收：

- continuation 跑完后 Mission 能看到最近一次 outcome。
- 多次 resume attempts 不覆盖所有历史，至少保留最近一次和累计数。

## 明确不要做

- Session B 不做真实外部动作执行器。
- Session A 不重写 Mission runner。
- 两边都不改模型路由核心，除非有单独 brief。
- 两边都不把“部分闭环”写成“完整自动运营”。

## 合并顺序

1. 先合 Session A 的 WorldGateway / UX 状态修正。
2. 再合 Session B 的 Mission reaper / scheduler。
3. 最后跑集成测试：WorldGateway 审批解除暂停后，Mission worker 能发现 queued task 并推进 continuation。
