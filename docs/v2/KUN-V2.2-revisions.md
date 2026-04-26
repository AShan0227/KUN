# KUN V2.2 修订 (2026-04-26 第六轮 — 决策核心 + 按需扩展原则)

> **修订背景**: 用户在跟 GPT 深度讨论后, 抽出 5 个真有价值的升级点 (其余 5 个 GPT 提议的"缺失"在 V2.1 已实装). 这一轮把"按需扩展"提升为 KUN 通用范式, 把守望从被动监控升级成主动决策投资人.
>
> **跟 V2.1 关系**: V2.2 不替换 V2.1, 而是叠加 4 个新章节 (§19 / §20 / §21 / §22) + 修改 5 个老章节. 兼容 V2.1 已有 ~600 测试.

---

## §19 决策核心: 边际收益 + 按需扩展 (KUN 心脏, V2.2 新增)

**位置**: 主文档 §17 动态决策中枢之后, §18 全局视角注意力之前.

### 19.1 一句话定位

KUN 区别于一般 Agent 框架的本质: **不是"会做更多事", 而是"知道什么时候停, 什么时候只做最小一步"**. 这一节定义两个原则, 它们是 KUN 所有决策的元规则.

### 19.2 原则 A: 边际收益递减检测 (Marginal ROI Stop)

#### 大白话

每一步开始前, KUN 都问自己: "再花这一步的钱+时间, 任务结果会明显变好吗? 不会就停."

#### 形式化

对任意"可重复添加资源"的过程 P (拉记忆 / 多 judge 评议 / 搜索 / 多 agent 竞争 / idle-batch 子步骤), 设第 N 步的边际产出为 ΔV(N) = Value(N) - Value(N-1). 如果连续 K=2 步 ΔV < δ_marginal (可配置, 默认 0.05), 则 P **强制停止**, 即使预算允许.

```
ΔV(N) = Value(N) - Value(N-1)
if avg(ΔV(N-1), ΔV(N)) < δ_marginal:
    stop()
```

#### 应用范围 (V2.2 强制接入)

| 模块 | 现状 | 接入后 |
|------|------|--------|
| **multi_judge_review** | 固定跑 N 个 judge | 跑到第 K 个判官时, 若一致率 ≥ 0.85 且 ΔV < δ → 停 |
| **ImportanceScorer 拉记忆** | 拉 top-5 全部 | 拉到第 K 条, 若 ΔV (新条对 LLM 输出影响估值) < δ → 停 |
| **idle-batch 7 步** | 顺序跑全部 | 每步后评估, 后续步骤 ΔV 估值 < δ → 提前结束 |
| **ExternalInfoScanner** | 扫所有源 | 扫到第 K 个源, 信息 overlap > 70% → 停 |
| **多 agent 竞争方案生成** | N 个 agent 全跑 | 已生成方案多样性 < 阈值 → 停 |
| **搜索结果加深** | 固定 N 页 | 第 K+1 页结果与前 K 页 overlap → 停 |

#### 实装位置

新模块: `kun/engineering/marginal_roi.py`
```python
class MarginalROIStopCriterion:
    def __init__(self, delta_threshold: float = 0.05, window_k: int = 2): ...
    
    def should_stop(self, value_history: list[float]) -> tuple[bool, str]:
        """返 (是否停, 原因). 原因如 'marginal_below_threshold' / 'consecutive_no_improvement'."""

    def estimate_value(self, item: Any, context: dict) -> float:
        """估算单步价值 (LLM judge / 启发式 / capability_card 历史)."""
```

**接入点**: 上面 6 个模块各加 1 行 `if criterion.should_stop(values): break`.

### 19.3 原则 B: 按需扩展 / Anchor-Then-Expand (通用范式)

#### 大白话

任何"集合检索"操作 (拉记忆 / 列候选 / 选 skill / 跑 judge / 扫信息源 / 列诊断发现) 都不一次返 N 个, 而是:

1. **第 1 轮**: 返 1 个最可能的"锚点"
2. 调用方判断"够用吗?" — 够用就用, 不够进入第 2 轮
3. **第 2 轮**: 沿着锚点的关系/相似性扩展第 2-3 个
4. **总轮数 ≤ 3**, 超过强制停止

#### 形式化

```
Result = AnchorExpand(query, max_rounds=3)
  Round 1: anchor = retrieve_top_1(query)
           if caller.is_sufficient(anchor): return [anchor]
  Round 2: extension = expand_from(anchor)  # 按关系图 / 邻接 / 相似性
           if caller.is_sufficient(anchor + extension): return [anchor, extension]
  Round 3: ... (类似)
  enforce_stop_after_3()
```

#### 应用范围 (从 GPT 启发 + 我审计代码后扩展, V2.2 共 18 处)

**用户已识别 (4 处)**:
1. 记忆调用 (ImportanceScorer)
2. 知识库查询 (LayeredAsset)
3. skill 调用 (SkillSelector)
4. agent 通讯

**我审计代码后新增 (14 处)**:

| # | 场景 | 现在 | 改成 anchor-expand | 代码位置 |
|---|------|------|-------------------|---------|
| 5 | StrategyMatcher 候选枚举 | 一次列全部 | 先返 top-1, 调用方满足度低再 enumerate runners-up | `kun/core/strategy_matcher.py:240` |
| 6 | CapabilityRouter 模型排序 | 一次拉全部模型卡 | 先返最可靠模型, 冷启动模型按需加载 | `kun/interface/llm/capability_router.py:107` |
| 7 | Tier 枚举 (router) | 一次返 5 tier | 先返 purpose 推荐 tier, 备选按需 | `kun/interface/llm/strategy_router_bridge.py:127` |
| 8 | DiagnoseRunner findings | 一次返所有命中 | 先返 highest severity, 用户看不够再 expand | `kun/security/diagnose_runner.py:211` |
| 9 | FixPlan 生成 | 一次为每 finding 生成全 plan | 先 auto 类立即, user_confirm 类延迟 | `kun/security/diagnose_runner.py:275` |
| 10 | ExternalInfoScanner 多源扫描 | 遍历全部 fetcher | 先扫最高优先级, 不足按权重 expand | `kun/engineering/external_scan.py:117` |
| 11 | MultiJudge 评议 | 并发跑全部 judge | 先 1-2 个 (早期信号), 不确定再补 3-4 | `kun/engineering/multi_judge.py:57` |
| 12 | idle_batch step 调度 | 一次跑全部注册 step | 先 task_replay (数据采集), 成功再扩展 | `kun/engineering/idle_batch.py:84` |
| 13 | AttentionAnchor 检查 | 返所有匹配锚定 | 先返 highest weight (red_line), 不确定再扩展 | `kun/core/attention_anchor.py:123` |
| 14 | Panorama 模块按需展开 | 12 模块按 tier 一次构造 | 先返 minimal, 复杂度判断不够再升 tier | `kun/core/task_panorama.py:116` |
| 15 | IncidentResponse 动作矩阵 | L4 一次 8 动作并发 | 先 global_readonly, 用户审核再扩展 | `kun/security/incident_response.py:76` |
| 16 | Watchtower 规则触发 | 一次匹配全规则 | 先 highest severity, 副作用大的延迟 | `kun/watchtower/engine.py:114` |
| 17 | NUO 待审批列表 | 一次返 limit=50 | 先返最高风险 N=3, 用户逐步展开 | `kun/api/nuo/action_panel.py:56` |
| 18 | KnowledgePrecipitation 步分发 | 一次遍历全 step | 先 stats_writeback (最优先), 按 schedule expand | `kun/engineering/precipitation.py:107` |

