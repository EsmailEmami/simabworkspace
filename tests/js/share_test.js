// Share on your domain: the page shim and the service worker:  node tests/js/share_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..', '..');
const ORIGIN = 'https://projects.example.com';
const tick = () => new Promise((r) => setTimeout(r, 20));

function storage() {
  const m = new Map();
  return {
    get length() { return m.size; },
    key(i) { return [...m.keys()][i] ?? null; },
    getItem(k) { return m.has(k) ? m.get(k) : null; },
    setItem(k, v) { m.set(k, String(v)); },
    removeItem(k) { m.delete(k); },
    _m: m,
  };
}

async function shim() {
  const jar = new Map([['mmd.a', '2'], ['esi.old', '1']]);
  class Document {}
  Object.defineProperty(Document.prototype, 'cookie', {
    configurable: true,
    get() { return [...jar].map(([k, v]) => k + '=' + v).join('; '); },
    set(v) { const head = v.split(';')[0]; const i = head.indexOf('='); jar.set(head.slice(0, i), head.slice(i + 1)); this.lastSet = v; },
  });
  const document = new Document();
  document.currentScript = { getAttribute: (n) => (n === 'data-t' ? 'esi/front' : n === 'data-p' ? '{"6000":"esi/api"}' : null) };
  const posted = [];
  const listeners = {};
  class WS { constructor(u, p) { this.url = u; this.protocols = p; } }
  WS.OPEN = 1;
  const ctx = {
    document, Document, URL, Proxy, Object, String, Promise,
    location: new URL(ORIGIN + '/products'),
    localStorage: storage(), sessionStorage: storage(),
    WebSocket: WS,
    navigator: { serviceWorker: {
      controller: { postMessage: (m) => posted.push(m) },
      addEventListener: (k, f) => { listeners[k] = f; },
      startMessages() {},
      register: () => Promise.resolve('own'),
      getRegistration: () => Promise.resolve('ours'),
    } },
  };
  ctx.window = ctx;
  ctx.localStorage.setItem('mmd/front:k', 'other');
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'gui/static/share-shim.js'), 'utf8'), ctx);

  assert.strictEqual(ctx.sessionStorage.getItem('__simabT'), 'esi/front');
  assert.strictEqual(JSON.stringify(posted), '[{"simabTab":"esi/front"}]');
  // WebSockets of the page go to its project
  assert.strictEqual(new ctx.WebSocket('/hmr?t=1').url, 'wss://projects.example.com/__simab/p/esi/front/hmr?t=1');
  assert.strictEqual(new ctx.WebSocket('wss://projects.example.com/', ['vite-hmr']).protocols[0], 'vite-hmr');
  assert.strictEqual(new ctx.WebSocket('wss://other.example.com/x').url, 'wss://other.example.com/x');
  assert.strictEqual(new ctx.WebSocket('/__simab/p/esi/front/x').url, ORIGIN.replace('https', 'wss') + '/__simab/p/esi/front/x');
  assert.strictEqual(ctx.WebSocket.OPEN, 1);
  assert.strictEqual(new ctx.WebSocket('wss://localhost:6000/ws?a=1').url, 'wss://projects.example.com/__simab/p/esi/api/ws?a=1');
  assert.strictEqual(new ctx.WebSocket('ws://localhost:7000/ws').url, 'ws://localhost:7000/ws', 'not shared: left alone');
  // cookies: only its own, set with its prefix
  assert.strictEqual(document.cookie, 'old=1');
  document.cookie = 'sid=9; Path=/api; Domain=x; Secure';
  assert.strictEqual(document.lastSet, 'esi.sid=9; Secure; path=/');
  assert.strictEqual(document.cookie, 'old=1; sid=9');
  // localStorage: its own keys only
  ctx.localStorage.setItem('k', 'v');
  ctx.localStorage.n = 5;
  assert.strictEqual(ctx.localStorage.getItem('k'), 'v');
  assert.strictEqual(ctx.localStorage.n, '5');
  assert.strictEqual(ctx.localStorage.length, 2);
  assert.strictEqual(Object.keys(ctx.localStorage).sort().join(), 'k,n');
  assert.strictEqual(ctx.localStorage.missing, undefined);
  ctx.localStorage.clear();
  assert.strictEqual(ctx.localStorage.length, 0);
  const real = Object.getOwnPropertyDescriptor(ctx, 'localStorage').get.call(ctx);
  assert.ok(real !== ctx.sessionStorage);
  // the worker asks "which project are you?" after it restarted
  let said = null;
  listeners.message({ data: { simabWho: 1 }, ports: [{ postMessage: (m) => { said = m; } }] });
  assert.strictEqual(said, 'esi/front');
  // the page's own service worker does not push ours out
  assert.strictEqual(await ctx.navigator.serviceWorker.register('/sw.js'), 'ours');
}

