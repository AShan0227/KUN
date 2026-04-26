# Codex BATCH6 任务书 (V2.2 修订实装, ~80-100h)

> **背景**: V2.2 修订把"按需扩展"提升为通用范式, 把守望升级为决策投资人. 详见 `docs/v2/KUN-V2.2-revisions.md`.
>
> **角色边界**: Codex 做这 6 个**周边模块**的实装; Claude 做核心 (marginal_roi / anchor_expand 通用工具 / 守望 ValueDecisionRule + wire orchestrator / 4 个核心模块接 anchor-expand). 你不要碰 Claude 的核心, 我不会碰你的.

## 协作铁律 (BATCH6 严格执行)

1. **base 必须是 `feat/v2.1-foundation`** (BATCH4/5 已踩过坑)
   ```bash
   cd /Users/petrarain/KUN-codex
   git fetch origin
   git checkout -b feat/codex-batch6-<TASK_ID> origin/feat/v2.1-foundation
   ```
   每个 PR `--base feat/v2.1-foundation`.

2. **每个任务一个 PR**, 不要一锅推.

3. **CI 双绿才推**:
   ```bash
   uv run ruff format kun tests
   uv run ruff check kun tests
   uv run mypy kun
   uv run pytest -q                     # 跑全套 (不许 -x 提前退)
   ```

4. **不动 Claude 心脏部分** (核心 6 个文件 Claude 在做, 你别改):
   - `kun/engineering/marginal_roi.py` (Claude 写, 你别建)
   - `kun/core/anchor_expand.py` (Claude 写, 你别建)
   - `kun/watchtower/engine.py` (Claude 加 ValueDecisionRule, 你别动)
   - `kun/context/importance.py` (Claude 加 score_anchor_then_expand, 你别动)
   - `kun/context/packer.py` (Claude 加 pack_navigationally, 你别动)
   - `kun/skills/selector.py` (Claude 加 select_anchor_then_expand, 你别动)
   - `kun/engineering/multi_judge.py` (Claude 加 anchor-expand 模式, 你别动)

5. **预留接口而不是直接接** — 留 `# TODO: wire by Claude in V2.2` 注释.

## 设置

```bash
cd /Users/petrarain/KUN-codex
git fetch origin
git checkout -b feat/codex-batch6-c21 origin/feat/v2.1-foundation
cp ../KUN/.env .env
./scripts/bootstrap.sh
uv run pytest -q   # 应 ~600 全过 (foundation 已含 V2.1 + M4 持久化)
```

如果 bootstrap 后 pytest 不绿 → STOP, 报告.

---

## 6 个任务 (按优先级排, 可并行)

### C21. 三模式分级 FAST/SMART/MAX (~6-8h)

**目标**: V2.2 §21. 任务级"开几档功能"统一字段, 80% FAST + 15% SMART + 5% MAX.

**位置**:
- `kun/datamodel/task.py` 加字段
- `kun/api/execution_mode_classifier.py` 新模块

**字段**:
```python
class TaskMeta(BaseModel):
    # ... 现有字段
    execution_mode: Literal["FAST", "SMART", "MAX"] = "FAST"
    mode_override_reason: str = ""
```

**Classifier**:
```python
def classify_execution_mode(task_meta: dict, soul_file: SoulFile) -> tuple[str, str]:
    """返 (mode, reason).
    
    优先级 (高位覆盖低位):
    1. task_meta.force_mode (用户显式)
    2. risk_level=critical 或 estimated_cost > soul_file.approval_threshold_money → MAX
    3. complexity_score > 0.7 → MAX, > 0.3 → SMART
    4. 默认 FAST
    
    SoulFile.execution_mode_preference (V2.2 加):
    - default_mode (用户偏好)
    - always_max_kinds (强制 MAX 的 kind 列表)
    - always_fast_kinds (强制 FAST 的 kind 列表) — 这个最高优先级 (除 critical 外)
    """
```

**集成预留**:
```python
# TODO: orchestrator wire by Claude in V2.2
# orchestrator 用 task_meta.execution_mode 决定:
#   - panorama tier (FAST→minimal, SMART→light, MAX→full)
#   - multi_judge_review 启用与否
#   - 守望 ValueDecisionRule 启用与否
#   - ImportanceScorer max_rounds (FAST 0, SMART 1, MAX 3)
```

