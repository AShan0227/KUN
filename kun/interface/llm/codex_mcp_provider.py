"""Codex MCP-server adapter — the working path for ChatGPT-subscription gpt-5.3-codex-spark.

Background (2026-04-24): the initial `codex exec` path (see CodexCliProvider)
doesn't work with ChatGPT-account auth — that flow hits the OpenAI public
API endpoint which rejects the codex-family model ids. The **MCP server**
path routes through the same backend the Codex interactive UI uses, and the
rate-limits it reports tell us the real model id is `gpt-5.3-codex-spark`
(not the UI's "gpt-5.5" display name).

Implementation shape:
  - one long-lived ``codex mcp-server`` subprocess per provider instance
  - MCP JSON-RPC 2.0 over stdio; initialize → tools/call(codex) per invoke
  - a background reader task routes responses to their ``asyncio.Future``
    by JSON-RPC id; progress events (``codex/event`` notifications) are
    consumed and dropped — the final agent text is returned in the
    ``tools/call`` response's ``structuredContent.content``

Subscription-paid, so ``cost_usd_actual == 0`` (ADR-008 duality). We fill
``cost_usd_equivalent`` with a rough $/M-token estimate since the MCP
response doesn't carry a cost field.
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

log = get_logger("kun.llm.codex_mcp")

# Default model id — GPT-5.5 since 2026-04-23 (OpenAI's current frontier; the
# first fully-retrained base since GPT-4.5). Requires codex CLI ≥ 0.125.
# Override via KUN_CODEX_MCP_MODEL (e.g. fall back to gpt-5.3-codex-spark
# if you need to pin an older CLI).
_DEFAULT_MODEL = "gpt-5.5"

# Default reasoning effort — "low" is a 3s-turnaround baseline. Override via
# KUN_CODEX_REASONING (low / medium / high / xhigh).
_DEFAULT_REASONING = "low"

# Sandboxed working directory for the codex session. Must NOT contain
# AGENTS.md / CLAUDE.md — those would be pulled into the session context
# per turn and blow up latency. Created lazily on first invoke.
_DEFAULT_CWD = "/tmp/kun-codex-cwd"  # noqa: S108 — intentional sandbox root

# Rough $/M-token equivalent (for ADR-008 equivalent cost; actual = 0).
# Numbers are public-API list prices; we never charge them, but the
# orchestrator uses them to size budgets in NUO panels.
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    # GPT-5.5 (frontier, 2026-04-23+)
    "gpt-5.5": (12.0, 48.0),
    "gpt-5.5-mini": (1.5, 6.0),
    # GPT-5.3 family (codex specialty, 2026-02-05+)
    "gpt-5.3-codex-spark": (10.0, 40.0),
    "gpt-5.3-codex": (10.0, 40.0),
    "gpt-5-codex": (10.0, 40.0),
}

_PROTOCOL_VERSION = "2025-06-18"


class CodexMcpProvider(LLMProvider):
    """Subprocess MCP-client adapter for `codex mcp-server`."""

    name = "codex-mcp"
    supports_tools = False  # codex drives its own tool loop; we just pass a prompt
    supports_streaming = False
    supports_cache = True  # backend handles caching

    # Subscription-paid; actual $ = 0. Equivalent is computed per-call.
    price_input_per_mtok = 0.0
    price_output_per_mtok = 0.0
    price_cached_per_mtok = 0.0

    def __init__(
        self,
        tier: ModelTier = "coding",
        *,
        cli_path: str | None = None,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        timeout_sec: int = 180,
        run_cwd: str | None = None,
    ) -> None:
        self.tier = tier
        self.model_id = model_id or os.getenv("KUN_CODEX_MCP_MODEL") or _DEFAULT_MODEL
        self.reasoning_effort = (
            reasoning_effort or os.getenv("KUN_CODEX_REASONING") or _DEFAULT_REASONING
        )
        self._cli = cli_path or shutil.which("codex") or "codex"
        self._timeout = timeout_sec
        self._cwd = run_cwd or _DEFAULT_CWD
        os.makedirs(self._cwd, exist_ok=True)  # bare sandbox dir

        pin, pout = _PRICING_PER_MTOK.get(self.model_id, (10.0, 40.0))
        self.equivalent_price_input_per_mtok = pin
        self.equivalent_price_output_per_mtok = pout

        # --- subprocess + MCP session state ---
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._id_counter = 0
        self._initialized = False
        self._start_lock = asyncio.Lock()  # serializes (re)start
        self._send_lock = asyncio.Lock()  # serializes writes to stdin

    @staticmethod
    def available() -> bool:
        return shutil.which("codex") is not None

    # ---------- public ----------

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        await self._ensure_running()
        prompt = self._build_prompt(request)
        started = time.perf_counter()

        req_id = self._next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": "codex",
                "arguments": {
                    "prompt": prompt,
                    "model": self.model_id,
                    "approval-policy": "never",
                    "sandbox": "read-only",
                    "cwd": self._cwd,
                    # Keep codex stateless: no agent system prompt, no AGENTS.md
                    # pickup. Strictly a model-call adapter.
                    "base-instructions": "",
                    "developer-instructions": "",
                    "config": {"model_reasoning_effort": self.reasoning_effort},
                },
            },
        }

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._send(payload)
            response = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            await self._kill()
            raise RuntimeError(f"codex mcp-server timed out after {self._timeout}s") from None
        except Exception:
            self._pending.pop(req_id, None)
            raise

        latency_ms = (time.perf_counter() - started) * 1000

        if "error" in response:
            raise RuntimeError(f"codex MCP JSON-RPC error: {response['error']}")

        result = response.get("result") or {}
        if result.get("isError"):
            text = ""
            content_arr = result.get("content") or []
            if content_arr:
                text = (content_arr[0] or {}).get("text", "")
            raise RuntimeError(f"codex MCP call failed: {text[:500] or result}")

        content_text = ""
        structured = result.get("structuredContent") or {}
        if isinstance(structured, dict) and structured.get("content"):
            content_text = str(structured["content"])
        elif result.get("content"):
            first = result["content"][0] or {}
            content_text = str(first.get("text", ""))

        # Usage is not reported in the tools/call response; leave zeros —
        # rate-limit headroom is tracked via the codex/event stream in
        # future work. Cost equivalent still computes via our $/Mtok guess
        # over zero tokens = 0; that's fine for the subscription path.
        usage = UsageInfo()
        cost_equiv = self.compute_cost(usage, equivalent=True)

        llm_request_total.labels(
            provider=self.name, model=self.model_id, role="invoke", tenant_id="unknown"
        ).inc()
        llm_latency_seconds.labels(provider=self.name, model=self.model_id).observe(
            latency_ms / 1000
        )
        llm_cost_usd.labels(provider=self.name, model=self.model_id, tenant_id="unknown").inc(
            cost_equiv
        )

        return LLMResponse(
            content=content_text,
            usage=usage,
            model=self.model_id,
            provider=self.name,
            tier=self.tier,
            cost_usd_actual=0.0,
            cost_usd_equivalent=cost_equiv,
            latency_ms=latency_ms,
            finish_reason="stop",
        )

    async def health_check(self) -> bool:
        if not self.available():
            return False
        try:
            await self._ensure_running()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Terminate the MCP subprocess — call on shutdown."""
        await self._kill()

    # ---------- subprocess lifecycle ----------

    async def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.returncode is None and self._initialized:
            return
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None and self._initialized:
                return
            await self._kill()
            log.info("codex_mcp.starting", cli=self._cli, model=self.model_id)
            self._proc = await asyncio.create_subprocess_exec(
                self._cli,
                "mcp-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self._handshake()
            self._initialized = True

    async def _handshake(self) -> None:
        init_id = self._next_id()
        init_fut = asyncio.get_event_loop().create_future()
        self._pending[init_id] = init_fut
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "kun", "version": "0.1"},
                },
            }
        )
        try:
            await asyncio.wait_for(init_fut, timeout=10)
        except TimeoutError:
            self._pending.pop(init_id, None)
            raise RuntimeError("codex mcp-server initialize timeout") from None
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _kill(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._reader_task = None
        self._stderr_task = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None
        self._initialized = False
        # Drop any still-pending futures with a clean error.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("codex mcp-server restarted"))
        self._pending.clear()

    # ---------- IO ----------

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(msg) + "\n").encode("utf-8")
        async with self._send_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    log.debug("codex_mcp.bad_json", raw=line[:200].decode("utf-8", "replace"))
                    continue
                mid = obj.get("id")
                if mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(obj)
                # else: event/notification — ignore
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("codex_mcp.read_loop_error", error=str(e))

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                # codex prints diagnostics here — surface at debug
                log.debug("codex_mcp.stderr", line=line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ---------- helpers ----------

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    @staticmethod
    def _build_prompt(request: LLMRequest) -> str:
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
