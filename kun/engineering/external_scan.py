"""ExternalInfoScanner — 外部信息饱和度监控 (V2.1 §3.10 / 漏洞 3+16).

异步守望驱动 (不阻塞主路径). idle 周期跑外部检索, 把候选方案写到 EmergentSolution 库.

5 关键设计:
- 永不阻塞主路径 (任务执行 critical path 上不查外部)
- 预算可控 (默认 100 次/user/day)
- LLM 复审避免噪声
- 用户可关 (NUO 偏好库)
- 来源透明 (source_url + discovered_at)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
    SourceKind,
)

logger = logging.getLogger(__name__)


# 外部源 fetcher 签名
ExternalFetcher = Callable[[str], Awaitable[list[dict[str, Any]]]]
# GitHub repo metadata fetcher 签名 (url, max_bytes, timeout_sec → response)
ExternalGithubMetadataFetcher = Callable[
    [str, int, float], Awaitable["ExternalGithubFetchResponse"]
]
# LLM 复审签名 (raw_info → 是否对该 task_type 有用 + summary)
LLMReviewer = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]

EXTERNAL_SCAN_STRONG_REVIEW_ENABLED_ENV = "KUN_EXTERNAL_SCAN_STRONG_REVIEW_ENABLED"
EXTERNAL_SCAN_STRONG_REVIEW_MAX_TOKENS_ENV = "KUN_EXTERNAL_SCAN_STRONG_REVIEW_MAX_TOKENS"

ExternalSkillRiskLevel = Literal["low", "medium", "high", "critical"]
ExternalSkillReviewState = Literal["review_only"]
ExternalSkillDemandKind = Literal["coding", "writing", "review", "research", "ops", "unknown"]
ExternalSkillMaintenanceStatus = Literal["maintained", "stale", "deprecated", "unknown"]

_UNKNOWN_LICENSE_VALUES = {"", "unknown", "noassertion", "other", "none", "unlicensed"}
_EXECUTABLE_FILE_PATTERNS = (
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".bat",
    ".cmd",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".rb",
    ".go",
    ".rs",
    ".php",
    ".pl",
)
_EXECUTABLE_NAMES = {
    "makefile",
    "dockerfile",
    "justfile",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "gemfile",
}
_NETWORK_PATTERNS = (
    r"\bcurl\b",
    r"\bwget\b",
    r"\bfetch\s*\(",
    r"\baxios\.",
    r"\brequests\.",
    r"\burllib\.",
    r"\bhttpx\.",
    r"\bsocket\.",
    r"\bgit\s+clone\b",
    r"https?://",
)
_SECRET_PATTERNS = (
    r"api[_-]?key",
    r"secret",
    r"token",
    r"password",
    r"process\.env",
    r"os\.environ",
    r"\.env",
)
_FILE_WRITE_PATTERNS = (
    r">\s*[\w./~-]+",
    r"\btee\b",
    r"\bwriteFile(?:Sync)?\b",
    r"\bfs\.",
    r"\bopen\s*\([^)]*,\s*['\"][wa]",
    r"\bPath\([^)]*\)\.write_",
    r"\brm\s+-rf\b",
    r"\bmv\s+",
    r"\bcp\s+",
)
_GITHUB_INPUT_HOST = "github.com"
_GITHUB_FETCH_HOSTS = {"api.github.com", "raw.githubusercontent.com"}
_GITHUB_REPO_REF_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/" r"(?P<repo>[A-Za-z0-9._-]{1,100})(?:\.git)?$"
)
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_GITHUB_COMMIT_SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_GITHUB_TEXT_FILE_SUFFIXES = (".md", ".mdx", ".txt", ".json", ".yaml", ".yml", ".toml")
_GITHUB_NON_SKILL_DOC_NAMES = {
    "changelog.md",
    "code_of_conduct.md",
    "contributing.md",
    "license",
    "license.md",
    "readme.md",
    "security.md",
}
_GITHUB_DEFAULT_TIMEOUT_SEC = 5.0
_GITHUB_MAX_REPO_METADATA_BYTES = 128 * 1024
_GITHUB_MAX_TREE_BYTES = 2 * 1024 * 1024
_GITHUB_MAX_TREE_ENTRIES = 2000
_GITHUB_MAX_FILE_BYTES = 32 * 1024
_GITHUB_MAX_CANDIDATE_FILES = 24
_GITHUB_MAX_SUPPORT_FILES = 12
_DEMAND_MATCH_KINDS: tuple[ExternalSkillDemandKind, ...] = (
    "coding",
    "writing",
    "review",
    "research",
    "ops",
)
_DEMAND_MATCH_KEYWORDS: dict[ExternalSkillDemandKind, tuple[tuple[str, int], ...]] = {
    "coding": (
        ("implementation", 3),
        ("implement", 3),
        ("coding", 3),
        ("engineering", 2),
        ("engineer", 2),
        ("refactor", 3),
        ("debug", 3),
        ("test", 2),
        ("typescript", 3),
        ("python", 3),
        ("react", 3),
        ("api", 2),
        ("compiler", 2),
        ("lint", 2),
        ("code", 1),
        ("代码", 3),
        ("实现", 3),
        ("测试", 2),
    ),
    "writing": (
        ("documentation", 3),
        ("docs", 3),
        ("writing", 3),
        ("write", 2),
        ("editorial", 3),
        ("copy", 2),
        ("email", 2),
        ("memo", 2),
        ("proposal", 2),
        ("grammar", 2),
        ("style guide", 2),
        ("文档", 3),
        ("写作", 3),
        ("邮件", 2),
    ),
    "review": (
        ("code review", 5),
        ("pull request", 4),
        ("review", 4),
        ("reviewing", 4),
        ("diff", 3),
        ("diffs", 3),
        ("pr", 2),
        ("audit", 3),
        ("critique", 3),
        ("redline", 3),
        ("static analysis", 2),
        ("复审", 4),
        ("审查", 4),
        ("评审", 4),
    ),
    "research": (
        ("research", 4),
        ("literature", 3),
        ("paper", 3),
        ("citation", 3),
        ("sources", 2),
        ("source finding", 3),
        ("synthesis", 3),
        ("summarize", 2),
        ("survey", 3),
        ("arxiv", 3),
        ("资料", 3),
        ("研究", 4),
        ("调研", 4),
    ),
    "ops": (
        ("incident", 4),
        ("runbook", 4),
        ("deploy", 3),
        ("deployment", 3),
        ("ci", 2),
        ("cd", 2),
        ("docker", 3),
        ("kubernetes", 3),
        ("infra", 3),
        ("monitoring", 3),
        ("alert", 2),
        ("release", 2),
        ("migration", 2),
        ("运维", 4),
        ("部署", 3),
        ("监控", 3),
    ),
}


@dataclass(frozen=True)
class ExternalSkillDemandMatch:
    """Review-only task-demand fit inferred from external skill metadata."""

    primary: ExternalSkillDemandKind
    categories: list[ExternalSkillDemandKind]
    confidence: float
    scores: dict[ExternalSkillDemandKind, int] = field(default_factory=dict)
    matched_keywords: dict[ExternalSkillDemandKind, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExternalGithubFetchResponse:
    """Small response envelope for safe, injectable GitHub metadata reads."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class ExternalSkillSource:
    """Transparent provenance for an external skill candidate."""

    kind: str
    repo: str = ""
    url: str = ""
    commit_sha: str = ""
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class ExternalSkillSafetyAssessment:
    """Conservative static safety triage for a candidate skill."""

    risk_level: ExternalSkillRiskLevel
    license_id: str
    license_unknown: bool
    contains_execution_scripts: bool
    external_network_risk: bool
    secret_access_risk: bool
    file_write_risk: bool
    sandbox_suitable: bool
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalSkillMaintenanceAssessment:
    """Offline maintenance signal for a source or candidate."""

    score: float
    status: ExternalSkillMaintenanceStatus
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalSkillSourceRegistration:
    """Review-only registration record for a possible external capability source."""

    source_id: str
    name: str
    summary: str
    source: ExternalSkillSource
    safety: ExternalSkillSafetyAssessment
    maintenance: ExternalSkillMaintenanceAssessment
    demand_match: ExternalSkillDemandMatch
    candidate_count: int = 0
    tags: list[str] = field(default_factory=list)
    review_state: ExternalSkillReviewState = "review_only"
    production_action: bool = False
    promotion_allowed: bool = False
    auto_fetch_allowed: bool = False
    auto_install_allowed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def model_dump(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "summary": self.summary,
            "source": {
                "kind": self.source.kind,
                "repo": self.source.repo,
                "url": self.source.url,
                "commit_sha": self.source.commit_sha,
                "fetched_at": self.source.fetched_at.isoformat()
                if self.source.fetched_at is not None
                else None,
            },
            "safety": {
                "risk_level": self.safety.risk_level,
                "license_id": self.safety.license_id,
                "license_unknown": self.safety.license_unknown,
                "contains_execution_scripts": self.safety.contains_execution_scripts,
                "external_network_risk": self.safety.external_network_risk,
                "secret_access_risk": self.safety.secret_access_risk,
                "file_write_risk": self.safety.file_write_risk,
                "sandbox_suitable": self.safety.sandbox_suitable,
                "reasons": list(self.safety.reasons),
                "evidence": dict(self.safety.evidence),
            },
            "maintenance": {
                "score": self.maintenance.score,
                "status": self.maintenance.status,
                "reasons": list(self.maintenance.reasons),
                "evidence": dict(self.maintenance.evidence),
            },
            "demand_match": {
                "primary": self.demand_match.primary,
                "categories": list(self.demand_match.categories),
                "confidence": self.demand_match.confidence,
                "scores": dict(self.demand_match.scores),
                "matched_keywords": {
                    key: list(value) for key, value in self.demand_match.matched_keywords.items()
                },
                "reasons": list(self.demand_match.reasons),
            },
            "candidate_count": self.candidate_count,
            "tags": list(self.tags),
            "review_state": self.review_state,
            "production_action": self.production_action,
            "promotion_allowed": self.promotion_allowed,
            "auto_fetch_allowed": self.auto_fetch_allowed,
            "auto_install_allowed": self.auto_install_allowed,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ExternalSkillCandidate:
    """Review-only normalized external skill/behavior-template candidate."""

    candidate_id: str
    name: str
    summary: str
    source: ExternalSkillSource
    safety: ExternalSkillSafetyAssessment
    maintenance: ExternalSkillMaintenanceAssessment
    demand_match: ExternalSkillDemandMatch
    tags: list[str] = field(default_factory=list)
    review_state: ExternalSkillReviewState = "review_only"
    production_action: bool = False
    promotion_allowed: bool = False
    auto_install_allowed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_review_signal(self, tenant_id: str) -> Any:
        """Convert the candidate into a Qi problem signal for background review."""

        from kun.qi.problem_queue import QiProblemSignal

        severity = {
            "low": "info",
            "medium": "warning",
            "high": "error",
            "critical": "critical",
        }[self.safety.risk_level]
        return QiProblemSignal.build(
            tenant_id=tenant_id,
            category="risk",
            severity=severity,
            summary=f"Review external skill candidate: {self.name}",
            source="external_skill.discovery.candidate",
            task_type="skill.external_review",
            evidence=self.model_dump(),
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "summary": self.summary,
            "source": {
                "kind": self.source.kind,
                "repo": self.source.repo,
                "url": self.source.url,
                "commit_sha": self.source.commit_sha,
                "fetched_at": self.source.fetched_at.isoformat()
                if self.source.fetched_at is not None
                else None,
            },
            "safety": {
                "risk_level": self.safety.risk_level,
                "license_id": self.safety.license_id,
                "license_unknown": self.safety.license_unknown,
                "contains_execution_scripts": self.safety.contains_execution_scripts,
                "external_network_risk": self.safety.external_network_risk,
                "secret_access_risk": self.safety.secret_access_risk,
                "file_write_risk": self.safety.file_write_risk,
                "sandbox_suitable": self.safety.sandbox_suitable,
                "reasons": list(self.safety.reasons),
                "evidence": dict(self.safety.evidence),
            },
            "maintenance": {
                "score": self.maintenance.score,
                "status": self.maintenance.status,
                "reasons": list(self.maintenance.reasons),
                "evidence": dict(self.maintenance.evidence),
            },
            "demand_match": {
                "primary": self.demand_match.primary,
                "categories": list(self.demand_match.categories),
                "confidence": self.demand_match.confidence,
                "scores": dict(self.demand_match.scores),
                "matched_keywords": {
                    key: list(value) for key, value in self.demand_match.matched_keywords.items()
                },
                "reasons": list(self.demand_match.reasons),
            },
            "tags": list(self.tags),
            "review_state": self.review_state,
            "production_action": self.production_action,
            "promotion_allowed": self.promotion_allowed,
            "auto_install_allowed": self.auto_install_allowed,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ExternalSkillScanResult:
    """Review-only scan result for external skill metadata."""

    source_items: int
    candidates: list[ExternalSkillCandidate]
    risk_counts: dict[str, int]
    sandbox_suitable: int
    production_action: bool = False
    auto_install_allowed: bool = False
    promotion_allowed: bool = False

    def model_dump(self) -> dict[str, Any]:
        return {
            "source_items": self.source_items,
            "candidates": len(self.candidates),
            "risk_counts": dict(self.risk_counts),
            "sandbox_suitable": self.sandbox_suitable,
            "production_action": self.production_action,
            "auto_install_allowed": self.auto_install_allowed,
            "promotion_allowed": self.promotion_allowed,
            "top_candidates": [
                candidate.model_dump()
                for candidate in sorted(
                    self.candidates,
                    key=lambda item: (
                        _risk_sort_rank(item.safety.risk_level),
                        item.name,
                        item.candidate_id,
                    ),
                )[:5]
            ],
        }


@dataclass
class ScanBudget:
    """外部检索预算 (per-user per-day)."""

    user_id: str
    daily_limit: int = 100
    used_today: int = 0
    window_start: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ScanResult:
    """单次扫描结果摘要."""

    user_id: str
    scanned_task_types: list[str]
    sources_queried: int = 0
    candidates_added: int = 0
    candidates_rejected: int = 0
    duration_sec: float = 0.0


async def fetch_github_repo_external_skill_metadata(
    repo_ref: str,
    *,
    fetcher: ExternalGithubMetadataFetcher | None = None,
    timeout_sec: float = _GITHUB_DEFAULT_TIMEOUT_SEC,
    max_repo_metadata_bytes: int = _GITHUB_MAX_REPO_METADATA_BYTES,
    max_tree_bytes: int = _GITHUB_MAX_TREE_BYTES,
    max_tree_entries: int = _GITHUB_MAX_TREE_ENTRIES,
    max_file_bytes: int = _GITHUB_MAX_FILE_BYTES,
    max_candidate_files: int = _GITHUB_MAX_CANDIDATE_FILES,
    max_support_files: int = _GITHUB_MAX_SUPPORT_FILES,
) -> dict[str, Any]:
    """Fetch GitHub repo metadata and normalize it into review-only skill rows.

    This is a read-only discovery helper. It accepts either ``owner/name`` or a
    ``https://github.com/owner/name`` URL, then only calls GitHub API/raw
    metadata endpoints. It does not import, install, execute, or register any
    discovered code.
    """

    owner, repo = _parse_github_repo_ref(repo_ref)
    safe_timeout = _bounded_github_timeout(timeout_sec)
    safe_fetcher = fetcher or _default_github_metadata_fetcher
    repo_api_url = _github_api_url(f"/repos/{owner}/{repo}")
    repo_payload = await _github_fetch_json(
        repo_api_url,
        fetcher=safe_fetcher,
        max_bytes=max_repo_metadata_bytes,
        timeout_sec=safe_timeout,
    )

    full_name = _first_text(repo_payload.get("full_name"), f"{owner}/{repo}")
    default_branch = _safe_github_ref(
        _first_text(repo_payload.get("default_branch"), "main") or "main"
    )
    commit_payload = await _github_fetch_json(
        _github_api_url(f"/repos/{owner}/{repo}/commits/{quote(default_branch, safe='')}"),
        fetcher=safe_fetcher,
        max_bytes=max_repo_metadata_bytes,
        timeout_sec=safe_timeout,
    )
    pinned_commit_sha = _safe_github_commit_sha(_first_text(commit_payload.get("sha")))
    html_url = f"https://github.com/{owner}/{repo}"
    description = _first_text(repo_payload.get("description"))
    stars = _safe_int(repo_payload.get("stargazers_count"))
    license_value = repo_payload.get("license")

    tree_api_url = _github_api_url(
        f"/repos/{owner}/{repo}/git/trees/{pinned_commit_sha}?recursive=1"
    )
    tree_payload = await _github_fetch_json(
        tree_api_url,
        fetcher=safe_fetcher,
        max_bytes=max_tree_bytes,
        timeout_sec=safe_timeout,
    )
    raw_tree = tree_payload.get("tree")
    tree_entries = _github_tree_entries(raw_tree, max_entries=max_tree_entries)
    tree_entry_limit_reached = isinstance(raw_tree, list) and len(raw_tree) > len(tree_entries)

    candidate_entries = _github_candidate_skill_entries(
        tree_entries,
        repo_name=repo,
        limit=max_candidate_files,
    )
    support_entries = _github_support_file_entries(
        tree_entries,
        candidate_entries=candidate_entries,
        limit=max_support_files,
    )

    candidate_files = [
        await _github_fetch_tree_file_fragment(
            owner=owner,
            repo=repo,
            ref=pinned_commit_sha,
            entry=entry,
            fetcher=safe_fetcher,
            timeout_sec=safe_timeout,
            max_file_bytes=max_file_bytes,
        )
        for entry in candidate_entries
    ]
    support_files = [
        await _github_fetch_tree_file_fragment(
            owner=owner,
            repo=repo,
            ref=pinned_commit_sha,
            entry=entry,
            fetcher=safe_fetcher,
            timeout_sec=safe_timeout,
            max_file_bytes=max_file_bytes,
        )
        for entry in support_entries
    ]
    skills = [
        _github_skill_payload_from_file(
            owner=owner,
            repo=repo,
            ref=pinned_commit_sha,
            repo_description=description,
            file=file,
        )
        for file in candidate_files
    ]

    return {
        "source_kind": "github_repo",
        "repo": full_name,
        "full_name": full_name,
        "name": _first_text(repo_payload.get("name"), repo),
        "url": html_url,
        "html_url": html_url,
        "default_branch": default_branch,
        "commit_sha": pinned_commit_sha,
        "license": license_value,
        "stars": stars,
        "stargazers_count": stars,
        "description": description,
        "topics": repo_payload.get("topics")
        if isinstance(repo_payload.get("topics"), list)
        else [],
        "files": support_files,
        "skills": skills,
        "fetched_at": datetime.now(UTC).isoformat(),
        "review_state": "review_only",
        "production_action": False,
        "promotion_allowed": False,
        "auto_install_allowed": False,
        "metadata": {
            "github_repo_id": repo_payload.get("id"),
            "pinned_commit_sha": pinned_commit_sha,
            "tree_sha": tree_payload.get("sha"),
            "tree_truncated": bool(tree_payload.get("truncated")),
            "tree_entry_count": len(tree_entries),
            "tree_entry_limit_reached": tree_entry_limit_reached,
            "candidate_skill_file_count": len(candidate_files),
            "support_file_count": len(support_files),
        },
    }


def normalize_external_skill_candidates(
    raw_items: list[dict[str, Any]],
) -> list[ExternalSkillCandidate]:
    """Normalize offline GitHub repo / skill metadata into review-only candidates."""

    candidates: list[ExternalSkillCandidate] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        expanded_items = _expand_external_skill_item(raw)
        for item in expanded_items:
            candidate = normalize_external_skill_candidate(item)
            if candidate is not None:
                candidates.append(candidate)
    return _dedupe_external_skill_candidates(candidates)


def scan_external_skill_candidates(
    raw_items: list[dict[str, Any]],
) -> ExternalSkillScanResult:
    """Scan offline external skill metadata into review-only candidate evidence."""

    candidates = normalize_external_skill_candidates(raw_items)
    risk_counts: dict[str, int] = {}
    sandbox_suitable = 0
    for candidate in candidates:
        risk_counts[candidate.safety.risk_level] = (
            risk_counts.get(candidate.safety.risk_level, 0) + 1
        )
        if candidate.safety.sandbox_suitable:
            sandbox_suitable += 1
    return ExternalSkillScanResult(
        source_items=len(raw_items),
        candidates=candidates,
        risk_counts=risk_counts,
        sandbox_suitable=sandbox_suitable,
    )


def normalize_external_skill_source_registrations(
    raw_items: list[dict[str, Any]],
) -> list[ExternalSkillSourceRegistration]:
    """Normalize offline source registry rows into review-only registrations."""

    registrations: list[ExternalSkillSourceRegistration] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        registration = normalize_external_skill_source_registration(raw)
        if registration is not None:
            registrations.append(registration)
    return _dedupe_external_skill_source_registrations(registrations)


def normalize_external_skill_source_registration(
    raw: dict[str, Any],
) -> ExternalSkillSourceRegistration | None:
    """Normalize one offline external capability source registry row."""

    source = _external_skill_source(raw)
    name = _first_text(
        raw.get("name"),
        raw.get("source_name"),
        raw.get("repo"),
        raw.get("full_name"),
        raw.get("html_url"),
        raw.get("url"),
    )
    if not name and not source.repo and not source.url:
        return None
    summary = _first_text(
        raw.get("summary"),
        raw.get("description"),
        raw.get("readme"),
        raw.get("snippet"),
    )[:700]
    safety = assess_external_skill_safety(raw)
    maintenance = assess_external_skill_maintenance(raw)
    demand_match = match_external_skill_task_demand(raw)
    candidate_count = _external_skill_candidate_count(raw)
    key = "|".join(
        [
            source.kind,
            source.repo,
            source.url,
            source.commit_sha,
            name,
            summary[:120],
        ]
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return ExternalSkillSourceRegistration(
        source_id=f"esk_src_{digest}",
        name=name or source.repo or source.url,
        summary=summary,
        source=source,
        safety=safety,
        maintenance=maintenance,
        demand_match=demand_match,
        candidate_count=candidate_count,
        tags=_external_skill_source_tags(raw, safety, maintenance, demand_match),
    )


def normalize_external_skill_candidate(raw: dict[str, Any]) -> ExternalSkillCandidate | None:
    """Normalize one external skill metadata row without fetching the network."""

    source = _external_skill_source(raw)
    name = _first_text(
        raw.get("name"),
        raw.get("skill_name"),
        raw.get("repo"),
        raw.get("full_name"),
        raw.get("html_url"),
    )
    if not name and not source.url:
        return None
    summary = _first_text(
        raw.get("summary"),
        raw.get("description"),
        raw.get("readme"),
        raw.get("snippet"),
        raw.get("skill_md"),
    )[:700]
    safety = assess_external_skill_safety(raw)
    maintenance = assess_external_skill_maintenance(raw)
    demand_match = match_external_skill_task_demand(raw)
    key = "|".join(
        [
            source.kind,
            source.repo,
            source.url,
            source.commit_sha,
            name,
            summary[:120],
        ]
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return ExternalSkillCandidate(
        candidate_id=f"esk_{digest}",
        name=name or source.repo or source.url,
        summary=summary,
        source=source,
        safety=safety,
        maintenance=maintenance,
        demand_match=demand_match,
        tags=_external_skill_tags(raw, safety, demand_match),
    )


def match_external_skill_task_demand(raw: dict[str, Any]) -> ExternalSkillDemandMatch:
    """Infer which task demand an external skill/template likely serves.

    This is deliberately offline and review-only. It gives Qi/NUO a small,
    inspectable hint about whether a candidate is mostly for coding, writing,
    review, research, or ops without promoting or installing the skill.
    """

    haystack = _external_skill_demand_text(raw)
    scores: dict[ExternalSkillDemandKind, int] = {}
    matched_keywords: dict[ExternalSkillDemandKind, list[str]] = {}
    for kind in _DEMAND_MATCH_KINDS:
        score = 0
        hits: list[str] = []
        for keyword, weight in _DEMAND_MATCH_KEYWORDS[kind]:
            if _demand_keyword_matches(haystack, keyword):
                score += weight
                hits.append(keyword)
        if score > 0:
            scores[kind] = score
            matched_keywords[kind] = hits[:12]

    if not scores:
        return ExternalSkillDemandMatch(
            primary="unknown",
            categories=["unknown"],
            confidence=0.0,
            reasons=["no_task_demand_keywords_matched"],
        )

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    primary = ranked[0][0]
    top_score = ranked[0][1]
    categories = [
        kind
        for kind, score in ranked
        if score == top_score or score >= max(2, int(top_score * 0.45))
    ][:3]
    total_score = sum(scores.values())
    confidence = round(min(0.95, max(0.2, top_score / max(total_score, top_score))), 2)
    reasons = [f"matched_{primary}_keywords"]
    if len(categories) > 1:
        reasons.append("multi_demand_candidate")

    return ExternalSkillDemandMatch(
        primary=primary,
        categories=categories,
        confidence=confidence,
        scores=scores,
        matched_keywords=matched_keywords,
        reasons=reasons,
    )


def assess_external_skill_safety(raw: dict[str, Any]) -> ExternalSkillSafetyAssessment:
    """Conservative static safety triage for external skill metadata."""

    license_id = _license_id(raw)
    license_unknown = license_id.lower() in _UNKNOWN_LICENSE_VALUES
    files = _external_skill_files(raw)
    content = "\n".join([_first_text(raw.get("readme"), raw.get("skill_md"), raw.get("snippet"))])
    content = "\n".join([content, *[file["content"] for file in files]])
    paths = [file["path"] for file in files]

    executable_paths = [
        path for path in paths if _looks_like_executable_file(path, content_by_path=files)
    ]
    truncated_paths = [
        file["path"]
        for file in files
        if file.get("content_truncated")
        or file.get("content_fetch_skipped")
        or file.get("too_large")
    ]
    network_hits = _pattern_hits(content, _NETWORK_PATTERNS)
    secret_hits = _pattern_hits(content, _SECRET_PATTERNS)
    file_write_hits = _pattern_hits(content, _FILE_WRITE_PATTERNS)

    reasons: list[str] = []
    if license_unknown:
        reasons.append("license_unknown")
    if executable_paths:
        reasons.append("contains_execution_scripts")
    if network_hits:
        reasons.append("external_network_risk")
    if secret_hits:
        reasons.append("secret_access_risk")
    if file_write_hits:
        reasons.append("file_write_risk")
    if truncated_paths:
        reasons.append("content_not_fully_inspected")

    risk_score = 0
    risk_score += 1 if license_unknown else 0
    risk_score += 1 if executable_paths else 0
    risk_score += 1 if truncated_paths else 0
    risk_score += 1 if network_hits else 0
    risk_score += 2 if secret_hits else 0
    risk_score += 2 if file_write_hits else 0
    if secret_hits and file_write_hits:
        risk_score += 1
    risk_level: ExternalSkillRiskLevel
    if risk_score >= 5:
        risk_level = "critical"
    elif risk_score >= 3:
        risk_level = "high"
    elif risk_score >= 1:
        risk_level = "medium"
    else:
        risk_level = "low"

    sandbox_suitable = risk_level in {"low", "medium"} and not secret_hits
    if not sandbox_suitable:
        reasons.append("sandbox_not_suitable_without_manual_controls")
    if not reasons:
        reasons.append("static_metadata_no_obvious_risk")

    return ExternalSkillSafetyAssessment(
        risk_level=risk_level,
        license_id=license_id,
        license_unknown=license_unknown,
        contains_execution_scripts=bool(executable_paths),
        external_network_risk=bool(network_hits),
        secret_access_risk=bool(secret_hits),
        file_write_risk=bool(file_write_hits),
        sandbox_suitable=sandbox_suitable,
        reasons=sorted(set(reasons)),
        evidence={
            "source_url": str(raw.get("url") or raw.get("html_url") or raw.get("source_url") or ""),
            "executable_paths": executable_paths[:20],
            "network_hits": network_hits[:20],
            "secret_hits": secret_hits[:20],
            "file_write_hits": file_write_hits[:20],
            "inspected_file_count": len(files),
            "truncated_paths": truncated_paths[:20],
        },
    )


def assess_external_skill_maintenance(raw: dict[str, Any]) -> ExternalSkillMaintenanceAssessment:
    """Score maintenance from offline metadata only.

    The score is a planning hint, not approval. Unknown maintenance stays
    reviewable but visible to Qi/NUO/humans as missing evidence.
    """

    archived = bool(raw.get("archived"))
    deprecated = _truthy_flag(raw.get("deprecated")) or _metadata_mentions_deprecated(raw)
    candidate_count = _external_skill_candidate_count(raw)
    last_activity = _latest_datetime(
        raw.get("pushed_at"),
        raw.get("updated_at"),
        raw.get("last_commit_at"),
        raw.get("latest_release_at"),
    )
    last_activity_days = _age_days(last_activity)
    stars = _safe_int(raw.get("stars") or raw.get("stargazers_count"))
    forks = _safe_int(raw.get("forks") or raw.get("forks_count"))
    open_issues = _safe_int(raw.get("open_issues") or raw.get("open_issues_count"))
    maintainers = raw.get("maintainers") or raw.get("owners")
    maintainer_count = len(maintainers) if isinstance(maintainers, list) else 0

    reasons: list[str] = []
    score = 0.45

    if archived or deprecated:
        score = 0.05
        reasons.append("source_archived_or_deprecated")
    else:
        if last_activity_days is None:
            reasons.append("maintenance_recency_unknown")
            score -= 0.1
        elif last_activity_days <= 180:
            reasons.append("recent_activity")
            score += 0.3
        elif last_activity_days <= 730:
            reasons.append("activity_within_two_years")
            score += 0.15
        elif last_activity_days <= 1095:
            reasons.append("activity_stale_over_two_years")
            score -= 0.05
        else:
            reasons.append("activity_stale_over_three_years")
            score -= 0.25

        if stars >= 1000:
            reasons.append("strong_usage_signal")
            score += 0.12
        elif stars >= 50:
            reasons.append("some_usage_signal")
            score += 0.06

        if forks >= 50:
            reasons.append("fork_activity_signal")
            score += 0.04
        if maintainer_count > 0:
            reasons.append("maintainers_declared")
            score += min(0.12, 0.04 * maintainer_count)
        if candidate_count > 0:
            reasons.append("candidate_entries_declared")
            score += 0.08
        if open_issues > 0 and stars > 0 and open_issues > max(50, stars):
            reasons.append("open_issue_load_high")
            score -= 0.12

    bounded_score = round(max(0.0, min(1.0, score)), 2)
    if archived or deprecated:
        status: ExternalSkillMaintenanceStatus = "deprecated"
    elif last_activity_days is None and not any(
        reason in reasons
        for reason in (
            "strong_usage_signal",
            "some_usage_signal",
            "maintainers_declared",
            "candidate_entries_declared",
        )
    ):
        status = "unknown"
    elif bounded_score >= 0.65:
        status = "maintained"
    elif bounded_score < 0.35:
        status = "stale"
    else:
        status = "unknown"

    if not reasons:
        reasons.append("maintenance_metadata_sparse")
    return ExternalSkillMaintenanceAssessment(
        score=bounded_score,
        status=status,
        reasons=sorted(set(reasons)),
        evidence={
            "archived": archived,
            "deprecated": deprecated,
            "last_activity_at": last_activity.isoformat() if last_activity is not None else None,
            "last_activity_days": last_activity_days,
            "stars": stars,
            "forks": forks,
            "open_issues": open_issues,
            "maintainer_count": maintainer_count,
            "candidate_count": candidate_count,
        },
    )


def score_external_skill_safety(safety: ExternalSkillSafetyAssessment) -> float:
    """Convert static risk triage into a normalized 0..1 planning score."""

    score = {
        "low": 1.0,
        "medium": 0.7,
        "high": 0.35,
        "critical": 0.0,
    }.get(safety.risk_level, 0.0)
    if not safety.sandbox_suitable:
        score = min(score, 0.3)
    return round(score, 2)


def score_external_skill_license(license_id: str) -> float:
    """Conservative offline license score for human review planning."""

    normalized = str(license_id or "unknown").strip().lower()
    if normalized in _UNKNOWN_LICENSE_VALUES:
        return 0.2
    if normalized in {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0"}:
        return 1.0
    if normalized in {"gpl-2.0", "gpl-3.0", "lgpl-2.1", "lgpl-3.0", "agpl-3.0"}:
        return 0.45
    if normalized in {"proprietary", "all rights reserved", "unlicensed"}:
        return 0.1
    return 0.65


def score_external_skill_task_fit(
    task_demand: ExternalSkillDemandKind,
    demand_match: ExternalSkillDemandMatch,
) -> float:
    """Score how well a source/candidate demand match fits the current task need."""

    if task_demand == "unknown" or demand_match.primary == "unknown":
        return 0.15
    if demand_match.primary == task_demand:
        return 0.85
    if task_demand in demand_match.categories:
        return 0.65
    if task_demand == "coding" and "review" in demand_match.categories:
        return 0.45
    if task_demand == "review" and "coding" in demand_match.categories:
        return 0.45
    return 0.1


class ExternalInfoScanner:
    """外部信息饱和度异步守望.

    用法:
        scanner = ExternalInfoScanner(
            library=get_library(),
            fetchers={
                "github_issue": fetch_github_issues,
                "reddit": fetch_reddit,
                "arxiv": fetch_arxiv,
            },
            llm_reviewer=my_llm_reviewer,
            user_top_task_types_lookup=lambda uid: ["coding.py", "writing.email"],
            user_telemetry_enabled=lambda uid: True,
        )
        # idle-batch 周期调
        result = await scanner.scan_for_user("u-1")
    """

    def __init__(
        self,
        library: EmergentSolutionLibrary,
        *,
        fetchers: dict[SourceKind, ExternalFetcher] | None = None,
        llm_reviewer: LLMReviewer | None = None,
        user_top_task_types_lookup: Callable[[str], list[str]] | None = None,
        user_telemetry_enabled: Callable[[str], bool] | None = None,
        default_daily_limit: int = 100,
        max_candidates_per_source: int = 5,
    ) -> None:
        self._library = library
        self._fetchers = fetchers or {}
        self._llm_reviewer = llm_reviewer
        self._user_top_task_types_lookup = user_top_task_types_lookup
        self._user_telemetry_enabled = user_telemetry_enabled
        self.default_daily_limit = default_daily_limit
        self.max_candidates_per_source = max_candidates_per_source
        self._budgets: dict[str, ScanBudget] = {}

    def _get_budget(self, user_id: str) -> ScanBudget:
        b = self._budgets.get(user_id)
        if b is None:
            b = ScanBudget(user_id=user_id, daily_limit=self.default_daily_limit)
            self._budgets[user_id] = b
        # 24h 滚动
        if (datetime.now(UTC) - b.window_start).total_seconds() > 86400:
            b.used_today = 0
            b.window_start = datetime.now(UTC)
        return b

    def _under_budget(self, user_id: str, requested: int = 1) -> bool:
        b = self._get_budget(user_id)
        return (b.used_today + requested) <= b.daily_limit

    def _consume(self, user_id: str, n: int) -> None:
        b = self._get_budget(user_id)
        b.used_today += n

    async def scan_for_user(
        self,
        user_id: str,
    ) -> ScanResult:
        """异步守望周期任务: 为单个 user 扫高频 task_types.

        永不阻塞主路径 (这个函数本身就是 idle-batch 调起的).
        """
        start = datetime.now(UTC)

        # 用户禁用 telemetry → 不扫
        if self._user_telemetry_enabled is not None and not self._user_telemetry_enabled(user_id):
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[],
                duration_sec=0.0,
            )

        if self._user_top_task_types_lookup is None:
            top_types = []
        else:
            top_types = self._user_top_task_types_lookup(user_id)[:5]

        if not top_types:
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[],
                duration_sec=0.0,
            )

        sources_queried = 0
        candidates_added = 0
        candidates_rejected = 0

        for task_type in top_types:
            for source_kind, fetcher in self._fetchers.items():
                if not self._under_budget(user_id):
                    logger.info("scan budget exhausted for user %s, stopping", user_id)
                    break
                try:
                    raw_items = await fetcher(task_type)
                except Exception:
                    logger.exception("fetcher %s failed", source_kind)
                    continue
                self._consume(user_id, 1)
                sources_queried += 1

                # 限 max_candidates_per_source
                for raw in raw_items[: self.max_candidates_per_source]:
                    relevant, summary = await self._review(task_type, raw)
                    if not relevant:
                        candidates_rejected += 1
                        continue

                    sol = EmergentSolution(
                        task_type=task_type,
                        discovered_by="external_scan",
                        source=EmergentSource(
                            kind=source_kind,
                            url=raw.get("url", ""),
                            snippet=raw.get("snippet", "")[:300],
                        ),
                        description=summary,
                        estimated_outcome_delta=float(raw.get("estimated_outcome_delta", 0.0)),
                        estimated_cost_delta=float(raw.get("estimated_cost_delta", 0.0)),
                    )
                    self._library.add(sol)
                    candidates_added += 1

        elapsed = (datetime.now(UTC) - start).total_seconds()
        return ScanResult(
            user_id=user_id,
            scanned_task_types=top_types,
            sources_queried=sources_queried,
            candidates_added=candidates_added,
            candidates_rejected=candidates_rejected,
            duration_sec=elapsed,
        )

    async def scan_for_user_anchor_then_expand(
        self,
        user_id: str,
        *,
        max_rounds: int = 3,
    ) -> AsyncIterator[ScanResult]:
        """按需扫描外部来源.

        老的 ``scan_for_user`` 会遍历用户高频任务 × 所有来源. 新接口先扫最靠前的
        一个来源, 调用方需要更多信息时再继续 expand 后续来源.

        # TODO: wire by Claude in V2.2
        """
        if self._user_telemetry_enabled is not None and not self._user_telemetry_enabled(user_id):
            return

        top_types = (
            []
            if self._user_top_task_types_lookup is None
            else self._user_top_task_types_lookup(user_id)[:5]
        )
        pairs = [
            (task_type, source_kind, fetcher)
            for task_type in top_types
            for source_kind, fetcher in self._fetchers.items()
        ]
        if not pairs:
            return

        async def anchor_fn() -> ScanResult:
            task_type, source_kind, fetcher = pairs[0]
            return await self._scan_one_source(user_id, task_type, source_kind, fetcher)

        async def expand_fn(
            _anchor: ScanResult,
            prior: list[ScanResult],
        ) -> ScanResult | None:
            idx = len(prior)
            if idx >= len(pairs):
                return None
            task_type, source_kind, fetcher = pairs[idx]
            return await self._scan_one_source(user_id, task_type, source_kind, fetcher)

        async for result in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield result

    async def _scan_one_source(
        self,
        user_id: str,
        task_type: str,
        source_kind: SourceKind,
        fetcher: ExternalFetcher,
    ) -> ScanResult:
        """扫描一个 task_type/source 组合."""
        start = datetime.now(UTC)
        if not self._under_budget(user_id):
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[task_type],
                duration_sec=0.0,
            )

        try:
            raw_items = await fetcher(task_type)
        except Exception:
            logger.exception("fetcher %s failed", source_kind)
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[task_type],
                duration_sec=(datetime.now(UTC) - start).total_seconds(),
            )

        self._consume(user_id, 1)
        candidates_added = 0
        candidates_rejected = 0
        for raw in raw_items[: self.max_candidates_per_source]:
            relevant, summary = await self._review(task_type, raw)
            if not relevant:
                candidates_rejected += 1
                continue

            sol = EmergentSolution(
                task_type=task_type,
                discovered_by="external_scan",
                source=EmergentSource(
                    kind=source_kind,
                    url=raw.get("url", ""),
                    snippet=raw.get("snippet", "")[:300],
                ),
                description=summary,
                estimated_outcome_delta=float(raw.get("estimated_outcome_delta", 0.0)),
                estimated_cost_delta=float(raw.get("estimated_cost_delta", 0.0)),
            )
            self._library.add(sol)
            candidates_added += 1

        return ScanResult(
            user_id=user_id,
            scanned_task_types=[task_type],
            sources_queried=1,
            candidates_added=candidates_added,
            candidates_rejected=candidates_rejected,
            duration_sec=(datetime.now(UTC) - start).total_seconds(),
        )

    async def _review(
        self,
        task_type: str,
        raw: dict[str, Any],
    ) -> tuple[bool, str]:
        """LLM 复审避免噪声. 没注册 reviewer → 默认接受."""
        if self._llm_reviewer is None:
            return (True, raw.get("snippet", "")[:200])
        try:
            return await self._llm_reviewer(task_type, raw)
        except Exception:
            logger.exception("llm_reviewer failed, accepting raw")
            return (True, raw.get("snippet", "")[:200])

    def get_budget_status(self, user_id: str) -> dict[str, Any]:
        b = self._get_budget(user_id)
        return {
            "user_id": user_id,
            "daily_limit": b.daily_limit,
            "used_today": b.used_today,
            "remaining": max(0, b.daily_limit - b.used_today),
            "window_start": b.window_start.isoformat(),
        }


class StrongExternalScanReviewer:
    """Opt-in LLM reviewer for external/emergent scan rows.

    It only decides whether a clue should become a review-only candidate.
    It never promotes or activates anything.
    """

    def __init__(self, router: Any, *, max_tokens: int = 500) -> None:
        self.router = router
        self.max_tokens = max(160, max_tokens)

    async def __call__(self, task_type: str, raw: dict[str, Any]) -> tuple[bool, str]:
        from kun.interface.llm.base import LLMMessage, LLMRequest, TaskProfile

        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "You are KUN's external-scan reviewer. Return strict JSON only. "
                        "Decide whether this external clue is relevant, safe, and concrete "
                        "enough to become a review-only EmergentSolution candidate. Never "
                        "approve production adoption."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "task_type": task_type,
                            "raw_item": raw,
                            "contract": {
                                "output": {
                                    "relevant": "bool",
                                    "summary": "short actionable summary",
                                    "reason": "short reason",
                                },
                                "production_action": False,
                                "promotion_allowed": False,
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            temperature=0.1,
            max_tokens=self.max_tokens,
            profile=TaskProfile(
                task_type=f"external_scan.review.{task_type}",
                risk_level="medium",
                needs_reasoning=True,
                prefer_speed=False,
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "external_scan_review",
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "relevant": {"type": "boolean"},
                            "summary": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["relevant", "summary"],
                    },
                },
            },
        )
        response = await self.router.invoke(request, purpose="judge")
        payload = _json_object_from_text(response.content)
        relevant = bool(payload.get("relevant"))
        summary = str(payload.get("summary") or raw.get("snippet") or "")[:500]
        reason = str(payload.get("reason") or "").strip()
        if reason:
            summary = f"{summary}\n[strong_review_reason] {reason}"[:700]
        return relevant, summary


