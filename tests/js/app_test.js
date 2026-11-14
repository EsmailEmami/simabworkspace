// Boots the whole app.js on a fake DOM with a fake server:  node tests/js/app_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { install, Element } = require('./dom');

const ROOT = path.resolve(__dirname, '..', '..');
const document = install();
global.location.hash = '#t=testtoken';

// page skeleton (ids used by app.js)
['sidebar', 'projects', 'proj-filter', 'resizer', 'layout', 'empty', 'restore-box', 'toasts', 'menu-root', 'modal-root',
  'btn-palette', 'btn-history', 'btn-settings', 'update-note', 'btn-hide-side', 'show-side', 'st-ssh', 'st-sync'].forEach((id) => {
  const el = new Element(id.startsWith('btn') || id === 'show-side' ? 'button' : 'div');
  el.setAttribute('id', id);
  document.body.append(el);
});

// xterm + addons
class FakeTerm {
  constructor(o) { this.options = Object.assign({}, o); this.cols = o.cols; this.rows = o.rows; this.written = ''; this.unicode = {}; this.buffer = { active: { length: 0, getLine: () => null } }; }
  open(el) { this.el = el; } loadAddon() {} onData(f) { this.dataCb = f; } onResize() {} onBell() {} onSelectionChange() {}
  attachCustomKeyEventHandler() {} focus() {} write(d) { this.written += d; } reset() { this.written = ''; } dispose() {}
  hasSelection() { return false; } getSelection() { return ''; } clear() {} paste() {} clearSelection() {}
}
global.Terminal = FakeTerm;
const addon = (name) => ({ [name]: class { fit() {} findNext() { return true; } findPrevious() { return true; } onContextLoss() {} dispose() {} } });
Object.assign(global, { FitAddon: addon('FitAddon'), SearchAddon: addon('SearchAddon'), WebLinksAddon: addon('WebLinksAddon'),
  Unicode11Addon: addon('Unicode11Addon'), WebglAddon: addon('WebglAddon') });

// fake server
const calls = [];
const gitStatus = { repo: true, where: 'local', branch: 'main', upstream: 'origin/main', ahead: 0, behind: 2, conflicts: 1, last: 'abc first',
  files: [{ path: 'api/main.go', x: ' ', y: 'M', conflict: false, untracked: false }, { path: 'web/x.ts', x: 'U', y: 'U', conflict: true, untracked: false }] };
