"""Shared API runtime wiring.

HTTP and WebSocket routes must use the same app-level Orchestrator so the
Watchtower rules loaded during startup are actually used on real requests.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from kun.engineering.orchestrator import Orchestrator
from kun.watchtower.engine import RuleEngine


class _AppWithState(Protocol):
    state: Any


def install_runtime(app: _AppWithState, *, rule_engine: RuleEngine) -> Orchestrator:
    """Install shared runtime services onto ``app.state``."""
    orchestrator = Orchestrator(rule_engine=rule_engine)
    app.state.rule_engine = rule_engine
    app.state.orchestrator = orchestrator
    return orchestrator


def get_orchestrator(app: _AppWithState) -> Orchestrator:
    """Return the shared Orchestrator installed by the FastAPI lifespan."""
    orchestrator = getattr(app.state, "orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("API runtime has not been initialized")
    return cast(Orchestrator, orchestrator)
