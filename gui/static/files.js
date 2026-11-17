/* Simab Workspace — Files tab: the folders of a project (or the server's home folder), git changes marked;
   create, rename, copy / cut / paste, delete and edit files. */
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
  var clip = null;            // { project, path, cut } — copy / cut between Files tabs of the same place

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
    this.project = info.spec.project || '';
    this.isServer = !this.project;          // the server's own files: the SSH user's home folder
    this.key = this.project || '~';
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
      onclick: function () { SW.openSession({ kind: 'changes', project: self.project }); } });
    this.tree = h('div', { class: 'files-tree' });
    this.tree.addEventListener('contextmenu', function (e) { if (e.target === self.tree) { e.preventDefault(); self.menu(e.clientX, e.clientY); } });
    this.tree.tabIndex = 0;
    this.tree.addEventListener('keydown', function (e) { self.treeKey(e); });
    this.tree.addEventListener('dragover', function (ev) { ev.preventDefault(); if (ev.target === self.tree) self.tree.classList.add('ft-drop'); });
    this.tree.addEventListener('dragleave', function (ev) { if (!self.tree.contains(ev.relatedTarget)) self.tree.classList.remove('ft-drop'); });
    this.tree.addEventListener('drop', function (ev) { ev.preventDefault(); self.tree.classList.remove('ft-drop'); self.uploadFiles(ev.dataTransfer, ''); });
    this.side = h('div', { class: 'files-side' },
      h('div', { class: 'files-bar' }, this.filter, this.gitBtn,
        h('button', { class: 'icon-btn', title: 'New file', onclick: function () { self.create(false); } }, '+'),
        h('button', { class: 'icon-btn', title: 'New folder', onclick: function () { self.create(true); } }, '▢'),
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
    try { return JSON.parse(localStorage.getItem('sw-files:' + this.key) || '[]') || []; } catch (e) { return []; }
  };
  P.saveExpanded = function () {
    try { localStorage.setItem('sw-files:' + this.key, JSON.stringify(this.expanded.slice(0, 200))); } catch (e) { /* full */ }
  };
  P.isOpen = function (path) { return this.expanded.indexOf(path) >= 0; };

  // ---------------------------------------------------------------- loading
  P.list = function (path) {
    var self = this;
    var d = this.dirs[path] || (this.dirs[path] = { entries: null });
    d.loading = true;
    return api('GET', 'fs/' + enc(this.key) + '/list?path=' + enc(path)).then(function (r) {
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
    if (this.isServer) return Promise.resolve();            // the home folder is not one repository
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
    var dropTarget = e.dir ? path : parentOf(path);
    row.addEventListener('dragover', function (ev) { ev.preventDefault(); row.classList.add('ft-drop'); });
    row.addEventListener('dragleave', function (ev) { if (!row.contains(ev.relatedTarget)) row.classList.remove('ft-drop'); });
    row.addEventListener('drop', function (ev) { ev.preventDefault(); ev.stopPropagation(); row.classList.remove('ft-drop'); self.tree.classList.remove('ft-drop'); self.uploadFiles(ev.dataTransfer, dropTarget); });
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
    if (this.isServer) { this.allFiles = []; return; }       // the whole home folder is too big to list
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
      h('span', { class: 'files-path mono', dir: 'auto', title: f.path }, f.path + (f.editing && f.dirty ? ' •' : '')),
      ch ? h('span', { class: 'gcode c' + ch.code, title: 'Changed (git)' }, ch.code) : null,
      info ? h('span', { class: 'muted files-size' }, info) : null,
      h('span', { class: 'grow' }),
      ch ? h('button', { class: 'btn sm' + (f.mode === 'diff' ? ' on' : ''), title: f.mode === 'diff' ? 'Show the file' : 'Show what changed',
        onclick: function () { self.openFile(f.path, f.mode === 'diff' ? 'file' : 'diff'); } }, f.mode === 'diff' ? 'File' : 'Changes') : null,
      f.mode === 'file' && typeof d.text === 'string' && !f.editing ? h('button', { class: 'btn sm', title: 'Edit this file', onclick: function () { self.edit(); } }, 'Edit') : null,
      f.editing ? h('button', { class: 'btn sm primary', title: 'Save (Ctrl+S)', onclick: function () { self.save(); } }, 'Save') : null,
      f.editing ? h('button', { class: 'btn sm', title: 'Stop editing (changes are dropped)', onclick: function () { self.cancelEdit(); } }, 'Cancel') : null,
      h('button', { class: 'btn sm', disabled: !this.isServer && !(p && p.remote), title: 'Mention this file in a Claude chat', onclick: function () { askClaude(self.project, f.path); } }, 'Ask Claude'),
      h('button', { class: 'btn sm', title: 'Copy the path', onclick: function () { copyPath(f.path); } }, 'Copy path')
    ].filter(Boolean));
  };

  P.openFile = function (path, mode, quiet) {
    var self = this;
    mode = mode || 'file';
    var ch = this.changes[path];
    if (mode === 'diff' && !ch) mode = 'file';
    if (this.file && this.file.editing && this.file.dirty && !quiet && !window.confirm('Drop the changes to ' + this.file.path + '?')) return Promise.resolve();
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
    return api('GET', 'fs/' + enc(this.key) + '/read?path=' + enc(path)).then(function (d) {
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
    var into = isDir ? path : parentOf(path);
    SW.showMenu(x, y, [
      isDir ? { label: this.isOpen(path) ? 'Collapse' : 'Expand', action: function () { self.toggle(path); } }
        : { label: 'Open', action: function () { self.openFile(path); } },
      !isDir ? { label: 'Edit', action: function () { self.openFile(path).then(function () { self.edit(); }); } } : null,
      !isDir && ch ? { label: 'Show changes', action: function () { self.openFile(path, 'diff'); } } : null,
      '-',
      { label: 'New file…', action: function () { self.create(false, into); } },
      { label: 'New folder…', action: function () { self.create(true, into); } },
      { label: 'Rename…', k: 'F2', action: function () { self.rename(path); } },
      { label: 'Copy', k: 'Ctrl+C', action: function () { self.setClip(path, false); } },
      { label: 'Cut', k: 'Ctrl+X', action: function () { self.setClip(path, true); } },
      { label: 'Paste' + (clip ? ' "' + baseOf(clip.path) + '"' : ''), k: 'Ctrl+V', disabled: !this.canPaste(into), action: function () { self.paste(into); } },
      { label: 'Delete…', k: 'Del', action: function () { self.remove(path, isDir); } },
      '-',
      { label: 'Ask Claude about it', disabled: !this.isServer && !(p && p.remote), action: function () { askClaude(self.project, path + (isDir ? '/' : '')); } },
      { label: 'Copy path', action: function () { copyPath(path); } },
      { label: 'Refresh', action: function () { self.refresh(); } }
    ]);
  };

  P.menu = function (x, y, extra) {
    var self = this;
    SW.showMenu(x, y, [
      { label: 'Refresh', action: function () { self.refresh(); } },
      { label: 'Collapse all', disabled: !this.expanded.length, action: function () { self.expanded = []; self.saveExpanded(); self.renderTree(); } },
      { label: 'New file…', action: function () { self.create(false, ''); } },
      { label: 'New folder…', action: function () { self.create(true, ''); } },
      { label: 'Paste' + (clip ? ' "' + baseOf(clip.path) + '"' : ''), disabled: !this.canPaste(''), action: function () { self.paste(''); } },
      this.isServer ? null : { label: 'Git…', action: function () { var p = SW.project(self.project); if (p) SW.gitDialog(p); } },
      { label: 'Rename…', action: function () { SW.renameTab(self); } },
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { SW.closeTab(self); } }
    ].concat(extra || SW.layoutItemsFor(self)));
  };

  // ---------------------------------------------------------------- changes
  // The folder new things go into: the selected folder, the folder of the selected file, or the top.
  P.here = function () {
    var sel = this.sel;
    if (!sel) return '';
    var parent = parentOf(sel), d = this.dirs[parent], e = d && d.entries && d.entries.find(function (x) { return x.name === baseOf(sel); });
    return e && e.dir ? sel : parent;
  };

  P.change = function (op, body) {
    var self = this;
    return api('POST', 'fs/' + enc(this.key) + '/' + op, body).then(function (r) {
      return Promise.resolve(self.refresh(true)).then(function () { return r; });
    });
  };

  P.create = function (folder, into) {
    var self = this;
    into = into === undefined ? this.here() : into;
    SW.promptDlg(folder ? 'New folder' + (into ? ' in ' + into : '') : 'New file' + (into ? ' in ' + into : ''), '').then(function (name) {
      if (!name) return;
      if (/[\/]/.test(name)) { toast('A name cannot contain /', 'warn'); return; }
      self.change(folder ? 'mkdir' : 'newfile', { path: join(into, name) }).then(function (r) {
        if (into && !self.isOpen(into)) self.expanded.push(into);
        return self.reveal(r.path).then(function () {
          if (!folder) return self.openFile(r.path).then(function () { self.edit(); });
        });
      }).catch(fail);
    });
  };

  P.rename = function (path) {
    var self = this;
    if (!path) return;
    SW.promptDlg('Rename "' + baseOf(path) + '"', baseOf(path)).then(function (name) {
      if (!name || name === baseOf(path)) return;
      if (/[\/]/.test(name)) { toast('A name cannot contain /', 'warn'); return; }
      self.change('rename', { path: path, name: name }).then(function (r) {
        self.expanded = self.expanded.map(function (x) { return x === path ? r.path : x.indexOf(path + '/') === 0 ? r.path + x.slice(path.length) : x; });
        if (self.file && (self.file.path === path || self.file.path.indexOf(path + '/') === 0)) self.file.path = r.path + self.file.path.slice(path.length);
        self.sel = r.path;
        self.saveExpanded();
        return self.refresh(true);
      }).catch(fail);
    });
  };

  P.setClip = function (path, cut) {
    if (!path) return;
    clip = { project: this.key, path: path, cut: !!cut };
    toast((cut ? 'Cut ' : 'Copied ') + baseOf(path) + ' — paste it into a folder', 'ok', { timeout: 2500 });
  };
  P.canPaste = function (into) {
    return !!clip && clip.project === this.key && !(clip.cut && (into === clip.path || into.indexOf(clip.path + '/') === 0));
  };
  P.paste = function (into) {
    var self = this;
    if (!this.canPaste(into)) return;
    var c = clip;
    this.change(c.cut ? 'move' : 'copy', { path: c.path, to: into }).then(function (r) {
      if (c.cut) clip = null;
      if (into && !self.isOpen(into)) self.expanded.push(into);
      return self.reveal(r.path);
    }).catch(fail);
  };

  P.remove = function (path, isDir) {
    var self = this;
    if (!path) return;
    SW.confirmDlg('Delete "' + path + '"' + (isDir ? ' and everything in it' : '') + '? This cannot be undone.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      self.change('delete', { path: path }).then(function () {
        self.expanded = self.expanded.filter(function (x) { return x !== path && x.indexOf(path + '/') !== 0; });
        self.saveExpanded();
        if (self.file && (self.file.path === path || self.file.path.indexOf(path + '/') === 0)) self.closeFile();
        self.sel = parentOf(path);
        self.renderTree();
      }).catch(fail);
    });
  };

  P.treeKey = function (e) {
    var sel = this.sel, mod = e.ctrlKey || e.metaKey;
    if (!sel && !(mod && e.key.toLowerCase() === 'v')) return;
    var parent = parentOf(sel), d = this.dirs[parent], ent = d && d.entries && d.entries.find(function (x) { return x.name === baseOf(sel); });
    var k = e.key.toLowerCase();
    if (e.key === 'F2') { e.preventDefault(); this.rename(sel); }
    else if (e.key === 'Delete') { e.preventDefault(); this.remove(sel, ent && ent.dir); }
    else if (mod && k === 'c') { e.preventDefault(); this.setClip(sel, false); }
    else if (mod && k === 'x') { e.preventDefault(); this.setClip(sel, true); }
    else if (mod && k === 'v') { e.preventDefault(); this.paste(this.here()); }
    else if (e.key === 'Enter' && ent) { e.preventDefault(); if (ent.dir) this.toggle(sel); else this.openFile(sel); }
  };

  // ---------------------------------------------------------------- editing
  P.edit = function () {
    var self = this, f = this.file;
    if (!f || !f.data || typeof f.data.text !== 'string') return;
    f.editing = true;
    f.dirty = false;
    var ta = this.editor = h('textarea', { class: 'files-edit', spellcheck: 'false', dir: 'auto', wrap: 'off' });
    ta.value = f.data.text;
    ta.addEventListener('input', function () { if (!f.dirty) { f.dirty = true; self.renderHead(); } });
    ta.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); self.save(); }
      else if (e.key === 'Tab' && !e.shiftKey) {
        e.preventDefault();
        var a = ta.selectionStart, b = ta.selectionEnd;
        ta.value = ta.value.slice(0, a) + '\t' + ta.value.slice(b);
        ta.selectionStart = ta.selectionEnd = a + 1;
        if (!f.dirty) { f.dirty = true; self.renderHead(); }
      } else if (e.key === 'Escape' && !f.dirty) { self.cancelEdit(); }
    });
    this.vbody.replaceChildren(ta);
    this.renderHead();
    setTimeout(function () { ta.focus(); }, 30);
  };

  P.cancelEdit = function () {
    var f = this.file;
    if (!f) return;
    if (f.dirty && !window.confirm('Drop your changes to ' + f.path + '?')) return;
    f.editing = false; f.dirty = false;
    this.renderHead();
    this.vbody.replaceChildren(this.content(f.data));
  };

  P.save = function (force) {
    var self = this, f = this.file;
    if (!f || !f.editing || !this.editor) return Promise.resolve();
    var text = this.editor.value;
    return api('POST', 'fs/' + enc(this.key) + '/write', { path: f.path, text: text, mtime: f.data.mtime, force: !!force }).then(function (r) {
      f.data = Object.assign({}, f.data, { text: text, mtime: r.mtime, size: r.size });
      f.dirty = false;
      toast('Saved ' + baseOf(f.path), 'ok', { timeout: 2000 });
      self.renderHead();
    }).catch(function (e) {
      if (e.status === 409) {
        return SW.confirmDlg(e.message + '. Save yours over it?', 'Save over it', true).then(function (ok) { if (ok) return self.save(true); });
      }
      fail(e);
    });
  };

  P.dispose = function () {
    this.closed = true;
    this.el.remove();
    this.pane.remove();
  };

  P.uploadFiles = function (dt, folder) {
    var self = this;
    uploadDropped(this.key, dt, folder).then(function (n) { if (n) self.refresh(true); });
  };

  // ---------------------------------------------------------------- helpers
  // Files dropped from the computer onto a folder: one after another, one toast for all.
  // Folders cannot be sent (the browser gives no bytes for them) — say so instead of failing quietly.
  var UPLOAD_MAX = 50000000;
  function droppedFiles(dt) {
    var files = Array.prototype.slice.call((dt && dt.files) || []), dirs = [];
    Array.prototype.forEach.call((dt && dt.items) || [], function (it) {
      var en = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (en && en.isDirectory) dirs.push(en.name);
    });
    return { files: files.filter(function (f) { return dirs.indexOf(f.name) < 0; }), dirs: dirs };
  }
  function uploadDropped(project, dt, folder) {
    var got = droppedFiles(dt), files = got.files, errs = [], done = 0;
    if (got.dirs.length) toast('Folders cannot be uploaded: ' + got.dirs.join(', '), 'err');
    files = files.filter(function (f) {
      if (f.size > UPLOAD_MAX) { errs.push(f.name + ': too large (max 50 MB)'); return false; }
      return true;
    });
    if (!files.length) { if (errs.length) toast(errs.join('; '), 'err'); return Promise.resolve(0); }
    var where = folder ? folder + '/' : 'the project folder';
    var t = toast('Uploading ' + (files.length === 1 ? files[0].name : files.length + ' files') + ' to ' + where + '…', 'ok', { sticky: true });
    var chain = Promise.resolve();
    files.forEach(function (file) {
      chain = chain.then(function () {
        var url = '/api/fs/' + encodeURIComponent(project) + '/upload?path=' + encodeURIComponent(folder || '') + '&name=' + encodeURIComponent(file.name);
        return fetch(url, { method: 'POST', credentials: 'same-origin',
          headers: { 'X-Token': SW.token || '', 'X-SW': '1', 'Content-Type': 'application/octet-stream' }, body: file })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status)); done++; }); })
          .catch(function (e) { errs.push(file.name + ': ' + e.message); });
      });
    });
    return chain.then(function () {
      if (t && t.remove) t.remove();
      if (done) toast('Uploaded ' + (done === 1 && files.length === 1 ? files[0].name : done + ' files') + ' to ' + where, 'ok', { timeout: 2500 });
      if (errs.length) toast('Upload failed — ' + errs.join('; '), 'err');
      return done;
    });
  }

  function copyPath(path) {
    navigator.clipboard.writeText(path).then(function () { toast('Copied ' + path, 'ok'); }).catch(fail);
  }

  // Put @path into the project's Claude chat (the open one, or a new chat).
  function askClaude(project, path) {
    var S = SW.S;
    var chats = S.tabs.filter(function (t) { return t.isChat && !t.gone && (t.info.spec.project || '') === (project || ''); });
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
    return SW.openSession({ kind: 'chat', project: project || undefined }).then(function (t) { put(t); return t; });
  }

  // another branch: the files changed — every Files tab of that project reads them again
  window.addEventListener('sw-git-changed', function (e) {
    ((SW.S && SW.S.tabs) || []).forEach(function (t) { if (t.isFiles && t.project === e.detail) t.refresh(true); });   // Files and Changes tabs
    if (window.sideFilesPanel && window.sideFilesPanel.project === e.detail) window.sideFilesPanel.refreshTree();
  });

  // ---------------------------------------------------------------- SideFilesPanel
  // A compact read-only file tree in the sidebar that follows the active project.
  // Supports drag-and-drop upload from the local OS into any folder.
  function SideFilesPanel() {
    var self = this;
    this.project = null;
    this.projectDirs = {};   // project -> { path -> { entries, error, loading } }
    this.projectExpanded = {};  // project -> [open paths]
    this.projectChanges = {};   // project -> { path -> git code }
    this.closedByUser = false;

    this.el = document.getElementById('side-files');
    this.projEl = document.getElementById('side-files-proj');
    this.treeEl = document.getElementById('side-files-tree');
    if (!this.el) return;

    document.getElementById('side-files-close').addEventListener('click', function () {
      self.closedByUser = true;
      self.el.classList.add('hidden');
    });
  }

  var SP = SideFilesPanel.prototype;

  SP.onActivate = function (tab) {
    if (!this.el) return;
    var project = tab && tab.info && tab.info.spec && tab.info.spec.project;
    if (!project) { this.el.classList.add('hidden'); return; }
    if (this.closedByUser && project === this.project) return;
    this.closedByUser = false;
    var switched = project !== this.project;
    this.project = project;
    this.projEl.textContent = project;
    this.el.classList.remove('hidden');
    if (!this.projectDirs[project] || switched) {
      this.projectDirs[project] = {};
      this._load('');
      this._loadGit();
    } else {
      this._render();
    }
  };

  SP._dirs = function () { return this.projectDirs[this.project] || (this.projectDirs[this.project] = {}); };
  SP._expanded = function () { return this.projectExpanded[this.project] || (this.projectExpanded[this.project] = []); };
  SP._changes = function () { return this.projectChanges[this.project] || {}; };
  SP._isOpen = function (path) { return this._expanded().indexOf(path) >= 0; };

  SP._load = function (path) {
    var self = this, project = this.project;
    if (!project) return Promise.resolve();
    var dirs = this._dirs();
    dirs[path] = { loading: true };
    return api('GET', 'fs/' + enc(project) + '/list?path=' + enc(path)).then(function (r) {
      if (self.project !== project) return;
      self._dirs()[path] = { entries: r.entries || [] };
      self._render();
    }).catch(function (e) {
      if (self.project !== project) return;
      self._dirs()[path] = { error: e.message };
      self._render();
    });
  };

  SP._loadGit = function () {
    var self = this, project = this.project;
    if (!project) return;
    api('GET', 'git/' + enc(project) + '/repos').then(function (d) {
      if (self.project !== project) return;
      var changes = {};
      (d.repos || []).forEach(function (repo) {
        var st = (d.statuses || {})[repo];
        if (!st || !st.repo) return;
        (st.files || []).forEach(function (f) {
          var full = join(repo, f.path.replace(/\/$/, ''));
          changes[full] = SW.gitCode(f);
        });
      });
      self.projectChanges[project] = changes;
      self._render();
    }).catch(function () {});
  };

  SP.refreshTree = function () {
    if (!this.project) return;
    this.projectDirs[this.project] = {};
    this._load('');
    var expanded = this._expanded().slice();
    var self = this;
    expanded.forEach(function (p) { self._load(p); });
    this._loadGit();
  };

  SP._toggle = function (path) {
    var expanded = this._expanded(), idx = expanded.indexOf(path);
    if (idx >= 0) {
      this.projectExpanded[this.project] = expanded.filter(function (x) { return x !== path && x.indexOf(path + '/') !== 0; });
      this._render();
      return;
    }
    expanded.push(path);
    this._render();
    if (!this._dirs()[path]) this._load(path);
  };

  SP._render = function () {
    var self = this, project = this.project;
    if (!project || !this.el) return;
    var dirs = this._dirs(), changes = this._changes();
    var root = dirs[''];
    if (!root || root.loading) {
      this.treeEl.replaceChildren(h('div', { class: 'sf-note' }, 'Loading…'));
      return;
    }
    if (root.error) {
      this.treeEl.replaceChildren(h('div', { class: 'sf-note' }, root.error));
      return;
    }
    var rows = [];
    (function walk(dir, depth) {
      var d = dirs[dir];
      if (!d || !d.entries) return;
      d.entries.forEach(function (e) {
        var path = join(dir, e.name);
        var gc = changes[path];
        var row = h('div', {
          class: 'sf-row' + (e.dir ? ' sf-dir' : ''),
          style: 'padding-left:' + (8 + depth * 12) + 'px',
          title: path
        },
          h('i', { class: 'sf-icon' }, e.dir ? (self._isOpen(path) ? '▾' : '▸') : ''),
          h('span', { class: 'sf-name', dir: 'auto' }, e.name),
          gc ? h('span', { class: 'sf-gc gcode c' + gc }, gc) : null
        );
        row.addEventListener('click', function () {
          if (e.dir) {
            self._toggle(path);
          } else {
            SW.openSession({ kind: 'files', project: project }).then(function (tab) {
              if (tab && tab.reveal) tab.reveal(path);
            });
          }
        });
        var dropTo = e.dir ? path : dir;     // dropped on a file: into the folder it is in
        row.addEventListener('dragover', function (ev) { ev.preventDefault(); row.classList.add('drop-over'); });
        row.addEventListener('dragleave', function (ev) { if (!row.contains(ev.relatedTarget)) row.classList.remove('drop-over'); });
        row.addEventListener('drop', function (ev) { ev.preventDefault(); ev.stopPropagation(); row.classList.remove('drop-over'); self.treeEl.classList.remove('drop-over'); self._upload(ev.dataTransfer, dropTo); });
        rows.push(row);
        if (e.dir && self._isOpen(path)) walk(path, depth + 1);
      });
    })('', 0);
    if (!rows.length) rows.push(h('div', { class: 'sf-note' }, 'This folder is empty.'));
    this.treeEl.ondragover = function (ev) { ev.preventDefault(); self.treeEl.classList.add('drop-over'); };
    this.treeEl.ondragleave = function (ev) { if (!self.treeEl.contains(ev.relatedTarget)) self.treeEl.classList.remove('drop-over'); };
    this.treeEl.ondrop = function (ev) { ev.preventDefault(); self.treeEl.classList.remove('drop-over'); self._upload(ev.dataTransfer, ''); };
    this.treeEl.replaceChildren.apply(this.treeEl, rows);
  };

  SP._upload = function (dt, folder) {
    var self = this, project = this.project;
    if (!project) return;
    uploadDropped(project, dt, folder).then(function (n) { if (n && self.project === project) self.refreshTree(); });
  };

  // ---------------------------------------------------------------- ChangesTab
  // Review what changed (by you or by Claude): the changed files on the left, the diff on the
  // right; tick what goes into the commit, discard a file, pull / push.
  function ChangesTab(info) {
    var self = this;
    this.id = info.id;
    this.info = info;
    this.alive = true;
    this.exitCode = null;
    this.isFiles = true;          // no process: app.js treats it like a Files tab
    this.isChanges = true;
    this.opened = true;
    this.faOpen = false;
    this.closed = false;
    this.gone = false;
    this.project = info.spec.project || '';
    this.data = null;             // git/<p>/repos answer
    this.picked = {};             // repo + '\0' + path -> false when left out of the commit
    this.sel = null;              // { repo, path }
    this.focusOn = null;          // files to show first (from a chat turn)
    this.hmode = 'changes';       // 'changes' or 'history'
    this.histRepo = null;         // repo path for history view
    this.histLog = null;          // log data: {commits, ahead, behind}
    this.histCommit = null;       // hash of commit shown in view
    this.pickedHunks = {};        // key -> Set of checked hunk indices | null (all)
    this.hunkMode = false;        // show hunk checkboxes in diff view
    this._lastDiff = null;        // cached diff text for hunk building
    SW.tabEl(this);
    this.branchEl = h('span', { class: 'ch-branch mono' });
    this.list = h('div', { class: 'files-tree ch-list' });
    this.msg = h('textarea', { class: 'inp ch-msg', rows: 2, placeholder: 'Commit message', dir: 'auto' });
    this.msg.addEventListener('keydown', function (e) { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); self.commit(); } });
    this.commitBtn = h('button', { class: 'btn primary', onclick: function () { self.commit(); } }, 'Commit');
    this.modeToggle = h('div', { class: 'ch-mode-toggle' },
      h('button', { class: 'btn sm on', onclick: function () { self.switchMode('changes'); } }, 'Changes'),
      h('button', { class: 'btn sm',    onclick: function () { self.switchMode('history'); } }, 'History'));
    this.commitArea = h('div', { class: 'ch-commit' }, this.msg, this.commitBtn);
    this.side = h('div', { class: 'files-side' },
      h('div', { class: 'files-bar' }, this.branchEl, h('span', { class: 'grow' }),
        h('button', { class: 'icon-btn', title: 'Pull', onclick: function () { self.git('pull'); } }, '↓'),
        h('button', { class: 'icon-btn', title: 'Push', onclick: function () { self.git('push'); } }, '↑'),
        h('button', { class: 'icon-btn', title: 'Refresh', onclick: function () { self.refresh(); } }, '⟳')),
      this.modeToggle,
      this.list,
      this.commitArea);
    this.vhead = h('div', { class: 'files-vhead' });
    this.vbody = h('div', { class: 'files-vbody' });
    this.view = h('div', { class: 'files-view' }, this.vhead, this.vbody);
    this.body = h('div', { class: 'pbody files-body' }, this.side, this.view);
    this.pane = h('div', { class: 'pane files hidden-pane' }, this.body);
    this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Pick a file to see what changed.'));
    this.refresh();
  }
  var C = ChangesTab.prototype;
  C.focus = function () { if (!TOUCH) this.list.focus(); };
  C.ensureOpen = function () { if (this.loadedAt && Date.now() - this.loadedAt > STALE) this.refresh(true); };
  C.doFit = function () {};
  C.toggleFa = function () {};
  C.renderFa = function () {};
  C.toggleComposer = function () { this.msg.focus(); };
  C.connect = function () {};
  C.stop = function () {};
  C.restart = function () { this.refresh(); };
  C.applyInfo = function () { this.ttlEl.textContent = this.info.title; };
  C.dispose = function () { this.closed = true; this.el.remove(); this.pane.remove(); };
  C.menu = function (x, y, extra) { SW.showMenu(x, y, [{ label: 'Refresh', action: this.refresh.bind(this) }].concat(extra ? ['-'].concat(extra) : [])); };

  C.files = function () {           // [{ repo, where, f }]
    var d = this.data, out = [];
    if (!d) return out;
    (d.repos || []).forEach(function (repo) {
      var st = (d.statuses || {})[repo];
      if (st && st.repo) (st.files || []).forEach(function (f) { out.push({ repo: repo, where: d.where, f: f }); });
    });
    return out;
  };

  C.refresh = function (quiet) {
    var self = this;
    if (!quiet) this.list.replaceChildren(h('div', { class: 'muted files-note' }, 'Reading git…'));
    return api('GET', 'git/' + enc(this.project) + '/repos').then(function (d) {
      self.data = d;
      self.loadedAt = Date.now();
      self.render();
      var all = self.files();
      var still = self.sel && all.some(function (x) { return x.repo === self.sel.repo && x.f.path === self.sel.path; });
      var first = null;
      if (self.focusOn) first = all.find(function (x) {                // Claude's paths are absolute on the server
        var full = join(x.repo, x.f.path);
        return self.focusOn.some(function (a) { return a === full || a.slice(-full.length - 1) === '/' + full; });
      });
      self.focusOn = null;
      if (self.hmode === 'history') { self.fetchHistory(); return; }
      if (first) self.show(first);
      else if (!still) { self.sel = null; if (!TOUCH && all.length) self.show(all[0]); else self.vbody.replaceChildren(h('div', { class: 'muted files-note' }, all.length ? 'Pick a file to see what changed.' : 'No changes — everything is committed.')); }
    }).catch(function (e) { self.list.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
  };

  C.render = function () {
    var self = this, d = this.data || {}, all = this.files();
    var st0 = (d.statuses || {})[(d.repos || [])[0]] || {};
    var multi = (d.repos || []).length > 1;
    this.branchEl.textContent = st0.branch ? '⎇ ' + st0.branch + (st0.ahead ? ' ↑' + st0.ahead : '') + (st0.behind ? ' ↓' + st0.behind : '') : '';
    var rows = [], lastRepo = null;
    all.forEach(function (x) {
      if (multi && x.repo !== lastRepo) { rows.push(h('div', { class: 'ch-repo mono' }, x.repo || '(project)')); lastRepo = x.repo; }
      var key = x.repo + '\0' + x.f.path;
      var box = h('input', { type: 'checkbox', title: 'In the next commit' });
      box.checked = self.picked[key] !== false;
      box.addEventListener('click', function (e) { e.stopPropagation(); self.picked[key] = box.checked; self.renderCount(); });
      var code = shownCode(SW.gitCode(x.f));
      var on = self.sel && self.sel.repo === x.repo && self.sel.path === x.f.path;
      var row = h('div', { class: 'ft-row ch-row' + (on ? ' sel' : ''), title: join(x.repo, x.f.path),
        onclick: function () { self.show(x); },
        oncontextmenu: function (e) { e.preventDefault(); self.rowMenu(x, e.clientX, e.clientY); } },
        box, h('span', { class: 'ft-name' }, baseOf(x.f.path.replace(/\/$/, ''))),
        h('span', { class: 'muted ch-dir mono' }, parentOf(x.f.path.replace(/\/$/, ''))),
        h('span', { class: 'ft-git g' + code }, code));
      rows.push(row);
    });
    if (!all.length) rows.push(h('div', { class: 'muted files-note' }, 'No changes — everything is committed.'));
    this.list.replaceChildren.apply(this.list, rows);
    this.renderCount();
  };

  C.pickedFiles = function () {
    var self = this;
    return this.files().filter(function (x) { return self.picked[x.repo + '\0' + x.f.path] !== false; });
  };
  C.renderCount = function () {
    var self = this, picked = this.pickedFiles(), all = this.files();
    var n = picked.length;
    picked.forEach(function (x) {
      var ph = self.pickedHunks[x.repo + '\0' + x.f.path];
      if (ph !== null && ph !== undefined && ph.size === 0) n--;
    });
    this.commitBtn.textContent = n === all.length ? 'Commit' + (all.length ? ' all ' + all.length : '') : 'Commit ' + n + ' of ' + all.length;
    this.commitBtn.disabled = !n;
  };

  C.show = function (x) {
    var self = this;
    var key = x.repo + '\0' + x.f.path;
    this.sel = { repo: x.repo, path: x.f.path, key: key };
    this.body.classList.add('viewing');
    this.render();
    var full = join(x.repo, x.f.path);
    this._lastDiff = null;
    var hunksBtn = h('button', { class: 'btn sm' + (this.hunkMode ? ' on' : ''), title: 'Pick hunks to include in the commit',
      onclick: function () {
        self.hunkMode = !self.hunkMode;
        hunksBtn.classList.toggle('on', self.hunkMode);
        if (self._lastDiff !== null) {
          self.vbody.replaceChildren(self.hunkMode ? self.diffHunksEl(self._lastDiff, key) : SW.diffEl(self._lastDiff || '(no textual changes)'));
        }
        self.renderCount();
      } });
    this.vhead.replaceChildren(
      h('button', { class: 'btn sm files-back', onclick: function () { self.body.classList.remove('viewing'); } }, '←'),
      h('span', { class: 'mono grow' }, full),
      hunksBtn,
      h('button', { class: 'btn sm', title: 'Open in the Files tab', onclick: function () {
        SW.openSession({ kind: 'files', project: self.project }).then(function (t) { if (t && t.openFile) t.openFile(full.replace(/\/$/, '')); });
      } }, 'Open'),
      h('button', { class: 'btn sm', title: 'Ask Claude about this file', onclick: function () { askClaude(self.project, full); } }, 'Ask Claude'),
      h('button', { class: 'btn sm danger', onclick: function () { self.discard([x]); } }, 'Discard'));
    this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Loading…'));
    api('GET', 'git/' + enc(this.project) + '/diff?repo=' + enc(x.repo) + '&where=' + enc(x.where || '') + '&path=' + enc(x.f.path) + '&untracked=' + (x.f.untracked ? 1 : 0))
      .then(function (d) {
        if (!self.sel || self.sel.key !== key) return;
        self._lastDiff = d.diff || '';
        self.vbody.replaceChildren(self.hunkMode ? self.diffHunksEl(self._lastDiff, key) : SW.diffEl(self._lastDiff || '(no textual changes)'));
      })
      .catch(function (e) { self.vbody.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
  };

  C.rowMenu = function (x, cx, cy) {
    var self = this;
    SW.showMenu(cx, cy, [
      { label: 'Show the change', action: function () { self.show(x); } },
      { label: 'Ask Claude about it', action: function () { askClaude(self.project, join(x.repo, x.f.path)); } },
      '-',
      { label: 'Discard the change…', action: function () { self.discard([x]); } }]);
  };

  C.discard = function (xs) {
    var self = this, x = xs[0];
    var what = x.f.untracked ? 'Delete the new file ' + x.f.path + '?' : 'Throw away the changes to ' + x.f.path + '? It goes back to the last commit.';
    SW.confirmDlg(what, x.f.untracked ? 'Delete' : 'Discard', true).then(function (ok) {
      if (!ok) return;
      api('POST', 'git/' + enc(self.project) + '/discard', { repo: x.repo, where: x.where, files: [x.f.path] }).then(function () {
        toast('Discarded ' + baseOf(x.f.path.replace(/\/$/, '')), 'ok');
        self.sel = null;
        window.dispatchEvent(new CustomEvent('sw-git-changed', { detail: self.project }));
        self.refresh(true);
      }).catch(fail);
    });
  };

  C.commit = function () {
    var self = this, msg = this.msg.value.trim();
    if (!msg) { this.msg.focus(); toast('Write a commit message', 'warn'); return; }
    var picked = this.pickedFiles(), byRepo = {};
    if (!picked.length) return;
    var all = this.files();
    picked.forEach(function (x) {
      var key = x.repo + '\0' + x.f.path;
      var ph = self.pickedHunks[key];
      var r = byRepo[x.repo] = byRepo[x.repo] || { where: x.where, files: [], patches: {} };
      // per-hunk: only use patch when this file is currently shown and has partial hunk selection
      if (ph !== null && ph !== undefined && ph.size > 0 && self._lastDiff !== null && self.sel && self.sel.key === key) {
        var patch = self.getHunkPatch(key, self._lastDiff);
        if (patch) { r.patches[x.f.path] = patch; return; }
      }
      r.files.push(x.f.path);
    });
    this.commitBtn.disabled = true;
    var chain = Promise.resolve();
    Object.keys(byRepo).forEach(function (repo) {
      var r = byRepo[repo];
      var allOfRepo = all.filter(function (x) { return x.repo === repo; }).length === (r.files.length + Object.keys(r.patches).length);
      var hasPatch = Object.keys(r.patches).length > 0;
      chain = chain.then(function () {
        return api('POST', 'git/' + enc(self.project) + '/commit', {
          repo: repo, where: r.where, message: msg,
          files: hasPatch ? (r.files.length ? r.files : null) : (allOfRepo ? null : r.files),
          patches: hasPatch ? r.patches : undefined
        });
      });
    });
    chain.then(function () {
      self.msg.value = '';
      self.picked = {};
      self.pickedHunks = {};
      self.hunkMode = false;
      self._lastDiff = null;
      toast('Committed', 'ok');
      window.dispatchEvent(new CustomEvent('sw-git-changed', { detail: self.project }));
      return self.refresh(true);
    }).catch(fail).then(function () { self.renderCount(); });
  };

  C.git = function (act) {
    var self = this, d = this.data || {};
    var repos = (d.repos || []).filter(function (r) { var st = (d.statuses || {})[r]; return st && st.repo; });
    if (!repos.length) return;
    toast(act === 'pull' ? 'Pulling…' : 'Pushing…', 'ok', { timeout: 1500 });
    var chain = Promise.resolve();
    repos.forEach(function (r) { chain = chain.then(function () { return api('POST', 'git/' + enc(self.project) + '/' + act, { repo: r, where: d.where }); }); });
    chain.then(function () { toast(act === 'pull' ? 'Pulled' : 'Pushed', 'ok'); return self.refresh(true); }).catch(fail);
  };

  C.switchMode = function (m) {
    this.hmode = m;
    var btns = this.modeToggle.querySelectorAll('.btn');
    btns[0].classList.toggle('on', m === 'changes');
    btns[1].classList.toggle('on', m === 'history');
    this.commitArea.classList.toggle('hidden', m === 'history');
    this.body.classList.remove('viewing');
    if (m === 'history') {
      this.list.replaceChildren(h('div', { class: 'muted files-note' }, 'Loading history…'));
      this.histCommit = null;
      this.vhead.replaceChildren();
      this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Pick a commit to see what changed.'));
      this.fetchHistory();
    } else {
      this.render();
    }
  };

  C.fetchHistory = function (repo) {
    var self = this;
    var d = this.data || {};
    if (repo !== undefined) this.histRepo = repo;
    if (!this.histRepo && d.repos && d.repos.length) this.histRepo = d.repos[0];
    if (!this.histRepo) { this.list.replaceChildren(h('div', { class: 'muted files-note' }, 'No repository.')); return; }
    api('GET', 'git/' + enc(this.project) + '/log?repo=' + enc(this.histRepo)).then(function (data) {
      self.histLog = data;
      self.renderHistory();
    }).catch(function (e) { self.list.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
  };

  C.renderHistory = function () {
    var self = this, d = this.data || {};
    var repos = (d.repos || []).filter(function (r) { return (d.statuses || {})[r] && (d.statuses || {})[r].repo; });
    var rows = [];
    if (repos.length > 1) {
      var sel = h('select', { class: 'inp ch-repo-sel', onchange: function () { self.fetchHistory(sel.value); } });
      repos.forEach(function (r) { var o = h('option', { value: r }, r || '(root)'); if (r === self.histRepo) o.selected = true; sel.appendChild(o); });
      rows.push(h('div', { class: 'ch-repo-row' }, sel));
    }
    var log = this.histLog || {};
    var commits = log.commits || [];
    if (!commits.length) rows.push(h('div', { class: 'muted files-note' }, 'No commits yet.'));
    commits.forEach(function (c) {
      var stateClass = c.state === 'push' ? 'push' : c.state === 'pull' ? 'pull' : 'sync';
      var on = self.histCommit === c.hash;
      var row = h('div', { class: 'ch-hist-row' + (on ? ' sel' : ''), onclick: function () { self.showCommit(c); } },
        h('span', { class: 'ch-hist-state ' + stateClass, title: c.state }),
        h('span', { class: 'ch-hist-hash mono' }, c.short),
        h('span', { class: 'ch-hist-subj', dir: 'auto' }, c.subject),
        h('span', { class: 'ch-hist-date muted' }, SW.agoShort ? SW.agoShort(c.date) : ''));
      rows.push(row);
    });
    this.list.replaceChildren.apply(this.list, rows);
  };

  C.showCommit = function (c) {
    var self = this;
    this.histCommit = c.hash;
    this.renderHistory();
    this.body.classList.add('viewing');
    this.vhead.replaceChildren(
      h('button', { class: 'btn sm files-back', onclick: function () { self.body.classList.remove('viewing'); } }, '←'),
      h('span', { class: 'ch-hist-hash mono', style: 'flex:none' }, c.short),
      h('span', { dir: 'auto', style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, c.subject));
    this.vbody.replaceChildren(h('div', { class: 'muted files-note' }, 'Loading…'));
    api('GET', 'git/' + enc(this.project) + '/show?repo=' + enc(this.histRepo || '') + '&hash=' + enc(c.hash))
      .then(function (d) { if (self.histCommit === c.hash) self.vbody.replaceChildren(SW.diffEl(d.diff || '(empty commit)')); })
      .catch(function (e) { self.vbody.replaceChildren(h('div', { class: 'muted files-note' }, e.message)); });
  };

  C.diffHunksEl = function (diff, key) {
    var self = this;
    var lines = String(diff || '').split('\n');
    var file_header = [], hunks = [], cur = null;
    lines.forEach(function (l) {
      if (/^(diff |index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(l)) { file_header.push(l); return; }
      if (l.slice(0, 2) === '@@') { if (cur) hunks.push(cur); cur = { header: l, lines: [] }; return; }
      if (cur) cur.lines.push(l); else file_header.push(l);
    });
    if (cur) hunks.push(cur);
    if (!hunks.length) return SW.diffEl(diff);
    if (!this.pickedHunks[key]) this.pickedHunks[key] = null;  // null = all selected
    var wrap = h('div', { class: 'gdiff ch-hunks' });
    file_header.forEach(function (l) {
      var cls = /^(--- |\+\+\+ |diff |index |new file|deleted file|similarity|rename )/.test(l) ? 'meta' : '';
      wrap.append(h('div', { class: 'gl ' + cls }, l || ' '));
    });
    hunks.forEach(function (hunk, i) {
      var ph = self.pickedHunks[key];
      var checked = ph === null || ph.has(i);
      var box = h('input', { type: 'checkbox', class: 'ch-hunk-box', title: 'Include this hunk in the commit' });
      box.checked = checked;
      var hunkEl = h('div', { class: 'ch-hunk' + (checked ? '' : ' ch-hunk-off') });
      box.addEventListener('change', function () {
        var cur2 = self.pickedHunks[key];
        if (cur2 === null) cur2 = new Set(hunks.map(function (_, j) { return j; }));
        if (box.checked) cur2.add(i); else cur2.delete(i);
        if (cur2.size === hunks.length) cur2 = null;
        self.pickedHunks[key] = cur2;
        hunkEl.classList.toggle('ch-hunk-off', !box.checked);
        self.renderCount();
      });
      var headerEl = h('div', { class: 'gl hunk ch-hunk-head' }, box, h('span', null, hunk.header));
      hunkEl.append(headerEl);
      hunk.lines.forEach(function (l) {
        var cls = l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : '';
        hunkEl.append(h('div', { class: 'gl ' + cls }, l || ' '));
      });
      wrap.append(hunkEl);
    });
    return wrap;
  };

  C.getHunkPatch = function (key, diff) {
    var ph = this.pickedHunks[key];
    if (ph === null) return null;
    var lines = String(diff || '').split('\n');
    var file_header = [], hunks = [], cur = null;
    lines.forEach(function (l) {
      if (/^(diff |index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(l)) { file_header.push(l); return; }
      if (l.slice(0, 2) === '@@') { if (cur) hunks.push(cur); cur = { header: l, lines: [] }; return; }
      if (cur) cur.lines.push(l); else file_header.push(l);
    });
    if (cur) hunks.push(cur);
    if (!ph.size) return null;
    var out = file_header.join('\n') + '\n';
    hunks.forEach(function (hk, i) { if (ph.has(i)) out += hk.header + '\n' + hk.lines.join('\n') + '\n'; });
    return out;
  };

  window.SideFilesPanel = SideFilesPanel;

  window.SWFiles = { FilesTab: FilesTab, ChangesTab: ChangesTab, askClaude: askClaude };
})();
