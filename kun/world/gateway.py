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
from difflib import unified_diff
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from kun.interface.hermes import DefaultHermesAdapter, HermesAdapter

_PREVIEW_MAX_CHARS = 12_000


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
    capability_status: Literal[
        "supported_execute",
        "supported_draft",
        "supported_dry_run",
        "supported_plan",
        "missing_handler",
        "preview_failed",
    ] = "missing_handler"
    external_dispatched: bool = False
    requires_handler: bool = True
    rendered_payload: str = ""
    audit: dict[str, Any] = Field(default_factory=dict)
    user_summary: str = "这个动作还没有真实执行器。"
    next_step: str = "先补 WorldGateway handler，或改成已有的低风险动作类型。"
    permissions_required: list[str] = Field(default_factory=list)
    message: str = (
        "World Gateway recorded and authorized this action, but no external "
        "delivery handler is attached yet."
    )


class WorldHandlerResult(BaseModel):
    """Result returned by a concrete low-risk world handler."""

    handler_id: str
    status: Literal["executed", "drafted", "dry_run", "preview"]
    external_dispatched: bool = False
    rendered_payload: str = ""
    artifact_ref: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    message: str


class WorldHandlerDescriptor(BaseModel):
    """User-facing capability descriptor for one gateway handler."""

    action_type: str
    handler_id: str
    user_label: str
    mode: Literal["execute", "draft", "dry_run", "plan"]
    external_dispatched: bool = False
    artifact_kind: str = ""
    safety_note: str
    approval_effect: str
    cannot_do: list[str] = Field(default_factory=list)
    permissions_required: list[str] = Field(default_factory=list)
    next_step: str = ""


class WorldActionHandler:
    """Base class for concrete WorldGateway handlers."""

    action_type: ClassVar[str]
    handler_id: ClassVar[str]
    mode: ClassVar[Literal["execute", "draft", "dry_run", "plan"]] = "dry_run"
    external_dispatched: ClassVar[bool] = False
    artifact_kind: ClassVar[str] = ""
    safety_note: ClassVar[str] = "Handled by World Gateway."
    user_label: ClassVar[str] = "外部动作"
    approval_effect: ClassVar[str] = "批准后由 World Gateway 处理。"
    cannot_do: ClassVar[list[str]] = []
    permissions_required: ClassVar[list[str]] = []
    next_step: ClassVar[str] = "查看执行结果和审计记录。"

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        """Return a no-side-effect preview for human approval."""
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            audit={"action_type": action.action_type},
            message=self.safety_note,
        )

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
    user_label = "写入本地文件"
    approval_effect = "批准后会在 KUN 受控输出目录里写文件。"
    cannot_do: ClassVar[list[str]] = ["不能写绝对路径", "不能写出受控输出目录"]
    permissions_required: ClassVar[list[str]] = ["human_approval", "controlled_output_dir"]
    next_step = "批准前先看 diff；批准后检查产物路径。"

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        target, content = self._resolve_target_and_content(action)
        existed_before = target.exists()
        previous = target.read_text(encoding="utf-8") if existed_before else ""
        relative_path = str(target.relative_to(self.output_root))
        diff_text, truncated = _render_unified_diff(
            previous=previous,
            proposed=content,
            fromfile=relative_path if existed_before else "/dev/null",
            tofile=relative_path,
        )
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=diff_text,
            audit={
                "output_root": str(self.output_root),
                "path": str(target),
                "relative_path": relative_path,
                "bytes": len(content.encode("utf-8")),
                "existed_before": existed_before,
                "would_create": not existed_before,
                "would_overwrite": existed_before,
                "diff_truncated": truncated,
            },
            message=(
                "Preview only. Approval will write this file under the controlled "
                "KUN output directory."
            ),
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        target, content = self._resolve_target_and_content(action)
        existed_before = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
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

    def _resolve_target_and_content(self, action: WorldAction) -> tuple[Path, str]:
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
        return target, content


