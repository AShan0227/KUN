"""Intent interpreter — JSON parsing robustness."""

import pytest
from kun.agents.director.intent import IntentInterpreter
from kun.datamodel.task import Owner
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.stub_provider import StubProvider


@pytest.mark.unit
def test_parse_json_direct():
    j = IntentInterpreter._parse_json('{"task_type": "x", "risk_level": "low"}')
    assert j["task_type"] == "x"


@pytest.mark.unit
def test_parse_json_in_code_fence():
    src = '```json\n{"task_type": "y"}\n```'
    j = IntentInterpreter._parse_json(src)
    assert j["task_type"] == "y"


@pytest.mark.unit
def test_parse_json_embedded_in_text():
    src = 'Here is the result: {"task_type": "z"} thanks'
    j = IntentInterpreter._parse_json(src)
    assert j["task_type"] == "z"


@pytest.mark.unit
def test_parse_json_garbage_returns_empty():
    assert IntentInterpreter._parse_json("no json here") == {}


def _json_builder(content: str):
    def _b(request):
        return LLMResponse(
            content=content,
            usage=UsageInfo(input_tokens=5, output_tokens=20),
            model="stub",
            provider="stub",
            tier="top",
        )

    return _b


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_builds_task_ref():
    stub = StubProvider(
        tier="top",
        builder=_json_builder(
            '{"task_type": "coding.python.fastapi", "risk_level": "medium", '
            '"complexity_score": 0.5, "estimated_cost_usd": 0.1, '
            '"success_criteria_short": "write endpoint"}'
        ),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)
    tr = await interpreter.interpret(
        "Write a FastAPI endpoint",
        owner=Owner(tenant_id="u-sylvan"),
    )
    assert tr.meta.task_type == "coding.python.fastapi"
    assert tr.meta.risk_level == "medium"
    assert tr.meta.complexity_score == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_fallback_to_defaults_on_bad_json():
    stub = StubProvider(
        tier="top",
        builder=_json_builder("This is not JSON, sorry."),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)
    tr = await interpreter.interpret(
        "Something",
        owner=Owner(tenant_id="u-sylvan"),
    )
    assert tr.meta.task_type == "general.default"
    assert tr.meta.risk_level == "low"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_normalizes_unknown_constraint_kinds():
    stub = StubProvider(
        tier="top",
        builder=_json_builder(
            '{"task_type": "general.default", "risk_level": "medium", '
            '"success_criteria_short": "keep task isolated", '
            '"goal_detail": "Run the task in an isolated workspace.", '
            '"constraints": ['
            '{"kind": "workspace_isolation", "detail": "use only /tmp/work"}, '
            '{"kind": "path_only", "detail": "/tmp/work"}, '
            '"do not touch main branch"'
            "]}",
        ),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)

    tr = await interpreter.interpret(
        "Run in a new isolated workspace",
        owner=Owner(tenant_id="u-sylvan"),
    )

    assert tr.spec is not None
    assert [constraint.kind for constraint in tr.spec.constraints] == [
        "custom",
        "path_only",
        "custom",
    ]
    assert tr.spec.constraints[0].detail == "workspace_isolation: use only /tmp/work"
    assert tr.spec.constraints[2].detail == "do not touch main branch"


# ===================== L1.7 · complexity / priority_profile / GoalAnchor =====================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_derives_complexity_simple():
    """简单任务: complexity_score=0.2 + steps=1 + risk=low → complexity=simple,
    priority_profile=cost_first, 不生成 GoalAnchor."""
    stub = StubProvider(
        tier="top",
        builder=_json_builder(
            '{"task_type": "general.default", "risk_level": "low", '
            '"complexity_score": 0.2, "estimated_steps": 1, '
            '"success_criteria_short": "say hello"}'
        ),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)

    tr = await interpreter.interpret("hello", owner=Owner(tenant_id="u-sylvan"))

    assert tr.meta.complexity == "simple"
    assert tr.meta.priority_profile == "cost_first"
    assert tr.meta.estimated_steps == 1
    assert getattr(tr, "goal_anchor", None) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_derives_complexity_complex_and_generates_goal_anchor():
    """复杂任务: complexity_score=0.7 → complex + speed_first + 生成 GoalAnchor."""
    stub = StubProvider(
        tier="top",
        builder=_json_builder(
            '{"task_type": "coding.python.refactor", "risk_level": "medium", '
            '"complexity_score": 0.7, "estimated_steps": 6, '
            '"success_criteria_short": "all tests pass and code refactored",'
            '"goal_detail": "Refactor module X to use new pattern",'
            '"success_metrics": ["tests pass", "no regression", "lint clean"],'
            '"out_of_scope": ["touching unrelated modules"]}'
        ),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)

    tr = await interpreter.interpret(
        "Refactor module X to use new pattern",
        owner=Owner(tenant_id="u-sylvan"),
    )

    # complexity 三因素都过阈
    assert tr.meta.complexity == "complex"
    # 复杂任务 → speed_first (效果 > 速度 > 成本)
    assert tr.meta.priority_profile == "speed_first"
    assert tr.meta.estimated_steps == 6

    # GoalAnchor 生成 + 字段正确
    goal_anchor = getattr(tr, "goal_anchor", None)
    assert goal_anchor is not None
    assert goal_anchor.goal_statement == "all tests pass and code refactored"
    assert goal_anchor.task_id == tr.meta.task_id
    assert "tests pass" in goal_anchor.success_criteria
    assert "touching unrelated modules" in goal_anchor.out_of_scope
    assert goal_anchor.immutable is True
    # render 后能拿到 pinned 段
    rendered = goal_anchor.render_for_system_prompt()
    assert "GOAL ANCHOR (immutable, pinned" in rendered
    assert "all tests pass and code refactored" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_critical_risk_forces_speed_first():
    """critical risk 强制 speed_first (即使 complexity=simple), 也生成 GoalAnchor."""
    stub = StubProvider(
        tier="top",
        builder=_json_builder(
            '{"task_type": "ops.deploy", "risk_level": "critical", '
            '"complexity_score": 0.2, "estimated_steps": 1, '
            '"success_criteria_short": "rollout to prod"}'
        ),
    )
    router = LLMRouter({"top": stub, "fallback": stub})
    interpreter = IntentInterpreter(router)

    tr = await interpreter.interpret("deploy to prod", owner=Owner(tenant_id="u-sylvan"))

    # critical 触发 long_task / complex (按 _derive_complexity)
    assert tr.meta.complexity == "complex"
    assert tr.meta.priority_profile == "speed_first"
    assert tr.meta.risk_level == "critical"
    # critical → long_task → GoalAnchor 生成
    assert getattr(tr, "goal_anchor", None) is not None
