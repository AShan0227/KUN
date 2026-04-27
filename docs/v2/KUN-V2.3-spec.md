# KUN V2.3 产品方案

> **创建**: 2026-04-27
> **作者**: Claude (心脏 / wire) + 用户 (产品决策)
> **状态**: 草稿, 等用户 review 拍板后启动开发
> **基础**: V2.2 (Wire 1-37 + codex BATCH7-12 共 ~30 PR, 完成度 ~98%)

---

## 0. V2.3 一句话定位

> **V2.2 让 KUN 能干活, V2.3 让 KUN 自己变得更会干活 — 通过"启 + 鲲" 双子系统的协议涌现, 反复探索沉淀出 KUN 专属"标准说明书".**

---

## 1. 为什么 V2.3 (V2.2 audit 之后的反思)

### 1.1 V2.2 的成就

V2.2 把 KUN 做成了"功能完整的 agent 系统":
- LLM 大脑 + 规则边界 + Skill 工具箱 + 门禁权限 + 质检 + 反馈学习
- §19-§28 共 10 章节 ~98% 实装
- 1302+ 测试全过

### 1.2 V2.2 的不足 (用户原话)

> "我看了整个流程图, 我们并没有突破的差异化"

**事实**: 现 KUN 跟 LangChain / AutoGen / CrewAI / Claude Code / Devin 在**结构上没本质区别** — 都是 agent 框架标配的 LLM + 工具 + 规则 + 反馈.

**KUN 已有的"潜在差异化"** (但都不算颠覆):
- Hermes 5 action_type 强制结构化
- 启时间隔离 + 自我探索
- Lab → 主仓库参数闭环
- MarginalROI 按需扩展
- Mempalace 沿 graph 走
- 6 维 reward
- 4 档 ExecutionMode

### 1.3 V2.3 要找的真差异化

**3 条**:
1. **协议涌现** — 启反复探索, 涌现 KUN 专属"标准说明书". 这是 IP.
2. **Predictive Coding 实时校正** — 每一步预测 + 校正, 不是"做完再总结". 用户体感"它越用越懂我".
3. **Pheromone 行为涌现** — 规则不是手写, 是从行为里自然涌现. 跟蚁群一样.

**还要**: 鲲保证稳定 100% 利用, 启 100% 探索 — 别家通常是"探索 + 利用混在一起".

---

## 2. V2.3 核心架构

### 2.1 启 + 鲲 双子系统

```
┌─────────────────────────────────────────────────┐
│ 鲲 (KUN, 主, 用户日常用, 100% 稳定)              │
│ - 跑生产 task                                    │
│ - 100% 走启验证过的最佳协议                      │
│ - hook 实时预测 + 记录 prediction_error         │
│                                                 │
│ load (启 export):                                │
│ - protocol.yaml         ← KUN 怎么干活 标准说明书 │
│ - prediction_model      ← Predictive Coding 模型 │
│ - skill_pheromone       ← 蚁群涌现的 skill 偏好  │
└────────────────┬────────────────────────────────┘
                 │
        ↑ load   │   ↓ error data 回流
                 │
┌────────────────▼────────────────────────────────┐
│ 启 (Qi, 实验室模式, 用户偶尔启动, 100% 探索)      │
│ - 触发: cron 定时 / kun qi start                  │
│ - 时间窗口内 active, 窗外强制 off                 │
│ - 烧成本探索 + 训练:                              │
│   - Darwin Gödel 自我探索                         │
│   - HEX ENSEMBLE 多 path                          │
│   - Predictive Coding 训练                        │
│   - Pheromone 涌现                                │
│   - 5% 非最佳路径测试                             │
│   - AI Scientist v2 树搜索                        │
│                                                 │
│ 输出 (经过 N 次验证后下放鲲):                      │
│ - protocol.yaml                                  │
│ - prediction_model.json                          │
│ - skill_pheromone.json                           │
└─────────────────────────────────────────────────┘
```

### 2.2 关键边界

