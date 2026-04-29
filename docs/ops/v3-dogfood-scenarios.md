# KUN V3 Ops Dogfood Scenarios

This checklist is the production-facing smoke layer for V3. It is deliberately
honest: a scenario can be `limited` and still worth running, but `blocked`
means it should not be used as launch evidence yet.

## Run The Smoke Report

```bash
uv run python scripts/dogfood_v3_ops_smoke.py --report-path .kun-v3-ops-dogfood.json
```

For release candidates, make blocked scenarios fail the gate:

```bash
uv run python scripts/dogfood_v3_ops_smoke.py --fail-on-blocked
```

The same data is exposed at:

```text
/nuo/health/dogfood-scenarios
```

## Scenario Set

- `mission_long_horizon_resume`: verifies long-running Mission resume, scheduler,
  reaper, budget, checkpoint, and frontend visibility.
- `safe_world_action_review`: verifies low-risk WorldGateway action preview,
  approval, execution record, and artifact path.
- `memory_strategy_reuse_loop`: verifies task memory and meta-decision reuse
  influence later strategy selection.
- `release_ops_smoke`: verifies production release prerequisites: readiness,
  secrets hygiene, backup/restore drill, CI/release checklist, and monitoring.

## Evidence Rule

Each scenario needs concrete evidence before it can be marked complete:

- an API endpoint or report path;
- a smoke command;
- explicit blockers when status is `blocked`;
- ready criteria that a human operator can verify.
