# L3 · 进度微日志（追加式）

> 完整回顾在 `L3-retrospective.md`（L3 全 7 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L3.1 · 第 2 条 RSI 实例：context 压缩策略

**完成**：2026-05-27 / commit pending

**做了什么**：
- `SupervisorService` 增 5 号异常类型 `context_oversized_spike`：监听 `llm.invoke.completed` 事件，窗口内 `input_tokens >= context_oversized_input_tokens (默认 80k)` 的事件数累计 `>= context_oversized_threshold (默认 3)` 触发
- `SupervisorAnomalyState` 加两个 per-tenant 阈值字段，允许单独调整（高 token 任务的 tenant 可放宽）
- `_check_context_oversized` 沿用 `_build_request` 模板，dedup_key `tenant:llm.context:context_oversized_spike` — 1h 不重复
- `StrategistService` 增 `_candidates_for_context_oversized_spike` 生成器，挂入 `_CANDIDATE_GENERATORS` 字典，Explorer Pool 3 模式：
  - **Conservative**: `context_summary_compression`（older_than_turns=10, summary=500 tokens, canary 30%，acceptance = threshold * 0.6）
  - **Aggressive**: `context_hard_truncation`（max_turns=20, preserve_top_pins=True 防丢 anchor pinning, canary 20%）
  - **Performance**: `context_rag_retrieval`（top_k=8, fallback_to_truncation=True, shadow 100%）
- acceptance_threshold 从 evidence 的 `threshold_tokens` 字段动态算（threshold × 0.6/0.4/0.3 按 mode），evidence 缺时 fallback 80k 默认

**关键决策**：
- **context 改动属 L2 (module) 而不是 L1 (activation)**：context 压缩动 prompt assembler 模块的核心实现，比 LLM 路由 (L1) 风险更高。target_level=2 让 RCDH 诊断 + Gate 准入门禁知道这条
- **Aggressive 必带 `preserve_top_pins=True`**：硬截断不能丢掉 anchor pinning（ADR-022 Layer 2）。这是 anchor_pinning_at_prompt_top methodology 的硬约束 — 写进 change_spec 让 Executor 实装时不忘
- **Performance shadow 100%**：RAG 检索改动巨大，先 shadow 全量观察检索相关度，再决定上不上 canary。避免 prod 体验骤变
- **dedup_key 用 `llm.context` 而非 `llm.router`**：context 压缩属 context 子系统，与 LLM 路由分离，让两条 RSI 实例独立触发独立晋级
- **threshold-based acceptance 而非固定常数**：evidence 报 50k 阈值时 acceptance 应该是 30k (50k*0.6) 而不是 48k (80k*0.6 默认)。让 acceptance 跟着真实阈值缩放

**10 个新单测**覆盖：低于阈值不触发 / 单次超阈不触发 / 窗口内 3 次触发 / dedup TTL / 自定义阈值 / 非 llm.invoke 不计入 / 3 mode 候选齐全 + Aggressive 保 pinning / acceptance 动态 / 默认 fallback / 每候选必带 rollback_on。938/938 unit tests pass，ruff clean。

**为下一步**：L3.2 第 3 条 RSI 实例 —— skill 选择启发式（task_type → skill_id 路由也走 RSI 闭环）。

---

## L3.2 · 第 3 条 RSI 实例：skill 选择启发式

**完成**：2026-05-27 / commit pending

**做了什么**：
- `SupervisorService` 增 6 号异常类型 `skill_mismatch_spike`：监听 `skill.invocation.completed` 事件，同 `(task_type, skill_id)` 对在窗口内失败率 `>= skill_mismatch_rate (默认 0.5)` 且样本数 `>= skill_mismatch_min_samples (默认 4)` 触发
- `_check_skill_mismatch` 用 `conservative_sample_threshold` 思路 — 样本不足静默（cold-start damping 同源原则）
- 失败率 ≥ 0.8 时 priority 自动升 `high`（强信号）
- target_module 落 `skill.{skill_id}` 让 dedup 精确到具体 skill
- `StrategistService` 增 `_candidates_for_skill_mismatch_spike` 3 Explorer 候选：
  - **Conservative** `skill_id_swap`: target_level=1（路由层），换 next_best capability_card 推荐，canary 30%，acceptance = `1 - failure_rate * 0.5`（目标失败率减半）
  - **Aggressive** `task_type_split`: target_level=0（设计层！task_type 拆分是产品决策），shadow，requires_director_assistance=True，让 Director 协助拆分定义
  - **Performance** `capability_damping_tweak`: target_module 跳到 `capability_router`，把 cold-start damping 从 `n/30` 调到 `n/15`，让历史失败信号更快显现到路由

