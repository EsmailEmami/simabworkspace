/* Simab Workspace — Computers: builds on the user's own computers (onpc).
   Claude stays on the server; a project set to "Builds run on: My computers" runs its builds / tests
   on a connected computer of this server user. This file: the Computers dialog (this computer's link,
   the user's computers, builds per project, permission rules, recent commands), the badge of each
   project in the sidebar (where its builds run, and if that works now), the permission question that
   pops up in every window of the app, and the live output box. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail, toast = SW.toast, S = SW.S;
  var POLL = 2000;              // permission questions (installed app, only while connected)
  var INFO_POLL = 30000;        // computers + builds per project (sidebar badges)
  // like Claude's own permission modes (chat.js MODES); the deny list always wins
  var MODES = [['chat', 'same as chat', 'Follows the chat that runs it: auto → runs without asking; ask, auto-edit, plan → asks'],
    ['every', 'ask', 'Asks before every command'],
    ['new', 'ask new', 'Asks once per command; remembered and allowed ones run'],
    ['all', 'auto', 'Runs every command without asking']];
  var AUTO_CHAT = { auto: 1, bypassPermissions: 1 };
  var OUTPUTS = [['chat', 'In the chat only'], ['panel', 'Also live in a box on this computer']];
  var OS = { windows: 'Windows', mac: 'Mac', linux: 'Linux', wsl: 'WSL' };
  var shown = {};               // ask id -> its modal
  var live = null;              // the live output box
  var seenRuns = {};
  var pollTimer = null;
  var info = null;              // last GET pc/devices: { devices, builds, self }

  function ago(t, now) {
    var s = Math.max(0, Math.round((now || Date.now() / 1000) - t));
    return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + 'm ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
  }
  function lines(v) { return (v || '').split('\n').map(function (x) { return x.trim(); }).filter(Boolean); }
  function dot(state) { return h('i', { class: 'pc-dot ' + state }); }
  function online(d) { return ((d && d.devices) || []).filter(function (x) { return x.online; }); }
  function kids(el, list) { el.replaceChildren.apply(el, list.filter(Boolean)); }

  // where a project builds and whether that works right now
  function buildState(name, d) {
    d = d || info;
    if (!d || !d.builds || !d.builds[name]) return null;
    if (d.builds[name] === 'local') {               // Local only: the files live on one computer
      var pr = (S.projects || []).filter(function (x) { return x.name === name; })[0] || {};
      var dev = ((d.devices) || []).filter(function (x) { return x.id === pr.localOnly; })[0];
      var who = dev ? dev.name : 'its computer';
      return dev && dev.online ? { on: 'local', ok: true, text: 'Lives on ' + who + ' — Claude works on it there' }
        : { on: 'local', ok: false, text: 'Lives on ' + who + ', which is not connected — Claude cannot work on it now' };
    }
    if (d.builds[name] !== 'computers') return { on: 'server', ok: true, text: 'Commands run on the server' };
    var pin = (d.pins || {})[name];
    if (pin) {                                       // a computer chosen for this project
      var pd = (d.devices || []).filter(function (x) { return x.id === pin; })[0];
      var pn = pd ? pd.name : 'the chosen computer';
      return pd && pd.online ? { on: 'computers', ok: true, text: 'Commands run on ' + pn + ' (chosen; files synced with the server)' }
        : { on: 'computers', ok: false, text: pn + ' (chosen for this project) is not connected — Claude cannot run its commands' };
    }
    var on = online(d);
    return on.length ? { on: 'computers', ok: true, text: 'Commands run on ' + on.map(function (x) { return x.name; }).join(', ') + ' (files synced with the server)' }
      : { on: 'computers', ok: false, text: 'No computer of yours is connected — Claude cannot run its commands' };
  }

  var infoStr = '';
  function setInfo(x) {
    var str = JSON.stringify(x);
    info = x;
    if (str !== infoStr && SW.renderProjects) SW.renderProjects();
    infoStr = str;
    decorate();
  }
  function refreshInfo() {
    return api('GET', 'pc/devices').then(function (x) { setInfo(x); return x; }).catch(function () { return info; });
  }

  // the sidebar: only projects that build on the computers get a mark (green: works, red: it will stop)
  function badge(p) {
    var st = buildState(p.name);
    if (!st || st.on === 'server') return null;
    // two different things: Local only = the files live there; My computers = only builds run there
    var local = st.on === 'local';
    return h('span', { class: 'gb pcb ' + (local ? 'lo ' : 'bd ') + (st.ok ? 'ok' : 'bad'), title: st.text + ' — click for Computers',
      onclick: function (e) { e.stopPropagation(); open(p.name); } }, (local ? '⌂ local' : '⚒ PC') + (st.ok ? '' : ' off'));
  }
  function menuLabel(name) {
    var st = buildState(name);
    return !st ? '' : st.on === 'server' ? 'Server' : st.on === 'local' ? (st.ok ? 'Local only' : 'Local only · off') : st.ok ? 'My computers' : 'My computers · none on';
  }

  // ------------------------------------------------------------------ the dialog
  function open(focusProject) {
    var body = h('div', { class: 'pc-dlg' });
    var md = SW.openModal({ title: 'Computers', size: 'pcm', body: body,
      foot: [h('span', { class: 'left muted' }, 'Claude stays on the server · builds run where you choose'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    var me = null, devs = info, timer = null;
    md.onclose = function () { clearInterval(timer); };

    function load() {
      var a = S.web ? Promise.resolve(null) : api('GET', 'pc').then(function (x) { me = Object.assign(x, { recent: me && me.recent }); });
      var b = api('GET', 'pc/devices').then(function (x) { devs = x; setInfo(x); }).catch(function (e) { devs = { error: e.message || String(e) }; });
      var c = S.web ? Promise.resolve() : api('GET', 'pc/asks').then(function (x) { if (me) me.recent = x; });
      return Promise.all([a, b]).then(function () { return c; }).then(render).catch(fail);
    }
    timer = setInterval(function () {
      if (S.web) return;
      Promise.all([api('GET', 'pc').then(function (x) { me = Object.assign(x, { recent: me && me.recent }); }),
        api('GET', 'pc/asks').then(function (x) { if (me) me.recent = x; })]).then(function () { renderThis(); renderRecent(); }).catch(function () {});
    }, 3000);

    function card(title, sub, content) {
      return h('section', { class: 'pc-card' }, h('div', { class: 'pc-card-h' }, h('b', null, title), sub ? h('span', { class: 'muted' }, sub) : null), content);
    }
    var thisBox = h('div'), devBox = h('div'), buildBox = h('div'), ruleBox = h('div'), recentBox = h('div');
    kids(body, [
      S.web ? null : card('This computer', '', thisBox),
      card('Your computers', 'connected to this server user', devBox),
      card('Commands run on', 'per project', buildBox),
      S.web ? null : card('Permission on this computer', 'what Claude may run here', ruleBox),
      S.web ? null : card('Recent commands here', '', recentBox)]);

    function render() { renderThis(); renderDevs(); renderBuilds(); renderRules(); renderRecent(); }

    function renderThis() {
      if (S.web || !me) return;
      var c = me.config, on = !!c.on;
      var kind = !on ? 'off' : me.state === 'connected' ? 'on' : me.state === 'error' ? 'err' : 'wait';
      var label = { off: 'Not connected', on: 'Connected', err: 'Cannot connect', wait: 'Connecting…' }[kind];
      var name = h('input', { class: 'inp pc-name', value: c.name, maxlength: 40, title: 'The name Claude and your other devices see' });
      name.addEventListener('change', function () { if (name.value.trim() && name.value.trim() !== c.name) save({ name: name.value.trim() }).then(load); });
      kids(thisBox, [
        h('div', { class: 'pc-this' },
          h('div', { class: 'pc-this-main' }, name, h('span', { class: 'muted' }, OS[me.os] || me.os)),
          h('span', { class: 'pc-pill ' + kind }, dot(kind), label),
          h('button', { class: 'btn' + (on ? '' : ' primary'), onclick: function () {
            save({ on: !on }).then(function () { S.pc = { on: !on }; startPoll(); setTimeout(load, 3500); });
          } }, on ? 'Disconnect' : 'Connect this computer')),
        kind === 'err' ? h('div', { class: 'pc-note err' }, me.error) : null,
        kind === 'on' ? h('div', { class: 'pc-note' }, 'Claude can run builds of your “My computers” projects here (after you allow them).') : null,
        kind === 'off' ? h('div', { class: 'pc-note' }, 'Connect to let Claude run builds, tests and installs on this computer instead of the server.') : null]);
    }

    function renderDevs() {
      if (!devs) { kids(devBox, [h('div', { class: 'muted' }, 'Loading…')]); return; }
      if (devs.error) { kids(devBox, [h('div', { class: 'pc-note err' }, devs.error)]); return; }
      var list = devs.devices || [];
      kids(devBox, (list.length ? list.map(function (d) {
        return h('div', { class: 'pc-dev' }, dot(d.online ? 'on' : 'off'),
          h('div', { class: 'pc-dev-main' },
            h('div', null, h('b', null, d.name || '?'), d.id === devs.self ? h('span', { class: 'pc-tag' }, 'this computer') : null),
            h('small', { class: 'muted' }, (OS[d.os] || d.os) + ' · ' + (d.online ? 'connected' : 'offline') + (d.since ? ' · last connected ' + ago(d.since) : ''))),
          h('button', { class: 'btn sm danger', title: 'Forget this computer on the server', onclick: function () {
            SW.confirmDlg('Remove “' + (d.name || '?') + '”? Claude can no longer run commands there until it connects again.', 'Remove', true).then(function (ok) {
              if (ok) api('POST', 'pc/remove', { id: d.id }).then(function (x) { devs = x; setInfo(x); load(); }).catch(fail);
            });
          } }, 'Remove'));
      }) : [h('div', { class: 'muted' }, 'No computer connected yet.')]).concat([
        S.web ? h('div', { class: 'pc-actions', style: 'justify-content:flex-start' },
          h('button', { class: 'btn primary sm', onclick: pairDialog }, '+ Connect a computer'),
          h('span', { class: 'muted' }, 'one line on the new computer — no password, the link works once for 10 minutes')) : null,
        h('div', { class: 'pc-actions' }, h('button', { class: 'btn sm', onclick: load }, 'Refresh'))]));
    }

    function renderBuilds() {
      if (!devs || devs.error) { kids(buildBox, []); return; }
      var b = devs.builds || {};
      var names = Object.keys(b).sort();
      if (!names.length) { kids(buildBox, [h('div', { class: 'muted' }, 'No project with a server folder.')]); return; }
      function pinSel(n) {
        var sel = h('select', { class: 'inp pc-pin', title: 'Which computer runs this project\'s commands' },
          [h('option', { value: '' }, 'Any connected')].concat((devs.devices || []).map(function (x) { return h('option', { value: x.id }, x.name); })));
        sel.value = (devs.pins || {})[n] || '';
        sel.addEventListener('change', function () {
          api('POST', 'pc/builds', { project: n, on: 'computers', dev: sel.value }).then(function (x) {
            devs = x; setInfo(x); renderBuilds();
            toast(n + ': commands run on ' + (sel.value ? sel.options[sel.selectedIndex].textContent : 'any connected computer'), 'ok');
          }).catch(fail);
        });
        return sel;
      }
      kids(buildBox, [h('div', { class: 'pc-note' }, 'My computers: the files stay on the server (synced with this computer) and Claude works on them there, '
        + 'but every command it runs (build, npm, git, tests, dev servers…) runs on a connected computer. If none is connected it stops and tells you — '
        + 'it never runs them on the server instead. New chats pick up a change.')].concat(names.map(function (n) {
        var st = buildState(n, devs);
        function pick(v) {
          if (v === b[n]) return;
          api('POST', 'pc/builds', { project: n, on: v }).then(function (x) {
            devs = x; setInfo(x); renderBuilds();
            toast(n + ': builds run on ' + (v === 'server' ? 'the server' : 'your computers'), 'ok');
          }).catch(fail);
        }
        var mountErr = st.on === 'local' && me && me.mounts && me.mounts[n] && me.mounts[n] !== 'ok' ? me.mounts[n] : '';
        var row = h('div', { class: 'pc-build' + (n === focusProject ? ' focus' : '') },
          h('div', { class: 'pc-build-main' }, h('b', null, n),
            h('small', { class: st.on === 'server' ? 'muted' : st.ok ? 'ok' : 'err' }, (st.on === 'server' ? '' : st.ok ? '● ' : '⚠ ') + st.text),
            mountErr ? h('small', { class: 'err' }, '⚠ the server could not open it: ' + mountErr, ' ',
              h('button', { class: 'btn sm', onclick: function () { api('POST', 'pc/mount').then(load).catch(fail); } }, 'Try again')) : null),
          b[n] === 'computers' && (devs.devices || []).length > 1 ? pinSel(n) : null,
          st.on === 'local' ? h('span', { class: 'pc-tag' }, 'Local only') : h('div', { class: 'pc-seg' },
            h('button', { class: b[n] === 'server' ? 'on' : '', onclick: function () { pick('server'); } }, 'Server'),
            h('button', { class: b[n] === 'computers' ? 'on' : '', onclick: function () { pick('computers'); } }, 'My computers')));
        if (n === focusProject) setTimeout(function () { if (row.scrollIntoView) row.scrollIntoView({ block: 'center' }); }, 50);
        return row;
      })));
    }

    function renderRules() {
      if (S.web || !me) return;
      var c = me.config;
      var mode = { value: c.mode };
      var modeBox = h('div', { class: 'seg pc-modes' }, MODES.map(function (m) {
        var el = h('button', { class: 'segb' + (m[0] === c.mode ? ' on' : ''), onclick: function () {
          mode.value = m[0];
          Array.prototype.forEach.call(modeBox.children, function (x) { x.classList.toggle('on', x === el); });
        } }, h('b', null, m[1]), h('small', null, m[2]));
        return el;
      }));
      var out = h('select', { class: 'inp' }, OUTPUTS.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
      out.value = c.output;
      var autoOpen = h('input', { type: 'checkbox', checked: !!c.autoOpen });
      var allow = h('textarea', { class: 'inp mono', rows: 4, placeholder: 'npm run *\nng build*' });
      allow.value = (c.allow || []).join('\n');
      var deny = h('textarea', { class: 'inp mono', rows: 4 });
      deny.value = (c.deny || []).join('\n');
      var projs = Object.keys((devs && devs.builds) || {}).sort();
      var all = h('input', { type: 'checkbox', checked: !(c.projects || []).length });
      var boxes = projs.map(function (n) { return { n: n, el: h('input', { type: 'checkbox', checked: (c.projects || []).indexOf(n) >= 0 }) }; });
      var plist = h('div', { class: 'pc-projs' + (all.checked ? ' hidden' : '') }, boxes.map(function (b) { return h('label', { class: 'check' }, b.el, h('span', null, b.n)); }));
      all.addEventListener('change', function () { plist.classList.toggle('hidden', all.checked); });
      var always = c.always || {};
      var rem = [];
      Object.keys(always).forEach(function (p) { always[p].forEach(function (cmd) { rem.push({ p: p, cmd: cmd }); }); });
      var remBox = h('div', { class: 'pc-rems' }, rem.length ? rem.map(function (r) {
        return h('div', { class: 'pc-rem' }, h('span', { class: 'muted' }, r.p), h('code', null, r.cmd),
          h('button', { class: 'icon-btn', title: 'Forget', onclick: function () {
            var a = JSON.parse(JSON.stringify(always));
            a[r.p] = a[r.p].filter(function (x) { return x !== r.cmd; });
            save({ always: a }).then(load);
          } }, '✕'));
      }) : h('div', { class: 'muted' }, 'None yet — “Always allow this” in a question adds one.'));
      kids(ruleBox, [
        h('div', { class: 'field' }, h('label', null, 'When Claude wants to run a command here'), modeBox),
        h('div', { class: 'field' }, h('label', null, 'Output'), out),
        h('label', { class: 'check' }, autoOpen, h('span', null, 'Open dev servers in the browser by themselves',
          h('small', null, 'Otherwise a message offers to open them when Claude starts one (onpc --bg).'))),
        h('div', { class: 'pc-grid' },
          h('div', { class: 'field' }, h('label', null, 'Always allowed — one per line, * = anything'), allow),
          h('div', { class: 'field' }, h('label', null, 'Never allowed — wins over everything'), deny)),
        h('div', { class: 'field' }, h('label', null, 'Projects that may run here'), h('label', { class: 'check' }, all, h('span', null, 'All projects')), plist),
        h('div', { class: 'field' }, h('label', null, 'Remembered commands'), remBox),
        h('div', { class: 'pc-actions' },
          h('span', { class: 'muted' }, 'A question waits 2 minutes, then the command is not run.'),
          h('button', { class: 'btn primary', onclick: function () {
            save({ mode: mode.value, output: out.value, autoOpen: autoOpen.checked, allow: lines(allow.value), deny: lines(deny.value),
              projects: all.checked ? [] : boxes.filter(function (b) { return b.el.checked; }).map(function (b) { return b.n; }) })
              .then(function () { toast('Saved', 'ok'); load(); });
          } }, 'Save rules'))]);
    }

    function renderRecent() {
      if (S.web || !me) return;
      var runs = (me.recent && me.recent.runs) || [];
      kids(recentBox, runs.length ? runs.map(function (r) {
        return h('div', { class: 'pc-run', title: 'Show the output', onclick: function () { showRun(r.id); } },
          h('span', { class: 'pc-state ' + r.state + (r.state === 'done' && r.code !== 0 ? ' bad' : '') }, r.state === 'done' ? (r.code === 0 ? '✓ done' : '✗ exit ' + r.code) : r.state),
          h('span', { class: 'muted' }, r.project), h('code', null, r.cmd), h('span', { class: 'muted' }, ago(r.start, me.recent.now)));
      }) : [h('div', { class: 'muted' }, 'Nothing run here yet.')]);
    }

    function save(patch) { return api('POST', 'pc', patch).then(function (x) { me = Object.assign(x, { recent: me && me.recent }); S.pc = { on: !!x.config.on }; return x; }).catch(function (e) { fail(e); throw e; }); }

    render();
    load();
    return md;
  }

  // Connect a computer (web version): a one-time link → one line to paste on the new computer
  var REL = 'https://github.com/EsmailEmami/simabworkspace/releases/latest/download/';
  function pairDialog() {
    api('POST', 'pc/pair').then(function (r) {
      var link = location.origin + '/pair/' + r.code;
      var cmds = {
        unix: 'curl -fsSL ' + REL + "install.sh | SIMAB_PAIR='" + link + "' bash",
        win: "$env:SIMAB_PAIR='" + link + "'; irm " + REL + 'install.ps1 | iex' };
      var which = /Win/.test(navigator.platform || '') ? 'win' : 'unix';
      var pre = h('pre', { class: 'pc-cmd' });
      var left = h('span', { class: 'muted' });
      var tabs = h('div', { class: 'pc-seg' });
      function show(w) {
        which = w;
        pre.textContent = cmds[w];
        kids(tabs, [['unix', 'Mac · Linux · WSL'], ['win', 'Windows (PowerShell)']].map(function (x) {
          return h('button', { class: x[0] === w ? 'on' : '', onclick: function () { show(x[0]); } }, x[1]);
        }));
      }
      show(which);
      var copy = h('button', { class: 'btn primary', onclick: function () {
        (navigator.clipboard ? navigator.clipboard.writeText(cmds[which]) : Promise.reject()).then(function () { toast('Copied — paste it on the new computer', 'ok'); }, function () { toast('Select the line and copy it', 'warn'); });
      } }, 'Copy');
      var md = SW.openModal({ title: 'Connect a computer', body: h('div', { class: 'pc-pair' },
        h('div', { class: 'pc-note', style: 'margin-top:0' }, 'On the new computer open a terminal (Windows: PowerShell) and paste this one line. '
          + 'It installs Simab Workspace, connects it to your server user and turns on Computers. Nothing else to type.'),
        tabs, pre, left),
        foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close'), copy] });
      var t = setInterval(function () {
        var s2 = Math.round(r.until - Date.now() / 1000);
        left.textContent = s2 > 0 ? 'Works once, for ' + Math.floor(s2 / 60) + ':' + ('0' + (s2 % 60)).slice(-2) + ' more.' : 'Expired — close and make a new one.';
        if (s2 <= 0) clearInterval(t);
      }, 1000);
      md.onclose = function () { clearInterval(t); };
    }).catch(fail);
  }

  function showRun(id) {
    api('GET', 'pc/run/' + id).then(function (r) {
      SW.openModal({ title: r.cmd, size: 'wide', body: h('pre', { class: 'pc-out' }, r.out || '(no output)') });
    }).catch(fail);
  }

  // ------------------------------------------------------------------ the permission question
  function askModal(a) {
    var left = h('span', { class: 'muted' });
    function answer(v) {
      sendAnswer(a, v);
      close();
    }
    function close() { if (shown[a.id]) { shown[a.id].close(); delete shown[a.id]; } }
    var md = SW.openModal({ title: 'Run on this computer?', size: 'sm', body: h('div', { class: 'pc-ask' },
      h('div', { class: 'muted' }, 'Claude (project ', h('b', null, a.project), ') wants to run:'),
      h('pre', { class: 'pc-cmd' }, a.cmd), left),
      foot: [h('button', { class: 'btn danger', onclick: function () { answer('deny'); } }, 'Deny'),
        h('div', { class: 'pc-ask-btns' },
          h('button', { class: 'btn', title: 'Never ask again for this exact command in ' + a.project, onclick: function () { answer('always'); } }, 'Always allow this'),
          h('button', { class: 'btn', title: 'Everything in ' + a.project + ' runs without asking for 1 hour', onclick: function () { answer('hour'); } }, 'Allow all 1 hour'),
          h('button', { class: 'btn primary', autofocus: true, onclick: function () { answer('once'); } }, 'Allow once'))] });
    md.onclose = function () { delete shown[a.id]; };
    shown[a.id] = { a: a, left: [left], close: function () { md.close(); } };
    try {
      if (document.hidden && window.Notification && Notification.permission === 'granted') new Notification('Claude wants to run a command', { body: a.project + ': ' + a.cmd, icon: 'icon.svg' });
    } catch (e) { /* ignore */ }
  }

  // the installed app answers its own questions; the web version through the server (PC_BRIDGE_PY)
  function sendAnswer(a, v) {
    return (S.web ? api('POST', 'pc/remote-answer', { dev: a.dev, id: a.id, answer: v }) : api('POST', 'pc/answer', { id: a.id, answer: v }))
      .catch(function () { /* answered elsewhere / gone */ });
  }

  // The question goes ONLY into the chat that ran onpc (a.session: its tab, found by the server from
  // the chat's relay key), right above its message box like Claude's own permission cards, and that
  // tab rings. Never into another tab or as a dialog over whatever is open. That chat is not open in
  // this app (started on the web / another device): one quiet toast in the main window that opens it.
  var DONE = { once: 'Allowed once — running on this computer', always: 'Always allowed here — running on this computer',
    hour: 'Allowed everything in this project for 1 hour — running', deny: 'Denied — not run' };
  function chatsFor(a) {
    var tabs = (S.tabs || []).filter(function (t) { return t.pcAskCard && !t.closed && t.info && t.info.spec && t.info.spec.kind === 'chat'; });
    if (a.session) return tabs.filter(function (t) { return t.info.id === a.session; });
    if (a.chat) return [];
    // a chat started before this version does not say which it is: only a working chat of that project
    return tabs.filter(function (t) { return t.busy && t.info.spec.project === a.project; }).slice(0, 1);
  }
  var toasted = {};
  function ask(a, mode) {
    var tabs = chatsFor(a);
    if (!tabs.length) {
      if (a.session || (SW.wid && SW.wid()) || toasted[a.id]) return;      // the chat is in another window / already said
      toasted[a.id] = 1;
      toast('Claude (' + a.project + ', a chat not open here) wants to run: ' + a.cmd + ' — click to answer', 'warn',
        { onclick: function () { if (shown[a.id] || !toasted[a.id]) return; askModal(a); } });
      return;
    }
    var views = [];
    if (mode === 'chat' && tabs.some(function (t) { return AUTO_CHAT[t.mode]; })) {      // the chat is in auto: no question
      shown[a.id] = { a: a, left: [], answered: 'once', close: function () {} };
      sendAnswer(a, 'once');
      tabs.filter(function (t) { return AUTO_CHAT[t.mode]; }).slice(0, 1).forEach(function (t) {
        if (t.system) t.system('⌂ Ran on this computer without asking (chat in auto): ' + a.cmd, 'mark');
      });
      return;
    }
    function answer(v) {
      if (!shown[a.id] || shown[a.id].answered) return;
      shown[a.id].answered = v;
      sendAnswer(a, v);
      views.forEach(function (x) { x.close(DONE[v]); });
      delete shown[a.id];
    }
    var modes = S.web ? null : { list: MODES, value: mode || 'new', set: function (v) {
      api('POST', 'pc', { mode: v }).then(function () {
        toast('This computer: ' + MODES.filter(function (m) { return m[0] === v; })[0][1], 'ok');
        // the new mode lets this command run without a question → run it now
        if (v === 'all' || (v === 'chat' && tabs.some(function (t) { return AUTO_CHAT[t.mode]; }))) answer('once');
      }).catch(fail);
    } };
    views = tabs.map(function (t) { return t.pcAskCard(a, answer, modes); });
    shown[a.id] = { a: a, left: views.map(function (x) { return x.left; }), close: function (text) { views.forEach(function (x) { x.close(text); }); } };
  }

  // ------------------------------------------------------------------ in the chat itself
  var chatRuns = {};            // run id -> finished and shown
  function chatTab(sid) {
    return (S.tabs || []).filter(function (t) { return t.pcRunView && !t.closed && t.info && t.info.id === sid; })[0];
  }
  function myName() {
    var d = info && (info.devices || []).filter(function (x) { return x.id === info.self; })[0];
    return d ? d.name : 'this computer';
  }
  var bootAt = Date.now() / 1000 - 2;
  function showInChat(tab, r) {
    if (chatRuns[r.id]) return;
    if (r.start < bootAt && !(tab.pcRuns && tab.pcRuns[r.id])) { chatRuns[r.id] = 1; return; }   // from before this window
    if (r.state !== 'running' && r.state !== 'asking') chatRuns[r.id] = 1;     // last update, then leave it
    if (r.state === 'asking') { tab.pcRunView(r, null, myName()); return; }
    api('GET', 'pc/run/' + r.id).then(function (d) { tab.pcRunView(d, d.out, myName()); }).catch(function () {});
  }
  // every chat says where its work runs (Local only / builds on your computers)
  function decorate() {
    (S.tabs || []).forEach(function (t) {
      if (!t.setWhere || !t.info || !t.info.spec || t.info.spec.kind !== 'chat') return;
      var p = (S.projects || []).filter(function (x) { return x.name === t.info.spec.project; })[0];
      var st = p ? buildState(p.name) : null;
      if (!st || st.on === 'server') { t.setWhere(''); return; }
      t.setWhere(st.on === 'local' ? '⌂ local' + (st.ok ? '' : ' · off') : '⚒ runs on PC' + (st.ok ? '' : ' · off'), st.text, st.ok ? (st.on === 'local' ? 'lo' : 'bd') : 'bad');
    });
  }

  // ------------------------------------------------------------------ live output box
  function liveBox(run) {
    if (live) live.el.remove();
    var pre = h('pre', { class: 'pc-out' });
    var head = h('span', null, '');
    var el = h('div', { class: 'pc-live' }, h('div', { class: 'pc-live-head' }, head,
      h('button', { class: 'icon-btn', title: 'Close', onclick: function () { el.remove(); live = null; } }, '✕')), pre);
    document.body.append(el);
    live = { el: el, id: run.id, pre: pre, head: head };
    refreshLive();
  }
  function refreshLive() {
    if (!live) return;
    var l = live;
    api('GET', 'pc/run/' + l.id).then(function (r) {
      if (live !== l) return;
      l.head.textContent = (r.state === 'done' ? (r.code === 0 ? '✓ ' : '✗ ' + r.code + ' ') : r.state + ' · ') + r.project + ': ' + r.cmd;
      var bottom = l.pre.scrollTop + l.pre.clientHeight >= l.pre.scrollHeight - 4;
      l.pre.textContent = r.out;
      if (bottom) l.pre.scrollTop = l.pre.scrollHeight;
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ polling
  function poll() {
    if (S.web || !S.pc || !S.pc.on) return Promise.resolve();
    return api('GET', 'pc/asks').then(function (x) {
      var now = x.now || Date.now() / 1000;
      var openIds = {};
      (x.asks || []).forEach(function (a) { openIds[a.id] = 1; if (!shown[a.id]) ask(a, x.mode); });
      Object.keys(shown).forEach(function (id) {
        var s = shown[id];
        if (!openIds[id]) {                 // answered in another window, or the time ran out
          var r = (x.runs || []).filter(function (r) { return r.id === s.a.run; })[0];
          s.close(r && r.state === 'denied' ? 'Not run on this computer' : 'Answered — running on this computer');
          delete shown[id];
          return;
        }
        s.left.forEach(function (el) { el.textContent = 'not run if nobody answers in ' + Math.max(0, Math.round(s.a.until - now)) + 's'; });
      });
      (x.runs || []).forEach(function (r) {
        var tab = r.session && chatTab(r.session);
        if (tab) { showInChat(tab, r); return; }       // in its own chat, inside the Bash card
        if (seenRuns[r.id]) return;
        seenRuns[r.id] = 1;
        if (r.state === 'running' && x.output === 'panel' && !r.session) liveBox(r);
      });
      if (live) refreshLive();
      decorate();
      devServers(x.bg || []);
      devUp(x.bg || [], x.autoOpen);
      linkWatch(x.link);
    }).catch(function () {});
  }
  function startPoll() {
    if (pollTimer || S.web) return;
    var first = true;
    pollTimer = setInterval(function () {
      if (!S.pc || !S.pc.on) return;
      if (first) {                     // runs from before this window opened are not "new"
        first = false;
        api('GET', 'pc/asks').then(function (x) { (x.runs || []).forEach(function (r) { if (r.state !== 'running') seenRuns[r.id] = 1; }); poll(); }).catch(function () {});
        return;
      }
      poll();
    }, POLL);
  }
  startPoll();
  setTimeout(refreshInfo, 1500);
  setInterval(function () { if (!document.hidden) refreshInfo(); }, INFO_POLL);

  // ------------------------------------------------------------------ the link's quality (sidebar, next to "simab")
  // Local only projects live and die by this computer's internet: 5 bars from the round trip to the
  // server, and what goes over the link right now (↓ from the server, ↑ to it).
  function rate(b) { return b < 1024 ? Math.round(b) + ' B/s' : b < 1048576 ? (b / 1024).toFixed(b < 10240 ? 1 : 0) + ' KB/s' : (b / 1048576).toFixed(1) + ' MB/s'; }
  function netBox(n) {
    var el = document.getElementById('pc-net');
    if (!el) return;
    var show = !S.web && S.pc && S.pc.on && (S.projects || []).some(function (p) { return p.localOnly; });
    el.classList.toggle('hidden', !show);
    if (!show || !n) return;
    var bars = [1, 2, 3, 4, 5].map(function (i) { return h('i', { class: 'nb b' + i + (i <= n.bars ? ' on' : '') }); });
    el.className = 'pc-net q' + n.bars;
    el.replaceChildren(h('span', { class: 'nbars' }, bars),
      h('span', { class: 'nrate' }, '↓' + rate(n.down) + ' ↑' + rate(n.up)));
    el.title = n.state !== 'connected' ? 'This computer is not connected to the server — Local only projects cannot work'
      : 'Connection to the server: ' + (n.rtt == null ? 'no answer' : n.rtt + ' ms round trip') + '\nNow: ↓ ' + rate(n.down) + ' from the server, ↑ ' + rate(n.up) + ' to it';
  }
  function pollNet() {
    if (S.web || !S.pc || !S.pc.on || !(S.projects || []).some(function (p) { return p.localOnly; })) { netBox(null); return Promise.resolve(); }
    return api('GET', 'pc/net').then(netBox).catch(function () { netBox({ state: 'off', bars: 0, down: 0, up: 0 }); });
  }
  setInterval(function () { if (!document.hidden) pollNet(); }, 1000);

  // web version: the questions of the user's computers, in the chat that asks (or one toast)
  function webPoll() {
    if (!S.web) return Promise.resolve();
    return api('GET', 'pc/remote-asks').then(function (x) {
      var now = x.now || Date.now() / 1000, openIds = {};
      (x.asks || []).forEach(function (a) { openIds[a.id] = 1; if (!shown[a.id]) ask(a, ''); });
      Object.keys(shown).forEach(function (id) {
        var s = shown[id];
        if (!openIds[id]) { s.close('Answered — see the chat'); delete shown[id]; return; }
        s.left.forEach(function (el) { el.textContent = 'on ' + (s.a.devName || 'your computer') + ' · not run if nobody answers in ' + Math.max(0, Math.round(s.a.until - now)) + 's'; });
      });
    }).catch(function () {});
  }
  setInterval(function () { if (S.web && !document.hidden) webPoll(); }, 3000);

  // dev servers (onpc --bg) in the chats of their project
  var DEV_ACT = {
    stop: function (x) { api('POST', 'pc/bgstop', { name: x.name }).then(poll).catch(fail); },
    logs: function (x) {
      api('GET', 'pc/bglog/' + encodeURIComponent(x.name)).then(function (d) {
        SW.openModal({ title: d.name + ' · ' + d.state + (d.url ? ' · ' + d.url : ''), size: 'wide', body: h('pre', { class: 'pc-out' }, d.out || '(no output yet)') });
      }).catch(fail);
    } };
  var dismissed = {};
  function devServers(list) {
    DEV_ACT.stop = function (x) {
      if (x.state !== 'running') { dismissed[x.name + x.start] = 1; devServers(list); return; }
      api('POST', 'pc/bgstop', { name: x.name }).then(poll).catch(fail);
    };
    var live2 = list.filter(function (x) { return x.state === 'running' || (x.state === 'exited' && !dismissed[x.name + x.start]); });
    (S.tabs || []).forEach(function (t) {
      if (!t.setDevServers || !t.info || !t.info.spec || t.info.spec.kind !== 'chat') return;
      t.setDevServers(live2.filter(function (x) { return x.project === t.info.spec.project; }), DEV_ACT);
    });
  }

  // a dev server got its address: open it in the browser (setting) or offer it once
  var upSeen = null;
  function devUp(list, auto) {
    var first = upSeen === null;
    upSeen = upSeen || {};
    list.forEach(function (x) {
      var k = x.name + ':' + x.start;
      if (x.state !== 'running' || !x.url || upSeen[k]) return;
      upSeen[k] = 1;
      if (first) return;                  // running before this window opened: not new
      if (auto) { window.open(x.url, '_blank', 'noopener'); toast(x.name + ' is running — opened ' + x.url, 'ok'); return; }
      toast(x.name + ' is running at ' + x.url + ' — click to open', 'ok', { sticky: true, onclick: function () { window.open(x.url, '_blank', 'noopener'); } });
    });
  }

  // the computer's link dropped while Claude works on a project here: say it in that chat at once,
  // and again when it is back (with Continue)
  var lastLink = '', warned = {};
  function linkWatch(state) {
    if (!state) return;
    var was = lastLink;
    lastLink = state;
    if (!was || (was === 'connected') === (state === 'connected')) return;
    (S.tabs || []).forEach(function (t) {
      if (!t.system || !t.info || !t.info.spec || t.info.spec.kind !== 'chat') return;
      var st = buildState(t.info.spec.project);
      if (!st || st.on === 'server') return;
      var id = t.info.id;
      if (state !== 'connected' && t.busy) {
        warned[id] = 1;
        t.system('⚠ Your computer lost its connection to the server — Claude cannot reach '
          + (st.on === 'local' ? 'the project files' : 'it for builds') + ' until it is back.', 'err');
        toast('Your computer is offline — Claude in ' + t.info.spec.project + ' is stuck', 'warn');
      } else if (state === 'connected' && warned[id]) {
        delete warned[id];
        var box = t.system('✓ Your computer is connected again.', 'mark');
        if (box && box.append && t.ta && t.submit) box.append(' ', h('button', { class: 'btn sm', onclick: function () {
          t.ta.value = 'My computer is connected again — continue where you stopped (redo the step that failed).';
          t.submit();
        } }, 'Continue'));
      }
    });
  }

  // where a Bash command of this chat ran: "pc" / "server" / "" (a server project: no tag).
  // Same rules as onpc's hook: Local only → the computer, except commands about Claude's own files.
  var CLAUDE_FILES = /~\/\.claude|\$\{?HOME\}?\/\.claude|\/\.claude\/|\/tmp\/claude-/;
  function bashWhere(tab, cmd) {
    var p = tab && tab.info && tab.info.spec && tab.info.spec.project;
    var st = p ? buildState(p) : null;
    if (!st || st.on === 'server' || typeof cmd !== 'string') return '';
    return CLAUDE_FILES.test(cmd) ? 'server' : 'pc';     // Local only and My computers: every command, like onpc's hook
  }

  setInterval(decorate, 5000);         // new chat tabs
  window.SWComputers = { open: open, poll: poll, webPoll: webPoll, decorate: decorate, pollNet: pollNet, bashWhere: bashWhere, _linkWatch: linkWatch, badge: badge, menuLabel: menuLabel, buildState: buildState, refresh: refreshInfo,
    _setInfo: setInfo, _shown: shown, _live: function () { return live; } };
})();
