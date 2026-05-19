"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch, formatKunApiError } from "@/kunApiClient";

type CockpitTone = "working" | "waiting" | "blocked" | "ready" | "done";

type TaskCockpitView = {
  mission_id: string;
  objective: string;
  owner: string;
  task_type: string;
  status: string;
  tone: CockpitTone;
  headline: string;
  status_text: string;
  blocking_reason: string;
  next_step: string;
  safe_to_continue: boolean;
  plan: {
    plan_ref: string | null;
    version: string | null;
    objective: string;
    acceptance_criteria: string[];
    constraints: string[];
    risks: string[];
    open_questions: string[];
    human_confirmation_points: string[];
  };
  progress: {
    total: number;
    done: number;
    running: number;
    queued: number;
    ready: number;
    waiting: number;
    blocked: number;
    failed: number;
    percent_complete: number;
  };
  quality_gate: {
    gate_ref: string | null;
    status: string;
    verdict: string;
    stage: string;
    next_action: string;
    text: string;
    result_quality: number | null;
    evidence_quality: number | null;
    failure_category: string | null;
    root_cause: string;
    hard_gate_failures: string[];
    evidence_refs: string[];
    test_refs: string[];
    review_refs: string[];
  };
  collaboration: {
    human_needed: boolean;
    open_ticket_count: number;
    next_human_action: string;
    tickets: Array<{
      ticket_id: string;
      role_needed: string;
      type: string;
      status: string;
      why_needed: string;
      recommended_option: string;
      deadline: string;
      risk_if_skipped: string;
      output_contract: string;
    }>;
  };
  artifacts: {
    manifest_count: number;
    delivery_ready: boolean;
    latest_delivery_manifest_ref: string | null;
    delivery_manifest_refs: string[];
    deliverables: Array<{
      artifact_ref: string;
      kind: string;
      path_or_uri: string;
      access_status: string;
      supports: string[];
      source_quality: string;
    }>;
    evidence_refs: string[];
    test_refs: string[];
    review_refs: string[];
  };
  daemon: {
    healthy: boolean;
    text: string;
    service_status: string;
    last_heartbeat_at: string | null;
    next_wakeup_at: string | null;
    stopped_reason: string | null;
    stale: boolean;
    latest_progress_artifact_ref: string | null;
    progress_artifact_refs: string[];
  };
  work_items: Array<{
    work_item_id: string;
    lane: "ready" | "running" | "waiting" | "blocked" | "queued" | "done";
    title: string;
    owner: string;
    status: string;
    status_text: string;
    expected_output: string;
    artifact_manifest_ref: string | null;
    needs_attention: boolean;
  }>;
  risks: string[];
  recovery_actions: string[];
  acceptance: {
    acceptance_ref: string;
    decision: string;
    reviewer: string;
    satisfaction: number;
    reason: string;
    requested_changes: string[];
  } | null;
  technical_refs: string[];
};

type MissionListItem = {
  mission_id: string;
  objective: string;
  status: string;
  updated_at: string;
};

const toneClass: Record<CockpitTone, string> = {
  working: "bg-blue-50 text-blue-700 border-blue-200",
  waiting: "bg-amber-50 text-amber-700 border-amber-200",
  blocked: "bg-red-50 text-red-700 border-red-200",
  ready: "bg-emerald-50 text-emerald-700 border-emerald-200",
  done: "bg-zinc-100 text-zinc-700 border-zinc-200",
};

const laneLabel: Record<TaskCockpitView["work_items"][number]["lane"], string> = {
  ready: "待执行",
  running: "执行中",
  waiting: "等确认",
  blocked: "阻断",
  queued: "排队",
  done: "完成",
};

