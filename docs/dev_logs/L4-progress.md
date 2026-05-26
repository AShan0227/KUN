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