#### 实装位置

新通用工具: `kun/core/anchor_expand.py`
```python
class AnchorExpandIterator(Generic[T]):
    """通用按需扩展迭代器."""
    def __init__(
        self,
        anchor_fn: Callable[[], Awaitable[T]],
        expand_fn: Callable[[T, list[T]], Awaitable[T | None]],
        max_rounds: int = 3,
        stop_criterion: MarginalROIStopCriterion | None = None,
    ): ...

    async def __aiter__(self) -> AsyncIterator[T]:
        yield await self.anchor_fn()
        for round_n in range(1, self.max_rounds):
            next_item = await self.expand_fn(...)
            if next_item is None: break
            yield next_item
            if self.stop_criterion and self.stop_criterion.should_stop(values): break
```

每个上面 18 个应用点改成 `async for item in AnchorExpandIterator(...): use(item)`.

### 19.4 两原则联合: 守望 = 决策投资人

#### 大白话

把 V2.1 §17 StrategyMatcher 从"router 内部用"提升为"守望主决策核心". 守望在每一步开始前调 StrategyMatcher 算 ROI, 并用 marginal_roi 判断是否停止. 守望不是"事后监控", 而是 **"投资人 — 每一步前先算账"**.

#### 形式化

```
守望主循环 (V2.2):
1. 接到 step_about_to_start 信号
2. 调 StrategyMatcher.value(action, context) → expected_value
3. 调 MarginalROIStopCriterion.should_stop(value_history) → stop?
4. 若 stop: 触发 watchtower_intervention(reason="marginal_roi_stop")
5. 若 expected_value < threshold: 触发 watchtower_intervention(reason="value_below_threshold")
6. 否则: 放行 step
```

#### 实装

`kun/watchtower/engine.py` 加新规则种类 `value_decision`:
```python
class ValueDecisionRule(BaseRule):
    """守望主决策规则: 用 strategy_matcher + marginal_roi 实时判每一步."""
    async def evaluate(self, namespace: dict) -> bool:
        ev = strategy_matcher.value(action=namespace["pending_action"], ...)
        marginal_stop = self.roi_criterion.should_stop(namespace["value_history"])
        if marginal_stop[0] or ev < self.min_value_threshold:
            return True  # fire intervention
        return False
```

V2.2 launch 时, watchtower 默认加载 1 条 `ValueDecisionRule`, 强制接到 orchestrator step loop.

### 19.5 跟 V2.1 §17 关系

| V2.1 §17 | V2.2 §19 |
|----------|----------|
| 18 决策点 + 5 维信号 | 不变, 仍是基础 |
| StrategyMatcher 在 router | 也在 watchtower (V2.2 wire) |
| 决策候选一次 enumerate | 改为 anchor-expand 模式 (V2.2 §19.3) |
| 没有"continue or stop" decision kind | V2.2 加 `marginal_stop` decision kind |

---

## §20 知识图谱 + 导航式记忆 (V2.2 新增, 合并 mempalace 思路)

**位置**: 主文档 §16 简洁化原则之后, §16.12 KnowledgePrecipitation 之前.

### 20.1 一句话定位

V2.1 LayeredAsset / capability_card / SoulFile 都是"实体卡片"独立存. V2.2 加一层"关系网" — **存"谁连到谁"**, 让 ImportanceScorer 能"沿着 path 走"而不是只"找最像的".

### 20.2 关系存储 (Knowledge Graph)

#### 数据模型

新表 `entity_relationships`:
```python
class EntityRelationshipRow(Base):
    __tablename__ = "entity_relationships"

    relation_id: str (PK, ULID)
    tenant_id: str (PK, RLS)
    source_entity_kind: str  # capability_card / asset / skill / soul_file / task / etc
    source_entity_id: str
    target_entity_kind: str
    target_entity_id: str
    relation_type: str  # depends_on / mentions / verifies / contradicts / similar_to / co_occurs / produced_by / etc
    confidence: float    # 0..1
    evidence_count: int  # 几次同模式才确认
    created_at: datetime
    last_reinforced_at: datetime
```

复合 PK: (tenant_id, relation_id), index on (tenant_id, source_entity_id, relation_type).

#### 关系类型分类

| 类型 | 例子 | 来源 |
|------|------|------|
| **depends_on** | `auth_service.py` depends_on `jwt_utils.py` | 静态分析 / import 关系 |
| **mentions** | `architecture.md` mentions `auth_service` | 全文检索 |
| **verifies** | `test_auth.py` verifies `auth_service.py` | 测试关联 |
| **contradicts** | record_A 说 X, record_B 说 not-X | 矛盾检测 (multi_judge) |
| **similar_to** | task_A.fingerprint ≈ task_B.fingerprint | embedding 距离 |
| **co_occurs** | capability_card_X 总是跟 memory_Y 一起被用 | 行为日志聚类 |
| **produced_by** | asset_X produced_by skill_Y in task_Z | 任务执行回写 |
| **transfer_confidence** | 任务簇 A 经验 → 任务簇 B (置信度 0.78) | KnowledgePrecipitation 学习 |

#### 自动构建机制

通过 KnowledgePrecipitation 的新 step `RelationshipMineStep` (V2.2 加):
- 每 daily 跑一次, 扫近 24h 行为日志 (event_log)
- 用 co-occurrence + temporal correlation 挖掘隐含关系
- 关系入库时 confidence 起步 0.3, evidence_count ≥3 升到 0.7, ≥10 升到 0.9
- 用户可在 NUO 看/改/删关系 (审计权)

### 20.3 导航式检索 (mempalace 思路落地)

#### 大白话

ImportanceScorer 不再"top-K 拉满", 而是:
1. 第一轮: 返 1 个 anchor entity (最相关)
2. 沿 anchor 的 `depends_on / mentions / similar_to` 邻接拉第 2 个 (按需)
3. 第 3 轮拉第 3 个
4. ≤3 轮强制停

这样 context 里**塞的不是 5 条相关记忆, 而是 1 条核心 + 2 条邻接** — token 省 30-50%, context 更聚焦.

#### 实装

`kun/context/importance.py` 加新方法:
```python
def score_anchor_then_expand(
    self,
    query: str,
    *,
    max_rounds: int = 3,
    relationship_hops: int = 1,  # 沿关系图最多走 1 跳
) -> AsyncIterator[tuple[ScoredItem, list[ScoredItem]]]:
    """yield (anchor, expansion_list).
    
    expansion_list 是按关系图 hop 1 收集的邻接节点, 按重要性排序.
    """
```

