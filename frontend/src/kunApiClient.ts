"use client";

export const API_ORIGIN =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";

const DEFAULT_TENANT_ID =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_KUN_TENANT_ID) || "u-sylvan";
const DEFAULT_USER_ID =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_KUN_USER_ID) || "sylvan";

const STORAGE_KEYS = {
  tenantId: "kun.tenant_id",
  userId: "kun.user_id",
  authToken: "kun.auth_token",
  refreshToken: "kun.refresh_token",
} as const;

export type KunIdentity = {
  tenantId: string;
  userId: string;
  authToken?: string;
};

export type KunIdentitySource = {
  identity: KunIdentity;
  tenantIdSource: "saved" | "default";
  userIdSource: "saved" | "default";
  authTokenSource: "saved" | "empty";
};

function readLocalStorage(key: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(key)?.trim() || "";
  } catch {
    return "";
  }
}

export function getKunIdentity(): KunIdentity {
  return getKunIdentitySource().identity;
}

export function getKunIdentitySource(): KunIdentitySource {
  const savedTenantId = readLocalStorage(STORAGE_KEYS.tenantId);
  const savedUserId = readLocalStorage(STORAGE_KEYS.userId);
  const savedAuthToken = readLocalStorage(STORAGE_KEYS.authToken);
  return {
    identity: {
      tenantId: savedTenantId || DEFAULT_TENANT_ID,
      userId: savedUserId || DEFAULT_USER_ID,
      authToken: savedAuthToken || undefined,
    },
    tenantIdSource: savedTenantId ? "saved" : "default",
    userIdSource: savedUserId ? "saved" : "default",
    authTokenSource: savedAuthToken ? "saved" : "empty",
  };
}

export function saveKunIdentity(identity: KunIdentity): void {
  if (typeof window === "undefined") return;
  const tenantId = identity.tenantId.trim();
  const userId = identity.userId.trim();
  const authToken = identity.authToken?.trim() || "";
  window.localStorage.setItem(STORAGE_KEYS.tenantId, tenantId || DEFAULT_TENANT_ID);
  window.localStorage.setItem(STORAGE_KEYS.userId, userId || DEFAULT_USER_ID);
  if (authToken) {
    window.localStorage.setItem(STORAGE_KEYS.authToken, authToken);
  } else {
    window.localStorage.removeItem(STORAGE_KEYS.authToken);
  }
}

export function clearKunIdentity(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(STORAGE_KEYS.tenantId);
  window.localStorage.removeItem(STORAGE_KEYS.userId);
  window.localStorage.removeItem(STORAGE_KEYS.authToken);
}

export function getKunRefreshToken(): string {
  return readLocalStorage(STORAGE_KEYS.refreshToken);
}

export function saveKunRefreshToken(token: string): void {
  if (typeof window === "undefined") return;
  const cleaned = token.trim();
  if (cleaned) {
    window.localStorage.setItem(STORAGE_KEYS.refreshToken, cleaned);
  } else {
    window.localStorage.removeItem(STORAGE_KEYS.refreshToken);
  }
}

export function clearKunRefreshToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(STORAGE_KEYS.refreshToken);
}

function authHeaderValue(token: string): string {
  return token.toLowerCase().startsWith("bearer ") ? token : `Bearer ${token}`;
}

export function kunHeaders(extra?: HeadersInit): Headers {
  const identity = getKunIdentity();
  const headers = new Headers({
    "X-Tenant-Id": identity.tenantId,
    "X-User-Id": identity.userId,
  });
  if (identity.authToken) {
    headers.set("Authorization", authHeaderValue(identity.authToken));
  }
  if (extra) {
    new Headers(extra).forEach((value, key) => headers.set(key, value));
  }
  return headers;
}

export function apiUrl(path: string): string {
  return `${API_ORIGIN}${path}`;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(apiUrl(path), {
    ...init,
    headers: kunHeaders(init?.headers),
  });
}

export function kunWebSocketUrl(): string {
  if (typeof window === "undefined") return "";
  const identity = getKunIdentity();
  const base = API_ORIGIN || `${window.location.protocol}//${window.location.host}`;
  const proto = base.startsWith("https") ? "wss:" : "ws:";
  const host = base.replace(/^https?:\/\//, "");
  const params = new URLSearchParams({
    tenant_id: identity.tenantId,
    user_id: identity.userId,
  });
  if (identity.authToken) {
    params.set("auth_token", identity.authToken);
  }
  return `${proto}//${host}/ws?${params.toString()}`;
}
