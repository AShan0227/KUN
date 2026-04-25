"""输出翻译适配器测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from kun.interface.adapters import get_adapter, list_adapters, register, translate, translate_for
from kun.interface.adapters.human import HumanAdapter
from kun.interface.llm import LLMRequest, LLMResponse, LLMRouter
from kun.interface.llm.stub_provider import StubProvider


class _StubAdapter:
    name = "unit-stub"

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        return f"{recipient_kind}:{payload['ok']}:{(context or {}).get('x')}"


class _HumanRouter(LLMRouter):
    def __init__(self) -> None:
        provider = StubProvider()
        super().__init__({"cheap": provider, "fallback": provider})
        self.last_request: LLMRequest | None = None

    async def invoke(self, request: LLMRequest, *, purpose: str = "execution") -> LLMResponse:
        self.last_request = request
        return LLMResponse(content="已经整理好了", provider="stub", model="stub", tier="cheap")


@pytest.mark.unit
def test_builtin_adapters_are_registered() -> None:
    names = set(list_adapters())

    assert {"human", "a2a", "rest", "markdown", "email"} <= names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_dispatches_stub_adapter() -> None:
    register(_StubAdapter())

    output = await translate(
        "unit-stub",
        payload={"ok": True},
        recipient_kind="user",
        context={"x": 1},
    )

    assert output == "user:True:1"
    assert get_adapter("unit-stub").name == "unit-stub"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a2a_adapter_outputs_json_rpc() -> None:
    output = await translate(
        "a2a",
        payload={"task_id": "task-1", "goal": "ship"},
        recipient_kind="agent",
        context={"method": "task.delegate"},
    )
    decoded = json.loads(output)

    assert decoded["jsonrpc"] == "2.0"
    assert decoded["id"] == "task-1"
    assert decoded["method"] == "task.delegate"
    assert decoded["params"]["recipient_kind"] == "agent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_translate_for_uses_default_mapping() -> None:
    output = await translate_for(
        payload={"task_id": "task-1", "goal": "ship"},
        recipient_kind="a2a",
        context={"method": "task.delegate"},
    )
    decoded = json.loads(output)

    assert decoded["method"] == "task.delegate"
    assert decoded["params"]["recipient_kind"] == "a2a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_adapter_outputs_request_template() -> None:
    output = await translate(
        "rest",
        payload={"hello": "world"},
        recipient_kind="company",
        context={"path": "/v1/tasks"},
    )
    decoded = json.loads(output)

    assert decoded["method"] == "POST"
    assert decoded["path"] == "/v1/tasks"
    assert decoded["body"] == {"hello": "world"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_markdown_and_email_adapters_are_readable() -> None:
    markdown = await translate(
        "markdown",
        payload={"title": "日报", "summary": "完成 T2"},
        recipient_kind="user",
    )
    email = await translate(
        "email",
        payload={"title": "日报", "summary": "<unsafe>"},
        recipient_kind="user",
    )

    assert markdown.startswith("# 日报")
    assert "完成 T2" in markdown
    assert "<unsafe>" not in email
    assert "&lt;unsafe&gt;" in email


@pytest.mark.unit
@pytest.mark.asyncio
async def test_human_adapter_uses_router_without_importing_provider() -> None:
    router = _HumanRouter()
    output = await HumanAdapter(router=router).translate(
        payload={"summary": "done"},
        recipient_kind="user",
        context={"language": "zh"},
    )

    assert output == "已经整理好了"
    assert router.last_request is not None
    assert "payload=" in router.last_request.messages[-1].content
