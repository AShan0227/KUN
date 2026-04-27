# BATCH11 brief — 完成 BATCH9/10 剩余 + 新一波 V2.2 收尾

派给 codex. 总量 ~50-70h. 全部基于 `feat/v2.1-foundation` (现 head `946b8e6`).

跟 BATCH9/10 关系: BATCH9/10 共 16 任务, 你已完成 8 个 (4 merged: #56/#57/#59 + 4 等 rebase: #51/#52/#53/#54/#55). BATCH11 是:

1. **优先**: 处理 BATCH10 剩余 6 个未完成任务 (我已确认 C37 不需重做 — RelationshipMineStep 真实装)
2. **新增**: BATCH9/10 follow-up 7 个 + V2.2 收尾 5 个

## 第一批 — BATCH10 剩余 (优先)

### C32 ExecutionMode 加 ENSEMBLE 第 4 档 (V2.2 §21) — ~10-12h

**现状**: ExecutionMode = FAST/SMART/MAX. KUN-Lab EnsembleExecutor 只在 lab 跑 (env-gated).

**任务**:
1. `kun/api/execution_mode_classifier.py` 加 `"ENSEMBLE"` 字面量 + 决策规则:
   - SoulFile.execution_mode_preference 加 `always_ensemble_kinds: list[str]`
   - `risk_level=critical AND user_can_wait=True` → ENSEMBLE
   - `complexity_score > 0.9 AND estimated_cost > approval_threshold * 0.8` → ENSEMBLE
2. `kun/engineering/orchestrator.py` 见到 mode=ENSEMBLE → 走 EnsembleExecutor (Wire 19/20 心脏) 替单 LLM
3. 用户体验: ENSEMBLE 走 5 path → multi_judge 选最优 → 给用户 winner + "5 个方案对比" 可展开
4. 测试: classifier 决策规则 8-10 + orchestrator 集成 4-5

依赖: Wire 20 LLMRouterEnsembleAdapter 现成. **注意 Wire 25 已加 lab hint layer 5 (ExecutionMode classifier 查 LabRecipeRegistry), ENSEMBLE 字面量加在它之前生效.**

### C38 entity_relationships HTTP endpoint + UI — ~6-8h

**现状**: 表 + ORM + RelationshipMineStep 现成 (C37 confirmed real). #56 加了 metrics + dashboard. 但没 API 让 admin 看图.

**任务** (跟 `kun/api/blackboard.py` 同模式):
1. `GET /api/graph/relationships?source_kind=...&source_id=...&hops=1` — 列邻接 (复用 Wire 30 GraphTraversal)
2. `GET /api/graph/relationships/{id}` — 单条 detail
3. `DELETE /api/graph/relationships/{id}` — 用户删错关系 (审计权)
4. `PATCH /api/graph/relationships/{id}` — 改 confidence
5. `POST /api/graph/relationships` — 手动加 (admin)
6. WebSocket `/ws/graph/explore` — 拖拽探索图
7. 测试: TestClient 8-10

### C41 task_panorama 真用 GraphTraversal — ~6-8h

**任务**:
1. `kun/core/task_panorama.py` 加载第 N 模块时, 用 GraphTraversal 沿 anchor module 走邻接
2. 跟 ExecutionMode 联动: SMART 模式只走 1 跳, MAX 模式走 2 跳, ENSEMBLE 模式走 3 跳
3. 测试 6-8

依赖: GraphTraversal (Wire 30) 现成 + task_panorama 现有.

### C42 strategy_matcher 真用 GraphTraversal — ~5-7h

**任务**:
1. 找 strategy 时, 拿当前 task 的 anchor strategy → GraphTraversal 走 `transfer_confidence` 关系拉相邻 strategy (任务簇 A → B 经验迁移)
2. transfer_confidence 关系应由 KnowledgePrecipitation.WeightTuneStep 写入 (现 stub, 你这次实装)
3. 测试 5-7

### C43 input_translator wire 到 chat REST + WS — ~8-10h

**现状**: `kun/interface/input_translator.py` 完整 (含 Magika 集成), 但 chat.py + ws.py 没接.

**任务**:
1. `kun/api/chat.py` ChatRequest 加 `attachments: list[Attachment]` 可选
   - Attachment: filename + content_b64 + content_type (optional)
2. RealWorldTranslator.detect → 推荐 handler → 决定路由
   - text → 走现有 chat
   - file (pdf/docx) → reader 解析 → 拼进 user message
   - image → OCR (or 拒绝)
   - video/audio → 拒绝 (V2.2 范围外)
3. WS `/ws` 加 `binary_frame` handler — receive_bytes → translator.detect
4. 测试 8-10

依赖: input_translator 现成.

### C44 incident_response 接通 — ~4-6h

**任务**:
1. watchtower rule "guard.budget.exceeded" + "security.cross_tenant_attempt" 触发 → IncidentResponse.trigger(severity, payload)
2. IncidentResponse 5 步: detect → contain → eradicate → recover → lesson
3. 加 idle_batch step 周期跑 lessons learned distillation
4. 测试 5-7

依赖: incident_response.py 现成.

---

## 第二批 — BATCH9/10 follow-up (中优先)

### C45 LabRecipeRegistry persistence — ~4-5h

Wire 25 LabRecipeRegistry 是 in-memory. 进程重启清零. 接 alembic + 复用 Wire 29B SqlCursorStorage 的模式.

### C46 hermes prompt template versioning — ~4-5h

Wire 29A `_LAB_STRATEGY_PROMPT_HINT` 是硬编码 dict. 应该:
1. 加 `kun/datamodel/prompt_template.py` schema (template_id + version + content)
2. alembic 0014_prompt_templates 表
3. lab recipe 推过来时存进 templates 表 (替 in-memory dict)
4. hermes _build_request 查表

### C47 Lab benchmark 跑 historical task → recipe replay — ~5-7h

`kun lab benchmark` 跑预设 dataset. 加: `kun lab benchmark replay --task-id <id>` — 拉 task 历史, 重跑 ensemble 看 winner_strategy 跟原 task 是否一致.

---

## 第三批 — V2.2 收尾 + 新方向 (低优先, 等用户拍板)

### C48 真 user dogfood demo — ~6-10h

跑真实任务 (用 KUN_LAB_MODE=1 + KUN_LAB_BRIDGE_ENABLED=1) 看完整闭环 working:
1. 跑 5 种典型任务 (写作 / 决策 / 编程 / 分析 / 创意)
2. 收集 1 周 lab 推送的 recipe
3. 写 demo 报告: lab 学到了啥, ExecutionMode classifier 怎么用 lab hint
4. 这是给用户 dogfood 验证.

### C49 整合性测试 — ~6-8h

加 `tests/integration/test_v22_full_loop.py`:
- 启动真 install_runtime (in-memory)
- 跑 task → orchestrator → hermes → execution → done
- 验 verification + lab recipe + mempalace 全跑通
- 用 SqliteStubProvider 不真调 LLM

### C50 PROMISES.md auto-generator — ~3-4h

我每次手写 PROMISES.md Z.X 节. 应该自动: 从 git log + commit message 抽 Wire 编号 + 描述, 自动 append 进 PROMISES.md.

---

## 排期建议

按 ROI:
1. **第 1 周**: C32 (ENSEMBLE 第 4 档, 用户最关心) + C38 (HTTP API)
2. **第 2 周**: C41 / C42 / C43 (心脏外围, 我跟你不会冲突)
3. **第 3 周**: C44 + 第二批 follow-up (C45/C46/C47)
4. **第 4 周**: 第三批 (C48 dogfood / C49 integration / C50 auto-promises) — 等用户对 V2.3 / dogfood 拍板再启动.

## 重要约束

1. **rebase 守纪**: 每次 push 前先 rebase feat/v2.1-foundation 到最新, 防 stacked PR base 失效 (#51-#55 经验)
2. **commit 前 4 step**: ruff format + ruff check + mypy + pytest. 我自己之前漏 format 让你 PR 挂, 抱歉.
3. **不动 Wire 19-37 接口** (kun/lab/* / kun/context/graph_traversal.py /
   ImportanceScorer.score_anchor_then_expand / kun/api/execution_mode_classifier.py /
   StructuredStepGenerator with consistency_checker / Orchestrator with verification_runner)
4. **C37 不重做** — codex audit 后确认 RelationshipMineStep 已真实装, 不是 stub. 谢

## 当前 PR 状态 (供 rebase 参考)

| PR | base | 状态 | 行动 |
|----|------|------|------|
| #51 C36 alembic | feat/v2.1-foundation | UNKNOWN (rebase 后变 BEHIND) | rebase + push |
| #52 C35 Grafana provision | feat/v2.1-foundation | ruff format check fail | `ruff format kun tests` + push |
| #53 C29 ExperimentLog DB | codex/batch9-c36 (无效) | base 失效 | 改 base 到 feat/v2.1-foundation + rebase |
| #54 C30 lab CLI | codex/batch9-c29 | 等 #53 | base 改成 #53 新 branch |
| #55 C31 lab HTTP API | codex/batch9-c29 | 等 #53 | 同上 |
| #58 C34 jury consistency | feat/v2.1-foundation | CONFLICTING (跟 Wire 35) | 看下面 conflict 提示 |

### #58 conflict 解决提示

我 Wire 35 给 StructuredStepGenerator 加了 `consistency_checker` 注入参数 + Inference-Time Rethinking loop. 你的 C34 加 `check_with_jury` 应该作为 `ThoughtActionConsistency` 的 LLM judge **注入到现有 checker**, 而不是改 generator. 流程:

```python
# Wire 35 现有
class StructuredStepGenerator:
    def __init__(self, llm_router, *, consistency_checker=None, max_rethinks=2):
        self._consistency = consistency_checker  # ThoughtActionConsistency

class ThoughtActionConsistency:
    def __init__(self, *, consistency_threshold=0.5, llm_judge=None):
        self._llm_judge = llm_judge  # ← C34 的 jury 装这

    async def check(self, step):
        # 启发式低 → 调 self._llm_judge → max(heuristic, llm_score)

# 你的 C34 修改:
# 加 jury_judge factory: 接 router + 5 judges → 包装成 ThoughtActionConsistency.llm_judge 协议
# install_runtime: ThoughtActionConsistency(llm_judge=jury_judge_factory(get_router()))
```

这样 Wire 35 retry loop + C34 multi-judge 自然链上.

---

谢谢 codex 这一轮的工作 (4 PR + 5 PR rebase + C37 audit). 持续推 ⚡
