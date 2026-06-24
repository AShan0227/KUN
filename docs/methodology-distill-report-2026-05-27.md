# Methodology Distill 报告

**Date**: 2026-05-27

## 数字汇总

- Scanned: 271 candidates
- Novel: 192
- Duplicates skipped: 79
- Source files: 11

## 按阶段分组 (section count)

- **Heuristics Extracted**: 15 candidates
- **L1.1**: 3 candidates
- **L1.2**: 5 candidates
- **L1.3**: 4 candidates
- **L1.4**: 3 candidates
- **L1.6**: 3 candidates
- **L2.1**: 4 candidates
- **L2.2**: 5 candidates
- **L2.3**: 4 candidates
- **L2.4**: 7 candidates
- **L2.5**: 5 candidates
- **L2.6**: 4 candidates
- **L2.7**: 6 candidates
- **L2.8**: 6 candidates
- **L2.9**: 7 candidates
- **L3.1**: 5 candidates
- **L3.2**: 5 candidates
- **L3.3**: 4 candidates
- **L3.4**: 7 candidates
- **L3.5**: 7 candidates
- **L3.6**: 6 candidates
- **L4.1**: 5 candidates
- **L4.2**: 5 candidates
- **L4.3**: 6 candidates
- **L4.4**: 6 candidates
- **L4.5**: 9 candidates
- **L4.6**: 7 candidates
- **L5.1**: 7 candidates
- **L5.2**: 7 candidates
- **L5.3**: 7 candidates
- **L5.4**: 6 candidates
- **L5.5**: 5 candidates
- **Methodology Card Candidates` / `### 关键决策` 五种 header 形式**: 7 candidates

## 完整候选清单 (按阶段)

### Heuristics Extracted

- topic
- 加进 EntityKind = Literal[...] 字面量
- 加进 _PREFIX dict
- noise_floor_ge2_signals_for_engineering_check（candidate，本阶段不入库
- self_referential_use_human_review_not_reject（candidate，本阶段不入库）
- escalation_path_accumulate_not_replace（candidate）
- source_of_truth_centralization_for_shared_predicates（candidate）
- fan_out_not_assign（candidate）
- reservation_pattern_prevent_race（candidate）
- 文档先行
- ADR 按依赖顺序写（先被引用的 → 后引用别人的）
- 主文档不完全重写，加 §0 修订摘要 + 退役标记
- 每对相关 ADR 配对 commit
- 全部 ADR 落定后才动 code
- 主体内容保留供历史引用

### L1.1

- 用 typing.Protocol 而非 abc.ABC
- governance 模块不只是 docstring stub
- 每个 base.py / governance 模块 docstring 明确指 ADR-020/021/022/023/024 + 实施路径标签（L1.2 / L1.3 / L2 等），让后续子任务有清晰目标

### L1.2

- 5 commit 而非 1 mega
- 不留旧位置 shim
- router.py 改名 role_router.py
- 同时迁 multi_judge.py 跟 validation.py
- logger / tracer name 同步更新

### L1.3

- schema 约束放 DB 层而非纯 application 层
- RLS 一并加，不留"以后再加"
- metadata 列改用 ORM attribute capability_metadata
- part index 而非 full index

### L1.4

- 审计可能误判，要交叉验证
- 重命名 > 合并
- 批量重命名用 sed

### L1.6

- 不改 decide() 改 invoke()
- 保守阈值
- _apply_capability_adjustment 是私有方法

### L2.1

- 工程化优先（规则 + 阈值）vs LLM 判断
- Per-tenant state in-memory
- Dedup key tenant_id
- 回调式 emitter 而非直接写 DB

### L2.2

- 工程化规则 + 关键词命中 ≥ LLM
- scope_expansion 关键词独立成强信号
- clarification 阈值 ≥ 1 goal token
- "and / plus" 不进 scope_expansion 模板
- out_of_scope 优先于 clarification

### L2.3

- 双阈值 OR 触发
- on_idle_tick 单独 API
- derive_action 显式分级而不是 verdict 直接当 action
- emitter 异常吞掉 + log.warning

### L2.4

- 三个 sub-commit 串行
- tier="cheap" 不是 "local" 新档
- supports_tools=False
- JSON-first 解析 + 文本兜底
- asyncio.Semaphore(2) 默认
- profiles
- KUN_EXTERNAL_SUPERVISOR_ENABLED=false 默认

### L2.5

- 单工程化 signal 不足以判自嗨
- GateAdvisory.recommended_action 优先 LLM 提供值
- DebriefRecord.recommended_capability_writeback 是 dict 不是结构化对象
- evidence_quality_score 工程化打分
- L2.5 不接 NATS / 不写 evidence_ledger 表

### L2.6

- L0 keywords 不含原子 "spec"
- narrow_scope 权重 evidence > path > keyword
- narrow_scope_module_focus 只在 1-3 个模块时触发
- 强制升级不降级 L0/L1

### L2.7

- fallback_provider 是 aggressive 的硬依赖
- rollback_on 必须配齐
- sampling_rate 按 mode 阶梯
- replace(c, requires_human_review=True) 用 dataclasses.replace
- emitter exception 吞掉 + log
- anomaly_kind 未知返回空 list 而不是抛

### L2.8

- 4 条独立 rule_results 而不是单一 bool
- R4 self-referential 不是 reject 而是 awaiting_human_review
- enabled=False + promotion_state="merged" 是新生 capability 的默认
- debrief.verdict == "concerning" 不算 reject
- capability_writer 失败吞掉 vs capability_state_writer 失败 raise
- promotion_deadline 14 天默认

### L2.9