| 维度 | 鲲 | 启 |
|------|----|-----|
| **谁用** | 用户日常 | 用户 (你) 偶尔启动 |
| **稳定性** | 100% (绝不冒险) | 不要求 (烧成本试) |
| **探索 vs 利用** | 100% 利用 | 100% 探索 |
| **触发** | 用户请求 | cron / `kun qi start` |
| **预算** | 用户单 task | 日级总预算 (启专属) |
| **代码** | `kun/` | `kun/qi/` (子模块, 共享核心) |
| **数据库** | 共用主 schema | 共用 (但表 prefix `qi_*`) |
| **协议** | 只 load 不 write | write protocol/model/pheromone |
| **后续** | 跑出 prediction_error 数据回流给启 | 重训 + 输出新版 |

### 2.3 协议下放规则

启输出的 protocol/model/pheromone **不会自动**进鲲:

| 阶段 | 验证条件 |
|------|----------|
| 1. 启实验 | 启窗口内跑 ≥10 次 |
| 2. 启验证 | win_rate ≥ 0.7 + cost 在预算内 |
| 3. shadow 阶段 | 启窗口结束时, 主仓库 shadow 模式跑 (旁路, 不影响实际答案) |
| 4. canary 阶段 | shadow 通过后, 5% 流量灰度 (观察 1 周) |
| 5. stable 阶段 | canary 数据好, 100% 流量 |

**用户最高权限**: 任何阶段可以一键 rollback.

跟 V2.1 §16.6 GuardPolicy 一致, 但多了"启实验 + 启验证" 前置.

---

## 3. 协议 (Protocol) — V2.3 核心资产

### 3.1 是什么

**协议 = KUN 怎么干特定类型任务的标准说明书**.

不是规则 (硬编码 if/else), 不是 prompt (LLM 自由说话), 是**结构化的最佳策略包**.

**示例** (假设的 protocol.yaml):
```yaml
protocol_id: writing.creative.short_form
version: 1.2.0
created_at: 2026-05-15T03:00:00Z
created_by: qi (run-id qr-xxx)
validated_in_canary: 2026-05-22 (1 week, 5% traffic, win_rate=0.84)

trigger:
  task_type_pattern: "writing.creative.*"
  complexity_score: [0.3, 0.7]

execution:
  mode: SMART
  llm_strategy: tier_top_low_temp
  max_steps: 3
  expected_cost_usd: 0.08
  expected_duration_sec: 18

skill_chain:
  - skill: research.web_fetch
    when: "context_lacks_facts"
    timeout: 10s
  - skill: writing.creative_polish
    when: "always"
    fallback: writing.basic
  - skill: writing.review_self_critique
    when: "complexity > 0.5"

hermes_template:
  system_prompt_addon: "[Lab-validated] Take a contrarian view first."
  action_type_preference: [diverse_perspective, chain_of_thought]

verification:
  - kind: char_count_min
    spec: {min: 30}
    required: true
  - kind: lint_pass
    spec: {checker: "writing-grammar"}
    required: false

reward_weights:  # 6 维 reward 的本任务专属权重
  cost: 0.1
  latency: 0.1
  quality: 0.5
  user_satisfaction: 0.2
  reuse_potential: 0.05
  contribution: 0.05

a_b_pairing:  # 跟下个版本对比
  challenger_protocol: writing.creative.short_form@1.3.0-canary
  ratio: 0.05
```

### 3.2 跟现 LabRecipeRegistry 的区别

| 维度 | LabRecipeRegistry (V2.2) | ProtocolRegistry (V2.3) |
|------|-------------------------|------------------------|
| 粒度 | 单 (task_type, target_module) → strategy | 完整任务执行模板 |
| 字段 | strategy + win_rate + confidence | 完整 (trigger / execution / skill_chain / hermes / verification / reward / a_b) |
| 版本 | 无 | semantic version + history |
| A/B | 无 | challenger 内置 |
| 用途 | classifier hint | classifier + skill selector + hermes + verification 全用 |
| 来源 | 启的 strategy 推升 | 启的多轮探索 + 多 dimension 沉淀 |

### 3.3 协议 lifecycle

```
启 探索 (≥10 runs)
  ↓ win_rate ≥ 0.7
启 输出 protocol.yaml@x.y.0-experimental
  ↓ shadow 阶段 (主仓库旁路跑, 不影响答案)
protocol@x.y.0-shadow (1 周)
  ↓ shadow 数据好
protocol@x.y.0-canary (5% 流量, 1 周)
  ↓ canary 数据好
protocol@x.y.0-stable (100% 流量, 现行)
  ↓ 启又跑出 protocol@x.y+1.0
循环
```

