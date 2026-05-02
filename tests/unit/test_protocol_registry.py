"""Wire 39: ProtocolRegistry (V2.3 §3)."""

from __future__ import annotations

import pytest
from kun.qi import (
    InMemoryProtocolStorage,
    Protocol,
    ProtocolExecution,
    ProtocolHermesTemplate,
    ProtocolRegistry,
    ProtocolSkillStep,
    ProtocolTrigger,
    ProtocolVerificationSpec,
    get_protocol_registry,
    reset_protocol_registry,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_protocol_registry()
    yield
    reset_protocol_registry()


def _make_protocol(
    *,
    protocol_id: str = "writing.creative.short_form",
    version: str = "1.0.0",
    status: str = "experimental",
    pattern: str = "writing.creative.*",
    tenant: str = "u-test",
) -> Protocol:
    return Protocol(
        protocol_id=protocol_id,
        version=version,
        tenant_id=tenant,
        status=status,  # type: ignore[arg-type]
        trigger=ProtocolTrigger(task_type_pattern=pattern),
        execution=ProtocolExecution(mode="SMART", llm_strategy="tier_top_low_temp"),
        skill_chain=[
            ProtocolSkillStep(skill="writing.creative_polish", when="always"),
        ],
        hermes_template=ProtocolHermesTemplate(system_prompt_addon="[Lab] Take a contrarian view"),
        verification=[
            ProtocolVerificationSpec(kind="char_count_min", spec={"min": 30}),
        ],
    )


# ---- Protocol pydantic ----


def test_protocol_basic_construction() -> None:
    p = _make_protocol()
    assert p.protocol_id == "writing.creative.short_form"
    assert p.execution.mode == "SMART"
    assert p.skill_chain[0].skill == "writing.creative_polish"


def test_protocol_matches_task_type_pattern() -> None:
    p = _make_protocol(pattern="writing.creative.*")
    assert p.matches({"task_type": "writing.creative.short_form"}) is True
    assert p.matches({"task_type": "writing.creative.long_form"}) is True
    assert p.matches({"task_type": "coding.python"}) is False


def test_protocol_matches_complexity_range() -> None:
    p = _make_protocol()
    p.trigger.complexity_score_min = 0.3
    p.trigger.complexity_score_max = 0.7
    assert p.matches({"task_type": "writing.creative.x", "complexity_score": 0.5}) is True
    assert p.matches({"task_type": "writing.creative.x", "complexity_score": 0.2}) is False
    assert p.matches({"task_type": "writing.creative.x", "complexity_score": 0.9}) is False


def test_protocol_matches_risk_level() -> None:
    p = _make_protocol()
    p.trigger.risk_levels = ["low", "medium"]
    assert p.matches({"task_type": "writing.creative.x", "risk_level": "low"}) is True
    assert p.matches({"task_type": "writing.creative.x", "risk_level": "critical"}) is False


# ---- InMemoryProtocolStorage ----


@pytest.mark.asyncio
async def test_storage_save_then_get() -> None:
    storage = InMemoryProtocolStorage()
    p = _make_protocol()
    await storage.save(p)
    loaded = await storage.get(p.tenant_id, p.protocol_id, p.version)
    assert loaded is not None
    assert loaded.protocol_id == p.protocol_id


@pytest.mark.asyncio
async def test_storage_get_active_returns_stable() -> None:
    storage = InMemoryProtocolStorage()
    await storage.save(_make_protocol(version="1.0.0", status="stable"))
    await storage.save(_make_protocol(version="1.1.0", status="experimental"))
    active = await storage.get_active("u-test", "writing.creative.short_form")
    assert active is not None
    assert active.version == "1.0.0"  # stable


@pytest.mark.asyncio
async def test_storage_get_active_with_status_filter() -> None:
    storage = InMemoryProtocolStorage()
    await storage.save(_make_protocol(version="1.1.0", status="canary"))
    canary = await storage.get_active("u-test", "writing.creative.short_form", status="canary")
    assert canary is not None
    assert canary.version == "1.1.0"


@pytest.mark.asyncio
async def test_storage_list_all_per_tenant() -> None:
    storage = InMemoryProtocolStorage()
    await storage.save(_make_protocol(tenant="u-A", protocol_id="a"))
    await storage.save(_make_protocol(tenant="u-A", protocol_id="b"))
    await storage.save(_make_protocol(tenant="u-B", protocol_id="c"))
    a_list = await storage.list_all("u-A")
    b_list = await storage.list_all("u-B")
    assert len(a_list) == 2
    assert len(b_list) == 1


@pytest.mark.asyncio
async def test_storage_update_status_to_stable_sets_promoted_at() -> None:
    storage = InMemoryProtocolStorage()
    await storage.save(_make_protocol(status="canary"))
    await storage.update_status("u-test", "writing.creative.short_form", "1.0.0", "stable")
    updated = await storage.get("u-test", "writing.creative.short_form", "1.0.0")
    assert updated is not None
    assert updated.status == "stable"
    assert updated.promoted_at is not None


@pytest.mark.asyncio
async def test_storage_update_status_rolled_back_records_reason() -> None:
    storage = InMemoryProtocolStorage()
    await storage.save(_make_protocol(status="stable"))
    await storage.update_status(
        "u-test",
        "writing.creative.short_form",
        "1.0.0",
        "rolled_back",
        rollback_reason="winrate dropped",
    )
    updated = await storage.get("u-test", "writing.creative.short_form", "1.0.0")
    assert updated is not None
    assert updated.status == "rolled_back"
    assert updated.rollback_reason == "winrate dropped"
    assert updated.rollback_at is not None


# ---- ProtocolRegistry ----


@pytest.mark.asyncio
async def test_registry_singleton() -> None:
    a = get_protocol_registry()
    b = get_protocol_registry()
    assert a is b


@pytest.mark.asyncio
async def test_registry_save_and_get_active() -> None:
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="stable"))
    found = await reg.get_active("u-test", "writing.creative.short_form")
    assert found is not None
    assert found.status == "stable"


