"""Codex CLI OAuth adapter (for GPT-5 / GPT-5.5 via ChatGPT subscription).

Per user instruction (2026-04-24): don't use an OpenAI API key; use the
logged-in `codex` CLI subprocess.

Each `invoke()` spawns:

    codex exec --json --skip-git-repo-check \
        -c model="gpt-5.5" \
        -s read-only \
        "<prompt>"

`codex exec --json` emits JSONL events to stdout. We collect them and pick
out the final `agent_message` / `task_complete` to assemble the response.

Known limitation (2026-04): Codex OAuth tokens may expire; user must run
`codex login` to refresh when `health_check` returns False.
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

log = get_logger("kun.llm.codex_cli")


# Cost estimates for equivalent $ (GPT-5 family, USD per M tokens; approximate).
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5.5": (10.0, 40.0),
    "gpt-5": (10.0, 40.0),
    "gpt-5-mini": (0.5, 2.0),
}


class CodexCliProvider(LLMProvider):
    """Subprocess adapter for the `codex` CLI (ChatGPT OAuth)."""

    name = "codex-cli"
    supports_tools = False  # codex has its own tool loop; not injected here
    supports_streaming = False  # we collect all JSONL, return once
    supports_cache = True  # OpenAI side handles cache

    price_input_per_mtok = 0.0  # actual = 0 (subscription-paid)
    price_output_per_mtok = 0.0
    price_cached_per_mtok = 0.0

    def __init__(
        self,
        tier: ModelTier,
        *,
        cli_path: str | None = None,
        model_id: str = "gpt-5.5",
        timeout_sec: int = 240,
        run_cwd: str = "/tmp",  # noqa: S108 — intentional: codex sandbox root, isolated from project
    ) -> None:
        self.tier = tier
        self.model_id = model_id
        self._cli = cli_path or shutil.which("codex") or "codex"
        self._timeout = timeout_sec
        self._cwd = run_cwd
        pin, pout = _PRICING_PER_MTOK.get(model_id, (10.0, 40.0))
        self.equivalent_price_input_per_mtok = pin
        self.equivalent_price_output_per_mtok = pout

    @staticmethod
    def available() -> bool:
        return shutil.which("codex") is not None

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        prompt = self._build_prompt(request)

        cmd = [
            self._cli,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-c",
            f'model="{self.model_id}"',
            "-s",
            "read-only",
            "--color",
            "never",
            prompt,
        ]
        log.debug("codex_cli.invoke", model=self.model_id, prompt_len=len(prompt))

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
            raise RuntimeError(f"codex CLI timed out after {self._timeout}s") from None

        latency_ms = (time.perf_counter() - started) * 1000

        stderr_txt = stderr_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise RuntimeError(f"codex CLI exit {proc.returncode}: {stderr_txt[:500]}")

        events = self._parse_jsonl(stdout_b.decode("utf-8", "replace"))
        if not events:
            raise RuntimeError(f"codex CLI produced no events. stderr: {stderr_txt[:500]}")

        # Look for error events first
        for ev in events:
            if ev.get("type") in {"error", "turn.failed"}:
                msg = ev.get("message") or ev.get("error", {}).get("message") or str(ev)
                raise RuntimeError(f"codex CLI error: {msg}")

        content = self._extract_final_text(events)
        usage = self._extract_usage(events)
        equiv_cost = self.compute_cost(usage, equivalent=True)

        llm_request_total.labels(
            provider=self.name, model=self.model_id, role="invoke", tenant_id="unknown"
        ).inc()
        llm_latency_seconds.labels(provider=self.name, model=self.model_id).observe(
            latency_ms / 1000
        )
        llm_cost_usd.labels(provider=self.name, model=self.model_id, tenant_id="unknown").inc(
            equiv_cost
        )

        return LLMResponse(
            content=content,
            usage=usage,
            model=self.model_id,
            provider=self.name,
            tier=self.tier,
            cost_usd_actual=0.0,  # subscription-paid
            cost_usd_equivalent=equiv_cost,
            latency_ms=latency_ms,
            finish_reason="stop",
        )

    async def health_check(self) -> bool:
        if not self.available():
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli,
                "login",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,  # codex writes status to stderr
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            out = (stdout_b + stderr_b).decode("utf-8", "replace").lower()
            return proc.returncode == 0 and "logged in" in out
        except Exception:
            return False

    # --------- helpers ---------

    @staticmethod
    def _build_prompt(request: LLMRequest) -> str:
        """Collapse messages into a single prompt string."""
        parts: list[str] = []
        for m in request.messages:
            if m.role == "system":
                parts.append(f"# System\n{m.content}")
            elif m.role == "user":
                parts.append(f"# User\n{m.content}")
            elif m.role == "assistant":
                parts.append(f"# Assistant (prior)\n{m.content}")
            elif m.role == "tool":
                parts.append(f"# Tool result\n{m.content}")
        return "\n\n".join(parts) or "(empty)"

    @staticmethod
    def _parse_jsonl(text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _extract_final_text(events: list[dict[str, Any]]) -> str:
        """Pull the final agent message. Codex event shape varies by version.

        Handles two observed shapes:
          - {"type": "agent_message", "message": "..."}
          - {"msg": {"type": "agent_message", "message": "..."}}
          - {"type": "task.completed", "output": "..."}
        """
        final: str = ""
        for ev in events:
            # Unwrap msg wrapper if present
            kind = ev.get("type") or (ev.get("msg") or {}).get("type", "")
            body = ev if "msg" not in ev else ev.get("msg", {})
            if kind in {"agent_message", "agent_text"}:
                m = body.get("message") or body.get("text") or ""
                if m:
                    final = m
            elif kind in {"task.completed", "task_complete"}:
                # overrides — this is the definitive final
                out = body.get("output") or body.get("last_agent_message") or ""
                if out:
                    final = out
        return final

    @staticmethod
    def _extract_usage(events: list[dict[str, Any]]) -> UsageInfo:
        """Aggregate token counts across token_count events."""
        usage = UsageInfo()
        for ev in events:
            kind = ev.get("type") or (ev.get("msg") or {}).get("type", "")
            body = ev if "msg" not in ev else ev.get("msg", {})
            if kind in {"token_count", "usage"}:
                usage.input_tokens += int(body.get("input_tokens", 0) or 0)
                usage.output_tokens += int(body.get("output_tokens", 0) or 0)
                usage.cached_input_tokens += int(body.get("cached_input_tokens", 0) or 0)
        return usage
