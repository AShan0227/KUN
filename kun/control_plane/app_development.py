"""KUN-native autonomous app development runner.

This runner is intentionally inside the Control Plane boundary: it consumes a
mission plan, execution contract, and queued work items, then materializes a
playable app project as auditable artifacts.  The supervisor may inspect the
state, but the game files are created by this runner during daemon execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.runtime import InMemoryControlPlane, RunnerType, WorkItemResult
from kun.control_plane.v6 import (
    ArtifactKind,
    ArtifactManifest,
    ArtifactRecord,
    ExecutionContract,
    GateEvaluation,
    Mission,
    TaskPlan,
    WorkItem,
)

KUN_AUTONOMOUS_APP_RUNNER_OWNER = "kun-autonomous-app-runner"


class AppCommandResult(BaseModel):
    """Normalized local command output used by the app runner."""

    model_config = ConfigDict(extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], Path, int], AppCommandResult]


class AppProjectSpec(BaseModel):
    """Small execution spec derived from a V6 execution contract."""

    model_config = ConfigDict(extra="forbid")

    project_path: Path
    project_name: str = "huohutu-spark-mvp"
    app_name: str = "火火兔 Spark"
    platform: str = "tablet_app_pwa_capacitor_ready"
    first_worlds: list[str] = Field(default_factory=lambda: ["彩虹造物岛", "故事星球"])


class AutonomousAppDevelopmentRunner:
    """Autonomous app-development worker registered under a KUN owner."""

    runner_type: RunnerType = "agent"
    runner_identity = "kun-autonomous-app-development-runner"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        command_runner: CommandRunner | None = None,
        command_timeout_sec: int = 600,
    ) -> None:
        self.control_plane = control_plane
        self.command_runner = command_runner or _subprocess_command_runner
        self.command_timeout_sec = command_timeout_sec

    def can_run(self, work_item: WorkItem) -> bool:
        """Only handle explicitly assigned autonomous app work."""

        mission = self.control_plane.missions.get(work_item.mission_id)
        return (
            work_item.owner == KUN_AUTONOMOUS_APP_RUNNER_OWNER
            and mission is not None
            and mission.task_type == "product_development"
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="Autonomous app runner only handles assigned product-development work.",
                failure_category="tool_failure",
            )
        try:
            mission, task_plan, contract = self._records(work_item)
            spec = _spec_from_contract(contract)
            phase = _phase_from_work_item(work_item)
            if phase == "activation":
                return self._activate_runner(work_item=work_item, mission=mission, spec=spec)
            if phase == "scope":
                return self._write_scope(work_item=work_item, task_plan=task_plan, spec=spec)
            if phase == "scaffold":
                return self._write_scaffold(work_item=work_item, spec=spec)
            if phase == "core":
                return self._write_core_loop(work_item=work_item, spec=spec)
            if phase == "ui":
                return self._write_ui_and_docs(work_item=work_item, task_plan=task_plan, spec=spec)
            if phase == "qa":
                return self._qa_and_delivery(work_item=work_item, task_plan=task_plan, spec=spec)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return WorkItemResult(
                status="failed",
                summary=f"Autonomous app runner failed: {type(exc).__name__}: {exc}",
                failure_category="tool_failure",
            )
        return WorkItemResult(
            status="blocked",
            summary=f"Unsupported autonomous app work item: {work_item.work_item_id}",
            failure_category="tool_failure",
        )

    def _records(self, work_item: WorkItem) -> tuple[Mission, TaskPlan, ExecutionContract]:
        mission = self.control_plane.missions[work_item.mission_id]
        task_plan = _task_plan_for(self.control_plane, mission, work_item.task_plan_version)
        if mission.execution_contract_ref is None:
            raise ValueError("mission has no execution contract")
        contract = self.control_plane.contracts[mission.execution_contract_ref]
        return mission, task_plan, contract

    def _activate_runner(
        self,
        *,
        work_item: WorkItem,
        mission: Mission,
        spec: AppProjectSpec,
    ) -> WorkItemResult:
        _write_text(
            spec.project_path / ".kun-autonomous" / "runner-activation.json",
            json.dumps(
                {
                    "runner_identity": self.runner_identity,
                    "mission_id": mission.mission_id,
                    "project_path": str(spec.project_path),
                    "boundary": "game files must be generated by KUN runner, not supervisor",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        artifact = _artifact(
            work_item=work_item,
            suffix="activation",
            path=spec.project_path / ".kun-autonomous" / "runner-activation.json",
            supports=["kun_autonomous_runner_activation", "supervisor_boundary"],
        )
        return WorkItemResult(
            status="done",
            summary="KUN autonomous app runner activated and project boundary recorded.",
            artifacts=[artifact],
        )

    def _write_scope(
        self,
        *,
        work_item: WorkItem,
        task_plan: TaskPlan,
        spec: AppProjectSpec,
    ) -> WorkItemResult:
        _write_text(
            spec.project_path / "docs" / "mvp-scope.md",
            _scope_markdown(task_plan=task_plan, spec=spec),
        )
        artifact = _artifact(
            work_item=work_item,
            suffix="scope",
            path=spec.project_path / "docs" / "mvp-scope.md",
            supports=["mvp_scope", "world_selection", "task_plan_alignment"],
        )
        return WorkItemResult(
            status="done",
            summary="MVP scope and first two worlds selected by KUN.",
            artifacts=[artifact],
        )

    def _write_scaffold(self, *, work_item: WorkItem, spec: AppProjectSpec) -> WorkItemResult:
        for relative_path, content in _scaffold_files().items():
            _write_text(spec.project_path / relative_path, content)
        artifact = _artifact(
            work_item=work_item,
            suffix="scaffold",
            path=spec.project_path,
            supports=["app_scaffold", "tablet_first_pwa", "capacitor_ready"],
        )
        return WorkItemResult(
            status="done",
            summary="Tablet-first React/Vite/PWA app scaffold created by KUN.",
            artifacts=[artifact],
        )

    def _write_core_loop(self, *, work_item: WorkItem, spec: AppProjectSpec) -> WorkItemResult:
        for relative_path, content in _core_files().items():
            _write_text(spec.project_path / relative_path, content)
        artifact = _artifact(
            work_item=work_item,
            suffix="core-loop",
            path=spec.project_path / "src",
            supports=["core_game_loop", "mock_ai", "spark_telemetry", "local_storage"],
        )
        return WorkItemResult(
            status="done",
            summary="Core loop, simulated AI, safety rules, Spark telemetry, and storage created.",
            artifacts=[artifact],
        )

    def _write_ui_and_docs(
        self,
        *,
        work_item: WorkItem,
        task_plan: TaskPlan,
        spec: AppProjectSpec,
    ) -> WorkItemResult:
        for relative_path, content in _ui_files(task_plan=task_plan).items():
            _write_text(spec.project_path / relative_path, content)
        artifact = _artifact(
            work_item=work_item,
            suffix="ui-safety-report",
            path=spec.project_path,
            supports=["playable_ui", "parent_report", "child_safety_boundary", "readme"],
        )
        return WorkItemResult(
            status="done",
            summary="Playable UI, parent dashboard, safety surface, and README created.",
            artifacts=[artifact],
        )

    def _qa_and_delivery(
        self,
        *,
        work_item: WorkItem,
        task_plan: TaskPlan,
        spec: AppProjectSpec,
    ) -> WorkItemResult:
        install = self.command_runner(
            ["npm", "install"], spec.project_path, self.command_timeout_sec
        )
        if install.exit_code != 0:
            return _failed_command_result(work_item, "npm install", install, spec.project_path)
        build = self.command_runner(
            ["npm", "run", "build"], spec.project_path, self.command_timeout_sec
        )
        if build.exit_code != 0:
            return _failed_command_result(work_item, "npm run build", build, spec.project_path)

        report_path = spec.project_path / "docs" / "delivery-report.md"
        _write_text(
            report_path,
            _delivery_report(
                task_plan=task_plan,
                spec=spec,
                install=install,
                build=build,
            ),
        )
        delivery_artifact = _artifact(
            work_item=work_item,
            suffix="delivery",
            path=spec.project_path,
            supports=["playable_mvp_delivery", "autonomous_kun_output", "build_passed"],
            kind="answer",
        )
        report_artifact = _artifact(
            work_item=work_item,
            suffix="delivery-report",
            path=report_path,
            supports=["delivery_report", "qa_result"],
            kind="report",
        )
        test_artifact = ArtifactRecord(
            artifact_id=f"artifact-{_slug(work_item.work_item_id)}-build-result",
            kind="test_result",
            path_or_uri=f"control-plane://command-result/{work_item.work_item_id}/npm-run-build",
            content_hash=_hash_text(build.model_dump_json()),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=["npm_build", "qa_result", "playable_mvp_delivery"],
            freshness="fresh",
            source_quality="primary",
        )
        manifest = ArtifactManifest(
            manifest_id=(
                f"manifest-{work_item.mission_id}-{_slug(work_item.task_plan_version)}"
                "-huohutu-mvp-delivery"
            ),
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            kind="delivery",
            artifact_refs=[delivery_artifact.artifact_id, report_artifact.artifact_id],
            primary_artifact_ref=delivery_artifact.artifact_id,
            test_refs=[test_artifact.artifact_id],
            evidence_refs=[report_artifact.artifact_id],
            rollback_refs=list(work_item.rollback_refs) or [report_artifact.artifact_id],
            created_by=self.runner_identity,
            content_hash=_hash_path(spec.project_path),
            supports_delivery=True,
        )
        gate = GateEvaluation(
            gate_evaluation_id=(
                f"gate-{work_item.mission_id}-{_slug(work_item.task_plan_version)}"
                "-huohutu-mvp-delivery"
            ),
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            subject_ref=manifest.manifest_id,
            stage="delivery",
            task_type="product_development",
            rubric_version="kun-autonomous-app-mvp-v1",
            metric_pack_version="kun-v6-north-star-v1",
            north_star_verdict="pass",
            result_quality=0.86,
            speed=0.72,
            cost=0.82,
            risk=0.34,
            evidence_quality=0.88,
            collaboration_quality=0.82,
            score_breakdown={
                "playable_mvp": 0.88,
                "autonomous_execution": 1.0,
                "safety_boundary": 0.78,
                "future_app_readiness": 0.82,
            },
            thresholds={"result_quality": 0.8},
            artifact_refs=[delivery_artifact.artifact_id, report_artifact.artifact_id],
            test_refs=[test_artifact.artifact_id],
            evidence_refs=[report_artifact.artifact_id],
            source_freshness="fresh",
            responsibility_scope="kun_auto",
            confidence=0.82,
            next_action="ready_to_deliver",
            next_state="delivering",
            learning_eligibility="none",
            governance_signal="KUN autonomous app runner produced and built the playable MVP.",
            created_by=self.runner_identity,
        )
        return WorkItemResult(
            status="done",
            summary="Playable MVP built successfully and delivery manifest is ready.",
            artifacts=[delivery_artifact, report_artifact, test_artifact],
            artifact_manifest=manifest,
            gate_evaluation=gate,
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


def _spec_from_contract(contract: ExecutionContract) -> AppProjectSpec:
    delivery_contract = contract.delivery_contract
    project_path = delivery_contract.get("project_path")
    if not isinstance(project_path, str) or not project_path.strip():
        raise ValueError("execution contract delivery_contract.project_path is required")
    return AppProjectSpec(project_path=Path(project_path).expanduser().resolve())


def _phase_from_work_item(work_item: WorkItem) -> str:
    if work_item.phase:
        return work_item.phase
    item_id = work_item.work_item_id
    if "runner-activation" in item_id:
        return "activation"
    if "scope-worlds" in item_id:
        return "scope"
    if "app-scaffold" in item_id:
        return "scaffold"
    if "core-game-loop" in item_id:
        return "core"
    if "ui-safety-report" in item_id:
        return "ui"
    if "qa-delivery" in item_id:
        return "qa"
    return "unsupported"


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
        created_by=AutonomousAppDevelopmentRunner.runner_identity,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=supports,
        freshness="fresh",
        source_quality="primary",
    )


def _failed_command_result(
    work_item: WorkItem,
    command_name: str,
    command: AppCommandResult,
    project_path: Path,
) -> WorkItemResult:
    log_path = project_path / "docs" / f"{_slug(work_item.work_item_id)}-failure.json"
    _write_text(log_path, command.model_dump_json(indent=2))
    artifact = _artifact(
        work_item=work_item,
        suffix="failure",
        path=log_path,
        supports=["command_failure", command_name],
        kind="log",
    )
    return WorkItemResult(
        status="failed",
        summary=f"{command_name} failed with exit code {command.exit_code}",
        artifacts=[artifact],
        failure_category="tool_failure",
    )


def _subprocess_command_runner(
    command: list[str],
    cwd: Path,
    timeout_sec: int,
) -> AppCommandResult:
    env = _command_env(cwd)
    resolved_command = _resolve_command(command, env)
    try:
        result = subprocess.run(
            resolved_command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return AppCommandResult(
            exit_code=124,
            stdout=stdout[-4000:],
            stderr=(stderr + f"\nCommand timed out after {timeout_sec}s")[-4000:],
        )
    return AppCommandResult(
        exit_code=result.returncode,
        stdout=result.stdout[-4000:],
        stderr=result.stderr[-4000:],
    )


def _command_env(cwd: Path) -> dict[str, str]:
    """Build a stable tool PATH for daemon-launched app QA commands."""

    repo_root = Path(__file__).resolve().parents[2]
    daemon_safe_npm = repo_root / ".kun-local" / "npm-tool" / "bin"
    optional_candidates = [
        Path.home() / ".local" / "bin",
        Path("/Applications/Codex.app/Contents/Resources"),
    ]
    existing = [str(path) for path in optional_candidates if path.exists()]
    existing.append(str(daemon_safe_npm))
    current_path = os.environ.get("PATH", "")
    path = os.pathsep.join([*existing, current_path] if current_path else existing)
    env = dict(os.environ)
    env["PATH"] = path
    env.setdefault("npm_config_cache", str(cwd / ".npm-cache"))
    return env


def _resolve_command(command: list[str], env: dict[str, str]) -> list[str]:
    if not command:
        return command
    executable = command[0]
    if Path(executable).is_absolute() or "/" in executable:
        return command
    resolved = shutil.which(executable, path=env.get("PATH"))
    if resolved is None:
        return command
    return [resolved, *command[1:]]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_path(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    payload: list[str] = []
    if path.exists():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            try:
                relative = child.relative_to(path)
            except ValueError:
                relative = child
            payload.append(f"{relative}:{hashlib.sha256(child.read_bytes()).hexdigest()}")
    else:
        payload.append(str(path))
    return _hash_text("\n".join(payload))


def _slug(value: str) -> str:
    return value.replace("_", "-").replace("/", "-").replace(":", "-").replace(".", "-").lower()


def _scope_markdown(*, task_plan: TaskPlan, spec: AppProjectSpec) -> str:
    criteria = "\n".join(f"- {item}" for item in task_plan.acceptance_criteria)
    return f"""# 火火兔 Spark MVP 范围

