"""Tests for FastPath (V2.1 §17.4a)."""

from __future__ import annotations

from kun.engineering.fast_path import (
    CHITCHAT_HINTS,
    HIGH_RISK_KEYWORDS,
    FastPathRouter,
    detect_chitchat,
    has_high_risk_keyword,
)


def test_detect_chitchat_short_greeting() -> None:
    assert detect_chitchat("你好") is True
    assert detect_chitchat("hello") is True
    assert detect_chitchat("ok") is True
    assert detect_chitchat("?") is True


def test_detect_chitchat_long_message_not_chitchat() -> None:
    long = "你好" + "x" * 100
    assert detect_chitchat(long) is False


def test_high_risk_keyword_detected() -> None:
    hit, kw = has_high_risk_keyword("帮我删除生产数据库")
    assert hit is True
    assert kw is not None
    hit2, _ = has_high_risk_keyword("写一个简单脚本")
    assert hit2 is False


def test_fast_path_pre_check_high_risk_blocks() -> None:
    router = FastPathRouter()
    decision = router.try_fast(
        task_meta={"user_message": "请帮我删除所有用户数据"},
    )
    assert decision.is_fast is False
    assert any("high_risk_keyword" in v for v in decision.pre_check_violations)


def test_fast_path_pre_check_new_user_blocks() -> None:
    router = FastPathRouter(
        user_trust_lookup=lambda uid: 2,  # 新用户, 任务数 < 10
    )
    decision = router.try_fast(
        task_meta={"user_message": "你好"},
        user_meta={"user_id": "u-new"},
    )
    assert decision.is_fast is False
    assert any("new_user" in v for v in decision.pre_check_violations)


def test_fast_path_pre_check_cross_tenant_blocks() -> None:
    router = FastPathRouter()
    decision = router.try_fast(
        task_meta={
            "user_message": "你好",
            "crosses_tenant": True,
        },
    )
    assert decision.is_fast is False
    assert any("crosses_tenant" in v for v in decision.pre_check_violations)


def test_fast_path_pre_check_budget_blocks() -> None:
    router = FastPathRouter()
    decision = router.try_fast(
        task_meta={
            "user_message": "你好",
            "estimated_cost_usd": 50.0,
        },
        user_meta={"approval_threshold_money": 10.0},
    )
    assert decision.is_fast is False
    assert any("estimated_cost" in v for v in decision.pre_check_violations)


def test_fast_path_cache_hit() -> None:
    router = FastPathRouter(
        cache_lookup=lambda fp: {"cached_answer": "OK"} if fp == "abc123" else None,
        user_trust_lookup=lambda uid: 100,  # 老用户, 不被 new_user 拦
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "查询",
            "fingerprint": "abc123",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "cache_hit"
    assert decision.response_payload == {"cached_answer": "OK"}


def test_fast_path_template_match() -> None:
    router = FastPathRouter(
        template_lookup=lambda t: {"template": "hello"} if t == "tools.greeting" else None,
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "你好",
            "task_type": "tools.greeting",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "template_match"


def test_fast_path_history_reuse() -> None:
    router = FastPathRouter(
        history_lookup=lambda uid, t: {"reuse_decision": "x"} if t == "coding.py" else None,
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "写脚本",
            "task_type": "coding.py",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "history_reuse"


def test_fast_path_fixed_flow() -> None:
    router = FastPathRouter(
        deterministic_types=("tools.echo",),
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "echo hello",
            "task_type": "tools.echo",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "fixed_flow"


def test_fast_path_skill_direct() -> None:
    router = FastPathRouter(
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "用 python-exec 算 2+2",
            "explicit_skill_id": "python-exec",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "skill_direct"


def test_fast_path_chitchat_hit() -> None:
    router = FastPathRouter(
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={"user_message": "你好"},
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is True
    assert decision.hit == "chitchat"
    assert decision.response_payload["model_tier"] == "cheap"


def test_fast_path_no_match_falls_through() -> None:
    router = FastPathRouter(
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={
            "user_message": "请实现一个分布式系统的 leader 选举算法",
        },
        user_meta={"user_id": "u-1"},
    )
    assert decision.is_fast is False
    assert decision.reason == "no fast-path trigger matched"


def test_fast_path_speed_under_5ms() -> None:
    """所有 pre-check + 6 触发条件总耗时应远低于 5ms."""
    router = FastPathRouter(
        cache_lookup=lambda fp: None,
        template_lookup=lambda t: None,
        history_lookup=lambda uid, t: None,
        deterministic_types=(),
        user_trust_lookup=lambda uid: 100,
    )
    decision = router.try_fast(
        task_meta={"user_message": "x"},
        user_meta={"user_id": "u-1"},
    )
    # 即使全 lookup 不命中, decided_in_ms 也应 < 50ms (本机)
    assert decision.decided_in_ms < 50, f"too slow: {decision.decided_in_ms}ms"


def test_fast_path_high_risk_keyword_in_constants() -> None:
    """V2.1 §17.4a.2 规定的高风险词都在常量里."""
    assert "删除" in HIGH_RISK_KEYWORDS
    assert "deploy" in HIGH_RISK_KEYWORDS
    assert "支付" in HIGH_RISK_KEYWORDS


def test_chitchat_hints_in_constants() -> None:
    assert "你好" in CHITCHAT_HINTS
    assert "hello" in CHITCHAT_HINTS