class EmailDraftHandler(WorldActionHandler):
    """Create an email draft artifact; never sends mail."""

    action_type = "email.draft"
    handler_id = "email.draft.v1"
    mode = "draft"
    external_dispatched = False
    artifact_kind = "email_draft"
    safety_note = "只生成邮件草稿文件，不会真实发送邮件。"
    user_label = "生成邮件草稿"
    approval_effect = "批准后只生成邮件草稿，不会发送。"
    cannot_do: ClassVar[list[str]] = ["不能真实发送邮件", "不能调用邮箱服务"]
    permissions_required: ClassVar[list[str]] = ["human_approval"]
    next_step = "检查草稿内容，需要真实发送时再接 email.send handler。"

    def __init__(self, output_root: str | Path) -> None:
        self.draft_root = Path(output_root).expanduser().resolve() / "email_drafts"
        self.draft_root.mkdir(parents=True, exist_ok=True)

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        draft = self._draft(action)
        rendered = json.dumps(draft, ensure_ascii=False, indent=2)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                "sent": False,
                "recipient": draft["to"],
                "reason": "preview only; no email will be sent",
            },
            message="Preview only. Approval will create an email draft artifact; it will not send mail.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        draft = self._draft(action)
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

    def _draft(self, action: WorldAction) -> dict[str, Any]:
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
        return draft


class WebhookPostDryRunHandler(WorldActionHandler):
    """Render a webhook POST request without sending it."""

    action_type = "webhook.post_dry_run"
    handler_id = "webhook.post_dry_run.v1"
    mode = "dry_run"
    external_dispatched = False
    artifact_kind = "http_request_preview"
    safety_note = "只渲染 POST 请求包，不会发起网络请求。"
    user_label = "渲染 Webhook 请求"
    approval_effect = "批准后只生成请求预览，不会联网。"
    cannot_do: ClassVar[list[str]] = ["不能真实发起网络请求", "不能调用外部 API"]
    permissions_required: ClassVar[list[str]] = ["human_approval"]
    next_step = "确认请求包正确后，再接真实 API handler。"

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        request, parsed = self._request(action)
        rendered = json.dumps(request, ensure_ascii=False, indent=2)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                "url": request["url"],
                "host": parsed.netloc,
                "dry_run": True,
                "reason": "preview only; no network call will be made",
            },
            message="Preview only. Approval will render this request in dry-run mode.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        request, parsed = self._request(action)
        rendered = json.dumps(request, ensure_ascii=False, indent=2)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="dry_run",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                "url": request["url"],
                "host": parsed.netloc,
                "dry_run": True,
                "reason": "request rendered only; no network call was made",
            },
            message="Webhook request rendered in dry-run mode. No network call was made.",
        )

    def _request(self, action: WorldAction) -> tuple[dict[str, Any], Any]:
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
        return request, parsed


