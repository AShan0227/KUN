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

---

## L2.4 · External Supervisor + LocalLLMProvider (3 commit 串行)

**完成**：2026-05-27 / commits 5bbf486 + 6330d6e + (本提交)

### L2.4a (commit 5bbf486) — `LocalLLMProvider`

- 新建 `kun/interface/llm/local_provider.py`：thin adapter over `AsyncOpenAI`，默认指向 ollama (`http://localhost:11434/v1`, `qwen2.5:32b`)
- `cost = 0` / `supports_tools = False`（多数本地模型 OpenAI-compat tool calling 不稳）/ `tier = "cheap"`
- 通过构造参数可换 `llama.cpp` / `vLLM` / `TGI` endpoint，timeout 默认 120s
- 8 个新单测覆盖 defaults / custom endpoint / invoke happy path / stop words / length finish / 异常传播 / health_check 双向

### L2.4b (commit 6330d6e) — External Supervisor module

- 新建 `kun/external_supervisor/`：
  - `service.py` · `ExternalSupervisorService.analyze_observation(obs_kind, payload, anchor=…)`
    - 系统 prompt 顶部 pin "═══ EXTERNAL SUPERVISOR ═══" + GoalAnchor + observation
    - 调本地 LLM 出 JSON `{verdict, rationale, recommended_action}`
    - JSON-first 解析（含 embedded-in-prose 提取）+ 文本启发式兜底
    - `_coerce_verdict` 把 critical/warn/pass 等别名归一到 ok / concerning / alarming 三档
    - `asyncio.Semaphore` 限并发（本地推理慢，不能让 caller queue 炸）
  - `runner.py` · `python -m kun.external_supervisor` 入口：settings gating + 健康检查 + SIGTERM 优雅退出
  - `__init__.py` export 顶层 API
- `kun/core/config.py` 加 6 项 `KUN_EXTERNAL_SUPERVISOR_*` settings（enabled / model_id / base_url / api_key / timeout / max_concurrent）
- 17 个新单测（14 service + 3 runner）

### L2.4c (本提交) — docker-compose service

- `docker-compose.dev.yml` 加 `ollama` service：
  - 用 `profiles: ["external-supervisor"]` — 默认不拉起（qwen2.5:32b 镜像 ≈ 20GB）
  - 启用方式：`docker compose --profile external-supervisor up`
  - 健康检查 + 持久 `ollama_data` volume

**关键决策**：
- **三个 sub-commit 串行**：strictly 单 commit ≤ 5 文件 — provider / service / compose 三层物理分离，git history 也分离
- **`tier="cheap"` 不是 "local" 新档**：避免引入新 ModelTier literal 牵连多处 router 代码。本地模型 cost=0 但能力中档 → cheap 档语义最贴
- **`supports_tools=False`**：本地模型 tool calling 实测不稳，先关掉避免 silent error；Mode A/B（L2.5）用 prompt + JSON 解析模拟工具调用
- **JSON-first 解析 + 文本兜底**：本地模型不一定守 JSON 格式 — 启发式 fallback 保证 verdict 永远有值（即使是 "concerning" 默认）
- **`asyncio.Semaphore(2)` 默认**：本地推理慢（qwen2.5:32b ≈ 5-10s/请求），让 4 个并发 caller queue 就把 supervisor 拖死。L4 多实例时再调
- **`profiles: ["external-supervisor"]`**：默认 `docker compose up` 不拉 ollama — 没有人本地 dev 都想为 20GB 镜像付出 RAM
- **`KUN_EXTERNAL_SUPERVISOR_ENABLED=false` 默认**：runner 自检 + 优雅 disabled exit。production 显式开启避免误启

**25 个新单测**（8 provider + 14 service + 3 runner）。832/832 unit tests pass，ruff clean。

**为下一步**：L2.5 把 service 接进主线 —— Mode A（Director gate 前 sync 复核）+ Mode B（task done 后 debrief 写 evidence_ledger）+ 自嗨检测每次必跑。

---

