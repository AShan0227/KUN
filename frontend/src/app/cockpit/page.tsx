"use client";

/**
 * V7 §20 cockpit — daily-use dashboard (Phase X.F.COCKPIT-DAILY).
 *
 * Five panels backed by real DB readers in kun.api.cockpit_readers:
 *
 *   1. Writes Wired Status — at-a-glance dot for each of 4 X.B tables
 *   2. Capability Lifecycle — recent 9-stage transitions
 *   3. Mission Alignment — Mission Director review history
 *   4. Auditor Reports — V7 §16.6 risk + allow_release
 *   5. Ensemble Calls — cross-family divergence + cost
 *
 * Auto-refreshes every 8s. All values come from real cockpit endpoints
 * (no mocks). Surfaces reader_error_kind explicitly so DB unreachable vs
 * tenant truly empty are distinguishable (V7 §16.6 MF-6 honesty).
 *
 * Tenant defaults to NEXT_PUBLIC_KUN_TENANT_ID (u-sylvan). Change via
 * dropdown when multi-tenant deploys.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch, formatKunApiError } from "@/kunApiClient";

const REFRESH_MS = 8000;

type WritesStatus = {
  wired: boolean;
  writer: string | null;
  reader: string | null;
  warning: string | null;
  bridge_enabled: boolean | null;
};

type CapabilityTransition = {
  transition_id: string;
  capability_id: string;
  from_stage: string;
  to_stage: string;
  decided_at: string;
  decision_rationale: string;
  user_approval_ticket_id: string | null;
  evidence_refs: string[];
  metrics_snapshot: Record<string, unknown>;
};

type CapabilitySummary = {
  capability_id: string;
  current_stage: string;
  latest_transition_at: string;
  transitions_count: number;
};

type MissionReview = {
  review_id: string;
  task_id: string;
  task_plan_version: string;
  reviewed_at: string;
  verdict: string;
  alignment_score: number;
  findings: string[];
  info_gap_coverage: number;
  decomposition_coverage: number;
  evidence_coverage: number;
  plan_change_proposed: boolean;
  plan_change_proposal_id: string | null;
};

type AuditorReport = {
  report_id: string;
  audited_capability: string;
  audited_at: string;
  auditor_provider: string;
  design_promise: string;
  real_code_path: string;
  bypass_methods: string[];
  min_repro_steps: string;
  risk_level: string;
  must_fix: string[];
  acceptance_tests: string[];
  allow_release: boolean;
  rationale: string;
};

type EnsembleCall = {
  call_id: string;
  invoked_at: string;
  purpose: string;
  providers: Array<Record<string, string>>;
  consensus_strategy: string;
  divergence_score: number;
  divergence_signals: string[];
  consensus_provider: string | null;
  total_cost_usd: number;
  failure_count: number;
  n_providers_total: number;
  request_hash: string | null;
};

// ---------------------------------------------------------------------- //
// Page                                                                   //
// ---------------------------------------------------------------------- //

export default function CockpitPage() {
  const [tenant, setTenant] = useState<string>("u-sylvan");
  const [tick, setTick] = useState(0);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [writesStatus, setWritesStatus] = useState<Record<string, WritesStatus> | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilitySummary[]>([]);
  const [transitions, setTransitions] = useState<CapabilityTransition[]>([]);
  const [auditorReports, setAuditorReports] = useState<AuditorReport[]>([]);
  const [ensembleCalls, setEnsembleCalls] = useState<EnsembleCall[]>([]);
  const [missionTaskFilter, setMissionTaskFilter] = useState<string>("");
  const [missionReviews, setMissionReviews] = useState<MissionReview[]>([]);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string>("");

  const setErr = useCallback((key: string, value: string | null) => {
    setErrors((prev) => ({ ...prev, [key]: value }));
  }, []);

  // ---- writes status ----
  const fetchWritesStatus = useCallback(async () => {
    try {
      const r = await apiFetch(`/cockpit/writes-status`);
      const j = await r.json();
      setWritesStatus(j.tables ?? {});
      setErr("writes", null);
    } catch (err) {
      setErr("writes", formatKunApiError(err, "writes-status 失败"));
    }
  }, [setErr]);

  // ---- capabilities + transitions ----
  const fetchCapabilities = useCallback(async () => {
    try {
      const r = await apiFetch(`/cockpit/capabilities?tenant_id=${encodeURIComponent(tenant)}&limit=50`);
      const j = await r.json();
      setCapabilities(j.capabilities ?? []);
      setTransitions(j.transitions ?? []);
      setErr(
        "capabilities",
        j.reader_error_kind ? `${j.reader_error_kind}: ${j.reader_error_detail ?? ""}` : null,
      );
    } catch (err) {
      setErr("capabilities", formatKunApiError(err, "capabilities 失败"));
    }
  }, [tenant, setErr]);

  // ---- auditor reports ----
  const fetchAuditorReports = useCallback(async () => {
    try {
      const r = await apiFetch(
        `/cockpit/supervisor/auditor-reports?tenant_id=${encodeURIComponent(tenant)}&limit=20`,
      );
      const j = await r.json();
      setAuditorReports(j.auditor_reports ?? j.reports ?? []);
      setErr(
        "auditor",
        j.reader_error_kind ? `${j.reader_error_kind}: ${j.reader_error_detail ?? ""}` : null,
      );
    } catch (err) {
      setErr("auditor", formatKunApiError(err, "auditor 失败"));
    }
  }, [tenant, setErr]);

  // ---- ensemble calls ----
  const fetchEnsemble = useCallback(async () => {
    try {
      const r = await apiFetch(
        `/cockpit/ensemble/recent?tenant_id=${encodeURIComponent(tenant)}&limit=20`,
      );
      const j = await r.json();
      setEnsembleCalls(j.ensemble_calls ?? []);
      setErr(
        "ensemble",
        j.reader_error_kind ? `${j.reader_error_kind}: ${j.reader_error_detail ?? ""}` : null,
      );
    } catch (err) {
      setErr("ensemble", formatKunApiError(err, "ensemble 失败"));
    }
  }, [tenant, setErr]);

  // ---- mission alignment (only when task filter set) ----
  const fetchMissionReviews = useCallback(async () => {
    if (!missionTaskFilter.trim()) {
      setMissionReviews([]);
      setErr("mission", null);
      return;
    }
    try {
      const r = await apiFetch(
        `/cockpit/missions/${encodeURIComponent(missionTaskFilter.trim())}/alignment?tenant_id=${encodeURIComponent(tenant)}&limit=20`,
      );
      const j = await r.json();
      setMissionReviews(j.alignment_reviews ?? j.reviews ?? []);
      setErr(
        "mission",
        j.reader_error_kind ? `${j.reader_error_kind}: ${j.reader_error_detail ?? ""}` : null,
      );
    } catch (err) {
      setErr("mission", formatKunApiError(err, "mission 失败"));
    }
  }, [tenant, missionTaskFilter, setErr]);

  // ---- main refresh tick ----
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      await Promise.all([
        fetchWritesStatus(),
        fetchCapabilities(),
        fetchAuditorReports(),
        fetchEnsemble(),
        fetchMissionReviews(),
      ]);
      if (!cancelled) {
        setLastUpdatedAt(new Date().toISOString().slice(11, 19));
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [tick, fetchWritesStatus, fetchCapabilities, fetchAuditorReports, fetchEnsemble, fetchMissionReviews]);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  const totalCapabilities = capabilities.length;
  const totalProductionCaps = capabilities.filter((c) => c.current_stage === "production").length;
  const p0Count = auditorReports.filter((r) => r.risk_level === "P0").length;
  const blockedReleaseCount = auditorReports.filter((r) => !r.allow_release).length;
  const recentDivergence = useMemo(() => {
    if (ensembleCalls.length === 0) return null;
    const sum = ensembleCalls.reduce((acc, c) => acc + c.divergence_score, 0);
    return sum / ensembleCalls.length;
  }, [ensembleCalls]);

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <header className="border-b bg-white px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold">V7 Cockpit · 鲲驾驶舱</h1>
          <span className="text-xs text-gray-500">每 {REFRESH_MS / 1000}s 自动刷新 · 上次: {lastUpdatedAt || "—"}</span>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <label className="text-gray-600">Tenant</label>
          <input
            className="border rounded px-2 py-1 w-32"
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
          />
          <button
            className="border rounded px-3 py-1 bg-white hover:bg-gray-100"
            onClick={() => setTick((t) => t + 1)}
          >
            手动刷新
          </button>
        </div>
      </header>

      <main className="px-6 py-5 space-y-5">
        {/* Top stats */}
        <section className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
          <StatCard label="Capabilities" value={totalCapabilities} sub={`${totalProductionCaps} in PRODUCTION`} />
          <StatCard
            label="P0 Auditor"
            value={p0Count}
            sub={p0Count > 0 ? `⚠ ${blockedReleaseCount} blocked release` : "no P0"}
            tone={p0Count > 0 ? "bad" : "good"}
          />
          <StatCard
            label="Auditor Reports"
            value={auditorReports.length}
            sub={`${auditorReports.filter((r) => r.allow_release).length} allow release`}
          />
          <StatCard
            label="Ensemble Calls"
            value={ensembleCalls.length}
            sub={recentDivergence !== null ? `avg divergence ${recentDivergence.toFixed(2)}` : "—"}
            tone={recentDivergence !== null && recentDivergence > 0.5 ? "good" : recentDivergence !== null ? "warn" : undefined}
          />
          <StatCard label="Transitions" value={transitions.length} sub="recent lifecycle" />
        </section>

        {/* Writes Wired Status (V7 §16.6 MF-3) */}
        <Panel title="Writes Wired Status (V7 §16.6 MF-3)" error={errors.writes}>
          {writesStatus ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
              {Object.entries(writesStatus).map(([table, status]) => (
                <div key={table} className="rounded border bg-white p-3 flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <Dot tone={status.wired ? "good" : "warn"} />
                    <span className="font-medium">{table}</span>
                  </div>
                  <div className="text-gray-500">writer: {status.writer ?? "—"}</div>
                  <div className="text-gray-500">reader: {status.reader ?? "—"}</div>
                  {status.warning && <div className="text-yellow-700">⚠ {status.warning}</div>}
                </div>
              ))}
            </div>
          ) : (
            <Empty>读 writes-status 中...</Empty>
          )}
        </Panel>

        {/* Capability Lifecycle */}
        <Panel title="Capability Lifecycle (V7 §15)" error={errors.capabilities}>
          {capabilities.length === 0 ? (
            <Empty>租户 {tenant} 暂无 capability lifecycle 记录</Empty>
          ) : (
            <div className="overflow-auto">
              <table className="text-xs w-full">
                <thead className="text-left text-gray-500 border-b">
                  <tr>
                    <Th>capability_id</Th>
                    <Th>current_stage</Th>
                    <Th>latest_at</Th>
                    <Th>transitions</Th>
                  </tr>
                </thead>
                <tbody>
                  {capabilities.map((c) => (
                    <tr key={c.capability_id} className="border-b last:border-0">
                      <Td mono>{c.capability_id}</Td>
                      <Td>
                        <StageBadge stage={c.current_stage} />
                      </Td>
                      <Td>{c.latest_transition_at?.slice(0, 19).replace("T", " ") ?? "—"}</Td>
                      <Td>{c.transitions_count}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* Mission Alignment */}
        <Panel
          title="Mission Alignment (V7 §9.7)"
          error={errors.mission}
          actions={
            <div className="flex gap-2">
              <input
                className="border rounded px-2 py-1 text-xs w-64"
                placeholder="task_id 来过滤 mission alignment"
                value={missionTaskFilter}
                onChange={(e) => setMissionTaskFilter(e.target.value)}
              />
            </div>
          }
        >
          {missionReviews.length === 0 ? (
            <Empty>
              {missionTaskFilter.trim() ? `task ${missionTaskFilter} 暂无 reviews` : "输入 task_id 查 mission alignment 历史"}
            </Empty>
          ) : (
            <div className="overflow-auto">
              <table className="text-xs w-full">
                <thead className="text-left text-gray-500 border-b">
                  <tr>
                    <Th>verdict</Th>
                    <Th>alignment</Th>
                    <Th>info_gap</Th>
                    <Th>decomp</Th>
                    <Th>evidence</Th>
                    <Th>reviewed_at</Th>
                    <Th>findings (first)</Th>
                  </tr>
                </thead>
                <tbody>
                  {missionReviews.map((r) => (
                    <tr key={r.review_id} className="border-b last:border-0">
                      <Td>
                        <VerdictBadge verdict={r.verdict} />
                      </Td>
                      <Td mono>{r.alignment_score?.toFixed(2)}</Td>
                      <Td mono>{(r.info_gap_coverage * 100).toFixed(0)}%</Td>
                      <Td mono>{(r.decomposition_coverage * 100).toFixed(0)}%</Td>
                      <Td mono>{(r.evidence_coverage * 100).toFixed(0)}%</Td>
                      <Td>{r.reviewed_at?.slice(0, 19).replace("T", " ") ?? "—"}</Td>
                      <Td>
                        <span className="text-gray-600 line-clamp-2">{r.findings[0] ?? "—"}</span>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* Auditor Reports (V7 §16.6) */}
        <Panel title="Auditor Reports (V7 §16.6)" error={errors.auditor}>
          {auditorReports.length === 0 ? (
            <Empty>租户 {tenant} 暂无 auditor reports</Empty>
          ) : (
            <div className="overflow-auto">
              <table className="text-xs w-full">
                <thead className="text-left text-gray-500 border-b">
                  <tr>
                    <Th>capability</Th>
                    <Th>risk</Th>
                    <Th>allow_release</Th>
                    <Th>must_fix</Th>
                    <Th>bypass_methods</Th>
                    <Th>auditor_provider</Th>
                    <Th>audited_at</Th>
                  </tr>
                </thead>
                <tbody>
                  {auditorReports.map((r) => (
                    <tr key={r.report_id} className="border-b last:border-0">
                      <Td mono>{r.audited_capability}</Td>
                      <Td>
                        <RiskBadge risk={r.risk_level} />
                      </Td>
                      <Td>
                        {r.allow_release ? (
                          <span className="text-green-700">✓ allow</span>
                        ) : (
                          <span className="text-red-700">✗ block</span>
                        )}
                      </Td>
                      <Td>{r.must_fix.length}</Td>
                      <Td>{r.bypass_methods.length}</Td>
                      <Td className="text-gray-500">{r.auditor_provider}</Td>
                      <Td>{r.audited_at?.slice(0, 19).replace("T", " ") ?? "—"}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* Ensemble Calls (V7 §11.4) */}
        <Panel title="Multi-LLM Ensemble Calls (V7 §11.4)" error={errors.ensemble}>
          {ensembleCalls.length === 0 ? (
            <Empty>租户 {tenant} 暂无 ensemble call 记录 (KUN_V7_ENSEMBLE_ENABLED=true 时才记)</Empty>
          ) : (
            <div className="overflow-auto">
              <table className="text-xs w-full">
                <thead className="text-left text-gray-500 border-b">
                  <tr>
                    <Th>purpose</Th>
                    <Th>providers</Th>
                    <Th>strategy</Th>
                    <Th>divergence</Th>
                    <Th>cost</Th>
                    <Th>failures</Th>
                    <Th>invoked_at</Th>
                  </tr>
                </thead>
                <tbody>
                  {ensembleCalls.map((c) => (
                    <tr key={c.call_id} className="border-b last:border-0">
                      <Td>{c.purpose}</Td>
                      <Td className="text-gray-600">
                        {c.providers.map((p) => p.name || p.model_id || "?").join(" + ")}
                      </Td>
                      <Td>{c.consensus_strategy}</Td>
                      <Td>
                        <DivergenceBadge score={c.divergence_score} />
                      </Td>
                      <Td mono>${c.total_cost_usd.toFixed(5)}</Td>
                      <Td>{c.failure_count}</Td>
                      <Td>{c.invoked_at?.slice(0, 19).replace("T", " ") ?? "—"}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------- //
// Small components                                                       //
// ---------------------------------------------------------------------- //

function StatCard(props: { label: string; value: number | string; sub?: string; tone?: "good" | "warn" | "bad" }) {
  const toneClass =
    props.tone === "good"
      ? "border-green-300 bg-green-50"
      : props.tone === "warn"
        ? "border-yellow-300 bg-yellow-50"
        : props.tone === "bad"
          ? "border-red-300 bg-red-50"
          : "border-gray-200 bg-white";
  return (
    <div className={`rounded border p-3 ${toneClass}`}>
      <div className="text-xs text-gray-500">{props.label}</div>
      <div className="text-2xl font-semibold">{props.value}</div>
      {props.sub && <div className="text-xs text-gray-500 mt-1">{props.sub}</div>}
    </div>
  );
}

function Panel(props: {
  title: string;
  error?: string | null;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="bg-white border rounded-lg">
      <header className="px-4 py-2 border-b flex items-center justify-between">
        <h2 className="text-sm font-medium">{props.title}</h2>
        {props.actions}
      </header>
      <div className="p-4 space-y-3">
        {props.error && (
          <div className="bg-red-50 border border-red-200 text-red-800 text-xs rounded p-2">
            读失败: {props.error}
          </div>
        )}
        {props.children}
      </div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-xs text-gray-500 italic">{children}</div>;
}

function Dot({ tone }: { tone: "good" | "warn" | "bad" }) {
  const cls =
    tone === "good"
      ? "bg-green-500"
      : tone === "warn"
        ? "bg-yellow-500"
        : "bg-red-500";
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} />;
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-2 py-1 font-medium text-gray-500">{children}</th>;
}

function Td({ children, mono, className }: { children: React.ReactNode; mono?: boolean; className?: string }) {
  return (
    <td className={`px-2 py-1 align-top ${mono ? "font-mono" : ""} ${className ?? ""}`}>{children}</td>
  );
}

const STAGE_CLASS: Record<string, string> = {
  observation: "bg-gray-200 text-gray-700",
  candidate: "bg-blue-100 text-blue-800",
  replay: "bg-blue-200 text-blue-900",
  holdout: "bg-cyan-200 text-cyan-900",
  shadow: "bg-indigo-200 text-indigo-900",
  canary: "bg-amber-200 text-amber-900",
  production: "bg-green-200 text-green-900",
  monitor: "bg-green-100 text-green-800",
  rollback: "bg-red-200 text-red-900",
  retire: "bg-gray-300 text-gray-700",
};

function StageBadge({ stage }: { stage: string }) {
  const cls = STAGE_CLASS[stage] ?? "bg-gray-100 text-gray-700";
  return <span className={`inline-block px-2 py-0.5 rounded text-[11px] ${cls}`}>{stage}</span>;
}

const VERDICT_CLASS: Record<string, string> = {
  ok: "bg-green-100 text-green-800",
  drifting: "bg-yellow-100 text-yellow-800",
  off_anchor: "bg-orange-100 text-orange-800",
  needs_human: "bg-red-100 text-red-800",
};

function VerdictBadge({ verdict }: { verdict: string }) {
  const cls = VERDICT_CLASS[verdict] ?? "bg-gray-100 text-gray-700";
  return <span className={`inline-block px-2 py-0.5 rounded text-[11px] ${cls}`}>{verdict}</span>;
}

const RISK_CLASS: Record<string, string> = {
  P0: "bg-red-200 text-red-900",
  P1: "bg-orange-200 text-orange-900",
  P2: "bg-yellow-100 text-yellow-800",
};

function RiskBadge({ risk }: { risk: string }) {
  const cls = RISK_CLASS[risk] ?? "bg-gray-100 text-gray-700";
  return <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-mono ${cls}`}>{risk}</span>;
}

function DivergenceBadge({ score }: { score: number }) {
  const cls = score > 0.5 ? "bg-green-100 text-green-800" : score > 0.2 ? "bg-yellow-100 text-yellow-800" : "bg-gray-100 text-gray-700";
  return <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-mono ${cls}`}>{score.toFixed(2)}</span>;
}
