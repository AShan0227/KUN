"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiFetch,
  clearKunIdentity,
  clearKunRefreshToken,
  deleteKunIdentityProfile,
  getKunIdentitySource,
  getKunRefreshToken,
  listKunIdentityProfiles,
  saveKunIdentity,
  saveKunIdentityProfile,
  saveKunRefreshToken,
  type KunIdentity,
  type KunIdentityProfile,
  type KunIdentitySource,
} from "@/kunApiClient";

type CurrentSession = {
  tenant_id: string;
  user_id?: string | null;
  scopes: string[];
  audience: string;
  honest_limits: string[];
};

type SessionTokenSummary = {
  token_id: string;
  token_kind: string;
  status: string;
  expires_at?: string | null;
  revoked_at?: string | null;
  scopes: string[];
};

type CurrentUserSessions = {
  tenant_id: string;
  user_id: string;
  tokens: SessionTokenSummary[];
  honest_limits: string[];
};

type SessionAccountEntryProps = {
  compact?: boolean;
};

type SessionTokenPairResponse = {
  tenant_id: string;
  owner_user_id?: string;
  user_id?: string;
  access_token: string;
  refresh_token?: string;
  scopes: string[];
  audience: string;
  honest_limits: string[];
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
  const [profiles, setProfiles] = useState<KunIdentityProfile[]>(() =>
    listKunIdentityProfiles(),
  );
  const [profileLabel, setProfileLabel] = useState("");
  const [signupDraft, setSignupDraft] = useState({
    inviteCode: "",
    tenantId: identitySource.identity.tenantId,
    ownerUserId: identitySource.identity.userId,
    displayName: "",
  });
  const [acceptDraft, setAcceptDraft] = useState({
    inviteCode: "",
    inviteToken: "",
    tenantId: identitySource.identity.tenantId,
    userId: identitySource.identity.userId,
  });
  const [passwordDraft, setPasswordDraft] = useState({
    loginTenantId: identitySource.identity.tenantId,
    loginUserId: identitySource.identity.userId,
    loginPassword: "",
    setPassword: "",
  });
  const [refreshToken, setRefreshToken] = useState(() => getKunRefreshToken());
  const [currentSession, setCurrentSession] = useState<CurrentSession | null>(null);
  const [currentUserSessions, setCurrentUserSessions] = useState<CurrentUserSessions | null>(
    null,
  );
  const [sessionError, setSessionError] = useState("");
  const [tokenListError, setTokenListError] = useState("");
  const [savedNotice, setSavedNotice] = useState("");
  const [authActionError, setAuthActionError] = useState("");
  const [authActionNotice, setAuthActionNotice] = useState("");

  const sourceText = useMemo(() => sourceLabel(identitySource), [identitySource]);

  const reloadSource = useCallback(() => {
    const next = getKunIdentitySource();
    setIdentitySource(next);
    setDraft(next.identity);
    setRefreshToken(getKunRefreshToken());
    setProfiles(listKunIdentityProfiles());
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

  const refreshTokenList = useCallback(async () => {
    setTokenListError("");
    try {
      const response = await apiFetch("/api/auth/session/tokens");
      const payload = (await response.json().catch(() => null)) as CurrentUserSessions | null;
      if (!response.ok || !payload) {
        throw new Error(response.status ? `${response.status} ${response.statusText}` : "请求失败");
      }
      setCurrentUserSessions(payload);
    } catch (error) {
      setCurrentUserSessions(null);
      setTokenListError(error instanceof Error ? error.message : "无法读取 token 列表");
    }
  }, []);

  useEffect(() => {
    void refreshCurrentSession();
    if (!compact) {
      void refreshTokenList();
    }
  }, [compact, refreshCurrentSession, refreshTokenList]);

  const saveAndReload = () => {
    saveKunIdentity(draft);
    setSavedNotice("已保存，正在用新 session 重载");
    window.location.reload();
  };

  const saveProfile = () => {
    const tenantId = draft.tenantId.trim();
    const userId = draft.userId.trim();
    if (!tenantId || !userId) {
      setSavedNotice("tenant_id 和 user_id 都要填写后才能保存档案");
      return;
    }
    saveKunIdentityProfile({
      profileId: `${tenantId}:${userId}`,
      label: profileLabel.trim() || `${tenantId} / ${userId}`,
      tenantId,
      userId,
    });
    setProfiles(listKunIdentityProfiles());
    setProfileLabel("");
    setSavedNotice("已保存身份档案；档案不会保存 token");
  };

  const switchProfile = (profile: KunIdentityProfile) => {
    saveKunIdentity({
      tenantId: profile.tenantId,
      userId: profile.userId,
      authToken: "",
    });
    clearKunRefreshToken();
    setSavedNotice("已切换身份档案并清除 token，正在重载");
    window.location.reload();
  };

  const deleteProfile = (profileId: string) => {
    deleteKunIdentityProfile(profileId);
    setProfiles(listKunIdentityProfiles());
    setSavedNotice("已删除身份档案");
  };

  const clearAndReload = () => {
    clearKunIdentity();
    clearKunRefreshToken();
    setSavedNotice("已清除，正在恢复默认 session");
    window.location.reload();
  };

  const persistTokenPair = (payload: SessionTokenPairResponse) => {
    const userId = payload.owner_user_id || payload.user_id || draft.userId;
    saveKunIdentity({
      tenantId: payload.tenant_id,
      userId,
      authToken: payload.access_token,
    });
    if (payload.refresh_token) {
      saveKunRefreshToken(payload.refresh_token);
    }
    setAuthActionNotice("已保存 access token 和 refresh token，正在重载会话");
    window.location.reload();
  };

  const postAuthAction = async (path: string, body: unknown): Promise<SessionTokenPairResponse> => {
    setAuthActionError("");
    setAuthActionNotice("");
    const response = await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = (await response.json().catch(() => null)) as
      | (SessionTokenPairResponse & { detail?: string })
      | null;
    if (!response.ok || !payload) {
      throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  };

  const signup = async () => {
    try {
      const payload = await postAuthAction("/api/auth/signup", {
        invite_code: signupDraft.inviteCode,
        tenant_id: signupDraft.tenantId,
        owner_user_id: signupDraft.ownerUserId,
        display_name: signupDraft.displayName || undefined,
      });
      persistTokenPair(payload);
    } catch (error) {
      setAuthActionError(error instanceof Error ? error.message : "邀请码注册失败");
    }
  };

  const acceptInvite = async () => {
    try {
      const payload = await postAuthAction("/api/auth/invite/accept", {
        invite_code: acceptDraft.inviteToken ? undefined : acceptDraft.inviteCode,
        invite_token: acceptDraft.inviteToken || undefined,
        tenant_id: acceptDraft.tenantId,
        user_id: acceptDraft.userId,
      });
      persistTokenPair(payload);
    } catch (error) {
      setAuthActionError(error instanceof Error ? error.message : "接受邀请失败");
    }
  };

  const refreshAccessToken = async () => {
    try {
      const payload = await postAuthAction("/api/auth/session/refresh", {
        refresh_token: refreshToken,
      });
      persistTokenPair(payload);
    } catch (error) {
      setAuthActionError(error instanceof Error ? error.message : "续期失败");
    }
  };

  const passwordLogin = async () => {
    try {
      const payload = await postAuthAction("/api/auth/password/login", {
        tenant_id: passwordDraft.loginTenantId,
        user_id: passwordDraft.loginUserId,
        password: passwordDraft.loginPassword,
      });
      persistTokenPair(payload);
    } catch (error) {
      setAuthActionError(error instanceof Error ? error.message : "密码登录失败");
    }
  };

  const setOwnPassword = async () => {
    setAuthActionError("");
    setAuthActionNotice("");
    try {
      const response = await apiFetch("/api/auth/password/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: passwordDraft.setPassword }),
      });
      const payload = (await response.json().catch(() => null)) as
        | { detail?: string; status?: string }
        | null;
      if (!response.ok) {
        throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
      }
      setPasswordDraft((value) => ({ ...value, setPassword: "" }));
      setAuthActionNotice("密码已设置。它只保存哈希，不会保存明文。");
    } catch (error) {
      setAuthActionError(error instanceof Error ? error.message : "设置密码失败");
    }
  };

  const revokeToken = async (tokenId: string) => {
    setTokenListError("");
    try {
      const response = await apiFetch(`/api/auth/session/tokens/${encodeURIComponent(tokenId)}/revoke`, {
        method: "POST",
      });
      const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
      if (!response.ok) {
        throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
      }
      setAuthActionNotice(`已撤销 token：${tokenId}`);
      await refreshTokenList();
    } catch (error) {
      setTokenListError(error instanceof Error ? error.message : "撤销 token 失败");
    }
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

      <details className="mt-3 rounded border border-gray-200 bg-gray-50 p-3 text-xs">
        <summary className="cursor-pointer font-medium text-gray-700">
          租户 / 用户切换器
        </summary>
        <p className="mt-2 text-gray-500">
          只保存 tenant_id 和 user_id，不保存 token。切换档案会清除 access/refresh
          token，避免把旧租户 token 带到新租户。
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <input
            className="min-w-48 rounded border border-gray-200 px-2 py-1.5"
            placeholder="档案名，可空"
            value={profileLabel}
            onChange={(event) => setProfileLabel(event.target.value)}
          />
          <button
            className="rounded border border-gray-300 bg-white px-3 py-1.5 hover:bg-gray-100"
            onClick={saveProfile}
          >
            保存当前身份为档案
          </button>
        </div>
        {profiles.length > 0 ? (
          <div className="mt-3 divide-y divide-gray-200 rounded border border-gray-200 bg-white">
            {profiles.map((profile) => (
              <div
                key={profile.profileId}
                className="grid gap-2 p-2 md:grid-cols-[1fr_1fr_auto_auto]"
              >
                <div>
                  <div className="font-medium text-gray-700">{profile.label}</div>
                  <div className="text-gray-500">
                    {profile.tenantId} / {profile.userId}
                  </div>
                </div>
                <div className="text-gray-500">
                  {profile.updatedAt ? `更新于 ${new Date(profile.updatedAt).toLocaleString()}` : ""}
                </div>
                <button
                  className="rounded bg-kun-accent px-3 py-1.5 text-white hover:opacity-90"
                  onClick={() => switchProfile(profile)}
                >
                  切换
                </button>
                <button
                  className="rounded border border-red-200 bg-white px-3 py-1.5 text-red-600 hover:bg-red-50"
                  onClick={() => deleteProfile(profile.profileId)}
                >
                  删除
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-gray-500">还没有保存过身份档案。</p>
        )}
      </details>

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

      {!compact && (
        <div className="mt-4 grid gap-3 lg:grid-cols-4">
          <div className="rounded border border-gray-200 bg-gray-50 p-3 text-xs">
            <div className="font-medium text-gray-700">邀请码注册</div>
            <p className="mt-1 text-gray-500">
              仅在后端显式开启 self signup 时可用；这不是密码登录或 OAuth。
            </p>
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="invite_code"
              value={signupDraft.inviteCode}
              onChange={(event) =>
                setSignupDraft((value) => ({ ...value, inviteCode: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="tenant_id"
              value={signupDraft.tenantId}
              onChange={(event) =>
                setSignupDraft((value) => ({ ...value, tenantId: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="owner_user_id"
              value={signupDraft.ownerUserId}
              onChange={(event) =>
                setSignupDraft((value) => ({ ...value, ownerUserId: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="display_name，可空"
              value={signupDraft.displayName}
              onChange={(event) =>
                setSignupDraft((value) => ({ ...value, displayName: event.target.value }))
              }
            />
            <button
              className="mt-2 rounded bg-kun-accent px-3 py-1.5 text-white hover:opacity-90"
              onClick={() => void signup()}
            >
              注册并保存会话
            </button>
          </div>

          <div className="rounded border border-gray-200 bg-gray-50 p-3 text-xs">
            <div className="font-medium text-gray-700">接受成员邀请</div>
            <p className="mt-1 text-gray-500">
              可用一次性 invite token，或后端允许时用全局 invite_code。
            </p>
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="invite_token，可空"
              value={acceptDraft.inviteToken}
              onChange={(event) =>
                setAcceptDraft((value) => ({ ...value, inviteToken: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="invite_code，没 token 时填写"
              value={acceptDraft.inviteCode}
              onChange={(event) =>
                setAcceptDraft((value) => ({ ...value, inviteCode: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="tenant_id"
              value={acceptDraft.tenantId}
              onChange={(event) =>
                setAcceptDraft((value) => ({ ...value, tenantId: event.target.value }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="user_id"
              value={acceptDraft.userId}
              onChange={(event) =>
                setAcceptDraft((value) => ({ ...value, userId: event.target.value }))
              }
            />
            <button
              className="mt-2 rounded bg-kun-accent px-3 py-1.5 text-white hover:opacity-90"
              onClick={() => void acceptInvite()}
            >
              接受邀请并保存
            </button>
          </div>

          <div className="rounded border border-gray-200 bg-gray-50 p-3 text-xs">
            <div className="font-medium text-gray-700">refresh token 续期</div>
            <p className="mt-1 text-gray-500">
              用已保存或粘贴的 refresh token 换一个新的短期 access token。
            </p>
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="refresh token"
              type="password"
              value={refreshToken}
              onChange={(event) => {
                setRefreshToken(event.target.value);
                saveKunRefreshToken(event.target.value);
              }}
            />
            <button
              className="mt-2 rounded bg-kun-accent px-3 py-1.5 text-white hover:opacity-90"
              onClick={() => void refreshAccessToken()}
            >
              续期并保存
            </button>
            <button
              className="ml-2 mt-2 rounded border border-gray-300 bg-white px-3 py-1.5 hover:bg-gray-100"
              onClick={() => {
                clearKunRefreshToken();
                setRefreshToken("");
                setAuthActionNotice("已清除 refresh token");
              }}
            >
              清除 refresh
            </button>
          </div>

          <div className="rounded border border-gray-200 bg-gray-50 p-3 text-xs">
            <div className="font-medium text-gray-700">密码登录</div>
            <p className="mt-1 text-gray-500">
              默认关闭；后端开启 KUN_PASSWORD_LOGIN_ENABLED 后可用。不是 OAuth 或设备风控。
            </p>
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="tenant_id"
              value={passwordDraft.loginTenantId}
              onChange={(event) =>
                setPasswordDraft((value) => ({
                  ...value,
                  loginTenantId: event.target.value,
                }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="user_id"
              value={passwordDraft.loginUserId}
              onChange={(event) =>
                setPasswordDraft((value) => ({
                  ...value,
                  loginUserId: event.target.value,
                }))
              }
            />
            <input
              className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
              placeholder="password"
              type="password"
              value={passwordDraft.loginPassword}
              onChange={(event) =>
                setPasswordDraft((value) => ({
                  ...value,
                  loginPassword: event.target.value,
                }))
              }
            />
            <button
              className="mt-2 rounded bg-kun-accent px-3 py-1.5 text-white hover:opacity-90"
              onClick={() => void passwordLogin()}
            >
              密码登录并保存
            </button>
            <div className="mt-3 border-t border-gray-200 pt-2">
              <div className="font-medium text-gray-700">给当前用户设置密码</div>
              <input
                className="mt-2 w-full rounded border border-gray-200 px-2 py-1.5"
                placeholder="新密码，至少 12 位"
                type="password"
                value={passwordDraft.setPassword}
                onChange={(event) =>
                  setPasswordDraft((value) => ({
                    ...value,
                    setPassword: event.target.value,
                  }))
                }
              />
              <button
                className="mt-2 rounded border border-gray-300 bg-white px-3 py-1.5 hover:bg-gray-100"
                onClick={() => void setOwnPassword()}
              >
                设置当前用户密码
              </button>
            </div>
          </div>
        </div>
      )}

      {!compact && (
        <div className="mt-4 rounded border border-gray-200 bg-white p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="font-medium text-gray-700">我的 token 账本</div>
              <p className="mt-1 text-gray-500">
                只显示 token_id、状态和权限，不显示原始 token 或 hash。
              </p>
            </div>
            <button
              className="rounded border border-gray-200 bg-white px-3 py-1 hover:bg-gray-100"
              onClick={() => void refreshTokenList()}
            >
              刷新列表
            </button>
          </div>
          {currentUserSessions && currentUserSessions.tokens.length > 0 ? (
            <div className="mt-3 divide-y divide-gray-100">
              {currentUserSessions.tokens.slice(0, 12).map((token) => (
                <div
                  key={token.token_id}
                  className="grid gap-2 py-2 md:grid-cols-[1.5fr_0.8fr_0.8fr_1fr_auto]"
                >
                  <span className="break-all text-gray-700">{token.token_id}</span>
                  <span>{token.token_kind}</span>
                  <span>{token.status}</span>
                  <span className="text-gray-500">{token.expires_at || "无过期时间"}</span>
                  <button
                    className="rounded border border-red-200 bg-white px-2 py-1 text-red-600 hover:bg-red-50 disabled:opacity-40"
                    disabled={token.status === "revoked"}
                    onClick={() => void revokeToken(token.token_id)}
                  >
                    撤销
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-2 text-gray-500">
              {tokenListError ? `读取失败：${tokenListError}` : "当前没有可显示的 token 记录"}
            </p>
          )}
          {currentUserSessions && currentUserSessions.honest_limits.length > 0 && (
            <ul className="mt-2 list-disc pl-4 text-gray-500">
              {currentUserSessions.honest_limits.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!compact && (authActionError || authActionNotice) && (
        <div
          className={`mt-3 rounded border p-3 text-xs ${
            authActionError
              ? "border-red-200 bg-red-50 text-red-700"
              : "border-green-200 bg-green-50 text-green-700"
          }`}
        >
          {authActionError || authActionNotice}
        </div>
      )}
    </section>
  );
}
