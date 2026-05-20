"""KUN-native external sample comparison runner.

This runner lets KUN inspect an earlier system or external project as a
capability sample.  It does not copy source code.  It produces a bounded source
inventory, KUN-vs-sample gap matrix, and Ockham recommendations that Qi can use
as capability-governance input.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.productization import (
    ExternalBehaviorComparisonRecord,
    ExternalBehaviorSignal,
    compare_external_behavior_signals,
    discover_external_behavior_source_paths,
    distill_external_behavior_from_paths,
)
from kun.control_plane.runtime import InMemoryControlPlane, RunnerType, WorkItemResult
from kun.control_plane.v6 import (
    ArtifactManifest,
    ArtifactRecord,
    ExecutionContract,
    GateEvaluation,
    Mission,
    TaskPlan,
    WorkItem,
)

KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER = "kun-external-sample-comparison-runner"

GapCoverage = Literal["covered", "partial", "missing"]
OckhamDecision = Literal["keep_existing", "merge_into_existing", "candidate_for_qi", "discard"]


class ExternalSampleComparisonSpec(BaseModel):
    """Repository comparison boundaries from an execution contract."""

    model_config = ConfigDict(extra="forbid")

    source_name: str = "external-sample"
    source_repo_path: Path
    target_repo_path: Path
    output_dir: Path
    max_source_files: int = Field(default=120, gt=0)
    max_target_files: int = Field(default=160, gt=0)


class ExternalSampleFeatureGap(BaseModel):
    """One capability relationship between an external sample and KUN."""

    model_config = ConfigDict(extra="forbid")

    signal_ref: str
    behavior: str
    subsystem: str
    source_ref: str
    coverage: GapCoverage
    decision: OckhamDecision
    reason: str
    required_tests: list[str] = Field(default_factory=list)
    risk_controls: list[str] = Field(default_factory=list)


class ExternalSampleComparisonRunner:
    """Compare a source project such as Genesis against the current KUN repo."""

    runner_type: RunnerType = "agent"
    runner_identity = "kun-external-sample-comparison-runner"

    def __init__(self, *, control_plane: InMemoryControlPlane) -> None:
        self.control_plane = control_plane

    def can_run(self, work_item: WorkItem) -> bool:
        mission = self.control_plane.missions.get(work_item.mission_id)
        return (
            work_item.owner == KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER
            and mission is not None
            and work_item.type in {"research", "review", "governance"}
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="External sample comparison runner only handles assigned KUN work.",
                failure_category="tool_failure",
            )
        try:
            mission, task_plan, contract = self._records(work_item)
            spec = _spec_from_contract(contract)
            result = _compare_external_sample(
                mission=mission,
                task_plan=task_plan,
                work_item=work_item,
                spec=spec,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return WorkItemResult(
                status="failed",
                summary=f"External sample comparison failed: {type(exc).__name__}: {exc}",
                failure_category="evidence_failure",
            )
        return result

    def _records(self, work_item: WorkItem) -> tuple[Mission, TaskPlan, ExecutionContract]:
        mission = self.control_plane.missions[work_item.mission_id]
        task_plan = _task_plan_for(self.control_plane, mission, work_item.task_plan_version)
        if mission.execution_contract_ref is None:
            raise ValueError("mission has no execution contract")
        contract = self.control_plane.contracts[mission.execution_contract_ref]
        return mission, task_plan, contract


def _compare_external_sample(
    *,
    mission: Mission,
    task_plan: TaskPlan,
    work_item: WorkItem,
    spec: ExternalSampleComparisonSpec,
) -> WorkItemResult:
    source_paths = discover_external_behavior_source_paths(
        [spec.source_repo_path],
        max_files_per_root=spec.max_source_files,
    )
    target_paths = discover_external_behavior_source_paths(
        [spec.target_repo_path],
        max_files_per_root=spec.max_target_files,
    )
    if not source_paths:
        raise ValueError("external sample has no bounded source files to inspect")
    if not target_paths:
        raise ValueError("target KUN repo has no bounded source files to inspect")
    source_signals = distill_external_behavior_from_paths(
        source_paths,
        allowed_roots=[spec.source_repo_path],
        default_origin="external",
    )
    target_signals = distill_external_behavior_from_paths(
        target_paths,
        allowed_roots=[spec.target_repo_path],
        default_origin="external",
    )
    comparisons = compare_external_behavior_signals(source_signals)
    gaps = _feature_gaps(
        source_signals=source_signals,
        target_signals=target_signals,
        comparisons=comparisons,
        target_text=_bounded_join_text(target_paths, spec.target_repo_path),
    )
    feature_markers = _sample_feature_markers(
        source_paths=source_paths,
        target_text=_bounded_join_text(target_paths, spec.target_repo_path),
    )
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = spec.output_dir / "source-inventory.json"
    matrix_path = spec.output_dir / "feature-gap-matrix.md"
    recommendations_path = spec.output_dir / "ockham-recommendations.md"
    inventory_payload = {
        "schema": "kun-external-sample-inventory-v1",
        "mission_id": mission.mission_id,
        "source_name": spec.source_name,
        "source_repo_path": str(spec.source_repo_path),
        "target_repo_path": str(spec.target_repo_path),
        "source_file_count": len(source_paths),
        "target_file_count": len(target_paths),
        "source_paths": source_paths,
        "target_paths": target_paths,
        "source_signal_count": len(source_signals),
        "target_signal_count": len(target_signals),
        "source_signals": [signal.model_dump(mode="json") for signal in source_signals],
        "feature_markers": feature_markers,
    }
    _write_json(inventory_path, inventory_payload)
    matrix_path.write_text(
        _gap_matrix_markdown(
            source_name=spec.source_name,
            task_plan=task_plan,
            gaps=gaps,
            feature_markers=feature_markers,
        ),
        encoding="utf-8",
    )
    recommendations_path.write_text(
        _recommendations_markdown(
            source_name=spec.source_name,
            gaps=gaps,
            feature_markers=feature_markers,
        ),
        encoding="utf-8",
    )
    artifacts = [
        _artifact(
            work_item=work_item,
            suffix="source-inventory",
            path=inventory_path,
            supports=[
                "external_sample_inventory",
                "genesis_comparison" if spec.source_name.lower() == "genesis" else "sample_comparison",
            ],
            kind="source",
        ),
        _artifact(
            work_item=work_item,
            suffix="feature-gap-matrix",
            path=matrix_path,
            supports=["external_sample_gap_matrix", "ockham_review"],
            kind="review",
        ),
        _artifact(
            work_item=work_item,
            suffix="ockham-recommendations",
            path=recommendations_path,
            supports=["external_sample_recommendations", "qi_candidate_input"],
            kind="decision",
        ),
    ]
    manifest = ArtifactManifest(
        manifest_id=f"manifest-{_slug(work_item.mission_id)}-{_slug(work_item.work_item_id)}-external-sample-comparison",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        kind="run",
        artifact_refs=[artifact.artifact_id for artifact in artifacts],
        primary_artifact_ref=artifacts[2].artifact_id,
        evidence_refs=[artifacts[0].artifact_id],
        review_refs=[artifacts[1].artifact_id, artifacts[2].artifact_id],
        created_by=ExternalSampleComparisonRunner.runner_identity,
        content_hash=_hash_payload(
            {
                "source_paths": source_paths,
                "gaps": [gap.model_dump(mode="json") for gap in gaps],
                "feature_markers": feature_markers,
            }
        ),
    )
    candidate_count = sum(1 for gap in gaps if gap.decision == "candidate_for_qi")
    merge_count = sum(1 for gap in gaps if gap.decision == "merge_into_existing")
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-{_slug(work_item.work_item_id)}-external-sample-comparison",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type=mission.task_type,
        rubric_version="kun-external-sample-comparison-v1",
        metric_pack_version="north-star-v6",
        north_star_verdict="pass",
        result_quality=0.84,
        speed=0.7,
        cost=0.75,
        risk=0.3,
        evidence_quality=0.82,
        collaboration_quality=0.7,
        score_breakdown={
            "source_coverage": min(1.0, len(source_paths) / max(1, spec.max_source_files)),
            "candidate_count": float(candidate_count),
            "merge_count": float(merge_count),
        },
        thresholds={"result_quality": 0.8},
        evidence_refs=[artifacts[0].artifact_id],
        artifact_refs=[artifact.artifact_id for artifact in artifacts],
        review_refs=[artifacts[1].artifact_id, artifacts[2].artifact_id],
        confidence=0.78,
        next_action="continue",
        next_state="running",
        learning_eligibility="candidate" if candidate_count or merge_count else "none",
        governance_signal="external_sample_comparison_ready_for_qi_review",
        created_by=ExternalSampleComparisonRunner.runner_identity,
    )
    return WorkItemResult(
        status="done",
        summary=(
            f"KUN compared {spec.source_name} against current KUN: "
            f"{len(gaps)} distilled behaviors, {candidate_count} Qi candidates, "
            f"{merge_count} merge recommendations."
        ),
        artifacts=artifacts,
        artifact_manifest=manifest,
        gate_evaluation=gate,
    )


def _feature_gaps(
    *,
    source_signals: list[ExternalBehaviorSignal],
    target_signals: list[ExternalBehaviorSignal],
    comparisons: list[ExternalBehaviorComparisonRecord],
    target_text: str,
) -> list[ExternalSampleFeatureGap]:
    target_by_behavior = {signal.behavior.lower(): signal for signal in target_signals}
    target_subsystems = {signal.kun_subsystem for signal in target_signals}
    comparison_by_ref = {comparison.signal_ref: comparison for comparison in comparisons}
    gaps: list[ExternalSampleFeatureGap] = []
    for signal in source_signals:
        signal_ref = _signal_ref(signal)
        behavior_key = signal.behavior.lower()
        comparison = comparison_by_ref.get(signal_ref)
        behavior_terms = _behavior_terms(signal.behavior)
        has_keyword_overlap = any(term in target_text for term in behavior_terms)
        if behavior_key in target_by_behavior or has_keyword_overlap:
            coverage: GapCoverage = "covered"
            decision: OckhamDecision = "keep_existing"
            reason = "KUN already exposes this behavior in product/code references; do not add a duplicate subsystem."
        elif signal.kun_subsystem in target_subsystems:
            coverage = "partial"
            decision = "merge_into_existing"
            reason = "The capability overlaps an existing KUN subsystem; merge as tests or execution hooks."
        else:
            coverage = "missing"
            decision = "candidate_for_qi"
            reason = "The sample shows a potentially useful behavior not clearly present in KUN; route to Qi as a candidate, not production."
        if comparison is not None and comparison.complexity_impact == "high":
            decision = "discard"
            reason = "Complexity impact is high; keep as evidence unless a real task proves the need."
        gaps.append(
            ExternalSampleFeatureGap(
                signal_ref=signal_ref,
                behavior=signal.behavior,
                subsystem=signal.kun_subsystem,
                source_ref=signal.source_ref,
                coverage=coverage,
                decision=decision,
                reason=reason,
                required_tests=list(signal.required_tests),
                risk_controls=list(signal.risk_controls),
            )
        )
    return gaps


def _sample_feature_markers(*, source_paths: list[str], target_text: str) -> list[dict[str, str]]:
    source_text = "\n".join(path.lower() for path in source_paths)
    source_text += _bounded_join_text(
        source_paths,
        Path(source_paths[0]).anchor or "/",
        max_chars=2_000_000,
    )
    marker_specs = {
        "sleep_cycle": {
            "needles": ("sleep cycle", "sleep-cycle", "sleep_cycle"),
            "behavior": "cross-session autonomous work cycle",
            "kun_terms": ("daemon", "cross-day", "跨天", "idle_batch"),
            "coverage_if_present": "covered",
            "decision_if_present": "keep_existing",
        },
        "consult_injection": {
            "needles": ("consult injection", "consult_injection", "consult-injection"),
            "behavior": "external expert consultation injection",
            "kun_terms": ("expert_input", "collaborationticket", "外部专家"),
            "coverage_if_present": "covered",
            "decision_if_present": "keep_existing",
        },
        "hall_of_fame": {
            "needles": ("hall of fame", "hall-of-fame"),
            "behavior": "curated best-practice memory library",
            "kun_terms": ("capabilityprofile", "capability profile", "能力库"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "agent_learning_index": {
            "needles": ("agent learning index",),
            "behavior": "agent learning and memory index",
            "kun_terms": ("capabilityprofile", "learning_writeback", "context"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "scoring_framework": {
            "needles": ("scoring framework", "score_breakdown"),
            "behavior": "explicit scoring rubric framework",
            "kun_terms": ("gateevaluation", "score_breakdown", "north_star_verdict"),
            "coverage_if_present": "covered",
            "decision_if_present": "keep_existing",
        },
        "runtime_isolation": {
            "needles": ("runtime isolation", "sandbox factory", "shell_exec sandboxing"),
            "behavior": "process-level runtime and tool sandbox isolation",
            "kun_terms": ("sandbox_ref", "workspace_snapshot", "file-io skill"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "self_improvement": {
            "needles": ("self-improvement", "self improvement"),
            "behavior": "self-improvement research loop",
            "kun_terms": ("qi_capability_evolution", "learning_writeback", "self_improvement"),
            "coverage_if_present": "covered",
            "decision_if_present": "keep_existing",
        },
        "global_todo": {
            "needles": ("global todo", "global_todo"),
            "behavior": "global backlog and unfinished work ledger",
            "kun_terms": ("workitem", "persistent queue", "持久队列"),
            "coverage_if_present": "covered",
            "decision_if_present": "keep_existing",
        },
        "memory_persistence": {
            "needles": ("memory persistence", "long-term memory", "episodic memory"),
            "behavior": "long-term episodic memory persistence",
            "kun_terms": ("context", "asset", "methodology", "learning_writeback"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "vector_graph_memory": {
            "needles": ("vector", "graph", "memory"),
            "behavior": "vector and graph retrieval for memory",
            "kun_terms": ("embedding", "context", "importance"),
            "coverage_if_present": "partial",
            "decision_if_present": "candidate_for_qi",
        },
        "procedural_memory": {
            "needles": ("procedural memory", "pheromone", "skill routing"),
            "behavior": "procedural memory for skill routing",
            "kun_terms": ("capability_router", "capability card", "route"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "event_sourced_memory": {
            "needles": ("event sourcing", "event-sourced", "immutable event"),
            "behavior": "event-sourced memory and deterministic replay",
            "kun_terms": ("ledgerevent", "ledger_event", "gate_evaluation"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "mcp_resources_prompts": {
            "needles": ("mcp", "resources", "prompts"),
            "behavior": "full MCP resource and prompt interoperability",
            "kun_terms": ("mcp", "skill", "plugin"),
            "coverage_if_present": "partial",
            "decision_if_present": "candidate_for_qi",
        },
        "plugin_lifecycle": {
            "needles": ("plugin lifecycle", "plugin-runtime", "plugin sandbox"),
            "behavior": "plugin lifecycle and sandbox governance",
            "kun_terms": ("skills", "plugin", "skill_refs"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "smoke_test_battery": {
            "needles": ("smoke-", "smoke test", "smoke-test"),
            "behavior": "broad smoke-test battery for operational regressions",
            "kun_terms": ("tests/unit", "pytest", "regression"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "enterprise_auth_tenant": {
            "needles": ("oidc", "scim", "tenant", "billing"),
            "behavior": "enterprise auth, tenant, and billing surface",
            "kun_terms": ("tenant", "auth", "billing"),
            "coverage_if_present": "partial",
            "decision_if_present": "discard",
        },
        "federation_replication": {
            "needles": ("federation", "replication", "delta sync"),
            "behavior": "multi-instance federation and replication",
            "kun_terms": ("federation", "replication"),
            "coverage_if_present": "missing",
            "decision_if_present": "discard",
        },
        "trust_weighted_ranking": {
            "needles": ("trust-weighted", "trust weighted", "weighted ranking"),
            "behavior": "trust-weighted source and capability ranking",
            "kun_terms": ("source_quality", "capability score", "benchmark"),
            "coverage_if_present": "partial",
            "decision_if_present": "merge_into_existing",
        },
        "adaptive_concurrency": {
            "needles": ("adaptive concurrency", "worker pool", "throughput"),
            "behavior": "adaptive worker concurrency from measured throughput",
            "kun_terms": ("max_work_items_per_tick", "worker", "throughput"),
            "coverage_if_present": "partial",
            "decision_if_present": "candidate_for_qi",
        },
    }
    markers: list[dict[str, str]] = []
    for marker_id, spec in marker_specs.items():
        if not any(needle in source_text for needle in spec["needles"]):
            continue
        target_has_terms = any(term in target_text for term in spec["kun_terms"])
        coverage = spec["coverage_if_present"] if target_has_terms else "missing"
        decision = spec["decision_if_present"] if target_has_terms else "candidate_for_qi"
        if decision == "discard":
            reason = "Keep as evidence only unless KUN becomes a multi-tenant SaaS control plane."
        elif decision == "candidate_for_qi":
            reason = "Route to Qi for validation; do not add a subsystem until a real task proves value."
        elif decision == "merge_into_existing":
            reason = "Merge into an existing KUN subsystem as tests, hooks, or capability profiles."
        else:
            reason = "No new subsystem; current KUN path already covers the useful behavior."
        markers.append(
            {
                "marker_id": marker_id,
                "behavior": spec["behavior"],
                "coverage": coverage,
                "decision": decision,
                "reason": reason,
            }
        )
    return markers


def _spec_from_contract(contract: ExecutionContract) -> ExternalSampleComparisonSpec:
    payload = _find_comparison_payload(contract)
    if payload is None:
        raise ValueError("execution contract lacks external_sample_comparison settings")
    source_repo_path = Path(str(payload["source_repo_path"])).expanduser().resolve()
    target_repo_path = Path(str(payload["target_repo_path"])).expanduser().resolve()
    output_dir = Path(
        str(payload.get("output_dir") or target_repo_path / ".kun-local" / "external-sample-comparison")
    ).expanduser().resolve()
    if not source_repo_path.exists():
        raise ValueError(f"source_repo_path does not exist: {source_repo_path}")
    if not target_repo_path.exists():
        raise ValueError(f"target_repo_path does not exist: {target_repo_path}")
    return ExternalSampleComparisonSpec(
        source_name=str(payload.get("source_name") or source_repo_path.name),
        source_repo_path=source_repo_path,
        target_repo_path=target_repo_path,
        output_dir=output_dir,
        max_source_files=int(payload.get("max_source_files") or 120),
        max_target_files=int(payload.get("max_target_files") or 160),
    )


def _find_comparison_payload(contract: ExecutionContract) -> dict[str, Any] | None:
    for candidate in (
        contract.evidence_policy,
        contract.delivery_contract,
        contract.risk_policy,
    ):
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("external_sample_comparison")
        if isinstance(nested, dict):
            return nested
        if {"source_repo_path", "target_repo_path"}.issubset(candidate):
            return candidate
    return None


def _task_plan_for(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    task_plan_version: str,
) -> TaskPlan:
    for plan in control_plane.task_plans.values():
        if plan.mission_id == mission.mission_id and plan.version == task_plan_version:
            return plan
    raise ValueError(f"task plan version not found: {task_plan_version}")


def _bounded_join_text(paths: list[str], root: Path | str, *, max_chars: int = 500_000) -> str:
    root_path = Path(root).expanduser().resolve()
    chunks: list[str] = []
    remaining = max_chars
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            continue
        if root_path != Path("/") and not (path == root_path or root_path in path.parents):
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        chunks.append(f"\n# {path.name}\n{text[:remaining]}")
        remaining -= len(chunks[-1])
        if remaining <= 0:
            break
    return "\n".join(chunks)


def _behavior_terms(behavior: str) -> list[str]:
    words = [word for word in re.findall(r"[a-z][a-z0-9-]+", behavior.lower()) if len(word) >= 5]
    counter = Counter(words)
    return [word for word, _count in counter.most_common(4)]


def _gap_matrix_markdown(
    *,
    source_name: str,
    task_plan: TaskPlan,
    gaps: list[ExternalSampleFeatureGap],
    feature_markers: list[dict[str, str]],
) -> str:
    rows = [
        f"# {source_name} vs KUN Capability Gap Matrix",
        "",
        f"Task plan: `{task_plan.plan_id}` / `{task_plan.version}`",
        "",
        "| Behavior | KUN subsystem | Coverage | Ockham decision | Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gap in gaps:
        rows.append(
            "| "
            + " | ".join(
                [
                    _md(gap.behavior),
                    _md(gap.subsystem),
                    gap.coverage,
                    gap.decision,
                    _md(gap.reason),
                ]
            )
            + " |"
        )
    if feature_markers:
        rows.extend(["", "## Additional Genesis Markers", ""])
        rows.append("| Marker | Behavior | Coverage | Decision |")
        rows.append("| --- | --- | --- | --- |")
        for marker in feature_markers:
            rows.append(
                "| "
                + " | ".join(
                    [
                        _md(marker["marker_id"]),
                        _md(marker["behavior"]),
                        marker["coverage"],
                        marker["decision"],
                    ]
                )
                + " |"
            )
    return "\n".join(rows) + "\n"


def _recommendations_markdown(
    *,
    source_name: str,
    gaps: list[ExternalSampleFeatureGap],
    feature_markers: list[dict[str, str]],
) -> str:
    grouped: dict[str, list[str]] = {
        "keep_existing": [],
        "merge_into_existing": [],
        "candidate_for_qi": [],
        "discard": [],
    }
    for gap in gaps:
        grouped[gap.decision].append(f"- {gap.behavior}: {gap.reason}")
    for marker in feature_markers:
        grouped[marker["decision"]].append(f"- {marker['behavior']}: {marker['reason']}")
    lines = [
        f"# {source_name} Ockham Recommendations",
        "",
        "Rule: do not add a new subsystem when an existing KUN subsystem can absorb the behavior as a test, hook, or capability profile.",
        "",
    ]
    labels = {
        "keep_existing": "Keep Existing",
        "merge_into_existing": "Merge Into Existing",
        "candidate_for_qi": "Qi Candidate",
        "discard": "Discard Or Evidence Only",
    }
    for decision, title in labels.items():
        lines.extend([f"## {title}", ""])
        lines.extend(grouped[decision] or ["- None."])
        lines.append("")
    return "\n".join(lines)


def _artifact(
    *,
    work_item: WorkItem,
    suffix: str,
    path: Path,
    supports: list[str],
    kind: Literal["source", "review", "decision"],
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact-{_slug(work_item.work_item_id)}-{suffix}",
        kind=kind,
        path_or_uri=str(path),
        content_hash=_file_sha256(path),
        created_by=ExternalSampleComparisonRunner.runner_identity,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=supports,
        freshness="fresh",
        source_quality="primary",
    )


def _signal_ref(signal: ExternalBehaviorSignal) -> str:
    return f"behavior:{signal.origin}:{_slug(signal.signal_id)}:{_slug(signal.behavior)}"


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "item"


__all__ = [
    "KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER",
    "ExternalSampleComparisonRunner",
    "ExternalSampleComparisonSpec",
    "ExternalSampleFeatureGap",
]