### 3.4 协议存储

- **DB 表 `protocols`**: protocol_id (PK) + version (PK) + status (experimental/shadow/canary/stable/rolled_back) + content (JSONB) + created_at + validated_at + rollback_at
- **alembic migration 0016_protocols** (V2.3)
- **API**:
  - GET /api/protocols/{id} — 当前 stable 版
  - GET /api/protocols/{id}/history — 所有版本
  - POST /api/protocols/{id}/rollback — 回退到上一稳定版
- **CLI**:
  - `kun protocol list`
  - `kun protocol show <id>@<version>`
  - `kun protocol promote <id>@<version> stable`
  - `kun protocol rollback <id>`

### 3.5 鲲怎么消费协议

orchestrator 在 task 启动时:
1. 拿 task_meta.task_type → 查 ProtocolRegistry 找匹配 protocol (按 trigger 规则)
2. 加载 protocol → 替/补:
   - ExecutionMode (用 protocol.execution.mode)
   - hermes prompt 注入 (用 protocol.hermes_template)
   - skill_selector 优先级 (用 protocol.skill_chain)
   - verification specs (用 protocol.verification)
3. 跑 task
4. 完成后回填实际 cost / latency / quality → 启 (供 prediction_error)

**没匹配协议**: 走默认行为 (现 V2.2 行为). 协议是增强, 不是必需.

---

## 4. 启 V3 — 实验室模式工程化

### 4.1 时间窗口 + 触发

**SoulFile 加 qi_window 字段**:
```yaml
qi_window:
  start_hour: 2  # 凌晨 2 点
  end_hour: 5    # 5 点
  weekdays: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
  timezone: "Asia/Shanghai"
```

**启动逻辑**:
- cron 每分钟 check `is_qi_window_active(now)` → True 则启动启 (如果还没在跑)
- 用户手动: `kun qi start --duration 30min` (强制启 30 分钟, 即使在窗外)
- 窗口结束: 自动跑 `qi.shutdown()` — 总结 + export protocol/model/pheromone + emit event

**强制 off 守门**:
- 启的所有入口加 `_assert_qi_active()` check
- 即使 env `KUN_QI_MODE=1` 也得 `qi_window_active() or manual_override`
- 防误开烧钱

### 4.2 预算

**日级总预算** (新):
- SoulFile 加 `qi_daily_budget_usd: 5.0` (默认 $5/day)
- 启每次跑实验前 check 今日累计 cost vs budget
- 超 → 启自动暂停, emit `qi.budget_exhausted` event

**单实验级** (现 Wire 27 cost cap, 保留):
- EnsembleConfig.cost_budget_total_usd 单次实验上限

### 4.3 多轮探索 (Darwin Gödel)

启窗口内的"一轮探索":
1. **生成假设** (LLM): "如果用 chain_of_thought 跑 writing.creative.* 任务, 比 tier_top_low_temp 强"
2. **设计实验** (engineer): 拉 20 道历史 writing.creative 任务, 跑 ensemble 双对照
3. **跑实验**: 跑 20 × 2 path = 40 次
4. **评估结果** (multi_judge): 对比胜率
5. **生成下一假设** (基于结果): "好, 那 chain_of_thought + diverse_perspective 双 system prompt 是不是更强?"
6. **跑下一轮** (递归)

**预算上限触发**:
- 单轮预算 (e.g. $1)
- 总预算 (e.g. $5/day)
- 时间预算 (启窗口剩余时间)
任一触发 → 停 + 总结.

### 4.4 AI Scientist v2 树搜索

Darwin Gödel 是线性 (一轮接一轮), AI Scientist v2 是**树搜索** (多分枝并行):
- root: 用户问题
- 每个 node: 一个假设 + 实验 + 结果
- 子 node: 基于父结果的新假设
- 树深度上限 (e.g. 5 层)
- 树宽度上限 (e.g. 每层 3 分枝)
- value-guided expansion: 优先扩高 reward 的分枝