## L2.5 · Mode A + Mode B + 自嗨检测

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/external_supervisor/modes.py`：
  - `GateAdvisory` (frozen dataclass) · Mode A 输出 (verdict: approve/escalate/block + recommended_action)
  - `DebriefRecord` (frozen dataclass) · Mode B 输出 (verdict + evidence_quality_score + capability_writeback)
  - `SelfAggrandizementCheck` (frozen dataclass) · 自嗨检测输出 (is_self_aggrandizing + engineering_signals + llm_verdict)
- `mode_a_gate_review(service, *, anchor, gate_evidence, …)` → 调 service.analyze_observation(obs_kind="gate_review")，把 ok/concerning/alarming 映射到 approve/escalate/block；保留 LLM 提供的 recommended_action 优先
- `mode_b_task_debrief(service, *, anchor, task_summary, artifacts, …)` → 先工程化 `_compute_evidence_quality_score(artifacts)` 算 [0,1] 质量分（test_report+0.4 / artifact_link+0.2 / decision+0.1 / kind 多样性 + artifact 数量加成），再 LLM verdict；alarming → writeback.promotion_block_reason
- `check_self_aggrandizement(service, *, anchor, executor_self_report, evidence_artifacts, always_call_llm=False)`：
  - 5 条工程化规则（claims_all_done_but_no_test_report / evidence_count_zero / fallback_triggered_but_self_report_clean / rationale_too_short_vs_complexity / unverified_path:*）
  - 0 signal + always_call_llm=False → 跳过 LLM 调用（省 token）
  - ≥1 signal 或 always_call_llm=True → LLM 二次复核
  - 判定 `is_self_aggrandizing` = (engineering_signals ≥ 2) OR (LLM verdict == "alarming")
- `__init__.py` export 三个 dataclass + 三个 wrapper

**关键决策**：
- **engineering-first + LLM second**：自嗨检测是高频调用 —— 每个任务结束都跑。工程化规则覆盖 80% 明显作弊（claim done 无 test_report），LLM 只为模糊案例烧 token。0 signal 默认跳过 LLM 是关键省成本路径
- **单工程化 signal 不足以判自嗨**：一个 signal 在真实场景里噪声大（artifact_link 没 test_report kind 可能是任务类型决定的）。要么 ≥2 个 signal，要么 LLM 明确 alarming。低 noise floor 保证 Gate 不会被噪声卡住
- **`GateAdvisory.recommended_action` 优先 LLM 提供值**：LLM 给的具体 action（"request_more_evidence"）通常比 mapping 表的默认更精确。默认只在 LLM 没给时兜底
- **`DebriefRecord.recommended_capability_writeback` 是 dict 不是结构化对象**：L2.8 Gate 要消费它写 `runtime_capabilities` —— dict 让 schema 演化时 Gate 端不需要同步更新。仅约定 keys（supervisor_verdict / evidence_quality_score / promotion_block_reason）
- **evidence_quality_score 工程化打分**：不调 LLM 也能给 capability 升降级一个量化信号。test_report 权重 0.4 是因为它是最难造假的（要有真测试输出 + 通过/失败计数）。Multi-source（kind diversity）加成防止"只贴 1 个 artifact_link 假装完整"
- **L2.5 不接 NATS / 不写 evidence_ledger 表**：sync API 让主线直接调即可，async + 落库的部分进 L3 (NATS 订阅 + 异步消费)

**19 个新单测**覆盖：Mode A 三档 verdict 映射 + LLM action 优先 / quality_score 三档 / Mode B writeback 含 quality / alarming 加 promotion_block / 5 条工程化 signal / 0 signal 跳 LLM / 单 signal 不判自嗨 / always_call_llm flag 强制调 LLM / 多 signal + LLM alarming 判定。851/851 unit tests pass，ruff clean。

**为下一步**：L2.6 RCDH 4 级诊断 + diagnostic_records 写入 + narrow_scope ≤5 模块工具。

---

## L2.6 · RCDH 4 级诊断 + narrow_scope 工程化实装

**完成**：2026-05-27 / commit pending

**做了什么**：
- 实装 `kun/governance/diagnosis_scope.narrow_scope(symptom, evidence)` —— 之前是骨架：
  - 三层优先级 (evidence 显式 module × 10 / symptom 中 `kun/foo/bar` 路径 × 5 / 已知模块根关键词 × 1) + `Counter.most_common(5)` 自动截断
  - 28 个已知 KUN 模块根（director / executor / supervisor / strategist / external_supervisor / router / anchor / input_classifier / rcdh / plan_review / orchestrator / outbox 等）
  - `_PATH_RE` + `_DOTTED_RE` 分别匹配 `kun/foo/bar` 与 `kun.foo.bar` 形式，统一归一化到斜杠路径
- 实装 `kun/governance/rcdh.run_diagnostic(...)` —— 之前 stub：
  - 自动调 narrow_scope 圈定模块（caller 未提供时）
  - 4 个独立 `_check_level_N_*` 函数 engineering-first（无 LLM）：
    - L0: `_L0_DESIGN_KEYWORDS` 9 个 + cross_module_impact (`≥3` 不同模块出现在 evidence)
    - L1: `_L1_ACTIVATION_KEYWORDS` 10 个 + `capability_state` 字典查询 (传入时检查 evidence 提到的 capability 是否 disabled)
    - L2: `1-3` 个 scope_modules → `narrow_scope_module_focus`；同一 module 在 evidence 重复 ≥2 → `module_repeat_in_evidence`
    - L3: stack trace regex (`Traceback|File "..."|line ...`) + evidence kind=stack_trace + file+line 字段
  - 所有 4 级独立跑 → 留全 evidence，再按 0→3 取第一个 `is_root_cause=True` 为 `root_cause_level`
  - 强制升级：`repeat_history_count >= REPEAT_FORCE_ESCALATION_THRESHOLD (3)` 且 root_cause 落 L2/L3 → 强制改 L0 + recommended_action=redesign + 追加 `force_escalation` evidence
- `recommended_action` 自动映射：L0→redesign / L1→activate / L2→module_rsi / L3→code_fix

**关键决策**：
- **engineering-first L2 范围, LLM 兜底放 L3+**：4 级 keyword + 工程化规则覆盖明显模式（stack trace / module 重复 / 跨模块影响）；模糊案例进 L3 闭环交给 LLM Diagnostician 升级版做
- **`re.search(pattern, text)` 加 `\b` 强制 word boundary**：第一版用 `if kw in text` 子串匹配，结果 "module-specific bug" 命中 "spec"（L0 关键词），L0 误判 → L2 永远跑不到。修复用 `\b`：词边界精确匹配
- **L0 keywords 不含原子 "spec"**：常规英文很容易撞（specific/specification/specs）。只保留 "specification"（精确）+ "design decision" 这类多词短语
- **`narrow_scope` 权重 evidence > path > keyword**：evidence 中的 module 字段是上游模块自报，最可靠；symptom 中嵌入的 `kun/foo` 路径是 stack trace 通常很准；关键词命中（裸 token "executor"）噪声最大。权重 10/5/1 体现可靠度
- **`narrow_scope_module_focus` 只在 1-3 个模块时触发**：4-5 个模块虽未超上限，但说明问题分散 → 不算 module-level root cause，让 L0 cross_module_impact 抓
- **强制升级不降级 L0/L1**：repeat ≥ 3 时只把 L2/L3 升到 L0；如果已经是 L1（feature flag disabled），保持 L1 — 反复出现的激活问题不需要重新设计

**23 个新单测**覆盖：narrow_scope 7 种路径 / L0 keyword + cross-module / L1 keyword + capability_state / L2 single + repeat / L3 stack trace + file+line / 强制升级 3 种边界 / scope_modules 上限 / 自动 narrow / 无信号兜底 / LevelCheckResult 默认。874/874 unit tests pass，ruff clean。

**为下一步**：L2.7 第一条 RSI 实例 —— Strategist on-demand 用 LLM router 优化为例：异常 → Strategist 提候选 → Executor 跑实验 → Tester 评估 → Gate 启用 → 下次任务用新路由。

---

## L2.7 · Strategist on-demand + 第一条 RSI 实例 (LLM 路由优化)

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/strategist/service.py`：
  - `StrategyExperiment` (frozen dataclass) 对应 `runtime_experiments` 一行 — experiment_id / target_module / target_level / change_spec / rollout_mode / sampling_rate / success_metric / acceptance_threshold / rollback_on / ttl_seconds / status / explorer_mode / requires_human_review / rationale
  - `to_row_payload(tenant_id)` 转 ORM 可消费的 dict（L2.8 Gate / L3 DB writer 用）
  - `StrategistService.propose_candidates(request) → list[StrategyExperiment]`：根据 `anomaly_kind` 路由到候选生成器；未知 kind → 空 list + warning
  - `_candidates_for_llm_fallback_spike` 第一条 RSI 实例 — Explorer Pool 3 模式：
    - **Conservative**: tier upgrade strong→top, sampling 30%, canary，rollback_on cost > $0.5 / task_failure > 10%
    - **Aggressive**: fallback provider 直升 primary（依赖 evidence 含 fallback_provider；缺则跳过），sampling 50%, canary
    - **Performance**: 重试 1→3, sampling 100% shadow（不影响生产）, rollback_on latency_p95 > 5s
  - `_candidates_for_task_failure_spike` 单 Conservative 候选 — tier upgrade 该 task_type, canary 20%
