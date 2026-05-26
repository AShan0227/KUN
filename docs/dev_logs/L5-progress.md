# L5 · 进度微日志（追加式）

> 完整回顾在 `L5-retrospective.md`（L5 全 6 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L5.1 · Supervisor 异常聚类 (自创 RSI 第一步)

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/anomaly_cluster.py`：`AnomalyCluster` frozen dataclass + `cluster_anomalies()` + `cluster_to_search_request()`
- 3 条聚类规则按优先级评估：
  - **Rule 1 module_systemic**: 同 `target_module` + ≥2 不同 `anomaly_kind` → "module-level systemic issue"
  - **Rule 2 cross_module_pattern**: 同 `anomaly_kind` + ≥2 不同 `target_module` 且前缀相同 → "cross-module pattern, 可能设计层问题"
  - **Rule 3 tenant_degradation**: 同 tenant + ≥3 独立 anomaly (未被前 2 条 rule 抓) → "tenant-wide degradation"
- Member 不重复使用：进入 Rule 1 的 request 不再进 Rule 2 / 3, 避免同 anomaly 被多次聚类
- `combined_evidence` 保留 `_from_request_id` + `_from_anomaly_kind` 让审计可追溯
- Cluster priority 自动 `high` (≥ 2 members 已超过单异常基线)
- `cluster_to_search_request` 输出与单 anomaly request 兼容: `triggered_by="anomaly_cluster"` (与 anomaly_threshold 区分), `anomaly_kind=cluster_kind`, 额外字段 `cluster_member_request_ids` / `cluster_anomaly_kinds` / `cluster_target_modules`
- `SupervisorService.observe` 在每次 emit 前调 `_maybe_cluster` —— 新 request 入 recent buffer (limit 20) 后跑聚类，若有 cluster → 额外 emit 1 个 cluster request；走自己的 dedup (`tenant:cluster:kind:module`) 1h 不重复
- `__init__.py` export `AnomalyCluster` / `cluster_anomalies` / `cluster_to_search_request`

**关键决策**：
- **3 条规则按优先级 + Member 不重用**：先抓最强的 module_systemic (同 module 多 kind = 该 module 系统性问题)，再抓 cross_module_pattern (设计层问题)，最后兜底 tenant_degradation。member 进入前者的 cluster 后不再进后者 — 避免一个 anomaly 被多个 cluster 重复"算账"
- **Cluster 自带 dedup_key**：`tenant:cluster:kind:module` 让同 cluster 1h 内不重复触发 — 否则每次 observe 都会发现同 cluster
- **`triggered_by="anomaly_cluster"` 字段**：Strategist 收到时可知道这是系统性问题, 不应该走单 anomaly 的 Explorer Pool 而是走 LLM Strategist (L5+) 或 forward 设计层改动
- **`severity=strong` + `escalation_path=[role, task, gate]`**：cluster 默认 strong (≥2 anomaly), 跳过 weak/mid 直接给 Gate. Cluster 本身就是 escalation signal
- **`recent_emitted_requests` 滑动 buffer (limit 20)**：保留最近 20 个 request 跑聚类 — 太多让 cluster_anomalies O(n²) 慢; 太少抓不到跨时间聚类
- **不在 observe 主路径同步 cluster_to_search_request**：cluster 是"次生事件", 不阻塞主 anomaly 信号回 caller; 它走自己的 emit 路径
- **member_request_ids 用 set 不丢序但 sorted**: 用 set 集去重, sorted 让 dev_log 稳定可读

**14 + 1 个新单测**覆盖：3 个 prefix helper / 4 条 rule (无 cluster 单 request / 无 cluster 不相关 / Rule 1 / Rule 2 / Rule 2 前缀差异不形成 / Rule 3 / 跨 tenant 隔离) / member 不重用 / combined_evidence 追溯 / cluster_to_search_request 形状 + dedup_key / SupervisorService 集成验证 (3 task.failed + 1 task.done duration_outlier → 同 module 多 kind → cluster emit)。1110/1110 unit tests pass，ruff clean。

**为下一步**：L5.2 promotion_queue 超时规则 —— 候选 > N 天未晋级 → 重审或 expire。
