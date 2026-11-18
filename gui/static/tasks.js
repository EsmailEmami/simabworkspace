/* Simab Workspace — Tasks tab: the user's own to-do list. It is kept on the server
   (~/.config/simabworkspace/tasks.json), so the installed app and the web version show the same list.
   Add, tick done, project, priority, drag (or ⋯ Move up/down on touch) to reorder, hand one to Claude. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail;
  var TOUCH = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  var POLL = 15000;             // another device may have changed the list
  var PRIO = { 1: 'High', 2: 'Normal', 3: 'Low' };
  var cache = { tasks: null };  // the last list seen (for counts in menus)

  function openCount(project) {
    return (cache.tasks || []).filter(function (t) { return !t.done && (project === undefined || (t.project || '') === project); }).length;
  }
  function setRail() {
    var btn = document.querySelector('#btn-tasks'), b = btn && btn.querySelector('.rcount');
    if (!b) return;
    var n = openCount();
    b.textContent = cache.tasks && n ? String(n) : '';
  }

  function TasksTab(info) {
    var self = this;
    this.id = info.id;
    this.info = info;
    this.alive = true;
    this.exitCode = null;
    this.isFiles = true;          // no process: app.js treats it like a Files tab
    this.isTasks = true;
    this.opened = true;
    this.faOpen = false;
    this.closed = false;
    this.gone = false;
    this.project = '';
    this.tasks = cache.tasks ? cache.tasks.slice() : null;
    this.rev = -1;
    this.editing = null;          // id of the task being edited
    this.dragId = null;
    this.filterProj = localStorage.getItem('sw-tasks-filter') || '';     // '' all, '-' no project, else a project
    this.showDone = localStorage.getItem('sw-tasks-done') === '1';
    SW.tabEl(this);

    this.filterSel = h('select', { class: 'inp tk-filter', title: 'Show tasks of', onchange: function () { self.setFilter(self.filterSel.value); } });
    this.countEl = h('span', { class: 'muted tk-count' });
    this.input = h('input', { class: 'inp tk-new', placeholder: 'Add a task', dir: 'auto', spellcheck: 'false',
      onkeydown: function (e) { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); self.add(); } } });
    this.projSel = h('select', { class: 'inp tk-sel', title: 'Project of the new task' });
    this.prioSel = h('select', { class: 'inp tk-sel', title: 'Priority of the new task' },
      h('option', { value: '2' }, 'Normal'), h('option', { value: '1' }, 'High'), h('option', { value: '3' }, 'Low'));
    this.list = h('div', { class: 'tk-list' });
    this.doneHead = h('button', { class: 'tk-done-head', onclick: function () { self.toggleDone(); } });
    this.clearBtn = h('button', { class: 'btn sm', title: 'Remove the done tasks shown here', onclick: function () { self.clearDone(); } }, 'Clear');
    this.doneList = h('div', { class: 'tk-list done' });
    this.doneBox = h('div', { class: 'tk-donebox hidden' }, h('div', { class: 'tk-done-bar' }, this.doneHead, this.clearBtn), this.doneList);
    this.note = h('div', { class: 'muted files-note' }, 'Loading…');
    this.inner = h('div', { class: 'tk-inner' },
      h('div', { class: 'files-bar tk-bar' }, this.filterSel, h('span', { class: 'grow' }), this.countEl,
        h('button', { class: 'icon-btn', title: 'Refresh', onclick: function () { self.load(); } }, '⟳')),
      h('div', { class: 'tk-add' }, this.input, this.projSel, this.prioSel,
        h('button', { class: 'btn primary sm', title: 'Add (Enter)', onclick: function () { self.add(); } }, 'Add')),
      this.note, this.list, this.doneBox);
    this.body = h('div', { class: 'pbody tk-body' }, this.inner);
    this.pane = h('div', { class: 'pane files tasks hidden-pane' }, this.body);
    this.timer = setInterval(function () {
      if (!self.closed && !document.hidden && !self.pane.classList.contains('hidden-pane') && !self.editing && !self.dragId) self.load(true);
    }, POLL);
    this.render();
    this.load();
  }

  var P = TasksTab.prototype;

  // what app.js expects from every tab
  P.focus = function () { if (!TOUCH) this.input.focus(); };
  P.ensureOpen = function () { if (!this.editing) this.load(true); };
  P.doFit = function () {};
  P.toggleFa = function () {};
  P.renderFa = function () {};
  P.toggleComposer = function () { this.input.focus(); };
  P.connect = function () {};
  P.stop = function () {};
  P.restart = function () { this.load(); };
  P.applyInfo = function () { this.ttlEl.textContent = this.info.title; };
  P.dispose = function () {
    this.closed = true;
    clearInterval(this.timer);
    this.el.remove();
    this.pane.remove();
  };
  P.menu = function (x, y, extra) {
    var self = this;
    SW.showMenu(x, y, [
      { label: 'Refresh', action: function () { self.load(); } },
      { label: 'Sort by priority', disabled: !this.visible(false).length, action: function () { self.sortByPriority(); } },
      { label: (this.showDone ? 'Hide' : 'Show') + ' done tasks', action: function () { self.toggleDone(); } },
      { label: 'Rename…', action: function () { SW.renameTab(self); } },
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { SW.closeTab(self); } }
    ].concat(extra || SW.layoutItemsFor(self)));
  };

  // ---------------------------------------------------------------- data
  P.apply = function (r) {
    if (!r || (r.rev < this.rev && this.tasks)) return;          // an older answer than what we have
    var same = r.rev === this.rev && this.tasks && !this.dirty;
    this.rev = r.rev;
    this.tasks = r.tasks || [];
    this.dirty = false;
    cache.tasks = this.tasks.slice();
    setRail();
    if (!same) this.render();
  };

  P.load = function (quiet) {
    var self = this;
    return api('GET', 'tasks').then(function (r) {
      if (self.pendingOps) return;                 // a change is on its way: its answer is newer
      self.error = '';
      self.apply(r);
    }).catch(function (e) {
      if (e.offline) return;
      if (!quiet || !self.tasks) { self.error = e.message; self.render(); }
    });
  };

  // One change: shown at once, then the server's list (with everyone's changes) replaces ours.
  // Changes go one after another, so they reach the server in the order they were made.
  P.send = function (op, local) {
    var self = this;
    if (local && this.tasks) { local(this.tasks); this.dirty = true; this.render(); }
    this.pendingOps = (this.pendingOps || 0) + 1;
    this.chain = (this.chain || Promise.resolve()).then(function () {
      return api('POST', 'tasks', op).then(function (r) { self.pendingOps--; self.apply(r); });
    }).catch(function (e) { self.pendingOps--; fail(e); self.rev = -1; return self.load(true); });
    return this.chain;
  };

  P.find = function (id) { return (this.tasks || []).find(function (t) { return t.id === id; }); };

  P.matches = function (t) {
    var f = this.filterProj;
    return !f || (f === '-' ? !t.project : t.project === f);
  };
  // the tasks shown: open ones in their order, done ones newest first
  P.visible = function (done) {
    var self = this;
    var out = (this.tasks || []).filter(function (t) { return !!t.done === done && self.matches(t); });
    if (done) out.sort(function (a, b) { return (b.doneAt || 0) - (a.doneAt || 0); });
    return out;
  };

  // ---------------------------------------------------------------- actions
  P.add = function () {
    var text = this.input.value.trim();
    if (!text) { this.input.focus(); return; }
    var op = { op: 'add', text: text, project: this.projSel.value, prio: +this.prioSel.value || 2 };
    this.input.value = '';
    this.prioSel.value = '2';
    localStorage.setItem('sw-tasks-proj', op.project);
    this.send(op, function (all) { all.push({ id: 'new' + Date.now(), text: text, project: op.project, prio: op.prio, done: false, pending: true }); });
    if (!TOUCH) this.input.focus();
  };

  P.update = function (t, fields) {
    if (t.pending) return;
    this.send(Object.assign({ op: 'update', id: t.id }, fields), function () {
      Object.assign(t, fields);
      if ('done' in fields) t.doneAt = fields.done ? Date.now() / 1000 : 0;
    });
  };

  P.remove = function (t) {
    if (t.pending) return;
    this.send({ op: 'delete', id: t.id }, function (all) { all.splice(all.indexOf(t), 1); });
  };

  // put t just before (or after) another task of the whole list
  P.moveTo = function (t, ref, after) {
    if (t.pending || !ref || ref === t || ref.pending) return;
    var op = { op: 'move', id: t.id };
    op[after ? 'after' : 'before'] = ref.id;
    this.send(op, function (all) {
      all.splice(all.indexOf(t), 1);
      all.splice(all.indexOf(ref) + (after ? 1 : 0), 0, t);
    });
  };
  // up / down among the tasks shown right now (a filter may hide the ones between)
  P.step = function (t, d) {
    var vis = this.visible(false), i = vis.indexOf(t);
    if (i < 0 || !vis[i + d]) return;
    this.moveTo(t, vis[i + d], d > 0);
  };

  P.sortByPriority = function () {
    var vis = this.visible(false).filter(function (t) { return !t.pending; });
    var sorted = vis.slice().sort(function (a, b) { return (a.prio || 2) - (b.prio || 2) || vis.indexOf(a) - vis.indexOf(b); });
    if (sorted.every(function (t, i) { return t === vis[i]; })) return;
    this.send({ op: 'order', ids: sorted.map(function (t) { return t.id; }) }, function (all) {
      var slots = vis.map(function (t) { return all.indexOf(t); }).sort(function (a, b) { return a - b; });
      slots.forEach(function (at, i) { all[at] = sorted[i]; });
    });
  };

  P.clearDone = function () {
    var self = this, n = this.visible(true).length;
    if (!n) return;
    SW.confirmDlg('Remove ' + n + ' done task' + (n === 1 ? '' : 's') + '?', 'Remove', true).then(function (ok) {
      if (!ok) return;
      var op = { op: 'clear' };
      if (self.filterProj) op.project = self.filterProj === '-' ? '' : self.filterProj;
      self.send(op, function (all) {
        for (var i = all.length - 1; i >= 0; i--) if (all[i].done && self.matches(all[i])) all.splice(i, 1);
      });
    });
  };

  P.setFilter = function (f) {
    this.filterProj = f || '';
    localStorage.setItem('sw-tasks-filter', this.filterProj);
    this.render();
  };

  P.toggleDone = function () {
    this.showDone = !this.showDone;
    localStorage.setItem('sw-tasks-done', this.showDone ? '1' : '0');
    this.render();
  };

  // a new Claude chat in the task's project, the task already written in its message box
  P.askClaude = function (t) {
    var p = t.project && SW.project(t.project) ? t.project : undefined;
    return SW.openSession({ kind: 'chat', project: p }).then(function (tab) {
      if (!tab || !tab.ta) return tab;
      tab.ta.value = t.text;
      if (tab.autosize) tab.autosize();
      if (!TOUCH) tab.ta.focus();
      return tab;
    });
  };

  P.edit = function (t, row) {
    var self = this;
    if (t.pending) return;
    this.editing = t.id;
    var done = false;
    var inp = h('input', { class: 'inp tk-edit', value: t.text, dir: 'auto', spellcheck: 'false' });
    function finish(save) {
      if (done) return;
      done = true;
      self.editing = null;
      var v = inp.value.trim();
      if (save && v && v !== t.text) self.update(t, { text: v });
      else self.render();
    }
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    inp.addEventListener('blur', function () { finish(true); });
    var txt = row.querySelector('.tk-text');
    if (txt) txt.replaceWith(inp);
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
  };

  P.projectMenu = function (t, x, y) {
    var self = this;
    var names = projectNames(this.tasks);
    SW.showMenu(x, y, [{ header: 'Project' },
      { label: 'No project', k: !t.project ? '✓' : '', action: function () { self.update(t, { project: '' }); } }].concat(
      names.map(function (n) { return { label: n, k: t.project === n ? '✓' : '', action: function () { self.update(t, { project: n }); } }; })));
  };

  P.rowMenu = function (t, row, x, y) {
    var self = this;
    var vis = this.visible(false), i = vis.indexOf(t);
    SW.showMenu(x, y, [
      { label: 'Edit', action: function () { self.edit(t, row); } },
      { label: t.done ? 'Not done' : 'Done', action: function () { self.update(t, { done: !t.done }); } },
      { label: 'Ask Claude', action: function () { self.askClaude(t).catch(fail); } },
      { header: 'Priority' },
      { label: 'High', k: t.prio === 1 ? '✓' : '', action: function () { self.update(t, { prio: 1 }); } },
      { label: 'Normal', k: (t.prio || 2) === 2 ? '✓' : '', action: function () { self.update(t, { prio: 2 }); } },
      { label: 'Low', k: t.prio === 3 ? '✓' : '', action: function () { self.update(t, { prio: 3 }); } },
      '-',
      { label: 'Project…', k: t.project || '', action: function () { self.projectMenu(t, x, y); } },
      t.done ? null : { label: 'Move up', disabled: i <= 0, action: function () { self.step(t, -1); } },
      t.done ? null : { label: 'Move down', disabled: i < 0 || i >= vis.length - 1, action: function () { self.step(t, 1); } },
      t.done ? null : { label: 'Move to top', disabled: i <= 0, action: function () { self.moveTo(t, vis[0], false); } },
      '-',
      { label: 'Delete', action: function () { self.remove(t); } }
    ]);
  };

  // ---------------------------------------------------------------- drawing
  function projectNames(tasks) {
    var names = ((SW.S && SW.S.projects) || []).map(function (p) { return p.name; });
    (tasks || []).forEach(function (t) { if (t.project && names.indexOf(t.project) < 0) names.push(t.project); });
    return names;
  }
  function projectTitle(name) {
    var p = SW.project(name);
    return (p && p.title) || name;
  }

  P.renderSelects = function () {
    var self = this, names = projectNames(this.tasks);
    var count = function (n) { var c = (self.tasks || []).filter(function (t) { return !t.done && (n === '-' ? !t.project : t.project === n); }).length; return c ? ' · ' + c : ''; };
    var f = this.filterProj;
    if (f && f !== '-' && names.indexOf(f) < 0) names.push(f);
    this.filterSel.replaceChildren.apply(this.filterSel, [h('option', { value: '' }, 'All projects'), h('option', { value: '-' }, 'No project' + count('-'))]
      .concat(names.map(function (n) { return h('option', { value: n }, projectTitle(n) + count(n)); })));
    this.filterSel.value = f;
    // the new task's project: the one shown, else what was picked last
    var cur = f === '-' ? '' : f || (this.projInit ? this.projSel.value : localStorage.getItem('sw-tasks-proj') || '');
    this.projInit = true;
    this.projSel.replaceChildren.apply(this.projSel, [h('option', { value: '' }, 'No project')]
      .concat(names.map(function (n) { return h('option', { value: n }, projectTitle(n)); })));
    this.projSel.value = names.indexOf(cur) >= 0 ? cur : '';
  };

  P.row = function (t) {
    var self = this;
    var showProj = !this.filterProj && t.project;
    var c = showProj && SW.projectColor ? SW.projectColor(t.project) : '';
    var row = h('div', { class: 'tk-row prio' + (t.prio || 2) + (t.done ? ' done' : '') + (t.pending ? ' pending' : ''), 'data-id': t.id },
      h('button', { class: 'tk-check', title: t.done ? 'Mark as not done' : 'Mark as done',
        onclick: function (e) { e.stopPropagation(); self.update(t, { done: !t.done }); } }, t.done ? '✓' : ''),
      t.prio === 1 ? h('span', { class: 'tk-prio', title: 'High priority' }, '!') : null,
      h('span', { class: 'tk-text', dir: 'auto', title: TOUCH ? null : 'Click to edit',
        onclick: function () { if (!TOUCH) self.edit(t, row); } }, t.text),
      showProj ? h('button', { class: 'tk-proj mono', style: c ? 'color:' + c : null, title: 'Show only ' + t.project,
        onclick: function (e) { e.stopPropagation(); self.setFilter(t.project); } }, projectTitle(t.project)) : null,
      h('button', { class: 'icon-btn tk-more', title: 'More', onclick: function (e) {
        e.stopPropagation(); var r = e.currentTarget.getBoundingClientRect(); self.rowMenu(t, row, r.left, r.bottom); } }, '⋯'));
    row.dataset.id = t.id;
    row.addEventListener('contextmenu', function (e) { e.preventDefault(); self.rowMenu(t, row, e.clientX, e.clientY); });
    if (!t.done && !TOUCH && !t.pending) this.dragRow(t, row);
    return row;
  };

  P.dragRow = function (t, row) {
    var self = this;
    row.setAttribute('draggable', 'true');
    row.addEventListener('dragstart', function (e) {
      if (self.editing) { e.preventDefault(); return; }
      self.dragId = t.id;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('application/x-sw-task', t.id);
    });
    row.addEventListener('dragend', function () {
      self.dragId = null;
      row.classList.remove('dragging');
      self.list.querySelectorAll('.tk-row').forEach(function (r) { r.classList.remove('drop-before', 'drop-after'); });
    });
    row.addEventListener('dragover', function (e) {
      if (!self.dragId || self.dragId === t.id) return;
      e.preventDefault();
      var r = row.getBoundingClientRect();
      var after = e.clientY > r.top + r.height / 2;
      row.classList.toggle('drop-after', after);
      row.classList.toggle('drop-before', !after);
    });
    row.addEventListener('dragleave', function () { row.classList.remove('drop-before', 'drop-after'); });
    row.addEventListener('drop', function (e) {
      e.preventDefault();
      var after = row.classList.contains('drop-after');
      row.classList.remove('drop-before', 'drop-after');
      var moving = self.find(self.dragId);
      self.dragId = null;
      if (moving) self.moveTo(moving, t, after);
    });
  };

  P.render = function () {
    if (this.closed) return;
    this.renderSelects();
    if (!this.tasks) {
      this.note.textContent = this.error ? 'Could not read the tasks: ' + this.error : 'Loading…';
      this.note.classList.remove('hidden');
      this.list.replaceChildren();
      this.doneBox.classList.add('hidden');
      return;
    }
    var open = this.visible(false), done = this.visible(true);
    this.countEl.textContent = open.length ? open.length + ' open' : '';
    this.note.textContent = this.error ? 'Could not read the tasks: ' + this.error : 'Nothing to do' + (this.filterProj ? ' here' : '') + '.';
    this.note.classList.toggle('hidden', !!open.length && !this.error);
    this.list.replaceChildren.apply(this.list, open.map(this.row, this));
    this.doneBox.classList.toggle('hidden', !done.length);
    this.doneHead.textContent = (this.showDone ? '▾ ' : '▸ ') + 'Done · ' + done.length;
    this.clearBtn.classList.toggle('hidden', !this.showDone);
    this.doneList.replaceChildren.apply(this.doneList, this.showDone ? done.map(this.row, this) : []);
  };

  // open the Tasks tab (one per window), maybe showing one project only
  function openTasks(project) {
    return SW.openSession({ kind: 'tasks', title: 'Tasks' }).then(function (tab) {
      if (tab && tab.isTasks && project !== undefined) tab.setFilter(project);
      return tab;
    });
  }

  window.SWTasks = { TasksTab: TasksTab, open: openTasks, openCount: openCount, cache: cache };
})();
