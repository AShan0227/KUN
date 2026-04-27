"""KUN-Lab HTTP API (Batch9 C31)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant
from kun.lab import EnsembleConfig
from kun.lab.ensemble_executor import is_lab_enabled

router = APIRouter(prefix="/api/lab", tags=["lab"])


class LabExperimentSummary(BaseModel):
    experiment_id: str
    task_type: str
    prompt_hash: str = ""
    winning_path_idx: int
    total_cost_usd: float
    total_latency_sec: float
    created_at: str


class LabExperimentDetail(BaseModel):
    experiment_id: str
    task_type: str
    prompt_hash: str = ""
    ensemble_result: dict[str, Any]
    created_at: str


class LabRunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    task_type: str = "kun_lab.api"
    config: EnsembleConfig = Field(default_factory=EnsembleConfig)
    emit_events: bool = True


class LabPromoteRequest(BaseModel):
    min_total: int = Field(default=10, ge=1)
    min_winrate: float = Field(default=0.6, ge=0.0, le=1.0)


def _experiment_summary(exp: Any) -> LabExperimentSummary:
    result = exp.ensemble_result
    return LabExperimentSummary(
        experiment_id=exp.experiment_id,
        task_type=exp.task_type,
        prompt_hash=exp.prompt_hash,
        winning_path_idx=result.winning_path_idx,
        total_cost_usd=result.total_cost_usd,
        total_latency_sec=result.total_latency_sec,
        created_at=exp.created_at.isoformat(),
    )


def _experiment_detail(exp: Any) -> LabExperimentDetail:
    return LabExperimentDetail(
        experiment_id=exp.experiment_id,
        task_type=exp.task_type,
        prompt_hash=exp.prompt_hash,
        ensemble_result=exp.ensemble_result.model_dump(mode="json"),
        created_at=exp.created_at.isoformat(),
    )


def _get_experiment_or_404(experiment_id: str) -> Any:
    from kun.lab import get_experiment_log

    for exp in get_experiment_log().list_all():
        if exp.experiment_id == experiment_id:
            return exp
    raise HTTPException(status_code=404, detail="lab experiment not found")


def _require_lab_enabled() -> None:
    if not is_lab_enabled():
        raise HTTPException(status_code=403, detail="KUN_LAB_MODE=1 required")


def _require_admin_scope() -> None:
    scopes = set(current_tenant().scopes)
    if not ({"admin", "lab:admin"} & scopes):
        raise HTTPException(status_code=403, detail="admin scope required")


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]


@router.get("/experiments", response_model=list[LabExperimentSummary])
async def list_lab_experiments(
    task_type: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> list[LabExperimentSummary]:
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    experiments = log.by_task_type(task_type) if task_type else log.list_all()
    experiments = list(reversed(experiments))[:limit]
    return [_experiment_summary(exp) for exp in experiments]


@router.get("/experiments/{experiment_id}", response_model=LabExperimentDetail)
async def get_lab_experiment(experiment_id: str) -> LabExperimentDetail:
    return _experiment_detail(_get_experiment_or_404(experiment_id))


@router.get("/recipes")
async def list_lab_recipes() -> list[dict[str, Any]]:
    from kun.lab import get_recipe_registry

    return [_recipe_to_dict(entry) for entry in get_recipe_registry().all()]


@router.get("/recipes/{task_type}")
async def get_lab_recipes_by_task_type(task_type: str) -> list[dict[str, Any]]:
    from kun.lab import get_recipe_registry

    return [_recipe_to_dict(entry) for entry in get_recipe_registry().by_task_type(task_type)]


@router.post("/run", response_model=LabExperimentDetail)
async def run_lab_experiment(req: LabRunRequest) -> LabExperimentDetail:
    _require_lab_enabled()
    from kun.lab import EnsembleExecutor, LabEventEmitter, get_experiment_log, make_default_adapter

    cfg = req.config.model_copy(deep=True)
    cfg.metadata = {**cfg.metadata, "prompt": req.prompt}
    adapter = make_default_adapter(task_type=req.task_type)
    emitter = LabEventEmitter(task_type_default=req.task_type) if req.emit_events else None
    executor = EnsembleExecutor(
        adapter,
        event_emitter=emitter.on_experiment_completed if emitter else None,
    )
    result = await executor.run(req.prompt, config=cfg, task_type=req.task_type)
    exp = get_experiment_log().record(
        task_type=req.task_type,
        ensemble_result=result,
        prompt_hash=_hash_prompt(req.prompt),
    )
    return _experiment_detail(exp)


@router.post("/promote")
async def promote_lab_recipes(req: LabPromoteRequest) -> dict[str, Any]:
    _require_admin_scope()
    from kun.lab import LabEventEmitter, RecipePromoter, get_experiment_log

    emitter = LabEventEmitter()
    promoter = RecipePromoter(
        get_experiment_log(),
        min_total=req.min_total,
        min_winrate=req.min_winrate,
        event_emitter=emitter.on_recipe_promoted,
    )
    promotions = await promoter.promote_eligible()
    return {
        "promoted": len(promotions),
        "promotions": [p.model_dump(mode="json") for p in promotions],
    }


@router.websocket("/ws/experiment/{experiment_id}/stream")
async def stream_lab_experiment(ws: WebSocket, experiment_id: str) -> None:
    await ws.accept()
    try:
        exp = _get_experiment_or_404(experiment_id)
    except HTTPException:
        await ws.send_json({"event": "error", "detail": "lab experiment not found"})
        await ws.close(code=1008)
        return

    for path in exp.ensemble_result.path_results:
        await ws.send_json(
            {
                "event": "path.completed",
                "experiment_id": experiment_id,
                "path_idx": path.path_idx,
                "score": path.score,
                "cost_usd": path.cost_usd,
                "error": path.error,
            }
        )
    await ws.send_json(
        {
            "event": "experiment.completed",
            "experiment_id": experiment_id,
            "winning_path_idx": exp.ensemble_result.winning_path_idx,
        }
    )
    await ws.close()


def _recipe_to_dict(entry: Any) -> dict[str, Any]:
    data = asdict(entry)
    data["last_updated"] = entry.last_updated.isoformat()
    return data


__all__ = ["router"]
