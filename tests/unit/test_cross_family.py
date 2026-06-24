"""V7 Phase G — kun.interface.llm.cross_family 单测.

V7 §11.2 cross-family 强制约束实装.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.cross_family import (
    CrossFamilyConfigError,
    LLMFamily,
    classify_family,
    is_cross_family,
    validate_cross_family_config,
)


@pytest.mark.unit
class TestClassifyFamily:
    def test_anthropic_claude(self) -> None:
        assert classify_family("claude-opus-4-7") == LLMFamily.ANTHROPIC
        assert classify_family("claude-sonnet-4-6") == LLMFamily.ANTHROPIC
        assert classify_family("claude-haiku-4-5-20251001") == LLMFamily.ANTHROPIC

    def test_openai_gpt(self) -> None:
        assert classify_family("gpt-5") == LLMFamily.OPENAI_GPT
        assert classify_family("gpt-5.5") == LLMFamily.OPENAI_GPT
        assert classify_family("gpt-5.3-codex-spark") == LLMFamily.OPENAI_GPT
        assert classify_family("codex-spark") == LLMFamily.OPENAI_GPT

    def test_qwen(self) -> None:
        assert classify_family("qwen-32b") == LLMFamily.QWEN
        assert classify_family("Qwen-Max") == LLMFamily.QWEN  # case insensitive

    def test_llama(self) -> None:
        assert classify_family("llama-3.1-70b") == LLMFamily.LLAMA
        assert classify_family("Llama-3-Distill") == LLMFamily.LLAMA

    def test_deepseek(self) -> None:
        assert classify_family("deepseek-r1") == LLMFamily.DEEPSEEK
        assert classify_family("DeepSeek-V3") == LLMFamily.DEEPSEEK

    def test_mistral(self) -> None:
        assert classify_family("mistral-7b") == LLMFamily.MISTRAL
        assert classify_family("mixtral-8x7b") == LLMFamily.MISTRAL

    def test_gemini(self) -> None:
        assert classify_family("gemini-pro") == LLMFamily.GEMINI
        assert classify_family("gemini-flash") == LLMFamily.GEMINI

    def test_minimax(self) -> None:
        assert classify_family("MiniMax-M2.7") == LLMFamily.MINIMAX

    def test_local_other(self) -> None:
        assert classify_family("phi-3") == LLMFamily.LOCAL_OTHER
        assert classify_family("gemma-7b") == LLMFamily.LOCAL_OTHER
        assert classify_family("OpenHermes-2.5") == LLMFamily.LOCAL_OTHER

    def test_unknown(self) -> None:
        assert classify_family("some-random-name") == LLMFamily.UNKNOWN
        assert classify_family("") == LLMFamily.UNKNOWN


@pytest.mark.unit
class TestIsCrossFamily:
    def test_same_family_anthropic_tiers(self) -> None:
        """Opus + Haiku 同 Anthropic family, 不算 cross."""
        assert is_cross_family("claude-opus-4-7", "claude-haiku-4-5") is False

    def test_anthropic_vs_openai_cross(self) -> None:
        assert is_cross_family("claude-opus-4-7", "gpt-5.5") is True

    def test_local_qwen_vs_cloud_anthropic_cross(self) -> None:
        """本地 Qwen vs cloud Anthropic Haiku — 满足 cross-family (V7 §11.3)."""
        assert is_cross_family("qwen-32b", "claude-haiku-4-5") is True

    def test_same_family_qwen_tiers(self) -> None:
        assert is_cross_family("qwen-7b", "qwen-max") is False

    def test_unknown_treated_as_cross(self) -> None:
        """UNKNOWN family 跟任何 family 都算 cross (defensive default)."""
        assert is_cross_family("unknown-model-xyz", "claude-haiku-4-5") is True


@pytest.mark.unit
class TestValidateCrossFamilyConfig:
    def test_cross_family_passes(self) -> None:
        # 不抛
        validate_cross_family_config(
            executor_model="gpt-5.5",
            external_supervisor_model="claude-haiku-4-5",
        )

    def test_same_family_raises(self) -> None:
        with pytest.raises(CrossFamilyConfigError, match="Cross-family required"):
            validate_cross_family_config(
                executor_model="claude-opus-4-7",
                external_supervisor_model="claude-haiku-4-5",
            )

    def test_supervisor_missing_raises_when_required(self) -> None:
        with pytest.raises(CrossFamilyConfigError, match="not configured"):
            validate_cross_family_config(
                executor_model="claude-opus-4-7",
                external_supervisor_model=None,
                require_external_supervisor=True,
            )

    def test_supervisor_missing_ok_when_not_required(self) -> None:
        # 短任务路径, 不要求 supervisor
        validate_cross_family_config(
            executor_model="claude-opus-4-7",
            external_supervisor_model=None,
            require_external_supervisor=False,
        )

    def test_local_vs_cloud_passes(self) -> None:
        """V7 §11.3 本地 LLM 跟 cloud 跨 family, 满足 cross-family."""
        validate_cross_family_config(
            executor_model="claude-opus-4-7",
            external_supervisor_model="qwen-32b",  # 本地
        )
