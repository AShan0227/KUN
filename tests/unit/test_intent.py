"""Intent interpreter — JSON parsing robustness."""

import pytest
from kun.brain.intent import IntentInterpreter
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
