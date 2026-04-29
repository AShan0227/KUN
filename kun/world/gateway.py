"""V3 World Gateway.

World Gateway is the only module allowed to prepare real-world side effects.
The first production-safe slice is deliberately conservative: it creates an
audit packet and releases the approval gate, but it does not send emails, call
paid APIs, publish content, or move money until explicit delivery handlers are
registered in a later slice.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from kun.interface.hermes import DefaultHermesAdapter, HermesAdapter


class WorldAction(BaseModel):
    """A tenant-scoped side-effect request."""

    action_id: str
    task_ref: str
    action_type: str
    target_ref: str
    risk_level: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorldGatewayResult(BaseModel):
    """Gateway audit result."""

    action_id: str
    gateway_mode: str = "approval_gate"
    external_dispatched: bool = False
    requires_handler: bool = True
    rendered_payload: str = ""
    audit: dict[str, Any] = Field(default_factory=dict)
    message: str = (
        "World Gateway recorded and authorized this action, but no external "
        "delivery handler is attached yet."
    )


class WorldHandlerResult(BaseModel):
    """Result returned by a concrete low-risk world handler."""

    handler_id: str
    status: Literal["executed", "drafted", "dry_run"]
    external_dispatched: bool = False
    rendered_payload: str = ""
    artifact_ref: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    message: str


class WorldHandlerDescriptor(BaseModel):
    """User-facing capability descriptor for one gateway handler."""

    action_type: str
    handler_id: str
    mode: Literal["execute", "draft", "dry_run", "plan"]
    external_dispatched: bool = False
    artifact_kind: str = ""
    safety_note: str


class WorldActionHandler:
    """Base class for concrete WorldGateway handlers."""

    action_type: str
    handler_id: str
    mode: Literal["execute", "draft", "dry_run", "plan"] = "dry_run"
    external_dispatched: bool = False
    artifact_kind: str = ""
    safety_note: str = "Handled by World Gateway."

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        raise NotImplementedError


class LocalFileWriteHandler(WorldActionHandler):
    """Write a file under a controlled output directory."""

    action_type = "local_file.write"
    handler_id = "local_file.write.v1"
    mode = "execute"
    external_dispatched = True
    artifact_kind = "local_file"
    safety_note = "只允许写入 KUN 受控输出目录，禁止绝对路径和路径穿越。"

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        raw_path = str(
            action.payload.get("relative_path")
            or action.payload.get("path")
            or action.target_ref
            or ""
        ).strip()
        if not raw_path:
            raise ValueError("local_file.write requires payload.path or target_ref")
        if Path(raw_path).is_absolute():
            raise ValueError("local_file.write only accepts relative paths")
        target = (self.output_root / raw_path).resolve()
        if not _is_relative_to(target, self.output_root):
            raise ValueError("local_file.write path escapes output root")

        content = action.payload.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed_before = target.exists()
        target.write_text(content, encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="executed",
            external_dispatched=True,
            rendered_payload=content,
            artifact_ref=str(target),
            audit={
                "output_root": str(self.output_root),
                "path": str(target),
                "relative_path": str(target.relative_to(self.output_root)),
                "bytes": len(content.encode("utf-8")),
                "existed_before": existed_before,
            },
            message="Local file written under the controlled KUN output directory.",
        )


class EmailDraftHandler(WorldActionHandler):
    """Create an email draft artifact; never sends mail."""

    action_type = "email.draft"
    handler_id = "email.draft.v1"
    mode = "draft"
    external_dispatched = False
    artifact_kind = "email_draft"
    safety_note = "只生成邮件草稿文件，不会真实发送邮件。"

    def __init__(self, output_root: str | Path) -> None:
        self.draft_root = Path(output_root).expanduser().resolve() / "email_drafts"
        self.draft_root.mkdir(parents=True, exist_ok=True)

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        draft = {
            "to": action.payload.get("to") or action.target_ref,
            "subject": action.payload.get("subject", ""),
            "body": action.payload.get("body", ""),
            "cc": action.payload.get("cc", []),
            "bcc": action.payload.get("bcc", []),
            "sent": False,
            "task_ref": action.task_ref,
            "action_id": action.action_id,
        }
        path = self.draft_root / f"{_safe_artifact_name(action.action_id)}.json"
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="drafted",
            external_dispatched=False,
            rendered_payload=json.dumps(draft, ensure_ascii=False, indent=2),
            artifact_ref=str(path),
            audit={
                "path": str(path),
                "sent": False,
                "recipient": draft["to"],
                "reason": "draft only; no email was sent",
            },
            message="Email draft created. It was not sent.",
        )


class WebhookPostDryRunHandler(WorldActionHandler):
    """Render a webhook POST request without sending it."""

    action_type = "webhook.post_dry_run"
    handler_id = "webhook.post_dry_run.v1"
    mode = "dry_run"
    external_dispatched = False
    artifact_kind = "http_request_preview"
    safety_note = "只渲染 POST 请求包，不会发起网络请求。"

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        url = str(action.payload.get("url") or action.target_ref or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("webhook.post_dry_run requires an http(s) URL")
        request = {
            "method": "POST",
            "url": url,
            "headers": action.payload.get("headers", {}),
            "json": action.payload.get("json", action.payload.get("body", {})),
            "dry_run": True,
        }
        rendered = json.dumps(request, ensure_ascii=False, indent=2)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="dry_run",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                "url": url,
                "host": parsed.netloc,
                "dry_run": True,
                "reason": "request rendered only; no network call was made",
            },
            message="Webhook request rendered in dry-run mode. No network call was made.",
        )


class BrowserPlanHandler(WorldActionHandler):
    """Create a browser operation plan; never controls a browser."""

    action_type = "browser.plan"
    handler_id = "browser.plan.v1"
    mode = "plan"
    external_dispatched = False
    artifact_kind = "browser_plan"
    safety_note = "只生成浏览器操作计划，不会真实点击或控制浏览器。"

    def __init__(self, output_root: str | Path) -> None:
        self.plan_root = Path(output_root).expanduser().resolve() / "browser_plans"
        self.plan_root.mkdir(parents=True, exist_ok=True)

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        plan = {
            "url": action.payload.get("url") or action.target_ref,
            "objective": action.payload.get("objective", ""),
            "steps": action.payload.get("steps", []),
            "executed": False,
            "task_ref": action.task_ref,
            "action_id": action.action_id,
        }
        path = self.plan_root / f"{_safe_artifact_name(action.action_id)}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="drafted",
            external_dispatched=False,
            rendered_payload=json.dumps(plan, ensure_ascii=False, indent=2),
            artifact_ref=str(path),
            audit={
                "path": str(path),
                "executed": False,
                "reason": "browser plan only; no browser automation was run",
            },
            message="Browser operation plan created. No browser action was executed.",
        )


class WorldGateway:
    """Prepare and audit side-effect actions."""

    def __init__(
        self,
        *,
        hermes_adapter: HermesAdapter | None = None,
        artifact_root: str | Path | None = None,
        handlers: list[WorldActionHandler] | None = None,
    ) -> None:
        self.hermes_adapter = hermes_adapter or DefaultHermesAdapter()
        self.artifact_root = (
            Path(artifact_root or os.getenv("KUN_WORLD_ARTIFACT_ROOT") or ".kun-world")
            .expanduser()
            .resolve()
        )
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.handlers: dict[str, WorldActionHandler] = {}
        for handler in handlers or self._default_handlers():
            self.register_handler(handler)

    def register_handler(self, handler: WorldActionHandler) -> None:
        self.handlers[handler.action_type] = handler

    def supported_action_types(self) -> list[str]:
        return sorted(self.handlers)

    def handler_descriptors(self) -> list[WorldHandlerDescriptor]:
        return [
            WorldHandlerDescriptor(
                action_type=handler.action_type,
                handler_id=handler.handler_id,
                mode=handler.mode,
                external_dispatched=handler.external_dispatched,
                artifact_kind=handler.artifact_kind,
                safety_note=handler.safety_note,
            )
            for handler in sorted(self.handlers.values(), key=lambda item: item.action_type)
        ]

    async def execute_approved(self, action: WorldAction) -> WorldGatewayResult:
        target = self._target_for(action.action_type)
        packet = await self.hermes_adapter.translate_external(
            target=target,
            payload={
                "action_id": action.action_id,
                "task_ref": action.task_ref,
                "action_type": action.action_type,
                "target_ref": action.target_ref,
                "payload": action.payload,
            },
            context={
                "risk_level": action.risk_level,
                "gateway_mode": "approval_gate",
                "method": "side_effect.prepare",
            },
        )
        now = datetime.now(UTC).isoformat()
        handler = self.handlers.get(action.action_type)
        if handler is not None:
            handler_result = await handler.execute(action)
            return WorldGatewayResult(
                action_id=action.action_id,
                gateway_mode=f"handler_{handler_result.status}",
                external_dispatched=handler_result.external_dispatched,
                requires_handler=False,
                rendered_payload=handler_result.rendered_payload or packet.rendered,
                audit={
                    "prepared_at": now,
                    "target": target,
                    "risk_level": action.risk_level,
                    "action_type": action.action_type,
                    "external_dispatched": handler_result.external_dispatched,
                    "requires_handler": False,
                    "handler_id": handler_result.handler_id,
                    "handler_status": handler_result.status,
                    "artifact_ref": handler_result.artifact_ref,
                    **handler_result.audit,
                },
                message=handler_result.message,
            )

        return WorldGatewayResult(
            action_id=action.action_id,
            rendered_payload=packet.rendered,
            audit={
                "prepared_at": now,
                "target": target,
                "risk_level": action.risk_level,
                "action_type": action.action_type,
                "external_dispatched": False,
                "requires_handler": True,
                "supported_action_types": self.supported_action_types(),
                "reason": "no delivery handler registered for this action_type",
            },
        )

    def _target_for(self, action_type: str) -> Literal["api", "external_agent", "human"]:
        if action_type.startswith(("message.", "content.", "payment.", "deployment.")):
            return "api"
        if action_type.startswith("external_agent."):
            return "external_agent"
        return "human"

    def _default_handlers(self) -> list[WorldActionHandler]:
        return [
            LocalFileWriteHandler(self.artifact_root / "files"),
            EmailDraftHandler(self.artifact_root),
            WebhookPostDryRunHandler(),
            BrowserPlanHandler(self.artifact_root),
        ]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_artifact_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80]


_gateway: WorldGateway | None = None


def get_world_gateway() -> WorldGateway:
    global _gateway
    if _gateway is None:
        _gateway = WorldGateway()
    return _gateway


def set_world_gateway(gateway: WorldGateway) -> None:
    global _gateway
    _gateway = gateway


__all__ = [
    "BrowserPlanHandler",
    "EmailDraftHandler",
    "LocalFileWriteHandler",
    "WebhookPostDryRunHandler",
    "WorldAction",
    "WorldActionHandler",
    "WorldGateway",
    "WorldGatewayResult",
    "WorldHandlerDescriptor",
    "WorldHandlerResult",
    "get_world_gateway",
    "set_world_gateway",
]
