const CACHE_NAME = 'mana3-v9';
const ASSETS = [
  '/index.html',
  // Was DM Sans, which index.html doesn't use. The app renders Inter and
  // JetBrains Mono, so the precache was warming a font nobody reads.
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400&display=swap'
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

// Notification click — always land on Today.
//
// Previously an observation notification carried no type, so url stayed '/'
// and a cold open landed on the Record tab (the default active view). The
// already-open path called postMessage, but index.html had no message
// listener, so that did nothing at all. Both paths are handled here now, and
// index.html has the matching listener.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const data = event.notification.data || {};
  const view = data.type === 'prompt' ? 'prompt' : 'today';

  const params = new URLSearchParams({ view: view });
  if (data.client_id) params.set('client', data.client_id);
  if (data.date) params.set('date', data.date);
  const url = '/?' + params.toString();

  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: 'window', includeUncontrolled: true });

    for (const client of windows) {
      // Message first: if the app is alive it switches tab without a reload,
      // which keeps any unsent check-in in place.
      client.postMessage({ type: 'notification-click', view: view, data: data });
      if ('focus' in client) {
        await client.focus();
      }
      return;
    }

    // Nothing open — cold start with the params, which index.html reads.
    await clients.openWindow(url);
  })());
});