class BrowserPlanHandler(WorldActionHandler):
    """Create a browser operation plan; never controls a browser."""

    action_type = "browser.plan"
    handler_id = "browser.plan.v1"
    mode = "plan"
    external_dispatched = False
    artifact_kind = "browser_plan"
    safety_note = "只生成浏览器操作计划，不会真实点击或控制浏览器。"
    user_label = "生成浏览器操作计划"
    approval_effect = "批准后只生成浏览器计划，不会点击网页。"
    cannot_do: ClassVar[list[str]] = ["不能真实打开浏览器", "不能提交表单", "不能点击网页"]
    permissions_required: ClassVar[list[str]] = ["human_approval"]
    next_step = "检查操作计划，需要真实浏览器执行时再接 browser.execute handler。"

    def __init__(self, output_root: str | Path) -> None:
        self.plan_root = Path(output_root).expanduser().resolve() / "browser_plans"
        self.plan_root.mkdir(parents=True, exist_ok=True)

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        plan = self._plan(action)
        rendered = json.dumps(plan, ensure_ascii=False, indent=2)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                "executed": False,
                "reason": "preview only; no browser automation will be run",
            },
            message="Preview only. Approval will create a browser plan artifact; it will not click.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        plan = self._plan(action)
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

    def _plan(self, action: WorldAction) -> dict[str, Any]:
        plan = {
            "url": action.payload.get("url") or action.target_ref,
            "objective": action.payload.get("objective", ""),
            "steps": action.payload.get("steps", []),
            "executed": False,
            "task_ref": action.task_ref,
            "action_id": action.action_id,
        }
        return plan


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
                user_label=handler.user_label,
                mode=handler.mode,
                external_dispatched=handler.external_dispatched,
                artifact_kind=handler.artifact_kind,
                safety_note=handler.safety_note,
                approval_effect=handler.approval_effect,
                cannot_do=handler.cannot_do,
                permissions_required=handler.permissions_required,
                next_step=handler.next_step,
            )
            for handler in sorted(self.handlers.values(), key=lambda item: item.action_type)
        ]

    async def preview(self, action: WorldAction) -> WorldGatewayResult:
        target = self._target_for(action.action_type)
        now = datetime.now(UTC).isoformat()
        handler = self.handlers.get(action.action_type)
        if handler is None:
            packet = await self._translate_packet(action, target=target, gateway_mode="preview")
            return WorldGatewayResult(
                action_id=action.action_id,
                gateway_mode="missing_handler_preview",
                capability_status="missing_handler",
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
                user_summary="这个动作目前没有执行器；批准后只会留下审计记录，不会真实外发。",
                next_step=(
                    "把动作改成已支持类型，或先补一个 WorldGateway handler，"
                    "再让 KUN 执行真实外部动作。"
                ),
                message=(
                    "No World Gateway handler is attached for this action type. "
                    "Approval will only create an audit packet."
                ),
            )

        handler_result = await handler.preview(action)
        return WorldGatewayResult(
            action_id=action.action_id,
            gateway_mode="handler_preview",
            capability_status=_capability_status(handler),
            external_dispatched=False,
            requires_handler=False,
            rendered_payload=handler_result.rendered_payload,
            audit={
                "prepared_at": now,
                "target": target,
                "risk_level": action.risk_level,
                "action_type": action.action_type,
                "external_dispatched": False,
                "requires_handler": False,
                "handler_id": handler_result.handler_id,
                "handler_status": handler_result.status,
                "artifact_kind": handler.artifact_kind,
                **handler_result.audit,
            },
            user_summary=_preview_summary(handler),
            next_step=handler.next_step,
            permissions_required=handler.permissions_required,
            message=handler_result.message,
        )

    async def execute_approved(self, action: WorldAction) -> WorldGatewayResult:
        target = self._target_for(action.action_type)
        packet = await self._translate_packet(
            action,
            target=target,
            gateway_mode="approval_gate",
        )
        now = datetime.now(UTC).isoformat()
        handler = self.handlers.get(action.action_type)
        if handler is not None:
            handler_result = await handler.execute(action)
            return WorldGatewayResult(
                action_id=action.action_id,
                gateway_mode=f"handler_{handler_result.status}",
                capability_status=_capability_status(handler),
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
                user_summary=_execution_summary(handler),
                next_step=handler.next_step,
                permissions_required=handler.permissions_required,
                message=handler_result.message,
            )

        return WorldGatewayResult(
            action_id=action.action_id,
            capability_status="missing_handler",
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
            user_summary="这个动作没有执行器；本次只记录审计，不会真实外发。",
            next_step=(
                "先补 action_type 对应的 WorldGateway handler，或改用 "
                f"{', '.join(self.supported_action_types())}。"
            ),
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

    async def _translate_packet(
        self,
        action: WorldAction,
        *,
        target: Literal["api", "external_agent", "human"],
        gateway_mode: str,
    ) -> Any:
        return await self.hermes_adapter.translate_external(
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
                "gateway_mode": gateway_mode,
                "method": "side_effect.prepare",
            },
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_artifact_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80]


def _capability_status(
    handler: WorldActionHandler,
) -> Literal[
    "supported_execute",
    "supported_draft",
    "supported_dry_run",
    "supported_plan",
]:
    if handler.mode == "execute":
        return "supported_execute"
    if handler.mode == "draft":
        return "supported_draft"
    if handler.mode == "dry_run":
        return "supported_dry_run"
    return "supported_plan"


def _preview_summary(handler: WorldActionHandler) -> str:
    if handler.mode == "execute":
        return "这个动作已支持；批准后会执行受控动作。"
    if handler.mode == "draft":
        return "这个动作已支持；批准后只生成草稿，不会外发。"
    if handler.mode == "dry_run":
        return "这个动作已支持；批准后只生成 dry-run 请求包，不会联网。"
    return "这个动作已支持；批准后只生成计划，不会真实操作。"


def _execution_summary(handler: WorldActionHandler) -> str:
    if handler.mode == "execute":
        return "WorldGateway 已执行受控动作，并留下审计。"
    if handler.mode == "draft":
        return "WorldGateway 已生成草稿，没有真实外发。"
    if handler.mode == "dry_run":
        return "WorldGateway 已生成 dry-run 请求包，没有联网。"
    return "WorldGateway 已生成操作计划，没有真实操作外部系统。"


def _render_unified_diff(
    *,
    previous: str,
    proposed: str,
    fromfile: str,
    tofile: str,
) -> tuple[str, bool]:
    diff = "".join(
        unified_diff(
            previous.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )
    if not diff:
        diff = "(no content change)"
    if len(diff) <= _PREVIEW_MAX_CHARS:
        return diff, False
    return diff[:_PREVIEW_MAX_CHARS] + "\n... diff truncated ...", True


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
