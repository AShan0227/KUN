"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * 傩 · 管家视图 (ADR-012).
 *
 * 第 1 层 — 极简管家面板 (UI 铁律 §10.3):
 *   - 系统健康 (任务总数 / 跑中 / 事件积压 / 待审批)
 *   - 成本和预算 (日 / 月, 等效 vs 实际)
 *   - 待审批动作列表 (R-A7)
 *   - 模型画像 (R-A3 capability_card 数据)
 *
 * Fetch base 用 NEXT_PUBLIC_API_ORIGIN, 缺省 '' 走相对路径 (next rewrite 转 8000).
 */

const API_BASE =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";

const TENANT = "u-sylvan"; // dev default; future: from auth
const FETCH_HEADERS: HeadersInit = { "X-Tenant-Id": TENANT };

type Health = {
  tenant_id: string;
  total_tasks: number;
  tasks_by_status: Record<string, number>;
  events_outbox_lag: number;
  pending_actions?: number;
};

type Budget = {
  tenant_id: string;
  budget_daily_usd: number;
  budget_monthly_usd: number;
  day_actual_usd: number;
  day_equivalent_usd: number;
  month_actual_usd: number;
  month_equivalent_usd: number;
};

type PendingAction = {
  action_id: string;
  task_ref: string;
  action_type: string;
  target_ref: string;
  status: string;
  risk_level: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type CapabilitySnapshot = {
  entity_id: string;
  display_name: string;
  family: string;
  maturity: string;
  overall_reliability: number;
  primary_strength: string;
  primary_weakness: string;
  capabilities: Array<{
    task_type: string;
    total_invocations: number;
    success_rate: number;
    avg_cost_usd: number;
    avg_duration_sec: number;
  }>;
  playbook_notes: string;
};

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...FETCH_HEADERS, ...(init?.headers || {}) },
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export default function NuoDashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [capability, setCapability] = useState<CapabilitySnapshot[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [decideBusy, setDecideBusy] = useState<string | null>(null);

  const reload = useCallback(() => {
    Promise.all([
      fetchJson<Health>("/nuo/health/summary"),
      fetchJson<Budget>("/nuo/budget/summary"),
      fetchJson<{ actions: PendingAction[] }>("/nuo/actions/pending"),
      fetchJson<{ snapshots: CapabilitySnapshot[] }>("/nuo/capability/summary"),
    ])
      .then(([h, b, a, c]) => {
        setHealth(h);
        setBudget(b);
        setActions(a.actions || []);
        setCapability(c.snapshots || []);
        setErr(null);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 15000);
    return () => clearInterval(id);
  }, [reload]);

  const decide = useCallback(
    async (actionId: string, decision: "approve" | "reject" | "cancel") => {
      setDecideBusy(actionId);
      try {
        await fetchJson(`/nuo/actions/${actionId}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision }),
        });
        reload();
      } catch (e) {
        setErr(String(e));
      } finally {
        setDecideBusy(null);
      }
    },
    [reload],
  );

  if (err) return <div className="p-6 text-kun-bad">{err}</div>;
  if (!health || !budget) return <div className="p-6 text-gray-500">加载中...</div>;

  const dayRatio = budget.day_equivalent_usd / Math.max(budget.budget_daily_usd, 1e-9);
  const monthRatio = budget.month_equivalent_usd / Math.max(budget.budget_monthly_usd, 1e-9);

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl font-semibold">傩 · 管家面板</h1>
        <p className="text-sm text-gray-500">
          租户 {health.tenant_id} · KUN Agent OS 管家视图
        </p>
      </div>

      <section className="grid grid-cols-4 gap-4">
        <Card title="任务总量" value={String(health.total_tasks)} />
        <Card
          title="运行中"
          value={String(health.tasks_by_status?.running ?? 0)}
          hint={`排队 ${health.tasks_by_status?.queued ?? 0}`}
        />
        <Card title="事件积压" value={String(health.events_outbox_lag)} />
        <Card
          title="待审批"
          value={String(health.pending_actions ?? actions.length)}
          hint={actions.length > 0 ? "见下方" : "暂无"}
        />
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium mb-2">预算 / 成本</h2>
        <Bar label="今日" ratio={dayRatio} now={budget.day_equivalent_usd} cap={budget.budget_daily_usd} />
        <Bar label="本月" ratio={monthRatio} now={budget.month_equivalent_usd} cap={budget.budget_monthly_usd} />
        <p className="text-xs text-gray-500 mt-3">
          等效价 (开发期): ${budget.day_equivalent_usd.toFixed(4)} · 实际 (API): $
          {budget.day_actual_usd.toFixed(4)} (ADR-008)
        </p>
      </section>

      {actions.length > 0 && (
        <section className="bg-white rounded-lg shadow-sm p-4">
          <h2 className="text-base font-medium mb-2">待审批动作</h2>
          <p className="text-xs text-gray-500 mb-3">
            高风险副作用动作（发邮件 / 删除 / 支付 / 部署 等）暂停在这里等你拍板。
            通过后会自动解除任务暂停继续跑。
          </p>
          <div className="space-y-2">
            {actions.map((a) => (
              <div key={a.action_id} className="border rounded p-2 text-sm">
                <div className="flex justify-between">
                  <div>
                    <span className="font-medium">{a.action_type}</span>
                    <span className="text-xs text-gray-500 ml-2">
                      → {a.target_ref}
                    </span>
                    <span
                      className={
                        a.risk_level === "critical"
                          ? "text-kun-bad text-xs ml-2"
                          : a.risk_level === "high"
                            ? "text-kun-warn text-xs ml-2"
                            : "text-gray-400 text-xs ml-2"
                      }
                    >
                      [{a.risk_level}]
                    </span>
                  </div>
                  <div className="space-x-1">
                    <button
                      className="bg-kun-good text-white px-2 py-1 rounded text-xs disabled:opacity-50"
                      disabled={decideBusy === a.action_id}
                      onClick={() => decide(a.action_id, "approve")}
                    >
                      通过
                    </button>
                    <button
                      className="bg-kun-bad text-white px-2 py-1 rounded text-xs disabled:opacity-50"
                      disabled={decideBusy === a.action_id}
                      onClick={() => decide(a.action_id, "reject")}
                    >
                      拒绝
                    </button>
                    <button
                      className="bg-gray-300 px-2 py-1 rounded text-xs disabled:opacity-50"
                      disabled={decideBusy === a.action_id}
                      onClick={() => decide(a.action_id, "cancel")}
                    >
                      取消
                    </button>
                  </div>
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  task {a.task_ref.slice(0, 16)}... · {a.created_at}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {capability.length > 0 && (
        <section className="bg-white rounded-lg shadow-sm p-4">
          <h2 className="text-base font-medium mb-2">模型画像</h2>
          <p className="text-xs text-gray-500 mb-3">
            实测每个模型在每种任务上的成功率、成本、耗时。手册（playbook.yaml）说&ldquo;应该这样&rdquo;，画像说&ldquo;实际这样&rdquo;。
          </p>
          <div className="space-y-3">
            {capability.slice(0, 6).map((c) => (
              <div key={c.entity_id} className="border rounded p-2 text-sm">
                <div className="flex justify-between">
                  <div>
                    <span className="font-medium">{c.display_name}</span>
                    <span className="text-xs text-gray-500 ml-2">
                      [{c.family || "?"} · {c.maturity}]
                    </span>
                  </div>
                  <span className="text-xs">
                    可靠度 {(c.overall_reliability * 100).toFixed(1)}%
                  </span>
                </div>
                {c.capabilities.length > 0 && (
                  <table className="w-full text-xs text-gray-600 mt-2">
                    <thead className="text-gray-400">
                      <tr>
                        <th className="text-left">任务类型</th>
                        <th>调用</th>
                        <th>成功率</th>
                        <th>$/次</th>
                        <th>秒/次</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.capabilities.slice(0, 3).map((row) => (
                        <tr key={row.task_type}>
                          <td>{row.task_type}</td>
                          <td className="text-center">{row.total_invocations}</td>
                          <td className="text-center">
                            {(row.success_rate * 100).toFixed(0)}%
                          </td>
                          <td className="text-center">
                            ${row.avg_cost_usd.toFixed(4)}
                          </td>
                          <td className="text-center">
                            {row.avg_duration_sec.toFixed(1)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function Card({ title, value, hint }: { title: string; value: string; hint?: string }) {
  return (
    <div className="bg-white rounded-lg shadow-sm p-4">
      <div className="text-xs text-gray-500">{title}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
      {hint && <div className="text-xs text-gray-400 mt-1">{hint}</div>}
    </div>
  );
}

function Bar({
  label,
  ratio,
  now,
  cap,
}: {
  label: string;
  ratio: number;
  now: number;
  cap: number;
}) {
  const pct = Math.min(100, Math.max(0, ratio * 100));
  const color =
    ratio >= 0.95
      ? "bg-kun-bad"
      : ratio >= 0.5
        ? "bg-kun-warn"
        : "bg-kun-good";
  return (
    <div className="mb-3">
      <div className="flex justify-between text-xs text-gray-500">
        <span>{label}</span>
        <span>
          ${now.toFixed(4)} / ${cap.toFixed(2)}
        </span>
      </div>
      <div className="h-2 bg-gray-100 rounded mt-1 overflow-hidden">
        <div className={`${color} h-full`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
