// Service worker for push notifications and PWA installation. It has no fetch handler on purpose: passing every request
// through respondWith(fetch(...)) cached nothing, delayed each request, and turned any dropped connection into
// "Failed to fetch". Without one, the browser loads pages and API calls directly.

// Bump this when the shell changes so installed PWAs fetch the new service worker (it keeps no caches of its own).
const SW_VERSION = "2026-10-14-no-login";

// A new version takes over open pages at once instead of waiting for every tab to close.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let data = { title: "Spooler", body: "" };
  try { data = e.data.json(); } catch {}
  const extra = data.data || {};
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.svg",
      badge: "/icon.svg",
      // A reminder for the same print replaces its earlier notification and alerts again.
      tag: extra.tag || undefined,
      renotify: !!extra.tag,
      // Android Chrome shows these buttons; iOS ignores them and just opens the app on tap.
      actions: extra.actions || [],
      data: extra,
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const extra = e.notification.data || {};
  if (e.action === "bed-cleared" && extra.bed_cleared) {
    // Printed OK · Bed cleared without opening the app: the same request the in-app prompt makes.
    e.waitUntil(
      fetch(extra.bed_cleared.url, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(extra.bed_cleared.body),
      }).then((r) => {
        if (r.ok) return;
        // Refused (409: a newer print has ended since this notification): open the printer to answer there.
        return clients.openWindow(extra.url || "/");
      }).catch(() => clients.openWindow(extra.url || "/"))
    );
    return;
  }
  e.waitUntil(clients.openWindow(extra.url || "/"));
});

function _b64ToUint8Array(b64) {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

// Automatically renew an expired push subscription without user interaction.
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil((async () => {
    try {
      const keyResp = await fetch("/api/push-public-key");
      if (!keyResp.ok) return;
      const { publicKey } = await keyResp.json();
      const sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _b64ToUint8Array(publicKey),
      });
      await fetch("/api/push-subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(sub.toJSON()),
      });
    } catch (err) {
      console.warn("[SW] Push resubscribe failed:", err);
    }
  })());
});
