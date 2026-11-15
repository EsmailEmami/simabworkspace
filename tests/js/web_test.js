// The web version (no token in the address, a sign-in cookie instead):  node tests/js/web_test.js
'use strict';
const path = require('path');
const assert = require('assert');
const { install, Element } = require('./dom');

const ROOT = path.resolve(__dirname, '..', '..');
let document = install();
global.location.protocol = 'https:';
global.location.reload = () => { reloaded++; };
let sessSeq = 0;
let reloaded = 0;

function skeleton() {
  ['sidebar', 'projects', 'proj-filter', 'resizer', 'layout', 'empty', 'restore-box', 'toasts', 'menu-root', 'modal-root',
    'btn-palette', 'btn-history', 'btn-settings', 'btn-usage', 'update-note', 'usage', 'server-stats', 'btn-hide-side', 'show-side', 'st-ssh', 'st-sync',
    'empty-hint', 'empty-actions'].forEach((id) => {
    const el = new Element(id.startsWith('btn') || id === 'show-side' ? 'button' : 'div');
    el.setAttribute('id', id);
    document.body.append(el);
  });
}
skeleton();
global.Terminal = class {
  constructor(o) { (global.terms = global.terms || []).push(this); this.options = Object.assign({}, o); this.cols = o.cols; this.rows = o.rows; this.unicode = {}; this.buffer = { active: { length: 0, getLine: () => null } }; }
  open() {} loadAddon() {} onData(f) { this.dataCb = f; } onResize() {} onBell() {} onSelectionChange() {}
  attachCustomKeyEventHandler() {} focus() {} write() {} reset() {} dispose() {} hasSelection() { return false; } getSelection() { return ''; } clear() {}
};
const addon = (name) => ({ [name]: { [name]: class { fit() {} findNext() { return true; } findPrevious() { return true; } onContextLoss() {} dispose() {} } } });
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
  else if (url.startsWith('/api/browse')) {
    const p = decodeURIComponent(url.split('path=')[1] || '~');
    const tree = { '/home/esi': ['cheshmeh', 'hamava', '.cache'], '/home/esi/cheshmeh': ['cheshmeh-api-service', 'cheshmeh-web-app'] };
    const path = p === '~' ? '/home/esi' : p;
    res = { path, parent: path === '/' ? '' : path.replace(/\/[^/]+$/, '') || '/', home: '/home/esi', git: false,
      dirs: (tree[path] || []).map((n) => ({ name: n, git: n.startsWith('cheshmeh-') ? 'repo' : '', hidden: n[0] === '.', project: '' })) };
  } else if (url === '/api/manual') res = { project: body };
  else if (url === '/api/logout') res = { ok: true };
  else if (url === '/api/sessions' && method === 'POST') res = { id: 's' + (++sessSeq), title: 'cheshmeh · server', alive: true, exitCode: null, url: '',
    spec: { kind: body.kind, project: body.project, where: body.where, cmdId: '' } };
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(res) });
};

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }
const q = (sel, root) => (root || document.body).querySelectorAll(sel);

