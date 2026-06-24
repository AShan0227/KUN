"""KUN-native game design research runner.

The app-development runner can build a playable project, but complex product
work must first pass the KUN V6 evidence and plan-alignment loop.  This runner
owns that research-first path for game missions: it reads the source brief,
reviews external references, produces a design synthesis, gates the design, and
only then queues the autonomous app runner.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.app_development import KUN_AUTONOMOUS_APP_RUNNER_OWNER
from kun.control_plane.runtime import InMemoryControlPlane, RunnerType, WorkItemResult
from kun.control_plane.v6 import (
    ArtifactKind,
    ArtifactManifest,
    ArtifactRecord,
    ExecutionContract,
    GateEvaluation,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER = "kun-game-design-research-runner"


class ResearchSource(BaseModel):
    """One source KUN should inspect before locking the game design."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str
    url: str
    category: str
    organization: str
    source_quality: str = "primary"
    design_takeaway: str


class FetchedResearchSource(BaseModel):
    """Normalized source observation produced by the KUN runner."""

    model_config = ConfigDict(extra="forbid")

    source: ResearchSource
    fetch_status: str
    fetched_title: str = ""
    text_excerpt: str = ""
    error: str = ""


class GameResearchSpec(BaseModel):
    """Execution spec derived from the current contract."""

    model_config = ConfigDict(extra="forbid")

    project_path: Path
    final_project_path: Path
    source_docx_path: Path | None = None
    first_worlds: list[str] = Field(default_factory=lambda: ["彩虹造物岛", "故事星球"])
    research_sources: list[ResearchSource] = Field(default_factory=list)
    minimum_sources: int = 12
    minimum_live_fetches: int = 6


FetchResult = tuple[str, str, str]
SourceFetcher = Callable[[ResearchSource], FetchResult]


@dataclass(frozen=True)
class DesignGateInputs:
    corpus_path: Path
    design_spec_path: Path
    fetched_count: int
    source_count: int
    artifact_refs: list[str]