- 自指限制 `_is_self_referential(target_module)` 检测 5 个监督角色前缀（strategist / supervisor / gate / director / external_supervisor）4 种命名形式（裸名 / 点路径 / `kun.agents.X` / `kun/agents/X`）；命中 → `requires_human_review=True` + `status="awaiting_human_review"` + rationale 加 `[SELF-REFERENTIAL]` 标记
- `__init__.py` export `StrategistService` / `StrategyExperiment` / `ExperimentEmitter` / `experiment_as_dict`

**关键决策**：
- **engineering rule-based candidates，不调 LLM**：L2 范围只做 rule-based Explorer。3 个 mode 的差异由工程化决策树驱动（conservative=最小风险参数 / aggressive=大幅改动 / performance=不动主路径只 shadow）。L3+ 闭环再上 LLM Explorer
- **fallback_provider 是 aggressive 的硬依赖**：没有 fallback 信息就不知道改谁。evidence 没填则跳过 aggressive 而不是猜 — 避免做 `primary=anthropic → primary=stub` 这种荒诞实验
- **rollback_on 必须配齐**：每个候选都附 2-3 个回滚触发器（成本/失败率/延迟）。没回滚保护就上线 = 出问题靠人发现，与 RSI 闭环宗旨违背
- **sampling_rate 按 mode 阶梯**：performance=100%（shadow，无伤），conservative=30%（canary 真路径），aggressive=50%（明确风险更高时反而 sample 多让信号更快显现，但 rollback 触发器也更紧）
- **`replace(c, requires_human_review=True)` 用 dataclasses.replace**：frozen dataclass 不能就地改，replace 返回新实例。保持 immutability 同时实现"通过自指 → 调整 status"
- **emitter exception 吞掉 + log**：失败原因后续 RSI 闭环自己发现并修；不让 Strategist 主路径挂掉
- **`anomaly_kind` 未知返回空 list 而不是抛**：上游 Supervisor 写的 request 可能是新 kind，Strategist 端 graceful — warning 进 log，让 LLM Strategist (L3+) 接手

