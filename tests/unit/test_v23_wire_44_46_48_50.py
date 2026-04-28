"""V2.3 Wire 44/46/48/50 综合测试 — AntiGaming + Verification 模板 + Feedback API + Darwin Gödel."""

from __future__ import annotations

import pytest

# ===== Wire 44: AntiGamingDetector =====


def test_detect_copy_prompt() -> None:
    from kun.security.anti_gaming import detect_copy_prompt

    finding = detect_copy_prompt(prompt="What is 1+1?", answer="What is 1+1?")
    assert finding is not None
    assert finding.pattern == "copy_prompt"
    assert finding.severity == "high"


def test_detect_copy_prompt_dissimilar_returns_none() -> None:
    from kun.security.anti_gaming import detect_copy_prompt

    finding = detect_copy_prompt(prompt="What is 1+1?", answer="The answer is 2")
    assert finding is None


def test_detect_copy_prior_answer() -> None:
    from kun.security.anti_gaming import detect_copy_prior_answer

    finding = detect_copy_prior_answer(
        answer="The result is approximately 42",
        prior_answers=["The result is approximately 42 yes"],
    )
    assert finding is not None
    assert finding.pattern == "copy_prior_answer"


def test_detect_skip_step() -> None:
    from kun.security.anti_gaming import detect_skip_step

    finding = detect_skip_step(planned_steps=10, actual_steps=2)
    assert finding is not None
    assert finding.pattern == "skip_step"


def test_detect_skip_step_no_skip() -> None:
    from kun.security.anti_gaming import detect_skip_step

    finding = detect_skip_step(planned_steps=4, actual_steps=4)
    assert finding is None


def test_detect_fake_completion() -> None:
    from kun.security.anti_gaming import detect_fake_completion

    finding = detect_fake_completion(
        answer="已完成. 没问题.", has_assets=False, has_skill_traces=False
    )
    assert finding is not None
    assert finding.pattern == "fake_completion"


def test_detect_fake_completion_with_assets_pass() -> None:
    from kun.security.anti_gaming import detect_fake_completion

    finding = detect_fake_completion(answer="已完成", has_assets=True)
    assert finding is None  # 有 asset 证明 → 不算 fake


def test_detect_over_spec() -> None:
    from kun.security.anti_gaming import detect_over_spec

    finding = detect_over_spec(
        used_skills=["reader", "writer", "rogue_skill"],
        allowed_skills=["reader", "writer"],
    )
    assert finding is not None
    assert finding.pattern == "over_spec"
    assert "rogue_skill" in finding.evidence["unauthorized_skills"]


def test_detect_over_spec_no_allowlist_returns_none() -> None:
    from kun.security.anti_gaming import detect_over_spec

    finding = detect_over_spec(used_skills=["any"], allowed_skills=None)
    assert finding is None  # 无 allowlist → 不检测


def test_detect_answer_off_topic() -> None:
    from kun.security.anti_gaming import detect_answer_off_topic

    finding = detect_answer_off_topic(
        prompt="What is the capital of France?",
        answer="今天天气真好, 我去吃饭了, 顺便看了部电影",
    )
    assert finding is not None
    assert finding.pattern == "answer_off_topic"


def test_anti_gaming_detector_integrated_finds_first_pattern() -> None:
    from kun.security.anti_gaming import AntiGamingDetector

    detector = AntiGamingDetector()
    finding = detector.check(
        prompt="What is 1+1?",
        answer="What is 1+1?",  # copy_prompt
        planned_steps=4,
        actual_steps=4,
    )
    assert finding is not None
    assert finding.pattern == "copy_prompt"


def test_anti_gaming_detector_no_issue() -> None:
    from kun.security.anti_gaming import AntiGamingDetector

    detector = AntiGamingDetector(off_topic_threshold=0.05)
    finding = detector.check(
        prompt="Calculate one plus one and explain why",
        answer="One plus one equals two because addition combines two units into a sum",
        planned_steps=2,
        actual_steps=2,
        has_assets=True,
    )
    assert finding is None


# ===== Wire 46: Verification 默认模板 =====


def test_default_verification_writing() -> None:
    from kun.datamodel.verification_templates import get_default_verification_specs

    specs = get_default_verification_specs("writing.creative.short")
    assert len(specs) >= 1
    assert specs[0].kind == "exact_output"


def test_default_verification_coding() -> None:
    from kun.datamodel.verification_templates import get_default_verification_specs

    specs = get_default_verification_specs("coding.python.fastapi")
    assert any(s.kind == "lint_pass" for s in specs)


def test_default_verification_decision() -> None:
    from kun.datamodel.verification_templates import get_default_verification_specs

    specs = get_default_verification_specs("decision.product")
    assert any(s.kind == "exact_output" for s in specs)


def test_default_verification_unknown_returns_empty() -> None:
    from kun.datamodel.verification_templates import get_default_verification_specs

    specs = get_default_verification_specs("general.x")
    assert specs == []


def test_merge_default_with_llm_provided() -> None:
    from kun.datamodel.verification_spec import VerificationSpec
    from kun.datamodel.verification_templates import merge_with_default

    llm_specs = [VerificationSpec(kind="hash_match", spec={"sha": "abc"})]
    merged = merge_with_default("writing.creative.x", llm_specs)
    # LLM 加的 + default writing 的
    kinds = {s.kind for s in merged}
    assert "exact_output" in kinds  # default
    assert "hash_match" in kinds  # LLM