**用途**: 找最佳协议时, 树搜索比线性效率高.

### 4.5 5% 非最佳路径测试

启窗口内, 5% 实验**故意走非最佳路径**:
- 例如: 已知 strategy A win_rate 90%, 5% 实验跑 strategy B (win_rate 70%)
- 看 B 在新场景下是否反超
- 防"探索停滞"

---

## 5. Predictive Coding (脑科学) — 鲲实时校正

### 5.1 是什么

人脑不是"被动等结果". 大脑总在**预测下一秒**, 错了 (prediction error) 才更新模型.

**KUN 类比**:
- 每个 step 启动前: 算 expected_outcome / expected_cost / expected_quality
- step 完成: 算 actual 三个值
- prediction_error = actual - expected
- error 立即写入预测模型 (不是凌晨 batch)
- 下个 step 启动时, 用累积 error 调整 expected → 越来越准

### 5.2 跟传统反馈的区别

| 传统反馈 (V2.2 现状) | Predictive Coding (V2.3) |
|---------------------|--------------------------|
| step 完成才记 outcome | step **启动前** 已经预测 |
| outcome 只用于 capability_card | error 实时影响下个 step 预测 |
| 凌晨 batch 学 | step 级实时学 (用模型预测, 模型本身慢学) |
| 用户体感: 跟之前一样 | 用户体感: KUN 越用越懂我 |

### 5.3 工程拆分 (插件式 hook)

**鲲 (主) — 提供 hook 接口**:

```python
class Orchestrator:
    def __init__(self, ..., prediction_provider=None, model_updater=None):
        self._predictor = prediction_provider  # 默认 None, 不影响行为
        self._updater = model_updater

    async def run_step(self, step):
        # pre_step hook
        expected = await self._predictor.predict(step) if self._predictor else None

        actual = await self._execute_step(step)

        # post_step hook
        if self._updater and expected:
            error = compute_error(expected, actual)
            await self._updater.record(step, expected, actual, error)
```

**没装 plugin = 鲲完全不变**. 装了 plugin 才有预测.

**启 — 训练 + 输出 model**:
- 启窗口内跑大量探索, 累积 (state, expected, actual, error) 数据
- 训一个**轻量 predictor** (简单 regression / lookup table — 不用大 LLM)
- export `prediction_model.json`

**闭环**:
```
启 周期跑
  ├── 探索 + 训练
  ├── 输出 prediction_model.json
  └── 输出 protocol.yaml
                ↓
鲲 启动时 load
  ├── prediction_provider = load(prediction_model.json)
  ├── 跑生产 task
  ├── hook 实时预测 + 记录 (expected, actual, error)
  └── 周期 export error 数据回启
                ↑
启 下次窗口
  ├── 拿新 error 数据重训
  └── 输出新 prediction_model.json
```

### 5.4 用户体感

- KUN 启动时间: +1-2 秒 (load model)
- 跑 task: 决策更准 (cost 估更准, quality 估更准 → ValueGate 决策更准)
- 长期: 预测精度持续提升, 用户感觉"它越来越懂我了"

---

## 6. Pheromone (生物群体) — 行为涌现规则

### 6.1 是什么

蚂蚁找最短路: 不靠中央指挥. 每只蚂蚁随机走, 走过留信息素, 后蚁跟信息素强的走. 多次迭代后, 最短路自然涌现.

**KUN 类比**:
- entity_relationships graph 加 `pheromone_strength` 字段
- 每次 task 走过某 path → pheromone +0.1
- 时间衰减: 每天自动 ×0.95 (没人走的路慢慢被遗忘)
- ImportanceScorer / GraphTraversal 选邻居时, 按 **pheromone × confidence** (不是 confidence 单维)
- Skill 调度同样: 多 task 都走 reader→writer→reviewer 链 → 这条 chain pheromone 强 → 下次 LLM 直接被 hint 走

### 6.2 跟手写规则的区别

| 手写规则 (V2.2 现状) | Pheromone 涌现 (V2.3) |
|---------------------|----------------------|
| 工程师写 if/else: "writing 任务 → 用 writing skill" | 多 task 实际走过 → 自动累积 |
| 规则修改要 PR + review | 规则随行为自动调整 |
| 新场景没规则 → fallback default | 新场景多人走过 → 自然涌现规则 |
| 老场景规则永远不变 | 没人走的规则自动衰减消失 |

