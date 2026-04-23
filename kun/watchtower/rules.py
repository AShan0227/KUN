"""Rule definitions — Pydantic models that mirror the YAML schema.

Example (rules/guard/cost_runaway.yaml):

    id: cost_runaway
    kind: guard
    trigger:
      event_type: task.step.completed
      when: "event.accumulated_cost_usd > task.estimated_cost_usd * 1.2"
    severity: medium
    actions:
      - handler: pause_task
      - handler: notify_user
        params: { template: cost_exceeded }
    cooldown_sec: 300
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RuleKind = Literal["guard", "validation", "ci", "anomaly", "cache"]
Severity = Literal["info", "low", "medium", "high", "critical"]


class RuleTrigger(BaseModel):
    """When to evaluate the rule."""

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(description="Event type to subscribe to, or '*' for all")
    when: str = Field(
        default="True",
        description="Python expression; variables: event, task, runtime, env",
    )


class RuleAction(BaseModel):
    """An action to take on trigger."""

    model_config = ConfigDict(extra="forbid")

    handler: str = Field(description="Registered handler name")
    params: dict[str, object] = Field(default_factory=dict)


class GuardRule(BaseModel):
    """A single rule definition (matches YAML 1:1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: RuleKind
    description: str = ""
    trigger: RuleTrigger
    severity: Severity = "medium"
    actions: list[RuleAction] = Field(default_factory=list)
    cooldown_sec: int = Field(default=0, description="Anti-flap window in seconds")
    enabled: bool = True
    version: int = 1
    tags: list[str] = Field(default_factory=list)
