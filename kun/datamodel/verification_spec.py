"""任务完成验证规格 (BATCH4 C3 / T53)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VerificationKind = Literal[
    "exact_output",
    "test_pass",
    "lint_pass",
    "url_check",
    "human_approval",
    "hash_match",
    "schema_validate",
]


class VerificationSpec(BaseModel):
    """单个可验证产物的验收规格."""

    model_config = ConfigDict(extra="forbid")

    kind: VerificationKind
    spec: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    timeout_sec: int = Field(default=60, gt=0, le=3600)


class VerificationResult(BaseModel):
    """验证结果."""

    model_config = ConfigDict(extra="forbid")

    kind: VerificationKind
    passed: bool
    evidence_url: str | None = None
    error_msg: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
