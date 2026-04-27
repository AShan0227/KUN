"""Wire 31: hermes ExecutionStep.action_type → step_plan.skill_hint 覆盖."""

from __future__ import annotations

from kun.engineering.execution_protocol import ExecutionStep
from kun.engineering.orchestrator import _hermes_skill_from_action


def _make_step(action_type: str, payload: dict | None = None) -> ExecutionStep:
    return ExecutionStep(
        step_id=1,
        thought="reasoning",
        action_type=action_type,  # type: ignore[arg-type]
        action_payload=payload or {},
        expected_outcome="ok",
        confidence=0.7,
    )


def test_use_skill_with_skill_id_returns_id() -> None:
    step = _make_step("use_skill", {"skill_id": "code.lint"})
    assert _hermes_skill_from_action(step) == "code.lint"


def test_use_skill_with_skill_alias_returns_id() -> None:
    """payload.skill (而不是 skill_id) 也接受."""
    step = _make_step("use_skill", {"skill": "code.format"})
    assert _hermes_skill_from_action(step) == "code.format"


def test_use_skill_without_skill_id_returns_none() -> None:
    step = _make_step("use_skill", {})
    assert _hermes_skill_from_action(step) is None


def test_use_skill_blank_skill_id_returns_none() -> None:
    step = _make_step("use_skill", {"skill_id": "   "})
    assert _hermes_skill_from_action(step) is None


def test_web_search_action_returns_web_search_skill() -> None:
    step = _make_step("web_search", {"query": "x"})
    assert _hermes_skill_from_action(step) == "web_search"


def test_use_memory_returns_none() -> None:
    """use_memory 不覆盖 skill_hint (走 ImportanceScorer 路径, Wire 32 处理)."""
    step = _make_step("use_memory")
    assert _hermes_skill_from_action(step) is None


def test_ask_user_returns_none() -> None:
    """ask_user 不覆盖 skill_hint (Wire 32 单独 wire 成 PendingAction)."""
    step = _make_step("ask_user", {"question": "X?"})
    assert _hermes_skill_from_action(step) is None


def test_direct_llm_returns_none() -> None:
    """direct_llm 跑现有 LLM step 路径, 不覆盖."""
    step = _make_step("direct_llm")
    assert _hermes_skill_from_action(step) is None


def test_none_step_returns_none() -> None:
    assert _hermes_skill_from_action(None) is None


def test_skill_id_preferred_over_skill_alias() -> None:
    """两个都给, 用 skill_id."""
    step = _make_step("use_skill", {"skill_id": "primary", "skill": "alias"})
    assert _hermes_skill_from_action(step) == "primary"
