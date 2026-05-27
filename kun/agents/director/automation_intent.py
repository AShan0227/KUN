"""Automation intent extraction (L6.E).

把 IntentInterpreter LLM 输出的 JSON 中 "automation" 字段抽成 Action 对象,
让 Executor 可以直接交给 AdapterRouter 执行.

不依赖 LLM 调用 — 是个纯函数, 接受已经 parse 出来的 JSON dict, 返回 Action.

设计:
  - IntentInterpreter 已经把 LLM 输出 parse 成 dict (`parsed`)
  - Caller 把 dict 传给 extract_automation_action(parsed, *, tenant_id)
  - 如果 dict 含有 `automation: {target_platform, operation, payload}`
    且字段都齐, 返回 Action; 否则返回 None
  - Director 把 Action 挂在 TaskRef.automation_action (extra="allow" 允许)
  - Executor 看到 TaskRef.automation_action != None → 走 router path

为什么不直接改 IntentInterpreter 加字段:
  - 让 IntentInterpreter 保持 single-responsibility (NL → parsed dict)
  - automation 抽取可独立测 + 独立演化
  - 任何 caller (不止 IntentInterpreter) 都能复用
"""

from __future__ import annotations

from typing import Any

from kun.core.logging import get_logger
from kun.interface.automation import Action, make_action

log = get_logger("kun.agents.director.automation_intent")


# Suggested LLM prompt fragment to append to IntentInterpreter system prompt
# when supporting commercial Phase 2 use:
AUTOMATION_PROMPT_HINT = """
如果是平台自动化任务 (操作 Shopify / Meta Ads / 微信公众号 / Salesforce 等),
JSON 中额外加 `automation` 字段:

  "automation": {
    "target_platform": "shopify",      // 必填: 平台名小写
    "operation": "create_product",     // 必填: 操作名 snake_case
    "payload": {                       // 必填: 操作参数 dict
      "title": "Test Mug",
      "vendor": "ACME"
    }
  }

支持的 platform / operation 由 adapter registry 决定. 不确定时省略 automation
字段, KUN 会按一般任务处理.
"""


# Required keys in automation block
_REQUIRED_AUTOMATION_KEYS = ("target_platform", "operation", "payload")


def extract_automation_action(
    parsed_intent: dict[str, Any],
    *,
    tenant_id: str = "default",
) -> Action | None:
    """从 IntentInterpreter parsed JSON 抽 Action.

    返回 Action 当且仅当:
      - parsed_intent 含 `automation` key
      - automation 是 dict
      - 包含 target_platform / operation / payload 全部 3 字段
      - target_platform / operation 是非空 str
      - payload 是 dict

    其他情况 (key 缺失 / 类型不对) → 返回 None.

    Args:
      parsed_intent: IntentInterpreter._parse_json 的输出
      tenant_id: 当前租户 (从 Owner 来)

    Returns:
      Action 或 None.
    """
    automation = parsed_intent.get("automation")
    if not isinstance(automation, dict):
        return None

    missing = [k for k in _REQUIRED_AUTOMATION_KEYS if k not in automation]
    if missing:
        log.warning(
            "automation_intent.missing_keys",
            missing=missing,
            keys_present=list(automation.keys()),
        )
        return None

    target_platform = automation.get("target_platform")
    operation = automation.get("operation")
    payload = automation.get("payload")

    if not isinstance(target_platform, str) or not target_platform.strip():
        log.warning("automation_intent.invalid_target_platform", value=target_platform)
        return None
    if not isinstance(operation, str) or not operation.strip():
        log.warning("automation_intent.invalid_operation", value=operation)
        return None
    if not isinstance(payload, dict):
        log.warning("automation_intent.invalid_payload_type", type=type(payload).__name__)
        return None

    # Optional override: requested_kind (api / browser) — caller can hint Router
    requested_kind = automation.get("requested_kind")
    if requested_kind not in (None, "api", "browser"):
        log.warning(
            "automation_intent.invalid_requested_kind",
            value=requested_kind,
        )
        requested_kind = None

    return make_action(
        target_platform=target_platform.strip().lower(),
        operation=operation.strip(),
        payload=payload,
        tenant_id=tenant_id,
        requested_kind=requested_kind,
    )


__all__ = [
    "AUTOMATION_PROMPT_HINT",
    "extract_automation_action",
]
