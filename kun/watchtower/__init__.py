"""Watchtower (守望子系统) — 系统隐藏大脑.

ADR-004: YAML 声明规则 + Python handler. Prometheus alerting rules 风格.
ADR-018 §16.8: GuardRule 单一规则引擎承载 guard/validation/ci/anomaly 四类.
"""

from kun.watchtower.engine import RuleEngine, load_rules
from kun.watchtower.handlers import register_handler
from kun.watchtower.rules import GuardRule, RuleAction, RuleTrigger

__all__ = [
    "GuardRule",
    "RuleAction",
    "RuleEngine",
    "RuleTrigger",
    "load_rules",
    "register_handler",
]
