/* Simab Workspace service worker: makes the web version installable and opens it fast.
   Network first (a new version is used at once); the saved copy only when offline.
   Never touches /api or /ws. */
'use strict';
var CACHE = 'simab-shell-2';
var SHELL = ['/', '/app.css', '/app.js', '/chat.js', '/files.js', '/icon.svg', '/manifest.webmanifest', '/vendor/xterm.css', '/vendor/xterm.js',
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
    return caches.match(req).then(function (hit) { return hit || caches.match('/'); });
  }));
});
