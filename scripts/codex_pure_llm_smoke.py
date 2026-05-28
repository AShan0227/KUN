"""LT.CODEX-PURE-LLM / LT.TOOLS-GAP-2 smoke test.

# SCOPE (V7 §16.3): LLM-provider 层 smoke test, NOT task-execution 路径.
# 直接调 CodexMcpProvider, 不走 Orchestrator — 这是合理的, 因为它验证的是
# LLM provider 接口契约, 不是 task execution. **不产 capability / 不进
# capability_card / 不算"已开发能力"**. 跟 e2e_rsi_demo.py (绕过 Orchestrator
# 的真 fixture-only) 性质不同 — 这个是 LLM-layer test, 那个是 task-layer fixture.

Fires ONE small request against the live codex MCP-server with a realistic
prompt structure matching what LongTaskOrchestrator sends (skill_directive
inside system message — same shape build_skill_directive produces). Verifies:

  1. Response does NOT contain a sandbox-refusal pattern (LT.CODEX-PURE-LLM
     regression).
  2. Response does NOT invent tool names not in the available list
     (LT.TOOLS-GAP-2 regression — dogfood v5 hallucinated "presentations"
     etc).
  3. Response contains AT LEAST ONE ``<skill name="...">{json}</skill>``
     block that KUN's actual parser (``parse_skill_calls``) accepts.

Run from the repo root:

    .venv/bin/python scripts/codex_pure_llm_smoke.py

Expects ``codex`` CLI installed + ChatGPT subscription session active.
Costs subscription quota; one call (~< 500 tokens).
"""

from __future__ import annotations

import asyncio
import re
import sys

from kun.engineering.agent_loop import build_skill_directive, parse_skill_calls
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.codex_mcp_provider import CodexMcpProvider
from kun.skills.dispatcher import autoload_builtins

# parse_skill_calls filters out unknown skills via dispatcher.is_registered.
# uvicorn boot calls autoload_builtins() — replicate that here so the smoke
# can verify the model's <skill name="X"> calls against real registered names.
autoload_builtins()

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

# Tool names NOT in our available list — if model emits these, it hallucinated
# (these are real Claude Code plugin names that gpt-5.5 reached for in v5)
_HALLUCINATED_NAMES = {"presentations", "spreadsheets", "documents", "github"}


# A minimal but realistic set of skill summaries mirroring what KUN's selector
# would pick for "read dev_logs + write markdown" task. Use REAL KUN skill IDs.
SKILL_SUMMARIES = [
    (
        "file-io",
        "读写沙箱内文件 (KUN_SKILL_FILE_ROOT 限定)",
        {
            "type": "object",
            "required": ["op", "path"],
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["read", "write", "list"],
                },
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    ),
    (
        "shell-exec",
        "在沙箱里执行 shell 命令, 受 allowlist 约束",
        {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
        },
    ),
    (
        "writing-markdown",
        "Produce well-formatted Markdown (headings, lists, tables, code blocks)",
        {
            "type": "object",
            "required": ["intent"],
            "properties": {"intent": {"type": "string"}},
        },
    ),
]


USER_PROMPT = (
    "Please write a file `notes/hello.md` whose body is just the word 'hi'. "
    "Use the host tool that handles file write. Emit XML."
)


async def main() -> int:
    if not CodexMcpProvider.available():
        print("❌ codex CLI not found on PATH — install codex or skip this smoke")
        return 2

    provider = CodexMcpProvider(tier="coding", timeout_sec=120)

    # Build the system prompt EXACTLY as LongTaskOrchestrator would:
    #   task_text + (separator) + skill_directive
    skill_directive = build_skill_directive(SKILL_SUMMARIES)
    system_message = (
        "You are KUN's executor LLM. The user wants a file written.\n\n"
        + skill_directive
    )

    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=system_message),
            LLMMessage(role="user", content=USER_PROMPT),
        ],
        max_tokens=400,
    )

    print(f"🚀 Calling gpt-5.x via codex MCP (model={provider.model_id})...")
    print(f"   System prompt length: {len(system_message)} chars")
    try:
        resp = await provider.invoke(request)
    finally:
        await provider.close()

    content = resp.content or ""
    print(f"\n📥 Response ({len(content)} chars, latency={resp.latency_ms:.0f}ms):")
    print("─" * 60)
    print(content)
    print("─" * 60)

    # Check 1: sandbox refusal
    sandbox_refusals = [
        pattern.pattern
        for pattern in _SANDBOX_REFUSAL_PATTERNS
        if pattern.search(content)
    ]

    # Check 2: hallucinated tool names
    content_lower = content.lower()
    hallucinated_found = [
        name for name in _HALLUCINATED_NAMES if name in content_lower
    ]

    # Check 3: at least one parseable <skill> block
    parsed = parse_skill_calls(content)
    available_names = {summary[0] for summary in SKILL_SUMMARIES}
    parsed_real = [p for p in parsed if p.name in available_names]
    parsed_unknown = [p for p in parsed if p.name not in available_names]

    print("\n🧪 Verdict:")
    print(f"   sandbox refusal patterns: {sandbox_refusals or 'none ✓'}")
    print(f"   hallucinated tool names: {hallucinated_found or 'none ✓'}")
    print(f"   parseable <skill> blocks: {len(parsed)}")
    print(f"     valid (matching real skill): {[p.name for p in parsed_real]}")
    print(f"     unknown (made-up): {[p.name for p in parsed_unknown]}")

    fail = False
    if sandbox_refusals:
        print("\n❌ FAIL: model refused via sandbox excuse (LT.CODEX-PURE-LLM regress).")
        fail = True
    if hallucinated_found:
        print(
            "\n❌ FAIL: model emitted hallucinated tool names not in the list "
            "(LT.TOOLS-GAP-2 regress)."
        )
        fail = True
    if not parsed_real:
        print(
            "\n❌ FAIL: no parseable <skill> block matching a real available "
            "tool. Model didn't use the host protocol properly."
        )
        fail = True
    if fail:
        return 1

    print(
        "\n✅ PASS: model returned proper <skill> JSON XML using a real "
        "host tool, no sandbox refusal, no hallucination."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
