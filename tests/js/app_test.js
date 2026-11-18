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
  'btn-claude', 'btn-tasks', 'btn-palette', 'btn-history', 'btn-settings', 'btn-usage', 'update-note', 'usage', 'server-stats', 'btn-hide-side', 'show-side', 'st-ssh', 'st-sync'].forEach((id) => {
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
  defaultIgnores: ['node_modules', 'dist'],
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
let extraSessions = [];
const CONV_VS = '11111111-aaaa-bbbb-cccc-000000000001', CONV_OLD = '11111111-aaaa-bbbb-cccc-000000000002';
let fakeRunning = { cheshmeh: [{ pid: 4242, sessionId: CONV_VS, cwd: '/home/esi/cheshmeh', status: 'busy', owner: 'vscode', relay: '', tab: '', tmux: '', started: Date.now() / 1000 - 600, updated: Date.now() / 1000 - 5 }] };
const winOf = {};
let fakeTabs = { sessions: [], windows: [], now: 0 };
const fsTree = {
  '': [{ name: 'cheshmeh-web-app', dir: true, size: 0 }, { name: 'node_modules', dir: true, size: 0, ignored: true }, { name: 'README.md', dir: false, size: 12 }],
  'cheshmeh-web-app': [{ name: 'api', dir: true, size: 0 }, { name: 'web', dir: true, size: 0 }],
  'cheshmeh-web-app/api': [{ name: 'main.go', dir: false, size: 30 }]
};
const now = Date.now() / 1000;
const dockerSummary = { summary: { cheshmeh: { total: 3, running: 2, bad: 1 } }, events: [], seq: 0 };
const containers = [
  { id: 'a1', name: 'cheshmeh-api-1', image: 'api:dev', state: 'running', health: '', exitCode: 0, startedAt: new Date(Date.now() - 7200e3).toISOString(),
    restarts: 0, compose: 'cheshmeh', service: 'api', workdir: '/home/esi/cheshmeh/docker/local', configFiles: '', ports: [{ host: 18080, port: 8080, proto: 'tcp', ip: '0.0.0.0' }], proj: 'cheshmeh' },
  { id: 'b2', name: 'cheshmeh-db-1', image: 'postgres:16', state: 'running', health: 'healthy', exitCode: 0, startedAt: new Date(Date.now() - 60e3).toISOString(),
    restarts: 0, compose: 'cheshmeh', service: 'db', workdir: '/home/esi/cheshmeh/docker/local', configFiles: '', ports: [{ host: 5432, port: 5432, proto: 'tcp', ip: '0.0.0.0' }], proj: 'cheshmeh' },
  { id: 'c3', name: 'cheshmeh-worker-1', image: 'api:dev', state: 'exited', health: '', exitCode: 1, startedAt: '', finishedAt: new Date(Date.now() - 300e3).toISOString(),
    restarts: 4, compose: 'cheshmeh', service: 'worker', workdir: '/home/esi/cheshmeh/docker/local', configFiles: '', ports: [], proj: 'cheshmeh' }];
let tunnels = [{ project: 'cheshmeh', id: 't1', name: 'db', localPort: 5432, remoteHost: '127.0.0.1', remotePort: 5432, on: true, http: false, state: 'up', error: '' }];
const usage = { limits: { windows: { five_hour: { used: 0.38, resetsAt: now + 3600 }, seven_day: { used: 0.12, resetsAt: now + 3 * 86400 } }, status: 'allowed', updated: now },
  today: 1.5, week: 7.25, days: [{ day: '2026-11-14', projects: { cheshmeh: 1.2, shop: 0.3 }, total: 1.5 }] };
// the task list on the "server": the same operations as TASKS_PY
const fakeTasks = { tasks: [], rev: 0, n: 0 };
function tasksOp(op) {
  const T = fakeTasks, find = (id) => T.tasks.find((t) => t.id === id);
  if (!op) return { tasks: T.tasks.map((t) => Object.assign({}, t)), rev: T.rev };
  const t = find(op.id);
  const setSt = (x, st) => { if (st && st !== x.status) { x.status = st; x.done = st === 'done'; x.doneAt = x.done ? Date.now() / 1000 : 0; } };
  if (op.op === 'add') { T.tasks.push({ id: 'a' + String(++T.n).padStart(11, '0'), text: op.text, project: op.project || '', prio: op.prio || 2, status: 'todo', done: false, at: 1, doneAt: 0 }); setSt(T.tasks[T.tasks.length - 1], op.status); }
  else if (op.op === 'update' && t) { ['text', 'project', 'prio'].forEach((k) => { if (k in op) t[k] = op[k]; }); setSt(t, op.status || ('done' in op ? (op.done ? 'done' : 'todo') : null)); }
  else if (op.op === 'delete' && t) T.tasks.splice(T.tasks.indexOf(t), 1);
  else if (op.op === 'move' && t) { setSt(t, op.status); const ref = find(op.before || op.after); T.tasks.splice(T.tasks.indexOf(t), 1); T.tasks.splice(ref ? T.tasks.indexOf(ref) + (op.after ? 1 : 0) : T.tasks.length, 0, t); }
  else if (op.op === 'order') { const want = op.ids.map(find); const slots = want.map((x) => T.tasks.indexOf(x)).sort((a, b) => a - b); slots.forEach((at, i) => { T.tasks[at] = want[i]; }); }
  else if (op.op === 'clear') T.tasks = T.tasks.filter((x) => !(x.status === 'done' && (op.project === undefined || x.project === op.project)));
  T.rev++;
  return tasksOp(null);
}
global.fetch = (url, opts) => {
  if (offline) return Promise.reject(new TypeError('Failed to fetch'));
  const method = (opts && opts.method) || 'GET';
  const body = opts && typeof opts.body === 'string' ? JSON.parse(opts.body) : opts && opts.body;
  calls.push({ method, url, body, headers: opts && opts.headers });
  let res = {};
  if (url === '/api/state') res = state;
  else if (url === '/api/projects') res = { projects: state.projects };
  else if (url.startsWith('/api/status')) res = { ssh: 'ok', daemon: true };
  else if (url.startsWith('/api/git/cheshmeh/repos')) res = { where: 'local', gitSync: true, repos: ['cheshmeh-api-service', 'cheshmeh-web-app'],
    statuses: { 'cheshmeh-api-service': Object.assign({}, gitStatus, { files: [] }), 'cheshmeh-web-app': gitStatus } };
  else if (url.startsWith('/api/git/cheshmeh/status')) res = gitStatus;
  else if (url.startsWith('/api/color/')) res = { color: body.color };
  else if (url.startsWith('/api/title/')) res = { title: body.title };
  else if (url.startsWith('/api/starter/')) res = { starter: (body.text || '').trim() };
  else if (url.startsWith('/api/git-summary')) res = { summary: { cheshmeh: { changes: 3, ahead: 1, behind: 2, conflicts: 0, repos: 2, branch: 'main', branches: 1, repo: null, where: 'local' }, shop: { changes: 0, ahead: 0, behind: 0, conflicts: 0, repos: 1, branch: 'dev', branches: 1, repo: '', where: 'server' } } };
  else if (url === '/api/connection') res = { user: 'esi', host: 'h', port: '225', key: '', keys: [{ path: '/home/me/.ssh/id_ed25519', type: 'ed25519', comment: 'me' }], autostart: true, daemon: true };
  else if (url === '/api/prereqs' && method === 'GET') res = { items: [{ name: 'mutagen', ok: true, version: '0.18', why: 'sync', sudo: false }, { name: 'jq', ok: false, version: '', why: 'scripts', sudo: false }], docker: false, wsl: false, sudoWithoutPassword: false };
  else if (url.startsWith('/api/prereqs/')) res = { ok: true };
  else if (url.startsWith('/api/move/')) res = { ok: true };
  else if (url === '/api/sync/cheshmeh/details') res = { name: 'cheshmeh', state: 'ok', label: 'in sync', status: 'watching', paused: false, local: '/l/cheshmeh', remote: '/home/esi/cheshmeh',
    server: 'esi@h', error: '', cycles: 3, ignores: ['node_modules'], defaultIgnores: ['node_modules', 'dist'], git: true, progress: null,
    conflicts: [{ path: 'web/app.ts', local: [{ path: 'web/app.ts', was: 'file', now: 'file' }], server: [{ path: 'web/app.ts', was: 'file', now: 'deleted' }] }] };
  else if (url === '/api/sync/cheshmeh/resolve') res = { ok: true };
  else if (url === '/api/sync/cheshmeh/ignores') res = { ok: true, ignores: body.ignores, deleted: body.delete && body.delete.server ? { server: { deleted: 1, errors: [] } } : {} };
  else if (url === '/api/sync/cheshmeh/ignores/preview') res = { added: ['*.log', '/tmp', 'dist'],
    server: { items: [{ path: 'web/dist', dir: true, size: 2048 }], total: 2048, truncated: false }, local: { items: [], total: 0, truncated: false } };
  else if (url === '/api/server-folders') res = { folders: [{ path: '/home/esi/shop', name: 'shop', git: 'repo', mtime: 1, project: '' }, { path: '/home/esi/cheshmeh', name: 'cheshmeh', git: 'repos inside', mtime: 2, project: 'cheshmeh' }] };
  else if (url === '/api/add-project') res = { ok: true, project: 'shop' };
  else if (url.startsWith('/api/update') && method === 'GET') res = { current: '9', latest: '10.0.0', available: true, notes: '## Changes\n- faster', url: 'https://x' };
  else if (url === '/api/update' && method === 'POST') res = { ok: true, version: '10.0.0' };
  else if (url.startsWith('/api/activity')) res = { items: [
    { t: Date.now() / 1000, kind: 'prompt', project: 'cheshmeh', text: 'باگ پرداخت را درست کن', session: '11111111-2222-3333-4444-555555555555' },
    { t: Date.now() / 1000 - 60, kind: 'run-exit', project: 'cheshmeh', text: 'web', code: 1, seconds: 12.5, cmdId: 'c1' },
    { t: Date.now() / 1000 - 86400 * 3, kind: 'git-commit', project: 'cheshmeh', text: 'payment bug', repo: 'cheshmeh-api-service' }] };
  else if (url.startsWith('/api/docker-summary')) res = dockerSummary;
  else if (url === '/api/docker/cheshmeh') res = { containers, error: '' };
  else if (url === '/api/tunnels') res = { tunnels };
  else if (url.startsWith('/api/tunnels/cheshmeh/')) { tunnels = tunnels.map((t) => Object.assign({}, t, { on: body.on, state: body.on ? 'connecting' : 'off' })); res = { tunnels }; }
  else if (url === '/api/tunnels/cheshmeh') { tunnels = body.tunnels.map((t) => Object.assign({ project: 'cheshmeh', state: 'connecting', error: '' }, t, { id: t.id || 'n' + t.remotePort })); res = { tunnels }; }
  else if (url === '/api/usage') res = usage;
  else if (url.startsWith('/api/git/cheshmeh/diff')) res = { diff: '@@ -1 +1 @@\n-a\n+b' };
  else if (url === '/api/git/cheshmeh/discard') res = { status: gitStatus };
  else if (url.startsWith('/api/git/cheshmeh/commit')) res = { output: 'ok', status: Object.assign({}, gitStatus, { files: [gitStatus.files[1]], ahead: 1 }) };
  else if (url.startsWith('/api/fs/cheshmeh/list')) {
    const p = decodeURIComponent(url.split('path=')[1] || '');
    res = fsTree[p] ? { path: p, entries: fsTree[p], more: false, where: 'local' } : null;
    if (!res) return Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({ error: 'not a folder' }) });
  }
  else if (url.startsWith('/api/fs/cheshmeh/upload')) {
    if (body && body.name === 'bad.bin') return Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({ error: 'folder not found' }) });
    res = { path: 'x', size: body.size };
  }
  else if (url.startsWith('/api/fs/cheshmeh/read')) {
    const p = decodeURIComponent(url.split('path=')[1] || '');
    res = p.endsWith('.png') ? { path: p, size: 9, image: 'data:image/png;base64,AA==' } : { path: p, size: 12, text: 'line one\nسلام\n' };
  }
  else if (url === '/api/files/cheshmeh') res = { files: ['README.md', 'cheshmeh-web-app/', 'cheshmeh-web-app/api/', 'cheshmeh-web-app/api/main.go', 'cheshmeh-web-app/web/x.ts'] };
  else if (url === '/api/tasks') res = tasksOp(method === 'POST' ? body : null);
  else if (url === '/api/tabs' || url === '/api/tabs?vis=1') res = fakeTabs;
  else if (url.startsWith('/api/server-stats')) res = Object.assign({ cpu: 92.4, cores: 8, load: [7.1, 5, 3], memTotal: 16e9, memUsed: 12.4e9, swapTotal: 0, swapUsed: 0,
    diskTotal: 500e9, diskUsed: 120e9, diskFree: 380e9, diskPath: '/home/esi' }, url.indexOf('top=1') > 0 ? {
    topCpu: [{ pid: 1, user: 'esi', cpu: 180.5, mem: 2, rss: 800e6, name: 'node' }], topMem: [{ pid: 2, user: 'esi', cpu: 3, mem: 30, rss: 4.8e9, name: 'postgres' }] } : {});
  else if (url.startsWith('/api/claude-running/stop')) { const k = body.project || ''; fakeRunning[k] = (fakeRunning[k] || []).filter((x) => x.pid !== body.pid); res = { ok: true, running: fakeRunning[k] }; }
  else if (url.startsWith('/api/claude-running')) res = { projects: fakeRunning, t: 0 };
  else if (url.startsWith('/api/claude-sessions/cheshmeh')) res = { sessions: [
    { id: CONV_VS, mtime: Date.now() / 1000, title: 'Fix the payment page', first: 'fix it', last: '', prompts: 4 },
    { id: CONV_OLD, mtime: Date.now() / 1000 - 9000, title: 'Old work', first: 'x', last: '', prompts: 2 }].concat(extraSessions) };
  else if (url.startsWith('/api/git/cheshmeh/log')) res = { upstream: 'origin/main', ahead: 1, behind: 1, commits: [
    { hash: 'b'.repeat(40), short: 'bbbbbbb', author: 'Sara', date: Date.now() / 1000 - 120, refs: ['origin/main'], merge: false, subject: 'from GitHub', state: 'pull' },
    { hash: 'a'.repeat(40), short: 'aaaaaaa', author: 'Me', date: Date.now() / 1000 - 600, refs: ['HEAD -> main'], merge: false, subject: 'my change', state: 'push' },
    { hash: 'c'.repeat(40), short: 'ccccccc', author: 'Me', date: Date.now() / 1000 - 86400 * 3, refs: [], merge: false, subject: 'old', state: 'synced' }] };
  else if (url.startsWith('/api/git/cheshmeh/show')) res = { diff: 'commit ' + 'a'.repeat(40) + '\n+added line' };
  else if (url === '/api/git/cheshmeh/untrack') res = { status: Object.assign({}, gitStatus, { files: gitStatus.files.filter((f) => !f.ignored) }) };
  else if (url.startsWith('/api/browse')) {
    const p = decodeURIComponent(url.split('path=')[1] || '~');
    const tree = { '/home/esi': ['work', 'cheshmeh'], '/home/esi/work': ['old-app'], '/home/esi/work/old-app': [] };
    const path = p === '~' ? '/home/esi' : p;
    res = { path, parent: path === '/' ? '' : path.replace(/\/[^/]+$/, '') || '/', home: '/home/esi', git: false,
      dirs: (tree[path] || []).map((n) => ({ name: n, git: '', hidden: false })) };
  }
  else if (url.startsWith('/api/git/shop/branches')) res = { current: 'dev', local: [{ name: 'dev', date: 0, subject: 'wip', upstream: 'origin/dev' }, { name: 'main', date: 0, subject: 'release' }],
    remote: [{ name: 'origin/dev', local: true }, { name: 'origin/feature/pay', date: 0, subject: 'payments', local: false }] };
  else if (url === '/api/git/shop/switch') {
    if (!body.stash && body.branch === 'origin/feature/pay') return Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'Your uncommitted changes would be overwritten by this branch.' }) });
    res = { output: '', status: Object.assign({}, gitStatus, { branch: body.create ? body.branch : body.branch.replace(/^origin\//, '') }) };
  }
  else if (/^\/api\/sessions\/[^/]+\/win$/.test(url)) { const id = url.split('/')[3]; winOf[id] = body.win; res = { id, title: 'moved ' + id, alive: true, exitCode: null, url: '', win: body.win, moved: 1e12, spec: { kind: 'shell', project: '', where: 'local' } }; }
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

require(path.join(ROOT, 'gui/static/sounds.js'));
require(path.join(ROOT, 'gui/static/app.js'));
global.SW && require(path.join(ROOT, 'gui/static/chat.js'));
global.SW && require(path.join(ROOT, 'gui/static/files.js'));
global.SW && require(path.join(ROOT, 'gui/static/tasks.js'));

const tests = {
  'local paths: ~ shown as the real folder (Windows and Linux)'() {
    const S = global.SW.S, old = S.home;
    S.home = 'C:\\Users\\me';
    assert.strictEqual(global.SW.fullLocal('~/shop'), 'C:\\Users\\me\\shop');
    assert.strictEqual(global.SW.fullLocal('~'), 'C:\\Users\\me');
    assert.strictEqual(global.SW.fullLocal('D:/work/x'), 'D:\\work\\x');
    S.home = '/home/me';
    assert.strictEqual(global.SW.fullLocal('~/a/b'), '/home/me/a/b');
    S.home = old;
  },

  async 'project menu: copy the server path (~ in full) and the local path'() {
    await settle();
    const SW = global.SW;
    const copied = [];
    navigator.clipboard.writeText = (t) => { copied.push(t); return Promise.resolve(); };
    q('.act.more')[0].click();
    q('button').find((b) => b.textContent.trim().indexOf('Copy server path') === 0).click(); await settle();
    q('.act.more')[0].click();
    q('button').find((b) => b.textContent.trim().indexOf('Copy local path') === 0).click(); await settle();
    assert.deepStrictEqual(copied, ['/home/esi/cheshmeh', '/l/cheshmeh']);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'project menu: starter message for new Claude chats'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.act.more')[0].click();
    q('button').find((b) => b.textContent.trim().indexOf('Starter message…') === 0).click(); await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Starter message — cheshmeh/);
    const ta = modal.querySelector('textarea');
    ta.value = '  Read the memory files first.  ';
    q('.btn.primary', modal).pop().click(); await settle();
    const post = calls.filter((c) => c.url === '/api/starter/cheshmeh').pop();
    assert.deepStrictEqual(post.body, { text: '  Read the memory files first.  ' });
    assert.strictEqual(global.SW.project('cheshmeh').starter, 'Read the memory files first.');
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'project menu: Open folder opens the local copy'() {
    await settle();
    q('.act.more')[0].click();
    const it = q('button').find((b) => b.textContent.trim().indexOf('Open folder') === 0);
    assert.ok(it, 'Open folder in the menu');
    if (!it.disabled) {
      it.click();
      await settle();
      assert.ok(calls.some((c) => c.method === 'POST' && /^\/api\/open-folder\//.test(c.url)));
    }
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'boots, shows projects and status, no Persian in the UI'() {
    await settle();
    assert.strictEqual(q('.proj').length, 2);
    assert.match(document.body.querySelector('#st-ssh').className, /ok/);
    assert.ok(!/[؀-ۿ]/.test(document.body.textContent), 'English only');
    assert.strictEqual(document.body.querySelector('#empty').classList.contains('hidden'), false, 'empty state shown');
  },

  async 'project actions open tabs; Terminal goes to the server'() {
    q('.proj-head')[0].dispatch('click');
    assert.deepStrictEqual(q('.act', q('.proj')[0]).map((b) => b.textContent.replace(/[0-9↑↓!✓]+$/, '')), ['Claude', 'Git', '⋯'], 'only Claude and Git as buttons');
    q('.act.more')[0].click();
    q('button').find((b) => b.textContent.trim() === 'Server terminal').click();
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

  async 'git panel: history shows what to push and pull; files in .gitignore can be untracked'() {
    gitStatus.files.push({ path: '.env', x: ' ', y: 'M', conflict: false, untracked: false, ignored: true });
    q('.act').find((b) => b.textContent.indexOf('Git') === 0).click();
    await settle();
    const modal = q('.modal').pop();
    assert.match(q('.ghead', modal)[0].textContent, /↓2 to pull/);
    // .gitignore'd but tracked
    assert.ok(q('.ftag', modal).some((t) => t.textContent === 'ignored'));
    const stop = q('button', modal).find((b) => b.textContent === 'Stop tracking it');
    assert.ok(stop, 'offers to stop tracking it');
    stop.click(); await settle();
    const un = calls.filter((c) => c.url === '/api/git/cheshmeh/untrack').pop();
    assert.deepStrictEqual(un.body.files, ['.env']);
    assert.ok(!q('button', modal).some((b) => b.textContent === 'Stop tracking it'));
    // history
    q('.gtab', modal).find((b) => b.textContent === 'History').click(); await settle();
    assert.match(q('.hsum', modal)[0].textContent, /↑ 1 commit to push · ↓ 1 commit to pull/);
    const rows = q('.hrow', modal);
    assert.deepStrictEqual(rows.map((r) => [r.querySelector('.hm').textContent, r.querySelector('.hsub').textContent]),
      [['↓', 'from GitHub'], ['↑', 'my change'], ['✓', 'old']]);
    assert.ok(q('.href', rows[1]).some((r) => r.textContent === 'main'));
    rows[1].click(); await settle();
    assert.ok(calls.some((c) => c.url.indexOf('/api/git/cheshmeh/show?repo=cheshmeh-web-app&where=local&hash=' + 'a'.repeat(40)) === 0));
    assert.match(q('.gdiff', modal)[0].textContent, /\+added line/);
    gitStatus.files.pop();
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

  async 'docker: the project shows its containers; logs, restart and a crash notice'() {
    document.body.querySelector('#modal-root').replaceChildren();
    assert.ok(q('.gb', q('.proj-head')[0]).some((b) => b.textContent === '▣1'), 'a crashed container shows on the project');
    q('.act.more')[0].click();
    const dk = q('button').find((b) => b.textContent.indexOf('Docker containers…') === 0);
    assert.strictEqual(dk.querySelector('.k').textContent, '2/3 · 1 down', 'the menu shows running/total');
    assert.strictEqual(q('button').find((b) => b.textContent.indexOf('Port forwarding…') === 0).querySelector('.k').textContent, '1/1');
    dk.click();
    await settle();
    const modal = q('.modal').pop();
    const rows = q('.dkrow', modal);
    assert.deepStrictEqual(rows.map((r) => r.querySelector('b').textContent), ['api', 'db', 'worker']);
    assert.match(rows[0].textContent, /up 2h/);
    assert.match(rows[2].textContent, /crashed · exit 1/);
    assert.ok(!q('button', rows[2]).some((b) => b.textContent === 'Shell'), 'no shell in a stopped container');
    q('button', rows[0]).find((b) => b.textContent === 'Logs').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sessions' && c.method === 'POST').pop();
    assert.deepStrictEqual([post.body.kind, post.body.project, post.body.container, post.body.action], ['docker', 'cheshmeh', 'cheshmeh-api-1', 'logs']);
    // a crash reported by the server's watcher appears as a toast after the next refresh
    dockerSummary.events = [{ seq: 1, project: 'cheshmeh', name: 'cheshmeh-worker-1', service: 'worker', why: 'stopped with exit code 1', t: now }];
    dockerSummary.seq = 1;
    q('button', rows[2]).find((b) => b.textContent === 'Start').click();
    await settle();
    assert.deepStrictEqual(calls.filter((c) => c.url === '/api/docker/cheshmeh' && c.method === 'POST').pop().body, { container: 'cheshmeh-worker-1', action: 'start' });
    assert.ok(q('.toast').some((t) => /cheshmeh · worker: stopped with exit code 1/.test(t.textContent)), 'crash toast');
  },

  async 'ports: a docker port becomes a tunnel; tunnels toggle from the sidebar'() {
    const modal = q('.modal').pop();
    const chips = q('.pchip', modal);
    assert.deepStrictEqual(chips.map((c) => c.classList.contains('on')), [false, true], 'db is already forwarded');
    chips[0].click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/tunnels/cheshmeh').pop();
    assert.deepStrictEqual(post.body.tunnels.map((t) => [t.name, t.localPort, t.remotePort, t.on]), [['db', 5432, 5432, true], ['api', 18080, 18080, true]]);
    document.body.querySelector('#modal-root').replaceChildren();
    await settle();
    const lines = q('.tline');
    assert.deepStrictEqual(lines.map((l) => l.querySelector('.nm').textContent), ['db', 'api']);
    lines[0].dispatch('click');
    await settle();
    const tog = calls.filter((c) => c.url === '/api/tunnels/cheshmeh/t1').pop();
    assert.deepStrictEqual(tog.body, { on: false });
  },

  async 'ports dialog edits tunnels'() {
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.indexOf('Port forwarding…') === 0).click();
    await settle();
    const modal = q('.modal').pop();
    assert.strictEqual(q('.cmd-edit', modal).length, 2);
    // what can be forwarded: the containers' published ports, already-forwarded ones marked
    await settle(); await settle();
    const avail = q('.pchip', q('.tavail', modal)[0]);
    assert.deepStrictEqual(avail.map((b) => b.textContent), ['api :18080→8080 ✓', 'db :5432 ✓']);
    assert.ok(avail.every((b) => b.disabled));
    q('.btn', modal).find((b) => b.textContent === '+ Add').click();
    const boxes = q('.cmd-edit', modal);
    const ins = q('input', boxes[2]);
    ins[0].value = 'web'; ins[3].value = '4200';
    q('.btn.primary', modal).pop().click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/tunnels/cheshmeh').pop();
    assert.deepStrictEqual(post.body.tunnels[2], { name: 'web', localPort: '', remoteHost: '127.0.0.1', remotePort: '4200', on: true, http: false });
    assert.strictEqual(q('.modal').length, 0);
  },

  async 'Claude usage: limits in the sidebar, details on click'() {
    const el = document.body.querySelector('#usage');
    assert.ok(!el.classList.contains('hidden'));
    assert.deepStrictEqual(q('.um', el).map((m) => m.textContent), ['5h38%', '7d12%']);
    assert.match(el.title, /5-hour limit: 38% used, resets in (59m|1h 0m)/);
    document.body.querySelector('#btn-usage').click();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /today \$1\.50 · last 7 days \$7\.25/);
    assert.match(modal.textContent, /cheshmeh \$1\.20 · shop \$0\.30/);
    document.body.querySelector('#modal-root').replaceChildren();
    // close to the limit → one warning
    usage.limits.windows.five_hour.used = 0.85;
    global.dispatchEvent && global.dispatchEvent(new CustomEvent('sw-usage'));
    await new Promise((r) => setTimeout(r, 700));
    await settle();
    assert.ok(q('.toast').some((t) => /85% of the 5-hour limit/.test(t.textContent)));
  },

  async 'history lists what you did and can continue a chat'() {
    extraSessions = [{ id: '11111111-2222-3333-4444-555555555555', mtime: 1, title: 'Payment page bug', first: '', last: '', prompts: 1 }];
    document.body.querySelector('#btn-history').click();
    await settle();
    extraSessions = [];
    const modal = document.body.querySelector('.modal');
    const rows = q('.hrow', modal);
    assert.strictEqual(rows.length, 3);
    // Claude's title for the conversation first, what was written after it
    assert.strictEqual(q('.httl', rows[0])[0].textContent, 'Payment page bug');
    assert.strictEqual(q('.hsaid', rows[0])[0].textContent, 'باگ پرداخت را درست کن');
    assert.ok(calls.some((c) => c.url === '/api/claude-sessions/cheshmeh'));
    assert.deepStrictEqual(q('.hday', modal).map((d) => d.textContent).slice(0, 1), ['Today']);
    assert.ok(rows[1].classList.contains('fail'), 'failed run is marked');
    assert.match(rows[2].textContent, /commit: payment bug/);
    const search = modal.querySelector('input');
    search.value = 'page bug'; search.dispatch('input');
    assert.strictEqual(q('.hrow', modal).length, 1, 'the title is searched too');
    search.value = 'پرداخت'; search.dispatch('input');
    assert.strictEqual(q('.hrow', modal).length, 1);
    const btns = q('.btn', q('.hrow', modal)[0]).map((b) => b.textContent);
    assert.deepStrictEqual(btns, ['Open'], 'opens in our own chat panel — no text terminal');
    q('.btn', q('.hrow', modal)[0])[0].click();
    await settle();
    const posts = () => calls.filter((c) => c.url === '/api/sessions' && c.method === 'POST');
    const post = posts().pop();
    assert.deepStrictEqual([post.body.kind, post.body.resume], ['chat', '11111111-2222-3333-4444-555555555555']);
    assert.strictEqual(post.body.title, 'Claude · Payment page bug');
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
    assert.deepStrictEqual(post.body, { mode: 'download', remote: '/home/esi/shop', local: '~/shop', name: '', git: true, streams: 4, ignores: ['node_modules', 'dist'] });
    assert.ok(q('.toast').some((t) => /shop: downloading in the background/.test(t.textContent)));
  },

  async 'add project from this machine: Browse… the server folders, go in and out, pick'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.add-link').find((b) => b.textContent === '+ add project').click();
    await settle();
    const modal = q('.modal').pop();
    q('.segb', modal).find((b) => b.querySelector('b').textContent === 'From this machine').click();
    const inputs = q('input', modal);
    const local = inputs.find((i) => i.getAttribute('placeholder') === '~/projects/my-project');
    const remote = inputs.find((i) => i.getAttribute('placeholder') === '~/my-project');
    local.value = '/Users/me/code/my-shop';
    const ign = q('textarea', modal)[0];
    assert.strictEqual(ign.value, 'node_modules\ndist', 'Not synced starts with the defaults');
    ign.value = 'node_modules\n/dist, *.log\n';
    const browse = q('button', modal).filter((b) => b.textContent === 'Browse…').pop();
    browse.click(); await settle();
    const dlg = q('.modal').pop();
    assert.notStrictEqual(dlg, modal, 'its own window');
    const names = () => q('.frow', dlg).map((r) => r.querySelector('.fname').textContent);
    assert.deepStrictEqual(names(), ['..', 'work', 'cheshmeh']);
    q('.frow', dlg)[1].click(); await settle();                       // into work
    assert.deepStrictEqual(names(), ['..', 'old-app']);
    q('.frow', dlg)[1].click(); await settle();                       // into old-app
    q('.frow', dlg)[0].click(); await settle();                       // .. back out
    assert.deepStrictEqual(names(), ['..', 'old-app']);
    const inside = q('button', dlg).find((b) => /^Inside it: /.test(b.textContent));
    assert.strictEqual(inside.textContent, 'Inside it: my-shop');
    inside.click(); await settle();
    assert.strictEqual(remote.value, '/home/esi/work/my-shop');
    assert.strictEqual(q('.modal').pop(), modal, 'back in Add project');
    // Use this folder
    browse.click(); await settle();
    const dlg2 = q('.modal').pop();
    assert.deepStrictEqual(q('.frow', dlg2).map((r) => r.querySelector('.fname').textContent), ['..', 'old-app'], 'opens where the typed folder is');
    q('button', dlg2).find((b) => b.textContent === 'Use this folder').click(); await settle();
    assert.strictEqual(remote.value, '/home/esi/work');
    q('.btn.primary', modal).pop().click(); await settle();
    const post = calls.filter((c) => c.url === '/api/add-project').pop();
    assert.deepStrictEqual([post.body.mode, post.body.remote, post.body.local], ['upload', '/home/esi/work', '/Users/me/code/my-shop']);
    assert.deepStrictEqual(post.body.ignores, ['node_modules', '/dist', '*.log']);
    assert.strictEqual(post.body.streams, 4, 'first copy: 4 connections at once by default');
    assert.strictEqual(localStorage.getItem('sw-copy-streams'), '4');
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'upload progress shows under the project and a toast when it is done'() {
    state.projects[1].sync = { state: 'busy', label: 'syncing', progress: { files: 30, totalFiles: 120, bytes: 5242880, totalBytes: 20971520, percent: 25, eta: 3900 } };
    await new Promise((r) => setTimeout(r, 5200));       // the project list refreshes every 5 s
    await settle();
    const bar = q('.sbar')[0];
    assert.ok(bar, 'progress bar shown');
    assert.strictEqual(bar.querySelector('i').style.cssText, 'width:25%');
    assert.match(bar.textContent, /25% · 5\.0 MB \/ 20\.0 MB · 1 h 5 min left · 30\/120 items/);
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

  async 'settings: a sound or a voice per chat state, kept on this device'() {
    document.body.querySelector('#modal-root').replaceChildren();
    document.body.querySelector('#btn-settings').click();
    q('button').find((b) => b.textContent.trim() === 'Settings…').click();
    await settle();
    const modal = q('.modal').pop();
    const rows = q('.snd-row', modal);
    assert.deepStrictEqual(rows.map((r) => r.querySelector('.snd-name').textContent),
      ['Task done', 'Claude has a question', 'Claude needs permission', 'Plan ready', 'Stopped with an error', 'Voice language']);
    const modes = q('.snd-mode', modal);
    assert.deepStrictEqual(modes[0].querySelectorAll('option').map((o) => o.textContent), ['Off', 'Sound', 'Voice']);
    assert.strictEqual(modes[0].value, 'chime');
    const tried = [];
    const play = global.SWSound.play;
    global.SWSound.play = (...a) => tried.push(a);
    modes[1].value = 'voice';
    q('.snd-try', modal)[1].click();
    assert.deepStrictEqual(tried[0], ['question', 'simab', { mode: 'voice', lang: 'en' }], '▶ tries the choice before saving');
    global.SWSound.play = play;
    assert.ok(!localStorage.getItem('sw-sounds'), 'not saved yet');
    q('.snd-lang', modal)[0].value = 'fa';
    modes[0].value = 'off';
    const keep = global.SW.S.settings;
    q('.btn.primary', modal).pop().click();
    await settle();
    global.SW.S.settings = keep;
    assert.deepStrictEqual(JSON.parse(localStorage.getItem('sw-sounds')),
      { done: 'off', question: 'voice', permission: 'chime', plan: 'chime', error: 'chime', lang: 'fa' });
    assert.ok(!('done' in calls.filter((c) => c.url === '/api/settings').pop().body), 'not sent to the server');
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

  async 'stars: a starred project-menu entry comes first, in every project'() {
    document.body.querySelector('#modal-root').replaceChildren();
    const menuButtons = () => q('button', document.body.querySelector('#menu-root'));
    q('.proj-head')[0].querySelector('.icon-btn').click();
    const docker = menuButtons().find((b) => b.textContent.indexOf('Docker containers…') === 0);
    q('.star', docker)[0].click();
    let post = calls.filter((c) => c.url === '/api/settings').pop();
    assert.deepStrictEqual(post.body, { starred: ['menu:Docker containers…'] });
    // the menu is shown again with it on top, under "Starred", and not twice
    const menu = document.body.querySelector('#menu-root');
    assert.strictEqual(q('.mh', menu)[0].textContent, 'Starred');
    assert.strictEqual(menuButtons()[0].textContent.indexOf('Docker containers…'), 0);
    assert.strictEqual(menuButtons().filter((b) => b.textContent.indexOf('Docker containers…') === 0).length, 1);
    assert.ok(q('.star', menuButtons()[0])[0].classList.contains('on'));
    // the label changes (pause ↔ resume) but the star stays: stable id
    q('.star', menuButtons().find((b) => /^(Pause|Resume) sync/.test(b.textContent)))[0].click();
    post = calls.filter((c) => c.url === '/api/settings').pop();
    assert.deepStrictEqual(post.body, { starred: ['menu:Docker containers…', 'menu:Pause / resume sync'] });
    // unstar
    q('.star', menuButtons()[0])[0].click();
    post = calls.filter((c) => c.url === '/api/settings').pop();
    assert.deepStrictEqual(post.body, { starred: ['menu:Pause / resume sync'] });
    document.body.querySelector('#menu-root').replaceChildren();
  },

  async 'stars: drag starred menu entries to reorder (mouse: the row, finger: the grip)'() {
    document.body.querySelector('#modal-root').replaceChildren();
    const root = document.body.querySelector('#menu-root');
    const buttons = () => q('button', root);
    const lastStars = () => calls.filter((c) => c.url === '/api/settings').pop().body.starred;
    q('.proj-head')[0].querySelector('.icon-btn').click();
    for (const label of ['Git…', 'Files']) q('.star', buttons().find((b) => b.textContent.indexOf(label) === 0))[0].click();
    // menu:Pause / resume sync (earlier test), Git…, Files — in the order they were starred
    let top = q('.starred', root);
    assert.deepStrictEqual(top.map((b) => b.getAttribute('data-star')), ['menu:Pause / resume sync', 'menu:Git…', 'menu:Files']);
    assert.strictEqual(q('.grip', top[0]).length, 1);
    top.forEach((b, i) => { b.getBoundingClientRect = () => ({ top: i * 30, bottom: i * 30 + 30, height: 30, left: 0, right: 200, width: 200 }); });
    const ev = (type, y, id, kind) => dispatchEvent({ type, clientY: y, pointerId: id, pointerType: kind, preventDefault() {} });
    // a finger on the row itself does not drag (the menu scrolls)
    top[2].dispatch('pointerdown', { button: 0, pointerId: 3, pointerType: 'touch', clientY: 75, target: top[2] });
    ev('pointermove', 5, 3, 'touch'); ev('pointerup', 5, 3, 'touch');
    assert.deepStrictEqual(lastStars(), ['menu:Pause / resume sync', 'menu:Git…', 'menu:Files']);
    // the mouse drags "Files" above "Pause / resume sync"
    top[2].dispatch('pointerdown', { button: 0, pointerId: 1, pointerType: 'mouse', clientY: 75, target: top[2] });
    ev('pointermove', 73, 1, 'mouse');                               // under 5 px: not a drag yet
    assert.ok(!top[2].classList.contains('dragging'));
    ev('pointermove', 5, 1, 'mouse');
    assert.ok(top[2].classList.contains('dragging'));
    ev('pointerup', 5, 1, 'mouse');
    assert.deepStrictEqual(lastStars(), ['menu:Files', 'menu:Pause / resume sync', 'menu:Git…']);
    top = q('.starred', root);
    assert.deepStrictEqual(top.map((b) => b.getAttribute('data-star')), ['menu:Files', 'menu:Pause / resume sync', 'menu:Git…']);
    // the click that ends a drag does not run the entry
    const n = calls.length;
    top[0].click();
    assert.ok(!calls.slice(n).some((c) => c.url.indexOf('/api/fs/') === 0));
    assert.ok(root.children.length > 0, 'menu still open');
    // a finger drags by the grip
    top.forEach((b, i) => { b.getBoundingClientRect = () => ({ top: i * 30, bottom: i * 30 + 30, height: 30, left: 0, right: 200, width: 200 }); });
    const grip = q('.grip', top[0])[0];
    top[0].dispatch('pointerdown', { button: 0, pointerId: 4, pointerType: 'touch', clientY: 10, target: grip });
    ev('pointermove', 80, 4, 'touch'); ev('pointerup', 80, 4, 'touch');
    assert.deepStrictEqual(lastStars(), ['menu:Pause / resume sync', 'menu:Git…', 'menu:Files']);
    root.replaceChildren();
    await new Promise((r) => setTimeout(r, 450));
  },

  async 'stars: a dragged order in Ctrl+Shift+P is kept, other stars keep their places'() {
    for (let i = 0; i < 10; i++) dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
    document.body.querySelector('#modal-root').replaceChildren();
    document.getElementById('btn-palette').click();
    const pal = q('.modal').pop();
    const lastStars = () => calls.filter((c) => c.url === '/api/settings').pop().body.starred;
    const row = (label) => q('.res', pal).find((r) => r.textContent.indexOf(label) === 0);
    q('.star', row('cheshmeh: files'))[0].click();
    q('.star', row('cheshmeh: server terminal'))[0].click();
    let top = q('.starred', pal);
    assert.deepStrictEqual(top.map((r) => r.getAttribute('data-star')), ['cmd:cheshmeh: files', 'cmd:cheshmeh: server terminal']);
    top.forEach((b, i) => { b.getBoundingClientRect = () => ({ top: i * 30, bottom: i * 30 + 30, height: 30, left: 0, right: 200, width: 200 }); });
    top[1].dispatch('pointerdown', { button: 0, pointerId: 9, pointerType: 'mouse', clientY: 45, target: top[1] });
    dispatchEvent({ type: 'pointermove', clientY: 3, pointerId: 9, preventDefault() {} });
    dispatchEvent({ type: 'pointerup', clientY: 3, pointerId: 9 });
    // menu stars stay where they were in the list, only the two commands swapped
    assert.deepStrictEqual(lastStars(), ['menu:Pause / resume sync', 'menu:Git…', 'menu:Files', 'cmd:cheshmeh: server terminal', 'cmd:cheshmeh: files']);
    assert.strictEqual(q('.res', pal)[0].textContent.indexOf('cheshmeh: server terminal'), 0);
    q('.star', row('cheshmeh: server terminal'))[0].click();                // unstar both again
    q('.star', row('cheshmeh: files'))[0].click();
    assert.deepStrictEqual(lastStars(), ['menu:Pause / resume sync', 'menu:Git…', 'menu:Files']);
    dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
    await new Promise((r) => setTimeout(r, 450));                            // clicks right after a drag are ignored
  },

  async 'stars: starred commands come first in Ctrl+Shift+P, also when filtering'() {
    for (let i = 0; i < 10; i++) dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });   // earlier tests' dialogs
    document.body.querySelector('#modal-root').replaceChildren();
    document.getElementById('btn-palette').click();
    let pal = q('.modal').pop();
    const files = q('.res', pal).find((r) => r.textContent.indexOf('cheshmeh: files') === 0);
    const n = calls.length;
    q('.star', files)[0].click();
    assert.ok(!calls.slice(n).some((c) => c.url === '/api/sessions'));          // starring does not run it
    assert.ok(q('.modal').pop() === pal);                                         // palette stays open
    assert.strictEqual(q('.res', pal)[0].textContent.indexOf('cheshmeh: files'), 0);
    assert.ok(q('.res', pal)[0].classList.contains('last-star'));
    const tabRow = q('.res', pal).find((r) => /tab$/.test(r.textContent));
    if (tabRow) assert.ok(q('.star', tabRow)[0].classList.contains('none'));    // open tabs: no star
    const inp = q('input', pal)[0];
    inp.value = 'cheshmeh'; inp.dispatch('input');
    assert.strictEqual(q('.res', pal)[0].textContent.indexOf('cheshmeh: files'), 0);
    const lastStars = () => calls.filter((c) => c.url === '/api/settings').pop().body.starred;
    assert.ok(lastStars().indexOf('cmd:cheshmeh: files') >= 0);
    q('.star', q('.res', pal)[0])[0].click();                                      // unstar
    assert.ok(lastStars().indexOf('cmd:cheshmeh: files') < 0);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'not synced: edit the list, add the defaults, save'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim() === 'Not synced…').click();
    await settle();
    const modal = q('.modal').pop();
    const ta = q('textarea', modal)[0];
    assert.strictEqual(ta.value, 'node_modules');
    ta.value = 'node_modules\n  *.log , /tmp\n';
    q('button', modal).find((b) => b.textContent === 'Add defaults').click();
    assert.strictEqual(ta.value, 'node_modules\n*.log\n/tmp\ndist');
    q('.btn.primary', modal).pop().click();
    await settle();
    // step 2: what the new entries name already, per side; nothing deleted unless ticked
    assert.ok(!calls.some((c) => c.url === '/api/sync/cheshmeh/ignores'));
    assert.match(modal.textContent, /On the server — 1 found · 2\.0 KB/);
    assert.match(modal.textContent, /web\/dist\//);
    assert.match(modal.textContent, /On this machine.*Nothing there/);
    const boxes = q('input', modal).filter((i) => i.getAttribute('type') === 'checkbox');
    assert.strictEqual(boxes.length, 1);
    boxes[0].checked = true; boxes[0].dispatch('change');
    const save = q('.btn.primary', modal).pop();
    assert.strictEqual(save.textContent, 'Save and delete');
    save.click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/sync/cheshmeh/ignores').pop();
    assert.deepStrictEqual(post.body, { ignores: ['node_modules', '*.log', '/tmp', 'dist'], delete: { server: ['web/dist'] } });
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'not synced: removing an entry saves at once, nothing to delete'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim() === 'Not synced…').click();
    await settle();
    const modal = q('.modal').pop();
    q('textarea', modal)[0].value = '';
    const n = calls.length;
    q('.btn.primary', modal).pop().click();
    await settle();
    assert.ok(!calls.slice(n).some((c) => c.url.indexOf('/preview') > 0));
    assert.deepStrictEqual(calls.filter((c) => c.url === '/api/sync/cheshmeh/ignores').pop().body, { ignores: [], delete: {} });
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'sync details: Edit… opens the not-synced list'() {
    document.body.querySelector('#modal-root').replaceChildren();
    q('.proj-head')[0].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim().indexOf('Sync details') === 0).click();
    await settle();
    q('.ign-edit', q('.modal').pop())[0].click();
    await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Not synced — cheshmeh/);
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

  async 'Files tab: tree with git marks, file view, changes, Ask Claude, find file'() {
    const before = calls.length;
    q('.act.more')[0].click();
    const it = q('button').find((b) => b.textContent.trim() === 'Files');
    assert.ok(it && !it.disabled, 'Files in the project menu');
    it.click();
    await settle();
    const post = calls.slice(before).find((c) => c.method === 'POST' && c.url === '/api/sessions');
    assert.deepStrictEqual([post.body.kind, post.body.project], ['files', 'cheshmeh']);
    const tab = global.SW.S.active;
    assert.ok(tab.isFiles);
    const rows = () => q('.ft-row', tab.pane);
    const names = () => rows().map((r) => r.querySelector('.ft-name').textContent);
    assert.deepStrictEqual(names(), ['cheshmeh-web-app', 'node_modules', 'README.md']);
    assert.ok(rows()[1].classList.contains('ignored'));
    // the repository folder has changes inside (from the git status of cheshmeh-web-app)
    assert.ok(rows()[0].classList.contains('changed-in'));
    assert.strictEqual(q('.files-git', tab.pane)[0].textContent, '⎇ 2');
    // open folders: the git code shows on the file itself
    rows()[0].click(); await settle();
    rows().find((r) => r.textContent.indexOf('api') >= 0).click(); await settle();
    assert.deepStrictEqual(names(), ['cheshmeh-web-app', 'api', 'main.go', 'web', 'node_modules', 'README.md']);
    const main = rows()[2];
    assert.ok(main.classList.contains('changed') && main.classList.contains('cM'));
    assert.strictEqual(main.querySelector('.gcode').textContent, 'M');
    assert.match(localStorage.getItem('sw-files:cheshmeh'), /cheshmeh-web-app\/api/);
    // the file, read-only, with line numbers
    main.click(); await settle();
    assert.ok(calls.some((c) => c.url === '/api/fs/cheshmeh/read?path=' + encodeURIComponent('cheshmeh-web-app/api/main.go')));
    assert.strictEqual(q('.ft-nums', tab.pane)[0].textContent, '1\n2');
    assert.strictEqual(q('.ft-text', tab.pane)[0].textContent, 'line one\nسلام');
    assert.ok(tab.body.classList.contains('viewing'));
    // its changes: the diff of the right repository and path
    q('button', tab.vhead).find((b) => b.textContent === 'Changes').click(); await settle();
    const diff = calls.filter((c) => c.url.startsWith('/api/git/cheshmeh/diff')).pop();
    assert.match(diff.url, /repo=cheshmeh-web-app&where=local&path=api%2Fmain\.go&untracked=0/);
    assert.ok(q('.gl', tab.pane).some((l) => l.classList.contains('add')));
    // Ask Claude: a chat opens with @path in the message box
    q('button', tab.vhead).find((b) => b.textContent === 'Ask Claude').click(); await settle();
    const chat = global.SW.S.active;
    assert.ok(chat.isChat, 'a Claude chat opened');
    assert.strictEqual(chat.ta.value, '@cheshmeh-web-app/api/main.go ');
    // opening Files again goes back to the same tab
    const n = calls.length;
    global.SW.openSession({ kind: 'files', project: 'cheshmeh' }); await settle();
    assert.strictEqual(global.SW.S.active, tab);
    assert.ok(!calls.slice(n).some((c) => c.url === '/api/sessions'));
    // find file
    tab.filter.value = 'x.t';
    tab.filter.dispatch('input'); await settle();
    assert.deepStrictEqual(rows().map((r) => r.querySelector('.ft-name').textContent), ['x.ts']);
    assert.ok(rows()[0].classList.contains('cU'), 'conflict marked in the results too');
    tab.filter.dispatch('keydown', { key: 'Enter' }); await settle();
    assert.strictEqual(tab.file.path, 'cheshmeh-web-app/web/x.ts');
    assert.strictEqual(tab.filter.value, '');
    // drop files from the computer onto a folder: one request each, to that folder only, with X-SW
    const up = () => calls.filter((c) => c.url.startsWith('/api/fs/cheshmeh/upload'));
    const upBefore = up().length;
    let stopped = false;
    const folder = rows().find((r) => r.querySelector('.ft-name').textContent === 'cheshmeh-web-app');
    folder.dispatch('drop', { dataTransfer: { files: [{ name: 'a.txt', size: 3 }, { name: 'b.txt', size: 4 }], items: [] }, stopPropagation() { stopped = true; } });
    await settle(); await settle();
    const sent = up().slice(upBefore);
    assert.ok(stopped, 'the drop does not reach the whole tree too (it uploaded everything twice)');
    assert.deepStrictEqual(sent.map((c) => c.url), ['a.txt', 'b.txt'].map((n) => '/api/fs/cheshmeh/upload?path=cheshmeh-web-app&name=' + n));
    assert.strictEqual(sent[0].headers['X-SW'], '1');
    assert.strictEqual(typeof sent[0].headers['X-Token'], 'string');
    assert.ok(q('.toast').some((t) => t.textContent === 'Uploaded 2 files to cheshmeh-web-app/'));
    assert.ok(!q('.toast').some((t) => /^Uploading/.test(t.textContent)), 'the Uploading… note goes away');
    // a folder dropped (no bytes in the browser) and a server error: said, not silent
    tab.tree.dispatch('drop', { dataTransfer: { files: [{ name: 'pics', size: 0 }, { name: 'bad.bin', size: 1 }], items: [{ webkitGetAsEntry: () => ({ isDirectory: true, name: 'pics' }) }, { webkitGetAsEntry: () => ({ isDirectory: false, name: 'bad.bin' }) }] } });
    await settle(); await settle();
    assert.deepStrictEqual(up().slice(upBefore + 2).map((c) => c.url), ['/api/fs/cheshmeh/upload?path=&name=bad.bin']);
    assert.ok(q('.toast').some((t) => t.textContent === 'Folders cannot be uploaded: pics'));
    assert.ok(q('.toast').some((t) => t.textContent === 'Upload failed — bad.bin: folder not found'));
    // closing never asks (nothing runs)
    const tabsBefore = global.SW.S.tabs.length;
    global.SW.closeTab(tab); await settle();
    assert.strictEqual(global.SW.S.tabs.length, tabsBefore - 1);
    assert.strictEqual(q('.modal').length, 0);
    global.SW.closeTab(chat, true); await settle();
  },

  async 'sidebar width can be dragged with a finger (pointer events), not only a mouse'() {
    const rz = document.body.querySelector('#resizer');
    const ev = (type, x, id) => global.dispatchEvent({ type, clientX: x, clientY: 10, pointerId: id, pointerType: 'touch' });
    rz.dispatch('pointerdown', { button: 0, pointerId: 7, pointerType: 'touch', clientX: 230, clientY: 10 });
    ev('pointermove', 900, 8);                                  // another finger: ignored
    assert.notStrictEqual(document.documentElement.style['--side'], '480px');
    ev('pointermove', 320, 7);
    assert.strictEqual(document.documentElement.style['--side'], '320px');
    ev('pointerup', 320, 7);
    assert.strictEqual(localStorage.getItem('sw-side'), '320');
    ev('pointermove', 400, 7);                                  // after lifting the finger nothing moves
    assert.strictEqual(document.documentElement.style['--side'], '320px');
    const css = fs.readFileSync(path.join(ROOT, 'gui/static/app.css'), 'utf8');
    assert.match(css, /#resizer \{[^}]*touch-action: none/);
    assert.match(css, /\.gutter \{[^}]*touch-action: none/);
  },

  async 'a project can be renamed (shown name only)'() {
    const shop = state.projects[1];
    q('.proj-head')[1].querySelector('.icon-btn').click();
    q('button').find((b) => b.textContent.trim() === 'Rename…').click();
    await settle();
    const md = q('.modal').pop();
    const inp = q('.inp', md).pop();
    assert.strictEqual(inp.value, 'shop');
    inp.value = 'فروشگاه';
    q('button', md).find((b) => b.textContent === 'Save').click();
    await settle();
    const post = calls.filter((c) => c.url === '/api/title/shop').pop();
    assert.deepStrictEqual(post.body, { title: 'فروشگاه' });
    shop.title = 'فروشگاه';
    global.SW.renderProjects();
    assert.ok(q('.proj-name').some((n) => n.textContent.indexOf('فروشگاه') === 0), 'the new name is shown');
    assert.ok(q('.proj-head').some((x) => /name: shop/.test(x.getAttribute('title') || x.title || '')), 'the real name in the tooltip');
    delete shop.title;
    global.SW.renderProjects();
  },

  async 'closing a working chat: close only the tab, or stop it'() {
    const SW = global.SW;
    const open = async () => { await SW.openSession({ kind: 'chat', project: 'cheshmeh' }); await settle(); const t = SW.S.active; t.busy = true; return t; };
    let chat = await open();
    SW.closeTab(chat); await settle();
    let md = q('.modal').pop();
    assert.match(md.textContent, /still working/);
    q('button', md).find((b) => b.textContent === 'Close tab, keep working').click(); await settle();
    assert.ok(SW.S.tabs.indexOf(chat) < 0, 'the tab is closed');
    assert.ok(calls.some((c) => c.method === 'DELETE' && c.url === '/api/sessions/' + chat.id + '?keep=1'), 'Claude keeps working');
    chat = await open();
    const sent = [];
    chat.send = (m) => { sent.push(m); return true; };
    SW.closeTab(chat); await settle();
    q('button', q('.modal').pop()).find((b) => b.textContent === 'Stop it').click();
    await new Promise((r) => setTimeout(r, 450)); await settle();
    assert.deepStrictEqual(sent, [{ t: 'interrupt' }]);
    assert.ok(calls.some((c) => c.method === 'DELETE' && c.url === '/api/sessions/' + chat.id), 'closed');
  },

  async 'extra windows: move a tab out, get it back, never twice'() {
    const SW = global.SW, S = SW.S;
    await SW.openSession({ kind: 'shell', where: 'local', project: '' }); await settle();
    const tab = S.active, n = S.tabs.length;
    const opened = [];
    global.open = (url, name, feat) => { opened.push({ url, name, feat }); return {}; };
    const wid = SW.moveToNewWindow(tab);
    assert.ok(wid);
    assert.strictEqual(opened.length, 1);
    assert.match(opened[0].url, new RegExp('#t=testtoken&w=' + wid + '&take=' + tab.id + '$'));
    assert.match(opened[0].feat, /popup=yes,width=\d+,height=\d+,left=\d+,top=\d+/);
    assert.strictEqual(S.tabs.length, n - 1, 'gone from this window');
    assert.ok(!calls.some((c) => c.method === 'DELETE' && c.url === '/api/sessions/' + tab.id), 'not killed');
    // the menu offers the other window
    fakeTabs = { sessions: [], windows: [{ id: wid, n: 2 }], now: 0 };
    await SW.syncTabs();
    const t2 = S.tabs[0];
    t2.el.dispatch('contextmenu', { clientX: 1, clientY: 1 });
    const labels = q('button', document.body.querySelector('#menu-root')).map((b) => b.textContent.trim());
    assert.ok(labels.indexOf('Move to new window') >= 0 && labels.indexOf('Move to window 2') >= 0, labels.join('|'));
    document.body.querySelector('#menu-root').replaceChildren();
    // while the move settles, the tab is not taken back
    fakeTabs = { sessions: [{ id: tab.id, title: 'x', spec: { kind: 'shell', project: '', where: 'local' }, alive: true, win: '', moved: 1e12 }], windows: [], now: 0 };
    await SW.syncTabs();
    assert.ok(!S.tabs.some((t) => t.id === tab.id));
    // a tab of a closed window comes back (moved after this window started); twice → still one tab
    fakeTabs.sessions.push({ id: 'back1', title: 'back', spec: { kind: 'shell', project: '', where: 'local' }, alive: true, win: '', moved: 1e12 });
    await SW.syncTabs(); await SW.syncTabs(); await settle();
    assert.strictEqual(S.tabs.filter((t) => t.id === 'back1').length, 1);
    // tabs of other windows and old tabs are left alone
    fakeTabs.sessions.push({ id: 'old1', title: 'old', spec: { kind: 'shell', project: '', where: 'local' }, alive: true, win: '', moved: 0 });
    await SW.syncTabs();
    assert.ok(!S.tabs.some((t) => t.id === 'old1'));
    // moved away by another window → let go here (without killing it)
    fakeTabs.sessions[1].win = wid;
    await SW.syncTabs(); await settle();
    assert.ok(!S.tabs.some((t) => t.id === 'back1'));
    assert.ok(!calls.some((c) => c.method === 'DELETE' && c.url === '/api/sessions/back1'));
    // Move to window N
    await SW.openSession({ kind: 'shell', where: 'local', project: '' }); await settle();
    const t3 = S.active;
    await SW.moveToWindow(t3, wid); await settle();
    assert.strictEqual(winOf[t3.id], wid);
    assert.ok(!S.tabs.includes(t3));
    fakeTabs = { sessions: [], windows: [], now: 0 };
  },

  async 'branches: shown next to the project, switch, track a remote one (stash asked), create'() {
    const SW = global.SW;
    await refreshFromOutside();
    const labels = q('.pbranch').map((e) => e.textContent.trim());
    assert.deepStrictEqual(labels, ['⎇ main', '⎇ dev']);
    q('.pbranch')[1].click(); await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Branch — shop/);
    const rows = () => q('.brow', modal).map((b) => b.querySelector('.bname').textContent);
    assert.deepStrictEqual(rows(), ['✓ dev', 'main', 'origin/feature/pay']);      // origin/dev has a local branch already
    // a branch only on the remote, with clashing changes: asked, then stashed
    q('.brow', modal)[2].click(); await settle();
    const ask = q('.modal').pop();
    assert.match(ask.textContent, /git stash/);
    q('.btn.primary', ask).pop().click(); await settle();
    const sw = calls.filter((c) => c.url === '/api/git/shop/switch');
    assert.deepStrictEqual(sw.map((c) => [c.body.branch, !!c.body.track, !!c.body.stash, c.body.repo, c.body.where]),
      [['origin/feature/pay', true, false, '', 'server'], ['origin/feature/pay', true, true, '', 'server']]);
    assert.ok(q('.toast').some((t) => /shop: on feature\/pay/.test(t.textContent)));
    // create a new branch by typing its name
    SW.branchPicker(SW.project('shop'), '', 'server', () => {}); await settle();
    const m2 = q('.modal').pop();
    const f = m2.querySelector('input');
    f.value = 'fix/login';
    f.dispatch('input');
    const last = q('.brow', m2).pop();
    assert.match(last.textContent, /Create branch “fix\/login”/);
    last.click(); await settle();
    const made = calls.filter((c) => c.url === '/api/git/shop/switch').pop();
    assert.deepStrictEqual([made.body.branch, made.body.create], ['fix/login', true]);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'Claude sessions: running ones show on the project; watch, stop, never a second Claude'() {
    const SW = global.SW;
    await SW.refreshRunning(true); await settle();
    const colored = global.SW.S.tabs.filter((t) => t.info.spec.project === 'cheshmeh');
    assert.ok(colored.length && colored.every((t) => t.pane.classList.contains('colored') && t.el.classList.contains('colored')), 'project colour on the tab and around its area');
    const badge = q('.gb.run')[0];
    assert.ok(badge, 'a running Claude shows on the project');
    assert.strictEqual(badge.textContent, '✳1');
    assert.ok(badge.classList.contains('busy'));
    assert.match(badge.getAttribute('title'), /VS Code · busy/);
    badge.click(); await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Claude sessions — cheshmeh/);
    const row = q('.run-row', modal)[0];
    assert.match(row.textContent, /Fix the payment page/);
    assert.match(row.textContent, /VS Code · working/);
    assert.deepStrictEqual(q('button', row).map((b) => b.textContent), ['Watch', 'Continue here', 'Stop']);
    // earlier conversations do not list the running one again
    assert.deepStrictEqual(q('.sess-main', modal).map((e) => e.querySelector('.st').textContent), ['Old work']);
    // opening the running conversation any other way asks first
    const before = calls.filter((c) => c.url === '/api/sessions').length;
    await SW.openSession({ kind: 'chat', project: 'cheshmeh', resume: CONV_VS, title: 'Claude · Fix the payment page' }); await settle();
    assert.strictEqual(calls.filter((c) => c.url === '/api/sessions').length, before, 'no second Claude started');
    const ask = q('.modal').pop();
    assert.match(ask.textContent, /Already running in VS Code/);
    assert.deepStrictEqual(q('.btn', ask).map((b) => b.textContent), ['Cancel', 'Watch it', 'Stop it there, continue here']);
    // Watch it: a read-only tab
    q('button', ask).find((b) => b.textContent === 'Watch it').click(); await settle();
    const w = calls.filter((c) => c.url === '/api/sessions').pop();
    assert.deepStrictEqual([w.body.kind, w.body.resume, w.body.watch], ['chat', CONV_VS, true]);
    const tab = SW.S.active;
    tab.applyState({ alive: true, busy: true, watch: { running: true, status: 'busy', owner: 'vscode' } });
    assert.ok(tab.pane.classList.contains('watching'));
    assert.match(tab.watchBar.textContent, /Watching — runs in VS Code, working/);
    tab.ta.value = 'hello'; tab.submit();
    assert.ok(!global.sockets[global.sockets.length - 1].sent.some((m) => m.t === 'send'), 'nothing is sent while watching');
    // Stop it from the Claude sessions dialog
    document.body.querySelector('#modal-root').replaceChildren();
    SW.claudeSessionsDialog(SW.project('cheshmeh')); await settle();
    const m2 = q('.modal').pop();
    q('button', q('.run-row', m2)[0]).find((b) => b.textContent === 'Stop').click(); await settle();
    q('.btn.danger', q('.modal').pop()).pop().click(); await settle();
    const st = calls.filter((c) => c.url === '/api/claude-running/stop').pop();
    assert.deepStrictEqual(st.body, { project: 'cheshmeh', pid: 4242, sessionId: CONV_VS });
    assert.strictEqual(q('.gb.run').length, 0, 'gone from the project');
    tab.applyState({ alive: true, busy: false, watch: { running: false, status: '', owner: '' } });
    assert.match(tab.watchBar.textContent, /not running any more/);
    // now it can be continued here without a question
    q('button', tab.watchBar).find((b) => b.textContent === 'Continue here').click(); await settle();
    const c2 = calls.filter((c) => c.url === '/api/sessions').pop();
    assert.deepStrictEqual([c2.body.resume, !!c2.body.watch], [CONV_VS, false]);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'server load: cpu, ram and disk above Claude usage; details with the busiest processes'() {
    await new Promise((r) => setTimeout(r, 5200)); await settle();          // polled every 5 s
    const el = document.body.querySelector('#server-stats');
    assert.ok(!el.classList.contains('hidden'));
    const ms = q('.um', el);
    assert.deepStrictEqual(ms.map((m) => [m.querySelector('.ul').textContent, m.querySelector('.up').textContent]), [['cpu', '92%'], ['ram', '78%'], ['disk', '24%']]);
    assert.ok(ms[0].classList.contains('err') && ms[1].classList.contains('warn') && !ms[2].classList.contains('warn'));
    assert.match(el.title, /Memory 12\.4 GB of 16\.0 GB/);
    el.click(); await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /8 cores/);
    assert.match(modal.textContent, /postgres/);
    assert.match(modal.textContent, /4\.8 GB/);
    document.body.querySelector('#modal-root').replaceChildren();
  },

  async 'all Claude sessions from the Menu; a Claude chat without a project from the sidebar'() {
    const SW = global.SW;
    fakeRunning = { cheshmeh: [{ pid: 7001, sessionId: CONV_VS, cwd: '/home/esi/cheshmeh', status: 'busy', owner: 'vscode', relay: '', tab: '', tmux: '', started: Date.now() / 1000 - 60, updated: Date.now() / 1000 }],
      '': [{ pid: 7002, sessionId: '22222222-aaaa-bbbb-cccc-000000000009', cwd: '/home/esi', status: 'idle', owner: 'terminal', relay: '', tab: '', tmux: '', started: Date.now() / 1000 - 600, updated: Date.now() / 1000 - 30 }] };
    await SW.refreshRunning(true); await settle();
    document.body.querySelector('#btn-settings').click();
    const item = q('button').find((b) => b.textContent.trim().indexOf('Claude sessions — all projects…') === 0);
    assert.ok(item, 'in the Menu');
    assert.match(item.textContent, /2 running/);
    item.click(); await settle();
    const modal = q('.modal').pop();
    assert.match(modal.textContent, /Claude sessions — all projects/);
    assert.deepStrictEqual(q('.run-proj', modal).map((e) => e.textContent), ['cheshmeh · 1', 'No project (home folder) · 1']);
    assert.strictEqual(q('.run-row', modal).length, 2);
    // earlier conversations of every project, newest first, with the project's name
    assert.ok(calls.some((c) => c.url === '/api/claude-sessions/cheshmeh') && calls.some((c) => c.url === '/api/claude-sessions/'));
    assert.match(q('.sess-main', modal)[0].textContent, /Old work.*cheshmeh/);
    // Stop in another project from here
    q('button', q('.run-row', modal)[1]).find((b) => b.textContent === 'Stop').click(); await settle();
    q('.btn.danger', q('.modal').pop()).pop().click(); await settle();
    const st = calls.filter((c) => c.url === '/api/claude-running/stop').pop();
    assert.deepStrictEqual([st.body.project, st.body.pid], ['', 7002]);
    document.body.querySelector('#modal-root').replaceChildren();
    // the sidebar's Claude button: a chat without a project
    const n = calls.length;
    document.body.querySelector('#btn-claude').click(); await settle();
    const post = calls.slice(n).find((c) => c.url === '/api/sessions');
    assert.deepStrictEqual([post.body.kind, post.body.project], ['chat', undefined]);
    fakeRunning = { cheshmeh: [] };
  },

  async 'Alt+W closes the active tab; empty section is removed when closed'() {
    const before = q('.tab').length;
    const tab = q('.tab')[0];
    tab.querySelector('.x').click();
    await settle();
    assert.strictEqual(q('.tab').length, before - 1);
  },
  async 'run commands: the address a command prints gets an Open link; the last result shows; shared rows are marked'() {
    const SW = global.SW, S = SW.S;
    const tab = await SW.openSession({ kind: 'run', project: 'cheshmeh', cmdId: 'c1' }); await settle();
    const ws = global.sockets[global.sockets.length - 1];
    ws.onmessage({ data: JSON.stringify({ t: 'o', d: '\x1b[32m** Angular Live Development Server is listening, open your browser on http://localhost:4200/ **\x1b[0m' }) });
    assert.strictEqual(tab.foundUrl, 'http://localhost:4200/');
    const bar = tab.pane.querySelector('.urlbar');
    assert.ok(bar && /localhost:4200/.test(bar.textContent));
    assert.strictEqual(bar.querySelector('a').getAttribute('href'), 'http://localhost:4200/');
    assert.ok(q('.res').some((e) => e.classList.contains('link')), 'an ↗ next to the running command');
    ws.onmessage({ data: JSON.stringify({ t: 'x', code: 1 }) });
    SW.renderProjects();
    const res = q('.res').find((e) => e.classList.contains('err'));
    assert.ok(res && res.textContent === '✗1', 'the last run failed with 1');
    SW.closeTab(tab, true); await settle();
  },
  async 'Changes tab: changed files, the diff, commit only the ticked ones, discard'() {
    const SW = global.SW, S = SW.S;
    const tab = await SW.openSession({ kind: 'changes', project: 'cheshmeh', focus: ['/home/esi/cheshmeh/cheshmeh-web-app/web/x.ts'] }); await settle();
    assert.ok(tab.isChanges);
    const rows = q('.ch-row', tab.pane);
    assert.deepStrictEqual(rows.map((r) => r.querySelector('.ft-name').textContent), ['main.go', 'x.ts']);
    assert.strictEqual(tab.sel.path, 'web/x.ts', 'the file Claude changed is shown first');
    assert.ok(calls.some((c) => c.url.indexOf('/api/git/cheshmeh/diff?repo=cheshmeh-web-app&where=local&path=web%2Fx.ts') === 0));
    // the same tab is reused
    assert.strictEqual(await SW.openSession({ kind: 'changes', project: 'cheshmeh' }), tab);
    // leave x.ts out of the commit
    const box = rows[1].querySelector('input');
    box.checked = false; box.dispatch('click', { stopPropagation() {} });
    assert.strictEqual(tab.commitBtn.textContent, 'Commit 1 of 2');
    tab.msg.value = 'fix main';
    tab.commit(); await settle();
    const cm = calls.filter((c) => c.url === '/api/git/cheshmeh/commit').pop();
    assert.deepStrictEqual([cm.body.repo, cm.body.message, cm.body.files], ['cheshmeh-web-app', 'fix main', ['api/main.go']]);
    tab.discard([tab.files()[0]]); await settle();
    const dlg = q('.modal').pop();
    assert.match(dlg.textContent, /Throw away the changes to api\/main\.go/);
    q('button', dlg).find((b) => b.textContent === 'Discard').click(); await settle();
    const dc = calls.filter((c) => c.url === '/api/git/cheshmeh/discard').pop();
    assert.deepStrictEqual(dc.body.files, ['api/main.go']);
    SW.closeTab(tab, true); await settle();
  },

  async 'Tasks board: + dialog, columns, drag between columns, edit, filter, ask Claude'() {
    const SW = global.SW, S = SW.S;
    for (let i = 0; i < 10; i++) dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
    const rc = new Element('b'); rc.className = 'rcount'; document.body.querySelector('#btn-tasks').append(rc);
    document.body.querySelector('#btn-tasks').click(); await settle();
    const tab = S.active;
    assert.ok(tab && tab.isTasks, 'the rail button opens the Tasks tab');
    assert.strictEqual(calls.filter((c) => c.url === '/api/sessions' && c.body.kind === 'tasks').pop().body.title, 'Tasks');
    document.body.querySelector('#btn-tasks').click(); await settle();
    assert.strictEqual(S.tabs.filter((t) => t.isTasks).length, 1, 'one Tasks tab, reused');
    // four columns, in this order
    assert.deepStrictEqual(q('.tk-cname', tab.board).map((e) => e.textContent), ['Backlog', 'To do', 'In progress', 'Done']);
    const cards = (st) => q('.tk-card', tab.cols[st].list);
    const texts = (st) => cards(st).map((c) => q('.tk-ctext', c)[0].textContent);
    assert.strictEqual(q('.tk-empty', tab.cols.todo.list).length, 1, 'an empty column says where tasks go');
    const modal = () => q('.modal').pop();
    // + New task: a dialog
    q('.tk-new', tab.pane)[0].click();
    let d = tab.dialog;
    assert.ok(d && modal(), 'the dialog opens');
    assert.strictEqual(d.status.value, 'todo');
    d.save();
    assert.match(q('.add-err', modal())[0].textContent, /Write the task first/);
    d.text.value = 'fix login'; d.project.value = 'cheshmeh'; d.prio.value = '1';
    d.text.dispatch('keydown', { key: 'Enter' });
    assert.strictEqual(tab.dialog, null, 'Enter adds and closes');
    assert.deepStrictEqual(texts('todo'), ['fix login'], 'shown at once');
    assert.ok(cards('todo')[0].classList.contains('pending'));
    await settle();
    // + of a column adds into that column
    q('.tk-cadd', tab.cols.backlog.el)[0].click();
    d = tab.dialog; assert.strictEqual(d.status.value, 'backlog');
    d.text.value = 'صفحه پرداخت'; d.project.value = 'shop'; d.save(); await settle();
    q('.tk-cadd', tab.cols.todo.el)[0].click();
    d = tab.dialog; d.text.value = 'call Sara'; d.save(); await settle();
    assert.deepStrictEqual(texts('backlog'), ['صفحه پرداخت']);
    assert.deepStrictEqual(texts('todo'), ['fix login', 'call Sara']);
    const c0 = cards('todo')[0];
    assert.strictEqual(q('.tk-pill', c0)[0].textContent, 'High');
    assert.strictEqual(q('.tk-cproj', c0)[0].textContent, 'cheshmeh');
    assert.strictEqual(q('.tk-ctext', cards('backlog')[0])[0].getAttribute('dir'), 'auto', 'Persian reads right');
    assert.strictEqual(tab.countEl.textContent, '3 open');
    assert.strictEqual(rc.textContent, '3');
    // drag a card into another column: onto a card (before / after it) or onto the empty column
    const hit = (el) => { global.document.elementFromPoint = () => el; };
    tab.dragStart(tab.find(fakeTasks.tasks[2].id), cards('todo')[1], 10, 10);
    assert.ok(cards('todo')[1].classList.contains('dragging'));
    assert.strictEqual(q('.tk-ghost').length, 1, 'a copy follows the pointer');
    hit(q('.tk-empty', tab.cols.doing.list)[0]); tab.dragMove(100, 100);
    assert.ok(tab.cols.doing.el.classList.contains('drop'));
    tab.dragEnd(); await settle();
    assert.strictEqual(q('.tk-ghost').length, 0);
    assert.deepStrictEqual(texts('doing'), ['call Sara']);
    assert.strictEqual(fakeTasks.tasks.find((t) => t.text === 'call Sara').status, 'doing');
    tab.dragStart(tab.find(fakeTasks.tasks[0].id), cards('todo')[0], 10, 10);
    hit(q('.tk-ctext', cards('doing')[0])[0]); tab.dragMove(100, 100);         // lower half of the card: after it
    assert.ok(cards('doing')[0].classList.contains('drop-before'));
    tab.dragMove(100, 590);
    assert.ok(cards('doing')[0].classList.contains('drop-after'));
    tab.dragEnd(); await settle();
    assert.deepStrictEqual(texts('doing'), ['call Sara', 'fix login']);
    assert.deepStrictEqual(texts('todo'), []);
    // drop on itself: nothing sent
    const n0 = calls.length;
    tab.dragStart(tab.find(fakeTasks.tasks[0].id), cards('doing')[1], 10, 10);
    hit(cards('doing')[1]); tab.dragMove(100, 100); tab.dragEnd(); await settle();
    assert.strictEqual(calls.filter((c, i) => i >= n0 && c.method === 'POST' && c.url === '/api/tasks').length, 0);
    // to Done from the ⋯ menu (also the touch way)
    const menuBtn = (label) => q('button', document.body.querySelector('#menu-root')).find((b) => b.textContent.indexOf(label) === 0);
    q('.tk-cmore', cards('doing')[0])[0].click();
    assert.ok(!menuBtn('In progress'), 'its own column is not offered');
    menuBtn('Done').click(); await settle();
    assert.deepStrictEqual(texts('done'), ['call Sara']);
    assert.strictEqual(fakeTasks.tasks.find((t) => t.text === 'call Sara').done, true);
    assert.strictEqual(rc.textContent, '2');
    // Move up inside a column
    q('.tk-cadd', tab.cols.doing.el)[0].click(); d = tab.dialog; d.text.value = 'deploy'; d.save(); await settle();
    assert.deepStrictEqual(texts('doing'), ['fix login', 'deploy']);
    q('.tk-cmore', cards('doing')[1])[0].click();
    assert.ok(menuBtn('Move down').disabled);
    menuBtn('Move up').click(); await settle();
    assert.deepStrictEqual(texts('doing'), ['deploy', 'fix login']);
    // click a card: edit in the same dialog (text, status, project) — not right after a drag
    cards('doing')[1].click();
    assert.strictEqual(tab.dialog, null, 'the click that ends a drag does not open it');
    tab.justDragged = 0;
    cards('doing')[1].click();
    d = tab.dialog;
    assert.strictEqual(d.text.value, 'fix login');
    assert.strictEqual(d.status.value, 'doing');
    d.text.value = 'fix login page'; d.status.value = 'backlog'; d.save(); await settle();
    assert.deepStrictEqual(texts('backlog'), ['fix login page', 'صفحه پرداخت'], 'on top of the new column');
    // filter one project: the + dialog takes that project
    tab.filterSel.value = 'shop'; tab.filterSel.dispatch('change');
    assert.deepStrictEqual(texts('backlog'), ['صفحه پرداخت']);
    assert.strictEqual(q('.tk-cproj', tab.board).length, 0, 'no project label when one project is shown');
    q('.tk-new', tab.pane)[0].click();
    assert.strictEqual(tab.dialog.project.value, 'shop');
    tab.dialog.close();
    tab.setFilter('');
    // clear done
    q('.tk-hbtn', tab.cols.done.el)[0].click(); await settle();
    q('.btn', document.body.querySelector('#modal-root')).find((b) => b.textContent === 'Remove').click(); await settle();
    assert.deepStrictEqual(texts('done'), []);
    assert.ok(!fakeTasks.tasks.some((t) => t.text === 'call Sara'));
    // ask Claude from the dialog: a new chat in the task's project with the task in its box
    cards('backlog')[1].click();
    q('.btn', modal()).find((b) => b.textContent === 'Ask Claude').click(); await settle();
    const chat = S.active;
    assert.ok(chat.isChat);
    assert.strictEqual(chat.info.spec.project, 'shop');
    assert.strictEqual(chat.ta.value, 'صفحه پرداخت');
    SW.closeTab(chat, true); await settle();
    // another device changed the board: seen on the next look
    fakeTasks.tasks.push({ id: 'b00000000001', text: 'from the phone', project: '', prio: 2, status: 'doing', done: false });
    fakeTasks.rev++;
    SW.activate(tab); tab.ensureOpen(); await settle();
    assert.ok(texts('doing').indexOf('from the phone') >= 0);
    // delete from the dialog
    cards('doing').find((c) => c.textContent.indexOf('from the phone') === 0).click();
    q('.btn', modal()).find((b) => b.textContent === 'Delete').click(); await settle();
    assert.ok(!fakeTasks.tasks.some((t) => t.text === 'from the phone'));
    delete global.document.elementFromPoint;
    SW.closeTab(tab, true); await settle();
    assert.ok(!S.tabs.some((t) => t.isTasks));
  },

  async 'Ctrl+P: go to a file of the project you are in'() {
    const SW = global.SW, S = SW.S;
    for (let i = 0; i < 10; i++) dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });   // earlier tests' dialogs
    const files = await SW.openSession({ kind: 'files', project: 'cheshmeh' }); await settle();
    global.dispatchEvent({ type: 'keydown', key: 'p', code: 'KeyP', ctrlKey: true, preventDefault() {}, stopPropagation() {} });
    await settle();
    const pal = q('.palette').pop();
    assert.ok(pal, 'the file finder opens');
    const inp = pal.querySelector('input');
    inp.value = 'main';
    inp.dispatch('input');
    const res = q('.res', pal);
    assert.strictEqual(res[0].textContent.indexOf('main.go'), 0);
    assert.ok(!res.some((r) => /\/$/.test(r.textContent)), 'no folders');
    res[0].click(); await settle();
    assert.strictEqual(S.active, files);
    assert.strictEqual(files.file.path, 'cheshmeh-web-app/api/main.go');
    SW.closeTab(files, true); await settle();
  }
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String(e.stack || e).split('\n').slice(0, 5).join('\n     ')); }
  }
  process.exit(failed ? 1 : 0);
})();
