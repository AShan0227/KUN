"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiFetch,
  clearKunIdentity,
  getKunIdentitySource,
  saveKunIdentity,
  type KunIdentity,
  type KunIdentitySource,
} from "@/kunApiClient";

type CurrentSession = {
  tenant_id: string;
  user_id?: string | null;
  scopes: string[];
  audience: string;
  honest_limits: string[];
};

type SessionAccountEntryProps = {
  compact?: boolean;
};

function sourceLabel(source: KunIdentitySource): string {
  const savedCount = [
    source.tenantIdSource === "saved",
    source.userIdSource === "saved",
    source.authTokenSource === "saved",
  ].filter(Boolean).length;
  if (savedCount === 0) return "环境默认";
  if (savedCount === 3) return "本地保存";
  return "本地保存 + 默认值";
}

function tokenLabel(identity: KunIdentity): string {
  if (!identity.authToken) return "未设置 bearer token";
  return "已设置 bearer token";
}

export function SessionAccountEntry({ compact = false }: SessionAccountEntryProps) {
  const [identitySource, setIdentitySource] = useState(() => getKunIdentitySource());
  const [draft, setDraft] = useState<KunIdentity>(() => identitySource.identity);
  const [currentSession, setCurrentSession] = useState<CurrentSession | null>(null);
  const [sessionError, setSessionError] = useState("");
  const [savedNotice, setSavedNotice] = useState("");

  const sourceText = useMemo(() => sourceLabel(identitySource), [identitySource]);

  const reloadSource = useCallback(() => {
    const next = getKunIdentitySource();
    setIdentitySource(next);
    setDraft(next.identity);
  }, []);

  const refreshCurrentSession = useCallback(async () => {
    setSessionError("");
    try {
      const response = await apiFetch("/api/auth/session/me");
      const payload = (await response.json().catch(() => null)) as CurrentSession | null;
      if (!response.ok || !payload) {
        throw new Error(response.status ? `${response.status} ${response.statusText}` : "请求失败");
      }
      setCurrentSession(payload);
    } catch (error) {
      setCurrentSession(null);
      setSessionError(error instanceof Error ? error.message : "无法读取当前 session");
    }
  }, []);

  useEffect(() => {
    void refreshCurrentSession();
  }, [refreshCurrentSession]);

  const saveAndReload = () => {
    saveKunIdentity(draft);
    setSavedNotice("已保存，正在用新 session 重载");
    window.location.reload();
  };

  const clearAndReload = () => {
    clearKunIdentity();
    setSavedNotice("已清除，正在恢复默认 session");
    window.location.reload();
  };

  return (
    <section
      className={
        compact
          ? "rounded border border-gray-200 bg-gray-50 p-3 text-xs"
          : "bg-white p-5 shadow-sm"
      }
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className={compact ? "text-sm font-medium" : "text-lg font-semibold"}>
            前端会话 / 账号
          </h2>
          <p className="mt-1 text-xs text-gray-500">
            当前来源：{sourceText} · {tokenLabel(identitySource.identity)}
          </p>
        </div>
        <button
          className="rounded border border-gray-200 bg-white px-3 py-1 text-xs hover:bg-gray-100"
          onClick={() => void refreshCurrentSession()}
        >
          刷新状态
        </button>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-[1fr_1fr_1.5fr]">
        <label className="block">
          <span className="text-xs text-gray-500">tenant_id</span>
          <input
            className="mt-1 w-full rounded border border-gray-200 px-2 py-1.5 text-sm"
            value={draft.tenantId}
            onChange={(event) => setDraft((value) => ({ ...value, tenantId: event.target.value }))}
          />
        </label>
        <label className="block">
          <span className="text-xs text-gray-500">user_id</span>
          <input
            className="mt-1 w-full rounded border border-gray-200 px-2 py-1.5 text-sm"
            value={draft.userId}
            onChange={(event) => setDraft((value) => ({ ...value, userId: event.target.value }))}
          />
        </label>
        <label className="block">
          <span className="text-xs text-gray-500">bearer token</span>
          <input
            className="mt-1 w-full rounded border border-gray-200 px-2 py-1.5 text-sm"
            placeholder="可粘贴 Bearer ... 或纯 token"
            type="password"
            value={draft.authToken ?? ""}
            onChange={(event) =>
              setDraft((value) => ({ ...value, authToken: event.target.value }))
            }
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          className="rounded bg-kun-accent px-3 py-1.5 text-sm text-white hover:opacity-90"
          onClick={saveAndReload}
        >
          保存并重连
        </button>
        <button
          className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-100"
          onClick={() => {
            reloadSource();
            setSavedNotice("已恢复表单为当前设置");
          }}
        >
          恢复表单
        </button>
        <button
          className="rounded border border-red-200 bg-white px-3 py-1.5 text-sm text-red-600 hover:bg-red-50"
          onClick={clearAndReload}
        >
          清除
        </button>
        {savedNotice && <span className="text-xs text-gray-500">{savedNotice}</span>}
      </div>

      <div className="mt-3 rounded border border-gray-200 bg-white p-3 text-xs">
        <div className="font-medium text-gray-700">服务端 session</div>
        {currentSession ? (
          <div className="mt-1 grid gap-1 text-gray-600 md:grid-cols-2">
            <span>tenant: {currentSession.tenant_id}</span>
            <span>user: {currentSession.user_id || "未声明"}</span>
            <span>audience: {currentSession.audience}</span>
            <span>scopes: {currentSession.scopes.length ? currentSession.scopes.join(", ") : "无"}</span>
          </div>
        ) : (
          <p className="mt-1 text-gray-500">
            {sessionError ? `读取失败：${sessionError}` : "正在读取..."}
          </p>
        )}
      </div>
    </section>
  );
}
