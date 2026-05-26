# Control Plane — Domain Isolation Roadmap

`kun/control_plane/` is ~22k LOC and mixes two layers:

1. **Core V6 runtime** (~12k LOC) — mission/work-item state machine,
   execution contracts, ledgering, supervisor protocol. M1–M3 scope.
2. **Domain-specific runners + templates** (~10k LOC) — game production,
   advertising missions (RainFlow), A/B regression infra (Frontier50),
   game design research, external sample comparison. Not in any ADR's
   acceptance criteria.

## Current state (after 鲲V1.1-dev audit pass)

- The 6 game-production template modules (~4.8k LOC of pure data
  returning `{path: content}` dicts) have been moved into
  `kun/control_plane/templates/` to stop crowding the core runtime.
- The core runtime no longer reverse-imports `RAINFLOW_AD_PRODUCTION_MODE`
  from the rainflow domain module (a local copy lives in `runtime.py`
  with a "keep in sync" note).
- Files still in `kun/control_plane/` that are domain-specific (not
  required for the core agent OS):

| File | LOC | Domain | Used by |
|------|----:|--------|---------|
| `game_production.py` | 7907 | game production | CLI runner `kun-game-production-runner`, supervisor `external-supervisor-gpt5.5` |
| `rainflow_ad_mission.py` | 666 | advertising mission isolation | CLI rainflow commands, runtime helpers `_is_rainflow_*` (still in runtime.py — needs further extraction) |
| `frontier50_external.py` | 492 | A/B regression testing | CLI flag `--frontier50-live-workdir` only |
| `game_design_research.py` | 1243 | game design research | `feature_activation_audit` (audit-only) |
| `external_sample_comparison.py` | 995 | sample comparison | `feature_activation_audit` + CLI |
| `mission_director.py` | 972 | LLM-driven runner | optional via `KUN_MISSION_DIRECTOR_MODE` |
| `app_development.py` | 1045 | app scaffolding | autonomous app runner (huohutu pipeline) |

## Planned reorg (not in this PR)

Move each domain to `kun/domains/<name>/` so the core runtime stays
focused. Order of risk (low → high):

1. `frontier50_external.py` → `kun/domains/frontier50/`
2. `game_design_research.py` → `kun/domains/game_design_research/`
3. `external_sample_comparison.py` → `kun/domains/external_sample_comparison/`
4. `rainflow_ad_mission.py` → `kun/domains/rainflow/` (also extract
   `_is_rainflow_*` helpers from `runtime.py` into a runtime plugin
   hook — biggest piece of work)
5. `game_production.py` + `kun/control_plane/templates/` → `kun/domains/game_production/`
6. `app_development.py` → `kun/domains/app_development/`
7. `mission_director.py` → `kun/domains/mission_director/`

Each move touches:
- `kun/cli.py` runner registration
- `kun/control_plane/__init__.py` re-exports
- `kun/control_plane/feature_activation_audit.py` imports
- the corresponding tests under `tests/unit/test_control_plane_*_v6.py`
  (rename → `tests/domains/<name>/`)

Each step should land as its own PR with all consumers updated atomically.
