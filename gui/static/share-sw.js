// Simab Workspace — Share on your domain: keeps every tab on its own project, so several projects
// on one domain never mix their /assets/…, /api/… (see "share" in server.py).
'use strict';
var tabs = new Map();      // client id → "project/name"
var claims = new Map();    // a page about to open → "project/name" (said by the page that opens it)
var workers = new Map();   // worker script URL → "project/name"

function noHash(u) { return String(u).split('#')[0]; }
function claim(url, t) {
  claims.set(url, t);
  setTimeout(function () { if (claims.get(url) === t) claims.delete(url); }, 10000);
}

self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('message', function (e) {
  var d = e.data || {}, port = e.ports && e.ports[0];
  if (typeof d.simabClaim === 'string' && typeof d.t === 'string') {
    var url = noHash(d.simabClaim), free = !claims.has(url);
    if (free) claim(url, d.t);
    if (port) port.postMessage({ ok: free });
  }
  if (typeof d.simabTab === 'string' && e.source && e.source.id) tabs.set(e.source.id, d.simabTab);
});

function ask(id) {                 // after a restart of this worker: the page says which project it is
  return self.clients.get(id).then(function (c) {
    if (!c) return '';
    if (workers.has(c.url)) return workers.get(c.url);
    return new Promise(function (res) {
      var ch = new MessageChannel(), done = false;
      ch.port1.onmessage = function (e) { done = true; res(typeof e.data === 'string' ? e.data : ''); };
      c.postMessage({ simabWho: 1 }, [ch.port2]);
      setTimeout(function () { if (!done) res(''); }, 2000);
    });
  }).then(function (t) { if (t) tabs.set(id, t); return t; }, function () { return ''; });
}
function targetOf(id) {
  if (!id) return Promise.resolve('');
  return tabs.has(id) ? Promise.resolve(tabs.get(id)) : ask(id);
}

function pass(req, t, nav) {
  var u = new URL(req.url), h = new Headers(req.headers);
  h.set('X-Simab-SW', '1');
  if (nav) h.set('X-Simab-Nav', '1');
  var init = { method: req.method, headers: h, credentials: 'include', redirect: 'manual',
    cache: req.cache === 'only-if-cached' ? 'default' : req.cache };
  var body = req.method === 'GET' || req.method === 'HEAD' ? Promise.resolve(null) : req.clone().arrayBuffer();
  return body.then(function (b) {
    if (b) init.body = b;
    return fetch('/__simab/p/' + t + u.pathname + u.search, init);
  }).then(function (r) {
    var loc = r.headers.get('X-Simab-Location');
    if (!loc) return r;
    var to = new URL(loc, req.url);
    if (nav && to.origin === self.location.origin) claim(noHash(to.href), t);
    return Response.redirect(to.href, 302);       // followed by the browser, back through here
  });
}

self.addEventListener('fetch', function (e) {
  var req = e.request, u = new URL(req.url);
  if (u.origin !== self.location.origin || u.pathname.indexOf('/__simab/') === 0) return;
  if (req.mode === 'navigate') {
    var t = claims.get(noHash(req.url));
    if (!t) return;                // the server's page asks this tab's sessionStorage and comes back
    claims.delete(noHash(req.url));
    if (e.resultingClientId) tabs.set(e.resultingClientId, t);
    e.respondWith(pass(req, t, true));
    return;
  }
  e.respondWith(targetOf(e.clientId).then(function (t) {
    if (!t) return fetch(req);
    if (req.destination === 'worker' || req.destination === 'sharedworker') workers.set(req.url, t);
    return pass(req, t, false);
  }));
});
