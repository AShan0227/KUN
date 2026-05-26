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

---

## L5.2 · Promotion Queue 超时规则

**完成**：2026-05-27 / commit pending

**做了什么**：
- 改造 `kun/governance/promotion_queue.py`（之前 stub 化）：
  - `evaluate_capability_timeout(capability, now, stale_threshold_days)` pure 函数：返回 `TimeoutCheckResult(expired, stale, days_in_state, days_until_deadline, recommended_action)`
  - 4 档 recommended_action: `keep` (fresh) / `re_evaluate` (stale 但未过 deadline) / `mark_expired` (过 deadline) / `advance` (ready state 待 Gate enable)
  - 支持 ISO 字符串 timestamp + 无 tzinfo 当 UTC（容错优先, 与 L3.3 capability_history 同源）
  - `RuntimeCapability` model 增 `last_state_change_at` + `awaiting_human_review` 加入 `PromotionState` Literal
- `PromotionTimeoutSweeper` 周期扫描器:
  - `capability_reader: Callable[[], Awaitable[list[dict]]]` 注入读取器（DB / fake）
  - `state_writer: Callable[[capability_id, payload], Awaitable[None]]` 写更新 (expired)
  - `search_emitter: Callable[[payload], Awaitable[None]]` 写 reaudit search_request
  - `sweep()` 单次扫描 → 报告 `{scanned, expired, stale, search_requests_emitted, results}`
- `_build_reaudit_search_request(capability, result, now)` 输出与 Supervisor cluster 兼容的 strategy_search_request:
  - `triggered_by="promotion_timeout"`
  - `anomaly_kind="promotion_expired"` (deadline 过) or `"promotion_stale"` (停留过久)
  - `priority="high"` (expired) or `"medium"` (stale)
  - `severity="strong"` (expired) or `"mid"` (stale)
  - `dedup_key=tenant:promotion_timeout:capability_id` per-capability 唯一
- 三个 callback 失败语义清楚: reader 失败 → 返回 error 报告但不抛；state_writer / search_emitter 失败 → log warning 不打挂 sweep 主路径

**关键决策**：
- **`evaluate_capability_timeout` 是 pure 函数**：让单测零 mock; sweeper class 只负责 IO orchestration
- **`stale` 和 `expired` 两档语义不同**：stale = "在该 state 停留过久" (e.g. in_canary 卡 7 天) → 推 reaudit search; expired = "超过 promotion_deadline 总期限" (e.g. 14 天没走完) → state 强制 expired + 推 reaudit search. 两者都触发 reaudit, 区别在是否动 state
- **ready state stale 不算 reaudit**：ready 是"等 Gate enable"的合理 state, 单 Gate 慢不是 capability 自己的错; recommended_action="advance" 推 Gate 操作
- **iso 字符串 timestamp 容错**：与 L3.3 `select_repair_direction` 同思路 — 数据从 DB 读出来可能是 string, 不强求 datetime 对象, 内部转换
- **reader 失败返回 error 报告但不抛**：sweep 是 idle-batch 周期调用, 单次失败不应该让整个 batch 任务挂掉; log + return error 让上游 retry
- **state_writer / search_emitter 失败 swallow**：单 capability 写失败不影响其他 capability 的扫描; 错误进 log, 下次 sweep 再处理
- **`promotion_state="awaiting_human_review"` 加入 PromotionState Literal**：L3.5 Gate self-referential awaiting_human_review 写 row 时 state 值需要在 PromotionState 类型里，否则违反 Literal 类型约束

**16 个新单测**覆盖：fresh 不 expired/stale / past deadline expired / stale after threshold / ready advance / ready+stale 仍 advance / ISO 字符串 / 无 tzinfo / days_in_state 计算 / sweeper empty list / 标 expired + emit / 仅 stale 推 reaudit 不动 state / 混合 capabilities / reader 异常 / state_writer 异常吞 / search_emitter 异常吞 / dataclass 字段。1126/1126 unit tests pass，ruff clean。

**为下一步**：L5.3 监督线高优触发通道 —— Supervisor cluster (L5.1) + promotion_timeout (L5.2) 通过专门通道直接推 Strategist, 跳过普通 search_request 队列, 优先消费。