### 6.3 工程化

**alembic migration 0017_pheromone**:
```sql
ALTER TABLE entity_relationships
  ADD COLUMN pheromone_strength FLOAT DEFAULT 0.0,
  ADD COLUMN last_reinforced_at TIMESTAMPTZ;
```

**写入 hook** (鲲跑生产 task 时):
```python
async def post_step_pheromone(step, prior_step):
    if prior_step is None:
        return
    # 加强 prior_step.skill → step.skill 的边
    edge = await get_or_create_edge(
        source=("skill", prior_step.skill),
        target=("skill", step.skill),
        relation_type="follows",
    )
    edge.pheromone_strength = min(1.0, edge.pheromone_strength + 0.05)
    edge.last_reinforced_at = now()
    await save(edge)
```

**衰减 cron** (每日凌晨):
```python
async def daily_decay():
    await db.execute("""
        UPDATE entity_relationships
        SET pheromone_strength = pheromone_strength * 0.95
        WHERE pheromone_strength > 0.01;
    """)
```

**消费** (GraphTraversal 选邻居):
```python
def neighbor_score(neighbor):
    return neighbor.confidence * (0.5 + neighbor.pheromone_strength)
    # confidence 是基础, pheromone 加成 (0.5-1.5x)
```

### 6.4 启 vs 鲲分工

- **鲲**: 写入 pheromone (生产 task 走过路径)
- **启**: 验证 pheromone 涌现是否合理 (跑 A/B: 走 pheromone 强 vs 弱的 path 哪个赢)
- **协议**: 启把 pheromone 涌现的优秀链路 codify 进 protocol.skill_chain

### 6.5 跟 Darwin Gödel 联动

启的探索就是"放出新蚂蚁". 启窗口内:
1. 启 5% 故意走低 pheromone path (探索新路径)
2. 如果发现胜率高 → 加强 pheromone
3. 鲲下次自然走新路径

---

## 7. AntiGamingDetector — 反作弊套路库

### 7.1 已知作弊套路

LLM 容易"耍滑头". 已知套路:

| 套路 | 描述 | 检测方法 |
|------|------|----------|
| 答非所问 | LLM 写一堆别的, 把用户问题忽略 | 答案跟 prompt 关键词重合度 < 30% |
| 复制 prompt | 把用户的 prompt 当答案 | string similarity > 80% |
| 假数据 | 编一个"看起来对"的数字 | verification (跑测试) + 数据来源声明 |
| 跳 step | 该 4 步走 2 步 | 比对 protocol.execution.skill_chain.length |
| 抄上轮答案 | 任务变了答案没变 | 跟 prior_outputs 相似度 |
| 假装答了 | "我已经处理好了" 但没真做 | 没 produced asset / 没 skill 调用 trace |
| 超 spec | 用了 protocol 没允许的 skill | protocol.allowlist check |

### 7.2 检测时机

| 时机 | 套路检测 | 命中后 |
|------|---------|--------|
| 每个 step 完成 | 答非所问 / 复制 prompt / 抄上轮 | 拒绝 step + emit `gaming.detected` event + 走 rethink |
| Task 完成前 (verification 之前) | 跳 step / 假装答了 / 超 spec | mark task failed |
| 启探索时 | 全部套路都跑 (启自己也可能作弊) | 该次实验作废, 不计入 stats |

### 7.3 跟 jury / verification 的区别

| 组件 | 角色 |
|------|------|
| **AntiGamingDetector** | quick check 已知套路, **命中直接拒**, 不劳烦 jury (省钱 + 快) |
| **jury (multi-judge)** | 综合判断答案质量, **耗 LLM call** |
| **verification** | 跑确定性测试 (e.g. pytest pass) |

3 个一起用: AntiGamingDetector 第 1 道 (cheap), verification 第 2 道 (确定性), jury 第 3 道 (深度).

### 7.4 工程化

