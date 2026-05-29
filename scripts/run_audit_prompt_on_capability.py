"""V7 Phase X.N — drive any LLM with the hidden-orphan audit prompt.

Loads ``docs/templates/hidden-orphan-audit-prompt.md``, injects the user-
provided capability list + production entries, calls an LLM, and asserts
the response follows the §5 JSON schema (at minimum has the required
fields).

Use it on a PR diff to systematically run the audit against any
proposed capability change.

  Usage:
    .venv/bin/python scripts/run_audit_prompt_on_capability.py \\
        --capability TrifectaCoordinator \\
        --capability MethodologyRuntimeSelector \\
        --entries kun/engineering/orchestrator.py \\
        --entries kun/control_plane/daemon.py

  When KUN_V7_AUDIT_PROMPT_DRY_RUN=true, the script prints the rendered
  prompt + skipping the LLM call (useful for CI smoke / linting).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "templates"
    / "hidden-orphan-audit-prompt.md"
)


REQUIRED_FIELDS = (
    "audited_capabilities",
    "layer_check",
    "angle_check",
    "root_cause_check",
    "verdict",
    "meta_self_audit",
)


def render_audit_prompt(
    capabilities: list[str],
    production_entries: list[str],
) -> str:
    """Render the audit template with the user's capability list +
    production entries filled in at §8."""
    src = TEMPLATE_PATH.read_text(encoding="utf-8")
    # The template's §8 has two placeholder lines we need to fill.
    cap_str = ", ".join(capabilities) or "<not provided>"
    entries_str = "\n    ".join(production_entries) or "<not provided>"
    src = src.replace(
        "[USER 在这里填入要审的 capability 列表 / 子系统 / claim]",
        cap_str,
    )
    src = src.replace(
        "[USER 在这里列出生产入口文件]",
        entries_str,
    )
    return src


def validate_response_against_schema(response_text: str) -> tuple[bool, list[str]]:
    """Extract first JSON object from the LLM's reply and validate the
    §5 required-fields set is present."""
    # Find first {...} JSON-ish block
    m = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not m:
        return False, ["LLM reply has no JSON object"]
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return False, [f"JSON parse failed: {e}"]
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        # Search recursively for the field anywhere in the JSON tree
        if not _has_field(parsed, field):
            missing.append(field)
    return (len(missing) == 0), missing


def _has_field(obj: object, name: str) -> bool:
    if isinstance(obj, dict):
        if name in obj:
            return True
        return any(_has_field(v, name) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_field(item, name) for item in obj)
    return False


async def _run_llm_audit(prompt: str) -> str:
    """Call an LLM with the audit prompt. Uses Anthropic Haiku by default.

    Returns the LLM's raw response text. Caller validates the schema.
    """
    from kun.interface.llm.anthropic_provider import AnthropicProvider
    from kun.interface.llm.base import LLMMessage, LLMRequest

    provider = AnthropicProvider(
        model_id="claude-haiku-4-5-20251001", tier="cheap"
    )
    req = LLMRequest(
        messages=[LLMMessage(role="user", content=prompt)],
        temperature=0.2,
        max_tokens=2000,
    )
    resp = await provider.invoke(req)
    return resp.content or ""


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the hidden-orphan audit prompt template on a capability list"
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Capability symbol to audit (can repeat)",
    )
    parser.add_argument(
        "--entries",
        action="append",
        default=[],
        help="Production entry file path (can repeat)",
    )
    args = parser.parse_args()

    if not args.capability:
        args.capability = ["LongTaskOrchestrator", "MethodologyRuntimeSelector"]
    if not args.entries:
        args.entries = [
            "kun/engineering/orchestrator.py",
            "kun/control_plane/daemon.py",
            "kun/engineering/idle_batch.py",
            "kun/api/main.py",
            "kun/api/ws.py",
        ]

    prompt = render_audit_prompt(args.capability, args.entries)
    print(f"Rendered prompt: {len(prompt)} chars")

    if os.environ.get("KUN_V7_AUDIT_PROMPT_DRY_RUN", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        print("\n=== DRY-RUN MODE — prompt only, no LLM call ===\n")
        print(prompt[:500] + "...")
        return 0

    print("\nCalling LLM...")
    try:
        response = await _run_llm_audit(prompt)
    except Exception as e:
        print(f"❌ LLM call failed: {type(e).__name__}: {e}")
        print("\n(retry with KUN_V7_AUDIT_PROMPT_DRY_RUN=true to skip the LLM)")
        return 2

    print(f"\nLLM response: {len(response)} chars")
    print("=" * 60)
    print(response[:800])
    print("=" * 60)

    ok, missing = validate_response_against_schema(response)
    if ok:
        print(
            "\n✅ Response schema validation PASS — all required fields "
            "present."
        )
        return 0
    print(f"\n❌ Response schema validation FAIL — missing: {missing}")
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
