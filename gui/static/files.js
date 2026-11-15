/* Simab Workspace — Files tab: the folders of a project, git changes marked, files shown read-only. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail, toast = SW.toast;
  var TOUCH = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  var STALE = 20000;            // re-read open folders and git when the tab comes back after this long

  function enc(s) { return encodeURIComponent(s); }
  function join(dir, name) { return dir ? dir + '/' + name : name; }
  function parentOf(path) { var i = path.lastIndexOf('/'); return i < 0 ? '' : path.slice(0, i); }
  function baseOf(path) { return path.slice(path.lastIndexOf('/') + 1); }
  function fmtSize(b) { var u = ['B', 'KB', 'MB', 'GB'], i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++; } return (i ? b.toFixed(1) : b) + ' ' + u[i]; }
  function shownCode(c) { return c === '?' ? 'N' : c; }

  function FilesTab(info) {
    var self = this;
    this.id = info.id;
    this.info = info;
    this.alive = true;
    this.exitCode = null;
    this.isFiles = true;
    this.opened = true;
    this.faOpen = false;
    this.closed = false;
    this.gone = false;
    this.project = info.spec.project;
    this.dirs = {};           // folder path ('' = project) -> { entries, error, more, loading }
    this.expanded = this.loadExpanded();
    this.sel = '';            // selected path (file or folder)
    this.file = null;         // the file shown on the right: { path, data, mode: 'file'|'diff' }
    this.changes = {};        // path -> { code, repo, rel, where, untracked }
    this.changedDirs = {};    // folder path -> number of changed files inside
    this.changeCount = 0;
    this.loadedAt = 0;
    this.allFiles = null;     // for the filter box
    SW.tabEl(this);

    this.filter = h('input', { class: 'inp files-filter', placeholder: 'Find file', spellcheck: 'false', dir: 'auto',
      oninput: function () { self.renderTree(); }, onkeydown: function (e) { self.filterKey(e); } });
    this.gitBtn = h('button', { class: 'icon-btn files-git hidden', title: 'Changes, commit, pull, push',
      onclick: function () { var p = SW.project(self.project); if (p) SW.gitDialog(p); } });
    this.tree = h('div', { class: 'files-tree' });
    this.tree.addEventListener('contextmenu', function (e) { if (e.target === self.tree) { e.preventDefault(); self.menu(e.clientX, e.clientY); } });
    this.side = h('div', { class: 'files-side' },
      h('div', { class: 'files-bar' }, this.filter, this.gitBtn,
        h('button', { class: 'icon-btn', title: 'Refresh', onclick: function () { self.refresh(); } }, '⟳')),
      this.tree);
    this.vhead = h('div', { class: 'files-vhead' });
    this.vbody = h('div', { class: 'files-vbody' });
    this.view = h('div', { class: 'files-view' }, this.vhead, this.vbody);
    this.body = h('div', { class: 'pbody files-body' }, this.side, this.view);
    this.pane = h('div', { class: 'pane files hidden-pane' }, this.body);
    this.showEmptyView();
    this.refresh();
  }

  var P = FilesTab.prototype;

  // what app.js expects from every tab
  P.focus = function () { if (!TOUCH) this.filter.focus(); };
  P.ensureOpen = function () { if (this.loadedAt && Date.now() - this.loadedAt > STALE) this.refresh(true); };
  P.doFit = function () {};
  P.toggleFa = function () {};
  P.renderFa = function () {};
  P.toggleComposer = function () { this.filter.focus(); };
  P.connect = function () {};
  P.stop = function () {};
  P.restart = function () { this.refresh(); };
  P.applyInfo = function () { this.ttlEl.textContent = this.info.title; };

  P.loadExpanded = function () {
    try { return JSON.parse(localStorage.getItem('sw-files:' + this.project) || '[]') || []; } catch (e) { return []; }
  };
  P.saveExpanded = function () {
    try { localStorage.setItem('sw-files:' + this.project, JSON.stringify(this.expanded.slice(0, 200))); } catch (e) { /* full */ }
  };
  P.isOpen = function (path) { return this.expanded.indexOf(path) >= 0; };

  // ---------------------------------------------------------------- loading
  P.list = function (path) {
    var self = this;
    var d = this.dirs[path] || (this.dirs[path] = { entries: null });
    d.loading = true;
    return api('GET', 'fs/' + enc(this.project) + '/list?path=' + enc(path)).then(function (r) {
      self.dirs[path] = { entries: r.entries || [], more: !!r.more };
      self.where = r.where;
    }).catch(function (e) {
      if (e.offline) { d.loading = false; return; }
      self.dirs[path] = { entries: [], error: e.message };
      if (path) self.expanded = self.expanded.filter(function (x) { return x !== path; });   // gone: stop opening it
    });
  };

  P.refresh = function (quiet) {
    var self = this;
    this.loadedAt = Date.now();
    this.allFiles = null;
    if (!quiet && !this.dirs['']) this.tree.replaceChildren(h('div', { class: 'muted files-note' }, 'Loading…'));
    var paths = [''].concat(this.expanded);
    var jobs = paths.map(function (p) { return self.list(p); });
    var done = Promise.all(jobs).then(function () { self.saveExpanded(); self.renderTree(); });
    this.loadGit();
    if (this.file && quiet) this.openFile(this.file.path, this.file.mode, true);
    return done;
  };

  P.loadGit = function () {
    var self = this;
    return api('GET', 'git/' + enc(this.project) + '/repos').then(function (d) {
      var changes = {}, dirs = {}, n = 0;
      (d.repos || []).forEach(function (repo) {
        var st = (d.statuses || {})[repo];
        if (!st || !st.repo) return;
        (st.files || []).forEach(function (f) {
          var full = join(repo, f.path.replace(/\/$/, ''));
          changes[full] = { code: shownCode(SW.gitCode(f)), repo: repo, rel: f.path, where: d.where, untracked: !!f.untracked };
          n++;
          for (var p = parentOf(full); ; p = parentOf(p)) { dirs[p] = (dirs[p] || 0) + 1; if (!p) break; }
        });
      });
      self.changes = changes;
      self.changedDirs = dirs;
      self.changeCount = n;
      self.gitBtn.textContent = '⎇ ' + n;
      self.gitBtn.classList.toggle('hidden', !n);
      self.renderTree();
      if (self.file) self.renderHead();
    }).catch(function () { /* not a repository, or git is missing: just no marks */ });
  };

  // ---------------------------------------------------------------- tree
  P.toggle = function (path) {
    var self = this;
    this.sel = path;
    if (this.isOpen(path)) {
      this.expanded = this.expanded.filter(function (x) { return x !== path && x.indexOf(path + '/') !== 0; });
      this.saveExpanded();
      this.renderTree();
      return Promise.resolve();
    }
    this.expanded.push(path);
    this.saveExpanded();
    this.renderTree();
    return this.list(path).then(function () { self.renderTree(); });
  };

  P.reveal = function (path) {
    // open every folder on the way to path, then select it
    var self = this, parts = path.split('/'), chain = Promise.resolve();
    parts.slice(0, -1).forEach(function (_, i) {
      var dir = parts.slice(0, i + 1).join('/');
      if (!self.isOpen(dir)) self.expanded.push(dir);
      if (!self.dirs[dir] || !self.dirs[dir].entries) chain = chain.then(function () { return self.list(dir); });
    });
    this.saveExpanded();
    this.sel = path;
    return chain.then(function () { self.renderTree(); });
  };

  P.row = function (path, e, depth) {
    var self = this;
    var ch = this.changes[path], inside = e.dir ? this.changedDirs[path] : 0;
    var cls = 'ft-row' + (e.dir ? ' dir' : '') + (e.ignored ? ' ignored' : '') + (this.sel === path ? ' sel' : '')
      + (ch ? ' changed c' + ch.code : inside ? ' changed-in' : '');
    var row = h('div', { class: cls, style: 'padding-left:' + (6 + depth * 14) + 'px', title: path + (ch ? ' · ' + ch.code : '') },
      h('span', { class: 'ft-caret' }, e.dir ? (this.isOpen(path) ? '▾' : '▸') : ''),
      h('span', { class: 'ft-name', dir: 'auto' }, e.name + (e.link ? ' ↗' : '')),
      ch ? h('span', { class: 'gcode c' + ch.code }, ch.code) : inside ? h('span', { class: 'ft-dot', title: inside + ' changed inside' }, '•') : null,
      TOUCH && this.sel === path ? h('button', { class: 'icon-btn ft-more', title: 'More', onclick: function (ev) {
        ev.stopPropagation(); var r = ev.currentTarget.getBoundingClientRect(); self.rowMenu(path, e.dir, r.left, r.bottom);
      } }, '⋯') : null);
    row.addEventListener('click', function () { if (e.dir) self.toggle(path); else self.openFile(path); });
    row.addEventListener('contextmenu', function (ev) { ev.preventDefault(); ev.stopPropagation(); self.sel = path; self.renderTree(); self.rowMenu(path, e.dir, ev.clientX, ev.clientY); });
    return row;
  };

  P.renderTree = function () {
    var q = this.filter.value.trim();
    if (q) return this.renderMatches(q);
    var self = this, rows = [];
    var root = this.dirs[''];
    if (!root || !root.entries) { if (root && root.error) this.tree.replaceChildren(h('div', { class: 'muted files-note' }, root.error)); return; }
    if (root.error) { this.tree.replaceChildren(h('div', { class: 'muted files-note' }, root.error)); return; }
    (function walk(dir, depth) {
      var d = self.dirs[dir];
      if (!d) return;
      if (d.error && dir) { rows.push(h('div', { class: 'muted files-note', style: 'padding-left:' + (20 + depth * 14) + 'px' }, d.error)); return; }
      if (!d.entries) { rows.push(h('div', { class: 'muted files-note', style: 'padding-left:' + (20 + depth * 14) + 'px' }, 'Loading…')); return; }
      d.entries.forEach(function (e) {
        var path = join(dir, e.name);
        rows.push(self.row(path, e, depth));
        if (e.dir && self.isOpen(path)) walk(path, depth + 1);
      });
      if (d.more) rows.push(h('div', { class: 'muted files-note', style: 'padding-left:' + (20 + depth * 14) + 'px' }, 'More files are not shown — use Find file.'));
    })('', 0);
    if (!rows.length) rows.push(h('div', { class: 'muted files-note' }, 'This folder is empty.'));
    this.tree.replaceChildren.apply(this.tree, rows);
  };

  // ---------------------------------------------------------------- find file
  P.loadAll = function () {
    var self = this;
    if (this.allFiles || this.loadingAll) return;
    this.loadingAll = true;
    api('GET', 'files/' + enc(this.project)).then(function (d) {
      self.allFiles = d.files || [];
      self.loadingAll = false;
      if (self.filter.value.trim()) self.renderTree();
    }).catch(function (e) { self.loadingAll = false; self.allFiles = []; fail(e); });
  };

  P.matches = function (q) {
    q = q.toLowerCase();
    var out = [], files = this.allFiles || [];
    for (var i = 0; i < files.length && out.length < 4000; i++) {
      var f = files[i];
      if (/\/$/.test(f)) continue;
      var fl = f.toLowerCase(), at = fl.indexOf(q);
      if (at < 0) continue;
      var base = baseOf(fl), b = base.indexOf(q);
      out.push([b === 0 ? 0 : b > 0 ? 1 : 2, f.length, f]);
    }
    out.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    return out.slice(0, 200).map(function (x) { return x[2]; });
  };

  P.renderMatches = function (q) {
    var self = this;
    if (!this.allFiles) {
      this.loadAll();
      this.tree.replaceChildren(h('div', { class: 'muted files-note' }, 'Looking…'));
      return;
    }
    var list = this.matches(q);
    this.found = list;
    this.foundSel = Math.min(this.foundSel || 0, Math.max(0, list.length - 1));
    if (!list.length) { this.tree.replaceChildren(h('div', { class: 'muted files-note' }, 'No file matches.')); return; }
    this.tree.replaceChildren.apply(this.tree, list.map(function (f, i) {
      var ch = self.changes[f], dir = parentOf(f);
      var row = h('div', { class: 'ft-row match' + (i === self.foundSel ? ' sel' : '') + (ch ? ' changed c' + ch.code : ''), title: f },
        h('span', { class: 'ft-name', dir: 'auto' }, baseOf(f)), dir ? h('span', { class: 'ft-dir', dir: 'auto' }, dir) : null,
        ch ? h('span', { class: 'gcode c' + ch.code }, ch.code) : null);
      row.addEventListener('click', function () { self.pickMatch(f); });
      row.addEventListener('contextmenu', function (ev) { ev.preventDefault(); self.rowMenu(f, false, ev.clientX, ev.clientY); });
      return row;
    }));
  };

  P.pickMatch = function (f) {
    this.filter.value = '';
    this.foundSel = 0;
    this.openFile(f);
    this.reveal(f);
  };

  P.filterKey = function (e) {
    var list = this.found || [];
    if (e.key === 'Escape') { this.filter.value = ''; this.renderTree(); return; }
    if (!this.filter.value.trim() || !list.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      this.foundSel = Math.max(0, Math.min(list.length - 1, (this.foundSel || 0) + (e.key === 'ArrowDown' ? 1 : -1)));
      this.renderTree();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      this.pickMatch(list[this.foundSel || 0]);
    }
  };

  // ---------------------------------------------------------------- viewer
  P.showEmptyView = function () {
    this.vhead.replaceChildren();
    this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Pick a file to see it here.'));
  };

  P.closeFile = function () {
    this.file = null;
    this.body.classList.remove('viewing');
    this.showEmptyView();
  };

  P.renderHead = function () {
    var self = this, f = this.file, ch = this.changes[f.path], d = f.data || {};
    var p = SW.project(this.project);
    var info = d.size !== undefined ? fmtSize(d.size) : '';
    this.vhead.replaceChildren.apply(this.vhead, [
      h('button', { class: 'icon-btn files-back', title: 'Back to the files', onclick: function () { self.closeFile(); } }, '←'),
      h('span', { class: 'files-path mono', dir: 'auto', title: f.path }, f.path),
      ch ? h('span', { class: 'gcode c' + ch.code, title: 'Changed (git)' }, ch.code) : null,
      info ? h('span', { class: 'muted files-size' }, info) : null,
      h('span', { class: 'grow' }),
      ch ? h('button', { class: 'btn sm' + (f.mode === 'diff' ? ' on' : ''), title: f.mode === 'diff' ? 'Show the file' : 'Show what changed',
        onclick: function () { self.openFile(f.path, f.mode === 'diff' ? 'file' : 'diff'); } }, f.mode === 'diff' ? 'File' : 'Changes') : null,
      h('button', { class: 'btn sm', disabled: !(p && p.remote), title: 'Mention this file in a Claude chat', onclick: function () { askClaude(self.project, f.path); } }, 'Ask Claude'),
      h('button', { class: 'btn sm', title: 'Copy the path', onclick: function () { copyPath(f.path); } }, 'Copy path')
    ].filter(Boolean));
  };

  P.openFile = function (path, mode, quiet) {
    var self = this;
    mode = mode || 'file';
    var ch = this.changes[path];
    if (mode === 'diff' && !ch) mode = 'file';
    this.sel = path;
    this.file = { path: path, mode: mode, data: this.file && this.file.path === path ? this.file.data : null };
    this.body.classList.add('viewing');
    this.renderHead();
    if (!this.filter.value.trim()) this.renderTree();
    if (!quiet) this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Loading…'));
    var still = function () { return self.file && self.file.path === path && self.file.mode === mode; };
    if (mode === 'diff') {
      return api('GET', 'git/' + enc(this.project) + '/diff?repo=' + enc(ch.repo) + '&where=' + enc(ch.where || '') + '&path=' + enc(ch.rel) + '&untracked=' + (ch.untracked ? 1 : 0))
        .then(function (d) { if (still()) self.vbody.replaceChildren(SW.diffEl(d.diff || '(no textual changes)')); })
        .catch(function (e) { if (still()) self.vbody.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
    }
    return api('GET', 'fs/' + enc(this.project) + '/read?path=' + enc(path)).then(function (d) {
      if (!still()) return;
      self.file.data = d;
      self.renderHead();
      self.vbody.replaceChildren(self.content(d));
    }).catch(function (e) { if (still()) self.vbody.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
  };

  P.content = function (d) {
    if (d.image) {
      return h('div', { class: 'files-img' }, h('img', { src: d.image, alt: d.path, onclick: function () { SW.viewImage(d.image); } }));
    }
    if (d.tooBig) return h('div', { class: 'muted files-note' }, 'Too big to show here (' + fmtSize(d.size) + ').');
    if (d.binary) return h('div', { class: 'muted files-note' }, 'Binary file (' + fmtSize(d.size) + ').');
    var text = d.text || '';
    if (!text) return h('div', { class: 'muted files-note' }, 'Empty file.');
    var lines = text.replace(/\n$/, '').split('\n').length, nums = [];
    for (var i = 1; i <= lines; i++) nums.push(i);
    return h('div', { class: 'files-code' },
      h('pre', { class: 'ft-nums', 'aria-hidden': 'true' }, nums.join('\n')),
      h('pre', { class: 'ft-text' }, text.replace(/\n$/, '')));
  };

  // ---------------------------------------------------------------- menus
  P.rowMenu = function (path, isDir, x, y) {
    var self = this, ch = this.changes[path], p = SW.project(this.project);
    SW.showMenu(x, y, [
      isDir ? { label: this.isOpen(path) ? 'Collapse' : 'Expand', action: function () { self.toggle(path); } }
        : { label: 'Open', action: function () { self.openFile(path); } },
      !isDir && ch ? { label: 'Show changes', action: function () { self.openFile(path, 'diff'); } } : null,
      { label: 'Ask Claude about it', disabled: !(p && p.remote), action: function () { askClaude(self.project, path + (isDir ? '/' : '')); } },
      { label: 'Copy path', action: function () { copyPath(path); } },
      '-',
      { label: 'Refresh', action: function () { self.refresh(); } }
    ]);
  };

  P.menu = function (x, y, extra) {
    var self = this;
    SW.showMenu(x, y, [
      { label: 'Refresh', action: function () { self.refresh(); } },
      { label: 'Collapse all', disabled: !this.expanded.length, action: function () { self.expanded = []; self.saveExpanded(); self.renderTree(); } },
      { label: 'Git…', action: function () { var p = SW.project(self.project); if (p) SW.gitDialog(p); } },
      { label: 'Rename…', action: function () { SW.renameTab(self); } },
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { SW.closeTab(self); } }
    ].concat(extra || SW.layoutItemsFor(self)));
  };

  P.dispose = function () {
    this.closed = true;
    this.el.remove();
    this.pane.remove();
  };

  // ---------------------------------------------------------------- helpers
  function copyPath(path) {
    navigator.clipboard.writeText(path).then(function () { toast('Copied ' + path, 'ok'); }).catch(fail);
  }

  // Put @path into the project's Claude chat (the open one, or a new chat).
  function askClaude(project, path) {
    var S = SW.S;
    var chats = S.tabs.filter(function (t) { return t.isChat && !t.gone && t.info.spec.project === project; });
    var put = function (tab) {
      if (!tab || !tab.ta) return;
      var v = tab.ta.value;
      tab.ta.value = v + (v && !/\s$/.test(v) ? ' ' : '') + '@' + path + ' ';
      if (tab.autosize) tab.autosize();
      SW.activate(tab);
      tab.ta.focus();
      tab.ta.setSelectionRange(tab.ta.value.length, tab.ta.value.length);
    };
    var last = chats[chats.length - 1];
    if (last) { put(last); return Promise.resolve(last); }
    return SW.openSession({ kind: 'chat', project: project }).then(function (t) { put(t); return t; });
  }

  window.SWFiles = { FilesTab: FilesTab, askClaude: askClaude };
})();