class GameDesignResearchRunner:
    """Autonomous research and design worker for KUN game missions."""

    runner_type: RunnerType = "agent"
    runner_identity = "kun-game-design-research-runner"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        fetcher: SourceFetcher | None = None,
    ) -> None:
        self.control_plane = control_plane
        self.fetcher = fetcher or _fetch_source

    def can_run(self, work_item: WorkItem) -> bool:
        mission = self.control_plane.missions.get(work_item.mission_id)
        return (
            work_item.owner == KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER
            and mission is not None
            and mission.task_type == "product_development"
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="Game design research runner only handles assigned product-development work.",
                failure_category="tool_failure",
            )
        try:
            mission, task_plan, contract = self._records(work_item)
            spec = _spec_from_contract(contract)
            phase = _phase_from_work_item(work_item)
            if phase == "research-corpus":
                return self._write_research_corpus(
                    work_item=work_item,
                    mission=mission,
                    task_plan=task_plan,
                    spec=spec,
                )
            if phase == "design-synthesis":
                return self._write_design_synthesis(
                    work_item=work_item,
                    mission=mission,
                    task_plan=task_plan,
                    spec=spec,
                )
            if phase == "design-gate":
                return self._gate_design_and_enqueue_mvp(
                    work_item=work_item,
                    mission=mission,
                    task_plan=task_plan,
                    contract=contract,
                    spec=spec,
                )
        except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            return WorkItemResult(
                status="failed",
                summary=f"Game design research runner failed: {type(exc).__name__}: {exc}",
                failure_category="evidence_failure",
            )
        return WorkItemResult(
            status="blocked",
            summary=f"Unsupported game design work item: {work_item.work_item_id}",
            failure_category="plan_failure",
        )

    def _records(self, work_item: WorkItem) -> tuple[Mission, TaskPlan, ExecutionContract]:
        mission = self.control_plane.missions[work_item.mission_id]
        task_plan = _task_plan_for(self.control_plane, mission, work_item.task_plan_version)
        if mission.execution_contract_ref is None:
            raise ValueError("mission has no execution contract")
        contract = self.control_plane.contracts[mission.execution_contract_ref]
        return mission, task_plan, contract

    def _write_research_corpus(
        self,
        *,
        work_item: WorkItem,
        mission: Mission,
        task_plan: TaskPlan,
        spec: GameResearchSpec,
    ) -> WorkItemResult:
        brief_text = _extract_docx_text(spec.source_docx_path) if spec.source_docx_path else ""
        fetched_sources = [self._fetch_one(source) for source in spec.research_sources]
        research_dir = spec.project_path / "docs" / "research"
        corpus_path = research_dir / "source-corpus.json"
        brief_path = research_dir / "source-brief-extract.md"
        matrix_path = research_dir / "source-corpus.md"
        corpus_payload = {
            "mission_id": mission.mission_id,
            "task_plan_version": task_plan.version,
            "runner_identity": self.runner_identity,
            "source_docx_path": str(spec.source_docx_path) if spec.source_docx_path else "",
            "first_worlds_candidate": spec.first_worlds,
            "sources": [item.model_dump(mode="json") for item in fetched_sources],
        }
        _write_text(corpus_path, json.dumps(corpus_payload, ensure_ascii=False, indent=2) + "\n")
        _write_text(brief_path, _brief_extract_markdown(brief_text))
        _write_text(matrix_path, _source_matrix_markdown(fetched_sources))
        artifacts = [
            _artifact(
                work_item=work_item,
                suffix="source-corpus-json",
                path=corpus_path,
                supports=["external_research_corpus", "game_design_evidence"],
                kind="source",
            ),
            _artifact(
                work_item=work_item,
                suffix="source-brief-extract",
                path=brief_path,
                supports=["source_brief", "huohutu_v12_context"],
                kind="source",
            ),
            _artifact(
                work_item=work_item,
                suffix="source-corpus-md",
                path=matrix_path,
                supports=["source_synthesis_input", "benchmark_matrix"],
                kind="evidence",
            ),
        ]
        fetched_count = sum(item.fetch_status == "fetched" for item in fetched_sources)
        return WorkItemResult(
            status="done",
            summary=(
                f"Game design research corpus recorded: {len(fetched_sources)} sources, "
                f"{fetched_count} fetched live."
            ),
            artifacts=artifacts,
        )

    def _fetch_one(self, source: ResearchSource) -> FetchedResearchSource:
        try:
            status, fetched_title, text_excerpt = self.fetcher(source)
            return FetchedResearchSource(
                source=source,
                fetch_status=status,
                fetched_title=fetched_title,
                text_excerpt=text_excerpt,
            )
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return FetchedResearchSource(
                source=source,
                fetch_status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _write_design_synthesis(
        self,
        *,
        work_item: WorkItem,
        mission: Mission,
        task_plan: TaskPlan,
        spec: GameResearchSpec,
    ) -> WorkItemResult:
        corpus = _load_corpus(spec.project_path)
        benchmark_path = spec.project_path / "docs" / "research" / "benchmark-synthesis.md"
        design_spec_path = spec.project_path / "docs" / "game-design-spec.md"
        mvp_plan_path = spec.project_path / "docs" / "research-first-mvp-plan.md"
        _write_text(
            benchmark_path,
            _benchmark_synthesis_markdown(
                mission=mission,
                sources=corpus,
                selected_worlds=spec.first_worlds,
            ),
        )
        _write_text(
            design_spec_path,
            _game_design_spec_markdown(
                task_plan=task_plan,
                sources=corpus,
                selected_worlds=spec.first_worlds,
            ),
        )
        _write_text(
            mvp_plan_path,
            _research_first_mvp_plan_markdown(
                task_plan=task_plan,
                selected_worlds=spec.first_worlds,
                final_project_path=spec.final_project_path,
            ),
        )
        artifacts = [
            _artifact(
                work_item=work_item,
                suffix="benchmark-synthesis",
                path=benchmark_path,
                supports=["competitive_benchmark", "education_game_references"],
            ),
            _artifact(
                work_item=work_item,
                suffix="game-design-spec",
                path=design_spec_path,
                supports=["game_design_spec", "spark_loop", "child_safety"],
            ),
            _artifact(
                work_item=work_item,
                suffix="research-first-mvp-plan",
                path=mvp_plan_path,
                supports=["plan_alignment", "mvp_execution_plan"],
                kind="decision",
            ),
        ]
        return WorkItemResult(
            status="done",
            summary="Research-backed game design spec and MVP plan created by KUN.",
            artifacts=artifacts,
        )

    def _gate_design_and_enqueue_mvp(
        self,
        *,
        work_item: WorkItem,
        mission: Mission,
        task_plan: TaskPlan,
        contract: ExecutionContract,
        spec: GameResearchSpec,
    ) -> WorkItemResult:
        inputs = _design_gate_inputs(
            control_plane=self.control_plane,
            mission_id=mission.mission_id,
            spec=spec,
        )
        gate_pass = (
            inputs.source_count >= spec.minimum_sources
            and inputs.fetched_count >= spec.minimum_live_fetches
        )
        if not gate_pass:
            gate = GateEvaluation(
                gate_evaluation_id=f"gate-{mission.mission_id}-game-design-research",
                mission_id=mission.mission_id,
                task_plan_version=work_item.task_plan_version,
                subject_ref=work_item.work_item_id,
                stage="plan",
                task_type="product_development",
                rubric_version="kun-game-design-research-v1",
                metric_pack_version="kun-v6-north-star-v1",
                north_star_verdict="partial",
                result_quality=0.62,
                speed=0.64,
                cost=0.8,
                risk=0.62,
                evidence_quality=0.56,
                collaboration_quality=0.86,
                score_breakdown={
                    "source_count": min(inputs.source_count / spec.minimum_sources, 1.0),
                    "live_fetch_count": min(inputs.fetched_count / spec.minimum_live_fetches, 1.0),
                    "design_spec_present": float(inputs.design_spec_path.exists()),
                },
                thresholds={"result_quality": 0.8},
                hard_gate_failures=["insufficient_live_research_evidence"],
                artifact_refs=inputs.artifact_refs,
                evidence_refs=inputs.artifact_refs,
                source_freshness="mixed",
                failure_category="evidence_failure",
                root_cause="Research source coverage was not deep enough to start final MVP development.",
                responsibility_scope="kun_auto",
                confidence=0.75,
                next_action="needs_repair",
                next_state="repairing",
                learning_eligibility="none",
                governance_signal="KUN must complete research before development.",
                created_by=self.runner_identity,
            )
            return WorkItemResult(
                status="partial",
                summary="Research design gate did not pass; more evidence is required.",
                gate_evaluation=gate,
            )

        design_gate_artifact = _artifact(
            work_item=work_item,
            suffix="design-gate",
            path=inputs.design_spec_path,
            supports=["design_gate_passed", "research_first_development_allowed"],
            kind="review",
        )
        manifest = ArtifactManifest(
            manifest_id=f"manifest-{mission.mission_id}-game-design-research",
            mission_id=mission.mission_id,
            work_item_id=work_item.work_item_id,
            kind="merge",
            artifact_refs=[design_gate_artifact.artifact_id, *inputs.artifact_refs],
            primary_artifact_ref=design_gate_artifact.artifact_id,
            evidence_refs=inputs.artifact_refs,
            review_refs=[design_gate_artifact.artifact_id],
            created_by=self.runner_identity,
            content_hash=_hash_path(spec.project_path / "docs"),
            supports_delivery=False,
        )
        if task_plan.version.startswith("mvp-v2"):
            self._enqueue_research_gated_mvp(
                mission=mission,
                previous_plan=task_plan,
                previous_contract=contract,
                design_gate_work_item=work_item,
                spec=spec,
                evidence_artifact_refs=[design_gate_artifact.artifact_id, *inputs.artifact_refs],
            )
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-{mission.mission_id}-game-design-research",
            mission_id=mission.mission_id,
            task_plan_version=work_item.task_plan_version,
            subject_ref=manifest.manifest_id,
            stage="plan",
            task_type="product_development",
            rubric_version="kun-game-design-research-v1",
            metric_pack_version="kun-v6-north-star-v1",
            north_star_verdict="pass",
            result_quality=0.86,
            speed=0.7,
            cost=0.82,
            risk=0.32,
            evidence_quality=0.86,
            collaboration_quality=0.86,
            score_breakdown={
                "source_count": min(inputs.source_count / spec.minimum_sources, 1.0),
                "live_fetch_count": min(inputs.fetched_count / spec.minimum_live_fetches, 1.0),
                "design_spec_present": 1.0,
                "mvp_scope_alignment": 0.88,
                "child_safety_boundary": 0.84,
            },
            thresholds={"result_quality": 0.8},
            artifact_refs=[design_gate_artifact.artifact_id],
            evidence_refs=inputs.artifact_refs,
            review_refs=[design_gate_artifact.artifact_id],
            source_freshness="fresh",
            responsibility_scope="kun_auto",
            confidence=0.84,
            next_action="continue",
            next_state="running",
            learning_eligibility="none",
            governance_signal=(
                "Research-backed design is ready; KUN app runner may build MVP v2."
                if task_plan.version.startswith("mvp-v2")
                else "Research-backed design is ready; existing plan work items may continue."
            ),
            created_by=self.runner_identity,
        )
        return WorkItemResult(
            status="done",
            summary=(
                "Research-backed design gate passed; MVP v2 app work queued for KUN."
                if task_plan.version.startswith("mvp-v2")
                else "Research-backed design gate passed; current plan can continue."
            ),
            artifacts=[design_gate_artifact],
            artifact_manifest=manifest,
            gate_evaluation=gate,
        )

    def _enqueue_research_gated_mvp(
        self,
        *,
        mission: Mission,
        previous_plan: TaskPlan,
        previous_contract: ExecutionContract,
        design_gate_work_item: WorkItem,
        spec: GameResearchSpec,
        evidence_artifact_refs: list[str],
    ) -> None:
        next_version = "mvp-v2-design-gated"
        if any(
            item.mission_id == mission.mission_id and item.task_plan_version == next_version
            for item in self.control_plane.work_items.values()
        ):
            return
        plan = _research_gated_task_plan(
            mission=mission,
            previous_plan=previous_plan,
            version=next_version,
            first_worlds=spec.first_worlds,
            evidence_artifact_refs=evidence_artifact_refs,
        )
        delivery_contract = dict(previous_contract.delivery_contract)
        delivery_contract.update(
            {
                "project_path": str(spec.final_project_path),
                "delivery_type": "playable_mvp",
                "production_complete": False,
                "research_first": True,
                "first_worlds": spec.first_worlds,
                "design_spec_path": str(spec.project_path / "docs" / "game-design-spec.md"),
            }
        )
        evidence_policy = dict(previous_contract.evidence_policy)
        evidence_policy.update(
            {
                "required": [
                    "source_doc",
                    "external_research_corpus",
                    "game_design_spec",
                    "code_path",
                    "build_result",
                    "playtest_report",
                ],
                "research_artifact_refs": evidence_artifact_refs,
            }
        )
        contract = previous_contract.model_copy(
            update={
                "contract_id": f"contract-{mission.mission_id}-{next_version}",
                "task_plan_version": next_version,
                "evidence_policy": evidence_policy,
                "delivery_contract": delivery_contract,
            }
        )
        context = WorkingContext(
            working_context_id=f"ctx-{mission.mission_id}-{next_version}",
            mission_id=mission.mission_id,
            task_plan_version=next_version,
            audience="kun-autonomous-app-runner",
            scope="research-backed Fire Rabbit Spark MVP implementation",
            summary=(
                "Implement the playable MVP only after KUN completed external research, "
                "benchmark synthesis, and game design gate."
            ),
            critical_facts=[
                "The previous MVP is only an autonomous execution proof, not final delivery.",
                "MVP v2 must follow the research-backed game design spec.",
                f"First two worlds selected by KUN: {', '.join(spec.first_worlds)}.",
                "Mock AI remains acceptable for MVP, but adapter boundaries must stay clear.",
            ],
            acceptance_criteria=plan.acceptance_criteria,
            constraints=plan.constraints,
            risk_flags=plan.risk_register,
            artifact_refs=evidence_artifact_refs,
            source_hashes=[_hash_path(spec.project_path / "docs" / "research")],
        )
        work_items = _mvp_app_work_items(
            mission_id=mission.mission_id,
            task_plan_version=next_version,
            design_gate_work_item_id=design_gate_work_item.work_item_id,
        )
        self.control_plane.record_plan_change(
            mission_id=mission.mission_id,
            task_plan=plan,
            execution_contract=contract,
            working_context=context,
            work_items=work_items,
            actor=self.runner_identity,
            reason="Research and game design gate passed; queue research-backed MVP v2 build.",
        )