async function worker() {
  const on = {};
  const fetched = [];
  let answer = () => new Response('ok');
  const clients = { c9: { url: ORIGIN + '/products', postMessage: (m, ports) => ports[0].postMessage('mmd/front') } };
  const ctx = {
    URL, Headers, Response, Promise, Map, MessageChannel, setTimeout, String,
    self: {
      location: { origin: ORIGIN },
      addEventListener: (k, f) => { on[k] = f; },
      skipWaiting() {},
      clients: { claim: () => Promise.resolve(), get: (id) => Promise.resolve(clients[id] || null) },
    },
    fetch: (url, init) => { url = typeof url === 'string' ? url : url.url; fetched.push({ url, init }); return Promise.resolve(answer(url)); },
  };
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'gui/static/share-sw.js'), 'utf8'), ctx);
  const ev = (url, o) => {
    const e = Object.assign({ request: Object.assign({ url, mode: 'cors', method: 'GET', headers: new Headers({ Accept: '*/*' }), cache: 'default', destination: '' }, o.request || {}),
      clientId: '', resultingClientId: '' }, o, { request: undefined });
    e.request = Object.assign({ url, mode: 'cors', method: 'GET', headers: new Headers({ Accept: '*/*' }), cache: 'default', destination: '' }, o.request || {});
    e.respondWith = (p) => { e.p = p; };
    on.fetch(e);
    return e;
  };
  const claim = (url, t) => { let ok; on.message({ data: { simabClaim: url, t }, ports: [{ postMessage: (m) => { ok = m.ok; } }] }); return ok; };

  assert.strictEqual(claim(ORIGIN + '/page?x=1#top', 'esi/front'), true);
  assert.strictEqual(claim(ORIGIN + '/page?x=1', 'mmd/front'), false, 'one tab at a time per address');
  // the page of the tab that claimed it
  let e = ev(ORIGIN + '/page?x=1', { request: { mode: 'navigate' }, resultingClientId: 'c1' });
  await e.p;
  assert.strictEqual(fetched[0].url, '/__simab/p/esi/front/page?x=1');
  assert.strictEqual(fetched[0].init.headers.get('X-Simab-Nav'), '1');
  assert.strictEqual(fetched[0].init.headers.get('X-Simab-SW'), '1');
  assert.strictEqual(fetched[0].init.redirect, 'manual');
  // its /assets/… go to the same project
  e = ev(ORIGIN + '/assets/a.js', { clientId: 'c1' });
  await e.p;
  assert.strictEqual(fetched[1].url, '/__simab/p/esi/front/assets/a.js');
  assert.strictEqual(fetched[1].init.headers.get('X-Simab-Nav'), null);
  // another tab (unknown after a restart) says its project
  e = ev(ORIGIN + '/assets/a.js', { clientId: 'c9' });
  await e.p;
  assert.strictEqual(fetched[2].url, '/__simab/p/mmd/front/assets/a.js');
  // a body goes along
  e = ev(ORIGIN + '/api', { clientId: 'c1', request: { method: 'POST', clone: () => ({ arrayBuffer: () => Promise.resolve(new Uint8Array([104, 105]).buffer) }) } });
  await e.p;
  assert.strictEqual(Buffer.from(fetched[3].init.body).toString(), 'hi');
  // a page nobody claimed: the server's page asks the tab (no answer from here)
  e = ev(ORIGIN + '/other', { request: { mode: 'navigate' } });
  assert.strictEqual(e.p, undefined);
  // other sites and our own files are not touched
  assert.strictEqual(ev('https://cdn.example.com/x.js', { clientId: 'c1' }).p, undefined);
  assert.strictEqual(ev(ORIGIN + '/__simab/sw.js', { clientId: 'c1' }).p, undefined);
  // a redirect of a page: claimed for the same tab, then followed
  answer = () => new Response('', { headers: { 'X-Simab-Location': '/login' } });
  claim(ORIGIN + '/go', 'esi/front');
  e = ev(ORIGIN + '/go', { request: { mode: 'navigate' }, resultingClientId: 'c2' });
  const r = await e.p;
  assert.strictEqual(r.status, 302);
  assert.strictEqual(r.headers.get('Location'), ORIGIN + '/login');
  assert.strictEqual(claim(ORIGIN + '/login', 'mmd/front'), false, 'the redirect target is kept for this tab');
  // a worker script: its own fetches belong to the same project
  answer = () => new Response('ok');
  e = ev(ORIGIN + '/w.js', { clientId: 'c1', request: { destination: 'worker' } });
  await e.p;
  clients.w1 = { url: ORIGIN + '/w.js', postMessage() { throw new Error('not asked'); } };
  e = ev(ORIGIN + '/data', { clientId: 'w1' });
  await e.p;
  assert.strictEqual(fetched[fetched.length - 1].url, '/__simab/p/esi/front/data');
  // the panel calls its API on https://localhost:6000 → the shared port 6000 (this project's first)
  answer = (url) => (url.indexOf('/__simab/ports') === 0
    ? new Response(JSON.stringify([{ port: 6000, t: 'esi/api' }, { port: 6000, t: 'mmd/api' }]))
    : new Response('ok'));
  e = ev('https://localhost:6000/api/v1/captcha?x=1', { clientId: 'c1', request: { mode: 'cors' } });
  await e.p;
  const last = fetched[fetched.length - 1];
  assert.strictEqual(fetched[fetched.length - 2].url, '/__simab/ports?t=esi%2Ffront');
  assert.strictEqual(last.url, '/__simab/p/esi/api/api/v1/captcha?x=1');
  assert.strictEqual(last.init.headers.get('X-Simab-From'), 'esi/front');
  const n = fetched.length;
  e = ev('http://localhost:7000/x', { clientId: 'c1' });
  await e.p;
  assert.strictEqual(fetched[n].url, 'http://localhost:7000/x', 'a port that is not shared goes out as it is');
  assert.strictEqual(ev('http://localhost:6000/', { clientId: 'c1', request: { mode: 'navigate' } }).p, undefined);
}

(async () => {
  await shim();
  await worker();
  await tick();
  console.log('share ok');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