`kun/context/packer.py` 加按需装载:
```python
async def pack_navigationally(self, query, ...) -> AsyncIterator[ContextItem]:
    """流式 yield context items. 调用方判断够了就 break."""
```

### 20.4 关系图 + anchor-expand + marginal_roi 三件套联动

```
用户问: "怎么写登录接口"

ImportanceScorer.score_anchor_then_expand("登录接口"):
  Round 1: yield ("auth_service.py 架构图", [])
           # caller (LLM) 检查: 够吗? 不够, 我要知道 JWT 细节
  Round 2: 沿 anchor 的 depends_on 找到 jwt_utils.py
           yield ("jwt_utils.py 实现", [auth_service.py])
           # caller: 够吗? marginal_roi 评估: 第 2 条带来 ΔV=0.4, 继续
  Round 3: 沿 jwt_utils.py 的 verifies 找到 test_jwt.py
           yield ("test_jwt.py", [...])
           # caller: marginal_roi 评估: ΔV=0.05 < δ, 停
```

最终 context 装 3 条 (而不是 V2.1 的 5 条全塞), 顺序还是按"路径"流入, 不是按相似性堆.

### 20.5 跟 V2.1 §16 LayeredAsset 关系

LayeredAsset 仍然是"实体卡"主体存储. V2.2 §20 是叠加在它之上的**关系层** (类似数据库的"二级索引"). LayeredAsset 不动, 关系表独立.

---

## §21 三模式分级: FAST / SMART / MAX (V2.2 新增)

**位置**: 主文档 §5 工程化子系统 内, §5.8 涌现方案识别+切换之后.

### 21.1 一句话定位

任务也分轻重. 不是每个任务都开守望全套, **80% 任务 (FAST 模式) 直接 LLM 一刀, 5% 任务 (MAX 模式) 才开全部事前模块 + 多 agent + 多 judge**.

### 21.2 三模式定义

| 模式 | 占比目标 | 例子 | 开了啥 | 延迟目标 |
|------|---------|------|--------|---------|
| **FAST** | 80% | "翻译这句话" | 直接 LLM, 无事前模块, 不查记忆, 不开守望主动决策 | ≤200ms |
| **SMART** | 15% | "写广告 / 改这个 bug" | 守望开 + 拉 1-2 条记忆 (anchor-expand) + capability_card 选模型 + multi_judge=1 | ≤30s |
| **MAX** | 5% | "并购方案 / 关键代码架构 / 高金额操作" | 全开: panorama 12 模块 + 多 agent 竞争 + multi_judge ≥3 + 守望主动决策每一步 + 沉淀新方法论 | ≤10min |

### 21.3 模式判定 (谁来选 mode)

加新模块 `kun/api/execution_mode_classifier.py`:

```python
def classify_execution_mode(task_meta: dict, soul_file: SoulFile) -> ExecutionMode:
    """根据 4 个维度判 mode. 优先级: 高位强制覆盖低位."""
    
    # 1. 用户显式指定 (最高优先级)
    if task_meta.get("force_mode"):
        return task_meta["force_mode"]
    
    # 2. 不可逆/高金额 → 强制 MAX
    if task_meta.get("risk_level") == "critical" or task_meta.get("estimated_cost_usd", 0) > soul_file.approval_threshold_money:
        return "MAX"
    
    # 3. complexity_score 判
    if task_meta.get("complexity_score", 0) > 0.7:
        return "MAX"
    elif task_meta.get("complexity_score", 0) > 0.3:
        return "SMART"
    
    # 4. 默认 FAST
    return "FAST"
```

### 21.4 任务字段

`kun/datamodel/task.py` TaskRef.meta 加字段:
```python
execution_mode: Literal["FAST", "SMART", "MAX"] = "FAST"
mode_override_reason: str = ""  # 如果不是默认 FAST, 记录 why
```

### 21.5 跟 V2.1 关系

V2.1 已有 4 档预算 / 4 档守望 / 4 档 BudgetTracker, 但没有**任务级"开几档功能"统一档**. V2.2 §21 补这个空缺.

`execution_mode` 影响:
- panorama tier (FAST → minimal, SMART → light, MAX → full)
- multi_judge_review 启用与否 (FAST off, SMART 1 judge, MAX ≥3)
- 守望 ValueDecisionRule 启用与否 (FAST off, SMART per-step, MAX per-action)
- ImportanceScorer max_rounds (FAST 0 不查, SMART 1, MAX 3)

---

## §22 hermes 结构化执行协议 (V2.2 新增)

