"""Rule Engine (ADR-004).

Features:
  - Load rules from YAML (rules/<kind>/*.yaml)
  - Evaluate `trigger.when` expression (safe AST eval via simpleeval)
  - Execute registered handlers
  - Cooldown tracking (anti-flap)
  - Metrics: rule eval latency, intervention rate
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

import yaml
from simpleeval import EvalWithCompoundTypes

from kun.core.logging import get_logger
from kun.core.metrics import watchtower_intervention_rate, watchtower_rule_latency_seconds
from kun.watchtower.handlers import get_handler
from kun.watchtower.rules import GuardRule, RuleKind

log = get_logger("kun.watchtower.engine")


def load_rules(
    root: str | Path = "rules",
    kinds: Iterable[RuleKind] | None = None,
) -> list[GuardRule]:
    """Load all rules from rules/<kind>/*.yaml files."""
    root = Path(root)
    if not root.exists():
        return []

    rules: list[GuardRule] = []
    kinds_list = list(kinds) if kinds else None

    for yaml_path in sorted(root.rglob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if data is None:
                continue
            # Infer kind from parent directory if not in YAML
            if "kind" not in data:
                data["kind"] = yaml_path.parent.name
            rule = GuardRule.model_validate(data)
            if kinds_list and rule.kind not in kinds_list:
                continue
            rules.append(rule)
        except Exception as e:
            log.warning("rules.load_failed", path=str(yaml_path), error=str(e))
    log.info("rules.loaded", count=len(rules), path=str(root))
    return rules


class RuleEngine:
    """Stateless-ish rule evaluator with a small cooldown table."""

    # Safe builtins exposed to expressions
    _SAFE_FUNCS: ClassVar[dict[str, Any]] = {
        "min": min,
        "max": max,
        "abs": abs,
        "len": len,
        "round": round,
        "sum": sum,
        "any": any,
        "all": all,
    }

    def __init__(self, rules: list[GuardRule] | None = None) -> None:
        self.rules: list[GuardRule] = rules or []
        self._cooldown: dict[tuple[str, str], float] = {}  # (rule_id, subject_key) → last_fired
        self._evaluator = EvalWithCompoundTypes(functions=self._SAFE_FUNCS)

    def add_rule(self, rule: GuardRule) -> None:
        self.rules.append(rule)

    def rules_for(self, event_type: str) -> list[GuardRule]:
        return [
            r
            for r in self.rules
            if r.enabled and (r.trigger.event_type == "*" or r.trigger.event_type == event_type)
        ]

    def _is_in_cooldown(self, rule: GuardRule, subject_key: str, now: float) -> bool:
        if rule.cooldown_sec <= 0:
            return False
        last = self._cooldown.get((rule.id, subject_key), 0.0)
        return (now - last) < rule.cooldown_sec

    def _mark_fired(self, rule: GuardRule, subject_key: str, now: float) -> None:
        self._cooldown[(rule.id, subject_key)] = now

    def _evaluate_when(self, rule: GuardRule, namespace: dict[str, Any]) -> bool:
        """Evaluate the 'when' expression safely."""
        expr = rule.trigger.when or "True"
        try:
            self._evaluator.names = namespace
            return bool(self._evaluator.eval(expr))
        except Exception as e:
            log.warning(
                "rule.eval_failed",
                rule_id=rule.id,
                expr=expr,
                error=str(e),
            )
            return False

    async def evaluate(
        self,
        event_type: str,
        *,
        namespace: dict[str, Any],
    ) -> list[str]:
        """Evaluate all matching rules; return list of rule_ids that fired."""
        fired: list[str] = []
        now = time.time()
        tenant_id = namespace.get("tenant_id") or namespace.get("event", {}).get(
            "tenant_id", "u-sylvan"
        )

        for rule in self.rules_for(event_type):
            start = time.perf_counter()
            subject_key = str(
                namespace.get("task_ref") or namespace.get("event", {}).get("task_ref") or "global"
            )

            if self._is_in_cooldown(rule, subject_key, now):
                continue

            ok = self._evaluate_when(rule, namespace)
            watchtower_rule_latency_seconds.labels(rule_id=rule.id).observe(
                time.perf_counter() - start
            )

            if not ok:
                continue

            self._mark_fired(rule, subject_key, now)
            fired.append(rule.id)
            watchtower_intervention_rate.labels(
                severity=rule.severity, rule_id=rule.id, tenant_id=tenant_id
            ).inc()

            # Execute actions
            ctx: dict[str, Any] = {
                "rule_id": rule.id,
                "kind": rule.kind,
                "severity": rule.severity,
                "event_type": event_type,
                "tenant_id": tenant_id,
                **{
                    k: v
                    for k, v in namespace.items()
                    if isinstance(v, str | int | float | bool | dict | list)
                },
            }

            for action in rule.actions:
                handler = get_handler(action.handler)
                if handler is None:
                    log.warning("rule.unknown_handler", rule_id=rule.id, handler=action.handler)
                    continue
                try:
                    await handler(ctx, action.params)
                except Exception as e:
                    log.exception(
                        "rule.handler_failed",
                        rule_id=rule.id,
                        handler=action.handler,
                        error=str(e),
                    )

        return fired