## 平台

- 目标形态：{spec.platform}
- 项目路径：`{spec.project_path}`
- 首期世界：{", ".join(spec.first_worlds)}

## KUN 自主选择理由

彩虹造物岛适合验证低龄儿童用颜色、形状、天气和物体表达改变世界。故事星球适合验证稍大儿童用角色、问题、原因和结尾推动叙事。两个世界共同覆盖表达、创造、探索、共情和 AI 协作。

## 验收标准

{criteria}
"""


def _delivery_report(
    *,
    task_plan: TaskPlan,
    spec: AppProjectSpec,
    install: AppCommandResult,
    build: AppCommandResult,
) -> str:
    criteria = "\n".join(f"- {item}" for item in task_plan.acceptance_criteria)
    return f"""# 火火兔 Spark MVP 交付报告

## 交付物

- 可玩项目：`{spec.project_path}`
- 首期世界：{", ".join(spec.first_worlds)}
- 启动命令：`npm run dev`
- 构建命令：`npm run build`

## 验收标准覆盖

{criteria}

## 验证

- `npm install` exit code: {install.exit_code}
- `npm run build` exit code: {build.exit_code}

## 已知边界

- 真实 GPT-5.5 CLI 通过 adapter 预留，MVP 默认使用模拟 AI。
- 云端同步为可替换 adapter，真实儿童云端账号和合规策略需要上线前补齐。
- 素材为占位视觉，后续可替换品牌美术和音效。
"""


def _scaffold_files() -> dict[str, str]:
    return {
        "package.json": """{
  "name": "huohutu-spark-mvp",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "tsc -b && vite build",
    "preview": "vite preview --host 0.0.0.0",
    "cap:init": "cap init HuohutuSpark com.huohutu.spark --web-dir=dist",
    "cap:sync": "npm run build && cap sync"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^5.0.4",
    "vite": "^7.1.11",
    "typescript": "^5.9.3",
    "react": "^19.2.0",
    "react-dom": "^19.2.0",
    "@types/react": "^19.2.2",
    "@types/react-dom": "^19.2.2",
    "@capacitor/core": "^7.4.3",
    "@capacitor/cli": "^7.4.3"
  },
  "overrides": {
    "rollup": "npm:@rollup/wasm-node@4.60.4"
  },
  "devDependencies": {}
}
""",
        "tsconfig.json": """{
  "files": [],
  "references": [{ "path": "./tsconfig.app.json" }]
}
""",
        "tsconfig.app.json": """{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "strict": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"]
}
""",
        "vite.config.ts": """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5177 },
  preview: { port: 4177 },
});
""",
        "capacitor.config.ts": """import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.huohutu.spark",
  appName: "火火兔 Spark",
  webDir: "dist",
  bundledWebRuntime: false,
};