**关键决策**：
- **`min_samples=4` 默认起点**：与 `conservative_sample_threshold` 的 weak_signal (5-9 不调整) 不同 — 4 已经足够走 RSI 入口（毕竟 RSI 是探索，不是直接 enable_capability）。Gate 仍然会要求 ≥ 10 才真 promote
- **Aggressive `task_type_split` target_level=0 而不是 1**：拆分 task_type 是产品定义层改动，需要 Director 重新建模这条 task。换 skill 是配置改动（level 1），拆类型是设计改动（level 0）
- **Performance 跨模块改 capability_router 而不是 skill 本身**：根因可能在 router 的 cold-start damping 太保守，让坏信号要积 30+ 样本才反映。target_module 跨模块写得清楚，避免 Gate 误以为是 skill 内部 bug
- **acceptance 动态绑 failure_rate**：失败率 80% 时 acceptance=0.6（成功率 60% 即可），失败率 50% 时 acceptance=0.75。让 acceptance 反映"显著改善"而非"绝对达标"
- **`failure_rate >= 0.8` 自动 priority=high**：严重 mismatch 不能等 default medium 排队，Strategist 应优先处理这类

**9 个新单测**覆盖：min_samples 边界 / 低失败率不触发 / 高失败率触发 / priority 升 high / 不同 pair 独立累积 / 缺字段不触发 / Strategist 3 mode + level / acceptance 动态 / rollback 必备。947/947 unit tests pass，ruff clean。

**为下一步**：L3.3 Forward / Backward 双修复策略 —— Strategist 在 anomaly 发生时 auto-select 是该 forward fix（往前进一步改）还是 backward rollback（回退到上一个 known-good capability）。

---

## L3.3 · Forward / Backward 双修复策略 (Strategist auto-select)

**完成**：2026-05-27 / commit pending

**做了什么**：
- `StrategistService.__init__` 增 `capability_history_reader: CapabilityHistoryReader | None` 注入：`Callable[[target_module], Awaitable[list[capability_entry]]]`，返回最近 capability 晋级记录（按时间倒序）
- `select_repair_direction(request, capability_history, lookback_hours=24)` engineering 规则：
  - history 空 / 无 enabled / 全是 24h 以外 → `forward`
  - 24h 内有 enabled capability 命中 target_module → `backward`
  - 容错 ISO 字符串 timestamp + 无 tzinfo 当 UTC
- `_candidate_for_backward_rollback(request, capability)`：单 StrategyExperiment（不走 Explorer Pool 3 模式），`explorer_mode="backward"`, `rollout_mode="direct"`（回滚不 canary）, `change_spec.kind="capability_rollback"` + `rollback_capability_id`, rollback_on 含"回滚的回滚"保护（如果 anomaly_rate 反而上升 → re-enable）
- `propose_candidates` 改造：先调 history_reader → select_repair_direction → 若 backward 走 `_candidate_for_backward_rollback` 单候选；否则走 Explorer Pool generator
- 共用 `_emit_and_adjust` 助手把"自指标 human review + emit 落库"抽出 — backward / forward 路径都走此 helper，确保自指限制 + emitter 行为一致
- history_reader 异常吞掉 + log，回退 forward（不打挂 Strategist 主路径）

