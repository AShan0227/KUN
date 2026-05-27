"""LT.CODEX-PURE-LLM smoke test — does gpt-5.x actually emit <skill> XML?

Fires ONE small request against the live codex MCP-server, asking gpt-5.x to
"write a file" using KUN's XML skill protocol. The smoke passes if:

  - Response does NOT contain a sandbox-refusal pattern ("read-only sandbox",
    "approval policy", "cannot create files", etc.) — that was dogfood v4's
    failure mode that the LT.CODEX-PURE-LLM refactor is supposed to fix.
  - Response DOES contain a parsable ``<skill name="write_file">`` XML block.

Run from the repo root:

    .venv/bin/python scripts/codex_pure_llm_smoke.py

Expects ``codex`` CLI installed + ChatGPT subscription session active.
Costs subscription quota; one call (~< 200 tokens).
"""

from __future__ import annotations

import asyncio
import re
import sys

from kun.interface.llm.base import LLMMessage, LLMRequest, ToolSpec
from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

_SANDBOX_REFUSAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"read[- ]only sandbox",
        r"approval policy",
        r"cannot create files?",
        r"can't write files?",
        r"don't have permission",
        r"no write access",
    )
]

_SKILL_XML_RE = re.compile(r'<skill\s+name=["\']write_file["\']', re.IGNORECASE)


SYSTEM_PROMPT = """\
You have one tool available via the KUN host's XML protocol:

  <skill name="write_file">
    <path>relative path</path>
    <content>file content</content>
  </skill>

When asked to create a file, emit the XML — KUN dispatches it.\
"""

USER_PROMPT = (
    "Please create a file at `notes/hello.md` whose body is just the word 'hi'. "
    "Use the host's write_file XML protocol — KUN will execute it."
)


async def main() -> int:
    if not CodexMcpProvider.available():
        print("❌ codex CLI not found on PATH — install codex or skip this smoke")
        return 2

    provider = CodexMcpProvider(tier="coding", timeout_sec=120)

    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=USER_PROMPT),
        ],
        tools=[
            ToolSpec(
                name="write_file",
                description="Write content at the given relative path.",
                schema_={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            )
        ],
        max_tokens=400,
    )

    print(f"🚀 Calling gpt-5.x via codex MCP (model={provider.model_id})...")
    try:
        resp = await provider.invoke(request)
    finally:
        await provider.close()

    content = resp.content or ""
    print(f"\n📥 Response ({len(content)} chars, latency={resp.latency_ms:.0f}ms):")
    print("─" * 60)
    print(content)
    print("─" * 60)

    sandbox_refusals = [
        pattern.pattern for pattern in _SANDBOX_REFUSAL_PATTERNS if pattern.search(content)
    ]
    has_skill_xml = bool(_SKILL_XML_RE.search(content))

    print("\n🧪 Verdict:")
    print(f"   sandbox refusal patterns: {sandbox_refusals or 'none ✓'}")
    print(f"   <skill name='write_file'> present: {'✓' if has_skill_xml else '✗'}")

    if sandbox_refusals:
        print("\n❌ FAIL: model refused via sandbox excuse. Refactor not effective.")
        return 1
    if not has_skill_xml:
        print(
            "\n⚠️  PARTIAL: no sandbox refusal but no <skill> XML either."
            "\n   Model may have responded with prose instead of using protocol."
            "\n   Inspect output above; may need stronger prompt or different model."
        )
        return 1
    print("\n✅ PASS: model returned proper <skill> XML, no sandbox refusal.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
