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
      onclick: function () { var p = SW.project(self.project); if (p) SW.gitDialog(p); } });
    this.tree = h('div', { class: 'files-tree' });
    this.tree.addEventListener('contextmenu', function (e) { if (e.target === self.tree) { e.preventDefault(); self.menu(e.clientX, e.clientY); } });
    this.tree.tabIndex = 0;
    this.tree.addEventListener('keydown', function (e) { self.treeKey(e); });
    this.tree.addEventListener('dragover', function (ev) { if (ev.target === self.tree) { ev.preventDefault(); self.tree.classList.add('ft-drop'); } });
    this.tree.addEventListener('dragleave', function (ev) { if (!self.tree.contains(ev.relatedTarget)) self.tree.classList.remove('ft-drop'); });
    this.tree.addEventListener('drop', function (ev) { if (ev.target === self.tree || ev.currentTarget === self.tree) { ev.preventDefault(); self.tree.classList.remove('ft-drop'); self.uploadFiles(ev.dataTransfer.files, ''); } });
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
    row.addEventListener('drop', function (ev) { ev.preventDefault(); row.classList.remove('ft-drop'); self.uploadFiles(ev.dataTransfer.files, dropTarget); });
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

  P.uploadFiles = function (files, folder) {
    var self = this;
    if (!files || !files.length) return;
    Array.prototype.forEach.call(files, function (file) {
      var url = '/api/fs/' + encodeURIComponent(self.key) + '/upload?path=' + encodeURIComponent(folder) + '&name=' + encodeURIComponent(file.name);
      SW.toast('Uploading ' + file.name + '…', 'ok', { timeout: 3000 });
      var reader = new FileReader();
      reader.onload = function (ev) {
        fetch(url, { method: 'POST', headers: { 'X-Token': SW.token(), 'Content-Type': 'application/octet-stream' }, body: ev.target.result })
          .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.error || r.statusText); }); })
          .then(function () { SW.toast('Uploaded ' + file.name, 'ok', { timeout: 2000 }); self.refresh(true); })
          .catch(function (e) { SW.toast(file.name + ': ' + e.message, 'err'); });
      };
      reader.readAsArrayBuffer(file);
    });
  };

  // ---------------------------------------------------------------- helpers
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
    ((SW.S && SW.S.tabs) || []).forEach(function (t) { if (t.isFiles && t.project === e.detail) t.refresh(true); });
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
        if (e.dir) {
          row.addEventListener('dragover', function (ev) { ev.preventDefault(); row.classList.add('drop-over'); });
          row.addEventListener('dragleave', function (ev) { if (!row.contains(ev.relatedTarget)) row.classList.remove('drop-over'); });
          row.addEventListener('drop', function (ev) { ev.preventDefault(); row.classList.remove('drop-over'); self._upload(ev.dataTransfer.files, path); });
        }
        rows.push(row);
        if (e.dir && self._isOpen(path)) walk(path, depth + 1);
      });
    })('', 0);
    if (!rows.length) rows.push(h('div', { class: 'sf-note' }, 'This folder is empty.'));
    this.treeEl.ondragover = function (ev) { ev.preventDefault(); self.treeEl.classList.add('drop-over'); };
    this.treeEl.ondragleave = function (ev) { if (!self.treeEl.contains(ev.relatedTarget)) self.treeEl.classList.remove('drop-over'); };
    this.treeEl.ondrop = function (ev) { ev.preventDefault(); self.treeEl.classList.remove('drop-over'); self._upload(ev.dataTransfer.files, ''); };
    this.treeEl.replaceChildren.apply(this.treeEl, rows);
  };

  SP._upload = function (files, folder) {
    var self = this, project = this.project;
    if (!files || !files.length || !project) return;
    Array.from(files).forEach(function (file) {
      toast('Uploading ' + file.name + '…', 'ok', { timeout: 2500 });
      var url = '/api/fs/' + encodeURIComponent(project) + '/upload?path=' + encodeURIComponent(folder) + '&name=' + encodeURIComponent(file.name);
      fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-Token': SW.token || '', 'X-SW': '1' },
        body: file
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || r.statusText); });
        return r.json();
      }).then(function () {
        toast('Uploaded ' + file.name, 'ok');
        if (self.project === project) self.refreshTree();
      }).catch(function (e) { toast('Upload failed: ' + e.message, 'err'); });
    });
  };

  window.SideFilesPanel = SideFilesPanel;

  window.SWFiles = { FilesTab: FilesTab, askClaude: askClaude };
})();
