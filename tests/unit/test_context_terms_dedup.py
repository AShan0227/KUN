"""Audit F098: the lexical tokenizer is shared, not copy-pasted.

``_terms`` was byte-identical in kun/context/importance.py and packer.py. Both
now import the single source in kun/context/text_terms.py. These tests pin the
dedup so the copy-paste cannot silently return, and lock the tokenizer behavior.
"""

from __future__ import annotations

import pytest

from kun.context import importance, packer
from kun.context.text_terms import terms


@pytest.mark.unit
def test_importance_and_packer_share_one_terms_function() -> None:
    assert importance._terms is terms
    assert packer._terms is terms


@pytest.mark.unit
def test_terms_tokenizes_lowercased_min_length_two() -> None:
    out = terms("Fix PyTest a b2 .x foo-bar")
    assert out == {"fix", "pytest", "b2", ".x", "foo-bar"}
    # single chars (a) dropped; everything lowercased
    assert "a" not in out