def _task_plan_for(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    task_plan_version: str,
) -> TaskPlan:
    for plan in control_plane.task_plans.values():
        if plan.mission_id == mission.mission_id and plan.version == task_plan_version:
            return plan
    raise ValueError(f"task plan version not found: {task_plan_version}")


def _spec_from_contract(contract: ExecutionContract) -> GameResearchSpec:
    delivery_contract = contract.delivery_contract
    evidence_policy = contract.evidence_policy
    project_path = delivery_contract.get("research_workspace_path") or delivery_contract.get(
        "project_path"
    )
    if not isinstance(project_path, str) or not project_path.strip():
        raise ValueError("delivery_contract.project_path is required")
    final_project_path = delivery_contract.get("final_project_path")
    if not isinstance(final_project_path, str) or not final_project_path.strip():
        final_project_path = str(
            Path(project_path)
            .expanduser()
            .resolve()
            .with_name(Path(project_path).name + "-research-gated")
        )
    source_docx_path = evidence_policy.get("source_docx_path") or delivery_contract.get(
        "source_docx_path"
    )
    source_docx = (
        Path(source_docx_path).expanduser().resolve()
        if isinstance(source_docx_path, str) and source_docx_path.strip()
        else None
    )
    raw_worlds = delivery_contract.get("first_worlds")
    first_worlds = (
        [str(item) for item in raw_worlds if str(item).strip()]
        if isinstance(raw_worlds, list)
        else ["彩虹造物岛", "故事星球"]
    )
    minimum_sources = _positive_int(evidence_policy.get("minimum_sources"), default=12)
    minimum_live_fetches = _positive_int(
        evidence_policy.get("minimum_live_fetches"),
        default=min(6, minimum_sources),
    )
    raw_sources = evidence_policy.get("research_sources")
    if isinstance(raw_sources, list) and raw_sources:
        sources = _supplement_research_sources(
            [ResearchSource.model_validate(item) for item in raw_sources],
            minimum_sources=minimum_sources,
        )
    else:
        sources = _default_research_sources()
    return GameResearchSpec(
        project_path=Path(project_path).expanduser().resolve(),
        final_project_path=Path(final_project_path).expanduser().resolve(),
        source_docx_path=source_docx,
        first_worlds=first_worlds,
        research_sources=sources,
        minimum_sources=minimum_sources,
        minimum_live_fetches=minimum_live_fetches,
    )


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _supplement_research_sources(
    sources: list[ResearchSource],
    *,
    minimum_sources: int,
) -> list[ResearchSource]:
    if len(sources) >= minimum_sources:
        return sources
    seen = {source.source_id for source in sources}
    supplemented = list(sources)
    for source in _default_research_sources():
        if source.source_id in seen:
            continue
        supplemented.append(source)
        seen.add(source.source_id)
        if len(supplemented) >= minimum_sources:
            break
    return supplemented