def test_merge_llm_overrides_default_same_kind() -> None:
    from kun.datamodel.verification_spec import VerificationSpec
    from kun.datamodel.verification_templates import merge_with_default

    # LLM 提供 exact_output (跟 default writing 同 kind)
    llm_specs = [VerificationSpec(kind="exact_output", spec={"min_length_chars": 100})]
    merged = merge_with_default("writing.creative.x", llm_specs)
    # 只一个 exact_output (LLM 覆盖)
    exact_specs = [s for s in merged if s.kind == "exact_output"]
    assert len(exact_specs) == 1
    assert exact_specs[0].spec["min_length_chars"] == 100  # LLM 的


# ===== Wire 48: 用户反馈 API =====


def test_user_feedback_request_validation() -> None:
    from kun.api.feedback import UserFeedbackRequest

    req = UserFeedbackRequest(rating=5, comment="great", tags=["fast", "accurate"])
    assert req.rating == 5
    assert req.comment == "great"


def test_user_feedback_request_rating_out_of_range() -> None:
    from kun.api.feedback import UserFeedbackRequest

    with pytest.raises(ValueError):
        UserFeedbackRequest(rating=10)


def test_user_feedback_request_default_empty_lists() -> None:
    from kun.api.feedback import UserFeedbackRequest

    req = UserFeedbackRequest(rating=3)
    assert req.comment == ""
    assert req.tags == []


# ===== Wire 50: Darwin Gödel =====


@pytest.mark.asyncio
async def test_darwin_loop_runs_max_rounds() -> None:
    from kun.qi import DarwinGodelLoop

    call_count = 0

    async def fake_runner(prompt, strategy):
        nonlocal call_count
        call_count += 1
        return (0.5, 0.01)  # 固定 score 不收敛

    loop = DarwinGodelLoop(
        round_runner=fake_runner,
        max_rounds=3,
        total_budget_usd=100.0,
        total_time_sec=100.0,
        convergence_threshold=0.001,  # 几乎不可能 converge
    )
    result = await loop.explore("test")
    # convergence_threshold 太严, 实际可能 converged 或 rounds_max
    assert result.total_rounds == 3
    assert result.stopped_reason in ("rounds_max", "converged")
    assert call_count == 3


@pytest.mark.asyncio
async def test_darwin_loop_stops_on_budget() -> None:
    from kun.qi import DarwinGodelLoop

    async def expensive_runner(prompt, strategy):
        return (0.5, 1.5)  # 1 轮就 1.5

    loop = DarwinGodelLoop(
        round_runner=expensive_runner,
        max_rounds=10,
        total_budget_usd=2.0,  # 1 轮 1.5, 第 2 轮就超
        total_time_sec=100.0,
    )
    result = await loop.explore("test")
    assert result.stopped_reason == "budget_exhausted"
    assert result.total_rounds <= 2


@pytest.mark.asyncio
async def test_darwin_loop_tracks_best_strategy() -> None:
    from kun.qi import DarwinGodelLoop

    scores = [0.3, 0.8, 0.5, 0.6]  # round 1 (idx 1) 最好
    call_count = 0

    async def varied_runner(prompt, strategy):
        nonlocal call_count
        score = scores[call_count]
        call_count += 1
        return (score, 0.01)

    loop = DarwinGodelLoop(
        round_runner=varied_runner,
        max_rounds=4,
        total_budget_usd=100.0,
        convergence_threshold=0.001,
    )
    result = await loop.explore("test")
    assert result.best_round_idx == 1
    assert result.best_score == 0.8


@pytest.mark.asyncio
async def test_darwin_loop_round_runner_exception_doesnt_break() -> None:
    from kun.qi import DarwinGodelLoop

    async def crash_runner(prompt, strategy):
        raise RuntimeError("crash")

    loop = DarwinGodelLoop(
        round_runner=crash_runner,
        max_rounds=2,
        convergence_threshold=0.001,
    )
    result = await loop.explore("test")
    # 不抛, total_rounds 仍 = 2
    assert result.total_rounds == 2
    assert result.best_score == 0.0


@pytest.mark.asyncio
async def test_darwin_loop_strategy_evolves_per_round() -> None:
    from kun.qi import DarwinGodelLoop

    captured: list[dict] = []
    counter = [0]

    async def runner(prompt, strategy):
        captured.append(strategy)
        counter[0] += 1
        # 让 score 每轮明显变化, 防触发 converged
        return (0.1 + 0.2 * counter[0], 0.01)

    loop = DarwinGodelLoop(
        round_runner=runner,
        max_rounds=4,
        total_budget_usd=100.0,
        convergence_threshold=0.001,
    )
    await loop.explore("test")
    assert len(captured) == 4
    # 前 3 轮是 default presets, 第 4 轮是基于 best 微调
    assert captured[0]["strategy"] == "tier_top_low_temp"
    assert captured[1]["strategy"] == "tier_strong_mid_temp"
    assert captured[2]["strategy"] == "chain_of_thought"
    # round 3 含 round_basis 字段
    assert "round_basis" in captured[3]