require(path.join(ROOT, 'gui/static/app.js'));
global.SW && require(path.join(ROOT, 'gui/static/chat.js'));
global.SW && require(path.join(ROOT, 'gui/static/files.js'));

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
    // this time: a phone (narrow touch screen)
    global.matchMedia = (q) => ({ matches: true, media: q, addEventListener() {} });
    for (const f of ['app.js', 'chat.js', 'files.js']) delete require.cache[require.resolve(path.join(ROOT, 'gui/static', f))];
    require(path.join(ROOT, 'gui/static/app.js'));
    require(path.join(ROOT, 'gui/static/chat.js'));
    require(path.join(ROOT, 'gui/static/files.js'));
    await settle();
    assert.strictEqual(q('.login').length, 0);
    // phone, nothing open yet: buttons instead of "press Ctrl+Shift+P"
    assert.strictEqual(document.body.querySelector('#empty-hint').textContent, 'Open a project to start.');
    const emptyBtns = q('.btn', document.body.querySelector('#empty-actions'));
    assert.deepStrictEqual(emptyBtns.map((b) => b.textContent), ['☰  Projects', '⌕  Search']);
    emptyBtns[0].click();
    assert.ok(document.body.classList.contains('side-open'), 'opens the projects drawer');
    q('#side-backdrop')[0].click();
    q('.proj-head')[0].dispatch('click');
    assert.deepStrictEqual(q('.act').map((b) => b.textContent), ['Claude', 'Git', '⋯']);
    assert.deepStrictEqual(q('.cmd').map((e) => [e.querySelector('.nm').textContent, e.classList.contains('off')]), [['web', true], ['logs', false]],
      'a local run command is shown, disabled');
    document.body.querySelector('#btn-settings').click();
    const menu = q('button').map((b) => b.textContent.trim());
    assert.ok(menu.includes('Sign out'));
    const quit = q('button').find((b) => b.textContent.trim().indexOf('Quit') === 0);
    assert.ok(quit.disabled && /installed app/.test(quit.textContent), 'Quit shown disabled');
    q('button').find((b) => b.textContent.trim() === 'Settings…').click();
    await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Signed in as esi@127\.0\.0\.1/);
    assert.ok(!q('button', modal).some((b) => /Uninstall|New key|Save & test|Stop service/.test(b.textContent)), 'no local setup controls');
    assert.match(modal.textContent, /In the installed app.*Two-way sync/);
    assert.ok(!calls.some((c) => c.url === '/api/connection' || c.url.startsWith('/api/update') || c.url === '/api/tunnels'), 'nothing local is asked for');
    document.body.querySelector('#modal-root').replaceChildren();
    // the project menu has no local terminal / VS Code / port forwarding
    q('.proj-head')[0].querySelector('.icon-btn').click();
    const items = q('button').map((b) => b.textContent.trim());
    assert.ok(items.includes('Server terminal'));
    for (const name of ['Local terminal', 'VS Code on server', 'Port forwarding…']) {
      const b = q('button').find((x) => x.textContent.trim().indexOf(name) === 0);
      assert.ok(b && b.disabled, name + ' shown, disabled');
    }
    // add a project: browse the server's folders and pick one
    document.body.querySelector('#modal-root').replaceChildren();
    q('.add-link').find((b) => /add project/.test(b.textContent)).click();
    await settle();
    const addModal = q('.modal').pop();
    assert.ok(!/null/.test(addModal.textContent), 'no stray "null"');
    assert.deepStrictEqual(q('.segb', addModal).map((b) => [b.querySelector('b').textContent, !!b.disabled]),
      [['Server folder', false], ['Sync to my computer', true], ['Upload from my computer', true]]);
    assert.deepStrictEqual(q('.fname', addModal).map((e) => e.textContent), ['..', 'cheshmeh', 'hamava'], 'hidden folders are hidden');
    q('.frow', addModal).find((r) => r.querySelector('.fname').textContent === 'cheshmeh').click();
    await settle();
    assert.deepStrictEqual(q('.fname', addModal).map((e) => e.textContent), ['..', 'cheshmeh-api-service', 'cheshmeh-web-app']);
    assert.match(addModal.textContent, /Has Git repositories inside/);
    assert.ok(q('input', addModal).some((i) => i.value === 'cheshmeh'), 'name filled from the folder');
    q('.btn.primary', addModal).pop().click();
    await settle();
    const add = calls.filter((c) => c.url === '/api/manual').pop();
    assert.deepStrictEqual(add.body, { name: 'cheshmeh', local: '', remote: '/home/esi/cheshmeh' });
    // terminals connect over wss on https
    document.body.querySelector('#menu-root').replaceChildren();
    q('.act.more')[0].click();
    q('button').find((b) => b.textContent.trim() === 'Server terminal').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sessions').pop();
    assert.ok(post, 'session asked for');
    // phone: drawer menu, one section, keys a touch keyboard lacks
    assert.ok(document.body.classList.contains('compact') && document.body.classList.contains('touch'));
    q('.menu-btn')[0].click();
    assert.ok(document.body.classList.contains('side-open'), 'the ☰ button opens the projects drawer');
    q('#side-backdrop')[0].click();
    assert.ok(!document.body.classList.contains('side-open'));
    const keys = q('.kb').map((b) => b.textContent);
    assert.deepStrictEqual(keys.slice(0, 8), ['Esc', 'Tab', 'Ctrl', '^C', '↑', '↓', '←', '→']);
    const ws = global.sockets[global.sockets.length - 1];
    ws.onopen && ws.onopen();
    q('.kb').find((b) => b.textContent === 'Esc').click();
    assert.deepStrictEqual(ws.sent.pop(), { t: 'i', d: '\x1b' });
    assert.match(ws.url, /^wss:/);
    // the chat on a phone: short hint, Enter = new line (the ↑ button sends), icons instead of words
    q('.act').find((b) => b.textContent === 'Claude').click();
    await settle();
    const chatWs = global.sockets[global.sockets.length - 1];
    chatWs.onopen && chatWs.onopen();
    const cta = q('textarea').pop();
    assert.ok(!/Esc/.test(cta.getAttribute('placeholder')), 'no keyboard hint on a phone');
    cta.value = 'سلام';
    const before = chatWs.sent.length;
    cta.dispatch('keydown', { key: 'Enter' });
    assert.strictEqual(chatWs.sent.filter((m) => m.t === 'send').length, 0, 'Enter does not send on a touch keyboard');
    assert.ok(chatWs.sent.length >= before);
    assert.deepStrictEqual(q('.ico').map((e) => e.textContent).slice(-2), ['⟲', '✎']);
    // back to the terminal tab for the key bar checks
    q('.tab').find((t) => /server/.test(t.textContent)).dispatch('click');
    await settle();
    // Ctrl, then a letter on the touch keyboard → the control key
    q('.kb').find((b) => b.textContent === 'Ctrl').click();
    assert.ok(q('.kb').find((b) => b.textContent === 'Ctrl').classList.contains('on'));
    global.terms[global.terms.length - 1].dataCb('d');
    assert.deepStrictEqual(ws.sent.pop(), { t: 'i', d: '\x04' });
    assert.ok(!q('.kb').find((b) => b.textContent === 'Ctrl').classList.contains('on'), 'Ctrl is used once');
    global.terms[global.terms.length - 1].dataCb('d');
    assert.deepStrictEqual(ws.sent.pop(), { t: 'i', d: 'd' });
  }
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String(e.stack || e).split('\n').slice(0, 30).join('\n     ')); }
  }
  process.exit(failed ? 1 : 0);
})();
