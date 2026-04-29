"""Project constitution CRUD API.

Standalone router for C15. Main app wiring can happen in Claude's M4 pass.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from kun.datamodel.project_constitution import (
    ConstitutionLoader,
    ConstitutionRule,
    ProjectConstitution,
    get_constitution_store,
)

router = APIRouter(prefix="/api/projects", tags=["project-constitution"])


class ConstitutionUpsertRequest(BaseModel):
    rules: list[ConstitutionRule] = Field(default_factory=list)
    updated_by: str


class ConstitutionListResponse(BaseModel):
    tenant_id: str
    items: list[ProjectConstitution]


@router.put(
    "/{project_id}/constitution",
    response_model=ProjectConstitution,
    status_code=status.HTTP_200_OK,
)
async def upsert_constitution(
    project_id: str,
    body: ConstitutionUpsertRequest,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> ProjectConstitution:
    item = ProjectConstitution(
        project_id=project_id,
        tenant_id=x_tenant_id,
        rules=body.rules,
        updated_by=body.updated_by,
    )
    return await get_constitution_store().put(item)


@router.get("/{project_id}/constitution", response_model=ProjectConstitution)
async def get_constitution(
    project_id: str,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> ProjectConstitution:
    item = await get_constitution_store().get(tenant_id=x_tenant_id, project_id=project_id)
    if item is None:
        raise HTTPException(status_code=404, detail="constitution not found")
    return item


@router.delete("/{project_id}/constitution", status_code=status.HTTP_204_NO_CONTENT)
async def delete_constitution(
    project_id: str,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> None:
    deleted = await get_constitution_store().delete(tenant_id=x_tenant_id, project_id=project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="constitution not found")


@router.get("", response_model=ConstitutionListResponse)
async def list_constitutions(
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> ConstitutionListResponse:
    items = await get_constitution_store().list(tenant_id=x_tenant_id)
    return ConstitutionListResponse(tenant_id=x_tenant_id, items=items)


@router.get("/{project_id}/constitution/prompt")
async def render_constitution_prompt(
    project_id: str,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> dict[str, str]:
    item = await get_constitution_store().get(tenant_id=x_tenant_id, project_id=project_id)
    if item is None:
        raise HTTPException(status_code=404, detail="constitution not found")
    return {"prompt": ConstitutionLoader().render_to_system_prompt(item)}


__all__ = [
    "ConstitutionListResponse",
    "ConstitutionUpsertRequest",
    "router",
]
