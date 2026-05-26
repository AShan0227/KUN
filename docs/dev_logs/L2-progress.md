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

---

## L2.2 · Director Input Classifier 6 类分流

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/director/input_classifier.py`：`classify_input(new_input, *, goal_anchor=None, source="user")` 同步 API，返回 `InputClassification(category, confidence, matched_signal, integration_hint)`
- 6 类：`interrupt` / `explicit_pivot` / `on_topic_clarification` / `scope_expansion` / `on_topic_progress` / `off_topic_noise`
- 规则优先级（高 → 低）：interrupt 关键词 → pivot 关键词 + goal 几乎无关 → out_of_scope 命中 → clarification 关键词 + ≥1 goal token 命中 → scope_expansion 关键词 → goal overlap ≥ 0.3 进 progress → 兜底 off_topic_noise
- `_extract_keywords` 同时处理中英文（`[一-鿿]+|[a-zA-Z0-9]{2,}`），`_overlap_ratio` 算 input ∩ goal / input
- `integration_hint.action` 给 Director：`cancel_task` / `pause_and_confirm` / `integrate_and_refine_criteria` / `trigger_rcdh_level_0` / `integrate` / `queue_for_post_task`

**关键决策**：
- **工程化规则 + 关键词命中 ≥ LLM**：anti-sycophancy 的工程化解药 — 噪音根本不进 working context（ADR-022 Layer 3）。LLM 兜底放 L3 闭环再上
- **scope_expansion 关键词独立成强信号**：用户说 "also/再加/additionally" 几乎都是 scope creep — 即使没 goal token 重叠也触发（"authentication" → "password reset" 概念相关但词不重）
- **clarification 阈值 ≥ 1 goal token**：早版本用 overlap ratio ≥ 0.2 太严，澄清通常字短 token 少；改成"任一 goal token 命中"，更符合真实输入分布
- **"and / plus" 不进 scope_expansion 模板**：太通用易把任意句误判扩展；"also" 必须接动词（`also (please|let's|add|include|do)`）才算强信号
- **out_of_scope 优先于 clarification**：用户在 GoalAnchor 显式声明的禁区比任何句法信号都强

**13 个新单测**覆盖：interrupt 中英文 / pivot / clarification 中英文 / off_topic_noise / scope_expansion 中英文 / on_topic_progress / out_of_scope 显式禁区 / 空输入 / 无 goal_anchor 兜底。789/789 unit tests pass，ruff clean。

**为下一步**：L2.3 Periodic Plan Review Heartbeat — Supervisor 每 N 步 / N 秒注入"现在停下来回看：是否还在 anchor 上"。

---

## L2.3 · Plan Review Heartbeat 长任务防漂心跳

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/plan_review_heartbeat.py`：`PlanReviewHeartbeat(step_interval=3, time_interval_sec=300, emitter=...)`
- 两个入口：`on_step_completed(...)` 推进 step 计数 + 双阈值检查；`on_idle_tick(...)` 只检查时间阈值（Executor 卡住时也能注入）
- `_TaskCounter` per-task in-memory 状态：`steps_since_last_review` / `last_review_at` / `total_steps` / `total_reviews`，`asyncio.Lock` 保证并发安全
- `evaluate_executor_self_report` 工程化规则评判：4 条规则（scope_creep_detected / on_anchor=False / goal_token 零命中 / criteria_done 回退）→ aligned / drifting / off_track
- `derive_action` 顺序映射：aligned→continue / drifting→pause_for_anchor_recheck / off_track→trigger_rcdh_level_0
- `ReviewTrigger` dataclass(frozen)：review_id（`new_id("plan_review")`）+ payload 含 verdict / drift_evidence / action_taken / trigger_reasons / total_steps
- `__init__.py` export `PlanReviewHeartbeat` / `ReviewTrigger` / `derive_action` / `evaluate_executor_self_report`

**关键决策**：
- **双阈值 OR 触发**：单一 step 阈值在"长 step 慢思考"任务（每步 30 分钟）下太迟；单一时间阈值在"密集小 step"任务下重复触发。两者并列才覆盖真实分布
- **`on_idle_tick` 单独 API**：长任务执行卡住时（等外部 IO / 等 LLM 慢响应），不会有 `step_completed`，但还是要按时间触发 plan review。这是"心跳"语义而不仅是"计步器"
- **engineering verdict 不调 LLM**：4 条规则是来自 ADR-022 §Layer 4 的具体 drift 信号清单。一条 = 轻度（pause 重对 anchor），两条 = 重度（直接走 RCDH L0）。LLM 兜底放 L3 闭环
- **`derive_action` 显式分级而不是 verdict 直接当 action**：让"verdict 怎么转 action"在一个地方改，不散落 — 后续若 anchor 类型不同需要不同动作策略，verdict 不动，只改 derive
- **emitter 异常吞掉 + log.warning**：心跳本身是旁路信号，不该把 Executor 主路径打挂。异常进 log 由 RSI 闭环（L2.7）自我修复

**18 个新单测**覆盖：4 条 verdict 规则 / derive_action mapping / step 阈值首次触发 / step 阈值 reset 后再次触发 / time 阈值触发 / idle_tick 只查时间 / emitter 调用 / emitter 异常吞 / off_track triggers_rcdh / 多任务隔离 / reset 清计数 / 非法参数拒绝 / 并发 asyncio.gather 5 路。807/807 unit tests pass，ruff clean。

**为下一步**：L2.4 External Supervisor 独立进程化 — docker-compose 加 service + `LocalLLMProvider`（ollama）让监督走本地模型。
