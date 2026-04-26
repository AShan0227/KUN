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

**修订日期**: 2026-04-26
**修订人**: Claude (基于用户跟 GPT 第六轮深度讨论后的反馈)