**SoulFile 加字段** (`kun/datamodel/soul_file.py`):
```python
class SoulFile:
    # ...
    execution_mode_preference: dict = Field(default_factory=lambda: {
        "default_mode": "FAST",
        "always_max_kinds": [],
        "always_fast_kinds": ["chitchat", "translate"],
    })
```

**注意**: SoulFile alembic 0011 已部署, 你加字段需要新 alembic 0012 (加 JSONB 扩展或单独列). 推荐: 直接放进 SoulFileRow.blob (JSONB), 不需要 schema 改动.

**单测**: ≥10 个 (3 mode 默认 + risk 强制 + complexity 阶梯 + always_max/fast kinds + force_mode 覆盖).

**验收**: `pytest tests/unit/test_execution_mode_classifier.py` 全过.

---

### C22. 知识图谱 entity_relationships 表 + RelationshipMineStep (~12-14h)

**目标**: V2.2 §20. 加关系存储 + 自动挖掘.

**位置**:
- `kun/datamodel/relationship.py` (新)
- `kun/core/orm.py` 加 EntityRelationshipRow
- `alembic/versions/0012_entity_relationships.py` (新 migration)
- `kun/engineering/precipitation.py` 加 RelationshipMineStep

**ORM**:
```python
class EntityRelationshipRow(Base):
    __tablename__ = "entity_relationships"

    relation_id: str (PK, ULID)
    tenant_id: str (PK, RLS)
    source_entity_kind: str
    source_entity_id: str
    target_entity_kind: str
    target_entity_id: str
    relation_type: str  # depends_on / mentions / verifies / contradicts / similar_to / co_occurs / produced_by / transfer_confidence
    confidence: float    # 0..1
    evidence_count: int
    metadata: dict (JSONB)
    created_at: datetime
    last_reinforced_at: datetime

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1"),
        CheckConstraint("evidence_count >= 0"),
        CheckConstraint("relation_type IN ('depends_on','mentions','verifies','contradicts','similar_to','co_occurs','produced_by','transfer_confidence')"),
        Index("ix_relationships_tenant_source", "tenant_id", "source_entity_kind", "source_entity_id"),
        Index("ix_relationships_tenant_target", "tenant_id", "target_entity_kind", "target_entity_id"),
    )
```

**Provider** (`kun/datamodel/relationship.py`):
```python
async def add_relationship(rel: EntityRelationship) -> None: ...
async def get_relationships_from(entity_kind, entity_id, tenant_id, *, relation_types=None, min_confidence=0.5) -> list[EntityRelationship]: ...
async def get_relationships_to(...) -> list[EntityRelationship]: ...
async def reinforce_relationship(...) -> None:  # evidence_count += 1, confidence 升
async def find_path(source_entity, target_entity, max_hops=3) -> list[Path] | None: ...  # BFS
```

**RelationshipMineStep** (`kun/engineering/precipitation.py` 加):
```python
class RelationshipMineStep:
    """daily 跑, 扫近 24h event_log 挖掘新关系."""
    
    source_event_type = "task.completed"
    step_kind: PrecipitationKind = "relationship_mine"
    schedule: PrecipitationSchedule = "daily"

    async def precipitate(self, event, context):
        # 1. co-occurrence 挖掘: 找近 24h 内总是一起出现的实体对
        # 2. temporal correlation: 实体 A 后总是 B (1h 内), 候选 produced_by 关系
        # 3. 关系入库, confidence 起步 0.3, evidence_count ≥3 升 0.7, ≥10 升 0.9
        ...
```

**单测**: ≥12 个 (CRUD + path finding + RelationshipMineStep + confidence 升级机制 + RLS tenant 隔离).

---

### C23. hermes 结构化执行协议 (~10-12h)

**目标**: V2.2 §22. 强制 LLM 结构化输出 (Thought / Action / Outcome / Cost / Confidence).

**位置**: `kun/engineering/execution_protocol.py` (新)

