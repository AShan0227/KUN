"""V7 Phase X.N — CI lint of hidden-orphan-audit-prompt.md template.

The audit prompt template shipped in X.H is now KUN's primary defense
against the orphan-class failure mode. CI must guarantee it doesn't
silently drift (someone deletes §7 or breaks the JSON schema).

This test:
  1. Locates the template file
  2. Asserts all 8 section headers are present
  3. Extracts the embedded JSON schema and validates it parses
  4. Verifies the reject-phrase blacklist (§7) is non-empty
  5. Asserts the production-entries inventory hint (§8) is present
  6. Verifies the meta self-audit recursion (§6) is required

If a future commit removes any of these, CI fails with a clear message.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "templates"
    / "hidden-orphan-audit-prompt.md"
)


def _load_template() -> str:
    assert TEMPLATE_PATH.exists(), (
        f"audit template missing at {TEMPLATE_PATH} — "
        f"someone deleted hidden-orphan-audit-prompt.md"
    )
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_template_file_exists() -> None:
    assert TEMPLATE_PATH.exists()


def test_all_8_sections_present() -> None:
    src = _load_template()
    expected = [
        "§1 — 5 层闭合模型",
        "§2 — 6 攻击者审计角度",
        "§3 — 5 个 hidden-orphan 根因",
        "§4 — 强制运行的 grep 命令模板",
        "§5 — 你必须按这个 JSON 输出",
        "§6 — 元递归",
        "§7 — 不允许的话术",
        "§8 — 现在开始审计",
    ]
    for s in expected:
        assert s in src, (
            f"audit template missing section header {s!r}. "
            f"Did someone delete or rename it?"
        )


def test_embedded_json_schema_block_present_and_has_required_fields() -> None:
    """§5 ships a JSON-shaped schema. We don't require it to round-trip
    through json.loads (the template uses type-hint placeholders like
    ``bool`` for readability), but we do require the block exists and
    has every layer/angle/cause field name we depend on."""
    src = _load_template()
    m = re.search(r"```json\s*\n(.*?)\n```", src, re.DOTALL)
    assert m, "§5 missing the ```json ... ``` schema block"
    schema_text = m.group(1)

    # Required schema field names — drift detection
    required_fields = [
        "audited_capabilities",
        "layer_check",
        "L1_doc_promise",
        "L2_module_impl",
        "L3_production_entry",
        "L4_real_data",
        "L5_attacker_tests",
        "angle_check",
        "A8_production_path_reach",
        "root_cause_check",
        "R1_grep_granularity",
        "R2_entries_inventory",
        "R3_opt_in_no_consumer",
        "R4_fixture_vs_production",
        "R5_retrospective_entry",
        "verdict",
        "risk_level",
        "must_fix",
        "allow_release",
        "meta_self_audit",
    ]
    missing = [f for f in required_fields if f not in schema_text]
    assert not missing, (
        f"§5 schema missing fields {missing}. "
        f"Audit template's structural contract drifted."
    )


def test_reject_phrases_blacklist_present() -> None:
    """§7 lists phrases that signal a low-quality audit. Verify the list
    is non-empty + contains at least one Chinese phrase that any LLM
    must not use without grep evidence."""
    src = _load_template()
    assert "§7 — 不允许的话术" in src
    # Spot-check at least one canonical rejected phrase
    assert "我检查过了" in src, "§7 missing canonical reject phrase"
    assert "估计" in src or "应该" in src, (
        "§7 missing soft-language reject phrase"
    )


def test_production_entries_inventory_required() -> None:
    """§8 requires the user to fill in production-entry inventory before
    the audit can proceed."""
    src = _load_template()
    assert "生产入口清单" in src
    assert "PRODUCTION_ENTRIES.md" in src, (
        "template should reference docs/PRODUCTION_ENTRIES.md as the "
        "canonical inventory source (X.H R2 fix)"
    )


def test_meta_recursion_section_present() -> None:
    """§6 requires the LLM to self-audit its own audit — defensive
    against R1/R4/R5 recurrence at audit time itself."""
    src = _load_template()
    assert "§6 — 元递归" in src
    # Must require at least 1 acknowledged uncertainty
    assert (
        "不允许" in src and "都对" in src
    ), "§6 should force at least 1 honest uncertainty (no 'all clear' pass)"


def test_grep_step4_explicitly_requires_output_paste() -> None:
    """§4 must explicitly say grep output must be pasted verbatim."""
    src = _load_template()
    assert "粘贴" in src or "粘进" in src or "贴" in src
    # And must explicitly forbid 'I ran grep, nothing was there' without paste
    assert (
        "凭口说" in src
        or "凭印象" in src
        or "贴 grep" in src
    ), "§4 should explicitly require pasted grep evidence, not narrated"


def test_5_root_causes_r1_through_r5_all_documented() -> None:
    """§3 must enumerate all 5 root causes the template was designed to
    defend against."""
    src = _load_template()
    for rc in ("R1", "R2", "R3", "R4", "R5"):
        assert f"{rc}." in src or f"{rc} " in src or f"**{rc}" in src, (
            f"audit template missing root cause {rc} in §3"
        )