let seq = 0;
const state = {
  version: '9', wsl: false, lastTabs: [], sessions: [],
  server: { configured: true, user: 'esi', host: 'h', port: '225' },
  settings: { fontSize: 13, persianView: 'auto', copyOnSelect: true, confirmClose: true },
  projects: [
    { name: 'cheshmeh', source: 'mutagen', local: '/l/cheshmeh', remote: '/home/esi/cheshmeh', localExists: true,
      sync: { state: 'ok', label: 'in sync', conflicts: 0 }, endpoint: {}, commands: [{ id: 'c1', name: 'web', cmd: 'npm start', where: 'local', cwd: 'web', url: '' }] },
    { name: 'shop', source: 'manual', local: '', remote: '/srv/shop', localExists: false, sync: null, endpoint: {}, commands: [] }
  ]
};
let offline = false;
global.fetch = (url, opts) => {
  if (offline) return Promise.reject(new TypeError('Failed to fetch'));
  const method = (opts && opts.method) || 'GET';
  const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({ method, url, body });
  let res = {};
  if (url === '/api/state') res = state;
  else if (url === '/api/projects') res = { projects: state.projects };
  else if (url.startsWith('/api/status')) res = { ssh: 'ok', daemon: true };
  else if (url.startsWith('/api/git/cheshmeh/repos')) res = { where: 'local', gitSync: true, repos: ['cheshmeh-api-service', 'cheshmeh-web-app'],
    statuses: { 'cheshmeh-api-service': Object.assign({}, gitStatus, { files: [] }), 'cheshmeh-web-app': gitStatus } };
  else if (url.startsWith('/api/git/cheshmeh/status')) res = gitStatus;
  else if (url.startsWith('/api/color/')) res = { color: body.color };
  else if (url.startsWith('/api/git-summary')) res = { summary: { cheshmeh: { changes: 3, ahead: 1, behind: 2, conflicts: 0, repos: 2 }, shop: { changes: 0, ahead: 0, behind: 0, conflicts: 0, repos: 1 } } };
  else if (url === '/api/connection') res = { user: 'esi', host: 'h', port: '225', key: '', keys: [{ path: '/home/me/.ssh/id_ed25519', type: 'ed25519', comment: 'me' }], autostart: true, daemon: true };
  else if (url === '/api/prereqs' && method === 'GET') res = { items: [{ name: 'mutagen', ok: true, version: '0.18', why: 'sync', sudo: false }, { name: 'jq', ok: false, version: '', why: 'scripts', sudo: false }], docker: false, wsl: false, sudoWithoutPassword: false };
  else if (url.startsWith('/api/prereqs/')) res = { ok: true };
  else if (url.startsWith('/api/move/')) res = { ok: true };
  else if (url === '/api/sync/cheshmeh/details') res = { name: 'cheshmeh', state: 'ok', label: 'in sync', status: 'watching', paused: false, local: '/l/cheshmeh', remote: '/home/esi/cheshmeh',
    server: 'esi@h', error: '', cycles: 3, ignores: ['node_modules'], git: true, progress: null,
    conflicts: [{ path: 'web/app.ts', local: [{ path: 'web/app.ts', was: 'file', now: 'file' }], server: [{ path: 'web/app.ts', was: 'file', now: 'deleted' }] }] };
  else if (url === '/api/sync/cheshmeh/resolve') res = { ok: true };
  else if (url === '/api/server-folders') res = { folders: [{ path: '/home/esi/shop', name: 'shop', git: 'repo', mtime: 1, project: '' }, { path: '/home/esi/cheshmeh', name: 'cheshmeh', git: 'repos inside', mtime: 2, project: 'cheshmeh' }] };
  else if (url === '/api/add-project') res = { ok: true, project: 'shop' };
  else if (url.startsWith('/api/update') && method === 'GET') res = { current: '9', latest: '10.0.0', available: true, notes: '## Changes\n- faster', url: 'https://x' };
  else if (url === '/api/update' && method === 'POST') res = { ok: true, version: '10.0.0' };
  else if (url.startsWith('/api/activity')) res = { items: [
    { t: Date.now() / 1000, kind: 'prompt', project: 'cheshmeh', text: 'باگ پرداخت را درست کن', session: '11111111-2222-3333-4444-555555555555' },
    { t: Date.now() / 1000 - 60, kind: 'run-exit', project: 'cheshmeh', text: 'web', code: 1, seconds: 12.5, cmdId: 'c1' },
    { t: Date.now() / 1000 - 86400 * 3, kind: 'git-commit', project: 'cheshmeh', text: 'payment bug', repo: 'cheshmeh-api-service' }] };
  else if (url.startsWith('/api/git/cheshmeh/diff')) res = { diff: '@@ -1 +1 @@\n-a\n+b' };
  else if (url.startsWith('/api/git/cheshmeh/commit')) res = { output: 'ok', status: Object.assign({}, gitStatus, { files: [gitStatus.files[1]], ahead: 1 }) };
  else if (url === '/api/sessions' && method === 'POST') {
    res = { id: 's' + (++seq), title: body.kind + ' ' + (body.project || 'home'), alive: true, exitCode: null, url: '',
      spec: Object.assign({ kind: body.kind, project: body.project || '', where: body.where || 'local', cmdId: body.cmdId || '' }, body.resume ? { resume: body.resume } : {}) };
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(res) });
};

const tick = () => new Promise((r) => setTimeout(r, 0));
// the sidebar's ⟲ button calls the API (like the periodic refreshes do)
const refreshFromOutside = async () => { document.body.querySelector('#btn-history').click(); await settle(); document.body.querySelector('#modal-root').replaceChildren(); };
async function settle() { for (let i = 0; i < 8; i++) await tick(); }
const q = (sel, root) => (root || document.body).querySelectorAll(sel);
const byText = (sel, text) => q(sel).find((e) => e.textContent.trim() === text);

require(path.join(ROOT, 'gui/static/app.js'));
global.SW && require(path.join(ROOT, 'gui/static/chat.js'));