def configured_external_scan_reviewer_from_env() -> LLMReviewer | None:
    """Return the opt-in strong reviewer for external scan rows."""

    if os.getenv(EXTERNAL_SCAN_STRONG_REVIEW_ENABLED_ENV, "0") != "1":
        return None
    from kun.interface.llm.router import get_router

    return StrongExternalScanReviewer(
        get_router(),
        max_tokens=_int_env(EXTERNAL_SCAN_STRONG_REVIEW_MAX_TOKENS_ENV, 500),
    )


def _parse_github_repo_ref(repo_ref: str) -> tuple[str, str]:
    text = str(repo_ref or "").strip()
    if not text:
        raise ValueError("GitHub repository reference is required")

    direct = _GITHUB_REPO_REF_RE.fullmatch(text)
    if direct is not None:
        return _validate_github_repo_parts(
            direct.group("owner"),
            direct.group("repo").removesuffix(".git"),
        )

    parsed = urlparse(text)
    if not parsed.scheme:
        raise ValueError("GitHub repository reference must be owner/name or a GitHub URL")
    if parsed.scheme != "https":
        raise ValueError("Only https GitHub repository URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("GitHub repository URL userinfo is not allowed")
    if parsed.port not in {None, 443}:
        raise ValueError("GitHub repository URL port is not allowed")
    host = (parsed.hostname or "").lower()
    if host != _GITHUB_INPUT_HOST:
        raise ValueError("Only github.com repository URLs are allowed")

    decoded_path = unquote(parsed.path or "")
    if "\\" in decoded_path or "\x00" in decoded_path:
        raise ValueError("GitHub repository path is not allowed")
    parts = [part for part in decoded_path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("GitHub repository path traversal is not allowed")
    if len(parts) < 2:
        raise ValueError("GitHub repository URL must include owner and repo")
    repo = parts[1].removesuffix(".git")
    return _validate_github_repo_parts(parts[0], repo)


def _validate_github_repo_parts(owner: str, repo: str) -> tuple[str, str]:
    if not _GITHUB_OWNER_RE.fullmatch(owner):
        raise ValueError("GitHub owner is not allowed")
    if not _GITHUB_REPO_RE.fullmatch(repo) or repo in {".", ".."}:
        raise ValueError("GitHub repository name is not allowed")
    if "/" in repo or "\\" in repo or "\x00" in repo:
        raise ValueError("GitHub repository name is not allowed")
    return owner, repo


def _github_api_url(path_and_query: str) -> str:
    if not path_and_query.startswith("/"):
        raise ValueError("GitHub API path must be absolute")
    url = f"https://api.github.com{path_and_query}"
    _assert_allowed_github_fetch_url(url)
    return url


def _github_raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    safe_path = _safe_github_tree_path(path)
    quoted_path = "/".join(quote(part, safe="") for part in safe_path.split("/"))
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(ref, safe='')}/{quoted_path}"
    _assert_allowed_github_fetch_url(url)
    return url


def _assert_allowed_github_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only https GitHub metadata URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("GitHub metadata URL userinfo is not allowed")
    if parsed.port not in {None, 443}:
        raise ValueError("GitHub metadata URL port is not allowed")
    host = (parsed.hostname or "").lower()
    if host not in _GITHUB_FETCH_HOSTS:
        raise ValueError("GitHub metadata URL host is not allowed")
    decoded_path = unquote(parsed.path or "")
    if "\\" in decoded_path or "\x00" in decoded_path:
        raise ValueError("GitHub metadata URL path is not allowed")
    if any(part in {".", ".."} for part in decoded_path.split("/") if part):
        raise ValueError("GitHub metadata URL path traversal is not allowed")


async def _github_fetch_json(
    url: str,
    *,
    fetcher: ExternalGithubMetadataFetcher,
    max_bytes: int,
    timeout_sec: float,
) -> dict[str, Any]:
    response = await _github_fetch_bytes(
        url,
        fetcher=fetcher,
        max_bytes=max_bytes,
        timeout_sec=timeout_sec,
    )
    if len(response.body) > max_bytes:
        raise ValueError("GitHub metadata response exceeded size limit")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub metadata response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GitHub metadata response must be a JSON object")
    return payload


async def _github_fetch_bytes(
    url: str,
    *,
    fetcher: ExternalGithubMetadataFetcher,
    max_bytes: int,
    timeout_sec: float,
) -> ExternalGithubFetchResponse:
    _assert_allowed_github_fetch_url(url)
    if max_bytes <= 0:
        raise ValueError("GitHub metadata max_bytes must be positive")
    response = await asyncio.wait_for(
        fetcher(url, max_bytes, timeout_sec),
        timeout=timeout_sec + 0.5,
    )
    if not isinstance(response, ExternalGithubFetchResponse):
        raise TypeError("GitHub metadata fetcher must return ExternalGithubFetchResponse")
    if response.status_code in {301, 302, 303, 307, 308}:
        raise ValueError("GitHub metadata redirects are not followed")
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"GitHub metadata fetch failed with status {response.status_code}")
    return response


