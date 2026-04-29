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
  audit?: {
    handler_id?: string;
    artifact_kind?: string;
    relative_path?: string;
    would_create?: boolean;
    would_overwrite?: boolean;
    diff_truncated?: boolean;
    error?: string;
  };
};

type AnchorPage<T> = {
  actions?: T[];
  findings?: T[];
  next_cursor: string | null;
  has_more: boolean;
  remaining: number;
  round: number;
  max_rounds: number;
};

type DiagnoseFinding = {
  finding_id: string;
  subsystem: string;
  category: string;
  severity: string;
  description: string;
  root_cause: string;
  cause_method: string;
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

type DeliveryCapability = {
  capability_id: string;
  label: string;
  status: "ready" | "partial" | "audit_only" | "not_ready";
  summary: string;
  done: string[];
  missing: string[];
  next_steps: string[];
};

type WorldHandler = {
  action_type: string;
  handler_id: string;
  user_label: string;
  mode: "execute" | "draft" | "dry_run" | "plan";
  external_dispatched: boolean;
  artifact_kind: string;
  safety_note: string;
  approval_effect: string;
  cannot_do: string[];
  permissions_required: string[];
  next_step: string;
};

type WorldGatewayHandlers = {
  artifact_root: string;
  handlers: WorldHandler[];
  unsupported_policy: string;
};

type ActionDecisionResult = {
  action_id: string;
  status: string;
  message: string;
  gateway?: {
    gateway_mode?: string;
    capability_status?: string;
    external_dispatched?: boolean;
    requires_handler?: boolean;
    user_summary?: string;
    next_step?: string;
    audit?: { artifact_ref?: string; handler_id?: string };
  } | null;
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
  const [recentActions, setRecentActions] = useState<PendingAction[]>([]);
  const [actionCursor, setActionCursor] = useState<string | null>(null);
  const [actionHasMore, setActionHasMore] = useState(false);
  const [actionRemaining, setActionRemaining] = useState(0);
  const [actionRound, setActionRound] = useState(1);
  const [findings, setFindings] = useState<DiagnoseFinding[]>([]);
  const [findingCursor, setFindingCursor] = useState<string | null>(null);
  const [findingHasMore, setFindingHasMore] = useState(false);
  const [findingRemaining, setFindingRemaining] = useState(0);
  const [findingRound, setFindingRound] = useState(1);
  const [diagnoseHint, setDiagnoseHint] = useState("");
  const [capability, setCapability] = useState<CapabilitySnapshot[]>([]);
  const [delivery, setDelivery] = useState<DeliveryCapability[]>([]);
  const [worldHandlers, setWorldHandlers] = useState<WorldHandler[]>([]);
  const [worldArtifactRoot, setWorldArtifactRoot] = useState("");
  const [unsupportedPolicy, setUnsupportedPolicy] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [decideBusy, setDecideBusy] = useState<string | null>(null);
  const [expandBusy, setExpandBusy] = useState<string | null>(null);

  const reload = useCallback(() => {
    Promise.all([
      fetchJson<Health>("/nuo/health/summary"),
      fetchJson<Budget>("/nuo/budget/summary"),
      fetchJson<AnchorPage<PendingAction>>("/nuo/actions/pending?limit=3"),
      fetchJson<AnchorPage<DiagnoseFinding>>(
        `/nuo/diagnose/findings?limit=3&hint_text=${encodeURIComponent(diagnoseHint)}`,
      ),
      fetchJson<{ snapshots: CapabilitySnapshot[] }>("/nuo/capability/summary"),
      fetchJson<{ items: DeliveryCapability[] }>("/nuo/health/delivery-status"),
      fetchJson<WorldGatewayHandlers>("/nuo/actions/handlers"),
      fetchJson<AnchorPage<PendingAction>>("/nuo/actions/recent?limit=5"),
    ])
      .then(([h, b, a, d, c, ds, wg, recent]) => {
        setHealth(h);
        setBudget(b);
        setActions(a.actions || []);
        setActionCursor(a.next_cursor);
        setActionHasMore(a.has_more);
        setActionRemaining(a.remaining);
        setActionRound(a.round);
        setFindings(d.findings || []);
        setFindingCursor(d.next_cursor);
        setFindingHasMore(d.has_more);
        setFindingRemaining(d.remaining);
        setFindingRound(d.round);
        setCapability(c.snapshots || []);
        setDelivery(ds.items || []);
        setWorldHandlers(wg.handlers || []);
        setWorldArtifactRoot(wg.artifact_root || "");
        setUnsupportedPolicy(wg.unsupported_policy || "");
        setRecentActions(recent.actions || []);
        setErr(null);
      })
      .catch((e) => setErr(String(e)));
  }, [diagnoseHint]);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 15000);
    return () => clearInterval(id);
  }, [reload]);

  const decide = useCallback(
    async (actionId: string, decision: "approve" | "reject" | "cancel") => {
      setDecideBusy(actionId);
      try {
        const result = await fetchJson<ActionDecisionResult>(`/nuo/actions/${actionId}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision }),
        });
        const artifact = result.gateway?.audit?.artifact_ref;
        setActionNotice(
          artifact ? `${result.message} 产物：${artifact}` : result.message,
        );
        reload();
      } catch (e) {
        setErr(String(e));
      } finally {
        setDecideBusy(null);
      }
    },
    [reload],
  );

  const loadMoreActions = useCallback(async () => {
    if (!actionCursor || !actionHasMore) return;
    setExpandBusy("actions");
    try {
      const page = await fetchJson<AnchorPage<PendingAction>>(
        `/nuo/actions/pending?limit=3&expand_after=${encodeURIComponent(actionCursor)}`,
      );
      setActions((prev) => [...prev, ...(page.actions || [])]);
      setActionCursor(page.next_cursor);
      setActionHasMore(page.has_more);
      setActionRemaining(page.remaining);
      setActionRound(page.round);
    } catch (e) {
      setErr(String(e));
    } finally {
      setExpandBusy(null);
    }
  }, [actionCursor, actionHasMore]);

  const loadMoreFindings = useCallback(async () => {
    if (!findingCursor || !findingHasMore) return;
    setExpandBusy("findings");
    try {
      const page = await fetchJson<AnchorPage<DiagnoseFinding>>(
        `/nuo/diagnose/findings?limit=3&expand_after=${encodeURIComponent(
          findingCursor,
        )}&hint_text=${encodeURIComponent(diagnoseHint)}`,
      );
      setFindings((prev) => [...prev, ...(page.findings || [])]);
      setFindingCursor(page.next_cursor);
      setFindingHasMore(page.has_more);
      setFindingRemaining(page.remaining);
      setFindingRound(page.round);
    } catch (e) {
      setErr(String(e));
    } finally {
      setExpandBusy(null);
    }
  }, [findingCursor, findingHasMore, diagnoseHint]);

  if (err) return <div className="p-6 text-kun-bad">{err}</div>;
  if (!health || !budget) return <div className="p-6 text-gray-500">加载中...</div>;

  const dayRatio = budget.day_equivalent_usd / Math.max(budget.budget_daily_usd, 1e-9);
  const monthRatio = budget.month_equivalent_usd / Math.max(budget.budget_monthly_usd, 1e-9);
  const incompleteCapabilityCount = delivery.filter((item) => item.status !== "ready").length;
  const pendingCount = health.pending_actions ?? actions.length;
  const healthLabel = health.events_outbox_lag > 0 ? "需关注" : "正常";
  const riskLabel =
    incompleteCapabilityCount > 0 || health.events_outbox_lag > 0 ? "有边界" : "低";

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl font-semibold">傩 · 管家面板</h1>
        <p className="text-sm text-gray-500">
          租户 {health.tenant_id} · 先看健康、成本、权限、风险，高级诊断收在下面。
        </p>
      </div>

      <section className="grid grid-cols-4 gap-4">
        <Card
          title="健康"
          value={healthLabel}
          hint={`任务 ${health.total_tasks} · 运行 ${health.tasks_by_status?.running ?? 0}`}
        />
        <Card
          title="成本"
          value={`$${budget.day_equivalent_usd.toFixed(4)}`}
          hint={`今日上限 $${budget.budget_daily_usd.toFixed(2)}`}
        />
        <Card
          title="权限"
          value={String(pendingCount)}
          hint={pendingCount > 0 ? "等你确认" : "暂无待确认"}
        />
        <Card
          title="风险"
          value={riskLabel}
          hint={`事件积压 ${health.events_outbox_lag} · 边界 ${incompleteCapabilityCount}`}
        />
      </section>

      {delivery.length > 0 && (
        <details className="bg-white rounded-lg shadow-sm p-4">
          <summary className="cursor-pointer text-base font-medium">高级 · 能力边界</summary>
          <p className="text-xs text-gray-500 mb-3">
            这里不讲愿景，只讲现在真实能做什么、哪里还没接通。
          </p>
          <div className="grid md:grid-cols-2 gap-3">
            {delivery.map((item) => (
              <div key={item.capability_id} className="border rounded p-3 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="font-medium">{item.label}</span>
                  <StatusPill status={item.status} />
                </div>
                <p className="text-xs text-gray-500 mt-1">{item.summary}</p>
                {item.missing.length > 0 && (
                  <p className="text-xs text-gray-400 mt-2">
                    未完成：{item.missing.slice(0, 2).join("；")}
                  </p>
                )}
              </div>
            ))}
          </div>
        </details>
      )}

      <section className="bg-white rounded-lg shadow-sm p-4">
        <h2 className="text-base font-medium mb-2">预算 / 成本</h2>
        <Bar label="今日" ratio={dayRatio} now={budget.day_equivalent_usd} cap={budget.budget_daily_usd} />
        <Bar label="本月" ratio={monthRatio} now={budget.month_equivalent_usd} cap={budget.budget_monthly_usd} />
        <p className="text-xs text-gray-500 mt-3">
          等效价 (开发期): ${budget.day_equivalent_usd.toFixed(4)} · 实际 (API): $
          {budget.day_actual_usd.toFixed(4)} (ADR-008)
        </p>
      </section>

      {actionNotice && (
        <section className="bg-emerald-50 border border-emerald-100 rounded-lg p-3 text-sm text-emerald-900">
          {actionNotice}
        </section>
      )}

      {worldHandlers.length > 0 && (
        <details className="bg-white rounded-lg shadow-sm p-4">
          <summary className="cursor-pointer text-base font-medium">外部动作网关</summary>
          <p className="text-xs text-gray-500 mt-2">
            产物根目录：{worldArtifactRoot || "未配置"}。{unsupportedPolicy}
          </p>
          <div className="grid md:grid-cols-2 gap-3 mt-3">
            {worldHandlers.map((handler) => (
              <div key={handler.action_type} className="border rounded p-3 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="font-medium">{handler.user_label || handler.action_type}</span>
                  <span className="text-xs text-gray-500">{handler.mode}</span>
                </div>
                <div className="mt-1 text-xs text-gray-500">{handler.action_type}</div>
                <p className="text-xs text-gray-500 mt-1">{handler.safety_note}</p>
                <p className="text-xs text-gray-700 mt-2">{handler.approval_effect}</p>
                {handler.cannot_do.length > 0 && (
                  <p className="text-xs text-gray-400 mt-1">
                    做不到：{handler.cannot_do.slice(0, 2).join("；")}
                  </p>
                )}
                <p className="text-xs text-gray-400 mt-2">
                  {handler.handler_id} · {handler.artifact_kind} ·{" "}
                  {handler.external_dispatched ? "会产生受控本地产物" : "不会外发"}
                </p>
              </div>
            ))}
          </div>
          {recentActions.length > 0 && (
            <div className="mt-4 border-t pt-3">
              <div className="text-sm font-medium">最近外部动作</div>
              <div className="mt-2 space-y-2">
                {recentActions.map((action) => (
                  <div key={action.action_id} className="rounded border p-2 text-xs">
                    <div className="flex justify-between gap-3">
                      <span className="font-medium">{action.action_type}</span>
                      <span className="text-gray-500">{action.status}</span>
                    </div>
                    <div className="mt-1 text-gray-500">{actionGatewaySummary(action)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </details>
      )}

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
                {a.gateway_preview && (
                  <div className="mt-2 rounded bg-gray-50 p-2 text-xs text-gray-600">
                    <div className="font-medium text-gray-700">
                      <span>网关预览：{gatewayPreviewLabel(a.gateway_preview)}</span>
                      <span
                        className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${gatewayCapabilityClass(
                          a.gateway_preview,
                        )}`}
                      >
                        {gatewayCapabilityLabel(a.gateway_preview)}
                      </span>
                    </div>
                    <div className="mt-1">
                      {a.gateway_preview.user_summary || a.gateway_preview.message}
                    </div>
                    {a.gateway_preview.next_step && (
                      <div className="mt-1 text-gray-500">
                        下一步：{a.gateway_preview.next_step}
                      </div>
                    )}
                    {a.gateway_preview.audit?.relative_path && (
                      <div className="mt-1 text-gray-400">
                        文件：{a.gateway_preview.audit.relative_path}
                      </div>
                    )}
                    {a.gateway_preview.rendered_payload && (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-gray-500">查看预览内容</summary>
                        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-[11px]">
                          {a.gateway_preview.rendered_payload}
                        </pre>
                      </details>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
          {actionHasMore && (
            <button
              className="mt-3 bg-gray-100 px-3 py-2 rounded text-xs disabled:opacity-50"
              disabled={expandBusy === "actions"}
              onClick={loadMoreActions}
            >
              查看更多（还有 {actionRemaining} 条，第 {actionRound + 1} 轮，最多 3 轮）
            </button>
          )}
        </section>
      )}

      <details className="bg-white rounded-lg shadow-sm p-4">
        <summary className="cursor-pointer text-base font-medium">高级 · 诊断面板</summary>
        <p className="text-xs text-gray-500 mb-3">
          先显示最严重的 3 条发现，需要再展开下一批。
        </p>
        <div className="flex gap-2 mb-3">
          <input
            className="border rounded px-2 py-1 text-sm flex-1"
            value={diagnoseHint}
            onChange={(e) => setDiagnoseHint(e.target.value)}
            placeholder="输入诊断线索，例如 auth tenant memory"
          />
          <button className="bg-gray-900 text-white px-3 py-1 rounded text-sm" onClick={reload}>
            刷新
          </button>
        </div>
        <div className="space-y-2">
          {findings.map((f) => (
            <div key={f.finding_id} className="border rounded p-2 text-sm">
              <div className="flex justify-between">
                <div>
                  <span className="font-medium">{f.subsystem}</span>
                  <span className="text-xs text-gray-500 ml-2">{f.category}</span>
                  <span
                    className={
                      f.severity === "critical" || f.severity === "error"
                        ? "text-kun-bad text-xs ml-2"
                        : f.severity === "warn"
                          ? "text-kun-warn text-xs ml-2"
                          : "text-gray-400 text-xs ml-2"
                    }
                  >
                    [{f.severity}]
                  </span>
                </div>
                <span className="text-xs text-gray-400">{f.cause_method}</span>
              </div>
              <div className="text-xs text-gray-500 mt-1">{f.description}</div>
              {f.root_cause && (
                <div className="text-xs text-gray-400 mt-1">原因：{f.root_cause}</div>
              )}
            </div>
          ))}
          {findings.length === 0 && <div className="text-xs text-gray-400">暂无诊断发现</div>}
        </div>
        {findingHasMore && (
          <button
            className="mt-3 bg-gray-100 px-3 py-2 rounded text-xs disabled:opacity-50"
            disabled={expandBusy === "findings"}
            onClick={loadMoreFindings}
          >
            查看更多（还有 {findingRemaining} 条，第 {findingRound + 1} 轮，最多 3 轮）
          </button>
        )}
      </details>

      {capability.length > 0 && (
        <details className="bg-white rounded-lg shadow-sm p-4">
          <summary className="cursor-pointer text-base font-medium">高级 · 模型画像</summary>
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
        </details>
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

function StatusPill({ status }: { status: DeliveryCapability["status"] }) {
  const label =
    status === "ready"
      ? "可测"
      : status === "partial"
        ? "半闭环"
        : status === "audit_only"
          ? "仅审计"
          : "未就绪";
  const color =
    status === "ready"
      ? "bg-kun-good text-white"
      : status === "partial"
        ? "bg-kun-warn text-white"
        : status === "audit_only"
          ? "bg-gray-700 text-white"
          : "bg-kun-bad text-white";
  return <span className={`${color} rounded px-2 py-0.5 text-xs whitespace-nowrap`}>{label}</span>;
}

function gatewayPreviewLabel(preview: GatewayPreview) {
  if (preview.user_summary) return preview.user_summary;
  if (preview.gateway_mode === "preview_failed") return "预览失败，批准前需要人工检查";
  if (preview.requires_handler) return "没有执行器，只会记录审计，不会真实外发";
  const handler = preview.audit?.handler_id || "已注册 handler";
  if (preview.external_dispatched) return `${handler} 会执行受控本地动作`;
  return `${handler} 会生成草稿 / dry-run 产物，不会外发`;
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

function actionGatewaySummary(action: PendingAction) {
  const executor = action.payload.executor;
  if (!isRecord(executor)) return "尚无执行详情。";
  const gateway = executor.gateway;
  if (!isRecord(gateway)) return "尚无网关详情。";
  const audit = gateway.audit;
  const artifact =
    isRecord(audit) && typeof audit.artifact_ref === "string" ? audit.artifact_ref : "";
  const summary = typeof gateway.user_summary === "string" ? gateway.user_summary : "";
  const nextStep = typeof gateway.next_step === "string" ? gateway.next_step : "";
  const mode = typeof gateway.gateway_mode === "string" ? gateway.gateway_mode : "unknown";
  const dispatched =
    gateway.external_dispatched === true ? "已执行受控动作" : "未外发 / 草稿 / dry-run";
  const base = summary || `${mode} · ${dispatched}`;
  const withArtifact = artifact ? `${base} · 产物：${artifact}` : base;
  return nextStep ? `${withArtifact} · 下一步：${nextStep}` : withArtifact;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
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