```python
# kun/security/anti_gaming.py
class AntiGamingDetector:
    GAMING_PATTERNS = [
        AnswerOffTopic(threshold=0.3),
        CopyPrompt(threshold=0.8),
        FakeData(needs_verification_kinds=["test_pass", "url_check"]),
        SkipStep(needs_protocol=True),
        CopyPriorAnswer(threshold=0.85),
        FakeCompletion(needs_assets_or_traces=True),
        OverSpec(needs_protocol=True),
    ]

    async def check(self, step, context) -> GamingFinding | None:
        for pattern in self.GAMING_PATTERNS:
            finding = await pattern.check(step, context)
            if finding:
                return finding
        return None
```

---

## 8. L1-L5 缺口补齐

### 8.1 L1 反馈快闭环 — 跟 Predictive Coding 联动

**问题**: 凌晨 batch 学, 用户体感不到"边干边学".

**V2.3 解决**: Predictive Coding hook 实时记录 prediction_error → 立即调整 expected. 用户做完事 5 秒后, 下个 step 的预测就基于最新 error.

### 8.2 L2 真用户反馈接入

**问题**: 现 KUN 主要靠 machine-judge. 用户 👍/👎 没强 wire 到决策.

**V2.3 解决**:
- 加 `kun/api/feedback.py` HTTP endpoint:
  - POST /api/tasks/{id}/feedback {"rating": 1-5, "comment": "..."}
- 用户反馈直接 emit `user.feedback.received` event
- 启窗口内拉 feedback events → 调整 protocol reward_weights
- 跟 Predictive Coding 联动: 用户反馈也算一种 actual (替补 machine-judge)

### 8.3 L3 Verification 默认模板

**问题**: verification spec 靠 LLM 自己出, LLM 偷懒不写就漏检.

**V2.3 解决**:
- 加 `kun/datamodel/verification_templates.py`:
  - `WRITING_TEMPLATE`: char_count_min + grammar_pass
  - `CODING_TEMPLATE`: pytest_pass + lint_pass + type_check
  - `DECISION_TEMPLATE`: contains_pros_cons + confidence_score
  - `RESEARCH_TEMPLATE`: source_cited + factual_check
- 按 task_type 自动加 default template
- LLM 可以补 task-specific spec, 但不能减默认

### 8.4 L4 Skill 链路 graph 化 — 跟 Pheromone 联动

**问题**: 多 skill 配合的顺序靠 LLM 每次重想.

**V2.3 解决**:
- entity_relationships 加 (skill_X, follows, skill_Y) 关系 (Pheromone 写入)
- skill_selector 不只按相关性, 还按 graph 邻接走
- 强 pheromone chain → 自动推 LLM"先 X 后 Y 后 Z"

### 8.5 L5 Capability card 实时回路

**问题**: capability_card 反馈到 router 慢 (idle_batch).

**V2.3 解决**:
- capability_card writeback 加 hot path: 关键指标 (success_rate / cost) 直接更新 in-memory cache
- router.decide() 优先查 cache (≤5min new), miss 时查 DB
- 长尾数据仍 idle_batch 处理

---

## 9. 反作弊优化

### 9.1 jury 扩到 SMART (codex C34 #58 已是 MAX)

V2.2 codex #58 把 multi-judge jury 接到了 MAX 模式. SMART 模式仍是单 LLM judge.

**V2.3 决策**:
- jury 跑 5 个 judge 太贵, SMART 不适合
- 加 **lite_jury** (2 judge), 给 SMART 用
- MAX 模式仍 5 judge

### 9.2 AntiGamingDetector 接 jury

如果 AntiGamingDetector 命中 (gaming pattern), 直接拒, 不调 jury — 省 5 个 LLM call.

---

## 10. 跨学科启发收口

V2.3 采纳:
- ✅ **脑科学 Predictive Coding** (核心) — §5
- ✅ **生物群体 Pheromone** (核心) — §6
- ✅ **量子 exploration vs exploitation** — 但只在启 (§4.5), 鲲不做

不采纳:
- ❌ 生命科学 / 化学催化 / 生态位 — 比喻好但工程化弱

---

## 11. 实施计划 (V2.3 wire 排期)

### Phase 1 (Week 1-2): 基础设施
- W1: 启 V3 工程化 (时间窗口 / 日预算 / 强制 off / `qi_window_active`)
- W2: ProtocolRegistry (alembic 0016 + DB + API + CLI)