async def _default_github_metadata_fetcher(
    url: str,
    max_bytes: int,
    timeout_sec: float,
) -> ExternalGithubFetchResponse:
    import httpx

    _assert_allowed_github_fetch_url(url)
    headers = {
        "Accept": "application/vnd.github+json, text/plain;q=0.9, */*;q=0.1",
        "User-Agent": "KUN-external-skill-discovery/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    chunks: list[bytes] = []
    total = 0
    async with (
        httpx.AsyncClient(follow_redirects=False, timeout=timeout_sec) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            remaining = max_bytes + 1 - total
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            total += min(len(chunk), remaining)
            if total > max_bytes:
                break
        return ExternalGithubFetchResponse(
            status_code=response.status_code,
            headers={str(key): str(value) for key, value in response.headers.items()},
            body=b"".join(chunks),
        )


def _bounded_github_timeout(timeout_sec: float) -> float:
    try:
        value = float(timeout_sec)
    except (TypeError, ValueError):
        value = _GITHUB_DEFAULT_TIMEOUT_SEC
    return max(0.5, min(30.0, value))


def _safe_github_ref(ref: str) -> str:
    value = str(ref or "").strip()
    if not value or len(value) > 255 or value.startswith(("/", ".")):
        raise ValueError("GitHub default branch is not allowed")
    if "\\" in value or "\x00" in value:
        raise ValueError("GitHub default branch is not allowed")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("GitHub default branch traversal is not allowed")
    return value


def _safe_github_commit_sha(sha: str) -> str:
    value = str(sha or "").strip()
    if not _GITHUB_COMMIT_SHA_RE.fullmatch(value):
        raise ValueError("GitHub commit SHA is required before fetching tree or raw files")
    return value.lower()


def _safe_github_tree_path(path: Any) -> str:
    value = str(path or "").strip()
    if not value or len(value) > 4096:
        raise ValueError("GitHub tree path is not allowed")
    decoded = unquote(value)
    if decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
        raise ValueError("GitHub tree path is not allowed")
    parts = decoded.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("GitHub tree path traversal is not allowed")
    return "/".join(parts)


def _github_tree_entries(raw_tree: Any, *, max_entries: int) -> list[dict[str, Any]]:
    if not isinstance(raw_tree, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in raw_tree:
        if len(entries) >= max(0, max_entries):
            break
        if not isinstance(raw, dict):
            continue
        try:
            path = _safe_github_tree_path(raw.get("path"))
        except ValueError:
            continue
        entries.append(
            {
                "path": path,
                "type": _first_text(raw.get("type")),
                "size": _safe_int(raw.get("size")),
                "sha": _first_text(raw.get("sha")),
            }
        )
    return entries


def _github_candidate_skill_entries(
    entries: list[dict[str, Any]],
    *,
    repo_name: str,
    limit: int,
) -> list[dict[str, Any]]:
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in entries:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "")
        score = _github_candidate_skill_path_score(path, repo_name=repo_name)
        if score is not None:
            scored.append((score, path, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[: max(0, limit)]]


def _github_candidate_skill_path_score(path: str, *, repo_name: str) -> int | None:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if name == "skill.md":
        return 0
    if name in {"skills.md", "skill.mdx"}:
        return 1
    if (
        "/skills/" in f"/{lowered}"
        and _has_github_text_suffix(lowered)
        and name not in _GITHUB_NON_SKILL_DOC_NAMES
    ):
        return 2
    if "skill" in name and _has_github_text_suffix(lowered):
        return 3
    if (
        repo_name.lower() in {"skills", "codex-skills", "agent-skills"}
        and _has_github_text_suffix(lowered)
        and name not in _GITHUB_NON_SKILL_DOC_NAMES
    ):
        return 4
    return None


def _github_support_file_entries(
    entries: list[dict[str, Any]],
    *,
    candidate_entries: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    candidate_paths = {str(entry.get("path") or "") for entry in candidate_entries}
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in entries:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "")
        if path in candidate_paths:
            continue
        lowered = path.lower()
        name = lowered.rsplit("/", 1)[-1]
        score: int | None = None
        if _looks_like_executable_file(path, content_by_path=[]):
            score = 0
        elif name in {"package.json", "pyproject.toml", "requirements.txt"}:
            score = 1
        if score is not None:
            scored.append((score, path, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[: max(0, limit)]]


async def _github_fetch_tree_file_fragment(
    *,
    owner: str,
    repo: str,
    ref: str,
    entry: dict[str, Any],
    fetcher: ExternalGithubMetadataFetcher,
    timeout_sec: float,
    max_file_bytes: int,
) -> dict[str, Any]:
    path = _safe_github_tree_path(entry.get("path"))
    size = _safe_int(entry.get("size"))
    payload: dict[str, Any] = {
        "path": path,
        "content": "",
        "size": size,
        "sha": _first_text(entry.get("sha")),
    }
    if size > max_file_bytes:
        payload.update(
            {
                "content_fetch_skipped": "file_too_large",
                "content_truncated": True,
                "too_large": True,
            }
        )
        return payload

    raw_url = _github_raw_url(owner, repo, ref, path)
    response = await _github_fetch_bytes(
        raw_url,
        fetcher=fetcher,
        max_bytes=max_file_bytes,
        timeout_sec=timeout_sec,
    )
    body = response.body[:max_file_bytes]
    payload["content"] = body.decode("utf-8", errors="replace")
    if len(response.body) > max_file_bytes:
        payload["content_truncated"] = True
    return payload


def _github_skill_payload_from_file(
    *,
    owner: str,
    repo: str,
    ref: str,
    repo_description: str,
    file: dict[str, Any],
) -> dict[str, Any]:
    path = _safe_github_tree_path(file.get("path"))
    content = _first_text(file.get("content"))
    skill_url = (
        f"https://github.com/{owner}/{repo}/blob/{quote(ref, safe='')}/"
        f"{'/'.join(quote(part, safe='') for part in path.split('/'))}"
    )
    return {
        "name": _github_skill_name_from_file(path, content),
        "description": _github_skill_summary_from_file(content) or repo_description,
        "url": skill_url,
        "commit_sha": ref,
        "files": [file],
    }


def _github_skill_name_from_file(path: str, content: str) -> str:
    for line in content.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:120]
    parts = path.split("/")
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0]
    if filename.lower() in {"skill.md", "skill.mdx"} and len(parts) >= 2:
        stem = parts[-2]
    return _humanize_slug(stem)


def _github_skill_summary_from_file(content: str) -> str:
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        lines.append(stripped)
        if len(" ".join(lines)) >= 300:
            break
    return " ".join(lines)[:500]


def _humanize_slug(value: str) -> str:
    words = [part for part in re.split(r"[-_\s.]+", value.strip()) if part]
    if not words:
        return value or "External skill"
    return " ".join(word[:1].upper() + word[1:] for word in words)[:120]


def _has_github_text_suffix(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in _GITHUB_TEXT_FILE_SUFFIXES)


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _expand_external_skill_item(raw: dict[str, Any]) -> list[dict[str, Any]]:
    skills = raw.get("skills")
    if not isinstance(skills, list):
        return [raw]
    expanded: list[dict[str, Any]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        files = []
        if isinstance(raw.get("files"), list):
            files.extend(raw["files"])
        if isinstance(skill.get("files"), list):
            files.extend(skill["files"])
        expanded.append(
            {
                **raw,
                **skill,
                "repo": raw.get("repo") or raw.get("full_name") or skill.get("repo"),
                "url": skill.get("url") or raw.get("url") or raw.get("html_url"),
                "files": files,
            }
        )
    return expanded or [raw]


def _dedupe_external_skill_candidates(
    candidates: list[ExternalSkillCandidate],
) -> list[ExternalSkillCandidate]:
    by_id: dict[str, ExternalSkillCandidate] = {}
    for candidate in candidates:
        by_id[candidate.candidate_id] = candidate
    return list(by_id.values())


def _dedupe_external_skill_source_registrations(
    registrations: list[ExternalSkillSourceRegistration],
) -> list[ExternalSkillSourceRegistration]:
    by_id: dict[str, ExternalSkillSourceRegistration] = {}
    for registration in registrations:
        by_id[registration.source_id] = registration
    return list(by_id.values())


def _external_skill_source(raw: dict[str, Any]) -> ExternalSkillSource:
    repo = _first_text(raw.get("repo"), raw.get("full_name"), raw.get("repository"))
    url = _first_text(raw.get("url"), raw.get("html_url"), raw.get("source_url"))
    return ExternalSkillSource(
        kind=_first_text(raw.get("source_kind"), raw.get("kind"), "github_repo") or "github_repo",
        repo=repo,
        url=url,
        commit_sha=_first_text(raw.get("commit_sha"), raw.get("sha")),
        fetched_at=_parse_datetime(raw.get("fetched_at") or raw.get("pushed_at")),
    )


def _external_skill_tags(
    raw: dict[str, Any],
    safety: ExternalSkillSafetyAssessment,
    demand_match: ExternalSkillDemandMatch,
) -> list[str]:
    tags = {
        "external_skill",
        "review_only",
        f"risk:{safety.risk_level}",
        f"demand:{demand_match.primary}",
    }
    for category in demand_match.categories:
        tags.add(f"demand:{category}")
    topics = raw.get("topics") or raw.get("tags") or []
    if isinstance(topics, list):
        tags.update(str(item).strip() for item in topics if str(item).strip())
    if safety.license_unknown:
        tags.add("license_unknown")
    if safety.sandbox_suitable:
        tags.add("sandbox_candidate")
    else:
        tags.add("manual_security_review")
    return sorted(tags)


def _external_skill_source_tags(
    raw: dict[str, Any],
    safety: ExternalSkillSafetyAssessment,
    maintenance: ExternalSkillMaintenanceAssessment,
    demand_match: ExternalSkillDemandMatch,
) -> list[str]:
    tags = set(_external_skill_tags(raw, safety, demand_match))
    tags.add("external_skill_source")
    tags.add(f"maintenance:{maintenance.status}")
    if maintenance.status in {"stale", "deprecated"}:
        tags.add("maintenance_review")
    return sorted(tags)


def _external_skill_demand_text(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "name",
        "skill_name",
        "summary",
        "description",
        "readme",
        "snippet",
        "skill_md",
        "task_type",
    ):
        value = raw.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("task_types", "use_cases", "topics", "tags"):
        value = raw.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, str):
            parts.append(value)
    for file in _external_skill_files(raw):
        parts.append(file["path"])
        parts.append(file["content"][:4000])
    return "\n".join(parts).lower()


def _demand_keyword_matches(haystack: str, keyword: str) -> bool:
    needle = keyword.lower().strip()
    if not needle:
        return False
    if any(ord(char) > 127 for char in needle):
        return needle in haystack
    if not re.fullmatch(r"[a-z0-9_ -]+", needle):
        return needle in haystack
    pattern = r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])"
    return re.search(pattern, haystack) is not None