@pytest.mark.asyncio
async def test_registry_find_protocol_for_task() -> None:
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="stable", pattern="writing.creative.*"))
    p = await reg.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.5}, "u-test"
    )
    assert p is not None


@pytest.mark.asyncio
async def test_registry_find_returns_none_when_no_match() -> None:
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="stable", pattern="writing.creative.*"))
    p = await reg.find_protocol_for(
        {"task_type": "coding.python", "complexity_score": 0.5}, "u-test"
    )
    assert p is None


@pytest.mark.asyncio
async def test_registry_find_picks_most_specific() -> None:
    """两个匹配 protocol → 选 pattern 字符长的 (more specific)."""
    reg = ProtocolRegistry()
    await reg.save(
        _make_protocol(
            status="stable",
            pattern="writing.*",  # broad
            protocol_id="generic",
            version="1.0.0",
        )
    )
    await reg.save(
        _make_protocol(
            status="stable",
            pattern="writing.creative.short_form",  # specific
            protocol_id="specific",
            version="1.0.0",
        )
    )
    p = await reg.find_protocol_for({"task_type": "writing.creative.short_form"}, "u-test")
    assert p is not None
    assert p.protocol_id == "specific"


@pytest.mark.asyncio
async def test_registry_only_returns_stable_for_find() -> None:
    """experimental / shadow / canary 协议不应该被 find 拿出来给生产用."""
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="canary"))
    p = await reg.find_protocol_for({"task_type": "writing.creative.x"}, "u-test")
    assert p is None  # canary 不算 stable


@pytest.mark.asyncio
async def test_registry_promote_lifecycle() -> None:
    """experimental → shadow → canary → stable."""
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="experimental"))
    pid = "writing.creative.short_form"

    await reg.promote("u-test", pid, "1.0.0", "shadow")
    p = await reg.get_active("u-test", pid, status="shadow")
    assert p is not None and p.status == "shadow"

    await reg.promote("u-test", pid, "1.0.0", "canary")
    p = await reg.get_active("u-test", pid, status="canary")
    assert p is not None and p.status == "canary"

    await reg.promote("u-test", pid, "1.0.0", "stable")
    p = await reg.get_active("u-test", pid)
    assert p is not None and p.status == "stable"
    assert p.promoted_at is not None


@pytest.mark.asyncio
async def test_registry_promote_invalid_transition_raises() -> None:
    """experimental → stable 必须经过 shadow + canary."""
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="experimental"))
    with pytest.raises(ValueError, match="Invalid transition"):
        await reg.promote("u-test", "writing.creative.short_form", "1.0.0", "stable")


@pytest.mark.asyncio
async def test_registry_promote_unknown_protocol_raises() -> None:
    reg = ProtocolRegistry()
    with pytest.raises(ValueError, match="not found"):
        await reg.promote("u-test", "unknown", "1.0.0", "shadow")


@pytest.mark.asyncio
async def test_registry_rollback() -> None:
    reg = ProtocolRegistry()
    await reg.save(_make_protocol(status="stable"))
    await reg.rollback("u-test", "writing.creative.short_form", "1.0.0", reason="user feedback bad")
    p = await reg.get_active("u-test", "writing.creative.short_form")
    assert p is None  # rolled_back 不再 active


@pytest.mark.asyncio
async def test_registry_cache_invalidated_on_promote() -> None:
    reg = ProtocolRegistry()
    p_old = _make_protocol(version="1.0.0", status="stable")
    await reg.save(p_old)
    cached = await reg.get_active("u-test", "writing.creative.short_form")
    assert cached.version == "1.0.0"

    # rollback → cache 应该 invalidate
    await reg.rollback("u-test", "writing.creative.short_form", "1.0.0", reason="test")
    after = await reg.get_active("u-test", "writing.creative.short_form")
    assert after is None
