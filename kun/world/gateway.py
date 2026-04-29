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
import smtplib
import ssl
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from difflib import unified_diff
from email.message import EmailMessage
from inspect import isawaitable
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

import httpx
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


class EmailSendHandler(WorldActionHandler):
    """Send email through an explicitly configured SMTP account."""

    action_type = "email.send"
    handler_id = "email.send.smtp.v1"
    mode = "execute"
    external_dispatched = True
    artifact_kind = "email_send_audit"
    safety_note = "真实发送邮件。默认不启用；必须配置 KUN_WORLD_EMAIL_SEND_ENABLED=true 和 SMTP。"
    user_label = "真实发送邮件"
    approval_effect = "批准后会通过已配置 SMTP 账号真实发出邮件。"
    cannot_do: ClassVar[list[str]] = ["不能自动撤回已成功送达的邮件"]
    permissions_required: ClassVar[list[str]] = [
        "human_approval",
        "smtp_credentials",
        "email_recipient_review",
    ]
    next_step = "批准前检查收件人、主题和正文；发送后只能补发更正邮件。"

    def __init__(
        self,
        *,
        output_root: str | Path,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str | None,
        smtp_password: str | None,
        smtp_from: str,
        use_tls: bool = True,
        sender: Callable[[EmailMessage], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
    ) -> None:
        self.audit_root = Path(output_root).expanduser().resolve() / "email_sent"
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from
        self.use_tls = use_tls
        self._sender = sender

    @classmethod
    def from_env(cls, output_root: str | Path) -> EmailSendHandler:
        host = os.getenv("KUN_WORLD_SMTP_HOST", "").strip()
        from_addr = os.getenv("KUN_WORLD_SMTP_FROM", "").strip()
        if not host or not from_addr:
            raise ValueError("email.send requires KUN_WORLD_SMTP_HOST and KUN_WORLD_SMTP_FROM")
        return cls(
            output_root=output_root,
            smtp_host=host,
            smtp_port=int(os.getenv("KUN_WORLD_SMTP_PORT", "587")),
            smtp_username=_empty_to_none(os.getenv("KUN_WORLD_SMTP_USERNAME")),
            smtp_password=_empty_to_none(os.getenv("KUN_WORLD_SMTP_PASSWORD")),
            smtp_from=from_addr,
            use_tls=_env_bool("KUN_WORLD_SMTP_TLS", default=True),
        )

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        message, audit = self._message(action)
        rendered = _render_email_preview(message)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=rendered,
            audit={
                **audit,
                "sent": False,
                "smtp_host": self.smtp_host,
                "compensation": "cannot_recall_automatically; send follow-up correction if needed",
            },
            message="Preview only. Approval will send this email through the configured SMTP account.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        message, audit = self._message(action)
        send_result = await self._send(message)
        rendered = _render_email_preview(message)
        path = self.audit_root / f"{_safe_artifact_name(action.action_id)}.json"
        audit_payload = {
            **audit,
            **send_result,
            "sent": True,
            "smtp_host": self.smtp_host,
            "compensation": "cannot_recall_automatically; send follow-up correction if needed",
        }
        path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="executed",
            external_dispatched=True,
            rendered_payload=rendered,
            artifact_ref=str(path),
            audit=audit_payload,
            message="Email sent through the configured SMTP account. Audit artifact written.",
        )

    async def _send(self, message: EmailMessage) -> dict[str, Any]:
        if self._sender is not None:
            result = self._sender(message)
            if isawaitable(result):
                awaited = await result
                return dict(awaited)
            return result
        return await _send_email_smtp(
            message,
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_username,
            password=self.smtp_password,
            use_tls=self.use_tls,
        )

    def _message(self, action: WorldAction) -> tuple[EmailMessage, dict[str, Any]]:
        to_values = _string_list(action.payload.get("to") or action.target_ref)
        if not to_values:
            raise ValueError("email.send requires payload.to or target_ref")
        subject = str(action.payload.get("subject") or "").strip()
        body = str(action.payload.get("body") or "").strip()
        if not subject or not body:
            raise ValueError("email.send requires subject and body")
        cc_values = _string_list(action.payload.get("cc"))
        bcc_values = _string_list(action.payload.get("bcc"))
        message = EmailMessage()
        message["From"] = self.smtp_from
        message["To"] = ", ".join(to_values)
        if cc_values:
            message["Cc"] = ", ".join(cc_values)
        if bcc_values:
            message["Bcc"] = ", ".join(bcc_values)
        message["Subject"] = subject
        message["X-KUN-Action-Id"] = action.action_id
        message.set_content(body)
        return message, {
            "to": to_values,
            "cc": cc_values,
            "bcc_count": len(bcc_values),
            "subject": subject,
            "body_bytes": len(body.encode("utf-8")),
            "message_id": str(uuid.uuid4()),
        }


