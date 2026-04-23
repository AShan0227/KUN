"use client";

import { useEffect, useState } from "react";

/**
 * 傩 · 管家视图 (ADR-012).
 *
 * 第 1 层 — 极简管家面板 (UI 铁律 §10.3):
 *   - 系统健康 (任务总数 / 跑中 / 事件积压)
 *   - 成本和预算 (日 / 月, 等效 vs 实际)
 *
 * 第 2 层节点图 / 第 3 层深度编辑后续添加.
 */

type Health = {
  tenant_id: string;
  total_tasks: number;
  tasks_by_status: Record<string, number>;
  events_outbox_lag: number;
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

export default function NuoDashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      fetch("/nuo/health/summary").then((r) => r.json()),
      fetch("/nuo/budget/summary").then((r) => r.json()),
    ])
      .then(([h, b]) => {
        setHealth(h);
        setBudget(b);
      })
      .catch((e) => setErr(String(e)));
  }, []);

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

      <section className="grid grid-cols-3 gap-4">
        <Card title="任务总量" value={String(health.total_tasks)} />
        <Card
          title="运行中"
          value={String(health.tasks_by_status?.running ?? 0)}
          hint={`排队 ${health.tasks_by_status?.queued ?? 0}`}
        />
        <Card title="事件积压" value={String(health.events_outbox_lag)} />
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