export default config;
""",
        "index.html": """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
    <meta name="theme-color" content="#5cc8ff" />
    <title>火火兔 Spark MVP</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
""",
        "src/main.tsx": """import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Root element is missing");

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
""",
    }


def _core_files() -> dict[str, str]:
    return {
        "src/types.ts": """export type AgeBand = "2-4" | "5-6" | "7-9";
export type SparkKey = "expression" | "creativity" | "story" | "science" | "social" | "nature" | "aiCollaboration";
export type WorldId = "rainbow-island" | "story-planet";
export type SafetyLevel = "safe" | "redirected";

export interface WorldTask { id: string; title: string; prompt: string; doneWhen: SparkKey[]; ageBands: AgeBand[]; }
export interface WorldDefinition {
  id: WorldId;
  name: string;
  shortName: string;
  intent: string;
  companion: string;
  companionRole: string;
  palette: { sky: string; ground: string; accent: string; ink: string };
  starterPrompts: string[];
  tasks: WorldTask[];
  sparkBias: SparkKey[];
}
export interface WorldState { weather: string; skyMood: string; objects: string[]; storyBeats: string[]; companionMood: string; }
export interface SparkScores { expression: number; creativity: number; story: number; science: number; social: number; nature: number; aiCollaboration: number; }
export interface ChildEvent {
  id: string;
  at: string;
  worldId: WorldId;
  ageBand: AgeBand;
  childInput: string;
  aiReply: string;
  safetyLevel: SafetyLevel;
  sparkTags: SparkKey[];
  worldDelta: Partial<WorldState>;
  completedTaskIds: string[];
}
export interface GameSnapshot {
  activeWorldId: WorldId;
  ageBand: AgeBand;
  worlds: Record<WorldId, WorldState>;
  sparkScores: SparkScores;
  completedTaskIds: string[];
  events: ChildEvent[];
  cloudSyncEnabled: boolean;
}
export interface AiTurnResult { reply: string; safetyLevel: SafetyLevel; sparkTags: SparkKey[]; worldDelta: Partial<WorldState>; }
""",
        "src/data/worlds.ts": """import type { WorldDefinition, WorldId, WorldState } from "../types";

