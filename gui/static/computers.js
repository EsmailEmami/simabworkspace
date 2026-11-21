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
  var MODES = [['new', 'Ask for new commands'], ['every', 'Ask every time'], ['all', 'Allow all (except the deny list)']];
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
    if (d.builds[name] !== 'computers') return { on: 'server', ok: true, text: 'Builds run on the server' };
    var on = online(d);
    return on.length ? { on: 'computers', ok: true, text: 'Builds run on ' + on.map(function (x) { return x.name; }).join(', ') }
      : { on: 'computers', ok: false, text: 'No computer of yours is connected — Claude will not build this project' };
  }

  var infoStr = '';
  function setInfo(x) {
    var str = JSON.stringify(x);
    info = x;
    if (str !== infoStr && SW.renderProjects) SW.renderProjects();
    infoStr = str;
  }
  function refreshInfo() {
    return api('GET', 'pc/devices').then(function (x) { setInfo(x); return x; }).catch(function () { return info; });
  }

  // the sidebar: only projects that build on the computers get a mark (green: works, red: it will stop)
  function badge(p) {
    var st = buildState(p.name);
    if (!st || st.on !== 'computers') return null;
    return h('span', { class: 'gb pcb ' + (st.ok ? 'ok' : 'bad'), title: st.text + ' — click for Computers',
      onclick: function (e) { e.stopPropagation(); open(p.name); } }, st.ok ? '⌂ PC' : '⌂ off');
  }
  function menuLabel(name) {
    var st = buildState(name);
    return !st ? '' : st.on === 'server' ? 'Server' : st.ok ? 'My computers' : 'My computers · none on';
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
      card('Builds run on', 'per project', buildBox),
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
        S.web ? h('div', { class: 'pc-note' }, 'To connect a computer: open Simab Workspace on it → ⚙ → Computers → Connect this computer.') : null,
        h('div', { class: 'pc-actions' }, h('button', { class: 'btn sm', onclick: load }, 'Refresh'))]));
    }

    function renderBuilds() {
      if (!devs || devs.error) { kids(buildBox, []); return; }
      var b = devs.builds || {};
      var names = Object.keys(b).sort();
      if (!names.length) { kids(buildBox, [h('div', { class: 'muted' }, 'No project with a server folder.')]); return; }
      kids(buildBox, [h('div', { class: 'pc-note' }, 'My computers: Claude runs builds, tests and installs on a connected computer. '
        + 'If none is connected it stops and tells you — it never builds on the server instead. New chats pick up a change.')].concat(names.map(function (n) {
        var st = buildState(n, devs);
        function pick(v) {
          if (v === b[n]) return;
          api('POST', 'pc/builds', { project: n, on: v }).then(function (x) {
            devs = x; setInfo(x); renderBuilds();
            toast(n + ': builds run on ' + (v === 'server' ? 'the server' : 'your computers'), 'ok');
          }).catch(fail);
        }
        var row = h('div', { class: 'pc-build' + (n === focusProject ? ' focus' : '') },
          h('div', { class: 'pc-build-main' }, h('b', null, n),
            h('small', { class: st.on === 'server' ? 'muted' : st.ok ? 'ok' : 'err' }, (st.on === 'server' ? '' : st.ok ? '● ' : '⚠ ') + st.text)),
          h('div', { class: 'pc-seg' },
            h('button', { class: b[n] === 'server' ? 'on' : '', onclick: function () { pick('server'); } }, 'Server'),
            h('button', { class: b[n] === 'computers' ? 'on' : '', onclick: function () { pick('computers'); } }, 'My computers')));
        if (n === focusProject) setTimeout(function () { if (row.scrollIntoView) row.scrollIntoView({ block: 'center' }); }, 50);
        return row;
      })));
    }

    function renderRules() {
      if (S.web || !me) return;
      var c = me.config;
      var mode = h('select', { class: 'inp' }, MODES.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
      mode.value = c.mode;
      var out = h('select', { class: 'inp' }, OUTPUTS.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
      out.value = c.output;
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
        h('div', { class: 'pc-grid' },
          h('div', { class: 'field' }, h('label', null, 'When Claude wants to run a command'), mode),
          h('div', { class: 'field' }, h('label', null, 'Output'), out)),
        h('div', { class: 'pc-grid' },
          h('div', { class: 'field' }, h('label', null, 'Always allowed — one per line, * = anything'), allow),
          h('div', { class: 'field' }, h('label', null, 'Never allowed — wins over everything'), deny)),
        h('div', { class: 'field' }, h('label', null, 'Projects that may run here'), h('label', { class: 'check' }, all, h('span', null, 'All projects')), plist),
        h('div', { class: 'field' }, h('label', null, 'Remembered commands'), remBox),
        h('div', { class: 'pc-actions' },
          h('span', { class: 'muted' }, 'A question waits 2 minutes, then the command is not run.'),
          h('button', { class: 'btn primary', onclick: function () {
            save({ mode: mode.value, output: out.value, allow: lines(allow.value), deny: lines(deny.value),
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

  function showRun(id) {
    api('GET', 'pc/run/' + id).then(function (r) {
      SW.openModal({ title: r.cmd, size: 'wide', body: h('pre', { class: 'pc-out' }, r.out || '(no output)') });
    }).catch(fail);
  }

  // ------------------------------------------------------------------ the permission question
  function askModal(a) {
    var left = h('span', { class: 'muted' });
    function answer(v) {
      api('POST', 'pc/answer', { id: a.id, answer: v }).catch(function () { /* answered elsewhere */ });
      close();
    }
    function close() { if (shown[a.id]) { shown[a.id].md.close(); delete shown[a.id]; } }
    var md = SW.openModal({ title: 'Run on this computer?', size: 'sm', body: h('div', { class: 'pc-ask' },
      h('div', { class: 'muted' }, 'Claude (project ', h('b', null, a.project), ') wants to run:'),
      h('pre', { class: 'pc-cmd' }, a.cmd), left),
      foot: [h('button', { class: 'btn danger', onclick: function () { answer('deny'); } }, 'Deny'),
        h('div', { class: 'pc-ask-btns' },
          h('button', { class: 'btn', title: 'Never ask again for this exact command in ' + a.project, onclick: function () { answer('always'); } }, 'Always allow this'),
          h('button', { class: 'btn', title: 'Everything in ' + a.project + ' runs without asking for 1 hour', onclick: function () { answer('hour'); } }, 'Allow all 1 hour'),
          h('button', { class: 'btn primary', autofocus: true, onclick: function () { answer('once'); } }, 'Allow once'))] });
    md.onclose = function () { delete shown[a.id]; };
    shown[a.id] = { md: md, left: left, until: a.until };
    try {
      if (document.hidden && window.Notification && Notification.permission === 'granted') new Notification('Claude wants to run a command', { body: a.project + ': ' + a.cmd, icon: 'icon.svg' });
    } catch (e) { /* ignore */ }
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
      (x.asks || []).forEach(function (a) { openIds[a.id] = 1; if (!shown[a.id]) askModal(a); });
      Object.keys(shown).forEach(function (id) {
        var s = shown[id];
        if (!openIds[id]) { s.md.close(); delete shown[id]; return; }
        s.left.textContent = 'Not run if nobody answers in ' + Math.max(0, Math.round(s.until - now)) + 's.';
      });
      (x.runs || []).forEach(function (r) {
        if (seenRuns[r.id]) return;
        seenRuns[r.id] = 1;
        if (r.state === 'running' && x.output === 'panel') liveBox(r);
      });
      if (live) refreshLive();
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

  window.SWComputers = { open: open, poll: poll, badge: badge, menuLabel: menuLabel, buildState: buildState, refresh: refreshInfo,
    _setInfo: setInfo, _shown: shown, _live: function () { return live; } };
})();
