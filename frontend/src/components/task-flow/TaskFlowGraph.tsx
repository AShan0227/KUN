"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";

export type FlowStepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export type TaskFlowStep = {
  step_id: string;
  title: string;
  status: FlowStepStatus;
  deps: string[];
  input?: string;
  output?: string;
  cost_usd?: number;
  duration_ms?: number;
};

export type TaskFlowPayload = {
  task_id: string;
  title: string;
  status: string;
  steps: TaskFlowStep[];
};

type TaskNodeData = {
  step: TaskFlowStep;
  selected: boolean;
  onSelect: (step: TaskFlowStep) => void;
};

type ControlAction = "pause" | "skip" | "force_done";

const API_BASE =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";
const TENANT = "u-sylvan";
const USER = "sylvan";
const FETCH_HEADERS: HeadersInit = { "X-Tenant-Id": TENANT, "X-User-Id": USER };

const STATUS_LABEL: Record<FlowStepStatus, string> = {
  pending: "待执行",
  running: "执行中",
  done: "完成",
  failed: "失败",
  skipped: "跳过",
};

const STATUS_CLASS: Record<FlowStepStatus, string> = {
  pending: "border-gray-300 bg-white text-gray-700",
  running: "border-kun-accent bg-indigo-50 text-kun-accent",
  done: "border-emerald-300 bg-emerald-50 text-emerald-700",
  failed: "border-kun-bad bg-red-50 text-kun-bad",
  skipped: "border-amber-300 bg-amber-50 text-amber-700",
};

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...FETCH_HEADERS, ...(init?.headers || {}) },
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

