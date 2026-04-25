"""人类评审对齐占位。

目标：多判官投票和人类评审的 Spearman 相关系数长期达到 0.80+。
当前仓库还没有人工标注集，所以这里先保留一个显式占位，避免目标丢失。
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.skip(reason="需要人工标注集后再启用")
def test_multi_judge_human_alignment_placeholder() -> None:
    """等有人工标注集后，在这里计算 Spearman >= 0.80。"""