**关键决策**：
- **24h 是 backward lookback 默认起点**：足够覆盖大多数 promotion 周期（promotion_deadline 默认 14 天，但 enable 通常在数小时内完成）。窗口太短漏掉缓慢恶化，太长误判（24h 前的改动不太可能突然恶化）
- **backward rollout_mode="direct" 而不是 canary**：回滚是已知态，没必要再 canary。canary 在 forward 探索时合理；rollback 直接关
- **rollback_on 含"回滚的回滚"触发器**：如果回滚后 anomaly_rate > 1.2 倍（更糟），自动 re-enable 原 capability。防止 backward 本身是错决策导致 dead-end
- **history_reader 是依赖注入，不是 import**：与 emitter 同思路，让 service 单测零外部依赖，prod 接 ORM query helper
- **`_emit_and_adjust` 共用让 backward 也走自指限制检查**：理论上 backward target_module 也可能是监督角色，必须经过 self-referential check。Helper 抽出来让两条路径行为一致
- **`select_repair_direction` 独立 module-level 函数**：纯函数 + 容易单测 + 让 LLM Strategist (L3+) 也可以复用此决策逻辑
- **history_reader 失败默认 forward 而非 backward**：caller 失败时回退到默认行为，让"无法判断"等价于"未发现近期 capability"。inverse 会让 DB 错误意外触发 rollback

**14 个新单测**覆盖：direction 6 case（无 history / 空 / disabled / 近期 enabled / 太老 / ISO 字符串 / 无 tzinfo ISO）/ backward candidate 形状 / propose 路由 4 case（backward / forward 无 reader / forward 空 history / reader 异常）/ backward emitter 调用 / backward + self-referential 仍人审。961/961 unit tests pass，ruff clean。

**为下一步**：L3.4 监督线三级阈值（weak / mid / strong）+ 4 级升级路径（role / task / gate / human）真接 — 把 Supervisor 异常按 severity 推到不同层。

---

