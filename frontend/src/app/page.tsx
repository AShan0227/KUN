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
  kind: "cost_tick" | "insight" | "surprise" | "alert" | "guard_intervention" | "idle_batch_report";
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
  const wsRef = useRef<WebSocket | null>(null);

  // V2.3 启状态 + 协议轮询 (每 30s 一次)
  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const [qiRes, protoRes] = await Promise.all([
          fetch(`${API_ORIGIN}/api/qi/status`, {
            headers: { "X-Tenant-Id": "u-sylvan" },
          }).catch(() => null),
          fetch(`${API_ORIGIN}/api/protocols?tenant=u-sylvan`).catch(() => null),
        ]);
        if (cancelled) return;
        if (qiRes && qiRes.ok) {
          const data = await qiRes.json();
          setQiStatus(data as QiStatus);
        }
        if (protoRes && protoRes.ok) {
          const data = await protoRes.json();
          setProtocols(data as Protocol[]);
        }
      } catch {
        // ignore polling errors
      }
    }
    void refresh();
    const id = setInterval(refresh, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

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

  return (
    <div className="grid grid-cols-[1fr_360px] gap-4 p-4 h-full">
      {/* Main channel */}
      <section className="bg-white rounded-lg shadow-sm flex flex-col min-h-[calc(100vh-100px)]">
        <header className="px-4 py-2 border-b text-sm text-gray-600 flex justify-between">
          <span>主通道 · 对话</span>
          <span>
            {connected ? (
              <span className="text-kun-good">● 已连接</span>
            ) : (
              <span className="text-kun-bad">● 未连接</span>
            )}
          </span>
        </header>
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
};

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