**Schema**:
```python
class ExecutionStep(BaseModel):
    step_id: int
    thought: str
    action_type: Literal["use_memory", "use_skill", "web_search", "ask_user", "direct_llm"]
    action_payload: dict
    expected_outcome: str
    confidence: float = 0.5
    cost_estimate_usd: float = 0.0
```

**Generator**:
```python
class StructuredStepGenerator:
    def __init__(self, llm_router): ...

    async def generate(self, prompt: str, context: dict, *, mode: str = "SMART") -> ExecutionStep:
        """调 LLM, 强制 JSON output schema, 返 ExecutionStep.
        
        FAST 模式: 不走结构化, 直接返 ExecutionStep(action_type="direct_llm").
        SMART/MAX: 强制 LLM JSON output (Anthropic / OpenAI 都支持 response_format).
        """
```

**Watchtower hook** (留接口给 Claude 接守望):
```python
# TODO: orchestrator + watchtower wire by Claude in V2.2
# orchestrator 调 generator.generate() → 守望 evaluate(step) → 决定 block/replace/insert/observe
```

**单测**: ≥10 个 (FAST 模式跳过结构化 / SMART 模式强制 JSON / confidence 边界 / cost_estimate 边界 / 错误 schema 容错).

---

### C24. anchor-expand 接其余 N 处 (~15-20h)

**当前实际剩余清单** (2026-04-26 更新, 已扣除已完成的):

