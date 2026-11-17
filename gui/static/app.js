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
  // extra windows (tabs moved to a second monitor): #w=<window id>&take=<tab ids to bring along>
  var wm = location.hash.match(/[#&]w=([a-z0-9]{4,16})/);
  var km = location.hash.match(/[#&]take=([a-f0-9,]+)/);
  var WID = wm ? wm[1] : (sessionStorage.getItem('sw-win') || '');
  var TAKE = km ? km[1].split(',').filter(Boolean) : [];
  if (wm) sessionStorage.setItem('sw-win', WID);
  if (tm) {
    token = tm[1];
    sessionStorage.setItem('sw-token', token);
  } else {
    token = sessionStorage.getItem('sw-token') || '';
  }
  if (tm || wm || km) history.replaceState(null, '', location.pathname);
  function wkey(k) { return WID ? k + ':' + WID : k; }      // each window keeps its own layout

  function api(method, path, body) {
    var opts = { method: method, headers: { 'X-Token': token, 'X-SW': '1' }, credentials: 'same-origin' };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch('/api/' + path, opts).catch(function () {
      // the app's background service is not reachable (stopped, crashed, WSL shut down…)
      connectionLost();
      var e = new Error('The app is not reachable — reconnecting…');
      e.offline = true;
      throw e;
    }).then(function (r) {
      if (connLost) connectionBack();
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 401) { if (d.login) showLogin(); else staleWindow(); }
        if (!r.ok) { var er = new Error(d.error || ('HTTP ' + r.status)); er.status = r.status; throw er; }
        return d;
      });
    });
  }

  // Lost connection: one clear notice (not a toast per request), retry until it is back.
  var connLost = null;
  function connectionLost() {
    if (connLost || staleShown) return;
    connLost = S.web ? h('div', { class: 'conn-lost' }, h('b', null, 'Lost connection.'), ' Reconnecting…')
      : h('div', { class: 'conn-lost' }, h('b', null, 'Lost connection to the app.'),
      ' Reconnecting… If it does not come back, start it again: Start menu → ', h('i', null, 'Simab Workspace'),
      ', or run ', h('kbd', null, 'simabworkspace gui'), '.');
    document.body.append(connLost);
    (function poll() {
      if (!connLost) return;
      fetch('/api/ping', { headers: { 'X-Token': token, 'X-SW': '1' }, credentials: 'same-origin' }).then(function (r) {
        if (r.status === 401) { connectionBack(); if (S.web) showLogin(); else staleWindow(); return; }
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

  // A connection can die without the browser noticing (a phone that slept, a CDN in between
  // that keeps its own side open): a quiet tab pings; no answer within PING_WAIT → it is
  // treated as dropped and the tab's normal reconnect runs.
  var PING_QUIET = 15000, PING_WAIT = 10000;
  function heartbeat() {
    var now = Date.now();
    (S.tabs || []).forEach(function (t) {
      var ws = t.ws;
      if (!ws || ws.readyState !== 1 || t.closed || t.gone) return;
      if (t.pingAt && (t.lastRx || 0) < t.pingAt) {
        if (now - t.pingAt > PING_WAIT) dropWs(t);
        return;
      }
      if (now - (t.lastRx || 0) > PING_QUIET) {
        t.pingAt = now;
        try { ws.send('{"t":"p"}'); } catch (e) { dropWs(t); }
      }
    });
  }
  function dropWs(t) {
    var ws = t.ws, oc = ws.onclose;
    t.pingAt = 0;
    ws.onclose = null;
    ws.onmessage = null;
    try { ws.close(); } catch (e) { /* already closed */ }
    if (oc) oc.call(ws);
  }
  setInterval(heartbeat, 2000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) heartbeat(); });
  window.addEventListener('online', heartbeat);

  // Web version: sign in with an SSH user and password of an allowed server.
  var loginShown = false;
  function showLogin() {
    if (loginShown) return;
    loginShown = true;
    fetch('/api/web-info', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).catch(function () { return {}; }).then(function (info) {
      var host = h('input', { class: 'inp mono', value: info.host || '', placeholder: 'server address', autocomplete: 'off', spellcheck: 'false' });
      var port = h('input', { class: 'inp mono', value: info.port || '22', placeholder: '22', inputmode: 'numeric' });
      var user = h('input', { class: 'inp mono', placeholder: 'user', autocomplete: 'username', autocapitalize: 'off', spellcheck: 'false' });
      var pw = h('input', { class: 'inp', type: 'password', placeholder: 'password', autocomplete: 'current-password' });
      var err = h('div', { class: 'add-err hidden' });
      var go = h('button', { class: 'btn primary', type: 'submit' }, 'Sign in');
      var code = h('input', { class: 'inp mono', placeholder: '123456', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6' });
      var codeField = h('div', { class: 'field hidden' }, h('label', null, 'Code from your authenticator app'), code);
      var fixed = (info.hosts || []).length === 1;
      function submit(e) {
        e.preventDefault();
        if (!user.value.trim()) { user.focus(); return; }
        if (!pw.value) { pw.focus(); return; }
        go.disabled = true; go.textContent = 'Signing in…'; err.classList.add('hidden');
        fetch('/api/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-SW': '1' },
          body: JSON.stringify({ host: host.value.trim(), port: port.value.trim(), user: user.value.trim(), password: pw.value, code: code.value.trim() }) })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) { var x = new Error(d.error || ('HTTP ' + r.status)); x.code = d.code; throw x; } }); })
          .then(function () { location.reload(); })
          .catch(function (e2) {
            go.disabled = false; go.textContent = 'Sign in'; err.textContent = e2.message; err.classList.remove('hidden');
            if (e2.code) { codeField.classList.remove('hidden'); code.value = ''; code.focus(); return; }    // the password was right
            pw.value = ''; pw.focus();
          });
      }
      var form = h('form', { class: 'login', onsubmit: submit },
        h('div', { class: 'login-brand' }, 'simab', h('span', null, 'workspace')),
        h('div', { class: 'muted login-sub' }, 'Sign in with your SSH user on the server.'),
        fixed ? h('div', { class: 'login-host mono' }, info.host + (info.port && info.port !== '22' ? ':' + info.port : '')) :
          h('div', { class: 'row' }, h('div', { class: 'field' }, h('label', null, 'Server'), host),
            h('div', { class: 'field', style: 'flex:0 0 80px' }, h('label', null, 'SSH port'), port)),
        h('div', { class: 'field' }, h('label', null, 'User'), user),
        h('div', { class: 'field' }, h('label', null, 'Password'), pw),
        codeField, err, go,
        h('div', { class: 'muted login-note' }, 'The password is only used to sign in; it is not stored.'));
      document.body.replaceChildren(h('div', { class: 'login-wrap' }, form));
      setTimeout(function () { user.focus(); }, 50);
    });
  }

  // PWA: the browser offers to install the web version as an app.
  var installEvt = null;
  window.addEventListener('beforeinstallprompt', function (e) { e.preventDefault(); installEvt = e; });
  window.addEventListener('appinstalled', function () { installEvt = null; toast('Installed — open it from your home screen', 'ok'); });
  function standalone() { return !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true; }
  function installApp() {
    if (installEvt) { installEvt.prompt(); installEvt = null; return; }
    var ios = /iPad|iPhone|Macintosh/.test(navigator.userAgent || '') && 'ontouchend' in document;
    var md = openModal({ title: 'Install as an app', size: 'sm', body: h('div', { class: 'install-help' },
      ios ? h('p', null, 'In Safari: tap Share, then “Add to Home Screen”.')
        : h('p', null, 'In Chrome: open the ⋮ menu, then “Install app” (or “Add to Home screen”).'),
      h('p', { class: 'muted' }, 'If Chrome does not offer it: this server uses its own certificate, so install it once on the device — ',
        h('a', { href: '/simab-server.crt', download: 'simab-server.crt' }, 'download the certificate'),
        ', then Settings → Security → Encryption & credentials → Install a certificate → CA certificate. Reload this page afterwards.')),
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
  }
  function registerWorker() {
    if (!S.web || !('serviceWorker' in navigator) || location.protocol !== 'https:') return;
    navigator.serviceWorker.addEventListener('message', function (e) {
      var tab = e.data && e.data.sw === 'open-tab' && S.tabs.find(function (t) { return t.id === e.data.tab; });
      if (tab) activate(tab);
    });
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () { /* untrusted certificate: no offline copy, still works */ });
  }

  // ---- phone notifications (Web Push): Claude finished / needs you, even with the page closed
  function pushSupported() {
    return S.web && location.protocol === 'https:' && 'serviceWorker' in navigator && window.PushManager && window.Notification;
  }
  function pushSub() {
    return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription().then(function (sub) { return { reg: reg, sub: sub }; }); });
  }
  function pushEnable() {
    return Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') throw new Error('Notifications are blocked for this site — allow them in the browser settings');
      return api('GET', 'push');
    }).then(function (info) {
      if (!info.available) throw new Error('Notifications are not available here');
      return pushSub().then(function (x) {
        var key = Uint8Array.from(atob(info.key.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((info.key.length + 3) % 4)), function (c) { return c.charCodeAt(0); });
        return x.sub || x.reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      });
    }).then(function (sub) {
      var ua = navigator.userAgent || '';
      var device = /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : 'Linux';
      return api('POST', 'push', { subscription: sub.toJSON ? sub.toJSON() : sub, device: device + (/Mobile/.test(ua) ? ' phone' : '') });
    });
  }
  function pushDisable() {
    return pushSub().then(function (x) {
      if (!x.sub) return null;
      var ep = x.sub.endpoint;
      return x.sub.unsubscribe().catch(function () {}).then(function () { return api('POST', 'push/remove', { endpoint: ep }); });
    });
  }
  function pushRow() {
    if (!S.web) return null;
    var btn = h('button', { class: 'btn sm' }, '…');
    var test = h('button', { class: 'btn sm hidden', onclick: function () {
      api('POST', 'push/test').then(function (d) { toast(d.sent ? 'Sent — lock the screen to see it' : 'Could not send', d.sent ? 'ok' : 'warn'); }).catch(fail);
    } }, 'Test');
    var state = h('span', { class: 'muted' });
    var on = false;
    function show(v, note) {
      on = v;
      btn.textContent = v ? 'Turn off' : 'Turn on';
      btn.disabled = false;
      test.classList.toggle('hidden', !v);
      state.textContent = note || (v ? 'on for this device' : 'off');
    }
    if (!pushSupported()) {
      btn.disabled = true; btn.textContent = 'Turn on';
      state.textContent = location.protocol !== 'https:' ? 'needs https' : 'not supported by this browser (on iPhone: add to Home Screen first)';
    } else {
      pushSub().then(function (x) { show(!!x.sub && Notification.permission === 'granted'); }).catch(function () { show(false); });
      btn.addEventListener('click', function () {
        btn.disabled = true;
        (on ? pushDisable().then(function () { show(false); }) : pushEnable().then(function () { show(true); toast('Notifications on for this device', 'ok'); }))
          .catch(function (e) { show(on); fail(e); });
      });
    }
    return h('div', { class: 'svc' }, h('span', null, 'Notifications on this device', h('br'), h('small', { class: 'muted' }, 'When Claude finishes or needs you, with the page closed — '), state), h('span', { style: 'display:flex;gap:6px' }, test, btn));
  }

  // ---- web sign-in security: two-step codes (authenticator app) and signed-in devices
  function accountRows() {
    if (!S.web) return null;
    var totpState = h('span', { class: 'muted' }, '…');
    var totpBtn = h('button', { class: 'btn sm', disabled: true }, '…');
    var devList = h('div', { class: 'devices' });
    var othersBtn = h('button', { class: 'btn sm hidden' }, 'Sign out the others');
    function ago(t) { var m = Math.round((Date.now() / 1000 - t) / 60); return !t ? '' : m < 2 ? 'now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' days ago'; }
    function render(d) {
      totpState.textContent = d.totp ? 'on — a code is asked at sign-in' : 'off';
      totpBtn.textContent = d.totp ? 'Turn off' : 'Set up';
      totpBtn.disabled = false;
      totpBtn.onclick = function () { (d.totp ? totpOff : totpSetup)(load); };
      if (!d.devices) return;
      devList.replaceChildren.apply(devList, d.devices.map(function (x) {
        return h('div', { class: 'dev' }, h('span', { class: 'grow' }, x.device + (x.current ? ' · this device' : ''), h('br'),
          h('small', { class: 'muted mono' }, x.ip + ' · ' + ago(x.last))),
          x.current ? null : h('button', { class: 'btn sm', onclick: function () { api('POST', 'account/signout', { id: x.id }).then(render).catch(fail); } }, 'Sign out'));
      }));
      othersBtn.classList.toggle('hidden', d.devices.filter(function (x) { return !x.current; }).length < 2);
    }
    othersBtn.onclick = function () { api('POST', 'account/signout', { id: 'others' }).then(render).catch(fail); };
    function load() { api('GET', 'account').then(render).catch(function () { totpState.textContent = 'not available'; }); }
    load();
    return h('div', null,
      h('div', { class: 'svc' }, h('span', null, 'Two-step sign-in', h('br'), h('small', { class: 'muted' }, 'A code from an authenticator app too — '), totpState), totpBtn),
      h('div', { class: 'svc' }, h('span', null, 'Signed-in devices'), othersBtn), devList);
  }
  function totpSetup(done) {
    api('POST', 'account/totp/setup').then(function (d) {
      var code = h('input', { class: 'inp mono', placeholder: '123456', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6' });
      var md = openModal({ title: 'Two-step sign-in', size: 'sm', body: [
        h('p', null, '1. Add this key to an authenticator app (Google Authenticator, Microsoft Authenticator, 2FAS…):'),
        h('p', null, h('a', { class: 'btn', href: d.uri }, 'Open in authenticator app')),
        h('p', { class: 'muted' }, 'or type the key:'),
        h('div', { class: 'mono totp-key' }, d.secret.replace(/(.{4})/g, '$1 ').trim()),
        h('p', null, '2. Enter the code it shows:'), code],
        foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
          h('button', { class: 'btn primary', onclick: function () {
            api('POST', 'account/totp', { code: code.value.trim(), on: true }).then(function () { md.close(); toast('Two-step sign-in is on', 'ok'); done(); }).catch(fail);
          } }, 'Turn on')] });
    }).catch(fail);
  }
  function totpOff(done) {
    promptDlg('Code from your authenticator app, to turn two-step sign-in off', '').then(function (c) {
      if (!c) return;
      api('POST', 'account/totp', { code: c.trim(), on: false }).then(function () { toast('Two-step sign-in is off', 'ok'); done(); }).catch(fail);
    });
  }

  function logout() {
    fetch('/api/logout', { method: 'POST', credentials: 'same-origin', headers: { 'X-SW': '1' } }).then(function () { location.reload(); }, function () { location.reload(); });
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
    filter: '', status: {}, wsl: false, lastTabs: [], version: '', git: {}, docker: {}, tunnels: [], usage: null
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
    if (tab.pane) {
      if (c) tab.pane.style.setProperty('--pc', c); else tab.pane.style.removeProperty('--pc');
      tab.pane.classList.toggle('colored', !!c);            // a frame in the project's colour around the work
    }
  }
  function repaintTabs() { S.tabs.forEach(paintTab); }

  // The text every new Claude chat of a project starts with — the user writes their request after it.
  function starterDialog(p) {
    var ta = h('textarea', { class: 'inp starter-ta', rows: 10, dir: 'auto', spellcheck: 'false',
      placeholder: 'e.g. Read memory/README.md and the notes it points to first. Answer in Persian.' });
    ta.value = p.starter || '';
    function save(text) {
      api('POST', 'starter/' + encodeURIComponent(p.name), { text: text }).then(function (d) {
        p.starter = d.starter;
        md.close();
        toast(d.starter ? 'New chats in ' + p.name + ' start with it' : 'Starter message removed', 'ok');
        refreshProjects();
      }).catch(fail);
    }
    var md = openModal({ title: 'Starter message — ' + p.name, body: [
      h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:8px' },
        'Every new Claude chat in this project starts with this text in the message box — you add your request after it and send. Not used when you continue a conversation.'),
      ta],
      foot: [p.starter ? h('button', { class: 'btn danger', onclick: function () { save(''); } }, 'Remove') : null, h('span', { class: 'grow' }),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: function () { save(ta.value); } }, 'Save')].filter(Boolean) });
    setTimeout(function () { ta.focus(); }, 40);
  }

  // the name shown for a project: the user's own title, else its real name (which never changes)
  function pTitle(p) { return (p && (p.title || p.name)) || ''; }
  function renameProject(p) {
    promptDlg('Rename “' + pTitle(p) + '”', pTitle(p)).then(function (v) {
      if (v === null) return;
      api('POST', 'title/' + encodeURIComponent(p.name), { title: v }).then(function (d) {
        p.title = d.title;
        refreshProjects();
      }).catch(fail);
    });
  }

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
    if (spec.kind === 'docker') return ['▣', 'server'];
    if (spec.kind === 'files') return ['▤', 'files'];
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
  // ---- stars: starred commands / menu entries come first (kept in the settings, all devices)
  function starredList() { return (S.settings && S.settings.starred) || []; }
  function isStarred(key) { return starredList().indexOf(key) >= 0; }
  function toggleStar(key) {
    var list = starredList().slice(), i = list.indexOf(key);
    if (i >= 0) list.splice(i, 1); else list.push(key);
    S.settings.starred = list;
    api('POST', 'settings', { starred: list }).then(function (d) { if (d && d.settings) S.settings.starred = d.settings.starred; }).catch(fail);
  }
  function starBtn(key, after) {
    var on = isStarred(key);
    return h('span', { class: 'star' + (on ? ' on' : ''), title: on ? 'Unstar' : 'Star — show first',
      onmousedown: function (e) { e.stopPropagation(); },
      'aria-label': on ? 'Unstar' : 'Star',
      onclick: function (e) { e.stopPropagation(); e.preventDefault(); toggleStar(key); after(); } });   // ☆/★ drawn by CSS
  }
  function starKey(prefix, it) { return prefix + ':' + (it.id || it.label); }
  function starrable(it) { return it && it !== '-' && !it.header && it.label && it.action; }
  // starred entries in the order of the star list (the user's own order, changed by dragging)
  function starSort(prefix, arr) {
    var l = starredList();
    return arr.slice().sort(function (a, b) { return l.indexOf(starKey(prefix, a)) - l.indexOf(starKey(prefix, b)); });
  }
  // new order of the shown starred keys; the other keys keep their places in the list
  function setStarOrder(keys) {
    var list = starredList().slice(), slots = [];
    list.forEach(function (k, i) { if (keys.indexOf(k) >= 0) slots.push(i); });
    if (slots.length !== keys.length) return;
    var before = list.join('\n');
    slots.forEach(function (pos, j) { list[pos] = keys[j]; });
    if (list.join('\n') === before) return;
    S.settings.starred = list;
    api('POST', 'settings', { starred: list }).then(function (d) { if (d && d.settings) S.settings.starred = d.settings.starred; }).catch(fail);
  }
  // drag a starred row up / down among the starred rows of box (attribute data-star = key).
  // Mouse: the whole row; finger / pen: the ⠿ grip only (the list still scrolls).
  var starDragEnd = 0;
  function justDragged() { return Date.now() - starDragEnd < 400; }
  function dragStar(row, box, done) {
    row.addEventListener('pointerdown', function (e) {
      if (e.button) return;
      var grip = !!(e.target && e.target.classList && e.target.classList.contains('grip'));
      if (e.pointerType && e.pointerType !== 'mouse' && !grip) return;
      if (grip && e.preventDefault) e.preventDefault();
      var sy = e.clientY, moved = false;
      function rows() { return Array.prototype.filter.call(box.children, function (c) { return c.classList.contains('starred'); }); }
      function move(ev) {
        if (ev.pointerId !== e.pointerId) return;
        if (!moved) {
          if (Math.abs(ev.clientY - sy) < 5) return;
          moved = true; row.classList.add('dragging'); document.body.classList.add('star-drag');
        }
        if (ev.preventDefault) ev.preventDefault();
        var rs = rows();
        for (var i = 0; i < rs.length; i++) {
          if (rs[i] === row) continue;
          var b = rs[i].getBoundingClientRect();
          if (ev.clientY >= b.top && ev.clientY < b.bottom) {
            box.insertBefore(row, ev.clientY < b.top + b.height / 2 ? rs[i] : rs[i].nextSibling);
            break;
          }
        }
      }
      function up(ev) {
        if (ev.pointerId !== e.pointerId) return;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
        if (!moved) return;
        row.classList.remove('dragging'); document.body.classList.remove('star-drag');
        starDragEnd = Date.now();
        done(rows().map(function (r) { return r.getAttribute('data-star'); }));
      }
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    });
  }

  function showMenu(x, y, items, opts) {
    closeMenu();
    var el = h('div', { class: 'menu' });
    var last = null;
    var star = opts && opts.star, orig = items;
    var top = star ? starSort(star, items.filter(function (it) { return starrable(it) && isStarred(starKey(star, it)); })) : [];
    if (top.length) items = [{ header: 'Starred' }].concat(top, ['-'], items.filter(function (it) { return top.indexOf(it) < 0; }));
    items.forEach(function (it) {
      if (!it) return;
      if (it === '-') { if (last && last !== '-') el.append(h('hr')); last = '-'; return; }
      last = it;
      if (it.header) { el.append(h('div', { class: 'mh' }, it.header)); return; }
      var onTop = top.indexOf(it) >= 0;                // in the Starred block (moved there, not repeated below)
      var b = h('button', { disabled: !!it.disabled, class: onTop ? 'starred' : null, 'data-star': onTop ? starKey(star, it) : null,
          onclick: function () { if (justDragged()) return; closeMenu(); it.action(); } },
        onTop && top.length > 1 ? h('span', { class: 'grip', title: 'Drag to reorder' }) : null,
        it.label, it.k || (star && starrable(it)) ? h('span', { class: 'k' }, it.k || '') : null,
        star && starrable(it) ? starBtn(starKey(star, it), function () { showMenu(x, y, orig, opts); }) : null);
      if (onTop && top.length > 1) dragStar(b, el, function (keys) { setStarOrder(keys); showMenu(x, y, orig, opts); });
      el.append(b);
    });
    if (el.lastChild && el.lastChild.tagName === 'HR') el.lastChild.remove();
    $('#menu-root').append(el);
    var r = el.getBoundingClientRect();
    el.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + 'px';
    el.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + 'px';
    menuEl = el;
  }
  function menuAt(btn, items, opts) { var r = btn.getBoundingClientRect(); showMenu(r.left, r.bottom + 4, items, opts); }
  document.addEventListener('mousedown', function (e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }, true);
  window.addEventListener('blur', closeMenu);

  // ------------------------------------------------------------------ modals
  var modalStack = [];
  function openModal(opts) {
    if (typeof compact === 'function' && compact()) closeDrawer();
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
    this.keybar = TOUCH.matches ? this.makeKeybar() : null;
    this.pane = h('div', { class: 'pane hidden-pane' }, this.body, this.composer, this.keybar);

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
      if (self.ctrlArmed && d.length === 1) {
        var c = d.toUpperCase().charCodeAt(0);
        if (c >= 64 && c <= 95) d = String.fromCharCode(c - 64);
        self.armCtrl(false);
      }
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

  // Keys a touch keyboard does not have (Esc, Tab, Ctrl, arrows…), above it.
  var KEYS = [['Esc', '\x1b'], ['Tab', '\t'], ['Ctrl', null], ['^C', '\x03'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['←', '\x1b[D'], ['→', '\x1b[C'],
    ['|', '|'], ['/', '/'], ['-', '-'], ['~', '~'], ['Aa', 'composer']];
  Tab.prototype.makeKeybar = function () {
    var self = this;
    var bar = h('div', { class: 'keybar' });
    KEYS.forEach(function (k) {
      var b = h('button', { class: 'kb', title: k[1] === 'composer' ? 'Type in a text box (Persian, autocorrect)' : k[0] }, k[0]);
      b.addEventListener('pointerdown', function (e) { e.preventDefault(); });      // keep the keyboard open
      b.addEventListener('click', function () {
        if (k[0] === 'Ctrl') { self.armCtrl(!self.ctrlArmed); return; }
        if (k[1] === 'composer') { self.toggleComposer(); return; }
        if (self.alive) self.send({ t: 'i', d: k[1] });
        self.term.focus();
      });
      if (k[0] === 'Ctrl') self.ctrlBtn = b;
      bar.append(b);
    });
    return bar;
  };
  Tab.prototype.armCtrl = function (on) {
    this.ctrlArmed = on;
    if (this.ctrlBtn) this.ctrlBtn.classList.toggle('on', on);
    if (on) this.term.focus();
  };

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
    var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?id=' + encodeURIComponent(this.id) + '&t=' + encodeURIComponent(token));
    this.ws = ws;
    ws.onopen = function () {
      self.retry = 0;
      self.lastRx = Date.now();
      self.pingAt = 0;
      if (self.opened) self.send({ t: 'r', c: self.term.cols, r: self.term.rows });
      if (self.alive) self.setBanner(null);
    };
    ws.onmessage = function (ev) {
      var msg;
      self.lastRx = Date.now();
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.t === 'o') { self.term.write(msg.d); self.onOutput(); if (self.info.spec.kind === 'run' && !self.foundUrl) self.scanUrl(msg.d); }
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

  // a run command printed the address it serves (Angular, Vite, Django…): offer to open it
  var URL_RE = /\bhttps?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d{2,5})\b[^\s"'\x1b]*/;
  Tab.prototype.scanUrl = function (text) {
    var m = URL_RE.exec(String(text || '').replace(/\x1b\[[0-9;]*m/g, ''));
    if (!m) return;
    var port = Number(m[1]);
    this.foundUrl = 'http://localhost:' + port + m[0].replace(/^[^/]*\/\/[^/]*/, '').replace(/[.,;)]+$/, '');
    this.foundPort = port;
    var self = this, server = this.info.spec.where === 'server';
    var p = project(this.info.spec.project);
    if (this.urlBar) this.urlBar.remove();
    this.urlBar = h('div', { class: 'urlbar' }, h('span', { class: 'mono' }, (server ? 'on the server · ' : '') + 'localhost:' + port),
      !server ? h('a', { class: 'btn sm', href: this.foundUrl, target: '_blank', rel: 'noopener' }, 'Open ↗')
        : !S.web && p ? h('button', { class: 'btn sm', onclick: function () { tunnelsDialog(p, { name: self.info.title, remotePort: port, localPort: port < 1024 ? port + 10000 : port, http: true }); } }, 'Forward to this computer…')
          : h('span', { class: 'muted', title: 'Port forwarding is in the installed app' }, 'port forwarding: installed app'),
      h('button', { class: 'icon-btn', title: 'Hide', onclick: function () { self.urlBar.remove(); } }, '✕'));
    this.body.append(this.urlBar);
    renderProjects();
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
    this.tabsEl.addEventListener('dragover', function (e) { if (dragTab || foreignDrag(e)) e.preventDefault(); });
    this.tabsEl.addEventListener('drop', function (e) {
      if (e.target === g.tabsEl && foreignDrag(e)) { e.preventDefault(); takeDropped(e, g, null); return; }
      if (!dragTab || e.target !== g.tabsEl) return;
      e.preventDefault();
      moveTab(dragTab, g, g.tabs.length);
    });
    this.closeBtn = h('button', { class: 'icon-btn', title: 'Close section (tabs move to the next one)', onclick: function () { closeGroup(g); } }, '✕');
    this.bar = h('div', { class: 'gbar' }, h('button', { class: 'icon-btn menu-btn', title: 'Projects', onclick: function () { openDrawer(); } }, '☰'), this.tabsEl,
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
      if (!(dragTab || foreignDrag(e)) || g.bar.contains(e.target)) return;
      e.preventDefault();
      g.showDrop(dropZone(g, e));
    });
    this.el.addEventListener('dragleave', function (e) { if (!g.el.contains(e.relatedTarget)) g.showDrop(null); });
    this.el.addEventListener('drop', function (e) {
      if (foreignDrag(e) && !g.bar.contains(e.target)) { e.preventDefault(); var fz = dropZone(g, e); g.showDrop(null); takeDropped(e, g, fz); return; }
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
    if (compact()) return g;                  // one section at a time on narrow screens
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

  // drag with mouse, finger or pen: start(e) returns {move(e), up()}; the page does not scroll meanwhile
  function onDrag(el, start) {
    el.addEventListener('pointerdown', function (e) {
      if (e.button) return;                     // only the main mouse button
      e.preventDefault();
      var d = start(e);
      if (!d) return;
      function move(ev) { if (ev.pointerId === e.pointerId) d.move(ev); }
      function up(ev) {
        if (ev.pointerId !== e.pointerId) return;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
        d.up();
      }
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    });
  }

  function gutter(node, i, splitEl) {
    var gEl = h('div', { class: 'gutter ' + node.dir });
    onDrag(gEl, function (e) {
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
        gEl.classList.remove('drag');
        document.body.classList.remove('resizing-h', 'resizing-v');
        fitAll();
        saveLayout();
      }
      return { move: move, up: up };
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
    if (L.root) localStorage.setItem(wkey('sw-layout'), JSON.stringify(ser(L.root)));
  }, 300);

  function initLayout(sessionIds) {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(wkey('sw-layout')) || 'null'); } catch (e) { saved = null; }
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
    if (compact()) return [];
    return [
      '-',
      { label: 'Split right', k: 'Alt+\\', action: function () { var ng = splitGroup(tab.group, 'right', true); moveTab(tab, ng, 0); } },
      { label: 'Split down', k: 'Alt+-', action: function () { var ng = splitGroup(tab.group, 'bottom', true); moveTab(tab, ng, 0); } },
      L.groups.length > 1 ? { label: 'Move to next section', action: function () {
        var i = L.groups.indexOf(tab.group);
        moveTab(tab, L.groups[(i + 1) % L.groups.length]);
      } } : null,
      '-',
      { label: 'Move to new window', action: function () { moveToNewWindow(tab); } },
      WID ? { label: 'Move to main window', action: function () { moveToWindow(tab, ''); } } : null
    ].concat((S.windows || []).filter(function (w) { return w.id !== WID; }).map(function (w) {
      return { label: 'Move to window ' + w.n, action: function () { moveToWindow(tab, w.id); } };
    }));
  }

  // ------------------------------------------------------------------ extra windows (second monitor)
  // Every window shows the tabs the server lists for it (win: '' = the main window). Moving a tab
  // only changes that; each window then picks up / lets go of tabs (at once by BroadcastChannel,
  // else on the next poll or when it gets focus).
  var BC = null;
  try { BC = window.BroadcastChannel ? new BroadcastChannel('sw-tabs') : null; } catch (e) { BC = null; }
  if (BC) BC.onmessage = function () { syncTabs(); };
  function tellWindows() { if (BC) try { BC.postMessage({ t: 'sync', from: WID }); } catch (e) { /* closed */ } }
  var leaving = {};            // tab id -> time it left: do not take it back while the move settles

  function newWid() { return Math.random().toString(36).slice(2, 10).replace(/[^a-z0-9]/g, '') + 'w'; }

  function windowUrl(wid, take) {
    return location.pathname + '#' + (token ? 't=' + token + '&' : '') + 'w=' + wid + (take ? '&take=' + take : '');
  }

  function moveToNewWindow(tab, x, y) {
    var wid = newWid();
    var W = Math.max(640, window.outerWidth || 1200), H = Math.max(480, window.outerHeight || 800);
    if (x === undefined) { x = (window.screenX || 0) + 60; y = (window.screenY || 0) + 60; }
    var w = window.open(windowUrl(wid, tab.id), 'sw-' + wid,
      'popup=yes,width=' + W + ',height=' + H + ',left=' + Math.round(x) + ',top=' + Math.round(y));
    if (!w) { toast('The new window was blocked — allow pop-ups for this app and try again', 'err'); return null; }
    leaving[tab.id] = Date.now();
    removeTab(tab, false);
    return wid;
  }

  function moveToWindow(tab, wid) {
    return api('POST', 'sessions/' + tab.id + '/win', { win: wid }).then(function () {
      leaving[tab.id] = Date.now();
      removeTab(tab, false);
      tellWindows();
    }).catch(fail);
  }

  var syncing = false;
  function syncTabs() {
    if (syncing || !S.booted) return Promise.resolve();
    syncing = true;
    return api('GET', 'tabs' + (document.hidden ? '' : '?vis=1')).then(function (d) {
      syncing = false;
      S.windows = d.windows || [];
      renderTitle();
      var by = {};
      (d.sessions || []).forEach(function (x) { by[x.id] = x; });
      S.tabs.slice().forEach(function (t) {
        var x = by[t.id];
        if (x && (x.win || '') !== WID) removeTab(t, false);          // moved to another window
      });
      var now = Date.now();
      (d.sessions || []).forEach(function (x) {
        if ((x.win || '') !== WID || S.tabs.some(function (t) { return t.id === x.id; })) return;
        if (leaving[x.id] && now - leaving[x.id] < 10000) return;
        if (x.moved && x.moved >= S.bootNow) addTab(x, false);       // moved here (or back from a closed window)
      });
    }).catch(function () { syncing = false; });
  }

  function hasTabDrag(e) {
    var ty = e.dataTransfer && e.dataTransfer.types;
    return !!ty && Array.prototype.indexOf.call(ty, 'application/x-sw-tab') >= 0;
  }
  function foreignDrag(e) { return !dragTab && hasTabDrag(e); }

  // a tab dragged in from another window: it becomes this window's
  function takeDropped(e, g, zone, index) {
    var sid = e.dataTransfer.getData('application/x-sw-tab');
    if (!sid || S.tabs.some(function (t) { return t.id === sid; })) return Promise.resolve(null);
    return api('POST', 'sessions/' + sid + '/win', { win: WID }).then(function (info) {
      var target = zone && zone !== 'center' && !compact() ? splitGroup(g, zone, true) : g;
      var t = addTab(info, false, target);
      if (index !== undefined && index < target.tabs.length - 1) moveTab(t, target, index);
      renderLayout();
      tellWindows();
      return t;
    }).catch(function (er) { fail(er); return null; });
  }

  function outsideWindow(e) {
    if (!e.screenX && !e.screenY) return false;            // some systems report nothing
    var x0 = window.screenX || 0, y0 = window.screenY || 0;
    return e.screenX < x0 || e.screenY < y0 || e.screenX > x0 + (window.outerWidth || 0) || e.screenY > y0 + (window.outerHeight || 0);
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
    if (window.sideFilesPanel) window.sideFilesPanel.onActivate(tab);
    $('#empty').classList.add('hidden');
    g.renderEmpty();
    tab.el.classList.remove('activity', 'bell', 'done');
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

  function windowNumber() {
    var w = WID && (S.windows || []).find(function (x) { return x.id === WID; });
    return w ? w.n : 0;
  }
  function renderTitle() {
    var n = windowNumber();
    document.title = (S.active ? S.active.info.title + ' — ' : '') + 'Simab Workspace' + (n ? ' · window ' + n : '');
  }

  function addTab(info, background, group) {
    var had = S.tabs.find(function (t) { return t.id === info.id; });
    if (had) return had;
    var tab = info.spec.kind === 'chat' ? new window.SWChat.ChatTab(info)
      : info.spec.kind === 'files' ? new window.SWFiles.FilesTab(info)
      : info.spec.kind === 'changes' ? new window.SWFiles.ChangesTab(info) : new Tab(info, estimateSize());
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

  // Take a tab out of this window. kill: also end it on the server (else it lives on elsewhere).
  function removeTab(tab, kill) {
      var i = S.tabs.indexOf(tab);
      if (i < 0) return;
      S.tabs.splice(i, 1);
      var g = tab.group;
      var gi = g.tabs.indexOf(tab);
      g.tabs.splice(gi, 1);
      tab.dispose();
      if (kill && !tab.gone) api('DELETE', 'sessions/' + tab.id + (kill === 'keep' ? '?keep=1' : '')).catch(function () {});
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
  }

  function closeTab(tab, force) {
    var go = function () { removeTab(tab, true); };
    if (!force && tab.isChat && tab.busy && !tab.gone && !tab.watching) {
      // Claude is working: closing the tab need not stop it (it may be open on another device too)
      var md = openModal({ title: 'Claude is still working', size: 'sm',
        body: h('div', null, '“' + tab.info.title + '” is still working. Close only this tab and let it finish, or stop it?'),
        foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
          h('button', { class: 'btn danger', onclick: function () { md.close(); tab.interrupt(); setTimeout(go, 400); } }, 'Stop it'),
          h('button', { class: 'btn primary', onclick: function () { md.close(); removeTab(tab, 'keep'); } }, 'Close tab, keep working')] });
      return;
    }
    // uploads/downloads keep going in the background (Mutagen does the copying), so closing is safe
    var safe = ['shell', 'sync-info', 'sync-monitor', 'add-local', 'add-server', 'git-sync-on'];
    var risky = tab.isFiles ? false : tab.isChat ? tab.busy : (tab.alive && !tab.gone && safe.indexOf(tab.info.spec.kind) < 0);
    if (!force && risky && S.settings.confirmClose) {
      confirmDlg('“' + tab.info.title + '” is still running. Close it and stop what it is doing?', 'Close', true)
        .then(function (ok) { if (ok) go(); });
    } else go();
  }

  function openSession(spec, opts) {
    opts = opts || {};
    if (compact()) closeDrawer();
    if (spec.kind === 'chat' && spec.resume && !spec.watch && !spec.force) {
      var other = runningConv(spec.project, spec.resume);
      var mine = S.tabs.some(function (t) { return t.isChat && !t.gone && !t.info.spec.watch && (t.sessionId === spec.resume || t.info.spec.resume === spec.resume); });
      if (other && other.owner !== 'simab' && !other.tab && !mine) {
        askRunningElsewhere(spec.project, other, (spec.title || '').replace(/^Claude · /, ''));
        return Promise.resolve(null);
      }
    }
    if (spec.kind === 'chat' && spec.resume && !spec.watch) {
      var open = S.tabs.find(function (t) { return t.isChat && !t.gone && (t.sessionId === spec.resume || t.info.spec.resume === spec.resume); });
      if (open) { activate(open); return Promise.resolve(open); }
    }
    if (spec.kind === 'files') {
      var fx = S.tabs.find(function (t) { return t.isFiles && !t.isChanges && !t.gone && (t.info.spec.project || '') === (spec.project || ''); });
      if (fx) { activate(fx); return Promise.resolve(fx); }
    }
    if (spec.kind === 'changes') {
      var cx = S.tabs.find(function (t) { return t.isChanges && !t.gone && t.project === spec.project; });
      if (cx) { if (spec.focus) cx.focusOn = spec.focus; activate(cx); cx.refresh(true); return Promise.resolve(cx); }
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
    return api('POST', 'sessions', Object.assign({}, spec, { cols: sz.cols, rows: sz.rows }, WID ? { win: WID } : {})).then(function (info) {
      var t = addTab(info, opts.background);
      if (t && t.isChanges && spec.focus) { t.focusOn = spec.focus; t.refresh(true); }
      renderProjects();
      return t;
    }).catch(function (e) { fail(e); return null; });
  }

  function lastRun(projectName, cmdId) {
    var done = S.tabs.filter(function (t) { return t.info.spec.kind === 'run' && t.info.spec.project === projectName && t.info.spec.cmdId === cmdId && !t.alive && t.exitCode !== undefined; });
    return done[done.length - 1] || null;
  }

  function runningCmd(projectName, cmdId) {
    return S.tabs.find(function (t) { return t.info.spec.kind === 'run' && t.info.spec.project === projectName && t.info.spec.cmdId === cmdId && t.alive && !t.gone; });
  }

  function tabMenu(tab, x, y) {
    if (tab.isChat || tab.isFiles) { tab.menu(x, y, layoutItems(tab)); return; }
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
      e.dataTransfer.setData('application/x-sw-tab', tab.id);
      document.body.classList.add('dragging-tab');
    });
    el.addEventListener('dragend', function (e) {
      // dropped outside every window (on the desktop, the other monitor…): a new window, like VS Code
      var dt = e && e.dataTransfer;
      if (dt && dt.dropEffect === 'none' && outsideWindow(e) && !compact() && S.tabs.indexOf(tab) >= 0) {
        moveToNewWindow(tab, e.screenX - 80, e.screenY - 20);
      } else if (dt && dt.dropEffect === 'move') setTimeout(syncTabs, 400);      // maybe taken by another window
      el.classList.remove('dragging'); dragTab = null;
      document.body.classList.remove('dragging-tab');
      S.tabs.forEach(function (t) { t.el.classList.remove('drop-before', 'drop-after'); });
      L.groups.forEach(function (g) { g.showDrop(null); });
    });
    el.addEventListener('dragover', function (e) {
      if (!(dragTab || foreignDrag(e)) || dragTab === tab) return;
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
      if (foreignDrag(e)) { takeDropped(e, tab.group, null, tab.group.tabs.indexOf(tab) + (after ? 1 : 0)); return; }
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

  // ------------------------------------------------------------------ Claude sessions running on the server
  // From this app, VS Code or a terminal (the server's ~/.claude/sessions). Shown on the project,
  // listed in "Claude sessions", and checked before a conversation is opened a second time.
  var OWNER = { simab: 'this app', vscode: 'VS Code', terminal: 'terminal', other: 'another program' };
  function ownerLabel(x) { return x.tab ? 'here, in a tab' : (OWNER[x.owner] || x.owner) + (x.tmux ? ' (tmux)' : ''); }
  function runningOf(name) { return (S.running && S.running[name]) || []; }
  function runningConv(name, conv) { return runningOf(name).find(function (x) { return x.sessionId === conv; }) || null; }

  function refreshRunning(force) {
    if (!serverReady()) return Promise.resolve();
    return api('GET', 'claude-running' + (force ? '?force=1' : '')).then(function (d) {
      var key = JSON.stringify(Object.keys(d.projects).sort().map(function (k) {
        return [k, d.projects[k].map(function (x) { return [x.pid, x.status, x.tab]; })];
      }));
      S.running = d.projects;
      if (key !== S.runningKey) { S.runningKey = key; renderProjects(); }
      window.dispatchEvent(new CustomEvent('sw-running'));
    }).catch(function () {});
  }

  function runningBadge(p) {
    var list = runningOf(p.name);
    if (!list.length) return null;
    var busy = list.some(function (x) { return x.status === 'busy'; });
    return h('span', { class: 'gb run' + (busy ? ' busy' : ''), title: plural(list.length, 'Claude session') + ' running: ' +
      list.map(function (x) { return ownerLabel(x) + ' · ' + (x.status || '?'); }).join(', ') + ' — click to see them',
      onclick: function (e) { e.stopPropagation(); claudeSessionsDialog(p); } }, '✳' + list.length);
  }

  function claudeStop(project, x) {
    return api('POST', 'claude-running/stop', { project: project, pid: x.pid, sessionId: x.sessionId }).then(function (d) {
      S.running = S.running || {};
      S.running[project] = d.running;
      renderProjects();
      window.dispatchEvent(new CustomEvent('sw-running'));
      return d;
    });
  }

  // continue a conversation here that another Claude is running: stop that one first
  function claudeTakeOver(project, x, title) {
    return claudeStop(project, x).then(function () {
      return openSession({ kind: 'chat', project: project, resume: x.sessionId, title: title || '' });
    }).catch(fail);
  }

  function claudeWatch(project, conv, title) {
    var open = S.tabs.find(function (t) { return t.isChat && !t.gone && t.info.spec.watch && t.info.spec.resume === conv; });
    if (open) { activate(open); return Promise.resolve(open); }
    return openSession({ kind: 'chat', project: project, resume: conv, watch: true, title: 'Watch · ' + (title || project).slice(0, 60) });
  }

  // a conversation another program is running: ask instead of starting a second Claude on it
  function askRunningElsewhere(project, x, title) {
    var where = ownerLabel(x);
    var md = openModal({ title: 'Already running in ' + where, size: 'sm',
      body: h('div', null, '“' + (title || 'This conversation') + '” is running in ' + where + (x.status ? ' (' + x.status + ')' : '') +
        '. Opening it here as well would start a second Claude on the same conversation.'),
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn', onclick: function () { md.close(); claudeWatch(project, x.sessionId, title); } }, 'Watch it'),
        h('button', { class: 'btn primary', onclick: function () { md.close(); claudeTakeOver(project, x, title ? 'Claude · ' + title : ''); } }, 'Stop it there, continue here')] });
  }

  // Claude sessions of one project (p), or of every project (p = null): what runs now — here,
  // in VS Code or a terminal — and the earlier conversations.
  function claudeSessionsDialog(p) {
    var all = !p, past = null, timer = null;
    var runBox = h('div', { class: 'run-list' }, h('div', { class: 'muted' }, 'Looking on the server…'));
    var pastBox = h('div', { class: 'sess-list' }, h('div', { class: 'muted' }, 'Loading…'));
    var filter = h('input', { class: 'inp', placeholder: 'Filter conversations', dir: 'auto' });
    var NOPROJ = 'No project (home folder)';
    function pname(name) { return name ? pTitle(S.projects.find(function (x) { return x.name === name; })) || name : NOPROJ; }
    function titleOf(conv) {
      var c = (past || []).find(function (x) { return x.id === conv; });
      return c ? sessionTitle(c) : '';
    }
    function act(label, cls, fn, title) { return h('button', { class: 'btn sm' + (cls ? ' ' + cls : ''), title: title || null, onclick: fn }, label); }
    function row(proj, x) {
      var t = titleOf(x.sessionId) || x.name || x.sessionId.slice(0, 8);
      var tab = x.tab && S.tabs.find(function (y) { return y.id === x.tab; });
      var acts = [];
      if (tab) acts.push(act('Show', 'primary', function () { md.close(); activate(tab); }));
      else if (x.owner === 'simab') acts.push(act('Open', 'primary', function () { md.close(); openSession({ kind: 'chat', project: proj, resume: x.sessionId, title: 'Claude · ' + t }); }, 'Open its chat here (the same Claude)'));
      else {
        acts.push(act('Watch', '', function () { md.close(); claudeWatch(proj, x.sessionId, t); }, 'See it live here, read-only'));
        acts.push(act('Continue here', 'primary', function () {
          confirmDlg('Stop it in ' + ownerLabel(x) + ' and continue “' + t + '” here?', 'Continue here').then(function (ok) {
            if (ok) { md.close(); claudeTakeOver(proj, x, 'Claude · ' + t); }
          });
        }, 'Stops it there, then opens the conversation here'));
      }
      acts.push(act('Stop', 'danger', function () {
        confirmDlg('Stop the Claude running “' + t + '” in ' + ownerLabel(x) + '? What it is doing now is cut off; the conversation is kept.', 'Stop', true).then(function (ok) {
          if (ok) claudeStop(proj, x).then(function () { toast('Stopped', 'ok'); }).catch(fail);
        });
      }));
      return h('div', { class: 'run-row' },
        h('i', { class: 'dot ' + (x.status === 'busy' ? 'busy' : 'ok'), title: x.status }),
        h('div', { class: 'run-main' },
          h('div', { class: 'st', dir: 'auto' }, t),
          h('div', { class: 'sm' }, ownerLabel(x) + ' · ' + (x.status === 'busy' ? 'working' : x.status || 'running') +
            (x.started ? ' · since ' + agoShort(x.started) : '') + (x.updated ? ' · last activity ' + agoShort(x.updated) + ' ago' : ''))),
        h('div', { class: 'run-acts' }, acts));
    }
    function renderRunning() {
      var names = all ? Object.keys(S.running || {}).sort(function (a, b) { return (a === '') - (b === '') || a.localeCompare(b); }) : [p.name];
      var rows = [];
      names.forEach(function (n) {
        var list = runningOf(n);
        if (!list.length) return;
        if (all) rows.push(h('div', { class: 'run-proj', style: '--pc:' + (n ? projectColor(n) : 'var(--line2)') }, pname(n), h('span', { class: 'muted' }, ' · ' + list.length)));
        list.forEach(function (x) { rows.push(row(n, x)); });
      });
      if (!rows.length) rows.push(h('div', { class: 'muted' }, all ? 'No Claude is running on the server right now.' : 'No Claude is running in this project right now.'));
      runBox.replaceChildren.apply(runBox, rows);
    }
    function renderPast() {
      if (!past) return;
      var q = filter.value.trim().toLowerCase();
      var shown = past.filter(function (c) { return !runningConv(c.proj, c.id) && (!q || (c.title + ' ' + c.first + ' ' + c.last + ' ' + (c.proj || '')).toLowerCase().indexOf(q) >= 0); });
      if (!shown.length) { pastBox.replaceChildren(h('div', { class: 'muted' }, past.length ? 'No match' : 'No conversations yet')); return; }
      pastBox.replaceChildren.apply(pastBox, shown.slice(0, all ? 60 : 40).map(function (c) {
        return h('div', { class: 'sess' },
          h('button', { class: 'sess-main', onclick: function () { md.close(); openSession({ kind: 'chat', project: c.proj, resume: c.id, title: 'Claude · ' + sessionTitle(c) }); } },
            h('div', { class: 'st', dir: 'auto' }, sessionTitle(c)),
            h('div', { class: 'sm' }, (all ? pname(c.proj) + ' · ' : '') + window.SWChat.fmtTime(c.mtime) + ' · ' + plural(c.prompts, 'message'))));
      }));
    }
    filter.addEventListener('input', renderPast);
    var onRun = function () { renderRunning(); renderPast(); };
    window.addEventListener('sw-running', onRun);
    var md = openModal({ title: all ? 'Claude sessions — all projects' : 'Claude sessions — ' + p.name, body: [
      h('div', { class: 'sec-h' }, 'Running now', h('span', { class: 'muted' }, ' · here, in VS Code or in a terminal')), runBox,
      h('div', { class: 'sec-h' }, 'Earlier conversations'), filter, h('div', { style: 'height:6px' }), pastBox],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); openSession(all ? { kind: 'chat' } : { kind: 'chat', project: p.name }); } }, all ? 'New chat (no project)' : 'New chat'),
        h('span', { class: 'grow' }), h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')],
      onclose: function () { clearInterval(timer); window.removeEventListener('sw-running', onRun); } });
    // earlier conversations: of this project, or of every project with a server folder (and home)
    var names = all ? S.projects.filter(hasRemote).map(function (x) { return x.name; }).concat(['']) : [p.name];
    Promise.all(names.map(function (n) {
      return api('GET', 'claude-sessions/' + encodeURIComponent(n)).then(function (d) {
        return (d.sessions || []).map(function (c) { return Object.assign({ proj: n }, c); });
      }).catch(function () { return []; });
    })).then(function (lists) {
      past = [].concat.apply([], lists).sort(function (a, b) { return b.mtime - a.mtime; });
      renderRunning(); renderPast();
    });
    refreshRunning(true).then(renderRunning);
    timer = setInterval(function () { refreshRunning(true); }, 3000);
  }

  function runningTotal() {
    return Object.keys(S.running || {}).reduce(function (n, k) { return n + S.running[k].length; }, 0);
  }

  function notify(title, body) {
    if (!S.settings.bellNotify || !document.hidden || !window.Notification || Notification.permission !== 'granted') return;
    try { new Notification(title, { body: body, icon: 'icon.svg' }); } catch (e) { /* ignore */ }
  }

  function openEditor(p, where) {
    api('POST', 'editor', { project: p.name, where: where }).then(function () { toast('Opening VS Code…', 'ok'); }).catch(fail);
  }

  function copyText(t) {
    if (!t) return;
    navigator.clipboard.writeText(t).then(function () { toast('Copied ' + t, 'ok'); }).catch(fail);
  }
  // the server folder in full (~ becomes the real home on the server)
  function copyServerPath(p) {
    var r = p.remote || '';
    if (r !== '~' && r.indexOf('~/') !== 0) { copyText(r); return; }
    var done = function (home) { copyText(home ? home + r.slice(1) : r); };
    if (S.serverHome) { done(S.serverHome); return; }
    api('GET', 'server-ls?path=' + encodeURIComponent('~/') + '&project=' + encodeURIComponent(p.name)).then(function (d) {
      S.serverHome = d.home || '';
      done(S.serverHome);
    }).catch(function () { done(''); });
  }

  function openFolder(p) {
    api('POST', 'open-folder/' + encodeURIComponent(p.name)).then(function (d) { toast('Opened ' + d.path, 'ok'); }).catch(fail);
  }

  // where a local path really is (~ = the user folder), shown under the field
  function fullLocal(v) {
    v = (v || '').trim();
    if (!v || !S.home) return v;
    var sep = S.home.indexOf('\\') >= 0 ? '\\' : '/';
    if (v === '~') return S.home;
    if (/^~[\\/]/.test(v)) return S.home.replace(/[\\/]$/, '') + sep + v.slice(2).replace(/[\\/]/g, sep);
    return sep === '\\' ? v.replace(/\//g, '\\') : v;
  }

  // a local folder field: Browse… (the system's folder dialog) and the full path under it
  function localFolderField(label, input) {
    var hint = h('div', { class: 'muted mono', style: 'font-size:11px;margin-top:4px;word-break:break-all' });
    function upd() { var f = fullLocal(input.value); hint.textContent = f ? '→ ' + f : ''; }
    input.addEventListener('input', upd);
    input._upd = upd;
    var browse = h('button', { class: 'btn', type: 'button', title: 'Choose the folder', style: 'flex:none', onclick: function () {
      browse.disabled = true;
      api('POST', 'pick-folder', { start: fullLocal(input.value) }).then(function (d) {
        if (d.path) { input.value = d.path; delete input.dataset.auto; upd(); }
      }).catch(fail).then(function () { browse.disabled = false; });
    } }, 'Browse…');
    upd();
    return h('div', { class: 'field' }, h('label', null, label), h('div', { style: 'display:flex;gap:6px' }, input, browse), hint);
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
  function openFiles(p) { return openSession({ kind: 'files', project: p.name }); }
  function openTerminal(p) { openSession({ kind: 'shell', where: hasRemote(p) ? 'server' : 'local', project: p.name }); }
  function openCode(p) { openEditor(p, hasRemote(p) ? 'server' : 'local'); }

  // In the web version the things that need your own computer stay visible but disabled,
  // so people know the installed app has them.
  var APP_ONLY = 'Only in the installed app (on your computer)';
  var WEB_HIDDEN = ['Local terminal', 'Open folder', 'Copy local path', 'VS Code on server', 'VS Code, local folder', 'Port forwarding…', 'Check for updates', 'Quit'];
  function webItems(items) {
    if (!S.web) return items;
    return items.map(function (it) {
      return it && it.label && WEB_HIDDEN.indexOf(it.label) >= 0 ? Object.assign({}, it, { disabled: true, k: 'installed app', action: function () {} }) : it;
    });
  }

  function dockerCount(name) {
    var d = S.docker[name];
    return d && d.total ? d.running + '/' + d.total + (d.bad ? ' · ' + d.bad + ' down' : '') : '';
  }
  function tunnelCount(name) {
    var t = projectTunnels(name);
    return t.length ? t.filter(function (x) { return x.state === 'up'; }).length + '/' + t.length : '';
  }

  function projectMenu(p, anchor) {
    var sync = p.source === 'mutagen';
    var r = hasRemote(p);
    menuAt(anchor, webItems([
      { header: 'Claude' },
      { label: 'New chat', disabled: !r, action: function () { openSession({ kind: 'chat', project: p.name }); } },
      { label: 'Continue last chat', disabled: !r, action: function () { resumeLatest(p.name); } },
      { label: 'Sessions — running and past…', disabled: !r, k: runningOf(p.name).length ? runningOf(p.name).length + ' running' : '', action: function () { claudeSessionsDialog(p); } },
      { label: 'Starter message…', disabled: !r, k: p.starter ? 'on' : '', action: function () { starterDialog(p); } },
      { label: 'Claude text terminal (TUI)', disabled: !r, action: function () { openSession({ kind: 'claude', where: 'server', project: p.name }); } },
      { header: 'Open' },
      { label: 'Files', disabled: !r && !p.localExists, action: function () { openFiles(p); } },
      { label: 'Changes — review, commit', disabled: !r && !p.localExists, action: function () { openSession({ kind: 'changes', project: p.name }); } },
      { label: 'Server terminal', disabled: !r, action: function () { openSession({ kind: 'shell', where: 'server', project: p.name }); } },
      { label: 'Local terminal', disabled: !p.localExists, action: function () { openSession({ kind: 'shell', where: 'local', project: p.name }); } },
      { label: 'VS Code on server', disabled: !r, action: function () { openEditor(p, 'server'); } },
      { label: 'VS Code, local folder', disabled: !p.localExists, action: function () { openEditor(p, 'local'); } },
      { label: 'Open folder', k: p.localExists ? '' : 'not synced', disabled: !p.localExists, action: function () { openFolder(p); } },
      { label: 'Copy server path', disabled: !r, action: function () { copyServerPath(p); } },
      { label: 'Copy local path', disabled: !p.localExists, action: function () { copyText(fullLocal(p.local)); } },
      '-',
      { label: 'Git…', action: function () { gitDialog(p); } },
      { label: 'Skills…', disabled: !r, action: function () { skillsDialog(p); } },
      { label: 'Docker containers…', disabled: !r, k: dockerCount(p.name), action: function () { dockerDialog(p); } },
      { label: 'Port forwarding…', disabled: !r, k: tunnelCount(p.name), action: function () { tunnelsDialog(p); } },
      { label: 'Rename…', action: function () { renameProject(p); } },
      { label: 'Colour…', action: function () { colorDialog(p); } },
      { label: 'History…', action: function () { historyDialog(p.name); } },
      { label: 'Run commands…', action: function () { editCommands(p); } },
      sync ? { header: 'Sync · ' + (p.sync ? p.sync.label : '') } : null,
      sync ? { label: 'Sync now', action: function () { syncAction(p, 'flush'); } } : null,
      sync ? (p.sync && p.sync.paused ? { id: 'Pause / resume sync', label: 'Resume sync', action: function () { syncAction(p, 'resume'); } } : { id: 'Pause / resume sync', label: 'Pause sync', action: function () { syncAction(p, 'pause'); } }) : null,
      sync ? { id: 'Sync details…', label: 'Sync details' + (p.sync && p.sync.conflicts ? ' · ' + plural(p.sync.conflicts, 'conflict') : '') + '…', action: function () { syncDetails(p); } } : null,
      sync ? { id: '.git sync', label: p.sync && p.sync.git === false ? 'Sync .git folders too' : 'Stop syncing .git folders', action: function () {
        var on = !(p.sync && p.sync.git !== false);
        confirmDlg(on ? 'Sync the .git folders of “' + p.name + '” too? Git then works the same on both sides. (Do not run git on both sides at the same moment.)'
          : 'Stop syncing the .git folders of “' + p.name + '”? Each side then keeps its own git history.', on ? 'Sync .git' : 'Stop', !on).then(function (ok) {
          if (ok) api('POST', 'git-sync/' + encodeURIComponent(p.name), { on: on }).then(function () { toast('Updating sync settings — this takes a moment', 'ok'); setTimeout(refreshProjects, 4000); }).catch(fail);
        });
      } } : null,
      sync ? { label: 'Not synced…', action: function () { ignoresDialog(p); } } : null,
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
    ]), { star: 'menu' });
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
    prompt: ['✳', 'Claude'], chat: ['✳', 'chat'], 'claude-tui': ['✳', 'Claude (terminal)'], 'claude-stop': ['✳', 'Claude stopped'],
    run: ['▶', 'run'], 'run-exit': ['■', 'finished'], terminal: ['›', 'terminal'],
    'git-commit': ['⎇', 'commit'], 'git-push': ['⎇', 'push'], 'git-pull': ['⎇', 'pull'], 'git-fetch': ['⎇', 'fetch'], 'git-switch': ['⎇', 'branch'],
    'docker-logs': ['▣', 'logs'], 'docker-shell': ['▣', 'container shell'], 'docker-rebuild': ['▣', 'rebuild'],
    'docker-start': ['▣', 'start'], 'docker-stop': ['▣', 'stop'], 'docker-restart': ['▣', 'restart'], 'docker-crash': ['▣', 'crashed'],
    tunnels: ['⇄', 'ports'],
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
    if (it.kind.indexOf('docker-') === 0 && p) out.push({ label: 'Docker', fn: function () { dockerDialog(p); } });
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
      var key = JSON.stringify(Object.keys(d.summary).map(function (k) { var g = d.summary[k]; return [k, g.changes, g.ahead, g.behind, g.conflicts, g.repos, g.branch, g.branches]; }));
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

  function agoShort(t) {
    if (!t) return '';
    var s = Date.now() / 1000 - t;
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + 'm' : s < 86400 ? Math.round(s / 3600) + 'h' : s < 86400 * 60 ? Math.round(s / 86400) + 'd'
      : new Date(t * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  // ------------------------------------------------------------------ branches
  function branchLabel(p) {
    var g = S.git[p.name];
    if (!g || !g.repos) return null;
    var text = g.branch ? g.branch : g.branches > 1 ? g.branches + ' branches' : '';
    if (!text) return null;
    return h('span', { class: 'pbranch', title: g.branch ? 'Git branch: ' + g.branch + ' — click to switch' : 'The repositories are on different branches — click',
      onclick: function (e) {
        e.stopPropagation();
        if (g.repo !== null && g.repo !== undefined) branchPicker(p, g.repo, g.where, function () {});
        else gitDialog(p);
      } }, ' ⎇ ' + text);
  }

  // Switch branch: local branches, branches only on the remote (GitHub…), or a new one.
  function branchPicker(p, repo, where, done) {
    var data = null, busy = false;
    var filter = h('input', { class: 'inp', placeholder: 'Find or create a branch', dir: 'auto', spellcheck: 'false', autofocus: true });
    var list = h('div', { class: 'blist' }, h('div', { class: 'muted' }, 'Loading branches…'));
    var path = 'git/' + encodeURIComponent(p.name) + '/';
    var md = openModal({ title: 'Branch — ' + p.name + (repo ? ' / ' + repo : ''), size: 'sm', body: [filter, list],
      onclose: function () { if (!busy) done(null); } });
    var ago = agoShort;
    function go(body, label) {
      if (busy) return;
      busy = true;
      list.replaceChildren(h('div', { class: 'muted' }, 'Switching to ' + label + '…'));
      api('POST', path + 'switch', Object.assign({ repo: repo, where: where }, body)).then(function (d) {
        busy = false;
        toast(p.name + ': on ' + (d.status.branch || label), 'ok');
        window.dispatchEvent(new CustomEvent('sw-git-changed', { detail: p.name }));
        refreshGit(p.name);
        md.close();
        done(d.status);
      }).catch(function (e) {
        busy = false;
        if (e.status === 409 && !body.stash) {
          confirmDlg(e.message + ' Put them aside (git stash) and switch to “' + label + '”? You can get them back later with git stash pop.', 'Stash and switch').then(function (ok) {
            if (ok) go(Object.assign({}, body, { stash: true }), label); else render();
          });
          return;
        }
        fail(e);
        render();
      });
    }
    function row(name, sub, cls, onclick) {
      return h('button', { class: 'brow' + (cls ? ' ' + cls : ''), title: name, onclick: onclick },
        h('span', { class: 'bname mono', dir: 'auto' }, name), h('span', { class: 'bsub muted', dir: 'auto' }, sub));
    }
    function render() {
      if (!data) return;
      var q = filter.value.trim(), ql = q.toLowerCase();
      var match = function (b) { return !ql || b.name.toLowerCase().indexOf(ql) >= 0; };
      var rows = [];
      var loc = data.local.filter(match), rem = data.remote.filter(function (b) { return !b.local && match(b); });
      if (loc.length) rows.push(h('div', { class: 'bhead' }, 'Branches'));
      loc.forEach(function (b) {
        var cur = b.name === data.current;
        rows.push(row((cur ? '✓ ' : '') + b.name, [ago(b.date), b.subject].filter(Boolean).join(' · '), cur ? 'cur' : '',
          function () { if (cur) md.close(); else go({ branch: b.name }, b.name); }));
      });
      if (rem.length) rows.push(h('div', { class: 'bhead' }, 'Only on the remote'));
      rem.forEach(function (b) {
        rows.push(row(b.name, [ago(b.date), b.subject].filter(Boolean).join(' · '), '', function () { go({ branch: b.name, track: true }, b.name.split('/').slice(1).join('/')); }));
      });
      var exists = data.local.some(function (b) { return b.name === q; });
      if (q && !exists) rows.push(row('+ Create branch “' + q + '”', 'from ' + (data.current || 'here') + ', and switch to it', 'create',
        function () { go({ branch: q, create: true }, q); }));
      if (!rows.length) rows.push(h('div', { class: 'muted' }, 'No branch matches.'));
      list.replaceChildren.apply(list, rows);
    }
    filter.addEventListener('input', render);
    filter.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var first = list.querySelector('.brow');
      if (first) first.click();
    });
    api('GET', path + 'branches?repo=' + encodeURIComponent(repo || '') + '&where=' + encodeURIComponent(where || '')).then(function (d) {
      data = d;
      render();
    }).catch(function (e) { list.replaceChildren(h('div', { class: 'muted' }, e.message)); });
    setTimeout(function () { filter.focus(); }, 40);
  }

  function skillsDialog(p) {
    var skRepos = [], skills = [], installed = [], selRepo = null;
    var repoList = h('div', { class: 'sk-repos' });
    var repoInput = h('input', { class: 'inp', placeholder: 'https://github.com/owner/repo', style: 'flex:1', autofocus: true });
    var skillList = h('div', { class: 'sk-skills' });
    var instList = h('div', { class: 'sk-inst' });
    var statusEl = h('div', { class: 'sk-status hidden' });

    function setStatus(msg, cls) {
      statusEl.textContent = msg || '';
      statusEl.className = 'sk-status' + (msg ? '' : ' hidden') + (cls ? ' ' + cls : '');
    }

    function renderRepos() {
      repoList.replaceChildren.apply(repoList, skRepos.map(function (r, i) {
        var active = selRepo && selRepo.url === r.url;
        return h('div', { class: 'sk-repo-row' + (active ? ' sel' : ''), onclick: function () { selRepo = r; renderRepos(); loadSkills(r); } },
          h('span', { class: 'sk-repo-name' }, r.name || r.url.replace('https://github.com/', '')),
          h('button', { class: 'icon-btn', title: 'Remove', onclick: function (e) {
            e.stopPropagation();
            skRepos.splice(i, 1);
            if (selRepo && selRepo.url === r.url) { selRepo = null; skills = []; skillList.replaceChildren(); }
            saveRepos(); renderRepos();
          } }, '✕'));
      }));
    }

    function saveRepos() { api('PUT', 'skills/repos', { repos: skRepos }).catch(fail); }

    function loadSkills(repo) {
      setStatus('Loading skills…');
      skillList.replaceChildren();
      var b64 = btoa(unescape(encodeURIComponent(repo.url))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
      api('GET', 'skills/list/' + b64).then(function (d) {
        skills = d.skills || [];
        setStatus('');
        renderSkills();
      }).catch(function (e) { setStatus('Failed: ' + (e.message || 'error'), 'err'); });
    }

    function renderSkills() {
      skillList.replaceChildren.apply(skillList, skills.length
        ? skills.map(function (s) {
          var isInst = installed.indexOf(s.name) >= 0;
          return h('div', { class: 'sk-skill-row' },
            h('span', { class: 'sk-skill-name' }, s.name.replace(/\.md$/, '')),
            isInst
              ? h('button', { class: 'btn sm danger', onclick: function () { uninstallSkill(s.name); } }, 'Uninstall')
              : h('button', { class: 'btn sm', onclick: function () { installSkill(s); } }, 'Install'));
        })
        : [h('div', { class: 'sk-empty' }, 'No .md skills found in this repo')]);
    }

    function loadInstalled() {
      if (!hasRemote(p)) { renderInstalled(); return; }
      api('GET', 'skills/installed/' + encodeURIComponent(p.name)).then(function (d) {
        installed = d.skills || [];
        renderInstalled();
        if (skills.length) renderSkills();
      }).catch(function () {});
    }

    function renderInstalled() {
      instList.replaceChildren.apply(instList, installed.length
        ? installed.map(function (name) {
          return h('div', { class: 'sk-inst-row' },
            h('span', null, name.replace(/\.md$/, '')),
            h('button', { class: 'btn sm danger', onclick: function () { uninstallSkill(name); } }, 'Remove'));
        })
        : [h('div', { class: 'sk-empty' }, hasRemote(p) ? 'No skills installed' : 'Project has no server')]);
    }

    function installSkill(s) {
      if (!hasRemote(p)) { toast('Project has no server', 'err'); return; }
      if (!selRepo) return;
      setStatus('Installing…');
      api('POST', 'skills/install/' + encodeURIComponent(p.name), { name: s.name, repo_url: selRepo.url, path: s.path })
        .then(function () { setStatus('Installed', 'ok'); loadInstalled(); })
        .catch(function (e) { setStatus('Install failed: ' + (e.message || 'error'), 'err'); });
    }

    function uninstallSkill(name) {
      api('DELETE', 'skills/installed/' + encodeURIComponent(p.name) + '/' + encodeURIComponent(name))
        .then(function () { loadInstalled(); if (skills.length) renderSkills(); })
        .catch(fail);
    }

    api('GET', 'skills/repos').then(function (d) {
      skRepos = d.repos || [];
      renderRepos();
    }).catch(fail);
    loadInstalled();

    var body = h('div', { class: 'sk-body' },
      h('div', { class: 'sk-left' },
        h('div', { class: 'sk-section-head' }, 'Repositories'),
        h('div', { class: 'sk-add-row' }, repoInput,
          h('button', { class: 'btn sm', onclick: function () {
            var url = repoInput.value.trim();
            if (!url) return;
            if (!/^https?:\/\/github\.com\//.test(url)) { toast('Enter a GitHub repo URL', 'err'); return; }
            var name = url.replace(/^https?:\/\/github\.com\//, '').replace(/\.git$/, '');
            skRepos.push({ url: url, name: name });
            saveRepos(); renderRepos(); repoInput.value = '';
            selRepo = skRepos[skRepos.length - 1]; renderRepos(); loadSkills(selRepo);
          } }, 'Add')),
        repoList,
        h('div', { class: 'sk-section-head', style: 'margin-top:12px' }, 'Installed in ' + p.name),
        instList),
      h('div', { class: 'sk-right' },
        h('div', { class: 'sk-section-head', id: 'sk-right-head' }, 'Select a repository →'),
        statusEl,
        skillList));

    openModal({ title: 'Skills — ' + p.name, size: 'wide', body: body });
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
          repo = r; current = null; st = null; histData = null; histSel = '';
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
    btns.discard = h('button', { class: 'btn danger', disabled: true, title: 'Discard selected changes (cannot be undone)', onclick: function () {
      var sel = st ? st.files.filter(function (f) { return picked.has(f.path); }) : [];
      if (sel.length) discard(sel);
    } }, 'Discard…');
    var allBox = h('input', { type: 'checkbox', checked: true, title: 'Select all', onchange: function () {
      picked = allBox.checked ? new Set(st.files.map(function (f) { return f.path; })) : new Set();
      renderList();
    } });
    msg.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commit(); } });

    // left side: the changes, or the history of the branch (what is pushed, what is not)
    var view = 'changes', histData = null, histSel = '';
    var changesTab = h('button', { class: 'gtab on', onclick: function () { showView('changes'); } }, 'Changes');
    var historyTab = h('button', { class: 'gtab', onclick: function () { showView('history'); } }, 'History');
    var gtabs = h('div', { class: 'gtabs-bar' }, changesTab, historyTab);
    var changesHead = h('label', { class: 'gall' }, allBox, 'Select all');
    var hist = h('div', { class: 'glist hidden' });
    function showView(v) {
      view = v;
      changesTab.classList.toggle('on', v === 'changes');
      historyTab.classList.toggle('on', v === 'history');
      changesHead.classList.toggle('hidden', v !== 'changes');
      list.classList.toggle('hidden', v !== 'changes');
      hist.classList.toggle('hidden', v !== 'history');
      if (v === 'history') loadHistory();
    }
    var HSTATE = { push: ['↑', 'Not pushed yet — only on this branch here'], pull: ['↓', 'Not pulled yet — only on the remote'],
      synced: ['✓', 'On both sides'], local: ['•', 'This branch is not on the remote yet — Push publishes it'] };
    function loadHistory() {
      if (!st || !st.repo) return;
      if (!histData) hist.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, 'Loading history…'));
      api('GET', 'git/' + encodeURIComponent(p.name) + '/log' + qs()).then(function (d) {
        histData = d;
        renderHistory();
      }).catch(function (e) { hist.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, e.message)); });
    }
    function renderHistory() {
      var d = histData;
      if (!d) return;
      var sum = !d.upstream ? h('div', { class: 'hsum local' }, 'Not on the remote yet — Push to publish this branch.')
        : d.ahead || d.behind ? h('div', { class: 'hsum' + (d.behind ? ' pull' : ' push') },
          [d.ahead ? '↑ ' + plural(d.ahead, 'commit') + ' to push' : '', d.behind ? '↓ ' + plural(d.behind, 'commit') + ' to pull' : ''].filter(Boolean).join(' · '),
          h('small', null, ' (as of the last fetch)'))
          : h('div', { class: 'hsum ok' }, '✓ In sync with ' + d.upstream, h('small', null, ' (as of the last fetch)'));
      var rows = d.commits.map(function (c) {
        var mk = HSTATE[c.state] || ['', ''];
        return h('div', { class: 'hrow ' + c.state + (c.hash === histSel ? ' sel' : ''), title: mk[1] + '\n' + c.short + ' · ' + c.author, onclick: function () { showCommit(c); } },
          h('span', { class: 'hm ' + c.state }, mk[0]),
          h('span', { class: 'hbody' },
            h('span', { class: 'hsub', dir: 'auto' }, c.subject),
            h('span', { class: 'hmeta' }, h('span', { class: 'mono' }, c.short), ' · ' + c.author + ' · ' + agoShort(c.date),
              c.refs.filter(function (r) { return r !== 'HEAD'; }).map(function (r) { return h('span', { class: 'href mono' }, r.replace(/^HEAD -> /, '')); }))));
      });
      if (!rows.length) rows.push(h('div', { class: 'muted', style: 'padding:10px' }, 'No commits yet.'));
      hist.replaceChildren.apply(hist, [sum].concat(rows));
    }
    function showCommit(c) {
      histSel = c.hash;
      renderHistory();
      diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, 'Loading…'));
      api('GET', 'git/' + encodeURIComponent(p.name) + '/show' + qs() + '&hash=' + encodeURIComponent(c.hash)).then(function (d) {
        if (histSel !== c.hash) return;
        diff.replaceChildren(h('div', { class: 'gdhead' }, h('span', { dir: 'auto' }, c.subject), h('span', { class: 'mono muted' }, c.short)), diffEl(d.diff));
      }).catch(function (e) { diff.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, e.message)); });
    }
    function untrack(files) {
      setBusy(true, 'untracking');
      gpost('untrack', { files: files.map(function (f) { return f.path; }) }).then(function (d) {
        setBusy(false, 'untrack');
        apply(d.status);
        toast('Git no longer tracks ' + (files.length === 1 ? files[0].path : plural(files.length, 'file')) + ' — commit to finish (the files stay on disk)', 'ok', { timeout: 7000 });
      }).catch(function (e) { setBusy(false, 'untrack'); fail(e); });
    }

    function discard(files) {
      var label = files.length === 1 ? '"' + files[0].path.split('/').pop() + '"' : files.length + ' files';
      var isDel = files.some(function (f) { return f.untracked; });
      var msg2 = isDel ? 'Delete new file ' + label + ' and discard all changes? This cannot be undone.'
        : 'Discard changes to ' + label + '? The ' + (files.length === 1 ? 'file goes' : 'files go') + ' back to the last commit. This cannot be undone.';
      confirmDlg(msg2, isDel ? 'Delete' : 'Discard', true).then(function (ok) {
        if (!ok) return;
        setBusy(true, 'discarding');
        gpost('discard', { files: files.map(function (f) { return f.path; }) }).then(function (d) {
          setBusy(false, 'discard'); apply(d.status);
          toast('Discarded ' + label, 'ok');
        }).catch(function (e) { setBusy(false, 'discard'); fail(e); });
      });
    }

    var md = openModal({
      title: 'Git — ' + p.name, size: 'wide', onclose: function () { refreshGit(p.name); },
      body: [switcher, head, banner, h('div', { class: 'ggrid' }, h('div', { class: 'gleft' }, gtabs, changesHead, list, hist), diff)],
      foot: [btns.fetch, btns.pull, btns.push, btns.discard, h('span', { class: 'grow' }), h('div', { class: 'gcommit' }, msg), commitBtn]
    });

    function setBusy(b, what) {
      busy = b;
      Object.keys(btns).forEach(function (k) { btns[k].disabled = b; });
      commitBtn.disabled = b || !st || !st.files.length || !picked.size;
      btns.discard.disabled = b || !picked.size;
      if (what) head.lastChild.textContent = b ? what + '…' : '';
    }

    function renderHead() {
      var ab = !st.upstream ? '' : [st.ahead ? '↑' + st.ahead + ' to push' : '', st.behind ? '↓' + st.behind + ' to pull' : ''].filter(Boolean).join(' · ');
      head.replaceChildren(
        h('button', { class: 'gbranch mono', title: 'Switch branch', onclick: function () {
          branchPicker(p, repo, where, function (ns) { if (ns) apply(ns); });
        } }, '⎇ ' + (st.branch || '(no branch)') + ' ▾'),
        st.upstream ? h('span', { class: 'muted mono' }, ' → ' + st.upstream) : h('span', { class: 'muted' }, ' · no upstream'),
        ab ? h('span', { class: 'mono gab', title: 'As of the last fetch' }, ab) : st.upstream ? h('span', { class: 'gsync', title: 'As of the last fetch' }, '✓ in sync') : null,
        h('span', { class: 'muted' }, ' · runs ' + (st.where === 'local' ? 'on this machine' : 'on the server')),
        st.last ? h('span', { class: 'muted mono glast', title: 'Last commit' }, st.last) : null,
        h('span', { class: 'muted gwork' }));
      var problems = [];
      if (p.sync && p.sync.git === false) {
        problems.push(h('div', null, 'The .git folder of this project is not synced, so the server and this machine can disagree. ',
          h('button', { class: 'btn sm', onclick: function () { md.close(); gitSyncOn(p); } }, 'Sync .git too')));
      }
      var ign = st.files.filter(function (f) { return f.ignored; });
      if (ign.length) {
        problems.push(h('div', null, plural(ign.length, 'file') + ' named in .gitignore ' + (ign.length === 1 ? 'is' : 'are') +
          ' still tracked by git (committed before), so changes show here anyway. ',
          h('button', { class: 'btn sm', title: ign.map(function (f) { return f.path; }).join('\n'), onclick: function () { untrack(ign); } }, 'Stop tracking ' + (ign.length === 1 ? 'it' : 'them'))));
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
          return h('div', { class: 'gfile' + (current === f.path ? ' sel' : '') + (f.conflict ? ' conflict' : ''), title: (GIT_LABEL[code] || code) + ': ' + f.path, onclick: function () { showDiff(f); },
            oncontextmenu: function (e) { e.preventDefault(); showMenu(e.clientX, e.clientY, [{ label: (f.untracked ? 'Delete file…' : 'Discard changes…'), action: function () { discard([f]); } }]); } },
            cb, h('span', { class: 'gcode c' + (code === '?' ? 'N' : code) }, code === '?' ? 'N' : code),
            h('span', { class: 'gname' }, name), h('span', { class: 'gdir' }, dir),
            f.ignored ? h('span', { class: 'ftag', title: 'Named in .gitignore, but git tracks it' }, 'ignored') : null);
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
      changesTab.textContent = st.files.length ? 'Changes · ' + st.files.length : 'Changes';
      if (view === 'history') loadHistory();
      renderSwitcher();
      // keep the user's selection; new files start selected
      var next = new Set();
      st.files.forEach(function (f) { if (!had || !had.has(f.path) || picked.has(f.path)) next.add(f.path); });
      picked = next;
      allBox.checked = picked.size === st.files.length;
      btns.discard.disabled = busy || !picked.size;
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
      root.replaceChildren(S.web
        ? h('div', { class: 'side-note' }, 'No projects yet. ', h('button', { class: 'add-link', onclick: addManualProject }, '+ add a project'), ' — pick its folder on the server.')
        : h('div', { class: 'side-note' },
        'No projects yet. ', h('button', { class: 'add-link', onclick: addManualProject }, '+ add a project'),
        ' — from the server, from this machine, or a server folder.'));
      return;
    }
    var list = S.projects.filter(function (p) { return !f || p.name.toLowerCase().indexOf(f) >= 0 || pTitle(p).toLowerCase().indexOf(f) >= 0; });
    root.replaceChildren.apply(root, list.map(projEl).concat([h('button', { class: 'add-link', style: 'margin:6px 2px', onclick: addManualProject }, '+ add project')]));
  }

  function projEl(p) {
    var open = S.open.has(p.name);
    var count = S.tabs.filter(function (t) { return t.info.spec.project === p.name && !t.gone; }).length;
    var sync = p.sync;
    var dot = sync ? sync.state : (p.localExists || p.remote ? 'ok' : '');
    var el = h('div', { class: 'proj' + (open ? ' open' : ''), style: '--pc:' + projectColor(p.name) });
    var more = h('button', { class: 'icon-btn', title: 'More', onclick: function (e) { e.stopPropagation(); projectMenu(p, more); } }, '⋯');
    var tip = [p.title ? 'name: ' + p.name : '', p.remote ? 'server: ' + p.remote : '', p.local ? 'local: ' + p.local : '',
      sync ? 'sync: ' + sync.label + (sync.conflicts ? ', ' + plural(sync.conflicts, 'conflict') : '') : ''].filter(Boolean).join('\n');
    el.append(h('div', { class: 'proj-head', title: tip, onclick: function () {
      if (S.open.has(p.name)) S.open.delete(p.name); else S.open.add(p.name);
      localStorage.setItem('sw-open', JSON.stringify(Array.from(S.open)));
      el.classList.toggle('open');
    } }, h('i', { class: 'dot ' + dot }), h('span', { class: 'proj-name', dir: 'auto' }, pTitle(p), branchLabel(p)), runningBadge(p), open ? null : gitBadges(p.name, true),
      sync && sync.conflicts ? h('span', { class: 'gb conflict', title: plural(sync.conflicts, 'sync conflict') + ' — click to resolve', onclick: function (e) { e.stopPropagation(); syncDetails(p); } }, '⇅' + sync.conflicts) : null,
      S.docker[p.name] && S.docker[p.name].bad ? h('span', { class: 'gb conflict', title: plural(S.docker[p.name].bad, 'container') + ' with problems — click', onclick: function (e) { e.stopPropagation(); dockerDialog(p); } }, '▣' + S.docker[p.name].bad) : null,
      more));

    var pr = sync && sync.progress;
    if (pr) {
      var prLabel = pr.totalBytes
        ? (pr.percent + '% · ' + fmtBytes(pr.bytes) + ' / ' + fmtBytes(pr.totalBytes) + ' · ' + pr.files + '/' + pr.totalFiles + ' items')
        : (pr.percent + '% · ' + pr.files + '/' + pr.totalFiles + ' items');
      el.append(h('div', { class: 'sbar', title: prLabel },
        h('i', { style: 'width:' + pr.percent + '%' }), h('span', null, prLabel)));
    }
    var r = hasRemote(p);
    var body = h('div', { class: 'proj-body' });
    body.append(h('div', { class: 'acts' },
      h('button', { class: 'act', disabled: !r, title: 'Chat with Claude in this project (runs on the server)', onclick: function () { openSession({ kind: 'chat', project: p.name }); } }, 'Claude'),
      h('button', { class: 'act git', disabled: !r && !p.localExists, title: 'Changes, commit, pull, push', onclick: function () { gitDialog(p); } }, 'Git', gitBadges(p.name)),
      h('button', { class: 'act more', title: 'Terminal, VS Code, Docker, ports, history…', onclick: function (e) { e.stopPropagation(); projectMenu(p, e.currentTarget); } }, '⋯')));
    p.commands.forEach(function (c) {
      if (S.web && c.where !== 'server') {
        body.append(h('div', { class: 'cmd off', title: c.cmd + '\nruns on your computer — ' + APP_ONLY },
          h('span', { class: 'ic' }, '▶'), h('span', { class: 'nm' }, c.name), h('span', { class: 'tag' }, 'loc')));
        return;
      }
      var t = runningCmd(p.name, c.id);
      var last = t ? null : lastRun(p.name, c.id);
      var res = last ? (last.exitCode === 0 ? h('span', { class: 'res ok', title: 'last run: finished fine' }, '✓') : h('span', { class: 'res err', title: 'last run: exit code ' + last.exitCode }, '✗' + (last.exitCode != null ? last.exitCode : ''))) : null;
      var link = t && t.foundUrl && c.where !== 'server' ? h('a', { class: 'res link', href: t.foundUrl, target: '_blank', rel: 'noopener', title: 'Open ' + t.foundUrl, onclick: function (e) { e.stopPropagation(); } }, '↗') : null;
      body.append(h('div', { class: 'cmd' + (t ? ' running' : ''), title: c.cmd + (c.cwd ? '\nin ' + c.cwd : '') + (c.where === 'server' ? '\nruns on the server' : '\nruns on this computer') + (c.shared ? '\nshared with the project (.simab.json)' : '') + (t ? '\nclick to stop' : ''),
        onclick: function () { runCommand(p, c); } },
        h('span', { class: 'ic' }, t ? '■' : '▶'), h('span', { class: 'nm' }, c.name), res, link,
        h('span', { class: 'tag' }, (c.shared ? '⇄ ' : '') + (c.where === 'server' ? 'srv' : 'loc'))));
    });
    if (!p.commands.length) body.append(h('button', { class: 'add-link', onclick: function () { editCommands(p); } }, '+ run command'));
    projectTunnels(p.name).forEach(function (t) { var tl = tunnelLine(t); if (tl) body.append(tl); });
    el.append(body);
    return el;
  }

  // ------------------------------------------------------------------ run command editor
  // The folder of a run command: browse the project's folders on the server, keep the path
  // relative to the project ("" = project root); folders outside the project are refused.
  function pickCmdFolder(p, rel, done) {
    api('GET', 'browse?path=' + encodeURIComponent(p.remote)).then(function (d) {
      var root = d.path.replace(/\/$/, '');
      serverFolderDialog({ title: 'Folder of the command — ' + p.name, start: rel ? root + '/' + rel.replace(/^\/+/, '') : root,
        note: 'Pick a folder inside the project (' + root + ').',
        pick: function (path) {
          path = path.replace(/\/$/, '');
          if (path === root) done('');
          else if (path.indexOf(root + '/') === 0) done(path.slice(root.length + 1));
          else toast('Pick a folder inside the project', 'err');
        } });
    }).catch(fail);
  }

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
      where.value = S.web ? (c.shared && c.where === 'local' ? 'local' : 'server') : (c.where || 'local');
      if (S.web) { where.disabled = true; where.title = 'In the web version commands run on the server. Local commands: ' + APP_ONLY; }
      var shared = h('input', { type: 'checkbox' });
      shared.checked = !!c.shared;
      var cmd = h('textarea', { class: 'inp mono', rows: 2, value: c.cmd || '', placeholder: 'npm start', spellcheck: 'false' });
      var cwd = h('input', { class: 'inp mono', value: c.cwd || '', placeholder: 'folder (empty = project root)', list: dirsId });
      var url = h('input', { class: 'inp mono', value: c.url || '', placeholder: 'URL to open (optional)' });
      var browse = h('button', { class: 'btn cwd-browse', title: 'Pick a folder of the project on the server', disabled: !hasRemote(p),
        onclick: function () { pickCmdFolder(p, cwd.value.trim(), function (rel) { cwd.value = rel; }); } }, 'Browse…');
      var box = h('div', { class: 'cmd-edit' },
        h('div', { class: 'top' }, name, where,
          h('button', { class: 'icon-btn', title: 'Move up', onclick: function () { var s = box.previousElementSibling; if (s) list.insertBefore(box, s); } }, '↑'),
          h('button', { class: 'icon-btn', title: 'Remove', onclick: function () { box.remove(); } }, '✕')),
        h('div', { class: 'field', style: 'margin-bottom:6px' }, cmd),
        h('div', { class: 'row' }, h('div', { style: 'display:flex;gap:6px;flex:1;min-width:0' }, cwd, browse), url),
        h('label', { class: 'check', style: 'margin-top:6px', title: 'Saved in the project as .simab.json — every device and everyone on the project gets it (commit it to git to share)' },
          shared, ' shared with the project (.simab.json)'));
      box._get = function () { return { id: c.id, name: name.value, where: where.value, cmd: cmd.value, url: url.value, cwd: cwd.value, shared: shared.checked }; };
      return box;
    }
    p.commands.forEach(function (c) { list.append(row(Object.assign({}, c))); });
    var newWhere = S.web || !p.localExists ? 'server' : 'local';
    if (!p.commands.length) list.append(row({ where: newWhere }));
    var sugBtn = h('button', { class: 'btn', onclick: function () {
      api('GET', 'suggest/' + encodeURIComponent(p.name)).then(function (d) {
        if (!d.suggestions.length) { toast('Nothing to suggest (looks for package.json, compose, go.mod)', 'warn'); return; }
        menuAt(sugBtn, d.suggestions.map(function (s) { return { label: s.name, k: s.where, action: function () { list.append(row(s)); } }; }));
      }).catch(fail);
    } }, 'Suggest');
    var md = openModal({
      title: 'Run commands — ' + p.name,
      body: [h('div', { class: 'muted', style: 'margin-bottom:10px;font-size:12px' }, S.web ? 'Each command becomes a ▶ under the project and runs on the server, in its own tab.'
        : 'Each command becomes a ▶ under the project and runs in its own tab. Local runs on this machine (synced folder), Server runs over SSH.'), dirs, list],
      foot: [h('button', { class: 'btn left', onclick: function () { list.append(row({ where: newWhere })); } }, '+ Add'), sugBtn,
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: function () {
          var cmds = Array.prototype.map.call(list.children, function (b) { return b._get(); }).filter(function (c) { return c.name.trim() || c.cmd.trim(); });
          api('POST', 'commands/' + encodeURIComponent(p.name), { commands: cmds }).then(function () { md.close(); refreshProjects(); }).catch(fail);
        } }, 'Save')]
    });
  }

  // Browse folders on the server: click a folder to go into it; the folder you are in is the one picked.
  function folderPicker(onChange, start) {
    var cur = '', showHidden = false, data = null;
    var pathIn = h('input', { class: 'inp mono', placeholder: '~/my-project', spellcheck: 'false', autocomplete: 'off' });
    var list = h('div', { class: 'flist' }, h('div', { class: 'muted', style: 'padding:10px' }, 'Loading folders on the server…'));
    var crumbs = h('div', { class: 'crumbs' });
    var hid = h('input', { type: 'checkbox' });
    hid.addEventListener('change', function () { showHidden = hid.checked; render(); });
    pathIn.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); go(pathIn.value.trim() || '~'); } });
    function render() {
      if (!data) return;
      var parts = data.path.split('/').filter(Boolean), acc = '';
      crumbs.replaceChildren.apply(crumbs, [h('button', { class: 'crumb', title: 'Your home folder', onclick: function () { go('~'); } }, '⌂')]
        .concat([h('button', { class: 'crumb', onclick: function () { go('/'); } }, '/')])
        .concat(parts.map(function (p, i) {
          acc += '/' + p;
          var to = acc;
          return h('button', { class: 'crumb' + (i === parts.length - 1 ? ' on' : ''), onclick: function () { go(to); } }, p + (i < parts.length - 1 ? ' /' : ''));
        })));
      var dirs = data.dirs.filter(function (d) { return showHidden || !d.hidden; });
      var rows = [];
      if (data.parent) rows.push(h('button', { class: 'frow', onclick: function () { go(data.parent); } }, h('span', { class: 'fname mono' }, '..'), h('span', { class: 'ftag' }, 'up')));
      dirs.forEach(function (d) {
        var full = data.path.replace(/\/$/, '') + '/' + d.name;
        rows.push(h('button', { class: 'frow', title: full + (d.project ? '\nalready added as ' + d.project : ''), onclick: function () { go(full); } },
          h('span', { class: 'fname mono' }, d.name),
          d.git ? h('span', { class: 'ftag' }, 'git') : null,
          d.project ? h('span', { class: 'ftag done' }, d.project) : null));
      });
      if (!dirs.length) rows.push(h('div', { class: 'muted', style: 'padding:10px' }, 'No folders inside'));
      list.replaceChildren.apply(list, rows);
    }
    function go(path) {
      api('GET', 'browse?path=' + encodeURIComponent(path)).then(function (d) {
        data = d; cur = d.path;
        pathIn.value = d.path;
        list.scrollTop = 0;
        render();
        onChange(d);
      }).catch(function (e) { list.replaceChildren(h('div', { class: 'add-err' }, e.message)); });
    }
    go(start || '~');
    var el = h('div', { class: 'fpick' }, crumbs, list,
      h('div', { class: 'fpick-foot' }, pathIn, h('label', { class: 'check', style: 'margin:0;flex:none' }, hid, h('span', null, 'hidden'))));
    el.value = function () { return pathIn.value.trim(); };
    return el;
  }

  // A server folder in a window of its own: go into folders and out again, then pick one.
  // opts: { title, start, note, newName (offer "Inside it: <newName>"), pick(path) }
  function serverFolderDialog(opts) {
    var cur = null;
    var useBtn = h('button', { class: 'btn' }, 'Use this folder');
    var insideBtn = opts.newName ? h('button', { class: 'btn primary' }) : null;
    var newBtn = h('button', { class: 'btn', title: 'A new folder inside the one you are in' }, 'New folder…');
    var picker = folderPicker(function (d) {
      cur = d;
      useBtn.disabled = newBtn.disabled = false;
      if (insideBtn) {
        var taken = d.dirs.some(function (x) { return x.name === opts.newName; });
        insideBtn.textContent = 'Inside it: ' + opts.newName;
        insideBtn.title = (d.path.replace(/\/$/, '') + '/' + opts.newName) + (taken ? ' (exists — it must be empty)' : ' (new)');
      }
    }, opts.start);
    useBtn.disabled = newBtn.disabled = true;
    function done(path) { md.close(); opts.pick(path); }
    useBtn.addEventListener('click', function () { if (cur) done(cur.path); });
    if (insideBtn) insideBtn.addEventListener('click', function () { if (cur) done(cur.path.replace(/\/$/, '') + '/' + opts.newName); });
    newBtn.addEventListener('click', function () {
      if (!cur) return;
      promptDlg('New folder in ' + cur.path, '').then(function (n) {
        if (!n) return;
        if (/[\/]/.test(n) || n === '.' || n === '..') { toast('A folder name only — no /', 'err'); return; }
        done(cur.path.replace(/\/$/, '') + '/' + n);
      });
    });
    var md = openModal({ title: opts.title || 'Folder on the server', body: [
      h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:10px' }, opts.note || 'Click a folder to go into it, .. to go back up.'),
      picker],
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), h('span', { class: 'grow' }), newBtn, useBtn, insideBtn].filter(Boolean) });
    return md;
  }

  // a server folder field: typed, or chosen with Browse…
  function serverFolderField(label, input, opts) {
    var browse = h('button', { class: 'btn', type: 'button', title: 'Browse the folders on the server', style: 'flex:none', onclick: function () {
      var o = typeof opts === 'function' ? opts() : (opts || {});
      var v = input.value.trim();
      serverFolderDialog(Object.assign({ start: v ? v.replace(/\/[^/]*$/, '') || v : '~', pick: function (path) {
        input.value = path;
      } }, o));
    } }, 'Browse…');
    return h('div', { class: 'field' }, h('label', null, label), h('div', { style: 'display:flex;gap:6px' }, input, browse));
  }

  // Add project — all in the window: download from the server, upload from this machine, or server-only
  function addManualProject() {
    var mode = 'download', folders = null, pickedRemote = '', picker = null, pickedInfo = null;
    var name = h('input', { class: 'inp mono', placeholder: 'empty = folder name' });
    var local = h('input', { class: 'inp mono', placeholder: '~/projects/my-project' });
    var remote = h('input', { class: 'inp mono', placeholder: '~/my-project' });
    var git = h('input', { type: 'checkbox', checked: true });
    var errBox = h('div', { class: 'add-err hidden' });
    var list = h('div', { class: 'flist' }, h('div', { class: 'muted', style: 'padding:10px' }, 'Loading folders on the server…'));
    var filter = h('input', { class: 'inp', placeholder: 'Filter folders', autocomplete: 'off' });
    filter.addEventListener('input', renderFolders);

    var pickedNote = h('div', { class: 'muted mono', style: 'font-size:11.5px;margin-top:4px;min-height:15px' });
    function renderFolders() {
      pickedNote.textContent = pickedRemote ? 'Picked: ' + pickedRemote : '';
      if (!folders) return;
      var q = filter.value.trim().toLowerCase();
      var shown = folders.filter(function (f) { return !q || f.name.toLowerCase().indexOf(q) >= 0; });
      if (!shown.length) { list.replaceChildren(h('div', { class: 'muted', style: 'padding:10px' }, 'No folders')); return; }
      list.replaceChildren.apply(list, shown.map(function (f) {
        return h('button', { class: 'frow' + (f.path === pickedRemote ? ' on' : ''), disabled: !!f.project,
          title: f.project ? 'Already synced as ' + f.project : f.path,
          onclick: function () {
            pickedRemote = f.path;
            if (!local.value || local.dataset.auto) { local.value = '~/' + f.name; local.dataset.auto = '1'; if (local._upd) local._upd(); }
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
    name.addEventListener('input', function () { delete name.dataset.auto; });

    var gitRow = function () { return h('label', { class: 'check' }, git, h('span', null, 'Sync the .git folders too', h('small', null, 'So Git works the same on both sides.'))); };
    var forms = {
      download: function () {
        loadFolders();
        var deeper = h('button', { class: 'btn', type: 'button', style: 'flex:none', title: 'Any folder on the server, not only the ones in your home', onclick: function () {
          serverFolderDialog({ title: 'Folder to download', start: pickedRemote || '~', pick: function (path) {
            pickedRemote = path;
            var base = path.replace(/\/$/, '').split('/').pop();
            if (!local.value || local.dataset.auto) { local.value = '~/' + base; local.dataset.auto = '1'; if (local._upd) local._upd(); }
            renderFolders();
          } });
        } }, 'Browse…');
        return [h('div', { class: 'field' }, h('label', null, 'Folder on the server'), h('div', { style: 'display:flex;gap:6px' }, filter, deeper), list, pickedNote),
          h('div', { class: 'row' },
            localFolderField('Put it on this machine at', local),
            h('div', { class: 'field' }, h('label', null, 'Name'), name)), gitRow()];
      },
      upload: function () {
        return [localFolderField('Folder on this machine', local),
          h('div', { class: 'row' },
            serverFolderField('Folder on the server', remote, function () {
              var base = local.value.trim().replace(/[\\/]+$/, '').split(/[\\/]/).pop();
              return { title: 'Where to put it on the server',
                note: 'Go to the folder it should go in. The server folder must be new or empty.',
                newName: base && base !== '~' ? base : '' };
            }),
            h('div', { class: 'field' }, h('label', null, 'Name'), name)),
          h('div', { class: 'muted', style: 'font-size:11.5px;margin:-6px 0 10px' }, 'Empty server folder = ~/<folder name>. It must be new or empty.'), gitRow()];
      },
      list: function () {
        picker = folderPicker(function (d) {
          pickedInfo = d;
          var base = d.path.split('/').filter(Boolean).pop() || '';
          if (!name.value || name.dataset.auto) { name.value = base.replace(/[^A-Za-z0-9._-]/g, '-'); name.dataset.auto = '1'; }
          note.textContent = d.project ? 'Already added as “' + d.project + '”.' : d.git ? 'A Git repository.' : (d.dirs.some(function (x) { return x.git; }) ? 'Has Git repositories inside.' : '');
        });
        var note = h('div', { class: 'muted', style: 'font-size:11.5px;min-height:15px;margin:-4px 0 8px' });
        return [h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:10px' }, 'Pick the project folder on the server: click a folder to open it — the folder you are in is the one added.'),
          h('div', { class: 'field' }, h('label', null, 'Folder on the server'), picker), note,
          h('div', { class: 'row' },
            h('div', { class: 'field' }, h('label', null, 'Name'), name),
            S.web ? null : h('div', { class: 'field' }, h('label', null, 'Folder on this machine (optional)'), local))];
      }
    };
    var area = h('div');
    var go = h('button', { class: 'btn primary', onclick: submit });
    var modes = S.web ? [['list', 'Server folder', 'Work on it on the server'], ['download', 'Sync to my computer', 'installed app only', true], ['upload', 'Upload from my computer', 'installed app only', true]]
      : [['download', 'From the server', 'Download, then keep in sync'], ['upload', 'From this machine', 'Upload, then keep in sync'], ['list', 'Server only', 'No sync, work on the server']];
    var seg = h('div', { class: 'seg' });
    function show(m) {
      mode = m;
      errBox.classList.add('hidden');
      seg.replaceChildren.apply(seg, modes.map(function (x) {
        return h('button', { class: 'segb' + (x[0] === m ? ' on' : ''), disabled: !!x[3], title: x[3] ? APP_ONLY : null, onclick: function () { show(x[0]); } }, h('b', null, x[1]), h('small', null, x[2]));
      }));
      area.replaceChildren.apply(area, forms[m]().filter(Boolean));
      go.textContent = { download: 'Download & sync', upload: 'Upload & sync', list: 'Add' }[m];
    }
    function fail2(e) { go.disabled = false; go.textContent = { download: 'Download & sync', upload: 'Upload & sync', list: 'Add' }[mode]; errBox.textContent = e.message; errBox.classList.remove('hidden'); }
    function submit() {
      errBox.classList.add('hidden');
      if (mode === 'list') {
        var rp = picker ? picker.value() : remote.value.trim();
        if (!rp) { errBox.textContent = 'Pick a folder on the server.'; errBox.classList.remove('hidden'); return; }
        if (pickedInfo && pickedInfo.project && pickedInfo.path === rp) { errBox.textContent = 'This folder is already the project “' + pickedInfo.project + '”.'; errBox.classList.remove('hidden'); return; }
        if (!name.value.trim()) { name.focus(); errBox.textContent = 'Give it a name.'; errBox.classList.remove('hidden'); return; }
        api('POST', 'manual', { name: name.value.trim(), local: S.web ? '' : local.value, remote: rp }).then(function () {
          md.close();
          S.open.add(name.value.trim()); localStorage.setItem('sw-open', JSON.stringify(Array.from(S.open)));
          refreshProjects();
        }).catch(fail2);
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
    show(S.web ? 'list' : 'download');
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

    var webBody = S.web ? [
      h('div', { class: 'sec' }, 'Account'),
      h('div', { class: 'svc' }, h('span', null, 'Signed in as ', h('b', { class: 'mono' }, S.server.user + '@' + S.server.host + (String(S.server.port) !== '22' ? ':' + S.server.port : ''))),
        h('button', { class: 'btn sm', onclick: logout }, 'Sign out')),
      pushRow(),
      accountRows(),
      chk('serverPersist', 'Keep server terminals alive (tmux)', 'Shells survive a dropped connection; reopening attaches to the same one.'),
      h('div', { class: 'sec' }, 'Claude'),
      h('div', { class: 'row' },
        field('Command', inp('claudeCmd', { class: 'inp mono' })),
        field('Default mode', sel('chatMode', [['default', 'Ask before edits'], ['acceptEdits', 'Auto-accept edits'], ['plan', 'Plan'], ['auto', 'Auto']]))),
      h('div', { class: 'sec' }, 'Terminal'),
      h('div', { class: 'row' }, field('Font size', inp('fontSize', { type: 'number', min: 9, max: 32, class: 'inp mono' }))),
      chk('copyOnSelect', 'Copy on select'),
      chk('persianView', 'Show the Persian reading view automatically', 'When Persian text appears in a terminal.'),
      chk('bellNotify', 'Notification when a tab needs you'),
      chk('dockerWatch', 'Watch the projects’ docker containers', 'Tells you when a container on the server crashes, keeps restarting or turns unhealthy.'),
      h('div', { class: 'sec' }, 'In the installed app'),
      h('div', { class: 'app-only' },
        ['Two-way sync of projects with your computer', 'Terminals and run commands on your computer', 'VS Code (local or Remote-SSH)',
          'Port forwarding to localhost', 'Updates, prerequisites, SSH keys'].map(function (t) { return h('div', null, '· ' + t); }),
        h('div', { class: 'muted', style: 'margin-top:6px' }, 'Install it on your computer from github.com/EsmailEmami/simabworkspace.'))
    ] : null;
    if (!S.web) api('GET', 'connection').then(function (c) {
      cUser.value = c.user; cHost.value = c.host; cPort.value = c.port;
      fillKeys(c.keys, c.key);
      autostart.checked = c.autostart;
      daemonInfo.textContent = c.daemon ? 'running' : 'stopped';
      api('GET', 'status?force=1').then(showStatus);
    }).catch(fail);
    var md = openModal({
      title: 'Settings',
      body: webBody || [
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
        chk('dockerWatch', 'Watch the projects’ docker containers', 'Tells you when a container on the server crashes, keeps restarting or turns unhealthy.'),
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

  // ------------------------------------------------------------------ not synced (ignores)
  function ignoresDialog(p) {
    var ta = h('textarea', { class: 'inp mono', rows: 10, spellcheck: 'false', placeholder: 'node_modules\n/dist\n*.log' });
    var errBox = h('div', { class: 'add-err' });
    var defaults = [], current = [], step = 1, found = null, boxes = {};
    function listOf() { return ta.value.split(/[\n,]/).map(function (x) { return x.trim(); }).filter(Boolean); }
    function size(b) { var u = ['B', 'KB', 'MB', 'GB'], i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++; } return (i ? b.toFixed(1) : b) + ' ' + u[i]; }
    function busy(on, label) { save.disabled = on; save.textContent = label; }
    function send(list) {
      var del = {};
      ['server', 'local'].forEach(function (w) { if (boxes[w] && boxes[w].checked) del[w] = found[w].items.map(function (x) { return x.path; }); });
      busy(true, 'Saving…'); errBox.textContent = '';
      api('POST', 'sync/' + encodeURIComponent(p.name) + '/ignores', { ignores: list, delete: del }).then(function (r) {
        md.close();
        var bits = [], errs = [];
        Object.keys(r.deleted || {}).forEach(function (w) {
          var d = r.deleted[w];
          bits.push('deleted ' + d.deleted + (w === 'server' ? ' on the server' : ' on this machine'));
          errs = errs.concat(d.errors || []);
        });
        toast(p.name + ': not synced — ' + (list.join(', ') || 'nothing') + (bits.length ? ' · ' + bits.join(', ') : ''), errs.length ? 'err' : 'ok', { timeout: 8000 });
        if (errs.length) toast('Could not delete: ' + errs.slice(0, 3).join('; '), 'err', { timeout: 10000 });
        refreshProjects();
      }).catch(function (e) { busy(false, step === 2 ? label2() : 'Save'); errBox.textContent = e.message; });
    }
    function label2() { return (boxes.server && boxes.server.checked) || (boxes.local && boxes.local.checked) ? 'Save and delete' : 'Save'; }
    function side(w, title) {
      var f = found[w] || { items: [] };
      if (f.error) return h('div', { class: 'ign-side' }, h('div', { class: 'sec' }, title), h('div', { class: 'add-err' }, f.error));
      if (!f.items.length) return h('div', { class: 'ign-side' }, h('div', { class: 'sec' }, title), h('div', { class: 'muted' }, 'Nothing there'));
      var cb = boxes[w] = h('input', { type: 'checkbox', onchange: function () { save.textContent = label2(); save.className = 'btn primary' + (label2() === 'Save' ? '' : ' danger'); } });
      return h('div', { class: 'ign-side' },
        h('div', { class: 'sec' }, title + ' — ' + f.items.length + (f.truncated ? '+' : '') + ' found · ' + size(f.total)),
        h('div', { class: 'ign-list mono' }, f.items.slice(0, 30).map(function (x) {
          return h('div', null, x.path + (x.dir ? '/' : ''), h('span', { class: 'muted' }, '  ' + size(x.size)));
        }), f.items.length > 30 ? h('div', { class: 'muted' }, '… and ' + (f.items.length - 30) + ' more') : null),
        h('label', { class: 'check', style: 'margin:8px 0 0' }, cb, 'Delete them ' + (w === 'server' ? 'on the server' : 'on this machine')));
    }
    var body = h('div');
    var first = [h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:10px' },
        'Folders and files never copied between this machine and the server. One per line: ',
        h('span', { class: 'mono' }, 'node_modules'), ' anywhere, ', h('span', { class: 'mono' }, '/dist'), ' only at the top, ',
        h('span', { class: 'mono' }, '*.log'), ' by name.'),
      ta,
      h('div', { class: 'muted', style: 'font-size:12px;margin-top:8px' }, 'Saving restarts this project’s sync. Copies made before stay unless you delete them in the next step.')];
    body.replaceChildren.apply(body, first.concat([errBox]));
    var save = h('button', { class: 'btn primary', disabled: true, onclick: function () {
      var list = listOf();
      if (step === 2) return send(list);
      var added = list.filter(function (x) { return current.indexOf(x) < 0; });
      if (!added.length) return send(list);
      busy(true, 'Looking…'); errBox.textContent = '';
      api('POST', 'sync/' + encodeURIComponent(p.name) + '/ignores/preview', { ignores: list }).then(function (r) {
        found = r; boxes = {};
        var any = ['server', 'local'].some(function (w) { return r[w] && (r[w].items.length || r[w].error); });
        if (!any) return send(list);
        step = 2;
        back.classList.remove('hidden'); addDef.classList.add('hidden');
        body.replaceChildren(
          h('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:6px' }, 'Copies of the new entries (' + r.added.join(', ') + ') that are already there:'),
          side('server', 'On the server'), side('local', 'On this machine'),
          h('div', { class: 'muted', style: 'font-size:12px;margin-top:8px' }, 'Deleting is for good. Leave a side unticked if something there still needs them (docker, a build). The sync is changed first, so a deletion is never copied to the other side.'),
          errBox);
        busy(false, 'Save');
      }).catch(function (e) { busy(false, 'Save'); errBox.textContent = e.message; });
    } }, 'Save');
    var addDef = h('button', { class: 'btn', disabled: true, onclick: function () {
      var have = listOf();
      defaults.forEach(function (x) { if (have.indexOf(x) < 0) have.push(x); });
      ta.value = have.join('\n');
    } }, 'Add defaults');
    var back = h('button', { class: 'btn hidden', onclick: function () {
      step = 1; boxes = {}; back.classList.add('hidden'); addDef.classList.remove('hidden');
      save.className = 'btn primary'; busy(false, 'Save'); errBox.textContent = '';
      body.replaceChildren.apply(body, first.concat([errBox]));
    } }, 'Back');
    var md = openModal({ title: 'Not synced — ' + p.name, body: body,
      foot: [addDef, back, h('span', { class: 'grow' }), h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), save] });
    api('GET', 'sync/' + encodeURIComponent(p.name) + '/details').then(function (d) {
      current = d.ignores || [];
      ta.value = current.join('\n');
      defaults = d.defaultIgnores || [];
      save.disabled = false; addDef.disabled = !defaults.length;
    }).catch(function (e) { errBox.textContent = e.message; });
  }

  // ------------------------------------------------------------------ sync details + conflicts
  function syncDetails(p) {
    var body = h('div', null, h('div', { class: 'muted' }, 'Loading…'));
    var syncNowBtn = h('button', { class: 'btn', onclick: function () { syncAction(p, 'flush'); setTimeout(load, 1500); } }, 'Sync now');
    var reconnectBtn = h('button', { class: 'btn hidden', onclick: function () { syncAction(p, 'resume'); setTimeout(load, 2000); } }, 'Reconnect');
    var md = openModal({ title: 'Sync — ' + p.name, size: 'wide', body: body,
      foot: [syncNowBtn, reconnectBtn, h('span', { class: 'grow' }), h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
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
        var isErr = d.state === 'err';
        syncNowBtn.classList.toggle('hidden', isErr);
        reconnectBtn.classList.toggle('hidden', !isErr);
        var rows = [
          h('div', { class: 'kvs' },
            kv('status', d.label + (d.paused ? '' : ' (' + d.status + ')'), d.state),
            kv('this machine', d.local), kv('server', d.server + ':' + d.remote),
            kv('.git folders', d.git ? 'synced' : 'not synced'),
            kv('sync cycles', String(d.cycles)),
            h('div', { class: 'kv' }, h('span', { class: 'k' }, 'not synced'),
              h('span', { class: 'v mono' }, (d.ignores || []).join(', ') || '—', ' ',
                h('a', { href: '#', class: 'ign-edit', onclick: function (e) { e.preventDefault(); md.close(); ignoresDialog(p); } }, 'Edit…'))),
            d.error ? kv('last error', d.error, 'err') : null,
            isErr ? h('div', { class: 'kv' }, h('span', { class: 'k' }), h('span', { class: 'v muted', style: 'font-size:12px' }, 'Click Reconnect to restore the sync after a network outage.')) : null)];
        if (d.progress) rows.push(h('div', { class: 'sbar', style: 'margin:10px 0' }, h('i', { style: 'width:' + d.progress.percent + '%' }),
          h('span', null, d.progress.percent + '% · ' + d.progress.files + '/' + d.progress.totalFiles + ' items')));
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

  // ------------------------------------------------------------------ docker on the server
  function ago(iso) {
    var t = Date.parse(iso || '');
    if (!t || t < 86400000) return '';
    var s = Math.max(0, (Date.now() - t) / 1000);
    return s < 60 ? Math.round(s) + 's' : s < 3600 ? Math.round(s / 60) + 'm' : s < 86400 ? Math.round(s / 3600) + 'h' : Math.round(s / 86400) + 'd';
  }
  var CLEAN_EXIT = [0, 137, 143];
  function containerState(c) {
    var restarts = c.restarts ? ' · ' + plural(c.restarts, 'restart') : '';
    if (c.state === 'running') {
      if (c.health === 'unhealthy') return ['err', 'unhealthy'];
      if (c.health === 'starting') return ['busy', 'starting'];
      return ['ok', 'up ' + ago(c.startedAt) + restarts];
    }
    if (c.state === 'restarting') return ['err', 'restarting' + restarts];
    if (c.state === 'paused') return ['warn', 'paused'];
    if (c.state === 'exited' || c.state === 'dead') {
      var crashed = CLEAN_EXIT.indexOf(c.exitCode) < 0;
      var when = ago(c.finishedAt);
      return [crashed ? 'err' : '', (crashed ? 'crashed · exit ' + c.exitCode : 'stopped') + (when ? ' · ' + when + ' ago' : '')];
    }
    return ['', c.state || 'unknown'];
  }

  function dockerTab(p, c, action) { openSession({ kind: 'docker', project: p.name, container: c.name, action: action }); }

  function dockerDialog(p) {
    var body = h('div', { class: 'dk' }, h('div', { class: 'muted' }, 'Reading the containers on the server…'));
    var timer = null, busy = false;
    var md = openModal({
      title: 'Docker — ' + p.name, size: 'wide', body: body,
      foot: [h('span', { class: 'muted left', style: 'font-size:12px' }, 'Containers started from this project’s folder on the server'),
        h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')],
      onclose: function () { clearInterval(timer); }
    });
    function act(c, action, btn) {
      btn.disabled = true;
      busy = true;
      api('POST', 'docker/' + encodeURIComponent(p.name), { container: c.name, action: action }).then(function (d) {
        busy = false;
        toast((c.service || c.name) + ': ' + { start: 'started', stop: 'stopped', restart: 'restarted' }[action], 'ok');
        render(d);
        refreshDocker();
      }).catch(function (e) { busy = false; btn.disabled = false; fail(e); });
    }
    function portChip(c, pt) {
      var t = S.tunnels.find(function (x) { return x.project === p.name && x.remotePort === pt.host && /^(127\.0\.0\.1|localhost)$/.test(x.remoteHost); });
      var label = ':' + pt.host + (pt.host !== pt.port ? '→' + pt.port : '');
      if (t) {
        return h('button', { class: 'pchip on ' + t.state, title: 'Forwarded: localhost:' + t.localPort + ' on this machine' + (t.state === 'up' ? '' : ' (' + t.state + ')') + '\nclick for options',
          onclick: function (e) { tunnelMenu(t, e.currentTarget); } }, label, h('span', { class: 'pto' }, '⇄ ' + t.localPort));
      }
      if (S.web) return h('span', { class: 'pchip', title: 'Port ' + pt.host + ' on the server (' + pt.proto + ')' }, label);
      return h('button', { class: 'pchip', title: 'Port ' + pt.host + ' on the server (' + pt.proto + ')\nclick: reach it from this machine (localhost:' + pt.host + ')',
        onclick: function () { addTunnel(p, { name: c.service || c.name, remoteHost: '127.0.0.1', remotePort: pt.host, localPort: pt.host, on: true, http: false }); setTimeout(load, 300); } }, label);
    }
    function row(c) {
      var st = containerState(c);
      var running = c.state === 'running' || c.state === 'restarting';
      var b = function (label, title, fn, cls) {
        var btn = h('button', { class: 'btn sm' + (cls ? ' ' + cls : ''), title: title, onclick: function () { fn(btn); } }, label);
        return btn;
      };
      return h('div', { class: 'dkrow' },
        h('i', { class: 'dot ' + st[0] }),
        h('div', { class: 'dkname' }, h('b', null, c.service || c.name), h('span', { class: 'muted mono', title: c.name }, c.image)),
        h('span', { class: 'dkst ' + st[0] }, st[1]),
        h('span', { class: 'dkports' }, c.ports.filter(function (pt) { return pt.proto === 'tcp'; }).map(function (pt) { return portChip(c, pt); })),
        h('span', { class: 'dkacts' },
          b('Logs', 'Follow the logs in a tab', function () { dockerTab(p, c, 'logs'); }),
          running ? b('Shell', 'A shell inside the container, in a tab', function () { dockerTab(p, c, 'shell'); }) : null,
          b(running ? 'Restart' : 'Start', running ? 'Restart the container' : 'Start the container', function (btn) { act(c, running ? 'restart' : 'start', btn); }),
          running ? b('Stop', 'Stop the container', function (btn) { act(c, 'stop', btn); }) : null,
          c.compose && c.service ? b('Rebuild', 'docker compose up -d --build ' + c.service + ' — in a tab', function () { dockerTab(p, c, 'rebuild'); md.close(); }) : null));
    }
    function render(d) {
      var rows = [];
      if (d.error) rows.push(h('div', { class: 'add-err' }, d.error));
      var groups = {};
      d.containers.forEach(function (c) { (groups[c.compose || '—'] = groups[c.compose || '—'] || []).push(c); });
      Object.keys(groups).sort().forEach(function (g) {
        var list = groups[g].sort(function (a, b) { return (a.service || a.name).localeCompare(b.service || b.name); });
        var up = list.filter(function (c) { return c.state === 'running'; }).length;
        rows.push(h('div', { class: 'sec' }, (g === '—' ? 'other containers' : g) + ' · ' + up + '/' + list.length + ' running'));
        list.forEach(function (c) { rows.push(row(c)); });
      });
      if (!d.containers.length && !d.error) rows.push(h('div', { class: 'muted' }, 'No containers from this project on the server. Start them with docker compose (a ▶ run command on the server is handy), and they show up here.'));
      body.replaceChildren.apply(body, rows);
    }
    function load() {
      if (busy) return;
      api('GET', 'docker/' + encodeURIComponent(p.name)).then(render).catch(function (e) {
        if (!body.querySelector('.dkrow')) body.replaceChildren(h('div', { class: 'add-err' }, e.message));
      });
    }
    load();
    timer = setInterval(function () { if (!document.hidden) load(); }, 6000);
  }

  function dockerLine(p) {
    var d = S.docker[p.name];
    if (!d || !d.total) return null;
    var cls = d.bad ? 'err' : d.running < d.total ? 'warn' : 'ok';
    return h('div', { class: 'cmd dkline', title: 'Docker on the server: ' + d.running + ' of ' + d.total + ' running' + (d.bad ? ', ' + d.bad + ' with problems' : '') + '\nclick for logs, restart, ports',
      onclick: function () { dockerDialog(p); } },
      h('span', { class: 'ic' }, '▣'), h('span', { class: 'nm' }, 'docker'),
      h('span', { class: 'gb dk ' + cls }, d.running + '/' + d.total));
  }

  function refreshDocker() {
    return api('GET', 'docker-summary?since=' + (S.dockerSeq || 0)).then(function (d) {
      if (S.dockerSeq !== undefined) {
        d.events.forEach(function (e) {
          var p = project(e.project);
          toast(e.project + ' · ' + (e.service || e.name) + ': ' + e.why, 'err', { timeout: 20000, onclick: p ? function () { dockerDialog(p); } : null });
          notify('Container ' + e.why, e.project + ' · ' + (e.service || e.name));
        });
      }
      S.dockerSeq = d.seq;
      var key = JSON.stringify(d.summary);
      S.docker = d.summary;
      if (key !== S.dockerKey) { S.dockerKey = key; renderProjects(); }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ port forwarding (ssh tunnels)
  function tunnelUrl(t) { return 'http://localhost:' + t.localPort + '/'; }
  function tunnelTitle(t) {
    var s = 'localhost:' + t.localPort + ' on this machine → ' + t.remoteHost + ':' + t.remotePort + ' on the server';
    if (t.state === 'up') s += '\nconnected';
    else if (t.state === 'connecting') s += '\nconnecting…';
    else if (t.state === 'error') s += '\n' + t.error;
    else s += '\noff — click to connect';
    return s;
  }
  function projectTunnels(name) { return S.tunnels.filter(function (t) { return t.project === name; }); }
  function plainTunnel(t) { return { id: t.id, name: t.name, localPort: t.localPort, remoteHost: t.remoteHost, remotePort: t.remotePort, on: t.on, http: t.http }; }

  function saveTunnels(name, list) {
    return api('POST', 'tunnels/' + encodeURIComponent(name), { tunnels: list }).then(function (d) {
      S.tunnels = d.tunnels;
      renderProjects();
      setTimeout(refreshTunnels, 3000);
      return d.tunnels;
    });
  }

  function addTunnel(p, spec) {
    if (spec.localPort < 1024) spec.localPort += 10000;     // low ports need admin rights on this machine
    var used = S.tunnels.map(function (t) { return t.localPort; });
    while (used.indexOf(spec.localPort) >= 0 && spec.localPort < 65535) spec.localPort++;
    var list = projectTunnels(p.name).map(plainTunnel).concat([spec]);
    return saveTunnels(p.name, list).then(function () {
      toast('localhost:' + spec.localPort + ' → server port ' + spec.remotePort, 'ok');
    }).catch(fail);
  }

  function toggleTunnel(t, on) {
    api('POST', 'tunnels/' + encodeURIComponent(t.project) + '/' + encodeURIComponent(t.id), { on: on === undefined ? !t.on : on }).then(function (d) {
      S.tunnels = d.tunnels;
      renderProjects();
      setTimeout(refreshTunnels, 3000);
    }).catch(fail);
  }

  function copyText(text) {
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast('Copied ' + text, 'ok'); }, function () {});
  }

  function tunnelMenu(t, anchor) {
    var p = project(t.project);
    menuAt(anchor, [
      { header: t.name + ' · localhost:' + t.localPort },
      t.on ? { label: 'Disconnect', action: function () { toggleTunnel(t, false); } } : { label: 'Connect', action: function () { toggleTunnel(t, true); } },
      { label: 'Open in the browser', action: function () { window.open(tunnelUrl(t), '_blank'); } },
      { label: 'Copy localhost:' + t.localPort, action: function () { copyText('localhost:' + t.localPort); } },
      p ? { label: 'Edit…', action: function () { tunnelsDialog(p); } } : null
    ]);
  }

  function tunnelLine(t) {
    var p = project(t.project);
    var dot = t.state === 'up' ? 'ok' : t.state === 'connecting' ? 'busy' : t.state === 'error' ? 'err' : '';
    var more = h('span', { class: 'tag', title: 'Options' }, '⋯');
    var el = h('div', { class: 'cmd tline' + (t.on ? ' running' : ''), title: tunnelTitle(t) + '\nclick: ' + (t.on ? 'disconnect' : 'connect') + ' · right-click: options',
      onclick: function (e) { if (e.target === more) { tunnelMenu(t, more); return; } toggleTunnel(t); },
      oncontextmenu: function (e) { e.preventDefault(); tunnelMenu(t, el); } },
      h('i', { class: 'dot ' + dot }), h('span', { class: 'nm' }, t.name),
      t.http && t.state === 'up' ? h('a', { class: 'tag tlink', href: tunnelUrl(t), target: '_blank', title: 'Open ' + tunnelUrl(t), onclick: function (e) { e.stopPropagation(); } }, ':' + t.localPort + ' ↗')
        : h('span', { class: 'tag' }, ':' + t.localPort), more);
    return p ? el : null;
  }

  function tunnelsDialog(p, pre) {
    var list = h('div');
    function row(t) {
      var name = h('input', { class: 'inp', value: t.name || '', placeholder: 'Name, e.g. postgres' });
      var lp = h('input', { class: 'inp mono', value: t.localPort ? String(t.localPort) : '', placeholder: 'same (≥1024)', inputmode: 'numeric' });
      var host = h('input', { class: 'inp mono', value: t.remoteHost || '127.0.0.1', placeholder: '127.0.0.1' });
      var rp = h('input', { class: 'inp mono', value: t.remotePort ? String(t.remotePort) : '', placeholder: '5432', inputmode: 'numeric' });
      var http = h('input', { type: 'checkbox' });
      http.checked = !!t.http;
      var st = t.state ? h('span', { class: 'dkst ' + (t.state === 'up' ? 'ok' : t.state === 'error' ? 'err' : ''), title: t.error || '' }, t.state === 'error' ? 'error' : t.state) : null;
      var box = h('div', { class: 'cmd-edit' },
        h('div', { class: 'top' }, name, st, h('button', { class: 'icon-btn', title: 'Remove', onclick: function () { box.remove(); } }, '✕')),
        h('div', { class: 'row trow' },
          h('label', null, h('span', null, 'this machine'), h('span', { class: 'mono muted' }, 'localhost:'), lp),
          h('span', { class: 'muted' }, '→'),
          h('label', null, h('span', null, 'on the server'), host, h('span', { class: 'mono muted' }, ':'), rp)),
        h('label', { class: 'check', style: 'margin-top:6px' }, http, ' a web page — show an ↗ link to open it'),
        t.error ? h('div', { class: 'add-err', style: 'margin-top:6px' }, t.error) : null);
      box._get = function () {
        return { id: t.id, name: name.value, localPort: lp.value.trim() || '', remoteHost: host.value.trim(), remotePort: rp.value.trim(),
          on: t.id ? !!t.on : true, http: http.checked };
      };
      return box;
    }
    var mine = projectTunnels(p.name);
    mine.forEach(function (t) { list.append(row(t)); });
    if (pre && !mine.some(function (t) { return String(t.remotePort) === String(pre.remotePort); })) list.append(row(pre));
    // what can be forwarded: the ports the project's containers publish on the server
    var avail = h('div', { class: 'tavail' }, h('span', { class: 'muted' }, 'Looking for open ports on the server…'));
    function rowsNow() { return Array.prototype.map.call(list.children, function (b) { return b._get(); }); }
    function renderAvail(cs) {
      var items = [];
      cs.forEach(function (c) {
        c.ports.filter(function (pt) { return pt.proto === 'tcp'; }).forEach(function (pt) {
          items.push({ name: c.service || c.name, port: pt.host, inner: pt.port, up: c.state === 'running', image: c.image });
        });
      });
      if (!items.length) { avail.replaceChildren(h('span', { class: 'muted' }, 'No published container ports found — add one by hand below.')); return; }
      avail.replaceChildren.apply(avail, items.map(function (it) {
        var done = rowsNow().some(function (t) { return String(t.remotePort) === String(it.port) && /^(127\.0\.0\.1|localhost)$/.test(t.remoteHost); });
        var b = h('button', { class: 'pchip' + (done ? ' on' : ''), disabled: done, title: done ? 'Already in the list' : it.name + ' (' + it.image + ')' + (it.up ? '' : ' — not running') + '\nclick to add',
          onclick: function () {
            list.append(row({ name: it.name, remoteHost: '127.0.0.1', remotePort: it.port, localPort: it.port < 1024 ? it.port + 10000 : it.port, http: false }));
            b.classList.add('on'); b.disabled = true;
          } }, h('b', null, it.name), ' :' + it.port + (it.inner !== it.port ? '→' + it.inner : ''), done ? ' ✓' : ' +');
        return b;
      }));
    }
    if (hasRemote(p)) api('GET', 'docker/' + encodeURIComponent(p.name)).then(function (d) { renderAvail(d.containers || []); })
      .catch(function (e) { avail.replaceChildren(h('span', { class: 'muted' }, 'Could not read the server’s ports: ' + e.message)); });
    else avail.replaceChildren(h('span', { class: 'muted' }, 'This project has no server folder.'));
    if (!mine.length) list.append(row({}));
    var md = openModal({
      title: 'Port forwarding — ' + p.name,
      body: [h('div', { class: 'muted', style: 'margin-bottom:10px;font-size:12px' },
        'Reach a port of the server from this machine, through SSH: a database for DBeaver, an API for Postman, a page in the browser. ',
        'Nothing is opened to the internet. A tunnel that is on reconnects by itself while the app runs.'),
        h('div', { class: 'sec' }, 'Open on the server — click to add'), avail,
        h('div', { class: 'sec' }, 'Forwarded'), list],
      foot: [h('button', { class: 'btn left', onclick: function () { list.append(row({})); } }, '+ Add'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'),
        h('button', { class: 'btn primary', onclick: function () {
          var items = Array.prototype.map.call(list.children, function (b) { return b._get(); }).filter(function (t) { return String(t.remotePort).trim(); });
          saveTunnels(p.name, items).then(function () { md.close(); }).catch(fail);
        } }, 'Save')]
    });
  }

  function refreshTunnels() {
    return api('GET', 'tunnels').then(function (d) {
      var key = JSON.stringify(d.tunnels.map(function (t) { return [t.project, t.id, t.name, t.state, t.on, t.localPort, t.error, t.http]; }));
      S.tunnels = d.tunnels;
      if (key !== S.tunnelsKey) { S.tunnelsKey = key; renderProjects(); }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ Claude usage (limits, cost)
  var USAGE_NAMES = { five_hour: '5-hour limit', seven_day: 'weekly limit', seven_day_overage_included: 'weekly limit (model)' };
  function pct(w) { return Math.round((w.used || 0) * 100); }
  function resetIn(ts) {
    var s = ts - Date.now() / 1000;
    if (!ts || s <= 0) return '';
    var m = Math.max(1, Math.round(s / 60)), d = Math.floor(m / 1440), hh = Math.floor((m % 1440) / 60), mm = m % 60;
    return d ? d + 'd ' + hh + 'h' : hh ? hh + 'h ' + mm + 'm' : mm + 'm';
  }
  function money(v) { return '$' + (v >= 100 ? v.toFixed(0) : v.toFixed(2)); }
  function liveWindows(u) {
    var w = (u && u.limits && u.limits.windows) || {}, now = Date.now() / 1000, out = {};
    Object.keys(w).forEach(function (k) { if (!w[k].resetsAt || w[k].resetsAt > now) out[k] = w[k]; });
    return out;
  }
  function meter(label, w) {
    var p = pct(w);
    return h('span', { class: 'um' + (p >= 90 ? ' err' : p >= 70 ? ' warn' : '') },
      h('span', { class: 'ul' }, label), h('span', { class: 'ub' }, h('i', { style: 'width:' + Math.min(100, p) + '%' })), h('span', { class: 'up' }, p + '%'));
  }

  function renderUsage() {
    var el = $('#usage');
    if (!el) return;
    var u = S.usage, w = liveWindows(u);
    if (!w.five_hour && !w.seven_day && !(u && u.today)) { el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    var tip = [];
    if (w.five_hour) tip.push('5-hour limit: ' + pct(w.five_hour) + '% used' + (resetIn(w.five_hour.resetsAt) ? ', resets in ' + resetIn(w.five_hour.resetsAt) : ''));
    if (w.seven_day) tip.push('weekly limit: ' + pct(w.seven_day) + '% used' + (resetIn(w.seven_day.resetsAt) ? ', resets in ' + resetIn(w.seven_day.resetsAt) : ''));
    if (u.today) tip.push('today ≈ ' + money(u.today) + ' (API price)');
    el.title = tip.join('\n') + '\nclick for details';
    el.replaceChildren.apply(el, [w.five_hour ? meter('5h', w.five_hour) : null, w.seven_day ? meter('7d', w.seven_day) : null,
      !w.five_hour && !w.seven_day ? h('span', { class: 'um' }, h('span', { class: 'ul' }, 'Claude today'), h('span', { class: 'up' }, money(u.today))) : null].filter(Boolean));
    // warn once per window when it gets close
    var f = w.five_hour;
    if (f) {
      var level = pct(f) >= 95 ? 95 : pct(f) >= 80 ? 80 : 0;
      var k = f.resetsAt + ':' + level;
      if (level && S.usageWarned !== k && S.usageWarned !== f.resetsAt + ':95') {
        S.usageWarned = k;
        toast('Claude: ' + pct(f) + '% of the 5-hour limit used' + (resetIn(f.resetsAt) ? ' — resets in ' + resetIn(f.resetsAt) : ''), 'warn', { timeout: 12000, onclick: usageDialog });
      }
    }
  }

  // ------------------------------------------------------------------ server load (CPU, memory, disk)
  function gb(b) { return b >= 1e12 ? (b / 1e12).toFixed(1) + ' TB' : b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB'; }
  function statMeter(label, p, title) {
    p = Math.max(0, Math.min(100, Math.round(p)));
    return h('span', { class: 'um' + (p >= 90 ? ' err' : p >= 75 ? ' warn' : ''), title: title },
      h('span', { class: 'ul' }, label), h('span', { class: 'ub' }, h('i', { style: 'width:' + p + '%' })), h('span', { class: 'up' }, p + '%'));
  }
  function renderStats() {
    var el = $('#server-stats'), d = S.stats;
    if (!el) return;
    if (!d || d.error || !d.memTotal) { el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    var mem = 100 * d.memUsed / d.memTotal, disk = d.diskTotal ? 100 * d.diskUsed / d.diskTotal : 0;
    el.title = 'Server now — CPU ' + d.cpu + '% of ' + d.cores + ' cores (load ' + d.load.join(' ') + ')\n' +
      'Memory ' + gb(d.memUsed) + ' of ' + gb(d.memTotal) + '\nDisk ' + gb(d.diskUsed) + ' of ' + gb(d.diskTotal) + ' (' + gb(d.diskFree) + ' free)\nclick for details';
    el.replaceChildren(statMeter('cpu', d.cpu), statMeter('ram', mem), statMeter('disk', disk));
  }
  function refreshStats() {
    if (!serverReady() || document.hidden) return Promise.resolve();
    return api('GET', 'server-stats').then(function (d) { S.stats = d; renderStats(); }).catch(function () {});
  }
  function statsDialog() {
    var body = h('div', null, h('div', { class: 'muted' }, 'Reading the server…'));
    var timer = null;
    function procTable(rows, by) {
      return h('table', { class: 'ptable' },
        h('tr', null, h('th', null, 'process'), h('th', null, 'user'), h('th', { class: 'n' }, 'cpu*'), h('th', { class: 'n' }, 'memory')),
        rows.map(function (r) {
          return h('tr', null, h('td', { class: 'mono' }, r.name), h('td', { class: 'muted' }, r.user),
            h('td', { class: 'n' + (by === 'cpu' ? ' b' : '') }, r.cpu.toFixed(1) + '%'), h('td', { class: 'n' + (by === 'mem' ? ' b' : '') }, gb(r.rss)));
        }));
    }
    function big(label, p, text) {
      p = Math.max(0, Math.min(100, Math.round(p)));
      return h('div', { class: 'ubig' + (p >= 90 ? ' err' : p >= 75 ? ' warn' : '') },
        h('div', { class: 'uhead' }, h('b', null, label), h('span', { class: 'muted' }, text)),
        h('div', { class: 'ub' }, h('i', { style: 'width:' + p + '%' })));
    }
    function load() {
      api('GET', 'server-stats?top=1').then(function (d) {
        if (d.error) { body.replaceChildren(h('div', { class: 'muted' }, d.error)); return; }
        S.stats = d; renderStats();
        body.replaceChildren(
          big('CPU', d.cpu, d.cpu + '% of ' + d.cores + ' cores · load ' + d.load.join(' ')),
          big('Memory', 100 * d.memUsed / d.memTotal, gb(d.memUsed) + ' of ' + gb(d.memTotal) + (d.swapUsed > 1e8 ? ' · swap ' + gb(d.swapUsed) : '')),
          big('Disk', d.diskTotal ? 100 * d.diskUsed / d.diskTotal : 0, gb(d.diskUsed) + ' of ' + gb(d.diskTotal) + ' · ' + gb(d.diskFree) + ' free · ' + d.diskPath),
          h('div', { class: 'sec' }, 'Most CPU'), procTable(d.topCpu || [], 'cpu'),
          h('div', { class: 'sec' }, 'Most memory'), procTable(d.topMem || [], 'mem'),
          h('div', { class: 'muted', style: 'font-size:11px;margin-top:6px' }, '* cpu of a process: its average since it started (100% = one core).'));
      }).catch(function (e) { body.replaceChildren(h('div', { class: 'muted' }, e.message)); });
    }
    openModal({ title: 'Server — ' + (S.server && S.server.host || ''), body: body, onclose: function () { clearInterval(timer); } });
    load();
    timer = setInterval(load, 4000);
  }

  function refreshUsage() {
    return api('GET', 'usage').then(function (u) { S.usage = u; renderUsage(); }).catch(function () {});
  }

  function usageDialog() {
    var u = S.usage || {}, w = liveWindows(u), rows = [];
    rows.push(h('div', { class: 'sec' }, 'Claude subscription limits'));
    var keys = Object.keys(w);
    if (!keys.length) rows.push(h('div', { class: 'muted' }, 'Not known yet — shown after Claude answers in a chat here.'));
    keys.forEach(function (k) {
      var x = w[k], p = pct(x);
      rows.push(h('div', { class: 'ubig' + (p >= 90 ? ' err' : p >= 70 ? ' warn' : '') },
        h('div', { class: 'uhead' }, h('b', null, USAGE_NAMES[k] || k), h('span', { class: 'muted' }, p + '% used' + (resetIn(x.resetsAt) ? ' · resets in ' + resetIn(x.resetsAt) : ''))),
        h('div', { class: 'ub' }, h('i', { style: 'width:' + Math.min(100, p) + '%' }))));
    });
    if (u.limits && u.limits.status === 'rejected') rows.push(h('div', { class: 'add-err' }, 'The limit is reached — Claude answers again when it resets.'));
    if (u.limits && u.limits.updated) rows.push(h('div', { class: 'muted', style: 'font-size:11.5px;margin-top:6px' }, 'As of Claude’s last answer, ' + (ago(new Date(u.limits.updated * 1000).toISOString()) || '0s') + ' ago.'));
    rows.push(h('div', { class: 'sec' }, 'Cost at API prices · today ' + money(u.today || 0) + ' · last 7 days ' + money(u.week || 0)));
    rows.push(h('div', { class: 'muted', style: 'font-size:11.5px;margin-bottom:8px' }, 'What the same use would cost on the Claude API. On a Claude subscription you do not pay it — it shows where the limit goes.'));
    (u.days || []).slice(0, 14).forEach(function (d) {
      var ps = Object.keys(d.projects).sort(function (a, b) { return d.projects[b] - d.projects[a]; });
      rows.push(h('div', { class: 'kv' }, h('span', { class: 'k mono' }, d.day), h('span', { class: 'v' }, h('b', null, money(d.total)), '  ',
        h('span', { class: 'muted' }, ps.map(function (n) { return n + ' ' + money(d.projects[n]); }).join(' · ')))));
    });
    if (!(u.days || []).length) rows.push(h('div', { class: 'muted' }, 'Nothing yet.'));
    var md = openModal({ title: 'Claude usage', body: rows, foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
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
        items.push({ label: n + ': Claude sessions — running and past', grp: n, action: function () { claudeSessionsDialog(p); } });
        items.push({ label: n + ': starter message for new Claude chats', grp: n, action: function () { starterDialog(p); } });
        items.push({ label: n + ': Claude, past chats…', grp: n, action: function () { pickClaudeSession(n, function (sid, title) { openSession({ kind: 'chat', project: n, resume: sid, title: 'Claude · ' + title }); }); } });
        items.push({ label: n + ': server terminal', grp: n, action: function () { openSession({ kind: 'shell', where: 'server', project: n }); } });
        items.push({ label: n + ': VS Code on server', grp: n, action: function () { openEditor(p, 'server'); } });
        items.push({ label: n + ': docker containers — logs, restart, ports', grp: n, action: function () { dockerDialog(p); } });
        items.push({ label: n + ': port forwarding', grp: n, action: function () { tunnelsDialog(p); } });
      }
      projectTunnels(n).forEach(function (t) {
        items.push({ id: n + ': tunnel ' + t.name, label: n + ': ' + (t.on ? 'disconnect ' : 'connect ') + t.name + ' (localhost:' + t.localPort + ')', grp: n, action: function () { toggleTunnel(t); } });
      });
      if (p.localExists) {
        items.push({ label: n + ': local terminal', grp: n, action: function () { openSession({ kind: 'shell', where: 'local', project: n }); } });
        items.push({ label: n + ': VS Code, local folder', grp: n, action: function () { openEditor(p, 'local'); } });
      }
      p.commands.forEach(function (c) {
        var running = !!runningCmd(n, c.id);
        items.push({ id: n + ': command ' + c.id, label: n + ': ' + (running ? 'stop ' : 'run ') + c.name, grp: n, action: function () { runCommand(p, c); } });
      });
      if (hasRemote(p) || p.localExists) items.push({ label: n + ': files', grp: n, action: function () { openFiles(p); } });
      if (hasRemote(p) || p.localExists) items.push({ label: n + ': changes — review, commit', grp: n, action: function () { openSession({ kind: 'changes', project: n }); } });
      items.push({ label: n + ': git — changes, commit, pull, push', grp: n, action: function () { gitDialog(p); } });
      var gs = S.git[n];
      if (gs && gs.repos === 1) items.push({ id: n + ': switch branch', label: n + ': switch branch' + (gs.branch ? ' (now ' + gs.branch + ')' : '') + '…', grp: n, action: function () { branchPicker(p, gs.repo, gs.where, function () {}); } });
      items.push({ label: n + ': edit run commands', grp: n, action: function () { editCommands(p); } });
    });
    items.push({ label: 'Go to file…', grp: 'app', action: function () { setTimeout(function () { quickOpen(); }, 0); } });
    items.push({ label: 'Claude (no project, home folder)', grp: 'app', action: function () { openSession({ kind: 'chat' }); } });
    items.push({ label: 'Claude sessions — all projects', grp: 'app', action: function () { claudeSessionsDialog(null); } });
    items.push({ label: 'Server terminal (home)', grp: 'app', action: function () { openSession({ kind: 'shell', where: 'server' }); } });
    items.push({ label: 'Local terminal (home)', grp: 'app', action: function () { openSession({ kind: 'shell', where: 'local' }); } });
    items.push({ label: 'Grid: all tabs side by side', grp: 'layout', action: gridAll });
    if (!compact()) {
      items.push({ label: 'Split right', grp: 'layout', action: function () { splitGroup(L.focused, 'right'); } });
      items.push({ label: 'Split down', grp: 'layout', action: function () { splitGroup(L.focused, 'bottom'); } });
    }
    items.push({ label: 'Single section', grp: 'layout', action: mergeAll });
    items.push({ label: 'Toggle sidebar', grp: 'layout', action: toggleWorkMode });
    items.push({ label: 'Add project…', grp: 'app', action: addManualProject });
    items.push({ label: 'History — what you did', grp: 'app', action: function () { historyDialog(); } });
    items.push({ label: 'Claude usage — limits and cost', grp: 'app', action: usageDialog });
    items.push({ label: 'Settings', grp: 'app', action: settingsDialog });
    if (S.web && !standalone()) items.push({ label: 'Install as an app', grp: 'app', action: installApp });
    if (S.web) items.push({ label: 'Sign out', grp: 'app', action: logout });
    else items.push({ label: 'Quit (stop every terminal)', grp: 'app', action: quitApp });
    if (S.web) items = items.filter(function (it) { return !/local terminal|VS Code|port forwarding|\(localhost:/.test(it.label); });
    return items;
  }

  // Ctrl+P: go to a file of the project you are in (opens it in its Files tab)
  var fileLists = {};
  function projectFiles(name) {
    var c = fileLists[name];
    if (c && Date.now() - c.at < 30000) return Promise.resolve(c.files);
    return api('GET', 'files/' + encodeURIComponent(name)).then(function (d) {
      var files = (d.files || []).filter(function (f) { return !/\/$/.test(f); });
      fileLists[name] = { at: Date.now(), files: files };
      return files;
    });
  }
  function currentProject() {
    var n = S.active && S.active.info && S.active.info.spec.project;
    if (n && project(n)) return project(n);
    return S.projects.length === 1 ? S.projects[0] : null;
  }
  function fileScore(path, words) {
    var low = path.toLowerCase(), base = low.slice(low.lastIndexOf('/') + 1), score = 0;
    for (var i = 0; i < words.length; i++) {
      var w = words[i];
      if (base.indexOf(w) === 0) score += 3;
      else if (base.indexOf(w) >= 0) score += 2;
      else if (low.indexOf(w) >= 0) score += 1;
      else return -1;
    }
    return score * 1000 - path.length;
  }
  function quickOpen(p) {
    p = p || currentProject();
    if (!p) {                                         // which project? pick one first
      menuAt($('#btn-palette') || document.body, S.projects.map(function (x) { return { label: x.title || x.name, action: function () { quickOpen(x); } }; }));
      return;
    }
    if (modalStack.length) return;
    var inp = h('input', { placeholder: 'Go to a file of ' + (p.title || p.name) + '…', autofocus: true, dir: 'auto', spellcheck: 'false' });
    var res = h('div', { class: 'results' }, h('div', { class: 'muted', style: 'padding:10px' }, 'Loading the file list…'));
    var files = null, shown = [], sel = 0;
    var overlay = h('div', { class: 'overlay' }, h('div', { class: 'modal palette' }, inp, res));
    function render() {
      if (!files) return;
      var words = inp.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      var hits = words.length ? files.map(function (f) { return { f: f, s: fileScore(f, words) }; }).filter(function (x) { return x.s >= 0; })
        .sort(function (a, b) { return b.s - a.s; }).slice(0, 60).map(function (x) { return x.f; }) : files.slice(0, 60);
      shown = hits;
      sel = Math.min(sel, Math.max(0, shown.length - 1));
      res.replaceChildren.apply(res, shown.map(function (f, i) {
        var cut = f.lastIndexOf('/');
        return h('div', { class: 'res' + (i === sel ? ' sel' : ''), onmousemove: function () { if (sel !== i) { sel = i; mark(); } }, onclick: function () { run(i); } },
          h('span', { class: 'mono' }, f.slice(cut + 1)), h('span', { class: 'grp mono' }, cut > 0 ? f.slice(0, cut) : ''));
      }));
      if (!shown.length) res.append(h('div', { class: 'muted', style: 'padding:10px' }, 'No such file'));
    }
    function mark() {
      Array.prototype.forEach.call(res.children, function (c, i) { c.classList.toggle('sel', i === sel); });
      if (res.children[sel]) res.children[sel].scrollIntoView({ block: 'nearest' });
    }
    function close() { var i = modalStack.indexOf(rec); if (i >= 0) modalStack.splice(i, 1); overlay.remove(); }
    function run(i) {
      var f = shown[i];
      close();
      if (!f) { if (S.active) S.active.focus(); return; }
      openSession({ kind: 'files', project: p.name }).then(function (t) { if (t && t.reveal) t.reveal(f); if (t && t.openFile) t.openFile(f); });
    }
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
    projectFiles(p.name).then(function (f) { files = f; render(); })
      .catch(function (e) { res.replaceChildren(h('div', { class: 'add-err', style: 'margin:10px' }, e.message)); });
    setTimeout(function () { inp.focus(); }, 20);
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
      var hits = all.filter(function (it) {
        var hay = (it.label + ' ' + it.grp).toLowerCase();
        return q.every(function (w) { return hay.indexOf(w) >= 0; });
      });
      // starred first (in their usual order), then the rest
      var top = starSort('cmd', hits.filter(function (it) { return it.grp !== 'tab' && isStarred(starKey('cmd', it)); }));
      shown = top.concat(hits.filter(function (it) { return top.indexOf(it) < 0; })).slice(0, 80);
      sel = Math.min(sel, Math.max(0, shown.length - 1));
      var drag = top.length > 1;
      res.replaceChildren.apply(res, shown.map(function (it, i) {
        var onTop = i < top.length;
        var row = h('div', { class: 'res' + (i === sel ? ' sel' : '') + (onTop ? ' starred' : '') + (i === top.length - 1 && i < shown.length - 1 ? ' last-star' : ''),
          'data-star': onTop ? starKey('cmd', it) : null,
          onmousemove: function () { if (sel !== i && !document.body.classList.contains('star-drag')) { sel = i; mark(); } },
          onclick: function () { if (!justDragged()) run(i); } },
          onTop && drag ? h('span', { class: 'grip', title: 'Drag to reorder' }) : null,
          it.label, h('span', { class: 'grp' }, it.grp),
          it.grp !== 'tab' ? starBtn(starKey('cmd', it), function () { render(); setTimeout(function () { inp.focus(); }, 0); }) : h('span', { class: 'star none' }));
        if (onTop && drag) dragStar(row, res, function (keys) { setStarOrder(keys); render(); setTimeout(function () { inp.focus(); }, 0); });
        return row;
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
      if (hasRemote(cur) || cur.localExists) items.push({ label: 'Files', action: function () { openFiles(cur); } });
      if (hasRemote(cur)) items.push({ label: 'Server terminal', action: function () { openSession({ kind: 'shell', where: 'server', project: cur.name }); } });
      if (cur.localExists) items.push({ label: 'Local terminal', action: function () { openSession({ kind: 'shell', where: 'local', project: cur.name }); } });
    }
    items.push({ header: 'Home' });
    items.push({ label: 'Server terminal', disabled: !serverReady(), action: function () { openSession({ kind: 'shell', where: 'server' }); } });
    items.push({ label: 'Local terminal', action: function () { openSession({ kind: 'shell', where: 'local' }); } });
    items.push('-');
    items.push({ label: 'Everything…', k: 'Ctrl+Shift+P', action: openPalette });
    items = webItems(items);
    if (anchor) menuAt(anchor, items);
    else { var r = (L.focused ? L.focused.bar : $('#layout')).getBoundingClientRect(); showMenu(r.left + 8, r.bottom + 4, items); }
  }

  // ------------------------------------------------------------------ keyboard
  function isGlobalShortcut(e) {
    if (e.isComposing || e.keyCode === 229) return false;      // on-screen keyboards (Android), IME input
    if (e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyP') return true;
    // Ctrl+P = go to file — except in a terminal, where the shell uses it (previous command)
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.code === 'KeyP' && !(S.active && S.active.term)) return true;
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
    if (e.code === 'KeyP' && !e.shiftKey) quickOpen();
    else if (e.code === 'KeyP') openPalette();
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
          // notify once when sync transitions into an error state
          var prev = (S.syncStates || {})[p.name];
          if (p.sync && p.sync.state === 'err' && prev && prev !== 'err') {
            toast(p.name + ': sync ' + (p.sync.label || 'disconnected') + ' — open Sync details to reconnect', 'err', { timeout: 10000 });
          }
        });
        S.syncStates = {};
        d.projects.forEach(function (p) { if (p.sync) S.syncStates[p.name] = p.sync.state; });
        S.projects = d.projects;
        renderProjects();
        repaintTabs();
        if (!S.tabs.length) renderEmpty();
        refreshStatus(false);
      }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ empty state
  function renderEmpty() {
    // what to do from here — buttons, so it works without a keyboard (phone, tablet)
    var hint = $('#empty-hint'), acts = $('#empty-actions');
    if (hint && acts) {
      var small = compact() || TOUCH.matches;
      hint.replaceChildren.apply(hint, small ? ['Open a project to start.']
        : ['Pick a project on the left, or press ', h('kbd', null, 'Ctrl+Shift+P'), '.']);
      acts.replaceChildren.apply(acts, [
        compact() ? h('button', { class: 'btn primary', onclick: openDrawer }, '☰  Projects') : null,
        S.projects.length ? null : h('button', { class: 'btn' + (compact() ? '' : ' primary'), onclick: addManualProject }, '+ Add project'),
        small ? h('button', { class: 'btn', onclick: openPalette }, '⌕  Search') : null
      ].filter(Boolean));
    }
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
        if (sp.kind === 'docker' && sp.action === 'rebuild') return null;
        var spec = { kind: sp.kind, where: sp.where, project: sp.project, cmdId: sp.cmdId, title: sp.title, claudeMode: sp.kind === 'claude' ? 'continue' : 'new' };
        if (sp.kind === 'docker') { spec.container = sp.container; spec.action = sp.action; }
        if ((sp.kind === 'chat' || sp.kind === 'claude') && sp.resume) spec.resume = sp.resume;
        if (sp.kind === 'chat' && sp.relay) spec.relay = sp.relay;
        if (sp.kind === 'chat' && sp.watch) spec.watch = true;      // the same Claude on the server, if it still works
        return openSession(spec, { background: i > 0 });
      });
    });
  }

  // ------------------------------------------------------------------ sidebar resize + work mode
  (function () {
    var w = Number(localStorage.getItem('sw-side')) || 230;
    document.documentElement.style.setProperty('--side', w + 'px');
    var rz = $('#resizer');
    // pointer events: mouse, touch (tablet) and pen alike
    onDrag(rz, function () {
      rz.classList.add('drag');
      return {
        move: function (e) {
          var side = $('#sidebar'), left = side ? side.getBoundingClientRect().left : 0;
          w = Math.max(170, Math.min(480, e.clientX - left));
          document.documentElement.style.setProperty('--side', w + 'px');
        },
        up: function () {
          rz.classList.remove('drag');
          localStorage.setItem('sw-side', String(w));
          fitAll();
        }
      };
    });
  })();

  // Small screens: the sidebar is a drawer over the workspace, one section at a time.
  var COMPACT = window.matchMedia ? window.matchMedia('(max-width: 760px)') : { matches: false };
  var TOUCH = window.matchMedia ? window.matchMedia('(pointer: coarse)') : { matches: false };
  function compact() { return !!COMPACT.matches; }
  function openDrawer() {
    document.body.classList.add('side-open');
    if (!$('#side-backdrop')) {
      var b = h('div', { id: 'side-backdrop', onclick: closeDrawer });
      document.body.append(b);
    }
  }
  function closeDrawer() {
    document.body.classList.remove('side-open');
    var b = $('#side-backdrop');
    if (b) b.remove();
  }
  function applyCompact() {
    document.body.classList.toggle('compact', compact());
    document.body.classList.toggle('touch', !!TOUCH.matches);
    if (!S.tabs.length) renderEmpty();
    if (compact()) {
      if (L.groups.length > 1) mergeAll();
    } else closeDrawer();
  }

  function setWorkMode(on) {
    document.body.classList.toggle('work-mode', on);
    localStorage.setItem(wkey('sw-work'), on ? '1' : '0');
    fitAll();
    setTimeout(fitAll, 120);
  }
  function toggleWorkMode() { setWorkMode(!document.body.classList.contains('work-mode')); }

  function serverTerminal() { return openSession({ kind: 'shell', where: 'server', title: 'Server terminal' }); }
  function serverFiles() { return openSession({ kind: 'files', title: 'Files · server' }); }

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

  // any script error is shown instead of failing silently — and written to the app's log, so a
  // problem on a phone or tablet can be found without a screenshot
  var lastErr = '', reported = {}, nReported = 0;
  function reportClientError(msg, where, stack) {
    var key = msg + '|' + where;
    if (reported[key] || nReported >= 20) return;
    reported[key] = 1;
    nReported++;
    api('POST', 'client-error', { message: String(msg).slice(0, 500), where: String(where || '').slice(0, 200),
      stack: String(stack || '').slice(0, 2000), page: location.pathname, ua: navigator.userAgent || '',
      size: (window.innerWidth || 0) + 'x' + (window.innerHeight || 0) }).catch(function () {});
  }
  window.addEventListener('error', function (e) {
    var msg = (e && e.message) || 'error';
    if (/ResizeObserver/.test(msg)) return;
    var where = e.filename ? e.filename.split('/').pop() + ':' + e.lineno + ':' + (e.colno || 0) : '';
    reportClientError(msg, where, e.error && e.error.stack);
    if (msg === lastErr) return;
    lastErr = msg;
    toast('Error: ' + msg + (where ? ' (' + where + ')' : ''), 'err', { timeout: 15000 });
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    if (r && r.status) return;                     // an API answer (shown where it was asked for)
    reportClientError((r && r.message) || String(r), 'promise', r && r.stack);
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
    closeTab: closeTab, renderProjects: renderProjects, activate: activate, setupDrag: setupDrag, isGlobalShortcut: isGlobalShortcut,
    confirmDlg: confirmDlg, promptDlg: promptDlg, openSession: openSession, showMenu: showMenu, renameTab: renameTab, renderTitle: renderTitle,
    notify: notify, pickClaudeSession: pickClaudeSession, layoutItemsFor: layoutItems, tabEl: tabEl, viewImage: viewImage,
    claudeInTerminal: claudeInTerminal, fullLocal: fullLocal, serverTerminal: serverTerminal, serverFiles: serverFiles,
    gitCode: gitCode, diffEl: diffEl, gitDialog: gitDialog, branchPicker: branchPicker, serverFolderDialog: serverFolderDialog, starterDialog: starterDialog,
    claudeSessionsDialog: claudeSessionsDialog, claudeStop: claudeStop, claudeTakeOver: claudeTakeOver, runningConv: runningConv, refreshRunning: refreshRunning,
    moveToNewWindow: moveToNewWindow, moveToWindow: moveToWindow, syncTabs: syncTabs, wid: function () { return WID; }, heartbeat: heartbeat, reportError: function (m, w, st) { reportClientError(m, w, st); }, saveLayout: function () { saveLayout(); }
  };

  // ------------------------------------------------------------------ boot
  function wire() {
    $('#btn-palette').addEventListener('click', openPalette);
    $('#update-note').addEventListener('click', updateDialog);
    $('#usage').addEventListener('click', usageDialog);
    $('#server-stats').addEventListener('click', statsDialog);
    $('#btn-usage').addEventListener('click', usageDialog);
    $('#btn-history').addEventListener('click', function () { historyDialog(); });
    var bc = $('#btn-claude');
    if (bc) bc.addEventListener('click', function () { openSession({ kind: 'chat' }); });
    var bt = $('#btn-terminal');
    if (bt) bt.addEventListener('click', serverTerminal);
    var bf = $('#btn-files');
    if (bf) bf.addEventListener('click', serverFiles);
    $('#btn-settings').addEventListener('click', function (e) {
      menuAt(e.currentTarget, webItems([
        { label: 'Claude sessions — all projects…', k: runningTotal() ? runningTotal() + ' running' : '', action: function () { claudeSessionsDialog(null); } },
        { label: 'History…', action: function () { historyDialog(); } },
        { label: 'Check for updates', action: function () { checkUpdate(true); } },
        { label: 'Settings…', action: settingsDialog },
        { label: 'Grid: all tabs side by side', action: gridAll },
        { label: 'Single section', disabled: L.groups.length < 2, action: mergeAll },
        { label: 'Hide sidebar', k: 'Alt+M', action: toggleWorkMode },
        '-',
        S.web && !standalone() ? { label: 'Install as an app…', action: installApp } : null,
        S.web ? { label: 'Sign out', action: logout } : null,
        { label: 'Quit', action: quitApp }
      ]));
    });
    $('#btn-hide-side').addEventListener('click', function () { if (compact()) closeDrawer(); else setWorkMode(true); });
    if (COMPACT.addEventListener) COMPACT.addEventListener('change', applyCompact);
    $('#show-side').addEventListener('click', function () { setWorkMode(false); });
    if (window.SideFilesPanel) { window.sideFilesPanel = new window.SideFilesPanel(); if (S.active) window.sideFilesPanel.onActivate(S.active); }
    $('#st-ssh').addEventListener('click', function () { refreshStatus(true); });
    $('#proj-filter').addEventListener('input', function (e) { S.filter = e.target.value; renderProjects(); });
    // an extra window starts with the sidebar hidden: it is for the tabs
    var wk = localStorage.getItem(wkey('sw-work'));
    if (WID ? wk !== '0' : wk === '1') setWorkMode(true);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { refreshProjects(); if (S.active) S.active.focus(); } });
    // back to the window: what you can see now is seen
    window.addEventListener('focus', function () {
      L.groups.forEach(function (g) { if (g.active && g.active.isChat && !Object.keys(g.active.perms || {}).length) g.active.el.classList.remove('done'); });
    });
    var asked = false;
    document.addEventListener('click', function () {
      if (asked) return;
      asked = true;
      if (S.settings.bellNotify && window.Notification && Notification.permission === 'default') Notification.requestPermission();
    });
  }

  function startWindows(st) {
    S.booted = true;
    setInterval(syncTabs, 4000);
    window.addEventListener('focus', function () { syncTabs(); });
    if (WID) {
      setInterval(function () { api('POST', 'windows/' + WID).catch(function () {}); }, 20000);
      window.addEventListener('pagehide', function () {
        // closing (or reloading): the server gives the tabs back to the main window unless we return
        try {
          fetch('/api/windows/' + WID, { method: 'DELETE', keepalive: true, credentials: 'same-origin', headers: { 'X-Token': token, 'X-SW': '1' } });
        } catch (e) { /* closing anyway */ }
        tellWindows();
      });
    } else {
      // forget the saved layouts of extra windows that are gone
      var alive = (st.windows || []).map(function (w) { return w.id; });
      try {
        var drop = [];
        for (var i = 0; i < (localStorage.length || 0); i++) {
          var k = localStorage.key(i), m = /^sw-(layout|work):([a-z0-9]+)$/.exec(k || '');
          if (m && alive.indexOf(m[2]) < 0) drop.push(k);
        }
        drop.forEach(function (k) { localStorage.removeItem(k); });
      } catch (e) { /* storage not available */ }
    }
  }

  function boot() {
    if (!token) {
      fetch('/api/web-info', { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }).then(function (info) {
        if (info.web) { S.web = true; start(); return; }
        document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' },
          h('p', { class: 'empty-title' }, 'Open this window with: simabworkspace gui'))));
      });
      return;
    }
    start();
  }

  function start() {
    wire();
    var take = TAKE.length ? Promise.all(TAKE.map(function (sid) {
      return api('POST', 'sessions/' + sid + '/win', { win: WID }).catch(function () { return null; });
    })) : WID ? api('POST', 'windows/' + WID).catch(function () {}) : Promise.resolve();
    take.then(function () { return api('GET', 'state'); }).then(function (st) {
      S.web = !!st.web;
      document.body.classList.toggle('web', S.web);
      registerWorker();
      S.settings = st.settings;
      S.server = st.server;
      S.projects = st.projects;
      S.projectsKey = JSON.stringify(st.projects);
      S.wsl = st.wsl;
      S.home = st.home || '';
      S.lastTabs = WID ? [] : st.lastTabs;
      S.version = st.version;
      S.windows = st.windows || [];
      S.bootNow = st.now || 0;
      document.body.classList.toggle('extra-window', !!WID);
      renderProjects();
      var want = sessionStorage.getItem('sw-active');
      var mine = (st.sessions || []).filter(function (x) { return (x.win || '') === WID; });
      if (TAKE.length) want = TAKE[0];
      var fromNote = /[#&]open=([0-9a-f]{12})/.exec(location.hash);      // a tapped phone notification
      if (fromNote) want = fromNote[1];
      var placed = initLayout(mine.map(function (x) { return x.id; }));
      mine.forEach(function (info) { addTab(info, true, placed[info.id] || L.focused); });
      L.groups.slice().forEach(function (g) { if (!g.tabs.length && L.groups.length > 1) removeGroup(g); });
      L.groups.forEach(function (g) {
        var a = g.tabs.find(function (x) { return x.id === g.wantActive; }) || g.tabs[0];
        if (a) showInGroup(g, a);
      });
      syncOrder();
      renderLayout();
      applyCompact();
      var t = S.tabs.find(function (x) { return x.id === want; }) || (L.focused && L.focused.active) || S.tabs[0];
      if (t) activate(t); else renderEmpty();
      refreshStatus(true);
      startWindows(st);
      // first run on a new machine: everything is set up in Settings
      if (!st.server.configured && !S.web) { toast('Welcome — set up this machine and your server here', 'ok', { timeout: 8000 }); settingsDialog(); }
      setInterval(refreshProjects, 5000);
      if (!S.web) {
        setTimeout(function () { checkUpdate(false); }, 3000);
        setInterval(function () { checkUpdate(false); }, 6 * 3600 * 1000);
      }
      refreshGit();
      setInterval(function () { refreshGit(); }, 10000);
      refreshRunning();
      setInterval(function () { refreshRunning(); }, 8000);
      setInterval(function () { refreshStatus(false); }, 20000);
      refreshDocker();
      setInterval(refreshDocker, 15000);
      if (!S.web) { refreshTunnels(); setInterval(refreshTunnels, 5000); }
      refreshUsage();
      setInterval(refreshUsage, 30000);
      refreshStats();
      setInterval(refreshStats, 5000);
      window.addEventListener('sw-usage', debounce(refreshUsage, 500));
    }).catch(function (e) {
      if (e.status === 401 && S.web) return;          // the sign-in page is shown
      document.body.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'empty-inner' },
        h('p', { class: 'empty-title' }, 'Cannot reach the app'), h('p', { class: 'muted' }, e.message + ' — run simabworkspace gui'))));
    });
  }

  var fonts = document.fonts && document.fonts.load
    ? Promise.all(['13px "JetBrains Mono"', 'bold 13px "JetBrains Mono"', '13px Inter', '13px Vazirmatn'].map(function (f) { return document.fonts.load(f).catch(function () {}); }))
    : Promise.resolve();
  fonts.then(boot);
})();
