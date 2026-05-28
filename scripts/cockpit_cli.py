"""V7 §20 任务驾驶舱 CLI viewer — 最小可行版.

# SCOPE (V7 §16.3): CLI 工具, 调 cockpit API 显示, 不走 Orchestrator,
# 不产 capability. 给开发同学和用户预览驾驶舱内容用.

跑法 (Phase E.A):
    .venv/bin/python scripts/cockpit_cli.py --base-url http://localhost:8000

输出示例:
    ═══ KUN V7 任务驾驶舱 (CLI v0.1) ═══

    📊 capabilities (V7 §16.1 7 层激活证据):
      [Phase E.A stub — no data yet]

    🎯 missions alignment (V7 §9.7 Mission Director):
      [Phase E.A stub]

    🔄 RSI 三线 trifecta (V7 §12.4):
      [Phase E.A stub]

    ⚖️ multi-LLM ensemble (V7 §11.4):
      [Phase E.A stub]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx


async def fetch_endpoint(client: httpx.AsyncClient, path: str) -> dict:
    try:
        resp = await client.get(path, timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:200]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _print_section(title: str, content: dict) -> None:
    print(f"\n{title}")
    print("-" * 60)
    if "error" in content:
        print(f"  ✗ {content['error']}")
    elif "note" in content and not content.get("capabilities") and not content.get("ensemble_calls"):
        print(f"  ℹ️  {content.get('note', '')}")
    else:
        print(json.dumps(content, indent=2, ensure_ascii=False)[:2000])


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="V7 §20 KUN 任务驾驶舱 CLI viewer (Phase E.A minimum viable)"
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="KUN API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Specific task_id for mission alignment + rsi-trifecta views",
    )
    args = parser.parse_args()

    print("═" * 60)
    print("    KUN V7 任务驾驶舱 (CLI v0.1, Phase E.A 最小可行版)")
    print("═" * 60)

    async with httpx.AsyncClient(base_url=args.base_url) as client:
        # Health
        health = await fetch_endpoint(client, "/cockpit/health")
        _print_section("🩺 健康检查", health)

        # Capabilities (7 层激活证据)
        caps = await fetch_endpoint(client, "/cockpit/capabilities")
        _print_section("📊 capabilities (V7 §16.1 7 层激活证据)", caps)

        # Mission alignment (if task_id provided)
        if args.task_id:
            alignment = await fetch_endpoint(
                client, f"/cockpit/missions/{args.task_id}/alignment"
            )
            _print_section(
                f"🎯 mission alignment for {args.task_id} (V7 §9.7 交付总监)",
                alignment,
            )

            trifecta = await fetch_endpoint(
                client, f"/cockpit/missions/{args.task_id}/rsi-trifecta"
            )
            _print_section(
                f"🔄 RSI 三线 trifecta for {args.task_id} (V7 §12.4)", trifecta
            )

        # Ensemble recent
        ensemble = await fetch_endpoint(client, "/cockpit/ensemble/recent?limit=5")
        _print_section("⚖️  multi-LLM ensemble 最近调用 (V7 §11.4)", ensemble)

        # Discipline recent
        discipline = await fetch_endpoint(client, "/cockpit/discipline/recent?limit=5")
        _print_section(
            "📐 Claude Code 工程纪律 最近检查 (V7 §11.5)", discipline
        )

        # Auditor reports
        auditor = await fetch_endpoint(
            client, "/cockpit/supervisor/auditor-reports?limit=5"
        )
        _print_section(
            "🕵️  外部监督者 auditor hat 周期报告 (V7 §16.6)", auditor
        )

    print()
    print("═" * 60)
    print(
        "  Phase E.A stub — 实装见 docs/v7/KUN-V7.md §20\n"
        "  Phase E.B (后续): 接真 DB\n"
        "  Phase E.C (后续, 3-4 周): frontend UI"
    )
    print("═" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
