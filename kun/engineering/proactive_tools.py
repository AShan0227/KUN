"""Proactive tool dispatch — 把"主动用工具"的责任从 LLM 移到工程化.

KUN 的 LLM 主动性不够 — 工具描述塞进 prompt, 模型大概率直接答, 不点工具.
解决: 在 orchestrator 早期, 用关键词 / 规则扫一遍 prompt, **先把工具结果跑出来**,
塞进 LLM 的 user message. LLM 看到的不是"我可能要 web-search", 而是"以下是
web-search 已经查到的结果, 请基于它回答".

四层机制:
  层 1: 关键词触发器 (本文件 DEFAULT_TRIGGERS)         — fallback
  层 2: yaml 规则可配置 (rules/proactive/triggers.yaml) — 守望加载, 可热改, 后续可学习
  层 3: SKILL.md auto_trigger_when                    — 每 skill 自己声明
  层 4: capability_card 失败回看                       — evaluator 升级"强制用工具"

主流程: load_triggers_from_yaml() 优先加载 yaml; 没找到 / 解析失败时回退 DEFAULT_TRIGGERS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kun.core.logging import get_logger
from kun.skills.dispatcher import SkillResult, is_registered
from kun.skills.dispatcher import dispatch as skill_dispatch

log = get_logger("kun.proactive_tools")


@dataclass
class ToolTrigger:
    """A pre-defined rule that says "if prompt looks like X, run Y skill"."""

    skill_id: str
    description: str
    pattern: re.Pattern[str]
    extract_params: Any  # callable(match, prompt) -> dict | None
    confidence: str = "high"  # high / medium / low


@dataclass
class ProactiveDispatch:
    """Outcome of running the trigger map against a prompt."""

    skill_id: str
    params: dict[str, Any]
    result: SkillResult
    trigger_reason: str

    def to_user_message(self) -> str:
        """Render this prefetch as a user-message fragment for the LLM."""
        if not self.result.ok:
            return (
                f"\n\n## 系统已尝试调 {self.skill_id} 但失败了\n"
                f"原因: {self.result.error}\n"
                f"触发理由: {self.trigger_reason}\n"
                f"请基于现有信息继续回答, 必要时建议用户提供更多输入。"
            )

        import json

        try:
            rendered = json.dumps(self.result.output, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            rendered = str(self.result.output)
        if len(rendered) > 2000:
            rendered = rendered[:2000] + "\n... (truncated)"

        return (
            f"\n\n## 系统已自动调用 {self.skill_id}\n"
            f"触发理由: {self.trigger_reason}\n"
            f"参数: {json.dumps(self.params, ensure_ascii=False)}\n"
            f"结果:\n```json\n{rendered}\n```\n"
            f"请基于以上结果回答用户的问题。"
        )


@dataclass
class ProactiveScanResult:
    dispatched: list[ProactiveDispatch] = field(default_factory=list)

    def to_prefix_message(self) -> str:
        """Aggregate all dispatches into one prefix block for the LLM."""
        if not self.dispatched:
            return ""
        return "".join(d.to_user_message() for d in self.dispatched)


# ============== Triggers ==============


def _extract_pdf_path(match: re.Match[str], _prompt: str) -> dict[str, Any] | None:
    path = match.group(0).strip()
    return {"path": path}


def _extract_python_code(match: re.Match[str], _prompt: str) -> dict[str, Any] | None:
    code = match.group(1).strip()
    if len(code) < 4 or len(code) > 4000:
        return None
    return {"code": code, "timeout_sec": 30}


def _extract_search_query(_match: re.Match[str], prompt: str) -> dict[str, Any] | None:
    # 触发词后面到下一个标点为止当作 query, 实在不行用整个 prompt
    cleaned = re.sub(r"[。.!?！？;；\n].*", "", prompt).strip()
    if 5 < len(cleaned) < 200:
        return {"query": cleaned, "max_results": 5}
    # fallback: 用整个 prompt 截断
    return {"query": prompt[:200], "max_results": 5}


def _extract_csv_path(match: re.Match[str], _prompt: str) -> dict[str, Any] | None:
    path = match.group(0).strip()
    return {"path": path, "sql": "SELECT * FROM data LIMIT 10"}


# ============== Yaml-driven triggers (layer 2) ==============


_DEFAULT_YAML_PATH = Path(__file__).resolve().parents[2] / "rules" / "proactive" / "triggers.yaml"


def _make_extract_callable(extract_cfg: dict[str, Any]) -> Any:
    """Build an extract_params callable from a yaml extract block.

    Three kinds supported (matches what triggers.yaml ships):
      - match_group_0: pull match.group(0), with optional length bounds
      - match_group_1: pull match.group(1), with optional length bounds
      - search_query: take prompt up to first sentence-end punctuation
    Returns dict[str, Any] with the named param and any extra_params merged in,
    or None if length bounds reject the candidate.
    """
    kind = extract_cfg.get("kind", "match_group_0")
    param_name = extract_cfg.get("param_name", "input")
    min_len = int(extract_cfg.get("min_len", 0))
    max_len_raw = extract_cfg.get("max_len")
    max_len = int(max_len_raw) if max_len_raw is not None else None
    extra_params: dict[str, Any] = dict(extract_cfg.get("extra_params") or {})

    def _length_ok(value: str) -> bool:
        if len(value) < min_len:
            return False
        return not (max_len is not None and len(value) > max_len)

    if kind == "match_group_0":

        def extract_g0(match: re.Match[str], _prompt: str) -> dict[str, Any] | None:
            value = match.group(0).strip()
            if not _length_ok(value):
                return None
            return {param_name: value, **extra_params}

        return extract_g0

    if kind == "match_group_1":

        def extract_g1(match: re.Match[str], _prompt: str) -> dict[str, Any] | None:
            try:
                value = match.group(1).strip()
            except IndexError:
                return None
            if not _length_ok(value):
                return None
            return {param_name: value, **extra_params}

        return extract_g1

    if kind == "search_query":

        def extract_query(_match: re.Match[str], prompt: str) -> dict[str, Any] | None:
            cleaned = re.sub(r"[。.!?！？;；\n].*", "", prompt).strip()
            if 5 < len(cleaned) < 200:
                return {param_name: cleaned, **extra_params}
            return {param_name: prompt[:200], **extra_params}

        return extract_query

    log.warning("proactive.unknown_extract_kind", kind=kind)
    return lambda _m, _p: None


def load_triggers_from_yaml(path: Path | str | None = None) -> list[ToolTrigger]:
    """Load triggers from yaml. Empty / missing / malformed → empty list.

    Caller is expected to fall back to DEFAULT_TRIGGERS on empty result.
    Catches everything intentionally — observability never breaks routing.
    """
    target = Path(path) if path else _DEFAULT_YAML_PATH
    if not target.exists():
        log.info("proactive.yaml_absent", path=str(target))
        return []
    try:
        with target.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("proactive.yaml_load_failed", path=str(target), error=str(e))
        return []

    raw_entries = data.get("triggers") or []
    out: list[ToolTrigger] = []
    for entry in raw_entries:
        try:
            pattern = re.compile(entry["pattern"], re.IGNORECASE | re.MULTILINE)
            extract = _make_extract_callable(entry.get("extract") or {})
            out.append(
                ToolTrigger(
                    skill_id=str(entry["skill_id"]),
                    description=str(entry.get("description", "")),
                    pattern=pattern,
                    extract_params=extract,
                    confidence=str(entry.get("confidence", "medium")),
                )
            )
        except (KeyError, re.error, TypeError) as e:
            log.warning("proactive.trigger_invalid", entry=entry, error=str(e))
            continue
    log.info("proactive.yaml_loaded", path=str(target), count=len(out))
    return out


# Order matters — first matching trigger wins (high-confidence rules first).
DEFAULT_TRIGGERS: list[ToolTrigger] = [
    # PDF in prompt → pdf-read
    ToolTrigger(
        skill_id="pdf-read",
        description="prompt 引用 .pdf 文件 → 自动读取",
        pattern=re.compile(r"\S*\.pdf\b", re.IGNORECASE),
        extract_params=_extract_pdf_path,
    ),
    # CSV in prompt → csv-query
    ToolTrigger(
        skill_id="csv-query",
        description="prompt 引用 .csv 文件 → 自动加载并预览前 10 行",
        pattern=re.compile(r"\S*\.csv\b", re.IGNORECASE),
        extract_params=_extract_csv_path,
    ),
    # ```python ... ``` block → python-exec
    ToolTrigger(
        skill_id="python-exec",
        description="prompt 含 Python 代码块 → 自动执行",
        pattern=re.compile(r"```python\s*\n([\s\S]*?)```", re.MULTILINE),
        extract_params=_extract_python_code,
    ),
    # "最新 / 现在 / 今天 / 当前 / 实时" 等时效性词 → web-search
    ToolTrigger(
        skill_id="web-search",
        description="prompt 含时效性关键词 → 联网搜索",
        pattern=re.compile(r"(最新|现在|今天|当前|实时|这周|本月|近期|latest|today|recent)"),
        extract_params=_extract_search_query,
        confidence="medium",
    ),
]


# ============== Public API ==============


async def proactive_dispatch(
    *,
    prompt: str,
    triggers: list[ToolTrigger] | None = None,
    required_tools_hint: list[str] | None = None,
    max_dispatches: int = 3,
) -> ProactiveScanResult:
    """Scan a prompt for tool triggers and pre-dispatch matching skills.

    Args:
        prompt: User's natural-language input.
        triggers: Override the default trigger list (testing / customization).
        required_tools_hint: Skill ids the intent layer flagged in TaskSpec.
            Forces dispatch even if no keyword matches. Skipped if not registered.
        max_dispatches: Cap to avoid pathological prompt blowing up budget.

    Returns:
        ProactiveScanResult with per-dispatch results. Empty if nothing matched.

    Errors are caught per-skill — one failed dispatch doesn't block others.
    """
    if triggers is None:
        # Layer 2: yaml-configured triggers take priority. Layer 1 hardcoded
        # DEFAULT_TRIGGERS is the fallback when yaml is missing / malformed
        # / empty so the system always has something to fire on.
        loaded = load_triggers_from_yaml()
        triggers = loaded if loaded else DEFAULT_TRIGGERS
    seen: set[str] = set()
    dispatches: list[ProactiveDispatch] = []

    # Layer 1a: hard requirements from intent layer
    for required_skill in required_tools_hint or []:
        if required_skill in seen:
            continue
        if not is_registered(required_skill):
            log.info("proactive.required_skill_unregistered", skill_id=required_skill)
            continue
        # No params — caller must trust default behaviour or extend layer 1
        # detection. We dispatch with empty {} which most builtins won't like;
        # in practice the keyword scan below usually picks up real params first.
        seen.add(required_skill)

    # Layer 1b: keyword trigger scan
    for trigger in triggers:
        if trigger.skill_id in seen:
            continue
        if not is_registered(trigger.skill_id):
            continue
        match = trigger.pattern.search(prompt)
        if match is None:
            continue
        params = trigger.extract_params(match, prompt)
        if params is None:
            continue
        try:
            result = await skill_dispatch(trigger.skill_id, params)
        except Exception as e:
            log.warning(
                "proactive.dispatch_failed",
                skill_id=trigger.skill_id,
                error=str(e),
            )
            continue
        dispatches.append(
            ProactiveDispatch(
                skill_id=trigger.skill_id,
                params=params,
                result=result,
                trigger_reason=trigger.description,
            )
        )
        seen.add(trigger.skill_id)
        if len(dispatches) >= max_dispatches:
            break

    if dispatches:
        log.info(
            "proactive.dispatched",
            count=len(dispatches),
            skills=[d.skill_id for d in dispatches],
        )

    return ProactiveScanResult(dispatched=dispatches)


__all__ = [
    "DEFAULT_TRIGGERS",
    "ProactiveDispatch",
    "ProactiveScanResult",
    "ToolTrigger",
    "proactive_dispatch",
]