class EnterpriseApiPostHandler(WorldActionHandler):
    """POST JSON to an allowlisted enterprise API host."""

    action_type = "enterprise_api.post"
    handler_id = "enterprise_api.post.v1"
    mode = "execute"
    external_dispatched = True
    artifact_kind = "api_call_audit"
    safety_note = (
        "真实调用企业 API。默认不启用；只允许 HTTPS + KUN_WORLD_API_ALLOWED_HOSTS 白名单。"
    )
    user_label = "调用企业 API"
    approval_effect = "批准后会向白名单企业 API 发起真实 POST 请求。"
    cannot_do: ClassVar[list[str]] = ["不能自动撤销已被对方系统处理的请求"]
    permissions_required: ClassVar[list[str]] = [
        "human_approval",
        "api_host_allowlist",
        "api_credentials",
    ]
    next_step = "批准前检查 URL、JSON 和幂等键；失败时按对方系统规则补偿。"

    def __init__(
        self,
        *,
        output_root: str | Path,
        allowed_hosts: set[str],
        timeout_sec: float = 10.0,
        auth_header: str | None = None,
        auth_value: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("enterprise_api.post requires at least one allowed host")
        self.audit_root = Path(output_root).expanduser().resolve() / "api_calls"
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.allowed_hosts = allowed_hosts
        self.timeout_sec = timeout_sec
        self.auth_header = auth_header
        self.auth_value = auth_value
        self._client = client

    @classmethod
    def from_env(cls, output_root: str | Path) -> EnterpriseApiPostHandler:
        allowed_hosts = _csv_set(os.getenv("KUN_WORLD_API_ALLOWED_HOSTS", ""))
        return cls(
            output_root=output_root,
            allowed_hosts=allowed_hosts,
            timeout_sec=float(os.getenv("KUN_WORLD_API_TIMEOUT_SEC", "10")),
            auth_header=_empty_to_none(os.getenv("KUN_WORLD_API_AUTH_HEADER")),
            auth_value=_empty_to_none(os.getenv("KUN_WORLD_API_AUTH_VALUE")),
        )

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        request = self._request(action)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=json.dumps(_redact_request(request), ensure_ascii=False, indent=2),
            audit={
                "url": request["url"],
                "host": request["host"],
                "would_post": True,
                "allowed_hosts": sorted(self.allowed_hosts),
                "compensation": "depends_on_remote_api; use idempotency key or follow-up reversal endpoint",
            },
            message="Preview only. Approval will POST JSON to the allowlisted enterprise API.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        request = self._request(action)
        response_audit = await self._post(request)
        path = self.audit_root / f"{_safe_artifact_name(action.action_id)}.json"
        audit_payload = {
            **response_audit,
            "url": request["url"],
            "host": request["host"],
            "request_json_bytes": len(json.dumps(request["json"], ensure_ascii=False).encode()),
            "compensation": "depends_on_remote_api; use idempotency key or follow-up reversal endpoint",
        }
        path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="executed",
            external_dispatched=True,
            rendered_payload=json.dumps(_redact_request(request), ensure_ascii=False, indent=2),
            artifact_ref=str(path),
            audit=audit_payload,
            message="Enterprise API POST completed. Audit artifact written.",
        )

    async def _post(self, request: dict[str, Any]) -> dict[str, Any]:
        client = self._client
        if client is not None:
            response = await client.post(
                request["url"],
                json=request["json"],
                headers=request["headers"],
                timeout=self.timeout_sec,
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as transient:
                response = await transient.post(
                    request["url"],
                    json=request["json"],
                    headers=request["headers"],
                )
        return {
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "response_bytes": len(response.content),
            "response_preview": response.text[:1000],
        }

    def _request(self, action: WorldAction) -> dict[str, Any]:
        url = str(action.payload.get("url") or action.target_ref or "").strip()
        parsed = urlparse(url)
        _assert_allowed_https_host(
            parsed,
            allowed_hosts=self.allowed_hosts,
            action_type=self.action_type,
        )
        headers = _safe_api_headers(action.payload.get("headers"))
        if self.auth_header and self.auth_value:
            headers[self.auth_header] = self.auth_value
        if "Idempotency-Key" not in headers:
            headers["Idempotency-Key"] = action.action_id
        return {
            "method": "POST",
            "url": url,
            "host": parsed.hostname or "",
            "headers": headers,
            "json": action.payload.get("json", action.payload.get("body", {})),
        }


BrowserRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class BrowserExecuteHandler(WorldActionHandler):
    """Run a small allowlisted Playwright browser script."""

    action_type = "browser.execute"
    handler_id = "browser.execute.playwright.v1"
    mode = "execute"
    external_dispatched = True
    artifact_kind = "browser_execution_audit"
    safety_note = "真实控制浏览器。默认不启用；只允许白名单 HTTPS host 和有限 step 类型。"
    user_label = "真实浏览器执行"
    approval_effect = "批准后会打开浏览器并执行白名单步骤。"
    cannot_do: ClassVar[list[str]] = ["不支持支付/提交敏感表单的自动确认", "不能绕过网站权限"]
    permissions_required: ClassVar[list[str]] = [
        "human_approval",
        "browser_host_allowlist",
        "browser_action_review",
    ]
    next_step = "批准前检查 URL 和步骤；执行后查看截图/审计记录。"
    allowed_steps: ClassVar[set[str]] = {"goto", "click", "fill", "screenshot"}

    def __init__(
        self,
        *,
        output_root: str | Path,
        allowed_hosts: set[str],
        runner: BrowserRunner | None = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("browser.execute requires at least one allowed host")
        self.audit_root = Path(output_root).expanduser().resolve() / "browser_runs"
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.allowed_hosts = allowed_hosts
        self._runner = runner

    @classmethod
    def from_env(cls, output_root: str | Path) -> BrowserExecuteHandler:
        return cls(
            output_root=output_root,
            allowed_hosts=_csv_set(os.getenv("KUN_WORLD_BROWSER_ALLOWED_HOSTS", "")),
        )

    async def preview(self, action: WorldAction) -> WorldHandlerResult:
        plan = self._plan(action)
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="preview",
            external_dispatched=False,
            rendered_payload=json.dumps(plan, ensure_ascii=False, indent=2),
            audit={
                "url": plan["url"],
                "host": plan["host"],
                "step_count": len(plan["steps"]),
                "would_control_browser": True,
                "allowed_hosts": sorted(self.allowed_hosts),
                "compensation": "browser side effects depend on website; manual reversal may be required",
            },
            message="Preview only. Approval will run browser automation against an allowlisted host.",
        )

    async def execute(self, action: WorldAction) -> WorldHandlerResult:
        plan = self._plan(action)
        runner = self._runner or _run_browser_plan_with_playwright
        result = await runner({**plan, "artifact_root": str(self.audit_root)})
        path = self.audit_root / f"{_safe_artifact_name(action.action_id)}.json"
        audit_payload = {
            **result,
            "url": plan["url"],
            "host": plan["host"],
            "step_count": len(plan["steps"]),
            "compensation": "browser side effects depend on website; manual reversal may be required",
        }
        path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorldHandlerResult(
            handler_id=self.handler_id,
            status="executed",
            external_dispatched=True,
            rendered_payload=json.dumps(plan, ensure_ascii=False, indent=2),
            artifact_ref=str(path),
            audit=audit_payload,
            message="Browser automation completed. Audit artifact written.",
        )

    def _plan(self, action: WorldAction) -> dict[str, Any]:
        url = str(action.payload.get("url") or action.target_ref or "").strip()
        parsed = urlparse(url)
        _assert_allowed_https_host(
            parsed,
            allowed_hosts=self.allowed_hosts,
            action_type=self.action_type,
        )
        raw_steps = action.payload.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("browser.execute requires a non-empty payload.steps list")
        steps: list[dict[str, Any]] = []
        for idx, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise ValueError(f"browser.execute step {idx} must be an object")
            kind = str(raw.get("kind") or raw.get("type") or "").strip()
            if kind not in self.allowed_steps:
                raise ValueError(f"browser.execute unsupported step kind: {kind}")
            steps.append(
                {k: v for k, v in raw.items() if k in {"kind", "type", "selector", "text", "path"}}
            )
        return {
            "url": url,
            "host": parsed.hostname or "",
            "objective": action.payload.get("objective", ""),
            "steps": steps,
            "task_ref": action.task_ref,
            "action_id": action.action_id,
        }


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
        handlers: list[WorldActionHandler] = [
            LocalFileWriteHandler(self.artifact_root / "files"),
            EmailDraftHandler(self.artifact_root),
            WebhookPostDryRunHandler(),
            BrowserPlanHandler(self.artifact_root),
        ]
        if _env_bool("KUN_WORLD_EMAIL_SEND_ENABLED"):
            handlers.append(EmailSendHandler.from_env(self.artifact_root))
        if _env_bool("KUN_WORLD_API_POST_ENABLED"):
            handlers.append(EnterpriseApiPostHandler.from_env(self.artifact_root))
        if _env_bool("KUN_WORLD_BROWSER_EXECUTE_ENABLED"):
            handlers.append(BrowserExecuteHandler.from_env(self.artifact_root))
        return handlers

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


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _render_email_preview(message: EmailMessage) -> str:
    rendered = {
        "from": message.get("From", ""),
        "to": message.get("To", ""),
        "cc": message.get("Cc", ""),
        "bcc": "[redacted]" if message.get("Bcc") else "",
        "subject": message.get("Subject", ""),
        "body": message.get_content(),
    }
    return json.dumps(rendered, ensure_ascii=False, indent=2)


async def _send_email_smtp(
    message: EmailMessage,
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_tls: bool,
) -> dict[str, Any]:
    def send() -> dict[str, Any]:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.starttls(context=context)
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        return {
            "smtp_host": host,
            "smtp_port": port,
            "smtp_tls": use_tls,
            "smtp_username_set": bool(username),
        }

    import asyncio

    return await asyncio.to_thread(send)


def _safe_api_headers(raw_headers: Any) -> dict[str, str]:
    allowed = {"accept", "content-type", "idempotency-key", "x-request-id"}
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            key_text = str(key).strip()
            if key_text.lower() in allowed and str(value).strip():
                headers[key_text] = str(value).strip()
    return headers


def _redact_request(request: dict[str, Any]) -> dict[str, Any]:
    headers = dict(request.get("headers", {}))
    for key in list(headers):
        if key.lower() in {"authorization", "proxy-authorization", "x-api-key"}:
            headers[key] = "[redacted]"
    return {**request, "headers": headers}


def _assert_allowed_https_host(
    parsed: Any,
    *,
    allowed_hosts: set[str],
    action_type: str,
) -> None:
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError(f"{action_type} requires an https URL")
    if host not in allowed_hosts:
        raise ValueError(f"{action_type} host is not allowlisted: {host}")


async def _run_browser_plan_with_playwright(plan: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only when real handler is enabled
        raise RuntimeError(
            "browser.execute requires playwright. Install it and run playwright install first."
        ) from exc

    screenshot_paths: list[str] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(str(plan["url"]), wait_until="networkidle")
            for idx, step in enumerate(plan["steps"]):
                kind = str(step.get("kind") or step.get("type"))
                selector = str(step.get("selector") or "")
                if kind == "goto":
                    # URL is fixed by the allowlisted plan; step-level goto is intentionally ignored.
                    continue
                if kind == "click":
                    await page.click(selector)
                elif kind == "fill":
                    await page.fill(selector, str(step.get("text") or ""))
                elif kind == "screenshot":
                    raw_path = str(step.get("path") or f"screenshot-{idx}.png")
                    target = (
                        Path(str(plan["artifact_root"])) / _safe_artifact_name(raw_path)
                    ).resolve()
                    await page.screenshot(path=str(target), full_page=True)
                    screenshot_paths.append(str(target))
        finally:
            await browser.close()
    return {
        "executed": True,
        "screenshot_paths": screenshot_paths,
    }


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
    "BrowserExecuteHandler",
    "BrowserPlanHandler",
    "EmailDraftHandler",
    "EmailSendHandler",
    "EnterpriseApiPostHandler",
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