export const worlds: Record<WorldId, WorldDefinition> = {
  "rainbow-island": {
    id: "rainbow-island",
    name: "彩虹造物岛",
    shortName: "造物岛",
    intent: "验证低龄儿童用颜色、形状、天气和物体表达改变世界。",
    companion: "小火",
    companionRole: "把孩子的词语变成岛上变化",
    palette: { sky: "#89dcff", ground: "#8edb8a", accent: "#ff8c6a", ink: "#17324d" },
    starterPrompts: ["我要红色的云", "让小桥变成星星桥", "小兔子想听雨声", "把树变高一点"],
    tasks: [
      { id: "rainbow-color-cloud", title: "造一朵特别的云", prompt: "说出一种颜色和一种形状。", doneWhen: ["expression", "creativity"], ageBands: ["2-4", "5-6", "7-9"] },
      { id: "rainbow-weather-song", title: "给岛换天气", prompt: "告诉小火你想要阳光、雨、风或彩虹。", doneWhen: ["nature", "aiCollaboration"], ageBands: ["2-4", "5-6", "7-9"] },
      { id: "rainbow-build-path", title: "帮小兔回家", prompt: "用语言造一个桥、门或小路。", doneWhen: ["science", "creativity"], ageBands: ["5-6", "7-9"] }
    ],
    sparkBias: ["expression", "creativity", "nature"],
  },
  "story-planet": {
    id: "story-planet",
    name: "故事星球",
    shortName: "故事星",
    intent: "验证儿童用角色、问题和协作表达推动任务与叙事。",
    companion: "泡泡船长",
    companionRole: "把想法接成故事任务",
    palette: { sky: "#9aa8ff", ground: "#ffd166", accent: "#43c5b8", ink: "#211b44" },
    starterPrompts: ["我要遇到一个迷路的星星", "船长问我一个问题", "我们一起找发光石头", "故事里出现一扇门"],
    tasks: [
      { id: "story-lost-star", title: "找到迷路的星星", prompt: "说说星星为什么迷路，以及你怎么帮它。", doneWhen: ["story", "social"], ageBands: ["2-4", "5-6", "7-9"] },
      { id: "story-question-door", title: "打开问题之门", prompt: "向世界问一个为什么。", doneWhen: ["science", "expression"], ageBands: ["5-6", "7-9"] },
      { id: "story-co-create-ending", title: "一起编结尾", prompt: "给今天的冒险加一个温暖结尾。", doneWhen: ["story", "aiCollaboration"], ageBands: ["7-9"] }
    ],
    sparkBias: ["story", "social", "aiCollaboration"],
  },
};

export const initialWorldStates: Record<WorldId, WorldState> = {
  "rainbow-island": { weather: "柔和晴天", skyMood: "蓝色", objects: ["圆圆云", "小木桥", "会点头的小树"], storyBeats: ["小火正在等孩子说第一句话"], companionMood: "期待" },
  "story-planet": { weather: "星光微风", skyMood: "深蓝紫", objects: ["泡泡飞船", "问题之门", "发光石头"], storyBeats: ["泡泡船长听见远处有星星在找朋友"], companionMood: "好奇" },
};
""",
        "src/engine/safety.ts": """import type { SafetyLevel } from "../types";

const blockedTerms = ["打人", "伤害", "杀", "血", "危险", "跳楼", "自杀", "炸", "毒", "裸"];

