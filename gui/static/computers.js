/* Simab Workspace — Computers: builds on the user's own computers (onpc).
   Claude stays on the server; a project set to "Builds run on: My computers" runs its builds / tests
   on a connected computer of this server user. This file: the Computers dialog (this computer's link,
   the user's computers, builds per project, permission rules, recent commands), the permission
   question that pops up in every window of the app, and the live output box. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail, toast = SW.toast, S = SW.S;
  var POLL = 2000;
  var MODES = [['new', 'Ask for new commands'], ['every', 'Ask every time'], ['all', 'Allow all (except the deny list)']];
  var OUTPUTS = [['chat', 'In the chat only'], ['panel', 'Also live in a box on this computer']];
  var OS = { windows: 'Windows', mac: 'Mac', linux: 'Linux', wsl: 'WSL' };
  var shown = {};               // ask id -> its modal
  var live = null;              // the live output box
  var seenRuns = {};
  var pollTimer = null;

  function ago(t, now) {
    var s = Math.max(0, Math.round((now || Date.now() / 1000) - t));
    return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + 'm ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
  }
  function lines(v) { return (v || '').split('\n').map(function (x) { return x.trim(); }).filter(Boolean); }
  function dot(state) { return h('i', { class: 'pc-dot ' + state }); }

  // ------------------------------------------------------------------ the dialog
  function open(focusProject) {
    var body = h('div', { class: 'pc-dlg' });
    var md = SW.openModal({ title: 'Computers', body: body,
      foot: [h('span', { class: 'left muted' }, 'Claude stays on the server; builds run on your computers.'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    var me = null, devs = null, timer = null;
    md.onclose = function () { clearInterval(timer); };

    function load() {
      var a = S.web ? Promise.resolve(null) : api('GET', 'pc').then(function (x) { me = x; });
      var b = api('GET', 'pc/devices').then(function (x) { devs = x; }).catch(function (e) { devs = { error: e.message || String(e) }; });
      var c = S.web ? Promise.resolve() : api('GET', 'pc/asks').then(function (x) { me && (me.recent = x); });
      return Promise.all([a, b, c]).then(render).catch(fail);
    }
    timer = setInterval(function () {
      if (S.web) return;
      Promise.all([api('GET', 'pc').then(function (x) { me = Object.assign(x, { recent: me && me.recent }); }),
        api('GET', 'pc/asks').then(function (x) { if (me) me.recent = x; })]).then(function () { renderThis(); renderRecent(); }).catch(function () {});
    }, 3000);

    var thisBox = h('div'), devBox = h('div'), buildBox = h('div'), ruleBox = h('div'), recentBox = h('div');
    body.append(
      S.web ? null : h('div', { class: 'sec' }, 'This computer'), S.web ? null : thisBox,
      h('div', { class: 'sec' }, 'Your computers'), devBox,
      h('div', { class: 'sec' }, 'Builds run on'), buildBox,
      S.web ? null : h('div', { class: 'sec' }, 'Permission on this computer'), S.web ? null : ruleBox,
      S.web ? null : h('div', { class: 'sec' }, 'Recent commands here'), S.web ? null : recentBox);

    function render() { renderThis(); renderDevs(); renderBuilds(); renderRules(); renderRecent(); }

    function renderThis() {
      if (S.web || !me) return;
      var c = me.config, on = !!c.on;
      var st = !on ? 'Not connected' : me.state === 'connected' ? 'Connected — Claude can run builds here'
        : me.state === 'error' ? 'Problem: ' + me.error : 'Connecting…';
      var name = h('input', { class: 'inp', value: c.name, maxlength: 40, style: 'max-width:220px' });
      thisBox.replaceChildren(
        h('div', { class: 'pc-this' },
          dot(!on ? 'off' : me.state === 'connected' ? 'on' : me.state === 'error' ? 'err' : 'wait'),
          h('b', null, c.name), h('span', { class: 'muted' }, ' · ' + (OS[me.os] || me.os)),
          h('span', { class: 'pc-st' + (me.state === 'error' && on ? ' err' : '') }, st),
          h('button', { class: 'btn' + (on ? '' : ' primary'), onclick: function () {
            save({ on: !on }).then(function () { S.pc = { on: !on }; startPoll(); setTimeout(load, 3500); });
          } }, on ? 'Disconnect' : 'Connect this computer')),
        h('div', { class: 'row', style: 'align-items:center;gap:8px;margin-top:8px' },
          h('span', { class: 'muted' }, 'Name'), name,
          h('button', { class: 'btn', onclick: function () { save({ name: name.value }).then(load); } }, 'Rename')));
    }

    function renderDevs() {
      if (!devs) { devBox.replaceChildren(h('div', { class: 'muted' }, 'Loading…')); return; }
      if (devs.error) { devBox.replaceChildren(h('div', { class: 'err' }, devs.error)); return; }
      var list = devs.devices || [];
      devBox.replaceChildren.apply(devBox, (list.length ? list.map(function (d) {
        return h('div', { class: 'pc-row' }, dot(d.online ? 'on' : 'off'),
          h('b', null, d.name || '?'), d.id === devs.self ? h('span', { class: 'pc-tag' }, 'this computer') : null,
          h('span', { class: 'muted' }, (OS[d.os] || d.os) + ' · ' + (d.online ? 'connected' : 'offline') + (d.since ? ' · since ' + ago(d.since) : '')),
          h('button', { class: 'btn danger sm', title: 'Forget this computer on the server', onclick: function () {
            SW.confirmDlg('Remove “' + (d.name || '?') + '”? Claude can no longer run commands there until it connects again.', 'Remove', true).then(function (ok) {
              if (ok) api('POST', 'pc/remove', { id: d.id }).then(function (x) { devs = x; render(); }).catch(fail);
            });
          } }, 'Remove'));
      }) : [h('div', { class: 'muted' }, 'No computer connected yet.')]).concat([
        S.web ? h('div', { class: 'muted pc-help' }, 'To connect a computer: open Simab Workspace on it → ⚙ → Computers → Connect this computer.') : null,
        h('button', { class: 'btn sm', style: 'margin-top:6px', onclick: load }, 'Refresh')]).filter(Boolean));
    }

    function renderBuilds() {
      if (!devs || devs.error) { buildBox.replaceChildren(); return; }
      var b = devs.builds || {};
      var names = Object.keys(b).sort();
      if (!names.length) { buildBox.replaceChildren(h('div', { class: 'muted' }, 'No project with a server folder.')); return; }
      buildBox.replaceChildren.apply(buildBox, [h('div', { class: 'muted pc-help' },
        'My computers: Claude runs builds, tests and installs of that project on a connected computer (onpc). '
        + 'When none is connected it stops and tells you — it never builds on the server instead.')].concat(names.map(function (n) {
        var sel = h('select', { class: 'inp', style: 'max-width:180px' }, h('option', { value: 'server' }, 'Server'), h('option', { value: 'computers' }, 'My computers'));
        sel.value = b[n];
        sel.addEventListener('change', function () {
          api('POST', 'pc/builds', { project: n, on: sel.value }).then(function (x) {
            devs = x; renderBuilds();
            toast(n + ': builds run on ' + (sel.value === 'server' ? 'the server' : 'your computers') + ' — new chats know it', 'ok');
          }).catch(function (e) { sel.value = b[n]; fail(e); });
        });
        var row = h('div', { class: 'pc-row' + (n === focusProject ? ' focus' : '') }, h('b', null, n), sel);
        if (n === focusProject) setTimeout(function () { if (row.scrollIntoView) row.scrollIntoView({ block: 'center' }); }, 50);
        return row;
      })));
    }

    function renderRules() {
      if (S.web || !me) return;
      var c = me.config;
      var mode = h('select', { class: 'inp', style: 'max-width:280px' }, MODES.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
      mode.value = c.mode;
      var out = h('select', { class: 'inp', style: 'max-width:280px' }, OUTPUTS.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
      out.value = c.output;
      var allow = h('textarea', { class: 'inp mono', rows: 3, placeholder: 'npm run *\nng build*' }, (c.allow || []).join('\n'));
      var deny = h('textarea', { class: 'inp mono', rows: 3 }, (c.deny || []).join('\n'));
      var projs = Object.keys((devs && devs.builds) || {}).sort();
      var all = h('input', { type: 'checkbox', checked: !(c.projects || []).length });
      var boxes = projs.map(function (n) { return { n: n, el: h('input', { type: 'checkbox', checked: (c.projects || []).indexOf(n) >= 0 }) }; });
      var plist = h('div', { class: 'pc-projs' + (all.checked ? ' hidden' : '') }, boxes.map(function (b) { return h('label', { class: 'check' }, b.el, h('span', null, b.n)); }));
      all.addEventListener('change', function () { plist.classList.toggle('hidden', all.checked); });
      var always = c.always || {};
      var rem = [];
      Object.keys(always).forEach(function (p) { always[p].forEach(function (cmd) { rem.push({ p: p, cmd: cmd }); }); });
      var remBox = h('div', null, rem.length ? rem.map(function (r) {
        return h('div', { class: 'pc-rem' }, h('span', { class: 'muted' }, r.p), h('code', null, r.cmd),
          h('button', { class: 'icon-btn', title: 'Forget', onclick: function () {
            var a = JSON.parse(JSON.stringify(always));
            a[r.p] = a[r.p].filter(function (x) { return x !== r.cmd; });
            save({ always: a }).then(load);
          } }, '✕'));
      }) : h('div', { class: 'muted' }, 'None yet — “Always allow” in a question adds one.'));
      ruleBox.replaceChildren(
        h('div', { class: 'field' }, h('label', null, 'When Claude wants to run a command here'), mode),
        h('div', { class: 'field' }, h('label', null, 'Always allowed (one per line, * = anything)'), allow),
        h('div', { class: 'field' }, h('label', null, 'Never allowed (wins over everything; checked on every part of a && b | c)'), deny),
        h('div', { class: 'field' }, h('label', null, 'Projects that may run here'), h('label', { class: 'check' }, all, h('span', null, 'All projects')), plist),
        h('div', { class: 'field' }, h('label', null, 'Remembered commands'), remBox),
        h('div', { class: 'field' }, h('label', null, 'Output'), out),
        h('div', { class: 'muted pc-help' }, 'A question waits 2 minutes, then the command is not run. It shows in every window of the app here.'),
        h('button', { class: 'btn primary', onclick: function () {
          save({ mode: mode.value, output: out.value, allow: lines(allow.value), deny: lines(deny.value),
            projects: all.checked ? [] : boxes.filter(function (b) { return b.el.checked; }).map(function (b) { return b.n; }) })
            .then(function () { toast('Saved', 'ok'); load(); });
        } }, 'Save rules'));
    }

    function renderRecent() {
      if (S.web || !me) return;
      var runs = (me.recent && me.recent.runs) || [];
      recentBox.replaceChildren.apply(recentBox, runs.length ? runs.map(function (r) {
        return h('div', { class: 'pc-run', title: 'Show the output', onclick: function () { showRun(r.id); } },
          h('span', { class: 'pc-state ' + r.state }, r.state === 'done' ? (r.code === 0 ? '✓' : '✗ ' + r.code) : r.state),
          h('span', { class: 'muted' }, r.project), h('code', null, r.cmd), h('span', { class: 'muted' }, ago(r.start, me.recent.now)));
      }) : [h('div', { class: 'muted' }, 'Nothing run here yet.')]);
    }

    function save(patch) { return api('POST', 'pc', patch).then(function (x) { me = Object.assign(x, { recent: me && me.recent }); S.pc = { on: !!x.config.on }; return x; }).catch(function (e) { fail(e); throw e; }); }

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

  // ------------------------------------------------------------------ polling (installed app only)
  function poll() {
    if (S.web || !S.pc || !S.pc.on) return Promise.resolve();
    return api('GET', 'pc/asks').then(function (x) {
      var now = x.now || Date.now() / 1000;
      var open = {};
      (x.asks || []).forEach(function (a) { open[a.id] = 1; if (!shown[a.id]) askModal(a); });
      Object.keys(shown).forEach(function (id) {
        var s = shown[id];
        if (!open[id]) { s.md.close(); delete shown[id]; return; }
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

  window.SWComputers = { open: open, poll: poll, _shown: shown, _live: function () { return live; } };
})();
