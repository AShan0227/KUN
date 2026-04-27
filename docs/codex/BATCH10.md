# BATCH10 brief — V2.2 §20 知识图谱真消费 + 心脏外围补全

派给 codex. 总量 ~50-70h. 全部基于 `feat/v2.1-foundation` (现 head `12baacb`).

跟 BATCH9 关系: BATCH9 是 KUN-Lab 周边补全 (8 任务, ~50-70h). BATCH10
是 V2.2 §20 知识图谱真消费 + 心脏外围 wire 缺口 (8 任务, ~50-70h). **可以
跟 BATCH9 并行做** (codebase 两条线无重叠).

---

## 背景 — Claude 这一轮做完的范围 (Wire 30 + 之前)

Wire 19-29 完成了 KUN-Lab 完整闭环 (V2.2 §26).
Wire 30 完成了 V2.2 §20 mempalace 真闭环:

- `kun/context/graph_traversal.py` GraphTraversal — entity_relationships 表 BFS 邻接
- `ImportanceScorer.score_anchor_then_expand` 加 graph_traversal 参数,
  expand 真沿 path 走 (mempalace 精髓)
- 13 测试, 1156/1156 全过

但 Wire 30 只把"读"端打通 (沿 graph 找邻居). **写"端 + 用端 + 可视化端**
还没接, BATCH10 补.

---

## C37 — RelationshipMineStep 真接入数据 (~6-8h)

**现状**: `kun/engineering/precipitation.py` 有 `RelationshipMineStep` 类
存在, 但实装 (`precipitate`) 是 stub — 没真扫 events / 没真挖关系入
`entity_relationships` 表.

**任务**:
1. `precipitate(event)` 真实装 (跟 NarrativeDistillStep 同模式):
   - 拉过去 24h 的 task.done / task.step.completed events
   - co-occurrence 挖掘: 同一 task 内出现的 (skill, asset) → produced_by
   - temporal correlation: skill_X 后 60s 内 skill_Y → co_occurs (lag_hours=1)
   - 重复模式 ≥3 次才 confidence=0.7, ≥10 次升 0.9
2. 写入: 调 `kun/core/orm.py: EntityRelationshipRow` upsert
3. 测试: 8-10 个 (mock events + 验 relationship row 真写入)

**依赖**: alembic 0012 现成. ORM `EntityRelationshipRow` 现成.

---

## C38 — entity_relationships HTTP endpoint + UI (~6-8h)

**现状**: 表存在, 但没 API 让用户/admin 看图. mempalace 是黑盒.

**任务**:
1. `GET /api/graph/relationships?source_kind=...&source_id=...&hops=1` — 列邻接
2. `GET /api/graph/relationships/{id}` — 单条 detail
3. `DELETE /api/graph/relationships/{id}` — 用户删错关系 (审计权)
4. `PATCH /api/graph/relationships/{id}` — 改 confidence
5. `POST /api/graph/relationships` — 手动加 (admin)
6. WebSocket `/ws/graph/explore` — 拖拽探索图
7. 测试: TestClient 8-10

**依赖**: GraphTraversal (Wire 30) + EntityRelationshipRow ORM.

---

## C39 — Knowledge Graph Grafana dashboard (~3-4h)

**现状**: 关系表数据没可视化. ops 看不到图增长 / 关系类型分布.

**任务**:
1. 加 Prometheus metrics in `kun/core/metrics.py`:
   - `kun_relationships_total{relation_type}` — gauge
   - `kun_relationships_confidence_p50{relation_type}` — gauge
   - `kun_graph_traversal_neighbors_count` — histogram (每次 traversal 拿到几个邻居)
   - `kun_relationship_mine_step_throughput` — counter (RelationshipMineStep 周期跑的关系数)
2. `kun/infra/grafana-dashboard-knowledge-graph.json` 6 panel:
   - 关系总数 (by type) timeseries
   - confidence p50 by type
   - 增长率 (rate of new relationships per day)
   - top 10 most-connected entities
   - traversal 邻居数 histogram
3. RelationshipMineStep + GraphTraversal 加触发点
4. 测试: 4-5 (dashboard JSON sanity + metric emit)

**依赖**: Wire 28 lab dashboard 同模式.

---

## C40 — kun lab benchmark 子命令 + 数据集 (~10-12h)

**现状**: `kun lab run` 跑单实验. 但没 benchmark suite — 不能批量跑同一题
比较多 strategy 胜率.

**任务**:
1. `kun lab benchmark suite/<dataset_name>` CLI:
   - 加载 dataset (yaml/jsonl in `data/lab_benchmarks/<name>.yaml`)
   - 跑 N 次 ensemble (per item × paths)
   - 输出 strategy 胜率排行表
