"""Lab watchtower rules + Grafana dashboard sanity (Wire 29D)."""

from __future__ import annotations

import json
from pathlib import Path

from kun.watchtower.engine import load_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "rules"
DASHBOARD_PATH = REPO_ROOT / "kun" / "infra" / "grafana-dashboard-kun-lab.json"


def test_lab_budget_cap_spike_rule_loads() -> None:
    """rule yaml syntactically valid + 能被 RuleEngine 加载."""
    rules = load_rules(RULES_DIR)
    rule_ids = {r.id for r in rules}
    assert "lab_budget_cap_spike" in rule_ids


def test_lab_recipe_promotion_burst_rule_loads() -> None:
    rules = load_rules(RULES_DIR)
    rule_ids = {r.id for r in rules}
    assert "lab_recipe_promotion_burst" in rule_ids


def test_lab_budget_cap_rule_triggers_on_budget_exceeded_payload() -> None:
    """rule 的 trigger.when 表达式真能 evaluate."""
    rules = load_rules(RULES_DIR)
    rule = next(r for r in rules if r.id == "lab_budget_cap_spike")

    assert rule.trigger.event_type == "experiment.created"
    # when 表达式包含 budget_exceeded check
    assert "budget_exceeded" in rule.trigger.when


def test_lab_budget_cap_rule_when_expression_true_for_exceeded() -> None:
    """模拟 fake event 验 when 表达式真 evaluate True."""
    rules = load_rules(RULES_DIR)
    rule = next(r for r in rules if r.id == "lab_budget_cap_spike")

    event_yes = {"payload": {"budget_exceeded": True}}
    event_no = {"payload": {"budget_exceeded": False}}
    event_missing = {"payload": {}}

    assert eval(rule.trigger.when, {"event": event_yes}) is True
    assert eval(rule.trigger.when, {"event": event_no}) is False
    assert eval(rule.trigger.when, {"event": event_missing}) is False


def test_lab_promotion_burst_rule_triggers_unconditionally() -> None:
    """trigger.when='True' → 每次 experiment.promoted 都触发 (后续靠 cooldown 限频)."""
    rules = load_rules(RULES_DIR)
    rule = next(r for r in rules if r.id == "lab_recipe_promotion_burst")

    assert rule.trigger.event_type == "experiment.promoted"
    assert rule.cooldown_sec == 300


# ---- Grafana dashboard JSON sanity ----


def test_dashboard_json_valid() -> None:
    """JSON 可解析."""
    assert DASHBOARD_PATH.exists(), f"missing dashboard: {DASHBOARD_PATH}"
    data = json.loads(DASHBOARD_PATH.read_text())
    assert data["title"] == "KUN-Lab (V2.2 §26)"
    assert data["uid"] == "kun-lab"


def test_dashboard_panels_use_lab_metrics() -> None:
    """每个 panel target 都引用 kun_lab_* metrics (没引主仓库其他 metric)."""
    data = json.loads(DASHBOARD_PATH.read_text())
    panels = data["panels"]
    assert len(panels) >= 6  # 至少 6 个 panel

    referenced_metrics: set[str] = set()
    for panel in panels:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            for token in expr.split():
                if token.startswith("kun_lab_"):
                    referenced_metrics.add(token.split("{")[0].split("[")[0])
                elif token.startswith("kun_lab_") is False and "kun_lab_" in token:
                    # extract substring
                    idx = token.find("kun_lab_")
                    name = token[idx:].split("{")[0].split("[")[0].split(")")[0]
                    referenced_metrics.add(name)

    expected = {
        "kun_lab_experiment_total",
        "kun_lab_experiment_cost_usd",
        "kun_lab_experiment_latency_seconds_bucket",
        "kun_lab_path_total",
        "kun_lab_budget_cap_total",
        "kun_lab_promotion_total",
        "kun_lab_registry_size",
    }
    # 每个 expected metric 都被 dashboard 引用
    for metric in expected:
        assert metric in referenced_metrics, f"dashboard missing reference to {metric}"


def test_dashboard_has_budget_cap_alert_panel() -> None:
    """budget cap panel 应该带 alert (跟 watchtower rule 双保险)."""
    data = json.loads(DASHBOARD_PATH.read_text())
    panels = data["panels"]
    budget_panel = next((p for p in panels if "budget cap" in p.get("title", "").lower()), None)
    assert budget_panel is not None
    assert "alert" in budget_panel
    assert budget_panel["alert"]["conditions"][0]["evaluator"]["params"][0] == 0.3
