from __future__ import annotations

from typing import Any, ClassVar

import pytest
from kun.world.handler_control import set_world_handler_control


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeRow:
    tenant_id = "tenant-a"
    action_type = "email.send"
    status = "disabled"
    reason = "bad config"
    source = "test"
    updated_by = "user-a"
    metadata_json: ClassVar[dict[str, Any]] = {}
    updated_at = None


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(_FakeRow())


@pytest.mark.unit
async def test_set_world_handler_control_returns_control() -> None:
    session = _FakeSession()

    control = await set_world_handler_control(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        action_type="email.send",
        status="disabled",
        reason="bad config",
        source="test",
        updated_by="user-a",
    )

    assert control.tenant_id == "tenant-a"
    assert control.action_type == "email.send"
    assert control.status == "disabled"
    assert control.reason == "bad config"
    assert session.statements


@pytest.mark.unit
async def test_set_world_handler_control_rejects_blank_action_type() -> None:
    with pytest.raises(ValueError, match="action_type"):
        await set_world_handler_control(
            _FakeSession(),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            action_type=" ",
            status="quarantined",
        )