export default function ControlPlaneCockpitPage() {
  const [missionId, setMissionId] = useState("");
  const [missions, setMissions] = useState<MissionListItem[]>([]);
  const [cockpit, setCockpit] = useState<TaskCockpitView | null>(null);
  const [loading, setLoading] = useState(false);
  const [daemonAction, setDaemonAction] = useState<"start" | "stop" | null>(null);
  const [daemonMessage, setDaemonMessage] = useState("");
  const [error, setError] = useState("");

  const fetchCockpit = useCallback(async () => {
    const id = missionId.trim();
    if (!id) return;
    setLoading(true);
    setError("");
    try {
      const response = await apiFetch(`/api/control-plane/v6/missions/${encodeURIComponent(id)}/cockpit`);
      if (!response.ok) {
        throw new Error(`任务驾驶舱读取失败：${response.status}`);
      }
      setCockpit((await response.json()) as TaskCockpitView);
    } catch (err) {
      setError(formatKunApiError(err, "任务驾驶舱读取失败"));
      setCockpit(null);
    } finally {
      setLoading(false);
    }
  }, [missionId]);

  const fetchMissions = useCallback(async () => {
    try {
      const response = await apiFetch("/api/control-plane/v6/missions");
      if (!response.ok) return;
      const body = (await response.json()) as MissionListItem[];
      setMissions(body);
      if (!missionId.trim() && body.length > 0) {
        setMissionId(body[0].mission_id);
      }
    } catch {
      setMissions([]);
    }
  }, [missionId]);

  const runDaemonAction = useCallback(
    async (action: "start" | "stop") => {
      setDaemonAction(action);
      setDaemonMessage("");
      try {
        const response = await apiFetch(
          action === "start"
            ? "/api/control-plane/v6/daemon-service/start-claim"
            : "/api/control-plane/v6/daemon-service/stop-request",
          {
            method: "POST",
            body: JSON.stringify(
              action === "start"
                ? { daemon_id: "kun-control-plane-daemon" }
                : {
                    daemon_id: "kun-control-plane-daemon",
                    requested_by: "cockpit-user",
                    reason: "requested_from_task_cockpit",
                  },
            ),
          },
        );
        if (!response.ok) {
          throw new Error(`后台监督操作失败：${response.status}`);
        }
        const body = (await response.json()) as {
          claim?: { accepted: boolean; text: string };
          text?: string;
        };
        setDaemonMessage(body.claim?.text ?? body.text ?? "后台监督状态已更新。");
        await fetchCockpit();
      } catch (err) {
        setDaemonMessage(formatKunApiError(err, "后台监督操作失败"));
      } finally {
        setDaemonAction(null);
      }
    },
    [fetchCockpit],
  );

  useEffect(() => {
    void fetchMissions();
  }, [fetchMissions]);

  useEffect(() => {
    void fetchCockpit();
  }, [fetchCockpit]);

  const visibleWorkItems = useMemo(
    () => cockpit?.work_items.slice(0, 12) ?? [],
    [cockpit],
  );

  return (
    <div className="kun-page">
      <section className="mx-auto flex max-w-7xl flex-col gap-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="kun-kicker">Control Plane</div>
            <h1 className="mt-2 text-2xl font-semibold tracking-normal">任务驾驶舱</h1>
          </div>
          <form
            className="flex w-full flex-col gap-2 sm:flex-row lg:w-auto"
            onSubmit={(event) => {
              event.preventDefault();
              void fetchCockpit();
            }}
          >
            {missions.length > 0 && (
              <select
                className="kun-input min-w-0 sm:w-80"
                value={missionId}
                onChange={(event) => setMissionId(event.target.value)}
              >
                {missions.map((mission) => (
                  <option key={mission.mission_id} value={mission.mission_id}>
                    {mission.mission_id} · {mission.status}
                  </option>
                ))}
              </select>
            )}
            <input
              className="kun-input min-w-0 sm:w-80"
              value={missionId}
              onChange={(event) => setMissionId(event.target.value)}
              placeholder="mission id"
            />
            <button className="kun-button kun-button-primary" disabled={loading} type="submit">
              {loading ? "刷新中" : "刷新"}
            </button>
          </form>
        </div>

        {error && (
          <div className="kun-surface border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </div>
        )}

        {cockpit && (
          <>
            <section className="kun-surface p-5">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div className="min-w-0">
                  <div className={`kun-badge border ${toneClass[cockpit.tone]}`}>
                    {cockpit.status}
                  </div>
                  <h2 className="mt-3 text-2xl font-semibold tracking-normal">
                    {cockpit.headline}
                  </h2>
                  <p className="mt-2 max-w-4xl text-sm leading-6 text-gray-600">
                    {cockpit.status_text}
                  </p>
                  {cockpit.blocking_reason && (
                    <p className="mt-2 max-w-4xl text-sm leading-6 text-red-700">
                      {cockpit.blocking_reason}
                    </p>
                  )}
                </div>
                <div className="grid min-w-64 grid-cols-2 gap-2">
                  <Metric label="完成" value={`${cockpit.progress.percent_complete}%`} />
                  <Metric label="可继续" value={cockpit.safe_to_continue ? "是" : "否"} />
                  <Metric label="需确认" value={cockpit.collaboration.human_needed ? "是" : "否"} />
                  <Metric label="交付物" value={cockpit.artifacts.delivery_ready ? "已准备" : "未准备"} />
                </div>
              </div>
              <div className="mt-5 grid gap-3 md:grid-cols-4">
                <Metric label="总工作项" value={cockpit.progress.total} />
                <Metric label="执行中" value={cockpit.progress.running} />
                <Metric label="待执行" value={cockpit.progress.ready} />
                <Metric label="阻断" value={cockpit.progress.blocked + cockpit.progress.failed} />
              </div>
            </section>

            <section className="grid gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(360px,0.75fr)]">
              <div className="flex flex-col gap-4">
                <Panel title="下一步" body={cockpit.next_step} />
                <section className="kun-surface p-5">
                  <SectionTitle title="工作项" />
                  <div className="mt-3 grid gap-2">
                    {visibleWorkItems.map((item) => (
                      <div
                        key={item.work_item_id}
                        className="kun-surface-muted flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0">
                          <div className="text-sm font-semibold">{item.title}</div>
                          <div className="mt-1 text-xs text-gray-500">{item.status_text}</div>
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="kun-badge border border-gray-200 bg-white text-gray-700">
                            {laneLabel[item.lane]}
                          </span>
                          <span className="text-xs text-gray-500">{item.owner}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
                <section className="kun-surface p-5">
                  <SectionTitle title="交付物" />
                  <div className="mt-3 grid gap-3">
                    {cockpit.artifacts.deliverables.length > 0 ? (
                      cockpit.artifacts.deliverables.map((item) => (
                        <div key={item.artifact_ref} className="kun-surface-muted p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-semibold">{item.kind}</span>
                            <span className="kun-badge border border-emerald-200 bg-emerald-50 text-emerald-700">
                              {item.access_status}
                            </span>
                          </div>
                          <div className="mt-2 break-all text-xs text-gray-500">
                            {item.path_or_uri}
                          </div>
                        </div>
                      ))
                    ) : (
                      <EmptyLine text="暂无可验收交付物。" />
                    )}
                  </div>
                </section>
              </div>

              <div className="flex flex-col gap-4">
                <section className="kun-surface p-5">
                  <SectionTitle title="质量门禁" />
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <span className="kun-badge border border-gray-200 bg-white text-gray-700">
                      {cockpit.quality_gate.status}
                    </span>
                    {cockpit.quality_gate.result_quality !== null && (
                      <span className="text-sm text-gray-500">
                        质量 {Math.round(cockpit.quality_gate.result_quality * 100)}%
                      </span>
                    )}
                  </div>
                  <p className="mt-3 text-sm leading-6 text-gray-600">{cockpit.quality_gate.text}</p>
                  <ListBlock items={cockpit.quality_gate.hard_gate_failures} empty="无硬阻断。" />
                </section>

                <section className="kun-surface p-5">
                  <SectionTitle title="人机协同" />
                  {cockpit.collaboration.tickets.length > 0 ? (
                    <div className="mt-3 grid gap-3">
                      {cockpit.collaboration.tickets.map((ticket) => (
                        <div key={ticket.ticket_id} className="kun-surface-muted p-3">
                          <div className="text-sm font-semibold">{ticket.role_needed}</div>
                          <p className="mt-1 text-sm leading-6 text-gray-600">
                            {ticket.why_needed}
                          </p>
                          {ticket.recommended_option && (
                            <div className="mt-2 text-sm text-blue-700">
                              建议：{ticket.recommended_option}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <EmptyLine text="当前不需要人确认。" />
                  )}
                </section>

                <section className="kun-surface p-5">
                  <SectionTitle title="后台监督" />
                  <div
                    className={`mt-3 kun-badge border ${
                      cockpit.daemon.healthy
                        ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                        : "border-amber-200 bg-amber-50 text-amber-700"
                    }`}
                  >
                    {cockpit.daemon.healthy ? "健康" : "需关注"}
                  </div>
                  <p className="mt-3 text-sm leading-6 text-gray-600">{cockpit.daemon.text}</p>
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                    <button
                      className="kun-button kun-button-primary min-h-0 px-3 py-2 text-sm disabled:opacity-50"
                      disabled={daemonAction !== null}
                      onClick={() => void runDaemonAction("start")}
                      type="button"
                    >
                      {daemonAction === "start" ? "处理中" : "启动或接管"}
                    </button>
                    <button
                      className="kun-button kun-button-secondary min-h-0 px-3 py-2 text-sm disabled:opacity-50"
                      disabled={daemonAction !== null}
                      onClick={() => void runDaemonAction("stop")}
                      type="button"
                    >
                      {daemonAction === "stop" ? "处理中" : "安全停止"}
                    </button>
                  </div>
                  {daemonMessage && (
                    <p className="mt-3 text-sm leading-6 text-blue-700">{daemonMessage}</p>
                  )}
                  <div className="mt-3 grid gap-2 text-sm text-gray-600">
                    <InfoRow label="服务状态" value={cockpit.daemon.service_status} />
                    <InfoRow label="最近心跳" value={formatDateTime(cockpit.daemon.last_heartbeat_at)} />
                    <InfoRow label="下次唤醒" value={formatDateTime(cockpit.daemon.next_wakeup_at)} />
                    <InfoRow label="停止原因" value={cockpit.daemon.stopped_reason ?? "无"} />
                  </div>
                </section>

                <section className="kun-surface p-5">
                  <SectionTitle title="风险和恢复" />
                  <ListBlock items={cockpit.risks} empty="暂无显性风险。" />
                  <ListBlock items={cockpit.recovery_actions} empty="暂无恢复动作。" />
                </section>
              </div>
            </section>
          </>
        )}
      </section>
    </div>
  );
}

function SectionTitle({ title }: { title: string }) {
  return <h2 className="text-base font-semibold tracking-normal">{title}</h2>;
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="kun-stat-card">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
    </div>
  );
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <section className="kun-surface p-5">
      <SectionTitle title={title} />
      <p className="mt-3 text-sm leading-6 text-gray-600">{body}</p>
    </section>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-gray-100 bg-white px-3 py-2">
      <span className="text-gray-500">{label}</span>
      <span className="min-w-0 truncate text-right text-gray-700">{value}</span>
    </div>
  );
}

function ListBlock({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <EmptyLine text={empty} />;
  return (
    <ul className="mt-3 space-y-2 text-sm text-gray-600">
      {items.slice(0, 6).map((item) => (
        <li key={item} className="rounded-lg border border-gray-200 bg-white px-3 py-2">
          {item}
        </li>
      ))}
    </ul>
  );
}

function EmptyLine({ text }: { text: string }) {
  return <div className="mt-3 text-sm text-gray-400">{text}</div>;
}

function formatDateTime(value: string | null) {
  if (!value) return "无";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleString();
}
