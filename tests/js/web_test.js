// The web version (no token in the address, a sign-in cookie instead):  node tests/js/web_test.js
'use strict';
const path = require('path');
const assert = require('assert');
const { install, Element } = require('./dom');

const ROOT = path.resolve(__dirname, '..', '..');
let document = install();
global.location.protocol = 'https:';
global.location.reload = () => { reloaded++; };
let reloaded = 0;

function skeleton() {
  ['sidebar', 'projects', 'proj-filter', 'resizer', 'layout', 'empty', 'restore-box', 'toasts', 'menu-root', 'modal-root',
    'btn-palette', 'btn-history', 'btn-settings', 'btn-usage', 'update-note', 'usage', 'btn-hide-side', 'show-side', 'st-ssh', 'st-sync'].forEach((id) => {
    const el = new Element(id.startsWith('btn') || id === 'show-side' ? 'button' : 'div');
    el.setAttribute('id', id);
    document.body.append(el);
  });
}
skeleton();
global.Terminal = class { constructor(o) { this.options = o; } open() {} loadAddon() {} onData() {} onResize() {} onBell() {} onSelectionChange() {}
  attachCustomKeyEventHandler() {} focus() {} write() {} dispose() {} };
const addon = (name) => ({ [name]: class { fit() {} dispose() {} onContextLoss() {} } });
Object.assign(global, addon('FitAddon'), addon('SearchAddon'), addon('WebLinksAddon'), addon('Unicode11Addon'), addon('WebglAddon'));

let signedIn = false;
const calls = [];
const state = {
  version: '9', wsl: false, lastTabs: [], sessions: [], web: true,
  server: { configured: true, user: 'esi', host: '127.0.0.1', port: '22' },
  settings: { fontSize: 13, persianView: 'auto' },
  projects: [{ name: 'cheshmeh', source: 'manual', local: '', remote: '/home/esi/cheshmeh', localExists: false, sync: null, endpoint: {},
    commands: [{ id: 'c1', name: 'web', cmd: 'npm start', where: 'local', cwd: '', url: '' }, { id: 'c2', name: 'logs', cmd: 'docker compose logs -f', where: 'server', cwd: '', url: '' }] }]
};
global.fetch = (url, opts) => {
  opts = opts || {};
  const method = opts.method || 'GET';
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({ method, url, body, headers: opts.headers || {} });
  let status = 200, res = {};
  if (url === '/api/web-info') res = { web: true, hosts: ['127.0.0.1:22'], host: '127.0.0.1', port: '22' };
  else if (url === '/api/login') {
    if (body.password === 'right') signedIn = true;
    else { status = 401; res = { error: 'wrong user or password' }; }
  } else if (!signedIn) { status = 401; res = { error: 'sign in', login: true }; }
  else if (url === '/api/state') res = state;
  else if (url === '/api/projects') res = { projects: state.projects };
  else if (url.startsWith('/api/status')) res = { ssh: 'ok', daemon: false };
  else if (url.startsWith('/api/git-summary')) res = { summary: {} };
  else if (url.startsWith('/api/docker-summary')) res = { summary: {}, events: [], seq: 0 };
  else if (url === '/api/usage') res = { limits: {}, today: 0, week: 0, days: [] };
  else if (url === '/api/logout') res = { ok: true };
  else if (url === '/api/sessions' && method === 'POST') res = { id: 's1', title: 'cheshmeh · server', alive: true, exitCode: null, url: '',
    spec: { kind: body.kind, project: body.project, where: body.where, cmdId: '' } };
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(res) });
};

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }
const q = (sel, root) => (root || document.body).querySelectorAll(sel);

require(path.join(ROOT, 'gui/static/app.js'));
global.SW && require(path.join(ROOT, 'gui/static/chat.js'));

const tests = {
  async 'not signed in: the sign-in page, with the one allowed server shown'() {
    await settle();
    const form = q('.login')[0];
    assert.ok(form, 'sign-in form');
    assert.match(q('.login-host', form)[0].textContent, /^127\.0\.0\.1$/);
    assert.ok(!calls.some((c) => c.url === '/api/state' && c.headers['X-Token']), 'no token needed');
  },

  async 'a wrong password shows the error; the right one reloads'() {
    const form = q('.login')[0];
    const [user, pw] = q('input', form);
    user.value = 'esi'; pw.value = 'nope';
    form.dispatch('submit', { preventDefault() {} });
    await settle();
    assert.match(q('.add-err', form)[0].textContent, /wrong user or password/);
    assert.strictEqual(pw.value, '', 'password cleared');
    const sent = calls.filter((c) => c.url === '/api/login').pop();
    assert.deepStrictEqual(sent.body, { host: '127.0.0.1', port: '22', user: 'esi', password: 'nope' });
    assert.strictEqual(sent.headers['X-SW'], '1');
    pw.value = 'right';
    form.dispatch('submit', { preventDefault() {} });
    await settle();
    assert.strictEqual(reloaded, 1);
  },

  async 'signed in: only what works on the server is offered'() {
    // the page reloads: a fresh document and a fresh copy of the code
    const fetch0 = global.fetch;
    document = install();
    global.location.protocol = 'https:';
    global.location.reload = () => { reloaded++; };
    global.fetch = fetch0;
    skeleton();
    for (const f of ['app.js', 'chat.js']) delete require.cache[require.resolve(path.join(ROOT, 'gui/static', f))];
    require(path.join(ROOT, 'gui/static/app.js'));
    require(path.join(ROOT, 'gui/static/chat.js'));
    await settle();
    assert.strictEqual(q('.login').length, 0);
    q('.proj-head')[0].dispatch('click');
    const acts = q('.act').map((b) => b.textContent);
    assert.deepStrictEqual(acts, ['Claude', 'Terminal', 'Git', 'Docker', 'History'], 'no Code, no Ports');
    assert.deepStrictEqual(q('.cmd').map((e) => e.querySelector('.nm').textContent), ['logs'], 'only server run commands');
    document.body.querySelector('#btn-settings').click();
    const menu = q('button').map((b) => b.textContent.trim());
    assert.ok(menu.includes('Sign out'));
    assert.ok(!menu.includes('Quit') && !menu.includes('Check for updates'));
    q('button').find((b) => b.textContent.trim() === 'Settings…').click();
    await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Signed in as esi@127\.0\.0\.1/);
    assert.ok(!/Uninstall|SSH key|Sync service/.test(modal.textContent));
    assert.ok(!calls.some((c) => c.url === '/api/connection' || c.url.startsWith('/api/update') || c.url === '/api/tunnels'), 'nothing local is asked for');
    document.body.querySelector('#modal-root').replaceChildren();
    // the project menu has no local terminal / VS Code / port forwarding
    q('.proj-head')[0].querySelector('.icon-btn').click();
    const items = q('button').map((b) => b.textContent.trim());
    assert.ok(items.includes('Server terminal'));
    assert.ok(!items.some((t) => /Local terminal|VS Code|Port forwarding/.test(t)));
    // terminals connect over wss on https
    q('.act').find((b) => b.textContent === 'Terminal').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sessions').pop();
    assert.ok(post, 'session asked for');
  }
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String(e.stack || e).split('\n').slice(0, 5).join('\n     ')); }
  }
  process.exit(failed ? 1 : 0);
})();