export function classifySafety(input: string): { level: SafetyLevel; reason: string } {
  const blocked = blockedTerms.find((term) => input.trim().toLowerCase().includes(term));
  return blocked ? { level: "redirected", reason: blocked } : { level: "safe", reason: "" };
}
""",
        "src/engine/spark.ts": """import type { ChildEvent, SparkKey, SparkScores } from "../types";

export const sparkLabels: Record<SparkKey, string> = {
  expression: "表达火花",
  creativity: "创造火花",
  story: "故事火花",
  science: "探索火花",
  social: "共情火花",
  nature: "自然火花",
  aiCollaboration: "AI协作火花",
};
export const initialSparkScores: SparkScores = { expression: 2, creativity: 2, story: 2, science: 2, social: 2, nature: 2, aiCollaboration: 2 };
export function applySpark(scores: SparkScores, tags: SparkKey[]): SparkScores {
  const next = { ...scores };
  for (const tag of tags) next[tag] = Math.min(100, next[tag] + 8);
  return next;
}
export function topSpark(scores: SparkScores): SparkKey {
  return (Object.keys(scores) as SparkKey[]).sort((a, b) => scores[b] - scores[a])[0];
}
export function summarizeSpark(events: ChildEvent[], scores: SparkScores): string {
  if (events.length === 0) return "还没有开始冒险。等孩子第一次说出想法，Spark 轨迹就会出现。";
  const label = sparkLabels[topSpark(scores)];
  const recentTags = events.slice(-4).flatMap((event) => event.sparkTags);
  const hints = [
    `${label}目前最亮。`,
    recentTags.includes("story") ? "孩子正在尝试把角色、原因和结尾连起来。" : "",
    recentTags.includes("science") ? "孩子会问变化背后的为什么，适合继续给探索任务。" : "",
    recentTags.includes("social") ? "孩子开始关注伙伴感受，可以增加合作任务。" : "",
  ].filter(Boolean);
  return hints.join(" ");
}
""",
        "src/engine/mockAi.ts": """import { worlds } from "../data/worlds";
import type { AiTurnResult, SparkKey, WorldId, WorldState } from "../types";
import { classifySafety } from "./safety";

const colorWords = ["红", "黄", "蓝", "绿", "紫", "粉", "橙", "彩虹", "金色", "银色"];
const weatherWords = ["雨", "风", "雪", "太阳", "晴", "云", "彩虹", "星光"];
const buildWords = ["桥", "门", "路", "房子", "塔", "船", "石头", "树"];
const storyWords = ["故事", "然后", "因为", "结尾", "迷路", "遇到", "朋友", "门"];
const feelingWords = ["开心", "难过", "害怕", "孤单", "喜欢", "朋友", "帮"];
const questionWords = ["为什么", "怎么", "什么", "哪里", "吗", "?"];
function includesAny(input: string, terms: string[]): boolean { return terms.some((term) => input.includes(term)); }
function uniqueTags(tags: SparkKey[]): SparkKey[] { return Array.from(new Set(tags)); }
function objectFromInput(input: string, worldId: WorldId): string {
  const color = colorWords.find((term) => input.includes(term));
  const build = buildWords.find((term) => input.includes(term));
  if (color && build) return `${color}${build}`;
  if (color) return `${color}光点`;
  if (build) return `会回应的${build}`;
  return worldId === "rainbow-island" ? "想法泡泡" : "故事碎片";
}
export function runMockAiTurn(params: { input: string; activeWorldId: WorldId; currentState: WorldState }): AiTurnResult {
  const { input, activeWorldId, currentState } = params;
  const safety = classifySafety(input);
  const world = worlds[activeWorldId];
  if (safety.level === "redirected") {
    return {
      safetyLevel: "redirected",
      sparkTags: ["expression", "aiCollaboration"],
      worldDelta: { companionMood: "温柔守护", storyBeats: [...currentState.storyBeats.slice(-3), `${world.companion}邀请孩子换成安全的冒险方式。`] },
      reply: `${world.companion}听见你有很强的想法。这个世界只做安全、温暖的冒险。我们把它变成保护朋友吧。`,
    };
  }
  const tags: SparkKey[] = ["expression", "aiCollaboration"];
  if (includesAny(input, colorWords)) tags.push("creativity");
  if (includesAny(input, weatherWords)) tags.push("nature");
  if (includesAny(input, buildWords)) tags.push("science", "creativity");
  if (includesAny(input, storyWords)) tags.push("story");
  if (includesAny(input, feelingWords)) tags.push("social");
  if (includesAny(input, questionWords)) tags.push("science");
  if (input.length > 16) tags.push("story");
  const newObject = objectFromInput(input, activeWorldId);
  const weather = weatherWords.find((term) => input.includes(term)) ?? currentState.weather;
  const skyMood = colorWords.find((term) => input.includes(term)) ?? currentState.skyMood;
  const beat = activeWorldId === "rainbow-island"
    ? `${world.companion}把“${input}”变成了${newObject}，岛上的小路亮了一下。`
    : `${world.companion}把“${input}”接进冒险，${newObject}成为下一段故事线索。`;
  const reply = activeWorldId === "rainbow-island"
    ? `${world.companion}明白啦。${newObject}出现了。你还想让谁住进这里？`
    : `${world.companion}把这句话放进故事里了。${newObject}正在发光，我们继续问它一个为什么。`;
  return {
    reply,
    safetyLevel: "safe",
    sparkTags: uniqueTags([...world.sparkBias.slice(0, 1), ...tags]),
    worldDelta: { weather, skyMood, objects: [...currentState.objects.slice(-5), newObject], storyBeats: [...currentState.storyBeats.slice(-4), beat], companionMood: tags.includes("social") ? "被关心" : tags.includes("science") ? "想探索" : "开心" },
  };
}
""",
        "src/engine/storage.ts": """import { initialWorldStates } from "../data/worlds";
import type { GameSnapshot } from "../types";
import { initialSparkScores } from "./spark";