**位置**: 主文档 §6 Engineering execution 之后, 跟 §22 是平行 deps OODA 外层循环 (BATCH5 C11 #30 已 merged).

### 22.1 一句话定位

现在 KUN orchestrator 让 LLM 自由说一段话, 我们解析. V2.2 强制 LLM 按"一格一格"输出 (Thought / Action / Action Payload / Expected Outcome / Cost Estimate / Confidence), 让守望能在每一步精确介入 (改 / 拦截 / 替换 action).

### 22.2 ExecutionStep schema

新模块 `kun/engineering/execution_protocol.py`:
```python
class ExecutionStep(BaseModel):
    step_id: int
    thought: str  # LLM 自我陈述意图
    action_type: Literal["use_memory", "use_skill", "web_search", "ask_user", "direct_llm"]
    action_payload: dict  # 各 action 具体参数
    expected_outcome: str
    confidence: float = 0.5  # 0..1, LLM 自评
    cost_estimate_usd: float = 0.0
```

### 22.3 LLM call 改 JSON output

orchestrator step 调 LLM 时强制 structured output (Anthropic / OpenAI 都支持):
```python
response = await llm_router.call(
    request,
    response_format={"type": "json_object", "schema": ExecutionStep.model_json_schema()},
)
step = ExecutionStep.model_validate_json(response.content)
```

### 22.4 守望介入点

每个 step 出来后, 守望可以:
- **block**: confidence < 0.3 → 拒绝执行, 让 LLM 重出一个
- **replace**: action_type=use_skill 但 cost_estimate > budget_remaining → 改成 use_memory (省钱)
- **insert**: 在 use_skill 前插一个 ask_user (高风险时)
- **observe**: 仅记录 (默认)

### 22.5 跟 OODA C11 (#30) 关系

| OODA C11 (任务级) | hermes 协议 (step 级) |
|-------------------|----------------------|
| Observe→Orient→Decide→Act→Reflect→Adjust→Done | Thought→Action→Outcome 每 step 一个 |
| 任务整体跑 N 个 cycle | 任务内每 step 都是结构化 |
| 管 plan/replan 节奏 | 管 LLM-call 内部输出格式 |

垂直关系, 不冲突. OODA Act 内部跑的就是 hermes 结构化 step.

### 22.6 不适用场景

不强制对 FAST 模式生效 (FAST 直接 LLM 一刀, 不需要结构化). 仅 SMART / MAX 模式启用 hermes 协议.

---

## 修改老章节 (V2.1 → V2.2)

### §3.2 ImportanceScorer 加 anchor_then_expand 方法

V2.1 §3.2 五维打分器保持不变, 在末尾追加 §3.2a:

> **§3.2a Anchor-Then-Expand 模式 (V2.2)**
> 默认 `score()` 仍返 top-K 全部 (向后兼容). V2.2 加 `score_anchor_then_expand()` 流式接口, SMART/MAX 模式默认走这个, FAST 模式不查记忆.

### §10.6 傩诊断加 marginal ROI stopping

V2.1 §10.6 5 步管道保持不变, 在 §10.6.5 之后追加 §10.6.6:

> **§10.6.6 边际收益停止准则 (V2.2)**
> findings / fix_plans 列表用 anchor-then-expand 输出 (先 highest severity, 用户看不够再 expand). 每个 finding 的 cause_attribute 用 marginal_roi: 跑 N 个候选根因后若 ΔV < δ → 停, 不再 LLM 兜底.

### §16.12 KnowledgePrecipitation 加 RelationshipMineStep

V2.1 §16.12 4 类 step (StatsWriteback / WeightTune / RuleEmerge / NarrativeDistill) 不变, V2.2 加第 5 类:

> **RelationshipMineStep (V2.2)**: schedule="daily", 扫近 24h event_log, 用 co-occurrence + temporal correlation 挖掘 entity_relationships 表的新关系. confidence 起步 0.3, 多次确认升到 0.9.

### §17 StrategyMatcher 加 marginal_stop decision kind

V2.1 §17 18 决策点不变, V2.2 加 1 个新 decision kind:

> **decision_kind = "marginal_stop"** (V2.2): 任意"可重复添加资源"过程的停止决策. enumerator 输入 value_history, 评估"continue" vs "stop", strategy_score 公式权重: α (continue 的 expected_value), γ (continue 的 latency cost). 守望 ValueDecisionRule 默认接这个 decision kind.

### §13.6 SoulFile 加 execution_mode 偏好

V2.1 §13.6 SoulFile 字段不变, V2.2 加:

```yaml
execution_mode_preference:
  default_mode: "FAST"  # 用户默认偏好
  always_max_kinds: []  # 任务 kind 列表, 这些 kind 强制 MAX (e.g. "code.production.deploy")
  always_fast_kinds: ["chitchat", "translate"]  # 这些强制 FAST 即使 complexity 高
```

---

## V2.2 实施路线 (任务排期)

### 我自己 (Claude) 做的核心 (~30-40h)

1. **守望 = 决策投资人** wire (~12-15h)
   - `kun/engineering/marginal_roi.py` MarginalROIStopCriterion
   - `kun/watchtower/engine.py` 加 ValueDecisionRule
   - 接到 orchestrator step loop (每 step 前调)
   - +10 单测

2. **anchor_expand 通用工具** (~6-8h)
   - `kun/core/anchor_expand.py` AnchorExpandIterator
   - +8 单测

3. **接 anchor_expand 到 ImportanceScorer + LayeredAsset + SkillSelector + multi_judge** (~10-15h)
   - 4 个核心模块, 每个加 anchor-expand 方法
   - 不破坏现有 API (老方法保留, 新方法叠加)
   - +12 单测

### Codex BATCH6 (~80-100h, 6 个独立任务)

1. **C21 三模式分级 (FAST/SMART/MAX)** ~8h
2. **C22 知识图谱 + RelationshipMineStep** ~12h
3. **C23 hermes 结构化执行协议** ~10h
4. **C24 anchor-expand 接其余 14 处** (StrategyMatcher / CapabilityRouter / DiagnoseRunner / ExternalScan / etc) ~20h
5. **C25 Panorama 按需展开优化** ~6h
6. **C26 NUO action_panel + diagnose_panel anchor-expand UX** ~8h

(BATCH5 C12-C20 仍然有效, 跟 BATCH6 可以并行做)

---

## 完成度对照

| 维度 | V2.1 | V2.2 (计划) | V2.2 (我心脏部分实装后) |
|------|------|------------|------------------------|
| 决策深度 | 局部最优 (router 用 strategy_matcher) | 全局最优 (守望 = 投资人) | ⏳ |
| 资源调用智能 | 一次拉满 | 按需扩展 ≤3 轮 | ⏳ |
| 任务分级 | 4 档预算 | + 任务级 3 模式 | ⏳ |
| 执行可控 | 自由 LLM 输出 | 结构化 hermes 协议 | ⏳ |
| 知识沉淀 | 实体卡片 | + 关系网 (graph) | ⏳ |

V2.2 实施后, KUN 从 "高度结构化的执行系统" → "会做选择 / 会下注 / 会停止 / 会节奏控制" 的决策系统.

---

## §23 输入翻译器 / 真实世界交互层 (V2.2 新增, Magika 启发)

**位置**: 主文档 §15 (skill 系统) 之后, §16 (LayeredAsset) 之前.

### 23.1 一句话定位

KUN 跟真实世界 (用户上传的文件 / 外部 API / 系统输入 / WS binary frame) 之间需要一个"翻译器" — **任何输入进 KUN 第一步先识别类型 + 推荐处理 pipeline, 不能直接拿原始 bytes 给 LLM**.

借鉴: Google Magika (AI 文件类型识别), 但 KUN 把它扩展成更广义的"输入理解层".

### 23.2 大白话

现在 KUN 的问题: 用户丢一个文件进来 (PDF / CSV / 图片 / 代码 / 二进制), KUN 要么直接送 LLM (浪费 token + LLM 不一定懂), 要么靠 mime_type 简单分类 (容易错).

升级后:
- 文件进来 → **InputTranslator.detect()** → 返 InputDescriptor (kind / confidence / suggested_handler / content_summary)
- KUN 按 descriptor 决定: 用 vision LLM 看图? 用 PDF skill 提取? 用 CSV query 算? 还是直接 reject ("二进制看不懂, 请说明这是什么")
- 跟 V2.2 §19 anchor-expand 配合: 第 1 轮 detect, 第 2 轮 extract, 第 3 轮 understand

### 23.3 应用范围 (不只文件)

| 输入种类 | 现状 | V2.2 后 |
|---------|------|---------|
| **用户上传文件** (PDF/CSV/img/audio/...) | 没 detect | Magika + extractor 链 |
| **用户消息** (text) | 当 plain text | 检测 JSON/Markdown/code/SQL/HTML/纯文本, 选不同 prompt |
| **外部 API 响应** | 直接 parse | 检测 JSON/XML/HTML/text + 编码 + schema |
| **skill 输出** | 当 string | 检测是 binary/text/structured, 决定下游怎么用 |
| **WS binary frame** | 没处理 | 走 InputTranslator |
| **粘贴板内容** (用户 paste) | 当 text | 检测 code language / log / stack trace / etc |

### 23.4 数据模型

```python
class InputDescriptor(BaseModel):
    kind: Literal[
        # text 类
        "plain_text", "json", "yaml", "markdown", "html", "xml", "sql", "code",
        # 二进制类 (Magika 输出)
        "pdf", "csv", "xlsx", "image_jpg", "image_png", "image_webp",
        "audio_mp3", "audio_wav", "video_mp4",
        "archive_zip", "archive_tar",
        "executable", "binary_unknown",
    ]
    mime_type: str
    confidence: float           # Magika 给的概率
    suggested_handler: str      # skill_id / model_purpose / "ask_user" / "reject"
    content_summary: str = ""   # 前 500 字 / 缩略图描述 / 表头 etc
    metadata: dict[str, Any]    # size, encoding, pages, dimensions, etc
    detected_at: datetime
```

### 23.5 实装结构

```
RealWorldTranslator (kun/interface/input_translator.py)
├── TextTypeDetector — text 流先猜 (规则: JSON 看 {/[, code 看 def/function, etc)
├── FileTypeDetector — 调 Magika, 100+ 类型
├── ContentExtractor — 按 kind 选 extractor (pdf2text / ocr / csv2dict / transcribe)
└── HandlerSuggester — 按 kind + content 推荐 skill_id / model_purpose
```

### 23.6 配 anchor-expand (V2.2 §19.3)

```
用户上传 unknown.bin

InputTranslator.detect_anchor_then_expand(file_bytes):
  Round 1 (anchor):
    Magika fast detect → InputDescriptor(kind="image_png", confidence=0.95)
    yield descriptor
    # caller: 够吗? 我要内容, 不够
  Round 2 (extract):
    OCR + dimensions
    yield descriptor.with_content(summary="A login screen", dimensions=(1920,1080))
    # caller: 够吗? marginal_roi 评估: ΔV 大, 继续
  Round 3 (understand):
    vision LLM 描述
    yield descriptor.with_understanding(...)
```

### 23.7 跟 V2.1 关系

V2.1 没有"输入理解层". V2.2 §23 补这层 — 是真实世界 ↔ KUN 之间的"翻译器".

后续(M5)可以扩到:
- **输出翻译器** — KUN 输出 → 真实世界格式 (PDF / 图表 / 代码 / 邮件)
- **环境感知器** — 主动扫用户文件夹 / 桌面 / 浏览器历史 (得用户授权)

V2.2 §23 是基础.

---

## §28 任务边界守护 / TaskBoundaryGuard (V2.2 新增, OffTopicEval 启发)

**位置**: 主文档 §7 (intent) 之后, §10 (傩诊断) 之前 — 任务边界跟意图理解是孪生.

### 28.1 一句话定位

OffTopicEval (Lambda, ICLR 2026 Agents in the Wild) 揭示: **即使 LLM 被给了明确角色和边界, 它"几乎每次都回答不该回答的问题"**. KUN 用工程化方式对治 — 加一层 "TaskBoundaryGuard", 在 intent 阶段就检测 task 是否在 agent role 的 scope 内, 不在 → reject + 反问.

### 28.2 大白话

举例: 用户雇了一个"营销文案 agent"专门写广告. 用户突然问: "帮我修个 bug." LLM 大概率会答 (虽然超出范围), 因为它"什么都能聊". 但作为产品, 这是个**严重问题** — 用户付费雇的是营销文案 agent, 不是通用 LLM. 答了等于:
1. 浪费成本 (跑了不该跑的任务)
2. 质量风险 (营销 agent 不擅长 debug)
3. 边界混乱 (用户长期会失去对 agent 角色的认知)
4. 安全隐患 (修 bug 可能触发用户没意识到的高危操作)

KUN 加 TaskBoundaryGuard:
- intent 之后, planner 之前, 算"task 在 role scope 内的概率" (boundary_score 0-1)
- < 阈值 (默认 0.4) → reject + 反问: "我是 [营销文案 agent], 这个 bug 修复任务不在我擅长范围. 您要继续吗? 我可以转给 [coding agent] 或者您自己处理."
- 用户确认 → 强制走, 但记账 (出了问题不背锅)
- 用户取消 → 任务结束

### 28.3 跟 KUN 现有架构关系

| KUN 现有 | OffTopicEval 维度 | 升级 |
|---------|-------------------|------|
| PlanOnlyGate | 防"高危操作" (destructive) | 不防 off-topic |
| SoulFile.professional_role | 描述用户角色 | 跟 agent role 不直接关联 |
| role_template (V1 §13) | 描述 agent 角色 | 没"allowed_task_types" 字段 |
| watchtower | 规则触发 (e.g. cost 超限) | 没"task scope" 规则 |
| ValueGate | 步级 ROI | 没"任务级 in-scope" 检测 |

V2.2 §28 补这个空缺.

### 28.4 数据模型

`kun/datamodel/role_template.py` (现有, 加字段):
```python
class RoleTemplate(BaseModel):
    role_id: str
    role_name: str
    description: str
    # V2.2 §28 加
    allowed_task_types: list[str] = []  # 白名单, e.g. ["marketing.copywriting", "marketing.ad"]
    forbidden_task_types: list[str] = []  # 黑名单, e.g. ["coding.*"]
    boundary_strict_mode: bool = True  # True → boundary_score 低就 reject; False → 给警告但放行
    out_of_scope_redirect: str = ""  # off-topic 时建议转给哪个 role
```

### 28.5 TaskBoundaryGuard 实装

```python
class BoundaryDecision(BaseModel):
    in_scope: bool
    boundary_score: float  # 0..1
    reason: str  # whitelist_match / blacklist_hit / llm_judge / no_scope_defined
    suggested_redirect: str = ""  # 建议转给的 role_id

class TaskBoundaryGuard:
    """在 intent 之后, planner 之前算 task 是否在 agent role scope 内."""
    
    def __init__(self, llm_judge=None, threshold=0.4): ...
    
    async def check(
        self,
        task_meta: dict,  # 含 task_type / success_criteria_short
        role_template: RoleTemplate,
    ) -> BoundaryDecision:
        # 1. allowed_task_types 命中 → in_scope=1.0
        # 2. forbidden_task_types 命中 → in_scope=0.0
        # 3. 都没命中 → LLM judge 算 in-scope 概率
        # 4. 没 role / 没 LLM judge → 默认 in_scope=0.7 (中性放行 + log)
```

### 28.6 跟 V2.2 已有模块联动

- **跟 §21 三模式**: TaskBoundaryGuard 在 mode classifier 之前 (boundary 第一道, mode 是第二道)
- **跟 §27 ThoughtActionConsistency**: hermes 出 ExecutionStep 后, 守望可以再算 "step.action_payload 是否在 scope 内" (双层护栏)
- **跟 §17 ValueGate**: off-topic task → expected_value = 0 → escalate
- **跟 SoulFile**: 用户的 \`always_max_kinds\` 优先级 < boundary_strict_mode (boundary 是硬约束)

### 28.7 配 OffTopicEval benchmark

V2.2 §26 KUN-Lab 学习成长区可以加一个"OffTopicEval benchmark" 跑测试:
- 给 KUN 装一个特定 role agent (e.g. 营销文案)
- 喂 100 条 off-topic 问题 (e.g. 写代码 / 修 bug / 算数学)
- 统计 reject rate, target ≥ 95% (vs LLM 直接答的 ≤ 10%)

### 28.8 实施 (Wire 18, ~6-8h)

`kun/security/task_boundary_guard.py` (新):
- TaskBoundaryGuard 类
- BoundaryDecision 模型
- LLM judge factory (cheap model 一次性)
- 启发式 fallback (没 LLM 时用)

orchestrator wire (Wire 18):
- intent.interpret → IntentInterpreter (现有)
- → TaskBoundaryGuard.check (新)
- in_scope=False + boundary_strict → emit "boundary_violation" event + ws ask_user
- 用户取消 → end task; 用户强制 → 走 (记账)

测试: ≥10 个 (whitelist / blacklist / LLM judge / 无 scope / strict 模式 / redirect 建议).

---

## §27 推理时反思 + 学习成长区 (V2.2 新增, ICLR 2026 启发)

**位置**: 主文档 §22 hermes 之后, 跟 §26 KUN-Lab 联动 (KUN-Lab 的"学习成长区" 实装这个).

### 27.1 一句话定位

借鉴 **Inference-Time Rethinking** (ICLR Latent Thinking Workshop): 模型在回答前修正推理 (潜在思维向量用于数学推理). 不需要训练, 纯推理时机制.

KUN 升级: 在 SMART/MAX 模式下, **关键 step 强制走 rethinking 路径** — LLM 出 hermes ExecutionStep 后, 不立即 act, 而是先"反思 thought 跟 action 是否一致" (FaithCoT 启发), 不一致 → 重新 generate.

### 27.2 大白话

现在 KUN hermes 协议 (V2.2 §22) 让 LLM 输出 thought + action_type + expected_outcome, 但: **LLM 可能只是写得好看, 实际决策跟 thought 没关系** (FaithCoT 揭示的"模型只是会解释, 不是会思考").

升级后:
1. LLM 出第一版 ExecutionStep (thought + action)
2. KUN 守望 (ValueGate) 算一个"thought-action consistency score"
3. consistency 低 → 让 LLM 重出, 直到一致
4. 不影响 FAST 模式 (跳过, 节省 latency)

### 27.3 thought-action consistency check

实装在 `kun/engineering/execution_protocol.py`:

```python
class ThoughtActionConsistency:
    """检测 thought 跟 action 是否真一致 (不是事后解释)."""
    
    async def check(self, step: ExecutionStep) -> tuple[float, str]:
        """返 (consistency_score 0..1, reason).
        
        启发式 + LLM judge 二合:
        - 启发式: thought 含 keyword 是否对应 action_type
          (e.g. thought 含 "拉记忆" → action_type=use_memory)
        - LLM judge: cheap model 一次性判 thought 是否能推出 action
        """
```

阈值: < 0.5 → 让 LLM 重出 (max 2 次重试).

### 27.4 学习成长区 (跟 KUN-Lab §26 联动)

KUN-Lab 加新模块 "学习成长区" (Learning Lab):
- 周期跑 Inference-Time Rethinking 实验
- 收集 KUN 在不同任务上的 thought 模式
- 找出"高 consistency + 高成功率" 的 thought 模板
- 沉淀进 KnowledgePrecipitation → 推给生产 KUN 的 hermes prompt

### 27.5 跟 §22 hermes 关系

V2.2 §22 ExecutionStep 不变. 加新字段:
```python
class ExecutionStep(BaseModel):
    # 原有: step_id / thought / action_type / action_payload / expected_outcome / confidence / cost_estimate_usd
    # V2.2 §27 加
    thought_action_consistency: float = 1.0  # 默认 1.0 (FAST 跳过 check)
    rethink_count: int = 0  # 这条 step 重出过几次
```

### 27.6 怎么"省掉形式化提效率" (FaithCoT 启发)

FaithCoT 揭示: 模型可能只是写解释. 那么:
- **FAST 模式**: 完全跳过 thought (直接 action_type, 默认 "direct_llm")
- **SMART 模式**: 加 thought 但不强制 consistency check (节省 1 次 LLM call)
- **MAX 模式**: 强制 consistency check + rethinking 重出

这跟 V2.2 §21 三模式分级天然对齐.

---

## §25 信用分配 + 稀疏奖励 (V2.2 新增, RL 经典问题在 KUN 的解法)

**位置**: 主文档 §17 动态决策中枢之后, §19 决策核心之前 (跟 §19 是孪生 — §19 是"现在选啥", §25 是"过去哪些选择起作用").

### 25.1 一句话定位

KUN 长任务 (≥10 step) 现在有"稀疏奖励 + 信用模糊"的经典 RL 问题:
- **稀疏**: 只有 task_done 时给 capability_card 写 outcome (pass/fail), 中间 step 没奖励信号
- **信用模糊**: task pass 时, 信用平摊到所有 step / 资源 → 真起作用的 step 没被强化, 真拖后腿的 step 没被惩罚
- **跟注意力孤立**: 注意力 (ImportanceScorer / AttentionAnchor) 是"现在该用啥", 跟"用过这资源带来啥结果" (信用) 没耦合

V2.2 §25 把这三件事统一: **注意力 = 信用分配 = 稀疏奖励 shaping**, 三者共用同一个 contribution_history.

### 25.2 大白话

举例: 用户问"修 auth bug", KUN 跑 8 步:
- step 1-3 拉 auth 相关记忆 + 读代码 (用了 5 条记忆 / 2 个 skill / 1 个 model)
- step 4 写补丁 + 跑测试 (用了 1 个 skill + 1 个 model)
- step 5 测试 fail, 调 multi_judge (用了 3 个 judge)
- step 6 修 bug + 重测 pass (用了 1 个 model)
- step 7 写 commit msg
- step 8 done, 用户验收 pass

**现在 KUN**:
- task pass → record_outcome("model:claude-opus", outcome="pass") + record_outcome("role_template:rt-coder", outcome="pass") + record_outcome("skill:coding-pytest", outcome="pass")
- 这 3 个实体都得 +1 success
- 但: step 5 multi_judge 救场了, 没被特别记功; step 7 写 commit msg 是 routine, 也得了 +1

**升级后** (§25):
- 每 step 完成时记 StepCredit (resources_used + immediate_reward)
- task done 时反思: 哪些 step 是关键路径 (没它任务就败)? → 这些 step 用的资源得 boost credit
- record_outcome 用 credit-weighted, 不是均摊
- 下次 ImportanceScorer.score 时, 高 credit 资源自动 boost (=注意力)

### 25.3 三件套架构

```
注意力 (ImportanceScorer) ←─ contribution_history ←─ 信用分配 (CreditAssignment)
                              ↑                       ↑
                              └─── 稀疏奖励 shaping ──┘
                                  (dense intermediate reward)
```

#### 25.3.1 dense intermediate reward (中间稠密奖励)

每 step 后给一个 immediate reward (不等 task done). 信号源:

| 信号 | 来源 | 给谁加 |
|------|------|--------|
| ValueGate.expected_value 上升幅度 | V2.2 §19.4 | step 用的资源 |
| multi_judge 一致率 | §17.10 | step 调的 judge models |
| 边际收益 (marginal_roi) | V2.2 §19.2 | step 用的扩展资源 (memory/skill) |
| step 没 escalate (顺利过 ValueGate) | V2.2 §19.4 | step 配置 (mode/skill choice) |
| code execute pass / lint pass | V2.2 §24 (CodeExecutor) | code skill / language model |

dense reward 累计 → step_value_history (V2.2 已有), 加权进 contribution_history.

#### 25.3.2 step credit (信用分配)

新数据模型:
```python
class StepCredit(BaseModel):
    step_id: int
    resources_used: dict[str, list[str]]  # {"memory": [...], "skill": [...], "model": [...]}
    immediate_reward: float                # dense intermediate reward
    credit_share: dict[str, float]         # {"memory:m1": 0.3, "skill:s2": 0.5, "model:m3": 0.2}
    is_critical_path: bool = False         # 反思后判定 (task done 时填)
```

每 step 结束时填 immediate_reward + credit_share. task done 时反思填 is_critical_path.

#### 25.3.3 retrospective reward (回溯奖励)

task done 后, LLM 反思 (cheap model, 一次性, 不阻 task):
```
[反思 prompt]
任务: <task_description>
结果: <pass/fail>
步骤摘要 + 资源使用:
  step 1: 用了 memory:m1, skill:s2 → ΔV=0.10
  step 2: 用了 model:m3 → ΔV=0.05
  ...
请判断: 哪些 step 是"关键路径" (没它任务就失败)? 列 step_id 和理由.
```

LLM 输出关键路径 step_id list → 给这些 step 的 resources_used 加 boost (×1.5).

#### 25.3.4 contribution_history (跟注意力耦合)

ImportanceScorer 加新维度 (跟 V2.2 §3.2a 5 维并列):
```python
class ImportanceScore(BaseModel):
    # V2.1 5 维
    semantic / frequency / recency / dependency / pin
    # V2.2 §25 新加
    contribution_score: float = 0.0  # 历史上对成功任务的贡献度 (0..1)
```

contribution_score 算法:
- 资产 X 的历史: 在 N 个 task 里被用过, 其中 K 个 task pass, M 个 task K 是关键路径之一
- contribution_score = 0.5 * (K/N) + 0.5 * (M/N)  # 50% 命中成功 + 50% 关键路径
- 没历史 → 0.0 (新资产不加分)

ImportanceScorer.score 默认权重 加 contribution_score 维度 (5 维 → 6 维), 跟 graph_boost (Wire 7) 累加.

### 25.4 实装 (BATCH7+8 协同, ~15-20h)

新模块 `kun/engineering/credit_assignment.py`:
- StepCredit / TaskCreditReport / RetrospectiveReflector
- 跟 capability_writeback 集成 (替换均摊 → credit-weighted)
- 跟 ImportanceScorer 集成 (加 contribution_score 维度)
- 跟 ValueGate 集成 (immediate_reward 反馈给 dense reward)

orchestrator wire:
- step 完成 → fill StepCredit (resources_used + immediate_reward)
- task done → RetrospectiveReflector.reflect → 填 is_critical_path
- record_outcome 改成 credit-weighted (调用 CreditAssignment.distribute_outcome)

测试: ≥15 个 (StepCredit / RetrospectiveReflector / contribution_score 算法 / capability_writeback 集成 / ImportanceScorer 集成)

### 25.4a RewardMap 升级: stage-level dense reward (ICLR 2026 启发)

**RewardMap (ICLR 2026)** 启发: 把"一个 step 的 immediate_reward" 升级为
"**step 内部 4 子阶段各自 reward**":

```
step
├── 阶段 1 感知 (perceive): 接到任务 + 解析输入 → reward_perceive (信号: 输入是否完整)
├── 阶段 2 理解 (understand): hermes thought 出来 → reward_understand (信号: thought 是否合理)
├── 阶段 3 推理 (reason): action 选择 → reward_reason (信号: action 跟 thought 一致 — V2.2 §27)
└── 阶段 4 决策 (decide): 真执行 → reward_decide (信号: 输出质量 / cost / latency)
```

为什么重要:
- 现在 step 失败 → "整个 step 减分", 但不知道是 thought 错 / action 错 / execute 错
- 升级后 → 哪一阶段错, 哪一阶段单独减分
- 学习效率暴涨 (KUN 知道"我在哪个子阶段总错")

升级 StepCredit 数据模型:
```python
class StageReward(BaseModel):
    stage: Literal["perceive", "understand", "reason", "decide"]
    reward: float  # 0..1
    reason: str = ""

class StepCredit(BaseModel):
    # 原字段不变
    # 加:
    stage_rewards: list[StageReward] = Field(default_factory=list)  # V2.2 §25.4a 4 子阶段
```

immediate_reward = sum(stage_rewards) / 4 (默认), 或加权.

跟 §27 thought-action consistency 联动:
- 阶段 3 reason reward = ThoughtActionConsistency.check(step) score
- 阶段 4 decide reward = ValueGate.expected_value 上升幅度

### 25.5 跟现有架构关系

| V2.1/V2.2 模块 | §25 怎么改 |
|---------------|-----------|
| capability_card | record_outcome 改 credit-weighted, 不再均摊到所有用过的资源 |
| ImportanceScorer | 加 contribution_score 维度 (§3.2a 5 维 → 6 维) |
| ValueGate | expected_value 算法加 contribution_score 信号 |
| KnowledgePrecipitation | RelationshipMineStep 之外, 加 CreditMineStep (从 task_credit_history 挖掘"高贡献资源" → 升 capability score) |
| anchor-expand | ImportanceScorer.score 自动用 contribution_score, 不需要额外接 |

向后兼容: 现有 record_outcome 行为保留作 fallback, contribution_score 默认 0 (不影响老数据).

---

## §26 KUN-Lab 内测版 (V2.2 新增, HEX 启发)

**位置**: 主文档 §22 后置, 跟 §10 傩诊断系统平行 (傩是"修", Lab 是"练").

### 26.1 一句话定位

借鉴 **HEX (UCF, ICLR 2026)** — 离散扩散 LLM 测试时推理扩展, 不需要训练, 24.72% → 88.10% (3.56x). 核心思路: **同任务跑多条生成路径, 集成胜出.**

KUN-Lab 是独立内测版, 不直接服务用户, 用来:
- 跑 ensemble (同任务 N 路径) → 收集"哪种 ensemble 配方有效"
- 安全实验新 strategy / 新 prompt / 新 mode 组合
- 沉淀 (KnowledgePrecipitation 真打通) → 推给生产 KUN

### 26.2 跟生产 KUN 区别

| | 生产 KUN | KUN-Lab |
|---|---------|---------|
| 用户 | 真用户 | 我们自己 (开发) |
| 任务延迟 SLA | 严格 (FAST≤200ms) | 不限 (可跑 10min) |
| 成本预算 | 严格 (用户钱) | 我们自己 burn (实验经费) |
| Mode | FAST 80% / SMART 15% / MAX 5% | 全部 ENSEMBLE (新加) |
| LLM 调用 | 单路径 | N 路径并发 + 投票 |
| KnowledgePrecipitation | 跑用户任务时 emit | 实验任务批量 emit, 沉淀更快 |

### 26.3 ENSEMBLE 模式设计

新 ExecutionMode (V2.2 §21 加第 4 档):
```python
ExecutionMode = Literal["FAST", "SMART", "MAX", "ENSEMBLE"]
```

ENSEMBLE 模式行为:
- 每 step 跑 N 路径 (e.g. N=5):
  - 路径 1: tier=top + temp=0.1
  - 路径 2: tier=strong + temp=0.5
  - 路径 3: tier=cheap + temp=0.7
  - 路径 4: tier=top + temp=0.1 + 不同 system prompt
  - 路径 5: tier=top + temp=0.0 + chain-of-thought prefix
- 5 个输出 → multi_judge 选最优 (复用 §17.10)
- 记录每条路径的 (config, output, score) 进 lab_experiment_log

### 26.4 实施 (kun-lab 独立项目, 不在 kun 主仓库)

新仓库: `AShan0227/kun-lab` (跟 kun 同 org, 独立 repo)
- 复用 kun 主仓库作为 dependency (`pip install -e ../kun`)
- 加 `kun_lab/ensemble_executor.py` (ENSEMBLE 模式 N 路径并发)
- 加 `kun_lab/dashboard/` (前端实验对照面板)
- 加 `kun_lab/recipe_promoter.py` (有效配方写入 KnowledgePrecipitation 推主仓库)

### 26.5 何时启动

V2.2 完整实装后 (BATCH8 完成 + Claude wire 14 完成) → 启 KUN-Lab.

预计工时: ~30-50h 起步版 (ensemble executor + dashboard + recipe promoter MVP).

---

## §24 代码能力层 / CodeCapability (V2.2 新增, Karpathy 启发)

**位置**: 主文档 §15 (skill 系统) 之后, 跟 §23 (输入翻译器) 平行.

### 24.1 一句话定位

KUN 现在能"调 skill" (starter_pack 5 个 skill: coding-pytest / os-shell / data-csv-query / etc), 但**不会真正"用 code 解决问题"** — 不能读 codebase 找 bug, 不能写代码后自动 lint/test 闭环, 不能 debug 错误.

借鉴: **Andrej Karpathy ai-skills** (LLM agent 用 code 的能力), KUN 加一层 **"代码能力层"** (CodeCapability), 让 KUN 真能写/读/跑/调试 code.

### 24.2 大白话

GPT-4 / Claude 已经会写代码, 但放在 agent 框架里, 它们"写完就交差", 不:
- 跑一下看对不对
- lint 通过吗
- 有 bug 自己 debug
- 看完整 codebase 找出依赖 / 调用关系

KUN 升级后, 写代码任务自动闭环:
1. **读** — 用户说"修 auth 的 bug", KUN 先 read codebase 找 anchor file (CodeReader)
2. **写** — 写补丁 (CodeWriter)
3. **跑** — sandbox 跑测试 (CodeExecutor)
4. **调** — 测试不过 → 看错误 → 自动改 (CodeDebugger)
5. **审** — 改完静态分析 + 自审 (CodeReviewer)

整个闭环就是一次任务, 不是"写完就走".

### 24.3 5 个组件

| 组件 | 干啥 | 跟现有 KUN 关系 |
|------|------|----------------|
| **CodeReader** | 读 codebase, 理解结构 (依赖图 / call graph / 模块) | 用 starter_pack 的 os-shell + grep, 加 LLM 解释 |
| **CodeWriter** | 写代码 + 触发 lint/format 自动闭环 | 用 starter_pack 的 writing-markdown + ruff/black |
| **CodeExecutor** | sandbox 跑 code, 看输出 | 用 starter_pack 的 coding-pytest + os-shell, 加 V2.1 sandbox |
| **CodeDebugger** | 接错误日志, 分类 + 给 fix 建议 | 跟 §10.6 傩诊断 + capability_card 配合 |
| **CodeReviewer** | 静态分析 + LLM 审查 + 给 diff 评价 | 跟 ValidationPipeline + multi_judge 配合 |

### 24.4 跟 V2.2 §22 hermes 集成 (action_type 扩展)

V2.2 §22 ExecutionStep 现有 5 个 action_type (use_memory / use_skill / web_search / ask_user / direct_llm). 加 4 个 code 类:

```python
ActionType = Literal[
    "use_memory", "use_skill", "web_search", "ask_user", "direct_llm",
    # V2.2 §24 新增
    "code_read",      # CodeReader: 读 codebase
    "code_write",     # CodeWriter: 写代码 + lint
    "code_execute",   # CodeExecutor: sandbox 跑
    "code_debug",     # CodeDebugger: debug 错误
]
```

LLM 在 SMART/MAX 模式 hermes 协议输出可以是:
```json
{
  "thought": "用户要修 auth bug, 先读 auth_service.py 看现状",
  "action_type": "code_read",
  "action_payload": {"file": "kun/api/auth.py", "lines": "1-50"},
  "expected_outcome": "了解 auth 流程",
  "confidence": 0.9,
  "cost_estimate_usd": 0.001
}
```

守望 (ValueGate) 看到 action_type=code_execute + cost 高 → 可以 block / 改 action 到 code_read 先.

### 24.5 跟 anchor-expand 集成 (V2.2 §19.3)

CodeReader 完美配 anchor-expand:
1. **Round 1 anchor**: grep + LLM 找最相关 file (1 个)
2. **Round 2 expand**: 沿 import / call graph 找邻接 file (2-3 个)
3. **Round 3**: 查测试 / docs (1-2 个)

跟 V2.2 §20 知识图谱 entity_relationships 配套: 代码文件之间的 \`depends_on\` / \`mentions\` / \`verifies\` 关系自动从 code 解析填进 graph.

### 24.6 实装结构

```
kun/skills/code_capability/  (新目录)
├── reader.py         # CodeReader (anchor-expand 模式)
├── writer.py         # CodeWriter (auto lint/format/test 闭环)
├── executor.py       # CodeExecutor (sandbox)
├── debugger.py       # CodeDebugger (错误分类 + fix 建议)
├── reviewer.py       # CodeReviewer (静态分析 + multi_judge)
└── __init__.py       # CodeCapability facade

kun/engineering/execution_protocol.py  (扩展)
└── ActionType 加 4 个 code 类

ValueGate.value_estimator (扩展)
└── 看到 hermes action_type=code_execute → 算 sandbox 成本
```

### 24.7 跟 V2.1 关系

V2.1 / V2.2 现有的 starter_pack skill 不动 (向后兼容), CodeCapability 是叠加层 — 它**协调** starter_pack skill 形成闭环, 不替换.

### 24.8 实施: BATCH7 C28 起步 (~10-15h)

第一个落地任务: 实装 CodeReader + CodeExecutor (最有价值的 2 个), CodeDebugger + CodeReviewer 后续 BATCH8.

---

**修订日期**: 2026-04-26
**修订人**: Claude (基于用户跟 GPT 第六轮深度讨论 + Google Magika + Andrej Karpathy ai-skills 启发)
