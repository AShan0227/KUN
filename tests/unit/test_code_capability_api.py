"""CodeCapability API runtime route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import kun.api.code_capability as code_api
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.code_capability import router
from kun.api.runtime import install_runtime
from kun.core.tenancy import TenantContext, tenant_scope
from kun.watchtower.engine import RuleEngine


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("KUN_CODE_CAPABILITY_WORKSPACE_ROOT", str(tmp_path))
    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    app.include_router(router)

    @app.middleware("http")
    async def _tenant_for_test(request, call_next):
        raw_scopes = request.headers.get("X-Scopes", "")
        scopes = tuple(scope.strip() for scope in raw_scopes.split(",") if scope.strip())
        with tenant_scope(TenantContext(tenant_id="tenant-code-test", scopes=scopes)):
            return await call_next(request)

    return app


@pytest.mark.unit
def test_review_diff_api_flags_dangerous_added_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))

    response = client.post(
        "/api/code-capability/review-diff",
        json={
            "diff": """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,1 @@
+eval("1+1")
""",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["findings"][0]["rule"] == "no-eval-exec"


@pytest.mark.unit
def test_review_file_api_rejects_workspace_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))

    response = client.post(
        "/api/code-capability/review-file",
        json={"path": str(tmp_path.parent)},
    )

    assert response.status_code == 400
    assert "escapes code workspace" in response.json()["detail"]


@pytest.mark.unit
def test_run_python_api_uses_bounded_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))

    ok = client.post(
        "/api/code-capability/run-python",
        json={"code": "print('api-runtime-ok')", "timeout_sec": 5},
    )
    escaped = client.post(
        "/api/code-capability/run-python",
        json={"code": "print('nope')", "cwd": str(tmp_path.parent), "timeout_sec": 5},
    )

    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert "api-runtime-ok" in ok.json()["stdout"]
    assert ok.json()["sandbox"]["cwd_restricted"] is True
    assert escaped.status_code == 400
    assert "escapes code workspace" in escaped.json()["detail"]


@pytest.mark.unit
def test_check_api_runs_lint_under_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
    client = TestClient(_app(tmp_path, monkeypatch))

    response = client.post(
        "/api/code-capability/check",
        json={"kind": "lint", "target": "ok.py", "tool": "ruff", "timeout_sec": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "lint"
    assert body["ok"] is True
    assert body["issues"] == []


@pytest.mark.unit
def test_execute_api_requires_execute_scope_when_scopes_are_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))

    blocked = client.post(
        "/api/code-capability/run-python",
        json={"code": "print('blocked')", "timeout_sec": 5},
        headers={"X-Scopes": "code:read"},
    )
    allowed = client.post(
        "/api/code-capability/run-python",
        json={"code": "print('allowed')", "timeout_sec": 5},
        headers={"X-Scopes": "code:execute"},
    )

    assert blocked.status_code == 403
    assert "code:execute" in blocked.json()["detail"]
    assert allowed.status_code == 200
    assert "allowed" in allowed.json()["stdout"]


@pytest.mark.unit
def test_propose_change_api_defaults_to_dry_run_and_requires_execute_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "module.py"
    target.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    client = TestClient(_app(tmp_path, monkeypatch))
    payload = {
        "path": "module.py",
        "replacement_content": "def value() -> int:\n    return 2\n",
    }

    blocked = client.post(
        "/api/code-capability/propose-change",
        json=payload,
        headers={"X-Scopes": "code:read"},
    )
    allowed = client.post(
        "/api/code-capability/propose-change",
        json=payload,
        headers={"X-Scopes": "code:execute"},
    )

    assert blocked.status_code == 403
    assert "code:execute" in blocked.json()["detail"]
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["ok"] is True
    assert body["mode"] == "dry_run"
    assert body["applied"] is False
    assert body["lint_results"][0]["ok"] is True
    assert target.read_text(encoding="utf-8") == "def value() -> int:\n    return 1\n"


@pytest.mark.unit
def test_propose_change_api_rejects_workspace_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))

    response = client.post(
        "/api/code-capability/propose-change",
        json={
            "path": str(tmp_path.parent / "escape.py"),
            "replacement_content": "VALUE = 1\n",
            "allow_apply": True,
        },
        headers={"X-Scopes": "code:execute"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["phase"] == "resolve"
    assert "escapes code workspace" in detail["error"]


@pytest.mark.unit
def test_propose_change_api_records_event_and_state_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    app = _app(tmp_path, monkeypatch)
    ledger = _FakeLedger()
    app.state.state_ledger = ledger
    emitted: list[Any] = []

    @asynccontextmanager
    async def fake_session_scope(*, tenant_id: str | None = None) -> AsyncIterator[object]:
        emitted.append({"tenant_id": tenant_id, "session": True})
        yield object()

    async def fake_emit(_session: object, event: object) -> None:
        emitted.append(event)

    monkeypatch.setattr(code_api, "session_scope", fake_session_scope)
    monkeypatch.setattr(code_api, "emit", fake_emit)

    client = TestClient(app)
    response = client.post(
        "/api/code-capability/propose-change",
        json={
            "task_id": "task-code-1",
            "reason": "修复单文件配置",
            "path": "module.py",
            "replacement_content": "VALUE = 2\n",
        },
        headers={"X-Scopes": "code:execute"},
    )

    assert response.status_code == 200
    event = emitted[-1]
    assert event.event_type == "code.change.proposed"
    assert event.task_ref == "task-code-1"
    assert event.payload["path"] == "module.py"
    assert event.payload["mode"] == "dry_run"
    assert event.payload["diff_sha256"]
    assert "diff" not in event.payload
    assert ledger.calls == [
        {
            "task_id": "task-code-1",
            "tenant_id": "tenant-code-test",
            "path": "module.py",
            "mode": "dry_run",
            "phase": "done",
            "ok": True,
            "applied": False,
            "rolled_back": False,
            "checks_passed": True,
            "reason": "修复单文件配置",
            "bytes_changed": response.json()["bytes_changed"],
        }
    ]


class _FakeLedger:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_code_change(self, task_id: str, **kwargs: Any) -> None:
        self.calls.append({"task_id": task_id, **kwargs})
