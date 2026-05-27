"""L6.E — extract_automation_action 单测 (Director 侧)."""

from __future__ import annotations

import pytest
from kun.agents.director.automation_intent import extract_automation_action
from kun.interface.automation import Action


def test_returns_none_when_no_automation_key() -> None:
    parsed = {"task_type": "coding.python", "goal_detail": "..."}
    assert extract_automation_action(parsed) is None


def test_returns_none_when_automation_not_dict() -> None:
    parsed = {"automation": "not a dict"}
    assert extract_automation_action(parsed) is None
    parsed = {"automation": []}
    assert extract_automation_action(parsed) is None
    parsed = {"automation": None}
    assert extract_automation_action(parsed) is None


def test_returns_none_when_missing_required_keys() -> None:
    # missing operation
    parsed = {"automation": {"target_platform": "shopify", "payload": {}}}
    assert extract_automation_action(parsed) is None
    # missing payload
    parsed = {"automation": {"target_platform": "shopify", "operation": "x"}}
    assert extract_automation_action(parsed) is None
    # missing target_platform
    parsed = {"automation": {"operation": "x", "payload": {}}}
    assert extract_automation_action(parsed) is None


def test_returns_none_when_empty_target_platform() -> None:
    parsed = {
        "automation": {
            "target_platform": "",
            "operation": "create_product",
            "payload": {},
        }
    }
    assert extract_automation_action(parsed) is None


def test_returns_none_when_target_platform_not_string() -> None:
    parsed = {
        "automation": {
            "target_platform": 42,
            "operation": "create_product",
            "payload": {},
        }
    }
    assert extract_automation_action(parsed) is None


def test_returns_none_when_operation_blank() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "   ",
            "payload": {},
        }
    }
    assert extract_automation_action(parsed) is None


def test_returns_none_when_payload_not_dict() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": "stringly",
        }
    }
    assert extract_automation_action(parsed) is None


def test_happy_path_returns_action() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": {"title": "Test Mug", "vendor": "ACME"},
        }
    }
    action = extract_automation_action(parsed, tenant_id="t-acme")
    assert isinstance(action, Action)
    assert action.target_platform == "shopify"
    assert action.operation == "create_product"
    assert action.payload == {"title": "Test Mug", "vendor": "ACME"}
    assert action.tenant_id == "t-acme"
    assert action.requested_kind is None
    assert action.action_id.startswith("act-")


def test_target_platform_lowercased_trimmed() -> None:
    parsed = {
        "automation": {
            "target_platform": "  SHOPIFY  ",
            "operation": "create_product",
            "payload": {},
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.target_platform == "shopify"


def test_operation_trimmed_but_case_preserved() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "  create_product  ",
            "payload": {},
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.operation == "create_product"


def test_requested_kind_api_passes_through() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "list_orders",
            "payload": {},
            "requested_kind": "api",
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.requested_kind == "api"


def test_requested_kind_browser_passes_through() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "list_orders",
            "payload": {},
            "requested_kind": "browser",
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.requested_kind == "browser"


def test_invalid_requested_kind_dropped_to_none() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "list_orders",
            "payload": {},
            "requested_kind": "magic",  # not api/browser
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.requested_kind is None


def test_default_tenant_id_when_not_specified() -> None:
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "list_orders",
            "payload": {},
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.tenant_id == "default"


def test_payload_preserved_verbatim() -> None:
    """payload 不会被规范化, 原样传给 adapter."""
    payload = {
        "title": "Test",
        "variants": [{"option1": "S", "price": "9.99"}],
        "metadata": {"nested": True},
    }
    parsed = {
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": payload,
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.payload == payload


@pytest.mark.parametrize(
    "platform,operation",
    [
        ("shopify", "create_product"),
        ("meta_ads", "create_ad"),
        ("wechat_mp", "publish_article"),
        ("salesforce", "log_call"),
    ],
)
def test_industry_agnostic(platform: str, operation: str) -> None:
    """4 行业都能从同一抽取器拿 Action."""
    parsed = {
        "automation": {
            "target_platform": platform,
            "operation": operation,
            "payload": {"k": "v"},
        }
    }
    action = extract_automation_action(parsed)
    assert action is not None
    assert action.target_platform == platform
    assert action.operation == operation
