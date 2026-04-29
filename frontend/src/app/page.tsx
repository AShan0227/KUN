"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * KUN 主工作区 — 对话框主入口 (ADR-010).
 *
 * - 双通道: main (对话) + side (cost_tick / insight / surprise / alert).
 * - 纠偏即说: 用户输入里出现 "不是这样" 等词自动被 server 识别为 correction.
 * - 费用实时展示: cost_tick 块累计显示.
 */

type Msg = {
  kind:
    | "user"
    | "thinking"
    | "action_plan"
    | "action"
    | "answer"
    | "error"
    | "correction_ack";
  text: string;
  at: string;
};

type WireMessage = Record<string, unknown> & {
  type?: string;
};

type SideMsg = {
  kind:
    | "cost_tick"
    | "insight"
    | "surprise"
    | "alert"
    | "guard_intervention"
    | "idle_batch_report"
    | "scorecard";
  payload: WireMessage;
  at: string;
};

type GraphNeighbor = {
  entity_kind: string;
  entity_id: string;
  relation_type: string;
  confidence: number;
  hops: number;
  score: number;
};

const API_ORIGIN =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";

const WS_URL = (() => {
  if (typeof window === "undefined") return "";
  // 优先用 NEXT_PUBLIC_API_ORIGIN (跨 origin 部署时), 否则同源
  const base = API_ORIGIN || `${window.location.protocol}//${window.location.host}`;
  const proto = base.startsWith("https") ? "wss:" : "ws:";
  const host = base.replace(/^https?:\/\//, "");
  return `${proto}//${host}/ws?tenant_id=u-sylvan&user_id=sylvan`;
})();

type QiStatus = {
  window_active: boolean;
  daily_limit_usd: number;
  spent_today_usd: number;
  remaining_usd: number;
};

type Protocol = {
  protocol_id: string;
  version: string;
  status: string;
  trigger: { task_type_pattern: string };
  execution: { mode: string };
  created_by: string;
};

type LedgerEntry = {
  task_id: string;
  tenant_id?: string;
  user_id?: string;
  title?: string;
  task_type?: string;
  status: string;
  current_goal: string;
  current_action?: string;
  current_step: number;
  total_steps: number;
  current_risk: string;
  execution_mode: string;
  strategy_pack_id?: string | null;
  decision_reason?: string | null;
  current_model?: string | null;
  current_skill?: string | null;
  budget_estimated_usd?: number;
  cost_so_far_usd: number;
  tokens_so_far?: number;
  pending_confirmations: string[];
  recent_events?: LedgerTrail[];
  updated_at?: string;
};

type LedgerTrail = {
  at?: string;
  kind?: string;
  summary?: string;
  data?: Record<string, unknown>;
};

type GlobalState = {
  task_count_running: number;
  task_count_queued: number;
  total_cost_today_usd: number;
  health_indicator: string;
  urgent_alert_count: number;
  active_state_ledger: LedgerEntry[];
};

type MissionSnapshot = {
  mission_id: string;
  title: string;
  objective: string;
  status: string;
  risk_level: string;
  budget_cap_usd: number;
  tasks: Array<{
    task_id: string;
    role: string;
    sequence_no: number;
    status: string;
    resume_attempts: number;
    last_resume_requested_at?: string | null;
  }>;
  milestones: Array<{ milestone_id: string; title: string; status: string }>;
  updated_at: string;
};

type MissionResumeResult = {
  mission_id: string;
  task_id: string;
  status: string;
  reason: string;
  outcome?: {
    executed_task_id?: string | null;
    final_status: string;
    answer_preview: string;
  } | null;
};

type PendingAction = {
  action_id: string;
  task_ref: string;
  action_type: string;
  target_ref: string;
  status: string;
  risk_level: string;
  payload: Record<string, unknown>;
  gateway_preview?: GatewayPreview | null;
  created_at: string;
};

type GatewayPreview = {
  gateway_mode?: string;
  capability_status?: string;
  external_dispatched?: boolean;
  requires_handler?: boolean;
  rendered_payload?: string;
  user_summary?: string;
  next_step?: string;
  permissions_required?: string[];
  message?: string;
  audit?: { handler_id?: string; relative_path?: string; artifact_kind?: string; error?: string };
};

type PendingActionPage = {
  actions: PendingAction[];
};

type TaskDetail = {
  rendered_for?: string;
  task_id: string;
  state_ledger?: LedgerEntry | null;
  workspace?: {
    artifacts?: Array<Record<string, unknown>>;
    handoff_packets?: Array<Record<string, unknown>>;
    last_update?: string;
  } | null;
  assets?: Record<string, unknown> | null;
  events?: Array<{
    event_id?: string;
    event_type?: string;
    occurred_at?: string;
    summary?: string;
    severity?: string;
  }>;
  rendered_at?: string;
};

export default function Home() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [side, setSide] = useState<SideMsg[]>([]);
  const [input, setInput] = useState("");
  const [graphKind, setGraphKind] = useState("task");
  const [graphId, setGraphId] = useState("");
  const [graphNeighbors, setGraphNeighbors] = useState<GraphNeighbor[]>([]);
  const [graphError, setGraphError] = useState("");
  const [connected, setConnected] = useState(false);
  const [totalCost, setTotalCost] = useState(0);
  const [qiStatus, setQiStatus] = useState<QiStatus | null>(null);
  const [protocols, setProtocols] = useState<Protocol[]>([]);
  const [globalState, setGlobalState] = useState<GlobalState | null>(null);
  const [missions, setMissions] = useState<MissionSnapshot[]>([]);
  const [missionBusy, setMissionBusy] = useState(false);
  const [missionNotice, setMissionNotice] = useState("");
  const [pendingActions, setPendingActions] = useState<PendingAction[]>([]);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
  const [taskDetailLoading, setTaskDetailLoading] = useState(false);
  const [taskDetailError, setTaskDetailError] = useState("");
  const wsRef = useRef<WebSocket | null>(null);

  const refreshDashboard = useCallback(async (cancelledRef?: { current: boolean }) => {
    try {
      const [qiRes, protoRes] = await Promise.all([
        fetch(`${API_ORIGIN}/api/qi/status`, {
          headers: { "X-Tenant-Id": "u-sylvan" },
        }).catch(() => null),
        fetch(`${API_ORIGIN}/api/protocols?tenant=u-sylvan`).catch(() => null),
      ]);
      if (cancelledRef?.current) return;
      if (qiRes && qiRes.ok) {
        const data = await qiRes.json();
        setQiStatus(data as QiStatus);
      }
      if (protoRes && protoRes.ok) {
        const data = await protoRes.json();
        setProtocols(data as Protocol[]);
      }
      const stateRes = await fetch(`${API_ORIGIN}/api/blackboard/state`, {
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      }).catch(() => null);
      if (!cancelledRef?.current && stateRes && stateRes.ok) {
        setGlobalState((await stateRes.json()) as GlobalState);
      }
      const missionRes = await fetch(`${API_ORIGIN}/api/missions?limit=5`, {
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      }).catch(() => null);
      if (!cancelledRef?.current && missionRes && missionRes.ok) {
        setMissions((await missionRes.json()) as MissionSnapshot[]);
      }
      const actionRes = await fetch(`${API_ORIGIN}/nuo/actions/pending?limit=3`, {
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      }).catch(() => null);
      if (!cancelledRef?.current && actionRes && actionRes.ok) {
        const page = (await actionRes.json()) as PendingActionPage;
        setPendingActions(page.actions ?? []);
      }
    } catch {
      // ignore polling errors
    }
  }, []);

  // V2.3 启状态 + 协议轮询 (每 30s 一次)
  useEffect(() => {
    const cancelledRef = { current: false };
    void refreshDashboard(cancelledRef);
    const id = setInterval(() => void refreshDashboard(cancelledRef), 30_000);
    return () => {
      cancelledRef.current = true;
      clearInterval(id);
    };
  }, [refreshDashboard]);

  useEffect(() => {
    if (!WS_URL) return;
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WireMessage;
        dispatchIncoming(msg);
      } catch {
        console.warn("bad ws frame", e.data);
      }
    };
    return () => ws.close();
  }, []);

  const dispatchIncoming = (msg: WireMessage) => {
    const at = new Date().toISOString();
    const type = msg.type;
    switch (type) {
      case "thinking":
      case "action_plan":
      case "action":
      case "answer":
      case "error":
      case "correction_ack":
        setMessages((m) => [
          ...m,
          { kind: type, text: formatMain(msg), at },
        ]);
        break;
      case "cost_tick":
        setTotalCost((t) => t + numberValue(msg.cost_usd_equivalent));
        setSide((s) => [...s, { kind: "cost_tick", payload: msg, at }]);
        break;
      case "insight":
      case "surprise":
      case "alert":
      case "guard_intervention":
      case "idle_batch_report":
      case "scorecard":
        setSide((s) => [...s, { kind: type, payload: msg, at }]);
        break;
      case "done":
        // no-op; covered by answer
        break;
      default:
        console.debug("unhandled msg", msg);
    }
  };

  const send = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const content = input.trim();
    if (!content) return;
    ws.send(JSON.stringify({ type: "user_message", content }));
    setMessages((m) => [
      ...m,
      { kind: "user", text: content, at: new Date().toISOString() },
    ]);
    setInput("");
  }, [input]);

  const decidePendingAction = useCallback(
    async (actionId: string, decision: "approve" | "reject") => {
      setActionBusy(actionId);
      try {
        const res = await fetch(`${API_ORIGIN}/nuo/actions/${actionId}/decision`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tenant-Id": "u-sylvan",
            "X-User-Id": "sylvan",
          },
          body: JSON.stringify({ decision }),
        });
        const payload = (await res.json().catch(() => ({}))) as WireMessage;
        if (!res.ok) throw new Error(JSON.stringify(payload));
        setSide((items) => [
          ...items,
          {
            kind: "guard_intervention",
            payload: {
              type: "pending_action_decision",
              action_id: actionId,
              decision,
              ...payload,
            },
            at: new Date().toISOString(),
          },
        ]);
        await refreshDashboard();
      } catch (err) {
        setSide((items) => [
          ...items,
          {
            kind: "alert",
            payload: {
              type: "pending_action_decision_failed",
              action_id: actionId,
              message: err instanceof Error ? err.message : "审批动作失败",
            },
            at: new Date().toISOString(),
          },
        ]);
      } finally {
        setActionBusy(null);
      }
    },
    [refreshDashboard],
  );

  const runMissionResume = useCallback(async () => {
    setMissionBusy(true);
    setMissionNotice("");
    try {
      const res = await fetch(`${API_ORIGIN}/api/missions/resume-worker/run-once?limit=5`, {
        method: "POST",
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      });
      const payload = (await res.json().catch(() => [])) as MissionResumeResult[];
      if (!res.ok) throw new Error(JSON.stringify(payload));
      const completed = payload.filter((item) => item.status === "completed").length;
      const failed = payload.filter((item) => item.status === "failed").length;
      const skipped = payload.filter((item) => item.status === "skipped").length;
      setMissionNotice(
        `推进 ${payload.length} 个任务：完成 ${completed}，失败 ${failed}，跳过 ${skipped}`,
      );
      await refreshDashboard();
    } catch (err) {
      setMissionNotice(err instanceof Error ? err.message : "Mission 推进失败");
    } finally {
      setMissionBusy(false);
    }
  }, [refreshDashboard]);

  const loadTaskDetail = useCallback(async (taskId: string) => {
    const id = taskId.trim();
    if (!id) return;
    setSelectedTaskId(id);
    setTaskDetailLoading(true);
    setTaskDetailError("");
    try {
      const res = await fetch(`${API_ORIGIN}/api/blackboard/full/${encodeURIComponent(id)}`, {
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      });
      if (!res.ok) throw new Error(await res.text());
      setTaskDetail((await res.json()) as TaskDetail);
    } catch (err) {
      setTaskDetail(null);
      setTaskDetailError(err instanceof Error ? err.message : "任务详情加载失败");
    } finally {
      setTaskDetailLoading(false);
    }
  }, []);

  const loadGraph = useCallback(async () => {
    const kind = graphKind.trim();
    const id = graphId.trim();
    if (!kind || !id) return;
    setGraphError("");
    try {
      const params = new URLSearchParams({
        source_kind: kind,
        source_id: id,
        hops: "1",
      });
      const res = await fetch(`/api/graph/relationships?${params.toString()}`, {
        headers: {
          "X-Tenant-Id": "u-sylvan",
          "X-User-Id": "sylvan",
        },
      });
      if (!res.ok) throw new Error(await res.text());
      const data = (await res.json()) as GraphNeighbor[];
      setGraphNeighbors(data);
    } catch (err) {
      setGraphNeighbors([]);
      setGraphError(err instanceof Error ? err.message : "关系图查询失败");
    }
  }, [graphId, graphKind]);

  const activeLedger = globalState?.active_state_ledger ?? [];
  const ledgerPendingCount = activeLedger.reduce(
    (sum, item) => sum + item.pending_confirmations.length,
    0,
  );
  const pendingDecisionCount = Math.max(ledgerPendingCount, pendingActions.length);

  return (
    <div className="grid grid-cols-[1fr_360px] gap-4 p-4 h-full">
      {/* Main channel */}
      <section className="bg-white rounded-lg shadow-sm flex flex-col min-h-[calc(100vh-100px)]">
        <header className="px-4 py-2 border-b text-sm text-gray-600 flex justify-between">
          <span>主通道 · 对话 + 任务看板</span>
          <span>
            {connected ? (
              <span className="text-kun-good">● 已连接</span>
            ) : (
              <span className="text-kun-bad">● 未连接</span>
            )}
          </span>
        </header>
        <div className="border-b bg-gray-50 px-4 py-3">
          <div className="grid grid-cols-4 gap-2 text-xs">
            <MiniCard
              label="运行中"
              value={String(globalState?.task_count_running ?? 0)}
              hint={`排队 ${globalState?.task_count_queued ?? 0}`}
            />
            <MiniCard
              label="今日成本"
              value={`$${(globalState?.total_cost_today_usd ?? totalCost).toFixed(4)}`}
              hint="真实执行口径"
            />
            <MiniCard
              label="风险"
              value={globalState?.health_indicator ?? "unknown"}
              hint={`告警 ${globalState?.urgent_alert_count ?? 0}`}
            />
            <MiniCard
              label="待确认"
              value={String(pendingDecisionCount)}
              hint="需要你拍板"
            />
          </div>
          <div className="mt-3 space-y-2">
            {pendingActions.length > 0 && (
              <div className="rounded border border-amber-200 bg-amber-50 p-2 text-xs">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-medium text-amber-900">待确认动作</span>
                  <span className="text-amber-700">{pendingActions.length} 个</span>
                </div>
                <div className="space-y-2">
                  {pendingActions.map((action) => (
                    <div
                      key={action.action_id}
                      className="rounded border border-amber-100 bg-white px-2 py-1.5"
                    >
                      <div className="flex justify-between gap-2">
                        <span className="truncate font-medium">
                          {action.action_type} → {action.target_ref || action.task_ref}
                        </span>
                        <span className="text-amber-700">{action.risk_level}</span>
                      </div>
                      <div className="mt-1 truncate text-gray-500">任务 {action.task_ref}</div>
                      {action.gateway_preview && (
                        <div className="mt-1 text-gray-500">
                          <div className="flex items-center gap-2">
                            <span className="truncate">
                              网关：{gatewayPreviewLabel(action.gateway_preview)}
                            </span>
                            <span
                              className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${gatewayCapabilityClass(
                                action.gateway_preview,
                              )}`}
                            >
                              {gatewayCapabilityLabel(action.gateway_preview)}
                            </span>
                          </div>
                          {action.gateway_preview.next_step && (
                            <div className="truncate text-gray-400">
                              下一步：{action.gateway_preview.next_step}
                            </div>
                          )}
                        </div>
                      )}
                      <div className="mt-2 flex gap-2">
                        <button
                          className="rounded border border-green-200 bg-green-50 px-2 py-1 text-green-700 disabled:opacity-50"
                          disabled={actionBusy === action.action_id}
                          onClick={() => void decidePendingAction(action.action_id, "approve")}
                        >
                          批准
                        </button>
                        <button
                          className="rounded border border-red-200 bg-red-50 px-2 py-1 text-red-700 disabled:opacity-50"
                          disabled={actionBusy === action.action_id}
                          onClick={() => void decidePendingAction(action.action_id, "reject")}
                        >
                          拒绝
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {missions.length > 0 && (
              <div className="rounded border border-gray-200 bg-white p-2 text-xs">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-medium">长期目标</span>
                  <button
                    className="rounded border border-gray-200 bg-gray-50 px-2 py-1 text-gray-700 disabled:opacity-50"
                    disabled={missionBusy}
                    onClick={() => void runMissionResume()}
                  >
                    {missionBusy ? "推进中" : `推进一次 · ${missions.length}`}
                  </button>
                </div>
                {missionNotice && (
                  <div className="mb-2 rounded border border-gray-100 bg-gray-50 px-2 py-1 text-gray-600">
                    {missionNotice}
                  </div>
                )}
                <div className="space-y-2">
                  {missions.slice(0, 3).map((mission) => (
                    <div
                      key={mission.mission_id}
                      className="rounded border border-gray-100 bg-gray-50 px-2 py-1.5"
                    >
                      <div className="flex justify-between gap-2">
                        <span className="truncate font-medium">{mission.title}</span>
                        <span className={missionStatusClass(mission.status)}>
                          {mission.status}
                        </span>
                      </div>
                      <div className="mt-1 truncate text-gray-500">
                        风险 {mission.risk_level} · 预算 ${mission.budget_cap_usd.toFixed(2)} ·
                        任务 {mission.tasks.length} · 里程碑 {mission.milestones.length}
                      </div>
                      {mission.tasks.length > 0 && (
                        <div className="mt-2 grid grid-cols-2 gap-1">
                          {mission.tasks.slice(0, 4).map((task) => (
                            <div
                              key={task.task_id}
                              className="rounded border border-white bg-white px-2 py-1"
                            >
                              <div className="flex items-center justify-between gap-1">
                                <span className="truncate text-gray-600">{task.role}</span>
                                <span className={missionStatusClass(task.status)}>
                                  {task.status}
                                </span>
                              </div>
                              <div className="mt-0.5 truncate text-[11px] text-gray-400">
                                {task.task_id} · 尝试 {task.resume_attempts}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
            {activeLedger.slice(0, 3).map((item) => (
              <button
                key={item.task_id}
                className={`w-full rounded border bg-white p-2 text-left text-xs ${
                  selectedTaskId === item.task_id ? "border-kun-accent" : "border-gray-200"
                }`}
                onClick={() => void loadTaskDetail(item.task_id)}
              >
                <div className="flex justify-between gap-2">
                  <span className="truncate font-medium">{item.current_goal || item.task_id}</span>
                  <span className="text-gray-500">{item.status}</span>
                </div>
                <div className="mt-1 text-gray-500">
                  第 {item.current_step}/{item.total_steps || 1} 步 · 风险 {item.current_risk} ·{" "}
                  {item.execution_mode}
                  {item.strategy_pack_id ? ` · 策略 ${item.strategy_pack_id}` : ""}
                </div>
                <div className="mt-1 truncate text-gray-500">
                  {item.current_model || "未选模型"}
                  {item.current_skill ? ` · ${item.current_skill}` : ""} · $
                  {item.cost_so_far_usd.toFixed(4)}
                </div>
                {item.decision_reason && (
                  <div className="mt-1 truncate text-gray-400">{item.decision_reason}</div>
                )}
              </button>
            ))}
            {(selectedTaskId || taskDetailLoading || taskDetailError) && (
              <TaskDetailPanel
                detail={taskDetail}
                loading={taskDetailLoading}
                error={taskDetailError}
                selectedTaskId={selectedTaskId}
                pendingActions={pendingActions.filter(
                  (action) => action.task_ref === selectedTaskId,
                )}
              />
            )}
            {globalState && activeLedger.length === 0 && (
              <div className="rounded border border-dashed border-gray-200 bg-white p-2 text-xs text-gray-500">
                现在没有活跃任务。你可以直接在下面给鲲一个目标。
              </div>
            )}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-2 text-sm">
          {messages.map((m, i) => (
            <div
              key={i}
              className={
                m.kind === "user"
                  ? "text-right"
                  : m.kind === "answer"
                    ? "font-medium"
                    : "text-gray-500"
              }
            >
              <span className="text-xs text-gray-400 mr-2">[{m.kind}]</span>
              {m.text}
            </div>
          ))}
        </div>
        <footer className="border-t p-2 flex gap-2">
          <input
            className="flex-1 border rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-kun-accent"
            placeholder="和鲲说点什么..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button
            className="bg-kun-accent text-white px-4 py-2 rounded text-sm"
            onClick={send}
          >
            发送
          </button>
        </footer>
      </section>

      {/* Side channel */}
      <aside className="bg-kun-side/70 rounded-lg p-3 flex flex-col text-sm space-y-3 overflow-y-auto max-h-[calc(100vh-100px)]">
        <div className="font-medium flex justify-between">
          <span>侧通道 · 系统</span>
          <span className="text-kun-accent">累计 ${totalCost.toFixed(4)}</span>
        </div>
        <div className="flex-1 space-y-2">
          {/* V2.3: 启 (Qi) 状态卡 + toggle 按钮 */}
          <div className="bg-white rounded p-2 border border-gray-200 text-xs space-y-1">
            <div className="font-medium flex justify-between">
              <span>🌙 启 (Qi) 状态</span>
              {qiStatus ? (
                qiStatus.window_active ? (
                  <span className="text-kun-good">● 活跃</span>
                ) : (
                  <span className="text-gray-500">○ 窗口外</span>
                )
              ) : (
                <span className="text-gray-400">加载中...</span>
              )}
            </div>
            {qiStatus && (
              <>
                <div className="text-gray-500 space-y-0.5">
                  <div>今日花费: ${qiStatus.spent_today_usd.toFixed(4)}</div>
                  <div>
                    剩余预算: ${qiStatus.remaining_usd.toFixed(2)} / $
                    {qiStatus.daily_limit_usd.toFixed(2)}
                  </div>
                </div>
                <div className="flex gap-1 pt-1">
                  {qiStatus.window_active ? (
                    <button
                      className="border rounded px-2 py-0.5 hover:bg-gray-50 flex-1"
                      onClick={async () => {
                        await fetch(`${API_ORIGIN}/api/qi/release`, { method: "POST" });
                        const r = await fetch(`${API_ORIGIN}/api/qi/status`);
                        if (r.ok) setQiStatus(await r.json());
                      }}
                    >
                      关闭
                    </button>
                  ) : (
                    <button
                      className="border rounded px-2 py-0.5 hover:bg-blue-50 bg-blue-50/30 flex-1"
                      onClick={async () => {
                        await fetch(`${API_ORIGIN}/api/qi/force_active`, { method: "POST" });
                        const r = await fetch(`${API_ORIGIN}/api/qi/status`);
                        if (r.ok) setQiStatus(await r.json());
                      }}
                    >
                      强制启动
                    </button>
                  )}
                  <button
                    className="border rounded px-2 py-0.5 hover:bg-gray-50"
                    title="跑一次 Darwin 探索 (30 秒, 真调 LLM)"
                    onClick={async () => {
                      const r = await fetch(`${API_ORIGIN}/api/qi/trigger_explore`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ job: "darwin" }),
                      });
                      const data = await r.json();
                      alert(`Darwin 探索完成: ${JSON.stringify(data)}`);
                      // 刷新协议库
                      const pr = await fetch(`${API_ORIGIN}/api/protocols?tenant=u-sylvan`);
                      if (pr.ok) setProtocols(await pr.json());
                    }}
                  >
                    🔬 跑探索
                  </button>
                </div>
              </>
            )}
          </div>

          {/* V2.3: 协议库卡片 */}
          <div className="bg-white rounded p-2 border border-gray-200 text-xs space-y-1">
            <div className="font-medium flex justify-between">
              <span>📜 协议库 ({protocols.length})</span>
              <span className="text-gray-400">stable+experimental</span>
            </div>
            {protocols.length === 0 && (
              <p className="text-gray-500">没有协议. 跑 `kun protocol list` 自动 seed.</p>
            )}
            <div className="space-y-1 max-h-40 overflow-y-auto">
              {protocols.map((p) => (
                <div
                  key={`${p.protocol_id}@${p.version}`}
                  className="border-t pt-1"
                >
                  <div className="font-medium truncate">
                    {p.protocol_id}
                    <span className="text-gray-400 ml-1">@{p.version}</span>
                  </div>
                  <div className="text-gray-500 flex justify-between">
                    <span>
                      {p.status === "stable" ? "🟢" : "🟡"} {p.status}
                    </span>
                    <span>{p.execution.mode}</span>
                    <span className="text-gray-400">
                      {p.created_by === "qi" ? "🌙 涌现" : "🌱 seed"}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white rounded p-2 border border-gray-200 text-xs space-y-2">
            <div className="font-medium">关系图</div>
            <div className="grid grid-cols-[88px_1fr] gap-2">
              <input
                className="border rounded px-2 py-1"
                value={graphKind}
                onChange={(e) => setGraphKind(e.target.value)}
                aria-label="关系源类型"
              />
              <input
                className="border rounded px-2 py-1"
                placeholder="source id"
                value={graphId}
                onChange={(e) => setGraphId(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void loadGraph();
                }}
                aria-label="关系源 ID"
              />
            </div>
            <button
              className="border rounded px-2 py-1 text-xs hover:bg-gray-50"
              onClick={() => void loadGraph()}
            >
              查询邻接
            </button>
            {graphError && <p className="text-kun-bad">{graphError.slice(0, 140)}</p>}
            {graphNeighbors.slice(0, 6).map((n, i) => (
              <div key={`${n.entity_kind}:${n.entity_id}:${i}`} className="border-t pt-1">
                <div className="font-medium">{n.entity_kind}:{n.entity_id}</div>
                <div className="text-gray-500">
                  {n.relation_type} · {n.confidence.toFixed(2)}
                </div>
              </div>
            ))}
          </div>
          {side.length === 0 && (
            <p className="text-xs text-gray-500">系统消息会出现在这里 (费用 / 洞察 / 告警 / 批处理报告).</p>
          )}
          {side.map((s, i) => (
            <div
              key={i}
              className="bg-white rounded p-2 border border-gray-200 text-xs"
            >
              <div className="font-medium">
                {ICONS[s.kind]} {s.kind}
              </div>
              <pre className="text-[11px] text-gray-600 mt-1 overflow-x-auto">
                {JSON.stringify(s.payload, null, 2).slice(0, 400)}
              </pre>
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}

const ICONS: Record<string, string> = {
  cost_tick: "💰",
  insight: "💡",
  surprise: "✨",
  alert: "⚠️",
  guard_intervention: "🛡️",
  idle_batch_report: "🌙",
  scorecard: "📊",
};

function TaskDetailPanel({
  detail,
  loading,
  error,
  selectedTaskId,
  pendingActions,
}: {
  detail: TaskDetail | null;
  loading: boolean;
  error: string;
  selectedTaskId: string;
  pendingActions: PendingAction[];
}) {
  const ledger = detail?.state_ledger ?? null;
  const artifacts = detail?.workspace?.artifacts ?? [];
  const trails = ledger?.recent_events ?? [];

  return (
    <div className="rounded border border-kun-accent/30 bg-blue-50/30 p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="font-medium">任务详情</div>
          <div className="mt-0.5 text-gray-500">{selectedTaskId}</div>
        </div>
        {ledger && <span className={missionStatusClass(ledger.status)}>{ledger.status}</span>}
      </div>

      {loading && <div className="mt-3 text-gray-500">加载任务状态...</div>}
      {error && <div className="mt-3 text-red-700">{error.slice(0, 220)}</div>}

      {ledger && (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <DetailCell label="目标" value={ledger.current_goal || ledger.title || "未记录"} />
          <DetailCell label="当前动作" value={ledger.current_action || "暂无"} />
          <DetailCell
            label="进度"
            value={`第 ${ledger.current_step}/${ledger.total_steps || 1} 步`}
          />
          <DetailCell
            label="预算"
            value={`已用 $${ledger.cost_so_far_usd.toFixed(4)} / 预估 $${numberValue(
              ledger.budget_estimated_usd,
            ).toFixed(4)}`}
          />
          <DetailCell label="风险" value={ledger.current_risk || "unknown"} />
          <DetailCell
            label="模型/Skill"
            value={`${ledger.current_model || "未记录"}${
              ledger.current_skill ? ` · ${ledger.current_skill}` : ""
            }`}
          />
          <DetailCell label="执行模式" value={ledger.execution_mode || "未记录"} />
          <DetailCell
            label="待确认"
            value={
              ledger.pending_confirmations.length > 0
                ? ledger.pending_confirmations.join("，")
                : pendingActions.length > 0
                  ? `${pendingActions.length} 个待审批动作`
                  : "暂无"
            }
          />
        </div>
      )}

      {ledger?.decision_reason && (
        <div className="mt-3 rounded bg-white p-2 text-gray-600">
          <span className="font-medium text-gray-700">为什么这么做：</span>
          {ledger.decision_reason}
        </div>
      )}

      {pendingActions.length > 0 && (
        <div className="mt-3 rounded bg-amber-50 p-2 text-amber-900">
          <div className="font-medium">等你确认</div>
          {pendingActions.map((action) => (
            <div key={action.action_id} className="mt-1">
              {action.action_type} → {action.target_ref || action.task_ref}（{action.risk_level}）
            </div>
          ))}
        </div>
      )}

      {trails.length > 0 && (
        <div className="mt-3">
          <div className="font-medium">最近动作</div>
          <div className="mt-1 space-y-1">
            {trails.slice(0, 5).map((event, idx) => (
              <div key={`${event.kind}-${idx}`} className="rounded bg-white px-2 py-1 text-gray-600">
                <span className="text-gray-400">{event.kind || "event"}</span>
                <span className="mx-1">·</span>
                <span>{event.summary || "无摘要"}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {artifacts.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-gray-600">查看产物 / 工作区</summary>
          <pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-[11px] text-gray-600">
            {JSON.stringify(artifacts.slice(0, 6), null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

function DetailCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-white px-2 py-1">
      <div className="text-gray-400">{label}</div>
      <div className="mt-0.5 truncate text-gray-700">{value}</div>
    </div>
  );
}

function gatewayPreviewLabel(preview: GatewayPreview) {
  if (preview.user_summary) return preview.user_summary;
  if (preview.gateway_mode === "preview_failed") return "预览失败";
  if (preview.requires_handler) return "没有执行器，只审计";
  if (preview.external_dispatched) return "批准后会执行受控本地动作";
  return "批准后只生成草稿 / dry-run";
}

function gatewayCapabilityLabel(preview: GatewayPreview) {
  if (preview.capability_status === "preview_failed") return "先检查";
  if (preview.capability_status === "missing_handler" || preview.requires_handler) return "只审计";
  if (preview.capability_status === "supported_execute") return "会执行";
  if (preview.capability_status === "supported_draft") return "草稿";
  if (preview.capability_status === "supported_dry_run") return "dry-run";
  if (preview.capability_status === "supported_plan") return "计划";
  return "待确认";
}

function gatewayCapabilityClass(preview: GatewayPreview) {
  if (preview.capability_status === "preview_failed") return "bg-red-50 text-red-700";
  if (preview.capability_status === "missing_handler" || preview.requires_handler) {
    return "bg-gray-100 text-gray-600";
  }
  if (preview.capability_status === "supported_execute") return "bg-green-50 text-green-700";
  return "bg-blue-50 text-blue-700";
}

function MiniCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="rounded border border-gray-200 bg-white px-3 py-2">
      <div className="text-gray-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-gray-900">{value}</div>
      <div className="mt-1 text-gray-400">{hint}</div>
    </div>
  );
}

function formatMain(msg: WireMessage): string {
  if (msg.type === "thinking") return `思考中... (${stringValue(msg.stage)})`;
  if (msg.type === "action_plan")
    return `类型 ${stringValue(msg.task_type)} / 风险 ${stringValue(msg.risk_level)} / 预估 $${numberValue(msg.estimated_cost_usd).toFixed(4)}`;
  if (msg.type === "action")
    return `执行步骤 ${stringValue(msg.step_id)}: ${stringValue(msg.description)}`;
  if (msg.type === "answer") return stringValue(msg.content);
  if (msg.type === "error") return `错误: ${stringValue(msg.message)}`;
  if (msg.type === "correction_ack") return `(已确认纠偏)`;
  return JSON.stringify(msg);
}

function stringValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function missionStatusClass(status: string): string {
  if (status === "done") return "shrink-0 text-green-700";
  if (status === "failed" || status === "cancelled") return "shrink-0 text-red-700";
  if (status === "paused" || status === "blocked") return "shrink-0 text-amber-700";
  if (status === "running" || status === "queued") return "shrink-0 text-blue-700";
  return "shrink-0 text-gray-500";
}
