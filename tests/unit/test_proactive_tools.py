"""Proactive tool dispatch — keyword triggers + dispatch + prefix injection."""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.engineering.proactive_tools import (
    DEFAULT_TRIGGERS,
    ProactiveDispatch,
    ProactiveScanResult,
    load_triggers_from_yaml,
    proactive_dispatch,
)
from kun.skills.dispatcher import SkillResult, autoload_builtins


@pytest.fixture(autouse=True)
def _ensure_builtins() -> None:
    autoload_builtins()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proactive_dispatch_no_trigger_returns_empty() -> None:
    """A boring prompt with no trigger keywords should produce no dispatches."""
    result = await proactive_dispatch(prompt="解释一下二分查找")
    assert result.dispatched == []
    assert result.to_prefix_message() == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proactive_dispatch_triggers_python_exec_on_code_block() -> None:
    """A prompt with a fenced ```python block must auto-run python-exec."""
    prompt = "帮我看看这段:\n```python\nprint(2+2)\n```"
    result = await proactive_dispatch(prompt=prompt)
    skill_ids = [d.skill_id for d in result.dispatched]
    assert "python-exec" in skill_ids
    py_dispatch = next(d for d in result.dispatched if d.skill_id == "python-exec")
    assert py_dispatch.result.ok
    assert "4" in py_dispatch.result.output["stdout"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proactive_dispatch_max_cap() -> None:
    """max_dispatches caps even when many triggers match."""
    # Prompt that touches multiple triggers. Cap to 1.
    prompt = "查最新数据 + 看看 ./data.csv + 跑这段:\n```python\nprint(1)\n```"
    result = await proactive_dispatch(prompt=prompt, max_dispatches=1)
    assert len(result.dispatched) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proactive_dispatch_failed_skill_does_not_block_others() -> None:
    """If one trigger's dispatch fails (e.g. file not found), others still run."""
    # csv-query will fail (no such file); python-exec should still run
    prompt = "看看 ./does_not_exist.csv\n\n```python\nprint('alive')\n```"
    result = await proactive_dispatch(prompt=prompt)
    # python-exec should be present and successful
    py = next((d for d in result.dispatched if d.skill_id == "python-exec"), None)
    assert py is not None
    assert py.result.ok
    # csv-query may or may not be in dispatched (depending on order); if it is,
    # it must be marked as failed.
    csv = next((d for d in result.dispatched if d.skill_id == "csv-query"), None)
    if csv is not None:
        assert csv.result.ok is False


@pytest.mark.unit
def test_default_triggers_cover_critical_skills() -> None:
    """Smoke: each registered builtin is reachable from at least one trigger."""
    triggered_skill_ids = {t.skill_id for t in DEFAULT_TRIGGERS}
    assert "pdf-read" in triggered_skill_ids
    assert "csv-query" in triggered_skill_ids
    assert "python-exec" in triggered_skill_ids
    assert "web-search" in triggered_skill_ids


@pytest.mark.unit
def test_prefix_message_renders_skill_id_and_reason() -> None:
    """Prefix block injected to LLM should clearly identify what was prefetched."""
    fake_dispatch = ProactiveDispatch(
        skill_id="web-search",
        params={"query": "kun project"},
        result=SkillResult(
            skill_id="web-search",
            ok=True,
            output=[{"title": "KUN", "url": "https://example.com", "snippet": "..."}],
        ),
        trigger_reason="时效性关键词",
    )
    scan = ProactiveScanResult(dispatched=[fake_dispatch])
    rendered = scan.to_prefix_message()
    assert "web-search" in rendered
    assert "时效性关键词" in rendered
    assert "kun project" in rendered


@pytest.mark.unit
def test_prefix_message_renders_failure_branch() -> None:
    """A failed prefetch should still produce a useful LLM-readable note."""
    failed = ProactiveDispatch(
        skill_id="pdf-read",
        params={"path": "missing.pdf"},
        result=SkillResult(skill_id="pdf-read", ok=False, error="not a file"),
        trigger_reason="prompt 引用 .pdf 文件",
    )
    scan = ProactiveScanResult(dispatched=[failed])
    rendered = scan.to_prefix_message()
    assert "失败" in rendered
    assert "not a file" in rendered


# ============== Layer 2: yaml 加载 ==============


@pytest.mark.unit
def test_load_triggers_from_yaml_default_path_has_core_skills() -> None:
    """默认 yaml 文件存在时, 必须把 4 个核心触发器都加载出来."""
    triggers = load_triggers_from_yaml()
    skill_ids = {t.skill_id for t in triggers}
    assert {"pdf-read", "csv-query", "python-exec", "web-search"} <= skill_ids


@pytest.mark.unit
def test_load_triggers_from_yaml_missing_file_returns_empty(tmp_path: Path) -> None:
    """文件不存在时返回空列表, 调用方负责 fallback 到 DEFAULT_TRIGGERS."""
    fake = tmp_path / "nonexistent.yaml"
    assert load_triggers_from_yaml(fake) == []


@pytest.mark.unit
def test_load_triggers_from_yaml_skips_invalid_entries(tmp_path: Path) -> None:
    """单条坏规则不能拖垮整份 yaml — 好的还能加载."""
    bad_yaml = tmp_path / "triggers.yaml"
    bad_yaml.write_text(
        """
version: 1
triggers:
  - skill_id: web-search
    pattern: '(test)'
    extract:
      kind: search_query
      param_name: query
  - skill_id: broken-skill
    pattern: '[unclosed'
    extract:
      kind: match_group_0
      param_name: x
""",
        encoding="utf-8",
    )
    triggers = load_triggers_from_yaml(bad_yaml)
    assert len(triggers) == 1
    assert triggers[0].skill_id == "web-search"


@pytest.mark.unit
def test_load_triggers_from_yaml_garbage_returns_empty(tmp_path: Path) -> None:
    """彻底坏的 yaml 不能让进程崩 — 返回空, 由调用方走 DEFAULT_TRIGGERS."""
    junk = tmp_path / "junk.yaml"
    junk.write_text(":\n  - this is: : not yaml\n  - [", encoding="utf-8")
    assert load_triggers_from_yaml(junk) == []


# ============== Layer 3: SKILL.md auto_trigger_when ==============


@pytest.mark.unit
def test_skill_registry_match_auto_triggers_finds_starter_pack() -> None:
    """starter pack data-csv-query 自带 auto_trigger_when, 看到 .csv 就命中."""
    from kun.skills.loader import get_registry, reset_registry

    reset_registry()
    autoload_builtins()  # 同时注册 starter pack 和 builtin 的 manifest
    reg = get_registry()
    hits = reg.match_auto_triggers("帮我看看 ./sales.csv")
    skill_ids = {h[0] for h in hits}
    # data-csv-query 在 starter pack 里声明了 .csv 触发器
    assert "data-csv-query" in skill_ids


@pytest.mark.unit
def test_skill_registry_match_auto_triggers_skips_bad_pattern() -> None:
    """坏的 regex 不能让 match_auto_triggers 抛 — 静默跳过."""
    from kun.skills.loader import (
        SkillManifest,
        SkillRecord,
        get_registry,
        reset_registry,
    )

    reset_registry()
    reg = get_registry()
    bad_record = SkillRecord(
        skill_id="bogus-skill",
        manifest=SkillManifest(
            name="bogus-skill",
            description="x",
            auto_trigger_when=[{"pattern": "[unclosed"}],
        ),
        body_md="",
        spdx_license=None,
        source_path="<test>",
    )
    reg.register(bad_record)
    # 不抛, 也不命中
    assert reg.match_auto_triggers("any prompt") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_layer3_triggers_via_skill_manifest() -> None:
    """注册一个带 auto_trigger_when 的 manifest, 验证 layer 3 真把它触发出来."""
    from kun.skills.loader import (
        SkillManifest,
        SkillRecord,
        get_registry,
        reset_registry,
    )

    reset_registry()
    autoload_builtins()  # 让 csv-query executor + builtin manifest 都进 registry
    reg = get_registry()
    # 用一个 layer 1/2 yaml 没覆盖的模式 (.tsv) → layer 3 独享触发
    reg.register(
        SkillRecord(
            skill_id="csv-query",  # 复用 builtin 执行器
            manifest=SkillManifest(
                name="csv-query",
                description="csv via manifest",
                auto_trigger_when=[
                    {
                        "pattern": r"\S*\.tsv\b",
                        "extract": {
                            "kind": "match_group_0",
                            "param_name": "path",
                            "extra_params": {"sql": "SELECT * FROM data LIMIT 1"},
                        },
                    }
                ],
            ),
            body_md="",
            spdx_license=None,
            source_path="<test:override>",
        )
    )
    # triggers=[] 关掉 layer 1/2, 只让 layer 3 工作
    result = await proactive_dispatch(
        prompt="瞧瞧 ./missing.tsv",
        triggers=[],
    )
    skill_ids = [d.skill_id for d in result.dispatched]
    assert "csv-query" in skill_ids
    csv = next(d for d in result.dispatched if d.skill_id == "csv-query")
    # 触发理由应当指向 SKILL.md auto_trigger_when, 不是 yaml/keyword
    assert "SKILL.md" in csv.trigger_reason
    # tsv 文件不存在, dispatcher 会失败 — 但 trigger 已经成功命中
    assert csv.params.get("path", "").endswith(".tsv")
