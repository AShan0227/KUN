"use client";

import { useCallback, useEffect, useState } from "react";

const API_BASE =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";
const TENANT = "u-sylvan";
const USER = "sylvan";
const FETCH_HEADERS: HeadersInit = { "X-Tenant-Id": TENANT, "X-User-Id": USER };

type PromiseBody = {
  commitments: string[];
  small_talk_free_rule: string;
  refund_rule: string;
  notice_window_days: number;
};

type Dashboard = {
  used_today: number;
  used_month: number;
  saved_by_kun: number;
  refundable_balance: number;
  audit_entry_count: number;
  upcoming_change_count: number;
};

type AuditEntry = {
  entry_id: string;
  occurred_at: string;
  kind: string;
  amount_usd: number;
  saved_usd: number;
  reason: string;
  task_id?: string | null;
  refund_eligible: boolean;
};

type UpcomingChange = {
  change_id: string;
  title: string;
  effective_at: string;
  impact_summary: string;
};

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...FETCH_HEADERS, ...(init?.headers || {}) },
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export default function BillingPage() {
  const [promise, setPromise] = useState<PromiseBody | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [changes, setChanges] = useState<UpcomingChange[]>([]);
  const [refundAmount, setRefundAmount] = useState("1.00");
  const [message, setMessage] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(() => {
    Promise.all([
      fetchJson<PromiseBody>("/api/billing/promise"),
      fetchJson<Dashboard>("/api/billing/dashboard"),
      fetchJson<{ entries: AuditEntry[] }>("/api/billing/audit-log"),
      fetchJson<{ changes: UpcomingChange[] }>("/api/billing/upcoming-changes"),
    ])
      .then(([p, d, a, c]) => {
        setPromise(p);
        setDashboard(d);
        setAudit(a.entries || []);
        setChanges(c.changes || []);
        setErr(null);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const requestRefund = useCallback(async () => {
    setMessage(null);
    try {
      const body = await fetchJson<{ message: string }>("/api/billing/refund-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount_usd: Number(refundAmount), reason: "用户自助退款" }),
      });
      setMessage(body.message);
      reload();
    } catch (e) {
      setErr(String(e));
    }
  }, [refundAmount, reload]);

  if (err) return <div className="p-6 text-kun-bad">{err}</div>;
  if (!promise || !dashboard) return <div className="p-6 text-gray-500">加载中...</div>;

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl font-semibold">计费透明</h1>
        <p className="text-sm text-gray-500">
          每笔钱花在哪、为什么花、能不能退，都在这里讲清楚。
        </p>
      </div>

      <section className="bg-white rounded-lg shadow-sm p-4 border-l-4 border-kun-accent">
        <h2 className="text-base font-medium">30 天预告承诺</h2>
        <p className="text-sm text-gray-600 mt-2">
          价格、套餐、扣费规则变化至少提前 {promise.notice_window_days} 天公开。
        </p>
        <div className="mt-3 text-sm text-gray-700 space-y-1">
          {promise.commitments.map((item) => (
            <p key={item}>· {item}</p>
          ))}
        </div>
      </section>

      <section className="grid grid-cols-4 gap-4">
        <MiniStat title="今日已用" value={`$${dashboard.used_today.toFixed(2)}`} />
        <MiniStat title="本月已用" value={`$${dashboard.used_month.toFixed(2)}`} />
        <MiniStat title="KUN 已节省" value={`$${dashboard.saved_by_kun.toFixed(2)}`} />
        <MiniStat title="可退余额" value={`$${dashboard.refundable_balance.toFixed(2)}`} />
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium">余额永不蒸发</h2>
        <p className="text-sm text-gray-600 mt-2">
          套餐变更不会静默清零余额。任何扣费、退款、调整都会留下可查记录。
        </p>
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium">自助退款</h2>
        <p className="text-sm text-gray-600 mt-2">{promise.refund_rule}</p>
        <div className="mt-3 flex gap-2">
          <input
            className="border rounded px-3 py-2 text-sm w-32"
            value={refundAmount}
            onChange={(e) => setRefundAmount(e.target.value)}
            inputMode="decimal"
          />
          <button
            className="bg-kun-accent text-white px-4 py-2 rounded text-sm"
            onClick={requestRefund}
          >
            申请退款
          </button>
        </div>
        {message && <p className="text-xs text-kun-good mt-2">{message}</p>}
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium">寒暄不计费</h2>
        <p className="text-sm text-gray-600 mt-2">{promise.small_talk_free_rule}</p>
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium">未来变化</h2>
        {changes.length === 0 ? (
          <p className="text-sm text-gray-500 mt-2">未来 30 天暂无计费变化。</p>
        ) : (
          <div className="mt-2 space-y-2">
            {changes.map((change) => (
              <div key={change.change_id} className="text-sm border rounded p-2">
                <p className="font-medium">{change.title}</p>
                <p className="text-gray-500">{change.impact_summary}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium">Audit log</h2>
        <div className="overflow-x-auto mt-3">
          <table className="w-full text-sm">
            <thead className="text-left text-gray-500">
              <tr>
                <th className="py-2">时间</th>
                <th className="py-2">类型</th>
                <th className="py-2">金额</th>
                <th className="py-2">节省</th>
                <th className="py-2">原因</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((entry) => (
                <tr key={entry.entry_id} className="border-t">
                  <td className="py-2 text-gray-500">{new Date(entry.occurred_at).toLocaleString()}</td>
                  <td className="py-2">{entry.kind}</td>
                  <td className="py-2">${entry.amount_usd.toFixed(2)}</td>
                  <td className="py-2">${entry.saved_usd.toFixed(2)}</td>
                  <td className="py-2">{entry.reason}</td>
                </tr>
              ))}
              {audit.length === 0 && (
                <tr>
                  <td className="py-3 text-gray-500" colSpan={5}>
                    暂无扣费记录。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function MiniStat({ title, value }: { title: string; value: string }) {
  return (
    <div className="bg-white rounded-lg shadow-sm p-4">
      <p className="text-xs text-gray-500">{title}</p>
      <p className="text-lg font-semibold mt-1">{value}</p>
    </div>
  );
}
