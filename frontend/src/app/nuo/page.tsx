"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/kunApiClient";

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

type Health = {
  tenant_id: string;
  total_tasks: number;
  tasks_by_status: Record<string, number>;
  events_outbox_lag: number;
  pending_actions?: number;
};

type SystemHealthFinding = {
  finding_id: string;
  severity: "info" | "warn" | "error" | "critical";
  subsystem: string;
  title: string;
  detail: string;
  suggested_action: string;
};

type CoordinationIssue = {
  issue_id: string;
  severity: "info" | "warn" | "error" | "critical";
  title: string;
  detail: string;
  suggested_action: string;
  task_id?: string | null;
  action_id?: string | null;
  action_type?: string | null;
};

type SystemHealthReport = {
  worst_severity: "info" | "warn" | "error" | "critical";
  coordination_summary?: Record<string, number>;
  coordination_issues?: CoordinationIssue[];
  findings?: SystemHealthFinding[];
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

type AccountSummary = {
  tenant_id: string;
  account: {
    display_name?: string | null;
    status: string;
    plan?: string | null;
    billing_status?: string | null;
  };
  members: unknown[];
  tokens: unknown[];
  counts: {
    members: number;
    issued_tokens: number;
    revoked_tokens: number;
    expired_tokens: number;
  };
  honest_limits: string[];
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
    policy?: {
      allowed?: boolean;
      block_reasons?: string[];
      missing_permissions?: string[];
    };
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

type ReadinessReport = {
  status: "pass" | "warn" | "block" | string;
  tenant_id: string;
  blockers: string[];
  warnings: string[];
  next_steps: string[];
  delivery_summary?: Record<string, number>;
};

type SecretAuditItem = {
  item_id: string;
  area: string;
  severity: "ok" | "warn" | "blocker";
  title: string;
  detail: string;
  suggested_action?: string;
  env_vars?: string[];
};

type SecretAuditReport = {
  env: string;
  status: "pass" | "warn" | "block";
  summary: Record<string, number>;
  items: SecretAuditItem[];
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
  allowed_risk_levels?: string[];
  requires_external_dispatch_confirmation?: boolean;
  retry_policy?: string;
  compensation_strategy?: string;
  next_step: string;
};

type WorldGatewayHandlers = {
  artifact_root: string;
  handlers: WorldHandler[];
  unsupported_policy: string;
};

type WorldHandlerHealth = {
  action_type: string;
  handler_id?: string;
  status: string;
  registered: boolean;
  configured: boolean;
  external_dispatched: boolean;
  has_compensation: boolean;
  control_status: "enabled" | "quarantined" | "disabled";
  control_reason?: string;
  failure_rate: number;
  total_seen: number;
  recommendation: string;
  issues: string[];
};

type WorldGatewayHandlerHealthResponse = {
  summary: Record<string, number>;
  handlers: WorldHandlerHealth[];
};

type WorldHandlerAutoDecision = {
  action_type: string;
  recommended_status: string;
  applied: boolean;
  can_auto_apply: boolean;
  requires_human_confirmation: boolean;
  risk_level: string;
  data_quality: string;
  reason: string;
  risk_summary: {
    failure_rate?: number;
    failure_rate_status?: string;
    missing_compensation?: boolean;
    external_dispatch_risk?: boolean;
    missing_secrets?: boolean;
    missing_handler_count?: number;
    policy_blocked_count?: number;
    total_seen?: number;
  };
};

type WorldHandlerAutoReport = {
  tenant_id: string;
  dry_run: boolean;
  decisions: WorldHandlerAutoDecision[];
  applied_count: number;
};

type WorldActionReliabilityItem = {
  action_id: string;
  task_ref: string;
  action_type: string;
  status: string;
  attempt_count: number;
  external_dispatched: boolean;
  can_auto_retry: boolean;
  recommended_action: string;
  reason: string;
  compensation_status: string;
  retry_status: string;
  idempotency_status: string;
  last_error: string;
};

type WorldActionReliabilityResponse = {
  tenant_id: string;
  summary: Record<string, number>;
  items: WorldActionReliabilityItem[];
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

type SecretStoreSetResponse = {
  path: string;
  scope: "tenant" | "global" | string;
  tenant_id: string;
  name: string;
  tenant_count: number;
  global_key_count: number;
  honest_limits: string[];
  message: string;
};

const WORLD_SECRET_OPTIONS = [
  "KUN_WORLD_EMAIL_SEND_ENABLED",
  "KUN_WORLD_SMTP_HOST",
  "KUN_WORLD_SMTP_PORT",
  "KUN_WORLD_SMTP_FROM",
  "KUN_WORLD_SMTP_USERNAME",
  "KUN_WORLD_SMTP_PASSWORD",
  "KUN_WORLD_SMTP_TLS",
  "KUN_WORLD_BROWSER_EXECUTE_ENABLED",
  "KUN_WORLD_BROWSER_ALLOWED_HOSTS",
  "KUN_WORLD_API_POST_ENABLED",
  "KUN_WORLD_API_ALLOWED_HOSTS",
  "KUN_WORLD_API_AUTH_HEADER",
  "KUN_WORLD_API_AUTH_VALUE",
  "KUN_WORLD_API_TIMEOUT_SEC",
] as const;

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await apiFetch(path, init);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export default function NuoDashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [systemReport, setSystemReport] = useState<SystemHealthReport | null>(null);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [accountSummary, setAccountSummary] = useState<AccountSummary | null>(null);
  const [accountLoading, setAccountLoading] = useState(true);
  const [accountUnavailable, setAccountUnavailable] = useState(false);
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
  const [readiness, setReadiness] = useState<ReadinessReport | null>(null);
  const [secretAudit, setSecretAudit] = useState<SecretAuditReport | null>(null);
  const [worldHandlers, setWorldHandlers] = useState<WorldHandler[]>([]);
  const [worldHealth, setWorldHealth] = useState<WorldHandlerHealth[]>([]);
  const [worldHealthSummary, setWorldHealthSummary] = useState<Record<string, number>>({});
  const [worldReliability, setWorldReliability] = useState<WorldActionReliabilityItem[]>([]);
  const [worldReliabilitySummary, setWorldReliabilitySummary] = useState<Record<string, number>>(
    {},
  );
  const [worldArtifactRoot, setWorldArtifactRoot] = useState("");
  const [unsupportedPolicy, setUnsupportedPolicy] = useState("");
  const [autoControlReport, setAutoControlReport] = useState<WorldHandlerAutoReport | null>(null);
  const [actionNotice, setActionNotice] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [decideBusy, setDecideBusy] = useState<string | null>(null);
  const [expandBusy, setExpandBusy] = useState<string | null>(null);
  const [autoControlBusy, setAutoControlBusy] = useState<"check" | "apply" | null>(null);
  const [secretName, setSecretName] = useState<(typeof WORLD_SECRET_OPTIONS)[number]>(
    "KUN_WORLD_SMTP_HOST",
  );
  const [secretValue, setSecretValue] = useState("");
  const [secretScope, setSecretScope] = useState<"tenant" | "global">("tenant");
  const [secretBusy, setSecretBusy] = useState(false);
  const [secretNotice, setSecretNotice] = useState("");

  const reload = useCallback(() => {
    Promise.all([
      fetchJson<Health>("/nuo/health/summary"),
      fetchJson<SystemHealthReport>("/nuo/health/report"),
      fetchJson<Budget>("/nuo/budget/summary"),
      fetchJson<AnchorPage<PendingAction>>("/nuo/actions/pending?limit=3"),
      fetchJson<AnchorPage<DiagnoseFinding>>(
        `/nuo/diagnose/findings?limit=3&hint_text=${encodeURIComponent(diagnoseHint)}`,
      ),
      fetchJson<{ snapshots: CapabilitySnapshot[] }>("/nuo/capability/summary"),
      fetchJson<{ items: DeliveryCapability[] }>("/nuo/health/delivery-status"),
      fetchJson<ReadinessReport>("/nuo/health/readiness"),
      fetchJson<SecretAuditReport>("/nuo/health/secret-audit"),
      fetchJson<WorldGatewayHandlers>("/nuo/actions/handlers"),
      fetchJson<WorldGatewayHandlerHealthResponse>("/nuo/actions/handler-health"),
      fetchJson<AnchorPage<PendingAction>>("/nuo/actions/recent?limit=5"),
      fetchJson<WorldActionReliabilityResponse>("/nuo/actions/execution-reliability?limit=8"),
    ])
      .then(([h, report, b, a, d, c, ds, ready, secrets, wg, wh, recent, reliability]) => {
        setHealth(h);
        setSystemReport(report);
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
        setReadiness(ready);
        setSecretAudit(secrets);
        setWorldHandlers(wg.handlers || []);
        setWorldHealth(wh.handlers || []);
        setWorldHealthSummary(wh.summary || {});
        setWorldArtifactRoot(wg.artifact_root || "");
        setUnsupportedPolicy(wg.unsupported_policy || "");
        setRecentActions(recent.actions || []);
        setWorldReliability(reliability.items || []);
        setWorldReliabilitySummary(reliability.summary || {});
        setErr(null);
      })
      .catch((e) => setErr(String(e)));

    fetchJson<AccountSummary>("/nuo/accounts/summary")
      .then((summary) => {
        setAccountSummary(summary);
        setAccountLoading(false);
        setAccountUnavailable(false);
      })
      .catch(() => {
        setAccountSummary(null);
        setAccountLoading(false);
        setAccountUnavailable(true);
      });
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
        const action = actions.find((item) => item.action_id === actionId);
        let externalDispatchConfirmed = false;
        if (decision === "approve" && needsExternalDispatchConfirmation(action)) {
          externalDispatchConfirmed = window.confirm(
            "这个动作会真实影响外部世界。请确认你已经检查目标、内容、风险和补偿方式。",
          );
          if (!externalDispatchConfirmed) return;
        }
        const result = await fetchJson<ActionDecisionResult>(`/nuo/actions/${actionId}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            external_dispatch_confirmed: externalDispatchConfirmed,
          }),
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
    [actions, reload],
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

  const runAutoControl = useCallback(
    async (dryRun: boolean) => {
      setAutoControlBusy(dryRun ? "check" : "apply");
      try {
        const report = await fetchJson<WorldHandlerAutoReport>(
          `/nuo/actions/handler-control/auto-quarantine/run?dry_run=${dryRun ? "true" : "false"}`,
          { method: "POST" },
        );
        setAutoControlReport(report);
        setActionNotice(
          dryRun
            ? `傩看完了：发现 ${report.decisions.length} 个需要处理的执行器。`
            : `傩已处理 ${report.applied_count} 个低风险问题，高风险项留给你确认。`,
        );
        reload();
      } catch (e) {
        setErr(String(e));
      } finally {
        setAutoControlBusy(null);
      }
    },
    [reload],
  );

  const saveWorldSecret = useCallback(async () => {
    const value = secretValue.trim();
    if (!value) {
      setSecretNotice("先填一个值。这个值不会被 API 回显。");
      return;
    }
    setSecretBusy(true);
    setSecretNotice("");
    try {
      const result = await fetchJson<SecretStoreSetResponse>("/nuo/health/secret-store/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: secretName, value, scope: secretScope }),
      });
      setSecretValue("");
      setSecretNotice(
        `${result.name} 已写入 ${result.scope === "tenant" ? "当前租户" : "全局"} secret store。${result.message}`,
      );
      reload();
    } catch (e) {
      setSecretNotice(`写入失败：${String(e)}`);
    } finally {
      setSecretBusy(false);
    }
  }, [reload, secretName, secretScope, secretValue]);

  if (err) return <div className="p-6 text-kun-bad">{err}</div>;
  if (!health || !budget) return <div className="p-6 text-gray-500">加载中...</div>;

  const dayRatio = budget.day_equivalent_usd / Math.max(budget.budget_daily_usd, 1e-9);
  const monthRatio = budget.month_equivalent_usd / Math.max(budget.budget_monthly_usd, 1e-9);
  const incompleteCapabilityCount = delivery.filter((item) => item.status !== "ready").length;
  const pendingCount = health.pending_actions ?? actions.length;
  const coordinationProblemCount = systemReport?.coordination_summary?.total ?? 0;
  const highSeverityCount =
    (systemReport?.coordination_summary?.error ?? 0) +
    (systemReport?.coordination_summary?.critical ?? 0);
  const healthLabel =
    highSeverityCount > 0 || systemReport?.worst_severity === "critical"
      ? "异常"
      : health.events_outbox_lag > 0 || coordinationProblemCount > 0
        ? "需关注"
        : "正常";
  const riskLabel =
    incompleteCapabilityCount > 0 || health.events_outbox_lag > 0 || coordinationProblemCount > 0
      ? "有边界"
      : "低";

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
          hint={`任务 ${health.total_tasks} · 协同问题 ${coordinationProblemCount}`}
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
          hint={`事件积压 ${health.events_outbox_lag} · 协同 ${coordinationProblemCount}`}
        />
      </section>

      <section className="bg-white rounded-lg shadow-sm p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-medium">账号与 Token</h2>
            <p className="text-xs text-gray-500 mt-1">
              {accountUnavailable
                ? "账号账本暂不可用"
                : accountSummary
                  ? `${accountSummary.account.display_name || accountSummary.tenant_id || health.tenant_id} · ${
                      accountSummary.account.status || "未知状态"
                    }`
                  : "账号账本加载中..."}
            </p>
          </div>
          {!accountUnavailable && accountSummary && (
            <span className="rounded bg-gray-100 px-2 py-1 text-xs text-gray-500">
              {accountSummary.account.status || "unknown"}
            </span>
          )}
        </div>
        {!accountUnavailable && accountSummary && (
          <div className="grid grid-cols-2 gap-3 mt-3 md:grid-cols-4">
            <MiniMetric label="成员数" value={accountSummary.counts.members} />
            <MiniMetric label="已签发 token" value={accountSummary.counts.issued_tokens} />
            <MiniMetric label="已撤销 token" value={accountSummary.counts.revoked_tokens} />
            <MiniMetric
              label="诚实限制"
              value={formatHonestyLimit(accountSummary.honest_limits)}
            />
          </div>
        )}
        {accountLoading && !accountUnavailable && !accountSummary && (
          <div className="mt-3 text-xs text-gray-400">正在读取账号账本。</div>
        )}
      </section>

      {systemReport && (
        <details className="bg-white rounded-lg shadow-sm p-4">
          <summary className="cursor-pointer text-base font-medium">
            高级 · 系统协同体检
          </summary>
          <p className="text-xs text-gray-500 mt-2">
            傩会检查审批、任务暂停、handler 隔离、事件账本之间有没有互相矛盾。
          </p>
          <div className="grid md:grid-cols-4 gap-3 mt-3">
            <MiniMetric label="总问题" value={systemReport.coordination_summary?.total ?? 0} />
            <MiniMetric label="警告" value={systemReport.coordination_summary?.warn ?? 0} />
            <MiniMetric label="错误" value={systemReport.coordination_summary?.error ?? 0} />
            <MiniMetric label="最高等级" value={systemReport.worst_severity} />
          </div>
          {(systemReport.coordination_issues || []).length > 0 && (
            <div className="mt-3 space-y-2">
              {(systemReport.coordination_issues || []).slice(0, 4).map((issue) => (
                <div key={issue.issue_id} className="border rounded p-3 text-sm">
                  <div className="flex justify-between gap-3">
                    <span className="font-medium">{issue.title}</span>
                    <span className="text-xs text-gray-500">{issue.severity}</span>
                  </div>
                  <p className="text-xs text-gray-600 mt-1">{issue.detail}</p>
                  <p className="text-xs text-gray-400 mt-1">{issue.suggested_action}</p>
                </div>
              ))}
            </div>
          )}
          {(systemReport.coordination_issues || []).length === 0 && (
            <p className="text-xs text-gray-400 mt-3">暂未发现模块协同冲突。</p>
          )}
        </details>
      )}

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

      {readiness && (
        <section className="bg-white rounded-lg shadow-sm p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-medium">正式测试就绪度</h2>
              <p className="text-xs text-gray-500 mt-1">
                这里不是愿景，是傩按当前代码和配置给出的真实判断。
              </p>
            </div>
            <ReadinessPill status={readiness.status} />
          </div>
          <div className="grid grid-cols-3 gap-3 mt-3">
            <MiniMetric label="阻塞项" value={readiness.blockers.length} />
            <MiniMetric label="提醒项" value={readiness.warnings.length} />
            <MiniMetric label="下一步" value={readiness.next_steps.length} />
          </div>
          {readiness.blockers.length > 0 && (
            <div className="mt-3 rounded border border-red-100 bg-red-50 p-3 text-xs text-red-800">
              {readiness.blockers.slice(0, 2).join("；")}
            </div>
          )}
          {readiness.next_steps.length > 0 && (
            <div className="mt-3 text-xs text-gray-500">
              下一步：{readiness.next_steps.slice(0, 2).join("；")}
            </div>
          )}
        </section>
      )}

      {secretAudit && (
        <section className="bg-white rounded-lg shadow-sm p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-medium">密钥 / 外部配置体检</h2>
              <p className="text-xs text-gray-500 mt-1">
                这里只展示风险和缺口，不展示任何密钥值。环境：{secretAudit.env}
              </p>
            </div>
            <SecretAuditPill status={secretAudit.status} />
          </div>
          <div className="grid grid-cols-3 gap-3 mt-3">
            <MiniMetric label="阻塞" value={secretAudit.summary.blocker ?? 0} />
            <MiniMetric label="提醒" value={secretAudit.summary.warn ?? 0} />
            <MiniMetric label="通过" value={secretAudit.summary.ok ?? 0} />
          </div>
          {secretAudit.items.some((item) => item.severity !== "ok") ? (
            <div className="mt-3 space-y-2">
              {secretAudit.items
                .filter((item) => item.severity !== "ok")
                .slice(0, 4)
                .map((item) => (
                  <div key={item.item_id} className="rounded border bg-gray-50 p-2 text-xs">
                    <div className="flex flex-wrap justify-between gap-2">
                      <span className="font-medium">{item.title}</span>
                      <span
                        className={
                          item.severity === "blocker" ? "text-kun-bad" : "text-kun-warn"
                        }
                      >
                        {item.severity}
                      </span>
                    </div>
                    <p className="mt-1 text-gray-600">{item.detail}</p>
                    {item.suggested_action && (
                      <p className="mt-1 text-gray-400">{item.suggested_action}</p>
                    )}
                    {(item.env_vars || []).length > 0 && (
                      <p className="mt-1 text-gray-400">配置：{item.env_vars?.join(" / ")}</p>
                    )}
                  </div>
                ))}
            </div>
          ) : (
            <p className="mt-3 text-xs text-gray-400">暂未发现密钥和外部配置风险。</p>
          )}
        </section>
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
          {Object.keys(worldHealthSummary).length > 0 && (
            <p className="text-xs text-gray-500 mt-1">
              体检：
              {Object.entries(worldHealthSummary)
                .map(([key, value]) => `${key} ${value}`)
                .join(" · ")}
            </p>
          )}
          <div className="mt-3 rounded border border-gray-200 bg-gray-50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-medium">傩能看病，也能先治小问题</div>
                <p className="text-xs text-gray-500 mt-1">
                  先列出失败率、缺补偿、真实外发、缺密钥这些风险。真会影响外部系统的项，只提醒你确认。
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  className="rounded bg-gray-900 px-3 py-1.5 text-xs text-white disabled:opacity-50"
                  disabled={autoControlBusy !== null}
                  onClick={() => runAutoControl(true)}
                >
                  看一遍
                </button>
                <button
                  className="rounded bg-kun-good px-3 py-1.5 text-xs text-white disabled:opacity-50"
                  disabled={autoControlBusy !== null}
                  onClick={() => runAutoControl(false)}
                >
                  处理低风险
                </button>
              </div>
            </div>
            {autoControlReport && (
              <div className="mt-3 space-y-2">
                <div className="text-xs text-gray-500">
                  {autoControlReport.dry_run ? "只看没动" : "已尝试处理"} · 已处理{" "}
                  {autoControlReport.applied_count} 个
                </div>
                {autoControlReport.decisions.length === 0 && (
                  <div className="text-xs text-gray-400">暂时没有需要傩处理的执行器。</div>
                )}
                {autoControlReport.decisions.map((decision) => (
                  <div key={decision.action_type} className="rounded border bg-white p-2 text-xs">
                    <div className="flex flex-wrap justify-between gap-2">
                      <span className="font-medium">{decision.action_type}</span>
                      <span className={decision.can_auto_apply ? "text-kun-good" : "text-kun-warn"}>
                        {decision.can_auto_apply ? "可自动处理" : "要你确认"} ·{" "}
                        {decision.risk_level}
                      </span>
                    </div>
                    <div className="mt-1 text-gray-600">{decision.reason}</div>
                    <div className="mt-1 text-gray-400">
                      {autoDecisionRiskSummary(decision)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="mt-3 rounded border border-gray-200 bg-gray-50 p-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-medium">外部动作配置</div>
                <p className="text-xs text-gray-500 mt-1">
                  这里只写入真实 WorldGateway 会读取的 KUN_WORLD_* 配置，用来给邮件、浏览器、企业 API
                  这些 handler 补密钥、白名单或开关。值不会回显；这还是本地 JSON secret store，
                  不是云 KMS、自动轮换或完整租户密钥平台。
                </p>
              </div>
              <span className="rounded bg-yellow-50 px-2 py-1 text-xs text-yellow-700">
                高级入口
              </span>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-[1.5fr_1fr_1fr_auto]">
              <select
                className="rounded border px-2 py-1.5 text-sm"
                value={secretName}
                onChange={(event) =>
                  setSecretName(event.target.value as (typeof WORLD_SECRET_OPTIONS)[number])
                }
              >
                {WORLD_SECRET_OPTIONS.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <input
                className="rounded border px-2 py-1.5 text-sm"
                placeholder="值，不会回显"
                type="password"
                value={secretValue}
                onChange={(event) => setSecretValue(event.target.value)}
              />
              <select
                className="rounded border px-2 py-1.5 text-sm"
                value={secretScope}
                onChange={(event) => setSecretScope(event.target.value as "tenant" | "global")}
              >
                <option value="tenant">当前租户</option>
                <option value="global">全局默认</option>
              </select>
              <button
                className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
                disabled={secretBusy}
                onClick={saveWorldSecret}
              >
                {secretBusy ? "写入中" : "写入"}
              </button>
            </div>
            {secretNotice && <p className="mt-2 text-xs text-gray-600">{secretNotice}</p>}
          </div>
          <div className="grid md:grid-cols-2 gap-3 mt-3">
            {worldHandlers.map((handler) => (
              <div key={handler.action_type} className="border rounded p-3 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="font-medium">{handler.user_label || handler.action_type}</span>
                  <span className="text-xs text-gray-500">{handler.mode}</span>
                </div>
                <HandlerHealthLine
                  card={worldHealth.find((item) => item.action_type === handler.action_type)}
                />
                <div className="mt-1 text-xs text-gray-500">{handler.action_type}</div>
                <p className="text-xs text-gray-500 mt-1">{handler.safety_note}</p>
                <p className="text-xs text-gray-700 mt-2">{handler.approval_effect}</p>
                {handler.cannot_do.length > 0 && (
                  <p className="text-xs text-gray-400 mt-1">
                    做不到：{handler.cannot_do.slice(0, 2).join("；")}
                  </p>
                )}
                {handler.requires_external_dispatch_confirmation && (
                  <p className="text-xs text-kun-warn mt-1">真实外发前需要二次确认</p>
                )}
                {handler.allowed_risk_levels && handler.allowed_risk_levels.length > 0 && (
                  <p className="text-xs text-gray-400 mt-1">
                    风险范围：{handler.allowed_risk_levels.join(" / ")}
                  </p>
                )}
                {handler.retry_policy && (
                  <p className="text-xs text-gray-400 mt-1">重试：{handler.retry_policy}</p>
                )}
                {handler.compensation_strategy && (
                  <p className="text-xs text-gray-400 mt-1">
                    补偿：{handler.compensation_strategy}
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
          {worldReliability.length > 0 && (
            <div className="mt-4 border-t pt-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-medium">外部执行可靠性</div>
                <div className="text-xs text-gray-400">
                  需看重试 {worldReliabilitySummary.needs_retry_review || 0} · 需看补偿{" "}
                  {worldReliabilitySummary.needs_compensation_review || 0}
                </div>
              </div>
              <div className="mt-2 space-y-2">
                {worldReliability.map((item) => (
                  <div key={item.action_id} className="rounded border p-2 text-xs">
                    <div className="flex justify-between gap-3">
                      <span className="font-medium">{item.action_type}</span>
                      <span
                        className={
                          item.recommended_action === "none" ? "text-gray-400" : "text-kun-warn"
                        }
                      >
                        {worldReliabilityLabel(item)}
                      </span>
                    </div>
                    <div className="mt-1 text-gray-500">{item.reason}</div>
                    <div className="mt-1 text-gray-400">
                      {item.status} · 尝试 {item.attempt_count} 次 ·{" "}
                      {item.external_dispatched ? "已影响外部" : "未外发"} ·{" "}
                      {item.idempotency_status}
                    </div>
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
                    {a.gateway_preview.audit?.policy?.allowed === false && (
                      <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
                        策略拦截：
                        {(a.gateway_preview.audit.policy.block_reasons || []).join("；")}
                      </div>
                    )}
                    {a.gateway_preview.permissions_required &&
                      a.gateway_preview.permissions_required.length > 0 && (
                        <div className="mt-1 text-gray-500">
                          需要权限：{a.gateway_preview.permissions_required.join(" / ")}
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

function MiniMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="border rounded p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-lg font-semibold mt-1">{value}</div>
    </div>
  );
}

function formatHonestyLimit(value: AccountSummary["honest_limits"]) {
  if (!value.length) return "无";
  return String(value.length);
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

function ReadinessPill({ status }: { status: ReadinessReport["status"] }) {
  const label = status === "pass" ? "可测" : status === "block" ? "有阻塞" : "可测但有提醒";
  const color =
    status === "pass"
      ? "bg-kun-good text-white"
      : status === "block"
        ? "bg-kun-bad text-white"
        : "bg-kun-warn text-white";
  return <span className={`${color} rounded px-2 py-1 text-xs whitespace-nowrap`}>{label}</span>;
}

function SecretAuditPill({ status }: { status: SecretAuditReport["status"] }) {
  const label = status === "pass" ? "安全" : status === "block" ? "有阻塞" : "有提醒";
  const color =
    status === "pass"
      ? "bg-kun-good text-white"
      : status === "block"
        ? "bg-kun-bad text-white"
        : "bg-kun-warn text-white";
  return <span className={`${color} rounded px-2 py-1 text-xs whitespace-nowrap`}>{label}</span>;
}

function HandlerHealthLine({ card }: { card?: WorldHandlerHealth }) {
  if (!card) {
    return <div className="mt-1 text-xs text-gray-400">体检：暂无数据</div>;
  }
  const cls =
    card.status === "ready"
      ? "text-kun-good"
      : card.status === "blocked" || card.control_status !== "enabled"
        ? "text-kun-bad"
        : "text-kun-warn";
  const control =
    card.control_status === "enabled"
      ? ""
      : ` · ${card.control_status}${card.control_reason ? `：${card.control_reason}` : ""}`;
  const issues = card.issues.length > 0 ? ` · ${card.issues.slice(0, 2).join("；")}` : "";
  return (
    <div className={`mt-1 text-xs ${cls}`}>
      体检：{card.status} · 配置{card.configured ? "已就绪" : "缺失"} · 补偿
      {card.has_compensation ? "有" : "缺"} · 失败率 {(card.failure_rate * 100).toFixed(0)}%
      {control}
      {issues}
    </div>
  );
}

function autoDecisionRiskSummary(decision: WorldHandlerAutoDecision) {
  const risk = decision.risk_summary;
  const failure =
    typeof risk.failure_rate === "number"
      ? `失败率 ${(risk.failure_rate * 100).toFixed(0)}%`
      : "失败率拿不到";
  const failureStatus = risk.failure_rate_status === "partial" ? "（数据不全）" : "";
  const items = [
    `${failure}${failureStatus}`,
    risk.missing_compensation ? "缺补偿" : "有补偿",
    risk.external_dispatch_risk ? "会真实外发" : "不会真实外发",
    risk.missing_secrets ? "缺密钥或配置" : "密钥配置看起来齐",
  ];
  if (typeof risk.missing_handler_count === "number" && risk.missing_handler_count > 0) {
    items.push(`缺执行器 ${risk.missing_handler_count} 次`);
  }
  if (typeof risk.policy_blocked_count === "number" && risk.policy_blocked_count > 0) {
    items.push(`被拦 ${risk.policy_blocked_count} 次`);
  }
  if (decision.data_quality === "partial") items.push("部分数据拿不到");
  return items.join(" · ");
}

function worldReliabilityLabel(item: WorldActionReliabilityItem) {
  if (item.can_auto_retry) return "可安全重试";
  if (item.recommended_action === "review_retry") return "看是否重试";
  if (item.recommended_action === "review_compensation") return "看补偿";
  if (item.recommended_action === "investigate") return "要排查";
  return "正常";
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

function needsExternalDispatchConfirmation(action?: PendingAction) {
  if (!action?.gateway_preview) return false;
  const permissions = action.gateway_preview.permissions_required || [];
  if (permissions.includes("external_dispatch_confirmation")) return true;
  const policy = action.gateway_preview.audit?.policy;
  return (policy?.missing_permissions || []).includes("external_dispatch_confirmation");
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
