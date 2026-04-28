"""Hermes prompt 接 lab recipe registry (Wire 29A).

跟 Wire 25 ExecutionMode classifier 对称 — lab 推荐 strategy 影响 hermes
system prompt, 让 lab 验证过的 chain_of_thought / diverse_perspective /
tier_top_low_temp 真改变实际推理风格.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from kun.datamodel.prompt_template import (
    HERMES_PROMPT_TARGET,
    InMemoryPromptTemplateStorage,
    PromptTemplate,
    PromptTemplateRegistry,
    get_prompt_template_registry,
    reset_prompt_template_registry,
    upsert_prompt_template_from_lab_recipe,
)
from kun.engineering.execution_protocol import (
    ExecutionStep,
    StructuredStepGenerator,
    _build_request,
    _maybe_lab_recipe_prompt_hint,
)
from kun.engineering.precipitation import AssetUpdate
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.lab import (
    LabRecipeEntry,
    get_recipe_registry,
    make_registry_apply_hook,
    reset_recipe_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_recipe_registry()
    reset_prompt_template_registry()
    yield
    reset_recipe_registry()
    reset_prompt_template_registry()


def _seed_registry(strategy: str, task_type: str = "ad_creative") -> None:
    get_recipe_registry().upsert(
        LabRecipeEntry(
            task_type=task_type,
            target_module="hermes_prompt_template",
            strategy=strategy,
            win_rate=0.85,
            confidence=0.85,
        )
    )


# ---- _maybe_lab_recipe_prompt_hint 单元 ----


def test_no_hint_when_registry_empty() -> None:
    assert _maybe_lab_recipe_prompt_hint({"task_type": "anything"}) is None


def test_no_hint_when_task_type_missing() -> None:
    _seed_registry("chain_of_thought")
    # context 没 task_type / task_kind
    assert _maybe_lab_recipe_prompt_hint({"foo": "bar"}) is None


def test_hint_picked_for_chain_of_thought() -> None:
    _seed_registry("chain_of_thought")
    hint = _maybe_lab_recipe_prompt_hint({"task_type": "ad_creative"})
    assert hint is not None
    assert "step by step" in hint.lower()
    assert "Lab-validated" in hint


def test_hint_picked_for_diverse_perspective() -> None:
    _seed_registry("diverse_perspective")
    hint = _maybe_lab_recipe_prompt_hint({"task_type": "ad_creative"})
    assert hint is not None
    assert "contrarian" in hint.lower()


def test_hint_picked_for_tier_top_low_temp() -> None:
    _seed_registry("tier_top_low_temp")
    hint = _maybe_lab_recipe_prompt_hint({"task_type": "ad_creative"})
    assert hint is not None
    assert "conservative" in hint.lower()


def test_no_hint_for_unmapped_strategy() -> None:
    """strategy 不在 _LAB_STRATEGY_PROMPT_HINT → None (不影响 hermes)."""
    _seed_registry("some_unknown_strategy")
    assert _maybe_lab_recipe_prompt_hint({"task_type": "ad_creative"}) is None


def test_no_hint_for_other_target_module() -> None:
    """registry 里有 entry 但 target_module=execution_mode_classifier → 不取 hermes."""
    get_recipe_registry().upsert(
        LabRecipeEntry(
            task_type="ad_creative",
            target_module="execution_mode_classifier",  # 不是 hermes
            strategy="chain_of_thought",
            win_rate=0.9,
            confidence=0.9,
        )
    )
    assert _maybe_lab_recipe_prompt_hint({"task_type": "ad_creative"}) is None


def test_hint_uses_task_kind_fallback() -> None:
    """没 task_type 但有 task_kind → fallback."""
    _seed_registry("chain_of_thought", task_type="biz_plan")
    hint = _maybe_lab_recipe_prompt_hint({"task_kind": "biz_plan"})
    assert hint is not None


def test_hint_prefers_task_specific_prompt_template() -> None:
    _seed_registry("chain_of_thought", task_type="biz_plan")
    get_prompt_template_registry().upsert(
        PromptTemplate(
            tenant_id="u-sylvan",
            task_type="biz_plan",
            target_module=HERMES_PROMPT_TARGET,
            strategy="chain_of_thought",
            version=2,
            source="kun_lab",
            content="[Lab-validated recipe] Use the CFO planning frame.",
        )
    )

    hint = _maybe_lab_recipe_prompt_hint({"task_type": "biz_plan"})
    assert hint == "[Lab-validated recipe] Use the CFO planning frame."


@pytest.mark.asyncio
async def test_upsert_prompt_template_from_lab_recipe_versions_template() -> None:
    storage = InMemoryPromptTemplateStorage()
    registry = PromptTemplateRegistry(storage=storage, tenant_id="u-sylvan")

    first = await upsert_prompt_template_from_lab_recipe(
        task_type="biz_plan",
        strategy="chain_of_thought",
        tenant_id="u-sylvan",
        content="[Lab-validated recipe] First custom template.",
        registry=registry,
    )
    second = await upsert_prompt_template_from_lab_recipe(
        task_type="biz_plan",
        strategy="chain_of_thought",
        tenant_id="u-sylvan",
        content="[Lab-validated recipe] Second custom template.",
        registry=registry,
    )

    assert first is not None
    assert second is not None
    assert first.version == 1
    assert second.version == 2
    picked = registry.get("biz_plan", HERMES_PROMPT_TARGET, "chain_of_thought")
    assert picked is not None
    assert picked.content.endswith("Second custom template.")
    stored = await storage.load_all("u-sylvan")
    assert len(stored) == 2
    assert sum(1 for row in stored if row.active) == 1


@pytest.mark.asyncio
async def test_lab_registry_hook_persists_hermes_prompt_template() -> None:
    storage = InMemoryPromptTemplateStorage()
    prompt_registry = get_prompt_template_registry(storage=storage, tenant_id="u-sylvan")
    registry = get_recipe_registry()
    hook = make_registry_apply_hook(registry)

    await hook(
        AssetUpdate(
            update_id="up-1",
            asset_kind="recipe",
            asset_ref=HERMES_PROMPT_TARGET,
            update_kind="promote",
            confidence=0.9,
            payload={
                "source": "kun_lab",
                "tenant_id": "u-sylvan",
                "task_type": "biz_plan",
                "strategy": "chain_of_thought",
                "win_rate": 0.86,
                "promotion_id": "promo-1",
                "prompt_template_content": "[Lab-validated recipe] Ask for constraints first.",
            },
        )
    )

    entry = registry.get("biz_plan", HERMES_PROMPT_TARGET)
    assert entry is not None
    template = prompt_registry.get("biz_plan", HERMES_PROMPT_TARGET, "chain_of_thought")
    assert template is not None
    assert template.version == 1
    assert template.content == "[Lab-validated recipe] Ask for constraints first."
    stored = await storage.load_all("u-sylvan")
    assert len(stored) == 1


# ---- _build_request 集成 ----


def test_build_request_no_lab_recipe_one_system_message() -> None:
    """没 lab recipe → messages 只有 1 个 system + 1 个 user (跟 Wire 11 行为一致)."""
    request = _build_request("hello", {"task_type": "no_recipe"}, "SMART")
    system_msgs = [m for m in request.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert "Hermes" in system_msgs[0].content


def test_build_request_with_lab_recipe_appends_extra_system() -> None:
    """有 lab recipe → messages 含 2 个 system (主 + lab hint)."""
    _seed_registry("chain_of_thought", task_type="biz_plan")
    request = _build_request("plan Q4", {"task_type": "biz_plan"}, "SMART")

    system_msgs = [m for m in request.messages if m.role == "system"]
    assert len(system_msgs) == 2
    assert "Hermes" in system_msgs[0].content
    assert "Lab-validated" in system_msgs[1].content
    assert "step by step" in system_msgs[1].content.lower()


def test_build_request_lab_hint_not_cached() -> None:
    """lab hint 是 dynamic (registry 可变), cache=False; 主 hermes prompt cache=True."""
    _seed_registry("chain_of_thought", task_type="biz_plan")
    request = _build_request("plan Q4", {"task_type": "biz_plan"}, "SMART")

    system_msgs = [m for m in request.messages if m.role == "system"]
    assert system_msgs[0].cache is True  # 主 hermes cacheable
    assert system_msgs[1].cache is False  # lab hint 不 cache (registry 会变)


def test_build_request_message_order_preserved() -> None:
    """system → lab_hint → user 顺序."""
    _seed_registry("diverse_perspective", task_type="biz_plan")
    request = _build_request("plan", {"task_type": "biz_plan"}, "SMART")
    roles = [m.role for m in request.messages]
    assert roles == ["system", "system", "user"]


# ---- StructuredStepGenerator 端到端 ----


@pytest.mark.asyncio
async def test_generator_uses_lab_hint_in_messages() -> None:
    """StructuredStepGenerator.generate 真把 lab hint 传给 LLM."""
    _seed_registry("chain_of_thought", task_type="biz_plan")

    captured_request: list[Any] = []

    fake_router = AsyncMock()

    async def fake_invoke(request, *, purpose):
        captured_request.append(request)
        # 返一个有效 ExecutionStep JSON
        step = ExecutionStep(
            step_id=1,
            thought="reasoning",
            action_type="direct_llm",
            action_payload={},
            expected_outcome="ok",
            confidence=0.8,
        )
        return LLMResponse(
            content=step.model_dump_json(),
            usage=UsageInfo(input_tokens=10, output_tokens=10),
        )

    fake_router.invoke = fake_invoke

    gen = StructuredStepGenerator(fake_router)
    await gen.generate("plan Q4", {"task_type": "biz_plan"}, mode="SMART")

    assert len(captured_request) == 1
    system_msgs = [m for m in captured_request[0].messages if m.role == "system"]
    assert len(system_msgs) == 2
    assert "Lab-validated" in system_msgs[1].content


@pytest.mark.asyncio
async def test_generator_fast_mode_skips_lab_hint() -> None:
    """FAST 模式直接走 fallback step, 不调 LLM, 也不查 registry."""
    _seed_registry("chain_of_thought", task_type="biz_plan")

    fake_router = AsyncMock()
    fake_router.invoke = AsyncMock(side_effect=AssertionError("FAST should not invoke"))

    gen = StructuredStepGenerator(fake_router)
    step = await gen.generate("plan", {"task_type": "biz_plan"}, mode="FAST")

    # FAST 走 fallback, step.action_type = direct_llm
    assert step.action_type == "direct_llm"
    fake_router.invoke.assert_not_called()


def test_lab_hint_safely_handles_registry_failure() -> None:
    """get_recipe_registry 异常 → hint 返 None, hermes 不爆."""
    import kun.engineering.execution_protocol as ep

    original = ep._maybe_lab_recipe_prompt_hint
    ep._maybe_lab_recipe_prompt_hint = lambda _ctx: (_ for _ in ()).throw(
        RuntimeError("simulated lab module crash")
    )
    try:
        # _build_request 要 catch 内部异常? 不, hint 函数自己 catch.
        # 直接调 hint 函数 simulate 抛异常, 测真接口 _build_request 应该 fallback
        # 但简化: 直接 test hint 自己内部 try/except
        pass
    finally:
        ep._maybe_lab_recipe_prompt_hint = original
    # 测 hint 函数自己的 try/except
    import builtins

    real_import = builtins.__import__

    def crashing_import(name, *args, **kwargs):
        if name == "kun.lab.recipe_registry":
            raise RuntimeError("simulated import crash")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = crashing_import
    try:
        result = _maybe_lab_recipe_prompt_hint({"task_type": "x"})
        assert result is None  # 静默
    finally:
        builtins.__import__ = real_import
