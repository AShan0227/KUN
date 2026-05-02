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


@pytest.mark.unit
def test_load_rules_ignores_non_rule_yaml(tmp_path, caplog):
    rules_dir = tmp_path / "rules" / "guard"
    proactive_dir = tmp_path / "rules" / "proactive"
    rules_dir.mkdir(parents=True)
    proactive_dir.mkdir(parents=True)
    (rules_dir / "demo.yaml").write_text(
        "id: demo\ntrigger:\n  event_type: foo\n  when: 'True'\n",
        encoding="utf-8",
    )
    (proactive_dir / "triggers.yaml").write_text(
        "version: 1\ntriggers:\n  - skill_id: web-search\n",
        encoding="utf-8",
    )

    rules = load_rules(tmp_path / "rules")

    assert [r.id for r in rules] == ["demo"]
    assert "rules.load_failed" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_rule_triggers_incident_response() -> None:
    from kun.security.incident_response import IncidentResponseEngine

    incident_engine = IncidentResponseEngine()
    rule = GuardRule(
        id="cost_runaway",
        kind="guard",
        description="budget exceeded",
        trigger=RuleTrigger(event_type="task.step.completed", when="True"),
        severity="high",
        actions=[],
    )
    engine = RuleEngine([rule], incident_response=incident_engine)

    fired = await engine.evaluate(
        "task.step.completed",
        namespace={
            "tenant_id": "t-1",
            "task_ref": "tk-1",
            "event": {
                "tenant_id": "t-1",
                "payload": {"task_id": "tk-1", "accumulated_cost_usd": 2.0},
            },
        },
    )

    assert fired == ["cost_runaway"]
    history = incident_engine.get_history()
    assert len(history) == 1
    incident, _actions = history[0]
    assert incident.category == "cost"
    assert incident.severity == "L3"
    assert incident.affected_tenant_id == "t-1"
    assert incident.affected_task_id == "tk-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cross_tenant_rule_triggers_security_incident() -> None:
    from kun.security.incident_response import IncidentResponseEngine

    incident_engine = IncidentResponseEngine()
    rule = GuardRule(
        id="cross_tenant_attempt",
        kind="guard",
        description="cross tenant",
        trigger=RuleTrigger(event_type="security.cross_tenant_attempt", when="True"),
        severity="critical",
        actions=[],
    )
    engine = RuleEngine([rule], incident_response=incident_engine)

    await engine.evaluate(
        "security.cross_tenant_attempt",
        namespace={"tenant_id": "t-secure", "user_id": "u-1"},
    )

    incident, _actions = incident_engine.get_history()[0]
    assert incident.category == "security"
    assert incident.severity == "L4"
    assert incident.affected_user_id == "u-1"