## L3.4 · 监督线三级阈值 + 4 级升级路径真接

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/escalation.py`：
  - `Severity` Literal: `weak / mid / strong`
  - `EscalationLevel` Literal: `role / task / gate / human`
  - `compute_severity(priority, repeat_count, failure_rate, sample_size)` engineering rules: priority=high + repeat≥2 OR failure_rate≥0.8 → strong；priority=medium + repeat≥2 OR failure_rate≥0.5 → mid；priority=high 单次 → mid；repeat≥3 强制 strong；其他 → weak
  - `escalation_path_for(severity, is_self_referential)`：weak→[role] / mid→[role, task] / strong→[role, task, gate]，自指任意严重度追加 human
  - `_is_self_referential(target_module)` 复用与 Strategist 同源逻辑（4 种命名形式 5 个角色前缀）
  - `decide_escalation(...)` → `EscalationDecision` frozen dataclass（severity / path / rationale / is_self_referential / target_module / contributing_signals）
- `SupervisorAnomalyState` 增 `repeat_counts: dict[dedup_key, int]` 跨 dedup_ttl 累计
- `SupervisorService._build_request` 调 `decide_escalation` → 输出 `strategy_search_request` 携带 4 个新字段：`severity` / `escalation_path` / `is_self_referential` / `repeat_count`
- 从 evidence 提 failure_rate / sample_size 喂给 severity 决策
- `__init__.py` export 6 个新名字

**关键决策**：
- **repeat_count 跨 dedup_ttl 累计而非每 TTL 重置**：dedup 是"窗口去重"，repeat 是"长期跟踪"。同一 dedup_key 第 1/2/3 次触发应该看到 repeat=1/2/3，让 severity 自然升级；TTL 是为了不把同 1 小时窗口的多次嗯触发当多次
- **`repeat_count >= 3` 强制 strong 不论 priority**：与 RCDH 强制升级（重复 ≥ 3 → 强制 L0 redesign）同源原则。系统重复出问题不能再 "weak/留痕"
- **escalation_path 是 list 而非单 level**：path 累加（mid 不替代 role 而是 role+task），让下游可以分阶段消费。L4 (human) 是独立追加，可能与 strong 并存
- **failure_rate 直接进 severity 决策**：第 1 次出现但失败率 80%+ 也应 strong；与 sample_size 协同避免低样本误判
- **escalation 是 pure module 而非 SupervisorService method**：让 LLM Strategist 或 Gate 等其他 caller 也能用 `decide_escalation` 不耦合 Supervisor 内部
- **`is_self_referential` 字段独立暴露**：让 Gate (L2.8) 后续直接读 `request.is_self_referential` 不需重新算
- **import escalation 在 `_build_request` 内（局部 import）**：避免循环依赖风险（escalation 不依赖 service，反之亦然），同时减少全局命名空间污染

**22 个新单测**覆盖：compute_severity 8 case（priority/repeat/failure_rate 组合 + 强制升级）/ escalation_path_for 4 case / _is_self_referential 拒绝其他 / decide_escalation 4 case / SupervisorService 4 个集成 case（字段透传 / repeat 累加 / strong path 含 gate / failure_rate 升 strong）。983/983 unit tests pass，ruff clean。

**为下一步**：L3.5 自指限制强化 — Strategist 改自己（target_module 命中监督角色）当前是 `requires_human_review=True`，要加：L4 升级时直接写 `promotion_queue.requires_human_review=True` + Gate 强制拒绝 auto-admit。

---

## L3.5 · 自指限制强化

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/governance/self_referential.py` 集中 source of truth：`SELF_REFERENTIAL_PREFIXES` 常量 + `is_self_referential(target_module)` 函数。处理 None / 空字符串容错。前两个调用方（`strategist/service.py` + `supervisor/escalation.py`）保留各自 `_is_self_referential` thin wrapper，body 改为 import + delegate
- Strategist 自指 candidate 强化：`_emit_and_adjust` 不仅设 `requires_human_review=True` + `status=awaiting_human_review`，**强制 `target_level=0`**（改自己属于设计层决策，不能让人误以为是简单的 module-level fix）。rationale 改为 `[SELF-REFERENTIAL: forced target_level=0 design-level human review]`
- Gate `_check_self_referential` 双层独立检查：除了 `experiment.requires_human_review` flag 之外，**还独立检查 `experiment.target_module` 是否命中前缀**——防 caller 漏标 flag 时仍被 Gate 抓住
- Gate `admit` 在 self-referential 通过 R1-R3 时**仍写一行 capability row**（promotion_state=awaiting_human_review / enabled=False / sampling_rate=0.0 / metadata.promotion_block_self_referential=True / metadata.self_referential_reason=...）→ 给 promotion_queue 跟踪人审进度。之前是 `capability_row_payload=None` 直接 reject
- Gate `enable_capability` 增 `metadata_lookup` + `human_approval_token` 参数：若 capability metadata 含 `promotion_block_self_referential=True` 且没传 token，**直接 `raise PermissionError`**。promote 自指 capability 必须显式 human approval
- `kun/governance/__init__.py` 导出 `SELF_REFERENTIAL_PREFIXES` + `is_self_referential` 给跨模块使用

**关键决策**：
- **集中 source of truth 是 L3.5 的核心**：之前 `_SELF_REFERENTIAL_PREFIXES` 在 strategist + escalation 各定义一次，未来再加调用方就有 3 份。governance 模块是合适的归宿——所有 RSI / RCDH / Gate 相关 governance 逻辑都在这里
- **保留 thin wrapper 而非 grep 替换调用点**：模块内部的 `_is_self_referential` 函数还在，body 改为 delegate。这样 strategist test 直接 import `_is_self_referential` 不破，新 caller 用 governance 模块 import。最小 blast radius
- **强制 `target_level=0` 是真正的"强化"**：之前自指仅标 flag，但 Strategist 可能仍输出 level=2 candidate。Gate 后续 promotion_queue 看到 level=2 + requires_human_review 容易困惑。统一升 level=0 让所有下游知道这是"重新设计"而不是"模块改一改"
- **写 row 而非 reject**：`promotion_state="awaiting_human_review"` + sampling=0.0 让 promotion_queue 仍能跟踪——人审 + token 后才走 enable。如果不写 row，所有"待人审"的自指改动就消失在日志里，promotion_queue 看不到，无法管理
- **`human_approval_token` 不验签只透传到 log**：实际人审 token 验证是更上层（auth service）的责任。Gate 只确保 flow 走过人审环节，token 是 "human in the loop" 的痕迹。L4+ 可以加签名验证
- **`metadata_lookup` 可选注入**：backward-compat — 老 caller 不传也能用（不做自指 gate）；新 caller 注入实际 ORM query。同 emitter 风格
- **R4 reason 区分两种命中**：`self_referential_requires_human_review` (flag 标了) vs `self_referential_target_module=X` (caller 漏标但 target 命中) — 让审计知道 caller 是否 follow contract

