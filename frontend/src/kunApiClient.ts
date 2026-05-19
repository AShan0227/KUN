"use client";

const CONFIGURED_API_ORIGIN =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_ORIGIN) || "";

const DEFAULT_TENANT_ID =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_KUN_TENANT_ID) || "u-sylvan";
const DEFAULT_USER_ID =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_KUN_USER_ID) || "sylvan";

function apiOrigin(): string {
  if (CONFIGURED_API_ORIGIN) return CONFIGURED_API_ORIGIN;
  if (typeof window === "undefined") return "";
  const { protocol, hostname, port, host } = window.location;
  if (protocol === "http:" && (port === "3000" || port === "3001")) {
    return `${protocol}//${hostname}:8000`;
  }
  return `${protocol}//${host}`;
}

function apiUrl(path: string): string {
  return `${apiOrigin()}${path}`;
}

function kunHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers({
    "X-Tenant-Id": DEFAULT_TENANT_ID,
    "X-User-Id": DEFAULT_USER_ID,
  });
  if (extra) {
    new Headers(extra).forEach((value, key) => headers.set(key, value));
  }
  return headers;
}

export class KunApiConnectionError extends Error {
  constructor(
    message = "无法连接 KUN 后端。请确认后端服务正在运行，然后重试；当前输入不会丢失。",
  ) {
    super(message);
    this.name = "KunApiConnectionError";
  }
}

export function formatKunApiError(err: unknown, fallback = "请求失败"): string {
  if (err instanceof KunApiConnectionError) return err.message;
  if (err instanceof TypeError && /fetch/i.test(err.message)) {
    return "无法连接 KUN 后端。请确认后端服务正在运行，然后重试；当前输入不会丢失。";
  }
  if (err instanceof Error && err.message.trim()) return err.message;
  return fallback;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = kunHeaders(init?.headers);
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  try {
    return await fetch(apiUrl(path), {
      ...init,
      headers,
    });
  } catch (err) {
    if (err instanceof TypeError && /fetch/i.test(err.message)) {
      throw new KunApiConnectionError();
    }
    throw err;
  }
}
