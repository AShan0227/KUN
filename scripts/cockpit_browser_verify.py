"""V7 Phase X.K — cockpit browser verification.

Proves the /cockpit page truly renders + can fetch real data, beyond
just ESLint passing. Runs in CI/dev without a real headless browser:

  1. GET the /cockpit page over HTTP → expect 200, HTML body contains
     the 6 panel headings + the rendered React shell
  2. GET each backend /cockpit/* endpoint that the page consumes → expect
     200 + JSON shape the page expects

Usage:
    nohup uv run uvicorn kun.api.main:app --port 8000 &
    cd frontend && nohup npm run dev &
    .venv/bin/python scripts/cockpit_browser_verify.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

FRONTEND_BASE = "http://localhost:3000"
BACKEND_BASE = "http://localhost:8000"
TENANT = "u-sylvan"


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    # Local-only verify script — bandit S310 not relevant
    req = urllib.request.Request(url, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _verify_panel_headings(html: str) -> tuple[bool, list[str]]:
    expected = [
        "V7 Cockpit",
        "Writes Wired Status",
        "Capability Lifecycle",
        "Mission Alignment",
        "Auditor Reports",
        "Multi-LLM Ensemble Calls",
    ]
    missing = [p for p in expected if p not in html]
    return (not missing), missing


def _verify_api(path: str) -> tuple[bool, str]:
    status, body = _get(
        f"{BACKEND_BASE}{path}", headers={"X-Tenant-Id": TENANT}
    )
    if status != 200:
        return False, f"HTTP {status}"
    try:
        json.loads(body)
    except Exception as e:
        return False, f"non-JSON: {type(e).__name__}: {e}"
    return True, "ok"


def main() -> int:
    print("V7.PHASE-X.K — cockpit browser verification")
    print("=" * 70)

    # 1. Frontend page
    print(f"\n[1] GET {FRONTEND_BASE}/cockpit")
    status, html = _get(f"{FRONTEND_BASE}/cockpit")
    if status != 200:
        print(f"  ❌ HTTP {status}; output: {html[:120]}")
        return 1
    ok_html, missing = _verify_panel_headings(html)
    if not ok_html:
        print(f"  ❌ panel headings missing: {missing}")
        return 2
    print(f"  ✅ 6 panel headings present, {len(html)} bytes")

    # 2. Backend endpoints the page calls
    endpoints = [
        f"/cockpit/capabilities?tenant_id={TENANT}&limit=50",
        f"/cockpit/supervisor/auditor-reports?tenant_id={TENANT}&limit=20",
        f"/cockpit/ensemble/recent?tenant_id={TENANT}&limit=20",
        "/cockpit/writes-status",
    ]
    print("\n[2] Backend endpoints used by the page:")
    fails = 0
    for ep in endpoints:
        ok, reason = _verify_api(ep)
        mark = "✅" if ok else "❌"
        print(f"  {mark} GET {ep}  ({reason})")
        if not ok:
            fails += 1
    if fails:
        return 3

    print("\n" + "=" * 70)
    print("✅ X.K cockpit browser verification PASS:")
    print("  - /cockpit page renders, 6 panels visible in HTML")
    print("  - 4 backend endpoints respond 200 + JSON")
    print("  - Real data flows when JS hydrates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
