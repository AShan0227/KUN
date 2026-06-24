"""V7 §11.2 cross-family LLM family classification + validation.

KUN V7 强制要求 External Supervisor (§10.2.3) / 启 Explorer Pool (§9.6) 等
跨 LLM critique 角色必须使用**不同 family** 的 LLM, 而不只是 "different vendor"
或 "different model name". 因为同 family 内不同 tier (e.g. Anthropic Opus +
Anthropic Haiku) 的 training pipeline 一致, bias 一致, 跨 critique 无价值.

V7 附录 B 定义 9 个 family:
- Anthropic Claude 系
- OpenAI GPT 系
- Qwen 系
- Llama 系
- DeepSeek 系
- Mistral 系
- Google Gemini 系
- MiniMax 系
- 本地小模型其他 (Phi / Gemma / OpenHermes 等)

本模块提供:
1. ``classify_family(model_id)`` — 给 model id 返 family
2. ``is_cross_family(model_a, model_b)`` — 两个 model 是否跨 family
3. ``validate_cross_family_config(...)`` — daemon 启动时 config 校验
"""

from __future__ import annotations

from enum import StrEnum


class LLMFamily(StrEnum):
    """LLM family classifications per V7 附录 B."""

    ANTHROPIC = "anthropic_claude"  # Opus / Sonnet / Haiku
    OPENAI_GPT = "openai_gpt"  # gpt-4 / gpt-5 / gpt-5.5 / Codex
    QWEN = "qwen"  # Qwen-7B / 32B / Max (本地或 cloud)
    LLAMA = "llama"  # Llama-3 / 3.1 / derivatives
    DEEPSEEK = "deepseek"  # DeepSeek-V3 / R1
    MISTRAL = "mistral"  # Mistral / Mixtral
    GEMINI = "gemini"  # Gemini Pro / Flash
    MINIMAX = "minimax"  # MiniMax-M2 / M2.7
    LOCAL_OTHER = "local_other"  # Phi / Gemma / OpenHermes 等
    UNKNOWN = "unknown"  # 无法识别的 model


# Model name pattern → family. 顺序敏感: 更具体的 pattern 先匹配。
_FAMILY_PATTERNS: tuple[tuple[str, LLMFamily], ...] = (
    # Anthropic family — claude- prefix 或 anthropic 字眼
    ("claude", LLMFamily.ANTHROPIC),
    ("anthropic", LLMFamily.ANTHROPIC),
    # OpenAI GPT family — gpt- prefix 或 codex
    ("gpt-", LLMFamily.OPENAI_GPT),
    ("codex", LLMFamily.OPENAI_GPT),
    # Qwen family
    ("qwen", LLMFamily.QWEN),
    # Llama family (注意先于 generic 'local-' 匹配)
    ("llama", LLMFamily.LLAMA),
    # DeepSeek
    ("deepseek", LLMFamily.DEEPSEEK),
    # Mistral / Mixtral
    ("mistral", LLMFamily.MISTRAL),
    ("mixtral", LLMFamily.MISTRAL),
    # Google Gemini
    ("gemini", LLMFamily.GEMINI),
    # MiniMax
    ("minimax", LLMFamily.MINIMAX),
    # 本地小模型其他常见名
    ("phi-", LLMFamily.LOCAL_OTHER),
    ("gemma", LLMFamily.LOCAL_OTHER),
    ("openhermes", LLMFamily.LOCAL_OTHER),
    ("hermes-", LLMFamily.LOCAL_OTHER),
)


def classify_family(model_id: str) -> LLMFamily:
    """Classify a model_id into one of the V7 附录 B LLM families.

    Match is case-insensitive substring on common model name patterns.
    Returns ``LLMFamily.UNKNOWN`` if no pattern matches.

    Examples:
        >>> classify_family("claude-opus-4-7")
        <LLMFamily.ANTHROPIC: 'anthropic_claude'>
        >>> classify_family("gpt-5.5")
        <LLMFamily.OPENAI_GPT: 'openai_gpt'>
        >>> classify_family("qwen-32b")
        <LLMFamily.QWEN: 'qwen'>
        >>> classify_family("some-random-name")
        <LLMFamily.UNKNOWN: 'unknown'>
    """
    lower = model_id.lower()
    for pattern, family in _FAMILY_PATTERNS:
        if pattern in lower:
            return family
    return LLMFamily.UNKNOWN


def is_cross_family(model_a: str, model_b: str) -> bool:
    """Return True if two models are in different LLM families.

    Per V7 §11.2 cross-family 强制约束:
    - Anthropic Opus + Anthropic Haiku → **same family** (not cross)
    - Anthropic Opus + OpenAI gpt-5 → **cross-family**
    - 本地 Qwen + cloud Anthropic Haiku → **cross-family**

    UNKNOWN family + 任何 family = treated as cross (safer default; unknown
    家族不该假装跟某 family 等同).
    """
    family_a = classify_family(model_a)
    family_b = classify_family(model_b)
    # UNKNOWN treated as cross (defensive default)
    if family_a == LLMFamily.UNKNOWN or family_b == LLMFamily.UNKNOWN:
        return True
    return family_a != family_b


class CrossFamilyConfigError(ValueError):
    """Raised when daemon detects same-family Executor + External Supervisor."""


def validate_cross_family_config(
    *,
    executor_model: str,
    external_supervisor_model: str | None,
    require_external_supervisor: bool = True,
) -> None:
    """Validate cross-family config at daemon startup (V7 §11.2).

    Raises:
        CrossFamilyConfigError: if violation.

    Rules:
        1. If ``require_external_supervisor=True`` and ``external_supervisor_model``
           is None → raise (long task requires External Supervisor).
        2. If both configured, they must be cross-family.

    Defensive default: rule 1 active for long-task production paths.
    """
    if external_supervisor_model is None:
        if require_external_supervisor:
            raise CrossFamilyConfigError(
                "External Supervisor model not configured. "
                "V7 §11.1 requires cross-family External Supervisor for long tasks. "
                "Set KUN_EXTERNAL_SUPERVISOR_MODEL or pass external_supervisor_model."
            )
        return  # short-task / opt-out path

    if not is_cross_family(executor_model, external_supervisor_model):
        executor_family = classify_family(executor_model)
        external_family = classify_family(external_supervisor_model)
        raise CrossFamilyConfigError(
            f"Cross-family required: executor={executor_model} (family={executor_family.value}) "
            f"vs external_supervisor={external_supervisor_model} (family={external_family.value}). "
            f"Same-family Supervisor cannot provide independent critique. "
            f"V7 §11.2 / §10.2.5 监督者递归不变量."
        )


__all__ = [
    "CrossFamilyConfigError",
    "LLMFamily",
    "classify_family",
    "is_cross_family",
    "validate_cross_family_config",
]
