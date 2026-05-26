# L2 · 进度微日志（追加式）

> 完整回顾在 `L2-retrospective.md`（L2 全 10 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L2.1 · SupervisorService 工程化异常阈值检测

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/service.py`：`SupervisorService` 工程化阈值检测，4 种异常类型（`llm_fallback_spike` / `task_failure_spike` / `task_anomaly_spike` / `duration_outlier`），各自阈值可配置
- `SupervisorAnomalyState` per-tenant 滑动窗口（默认 60s）累积事件
- `SearchRequestEmitter` 回调签名 — emitter 把构造好的 `strategy_search_request` payload 落 DB（L2.7 给真 emitter；测试用 fake）
- Dedup：同 tenant + 同 target_module + 同 anomaly_kind 在 dedup_ttl_sec（默认 1h）内不重复触发
- 4 种异常 → 3 档 priority：fallback medium / failure high / anomaly_score medium / duration_outlier low

**关键决策**：
- **工程化优先（规则 + 阈值）vs LLM 判断**：阈值检测一阶定性（少错），用 LLM 判定每个事件成本爆炸。规则不准时 RSI 闭环（L2.7）自动调阈值
- **Per-tenant state in-memory**：进程级足够，生产 multi-process 时换 Redis
- **Dedup key `tenant_id:target_module:anomaly_kind`**：精确到 target_module 而非全局 → 不同模块可独立触发
- **回调式 emitter 而非直接写 DB**：让测试 fake 简单 + 让 L2.7 接 Strategist 时灵活组合

**8 个新单测**覆盖：阈值未到 / 阈值刚到 / 各异常类型 / dedup / tenant 隔离。776/776 unit tests pass，ruff clean。

**为下一步**：L2.2 Director Input Classifier — 复用同样的"工程化规则 + LLM 兜底"模式，6 类分流。