V2.2 §19.3 原列 18 处, 已完成:
- ✅ ImportanceScorer (Claude Core 4a)
- ✅ ContextPacker (Claude Core 4c)
- ✅ SkillSelector (Claude Core 4b)
- ✅ multi_judge (Claude Core 4d)
- ✅ Panorama 模块按需展开 (codex C25 #33)
- ✅ Input Translator (codex C27 #34, 跟 anchor-expand 思路对齐)

**C24 真正剩 9 处可做** (跟 Claude 心脏 0 冲突, 大胆做):

| # | 接入点 | 代码位置 | 难度 |
|---|-------|---------|------|
| 1 | StrategyMatcher 候选枚举 | `kun/core/strategy_matcher.py:240` | M (核心决策) |
| 2 | CapabilityRouter 模型排序 | `kun/interface/llm/capability_router.py:107` | S |
| 3 | Tier 枚举 | `kun/interface/llm/strategy_router_bridge.py:127` | S |
| 4 | DiagnoseRunner findings | `kun/security/diagnose_runner.py:211` | M |
| 5 | FixPlan 生成 | `kun/security/diagnose_runner.py:275` | S |
| 6 | ExternalInfoScanner 多源 | `kun/engineering/external_scan.py:117` | M |
| 7 | idle_batch step 调度 | `kun/engineering/idle_batch.py:84` | M |
| 8 | AttentionAnchor 检查 | `kun/core/attention_anchor.py:123` | S |
| 9 | IncidentResponse 动作矩阵 | `kun/security/incident_response.py:76` | M |

**特别留意 (推到下一轮)**:
- ❌ Watchtower 规则触发 (`engine.py:114`) — Claude 计划在这里加 ValueDecisionRule 集成 (V2.2 §19.4 wire), C24 暂时跳过
- ❌ NUO action_panel + diagnose_panel — 跟 C26 重叠, C26 一起做
- ❌ KnowledgePrecipitation 分发 (`precipitation.py:107`) — RelationshipMineStep 已 wire 这里, 等 C24 时再 audit 是否还需要 anchor-expand

**通用模式** (每处接入用同一套):
```python
from kun.core.anchor_expand import AnchorExpandIterator
from kun.engineering.marginal_roi import MarginalROIStopCriterion, ValueEstimator

async def anchor_fn() -> T:
    return top_1(query)

async def expand_fn(anchor: T, prior: list[T]) -> T | None:
    return next_relevant(anchor, prior)

iterator = AnchorExpandIterator(
    anchor_fn=anchor_fn,
    expand_fn=expand_fn,
    max_rounds=3,
    stop_criterion=MarginalROIStopCriterion(...),
    value_estimator=ValueEstimator(custom_fn=...),
)
async for item in iterator:
    process(item)
```

**保留老 API**: 每个模块加新方法 (e.g. `select_anchor_then_expand`), 不替换老的 `select(top_k=...)`. 这样:
- 现有 600+ 测试不受影响
- 调用方按需用新 API
- 老 API 标 \"legacy, prefer xxx_anchor_then_expand\" docstring

**建议拆 3 个子 PR** (按依赖度排):
- **C24-a 决策类 4 处**: StrategyMatcher / CapabilityRouter / Tier 枚举 / FixPlan 生成 (~6-8h)
- **C24-b 守望/诊断类 3 处**: DiagnoseRunner findings / ExternalInfoScanner / IncidentResponse (~5-7h)
- **C24-c 调度/锚定类 2 处**: idle_batch step / AttentionAnchor (~3-4h)

每个子 PR ≥ 8 单测 (3 处 × 3 case + 1-2 边界).

**清单 (按代码位置, 每处独立改, 不互相依赖)**:

| 接入点 | 代码位置 | 改动方式 |
|-------|---------|---------|
| 5. StrategyMatcher 候选枚举 | `strategy_matcher.py:240` | enumerator 改 `AsyncIterator[Candidate]`, 配合 max_rounds |
| 6. CapabilityRouter 模型排序 | `capability_router.py:107` | rank_candidates 改 anchor 模式 |
| 7. Tier 枚举 | `strategy_router_bridge.py:127` | _enumerate_model_candidates 流式 |
| 8. DiagnoseRunner findings | `diagnose_runner.py:211` | _scope_identify 返 anchor + expand |
| 9. FixPlan 生成 | `diagnose_runner.py:275` | _generate_fix_plans 流式 |
| 10. ExternalInfoScanner 多源 | `external_scan.py:117` | scan_for_user 流式 |
| 11. idle_batch step 调度 | `idle_batch.py:84` | run_all 流式 |
| 12. AttentionAnchor 检查 | `attention_anchor.py:123` | must_check_for_decision 流式 |
| 13. Panorama 模块按需展开 | `task_panorama.py:116` | build 流式 (跟 C25 配套) |
| 14. IncidentResponse 动作矩阵 | `incident_response.py:76` | RESPONSE_MATRIX 改流式 |
| 15. Watchtower 规则触发 | `engine.py:114` | evaluate 流式 (highest severity 先) |
| 16. NUO action_panel | `action_panel.py:56` | list_pending_actions 流式 (anchor=highest risk) |
| 17. NUO diagnose_panel | (新建 panel) | 类似 |
| 18. KnowledgePrecipitation 分发 | `precipitation.py:107` | dispatch 流式 |

**通用模式** (每处接入用同一套):
```python
# Before
candidates = enumerate_all(query)
for c in candidates:
    process(c)

# After
async for candidate in AnchorExpandIterator(
    anchor_fn=lambda: top_1(query),
    expand_fn=lambda anchor, prior: next_relevant(anchor, prior),
    max_rounds=3,
):
    process(candidate)
    if marginal_roi.should_stop(values): break
```

**注意**: 14 处独立, 一个 PR 包不下. **建议拆成 3-4 个子 PR** (e.g. C24-a 决策类 5-7 / C24-b 诊断类 8-9 / C24-c 守望类 10-15-16 / C24-d 其他).

**单测**: 每处 ≥3 个 (anchor 单条 / 2 轮扩展 / max_rounds 上限). 总 ≥40 个.

---

### C25. Panorama 按需展开优化 (~6-8h)

**目标**: V2.2 §19.3 #14. 现 panorama 12 模块按 tier 一次构造 (minimal/light/medium/heavy/full), 改成流式 anchor-expand.

**位置**: `kun/core/task_panorama.py` 加 `build_anchored()` 方法

**机制**:
```python
async def build_anchored(self, task_ref) -> AsyncIterator[ModuleResult]:
    """先返 minimal 必跑模块 (intent_one_sentence + risk_summary 2 个), 调用方判断不够再 expand."""
    # Round 1: yield 2 个 minimal 模块
    # Round 2: yield risk_assessment + complexity_score 2 个 light 扩展
    # Round 3: yield multi_judge_review + cross_check 等 heavy 模块
    # 调用方根据 marginal_roi 判断停止
```

**单测**: ≥6 个 (FAST mode 1 轮 / SMART mode 2 轮 / MAX mode 3 轮 / marginal_roi 触发停).

---

### C27. 输入翻译器 / 真实世界交互层 (Magika 启发, ~8-10h, V2.2 §23 新增)

**目标**: 任何输入进 KUN 第一步都过"翻译器" — 识别类型 + 推荐处理 pipeline. 不只是文件, text 流也要分类型 (JSON/Markdown/code/SQL).

**位置**: `kun/interface/input_translator.py` (新)

**依赖**: 加 `magika>=0.6.0` 到 pyproject.toml dependencies (Google 开源, ~10MB ONNX 模型, MIT license).

**数据模型**:
```python
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, Field

InputKind = Literal[
    # text
    "plain_text", "json", "yaml", "markdown", "html", "xml", "sql", "code",
    # binary
    "pdf", "csv", "xlsx", "image_jpg", "image_png", "image_webp",
    "audio_mp3", "audio_wav", "video_mp4",
    "archive_zip", "archive_tar", "executable", "binary_unknown",
]

class InputDescriptor(BaseModel):
    kind: InputKind
    mime_type: str
    confidence: float  # 0..1
    suggested_handler: str  # skill_id / model_purpose / "ask_user" / "reject"
    content_summary: str = ""
    metadata: dict = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=datetime.utcnow)
```

**Translator**:
```python
class RealWorldTranslator:
    def __init__(self) -> None:
        from magika import Magika
        self._magika = Magika()
    
    async def detect(self, raw: bytes | str | Path) -> InputDescriptor:
        """主入口. 先 TextTypeDetector (如果是 str), 再 FileTypeDetector (Magika)."""
    
    async def detect_text_kind(self, text: str) -> InputDescriptor:
        """text 流分类: 启发式规则 (开头是 { → json, # 是 markdown, etc) + 置信度."""
    
    async def detect_file_kind(self, raw: bytes) -> InputDescriptor:
        """二进制文件: 调 Magika."""
    
    def suggest_handler(self, kind: InputKind, content: bytes) -> str:
        """按 kind 推荐 skill_id / model_purpose:
        - image_* → "vision_llm"
        - pdf → "pdf_extract" skill
        - csv → "csv_query" skill
        - audio_* / video_* → "transcribe" skill
        - executable / binary_unknown → "ask_user"
        - json / yaml / sql / code / etc → "direct_llm" (text 流)
        """
```

**ContentExtractor** (按 kind 选 extractor):
```python
class ContentExtractor:
    """提取内容摘要 (前 500 字 / 缩略图描述 / 表头 / 行列数 etc)."""
    
    async def extract_summary(self, descriptor: InputDescriptor, raw: bytes) -> str:
        # pdf → 第 1 页前 500 字
        # csv → 表头 + 前 5 行
        # image → dimensions + EXIF
        # audio → duration + sample_rate
        # text → 前 500 字
        # binary_unknown → ""
```

**配 anchor-expand** (V2.2 §19.3):
```python
class RealWorldTranslator:
    def detect_anchor_then_expand(self, raw: bytes) -> AsyncIterator[InputDescriptor]:
        """流式: anchor=fast detect, expand=extract content, expand=deep understand.
        
        Round 1: Magika fast detect (返 kind+confidence, 不读 content)
        Round 2: ContentExtractor.extract_summary (读内容摘要)
        Round 3: 调 vision/text LLM 给 deep understanding (可选, 高成本)
        ≤3 rounds, 调用方按 marginal_roi 判停
        """
```

**集成预留** (不主动 wire):
```python
# TODO: chat_handler / WS binary frame wire by Claude in V2.2
# 用户上传文件 → ws.binary frame → translator.detect → 按 suggested_handler 路由
```

**单测**: ≥12 个
- TextTypeDetector: JSON / Markdown / code / SQL / plain text 5 类各 1 测
- FileTypeDetector: pdf / png / csv / mp3 / zip / unknown 6 类用 fixture (小样本文件)
- ContentExtractor: pdf / csv / image / text 4 类
- detect_anchor_then_expand: 3 round 流式, marginal_roi 触发停止

**fixture 文件** (放 `tests/fixtures/input_samples/`):
- minimal.pdf (1 页 hello)
- minimal.png (10x10 黑色)
- minimal.csv (3 行 2 列)
- minimal.mp3 (1 秒静音)
- (optional, 可省) minimal.zip / minimal.exe

**验收**: `uv run pytest tests/unit/test_input_translator.py` 全过.

**注意**:
- magika 是 ~10MB ONNX 模型, lazy-load 第一次 detect 时加载
- 跟 V2.1/V2.2 现有架构兼容: InputDescriptor 是新数据, 不动现有 LayeredAsset
- M5 可以扩到"输出翻译器" (KUN → 真实世界格式) 和"环境感知器" (主动扫用户文件)

---

### C26. NUO action_panel + diagnose_panel anchor-expand UX (~8-10h)

**目标**: V2.2 §19.3 #16-17. NUO 前端"待审批列表"和"诊断面板"用 anchor-expand 模式 — 先显示最高风险/严重的 N=3, 用户点击 expand 看更多.

**位置**:
- `kun/api/nuo/action_panel.py` 改 list_pending_actions (anchor + cursor expand)
- `kun/api/nuo/diagnose_panel.py` (新建)
- `frontend/components/action-panel/...` 前端 expand UX

**API 改动**:
```python
@router.get("/api/nuo/actions")
async def list_pending_actions(
    expand_after: str | None = None,  # cursor: 上一轮最后一条 action_id
    max_rounds: int = 3,
):
    """anchor-expand:
    - expand_after=None → 返 top 3 highest risk
    - expand_after=<action_id> → 返下 3 条 (max_rounds 累计 ≤3)
    """
```

**前端**:
```tsx
// 第一屏 3 张卡, 底部 "查看更多 (还有 N 条)" 按钮
// 点击 → 加载下 3 张
// max_rounds=3 后 disable
```

**单测 + Playwright**: 至少 6 API + 2 e2e.

---

## 推送策略 (BATCH4/5 已 OK, 这次重申)

每完成一个任务:
```bash
cd /Users/petrarain/KUN-codex
uv run ruff format kun tests
uv run ruff check kun tests
uv run mypy kun
uv run pytest -q
git add -A   # 注意! 不要 git add 整个目录, 用 git add <specific_files>
git commit -m "feat(c21): 三模式分级"
git push origin feat/codex-batch6-c21
gh pr create --base feat/v2.1-foundation --title "BATCH6 C21: 三模式分级"
```

**绝对不能**:
- ❌ PR base 设成 main
- ❌ 不跑 ruff format
- ❌ 改 Claude 心脏 7 个文件
- ❌ 一锅推

---

## 等 Claude 先做完核心再开 BATCH6

Claude 心脏部分预计 1-2 天内完成, commit 到 foundation 后我会通知你. 你可以:

1. **现在开始**: C21 (三模式) / C23 (hermes 协议) — 这俩跟 anchor-expand 无依赖, 可以独立做
2. **等 Claude 完成后**: C22 (知识图谱, 依赖 RelationshipMineStep 跟 KnowledgePrecipitation 的 wire 一致) / C24 (anchor-expand 14 处, 依赖 anchor_expand 工具) / C25 / C26

C24 是大块 (≥4 个子 PR), 别一次性推. 一个一个独立 PR.

---

## 总工时估算 (2026-04-26 更新)

- C21: 6-8h ✅ merged (#31)
- C22: 12-14h ✅ merged (#35)
- C23: 10-12h ✅ merged (#32)
- C24: 15-20h (拆 3 子 PR, 9 处接入, 缩水 — 详见 C24 节)
- C25: 6-8h ✅ merged (#33)
- C26: 8-10h
- C27: 8-10h ✅ merged (#34)
- **剩余: 23-30h** (C21/C22/C23/C25/C27 全合, 剩 C24 + C26)

加上 BATCH5 剩余 9 个 (C12-C20 ~80-100h), codex 总待干工作 ~140-180h. 配合 Claude 30-40h 心脏部分, V2.2 完整实施总 ~170-220h.