- _BULLET_RE 抽 - /  / 编号列表；_SECTION_END_RE 三种边界（## 标题 /
- existing_seed_topics(seeds_root) 读所有 .yaml 抽 topic
- _candidate_overlaps_existing 双层去重
- distill(...) 主入口
- 改 kun/engineering/idle_batch.MethodologyDistillStep 调真 distill
- 改 idle_batch module docstring 把 methodology_distill 从 STUB 标记移到 ✅
- 实战 smoke

### L3.1

- context 改动属 L2 (module) 而不是 L1 (activation)
- Aggressive 必带 preserve_top_pins=True
- Performance shadow 100%
- dedup_key 用 llm.context 而非 llm.router
- threshold-based acceptance 而非固定常数

### L3.2

- min_samples=4 默认起点
- Aggressive task_type_split target_level=0 而不是 1
- Performance 跨模块改 capability_router 而不是 skill 本身
- acceptance 动态绑 failure_rate
- failure_rate >= 0.8 自动 priority=high

### L3.3

- 24h 是 backward lookback 默认起点
- backward rollout_mode="direct" 而不是 canary
- history_reader 是依赖注入，不是 import
- _emit_and_adjust 共用让 backward 也走自指限制检查

### L3.4

- repeat_count 跨 dedup_ttl 累计而非每 TTL 重置
- repeat_count >= 3 强制 strong 不论 priority
- escalation_path 是 list 而非单 level
- failure_rate 直接进 severity 决策
- escalation 是 pure module 而非 SupervisorService method
- is_self_referential 字段独立暴露
- import escalation 在 _build_request 内（局部 import）

### L3.5

- 集中 source of truth 是 L3.5 的核心
- 保留 thin wrapper 而非 grep 替换调用点
- 强制 target_level=0 是真正的"强化"
- 写 row 而非 reject
- human_approval_token 不验签只透传到 log
- metadata_lookup 可选注入
- R4 reason 区分两种命中

### L3.6

- 不"为达到 ≥3 而硬塞 caller"
- NotificationLayer 用 dict-shape 而非 Notification 对象
- _safe_notify 独立 helper
- Gate notification 在 awaiting_human_review 推, 不在 reject 推
- Supervisor 触发条件用 "human" in escalation_path
- GuardPolicy 退役 = 减少未来困惑

### L4.1

- 过滤层 ≠ 生成层
- experimental 预留 mode 不在默认 enabled
- 非法 mode 名静默丢弃 + 全非法时回默认
- frozenset 作 config value
- TYPE_CHECKING import

### L4.2

- fan-out 而非分配
- dedup_key 命名空间通过 instance 隔离自然实现
- return_exceptions=True + log 异常
- 未知 event_type 不创建 instance
- Pool 不持锁等 emitter / LLM 调用

### L4.3

- 每 mode 独立 budget 是核心
- temperature per mode
- provider_factory 而非每 mode 必传 provider
- factory 失败 fallback 到 shared 而非 raise
- 未知 mode 用默认 entry
- 不重写 Mode A/B 的 wrapper

### L4.4

- 不用 embedding model 做 dedup
- threshold=0.7 比 ADR §合议 写的 0.85 宽
- 合并时保留 acceptance 严 + conservative
- rank_candidates 4 维 tuple key
- deliberate 不调 cluster
- lazy import in service.py

### L4.5

- 双预算 (token + experiment) 而非单一指标
- per-tenant 独立 state
- 预扣 estimated_tokens 而非仅事后记
- record_token_usage 单独 API
- asyncio.Lock 全局而非 per-tenant
- tenant_id="default" 在 Strategist 调用
- Quota check 在 emit 前 / dedup 后
- 被拒 candidate 不写出
- window_seconds=0 抛 ValueError

### L4.6

- record_success 清整个 signature 失败历史而非递减
- top-5 change_spec keys
- 不算 change_spec 的 dict/list 字段
- filter 在 quota 之前
- record_failure 不调 check
- window 24h 默认
- per-tenant isolation

### L5.1

- 3 条规则按优先级 + Member 不重用
- Cluster 自带 dedup_key
- triggered_by="anomaly_cluster" 字段
- severity=strong + escalation_path=[role, task, gate]
- recent_emitted_requests 滑动 buffer (limit 20)
- 不在 observe 主路径同步 cluster_to_search_request
- member_request_ids 用 set 不丢序但 sorted

### L5.2

- evaluate_capability_timeout 是 pure 函数
- stale 和 expired 两档语义不同
- ready state stale 不算 reaudit
- iso 字符串 timestamp 容错
- reader 失败返回 error 报告但不抛
- state_writer / search_emitter 失败 swallow
- promotion_state="awaiting_human_review" 加入 PromotionState Literal

### L5.3

- 纯 pure functions + classification dataclass
- urgent 不分 cluster vs expired
- promotion_stale 在 high 而非 urgent
- 同 tier 内 FIFO 而非 LIFO
- boosted 字段在 classification
- split_by_tier 4 桶都返回 (即使空)
- 不在 prioritize_requests 输出里去重

### L5.4

- enrich 是 pure 函数
- target_level_hint 字段而非直接改 priority/severity
- explorer_mode_hint 是 hint 不是 constraint
- _ACTION_TO_LEVEL_HINT 字典就近 module-level
- 只追加 is_root_cause=True 的 evidence
- 不 mutate input dict

### L5.5

- diagnostic_runner 默认 None
- fail-graceful in _enrich_cluster_requests
- 每个 cluster request 独立 diagnose
- symptom 由 anomaly_kind + target_module 拼装
- 不在 propose_candidates 也接 RCDH

### Methodology Card Candidates` / `### 关键决策` 五种 header 形式

- title 切
- 只剥 //  ，保留下划线
- ...
- header 兼容 colon 在  内或外
- dedup 单 token 共现不算重叠
- YAML 解析失败仅 warning，不让 distill 崩
- idle_batch step 的 stub→真做切换

