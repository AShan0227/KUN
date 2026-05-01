"use client";

type KunNotificationPayload = {
  title: string;
  body: string;
  tag: string;
};

export async function ensureKunNotificationWorker(): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === "undefined") return null;
  if (!("serviceWorker" in navigator)) return null;
  try {
    const registration = await navigator.serviceWorker.register("/kun-notifications-sw.js");
    return registration;
  } catch {
    return null;
  }
}

export async function showKunNotification(
  payload: KunNotificationPayload,
): Promise<"service_worker" | "page" | "skipped"> {
  if (typeof window === "undefined" || !("Notification" in window)) return "skipped";
  if (Notification.permission !== "granted") return "skipped";
  const registration = await ensureKunNotificationWorker();
  if (registration?.active) {
    registration.active.postMessage({ type: "kun.notify", ...payload });
    return "service_worker";
  }
  if (registration?.showNotification) {
    await registration.showNotification(payload.title, {
      body: payload.body.slice(0, 180),
      tag: payload.tag,
      data: { source: "kun" },
    });
    return "service_worker";
  }
  new Notification(payload.title, {
    body: payload.body.slice(0, 180),
    tag: payload.tag,
  });
  return "page";
}
