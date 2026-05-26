# L4 · 进度微日志（追加式）

> 完整回顾在 `L4-retrospective.md`（L4 全 7 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L4.1 · Strategist Explorer Pool 配置化

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/strategist/explorer_pool.py`：`ExplorerPoolConfig` (frozen dataclass) + `ExplorerMode` Literal + `load_explorer_pool_config()` 从 env var 读取
- 默认 3 模式 `(conservative, aggressive, performance)`；支持 5 个已知 mode（含 `backward` 与 `experimental` 预留）；非法 mode 名静默丢弃
- `StrategistService.__init__` 增 `explorer_pool_config: ExplorerPoolConfig | None` 参数，默认 `load_explorer_pool_config()` 读 `KUN_STRATEGIST_EXPLORER_MODES` env
- `propose_candidates` 在 forward 路径调 `_explorer_pool.filter_candidates(candidates)` —— 仅保留 enabled forward mode；**backward 候选不过滤**（走自己的 forward/backward 决策路径）
- 候选生成器仍输出全 3 模式，**过滤是 Service 责任**（生成器不感知 config）

**关键决策**：
- **过滤层 ≠ 生成层**：生成器输出"已知最佳 N 模式"，filter 是 Service 端的资源/策略关卡。让 L4+ 加新 mode 时只改 generator + config 不改主路径
- **`backward` 不在 forward modes 列表里**：Pool config 只控 forward。backward 走 `select_repair_direction` 独立决策；即使 enabled_forward_modes=`frozenset()`，backward 仍能触发
- **`experimental` 预留 mode 不在默认 enabled**：必须显式启用，避免未来引入新 mode 时静默改变行为
- **非法 mode 名静默丢弃 + 全非法时回默认**：env var 容错优先于严格 — 错配置应"degraded but functional"，不应让 Strategist 拒绝启动
- **frozenset 作 config value**：immutable + hashable，多 service 共享同一 config 无副作用
- **TYPE_CHECKING import**：避免 strategist/service.py 主路径 import explorer_pool（防循环依赖）；运行时才在 `__init__` 内 lazy import

**15 个新单测**覆盖：默认/自定义/空 pool / filter backward 保留 / env 解析 4 case / load_explorer_pool_config 4 case / 集成 3 + 4 cases。1018/1018 unit tests pass，ruff clean。

**为下一步**：L4.2 Supervisor Pool 多实例 —— 不同 audit 维度 (latency / cost / safety) 各自独立 SupervisorService 实例，事件分流到对应维度。

---

## L4.2 · Supervisor Pool 多实例（按 audit 维度分流）

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/pool.py`：`SupervisorPool` + `SupervisorPoolConfig`
- 默认 3 audit 维度 + 各自订阅的 event_type:
  - `latency_audit` ← `task.done` (duration_outlier)
  - `cost_audit` ← `llm.fallback.triggered`, `llm.invoke.completed` (context_oversized)
  - `safety_audit` ← `task.failed`, `skill.invocation.completed`, `task.done` (task_anomaly_score)
- 同事件可被多 dim 消费 (e.g. `task.done` 同时进 latency + safety) — **fan-out 不丢信号**
- `instance_for(dim)` lazy-init 每 dim 独立 `SupervisorService` instance, dedup_key 命名空间天然隔离（不同 instance 各有自己的 `state.recent_search_requests`）
- `observe(event_type, payload)` 路由 fan-out + 并行 `asyncio.gather`，单 dim 异常 `return_exceptions=True` 吞掉不打挂其他 dim
- 共用的 `emitter` / `notification_sender` 注入到所有 instance — 一处配齐多 dim 都用

**关键决策**：
- **fan-out 而非分配**：`task.done` 被两 dim 同时消费（latency 检查 duration_outlier，safety 检查 task_anomaly_score）。**单事件被多 dim 看，不互相争抢**。如果改成"先到先得"会让 audit 维度互相覆盖
- **dedup_key 命名空间通过 instance 隔离自然实现**：不需要在 key 字符串里加 dim 前缀。两 dim 各持 `SupervisorService` instance，state 字典是独立 dict，dedup 自动不冲突
- **`return_exceptions=True` + log 异常**：单 dim 异常不让整个 Pool observe 挂掉。Supervisor 是旁路监督，不能把自己挂掉影响主线
- **lazy-init instance**：Pool 启动时不预创建所有 dim — 第一次该 dim 收到事件才 init。省内存，适合 dim 配置可能变化的场景
- **未知 event_type 不创建 instance**：`dimensions_for_event` 返回空时 observe 直接 `return {}`，不污染 Pool state
- **Pool 不持锁等 emitter / LLM 调用**：`async with self._lock` 仅包裹 `instance_for` 创建步骤，`asyncio.gather` 在锁外跑，避免单 dim 卡住其他 dim

**13 个新单测**覆盖：默认 3 dim / fan-out task.done / 唯一 cost_audit / 未知 event 空 / 自定义 config / lazy init / 不匹配 event 空返回 / 路由单 dim / 两 dim 并行 fan-out / state isolation / all_dimensions 列表 / 共享 emitter / 单 dim 异常吞。1031/1031 unit tests pass，ruff clean。

**为下一步**：L4.3 External Supervisor Pool 配置化 —— 按 audit mode 分实例（gate_review / task_debrief / self_aggrandizement 各自独立 ExternalSupervisorService 实例 + 本地模型 concurrency budget）。
