/* Simab Workspace — tabs, terminals, split sections, projects. Plain JS, no build step. */
'use strict';
(function () {

  // ------------------------------------------------------------------ helpers
  var $ = function (s, el) { return (el || document).querySelector(s); };

  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'style') el.style.cssText = v;
        else if (k === 'value') el.value = v;
        else if (k.indexOf('on') === 0 && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (typeof v === 'boolean' && k in el) el[k] = v;
        else el.setAttribute(k, v === true ? '' : v);
      });
    }
    var kids = Array.prototype.slice.call(arguments, 2);
    (function add(list) {
      list.forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        if (Array.isArray(c)) add(c);
        else el.append(c instanceof Node ? c : String(c));
      });
    })(kids);
    return el;
  }

  var FA_RE = /[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]/;
  function faNum(n) { return String(n); }
  function debounce(fn, ms) { var t; return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); }; }
  function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }
  function fmtBytes(b) { var u = ['B', 'KB', 'MB', 'GB'], i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++; } return (i ? b.toFixed(1) : b) + ' ' + u[i]; }

  // ------------------------------------------------------------------ token + api
  var token = '';
  var tm = location.hash.match(/t=([A-Za-z0-9_\-]+)/);
  if (tm) {
    token = tm[1];
    sessionStorage.setItem('sw-token', token);
    history.replaceState(null, '', location.pathname);
  } else {
    token = sessionStorage.getItem('sw-token') || '';
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { 'X-Token': token } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch('/api/' + path, opts).catch(function () {
      // the app's background service is not reachable (stopped, crashed, WSL shut down…)
      connectionLost();
      var e = new Error('The app is not reachable — reconnecting…');
      e.offline = true;
      throw e;
    }).then(function (r) {
      if (connLost) connectionBack();
      if (r.status === 401) staleWindow();
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var er = new Error(d.error || ('HTTP ' + r.status)); er.status = r.status; throw er; }
        return d;
      });
    });
  }

  // Lost connection: one clear notice (not a toast per request), retry until it is back.
  var connLost = null;
  function connectionLost() {
    if (connLost || staleShown) return;
    connLost = h('div', { class: 'conn-lost' }, h('b', null, 'Lost connection to the app.'),
      ' Reconnecting… If it does not come back, start it again: Start menu → ', h('i', null, 'Simab Workspace'),
      ', or run ', h('kbd', null, 'simabworkspace gui'), '.');
    document.body.append(connLost);
    (function poll() {
      if (!connLost) return;
      fetch('/api/ping', { headers: { 'X-Token': token } }).then(function (r) {
        if (r.status === 401) { connectionBack(); staleWindow(); return; }
        if (r.ok) { connectionBack(); return; }
        setTimeout(poll, 2000);
      }).catch(function () { setTimeout(poll, 2000); });
    })();
  }
  function connectionBack() {
    if (!connLost) return;
    connLost.remove();
    connLost = null;
    if (typeof refreshProjects === 'function') { refreshProjects(); refreshStatus(true); }
    S.tabs.forEach(function (t) { if (t.ws && t.ws.readyState > 1 && !t.closed && !t.gone && t.connect) t.connect(); });
  }

  // The app was restarted (e.g. updated): this window's key no longer works.
  var staleShown = false;
  function staleWindow() {
    if (staleShown) return;
    staleShown = true;
    var el = h('div', { class: 'empty stale' }, h('div', { class: 'empty-inner' },
      h('p', { class: 'empty-title' }, 'This window is from an older run of the app'),
      h('p', { class: 'muted' }, 'The app was restarted (probably updated). Use the window that just opened, or run ', h('kbd', null, 'simabworkspace gui'), '. You can close this one.')));
    document.body.append(el);
  }

  // ------------------------------------------------------------------ state
  var S = {
    settings: {}, server: {}, projects: [], projectsKey: '', tabs: [], active: null,
    open: new Set(JSON.parse(localStorage.getItem('sw-open') || '[]')),
    filter: '', status: {}, wsl: false, lastTabs: [], version: '', git: {}
  };

  var MONO = '"JetBrains Mono", "Vazirmatn", ui-monospace, Menlo, Consolas, monospace';
  var THEME = {
    background: '#0c0e12', foreground: '#d6dbe3', cursor: '#c0caf5', cursorAccent: '#0c0e12',
    selectionBackground: '#33467c99',
    black: '#15161e', red: '#f7768e', green: '#9ece6a', yellow: '#e0af68', blue: '#7aa2f7',
    magenta: '#bb9af7', cyan: '#7dcfff', white: '#a9b1d6',
    brightBlack: '#565f89', brightRed: '#ff899d', brightGreen: '#b9f27c', brightYellow: '#ffc777',
    brightBlue: '#8db0ff', brightMagenta: '#c7a9ff', brightCyan: '#a4daff', brightWhite: '#c0caf5'
  };

  function project(name) { return S.projects.find(function (p) { return p.name === name; }) || null; }

  // ------------------------------------------------------------------ project colours
  var PALETTE = ['#7aa2f7', '#9ece6a', '#e0af68', '#f7768e', '#bb9af7', '#7dcfff', '#ff9e64', '#73daca', '#e5c07b', '#c678dd', '#56b6c2', '#d19a66'];
  function autoColor(name) {
    var x = 0;
    for (var i = 0; i < name.length; i++) x = (x * 31 + name.charCodeAt(i)) >>> 0;
    return PALETTE[x % PALETTE.length];
  }
  function projectColor(name) {
    if (!name) return '';
    var p = project(name);
    return (p && p.color) || autoColor(name);
  }
  function paintTab(tab) {
    var c = projectColor(tab.info.spec.project);
    if (c) tab.el.style.setProperty('--pc', c); else tab.el.style.removeProperty('--pc');
    tab.el.classList.toggle('colored', !!c);
    if (tab.pane) { if (c) tab.pane.style.setProperty('--pc', c); else tab.pane.style.removeProperty('--pc'); }
  }
  function repaintTabs() { S.tabs.forEach(paintTab); }

  function colorDialog(p) {
    var cur = p.color || '';
    function save(c) {
      api('POST', 'color/' + encodeURIComponent(p.name), { color: c }).then(function () { md.close(); refreshProjects(); }).catch(fail);
    }
    var custom = h('input', { type: 'color', value: cur || autoColor(p.name), title: 'Any colour', onchange: function () { save(custom.value); } });
    var md = openModal({
      title: 'Colour — ' + p.name, size: 'sm',
      body: [h('div', { class: 'swatches' }, PALETTE.map(function (c) {
        return h('button', { class: 'swatch' + (c === (cur || autoColor(p.name)) ? ' on' : ''), style: 'background:' + c, title: c, onclick: function () { save(c); } });
      }), h('label', { class: 'swatch custom', title: 'Pick any colour' }, '+', custom)),
        h('div', { class: 'muted', style: 'font-size:12px;margin-top:10px' }, 'Tabs of this project are marked with it.')],
      foot: [h('button', { class: 'btn', disabled: !cur, onclick: function () { save(''); } }, 'Automatic'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')]
    });
  }
  function serverReady() { return !!(S.server && S.server.configured); }
  function kindMark(spec) {
    if (spec.kind === 'chat' || spec.kind === 'claude') return ['✳', 'claude'];
    if (spec.kind === 'run') return ['▶', 'run'];
    if (spec.kind === 'sync-info' || spec.kind === 'sync-monitor') return ['⇅', ''];
    return ['›', spec.where === 'server' ? 'server' : 'local'];
  }

  // ------------------------------------------------------------------ toasts
  function toast(msg, type, opts) {
    opts = opts || {};
    var el = h('div', { class: 'toast ' + (type || '') }, msg);
    el.addEventListener('click', function () { if (opts.onclick) opts.onclick(); el.remove(); });
    $('#toasts').append(el);
    setTimeout(function () { el.remove(); }, opts.timeout || (type === 'err' ? 7000 : 3500));
  }
  function fail(e) { if (e && e.offline) return; toast(e && e.message ? e.message : String(e), 'err'); }

  // ------------------------------------------------------------------ menus
  var menuEl = null;
  function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
  function showMenu(x, y, items) {
    closeMenu();
    var el = h('div', { class: 'menu' });
    var last = null;
    items.forEach(function (it) {
      if (!it) return;
      if (it === '-') { if (last && last !== '-') el.append(h('hr')); last = '-'; return; }
      last = it;
      if (it.header) { el.append(h('div', { class: 'mh' }, it.header)); return; }
      el.append(h('button', { disabled: !!it.disabled, onclick: function () { closeMenu(); it.action(); } },
        it.label, it.k ? h('span', { class: 'k' }, it.k) : null));
    });
    if (el.lastChild && el.lastChild.tagName === 'HR') el.lastChild.remove();
    $('#menu-root').append(el);
    var r = el.getBoundingClientRect();
    el.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + 'px';
    el.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + 'px';
    menuEl = el;
  }
  function menuAt(btn, items) { var r = btn.getBoundingClientRect(); showMenu(r.left, r.bottom + 4, items); }
  document.addEventListener('mousedown', function (e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }, true);
  window.addEventListener('blur', closeMenu);

  // ------------------------------------------------------------------ modals
  var modalStack = [];
  function openModal(opts) {
    var overlay = h('div', { class: 'overlay' });
    var modal = h('div', { class: 'modal ' + (opts.size || '') },
      h('div', { class: 'modal-head' }, opts.title, h('button', { class: 'icon-btn', title: 'Close (Esc)', onclick: function () { close(); } }, '✕')),
      h('div', { class: 'modal-body' }, opts.body),
      opts.foot ? h('div', { class: 'modal-foot' }, opts.foot) : null);
    overlay.append(modal);
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
    $('#modal-root').append(overlay);
    var rec = { close: close, onclose: opts.onclose };
    modalStack.push(rec);
    function close() {
      var i = modalStack.indexOf(rec);
      if (i >= 0) modalStack.splice(i, 1);
      overlay.remove();
      if (rec.onclose) rec.onclose();
      if (!modalStack.length && S.active) S.active.focus();
    }
    setTimeout(function () {
      var f = modal.querySelector('[autofocus]') || modal.querySelector('input, textarea, select');
      if (f) f.focus();
    }, 30);
    return rec;
  }

  function confirmDlg(text, okLabel, danger) {
    return new Promise(function (resolve) {
      var done = false;
      var ok = h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: function () { done = true; md.close(); resolve(true); } }, okLabel || 'OK');
      var md = openModal({
        title: 'Confirm', size: 'sm', body: h('div', null, text),
        foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), ok],
        onclose: function () { if (!done) resolve(false); }
      });
      setTimeout(function () { ok.focus(); }, 40);
    });
  }

  function promptDlg(title, value) {
    return new Promise(function (resolve) {
      var done = false;
      var inp = h('input', { class: 'inp', value: value || '', autofocus: true });
      var md;
      function ok() { done = true; md.close(); resolve(inp.value.trim()); }
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') ok(); });
      md = openModal({
        title: title, size: 'sm', body: inp,
        foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), h('button', { class: 'btn primary', onclick: ok }, 'Save')],
        onclose: function () { if (!done) resolve(null); }
      });
      setTimeout(function () { inp.select(); }, 40);
    });
  }

  // ------------------------------------------------------------------ terminal tab
  function estimateSize() {
    var t = S.active && S.active.term && S.active.opened ? S.active : S.tabs.find(function (x) { return x.term && x.opened; });
    if (t && t.term.cols > 20) return { cols: t.term.cols, rows: t.term.rows };
    var p = (L.focused ? L.focused.panesEl : $('#layout')).getBoundingClientRect();
    var fs = S.settings.fontSize || 13;
    return { cols: Math.max(40, Math.floor((p.width - 20) / (fs * 0.6))), rows: Math.max(10, Math.floor((p.height - 10) / (fs * 1.2 * 1.25))) };
  }

  function tabEl(tab) {
    var mk = kindMark(tab.info.spec);
    tab.ttlEl = h('span', { class: 'ttl' }, tab.info.title);
    tab.el = h('div', { class: 'tab', draggable: true },
      h('span', { class: 'k ' + mk[1] }, mk[0]), tab.ttlEl, h('i', { class: 'mark' }),
      h('button', { class: 'x', title: 'Close (Alt+W)', onclick: function (e) { e.stopPropagation(); closeTab(tab); } }, '×'));
    paintTab(tab);
    tab.el.addEventListener('click', function () { activate(tab); });
    tab.el.addEventListener('auxclick', function (e) { if (e.button === 1) { e.preventDefault(); closeTab(tab); } });
    tab.el.addEventListener('contextmenu', function (e) { e.preventDefault(); tabMenu(tab, e.clientX, e.clientY); });
    setupDrag(tab);
  }

  function Tab(info, size) {
    var self = this;
    this.id = info.id;
    this.info = info;
    this.alive = info.alive;
    this.exitCode = info.exitCode;
    this.opened = false;
    this.closed = false;
    this.gone = false;
    this.retry = 0;
    this.faOpen = false;
    this.faDismissed = false;
    this.history = [];
    this.histPos = -1;
    this.firstSnapshot = true;
    tabEl(this);

    this.host = h('div', { class: 'term-host' });
    this.faLines = h('div', { class: 'fa-lines' });
    this.fa = h('div', { class: 'fa-view hidden' },
      h('div', { class: 'fa-head' }, 'Persian reading view', h('span', { class: 'grow' }),
        h('button', { class: 'icon-btn', title: 'Close', onclick: function () { self.toggleFa(false); self.faDismissed = true; } }, '✕')),
      this.faLines);
    this.body = h('div', { class: 'pbody' }, this.host, this.fa);
    this.ta = h('textarea', { rows: 1, dir: 'auto', placeholder: 'Type here (Persian works), Enter to send · Esc back to terminal' });
    this.composer = h('div', { class: 'composer hidden' }, this.ta);
    this.ta.addEventListener('keydown', function (e) { self.composerKey(e); });
    this.ta.addEventListener('input', function () { self.ta.style.height = 'auto'; self.ta.style.height = Math.min(160, self.ta.scrollHeight + 2) + 'px'; });
    this.pane = h('div', { class: 'pane hidden-pane' }, this.body, this.composer);

    var st = S.settings;
    this.term = new Terminal({
      cols: size.cols, rows: size.rows,
      fontFamily: MONO, fontSize: st.fontSize || 13, lineHeight: 1.2, letterSpacing: 0,
      cursorBlink: true, cursorStyle: 'bar', allowProposedApi: true,
      scrollback: st.scrollback || 10000, theme: THEME, macOptionIsMeta: true,
      drawBoldTextInBrightColors: false, minimumContrastRatio: 1, smoothScrollDuration: 0
    });
    this.fit = new FitAddon.FitAddon();
    this.search = new SearchAddon.SearchAddon();
    this.term.loadAddon(this.fit);
    this.term.loadAddon(this.search);
    this.term.loadAddon(new WebLinksAddon.WebLinksAddon(function (e, uri) { window.open(uri, '_blank'); }));
    try { this.term.loadAddon(new Unicode11Addon.Unicode11Addon()); this.term.unicode.activeVersion = '11'; } catch (e) { /* ignore */ }

    this.term.onData(function (d) {
      if (!self.alive) { if (d === '\r') self.restart(); return; }
      self.send({ t: 'i', d: d });
    });
    this.term.onResize(function (sz) { self.send({ t: 'r', c: sz.cols, r: sz.rows }); });
    this.term.onBell(function () { self.onBell(); });
    this.term.onSelectionChange(function () {
      if (S.settings.copyOnSelect && self.term.hasSelection()) {
        var t = self.term.getSelection();
        if (t && navigator.clipboard) navigator.clipboard.writeText(t).catch(function () {});
      }
    });
    this.term.attachCustomKeyEventHandler(function (e) { return self.keyFilter(e); });
    this.host.addEventListener('contextmenu', function (e) { e.preventDefault(); tabMenu(self, e.clientX, e.clientY); });

    this.scheduleFa = debounce(function () { self.renderFa(); }, 160);
    this.detectFa = debounce(function () { self.autoFa(); }, 500);
    this.applyInfo();
    this.connect();
  }

  Tab.prototype.focus = function () {
    if (!this.composer.classList.contains('hidden') && document.activeElement === this.ta) return;
    this.term.focus();
  };

  Tab.prototype.ensureOpen = function () {
    if (this.opened) return;
    this.term.open(this.host);
    this.opened = true;
    if (window.WebglAddon) {
      try {
        var gl = new WebglAddon.WebglAddon();
        gl.onContextLoss(function () { gl.dispose(); });
        this.term.loadAddon(gl);
      } catch (e) { /* falls back to the DOM renderer */ }
    }
  };

  Tab.prototype.doFit = function () {
    if (!this.opened || this.pane.classList.contains('hidden-pane')) return;
    try { this.fit.fit(); } catch (e) { /* not visible yet */ }
  };

  Tab.prototype.send = function (msg) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg));
  };

  Tab.prototype.connect = function () {
    var self = this;
    if (this.closed || this.gone) return;
    var ws = new WebSocket('ws://' + location.host + '/ws?id=' + encodeURIComponent(this.id) + '&t=' + encodeURIComponent(token));
    this.ws = ws;
    ws.onopen = function () {
      self.retry = 0;
      if (self.opened) self.send({ t: 'r', c: self.term.cols, r: self.term.rows });
      if (self.alive) self.setBanner(null);
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.t === 'o') { self.term.write(msg.d); self.onOutput(); }
      else if (msg.t === 's') {
        self.term.reset();
        if (msg.d) self.term.write(msg.d);
        self.setAlive(msg.alive, msg.code, self.firstSnapshot);
        self.firstSnapshot = false;
        self.onOutput(true);
      } else if (msg.t === 'x') self.setAlive(false, msg.code, false);
      else if (msg.t === 'r') self.setAlive(true, null, false);
      else if (msg.t === 'gone') self.gone = true;
    };
    ws.onclose = function () {
      if (self.closed || self.gone) return;
      self.retry++;
      if (self.retry > 2) self.setBanner('reconnecting…', 'fail', true);
      setTimeout(function () { self.reconnect(); }, Math.min(5000, 300 * self.retry));
    };
  };

  Tab.prototype.reconnect = function () {
    var self = this;
    if (this.closed) return;
    api('GET', 'state').then(function (st) {
      if (!st.sessions.some(function (s) { return s.id === self.id; })) {
        self.gone = true;
        self.alive = false;
        self.applyInfo();
        self.setBanner('session ended (the app was restarted)', 'fail', false, true);
      } else self.connect();
    }).catch(function () { setTimeout(function () { self.reconnect(); }, 2000); });
  };

  Tab.prototype.onOutput = function (snapshot) {
    if (!snapshot && S.active !== this) this.el.classList.add('activity');
    if (this.faOpen) this.scheduleFa();
    else if (S.settings.persianView === 'auto' && !this.faDismissed) this.detectFa();
  };

  Tab.prototype.onBell = function () {
    if (S.active === this && !document.hidden) return;
    var self = this;
    this.el.classList.add('bell');
    toast(this.info.title + ' needs you', 'warn', { onclick: function () { activate(self); } });
    notify('Needs your attention', this.info.title);
  };

  Tab.prototype.setAlive = function (alive, code, initial) {
    var was = this.alive;
    this.alive = !!alive;
    this.exitCode = code;
    this.applyInfo();
    renderProjects();
    if (this.alive) { this.setBanner(null); return; }
    var kind = this.info.spec.kind;
    if (!initial && was && kind === 'shell' && code === 0) { closeTab(this, true); return; }
    var ok = code === 0;
    this.setBanner(ok ? 'finished' : 'exited with code ' + code, ok ? '' : 'fail');
    if (!initial && was && S.active !== this && kind !== 'shell') {
      var self = this;
      toast(this.info.title + (ok ? ' finished' : ' failed (code ' + code + ')'), ok ? 'ok' : 'err', { onclick: function () { activate(self); } });
    }
  };

  Tab.prototype.setBanner = function (text, cls, noButtons, onlyClose) {
    if (this.banner) { this.banner.remove(); this.banner = null; }
    if (!text) return;
    var self = this;
    this.banner = h('div', { class: 'banner ' + (cls || '') }, text,
      noButtons || onlyClose ? null : h('button', { class: 'btn sm', title: 'Enter', onclick: function () { self.restart(); } }, 'Restart'),
      noButtons ? null : h('button', { class: 'btn sm', onclick: function () { closeTab(self, true); } }, 'Close'));
    this.body.append(this.banner);
  };

  Tab.prototype.applyInfo = function () {
    var i = this.info;
    this.ttlEl.textContent = i.title;
    this.el.title = i.title + (i.spec.project ? ' — ' + i.spec.project : '') + ' (' + i.spec.where + ')';
    this.el.classList.toggle('exited', !this.alive);
    this.el.classList.toggle('fail', !this.alive && this.exitCode !== 0 && this.exitCode !== null && this.exitCode !== undefined);
    this.el.classList.toggle('busy', this.alive && i.spec.kind === 'run');
  };

  Tab.prototype.restart = function () {
    var self = this;
    if (this.gone) return;
    this.setBanner(null);
    api('POST', 'sessions/' + this.id + '/restart').then(function (info) {
      self.info = info;
      self.setAlive(true, null, false);
      self.focus();
    }).catch(fail);
  };

  Tab.prototype.stop = function () { api('POST', 'sessions/' + this.id + '/stop').catch(fail); };

  Tab.prototype.keyFilter = function (e) {
    if (e.type !== 'keydown') return true;
    if (isGlobalShortcut(e)) return false;
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyC') {
      e.preventDefault();
      var t = this.term.getSelection();
      if (t && navigator.clipboard) navigator.clipboard.writeText(t).catch(fail);
      return false;
    }
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyV') return false;    // the browser pastes
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyF') { e.preventDefault(); this.openSearch(); return false; }
    return true;
  };

  Tab.prototype.paste = function () {
    var self = this;
    if (!navigator.clipboard || !navigator.clipboard.readText) { toast('Use Ctrl+Shift+V to paste', 'warn'); return; }
    navigator.clipboard.readText().then(function (t) { if (t) self.term.paste(t); self.term.focus(); })
      .catch(function () { toast('Clipboard access denied — use Ctrl+Shift+V', 'warn'); });
  };

  Tab.prototype.openSearch = function () {
    var self = this;
    if (this.searchEl) { this.searchEl.querySelector('input').focus(); return; }
    var inp = h('input', { placeholder: 'Find', dir: 'auto' });
    function go(prev) {
      if (!inp.value) return;
      var found = prev ? self.search.findPrevious(inp.value) : self.search.findNext(inp.value);
      inp.style.color = found ? '' : 'var(--err)';
    }
    function close() { if (self.searchEl) { self.searchEl.remove(); self.searchEl = null; } self.term.clearSelection(); self.term.focus(); }
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); go(e.shiftKey); }
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    inp.addEventListener('input', debounce(function () { go(false); }, 150));
    this.searchEl = h('div', { class: 'searchbar' }, inp,
      h('button', { class: 'icon-btn', title: 'Previous (Shift+Enter)', onclick: function () { go(true); } }, '↑'),
      h('button', { class: 'icon-btn', title: 'Next (Enter)', onclick: function () { go(false); } }, '↓'),
      h('button', { class: 'icon-btn', title: 'Close', onclick: close }, '✕'));
    this.host.append(this.searchEl);
    inp.focus();
  };

  // Persian reading view: terminals cannot shape/order Persian; the browser can.
  Tab.prototype.toggleFa = function (force) {
    this.faOpen = typeof force === 'boolean' ? force : !this.faOpen;
    this.fa.classList.toggle('hidden', !this.faOpen);
    if (this.faOpen) { this.renderFa(); this.faLines.scrollTop = this.faLines.scrollHeight; }
    var self = this;
    requestAnimationFrame(function () { self.doFit(); });
  };

  Tab.prototype.autoFa = function () {
    if (this.faOpen || this.faDismissed || S.settings.persianView !== 'auto') return;
    var b = this.term.buffer.active;
    for (var i = Math.max(0, b.length - this.term.rows - 20); i < b.length; i++) {
      var l = b.getLine(i);
      if (l && FA_RE.test(l.translateToString(true))) { this.toggleFa(true); return; }
    }
  };

  var EDGE_START = /^\s*[─-╿]\s?/;
  var EDGE_END = /\s?[─-╿]\s*$/;
  var ONLY_BOX = /^[\s─-▟]*$/;

  Tab.prototype.renderFa = function () {
    if (!this.faOpen) return;
    var b = this.term.buffer.active;
    var lines = [];
    for (var i = Math.max(0, b.length - 800); i < b.length; i++) {
      var l = b.getLine(i);
      if (!l) continue;
      var s = l.translateToString(true);
      if (l.isWrapped && lines.length) lines[lines.length - 1] += s; else lines.push(s);
    }
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    var box = this.faLines;
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    var frag = document.createDocumentFragment();
    var lastGap = true;
    lines.forEach(function (raw) {
      var t = raw.replace(EDGE_START, '').replace(EDGE_END, '');
      if (!t.trim() || ONLY_BOX.test(t)) {
        if (/[─-╿]{3,}/.test(raw)) { frag.append(h('div', { class: 'fa-sep' })); lastGap = true; }
        else if (!lastGap) { frag.append(h('div', { class: 'fa-gap' })); lastGap = true; }
        return;
      }
      lastGap = false;
      var fa = FA_RE.test(t);
      frag.append(h('div', { class: 'fa-line' + (fa ? '' : ' code'), dir: 'auto' }, fa ? t.trim() : t.replace(/\s+$/, '')));
    });
    box.replaceChildren(frag);
    if (atBottom) box.scrollTop = box.scrollHeight;
  };

  Tab.prototype.toggleComposer = function (force) {
    var show = typeof force === 'boolean' ? force : this.composer.classList.contains('hidden');
    this.composer.classList.toggle('hidden', !show);
    var self = this;
    requestAnimationFrame(function () { self.doFit(); if (show) self.ta.focus(); else self.term.focus(); });
  };

  Tab.prototype.composerKey = function (e) {
    if (e.isComposing || isGlobalShortcut(e)) return;
    var ta = this.ta;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      var text = ta.value;
      if (!text.trim() || !this.alive) return;
      this.history.push(text);
      this.histPos = -1;
      this.term.paste(text);
      var self = this;
      setTimeout(function () { self.send({ t: 'i', d: '\r' }); }, 80);
      ta.value = '';
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); this.term.focus(); return; }
    if (e.key === 'ArrowUp' && this.history.length && !ta.value) {
      e.preventDefault();
      this.histPos = this.histPos < 0 ? this.history.length - 1 : Math.max(0, this.histPos - 1);
      ta.value = this.history[this.histPos];
    }
  };

  Tab.prototype.dispose = function () {
    this.closed = true;
    if (this.ws) try { this.ws.close(); } catch (e) { /* ignore */ }
    try { this.term.dispose(); } catch (e) { /* ignore */ }
    this.el.remove();
    this.pane.remove();
  };

  // ------------------------------------------------------------------ layout: split sections
  var L = { root: null, groups: [], focused: null, zoom: null };
  var gidSeq = 0;

  function Group() {
    var g = this;
    this.id = 'g' + (++gidSeq);
    this.tabs = [];
    this.active = null;
    this.tabsEl = h('div', { class: 'gtabs' });
    this.tabsEl.addEventListener('dragover', function (e) { if (dragTab) e.preventDefault(); });
    this.tabsEl.addEventListener('drop', function (e) {
      if (!dragTab || e.target !== g.tabsEl) return;
      e.preventDefault();
      moveTab(dragTab, g, g.tabs.length);
    });
    this.closeBtn = h('button', { class: 'icon-btn', title: 'Close section (tabs move to the next one)', onclick: function () { closeGroup(g); } }, '✕');
    this.bar = h('div', { class: 'gbar' }, this.tabsEl,
      h('button', { class: 'icon-btn newtab', title: 'New tab (Alt+T)', onclick: function (e) { focusGroup(g); newTabMenu(e.currentTarget); } }, '+'),
      h('span', { class: 'grow' }),
      h('span', { class: 'gtools' },
        h('button', { class: 'icon-btn', title: 'Split right (Alt+\\)', onclick: function () { splitGroup(g, 'right'); } }, '◫'),
        h('button', { class: 'icon-btn', title: 'Split down (Alt+-)', onclick: function () { splitGroup(g, 'bottom'); } }, '⊟'),
        this.closeBtn));
    this.bar.addEventListener('dblclick', function (e) { if (e.target === g.bar || e.target === g.tabsEl) toggleZoom(g); });
    this.emptyEl = h('div', { class: 'gempty' }, 'Drop a tab here, or press +');
    this.panesEl = h('div', { class: 'gpanes' }, this.emptyEl);
    this.dropEl = h('div', { class: 'drop-ind hidden' });
    this.el = h('div', { class: 'group' }, this.bar, this.panesEl, this.dropEl);
    this.el.addEventListener('mousedown', function () { if (L.focused !== g) focusGroup(g, true); }, true);
    this.el.addEventListener('dragover', function (e) {
      if (!dragTab || g.bar.contains(e.target)) return;
      e.preventDefault();
      g.showDrop(dropZone(g, e));
    });
    this.el.addEventListener('dragleave', function (e) { if (!g.el.contains(e.relatedTarget)) g.showDrop(null); });
    this.el.addEventListener('drop', function (e) {
      if (!dragTab || g.bar.contains(e.target)) return;
      e.preventDefault();
      var z = dropZone(g, e);
      g.showDrop(null);
      if (z === 'center') moveTab(dragTab, g, g.tabs.length);
      else {
        var t = dragTab;
        if (t.group === g && g.tabs.length === 1) return;
        var ng = splitGroup(g, z, true);
        moveTab(t, ng, 0);
      }
    });
    new ResizeObserver(debounce(function () { if (g.active) g.active.doFit(); }, 50)).observe(this.panesEl);
    this.node = { type: 'g', g: this };
  }

  Group.prototype.showDrop = function (zone) {
    var d = this.dropEl;
    if (!zone) { d.classList.add('hidden'); return; }
    var top = this.bar.offsetHeight;
    var half = 'calc((100% - ' + top + 'px) / 2)';
    var box = { center: [top + 'px', '0', '0', '0'], left: [top + 'px', '50%', '0', '0'], right: [top + 'px', '0', '0', '50%'],
      top: [top + 'px', '0', half, '0'], bottom: ['calc(' + top + 'px + ' + half + ')', '0', '0', '0'] }[zone];
    d.style.top = box[0]; d.style.right = box[1]; d.style.bottom = box[2]; d.style.left = box[3];
    d.classList.remove('hidden');
  };

  Group.prototype.renderEmpty = function () {
    this.emptyEl.classList.toggle('hidden', !!this.tabs.length || L.groups.length < 2);
  };

  function dropZone(g, e) {
    var r = g.panesEl.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    var edge = Math.min(x, 1 - x, y, 1 - y);
    if (edge > 0.22) return 'center';
    if (edge === x) return 'left';
    if (edge === 1 - x) return 'right';
    return edge === y ? 'top' : 'bottom';
  }

  function locate(target, node, parent, idx) {
    node = node || L.root;
    if (node === target) return { parent: parent || null, i: idx || 0 };
    if (node.type === 's') {
      for (var i = 0; i < node.kids.length; i++) {
        var r = locate(target, node.kids[i], node, i);
        if (r) return r;
      }
    }
    return null;
  }

  function splitGroup(g, side, noRender) {
    if (S.active && S.active.info.spec.project) L.lastProject = S.active.info.spec.project;
    var dir = side === 'left' || side === 'right' ? 'h' : 'v';
    var before = side === 'left' || side === 'top';
    var ng = new Group();
    L.groups.push(ng);
    var loc = locate(g.node);
    if (loc && loc.parent && loc.parent.dir === dir) {
      var half = loc.parent.sizes[loc.i] / 2;
      loc.parent.sizes[loc.i] = half;
      var at = before ? loc.i : loc.i + 1;
      loc.parent.kids.splice(at, 0, ng.node);
      loc.parent.sizes.splice(at, 0, half);
    } else {
      var sp = { type: 's', dir: dir, kids: before ? [ng.node, g.node] : [g.node, ng.node], sizes: [1, 1] };
      if (loc && loc.parent) loc.parent.kids[loc.i] = sp; else L.root = sp;
    }
    L.zoom = null;
    focusGroup(ng);
    if (!noRender) renderLayout();
    return ng;
  }

  function removeGroup(g) {
    if (L.groups.length < 2) return;
    var loc = locate(g.node);
    if (!loc || !loc.parent) return;
    var par = loc.parent;
    par.kids.splice(loc.i, 1);
    par.sizes.splice(loc.i, 1);
    if (par.kids.length === 1) {
      var only = par.kids[0];
      var pl = locate(par);
      if (pl && pl.parent) pl.parent.kids[pl.i] = only; else L.root = only;
    }
    L.groups.splice(L.groups.indexOf(g), 1);
    if (L.zoom === g) L.zoom = null;
    g.el.remove();
    if (L.focused === g) focusGroup(L.groups[0]);
    renderLayout();
  }

  function closeGroup(g) {
    if (L.groups.length < 2) return;
    var other = L.groups.find(function (x) { return x !== g; });
    g.tabs.slice().forEach(function (t) { moveTab(t, other, other.tabs.length, true); });
    removeGroup(g);
  }

  function toggleZoom(g) {
    if (L.groups.length < 2 && !L.zoom) return;
    L.zoom = L.zoom === g ? null : g;
    renderLayout();
  }

  function focusGroup(g, keepTab) {
    if (!g) return;
    L.focused = g;
    L.groups.forEach(function (x) { x.el.classList.toggle('focused', x === g && L.groups.length > 1); });
    S.active = g.active;
    if (!keepTab && g.active) activate(g.active);
    renderTitle();
  }

  // flex-grow as a share of 1000: a sum below 1 would leave part of the area empty
  function grow(node, i) {
    var sum = node.sizes.reduce(function (x, y) { return x + y; }, 0) || 1;
    return Math.max(1, Math.round(node.sizes[i] / sum * 1000));
  }

  function gutter(node, i, splitEl) {
    var gEl = h('div', { class: 'gutter ' + node.dir });
    gEl.addEventListener('mousedown', function (e) {
      e.preventDefault();
      var cells = Array.prototype.filter.call(splitEl.children, function (c) { return c.classList.contains('cell'); });
      var a = cells[i], b = cells[i + 1];
      var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      var horiz = node.dir === 'h';
      var total = horiz ? ra.width + rb.width : ra.height + rb.height;
      var sum = node.sizes[i] + node.sizes[i + 1];
      var start = horiz ? e.clientX : e.clientY;
      var startA = horiz ? ra.width : ra.height;
      gEl.classList.add('drag');
      document.body.classList.add(horiz ? 'resizing-h' : 'resizing-v');
      function move(ev) {
        var pos = horiz ? ev.clientX : ev.clientY;
        var wa = Math.max(total * 0.08, Math.min(total * 0.92, startA + pos - start));
        node.sizes[i] = sum * wa / total;
        node.sizes[i + 1] = sum - node.sizes[i];
        a.style.flex = grow(node, i) + ' 1 0px';
        b.style.flex = grow(node, i + 1) + ' 1 0px';
      }
      function up() {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
        gEl.classList.remove('drag');
        document.body.classList.remove('resizing-h', 'resizing-v');
        fitAll();
        saveLayout();
      }
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    });
    gEl.addEventListener('dblclick', function () {
      var sum = node.sizes[i] + node.sizes[i + 1];
      node.sizes[i] = node.sizes[i + 1] = sum / 2;
      renderLayout();
    });
    return gEl;
  }

  function renderLayout() {
    function build(n) {
      if (n.type === 'g') return n.g.el;
      var el = h('div', { class: 'split ' + n.dir });
      n.kids.forEach(function (k, i) {
        if (i) el.append(gutter(n, i - 1, el));
        el.append(h('div', { class: 'cell', style: 'flex:' + grow(n, i) + ' 1 0px' }, build(k)));
      });
      return el;
    }
    $('#layout').replaceChildren(L.zoom ? L.zoom.el : build(L.root));
    var many = L.groups.length > 1;
    L.groups.forEach(function (g) {
      g.closeBtn.classList.toggle('hidden', !many);
      g.el.classList.toggle('focused', g === L.focused && many);
      g.renderEmpty();
    });
    $('#empty').classList.toggle('hidden', S.tabs.length > 0 || many);
    if (!S.tabs.length && !many) renderEmpty();
    fitAll();
    saveLayout();
  }

  function fitAll() {
    requestAnimationFrame(function () {
      L.groups.forEach(function (g) { if (g.active) { g.active.doFit(); if (g.active.faOpen) g.active.renderFa(); } });
    });
  }

  function moveTab(tab, g, index, noRender) {
    var old = tab.group;
    if (old) {
      old.tabs.splice(old.tabs.indexOf(tab), 1);
      if (old.active === tab) {
        old.active = old.tabs[0] || null;
        if (old.active) showInGroup(old, old.active);
      }
    }
    if (index === undefined || index > g.tabs.length) index = g.tabs.length;
    g.tabs.splice(index, 0, tab);
    tab.group = g;
    var ref = g.tabs[index + 1];
    if (ref && ref.el.parentNode === g.tabsEl) g.tabsEl.insertBefore(tab.el, ref.el); else g.tabsEl.append(tab.el);
    g.panesEl.append(tab.pane);
    syncOrder();
    if (old && old !== g && !old.tabs.length && L.groups.length > 1 && !noRender) removeGroup(old);
    activate(tab);
    if (!noRender) renderLayout();
  }

  function showInGroup(g, tab) {
    g.active = tab;
    g.tabs.forEach(function (t) {
      t.el.classList.toggle('active', t === tab);
      t.pane.classList.toggle('hidden-pane', t !== tab);
    });
  }

  function syncOrder() {
    var order = [];
    walkGroups(function (g) { g.tabs.forEach(function (t) { order.push(t); }); });
    S.tabs = order;
    api('POST', 'order', { ids: order.map(function (t) { return t.id; }) }).catch(function () {});
  }

  function walkGroups(fn, node) {
    node = node || L.root;
    if (!node) return;
    if (node.type === 'g') fn(node.g); else node.kids.forEach(function (k) { walkGroups(fn, k); });
  }

  var saveLayout = debounce(function () {
    function ser(n) {
      if (n.type === 'g') return { type: 'g', tabs: n.g.tabs.map(function (t) { return t.id; }), active: n.g.active ? n.g.active.id : null, focused: n.g === L.focused };
      return { type: 's', dir: n.dir, sizes: n.sizes.slice(), kids: n.kids.map(ser) };
    }
    if (L.root) localStorage.setItem('sw-layout', JSON.stringify(ser(L.root)));
  }, 300);

  function initLayout(sessionIds) {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem('sw-layout') || 'null'); } catch (e) { saved = null; }
    var want = {};
    var focused = null;
    function build(n) {
      if (!n || (n.type !== 'g' && n.type !== 's')) return null;
      if (n.type === 'g') {
        var g = new Group();
        L.groups.push(g);
        (n.tabs || []).forEach(function (id) { if (sessionIds.indexOf(id) >= 0) want[id] = g; });
        g.wantActive = n.active;
        if (n.focused) focused = g;
        return g.node;
      }
      var kids = (n.kids || []).map(build).filter(Boolean);
      if (!kids.length) return null;
      if (kids.length === 1) return kids[0];
      var sizes = (n.sizes || []).slice(0, kids.length);
      while (sizes.length < kids.length) sizes.push(1);
      return { type: 's', dir: n.dir === 'v' ? 'v' : 'h', kids: kids, sizes: sizes };
    }
    L.root = build(saved);
    if (!L.root) { var g0 = new Group(); L.groups.push(g0); L.root = g0.node; }
    L.focused = focused || L.groups[0];
    return want;
  }

  function gridAll() {
    var tabs = S.tabs.slice();
    if (tabs.length < 2) { toast('Open at least two tabs first', 'warn'); return; }
    var cols = Math.ceil(Math.sqrt(tabs.length)), rows = Math.ceil(tabs.length / cols);
    var groups = [];
    L.groups.forEach(function (g) { g.el.remove(); });
    L.groups = [];
    L.zoom = null;
    var rowNodes = [];
    for (var r = 0; r < rows; r++) {
      var kids = [];
      for (var c = 0; c < cols && r * cols + c < tabs.length; c++) {
        var g = new Group();
        L.groups.push(g);
        groups.push(g);
        kids.push(g.node);
      }
      rowNodes.push(kids.length === 1 ? kids[0] : { type: 's', dir: 'h', kids: kids, sizes: kids.map(function () { return 1; }) });
    }
    L.root = rowNodes.length === 1 ? rowNodes[0] : { type: 's', dir: 'v', kids: rowNodes, sizes: rowNodes.map(function () { return 1; }) };
    tabs.forEach(function (t, i) {
      var g = groups[i];
      g.tabs = [t];
      t.group = g;
      g.tabsEl.append(t.el);
      g.panesEl.append(t.pane);
      showInGroup(g, t);
      t.ensureOpen();
    });
    L.focused = groups[0];
    renderLayout();
    activate(tabs[0]);
  }

  function mergeAll() {
    var tabs = S.tabs.slice();
    var g = L.focused || L.groups[0];
    L.groups.forEach(function (x) { if (x !== g) x.el.remove(); });
    L.groups = [g];
    L.root = g.node;
    L.zoom = null;
    g.tabs = [];
    tabs.forEach(function (t) { t.group = g; g.tabs.push(t); g.tabsEl.append(t.el); g.panesEl.append(t.pane); });
    renderLayout();
    if (S.active) activate(S.active); else if (tabs[0]) activate(tabs[0]);
  }

  function layoutItems(tab) {
    return [
      '-',
      { label: 'Split right', k: 'Alt+\\', action: function () { var ng = splitGroup(tab.group, 'right', true); moveTab(tab, ng, 0); } },
      { label: 'Split down', k: 'Alt+-', action: function () { var ng = splitGroup(tab.group, 'bottom', true); moveTab(tab, ng, 0); } },
      L.groups.length > 1 ? { label: 'Move to next section', action: function () {
        var i = L.groups.indexOf(tab.group);
        moveTab(tab, L.groups[(i + 1) % L.groups.length]);
      } } : null
    ];
  }

  // ------------------------------------------------------------------ tab management
  function activate(tab) {
    if (!tab) return;
    var g = tab.group;
    showInGroup(g, tab);
    if (L.focused !== g) {
      L.focused = g;
      L.groups.forEach(function (x) { x.el.classList.toggle('focused', x === g && L.groups.length > 1); });
    }
    S.active = tab;
    $('#empty').classList.add('hidden');
    g.renderEmpty();
    tab.el.classList.remove('activity', 'bell');
    tab.ensureOpen();
    sessionStorage.setItem('sw-active', tab.id);
    tab.el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    requestAnimationFrame(function () {
      tab.doFit();
      if (tab.faOpen) tab.renderFa();
      if (!modalStack.length) tab.focus();
    });
    renderTitle();
    saveLayout();
  }

  function renderTitle() { document.title = (S.active ? S.active.info.title + ' — ' : '') + 'Simab Workspace'; }

  function addTab(info, background, group) {
    var tab = info.spec.kind === 'chat' ? new window.SWChat.ChatTab(info) : new Tab(info, estimateSize());
    var g = group || L.focused || L.groups[0];
    paintTab(tab);
    tab.group = g;
    g.tabs.push(tab);
    S.tabs.push(tab);
    g.tabsEl.append(tab.el);
    g.panesEl.append(tab.pane);
    if (!background || !g.active) activate(tab);
    else if (g.active !== tab) tab.pane.classList.add('hidden-pane');
    $('#empty').classList.add('hidden');
    g.renderEmpty();
    return tab;
  }

  function closeTab(tab, force) {
    var go = function () {
      var i = S.tabs.indexOf(tab);
      if (i < 0) return;
      S.tabs.splice(i, 1);
      var g = tab.group;
      var gi = g.tabs.indexOf(tab);
      g.tabs.splice(gi, 1);
      tab.dispose();
      if (!tab.gone) api('DELETE', 'sessions/' + tab.id).catch(function () {});
      if (g.active === tab) {
        g.active = null;
        var next = g.tabs[gi] || g.tabs[gi - 1];
        if (next) { if (L.focused === g) activate(next); else showInGroup(g, next); }
      }
      if (!g.tabs.length && L.groups.length > 1) removeGroup(g);
      else {
        if (S.active === tab) S.active = L.focused ? L.focused.active : null;
        if (!S.tabs.length) renderLayout();
        g.renderEmpty();
      }
      renderTitle();
      renderProjects();
      saveLayout();
    };
    // uploads/downloads keep going in the background (Mutagen does the copying), so closing is safe
    var safe = ['shell', 'sync-info', 'sync-monitor', 'add-local', 'add-server', 'git-sync-on'];
    var risky = tab.isChat ? tab.busy : (tab.alive && !tab.gone && safe.indexOf(tab.info.spec.kind) < 0);
    if (!force && risky && S.settings.confirmClose) {
      confirmDlg('“' + tab.info.title + '” is still running. Close it and stop what it is doing?', 'Close', true)
        .then(function (ok) { if (ok) go(); });
    } else go();
  }

  function openSession(spec, opts) {
    opts = opts || {};
    if (spec.kind === 'chat' && spec.resume) {
      var open = S.tabs.find(function (t) { return t.isChat && !t.gone && (t.sessionId === spec.resume || t.info.spec.resume === spec.resume); });
      if (open) { activate(open); return Promise.resolve(open); }
    }
    if (spec.kind === 'run') {
      var ex = S.tabs.find(function (t) { return t.info.spec.kind === 'run' && t.info.spec.cmdId === spec.cmdId && t.info.spec.project === spec.project && !t.gone; });
      if (ex) { activate(ex); if (!ex.alive) ex.restart(); return Promise.resolve(ex); }
    }
    if (S.settings.serverPersist && spec.where === 'server') {
      spec.slot = S.tabs.filter(function (t) {
        return t.info.spec.kind === spec.kind && t.info.spec.project === (spec.project || '') && t.info.spec.where === 'server' && !t.gone;
      }).length;
    }
    var sz = estimateSize();
    return api('POST', 'sessions', Object.assign({}, spec, { cols: sz.cols, rows: sz.rows })).then(function (info) {
      var t = addTab(info, opts.background);
      renderProjects();
      return t;
    }).catch(function (e) { fail(e); return null; });
  }

  function runningCmd(projectName, cmdId) {
    return S.tabs.find(function (t) { return t.info.spec.kind === 'run' && t.info.spec.project === projectName && t.info.spec.cmdId === cmdId && t.alive && !t.gone; });
  }

  function tabMenu(tab, x, y) {
    if (tab.isChat) { tab.menu(x, y, layoutItems(tab)); return; }
    var run = tab.info.spec.kind === 'run';
    showMenu(x, y, [
      { label: 'Copy', k: 'Ctrl+Shift+C', disabled: !tab.term.hasSelection(), action: function () { navigator.clipboard.writeText(tab.term.getSelection()).catch(fail); } },
      { label: 'Paste', k: 'Ctrl+Shift+V', action: function () { tab.paste(); } },
      { label: 'Find', k: 'Ctrl+Shift+F', action: function () { tab.openSearch(); } },
      { label: 'Clear', action: function () { tab.term.clear(); } },
      '-',
      { label: (tab.faOpen ? 'Hide' : 'Show') + ' Persian reading view', action: function () { tab.toggleFa(); if (!tab.faOpen) tab.faDismissed = true; } },
      { label: 'Persian input box', k: 'Alt+F', action: function () { tab.toggleComposer(); } },
      '-',
      run && tab.info.url ? { label: 'Open ' + tab.info.url.replace(/^https?:\/\//, ''), action: function () { window.open(tab.info.url, '_blank'); } } : null,
      run && tab.alive ? { label: 'Stop', action: function () { tab.stop(); } } : null,
      { label: 'Restart', disabled: tab.gone, action: function () { tab.restart(); } },
      { label: 'Rename…', action: function () { renameTab(tab); } },
      { label: 'Duplicate', disabled: run, action: function () { openSession(Object.assign({}, tab.info.spec, { title: '' })); } }
    ].concat(layoutItems(tab), [
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { closeTab(tab); } },
      { label: 'Close others', disabled: S.tabs.length < 2, action: function () {
        confirmDlg('Close every tab except “' + tab.info.title + '”?', 'Close', true).then(function (ok) {
          if (ok) S.tabs.slice().forEach(function (t) { if (t !== tab) closeTab(t, true); });
        });
      } }
    ]));
  }

  function renameTab(tab) {
    promptDlg('Rename tab', tab.info.title).then(function (v) {
      if (!v) return;
      api('POST', 'sessions/' + tab.id + '/rename', { title: v }).then(function (info) { tab.info = info; tab.applyInfo(); renderTitle(); }).catch(fail);
    });
  }

  var dragTab = null;
  function setupDrag(tab) {
    var el = tab.el;
    el.addEventListener('dragstart', function (e) {
      dragTab = tab; el.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', tab.id);
      document.body.classList.add('dragging-tab');
    });
    el.addEventListener('dragend', function () {
      el.classList.remove('dragging'); dragTab = null;
      document.body.classList.remove('dragging-tab');
      S.tabs.forEach(function (t) { t.el.classList.remove('drop-before', 'drop-after'); });
      L.groups.forEach(function (g) { g.showDrop(null); });
    });
    el.addEventListener('dragover', function (e) {
      if (!dragTab || dragTab === tab) return;
      e.preventDefault();
      var r = el.getBoundingClientRect();
      var after = e.clientX > r.left + r.width / 2;
      el.classList.toggle('drop-after', after);
      el.classList.toggle('drop-before', !after);
    });
    el.addEventListener('dragleave', function () { el.classList.remove('drop-before', 'drop-after'); });
    el.addEventListener('drop', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var after = el.classList.contains('drop-after');
      el.classList.remove('drop-before', 'drop-after');
      if (!dragTab || dragTab === tab) return;
      var g = tab.group, t = dragTab;
      var idx = g.tabs.indexOf(tab) + (after ? 1 : 0);
      if (t.group === g && g.tabs.indexOf(t) < idx) idx--;
      moveTab(t, g, idx);
    });
  }

  // ------------------------------------------------------------------ project actions
  function sessionTitle(c) { return (c.title || c.first || c.last || c.id).replace(/\s+/g, ' ').slice(0, 70); }

  function resumeLatest(name) {
    api('GET', 'claude-sessions/' + encodeURIComponent(name || '')).then(function (d) {
      var c = d.sessions[0];
      if (!c) { openSession({ kind: 'chat', project: name }); return; }
      openSession({ kind: 'chat', project: name, resume: c.id, title: 'Claude · ' + sessionTitle(c) });
    }).catch(fail);
  }

  function pickClaudeSession(name, cb) {
    var list = h('div', { class: 'sess-list' }, h('div', { class: 'muted' }, 'Loading…'));
    var filter = h('input', { class: 'inp', placeholder: 'Filter conversations', dir: 'auto', autofocus: true });
    var all = [];
    function render() {
      var q = filter.value.trim().toLowerCase();
      var shown = all.filter(function (c) { return !q || (c.title + ' ' + c.first + ' ' + c.last).toLowerCase().indexOf(q) >= 0; });
      if (!shown.length) { list.replaceChildren(h('div', { class: 'muted' }, all.length ? 'No match' : 'No conversations yet')); return; }
      list.replaceChildren.apply(list, shown.map(function (c) {
        return h('div', { class: 'sess' },
          h('button', { class: 'sess-main', onclick: function () { md.close(); cb(c.id, sessionTitle(c)); } },
            h('div', { class: 'st', dir: 'auto' }, sessionTitle(c)),
            h('div', { class: 'sm' }, window.SWChat.fmtTime(c.mtime) + ' · ' + plural(c.prompts, 'message'))),
          null);
      }));
    }
    filter.addEventListener('input', render);
    var md = openModal({ title: 'Conversations' + (name ? ' — ' + name : ''), body: [filter, h('div', { style: 'height:8px' }), list] });
    api('GET', 'claude-sessions/' + encodeURIComponent(name || '')).then(function (d) { all = d.sessions; render(); })
      .catch(function (e) { list.replaceChildren(h('div', { class: 'muted' }, e.message)); });
  }

  function notify(title, body) {
    if (!S.settings.bellNotify || !document.hidden || !window.Notification || Notification.permission !== 'granted') return;
    try { new Notification(title, { body: body, icon: 'icon.svg' }); } catch (e) { /* ignore */ }
  }

  function openEditor(p, where) {
    api('POST', 'editor', { project: p.name, where: where }).then(function () { toast('Opening VS Code…', 'ok'); }).catch(fail);
  }

  function syncAction(p, action) {
    api('POST', 'sync/' + encodeURIComponent(p.name) + '/' + action).then(function () {
      toast({ flush: 'Syncing…', pause: 'Sync paused', resume: 'Sync resumed' }[action], 'ok');
      setTimeout(refreshProjects, 600);
    }).catch(fail);
  }

  function runCommand(p, c) {
    var t = runningCmd(p.name, c.id);
    if (t) { t.stop(); return; }
    openSession({ kind: 'run', project: p.name, cmdId: c.id });
  }

  function hasRemote(p) { return !!p.remote && serverReady(); }
  function openTerminal(p) { openSession({ kind: 'shell', where: hasRemote(p) ? 'server' : 'local', project: p.name }); }
  function openCode(p) { openEditor(p, hasRemote(p) ? 'server' : 'local'); }

  function projectMenu(p, anchor) {
    var sync = p.source === 'mutagen';
    var r = hasRemote(p);
    menuAt(anchor, [
      { header: 'Claude' },
      { label: 'New chat', disabled: !r, action: function () { openSession({ kind: 'chat', project: p.name }); } },
      { label: 'Continue last chat', disabled: !r, action: function () { resumeLatest(p.name); } },
      { label: 'Past chats…', disabled: !r, action: function () { pickClaudeSession(p.name, function (sid, title) { openSession({ kind: 'chat', project: p.name, resume: sid, title: 'Claude · ' + title }); }); } },
      { label: 'Claude text terminal (TUI)', disabled: !r, action: function () { openSession({ kind: 'claude', where: 'server', project: p.name }); } },
      { header: 'Open' },
      { label: 'Server terminal', disabled: !r, action: function () { openSession({ kind: 'shell', where: 'server', project: p.name }); } },
      { label: 'Local terminal', disabled: !p.localExists, action: function () { openSession({ kind: 'shell', where: 'local', project: p.name }); } },
      { label: 'VS Code on server', disabled: !r, action: function () { openEditor(p, 'server'); } },
      { label: 'VS Code, local folder', disabled: !p.localExists, action: function () { openEditor(p, 'local'); } },
      '-',
      { label: 'Git…', action: function () { gitDialog(p); } },
      { label: 'Colour…', action: function () { colorDialog(p); } },
      { label: 'History…', action: function () { historyDialog(p.name); } },
      { label: 'Run commands…', action: function () { editCommands(p); } },
      sync ? { header: 'Sync · ' + (p.sync ? p.sync.label : '') } : null,
      sync ? { label: 'Sync now', action: function () { syncAction(p, 'flush'); } } : null,
      sync ? (p.sync && p.sync.paused ? { label: 'Resume sync', action: function () { syncAction(p, 'resume'); } } : { label: 'Pause sync', action: function () { syncAction(p, 'pause'); } }) : null,
      sync ? { label: 'Sync details' + (p.sync && p.sync.conflicts ? ' · ' + plural(p.sync.conflicts, 'conflict') : '') + '…', action: function () { syncDetails(p); } } : null,
      sync ? { label: p.sync && p.sync.git === false ? 'Sync .git folders too' : 'Stop syncing .git folders', action: function () {
        var on = !(p.sync && p.sync.git !== false);
        confirmDlg(on ? 'Sync the .git folders of “' + p.name + '” too? Git then works the same on both sides. (Do not run git on both sides at the same moment.)'
          : 'Stop syncing the .git folders of “' + p.name + '”? Each side then keeps its own git history.', on ? 'Sync .git' : 'Stop', !on).then(function (ok) {
          if (ok) api('POST', 'git-sync/' + encodeURIComponent(p.name), { on: on }).then(function () { toast('Updating sync settings — this takes a moment', 'ok'); setTimeout(refreshProjects, 4000); }).catch(fail);
        });
      } } : null,
      sync ? { label: 'Move folders…', action: function () { moveDialog(p); } } : null,
      sync ? { label: 'Stop syncing…', action: function () {
        confirmDlg('Stop syncing “' + p.name + '”? Files stay on both sides; they just stop syncing. It stays in the list as a server project.', 'Stop syncing', true).then(function (ok) {
          if (ok) api('POST', 'sync/' + encodeURIComponent(p.name) + '/stop').then(function () { toast(p.name + ': sync stopped', 'ok'); refreshProjects(); }).catch(fail);
        });
      } } : null,
      p.source === 'manual' ? '-' : null,
      p.source === 'manual' ? { label: 'Remove from list', action: function () {
        confirmDlg('Remove “' + p.name + '” from the list? Files are not touched.', 'Remove', true).then(function (ok) {
          if (ok) api('DELETE', 'manual/' + encodeURIComponent(p.name)).then(refreshProjects).catch(fail);
        });
      } } : null
    ]);
  }



  // ------------------------------------------------------------------ image preview
  function viewImage(src) {
    var ov = h('div', { class: 'lightbox', title: 'Click or Esc to close' }, h('img', { src: src, alt: '' }));
    var rec = { close: function () { var i = modalStack.indexOf(rec); if (i >= 0) modalStack.splice(i, 1); ov.remove(); } };
    ov.addEventListener('click', rec.close);
    modalStack.push(rec);
    $('#modal-root').append(ov);
  }

  // ------------------------------------------------------------------ history of what you did
  var HIST_KIND = {
    prompt: ['✳', 'Claude'], chat: ['✳', 'chat'], 'claude-tui': ['✳', 'Claude (terminal)'],
    run: ['▶', 'run'], 'run-exit': ['■', 'finished'], terminal: ['›', 'terminal'],
    'git-commit': ['⎇', 'commit'], 'git-push': ['⎇', 'push'], 'git-pull': ['⎇', 'pull'], 'git-fetch': ['⎇', 'fetch'],
    vscode: ['◇', 'VS Code'], 'sync-flush': ['⇅', 'sync'], 'sync-pause': ['⇅', 'sync'], 'sync-resume': ['⇅', 'sync']
  };

  function dayLabel(t) {
    var d = new Date(t * 1000), now = new Date();
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === now.toDateString()) return 'Today';
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  }

  function claudeInTerminal(projectName, sessionId, title) {
    return openSession({ kind: 'claude', where: 'server', project: projectName, resume: sessionId || '', claudeMode: sessionId ? 'new' : 'continue',
      title: title ? 'Claude · ' + title.slice(0, 40) + ' · terminal' : '' });
  }

  function historyActions(it) {
    var p = project(it.project);
    var out = [];
    var claude = it.kind === 'prompt' || it.kind === 'chat' || it.kind === 'claude-tui';
    if (claude && it.session) {
      out.push({ label: 'Open', title: 'Open this conversation here, with its messages, and continue it', fn: function () {
        openSession({ kind: 'chat', project: it.project, resume: it.session, title: 'Claude · ' + it.text.slice(0, 40) });
      } });
    } else if (claude && p && hasRemote(p)) {
      out.push({ label: 'Open', title: 'Continue the last conversation of this project', fn: function () { resumeLatest(it.project); } });
    }
    if ((it.kind === 'run' || it.kind === 'run-exit') && it.cmdId && p && p.commands.some(function (c) { return c.id === it.cmdId; }))
      out.push({ label: 'Run again', fn: function () { openSession({ kind: 'run', project: it.project, cmdId: it.cmdId }); } });
    if (it.kind === 'terminal') out.push({ label: 'Open', fn: function () { openSession({ kind: 'shell', where: it.where || 'server', project: it.project }); } });
    if (it.kind.indexOf('git-') === 0 && p) out.push({ label: 'Git', fn: function () { gitDialog(p); } });
    return out;
  }

  function historyDialog(onlyProject) {
    var list = h('div', { class: 'hist' }, h('div', { class: 'muted' }, 'Loading…'));
    var q = h('input', { class: 'inp', placeholder: 'Search what you did…', dir: 'auto', autofocus: true });
    var proj = h('select', { class: 'inp', style: 'width:180px;flex:none' }, h('option', { value: '' }, 'All projects'),
      S.projects.map(function (p) { return h('option', { value: p.name }, p.name); }));
    proj.value = onlyProject || '';
    var items = [];
    function render() {
      var text = q.value.trim().toLowerCase();
      var shown = items.filter(function (it) {
        return (!proj.value || it.project === proj.value) &&
          (!text || (it.text + ' ' + it.project + ' ' + (it.cmd || '') + ' ' + (it.repo || '')).toLowerCase().indexOf(text) >= 0);
      });
      if (!shown.length) { list.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, items.length ? 'Nothing matches' : 'Nothing yet — what you do from now on shows up here.')); return; }
      var out = [], lastDay = '';
      shown.slice(0, 400).forEach(function (it) {
        var day = dayLabel(it.t);
        if (day !== lastDay) { out.push(h('div', { class: 'hday' }, day)); lastDay = day; }
        var k = HIST_KIND[it.kind] || ['•', it.kind];
        var ok = it.kind !== 'run-exit' ? null : it.code === 0;
        var detail = it.kind === 'run-exit' ? (ok ? 'ok' : 'exit ' + it.code) + ' · ' + (it.seconds < 60 ? it.seconds + 's' : Math.round(it.seconds / 60) + 'm')
          : it.kind === 'run' ? it.cmd : it.repo ? it.repo : it.images ? it.images + ' image(s)' : '';
        var acts = historyActions(it);
        var c = it.project ? projectColor(it.project) : '';
        out.push(h('div', { class: 'hrow' + (ok === false ? ' fail' : '') },
          h('span', { class: 'htime mono' }, new Date(it.t * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })),
          h('span', { class: 'hk', title: k[1] }, k[0]),
          it.project ? h('span', { class: 'hproj mono', style: 'color:' + c }, it.project) : h('span', { class: 'hproj muted' }, 'home'),
          h('span', { class: 'htext', dir: 'auto', title: it.text }, (it.kind.indexOf('git-') === 0 ? k[1] + (it.text ? ': ' : '') : '') + (it.text || k[1])),
          detail ? h('span', { class: 'hdetail mono', title: detail }, detail) : null,
          acts.map(function (a) { return h('button', { class: 'btn sm', title: a.title || '', onclick: function () { md.close(); a.fn(); } }, a.label); })));
      });
      list.replaceChildren.apply(list, out);
    }
    q.addEventListener('input', render);
    proj.addEventListener('change', render);
    var md = openModal({ title: 'History', size: 'wide', body: [h('div', { class: 'row', style: 'flex:none' }, q, proj), list] });
    api('GET', 'activity?limit=2000').then(function (d) { items = d.items || []; render(); })
      .catch(function (e) { list.replaceChildren(h('div', { class: 'muted' }, e.message)); });
  }

  // ------------------------------------------------------------------ git badges
  // changes (yellow) · ↑ commits to push · ↓ commits to pull · ! conflicts
  function gitBadges(name, compact) {
    var g = S.git[name];
    if (!g || !g.repos) return null;
    var out = [];
    if (g.conflicts) out.push(h('span', { class: 'gb conflict', title: plural(g.conflicts, 'conflict') }, '!' + g.conflicts));
    if (g.changes) out.push(h('span', { class: 'gb changes', title: plural(g.changes, 'changed file') + ' — commit them' }, String(g.changes)));
    if (g.ahead) out.push(h('span', { class: 'gb ahead', title: plural(g.ahead, 'commit') + ' to push' }, '↑' + g.ahead));
    if (g.behind) out.push(h('span', { class: 'gb behind', title: plural(g.behind, 'commit') + ' to pull' }, '↓' + g.behind));
    if (!out.length) return compact ? null : h('span', { class: 'gb clean', title: 'Nothing to commit, push or pull' }, '✓');
    return h('span', { class: 'gbs' }, out);
  }

  function refreshGit(name) {
    return api('GET', 'git-summary' + (name ? '?project=' + encodeURIComponent(name) + '&force=1' : '')).then(function (d) {
      var key = JSON.stringify(Object.keys(d.summary).map(function (k) { var g = d.summary[k]; return [k, g.changes, g.ahead, g.behind, g.conflicts, g.repos]; }));
      S.git = d.summary;
      if (key !== S.gitKey) { S.gitKey = key; renderProjects(); }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ git panel
  var GIT_LABEL = { M: 'modified', A: 'added', D: 'deleted', R: 'renamed', C: 'copied', U: 'conflict', '?': 'new' };

  function gitCode(f) {
    if (f.conflict) return 'U';
    if (f.untracked) return '?';
    var c = f.x !== ' ' ? f.x : f.y;
    return c === '?' ? '?' : c;
  }

  function diffEl(text) {
    var pre = h('div', { class: 'gdiff' });
    String(text || '').split('\n').forEach(function (l) {
      var cls = /^(\+\+\+|---|diff |index |new file|deleted file|similarity|rename )/.test(l) ? 'meta'
        : l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : l.slice(0, 2) === '@@' ? 'hunk' : '';
      pre.append(h('div', { class: 'gl ' + cls }, l || ' '));
    });
    return pre;
  }

  function gitDialog(p) {
    var st = null, picked = new Set(), current = null, busy = false;
    var repos = [], repo = '', where = null, counts = {};
    var switcher = h('div', { class: 'grepos hidden' });
    var head = h('div', { class: 'ghead' }, h('span', { class: 'muted' }, 'Looking for repositories…'));
    function qs() { return '?repo=' + encodeURIComponent(repo) + '&where=' + encodeURIComponent(where || ''); }
    function gpost(a, body) { return api('POST', 'git/' + encodeURIComponent(p.name) + '/' + a, Object.assign({ repo: repo, where: where }, body || {})); }
    function renderSwitcher() {
      switcher.classList.toggle('hidden', repos.length < 2);
      switcher.replaceChildren.apply(switcher, repos.map(function (r) {
        var n = counts[r];
        return h('button', { class: 'grepo' + (r === repo ? ' on' : ''), title: r || p.name, onclick: function () {
          if (r === repo || busy) return;
          repo = r; current = null; st = null;
          diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, 'Select a file to see its changes.'));
          renderSwitcher();
          load();
        } }, (r || p.name).split('/').pop(), n ? h('span', { class: 'gcount' }, String(n)) : null);
      }));
    }
    var banner = h('div', { class: 'gbanner hidden' });
    var list = h('div', { class: 'glist' });
    var diff = h('div', { class: 'gright' }, h('div', { class: 'muted', style: 'padding:12px' }, 'Select a file to see its changes.'));
    var msg = h('textarea', { class: 'inp', rows: 2, dir: 'auto', placeholder: 'Commit message' });
    var commitBtn = h('button', { class: 'btn primary', onclick: commit }, 'Commit');
    var btns = {};
    ['fetch', 'pull', 'push'].forEach(function (a) {
      btns[a] = h('button', { class: 'btn', onclick: function () { action(a); } }, a[0].toUpperCase() + a.slice(1));
    });
    var allBox = h('input', { type: 'checkbox', checked: true, title: 'Select all', onchange: function () {
      picked = allBox.checked ? new Set(st.files.map(function (f) { return f.path; })) : new Set();
      renderList();
    } });
    msg.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commit(); } });

    var md = openModal({
      title: 'Git — ' + p.name, size: 'wide', onclose: function () { refreshGit(p.name); },
      body: [switcher, head, banner, h('div', { class: 'ggrid' }, h('div', { class: 'gleft' }, h('label', { class: 'gall' }, allBox, 'Changes'), list), diff)],
      foot: [btns.fetch, btns.pull, btns.push, h('span', { class: 'grow' }), h('div', { class: 'gcommit' }, msg), commitBtn]
    });

    function setBusy(b, what) {
      busy = b;
      Object.keys(btns).forEach(function (k) { btns[k].disabled = b; });
      commitBtn.disabled = b || !st || !st.files.length || !picked.size;
      if (what) head.lastChild.textContent = b ? what + '…' : '';
    }

    function renderHead() {
      var ab = [st.ahead ? '↑' + st.ahead : '', st.behind ? '↓' + st.behind : ''].filter(Boolean).join(' ');
      head.replaceChildren(
        h('b', { class: 'mono' }, st.branch || '(no branch)'),
        st.upstream ? h('span', { class: 'muted mono' }, ' → ' + st.upstream) : h('span', { class: 'muted' }, ' · no upstream'),
        ab ? h('span', { class: 'mono gab' }, ab) : null,
        h('span', { class: 'muted' }, ' · runs ' + (st.where === 'local' ? 'on this machine' : 'on the server')),
        st.last ? h('span', { class: 'muted mono glast', title: 'Last commit' }, st.last) : null,
        h('span', { class: 'muted gwork' }));
      var problems = [];
      if (p.sync && p.sync.git === false) {
        problems.push(h('div', null, 'The .git folder of this project is not synced, so the server and this machine can disagree. ',
          h('button', { class: 'btn sm', onclick: function () { md.close(); gitSyncOn(p); } }, 'Sync .git too')));
      }
      if (st.conflicts) {
        problems.push(h('div', null, plural(st.conflicts, 'file') + ' with merge conflicts. Resolve them in VS Code, then commit here. ',
          h('button', { class: 'btn sm', onclick: function () { openCode(p); } }, 'Open in VS Code')));
      }
      banner.replaceChildren.apply(banner, problems);
      banner.classList.toggle('hidden', !problems.length);
      banner.classList.toggle('err', !!st.conflicts);
    }

    function renderList() {
      if (!st.files.length) {
        list.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, 'Nothing to commit — working tree clean.'));
      } else {
        list.replaceChildren.apply(list, st.files.map(function (f) {
          var code = gitCode(f);
          var cb = h('input', { type: 'checkbox', checked: picked.has(f.path), onclick: function (e) { e.stopPropagation(); }, onchange: function () {
            if (cb.checked) picked.add(f.path); else picked.delete(f.path);
            allBox.checked = picked.size === st.files.length;
            commitBtn.textContent = picked.size === st.files.length ? 'Commit' : 'Commit ' + picked.size;
            commitBtn.disabled = busy || !picked.size;
          } });
          var name = f.path.split('/').pop(), dir = f.path.slice(0, f.path.length - name.length);
          return h('div', { class: 'gfile' + (current === f.path ? ' sel' : '') + (f.conflict ? ' conflict' : ''), title: (GIT_LABEL[code] || code) + ': ' + f.path, onclick: function () { showDiff(f); } },
            cb, h('span', { class: 'gcode c' + (code === '?' ? 'N' : code) }, code === '?' ? 'N' : code),
            h('span', { class: 'gname' }, name), h('span', { class: 'gdir' }, dir));
        }));
      }
      commitBtn.textContent = picked.size === st.files.length ? 'Commit' : 'Commit ' + picked.size;
      commitBtn.disabled = busy || !st.files.length || !picked.size;
    }

    function showDiff(f) {
      current = f.path;
      renderList();
      diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, 'Loading…'));
      api('GET', 'git/' + encodeURIComponent(p.name) + '/diff' + qs() + '&path=' + encodeURIComponent(f.path) + '&untracked=' + (f.untracked ? 1 : 0))
        .then(function (d) {
          if (current !== f.path) return;
          diff.replaceChildren(h('div', { class: 'gdhead mono' }, f.path,
            h('button', { class: 'btn sm', onclick: function () { openCode(p); } }, 'Open in VS Code')), diffEl(d.diff || '(no textual changes)'));
        }).catch(function (e) { diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, e.message)); });
    }

    function apply(newSt) {
      var had = st ? new Set(st.files.map(function (f) { return f.path; })) : null;
      st = newSt;
      if (!st.repo) {
        head.replaceChildren(h('span', { class: 'muted' }, st.error || 'Not a git repository.'));
        list.replaceChildren(); setBusy(true); return;
      }
      counts[repo] = st.files.length;
      renderSwitcher();
      // keep the user's selection; new files start selected
      var next = new Set();
      st.files.forEach(function (f) { if (!had || !had.has(f.path) || picked.has(f.path)) next.add(f.path); });
      picked = next;
      allBox.checked = picked.size === st.files.length;
      renderHead();
      renderList();
      if (current && !st.files.some(function (f) { return f.path === current; })) {
        current = null;
        diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, 'Select a file to see its changes.'));
      }
    }

    function load() {
      return api('GET', 'git/' + encodeURIComponent(p.name) + '/status' + qs()).then(apply)
        .catch(function (e) { head.replaceChildren(h('span', { class: 'muted' }, e.message)); });
    }

    function discover() {
      api('GET', 'git/' + encodeURIComponent(p.name) + '/repos').then(function (d) {
        repos = d.repos || [];
        where = d.where;
        if (!repos.length) {
          head.replaceChildren(h('span', { class: 'muted' }, 'No git repository in this project' + (d.where === 'local' ? ' (on this machine)' : '') + '.'));
          if (d.gitSync === false) {
            banner.replaceChildren(h('div', null, 'The .git folders of this project are not synced to this machine. ',
              h('button', { class: 'btn sm', onclick: function () { md.close(); gitSyncOn(p); } }, 'Sync .git too')));
            banner.classList.remove('hidden');
          }
          setBusy(true);
          return;
        }
        Object.keys(d.statuses || {}).forEach(function (r) { counts[r] = (d.statuses[r].files || []).length; });
        // start with the first repository that has changes
        repo = repos.find(function (r) { return counts[r]; }) || repos[0];
        renderSwitcher();
        if (d.statuses && d.statuses[repo]) apply(d.statuses[repo]); else load();
      }).catch(function (e) { head.replaceChildren(h('span', { class: 'muted' }, e.message)); });
    }

    function action(a) {
      if (busy) return;
      setBusy(true, a === 'pull' ? 'pulling' : a === 'push' ? 'pushing' : 'fetching');
      gpost(a).then(function (d) {
        setBusy(false, a);
        apply(d.status);
        if (d.output === 'conflicts') toast('Pull stopped with conflicts — resolve them in VS Code', 'err', { timeout: 9000 });
        else toast(a === 'push' ? 'Pushed' : a === 'pull' ? 'Pulled' : 'Fetched', 'ok');
      }).catch(function (e) { setBusy(false, a); fail(e); });
    }

    function commit() {
      if (busy || !st || !picked.size) return;
      if (!msg.value.trim()) { msg.focus(); toast('Write a commit message', 'warn'); return; }
      var all = picked.size === st.files.length;
      setBusy(true, 'committing');
      gpost('commit', { message: msg.value, files: all ? null : Array.from(picked) }).then(function (d) {
        setBusy(false, 'commit');
        msg.value = '';
        apply(d.status);
        toast('Committed' + (st.upstream ? ' — Push to send it' : ''), 'ok');
      }).catch(function (e) { setBusy(false, 'commit'); fail(e); });
    }

    discover();
  }

  // ------------------------------------------------------------------ sidebar
  function renderProjects() {
    var root = $('#projects');
    if (!root) return;
    var f = S.filter.trim().toLowerCase();
    if (!S.projects.length) {
      root.replaceChildren(h('div', { class: 'side-note' },
        'No projects yet. Run ', h('code', null, 'simabworkspace add'), ' to sync one from the server, or ',
        h('button', { class: 'add-link', onclick: addManualProject }, 'add one by hand'), '.'));
      return;
    }
    var list = S.projects.filter(function (p) { return !f || p.name.toLowerCase().indexOf(f) >= 0; });
    root.replaceChildren.apply(root, list.map(projEl).concat([h('button', { class: 'add-link', style: 'margin:6px 2px', onclick: addManualProject }, '+ add project')]));
  }

  function projEl(p) {
    var open = S.open.has(p.name);
    var count = S.tabs.filter(function (t) { return t.info.spec.project === p.name && !t.gone; }).length;
    var sync = p.sync;
    var dot = sync ? sync.state : (p.localExists || p.remote ? 'ok' : '');
    var el = h('div', { class: 'proj' + (open ? ' open' : ''), style: '--pc:' + projectColor(p.name) });
    var more = h('button', { class: 'icon-btn', title: 'More', onclick: function (e) { e.stopPropagation(); projectMenu(p, more); } }, '⋯');
    var tip = [p.remote ? 'server: ' + p.remote : '', p.local ? 'local: ' + p.local : '',
      sync ? 'sync: ' + sync.label + (sync.conflicts ? ', ' + plural(sync.conflicts, 'conflict') : '') : ''].filter(Boolean).join('\n');
    el.append(h('div', { class: 'proj-head', title: tip, onclick: function () {
      if (S.open.has(p.name)) S.open.delete(p.name); else S.open.add(p.name);
      localStorage.setItem('sw-open', JSON.stringify(Array.from(S.open)));
      el.classList.toggle('open');
    } }, h('i', { class: 'dot ' + dot }), h('span', { class: 'proj-name' }, p.name), open ? null : gitBadges(p.name, true),
      sync && sync.conflicts ? h('span', { class: 'gb conflict', title: plural(sync.conflicts, 'sync conflict') + ' — click to resolve', onclick: function (e) { e.stopPropagation(); syncDetails(p); } }, '⇅' + sync.conflicts) : null,
      count ? h('span', { class: 'count' }, String(count)) : null, more));

    var pr = sync && sync.progress;
    if (pr) {
      el.append(h('div', { class: 'sbar', title: 'Syncing ' + pr.files + ' / ' + pr.totalFiles + ' files' + (pr.totalBytes ? ' · ' + fmtBytes(pr.bytes) + ' / ' + fmtBytes(pr.totalBytes) : '') },
        h('i', { style: 'width:' + pr.percent + '%' }), h('span', null, pr.percent + '% · ' + pr.files + '/' + pr.totalFiles + ' files' + (pr.totalBytes ? ' · ' + fmtBytes(pr.bytes) + ' of ' + fmtBytes(pr.totalBytes) : ''))));
    }
    var r = hasRemote(p);
    var body = h('div', { class: 'proj-body' });
    body.append(h('div', { class: 'acts' },
      h('button', { class: 'act', disabled: !r, title: 'Chat with Claude in this project (runs on the server)', onclick: function () { openSession({ kind: 'chat', project: p.name }); } }, 'Claude'),
      h('button', { class: 'act', disabled: !r && !p.localExists, title: r ? 'Terminal on the server, in the project' : 'Local terminal', onclick: function () { openTerminal(p); } }, 'Terminal'),
      h('button', { class: 'act', disabled: !r && !p.localExists, title: r ? 'VS Code connected to the server folder (Remote-SSH)' : 'VS Code, local folder', onclick: function () { openCode(p); } }, 'Code'),
      h('button', { class: 'act git', disabled: !r && !p.localExists, title: 'Changes, commit, pull, push', onclick: function () { gitDialog(p); } }, 'Git', gitBadges(p.name))));
    p.commands.forEach(function (c) {
      var t = runningCmd(p.name, c.id);
      body.append(h('div', { class: 'cmd' + (t ? ' running' : ''), title: c.cmd + (c.cwd ? '\nin ' + c.cwd : '') + (t ? '\nclick to stop' : ''),
        onclick: function () { runCommand(p, c); } },
        h('span', { class: 'ic' }, t ? '■' : '▶'), h('span', { class: 'nm' }, c.name),
        h('span', { class: 'tag' }, c.where === 'server' ? 'srv' : 'loc')));
    });
    if (!p.commands.length) body.append(h('button', { class: 'add-link', onclick: function () { editCommands(p); } }, '+ run command'));
    el.append(body);
    return el;
  }

  // ------------------------------------------------------------------ run command editor
  function editCommands(p) {
    var dirsId = 'dirs-' + Math.random().toString(36).slice(2);
    var dirs = h('datalist', { id: dirsId });
    api('GET', 'files/' + encodeURIComponent(p.name)).then(function (d) {
      dirs.replaceChildren.apply(dirs, (d.files || []).filter(function (f) { return /\/$/.test(f); }).slice(0, 3000)
        .map(function (f) { return h('option', { value: f.replace(/\/$/, '') }); }));
    }).catch(function () {});
    var list = h('div');
    function row(c) {
      var name = h('input', { class: 'inp', value: c.name || '', placeholder: 'Name, e.g. web' });
      var where = h('select', { class: 'inp' }, h('option', { value: 'local' }, 'Local'), h('option', { value: 'server' }, 'Server'));
      where.value = c.where || 'local';
      var cmd = h('textarea', { class: 'inp mono', rows: 2, value: c.cmd || '', placeholder: 'npm start', spellcheck: 'false' });
      var cwd = h('input', { class: 'inp mono', value: c.cwd || '', placeholder: 'folder (empty = project root)', list: dirsId });
      var url = h('input', { class: 'inp mono', value: c.url || '', placeholder: 'URL to open (optional)' });
      var box = h('div', { class: 'cmd-edit' },
        h('div', { class: 'top' }, name, where,
          h('button', { class: 'icon-btn', title: 'Move up', onclick: function () { var s = box.previousElementSibling; if (s) list.insertBefore(box, s); } }, '↑'),
          h('button', { class: 'icon-btn', title: 'Remove', onclick: function () { box.remove(); } }, '✕')),
        h('div', { class: 'field', style: 'margin-bottom:6px' }, cmd),
        h('div', { class: 'row' }, cwd, url));
      box._get = function () { return { id: c.id, name: name.value, where: where.value, cmd: cmd.value, url: url.value, cwd: cwd.value }; };
      return box;
    }
    p.commands.forEach(function (c) { list.append(row(Object.assign({}, c))); });
    if (!p.commands.length) list.append(row({ where: 'local' }));
    var sugBtn = h('button', { class: 'btn', onclick: function () {
      api('GET', 'suggest/' + encodeURIComponent(p.name)).then(function (d) {
        if (!d.suggestions.length) { toast('Nothing to suggest (reads the local copy of the project)', 'warn'); return; }
        menuAt(sugBtn, d.suggestions.map(function (s) { return { label: s.name, k: s.where, action: function () { list.append(row(s)); } }; }));
      }).catch(fail);
    } }, 'Suggest');
    var md = openModal({
      title: 'Run commands — ' + p.name,
      body: [h('div', { class: 'muted', style: 'margin-bottom:10px;font-size:12px' }, 'Each command becomes a ▶ under the project and runs in its own tab. Local runs on this machine (synced folder), Server runs over SSH.'), dirs, list],
      foot: [h('button', { class: 'btn left', onclick: function () { list.append(row({ where: 'local' })); } }, '+ Add'), sugBtn,
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: function () {
          var cmds = Array.prototype.map.call(list.children, function (b) { return b._get(); }).filter(function (c) { return c.name.trim() || c.cmd.trim(); });
          api('POST', 'commands/' + encodeURIComponent(p.name), { commands: cmds }).then(function () { md.close(); refreshProjects(); }).catch(fail);
        } }, 'Save')]
    });
  }

  // Add project — all in the window: download from the server, upload from this machine, or server-only
  function addManualProject() {
    var mode = 'download', folders = null, pickedRemote = '';
    var name = h('input', { class: 'inp mono', placeholder: 'empty = folder name' });
    var local = h('input', { class: 'inp mono', placeholder: '~/projects/my-project' });
    var remote = h('input', { class: 'inp mono', placeholder: '~/my-project' });
    var git = h('input', { type: 'checkbox', checked: true });
    var errBox = h('div', { class: 'add-err hidden' });
    var list = h('div', { class: 'flist' }, h('div', { class: 'muted', style: 'padding:10px' }, 'Loading folders on the server…'));
    var filter = h('input', { class: 'inp', placeholder: 'Filter folders', autocomplete: 'off' });
    filter.addEventListener('input', renderFolders);

    function renderFolders() {
      if (!folders) return;
      var q = filter.value.trim().toLowerCase();
      var shown = folders.filter(function (f) { return !q || f.name.toLowerCase().indexOf(q) >= 0; });
      if (!shown.length) { list.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, 'No folders')); return; }
      list.replaceChildren.apply(list, shown.map(function (f) {
        return h('button', { class: 'frow' + (f.path === pickedRemote ? ' on' : ''), disabled: !!f.project,
          title: f.project ? 'Already synced as ' + f.project : f.path,
          onclick: function () {
            pickedRemote = f.path;
            if (!local.value || local.dataset.auto) { local.value = '~/' + f.name; local.dataset.auto = '1'; }
            renderFolders();
          } },
          h('span', { class: 'fname mono' }, f.name),
          f.git ? h('span', { class: 'ftag' }, f.git === 'repo' ? 'git' : 'git inside') : null,
          f.project ? h('span', { class: 'ftag done' }, 'synced') : null);
      }));
    }
    function loadFolders() {
      if (folders) { renderFolders(); return; }
      api('GET', 'server-folders').then(function (d) { folders = d.folders; renderFolders(); })
        .catch(function (e) { list.replaceChildren(h('div', { class: 'add-err' }, e.message)); });
    }
    local.addEventListener('input', function () { delete local.dataset.auto; });

    var gitRow = function () { return h('label', { class: 'check' }, git, h('span', null, 'Sync the .git folders too', h('small', null, 'So Git works the same on both sides.'))); };
    var forms = {
      download: function () {
        loadFolders();
        return [h('div', { class: 'field' }, h('label', null, 'Folder on the server'), filter, list),
          h('div', { class: 'row' },
            h('div', { class: 'field' }, h('label', null, 'Put it on this machine at'), local),
            h('div', { class: 'field' }, h('label', null, 'Name'), name)), gitRow()];
      },
      upload: function () {
        return [h('div', { class: 'field' }, h('label', null, 'Folder on this machine'), local),
          h('div', { class: 'row' },
            h('div', { class: 'field' }, h('label', null, 'Folder on the server'), remote),
            h('div', { class: 'field' }, h('label', null, 'Name'), name)),
          h('div', { class: 'muted', style: 'font-size:11.5px;margin:-6px 0 10px' }, 'Empty server folder = ~/<folder name>. It must be new or empty.'), gitRow()];
      },
      list: function () {
        return [h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:10px' }, 'Work on a project right on the server (Claude, terminal, Git, run commands) without copying it to this machine.'),
          h('div', { class: 'row' },
            h('div', { class: 'field' }, h('label', null, 'Folder on the server'), remote),
            h('div', { class: 'field' }, h('label', null, 'Name'), name)),
          h('div', { class: 'field' }, h('label', null, 'Folder on this machine (optional)'), local)];
      }
    };
    var area = h('div');
    var go = h('button', { class: 'btn primary', onclick: submit });
    var modes = [['download', 'From the server', 'Download, then keep in sync'], ['upload', 'From this machine', 'Upload, then keep in sync'], ['list', 'Server only', 'No sync, work on the server']];
    var seg = h('div', { class: 'seg' });
    function show(m) {
      mode = m;
      errBox.classList.add('hidden');
      seg.replaceChildren.apply(seg, modes.map(function (x) {
        return h('button', { class: 'segb' + (x[0] === m ? ' on' : ''), onclick: function () { show(x[0]); } }, h('b', null, x[1]), h('small', null, x[2]));
      }));
      area.replaceChildren.apply(area, forms[m]());
      go.textContent = { download: 'Download & sync', upload: 'Upload & sync', list: 'Add' }[m];
    }
    function fail2(e) { go.disabled = false; go.textContent = { download: 'Download & sync', upload: 'Upload & sync', list: 'Add' }[mode]; errBox.textContent = e.message; errBox.classList.remove('hidden'); }
    function submit() {
      errBox.classList.add('hidden');
      if (mode === 'list') {
        if (!name.value.trim()) { name.focus(); return; }
        api('POST', 'manual', { name: name.value, local: local.value, remote: remote.value }).then(function () { md.close(); refreshProjects(); }).catch(fail2);
        return;
      }
      if (mode === 'download' && !pickedRemote) { errBox.textContent = 'Pick a folder on the server.'; errBox.classList.remove('hidden'); return; }
      if (mode === 'upload' && !local.value.trim()) { local.focus(); errBox.textContent = 'Which folder on this machine?'; errBox.classList.remove('hidden'); return; }
      go.disabled = true;
      go.textContent = 'Setting up…';
      api('POST', 'add-project', { mode: mode, remote: mode === 'download' ? pickedRemote : remote.value.trim(), local: local.value.trim(), name: name.value.trim(), git: git.checked })
        .then(function (d) {
          md.close();
          if (d.project) { S.open.add(d.project); localStorage.setItem('sw-open', JSON.stringify(Array.from(S.open))); }
          toast((d.project || 'Project') + ': ' + (mode === 'download' ? 'downloading' : 'uploading') + ' in the background — progress shows under the project', 'ok', { timeout: 7000 });
          refreshProjects();
        }).catch(fail2);
    }
    var md = openModal({ title: 'Add project', body: [seg, h('div', { style: 'height:12px' }), area, errBox],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), go] });
    show('download');
  }

  // ------------------------------------------------------------------ settings
  function settingsDialog() {
    var s = S.settings, f = {};
    function inp(k, attrs) { f[k] = h('input', Object.assign({ class: 'inp', value: s[k] === undefined ? '' : String(s[k]) }, attrs || {})); return f[k]; }
    function sel(k, opts) { f[k] = h('select', { class: 'inp' }, opts.map(function (o) { return h('option', { value: o[0] }, o[1]); })); f[k].value = s[k]; return f[k]; }
    function chk(k, label, help) { f[k] = h('input', { type: 'checkbox', checked: !!s[k] }); return h('label', { class: 'check' }, f[k], h('span', null, label, help ? h('small', null, help) : null)); }
    function field(label, el) { return h('div', { class: 'field' }, h('label', null, label), el); }

    // --- server connection (what `simabworkspace setup` asked in the terminal)
    var cUser = h('input', { class: 'inp mono', placeholder: 'user' });
    var cHost = h('input', { class: 'inp mono', placeholder: 'server.example.com' });
    var cPort = h('input', { class: 'inp mono', placeholder: '22' });
    var cKey = h('select', { class: 'inp mono' });
    var connMsg = h('div', { class: 'conn-msg muted' }, 'Loading…');
    var pw = h('input', { class: 'inp', type: 'password', placeholder: 'Server password (used once, not saved)', autocomplete: 'off' });
    var pwRow = h('div', { class: 'row hidden', style: 'align-items:center;margin-top:8px' }, pw,
      h('button', { class: 'btn', style: 'flex:none', onclick: installKey }, 'Install key'));
    var autostart = h('input', { type: 'checkbox' });
    var daemonInfo = h('span', { class: 'muted' }, '');

    function fillKeys(keys, cur) {
      cKey.replaceChildren.apply(cKey, [h('option', { value: '' }, 'default ssh keys')].concat(keys.map(function (k) {
        return h('option', { value: k.path }, k.path.replace(/^.*\/\.ssh\//, '~/.ssh/') + '  ' + k.type + (k.comment ? '  ' + k.comment : ''));
      })));
      cKey.value = cur || '';
    }
    function showStatus(st) {
      if (st.ssh === 'ok') { connMsg.className = 'conn-msg ok'; connMsg.textContent = '● Connected — password-less login works.'; pwRow.classList.add('hidden'); }
      else if (st.ssh === 'unconfigured') { connMsg.className = 'conn-msg warn'; connMsg.textContent = 'Fill in the server and save.'; }
      else {
        connMsg.className = 'conn-msg err';
        connMsg.textContent = '● Cannot log in without a password' + (st.sshError ? ' — ' + st.sshError : '') + '. Choose or create a key, then install it on the server with your password (once).';
        pwRow.classList.remove('hidden');
      }
    }
    api('GET', 'connection').then(function (c) {
      cUser.value = c.user; cHost.value = c.host; cPort.value = c.port;
      fillKeys(c.keys, c.key);
      autostart.checked = c.autostart;
      daemonInfo.textContent = c.daemon ? 'running' : 'stopped';
      api('GET', 'status?force=1').then(showStatus);
    }).catch(fail);
    function saveConn() {
      connMsg.className = 'conn-msg muted'; connMsg.textContent = 'Testing…';
      return api('POST', 'connection', { user: cUser.value, host: cHost.value, port: cPort.value, key: cKey.value })
        .then(function (st) { showStatus(st); return api('GET', 'state').then(function (x) { S.server = x.server; refreshStatus(false); renderProjects(); }); })
        .catch(function (e) { connMsg.className = 'conn-msg err'; connMsg.textContent = e.message; });
    }
    function createKey() {
      api('POST', 'ssh-key/create').then(function (d) { fillKeys(d.keys, d.key); toast('Key created: ' + d.key, 'ok'); saveConn(); }).catch(fail);
    }
    function installKey() {
      if (!pw.value) { pw.focus(); return; }
      connMsg.className = 'conn-msg muted'; connMsg.textContent = 'Installing the key on the server…';
      api('POST', 'connection', { user: cUser.value, host: cHost.value, port: cPort.value, key: cKey.value })
        .then(function () { return api('POST', 'ssh-key/install', { password: pw.value }); })
        .then(function (st) { pw.value = ''; showStatus(st); if (st.ssh === 'ok') toast('Key installed — no password needed from now on', 'ok'); })
        .catch(function (e) { pw.value = ''; connMsg.className = 'conn-msg err'; connMsg.textContent = e.message; });
    }
    function service(action) {
      api('POST', 'sync-service/' + action).then(function (d) { daemonInfo.textContent = d.daemon ? 'running' : 'stopped'; refreshProjects(); refreshStatus(true); }).catch(fail);
    }
    autostart.addEventListener('change', function () { api('POST', 'autostart', { on: autostart.checked }).catch(fail); });

    var md = openModal({
      title: 'Settings',
      body: [
        h('div', { class: 'sec' }, 'Server'),
        h('div', { class: 'row' }, field('User', cUser), field('Address', cHost), h('div', { style: 'flex:0 0 90px' }, field('SSH port', cPort))),
        h('div', { class: 'row', style: 'align-items:flex-end' }, field('SSH key', cKey),
          h('div', { class: 'field', style: 'flex:none;display:flex;gap:6px' },
            h('button', { class: 'btn', onclick: createKey, title: 'Create a new ed25519 key in ~/.ssh' }, 'New key'),
            h('button', { class: 'btn', onclick: saveConn }, 'Save & test'))),
        connMsg, pwRow,
        chk('serverPersist', 'Keep server terminals alive (tmux)', 'Shells survive a dropped connection; reopening attaches to the same one.'),

        h('div', { class: 'sec' }, 'Sync'),
        h('div', { class: 'svc' }, h('span', null, 'Sync service: ', daemonInfo),
          h('button', { class: 'btn sm', onclick: function () { service('pause-all'); } }, 'Pause all'),
          h('button', { class: 'btn sm', onclick: function () { service('resume-all'); } }, 'Resume all'),
          h('button', { class: 'btn sm', onclick: function () { service('stop'); } }, 'Stop service'),
          h('button', { class: 'btn sm', onclick: function () { service('start'); } }, 'Start service')),
        h('label', { class: 'check' }, autostart, h('span', null, 'Start syncing automatically', h('small', null, 'When a terminal opens on this machine (adds a line to ~/.bashrc).'))),

        h('div', { class: 'sec' }, 'This machine'),
        prereqBox(),
        h('div', { class: 'svc', style: 'margin-top:8px' }, h('span', { class: 'muted' }, 'Open this window from your app menu / Start menu — no terminal needed.'),
          h('button', { class: 'btn sm', onclick: function () {
            api('POST', 'shortcut').then(function (d) { toast('Added to: ' + d.where.join(', '), 'ok'); }).catch(fail);
          } }, 'Add shortcut')),

        h('div', { class: 'sec' }, 'Claude'),
        h('div', { class: 'row' },
          field('Command', inp('claudeCmd', { class: 'inp mono' })),
          field('Default mode', sel('chatMode', [['default', 'Ask before edits'], ['acceptEdits', 'Auto-accept edits'], ['plan', 'Plan'], ['auto', 'Auto']]))),
        h('div', { class: 'sec' }, 'Terminal'),
        h('div', { class: 'row' },
          field('Font size', inp('fontSize', { type: 'number', min: 9, max: 32, class: 'inp mono' })),
          field('Editor command', inp('editorCmd', { class: 'inp mono' }))),
        chk('copyOnSelect', 'Copy on select'),
        chk('persianView', 'Show the Persian reading view automatically', 'When Persian text appears in a terminal.'),
        chk('bellNotify', 'Desktop notification when a tab needs you'),
        h('div', { class: 'sec' }, 'Uninstall'),
        h('div', { class: 'svc' }, h('span', { class: 'muted' }, 'Stops all syncing (files stay on both sides) and removes settings and this app.'),
          h('button', { class: 'btn sm danger', onclick: uninstallDialog }, 'Uninstall…'))
      ],
      foot: [h('span', { class: 'left muted mono' }, 'v' + S.version),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: function () {
          var patch = {};
          Object.keys(f).forEach(function (k) {
            var el = f[k];
            if (k === 'persianView') patch[k] = el.checked ? 'auto' : 'off';
            else if (el.type === 'checkbox') patch[k] = el.checked;
            else if (el.type === 'number') patch[k] = Number(el.value) || 13;
            else patch[k] = el.value;
          });
          api('POST', 'settings', patch).then(function (d) {
            S.settings = d.settings;
            applySettings();
            md.close();
            if (S.settings.bellNotify && window.Notification && Notification.permission === 'default') Notification.requestPermission();
          }).catch(fail);
        } }, 'Save')]
    });
    f.persianView.checked = s.persianView === 'auto';
  }

  function applySettings() {
    S.tabs.forEach(function (t) { if (t.term) t.term.options.fontSize = S.settings.fontSize; });
    fitAll();
    renderProjects();
  }


  // ------------------------------------------------------------------ this machine: prerequisites
  function prereqBox() {
    var box = h('div', { class: 'prereqs' }, h('div', { class: 'muted' }, 'Checking…'));
    var pw = h('input', { class: 'inp', type: 'password', placeholder: 'Password of this computer (sudo) — used once, not saved', autocomplete: 'off' });
    var pwRow = h('div', { class: 'row hidden', style: 'margin-top:6px' }, pw);
    var wrap = h('div', null, box, pwRow);
    function load() {
      api('GET', 'prereqs').then(function (d) {
        var rows = d.items.map(function (it) {
          var btn = it.ok ? null : h('button', { class: 'btn sm', onclick: function () { install(it.name, btn); } }, 'Install');
          return h('div', { class: 'preq' + (it.ok ? ' ok' : ' miss') },
            h('span', { class: 'pk' }, it.ok ? '✓' : '✗'), h('b', { class: 'mono' }, it.name),
            h('span', { class: 'muted' }, it.ok ? (it.version || 'installed') : it.why + (it.sudo ? ' · needs sudo' : '')), btn);
        });
        rows.push(h('div', { class: 'preq' + (d.docker ? ' ok' : '') },
          h('span', { class: 'pk' }, d.docker ? '✓' : '–'), h('b', { class: 'mono' }, 'docker'),
          h('span', { class: 'muted' }, d.docker ? 'docker compose works' : 'optional — to run projects here' + (d.wsl ? ': Docker Desktop → Settings → Resources → WSL integration' : ': docs.docker.com/engine/install'))));
        box.replaceChildren.apply(box, rows);
        pwRow.classList.toggle('hidden', d.sudoWithoutPassword || d.items.every(function (it) { return it.ok || !it.sudo; }));
      }).catch(function (e) { box.replaceChildren(h('div', { class: 'add-err' }, e.message)); });
    }
    function install(name, btn) {
      btn.disabled = true; btn.textContent = 'Installing…';
      api('POST', 'prereqs/' + name, { password: pw.value }).then(function () {
        toast(name + ' installed', 'ok'); pw.value = ''; load(); refreshStatus(true);
      }).catch(function (e) {
        btn.disabled = false; btn.textContent = 'Install';
        if (e.status === 428) { pwRow.classList.remove('hidden'); pw.focus(); toast('Enter this computer’s password, then press Install again', 'warn'); }
        else fail(e);
      });
    }
    load();
    return wrap;
  }

  function uninstallDialog() {
    var typed = h('input', { class: 'inp mono', placeholder: 'type: uninstall', autocomplete: 'off' });
    var go = h('button', { class: 'btn danger', disabled: true, onclick: function () {
      api('POST', 'uninstall', { confirm: 'uninstall' }).then(function () {
        document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' },
          h('p', { class: 'empty-title' }, 'Uninstalled'), h('p', { class: 'muted' }, 'Syncing stopped; your files are still on this machine and on the server. You can close this window.'))));
      }).catch(fail);
    } }, 'Uninstall');
    typed.addEventListener('input', function () { go.disabled = typed.value.trim() !== 'uninstall'; });
    var md = openModal({ title: 'Uninstall Simab Workspace', size: 'sm',
      body: [h('div', { style: 'font-size:12.5px;margin-bottom:10px' }, 'Stops syncing every project (files stay on both sides), removes the settings, the window and the simabworkspace command. Mutagen itself stays.'), typed],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), go] });
  }

  // ------------------------------------------------------------------ move a synced project
  function moveDialog(p) {
    var local = h('input', { class: 'inp mono', value: p.local || '' });
    var remote = h('input', { class: 'inp mono', value: p.remote || '' });
    var errBox = h('div', { class: 'add-err hidden' });
    var go = h('button', { class: 'btn primary', onclick: function () {
      var nl = local.value.trim(), nr = remote.value.trim();
      if ((!nl || nl === p.local) && (!nr || nr === p.remote)) { errBox.textContent = 'Change at least one folder.'; errBox.classList.remove('hidden'); return; }
      go.disabled = true; go.textContent = 'Moving…'; errBox.classList.add('hidden');
      api('POST', 'move/' + encodeURIComponent(p.name), { local: nl !== p.local ? nl : '', remote: nr !== p.remote ? nr : '' }).then(function () {
        md.close(); toast(p.name + ': folders moved — syncing from the new places', 'ok', { timeout: 7000 }); refreshProjects();
      }).catch(function (e) { go.disabled = false; go.textContent = 'Move'; errBox.textContent = e.message; errBox.classList.remove('hidden'); });
    } }, 'Move');
    var md = openModal({ title: 'Move folders — ' + p.name,
      body: [h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:12px' },
        'Moves the project to new folders and syncs from there. A new folder must not exist yet or be empty. Stop anything running from this project first (e.g. its docker compose) — it keeps pointing at the old folder.'),
        h('div', { class: 'field' }, h('label', null, 'Folder on this machine'), local),
        h('div', { class: 'field' }, h('label', null, 'Folder on the server'), remote), errBox],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), go] });
  }


  function gitSyncOn(p) {
    api('POST', 'git-sync/' + encodeURIComponent(p.name), { on: true }).then(function () {
      toast(p.name + ': syncing the .git folders too — this takes a moment', 'ok', { timeout: 6000 });
      setTimeout(refreshProjects, 4000);
    }).catch(fail);
  }

  // ------------------------------------------------------------------ sync details + conflicts
  function syncDetails(p) {
    var body = h('div', null, h('div', { class: 'muted' }, 'Loading…'));
    var md = openModal({ title: 'Sync — ' + p.name, size: 'wide', body: body,
      foot: [h('button', { class: 'btn', onclick: function () { syncAction(p, 'flush'); setTimeout(load, 1500); } }, 'Sync now'),
        h('span', { class: 'grow' }), h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    function kv(k, v, cls) { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v mono ' + (cls || '') }, v)); }
    function resolve(c, keep, btn) {
      btn.disabled = true;
      api('POST', 'sync/' + encodeURIComponent(p.name) + '/resolve', { path: c.path, keep: keep }).then(function () {
        toast(c.path + ': kept the ' + (keep === 'local' ? 'local' : 'server') + ' version', 'ok');
        setTimeout(load, 1500);
      }).catch(function (e) { btn.disabled = false; fail(e); });
    }
    function change(list) {
      if (!list.length) return h('span', { class: 'muted' }, 'unchanged');
      return list.slice(0, 3).map(function (c) { return h('div', { class: 'mono' }, (c.path || '(this path)') + ': ' + c.was + ' → ' + c.now); });
    }
    function load() {
      api('GET', 'sync/' + encodeURIComponent(p.name) + '/details').then(function (d) {
        var rows = [
          h('div', { class: 'kvs' },
            kv('status', d.label + (d.paused ? '' : ' (' + d.status + ')'), d.state),
            kv('this machine', d.local), kv('server', d.server + ':' + d.remote),
            kv('.git folders', d.git ? 'synced' : 'not synced'),
            kv('sync cycles', String(d.cycles)),
            kv('not synced', (d.ignores || []).join(', ') || '—'),
            d.error ? kv('last error', d.error, 'err') : null)];
        if (d.progress) rows.push(h('div', { class: 'sbar', style: 'margin:10px 0' }, h('i', { style: 'width:' + d.progress.percent + '%' }),
          h('span', null, d.progress.percent + '% · ' + d.progress.files + '/' + d.progress.totalFiles + ' files')));
        rows.push(h('div', { class: 'sec' }, d.conflicts.length ? plural(d.conflicts.length, 'conflict') + ' — the same file changed on both sides' : 'No conflicts'));
        if (d.conflicts.length) {
          rows.push(h('div', { class: 'muted', style: 'font-size:12px;margin-bottom:8px' }, 'Nothing was overwritten. Pick which version to keep — it is copied over the other one. For folders, or to merge by hand, open VS Code.'));
          rows.push(h('div', { class: 'conflicts' }, d.conflicts.map(function (c) {
            var b1 = h('button', { class: 'btn sm', onclick: function () { resolve(c, 'local', b1); } }, 'Keep this machine’s');
            var b2 = h('button', { class: 'btn sm', onclick: function () { resolve(c, 'server', b2); } }, 'Keep the server’s');
            return h('div', { class: 'conf' },
              h('div', { class: 'cpath mono' }, c.path || '(project root)'),
              h('div', { class: 'csides' }, h('div', null, h('b', null, 'this machine'), change(c.local)), h('div', null, h('b', null, 'server'), change(c.server))),
              h('div', { class: 'cacts' }, b1, b2, h('button', { class: 'btn sm', onclick: function () { openCode(p); } }, 'Open in VS Code')));
          })));
        }
        body.replaceChildren.apply(body, rows);
      }).catch(function (e) { body.replaceChildren(h('div', { class: 'add-err' }, e.message)); });
    }
    load();
  }

  // ------------------------------------------------------------------ palette
  function paletteItems() {
    var items = [];
    S.tabs.forEach(function (t) { items.push({ label: t.info.title, grp: 'tab', action: function () { activate(t); } }); });
    S.projects.forEach(function (p) {
      var n = p.name;
      if (hasRemote(p)) {
        items.push({ label: n + ': Claude', grp: n, action: function () { openSession({ kind: 'chat', project: n }); } });
        items.push({ label: n + ': Claude, continue last chat', grp: n, action: function () { resumeLatest(n); } });
        items.push({ label: n + ': Claude, past chats…', grp: n, action: function () { pickClaudeSession(n, function (sid, title) { openSession({ kind: 'chat', project: n, resume: sid, title: 'Claude · ' + title }); }); } });
        items.push({ label: n + ': server terminal', grp: n, action: function () { openSession({ kind: 'shell', where: 'server', project: n }); } });
        items.push({ label: n + ': VS Code on server', grp: n, action: function () { openEditor(p, 'server'); } });
      }
      if (p.localExists) {
        items.push({ label: n + ': local terminal', grp: n, action: function () { openSession({ kind: 'shell', where: 'local', project: n }); } });
        items.push({ label: n + ': VS Code, local folder', grp: n, action: function () { openEditor(p, 'local'); } });
      }
      p.commands.forEach(function (c) {
        var running = !!runningCmd(n, c.id);
        items.push({ label: n + ': ' + (running ? 'stop ' : 'run ') + c.name, grp: n, action: function () { runCommand(p, c); } });
      });
      items.push({ label: n + ': git — changes, commit, pull, push', grp: n, action: function () { gitDialog(p); } });
      items.push({ label: n + ': edit run commands', grp: n, action: function () { editCommands(p); } });
    });
    items.push({ label: 'Server terminal (home)', grp: 'app', action: function () { openSession({ kind: 'shell', where: 'server' }); } });
    items.push({ label: 'Local terminal (home)', grp: 'app', action: function () { openSession({ kind: 'shell', where: 'local' }); } });
    items.push({ label: 'Grid: all tabs side by side', grp: 'layout', action: gridAll });
    items.push({ label: 'Split right', grp: 'layout', action: function () { splitGroup(L.focused, 'right'); } });
    items.push({ label: 'Split down', grp: 'layout', action: function () { splitGroup(L.focused, 'bottom'); } });
    items.push({ label: 'Single section', grp: 'layout', action: mergeAll });
    items.push({ label: 'Toggle sidebar', grp: 'layout', action: toggleWorkMode });
    items.push({ label: 'Add project…', grp: 'app', action: addManualProject });
    items.push({ label: 'History — what you did', grp: 'app', action: function () { historyDialog(); } });
    items.push({ label: 'Settings', grp: 'app', action: settingsDialog });
    items.push({ label: 'Quit (stop every terminal)', grp: 'app', action: quitApp });
    return items;
  }

  function openPalette() {
    if (modalStack.length) return;
    var all = paletteItems();
    var inp = h('input', { placeholder: 'Type a project, tab or command…', autofocus: true, dir: 'auto' });
    var res = h('div', { class: 'results' });
    var shown = [], sel = 0;
    var overlay = h('div', { class: 'overlay' }, h('div', { class: 'modal palette' }, inp, res));
    function render() {
      var q = inp.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      shown = all.filter(function (it) {
        var hay = (it.label + ' ' + it.grp).toLowerCase();
        return q.every(function (w) { return hay.indexOf(w) >= 0; });
      }).slice(0, 80);
      sel = Math.min(sel, Math.max(0, shown.length - 1));
      res.replaceChildren.apply(res, shown.map(function (it, i) {
        return h('div', { class: 'res' + (i === sel ? ' sel' : ''), onmousemove: function () { if (sel !== i) { sel = i; mark(); } }, onclick: function () { run(i); } },
          it.label, h('span', { class: 'grp' }, it.grp));
      }));
      if (!shown.length) res.append(h('div', { class: 'muted', style: 'padding:10px' }, 'Nothing found'));
    }
    function mark() {
      Array.prototype.forEach.call(res.children, function (c, i) { c.classList.toggle('sel', i === sel); });
      if (res.children[sel]) res.children[sel].scrollIntoView({ block: 'nearest' });
    }
    function close() { var i = modalStack.indexOf(rec); if (i >= 0) modalStack.splice(i, 1); overlay.remove(); }
    function run(i) { var it = shown[i]; close(); if (it) it.action(); else if (S.active) S.active.focus(); }
    var rec = { close: function () { close(); if (S.active) S.active.focus(); } };
    inp.addEventListener('input', function () { sel = 0; render(); });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); mark(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); mark(); }
      else if (e.key === 'Enter') { e.preventDefault(); run(sel); }
    });
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) rec.close(); });
    modalStack.push(rec);
    $('#modal-root').append(overlay);
    render();
    setTimeout(function () { inp.focus(); }, 20);
  }

  function newTabMenu(anchor) {
    var items = [];
    var cur = S.active && S.active.info.spec.project ? project(S.active.info.spec.project) : project(L.lastProject || '');
    if (cur) {
      items.push({ header: cur.name });
      if (hasRemote(cur)) items.push({ label: 'Claude', action: function () { openSession({ kind: 'chat', project: cur.name }); } });
      if (hasRemote(cur)) items.push({ label: 'Server terminal', action: function () { openSession({ kind: 'shell', where: 'server', project: cur.name }); } });
      if (cur.localExists) items.push({ label: 'Local terminal', action: function () { openSession({ kind: 'shell', where: 'local', project: cur.name }); } });
    }
    items.push({ header: 'Home' });
    items.push({ label: 'Server terminal', disabled: !serverReady(), action: function () { openSession({ kind: 'shell', where: 'server' }); } });
    items.push({ label: 'Local terminal', action: function () { openSession({ kind: 'shell', where: 'local' }); } });
    items.push('-');
    items.push({ label: 'Everything…', k: 'Ctrl+Shift+P', action: openPalette });
    if (anchor) menuAt(anchor, items);
    else { var r = (L.focused ? L.focused.bar : $('#layout')).getBoundingClientRect(); showMenu(r.left + 8, r.bottom + 4, items); }
  }

  // ------------------------------------------------------------------ keyboard
  function isGlobalShortcut(e) {
    if (e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyP') return true;
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      if (/^Digit[1-9]$/.test(e.code)) return true;
      if (['KeyT', 'KeyW', 'KeyF', 'KeyZ', 'KeyG', 'KeyM', 'Backslash', 'Minus', 'PageUp', 'PageDown'].indexOf(e.code) >= 0) return true;
    }
    return false;
  }

  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (menuEl) { closeMenu(); e.preventDefault(); return; }
      if (modalStack.length) { modalStack[modalStack.length - 1].close(); e.preventDefault(); return; }
    }
    if (!isGlobalShortcut(e)) return;
    if (modalStack.length && !(e.ctrlKey && e.code === 'KeyP')) return;
    e.preventDefault();
    e.stopPropagation();
    var gt = L.focused ? L.focused.tabs : S.tabs;
    var idx = gt.indexOf(S.active);
    if (e.code === 'KeyP') openPalette();
    else if (e.code === 'Backslash') splitGroup(L.focused, 'right');
    else if (e.code === 'Minus') splitGroup(L.focused, 'bottom');
    else if (e.code === 'KeyZ') toggleZoom(L.focused);
    else if (e.code === 'KeyM') toggleWorkMode();
    else if (e.code === 'KeyG') { if (L.groups.length > 1) focusGroup(L.groups[(L.groups.indexOf(L.focused) + 1) % L.groups.length]); }
    else if (e.code === 'KeyT') newTabMenu();
    else if (e.code === 'KeyW') { if (S.active) closeTab(S.active); }
    else if (e.code === 'KeyF') { if (S.active) S.active.toggleComposer(); }
    else if (e.code === 'PageUp') { if (gt.length) activate(gt[(idx - 1 + gt.length) % gt.length]); }
    else if (e.code === 'PageDown') { if (gt.length) activate(gt[(idx + 1) % gt.length]); }
    else {
      var n = Number(e.code.slice(5)) - 1;
      if (e.code === 'Digit9') n = gt.length - 1;
      if (gt[n]) activate(gt[n]);
    }
  }, true);

  // ------------------------------------------------------------------ status
  function setDot(id, cls, title) { var el = $(id); el.className = 'dot ' + cls; el.title = title; }

  function refreshStatus(force) {
    return api('GET', 'status' + (force ? '?force=1' : '')).then(function (st) {
      S.status = st;
      var srv = S.server.configured ? S.server.user + '@' + S.server.host + ':' + S.server.port : '';
      if (st.ssh === 'ok') setDot('#st-ssh', 'ok', 'Server: connected (' + srv + ')');
      else if (st.ssh === 'err') setDot('#st-ssh', 'err', 'Server: cannot connect — ' + (st.sshError || ''));
      else if (st.ssh === 'unconfigured') setDot('#st-ssh', 'warn', 'Server: not configured — run simabworkspace setup');
      else setDot('#st-ssh', 'busy', 'Server: checking…');
      var synced = S.projects.filter(function (p) { return p.source === 'mutagen'; });
      var bad = synced.filter(function (p) { return p.sync && (p.sync.state === 'err' || p.sync.state === 'warn'); }).length;
      if (!st.daemon) setDot('#st-sync', 'warn', 'Sync: stopped — run simabworkspace reconnect');
      else if (bad) setDot('#st-sync', 'err', 'Sync: ' + plural(bad, 'project') + ' with problems');
      else setDot('#st-sync', 'ok', 'Sync: ' + plural(synced.length, 'project') + ' in sync');
    }).catch(function () { setDot('#st-ssh', '', 'Server: unknown'); });
  }

  function refreshProjects() {
    return api('GET', 'projects').then(function (d) {
      var key = JSON.stringify(d.projects);
      if (key !== S.projectsKey) {
        S.projectsKey = key;
        var wasMoving = S.moving || {};
        S.moving = {};
        d.projects.forEach(function (p) {
          var pr = p.sync && p.sync.progress;
          if (pr) S.moving[p.name] = true;
          else if (wasMoving[p.name] && p.sync && p.sync.state === 'ok') {
            toast(p.name + ': sync finished — everything is copied', 'ok', { timeout: 8000 });
            notify('Sync finished', p.name);
          }
        });
        S.projects = d.projects;
        renderProjects();
        repaintTabs();
        refreshStatus(false);
      }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ empty state
  function renderEmpty() {
    var box = $('#restore-box');
    var last = S.lastTabs || [];
    if (!S.tabs.length && last.length) {
      box.classList.remove('hidden');
      box.replaceChildren(h('button', { class: 'btn', title: last.map(function (t) { return t.title; }).join('\n'), onclick: function () { restoreTabs(last); } },
        'Reopen last session · ' + plural(last.length, 'tab')));
    } else box.classList.add('hidden');
  }

  function restoreTabs(list) {
    $('#empty').classList.add('hidden');
    var chain = Promise.resolve();
    list.forEach(function (sp, i) {
      chain = chain.then(function () {
        var spec = { kind: sp.kind, where: sp.where, project: sp.project, cmdId: sp.cmdId, title: sp.title, claudeMode: sp.kind === 'claude' ? 'continue' : 'new' };
        if ((sp.kind === 'chat' || sp.kind === 'claude') && sp.resume) spec.resume = sp.resume;
        return openSession(spec, { background: i > 0 });
      });
    });
  }

  // ------------------------------------------------------------------ sidebar resize + work mode
  (function () {
    var w = Number(localStorage.getItem('sw-side')) || 230;
    document.documentElement.style.setProperty('--side', w + 'px');
    var rz = $('#resizer'), drag = false;
    rz.addEventListener('mousedown', function (e) { drag = true; rz.classList.add('drag'); e.preventDefault(); });
    window.addEventListener('mousemove', function (e) {
      if (!drag) return;
      w = Math.max(170, Math.min(480, e.clientX));
      document.documentElement.style.setProperty('--side', w + 'px');
    });
    window.addEventListener('mouseup', function () {
      if (!drag) return;
      drag = false; rz.classList.remove('drag');
      localStorage.setItem('sw-side', String(w));
      fitAll();
    });
  })();

  function setWorkMode(on) {
    document.body.classList.toggle('work-mode', on);
    localStorage.setItem('sw-work', on ? '1' : '');
    fitAll();
    setTimeout(fitAll, 120);
  }
  function toggleWorkMode() { setWorkMode(!document.body.classList.contains('work-mode')); }

  function quitApp() {
    confirmDlg('Quit? Every terminal, Claude chat and running command is stopped. (Closing the window instead keeps them running.)', 'Quit', true).then(function (ok) {
      if (!ok) return;
      api('POST', 'quit').catch(function () {}).then(function () {
        S.tabs.forEach(function (t) { t.closed = true; });
        document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' }, h('p', { class: 'empty-title' }, 'Stopped'), h('p', { class: 'muted' }, 'Run simabworkspace gui to start again.'))));
        setTimeout(function () { window.close(); }, 400);
      });
    });
  }

  // any script error is shown instead of failing silently
  var lastErr = '';
  window.addEventListener('error', function (e) {
    var msg = (e && e.message) || 'error';
    if (msg === lastErr || /ResizeObserver/.test(msg)) return;
    lastErr = msg;
    toast('Error: ' + msg + (e.filename ? ' (' + e.filename.split('/').pop() + ':' + e.lineno + ')' : ''), 'err', { timeout: 15000 });
  });

  // ------------------------------------------------------------------ updates
  function checkUpdate(force) {
    api('GET', 'update' + (force ? '?force=1' : '')).then(function (u) {
      S.update = u;
      var el = $('#update-note');
      el.classList.toggle('hidden', !u.available);
      if (u.available) {
        el.textContent = 'Update to ' + u.latest;
        if (!S.updateToasted) { S.updateToasted = true; toast('Simab Workspace ' + u.latest + ' is available', 'ok', { timeout: 8000, onclick: updateDialog }); }
      }
      if (force && !u.available) toast(u.error ? 'Could not check: ' + u.error : 'Up to date (' + u.current + ')', u.error ? 'warn' : 'ok');
    }).catch(function () {});
  }

  function updateDialog() {
    var u = S.update || {};
    var go = h('button', { class: 'btn primary', onclick: function () {
      go.disabled = true; go.textContent = 'Downloading…';
      api('POST', 'update').then(function (d) { md.close(); waitForRestart(d.version); })
        .catch(function (e) { go.disabled = false; go.textContent = 'Update now'; fail(e); });
    } }, 'Update now');
    var md = openModal({ title: 'Update to ' + u.latest, body: [
      h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:10px' }, 'You have ' + u.current + '. The update downloads and installs here, then the app restarts and this window reloads. Open terminals and chats restart too — reopen them with “Reopen last session”; Claude conversations continue where they were.'),
      h('pre', { class: 'tool-pre', style: 'max-height:300px' }, (u.notes || '').replace(/```/g, '').trim() || '—'),
      u.url ? h('a', { href: u.url, target: '_blank', style: 'font-size:12px;color:var(--accent)' }, 'Release page') : null],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Later'), go] });
  }

  function waitForRestart(version) {
    S.tabs.forEach(function (t) { t.closed = true; });
    var box = h('div', { class: 'empty stale' }, h('div', { class: 'empty-inner' }, h('p', { class: 'empty-title' }, 'Updating to ' + version + '…'), h('p', { class: 'muted' }, 'The app restarts; this window reloads by itself.')));
    document.body.append(box);
    var tries = 0;
    (function poll() {
      tries++;
      fetch('/api/ping', { headers: { 'X-Token': token } }).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.version === version) location.reload(); else setTimeout(poll, 1000);
      }).catch(function () { if (tries < 90) setTimeout(poll, 1000); else box.querySelector('.muted').textContent = 'The app did not come back — run: simabworkspace gui'; });
    })();
  }

  // ------------------------------------------------------------------ shared with chat.js
  window.SW = {
    h: h, api: api, toast: toast, fail: fail, S: S, faNum: faNum, token: token, project: project,
    closeTab: closeTab, activate: activate, setupDrag: setupDrag, isGlobalShortcut: isGlobalShortcut,
    confirmDlg: confirmDlg, openSession: openSession, showMenu: showMenu, renameTab: renameTab,
    notify: notify, pickClaudeSession: pickClaudeSession, layoutItemsFor: layoutItems, tabEl: tabEl, viewImage: viewImage,
    claudeInTerminal: claudeInTerminal
  };

  // ------------------------------------------------------------------ boot
  function wire() {
    $('#btn-palette').addEventListener('click', openPalette);
    $('#update-note').addEventListener('click', updateDialog);
    $('#btn-history').addEventListener('click', function () { historyDialog(); });
    $('#btn-settings').addEventListener('click', function (e) {
      menuAt(e.currentTarget, [
        { label: 'History…', action: function () { historyDialog(); } },
        { label: 'Check for updates', action: function () { checkUpdate(true); } },
        { label: 'Settings…', action: settingsDialog },
        { label: 'Grid: all tabs side by side', action: gridAll },
        { label: 'Single section', disabled: L.groups.length < 2, action: mergeAll },
        { label: 'Hide sidebar', k: 'Alt+M', action: toggleWorkMode },
        '-',
        { label: 'Quit', action: quitApp }
      ]);
    });
    $('#btn-hide-side').addEventListener('click', function () { setWorkMode(true); });
    $('#show-side').addEventListener('click', function () { setWorkMode(false); });
    $('#st-ssh').addEventListener('click', function () { refreshStatus(true); });
    $('#proj-filter').addEventListener('input', function (e) { S.filter = e.target.value; renderProjects(); });
    if (localStorage.getItem('sw-work')) setWorkMode(true);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { refreshProjects(); if (S.active) S.active.focus(); } });
    var asked = false;
    document.addEventListener('click', function () {
      if (asked) return;
      asked = true;
      if (S.settings.bellNotify && window.Notification && Notification.permission === 'default') Notification.requestPermission();
    });
  }

  function boot() {
    if (!token) {
      document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' },
        h('p', { class: 'empty-title' }, 'Open this window with: simabworkspace gui'))));
      return;
    }
    wire();
    api('GET', 'state').then(function (st) {
      S.settings = st.settings;
      S.server = st.server;
      S.projects = st.projects;
      S.projectsKey = JSON.stringify(st.projects);
      S.wsl = st.wsl;
      S.lastTabs = st.lastTabs;
      S.version = st.version;
      renderProjects();
      var want = sessionStorage.getItem('sw-active');
      var placed = initLayout(st.sessions.map(function (x) { return x.id; }));
      st.sessions.forEach(function (info) { addTab(info, true, placed[info.id] || L.focused); });
      L.groups.slice().forEach(function (g) { if (!g.tabs.length && L.groups.length > 1) removeGroup(g); });
      L.groups.forEach(function (g) {
        var a = g.tabs.find(function (x) { return x.id === g.wantActive; }) || g.tabs[0];
        if (a) showInGroup(g, a);
      });
      syncOrder();
      renderLayout();
      var t = S.tabs.find(function (x) { return x.id === want; }) || (L.focused && L.focused.active) || S.tabs[0];
      if (t) activate(t); else renderEmpty();
      refreshStatus(true);
      // first run on a new machine: everything is set up in Settings
      if (!st.server.configured) { toast('Welcome — set up this machine and your server here', 'ok', { timeout: 8000 }); settingsDialog(); }
      setInterval(refreshProjects, 5000);
      setTimeout(function () { checkUpdate(false); }, 3000);
      setInterval(function () { checkUpdate(false); }, 6 * 3600 * 1000);
      refreshGit();
      setInterval(function () { refreshGit(); }, 10000);
      setInterval(function () { refreshStatus(false); }, 20000);
    }).catch(function (e) {
      document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' },
        h('p', { class: 'empty-title' }, 'Cannot reach the app'), h('p', { class: 'muted' }, e.message + ' — run simabworkspace gui'))));
    });
  }

  var fonts = document.fonts && document.fonts.load
    ? Promise.all(['13px "JetBrains Mono"', 'bold 13px "JetBrains Mono"', '13px Inter', '13px Vazirmatn'].map(function (f) { return document.fonts.load(f).catch(function () {}); }))
    : Promise.resolve();
  fonts.then(boot);
})();