function buildWsUrl(taskId: string): string {
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/tasks/${encodeURIComponent(taskId)}/events`;
}

function TaskStepNode({ data }: NodeProps<TaskNodeData>) {
  const { step, selected, onSelect } = data;
  return (
    <button
      type="button"
      onClick={() => onSelect(step)}
      className={`w-56 rounded border px-3 py-2 text-left shadow-sm transition ${
        STATUS_CLASS[step.status]
      } ${selected ? "ring-2 ring-kun-accent" : ""}`}
    >
      <Handle type="target" position={Position.Left} className="!bg-gray-400" />
      <p className="text-xs opacity-70">step {step.step_id}</p>
      <p className="mt-1 truncate text-sm font-medium">{step.title}</p>
      <p className="mt-2 text-xs">{STATUS_LABEL[step.status]}</p>
      <Handle type="source" position={Position.Right} className="!bg-gray-400" />
    </button>
  );
}

const nodeTypes = { taskStep: TaskStepNode };

export function TaskFlowGraph({ taskId }: { taskId: string }) {
  const [payload, setPayload] = useState<TaskFlowPayload | null>(null);
  const [selectedStep, setSelectedStep] = useState<TaskFlowStep | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<ControlAction | null>(null);

  const reload = useCallback(() => {
    fetchJson<TaskFlowPayload>(`/api/tasks/${encodeURIComponent(taskId)}`)
      .then((data) => {
        setPayload(data);
        setSelectedStep((current) => {
          if (!current) return data.steps[0] ?? null;
          return data.steps.find((step) => step.step_id === current.step_id) ?? data.steps[0] ?? null;
        });
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [taskId]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    const wsUrl = buildWsUrl(taskId);
    if (!wsUrl) return;
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        const frame = JSON.parse(event.data) as Partial<TaskFlowStep> & { type?: string };
        if (!frame.step_id || !frame.status) return;
        setPayload((current) => {
          if (!current) return current;
          return {
            ...current,
            steps: current.steps.map((step) =>
              step.step_id === frame.step_id
                ? {
                    ...step,
                    ...frame,
                    status: frame.status as FlowStepStatus,
                  }
                : step,
            ),
          };
        });
      };
    } catch {
      ws = null;
    }
    return () => ws?.close();
  }, [taskId]);

  const nodes = useMemo<Node<TaskNodeData>[]>(() => {
    const steps = payload?.steps ?? [];
    return steps.map((step, index) => ({
      id: step.step_id,
      type: "taskStep",
      position: { x: (index % 3) * 310, y: Math.floor(index / 3) * 150 },
      data: {
        step,
        selected: selectedStep?.step_id === step.step_id,
        onSelect: setSelectedStep,
      },
    }));
  }, [payload?.steps, selectedStep?.step_id]);

  const edges = useMemo<Edge[]>(() => {
    const steps = payload?.steps ?? [];
    const stepIds = new Set(steps.map((step) => step.step_id));
    return steps.flatMap((step, index) => {
      const deps = step.deps?.length ? step.deps : index > 0 ? [steps[index - 1].step_id] : [];
      return deps
        .filter((dep) => stepIds.has(dep))
        .map((dep) => ({
          id: `${dep}->${step.step_id}`,
          source: dep,
          target: step.step_id,
          markerEnd: { type: MarkerType.ArrowClosed },
          style: { stroke: "#94a3b8" },
        }));
    });
  }, [payload?.steps]);

  const control = useCallback(
    async (action: ControlAction) => {
      if (!selectedStep) return;
      const label =
        action === "pause" ? "暂停任务" : action === "skip" ? "跳过这一步" : "强制完成这一步";
      if (!window.confirm(`${label}？`)) return;
      setBusyAction(action);
      try {
        if (action === "pause") {
          await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/kill`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason: "user_flow_pause" }),
          });
        } else {
          await fetchJson(
            `/api/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(
              selectedStep.step_id,
            )}/skip`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                action,
                reason: `user_flow_${action}`,
              }),
            },
          );
        }
        reload();
      } catch (e) {
        setError(String(e));
      } finally {
        setBusyAction(null);
      }
    },
    [reload, selectedStep, taskId],
  );

  if (error) {
    return <div className="rounded border border-kun-bad bg-red-50 p-4 text-sm text-kun-bad">{error}</div>;
  }

  if (!payload) {
    return <div className="p-6 text-sm text-gray-500">加载任务图...</div>;
  }

  return (
    <div className="grid min-h-[calc(100vh-140px)] grid-cols-[1fr_320px] gap-4">
      <section className="overflow-hidden rounded border bg-white">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div>
            <h1 className="text-base font-semibold">{payload.title}</h1>
            <p className="text-xs text-gray-500">
              {payload.task_id} · {payload.status}
            </p>
          </div>
          <button
            type="button"
            onClick={reload}
            className="rounded border px-3 py-1 text-xs hover:border-kun-accent hover:text-kun-accent"
          >
            刷新
          </button>
        </div>
        <div className="h-[calc(100vh-220px)] min-h-[520px]">
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView>
            <Background gap={18} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      </section>

      <aside className="rounded border bg-white p-4 text-sm">
        <p className="text-xs text-gray-500">当前节点</p>
        {selectedStep ? (
          <div className="mt-2 space-y-4">
            <div>
              <h2 className="font-semibold">{selectedStep.title}</h2>
              <p className="mt-1 text-xs text-gray-500">
                step {selectedStep.step_id} · {STATUS_LABEL[selectedStep.status]}
              </p>
            </div>
            <Metric label="成本" value={`$${(selectedStep.cost_usd ?? 0).toFixed(4)}`} />
            <Metric label="耗时" value={`${Math.round(selectedStep.duration_ms ?? 0)}ms`} />
            <TextBlock label="输入" value={selectedStep.input || "暂无"} />
            <TextBlock label="输出" value={selectedStep.output || "暂无"} />
            <div className="grid grid-cols-3 gap-2">
              <ControlButton label="暂停" busy={busyAction === "pause"} onClick={() => control("pause")} />
              <ControlButton label="跳过" busy={busyAction === "skip"} onClick={() => control("skip")} />
              <ControlButton
                label="完成"
                busy={busyAction === "force_done"}
                onClick={() => control("force_done")}
              />
            </div>
          </div>
        ) : (
          <p className="mt-2 text-gray-500">点一个节点看详情。</p>
        )}
      </aside>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-gray-50 p-2">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="mt-1 font-medium">{value}</p>
    </div>
  );
}

function TextBlock({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <pre className="mt-1 max-h-32 overflow-auto rounded bg-gray-50 p-2 text-xs whitespace-pre-wrap">
        {value}
      </pre>
    </div>
  );
}

function ControlButton({
  label,
  busy,
  onClick,
}: {
  label: string;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      className="rounded bg-kun-accent px-2 py-2 text-xs text-white disabled:opacity-50"
    >
      {busy ? "处理中" : label}
    </button>
  );
}