**13 个新单测**（governance 模块 4 / Strategist 强化 2 / Gate target 检查 + row 写入 2 / enable gate 4 / 集成 1）+ 修 1 个旧 test 适配新行为。996/996 unit tests pass，ruff clean。

**为下一步**：L3.6 ADR-018 半合并补齐 —— ValidationPipeline / NotificationLayer / GuardPolicy / GuardRule 各检查 ≥3 调用方并完成真正合并。

---

## L3.6 · ADR-018 半合并补齐

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `docs/adr-018-audit.md`：对 4 个 ADR-018 §16 半合并项做实际 caller 审计
  - **ValidationPipeline (§16.2)**：orchestrator + tests + Tester Protocol — 通过依赖注入接口已"by interface 真合并"，维持
  - **NotificationLayer (§16.3)**：原仅 orchestrator 1 caller → L3.6 加 Gate 自指 + Supervisor L4 = **3 真 caller**
  - **GuardRule (§16.8)**：watchtower engine + feature_activation_audit + tests = ≥2 prod caller + 测试覆盖，维持
  - **GuardPolicy**：grep 0 命中，从未实施 → **永久退役**（不再追求；若 L4+ 需要新 ADR 重新设计）
- `kun/agents/gate/service.py` 加 `notification_sender: NotificationSender | None` 参数 + `_safe_notify` helper；自指 awaiting_human_review 路径推 `alert` notification（含 capability_id / target_module / reasons）
- `kun/agents/supervisor/service.py` 加同样的 `notification_sender` 参数；observe 后若 `escalation_path` 含 `human` → 推 `alert`（含 anomaly_kind / severity / evidence）
- Notification 签名统一 `Callable[[dict[str, Any]], Awaitable[None]]` —— Service 端不 import `kun.engineering.notifications.push`，让 prod 接 DB writer / tests fake / 未来 webhook 零侵入
- 7 个新单测覆盖：Gate 自指推送 + 非自指不推 + sender 异常吞 / Supervisor L4 推送 + 非 human path 不推 + sender 异常吞 / 3 caller 共用同一签名集成测试

**关键决策**：
- **不"为达到 ≥3 而硬塞 caller"**：审计明确反对 KPI 化 "≥3 caller" 这条规则。GuardPolicy 没自然 caller 就退役，不假装实施；ValidationPipeline 主线接口已成熟就标"by interface 真合并"
- **NotificationLayer 用 dict-shape 而非 Notification 对象**：Service 不 import datamodel — 让接口最瘦。dict payload 在 sender 端转 Notification model + 落库。**减小 Service ↔ datamodel 耦合**
- **`_safe_notify` 独立 helper**：每个 Service 内一个小函数，失败吞 + log；不让 notification 失败打挂 admit/observe 主路径。同 emitter 失败语义
- **Gate notification 在 awaiting_human_review 推, 不在 reject 推**：reject 已经在 log 充分覆盖，notification 应该是"需要人介入"的明确信号 — 自指必须 NUO 看到（人审），普通 reject 是机器闭环
- **Supervisor 触发条件用 `"human" in escalation_path`**：L3.4 已经把自指追加 human 到 path，复用 path 而非 is_self_referential 字段 — 让未来 LLM Supervisor 显式标"need_human" 也能触发，不绑死自指
- **GuardPolicy 退役 = 减少未来困惑**：留着这个名字不实施，比把它真删除更糟（每个新人都会查它是什么）。明文归档决策更清晰

**7 个新单测**覆盖完整 L3.6 wiring + 3 caller 同签名集成。1003/1003 unit tests pass，ruff clean。

**为下一步**：L3.7 — L3 全 6 个子任务完成回顾 + retrospective (9 段 + Methodology Card Candidates) + 蒸馏 ≥3 份新 methodology seeds。
