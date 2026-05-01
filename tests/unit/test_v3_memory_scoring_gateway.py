"""V3 remaining core loop tests: memory writeback, scoring, World Gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.decision_ticket import (
    ticket_from_context_selection,
    ticket_from_protocol_applied,
    ticket_from_skill_selection,
    ticket_from_watchtower_decision,
)
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.memory.writeback import MemoryWriteback, MemoryWritebackResult
from kun.qi.protocol import Protocol, ProtocolExecution, ProtocolSkillStep, ProtocolTrigger
from kun.watchtower.decision_plane import WatchtowerDecisionPlane
from kun.watchtower.scoring import UnifiedScoringSystem
from kun.world.gateway import (
    BrowserExecuteHandler,
    EmailSendHandler,
    EnterpriseApiPostHandler,
    WorldAction,
    WorldGateway,
)


class _FakeSession:
    async def execute(self, *_args: object, **_kwargs: object) -> Any:
        class R:
            rowcount = 1

            def scalar_one_or_none(self) -> object | None:
                return None

            def scalar_one(self) -> int:
                return 0

            def all(self) -> list[object]:
                return []

            def one_or_none(self) -> object | None:
                return None

            def scalars(self) -> _FakeSession:
                return self

        return R()

    def add(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def flush(self) -> None:
        pass


@asynccontextmanager
async def _fake_session_scope(**_kwargs: object) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


class _RoutingStub(StubProvider):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = lambda _request: LLMResponse(
                content=(
                    '{"task_type": "education.lesson", "risk_level": "low", '
                    '"complexity_score": 0.4, "estimated_cost_usd": 0.05, '
                    '"estimated_duration_sec": 20, '
                    '"success_criteria_short": "设计一节复习课", '
                    '"goal_detail": "给用户设计一节复习课", '
                    '"success_metrics": ["覆盖关键知识点"]}'
                ),
                usage=UsageInfo(input_tokens=5, output_tokens=25),
            )
        else:
            self._builder = lambda _request: LLMResponse(
                content="复习课方案已完成。",
                usage=UsageInfo(input_tokens=10, output_tokens=6),
                model="stub-v3",
                provider="stub",
                tier="cheap",
            )
        return await super().invoke(request)


class _RecordingMemoryWriteback:
    def __init__(self) -> None:
        self.layers: list[str] = []

    async def record_meta_decision(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("meta_decision")
        return MemoryWritebackResult(
            asset_id="mm-meta",
            memory_layer="meta_decision",
            asset_kind="methodology",
            summary="meta",
        )

    async def record_process_step(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("execution_process")
        return MemoryWritebackResult(
            asset_id="mm-process",
            memory_layer="execution_process",
            asset_kind="memory",
            summary="process",
        )

    async def record_task_result(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("task_result")
        return MemoryWritebackResult(
            asset_id="mm-result",
            memory_layer="task_result",
            asset_kind="memory",
            summary="result",
        )


def _task_ref() -> TaskRef:
    owner = Owner(tenant_id="tenant-v3", user_id="u")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("设计学习计划", owner),
            task_type="education.lesson",
            risk_level="low",
            complexity_score=0.4,
            owner=owner,
            estimated_cost_usd=0.1,
            estimated_duration_sec=30,
            success_criteria_short="设计学习计划",
        ),
        spec=TaskSpec(
            goal_detail="给用户设计学习计划",
            required_skills=["lesson_planner"],
            success_metrics=["覆盖知识点"],
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_writeback_assets_are_retrievable_by_context_packer() -> None:
    store = InMemoryAssetStore()
    writeback = MemoryWriteback(store=store)
    task_ref = _task_ref()
    runtime = RuntimeState(task_ref=task_ref.meta.task_id, status="done")
    step = StepRecord(step_id=1, skill_used="lesson_planner", cost_usd_equivalent=0.02)
    runtime.accumulate_step(step)

    await writeback.record_process_step(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        step=step,
        answer="学习计划包括复习、练习和测验。",
        provider="stub",
        model="stub",
        tier="cheap",
    )
    await writeback.record_task_result(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        status="done",
        answer="学习计划已完成。",
        runtime=runtime,
        validation_outcome="pass",
        validation_score=0.9,
        surprise_score=0.2,
        score_overall=0.88,
    )

    pack = await ContextPacker(store=store).pack(task_ref, tenant_id="tenant-v3", limit=5)

    assert {item.asset_kind for item in pack.items} == {"memory"}
    assert any("任务结果" in item.summary for item in pack.items)
    assert any("执行过程" in item.summary for item in pack.items)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_result_memory_records_execution_path_for_strategy_reuse() -> None:
    store = InMemoryAssetStore()
    writeback = MemoryWriteback(store=store)
    task_ref = _task_ref()
    runtime = RuntimeState(task_ref=task_ref.meta.task_id, status="done")
    runtime.accumulate_step(StepRecord(step_id=1, skill_used="lesson_planner"))
    watchtower_ticket = ticket_from_watchtower_decision(
        tenant_id="tenant-v3",
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        estimated_cost_usd=task_ref.meta.estimated_cost_usd,
        decision=SimpleNamespace(
            strategy_pack_id="education",
            strategy_pack_name="Education",
            execution_mode="SMART",
            reason="命中教育任务策略",
            confidence=0.8,
            context_limit=3,
            skill_hints=["lesson_planner"],
            metric_dimensions=["success_rate"],
            reward_weights={"success_rate": 1.0},
            risk_watch=[],
            alert_flags=[],
        ),
    )
    context_ticket = ticket_from_context_selection(
        tenant_id="tenant-v3",
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        execution_mode="SMART",
        context_limit=3,
        context_pack=SimpleNamespace(
            items=[
                SimpleNamespace(
                    asset_id="memory-1",
                    asset_kind="memory",
                    relevance_score=0.9,
                )
            ]
        ),
    )
    skill_ticket = ticket_from_skill_selection(
        tenant_id="tenant-v3",
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        top_k=1,
        skills=[
            SimpleNamespace(
                skill_id="lesson_planner",
                manifest=SimpleNamespace(description="Plan lesson", maturity="stable"),
            )
        ],
    )

    await writeback.record_task_result(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        status="done",
        answer="学习计划已完成。",
        runtime=runtime,
        validation_outcome="pass",
        validation_score=0.9,
        surprise_score=0.2,
        score_overall=0.88,
        decision_tickets=[watchtower_ticket, context_ticket, skill_ticket],
    )
    assets = await store.list(tenant_id="tenant-v3", asset_kind="memory")
    result_asset = next(
        asset for asset in assets if asset.l1_metadata["memory_layer"] == "task_result"
    )

    assert result_asset.l1_metadata["strategy_pack_id"] == "education"
    assert result_asset.l1_metadata["execution_mode"] == "SMART"
    assert result_asset.l1_metadata["skill_ids"] == ["lesson_planner"]
    assert result_asset.l1_metadata["context_asset_ids"] == ["memory-1"]
    assert result_asset.l1_metadata["decision_path"][0]["decision_point"] == "strategy_selected"
    assert "education" in result_asset.tags
    assert "SMART" in result_asset.tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_writeback_records_protocol_as_meta_decision() -> None:
    store = InMemoryAssetStore()
    writeback = MemoryWriteback(store=store)
    task_ref = _task_ref()
    protocol = Protocol(
        protocol_id="education.lesson.plan",
        version="1.0.0",
        tenant_id="tenant-v3",
        status="stable",
        trigger=ProtocolTrigger(task_type_pattern="education.*"),
        execution=ProtocolExecution(mode="MAX"),
        skill_chain=[ProtocolSkillStep(skill="lesson_planner")],
    )
    ticket = ticket_from_protocol_applied(
        tenant_id="tenant-v3",
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        estimated_cost_usd=task_ref.meta.estimated_cost_usd,
        protocol=protocol,
    )

    result = await writeback.record_meta_decision(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        decision=protocol,
        decision_ticket=ticket,
    )
    assets = await store.list(tenant_id="tenant-v3", asset_kind="methodology")

    assert result.memory_layer == "meta_decision"
    assert assets[0].l1_metadata["decision_point"] == "protocol_applied"
    assert assets[0].l1_metadata["strategy_pack_id"] == "education.lesson.plan"
    assert assets[0].l1_metadata["execution_mode"] == "MAX"
    assert "lesson_planner" in assets[0].l1_metadata["skill_hints"]


@pytest.mark.unit
def test_unified_scorecard_uses_real_runtime_signals() -> None:
    task_ref = _task_ref()
    runtime = RuntimeState(task_ref=task_ref.meta.task_id, status="done")
    runtime.accumulate_step(
        StepRecord(step_id=1, skill_used="lesson_planner", cost_usd_equivalent=0.02)
    )
    decision = WatchtowerDecisionPlane().decide(task_ref)

    scorecard = UnifiedScoringSystem().score_task(
        task_ref=task_ref,
        runtime=runtime,
        status="done",
        validation_outcome="pass",
        validation_score=0.9,
        surprise_score=0.3,
        decision=decision,
    )

    assert scorecard.strategy_pack_id == "education"
    assert scorecard.overall > 0.7
    assert set(scorecard.metrics) >= {"success_rate", "cost", "risk", "reuse_value"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_consumes_memory_writeback_and_scorecard(monkeypatch) -> None:
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    stub = _RoutingStub(tier="top", latency_ms=0)
    set_router(LLMRouter({"top": stub, "cheap": stub, "fallback": stub, "coding": stub}))
    memory = _RecordingMemoryWriteback()
    orch = Orchestrator(
        decision_plane=WatchtowerDecisionPlane(),
        memory_writeback=memory,
        scoring_system=UnifiedScoringSystem(),
        output_translator=_identity_translator,
    )

    events = []
    async for event in orch.stream("帮我设计一节复习课"):
        events.append(event)

    assert {"meta_decision", "execution_process", "task_result"} <= set(memory.layers)
    assert any(event.kind == "scorecard" for event in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_records_audit_without_fake_external_dispatch() -> None:
    gateway = WorldGateway(artifact_root="/tmp/kun-test-world")

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-1",
            task_ref="task-1",
            action_type="message.send",
            target_ref="customer:1",
            risk_level="high",
            payload={"body": "hello"},
        )
    )

    assert result.external_dispatched is False
    assert result.requires_handler is True
    assert result.capability_status == "missing_handler"
    assert "记录审计" in result.user_summary
    assert "WorldGateway handler" in result.next_step
    assert result.audit["target"] == "api"
    assert "no delivery handler" in result.audit["reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_local_file_write_handler(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-file",
            task_ref="task-1",
            action_type="local_file.write",
            target_ref="reports/hello.txt",
            risk_level="low",
            payload={"content": "hello"},
        )
    )

    assert result.requires_handler is False
    assert result.external_dispatched is True
    assert result.capability_status == "supported_execute"
    assert "已执行受控动作" in result.user_summary
    assert result.gateway_mode == "handler_executed"
    path = tmp_path / "files" / "reports" / "hello.txt"
    assert path.read_text(encoding="utf-8") == "hello"
    assert result.audit["handler_id"] == "local_file.write.v1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_local_file_preview_diff_without_writing(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.preview(
        WorldAction(
            action_id="act-file-preview",
            task_ref="task-1",
            action_type="local_file.write",
            target_ref="reports/hello.txt",
            risk_level="low",
            payload={"content": "hello\n"},
        )
    )

    assert result.gateway_mode == "handler_preview"
    assert result.requires_handler is False
    assert result.external_dispatched is False
    assert result.capability_status == "supported_execute"
    assert "批准后会执行受控动作" in result.user_summary
    assert result.audit["handler_id"] == "local_file.write.v1"
    assert result.audit["would_create"] is True
    assert "+hello" in result.rendered_payload
    assert not (tmp_path / "files" / "reports" / "hello.txt").exists()


@pytest.mark.unit
def test_world_gateway_exposes_handler_registry(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    descriptors = {item.action_type: item for item in gateway.handler_descriptors()}

    assert descriptors["local_file.write"].mode == "execute"
    assert descriptors["local_file.write"].external_dispatched is True
    assert descriptors["local_file.write"].user_label == "写入本地文件"
    assert "受控输出目录" in descriptors["local_file.write"].approval_effect
    assert "不能写绝对路径" in descriptors["local_file.write"].cannot_do
    assert descriptors["email.draft"].mode == "draft"
    assert descriptors["email.draft"].external_dispatched is False
    assert "不能真实发送邮件" in descriptors["email.draft"].cannot_do
    assert descriptors["webhook.post_dry_run"].mode == "dry_run"
    assert descriptors["browser.plan"].mode == "plan"
    assert descriptors["local_file.write"].retry_policy
    assert descriptors["local_file.write"].compensation_strategy
    assert descriptors["email.draft"].requires_external_dispatch_confirmation is False


@pytest.mark.unit
def test_world_gateway_real_handlers_are_opt_in_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KUN_WORLD_EMAIL_SEND_ENABLED", raising=False)
    monkeypatch.delenv("KUN_WORLD_API_POST_ENABLED", raising=False)
    monkeypatch.delenv("KUN_WORLD_BROWSER_EXECUTE_ENABLED", raising=False)

    gateway = WorldGateway(artifact_root=tmp_path)

    assert "email.send" not in gateway.supported_action_types()
    assert "enterprise_api.post" not in gateway.supported_action_types()
    assert "browser.execute" not in gateway.supported_action_types()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_email_send_handler_can_use_injected_sender(tmp_path) -> None:
    sent: list[EmailMessage] = []

    async def sender(message: EmailMessage) -> dict[str, Any]:
        sent.append(message)
        return {"provider_message_id": "smtp-1"}

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            EmailSendHandler(
                output_root=tmp_path,
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username=None,
                smtp_password=None,
                smtp_from="kun@example.com",
                allowed_recipient_domains={"example.com"},
                sender=sender,
            )
        ],
    )

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-email-send",
            task_ref="task-1",
            action_type="email.send",
            target_ref="user@example.com",
            risk_level="high",
            payload={
                "subject": "Hi",
                "body": "Real send",
                "to": "user@example.com",
                "external_dispatch_confirmed": True,
            },
        )
    )

    assert len(sent) == 1
    assert sent[0]["To"] == "user@example.com"
    assert result.external_dispatched is True
    assert result.requires_handler is False
    assert result.audit["provider_message_id"] == "smtp-1"
    assert result.audit["compensation"].startswith("cannot_recall")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_email_send_uses_tenant_scoped_config(
    tmp_path,
    monkeypatch,
) -> None:
    sent: list[EmailMessage] = []

    async def sender(message: EmailMessage) -> dict[str, Any]:
        sent.append(message)
        return {"provider_message_id": "smtp-tenant"}

    monkeypatch.setenv("KUN_TENANT_TENANT_A_WORLD_SMTP_FROM", "tenant-a@example.com")
    monkeypatch.setenv("KUN_TENANT_TENANT_A_WORLD_SMTP_HOST", "smtp.tenant-a.example.com")

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            EmailSendHandler(
                output_root=tmp_path,
                smtp_host="smtp.global.example.com",
                smtp_port=587,
                smtp_username=None,
                smtp_password=None,
                smtp_from="global@example.com",
                allowed_recipient_domains={"example.com"},
                sender=sender,
            )
        ],
    )

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-email-tenant",
            tenant_id="tenant-a",
            task_ref="task-1",
            action_type="email.send",
            target_ref="user@example.com",
            risk_level="high",
            payload={
                "subject": "Hi",
                "body": "Tenant send",
                "to": "user@example.com",
                "external_dispatch_confirmed": True,
            },
        )
    )

    assert sent[0]["From"] == "tenant-a@example.com"
    assert result.audit["smtp_host"] == "smtp.tenant-a.example.com"
    assert result.audit["tenant_scoped_config"] is True
    assert result.audit["config_source"] == "tenant_override"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_blocks_real_email_to_unapproved_domain(tmp_path) -> None:
    sent: list[EmailMessage] = []

    async def sender(message: EmailMessage) -> dict[str, Any]:
        sent.append(message)
        return {"provider_message_id": "smtp-1"}

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            EmailSendHandler(
                output_root=tmp_path,
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username=None,
                smtp_password=None,
                smtp_from="kun@example.com",
                allowed_recipient_domains={"example.com"},
                sender=sender,
            )
        ],
    )

    with pytest.raises(ValueError, match="recipient domain is not allowlisted"):
        await gateway.execute_approved(
            WorldAction(
                action_id="act-email-domain-blocked",
                task_ref="task-1",
                action_type="email.send",
                target_ref="user@unknown.test",
                risk_level="high",
                payload={
                    "subject": "Hi",
                    "body": "Real send",
                    "to": "user@unknown.test",
                    "external_dispatch_confirmed": True,
                },
            )
        )

    assert sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_blocks_real_email_without_external_confirmation(tmp_path) -> None:
    sent: list[EmailMessage] = []

    async def sender(message: EmailMessage) -> dict[str, Any]:
        sent.append(message)
        return {"provider_message_id": "smtp-1"}

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            EmailSendHandler(
                output_root=tmp_path,
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username=None,
                smtp_password=None,
                smtp_from="kun@example.com",
                allowed_recipient_domains={"example.com"},
                sender=sender,
            )
        ],
    )

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-email-send-blocked",
            task_ref="task-1",
            action_type="email.send",
            target_ref="user@example.com",
            risk_level="high",
            payload={"subject": "Hi", "body": "Real send", "to": "user@example.com"},
        )
    )

    assert sent == []
    assert result.gateway_mode == "policy_blocked"
    assert result.external_dispatched is False
    assert result.requires_handler is False
    assert "external_dispatch_confirmation" in result.permissions_required
    assert result.audit["policy"]["allowed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_enterprise_api_requires_allowlisted_https_host(tmp_path) -> None:
    handler = EnterpriseApiPostHandler(
        output_root=tmp_path,
        allowed_hosts={"api.example.com"},
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
        ),
    )
    gateway = WorldGateway(artifact_root=tmp_path, handlers=[handler])

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-api",
            task_ref="task-1",
            action_type="enterprise_api.post",
            target_ref="https://api.example.com/orders",
            risk_level="high",
            payload={
                "json": {"order_id": "o-1"},
                "external_dispatch_confirmed": True,
            },
        )
    )

    assert result.external_dispatched is True
    assert result.audit["status_code"] == 200
    assert result.audit["host"] == "api.example.com"

    with pytest.raises(ValueError, match="not allowlisted"):
        await gateway.preview(
            WorldAction(
                action_id="act-api-bad",
                task_ref="task-1",
                action_type="enterprise_api.post",
                target_ref="https://evil.example.com/orders",
                risk_level="high",
                payload={"json": {"order_id": "o-1"}},
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_enterprise_api_uses_tenant_allowlist_and_auth(
    tmp_path,
    monkeypatch,
) -> None:
    captured_headers: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("KUN_TENANT_TENANT_A_WORLD_API_ALLOWED_HOSTS", "api.tenant-a.example.com")
    monkeypatch.setenv("KUN_TENANT_TENANT_A_WORLD_API_AUTH_HEADER", "X-API-Key")
    monkeypatch.setenv("KUN_TENANT_TENANT_A_WORLD_API_AUTH_VALUE", "tenant-secret")

    handler = EnterpriseApiPostHandler(
        output_root=tmp_path,
        allowed_hosts={"api.global.example.com"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
    )
    gateway = WorldGateway(artifact_root=tmp_path, handlers=[handler])

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-api-tenant",
            tenant_id="tenant-a",
            task_ref="task-1",
            action_type="enterprise_api.post",
            target_ref="https://api.tenant-a.example.com/orders",
            risk_level="high",
            payload={
                "json": {"order_id": "o-tenant"},
                "external_dispatch_confirmed": True,
            },
        )
    )

    assert result.external_dispatched is True
    assert result.audit["host"] == "api.tenant-a.example.com"
    assert result.audit["allowed_hosts"] == ["api.tenant-a.example.com"]
    assert result.audit["tenant_scoped_config"] is True
    assert result.audit["auth_source"] == "tenant_override"
    assert captured_headers["x-api-key"] == "tenant-secret"
    assert '"X-API-Key": "[redacted]"' in result.rendered_payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_browser_execute_uses_injected_runner_and_allowlist(tmp_path) -> None:
    async def runner(plan: dict[str, Any]) -> dict[str, Any]:
        return {"executed": True, "steps_seen": len(plan["steps"])}

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            BrowserExecuteHandler(
                output_root=tmp_path,
                allowed_hosts={"example.com"},
                runner=runner,
            )
        ],
    )

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-browser-exec",
            task_ref="task-1",
            action_type="browser.execute",
            target_ref="https://example.com",
            risk_level="high",
            payload={
                "external_dispatch_confirmed": True,
                "steps": [
                    {"kind": "click", "selector": "#start"},
                    {"kind": "screenshot", "path": "done.png"},
                ],
            },
        )
    )

    assert result.external_dispatched is True
    assert result.audit["executed"] is True
    assert result.audit["steps_seen"] == 2

    with pytest.raises(ValueError, match="unsupported step kind"):
        await gateway.preview(
            WorldAction(
                action_id="act-browser-bad",
                task_ref="task-1",
                action_type="browser.execute",
                target_ref="https://example.com",
                risk_level="high",
                payload={"steps": [{"kind": "pay", "selector": "#pay"}]},
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_browser_execute_uses_tenant_allowlist(
    tmp_path,
    monkeypatch,
) -> None:
    async def runner(plan: dict[str, Any]) -> dict[str, Any]:
        return {"executed": True, "allowed_hosts_seen": plan["allowed_hosts"]}

    monkeypatch.setenv(
        "KUN_TENANT_TENANT_A_WORLD_BROWSER_ALLOWED_HOSTS",
        "browser.tenant-a.example.com",
    )
    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            BrowserExecuteHandler(
                output_root=tmp_path,
                allowed_hosts={"browser.global.example.com"},
                runner=runner,
            )
        ],
    )

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-browser-tenant",
            tenant_id="tenant-a",
            task_ref="task-1",
            action_type="browser.execute",
            target_ref="https://browser.tenant-a.example.com",
            risk_level="high",
            payload={
                "external_dispatch_confirmed": True,
                "steps": [{"kind": "screenshot", "path": "done.png"}],
            },
        )
    )

    assert result.external_dispatched is True
    assert result.audit["allowed_hosts"] == ["browser.tenant-a.example.com"]
    assert result.audit["tenant_scoped_config"] is True
    assert result.audit["allowed_hosts_seen"] == ["browser.tenant-a.example.com"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_local_file_write_blocks_path_traversal(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    with pytest.raises(ValueError, match="escapes output root"):
        await gateway.execute_approved(
            WorldAction(
                action_id="act-file-bad",
                task_ref="task-1",
                action_type="local_file.write",
                target_ref="../escape.txt",
                risk_level="low",
                payload={"content": "bad"},
            )
        )

    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_email_draft_handler_does_not_send(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-email",
            task_ref="task-1",
            action_type="email.draft",
            target_ref="user@example.com",
            risk_level="medium",
            payload={"subject": "Hi", "body": "Draft only"},
        )
    )

    assert result.requires_handler is False
    assert result.external_dispatched is False
    assert result.capability_status == "supported_draft"
    assert "草稿" in result.user_summary
    assert result.gateway_mode == "handler_drafted"
    assert result.audit["sent"] is False
    assert result.audit["handler_id"] == "email.draft.v1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_webhook_dry_run_does_not_call_network(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-webhook",
            task_ref="task-1",
            action_type="webhook.post_dry_run",
            target_ref="https://example.com/hook",
            risk_level="low",
            payload={"json": {"ok": True}},
        )
    )

    assert result.requires_handler is False
    assert result.external_dispatched is False
    assert result.capability_status == "supported_dry_run"
    assert "dry-run" in result.user_summary
    assert result.gateway_mode == "handler_dry_run"
    assert result.audit["dry_run"] is True
    assert "example.com" in result.rendered_payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_browser_plan_does_not_control_browser(tmp_path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-browser",
            task_ref="task-1",
            action_type="browser.plan",
            target_ref="https://example.com",
            risk_level="medium",
            payload={"objective": "检查首页", "steps": ["open page", "inspect hero"]},
        )
    )

    assert result.requires_handler is False
    assert result.external_dispatched is False
    assert result.capability_status == "supported_plan"
    assert "操作计划" in result.user_summary
    assert result.gateway_mode == "handler_drafted"
    assert result.audit["executed"] is False
    assert result.audit["handler_id"] == "browser.plan.v1"


async def _identity_translator(**kwargs: object) -> str:
    payload = kwargs["payload"]
    assert isinstance(payload, dict)
    return str(payload["answer"])
