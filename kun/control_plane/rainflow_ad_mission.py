"""RainFlow information-flow ad mission scaffolding for the V6 Control Plane.

This turns the RainFlow ad-video brief into a concrete isolated mission package
with workspace, sandbox, resource locks, output directory, and stage-aware work
items.  The mission starts with Phase 1 mixed-edit quality repair and keeps AI
video generation explicitly gated behind that acceptance milestone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kun.control_plane.file_store import FileControlPlaneStore
from kun.control_plane.v6 import ExecutionContract, Mission, TaskPlan, WorkingContext, WorkItem

RAINFLOW_AD_PRODUCTION_MODE = "rainflow_information_flow_ad_v1"
RAINFLOW_DEFAULT_PORTS: tuple[int, int] = (3401, 3402)


class RainFlowAdMission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: Mission
    task_plan: TaskPlan
    execution_contract: ExecutionContract
    working_context: WorkingContext
    work_items: list[WorkItem]


class RainFlowMissionPackageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    mission_root: str
    workspace_path: str
    output_dir: str
    sandbox_root: str
    ports: dict[str, int]


def build_rainflow_ad_mission(
    *,
    root_dir: str | Path,
    mission_id: str = "msn-rainflow-ad-video",
    owner: str = "kun",
    task_plan_version: str = "rainflow-phase1-mixed-edit-v1",
    objective: str = (
        "Make RainFlow produce excellent information-flow ad videos with stronger hooks, "
        "clear structure, conversion logic, creator/spoken-sales handling, guidance, and "
        "smooth mixed-edit pacing before AI video generation upgrades."
    ),
    preferred_ports: Sequence[int] = RAINFLOW_DEFAULT_PORTS,
) -> RainFlowAdMission:
    """Build an isolated RainFlow mission package with concrete execution boundaries."""

    root = Path(root_dir).expanduser().resolve()
    mission_root = root / mission_id
    workspace_dir = mission_root / "workspace"
    output_dir = mission_root / "outputs"
    sandbox_dir = mission_root / "sandbox"
    return _build_rainflow_ad_mission_with_paths(
        mission_root=mission_root,
        workspace_dir=workspace_dir,
        output_dir=output_dir,
        sandbox_dir=sandbox_dir,
        mission_id=mission_id,
        owner=owner,
        task_plan_version=task_plan_version,
        objective=objective,
        preferred_ports=preferred_ports,
        root_dir_for_ports=root,
    )


def build_rainflow_ad_mission_from_record(
    record: RainFlowMissionPackageRecord,
    *,
    owner: str = "kun",
    task_plan_version: str = "rainflow-phase1-mixed-edit-v1",
    objective: str = (
        "Make RainFlow produce excellent information-flow ad videos with stronger hooks, "
        "clear structure, conversion logic, creator/spoken-sales handling, guidance, and "
        "smooth mixed-edit pacing before AI video generation upgrades."
    ),
) -> RainFlowAdMission:
    """Rehydrate a RainFlow mission package from persisted package/store metadata."""

    mission_root = Path(record.mission_root).expanduser().resolve()
    workspace_dir = Path(record.workspace_path).expanduser().resolve()
    output_dir = Path(record.output_dir).expanduser().resolve()
    sandbox_dir = Path(record.sandbox_root).expanduser().resolve()
    preferred_ports = (
        record.ports.get("app", RAINFLOW_DEFAULT_PORTS[0]),
        record.ports.get("preview", record.ports.get("app", RAINFLOW_DEFAULT_PORTS[0]) + 1),
    )
    return _build_rainflow_ad_mission_with_paths(
        mission_root=mission_root,
        workspace_dir=workspace_dir,
        output_dir=output_dir,
        sandbox_dir=sandbox_dir,
        mission_id=record.mission_id,
        owner=owner,
        task_plan_version=task_plan_version,
        objective=objective,
        preferred_ports=preferred_ports,
        root_dir_for_ports=mission_root.parent,
    )


def _build_rainflow_ad_mission_with_paths(
    *,
    mission_root: Path,
    workspace_dir: Path,
    output_dir: Path,
    sandbox_dir: Path,
    mission_id: str,
    owner: str,
    task_plan_version: str,
    objective: str,
    preferred_ports: Sequence[int],
    root_dir_for_ports: Path,
) -> RainFlowAdMission:
    for path in (mission_root, workspace_dir, output_dir, sandbox_dir):
        path.mkdir(parents=True, exist_ok=True)

    assigned_ports = _allocate_ports(
        preferred_ports,
        count=2,
        reserved_ports=_reserved_ports_from_root(
            root_dir_for_ports,
            current_mission_id=mission_id,
        ),
    )
    app_port, preview_port = assigned_ports
    workspace_ref = f"workspace://{workspace_dir}"
    sandbox_ref = f"sandbox://workspace-snapshot/{mission_id}"
    resource_locks = [
        f"mission:{mission_id}",
        f"workspace:{workspace_dir}",
        f"output_dir:{output_dir}",
    ]
    work_items = _build_initial_work_items(
        mission_id=mission_id,
        plan_version=task_plan_version,
        workspace_ref=workspace_ref,
        sandbox_ref=sandbox_ref,
        resource_locks=resource_locks,
    )
    mission = Mission(
        mission_id=mission_id,
        owner=owner,
        objective=objective,
        task_type="product_development",
        priority=96,
        risk_level="high",
        status="contracted",
        current_plan_version=task_plan_version,
    )
    task_plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version=task_plan_version,
        objective=objective,
        known_facts=[
            "RainFlow already has product-plan ideas for information-flow ad video quality.",
            "Phase 1 must fix mixed-edit quality before AI video generation integration.",
            "Acceptance requires real demo comparison, tests, rollback evidence, and clean retest/replay.",
        ],
        assumptions=[
            "Work stays on the isolated RainFlow development line until explicitly promoted.",
            "Creator/spoken-sales material exists but may require better screening, assembly, and guidance logic.",
        ],
        acceptance_criteria=[
            "Phase 1 mixed-edit output shows a stronger opening hook, smoother pacing, and clearer ad structure than the previous baseline.",
            "Creator/spoken-sales segments are usable and integrated rather than awkward inserts.",
            "CTA and guidance are explicit, conversion-oriented, and supported by demo evidence.",
            "Material selection, review/screening, acquisition, and mixed-edit assembly leave auditable artifacts and tests.",
            "AI video generation stage 1 remains blocked until Phase 1 passes with comparison evidence and rollback proof.",
        ],
        constraints=[
            "Do not touch RainFlow mainline; work only in the isolated workspace and branch.",
            "Do not declare success from reports, fixtures, or diagnosis without demos, tests, and acceptance evidence.",
            "Keep stage 2 and stage 3 AI generation work gated behind a passed Phase 1 mixed-edit baseline.",
            "If KUN blocks the mission, harden KUN in its own scoped work items with regression tests.",
        ],
        risk_register=[
            "Mechanical re-edits may improve metrics without improving ad feel.",
            "AI generation stages can hide unresolved pacing and structure problems if enabled too early.",
            "Regression risk exists if isolated assets, ports, or output directories are not explicitly locked.",
        ],
        evidence_plan=[
            "Store demo video comparisons and review manifests in the isolated output directory.",
            "Record mission-director review artifacts for product-quality rejection or continuation.",
            "Capture rollback-ready workspace snapshots before write phases.",
        ],
        decomposition=[
            "Audit which RainFlow ad-video capabilities already exist in code versus only in docs.",
            "Phase 1: repair mixed-edit quality, hook strength, structure, CTA, creator/spoken-sales handling, pacing, and assembly.",
            "Phase 1 acceptance: compare against baseline, retest, review, and block closure until evidence is fresh.",
            "Stage 1 AI video generation: generate transition clips for mixed edits only after Phase 1 passes.",
            "Stage 2 AI video generation: regenerate original visuals while preserving the same meaning.",
            "Stage 3 AI video generation: produce stronger ad creative and blend it into information-flow ads.",
        ],
        worker_plan=[
            "mission-director supervises quality, acceptance, and plan changes.",
            "kun runtime audits implementation gaps and performs the isolated mixed-edit work.",
            "qi and nuo handle replay/governance and recovery when evidence or environment is suspect.",
        ],
        merge_plan=[
            "Merge only fresh demo, test, and review evidence that maps to the current stage gate.",
            "Keep each AI stage on its own acceptance and rollback boundary.",
        ],
        test_plan=[
            "Run isolated mixed-edit regression tests and asset-selection checks.",
            "Run demo comparison and review gates before moving between stages.",
            "Retest after each rollback or recovery path, not just after the happy path.",
        ],
        rollback_plan=[
            "Restore the previous isolated workspace snapshot before any failed write phase.",
            "Keep each AI stage behind an explicit rollback gate so Stage 2 or 3 can be disabled without losing Phase 1.",
        ],
        human_confirmation_points=[
            "Reject any output that lacks hook, conversion logic, smoothness, creator/spoken-sales handling, or ad-grade polish.",
            "Approve Stage 1 AI generation only after a clear Phase 1 baseline win is demonstrated.",
        ],
        approval_status="approved",
    )
    execution_contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan.version,
        allowed_actions=[
            "read RainFlow local source and docs",
            "write isolated RainFlow workspace",
            "run isolated tests and comparison builds",
            "write demo artifacts into isolated outputs",
        ],
        forbidden_actions=[
            "write RainFlow mainline workspace",
            "advance AI generation stages before Phase 1 acceptance",
            "close the mission from reports without demo evidence",
        ],
        evidence_policy={
            "required": [
                "artifact_manifest",
                "gate_evaluation",
                "demo_comparison",
                "rollback_evidence",
            ]
        },
        delivery_contract={
            "production_mode": RAINFLOW_AD_PRODUCTION_MODE,
            "workspace_path": str(workspace_dir),
            "project_path": str(workspace_dir),
            "output_dir": str(output_dir),
            "sandbox_root": str(sandbox_dir),
            "sandbox_mode": "workspace_snapshot",
            "workspace_ref": workspace_ref,
            "sandbox_ref": sandbox_ref,
            "resource_locks": list(resource_locks),
            "ports": {"app": app_port, "preview": preview_port},
            "phase_order": [
                "phase1_mixed_edit",
                "stage1_transition",
                "stage2_regeneration",
                "stage3_advanced_creative",
            ],
            "stage_gate_policy": {
                "phase1_required_before_ai_generation": True,
                "stage1_goal": "transition clips for mixed edits",
                "stage2_goal": "replace visuals while preserving meaning",
                "stage3_goal": "blend stronger advanced ad creative into information-flow ads",
            },
        },
        risk_policy={
            "quality_precedes_speed_cost": True,
            "require_demo_comparison_before_close": True,
        },
        rollback_policy={
            "required_for": [
                "phase1_mixed_edit_write",
                "stage1_transition_generation",
                "stage2_visual_regeneration",
                "stage3_advanced_creative",
            ]
        },
    )
    working_context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan.version,
        audience="rainflow-ad-video",
        scope="isolated-rainflow-ad-mission",
        summary="Supervise RainFlow ad-video quality work on an isolated line with stage-gated AI upgrades.",
        critical_facts=task_plan.known_facts,
        acceptance_criteria=task_plan.acceptance_criteria,
        constraints=task_plan.constraints,
        open_questions=[],
        risk_flags=["premature-ai-stage", "mixed-edit-quality-regression", "mainline-safety"],
    )
    package = RainFlowAdMission(
        mission=mission,
        task_plan=task_plan,
        execution_contract=execution_contract,
        working_context=working_context,
        work_items=work_items,
    )
    (mission_root / "mission-package.json").write_text(
        json.dumps(
            {
                "mission_id": mission_id,
                "ports": contract_ports(contract=execution_contract),
                "workspace_path": str(workspace_dir),
                "output_dir": str(output_dir),
                "sandbox_root": str(sandbox_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return package


def _build_initial_work_items(
    *,
    mission_id: str,
    plan_version: str,
    workspace_ref: str,
    sandbox_ref: str,
    resource_locks: Sequence[str],
) -> list[WorkItem]:
    specs = [
        (
            "01-mission-director-audit",
            "review",
            "mission-director",
            100,
            [],
            "Audit implemented RainFlow capabilities versus the product brief and block wishful completion claims.",
        ),
        (
            "02-gap-audit",
            "research",
            "kun",
            95,
            [f"work-{mission_id}-01-mission-director-audit"],
            "Produce a code-versus-doc gap audit for RainFlow ad-video quality, mixed-edit flow, creator/spoken-sales handling, and acceptance evidence.",
        ),
        (
            "03-phase1-mixed-edit-repair",
            "execution",
            "kun",
            94,
            [f"work-{mission_id}-02-gap-audit"],
            "Implement Phase 1 mixed-edit quality upgrades for hook, structure, pacing, CTA, guidance, material selection, review, and assembly.",
        ),
        (
            "04-phase1-retest-and-demo-compare",
            "test",
            "kun",
            92,
            [f"work-{mission_id}-03-phase1-mixed-edit-repair"],
            "Run isolated tests and baseline-vs-current demo comparison for RainFlow ad-video quality.",
        ),
        (
            "05-phase1-acceptance-review",
            "review",
            "mission-director",
            100,
            [f"work-{mission_id}-04-phase1-retest-and-demo-compare"],
            "Block Stage 1 AI generation unless Phase 1 demo evidence clearly improves ad quality.",
        ),
        (
            "06-stage1-transition-generation",
            "execution",
            "kun",
            90,
            [f"work-{mission_id}-05-phase1-acceptance-review"],
            "Generate Stage 1 transition clips for mixed edits only after Phase 1 acceptance passes.",
        ),
        (
            "07-stage1-retest-and-gate",
            "test",
            "kun",
            89,
            [f"work-{mission_id}-06-stage1-transition-generation"],
            "Retest Stage 1 transitions, prove mixed-edit meaning is preserved, and record rollback evidence.",
        ),
        (
            "08-stage2-visual-regeneration",
            "execution",
            "kun",
            88,
            [f"work-{mission_id}-07-stage1-retest-and-gate"],
            "Regenerate or replace original visuals while preserving the same ad meaning after Stage 1 clears.",
        ),
        (
            "09-stage2-retest-and-gate",
            "test",
            "kun",
            87,
            [f"work-{mission_id}-08-stage2-visual-regeneration"],
            "Retest Stage 2 visual replacements for semantic fidelity, ad quality, and rollback readiness.",
        ),
        (
            "10-stage3-advanced-creative-generation",
            "execution",
            "kun",
            86,
            [f"work-{mission_id}-09-stage2-retest-and-gate"],
            "Create stronger Stage 3 advanced ad creative and blend it into the information-flow ad mix.",
        ),
        (
            "11-stage3-retest-and-gate",
            "test",
            "kun",
            85,
            [f"work-{mission_id}-10-stage3-advanced-creative-generation"],
            "Retest Stage 3 creative blending for ad polish, conversion logic, and rollback safety.",
        ),
        (
            "12-final-delivery",
            "merge",
            "kun",
            84,
            [f"work-{mission_id}-11-stage3-retest-and-gate"],
            "Deliver only after Phase 1 and AI Stages 1-3 all pass with fresh demo, gate, and rollback evidence.",
        ),
    ]
    items: list[WorkItem] = []
    for suffix, item_type, owner, priority, dependencies, expected_output in specs:
        items.append(
            WorkItem(
                work_item_id=f"work-{mission_id}-{suffix}",
                mission_id=mission_id,
                task_plan_version=plan_version,
                type=item_type,  # type: ignore[arg-type]
                owner=owner,
                priority=priority,
                dependencies=dependencies,
                resource_locks=list(resource_locks),
                workspace_ref=workspace_ref,
                sandbox_ref=sandbox_ref,
                expected_output=expected_output,
                phase=suffix.removeprefix("0").split("-", 1)[1] if "-" in suffix else suffix,
            )
        )
    return items


def contract_ports(*, contract: ExecutionContract) -> dict[str, int]:
    ports = contract.delivery_contract.get("ports", {})
    return {
        "app": int(ports["app"]),
        "preview": int(ports["preview"]),
    }


def discover_rainflow_mission_packages(
    *,
    root_dir: str | Path,
    recovery_store_paths: Sequence[str | Path] = (),
) -> list[RainFlowMissionPackageRecord]:
    root = Path(root_dir).expanduser().resolve()
    records: dict[str, RainFlowMissionPackageRecord] = {}
    if root.exists():
        for manifest in sorted(root.glob("*/mission-package.json")):
            record = _load_manifest_record(manifest)
            if record is not None:
                records[record.mission_id] = record
    for record in _recover_rainflow_mission_packages_from_stores(
        root_dir=root,
        recovery_store_paths=recovery_store_paths,
    ):
        records.setdefault(record.mission_id, record)
    return [records[key] for key in sorted(records)]


def _load_manifest_record(manifest: Path) -> RainFlowMissionPackageRecord | None:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mission_id = payload.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id:
        return None
    mission_root = manifest.parent.resolve()
    workspace_path = payload.get("workspace_path")
    output_dir = payload.get("output_dir")
    sandbox_root = payload.get("sandbox_root")
    ports_payload = payload.get("ports", {})
    return RainFlowMissionPackageRecord(
        mission_id=mission_id,
        mission_root=str(mission_root),
        workspace_path=str(workspace_path or mission_root / "workspace"),
        output_dir=str(output_dir or mission_root / "outputs"),
        sandbox_root=str(sandbox_root or mission_root / "sandbox"),
        ports=_normalize_port_payload(ports_payload),
    )


def _recover_rainflow_mission_packages_from_stores(
    *,
    root_dir: Path,
    recovery_store_paths: Sequence[str | Path],
) -> list[RainFlowMissionPackageRecord]:
    recovered: dict[str, RainFlowMissionPackageRecord] = {}
    for path in _unique_existing_store_paths(recovery_store_paths):
        try:
            store = FileControlPlaneStore(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for mission in store.list_missions():
            contract = store.get_execution_contract(mission.execution_contract_ref or "")
            if not _looks_like_rainflow_store_mission(mission=mission, contract=contract):
                continue
            delivery_contract = contract.delivery_contract if contract is not None else {}
            workspace_path = _first_non_empty_path(
                delivery_contract.get("workspace_path"),
                delivery_contract.get("project_path"),
            )
            if workspace_path is None:
                continue
            mission_root = (root_dir / mission.mission_id).resolve()
            output_dir = _first_non_empty_path(delivery_contract.get("output_dir")) or str(
                mission_root / "outputs"
            )
            sandbox_root = _first_non_empty_path(delivery_contract.get("sandbox_root")) or str(
                mission_root / "sandbox"
            )
            recovered[mission.mission_id] = RainFlowMissionPackageRecord(
                mission_id=mission.mission_id,
                mission_root=str(mission_root),
                workspace_path=workspace_path,
                output_dir=output_dir,
                sandbox_root=sandbox_root,
                ports=_ports_from_delivery_contract(delivery_contract),
            )
    return [recovered[key] for key in sorted(recovered)]


def _unique_existing_store_paths(paths: Sequence[str | Path]) -> list[Path]:
    existing: list[Path] = []
    seen: set[Path] = set()
    for candidate in paths:
        path = Path(candidate).expanduser().resolve()
        if path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        existing.append(path)
    return existing


def _looks_like_rainflow_store_mission(
    *,
    mission: Mission,
    contract: ExecutionContract | None,
) -> bool:
    payloads: list[object] = [mission.model_dump(mode="json")]
    if contract is not None:
        payloads.append(contract.delivery_contract)
    text = "\n".join(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads
    )
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "rainflow",
            "adflow",
            "information-flow ad",
            "phase1 mixed-edit",
            "phase1_must_pass_before_ai_video",
        )
    )


def _first_non_empty_path(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return str(Path(value).expanduser().resolve())
    return None


def _ports_from_delivery_contract(delivery_contract: object) -> dict[str, int]:
    if not isinstance(delivery_contract, dict):
        return {}
    explicit = _normalize_port_payload(delivery_contract.get("ports", {}))
    if explicit:
        return explicit
    preferred_port = delivery_contract.get("preferred_port")
    if isinstance(preferred_port, int) and 1 <= preferred_port <= 65535:
        preview_port = preferred_port + 1 if preferred_port < 65535 else preferred_port
        return {"app": preferred_port, "preview": preview_port}
    return {}


def _normalize_port_payload(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: int(value)
        for key, value in payload.items()
        if key in {"app", "preview"} and isinstance(value, int) and 1 <= value <= 65535
    }


def _allocate_ports(
    preferred_ports: Sequence[int],
    *,
    count: int,
    reserved_ports: set[int],
) -> list[int]:
    assigned: list[int] = []
    seen = set()
    for port in preferred_ports:
        if port in seen:
            continue
        seen.add(port)
        if port not in reserved_ports and 1 <= port <= 65535:
            assigned.append(port)
        if len(assigned) == count:
            return assigned
    candidate = max([*seen, *reserved_ports, 3399]) + 1
    while len(assigned) < count:
        if candidate > 65535:
            raise ValueError("could not allocate non-conflicting local ports")
        if candidate not in reserved_ports:
            assigned.append(candidate)
        candidate += 1
    return assigned


def _reserved_ports_from_root(root: Path, *, current_mission_id: str) -> set[int]:
    reserved: set[int] = set()
    for manifest in root.glob("*/mission-package.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("mission_id") == current_mission_id:
            continue
        ports = payload.get("ports")
        if not isinstance(ports, dict):
            continue
        for value in ports.values():
            if isinstance(value, int) and 1 <= value <= 65535:
                reserved.add(value)
    return reserved


__all__ = [
    "RAINFLOW_AD_PRODUCTION_MODE",
    "RAINFLOW_DEFAULT_PORTS",
    "RainFlowAdMission",
    "RainFlowMissionPackageRecord",
    "build_rainflow_ad_mission",
    "contract_ports",
    "discover_rainflow_mission_packages",
]