### Phase 2 (Week 3-4): 核心差异化
- W3: Predictive Coding hook (鲲 hook 接口) + 启训练 pipeline
- W4: Pheromone (alembic 0017 + 写入 hook + 衰减 cron + 消费)

### Phase 3 (Week 5-6): 反作弊 + 缺口补齐
- W5: AntiGamingDetector + lite_jury + verification 默认模板
- W6: L1/L2/L4/L5 (L3 是 W5)

### Phase 4 (Week 7-8): 启的高级探索
- W7: Darwin Gödel 多轮 + AI Scientist v2 树搜索 + 5% 非最佳
- W8: 集成测试 + dogfood + 协议下放 lifecycle

### 总工时估
- Claude (心脏 / wire / spec): ~120-180h
- codex (周边 / DB / CLI / API): ~80-120h
- 总: ~6-8 周 (跟 V2.2 节奏一致)

---

## 12. V2.3 不做什么 (明确边界)

### 12.1 鲲不做"5% 探索"
**原因**: 用户原话"鲲我们还是要尽可能保证交付稳定性的". 探索全在启.

### 12.2 协议不强制
**原因**: 协议是增强, 不是必需. 没匹配协议时鲲走 V2.2 默认行为.

### 12.3 启不对外
**原因**: 启前期只给用户 (你) 用. 后续效果好后, 启的核心能力**通过协议** 沉淀进鲲, 给客户用 — 而不是让客户直接用启.

### 12.4 跨学科不强加
**原因**: Predictive Coding + Pheromone + exploration vs exploitation 已采纳. 其他比喻不强加.

### 12.5 不做大模型微调
**原因**: V2.3 仍是"推理时优化" (inference-time), 不训大 LLM. 启训的是轻量 predictor (regression / lookup table).

### 12.6 不做多 agent 协作 (留给 V2.4+)
**原因**: V2.3 焦点是"单 KUN 越用越聪明". 多 agent (e.g. KUN-A + KUN-B 协作) 是 V2.4 起.

---

## 13. 风险评估

### 13.1 启的成本失控
**缓解**: 日级总预算 + 单实验 cost cap + 时间窗口强制 off + 命令手动 stop.

### 13.2 协议下放后用户体验回退
**缓解**: shadow 阶段先跑旁路 + canary 5% + 一键 rollback.

### 13.3 Predictive Coding model 偏置
**缓解**: 模型基于历史 error 训, 可能过拟合特定场景. 启周期重训 + A/B 验证.

### 13.4 Pheromone 强化错误路径
**缓解**: 5% 非最佳路径测试持续验证 + 衰减自动遗忘.

### 13.5 AntiGamingDetector 误伤
**缓解**: 阈值可配置 + 人工 audit log + 用户反馈通道.

### 13.6 启 + 鲲 schema 共享 → 启故障传染鲲
**缓解**: 启写入只走 `qi_*` 表前缀 + 协议下放有 shadow/canary 验证.

---

## 14. 成功指标 (V2.3 dogfood 验证)

### 14.1 数据指标
- protocol 数量: ≥10 个稳定协议
- prediction_error 中位数: 启用 PC 后下降 ≥30%
- pheromone 强度收敛: top 5 chain pheromone > 0.7
- 用户反馈 rating: 平均 ≥4.0/5
- 反作弊命中: ≥10 个真实 gaming case

### 14.2 用户体感指标 (你的主观判断)
- "KUN 越来越懂我了"
- "决策越来越快, 越来越准"
- "我做完一单, 下单立刻感觉变化"
- "kun qi 看到的探索结果有惊喜"

---

## 15. 参考

- V2.2: `docs/v2/KUN-V2.2-revisions.md`
- V2.2 audit: `docs/v2/V2.2-implementation-audit.md`
- V2.2 dogfood: `docs/ops/dogfood-checklist.md`
- V2.2 runbook: `docs/ops/runbook.md`
- 历史承诺: `docs/PROMISES.md`
- 论文启发 9 个: 用户讨论笔记 (略)

---

**修订日期**: 2026-04-27
**下一步**: 用户 review 拍板 → 启动 V2.3 开发 → BATCH13 brief 派 codex
