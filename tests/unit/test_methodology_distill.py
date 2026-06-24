"""L2.9 — methodology_distill 单测.

工程化抽取, 无 LLM. 测试用 tmp_path 隔离 fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.engineering.methodology_distill import (
    DistillReport,
    MethodologyCandidate,
    _candidate_overlaps_existing,
    _normalize_title_to_slug,
    distill,
    existing_seed_topics,
    scan_dev_logs,
)

# ---- helpers ----


def _write_dev_log(dev_logs_root: Path, name: str, content: str) -> Path:
    dev_logs_root.mkdir(parents=True, exist_ok=True)
    p = dev_logs_root / name
    p.write_text(content, encoding="utf-8")
    return p


def _write_seed(seeds_root: Path, topic: str) -> Path:
    seeds_root.mkdir(parents=True, exist_ok=True)
    p = seeds_root / f"{topic}.yaml"
    p.write_text(f"topic: {topic}\ntitle: dummy\n", encoding="utf-8")
    return p


# ---- _normalize_title_to_slug ----


def test_normalize_slug_english() -> None:
    assert _normalize_title_to_slug("Use word-boundary regex for keywords") == (
        "use_word_boundary_regex_for_keywords"
    )


def test_normalize_slug_drops_markdown() -> None:
    slug = _normalize_title_to_slug("**Bold title**")
    assert slug == "bold_title"


def test_normalize_slug_chinese_only_uses_fallback() -> None:
    slug = _normalize_title_to_slug("中文标题示例")
    assert slug.startswith("zh_")


# ---- scan_dev_logs basic ----


def test_scan_empty_root_returns_empty(tmp_path: Path) -> None:
    out = scan_dev_logs(tmp_path / "missing")
    assert out == []


def test_scan_simple_progress_log(tmp_path: Path) -> None:
    content = """\
## L2.5 · Mode A

**做了什么**：
- new module foo

**关键决策**：
- engineering-first beats LLM-first for hot-path code
- 4 rules independent rule_results

**为下一步**：
- L2.6 next.
"""
    _write_dev_log(tmp_path, "L2-progress.md", content)
    candidates = scan_dev_logs(tmp_path)
    assert len(candidates) == 2
    titles = [c.title for c in candidates]
    assert any("engineering-first" in t for t in titles)
    assert any("4 rules independent rule_results" in t for t in titles)
    assert all(c.source_section == "L2.5" for c in candidates)


def test_scan_extracts_title_before_colon(tmp_path: Path) -> None:
    """bullet 形式 `title — rationale` 把 title 切出来."""
    content = """\
## L2.6 · RCDH

**关键决策**：
- word-boundary regex — 子串误判会让 L0 抢 L2/L3 case
"""
    _write_dev_log(tmp_path, "L2-progress.md", content)
    cands = scan_dev_logs(tmp_path)
    assert len(cands) == 1
    # title = colon/dash 前内容
    assert cands[0].title.startswith("word-boundary regex")
    assert "子串误判" in cands[0].rationale


def test_scan_picks_multiple_sections(tmp_path: Path) -> None:
    content = """\
## L2.1 · Foo
**关键决策**：
- alpha
- beta

---

## L2.2 · Bar
**关键决策**：
- gamma
"""
    _write_dev_log(tmp_path, "L2-progress.md", content)
    cands = scan_dev_logs(tmp_path)
    titles = [c.title for c in cands]
    assert "alpha" in titles
    assert "beta" in titles
    assert "gamma" in titles


def test_scan_english_header_also_recognized(tmp_path: Path) -> None:
    content = """\
## L1.10
**Key Decisions:**
- prefer paired commits over giant PRs
"""
    _write_dev_log(tmp_path, "L1-retrospective.md", content)
    cands = scan_dev_logs(tmp_path)
    assert len(cands) == 1
    assert "paired commits" in cands[0].title.lower()


def test_scan_methodology_card_candidates_header(tmp_path: Path) -> None:
    content = """\
## L1.10

## Methodology Card Candidates

- card_one — one-line
- card_two — two-line
"""
    _write_dev_log(tmp_path, "L1-retrospective.md", content)
    cands = scan_dev_logs(tmp_path)
    titles = [c.title for c in cands]
    assert "card_one" in titles
    assert "card_two" in titles


def test_scan_skips_non_decision_bullets(tmp_path: Path) -> None:
    content = """\
## L2.5
**做了什么**：
- some implementation detail (should be ignored)

