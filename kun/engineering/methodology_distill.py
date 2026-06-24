"""Methodology Distill — 自动从 dev_logs 抽取候选方法论卡 (ADR-025).

输入: docs/dev_logs/L*-progress.md + L*-retrospective.md + 已有 seeds/methodologies/*.yaml
处理:
  1. 扫描 dev_logs 找 ## L 标题 + 关键决策 / Key Decisions / Methodology Card Candidates 段
  2. 提取每条 bullet 作为候选方法论
  3. 与已有 seeds topic 集合做去重 (title 子串 + topic slug 匹配)
  4. 输出 MethodologyCandidate dict 列表给上游 (LLM 整理 / 人筛选)

工程化抽取, 不调 LLM (L2 范围). LLM 把 candidate 转成完整 YAML 在 L3+ 闭环.
这个模块的存在意义: KUN 自己看自己的 dev_log → 持续蒸馏自己的开发方法论.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kun.core.logging import get_logger

log = get_logger("kun.engineering.methodology_distill")


# 段落标记 — 中英文混合; colon 可在 ** ** 之内或之外
_DECISION_HEADERS = (
    r"\*\*关键决策[：:]?\*\*[：:]?",
    r"\*\*Key Decisions[：:]?\*\*[：:]?",
    r"\*\*Methodology Card Candidates[：:]?\*\*[：:]?",
    r"## Methodology Card Candidates",
    r"### 关键决策",
)
_DECISION_HEADER_RE = re.compile("|".join(_DECISION_HEADERS), re.IGNORECASE)

# bullet list patterns — 主流形式 "- text" 或 "* text" 或编号 "1. text"
_BULLET_RE = re.compile(r"^[\s]*(?:[-*]|\d+\.)\s+(.+?)$", re.MULTILINE)

# 章节边界 — 下一个 ## 标题 / --- / 新的 **...**: 段落
_SECTION_END_RE = re.compile(
    r"^(##|---|\*\*[^*\n]+\*\*[：:]?\s*$)", re.MULTILINE
)


@dataclass(frozen=True)
class MethodologyCandidate:
    """单条候选方法论."""

    topic_slug: str  # 用首句 normalize 出来, 作 dedup key
    title: str  # bullet 第一句, 去 markdown 强调
    rationale: str  # 完整 bullet 内容 (含 dash 之后)
    source_file: str
    source_section: str  # 来自哪个章节标题 (e.g. "L2.5")
    confidence: float = 0.5  # 工程化抽取默认中等; LLM 整理后可调
    evidence_pointers: list[dict[str, Any]] = field(default_factory=list)


def _normalize_title_to_slug(title: str) -> str:
    """从 title 推 topic_slug (用于 dedup).

    保留 a-z0-9_, 中文转拼音过于复杂 — 中文 title 直接转小写 + 截前 40 ASCII 字符
    + hash 后缀避免冲突 (本提交不必要, 简单实现即可).
    """
    # 只剥 markdown 强调 / backtick, 保留下划线
    cleaned = re.sub(r"\*\*|\*|`", "", title).lower().strip()
    # ASCII 部分: 字母 / 数字 / 下划线连续段
    ascii_parts = re.findall(r"[a-z0-9_]+", cleaned)
    if ascii_parts:
        # 压缩重复下划线 + 拼接
        raw = "_".join(ascii_parts)
        raw = re.sub(r"_+", "_", raw).strip("_")
        slug = raw[:60]
    else:
        # 全中文 fallback — 取前 8 字符 + 长度作为 slug
        slug = f"zh_{len(cleaned)}_{abs(hash(cleaned)) % 10000:04d}"
    return slug


def _extract_section_title(text: str, header_match: re.Match[str]) -> str:
    """从 header 上方往回找 `## L2.X` 标题."""
    upper = text[: header_match.start()]
    matches = list(re.finditer(r"##\s+([^\n]+)", upper))
    if matches:
        last = matches[-1].group(1).strip()
        # 提取 L 编号
        m = re.search(r"L\d+(?:\.\d+)?", last)
        if m:
            return m.group(0)
        return last[:60]
    return "unknown"


def _scan_one_file(path: Path) -> list[MethodologyCandidate]:
    """扫单个 dev log 文件, 抽 ## section 下的"关键决策" bullets."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("methodology_distill.read_failed", path=str(path), error=str(e))
        return []

    candidates: list[MethodologyCandidate] = []
    for header_match in _DECISION_HEADER_RE.finditer(text):
        section_title = _extract_section_title(text, header_match)

        # 找下一个章节边界
        rest = text[header_match.end():]
        end_match = _SECTION_END_RE.search(rest)
        body = rest[: end_match.start()] if end_match else rest

        # 抽 bullets
        for bullet_match in _BULLET_RE.finditer(body):
            bullet_text = bullet_match.group(1).strip()
            if not bullet_text:
                continue

            # title = bullet 第一个冒号 / em dash 前的内容
            title_candidate = re.split(r"[：:]|—|--", bullet_text, maxsplit=1)[0].strip()
            # 只剥 markdown 强调 (** / * / `), 保留 identifier 中的下划线
            title_candidate = re.sub(r"\*\*|\*|`", "", title_candidate).strip()
            if not title_candidate:
                title_candidate = bullet_text[:80]

            candidates.append(
                MethodologyCandidate(
                    topic_slug=_normalize_title_to_slug(title_candidate),
                    title=title_candidate[:120],
                    rationale=bullet_text,
                    source_file=str(path),
                    source_section=section_title,
                    evidence_pointers=[
                        {
                            "type": "dev_log_section",
                            "file": str(path.name),
                            "section": section_title,
                        }
                    ],
                )
            )

    return candidates