const tests = {
  async 'boots, shows projects and status, no Persian in the UI'() {
    await settle();
    assert.strictEqual(q('.proj').length, 2);
    assert.match(document.body.querySelector('#st-ssh').className, /ok/);
    assert.ok(!/[؀-ۿ]/.test(document.body.textContent), 'English only');
    assert.strictEqual(document.body.querySelector('#empty').classList.contains('hidden'), false, 'empty state shown');
  },

  async 'project actions open tabs; Terminal goes to the server'() {
    q('.proj-head')[0].dispatch('click');
    byText('.act', 'Terminal').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sessions' && c.method === 'POST').pop();
    assert.deepStrictEqual([post.body.kind, post.body.where, post.body.project], ['shell', 'server', 'cheshmeh']);
    assert.strictEqual(q('.tab').length, 1);
    assert.ok(document.body.querySelector('#empty').classList.contains('hidden'));
  },

  async 'run command opens a run tab'() {
    q('.cmd')[0].dispatch('click');
    await settle();
    const post = calls.filter((c) => c.url === '/api/sessions' && c.method === 'POST').pop();
    assert.deepStrictEqual([post.body.kind, post.body.cmdId], ['run', 'c1']);
    assert.strictEqual(q('.tab').length, 2);
  },

  async 'split right makes two sections that fill the space'() {
    q('.gtools')[0].querySelectorAll('button')[0].click();     // split right
    await settle();
    assert.strictEqual(q('.group').length, 2);
    const cells = q('.cell');
    assert.strictEqual(cells.length, 2);
    cells.forEach((c) => assert.match(c.style.cssText, /^flex:500 1 0px$/));
  },

  async 'git panel lists changes, shows conflicts and commits the picked files'() {
    q('.act').find((b) => b.textContent.indexOf('Git') === 0).click();
    await settle();
    const modal = document.body.querySelector('.modal');
    assert.ok(modal, 'git dialog open');
    const repoBtns = q('.grepo', modal);
    assert.deepStrictEqual(repoBtns.map((b) => b.textContent), ['cheshmeh-api-service', 'cheshmeh-web-app2']);
    assert.ok(repoBtns[1].classList.contains('on'), 'opens the repository that has changes');
    assert.strictEqual(q('.gfile', modal).length, 2);
    assert.match(q('.gbanner', modal)[0].textContent, /conflicts/);
    assert.match(q('.ghead', modal)[0].textContent, /↓2/);
    q('.gfile', modal)[0].dispatch('click');
    await settle();
    assert.match(q('.gdiff', modal)[0].textContent, /\+b/);
    const boxes = q('.gfile', modal).map((f) => f.querySelector('input'));
    boxes[1].checked = false; boxes[1].dispatch('change');
    modal.querySelector('textarea').value = 'fix api';
    const commitBtn = q('.btn.primary', modal).pop();
    assert.strictEqual(commitBtn.textContent, 'Commit 1');
    commitBtn.click();
    await settle();
    const post = calls.filter((c) => c.url.endsWith('/commit')).pop();
    assert.deepStrictEqual(post.body, { repo: 'cheshmeh-web-app', where: 'local', message: 'fix api', files: ['api/main.go'] });
    assert.ok(calls.some((c) => c.url.indexOf('/diff?repo=cheshmeh-web-app&where=local&path=') > 0), 'diff asks the right repository');
    assert.strictEqual(q('.gfile', modal).length, 1);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'a Claude tab inside the real app loads history and streams (no name clashes)'() {
    const before = global.sockets.length;
    byText('.act', 'Claude').click();
    await settle();
    const ws = global.sockets[global.sockets.length - 1];
    assert.ok(global.sockets.length > before, 'chat connected');
    ws.onopen();
    const feed = (m) => ws.onmessage({ data: JSON.stringify(m) });
    feed({ t: 's', state: { alive: true, busy: false }, events: [{ type: '_history', entries: [
      { type: 'user', message: { content: [{ type: 'text', text: 'سلام' }] } },
      { type: 'assistant', message: { id: 'h1', content: [{ type: 'text', text: 'سلام! چطور کمکت کنم؟' }] } }] }] });
    feed({ t: 'e', e: { type: 'stream_event', parent_tool_use_id: null, event: { type: 'message_start', message: { id: 'm2' } } } });
    feed({ t: 'e', e: { type: 'stream_event', parent_tool_use_id: null, event: { type: 'content_block_start', index: 0, content_block: { type: 'text' } } } });
    feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm2', content: [{ type: 'text', text: 'خوبم' }] } } });
    const pane = q('.pane.chat').pop();
    assert.strictEqual(q('.amsg', pane).length, 2);
    assert.match(pane.textContent, /چطور کمکت کنم/);
    assert.ok(!q('.toast').some((t) => /error/i.test(t.textContent)), 'no display errors');
  },

  async 'each project has its own colour on its tabs'() {
    const tabs = q('.tab');
    const proj = tabs.filter((t) => t.classList.contains('colored'));
    assert.ok(proj.length >= 1, 'project tabs are coloured');
    proj.forEach((t) => assert.match(t.style['--pc'], /^#[0-9a-f]{6}$/));
    // set a colour from the project menu → server called, tabs repainted
    state.projects[0].color = '#123456';
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim() === 'Colour…').click();
    await settle();
    q('.swatch')[2].click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/color/cheshmeh').pop();
    assert.match(post.body.color, /^#[0-9a-f]{6}$/);
    assert.ok(q('.tab').filter((t) => t.classList.contains('colored')).every((t) => t.style['--pc'] === '#123456'));
  },

  async 'git badges show changes, push and pull counts'() {
    await settle();
    const gitBtn = q('.act').find((b) => b.textContent.indexOf('Git') === 0);
    const badges = q('.gb', gitBtn).map((b) => b.textContent);
    assert.deepStrictEqual(badges, ['3', '↑1', '↓2']);
    // a collapsed project shows the badges on its row too; a clean one shows nothing there
    const heads = q('.proj-head');
    assert.strictEqual(q('.gb', heads[1]).length, 0);
  },

  async 'history lists what you did and can continue a chat'() {
    document.body.querySelector('#btn-history').click();
    await settle();
    const modal = document.body.querySelector('.modal');
    const rows = q('.hrow', modal);
    assert.strictEqual(rows.length, 3);
    assert.deepStrictEqual(q('.hday', modal).map((d) => d.textContent).slice(0, 1), ['Today']);
    assert.ok(rows[1].classList.contains('fail'), 'failed run is marked');
    assert.match(rows[2].textContent, /commit: payment bug/);
    const search = modal.querySelector('input');
    search.value = 'پرداخت'; search.dispatch('input');
    assert.strictEqual(q('.hrow', modal).length, 1);
    const btns = q('.btn', q('.hrow', modal)[0]).map((b) => b.textContent);
    assert.deepStrictEqual(btns, ['Open'], 'opens in our own chat panel — no text terminal');
    q('.btn', q('.hrow', modal)[0])[0].click();
    await settle();
    const posts = () => calls.filter((c) => c.url === '/api/sessions' && c.method === 'POST');
    const post = posts().pop();
    assert.deepStrictEqual([post.body.kind, post.body.resume], ['chat', '11111111-2222-3333-4444-555555555555']);
    // opening the same conversation again focuses its tab instead of a second copy
    const n = posts().length;
    document.body.querySelector('#btn-history').click();
    await settle();
    const m2 = q('.modal').pop();
    q('.btn', q('.hrow', m2)[0])[0].click();
    await settle();
    assert.strictEqual(posts().length, n);
  },

  async 'add project from the server is fully graphical'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.add-link').find((b) => b.textContent === '+ add project').click();
    await settle();
    const modal = q('.modal').pop();
    assert.deepStrictEqual(q('.segb', modal).map((b) => b.querySelector('b').textContent), ['From the server', 'From this machine', 'Server only']);
    const rows = q('.frow', modal);
    assert.deepStrictEqual(rows.map((r) => r.querySelector('.fname').textContent), ['shop', 'cheshmeh']);
    assert.ok(rows[1].disabled, 'already synced folder cannot be picked');
    rows[0].click();
    const local = q('input', modal).find((i) => i.value === '~/shop');
    assert.ok(local, 'local folder filled in');
    q('.btn.primary', modal).pop().click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/add-project').pop();
    assert.deepStrictEqual(post.body, { mode: 'download', remote: '/home/esi/shop', local: '~/shop', name: '', git: true });
    assert.ok(q('.toast').some((t) => /shop: downloading in the background/.test(t.textContent)));
  },

  async 'upload progress shows under the project and a toast when it is done'() {
    state.projects[1].sync = { state: 'busy', label: 'syncing', progress: { files: 30, totalFiles: 120, bytes: 5242880, totalBytes: 20971520, percent: 25 } };
    await new Promise((r) => setTimeout(r, 5200));       // the project list refreshes every 5 s
    await settle();
    const bar = q('.sbar')[0];
    assert.ok(bar, 'progress bar shown');
    assert.strictEqual(bar.querySelector('i').style.cssText, 'width:25%');
    assert.match(bar.textContent, /25% · 30\/120 files · 5\.0 MB of 20\.0 MB/);
    state.projects[1].sync = { state: 'ok', label: 'in sync', progress: null };
    await new Promise((r) => setTimeout(r, 5200));
    await settle();
    assert.strictEqual(q('.sbar').length, 0);
    assert.ok(q('.toast').some((t) => /shop: sync finished/.test(t.textContent)));
  },

  async 'settings shows prerequisites and installs a missing one'() {
    document.body.querySelector('#modal-root').replaceChildren();
    document.body.querySelector('#btn-settings').click();
    q('button').find((b) => b.textContent.trim() === 'Settings…').click();
    await settle();
    const modal = q('.modal').pop();
    const rows = q('.preq', modal);
    assert.deepStrictEqual(rows.map((r) => r.querySelector('b').textContent), ['mutagen', 'jq', 'docker']);
    rows[1].querySelector('.btn').click();
    await settle();
    assert.ok(calls.some((c) => c.url === '/api/prereqs/jq' && c.method === 'POST'));
    assert.ok(q('input', modal).some((i) => i.value === 'esi'), 'connection loaded');
    assert.ok(q('button', modal).some((b) => b.textContent === 'Uninstall…'));
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'move folders of a synced project'() {
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim() === 'Move folders…').click();
    await settle();
    const modal = q('.modal').pop();
    const inputs = q('input', modal);
    inputs[0].value = '/l/new-cheshmeh';
    q('.btn.primary', modal).pop().click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/move/cheshmeh').pop();
    assert.deepStrictEqual(post.body, { local: '/l/new-cheshmeh', remote: '' });
  },

  async 'sync details show conflicts and resolve one'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim().indexOf('Sync details') === 0).click();
    await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /1 conflict/);
    assert.match(q('.conf', modal)[0].textContent, /web\/app\.ts/);
    q('button', modal).find((b) => b.textContent === 'Keep the server’s').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sync/cheshmeh/resolve').pop();
    assert.deepStrictEqual(post.body, { path: 'web/app.ts', keep: 'server' });
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'a new version shows a notice; clicking updates in place'() {
    await new Promise((r) => setTimeout(r, 3200));          // first check runs 3 s after start
    await settle();
    const note = document.body.querySelector('#update-note');
    assert.ok(!note.classList.contains('hidden'));
    assert.strictEqual(note.textContent, 'Update to 10.0.0');
    document.body.querySelector('#modal-root').replaceChildren();
    note.click();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /faster/);
    q('.btn.primary', modal).pop().click();
    await settle();
    assert.ok(calls.some((c) => c.url === '/api/update' && c.method === 'POST'));
    assert.ok(q('.stale').some((e) => /Updating to 10\.0\.0/.test(e.textContent)));
  },

  async 'when the app is unreachable: one notice, no toast storm, back by itself'() {
    const toastsBefore = q('.toast').length;
    offline = true;
    await refreshFromOutside();
    await settle();
    const lost = q('.conn-lost');
    assert.strictEqual(lost.length, 1);
    assert.match(lost[0].textContent, /Lost connection to the app/);
    assert.strictEqual(q('.toast').length, toastsBefore, 'no "Failed to fetch" toasts');
    offline = false;
    await new Promise((r) => setTimeout(r, 2300));
    await settle();
    assert.strictEqual(q('.conn-lost').length, 0, 'gone when the app is back');
  },

  async 'Alt+W closes the active tab; empty section is removed when closed'() {
    const before = q('.tab').length;
    const tab = q('.tab')[0];
    tab.querySelector('.x').click();
    await settle();
    assert.strictEqual(q('.tab').length, before - 1);
  }
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String(e.stack || e).split('\n').slice(0, 5).join('\n     ')); }
  }
  process.exit(failed ? 1 : 0);
})();
