"""Claude Code CLI OAuth adapter.

Per user instruction (2026-04-24): don't use an Anthropic API key; use the
logged-in `claude` CLI subprocess (OAuth to Claude Pro / Max / Team).

Each `invoke()` spawns:

    claude -p "<prompt>" \
        --model opus|sonnet|haiku \
        --system-prompt "<system>" \
        --output-format json \
        --tools "" \
        --bare \
        --permission-mode bypassPermissions

JSON output of `claude -p` includes:
  - result: str (final assistant text)
  - usage: { input_tokens, output_tokens, cache_read_input_tokens, ... }
  - total_cost_usd: float (subscription equivalent)
  - modelUsage: { model_id → {inputTokens, outputTokens, costUSD, ...} }
  - duration_ms, duration_api_ms
  - is_error: bool

We set `cost_usd_actual = 0` (subscription-paid, no API $ burned) and
`cost_usd_equivalent = total_cost_usd` (the "if this had been API" price).
This matches ADR-008 duality.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from typing import Any

from kun.core.logging import get_logger
from kun.core.metrics import llm_cost_usd, llm_latency_seconds, llm_request_total
from kun.interface.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    UsageInfo,
)

log = get_logger("kun.llm.claude_code")


# Map tier → claude CLI --model flag
_TIER_TO_MODEL: dict[ModelTier, str] = {
    "top": "opus",
    "strong": "sonnet",
    "cheap": "haiku",
    "coding": "sonnet",  # claude-code for coding; real coding default uses codex
    "fallback": "haiku",
}


class ClaudeCodeProvider(LLMProvider):
    """Subprocess adapter for the `claude` CLI (OAuth session)."""

    name = "claude-code-cli"
    supports_tools = False  # we disable claude's tools for KUN call
    supports_streaming = False  # -p is one-shot
    supports_cache = True  # Claude Code handles prompt caching internally

    # Subscription-paid; actual $ = 0. Equivalent is reported by the CLI per call.
    price_input_per_mtok = 0.0
    price_output_per_mtok = 0.0
    price_cached_per_mtok = 0.0

    def __init__(
        self,
        tier: ModelTier,
        *,
        cli_path: str | None = None,
        timeout_sec: int = 180,
        run_cwd: str = "/tmp",  # noqa: S108 — intentional: avoid pulling CLAUDE.md from project cwd
    ) -> None:
        self.tier = tier
        self.model_id = f"claude-code-{_TIER_TO_MODEL[tier]}"
        self._cli = cli_path or shutil.which("claude") or "claude"
        self._timeout = timeout_sec
        self._cwd = run_cwd

    @staticmethod
    def available() -> bool:
        """Check if the `claude` CLI is installed + has a valid session."""
        return shutil.which("claude") is not None

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()

        prompt, system = self._split_messages(request)
        model = _TIER_TO_MODEL.get(self.tier, "sonnet")

        # NB: do NOT pass --bare — it disables OAuth and forces API-key auth.
        # Use --tools "" + exclude-dynamic-system-prompt-sections instead,
        # and run in /tmp (no CLAUDE.md pickup).
        cmd = [
            self._cli,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",  # disable claude-code's own tools
            "--exclude-dynamic-system-prompt-sections",
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--disable-slash-commands",
        ]
        if system:
            cmd += ["--system-prompt", system]

        log.debug("claude_code.invoke", model=model, prompt_len=len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env={**os.environ, "NO_COLOR": "1"},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude CLI timed out after {self._timeout}s") from None

        latency_ms = (time.perf_counter() - started) * 1000

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exit {proc.returncode}: {stderr_b.decode('utf-8', 'replace')[:500]}"
            )

        try:
            data: dict[str, Any] = json.loads(stdout_b.decode("utf-8"))
        except json.JSONDecodeError as e:
            preview = stdout_b[:400].decode("utf-8", "replace")
            raise RuntimeError(f"claude CLI produced non-JSON output: {preview}") from e

        if data.get("is_error") or data.get("type") == "error":
            raise RuntimeError(
                f"claude CLI reported error: {data.get('result') or data.get('message') or data}"
            )

        content = str(data.get("result") or "")
        usage_blob = data.get("usage", {}) or {}
        usage = UsageInfo(
            input_tokens=int(usage_blob.get("input_tokens", 0) or 0),
            output_tokens=int(usage_blob.get("output_tokens", 0) or 0),
            cached_input_tokens=int(usage_blob.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage_blob.get("cache_creation_input_tokens", 0) or 0),
        )
        equiv_cost = float(data.get("total_cost_usd", 0.0) or 0.0)

        # Pick most-used model from modelUsage for reporting
        model_usage = data.get("modelUsage") or {}
        reported_model = self.model_id
        if model_usage:
            reported_model = max(
                model_usage.items(),
                key=lambda kv: kv[1].get("outputTokens", 0) or 0,
            )[0]

        # Metrics — mark cost_equiv (subscription-paid, no API $)
        llm_request_total.labels(
            provider=self.name,
            model=reported_model,
            role="invoke",
            tenant_id="unknown",
        ).inc()
        llm_latency_seconds.labels(provider=self.name, model=reported_model).observe(
            latency_ms / 1000
        )
        # Record the *equivalent* cost — it represents what we'd pay via API.
        llm_cost_usd.labels(provider=self.name, model=reported_model, tenant_id="unknown").inc(
            equiv_cost
        )

        return LLMResponse(
            content=content,
            usage=usage,
            model=reported_model,
            provider=self.name,
            tier=self.tier,
            cost_usd_actual=0.0,  # subscription, $0 API spend
            cost_usd_equivalent=equiv_cost,
            latency_ms=latency_ms,
            finish_reason=("length" if data.get("stop_reason") == "max_tokens" else "stop"),
        )

    async def health_check(self) -> bool:
        """Quick check: CLI present + session valid."""
        if not self.available():
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli,
                "-p",
                "pong",
                "--output-format",
                "json",
                "--model",
                "haiku",
                "--tools",
                "",
                "--permission-mode",
                "bypassPermissions",
                "--no-session-persistence",
                "--disable-slash-commands",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self._cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return proc.returncode == 0 and b'"is_error":false' in stdout
        except Exception:
            return False

    # --------- helpers ---------

    @staticmethod
    def _split_messages(request: LLMRequest) -> tuple[str, str]:
        """Turn messages list into (prompt, system_prompt)."""
        system_parts: list[str] = []
        convo_parts: list[str] = []
        for m in request.messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "user":
                convo_parts.append(m.content)
            elif m.role == "assistant":
                # Previous assistant turns — inline as context
                convo_parts.append(f"<previous_assistant>{m.content}</previous_assistant>")
            elif m.role == "tool":
                convo_parts.append(f"<tool_result>{m.content}</tool_result>")
        prompt = "\n\n".join(convo_parts) or "(empty)"
        system = "\n\n".join(system_parts)
        return prompt, system