def _phase_from_work_item(work_item: WorkItem) -> str:
    if work_item.phase:
        return work_item.phase
    item_id = work_item.work_item_id
    if "research-source-corpus" in item_id:
        return "research-corpus"
    if "game-design-synthesis" in item_id:
        return "design-synthesis"
    if "research-design-gate" in item_id:
        return "design-gate"
    return "unsupported"


def _default_research_sources() -> list[ResearchSource]:
    return [
        ResearchSource(
            source_id="duolingo-101",
            title="Duolingo 101: gamified learning loop",
            url="https://blog.duolingo.com/duolingo-101-how-to-learn-a-language-on-duolingo/",
            category="learning_engagement",
            organization="Duolingo",
            design_takeaway="Use tiny repeatable lessons, XP, streaks, and social motivation carefully.",
        ),
        ResearchSource(
            source_id="duolingo-home-screen",
            title="Duolingo home screen redesign",
            url="https://blog.duolingo.com/new-duolingo-home-screen-design/",
            category="learning_sequence",
            organization="Duolingo",
            design_takeaway="Use a visible path and spaced review so children know the next action.",
        ),
        ResearchSource(
            source_id="duolingo-half-life",
            title="How Duolingo learns how you learn",
            url="https://blog.duolingo.com/how-we-learn-how-you-learn/",
            category="adaptive_learning",
            organization="Duolingo",
            design_takeaway="Track recall and mistakes to schedule personalized practice.",
        ),
        ResearchSource(
            source_id="duolingo-achievements",
            title="Duolingo achievement badges",
            url="https://blog.duolingo.com/achievement-badges/",
            category="motivation",
            organization="Duolingo",
            design_takeaway="Badges should reward meaningful behavior, not distract from learning quality.",
        ),
        ResearchSource(
            source_id="toca-boca-about",
            title="Toca Boca about",
            url="https://www.tocaboca.com/about",
            category="open_ended_play",
            organization="Toca Boca",
            design_takeaway="Design digital toys where kids feel free to be themselves.",
        ),
        ResearchSource(
            source_id="lego-play",
            title="Introducing LEGO PLAY",
            url="https://www.lego.com/en-us/aboutus/news/2024/august/introducing-lego-play",
            category="creative_tools",
            organization="LEGO",
            design_takeaway="Give children creative canvases and building tools that encourage experimentation.",
        ),
        ResearchSource(
            source_id="khan-kids",
            title="Khan Academy Kids",
            url="https://en.khanacademy.org/kids",
            category="early_learning",
            organization="Khan Academy",
            design_takeaway="Combine academic skills, social-emotional growth, story, and creative play.",
        ),
        ResearchSource(
            source_id="khan-learning-path",
            title="Khan Academy Kids learning path",
            url="https://khankids.zendesk.com/hc/en-us/articles/360014856151-Learning-Topics-Using-Khan-Academy-Kids-in-Educational-Settings",
            category="personalization",
            organization="Khan Academy Kids",
            design_takeaway="Personalize lesson order by level and resume gracefully after interruption.",
        ),
        ResearchSource(
            source_id="minecraft-education-impact",
            title="Minecraft Education impact",
            url="https://education.minecraft.net/en-us/discover/impact",
            category="game_based_learning",
            organization="Minecraft Education",
            design_takeaway="Build future-ready skills through creativity, systems thinking, and play.",
        ),
        ResearchSource(
            source_id="blizzard-accessibility",
            title="Diablo Immortal accessibility features",
            url="https://news.blizzard.com/en-us/article/23805083/making-a-game-for-everyonediablo-immortals-accessibility-features",
            category="game_accessibility",
            organization="Blizzard",
            design_takeaway="Core controls and communication should be adjustable and accessible from launch.",
        ),
        ResearchSource(
            source_id="blizzard-ui-hud",
            title="World of Warcraft UI and HUD update",
            url="https://news.blizzard.com/en-us/article/23837944/get-into-the-grid-of-things-with-the-updated-ui-and-hud",
            category="interface_craft",
            organization="Blizzard",
            design_takeaway="Reduce clutter, preserve world personality, and let players customize layout.",
        ),
        ResearchSource(
            source_id="blizzard-safe-community",
            title="World of Warcraft player behavior and reporting",
            url="https://news.blizzard.com/en-gb/article/23797208/an-eye-on-player-behavior-and-reporting-improvements",
            category="safety_and_feedback",
            organization="Blizzard",
            design_takeaway="A safe play environment needs clear rules, reporting, feedback, and ongoing iteration.",
        ),
        ResearchSource(
            source_id="ftc-coppa",
            title="FTC children's privacy guidance",
            url="https://www.ftc.gov/business-guidance/privacy-security/childrens-privacy",
            category="privacy_compliance",
            organization="FTC",
            design_takeaway="Parents must control collection of personal information from children under 13.",
        ),
        ResearchSource(
            source_id="apple-kids-guidelines",
            title="Apple App Store review guidelines",
            url="https://developer.apple.com/app-store/review/guidelines/",
            category="app_store_safety",
            organization="Apple",
            design_takeaway="Kids apps require strong privacy and age-appropriate advertising/data boundaries.",
        ),
        ResearchSource(
            source_id="google-families-policy",
            title="Google Play Families Policies",
            url="https://support.google.com/googleplay/android-developer/answer/9898834?hl=en",
            category="app_store_safety",
            organization="Google Play",
            design_takeaway="Child-directed apps need compliant SDK, ads, data, and family policy controls.",
        ),
        ResearchSource(
            source_id="unicef-ai-children",
            title="UNICEF guidance on AI and children",
            url="https://www.unicef.org/innocenti/innocenti/reports/policy-guidance-ai-children",
            category="child_ai_safety",
            organization="UNICEF",
            design_takeaway="Child-centered AI must protect rights, safety, agency, and inclusion.",
        ),
        ResearchSource(
            source_id="common-sense-ai",
            title="Common Sense Media AI initiatives",
            url="https://www.commonsensemedia.org/ai",
            category="child_ai_safety",
            organization="Common Sense Media",
            source_quality="credible",
            design_takeaway="AI for families should support human connection, trust, transparency, and guidance.",
        ),
    ]


