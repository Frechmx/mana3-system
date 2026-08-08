const CACHE_NAME = 'mana3-v8';
const ASSETS = [
  '/index.html',
  'https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=JetBrains+Mono:wght@300&display=swap'
];
// Install - cache assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS);
    })
  );
  self.skipWaiting();
});
// Activate - clean old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});
// Fetch strategy:
// - App shell (index.html / navigations): NETWORK-FIRST so every deploy reaches
//   clients immediately; cache is only the offline fallback.
// - Static assets (fonts, icons): cache-first.
self.addEventListener('fetch', (event) => {
  if (event.request.method === 'POST' || event.request.url.includes('hook.') || event.request.url.includes('run.app')) {
    return;
  }
  const isAppShell = event.request.mode === 'navigate' || event.request.url.endsWith('/index.html');
  if (isAppShell) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('/index.html', copy));
          return response;
        })
        .catch(() => caches.match('/index.html'))
    );
    return;
  }
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request);
    })
  );
});
// Push notification received
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || 'MANA³';
  const notifData = data.data || {};
  const options = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: notifData.type === 'prompt' ? 'mana3-prompt' : 'mana3-observation',
    renotify: true,
    data: notifData
  };
  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});
// Notification click — open app with context
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  let url = '/';
  if (data.type === 'prompt') {
    url = `/?view=prompt&client=${encodeURIComponent(data.client_id)}&date=${data.date}`;
  }
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      // If app is already open, navigate it
      for (const client of windowClients) {
        if ('focus' in client) {
          client.focus();
          client.postMessage({ type: 'notification-click', data: data });
          return;
        }
      }
      // Otherwise open new window
      return clients.openWindow(url);
    })
  );
});