def _external_skill_files(raw: dict[str, Any]) -> list[dict[str, Any]]:
    files = raw.get("files") or raw.get("tree") or []
    out: list[dict[str, Any]] = []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                out.append({"path": item, "content": ""})
            elif isinstance(item, dict):
                path = _first_text(item.get("path"), item.get("name"), item.get("filename"))
                content = _first_text(item.get("content"), item.get("text"), item.get("body"))
                if path or content:
                    file_payload: dict[str, Any] = {"path": path, "content": content}
                    for key in (
                        "size",
                        "sha",
                        "content_truncated",
                        "content_fetch_skipped",
                        "too_large",
                    ):
                        if key in item:
                            file_payload[key] = item[key]
                    out.append(file_payload)
    for key, path in {
        "readme": "README.md",
        "skill_md": "SKILL.md",
        "package_json": "package.json",
    }.items():
        if isinstance(raw.get(key), str):
            out.append({"path": path, "content": str(raw[key])})
    return out


def _external_skill_candidate_count(raw: dict[str, Any]) -> int:
    raw_count = raw.get("candidate_count") or raw.get("skill_count")
    count = _safe_int(raw_count)
    skills = raw.get("skills")
    if isinstance(skills, list):
        count = max(count, len([skill for skill in skills if isinstance(skill, dict)]))
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        count = max(count, _safe_int(metadata.get("candidate_skill_file_count")))
    return max(0, count)


