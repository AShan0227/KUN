"""CodexMcpProvider's tool-call example matches the host parser (audit F123).

_build_prompt told gpt-5.x to emit <skill name="NAME"><param>value</param></skill>,
but KUN's host parser (agent_loop._CALL_RE) only accepts a JSON object body:
<skill name="X">{json}</skill>. A model following the old example had its tool
calls silently dropped. The example now uses the JSON-body form. We assert against
the parser's own regex so the test is independent of the skill registry.
"""

from __future__ import annotations

import json

import pytest
from kun.engineering.agent_loop import _CALL_RE
from kun.interface.llm.base import LLMRequest, ToolSpec
from kun.interface.llm.codex_mcp_provider import CodexMcpProvider


def _prompt_with_tool() -> str:
    req = LLMRequest(
        messages=[],
        tools=[ToolSpec(name="web-search", description="search the web", schema={})],
    )
    return CodexMcpProvider._build_prompt(req)


@pytest.mark.unit
def test_prompt_example_is_json_body_not_xml_params() -> None:
    prompt = _prompt_with_tool()
    assert '<skill name="NAME">{' in prompt  # canonical JSON-body shape
    assert "<param>" not in prompt  # old incompatible XML-children shape is gone


@pytest.mark.unit
def test_json_body_form_matches_parser_and_xml_form_does_not() -> None:
    json_form = '<skill name="web-search">{"query": "rust async"}</skill>'
    xml_form = '<skill name="web-search"><param>rust async</param></skill>'

    m = _CALL_RE.search(json_form)
    assert m is not None, "parser regex must match the JSON-body form"
    assert json.loads(m.group(2)) == {"query": "rust async"}

    # The pre-F123 example shape the prompt used to show does not match at all.
    assert _CALL_RE.search(xml_form) is None
