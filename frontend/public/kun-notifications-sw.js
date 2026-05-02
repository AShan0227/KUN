self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        const existing = clients.find((client) => "focus" in client);
        if (existing) return existing.focus();
        if (self.clients.openWindow) return self.clients.openWindow("/");
        return undefined;
      }),
  );
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "kun.notify") return;
  const title = String(data.title || "KUN 提醒");
  const body = String(data.body || "").slice(0, 180);
  const tag = String(data.tag || "kun-notification");
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      data: { source: "kun" },
    }),
  );
});
