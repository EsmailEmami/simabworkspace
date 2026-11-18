/* Simab Workspace — Tasks tab: the user's own task board. It is kept on the server
   (~/.config/simabworkspace/tasks.json), so the installed app and the web version show the same board.
   Four columns (Backlog, To do, In progress, Done); drag cards between and inside them (mouse,
   or press and hold on touch); + opens a dialog to add, a click on a card opens it to edit. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail;
  var TOUCH = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  var POLL = 15000;             // another device may have changed the board
  var HOLD = 380;               // touch: press this long to pick a card up
  var COLS = [
    { id: 'backlog', name: 'Backlog' },
    { id: 'todo', name: 'To do' },
    { id: 'doing', name: 'In progress' },
    { id: 'done', name: 'Done' }];
  var PRIO = { 1: 'High', 2: 'Normal', 3: 'Low' };
  var cache = { tasks: null };  // the last list seen (for counts in menus)

  function statusOf(t) { return t.status || (t.done ? 'done' : 'todo'); }
  function up(el, cls) { for (; el && el.classList; el = el.parentNode) if (el.classList.contains(cls)) return el; return null; }
  function isControl(el, stop) { for (; el && el !== stop; el = el.parentNode) if (el.tagName === 'BUTTON' || el.tagName === 'SELECT') return true; return false; }

  function openCount(project) {
    return (cache.tasks || []).filter(function (t) { return statusOf(t) !== 'done' && (project === undefined || (t.project || '') === project); }).length;
  }
  function setRail() {
    var btn = document.querySelector('#btn-tasks'), b = btn && btn.querySelector('.rcount');
    if (!b) return;
    var n = openCount();
    b.textContent = cache.tasks && n ? String(n) : '';
  }
  function projectNames(tasks) {
    var names = ((SW.S && SW.S.projects) || []).map(function (p) { return p.name; });
    (tasks || []).forEach(function (t) { if (t.project && names.indexOf(t.project) < 0) names.push(t.project); });
    return names;
  }
  function projectTitle(name) {
    var p = SW.project(name);
    return (p && p.title) || name;
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
    this.drag = null;             // { t, card, ghost, target }
    this.dialog = null;
    this.filterProj = localStorage.getItem('sw-tasks-filter') || '';     // '' all, '-' no project, else a project
    SW.tabEl(this);

    this.filterSel = h('select', { class: 'tk-filter', title: 'Show the tasks of', onchange: function () { self.setFilter(self.filterSel.value); } });
    this.countEl = h('span', { class: 'tk-count' });
    this.note = h('div', { class: 'tk-note hidden' });
    this.cols = {};
    this.board = h('div', { class: 'tk-board' }, COLS.map(function (c) {
      var col = { count: h('span', { class: 'tk-ccount' }), list: h('div', { class: 'tk-cards' }) };
      col.clear = c.id === 'done' ? h('button', { class: 'tk-hbtn hidden', title: 'Remove the done tasks shown here', onclick: function () { self.clearDone(); } }, 'Clear') : null;
      col.el = h('section', { class: 'tk-col ' + c.id, 'data-status': c.id },
        h('header', { class: 'tk-chead' }, h('i', { class: 'tk-dot' }), h('span', { class: 'tk-cname' }, c.name), col.count,
          h('span', { class: 'grow' }), col.clear,
          c.id === 'done' ? null : h('button', { class: 'tk-hbtn tk-cadd', title: 'Add to ' + c.name, onclick: function () { self.openDialog(null, c.id); } }, '+')),
        col.list);
      col.el.dataset.status = c.id;
      self.cols[c.id] = col;
      return col.el;
    }));
    this.body = h('div', { class: 'pbody tk-body' },
      h('div', { class: 'tk-top' }, this.filterSel, this.countEl, h('span', { class: 'grow' }),
        h('button', { class: 'tk-new', title: 'New task', onclick: function () { self.openDialog(null, 'todo'); } }, h('b', null, '+'), h('span', null, 'New task'))),
      this.note, this.board);
    this.pane = h('div', { class: 'pane files tasks hidden-pane' }, this.body);
    this.timer = setInterval(function () {
      if (!self.closed && !document.hidden && !self.pane.classList.contains('hidden-pane') && !self.drag && !self.dialog) self.load(true);
    }, POLL);
    this.render();
    this.load();
  }

  var P = TasksTab.prototype;

  // what app.js expects from every tab
  P.focus = function () {};
  P.ensureOpen = function () { if (!this.drag) this.load(true); };
  P.doFit = function () {};
  P.toggleFa = function () {};
  P.renderFa = function () {};
  P.toggleComposer = function () { this.openDialog(null, 'todo'); };
  P.connect = function () {};
  P.stop = function () {};
  P.restart = function () { this.load(); };
  P.applyInfo = function () { this.ttlEl.textContent = this.info.title; };
  P.dispose = function () {
    this.closed = true;
    clearInterval(this.timer);
    if (this.drag) this.dragCancel();
    this.el.remove();
    this.pane.remove();
  };
  P.menu = function (x, y, extra) {
    var self = this;
    SW.showMenu(x, y, [
      { label: 'New task…', action: function () { self.openDialog(null, 'todo'); } },
      { label: 'Refresh', action: function () { self.load(); } },
      { label: 'Sort by priority', disabled: !this.visible().length, action: function () { self.sortByPriority(); } },
      { label: 'Clear done', disabled: !this.visible('done').length, action: function () { self.clearDone(); } },
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
    if (!same && !this.drag) this.render();
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
  // the cards shown: of one column (in their order), or all
  P.visible = function (status) {
    var self = this;
    return (this.tasks || []).filter(function (t) { return self.matches(t) && (!status || statusOf(t) === status); });
  };

  // ---------------------------------------------------------------- actions
  P.add = function (fields) {
    var t = { id: 'new' + Date.now(), text: fields.text, project: fields.project || '', prio: fields.prio || 2,
      status: fields.status || 'todo', done: fields.status === 'done', pending: true };
    localStorage.setItem('sw-tasks-proj', t.project);
    this.send({ op: 'add', text: t.text, project: t.project, prio: t.prio, status: t.status }, function (all) { all.push(t); });
  };

  P.update = function (t, fields) {
    if (t.pending) return;
    this.send(Object.assign({ op: 'update', id: t.id }, fields), function () {
      Object.assign(t, fields);
      if (fields.status) { t.done = fields.status === 'done'; t.doneAt = t.done ? Date.now() / 1000 : 0; }
    });
  };

  P.remove = function (t) {
    if (t.pending) return;
    this.send({ op: 'delete', id: t.id }, function (all) { all.splice(all.indexOf(t), 1); });
  };

  // into a column (status), just before / after another card, or at the column's end (ref null)
  P.moveTo = function (t, status, ref, after) {
    if (t.pending || ref === t || (ref && ref.pending)) return;
    status = status || statusOf(t);
    if (!ref && status === statusOf(t)) {               // to the end of its own column
      var col = this.visible(status);
      if (col[col.length - 1] === t) { this.render(); return; }
    }
    var op = { op: 'move', id: t.id };
    if (status !== statusOf(t)) op.status = status;
    if (ref) op[after ? 'after' : 'before'] = ref.id;
    this.send(op, function (all) {
      if (op.status) { t.status = status; t.done = status === 'done'; t.doneAt = t.done ? Date.now() / 1000 : 0; }
      all.splice(all.indexOf(t), 1);
      if (ref) all.splice(all.indexOf(ref) + (after ? 1 : 0), 0, t);
      else all.push(t);
    });
  };
  // to another column from the menu or the dialog: on top there
  P.setStatus = function (t, status) {
    if (status === statusOf(t)) return;
    this.moveTo(t, status, this.visible(status)[0] || null, false);
  };
  // up / down inside its column among the cards shown (a filter may hide the ones between)
  P.step = function (t, d) {
    var col = this.visible(statusOf(t)), i = col.indexOf(t);
    if (i < 0 || !col[i + d]) return;
    this.moveTo(t, null, col[i + d], d > 0);
  };

  P.sortByPriority = function () {
    var vis = this.visible().filter(function (t) { return !t.pending; });
    var sorted = vis.slice().sort(function (a, b) { return (a.prio || 2) - (b.prio || 2) || vis.indexOf(a) - vis.indexOf(b); });
    if (sorted.every(function (t, i) { return t === vis[i]; })) return;
    this.send({ op: 'order', ids: sorted.map(function (t) { return t.id; }) }, function (all) {
      var slots = vis.map(function (t) { return all.indexOf(t); }).sort(function (a, b) { return a - b; });
      slots.forEach(function (at, i) { all[at] = sorted[i]; });
    });
  };

  P.clearDone = function () {
    var self = this, n = this.visible('done').length;
    if (!n) return;
    SW.confirmDlg('Remove ' + n + ' done task' + (n === 1 ? '' : 's') + '?', 'Remove', true).then(function (ok) {
      if (!ok) return;
      var op = { op: 'clear' };
      if (self.filterProj) op.project = self.filterProj === '-' ? '' : self.filterProj;
      self.send(op, function (all) {
        for (var i = all.length - 1; i >= 0; i--) if (statusOf(all[i]) === 'done' && self.matches(all[i])) all.splice(i, 1);
      });
    });
  };

  P.setFilter = function (f) {
    this.filterProj = f || '';
    localStorage.setItem('sw-tasks-filter', this.filterProj);
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

  // ---------------------------------------------------------------- the dialog (new / edit)
  P.openDialog = function (t, status) {
    var self = this;
    if (t && t.pending) return null;
    var f = this.filterProj;
    var proj = t ? t.project || '' : f === '-' ? '' : f || localStorage.getItem('sw-tasks-proj') || '';
    var names = projectNames(this.tasks);
    if (proj && names.indexOf(proj) < 0) proj = '';
    var text = h('textarea', { class: 'inp tk-dtext', rows: 3, dir: 'auto', spellcheck: 'false', placeholder: 'What needs doing?', autofocus: true });
    text.value = t ? t.text : '';
    var stSel = h('select', { class: 'inp' }, COLS.map(function (c) { return h('option', { value: c.id }, c.name); }));
    stSel.value = t ? statusOf(t) : status || 'todo';
    var pjSel = h('select', { class: 'inp' }, h('option', { value: '' }, 'No project'),
      names.map(function (n) { return h('option', { value: n }, projectTitle(n)); }));
    pjSel.value = proj;
    var prSel = h('select', { class: 'inp' }, [1, 2, 3].map(function (p) { return h('option', { value: String(p) }, PRIO[p]); }));
    prSel.value = String(t ? t.prio || 2 : 2);
    var err = h('div', { class: 'add-err hidden' });
    var md;
    function save() {
      var v = text.value.trim();
      if (!v) { err.textContent = 'Write the task first.'; err.classList.remove('hidden'); text.focus(); return; }
      var fields = { text: v, project: pjSel.value, prio: +prSel.value || 2, status: stSel.value };
      md.close();
      if (!t) { self.add(fields); return; }
      var ch = {};
      ['text', 'project', 'prio'].forEach(function (k) { if (fields[k] !== (t[k] || (k === 'prio' ? 2 : ''))) ch[k] = fields[k]; });
      if (Object.keys(ch).length) self.update(t, ch);
      if (fields.status !== statusOf(t)) self.setStatus(t, fields.status);
    }
    text.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !TOUCH) { e.preventDefault(); save(); }
    });
    var field = function (label, el) { return h('label', { class: 'tk-field' }, h('span', null, label), el); };
    md = SW.openModal({ title: t ? 'Task' : 'New task', size: 'sm tk-dialog',
      body: [text, h('div', { class: 'tk-fields' }, field('Status', stSel), field('Project', pjSel), field('Priority', prSel)), err],
      foot: [
        t ? h('button', { class: 'btn danger left', onclick: function () { md.close(); self.remove(t); } }, 'Delete') : null,
        t ? h('button', { class: 'btn', title: 'A new Claude chat with this task in its message box', onclick: function () { md.close(); self.askClaude(t).catch(fail); } }, 'Ask Claude') : null,
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: save }, t ? 'Save' : 'Add task')],
      onclose: function () { self.dialog = null; } });
    this.dialog = { close: md.close, save: save, text: text, status: stSel, project: pjSel, prio: prSel };
    return this.dialog;
  };

  P.cardMenu = function (t, x, y) {
    var self = this, st = statusOf(t);
    var col = this.visible(st), i = col.indexOf(t);
    SW.showMenu(x, y, [
      { label: 'Edit…', action: function () { self.openDialog(t); } },
      { label: 'Ask Claude', action: function () { self.askClaude(t).catch(fail); } },
      { header: 'Move to' }].concat(
      COLS.filter(function (c) { return c.id !== st; }).map(function (c) { return { label: c.name, action: function () { self.setStatus(t, c.id); } }; }),
      [{ header: 'Priority' }],
      [1, 2, 3].map(function (p) { return { label: PRIO[p], k: (t.prio || 2) === p ? '✓' : '', action: function () { self.update(t, { prio: p }); } }; }),
      ['-',
        { label: 'Move up', disabled: i <= 0, action: function () { self.step(t, -1); } },
        { label: 'Move down', disabled: i < 0 || i >= col.length - 1, action: function () { self.step(t, 1); } },
        '-',
        { label: 'Delete', action: function () { self.remove(t); } }]));
  };

  // ---------------------------------------------------------------- dragging cards
  // Mouse: press and move. Touch: press and hold, then move (a quick swipe still scrolls).
  // The card stays in place (faded); a small copy follows the pointer; the place it would go is marked.
  P.dragStart = function (t, card, x, y) {
    if (t.pending || this.drag) return;
    var ghost = h('div', { class: 'tk-ghost', dir: 'auto' }, t.text);
    document.body.append(ghost);
    card.classList.add('dragging');
    this.pane.classList.add('tk-dragging');
    this.drag = { t: t, card: card, ghost: ghost, target: null };
    this.dragMove(x, y);
  };

  P.dragMove = function (x, y) {
    var d = this.drag;
    if (!d) return;
    d.ghost.style.left = x + 12 + 'px';
    d.ghost.style.top = y + 8 + 'px';
    this.clearMarks();
    var el = document.elementFromPoint ? document.elementFromPoint(x, y) : null;
    var col = up(el, 'tk-col'), card = up(el, 'tk-card');
    d.target = null;
    if (!col || !this.board.contains(col)) return;
    var status = col.dataset.status;
    if (card && card !== d.card && card.dataset.id) {
      var r = card.getBoundingClientRect();
      var after = y > r.top + r.height / 2;
      card.classList.add(after ? 'drop-after' : 'drop-before');
      d.target = { status: status, ref: this.find(card.dataset.id), after: after };
    } else if (!card) {
      col.classList.add('drop');
      d.target = { status: status, ref: null };
    } else d.target = { status: status, same: true };
    // near an edge: scroll the board (phones: one column at a time) or the column
    var br = this.board.getBoundingClientRect();
    if (x < br.left + 36) this.board.scrollLeft -= 16;
    else if (x > br.right - 36) this.board.scrollLeft += 16;
    var list = this.cols[status].list, lr = list.getBoundingClientRect();
    if (y < lr.top + 30) list.scrollTop -= 12;
    else if (y > lr.bottom - 30) list.scrollTop += 12;
  };

  P.clearMarks = function () {
    var self = this;
    COLS.forEach(function (c) {
      self.cols[c.id].el.classList.remove('drop');
      self.cols[c.id].list.querySelectorAll('.tk-card').forEach(function (x) { x.classList.remove('drop-before', 'drop-after'); });
    });
  };

  P.dragCancel = function () {
    var d = this.drag;
    if (!d) return null;
    this.drag = null;
    this.clearMarks();
    d.ghost.remove();
    d.card.classList.remove('dragging');
    this.pane.classList.remove('tk-dragging');
    this.justDragged = Date.now();
    return d;
  };

  P.dragEnd = function () {
    var d = this.dragCancel();
    if (!d) return;
    var g = d.target;
    if (g && !g.same) this.moveTo(d.t, g.status, g.ref, g.after);
    else this.render();
  };

  P.wireDrag = function (t, card) {
    var self = this;
    // mouse
    card.addEventListener('pointerdown', function (e) {
      if (e.pointerType !== 'mouse' || e.button !== 0 || isControl(e.target, card) || t.pending) return;
      var x0 = e.clientX, y0 = e.clientY;
      function move(ev) {
        if (!self.drag && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) > 5) self.dragStart(t, card, ev.clientX, ev.clientY);
        if (self.drag) { ev.preventDefault(); self.dragMove(ev.clientX, ev.clientY); }
      }
      function stop(ev) {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', stop);
        document.removeEventListener('pointercancel', stop);
        if (self.drag) { if (ev.type === 'pointercancel') { self.dragCancel(); self.render(); } else self.dragEnd(); }
      }
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', stop);
      document.addEventListener('pointercancel', stop);
    });
    // touch: hold, then drag
    var timer = null, start = null;
    card.addEventListener('touchstart', function (e) {
      if (e.touches.length !== 1 || isControl(e.target, card) || t.pending) return;
      var p = e.touches[0];
      start = { x: p.clientX, y: p.clientY };
      clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        if (navigator.vibrate) navigator.vibrate(12);
        self.dragStart(t, card, start.x, start.y);
      }, HOLD);
    }, { passive: true });
    card.addEventListener('touchmove', function (e) {
      var p = e.touches[0];
      if (!p) return;
      if (self.drag && self.drag.card === card) { e.preventDefault(); self.dragMove(p.clientX, p.clientY); return; }
      if (timer && start && Math.abs(p.clientX - start.x) + Math.abs(p.clientY - start.y) > 8) { clearTimeout(timer); timer = null; }
    }, { passive: false });
    function end(e) {
      clearTimeout(timer); timer = null;
      if (self.drag && self.drag.card === card) {
        if (e.cancelable) e.preventDefault();
        if (e.type === 'touchcancel') { self.dragCancel(); self.render(); } else self.dragEnd();
      }
    }
    card.addEventListener('touchend', end);
    card.addEventListener('touchcancel', end);
  };

  // ---------------------------------------------------------------- drawing
  P.card = function (t) {
    var self = this;
    var c = t.project && SW.projectColor ? SW.projectColor(t.project) : '';
    var prio = t.prio || 2;
    var card = h('div', { class: 'tk-card prio' + prio + (t.pending ? ' pending' : ''), 'data-id': t.id, tabindex: '0' },
      h('div', { class: 'tk-ctext', dir: 'auto' }, t.text),
      h('div', { class: 'tk-cfoot' },
        prio !== 2 ? h('span', { class: 'tk-pill p' + prio }, PRIO[prio]) : null,
        t.project && !(this.filterProj && this.filterProj !== '-') ? h('span', { class: 'tk-cproj', title: t.project },
          h('i', { style: c ? 'background:' + c : null }), projectTitle(t.project)) : null,
        h('span', { class: 'grow' }),
        h('button', { class: 'tk-cmore', title: 'More', onclick: function (e) {
          e.stopPropagation(); var r = e.currentTarget.getBoundingClientRect(); self.cardMenu(t, r.left, r.bottom); } }, '⋯')));
    card.dataset.id = t.id;
    card.addEventListener('click', function (e) {
      if (self.justDragged && Date.now() - self.justDragged < 300) return;
      if (isControl(e.target, card)) return;
      self.openDialog(t);
    });
    card.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target === card) self.openDialog(t); });
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      if (!TOUCH) self.cardMenu(t, e.clientX, e.clientY);      // touch: a long press picks the card up instead
    });
    this.wireDrag(t, card);
    return card;
  };

  P.renderFilter = function () {
    var self = this, names = projectNames(this.tasks), f = this.filterProj;
    var count = function (n) { var c = (self.tasks || []).filter(function (t) { return statusOf(t) !== 'done' && (n === '-' ? !t.project : t.project === n); }).length; return c ? ' · ' + c : ''; };
    if (f && f !== '-' && names.indexOf(f) < 0) names.push(f);
    this.filterSel.replaceChildren.apply(this.filterSel, [h('option', { value: '' }, 'All projects'), h('option', { value: '-' }, 'No project' + count('-'))]
      .concat(names.map(function (n) { return h('option', { value: n }, projectTitle(n) + count(n)); })));
    this.filterSel.value = f;
  };

  P.render = function () {
    var self = this;
    if (this.closed) return;
    this.renderFilter();
    this.note.textContent = this.error ? 'Could not read the tasks: ' + this.error : '';
    this.note.classList.toggle('hidden', !this.error);
    var open = this.visible().filter(function (t) { return statusOf(t) !== 'done'; }).length;
    this.countEl.textContent = this.tasks ? open + ' open' : 'Loading…';
    COLS.forEach(function (c) {
      var col = self.cols[c.id], list = self.visible(c.id);
      col.count.textContent = list.length ? String(list.length) : '';
      if (col.clear) col.clear.classList.toggle('hidden', !list.length);
      col.list.replaceChildren.apply(col.list, list.length ? list.map(self.card, self)
        : [h('div', { class: 'tk-empty' }, self.tasks ? 'Drop tasks here' : '')]);
    });
  };

  // open the Tasks tab (one per window), maybe showing one project only
  function openTasks(project) {
    return SW.openSession({ kind: 'tasks', title: 'Tasks' }).then(function (tab) {
      if (tab && tab.isTasks && project !== undefined) tab.setFilter(project);
      return tab;
    });
  }

  window.SWTasks = { TasksTab: TasksTab, open: openTasks, openCount: openCount, cache: cache, COLS: COLS };
})();