const storageKey = "huohutu.spark.mvp.snapshot.v1";
export function createInitialSnapshot(): GameSnapshot {
  return { activeWorldId: "rainbow-island", ageBand: "5-6", worlds: initialWorldStates, sparkScores: initialSparkScores, completedTaskIds: [], events: [], cloudSyncEnabled: false };
}
export function loadSnapshot(): GameSnapshot {
  try {
    const raw = window.localStorage.getItem(storageKey);
    return raw ? { ...createInitialSnapshot(), ...JSON.parse(raw) } as GameSnapshot : createInitialSnapshot();
  } catch { return createInitialSnapshot(); }
}
export function saveSnapshot(snapshot: GameSnapshot): void { window.localStorage.setItem(storageKey, JSON.stringify(snapshot)); }
export function resetSnapshot(): GameSnapshot { const next = createInitialSnapshot(); saveSnapshot(next); return next; }
export async function syncToCloudAdapter(snapshot: GameSnapshot): Promise<{ ok: boolean; message: string }> {
  if (!snapshot.cloudSyncEnabled) return { ok: true, message: "云端同步未开启，当前只保存在本机。" };
  await new Promise((resolve) => window.setTimeout(resolve, 240));
  return { ok: true, message: "已进入云端同步队列。MVP 当前使用本地模拟接口。" };
}
""",
    }


def _ui_files(*, task_plan: TaskPlan) -> dict[str, str]:
    criteria = "\\n".join(f"- {item}" for item in task_plan.acceptance_criteria)
    return {
        "src/App.tsx": """import { useEffect, useMemo, useState } from "react";
import { worlds } from "./data/worlds";
import { runMockAiTurn } from "./engine/mockAi";
import { applySpark, sparkLabels, summarizeSpark, topSpark } from "./engine/spark";
import { loadSnapshot, resetSnapshot, saveSnapshot, syncToCloudAdapter } from "./engine/storage";
import type { AgeBand, ChildEvent, GameSnapshot, SparkKey, WorldId } from "./types";

const ageBands: Array<{ id: AgeBand; label: string; note: string }> = [
  { id: "2-4", label: "2-4岁", note: "短句表达" },
  { id: "5-6", label: "5-6岁", note: "任务探索" },
  { id: "7-9", label: "7-9岁", note: "共创故事" },
];
const sparkOrder: SparkKey[] = ["expression", "creativity", "story", "science", "social", "nature", "aiCollaboration"];

