/* Simab Workspace service worker: makes the web version installable and opens it fast.
   Network first (a new version is used at once); the saved copy only when offline.
   Never touches /api or /ws. */
'use strict';
var CACHE = 'simab-shell-12';
var SHELL = ['/', '/app.css', '/voice.js', '/sounds.js', '/qr.js', '/app.js', '/chat.js', '/files.js', '/tasks.js', '/computers.js', '/agents.js', '/prompts.js', '/icon.svg', '/manifest.webmanifest', '/vendor/xterm.css', '/vendor/xterm.js',
  '/vendor/addon-fit.js', '/vendor/addon-web-links.js', '/vendor/addon-search.js', '/vendor/addon-unicode11.js', '/vendor/addon-webgl.js'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function () {}).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  var url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.indexOf('/api/') === 0 || url.pathname === '/ws') return;
  e.respondWith(fetch(req).then(function (res) {
    if (res.ok && res.type === 'basic') {
      var copy = res.clone();
      caches.open(CACHE).then(function (c) { c.put(url.pathname === '/index.html' ? '/' : req, copy); });
    }
    return res;
  }).catch(function () {
    return caches.match(req).then(function (hit) { return hit || (req.mode === 'navigate' ? caches.match('/') : Response.error()); });
  }));
});

// Phone notifications: the push itself carries nothing; ask the app what happened.
self.addEventListener('push', function (e) {
  e.waitUntil(self.registration.pushManager.getSubscription().then(function (sub) {
    return fetch('/api/push/pending', { method: 'POST', credentials: 'same-origin',
      headers: { 'X-SW': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint: sub ? sub.endpoint : '' }) });
  }).then(function (r) { return r.ok ? r.json() : { messages: [] }; }).catch(function () { return { messages: [] }; }).then(function (d) {
    var msgs = d.messages || [];
    if (!msgs.length) msgs = [{ title: 'Simab Workspace', body: 'Claude needs you', tag: 'sw', tab: '' }];
    return Promise.all(msgs.map(function (m) {
      return self.registration.showNotification(m.title, { body: m.body, tag: m.tag, renotify: true, icon: '/icon-192.png', badge: '/icon-192.png', data: { tab: m.tab || '' } });
    }));
  }));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var tab = (e.notification.data && e.notification.data.tab) || '';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    var c = list.find(function (x) { return new URL(x.url).origin === location.origin; });
    if (c) { if (tab) c.postMessage({ sw: 'open-tab', tab: tab }); return c.focus(); }
    return self.clients.openWindow('/' + (tab ? '#open=' + tab : ''));
  }));
});
