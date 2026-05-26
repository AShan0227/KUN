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
