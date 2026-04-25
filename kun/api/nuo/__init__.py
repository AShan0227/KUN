"""傩 (NUO) — 电脑管家式运维门面 (ADR-012 schema-isolated).

保留为独立 API 命名空间, 未来可抽出为独立产品.
"""

from fastapi import APIRouter

from kun.api.nuo.action_panel import router as action_router
from kun.api.nuo.benchmark_panel import router as benchmark_router
from kun.api.nuo.budget_panel import router as budget_router
from kun.api.nuo.capability_panel import router as capability_router
from kun.api.nuo.health_panel import router as health_router

router = APIRouter()
router.include_router(health_router, prefix="/health")
router.include_router(budget_router, prefix="/budget")
router.include_router(action_router, prefix="/actions")
router.include_router(capability_router, prefix="/capability")
router.include_router(benchmark_router, prefix="/benchmark")