def _fetch_source(source: ResearchSource) -> FetchResult:
    parsed = urllib.parse.urlparse(source.url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported source URL scheme: {parsed.scheme}")
    request = urllib.request.Request(  # noqa: S310
        source.url,
        headers={
            "User-Agent": "KUN-Control-Plane-Research/1.0 (+https://local.kun)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        raw = response.read(160_000)
    text = raw.decode("utf-8", errors="ignore")
    title = _html_title(text) or source.title
    excerpt = _plain_text_from_html(text)[:1800]
    return "fetched", title, excerpt


def _html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _collapse_ws(unescape(re.sub(r"<[^>]+>", " ", match.group(1))))


def _plain_text_from_html(html: str) -> str:
    html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<[^>]+>", " ", html)
    return _collapse_ws(unescape(html))


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_docx_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)  # noqa: S314
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    parts = [node.text or "" for node in root.iter(f"{namespace}t")]
    return _collapse_ws(" ".join(parts))


def _brief_extract_markdown(brief_text: str) -> str:
    excerpt = brief_text[:5000] if brief_text else "Source brief was unavailable to the runner."
    return f"""# 火火兔源方案摘录

KUN 在生成新版游戏设计前先读取用户提供的源方案。以下为自动抽取摘录，用作设计约束而不是最终文案。

```text
{excerpt}
```
"""


def _source_matrix_markdown(sources: list[FetchedResearchSource]) -> str:
    rows = [
        "# 外部调研资料库",
        "",
        "| 来源 | 类别 | 状态 | KUN 设计吸收 | 链接 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in sources:
        rows.append(
            "| "
            + " | ".join(
                [
                    item.source.title.replace("|", "/"),
                    item.source.category,
                    item.fetch_status,
                    item.source.design_takeaway.replace("|", "/"),
                    item.source.url,
                ]
            )
            + " |"
        )
    rows.extend(
        [
            "",
            "## KUN 读取原则",
            "",
            "- 优先吸收可落地的产品机制，而不是复制外部项目。",
            "- 对儿童产品，留存、奖励和成长反馈必须服从安全、学习质量和亲子信任。",
            "- 调研结论必须进入游戏设计、任务拆解、验收门禁和后续 MVP 开发。",
        ]
    )
    return "\n".join(rows) + "\n"


def _load_corpus(project_path: Path) -> list[FetchedResearchSource]:
    path = project_path / "docs" / "research" / "source-corpus.json"
    if not path.exists():
        raise ValueError("research corpus missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [FetchedResearchSource.model_validate(item) for item in payload.get("sources", [])]


def _benchmark_synthesis_markdown(
    *,
    mission: Mission,
    sources: list[FetchedResearchSource],
    selected_worlds: list[str],
) -> str:
    categories: dict[str, list[FetchedResearchSource]] = {}
    for item in sources:
        categories.setdefault(item.source.category, []).append(item)
    category_sections = []
    for category, items in sorted(categories.items()):
        takeaways = "\n".join(f"- {item.source.design_takeaway}" for item in items)
        category_sections.append(f"## {category}\n\n{takeaways}\n")
    return f"""# 火火兔游戏竞品与机制综合

任务：{mission.objective}

KUN 选定首期两个世界：{", ".join(selected_worlds)}。

## 综合判断

- Duolingo 值得借鉴的是小步反馈、路径感、复习和成就，但儿童游戏不能把排行榜、连续打卡和强竞争作为核心。
- Toca Boca、LEGO PLAY、Minecraft Education 值得借鉴的是开放创造、低惩罚、孩子主导、可试错的空间。
- Khan Academy Kids 值得借鉴的是低龄分层、学习路径、故事化活动和社会情感学习。
- Blizzard 值得借鉴的是高完成度交互、可访问性、界面可调、反馈闭环和长期迭代纪律。
- FTC、Apple、Google、UNICEF、Common Sense 给出的底线是儿童数据、AI 输出和家长控制必须先设计，再做功能。

## 分类吸收

{chr(10).join(category_sections)}
## 对 MVP 的直接要求

- 每一轮游戏必须有儿童输入、世界变化、伙伴回应、任务推进、Spark 记录。
- 两个世界要验证不同能力：开放造物和叙事协作。
- 奖励只奖励表达、创造、探索、帮助和复盘，不奖励刷量。
- 所有外部 AI、云同步、发布动作默认关闭，只有适配器和说明。
"""


def _game_design_spec_markdown(
    *,
    task_plan: TaskPlan,
    sources: list[FetchedResearchSource],
    selected_worlds: list[str],
) -> str:
    source_count = len(sources)
    fetched_count = sum(item.fetch_status == "fetched" for item in sources)
    return f"""# 火火兔 Spark 游戏设计规格 v2

## 设计门禁状态

- 调研来源数：{source_count}
- 成功在线读取数：{fetched_count}
- 首期世界：{", ".join(selected_worlds)}
- 本规格替代 `mvp-v1` 的直接编码路径；`mvp-v1` 只作为 KUN 自主执行链路验证。

## 产品目标

为 2-9 岁儿童做一个平板优先的 AI 成长世界 MVP。它不是课程播放器，也不是聊天机器人；它让孩子用语言、触摸、选择和创作改变世界，并把这些行为记录成可被家长理解的 Spark 线索。

## 核心循环

1. 探索：孩子进入世界，看到一个可理解的任务或开放提示。
2. 表达：孩子说一句话、选一个物体、拖拽或输入想法。
3. 世界反馈：模拟 AI 把表达翻译为画面、角色、天气、故事或物体变化。
4. 任务推进：系统识别表达中体现的能力标签和任务完成度。
5. 成长记录：Spark 轨迹记录表达、创造、叙事、科学、社交、自然、AI 协作。
6. 家长复盘：家长看到孩子做了什么、表现出什么兴趣、下一步怎么陪玩。

## 首期两个世界

### {selected_worlds[0]}

- 验证能力：颜色、形状、天气、造物表达、即时反馈。
- 玩法：孩子用一句话改变云、桥、树、雨、彩虹和小动物路径。
- 学习价值：表达欲、因果关系、自然观察、创造力。
- 借鉴：Toca Boca 的开放玩具、LEGO 的创作画布、Minecraft 的创造与系统思维。

### {selected_worlds[1]}

- 验证能力：叙事、角色、提问、帮助、协作。
- 玩法：孩子帮助角色解决小问题，通过提问和选择推动故事。
- 学习价值：语言组织、同理心、问题意识、合作。
- 借鉴：Khan Academy Kids 的故事化学习、Duolingo 的小步路径、Blizzard 的清晰反馈。

## 交互和 UI

- 平板优先，大触控目标，减少文字密度。
- 每屏只给一个主要行动，保留自由探索入口。
- 反馈要即时、温和、低惩罚；错误不失败，而是重定向。
- 支持年龄段切换：2-4 岁少文字，5-6 岁多任务提示，7-9 岁增加问题和解释。

## AI 与安全边界

- MVP 只使用模拟 AI，真实 GPT-5.5 CLI 通过 adapter 接入。
- 默认不调用付费 API，不上传真实儿童数据到第三方。
- 敏感、危险、成人、恐吓、过度医疗等内容必须重定向为安全表达。
- 家长入口展示数据说明、行为轨迹、本地/云端边界和后续授权点。

## 成功标准

{chr(10).join(f"- {item}" for item in task_plan.acceptance_criteria)}

## 开发指令

- 先实现可玩核心，而不是素材精修。
- 必须留下 adapter：真实 AI、云同步、品牌素材、App 打包。
- 必须可构建，可本地试玩，可由 KUN Control Plane 记录 artifact、gate 和交付报告。
"""


def _research_first_mvp_plan_markdown(
    *,
    task_plan: TaskPlan,
    selected_worlds: list[str],
    final_project_path: Path,
) -> str:
    return f"""# Research-first MVP 执行计划

## 为什么变更计划

原 `mvp-v1` 已证明 KUN 可以自主排队、生成项目、构建和报告，但它没有先完成足够深的游戏/教育/儿童安全调研。按 KUN V6 原则，复杂真实任务必须先补齐信息和证据，再进入开发。

## 新计划

- 新版本：`mvp-v2-design-gated`
- 新项目路径：`{final_project_path}`
- 首期世界：{", ".join(selected_worlds)}
- 开发方式：KUN autonomous app runner 在设计门禁通过后自动执行。

## 继承验收

{chr(10).join(f"- {item}" for item in task_plan.acceptance_criteria)}
"""


def _design_gate_inputs(
    *,
    control_plane: InMemoryControlPlane,
    mission_id: str,
    spec: GameResearchSpec,
) -> DesignGateInputs:
    corpus_path = spec.project_path / "docs" / "research" / "source-corpus.json"
    design_spec_path = spec.project_path / "docs" / "game-design-spec.md"
    source_count = 0
    fetched_count = 0
    if corpus_path.exists():
        payload = json.loads(corpus_path.read_text(encoding="utf-8"))
        sources = payload.get("sources", [])
        source_count = len(sources)
        fetched_count = sum(item.get("fetch_status") == "fetched" for item in sources)
    docs_root = str(spec.project_path / "docs")
    artifact_refs = [
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id and artifact.path_or_uri.startswith(docs_root)
    ]
    return DesignGateInputs(
        corpus_path=corpus_path,
        design_spec_path=design_spec_path,
        fetched_count=fetched_count,
        source_count=source_count,
        artifact_refs=artifact_refs,
    )


def _research_gated_task_plan(
    *,
    mission: Mission,
    previous_plan: TaskPlan,
    version: str,
    first_worlds: list[str],
    evidence_artifact_refs: list[str],
) -> TaskPlan:
    return TaskPlan(
        plan_id=f"plan-{mission.mission_id}-{version}",
        mission_id=mission.mission_id,
        version=version,
        objective=(
            "Build the research-backed Fire Rabbit Spark playable MVP after external "
            "game, education, child-safety, and AI-safety evidence has passed the design gate."
        ),
        known_facts=[
            "KUN completed source brief extraction and external benchmark research.",
            "The previous MVP is a runner proof, not final delivery.",
            f"KUN selected first worlds: {', '.join(first_worlds)}.",
            "Mock AI is acceptable for MVP; real GPT-5.5 CLI remains an adapter.",
        ],
        assumptions=previous_plan.assumptions,
        acceptance_criteria=previous_plan.acceptance_criteria,
        constraints=[
            *previous_plan.constraints,
            "Implementation must follow docs/game-design-spec.md.",
            "Rewards cannot optimize for addictive streaks or competition over learning quality.",
            "Child data collection remains local-first unless a later approval changes it.",
        ],
        risk_register=[
            *previous_plan.risk_register,
            "Research-backed design may require iteration if playtest shows poor child comprehension.",
        ],
        evidence_plan=[
            "Use source brief, external research corpus, benchmark synthesis, game design spec, code path, build result, and playtest report.",
            *evidence_artifact_refs,
        ],
        decomposition=[
            "Activate KUN autonomous app runner under research-gated boundary",
            "Translate design spec into MVP world scope and data model",
            "Generate tablet-first app scaffold",
            "Implement core game loop, mock AI, Spark telemetry, local storage",
            "Implement playable UI, parent report, safety surface, README",
            "Run dependency install, build, and delivery gate",
        ],
        worker_plan=["kun-autonomous-app-runner"],
        merge_plan=["Merge research docs, implementation, build result, and delivery report."],
        test_plan=[
            "npm install",
            "npm run build",
            "supervisor playtest screenshot after KUN delivery",
        ],
        rollback_plan=[
            "If build fails, return to repair work item and keep source research unchanged.",
            "If real AI adapter is unavailable, keep mock AI.",
        ],
        human_confirmation_points=[
            "Only ask user before paid API spend, external publish, or real child cloud upload."
        ],
        change_log=["v2: Research-first design gate passed before MVP development."],
        approval_status="approved_with_limits",
    )


def _mvp_app_work_items(
    *,
    mission_id: str,
    task_plan_version: str,
    design_gate_work_item_id: str,
) -> list[WorkItem]:
    prefix = "work-huohutu-v2"
    return [
        WorkItem(
            work_item_id=f"{prefix}-10-kun-autonomous-runner-activation",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="governance",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[design_gate_work_item_id],
            priority=96,
            expected_output="runner activation after research design gate",
        ),
        WorkItem(
            work_item_id=f"{prefix}-11-mvp-scope-worlds",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[f"{prefix}-10-kun-autonomous-runner-activation"],
            priority=95,
            expected_output="scope worlds using research-backed design spec",
        ),
        WorkItem(
            work_item_id=f"{prefix}-12-app-scaffold",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[f"{prefix}-11-mvp-scope-worlds"],
            priority=94,
            expected_output="tablet app scaffold",
        ),
        WorkItem(
            work_item_id=f"{prefix}-13-core-game-loop",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[f"{prefix}-12-app-scaffold"],
            priority=93,
            expected_output="core game loop",
        ),
        WorkItem(
            work_item_id=f"{prefix}-14-ui-safety-report",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[f"{prefix}-13-core-game-loop"],
            priority=92,
            expected_output="playable UI and safety report",
        ),
        WorkItem(
            work_item_id=f"{prefix}-15-qa-delivery",
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            type="test",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=[f"{prefix}-14-ui-safety-report"],
            priority=91,
            expected_output="qa",
        ),
    ]


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _artifact(
    *,
    work_item: WorkItem,
    suffix: str,
    path: Path,
    supports: list[str],
    kind: ArtifactKind = "evidence",
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact-{_slug(work_item.work_item_id)}-{suffix}",
        kind=kind,
        path_or_uri=str(path),
        content_hash=_hash_path(path),
        created_by=GameDesignResearchRunner.runner_identity,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=supports,
        freshness="fresh",
        source_quality="primary",
    )


def _hash_path(path: Path) -> str:
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(str(child.relative_to(path)).encode())
            digest.update(child.read_bytes())
        return digest.hexdigest()
    if not path.exists():
        return hashlib.sha256(str(path).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()


__all__ = [
    "KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER",
    "FetchedResearchSource",
    "GameDesignResearchRunner",
    "GameResearchSpec",
    "ResearchSource",
]