2. 内置 3 个 benchmark dataset:
   - `marketing_copy.yaml` 20 题 (广告文案任务)
   - `code_refactor.yaml` 20 题 (代码重构任务)
   - `decision_analysis.yaml` 15 题 (决策分析)
3. `kun lab benchmark report --dataset xxx` — 历史 benchmark 报告
4. Prometheus metric: `kun_lab_benchmark_run_total{dataset}` + `_winrate{dataset, strategy}`
5. 测试: 5-7 (mock invoker 跑小 dataset)

**依赖**: KUN-Lab Wire 19-29 完整 (现成).

---

## C41 — task_panorama 真用 GraphTraversal (~6-8h)

**现状**: `kun/core/task_panorama.py` 是 V2.1 §20 12 个 panorama 模块的
聚合层. mempalace (Wire 30) 加了, 但 panorama 没用 — 还是按 score 加载.

**任务**:
1. panorama 加载第 N 个模块时, 用 GraphTraversal 沿 anchor module 走邻接
2. e.g. anchor=task.context_pack → 邻接拉 capability_card / soul_file
3. 跟 ExecutionMode 联动: SMART 模式只走 1 跳, MAX 模式走 2 跳
4. 测试: 6-8

**依赖**: GraphTraversal (Wire 30) + task_panorama 现有.

---

## C42 — strategy_matcher 真用 GraphTraversal (~5-7h)

**现状**: `kun/core/strategy_matcher.py` line 299 TODO "wire by Claude".
StrategyMatcher 现在按相似度找 strategy, 没用 graph 关系.

**任务**:
1. 找 strategy 时, 拿当前 task 的 anchor strategy → GraphTraversal 走
   `transfer_confidence` 关系拉相邻 strategy (任务簇 A → 任务簇 B 经验迁移)
2. transfer_confidence 关系由 KnowledgePrecipitation.WeightTuneStep 写入
3. 测试: 5-7

**依赖**: GraphTraversal + WeightTuneStep (现成 stub, C37 实装后会写真数据).

---

## C43 — input_translator wire 到 chat REST + WS (~8-10h)

**现状**: `kun/interface/input_translator.py` 完整 (含 Magika 集成), 但
chat.py + ws.py 没接 — 用户上传文件 / 图片 / 二进制 走不了 Magika 检测.

**任务**:
1. `kun/api/chat.py` ChatRequest 加 `attachments: list[Attachment]` 可选
   - Attachment: filename + content_b64 + content_type (optional)
2. 拿 RealWorldTranslator.detect → 推荐 handler → 决定路由
   - text → 走现有 chat
   - file (pdf/docx/etc) → 走 reader 解析 → 拼进 user message
   - image → 走 OCR (or 拒绝 if no OCR)
   - video/audio → 拒绝 (V2.2 范围外)
3. WS `/ws` 加 `binary_frame` handler — receive_bytes → translator.detect
4. 测试: 8-10

**依赖**: input_translator 现成 (Magika).

---

## C44 — incident_response wire (~4-6h)

**现状**: `kun/security/incident_response.py` line 209 TODO "wire by
Claude". 类存在但 watchtower 没真触发它.

**任务**:
1. watchtower rule "guard.budget.exceeded" + "security.cross_tenant_attempt"
   触发时 → IncidentResponse.trigger(severity, payload)
2. IncidentResponse 5 步: detect → contain → eradicate → recover → lesson
3. 加 idle_batch step 周期跑 lessons learned distillation
4. 测试: 5-7

**依赖**: incident_response.py 现成接口.

---

## 排期建议

按依赖关系:
- 第 1 批 (并行, 全独立): C37 / C39 / C40 / C43
- 第 2 批 (依赖 C37): C38 (HTTP) / C41 (panorama) / C42 (strategy)
- 第 3 批 (独立): C44

**全部基于 feat/v2.1-foundation, 不动心脏代码 (Wire 19-30 已有接口). CI 全绿后开 PR 让 Claude 审 + 决策合并. 跟 BATCH9 可并行.**

---

## 关键约束 (跟之前 BATCH 一样)

1. Claude 不抢 codex 周边模块工作
2. codex 不动 Wire 19-30 已有接口 (kun/lab/* / kun/context/graph_traversal.py /
   ImportanceScorer.score_anchor_then_expand / kun/api/execution_mode_classifier.py)
3. 任何 schema 改 (alembic) 要先看现有 0012, 不破坏现有 RLS / index
4. 任何 metrics 改 (kun/core/metrics.py) 加 new metric 不删旧的
