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
  kind: "user" | "thinking" | "action_plan" | "action" | "answer" | "error";
  text: string;
  at: string;
};

type SideMsg = {
  kind: "cost_tick" | "insight" | "surprise" | "alert" | "guard_intervention" | "idle_batch_report";
  payload: any;
  at: string;
};

const WS_URL = (() => {
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  return `${proto}//${host}/ws?tenant_id=u-sylvan&user_id=sylvan`;
})();

export default function Home() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [side, setSide] = useState<SideMsg[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [totalCost, setTotalCost] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!WS_URL) return;
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        dispatchIncoming(msg);
      } catch {
        console.warn("bad ws frame", e.data);
      }
    };
    return () => ws.close();
  }, []);

  const dispatchIncoming = (msg: any) => {
    const at = new Date().toISOString();
    switch (msg.type) {
      case "thinking":
      case "action_plan":
      case "action":
      case "answer":
      case "error":
      case "correction_ack":
        setMessages((m) => [
          ...m,
          { kind: msg.type, text: formatMain(msg), at },
        ]);
        break;
      case "cost_tick":
        setTotalCost((t) => t + (msg.cost_usd_equivalent || 0));
        setSide((s) => [...s, { kind: "cost_tick", payload: msg, at }]);
        break;
      case "insight":
      case "surprise":
      case "alert":
      case "guard_intervention":
      case "idle_batch_report":
        setSide((s) => [...s, { kind: msg.type, payload: msg, at }]);
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

function formatMain(msg: any): string {
  if (msg.type === "thinking") return `思考中... (${msg.stage || ""})`;
  if (msg.type === "action_plan")
    return `类型 ${msg.task_type} / 风险 ${msg.risk_level} / 预估 $${(msg.estimated_cost_usd || 0).toFixed(4)}`;
  if (msg.type === "action") return `执行步骤 ${msg.step_id}: ${msg.description}`;
  if (msg.type === "answer") return msg.content || "";
  if (msg.type === "error") return `错误: ${msg.message || ""}`;
  if (msg.type === "correction_ack") return `(已确认纠偏)`;
  return JSON.stringify(msg);
}