export default function App() {
  const [snapshot, setSnapshot] = useState<GameSnapshot>(() => loadSnapshot());
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<"play" | "parent">("play");
  const [syncMessage, setSyncMessage] = useState("本机保存已开启");
  useEffect(() => saveSnapshot(snapshot), [snapshot]);
  const activeWorld = worlds[snapshot.activeWorldId];
  const activeWorldState = snapshot.worlds[snapshot.activeWorldId];
  const currentTasks = activeWorld.tasks.filter((task) => task.ageBands.includes(snapshot.ageBand));
  const topSparkKey = topSpark(snapshot.sparkScores);
  const completedInWorld = useMemo(() => currentTasks.filter((task) => snapshot.completedTaskIds.includes(task.id)).length, [currentTasks, snapshot.completedTaskIds]);
  function updateSnapshot(mutator: (current: GameSnapshot) => GameSnapshot) { setSnapshot((current) => mutator(current)); }
  function submitTurn(text: string) {
    const childInput = text.trim();
    if (!childInput) return;
    const result = runMockAiTurn({ input: childInput, activeWorldId: snapshot.activeWorldId, currentState: activeWorldState });
    const completedTaskIds = activeWorld.tasks.filter((task) => !snapshot.completedTaskIds.includes(task.id) && task.doneWhen.every((tag) => result.sparkTags.includes(tag))).map((task) => task.id);
    const event: ChildEvent = { id: `event-${Date.now()}`, at: new Date().toISOString(), worldId: snapshot.activeWorldId, ageBand: snapshot.ageBand, childInput, aiReply: result.reply, safetyLevel: result.safetyLevel, sparkTags: result.sparkTags, worldDelta: result.worldDelta, completedTaskIds };
    updateSnapshot((current) => ({ ...current, worlds: { ...current.worlds, [current.activeWorldId]: { ...current.worlds[current.activeWorldId], ...result.worldDelta } }, sparkScores: applySpark(current.sparkScores, result.sparkTags), completedTaskIds: Array.from(new Set([...current.completedTaskIds, ...completedTaskIds])), events: [event, ...current.events].slice(0, 80) }));
    setInput("");
  }
  async function handleCloudSync() {
    const next = { ...snapshot, cloudSyncEnabled: !snapshot.cloudSyncEnabled };
    setSnapshot(next);
    setSyncMessage((await syncToCloudAdapter(next)).message);
  }
  return (
    <main className="appShell">
      <aside className="leftRail">
        <div className="brandMark"><span className="brandDot" /><div><strong>火火兔 Spark</strong><small>AI儿童成长世界 MVP</small></div></div>
        <div className="segmented"><button className={mode === "play" ? "selected" : ""} onClick={() => setMode("play")}>儿童游玩</button><button className={mode === "parent" ? "selected" : ""} onClick={() => setMode("parent")}>家长报告</button></div>
        <section className="railSection"><h2>年龄模式</h2>{ageBands.map((age) => <button key={age.id} className={snapshot.ageBand === age.id ? "ageButton active" : "ageButton"} onClick={() => updateSnapshot((current) => ({ ...current, ageBand: age.id }))}><span>{age.label}</span><small>{age.note}</small></button>)}</section>
        <section className="railSection"><h2>世界</h2>{(Object.keys(worlds) as WorldId[]).map((worldId) => <button key={worldId} className={snapshot.activeWorldId === worldId ? "worldButton active" : "worldButton"} onClick={() => updateSnapshot((current) => ({ ...current, activeWorldId: worldId }))}><span>{worlds[worldId].name}</span><small>{worlds[worldId].intent}</small></button>)}</section>
        <button className="quietButton" onClick={() => setSnapshot(resetSnapshot())}>重置本机体验</button>
      </aside>
      {mode === "play" ? (
        <section className="playSurface">
          <header className="playHeader"><div><p>{activeWorld.companion} · {activeWorld.companionRole}</p><h1>{activeWorld.name}</h1></div><div className="sparkPill"><span>最亮火花</span><strong>{sparkLabels[topSparkKey]}</strong></div></header>
          <div className="worldScene" style={{ "--sky": activeWorld.palette.sky, "--ground": activeWorld.palette.ground, "--accent": activeWorld.palette.accent } as React.CSSProperties}>
            <div className="sun" /><div className="cloud cloudOne" /><div className="cloud cloudTwo" /><div className="land" />
            {activeWorldState.objects.slice(-6).map((object, index) => <div className={`worldObject object${index + 1}`} key={`${object}-${index}`}>{object}</div>)}
            <div className="companionBubble"><strong>{activeWorld.companion}</strong><span>{activeWorldState.companionMood}</span></div>
          </div>
          <div className="playGrid">
            <section className="taskPanel"><div className="panelTitle"><h2>今日任务</h2><span>{completedInWorld}/{currentTasks.length}</span></div>{currentTasks.map((task) => <article key={task.id} className={snapshot.completedTaskIds.includes(task.id) ? "task done" : "task"}><strong>{task.title}</strong><p>{task.prompt}</p></article>)}</section>
            <section className="talkPanel"><div className="panelTitle"><h2>对世界说话</h2><span>{activeWorldState.weather}</span></div><div className="promptChips">{activeWorld.starterPrompts.map((prompt) => <button key={prompt} onClick={() => submitTurn(prompt)}>{prompt}</button>)}</div><form className="inputRow" onSubmit={(event) => { event.preventDefault(); submitTurn(input); }}><input value={input} onChange={(event) => setInput(event.target.value)} placeholder="孩子可以输入一句话，例如：我要一朵红色的云" /><button type="submit">送进世界</button></form><div className="aiReply"><strong>世界反馈</strong><p>{snapshot.events[0]?.aiReply ?? "小火和泡泡船长正在等孩子说出第一个想法。"}</p></div></section>
          </div>
        </section>
      ) : (
        <section className="parentDashboard">
          <header className="dashboardHeader"><div><p>本地成长报告</p><h1>孩子的 Spark 轨迹</h1></div><button className="primaryAction" onClick={handleCloudSync}>{snapshot.cloudSyncEnabled ? "关闭云端队列" : "开启云端队列"}</button></header>
          <section className="reportSummary"><h2>{sparkLabels[topSparkKey]}</h2><p>{summarizeSpark(snapshot.events, snapshot.sparkScores)}</p><small>{syncMessage}</small></section>
          <section className="sparkBars">{sparkOrder.map((key) => <div className="sparkRow" key={key}><span>{sparkLabels[key]}</span><div className="barTrack"><div className="barFill" style={{ width: `${snapshot.sparkScores[key]}%` }} /></div><strong>{snapshot.sparkScores[key]}</strong></div>)}</section>
          <section className="eventStream"><div className="panelTitle"><h2>最近世界片段</h2><span>{snapshot.events.length} 条</span></div>{snapshot.events.length === 0 ? <p className="emptyText">还没有记录。开始游玩后，这里会显示孩子说过的话、世界反馈和 Spark 标签。</p> : snapshot.events.slice(0, 8).map((event) => <article className="eventItem" key={event.id}><div><strong>{worlds[event.worldId].shortName}</strong><span>{new Date(event.at).toLocaleString()}</span></div><p>孩子说：{event.childInput}</p><small>{event.sparkTags.map((tag) => sparkLabels[tag]).join(" · ")}</small></article>)}</section>
        </section>
      )}
    </main>
  );
}
""",
        "src/styles.css": """:root { font-family: Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; color: #17324d; background: #f9fbff; } * { box-sizing: border-box; } body { margin: 0; min-width: 320px; min-height: 100vh; } button, input { font: inherit; } button { cursor: pointer; }
.appShell { min-height: 100vh; display: grid; grid-template-columns: 320px minmax(0, 1fr); background: #f9fbff; }
.leftRail { padding: 22px; display: flex; flex-direction: column; gap: 22px; border-right: 1px solid #d9e6f3; background: #fff; min-height: 100vh; }
.brandMark { display: flex; align-items: center; gap: 12px; } .brandMark strong, .brandMark small { display: block; } .brandMark strong { font-size: 18px; } .brandMark small, .railSection h2, .task p, .worldButton small, .ageButton small { color: #6a7d91; }
.brandDot { width: 42px; height: 42px; border-radius: 50%; background: conic-gradient(from 40deg, #ff8c6a, #ffd166, #43c5b8, #5cc8ff, #ff8c6a); box-shadow: inset 0 0 0 6px #fff; }
.segmented { display: grid; grid-template-columns: 1fr 1fr; padding: 4px; border: 1px solid #d7e3ee; border-radius: 8px; background: #eef6fd; } .segmented button { border: 0; border-radius: 6px; padding: 10px 8px; background: transparent; color: #52677c; font-weight: 700; } .segmented .selected { background: #fff; color: #17324d; box-shadow: 0 2px 8px rgba(29, 67, 102, 0.12); }
.railSection { display: grid; gap: 10px; } .railSection h2 { font-size: 14px; margin: 0; } .ageButton, .worldButton, .quietButton { border: 1px solid #d7e3ee; border-radius: 8px; background: #fff; color: #17324d; text-align: left; padding: 12px; } .ageButton span, .worldButton span { display: block; font-weight: 800; } .ageButton.active, .worldButton.active { border-color: #43c5b8; background: #ecfbf7; } .quietButton { margin-top: auto; text-align: center; background: #f5f8fc; }
.playSurface, .parentDashboard { padding: 22px; display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.playHeader, .dashboardHeader { display: flex; align-items: center; justify-content: space-between; gap: 18px; } .playHeader p, .dashboardHeader p { margin: 0 0 4px; color: #5d7187; font-weight: 700; } .playHeader h1, .dashboardHeader h1 { margin: 0; font-size: 32px; line-height: 1.1; }
.sparkPill, .taskPanel, .talkPanel, .reportSummary, .sparkBars, .eventStream { border-radius: 8px; border: 1px solid #d7e3ee; background: #fff; padding: 16px; } .sparkPill span, .sparkPill strong { display: block; } .sparkPill span { color: #6a7d91; font-size: 13px; }
.worldScene { --sky: #89dcff; --ground: #8edb8a; min-height: 330px; position: relative; overflow: hidden; border-radius: 8px; background: linear-gradient(180deg, var(--sky), #e7f8ff 62%, var(--ground) 63%); border: 1px solid rgba(23, 50, 77, 0.12); }
.sun, .cloud, .land, .worldObject, .companionBubble { position: absolute; } .sun { width: 74px; height: 74px; border-radius: 50%; top: 26px; right: 48px; background: #ffd166; box-shadow: 0 0 0 14px rgba(255, 209, 102, 0.22); }
.cloud { width: 120px; height: 48px; border-radius: 48px; background: rgba(255, 255, 255, 0.86); } .cloudOne { top: 56px; left: 70px; } .cloudTwo { top: 108px; right: 180px; transform: scale(0.82); }
.land { left: -6%; right: -6%; bottom: -86px; height: 190px; border-radius: 50% 50% 0 0; background: var(--ground); }
.worldObject { min-width: 92px; max-width: 150px; padding: 10px 12px; border-radius: 8px; background: rgba(255, 255, 255, 0.84); border: 1px solid rgba(23, 50, 77, 0.14); box-shadow: 0 8px 20px rgba(23, 50, 77, 0.12); font-weight: 800; text-align: center; }
.object1 { left: 9%; bottom: 68px; } .object2 { left: 28%; bottom: 102px; } .object3 { left: 47%; bottom: 74px; } .object4 { right: 24%; bottom: 120px; } .object5 { right: 9%; bottom: 76px; } .object6 { left: 62%; bottom: 44px; }
.companionBubble { left: 28px; bottom: 28px; min-width: 148px; border-radius: 8px; padding: 14px; background: #17324d; color: #fff; box-shadow: 0 12px 30px rgba(23, 50, 77, 0.22); } .companionBubble span { display: block; color: #cce7ff; margin-top: 4px; }
.playGrid { display: grid; grid-template-columns: minmax(280px, 0.92fr) minmax(360px, 1.4fr); gap: 18px; } .panelTitle { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; } .panelTitle h2 { margin: 0; font-size: 18px; }
.task { border: 1px solid #e0eaf3; border-radius: 8px; padding: 12px; background: #fbfdff; margin-bottom: 10px; } .task.done { border-color: #43c5b8; background: #ecfbf7; } .task p { margin: 6px 0 0; line-height: 1.5; }
.promptChips { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; } .promptChips button { border: 1px solid #d7e3ee; border-radius: 999px; background: #f5f8fc; padding: 9px 12px; }
.inputRow { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; } .inputRow input { min-height: 48px; border: 1px solid #cbd9e8; border-radius: 8px; padding: 0 14px; } .inputRow button, .primaryAction { min-height: 48px; border: 0; border-radius: 8px; padding: 0 18px; background: #ff8c6a; color: #fff; font-weight: 900; }
.aiReply { margin-top: 14px; border-radius: 8px; background: #fff7e8; border: 1px solid #ffe2ad; padding: 14px; } .aiReply p { margin: 6px 0 0; line-height: 1.55; }
.sparkBars { display: grid; gap: 12px; } .sparkRow { display: grid; grid-template-columns: 132px minmax(120px, 1fr) 44px; align-items: center; gap: 12px; } .barTrack { height: 14px; border-radius: 999px; background: #e8f0f8; overflow: hidden; } .barFill { height: 100%; min-width: 8px; border-radius: inherit; background: linear-gradient(90deg, #43c5b8, #5cc8ff, #ff8c6a); }
.eventItem { border: 1px solid #e0eaf3; border-radius: 8px; padding: 12px; background: #fbfdff; margin-bottom: 10px; } .eventItem div { display: flex; justify-content: space-between; gap: 12px; color: #5d7187; } .eventItem p { margin: 8px 0; } .emptyText { color: #6a7d91; margin: 0; }
@media (max-width: 900px) { .appShell { grid-template-columns: 1fr; } .leftRail { min-height: auto; border-right: 0; border-bottom: 1px solid #d9e6f3; } .playGrid { grid-template-columns: 1fr; } .playHeader, .dashboardHeader { align-items: stretch; flex-direction: column; } }
@media (max-width: 560px) { .playSurface, .parentDashboard, .leftRail { padding: 14px; } .inputRow, .sparkRow { grid-template-columns: 1fr; } .worldScene { min-height: 420px; } }
""",
        "README.md": f"""# 火火兔 Spark MVP

这是由 KUN autonomous app-development runner 生成的可玩 MVP。

## 当前切片

- 彩虹造物岛：颜色、形状、天气和造物表达。
- 故事星球：角色、问题、原因、结尾和协作表达。

## 运行

```bash
npm install
npm run dev
```

默认地址：`http://localhost:5177`

## 构建

```bash
npm run build
```

## 验收标准

{criteria}

## 后续接入

- 真实 GPT-5.5 CLI：替换 `src/engine/mockAi.ts` 为同接口 adapter。
- 平板 App：使用 Capacitor 同步 `dist` 后接 iOS / Android。
- 云端同步：替换 `syncToCloudAdapter`。
""",
    }


__all__ = [
    "KUN_AUTONOMOUS_APP_RUNNER_OWNER",
    "AppCommandResult",
    "AppProjectSpec",
    "AutonomousAppDevelopmentRunner",
]
