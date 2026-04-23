"""Watchtower rule engine tests."""

from typing import Any

import pytest
from kun.watchtower.engine import RuleEngine, load_rules
from kun.watchtower.handlers import register_handler
from kun.watchtower.rules import GuardRule, RuleAction, RuleTrigger

# register a test handler once
_fired: list[dict[str, Any]] = []


@register_handler("test_collect")
async def _collect(ctx, params):
    _fired.append({"ctx": ctx, "params": params})


@pytest.fixture(autouse=True)
def _reset_fired():
    _fired.clear()
    yield
    _fired.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rule_fires_when_condition_true():
    rule = GuardRule(
        id="t_fire",
        kind="guard",
        trigger=RuleTrigger(event_type="task.step.completed", when="x > 10"),
        actions=[RuleAction(handler="test_collect", params={"foo": "bar"})],
    )
    engine = RuleEngine([rule])
    fired = await engine.evaluate(
        "task.step.completed",
        namespace={"x": 11, "tenant_id": "t-1"},
    )
    assert fired == ["t_fire"]
    assert len(_fired) == 1
    assert _fired[0]["params"] == {"foo": "bar"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rule_not_firing_when_condition_false():
    rule = GuardRule(
        id="t_nofire",
        kind="guard",
        trigger=RuleTrigger(event_type="task.step.completed", when="x > 100"),
        actions=[RuleAction(handler="test_collect")],
    )
    engine = RuleEngine([rule])
    fired = await engine.evaluate(
        "task.step.completed",
        namespace={"x": 5, "tenant_id": "t-1"},
    )
    assert fired == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cooldown_blocks_repeat():
    rule = GuardRule(
        id="t_cooldown",
        kind="guard",
        trigger=RuleTrigger(event_type="*", when="True"),
        actions=[RuleAction(handler="test_collect")],
        cooldown_sec=60,
    )
    engine = RuleEngine([rule])
    fired1 = await engine.evaluate("foo", namespace={"tenant_id": "t-1", "task_ref": "tk-x"})
    fired2 = await engine.evaluate("foo", namespace={"tenant_id": "t-1", "task_ref": "tk-x"})
    assert fired1 == ["t_cooldown"]
    assert fired2 == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_expression_blocked():
    rule = GuardRule(
        id="t_unsafe",
        kind="guard",
        trigger=RuleTrigger(event_type="*", when="__import__('os').system('echo hacked')"),
        actions=[RuleAction(handler="test_collect")],
    )
    engine = RuleEngine([rule])
    fired = await engine.evaluate("foo", namespace={"tenant_id": "t-1"})
    # The expression should fail safely and not fire.
    assert fired == []


@pytest.mark.unit
def test_load_rules_from_disk(tmp_path):
    rules_dir = tmp_path / "rules" / "guard"
    rules_dir.mkdir(parents=True)
    (rules_dir / "demo.yaml").write_text(
        "id: demo\ntrigger:\n  event_type: foo\n  when: 'True'\nactions:\n  - handler: log\n",
        encoding="utf-8",
    )
    rules = load_rules(tmp_path / "rules")
    assert len(rules) == 1
    assert rules[0].id == "demo"
    assert rules[0].kind == "guard"