**关键决策**：
- the real decision
"""
    _write_dev_log(tmp_path, "L2-progress.md", content)
    cands = scan_dev_logs(tmp_path)
    titles = [c.title for c in cands]
    assert "the real decision" in titles
    assert "some implementation detail" not in titles[0]


# ---- existing_seed_topics ----


def test_existing_seed_topics_loads_yamls(tmp_path: Path) -> None:
    _write_seed(tmp_path, "anchor_pinning_at_prompt_top")
    _write_seed(tmp_path, "conservative_sample_threshold")
    out = existing_seed_topics(tmp_path)
    assert out == {
        "anchor_pinning_at_prompt_top",
        "conservative_sample_threshold",
    }


def test_existing_seed_topics_missing_dir(tmp_path: Path) -> None:
    out = existing_seed_topics(tmp_path / "missing")
    assert out == set()


def test_existing_seed_topics_skips_bad_yaml(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "good.yaml").write_text("topic: real_one\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("not: valid: yaml: [", encoding="utf-8")
    out = existing_seed_topics(tmp_path)
    assert "real_one" in out


# ---- overlap dedup ----


def test_overlap_direct_slug_match() -> None:
    c = MethodologyCandidate(
        topic_slug="anchor_pinning_at_prompt_top",
        title="x", rationale="x", source_file="x", source_section="x",
    )
    assert _candidate_overlaps_existing(c, {"anchor_pinning_at_prompt_top"})


def test_overlap_two_token_intersection() -> None:
    c = MethodologyCandidate(
        topic_slug="anchor_pinning_strategy",
        title="x", rationale="x", source_file="x", source_section="x",
    )
    # 与 anchor_pinning_at_prompt_top 有 anchor + pinning 共现 → 算重叠
    assert _candidate_overlaps_existing(c, {"anchor_pinning_at_prompt_top"})


def test_overlap_single_token_not_enough() -> None:
    c = MethodologyCandidate(
        topic_slug="foo_anchor_xyz",
        title="x", rationale="x", source_file="x", source_section="x",
    )
    # 与 anchor_pinning_at_prompt_top 只 anchor 一个 token 共现 → 不算
    assert not _candidate_overlaps_existing(
        c, {"anchor_pinning_at_prompt_top"}
    )


# ---- distill end-to-end ----


def test_distill_end_to_end(tmp_path: Path) -> None:
    dev_logs = tmp_path / "dev_logs"
    seeds = tmp_path / "seeds"

    content = """\
## L2.5
**关键决策**：
- engineering-first beats LLM-first
- anchor pinning at top — already covered (should dedup)
"""
    _write_dev_log(dev_logs, "L2-progress.md", content)
    _write_seed(seeds, "anchor_pinning_at_prompt_top")

    report = distill(dev_logs_root=dev_logs, seeds_root=seeds)
    assert isinstance(report, DistillReport)
    assert report.total_scanned == 2
    novel_titles = [c.title for c in report.novel_candidates]
    assert any("engineering-first" in t for t in novel_titles)
    # anchor pinning candidate 被 dedup
    assert all("anchor pinning at top" not in t for t in novel_titles)
    assert report.duplicates_skipped >= 1


def test_distill_with_no_existing_seeds(tmp_path: Path) -> None:
    dev_logs = tmp_path / "dev_logs"
    seeds = tmp_path / "seeds_missing"

    content = """\
## L2.X
**关键决策**：
- new methodology one
"""
    _write_dev_log(dev_logs, "L2-progress.md", content)

    report = distill(dev_logs_root=dev_logs, seeds_root=seeds)
    assert len(report.novel_candidates) == 1


def test_distill_dedupes_intra_scan_duplicates(tmp_path: Path) -> None:
    """同一 candidate 在两个 dev_log section 都出现 → 只留 1."""
    dev_logs = tmp_path / "dev_logs"

    a = """\
## L2.A
**关键决策**：
- the same idea repeated
"""
    b = """\
## L2.B
**关键决策**：
- the same idea repeated
"""
    _write_dev_log(dev_logs, "L2-progress.md", a)
    _write_dev_log(dev_logs, "L2-retrospective.md", b)
    report = distill(dev_logs_root=dev_logs, seeds_root=tmp_path / "no_seeds")
    assert len(report.novel_candidates) == 1
    assert report.duplicates_skipped == 1


# ---- IdleBatchStep wrapper ----


@pytest.mark.asyncio
async def test_methodology_distill_step_uses_real_distiller() -> None:
    """idle_batch.MethodologyDistillStep 调真 distill, 不再是 stub."""
    from kun.engineering.idle_batch import MethodologyDistillStep

    step = MethodologyDistillStep()
    assert step.stub is False  # 不再 stub
    result = await step.run(tenant_id="u-test")
    # 结构包含真 distill 字段
    assert "total_scanned" in result
    assert "novel_candidates" in result
    assert "duplicates_skipped" in result
    assert "sources_scanned" in result
    assert result["next_action"] in (
        "review_novel_candidates",
        "no_distillation_action",
    )