def _license_id(raw: dict[str, Any]) -> str:
    license_value = raw.get("license") or raw.get("license_id") or raw.get("spdx_id")
    if isinstance(license_value, dict):
        license_value = (
            license_value.get("spdx_id") or license_value.get("key") or license_value.get("name")
        )
    return str(license_value or "unknown").strip() or "unknown"


def _looks_like_executable_file(
    path: str,
    *,
    content_by_path: list[dict[str, Any]],
) -> bool:
    lowered = path.strip().lower()
    name = lowered.rsplit("/", 1)[-1]
    if name in _EXECUTABLE_NAMES:
        return True
    if any(lowered.endswith(suffix) for suffix in _EXECUTABLE_FILE_PATTERNS):
        return True
    for file in content_by_path:
        if file["path"] == path and file["content"].startswith("#!"):
            return True
    return False


def _pattern_hits(content: str, patterns: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        if re.search(pattern, content, flags=re.IGNORECASE):
            hits.append(pattern)
    return hits


def _risk_sort_rank(risk_level: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(risk_level, 9)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_datetime(*values: Any) -> datetime | None:
    dates = [_ensure_aware_datetime(item) for item in (_parse_datetime(value) for value in values)]
    valid_dates = [date for date in dates if date is not None]
    if not valid_dates:
        return None
    return max(valid_dates)


def _ensure_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_days(value: datetime | None) -> int | None:
    aware = _ensure_aware_datetime(value)
    if aware is None:
        return None
    return max(0, int((datetime.now(UTC) - aware).total_seconds() // 86400))


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "deprecated"}
    return bool(value)


def _metadata_mentions_deprecated(raw: dict[str, Any]) -> bool:
    values: list[str] = []
    for key in ("topics", "tags"):
        raw_value = raw.get(key)
        if isinstance(raw_value, list):
            values.extend(str(item) for item in raw_value)
        elif isinstance(raw_value, str):
            values.append(raw_value)
    return any("deprecated" in value.lower() for value in values)


def _json_object_from_text(text: str) -> dict[str, Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            raw = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = [
    "EXTERNAL_SCAN_STRONG_REVIEW_ENABLED_ENV",
    "ExternalFetcher",
    "ExternalGithubFetchResponse",
    "ExternalGithubMetadataFetcher",
    "ExternalInfoScanner",
    "ExternalSkillCandidate",
    "ExternalSkillDemandMatch",
    "ExternalSkillMaintenanceAssessment",
    "ExternalSkillSafetyAssessment",
    "ExternalSkillScanResult",
    "ExternalSkillSource",
    "ExternalSkillSourceRegistration",
    "LLMReviewer",
    "ScanBudget",
    "ScanResult",
    "StrongExternalScanReviewer",
    "assess_external_skill_maintenance",
    "assess_external_skill_safety",
    "configured_external_scan_reviewer_from_env",
    "fetch_github_repo_external_skill_metadata",
    "match_external_skill_task_demand",
    "normalize_external_skill_candidate",
    "normalize_external_skill_candidates",
    "normalize_external_skill_source_registration",
    "normalize_external_skill_source_registrations",
    "scan_external_skill_candidates",
    "score_external_skill_license",
    "score_external_skill_safety",
    "score_external_skill_task_fit",
]
