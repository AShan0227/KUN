"""CodeCapability API runtime route tests."""

from __future__ import annotations

from pathlib import Path

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