**14 个新单测**覆盖：自指 4 种命名形式 + 拒绝其他模块 / llm_fallback 三 mode / fallback_provider 缺失跳 aggressive / acceptance + rollback 强制配齐 / task_failure 单 candidate / 自指标 human review / 非自指不标 / unknown kind 空 list / missing kind 空 list / emitter 调用 / emitter 异常吞 / to_row_payload 形状 / experiment_as_dict 转换。888/888 unit tests pass，ruff clean。

**为下一步**：L2.8 Gate 准入门禁 —— 读 TestReport + DiagnosticRecord + DebriefRecord，决定是否把 experiment 写进 `runtime_capabilities` 让下次任务真用上。

---

## L2.8 · Gate 准入门禁 + runtime_capabilities 写入

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/gate/service.py`：
  - `GateService.admit(experiment, *, test_report, diagnostic_record=None, debrief=None, is_fix=False, tenant_id="default")`
  - 4 条工程化规则独立检查：
    - **R1** test_report.pass_rate ≥ `min_pass_rate` (默认 0.9)；缺 test_report 直接拒；可从 passed_count/total_count 反推 pass_rate
    - **R2** is_fix=True 必须带 `diagnostic_record.diagnostic_id`（ADR-021 拒绝裸修）
    - **R3** debrief.verdict ≠ "alarming"；evidence_quality_score ≥ `min_evidence_quality` (默认 0.5)
    - **R4** experiment.requires_human_review=True → 不自动启用，转 `awaiting_human_review`（不当 reject）
  - 所有规则通过 → 准备 `RuntimeCapability` row payload：enabled=False / promotion_state="merged" / 14 天 deadline / 把 experiment.rollback_on 和 sampling_rate 透传 / metadata 携带 experiment_id / explorer_mode / test_pass_rate / debrief_verdict / evidence_quality_score / diagnostic_id 全套证据
  - `capability_writer` 异步注入；写库失败仅 log（不打挂主路径）
- `GateService.enable_capability(capability_id, ...)`：promotion_queue 走完后调，flip enabled=True + state="enabled"。`capability_state_writer` 失败时 raise（与 admit 不同——这里写库失败一定要 caller 知道）
- `GateDecision` frozen dataclass：decision_id / verdict / reasons / capability_id / capability_row_payload / promotion_state / rule_results dict
- `__init__.py` export `GateService` / `GateDecision` / `CapabilityWriter` / `decision_as_dict`

**关键决策**：
- **4 条独立 rule_results 而不是单一 bool**：调用方可以单独审视哪条命中，方便后续 LLM Gate（L3+）做差异化兜底。`rule_results` 字典出现在 GateDecision，让审计直接看每条 pass/fail
- **R4 self-referential 不是 reject 而是 awaiting_human_review**：自指改动可能本身就是合理的（比如 Strategist 学到自己路由有 bug），不应直接拒，而是抬人手。reject + awaiting_human_review 两种不同 verdict 让 promotion_queue 知道这条不进自动晋级流
- **`enabled=False` + `promotion_state="merged"` 是新生 capability 的默认**：Gate 只签字"合并"，但实际让 capability_router 用上要等 promotion_queue 走完 replay → shadow → canary → ready 4 阶段（promotion_queue 是后续任务）
- **`debrief.verdict == "concerning"` 不算 reject**：concerning 是"建议关注"，alarming 才是否决权。R3 设计了三档区分
- **`capability_writer` 失败吞掉 vs `capability_state_writer` 失败 raise**：admit 写新 capability 失败可重试（capability 还没生效）；enable_capability 失败必须 caller 知道（promotion_queue 状态机会卡住）。两种失败模式区别对待
- **`promotion_deadline` 14 天默认**：来自 ADR-024 与已存在的 conservative_sample_threshold methodology — capability 需要足够采样窗口（replay + shadow + canary）才能拿到统计显著信号

**20 个新单测**覆盖：approve happy path / R1 缺失 + 低 pass_rate + counts 反推 / R2 fix 缺 diag + 接受 + 缺 id / R3 alarming + low quality + concerning OK + no debrief / R4 self-referential 标人审 / writer 调用 + reject 不调 + writer 异常吞 / enable flip + writer 调用 + writer 异常 raise / promotion_deadline 默认 14 + configurable。908/908 unit tests pass，ruff clean。

**为下一步**：L2.9 methodology_distill step 真实现 —— 扫 dev_logs + seeds 生成 LayeredAsset，让 KUN 自己蒸馏开发过程产出新方法论卡片。