def scan_dev_logs(dev_logs_root: Path | str) -> list[MethodologyCandidate]:
    """扫整个 docs/dev_logs/ 目录的 L*-progress.md + L*-retrospective.md."""
    root = Path(dev_logs_root)
    if not root.exists():
        log.warning("methodology_distill.root_missing", path=str(root))
        return []

    candidates: list[MethodologyCandidate] = []
    patterns = ("L*-progress.md", "L*-retrospective.md", "phase-*-*.md")
    for pat in patterns:
        for path in sorted(root.glob(pat)):
            candidates.extend(_scan_one_file(path))

    log.info(
        "methodology_distill.scanned",
        candidates_found=len(candidates),
        root=str(root),
    )
    return candidates


def existing_seed_topics(seeds_root: Path | str) -> set[str]:
    """读 seeds/methodologies/*.yaml, 提 topic 字段集合."""
    root = Path(seeds_root)
    if not root.exists():
        return set()
    topics: set[str] = set()
    for path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning(
                "methodology_distill.yaml_parse_failed", path=str(path), error=str(e)
            )
            continue
        topic = data.get("topic")
        if isinstance(topic, str) and topic.strip():
            topics.add(topic.strip())
    return topics


def _candidate_overlaps_existing(
    candidate: MethodologyCandidate, existing_topics: set[str]
) -> bool:
    """判定候选是否已被 seed 覆盖.

    匹配规则: topic_slug 直接命中 / 关键词子串 (≥ 2 词共现).
    """
    slug = candidate.topic_slug
    if slug in existing_topics:
        return True
    cand_tokens = set(slug.split("_"))
    if len(cand_tokens) < 2:
        return False
    for topic in existing_topics:
        ex_tokens = set(topic.split("_"))
        # ≥ 2 词共现 → 视为已覆盖
        if len(cand_tokens & ex_tokens) >= 2:
            return True
    return False


@dataclass(frozen=True)
class DistillReport:
    """蒸馏一次的输出."""

    total_scanned: int
    novel_candidates: list[MethodologyCandidate]
    duplicates_skipped: int
    sources: list[str]


def distill(
    *,
    dev_logs_root: Path | str = "docs/dev_logs",
    seeds_root: Path | str = "seeds/methodologies",
) -> DistillReport:
    """主入口 — 扫 dev_logs + 去重 → DistillReport."""
    scanned = scan_dev_logs(dev_logs_root)
    existing = existing_seed_topics(seeds_root)

    novel: list[MethodologyCandidate] = []
    seen_slugs: set[str] = set()
    duplicates_skipped = 0
    for c in scanned:
        if c.topic_slug in seen_slugs:
            duplicates_skipped += 1
            continue
        seen_slugs.add(c.topic_slug)
        if _candidate_overlaps_existing(c, existing):
            duplicates_skipped += 1
            continue
        novel.append(c)

    sources = sorted({c.source_file for c in scanned})
    return DistillReport(
        total_scanned=len(scanned),
        novel_candidates=novel,
        duplicates_skipped=duplicates_skipped,
        sources=sources,
    )


__all__ = [
    "DistillReport",
    "MethodologyCandidate",
    "distill",
    "existing_seed_topics",
    "scan_dev_logs",
]
