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
  identityProfiles: "kun.identity_profiles",
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

export type KunIdentityProfile = {
  profileId: string;
  label: string;
  tenantId: string;
  userId: string;
  updatedAt: string;
};

function readLocalStorage(key: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(key)?.trim() || "";
  } catch {
    return "";
  }
}

function writeLocalStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // LocalStorage may be unavailable in private/browser-restricted contexts.
  }
}

function removeLocalStorage(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore browser storage failures; the API headers will fall back to defaults.
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
  writeLocalStorage(STORAGE_KEYS.tenantId, tenantId || DEFAULT_TENANT_ID);
  writeLocalStorage(STORAGE_KEYS.userId, userId || DEFAULT_USER_ID);
  if (authToken) {
    writeLocalStorage(STORAGE_KEYS.authToken, authToken);
  } else {
    removeLocalStorage(STORAGE_KEYS.authToken);
  }
}

export function clearKunIdentity(): void {
  if (typeof window === "undefined") return;
  removeLocalStorage(STORAGE_KEYS.tenantId);
  removeLocalStorage(STORAGE_KEYS.userId);
  removeLocalStorage(STORAGE_KEYS.authToken);
}

export function getKunRefreshToken(): string {
  return readLocalStorage(STORAGE_KEYS.refreshToken);
}

export function saveKunRefreshToken(token: string): void {
  if (typeof window === "undefined") return;
  const cleaned = token.trim();
  if (cleaned) {
    writeLocalStorage(STORAGE_KEYS.refreshToken, cleaned);
  } else {
    removeLocalStorage(STORAGE_KEYS.refreshToken);
  }
}

export function clearKunRefreshToken(): void {
  if (typeof window === "undefined") return;
  removeLocalStorage(STORAGE_KEYS.refreshToken);
}

export function listKunIdentityProfiles(): KunIdentityProfile[] {
  const raw = readLocalStorage(STORAGE_KEYS.identityProfiles);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item): KunIdentityProfile | null => {
        if (!item || typeof item !== "object") return null;
        const candidate = item as Partial<KunIdentityProfile>;
        const tenantId = String(candidate.tenantId || "").trim();
        const userId = String(candidate.userId || "").trim();
        if (!tenantId || !userId) return null;
        return {
          profileId: String(candidate.profileId || `${tenantId}:${userId}`),
          label: String(candidate.label || `${tenantId} / ${userId}`),
          tenantId,
          userId,
          updatedAt: String(candidate.updatedAt || ""),
        };
      })
      .filter((item): item is KunIdentityProfile => item !== null)
      .slice(0, 20);
  } catch {
    return [];
  }
}

export function saveKunIdentityProfile(profile: Omit<KunIdentityProfile, "updatedAt">): void {
  const tenantId = profile.tenantId.trim();
  const userId = profile.userId.trim();
  if (!tenantId || !userId) return;
  const next: KunIdentityProfile = {
    profileId: profile.profileId || `${tenantId}:${userId}`,
    label: profile.label.trim() || `${tenantId} / ${userId}`,
    tenantId,
    userId,
    updatedAt: new Date().toISOString(),
  };
  const profiles = listKunIdentityProfiles().filter(
    (item) => item.profileId !== next.profileId,
  );
  writeLocalStorage(
    STORAGE_KEYS.identityProfiles,
    JSON.stringify([next, ...profiles].slice(0, 20)),
  );
}

export function deleteKunIdentityProfile(profileId: string): void {
  const profiles = listKunIdentityProfiles().filter((item) => item.profileId !== profileId);
  writeLocalStorage(STORAGE_KEYS.identityProfiles, JSON.stringify(profiles));
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

type WebSocketTicketResponse = {
  ticket: string;
  expires_at: number;
};

async function fetchKunWebSocketTicket(): Promise<WebSocketTicketResponse> {
  const response = await apiFetch("/api/auth/ws-ticket", { method: "POST" });
  if (!response.ok) {
    throw new Error(`ws ticket failed: ${response.status}`);
  }
  return (await response.json()) as WebSocketTicketResponse;
}

export async function kunWebSocketUrl(): Promise<string> {
  if (typeof window === "undefined") return "";
  const identity = getKunIdentity();
  const base = API_ORIGIN || `${window.location.protocol}//${window.location.host}`;
  const proto = base.startsWith("https") ? "wss:" : "ws:";
  const host = base.replace(/^https?:\/\//, "");
  const params = new URLSearchParams();
  if (identity.authToken) {
    const ticket = await fetchKunWebSocketTicket();
    params.set("ws_ticket", ticket.ticket);
  } else {
    params.set("tenant_id", identity.tenantId);
    params.set("user_id", identity.userId);
  }
  return `${proto}//${host}/ws?${params.toString()}`;
}
