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
  // a computer is up: the server says so, or it is THIS computer and its link is up right now
  function isOn(x) { return !!x && (x.online || (info && x.id === info.self && lastLink === 'connected')); }
  function online(d) { return ((d && d.devices) || []).filter(isOn); }
  var infoFresh = false;        // the first answer after start: until then the marks say "checking"
  var lastLink = '';
  function kids(el, list) { el.replaceChildren.apply(el, list.filter(Boolean)); }

  // where a project builds and whether that works right now
  // the project's files are on that computer (synced / Local only); unknown (older server data) → yes
  function hasProject(name, id) {
    var pr = (S.projects || []).filter(function (x) { return x.name === name; })[0];
    return !pr || !Array.isArray(pr.pcs) || pr.pcs.indexOf(id) >= 0;
  }
  // chatPc: the computer chosen in one chat (its ⚒ chip) — before the project's and the default
  // prefer: the computer the chat was opened on (default: this one) — no computer chosen = that one,
  // another connected one only while it is off (onpc does the same: SIMAB_PREFER)
  function buildState(name, d, chatPc, prefer) {
    d = d || info;
    if (!d || !d.builds || !d.builds[name]) return null;
    if (d.builds[name] === 'local') {               // Local only: the files live on one computer
      var pr = (S.projects || []).filter(function (x) { return x.name === name; })[0] || {};
      var owner = (d.pins || {})[name] || pr.localOnly;        // kept on the server: known on every computer
      var dev = ((d.devices) || []).filter(function (x) { return x.id === owner; })[0];
      var who = dev ? dev.name : 'its computer';
      return isOn(dev) ? { on: 'local', ok: true, dev: who, text: 'Lives on ' + who + ' — Claude works on it there' }
        : { on: 'local', ok: false, text: 'Lives on ' + who + ', which is not connected — Claude cannot work on it now' };
    }
    if (d.builds[name] !== 'computers') return { on: 'server', ok: true, text: 'Commands run on the server' };
    var pin = chatPc || (d.pins || {})[name] || d['default'];
    if (pin) {                                       // a computer chosen for this chat / project / all projects
      var why = chatPc ? 'chosen for this chat' : (d.pins || {})[name] ? 'chosen for this project' : 'your default computer';
      var pd = (d.devices || []).filter(function (x) { return x.id === pin; })[0];
      var pn = pd ? pd.name : 'the chosen computer';
      return isOn(pd) ? { on: 'computers', ok: true, dev: pn, text: 'Commands run on ' + pn + ' (' + why + '; files synced with the server)' }
        : { on: 'computers', ok: false, dev: pn, text: pn + ' (' + why + ') is not connected — Claude cannot run its commands' };
    }
    var me = prefer === undefined ? d.self : prefer;
    var md = (d.devices || []).filter(function (x) { return x.id === me; })[0];
    if (md) {                                        // a chat belongs to the computer it was opened on: never another one
      var mw = md.id === d.self ? 'this computer' : 'the computer this chat was opened on';
      return !isOn(md) ? { on: 'computers', ok: false, dev: md.name, text: md.name + ' (' + mw + ') is not connected — Claude cannot run its commands there' }
        : !hasProject(name, md.id) ? { on: 'computers', ok: false, dev: md.name, text: md.name + ' (' + mw + ') has not got this project — sync it there or pick another computer (⚒)' }
        : { on: 'computers', ok: true, dev: md.name, text: 'Commands run on ' + md.name + ' (' + mw + '; files synced with the server)' };
    }
    var on = online(d).filter(function (x) { return hasProject(name, x.id); });
    var one = on.filter(function (x) { return x.id === me; })[0] || on[0];
    return one ? { on: 'computers', ok: true, dev: one.name, text: 'Commands run on ' + one.name
        + (one.id === d.self ? ' (this computer' : one.id === me ? ' (the computer this chat was opened on' : me ? ' (the computer this chat was opened on is off' : ' (the first connected one')
        + (on.length > 1 ? '; if it goes off, the next connected one' : '') + '; files synced with the server)' }
      : online(d).length ? { on: 'computers', ok: false, text: 'None of your connected computers has this project — sync it to one (⋯ → Sync to this computer)' }
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
  function refreshInfo(fresh) {
    return api('GET', fresh || !infoFresh ? 'pc/devices/fresh' : 'pc/devices').then(function (x) {
      var first = !infoFresh;
      infoFresh = true;
      if (first) infoStr = '';            // draw the real marks now
      setInfo(x);
      return x;
    }).catch(function () { return info; });
  }

  // the sidebar: only projects that build on the computers get a mark (green: works, red: it will stop)
  function badge(p) {
    var st = buildState(p.name);
    if (!st || st.on === 'server') return null;
    if (!infoFresh) {                     // not asked yet since the start: never an old "off"
      return h('span', { class: 'gb pcb chk', title: 'checking where it runs…' }, st.on === 'local' ? '⌂ local …' : '⚒ PC …');
    }
    // two different things: Local only = the files live there; My computers = only builds run there
    var local = st.on === 'local';
    return h('span', { class: 'gb pcb ' + (local ? 'lo ' : 'bd ') + (st.ok ? 'ok' : 'bad'), title: st.text + ' — click for Computers',
      onclick: function (e) { e.stopPropagation(); open(p.name); } }, (local ? '⌂ ' : '⚒ ') + pcLabel(st, local) + (st.ok ? '' : ' off'));
  }
  // the computer's name on the sidebar mark ("2 PCs" when it may run on several)
  function pcLabel(st, local) {
    var n = st.dev ? st.dev.split(', ') : [];
    return n.length === 1 ? n[0] : n.length > 1 ? n.length + ' PCs' : local ? 'local' : 'PC';
  }
  function menuLabel(name) {
    var st = buildState(name);
    return !st ? '' : st.on === 'server' ? 'Server' : st.on === 'local' ? (st.ok ? 'Local only' : 'Local only · off') : st.ok ? 'My computers' : 'My computers · none on';
  }

  // ------------------------------------------------------------------ the dialog
  function open(focusProject) {
    var body = h('div', { class: 'pc-dlg' });
    var md = SW.openModal({ title: 'Computers', size: 'settings pcm', body: body,
      foot: [h('span', { class: 'left muted' }, 'Claude stays on the server · builds run where you choose'),
        h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    var me = null, devs = info, timer = null, checking = true;      // until the fresh answer: "checking…" 
    md.onclose = function () { clearInterval(timer); };

    function load() {
      var a = S.web ? Promise.resolve(null) : api('GET', 'pc').then(function (x) { me = Object.assign(x, { recent: me && me.recent }); });
      checking = true;
      renderDevs();
      var b = api('GET', 'pc/devices/fresh').then(function (x) { devs = x; setInfo(x); }).catch(function (e) { devs = { error: e.message || String(e) }; })
        .then(function () { checking = false; });
      var c = S.web ? Promise.resolve() : api('GET', 'pc/asks').then(function (x) { if (me) me.recent = x; });
      var d = S.web ? webPoll() : api('GET', 'pc/access').then(function (x) { acc = x; });
      return Promise.all([a, b, d]).then(function () { return c; }).then(render).catch(fail);
    }
    timer = setInterval(function () {
      if (S.web) return;
      Promise.all([api('GET', 'pc').then(function (x) { me = Object.assign(x, { recent: me && me.recent }); }),
        api('GET', 'pc/asks').then(function (x) { if (me) me.recent = x; }),
        api('GET', 'pc/access').then(function (x) { acc = x; })]).then(function () { renderThis(); renderRecent(); renderAccess(); }).catch(function () {});
    }, 3000);

    // in tabs like Settings (the same .set-* look): a rail on the left, a row on a phone
    var thisBox = h('div'), devBox = h('div'), buildBox = h('div'), ruleBox = h('div'), recentBox = h('div'), accBox = h('div'), acc = null;
    var tabs = [
      S.web ? null : ['This computer', '', thisBox],
      ['Your computers', 'connected to this server user', devBox],
      ['Commands run on', 'per project', buildBox],
      S.web ? null : ['Permission', 'what Claude may run on this computer', ruleBox],
      S.web ? null : ['Phone access', 'terminal and run commands from the web version, with this computer’s own MFA code', accBox],
      S.web ? null : ['Recent commands', 'run on this computer', recentBox]].filter(Boolean);
    var rail = h('div', { class: 'set-rail' }), panes = h('div', { class: 'set-pane' });
    function showTab(i) {
      Array.prototype.forEach.call(rail.children, function (b, j) { b.classList.toggle('active', j === i); });
      Array.prototype.forEach.call(panes.children, function (p, j) { p.classList.toggle('hidden', j !== i); });
      localStorage.setItem('sw-pc-tab', tabs[i][0]);
    }
    tabs.forEach(function (t, i) {
      rail.append(h('button', { class: 'set-tab', onclick: function () { showTab(i); } }, t[0]));
      panes.append(h('div', { class: 'set-group hidden' },
        h('div', { class: 'set-title' }, t[0], t[1] ? h('span', { class: 'muted' }, t[1]) : null), t[2]));
    });
    var names = tabs.map(function (t) { return t[0]; });
    showTab(focusProject ? names.indexOf('Commands run on') : Math.max(0, names.indexOf(localStorage.getItem('sw-pc-tab') || '')));
    kids(body, [h('div', { class: 'set-tabs' }, rail, panes)]);

    function render() { renderThis(); renderDevs(); renderBuilds(); renderRules(); renderRecent(); renderAccess(); }

    // who is in from the phone / web now (each with Disconnect) and what was done from there
    function renderAccess() {
      if (S.web || !acc) return;
      var off = function (id) { api('POST', 'pc/disconnect', { id: id }).then(function (x) { acc = x; renderAccess(); toast('Disconnected', 'ok'); }).catch(fail); };
      var rows = acc.unlocks.map(function (u) {
        var terms = acc.terms.filter(function (t) { return t.unlock === u.id; }), runs = acc.runs.filter(function (r) { return r.unlock === u.id; });
        return h('div', { class: 'pc-dev' }, dot('on'),
          h('div', { class: 'pc-dev-main' }, h('div', null, h('b', null, u.by || 'web')),
            h('small', { class: 'muted' }, 'unlocked until ' + hm(u.until)
              + terms.map(function (t) { return ' · terminal in ' + (t.project || 'home'); }).join('')
              + runs.map(function (r) { return ' · running ' + r.project + ': ' + r.cmd; }).join(''))),
          h('button', { class: 'btn sm danger', onclick: function () { off(u.id); } }, 'Disconnect'));
      });
      var devs = (acc.devices || []).map(function (d) {
        var inNow = acc.unlocks.some(function (u) { return u.dev === d.id; });
        return h('div', { class: 'pc-dev' }, dot(inNow ? 'on' : 'off'),
          h('div', { class: 'pc-dev-main' }, h('div', null, h('b', null, d.name)),
            h('small', { class: 'muted' }, inNow ? 'connected now' : d.last ? 'last used ' + ago(d.last, acc.now) : 'not used yet')),
          h('button', { class: 'btn sm danger', onclick: function () { mfaRemove(d); } }, 'Remove'));
      });
      kids(accBox, [
        h('div', { class: 'pc-note' }, acc.mfa
          ? 'On — each device below unlocks this computer for 1 hour with its own code: terminal, run commands, answers. Without it nothing from the server can do these (Claude in a chat included).'
          : 'Off — nothing from the server or the web version can open a terminal here, start a run command, or allow a command from the phone.'),
        h('div', { class: 'pc-actions', style: 'justify-content:flex-start' },
          h('button', { class: 'btn sm' + (acc.mfa ? '' : ' primary'), onclick: mfaSetup }, acc.mfa ? 'Add a device…' : 'Turn on…'),
          acc.mfa ? h('button', { class: 'btn sm', onclick: mfaOff }, 'Turn off') : null,
          rows.length > 1 ? h('button', { class: 'btn sm danger', onclick: function () { off('all'); } }, 'Disconnect all') : null),
        devs.length ? h('div', null, h('div', { class: 'muted' }, 'Devices'), devs) : null,
        h('div', { class: 'muted' }, 'Connected now'),
        rows.length ? h('div', null, rows) : h('div', { class: 'muted' }, 'Nobody is connected from the phone / web now.'),
        acc.log.length ? h('details', { class: 'pc-log' }, h('summary', null, 'Remote activity (' + acc.log.length + ')'),
          acc.log.slice(0, 40).map(function (x) {
            return h('div', { class: 'pc-run' }, h('small', { class: 'muted' }, ago(x.t, acc.now) + (x.by ? ' · ' + x.by : '')), ' ', x.text);
          })) : null]);
    }
    function mfaSetup() {
      SW.promptDlg('Name of this device (phone, tablet…)', (acc.devices || []).length ? 'Tablet' : 'Phone').then(function (name) {
        if (name && name.trim()) mfaScan(name.trim());
      });
    }
    function mfaScan(name) {
      api('POST', 'pc/mfa', { op: 'setup', name: name }).then(function (d) {
        var code = h('input', { class: 'inp mono', placeholder: '123456', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6' });
        var m = SW.openModal({ title: 'Phone access', size: 'sm', body: [
          h('p', null, '1. On ' + name + ', scan this code with the authenticator app (its own entry, not the sign-in one):'),
          h('img', { class: 'totp-qr', src: window.SWQR ? SWQR.dataUrl(d.uri) : '', alt: 'QR code' }),
          h('p', { class: 'muted' }, 'or ', h('a', { href: d.uri }, 'open it in the app'), ', or type the key:'),
          h('div', { class: 'mono totp-key' }, d.secret.replace(/(.{4})/g, '$1 ').trim()),
          h('p', null, '2. Enter the code it shows:'), code],
          foot: [h('button', { class: 'btn', onclick: function () { m.close(); } }, 'Cancel'),
            h('button', { class: 'btn primary', onclick: function () {
              api('POST', 'pc/mfa', { op: 'on', code: code.value.trim() }).then(function (x) { acc = x; m.close(); renderAccess(); toast(name + ' added', 'ok'); }).catch(fail);
            } }, 'Add')] });
      }).catch(fail);
    }
    function mfaOff() {
      SW.confirmDlg('Turn phone access off? Every device is removed and disconnected.', 'Turn off', true).then(function (ok) {
        if (ok) api('POST', 'pc/mfa', { op: 'off' }).then(function (x) { acc = x; renderAccess(); toast('Phone access is off', 'ok'); }).catch(fail);
      });
    }
    function mfaRemove(d) {
      SW.confirmDlg('Remove ' + d.name + '? It is disconnected and its code no longer works.', 'Remove', true).then(function (ok) {
        if (ok) api('POST', 'pc/mfa', { op: 'remove', id: d.id }).then(function (x) { acc = x; renderAccess(); toast(d.name + ' removed', 'ok'); }).catch(fail);
      });
    }

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
        return h('div', { class: 'pc-dev' }, dot(checking ? 'wait' : d.online ? 'on' : 'off'),
          h('div', { class: 'pc-dev-main' },
            h('div', null, h('b', null, d.name || '?'), d.id === devs.self ? h('span', { class: 'pc-tag' }, 'this computer') : null),
            h('small', { class: 'muted' }, (OS[d.os] || d.os) + ' · ' + (checking ? 'checking…' : d.online ? 'connected' : 'offline')
              + (d.since && !checking ? ' · last connected ' + ago(d.since) : ''))),
          S.web && d.online && !checking ? (unlockedUntil(d.id)
            ? h('button', { class: 'btn sm', title: 'Unlocked until ' + hm(unlockedUntil(d.id)), onclick: function () {
              api('POST', 'pc/lock', { dev: d.id }).then(function (r) { S.pcUnlocked = r.unlocked || {}; renderDevs(); }).catch(fail);
            } }, 'Lock')
            : h('button', { class: 'btn sm primary', title: 'Terminal, run commands and answers need its MFA code (1 hour)', onclick: function () {
              unlockDlg(d.id, d.name).then(renderDevs);
            } }, 'Unlock…')) : null,
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
      var defSel = null;
      if ((devs.devices || []).length > 1) {
        defSel = h('select', { class: 'inp pc-pin pc-default', title: 'Used by every My computers project without its own choice' },
          [h('option', { value: '' }, 'Any connected')].concat(devs.devices.map(function (x) { return h('option', { value: x.id }, x.name); })));
        defSel.value = devs['default'] || '';
        defSel.addEventListener('change', function () {
          api('POST', 'pc/default', { dev: defSel.value }).then(function (x) {
            devs = x; setInfo(x); renderBuilds();
            toast('Default computer: ' + (defSel.value ? defSel.options[defSel.selectedIndex].textContent : 'any connected'), 'ok');
          }).catch(fail);
        });
      }
      kids(buildBox, [h('div', { class: 'pc-note' }, 'My computers: the files stay on the server (synced with this computer) and Claude works on them there, '
        + 'but every command it runs (build, npm, git, tests, dev servers…) runs on a connected computer. If none is connected it stops and tells you — '
        + 'it never runs them on the server instead. Order: the chat\'s ⚒ chip, then the project, then the default.'),
        defSel ? h('div', { class: 'pc-build' }, h('div', { class: 'pc-build-main' }, h('b', null, 'Default computer'),
          h('small', { class: 'muted' }, 'for every project below without its own choice')), defSel) : null].concat(names.map(function (n) {
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
      var plist = h('div', { class: 'pc-projs' + (all.checked ? ' hidden' : '') }, boxes.map(function (b) { return h('label', { class: 'pc-proj' }, b.el, h('span', null, b.n)); }));
      all.addEventListener('change', function () { plist.classList.toggle('hidden', all.checked); });
      var always = c.always || {};
      var rem = [];
      Object.keys(always).forEach(function (p) { always[p].forEach(function (cmd) { rem.push({ p: p, cmd: cmd }); }); });
      var remBox = h('div', { class: 'pc-rems' }, rem.length ? rem.map(function (r) {
        return h('div', { class: 'pc-rem' }, h('span', { class: 'pc-tag' }, r.p), h('code', null, r.cmd),
          h('button', { class: 'icon-btn', title: 'Forget', onclick: function () {
            var a = JSON.parse(JSON.stringify(always));
            a[r.p] = a[r.p].filter(function (x) { return x !== r.cmd; });
            save({ always: a }).then(load);
          } }, '✕'));
      }) : h('div', { class: 'pc-empty' }, 'None yet — “Always allow this” in a question adds one.'));
      kids(ruleBox, [
        h('div', { class: 'field' }, h('label', null, 'When Claude wants to run a command here'), modeBox),
        h('div', { class: 'field' }, h('label', null, 'Output'), out),
        h('label', { class: 'check' }, autoOpen, h('span', null, 'Open dev servers in the browser by themselves',
          h('small', null, 'Otherwise a message offers to open them when Claude starts one (onpc --bg).'))),
        h('div', { class: 'pc-grid' },
          h('div', { class: 'field' }, h('label', null, 'Always allowed — one per line, * = anything'), allow),
          h('div', { class: 'field' }, h('label', null, 'Never allowed — wins over everything'), deny)),
        h('div', { class: 'field' }, h('label', null, 'Projects that may run here'),
          h('div', { class: 'pc-box' }, h('label', { class: 'check pc-all' }, all, h('span', null, 'All projects', h('small', null, 'Untick to choose them one by one.'))), plist)),
        h('div', { class: 'field' }, h('label', null, 'Remembered commands'), remBox),
        h('div', { class: 'pc-actions pc-save' },
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

  // ------------------------------------------------------------------ setup check (⚙ → Setup check…)
  var CLAUDE_INSTALL = 'curl -fsSL https://claude.ai/install.sh | bash';
  function setupCheck(auto) {
    var list = h('div', { class: 'setup-list' }, h('div', { class: 'muted' }, 'Checking…'));
    var md = null;
    function fixBtn(it) {
      var go = function (label, fn) { return h('button', { class: 'btn sm primary', onclick: fn }, label); };
      switch (it.fix) {
        case 'connection': case 'tools': return go(it.fix === 'tools' ? 'Install…' : 'Settings…', function () { md.close(); SW.settingsDialog(); });
        case 'install-claude': return go('Open server terminal', function () {
          md.close(); SW.openSession({ kind: 'shell', where: 'server' });
          if (navigator.clipboard) navigator.clipboard.writeText(CLAUDE_INSTALL).catch(function () {});
          toast('Paste and run: ' + CLAUDE_INSTALL + ' (copied)', 'ok', { sticky: true });
        });
        case 'login': return go('Sign in', function () { md.close(); SW.openSession({ kind: 'claude', where: 'server' }); toast('Sign in to Claude in this terminal (it asks the first time), then close it', 'ok', { timeout: 9000 }); });
        case 'computer': return go('Connect', function () { api('POST', 'pc', { on: true }).then(function () { S.pc = { on: true }; setTimeout(run, 4000); }).catch(fail); });
        case 'sshfs': return go('Prepare now', function (e) { e.currentTarget.disabled = true; e.currentTarget.textContent = 'Fetching…'; api('POST', 'setup-check/sshfs').then(run).catch(fail); });
        default: return null;
      }
    }
    function run() {
      return api('GET', 'setup-check').then(function (d) {
        kids(list, d.items.map(function (it) {
          return h('div', { class: 'setup-row ' + (it.ok ? 'ok' : it.optional ? 'opt' : 'bad') },
            h('span', { class: 'setup-ic' }, it.ok ? '✓' : it.optional ? '○' : '✗'),
            h('div', { class: 'setup-main' }, h('b', null, it.title), h('small', null, it.detail)),
            it.ok ? null : fixBtn(it));
        }));
        return d;
      }).catch(function (e) { kids(list, [h('div', { class: 'pc-note err' }, e.message || String(e))]); });
    }
    function open1() {
      md = SW.openModal({ title: 'Setup check', body: h('div', null, h('div', { class: 'pc-note', style: 'margin-top:0' },
        'Everything Simab Workspace needs, with a button to fix what is missing.'), list),
        foot: [h('button', { class: 'btn', onclick: function () { run(); } }, 'Check again'), h('button', { class: 'btn primary', onclick: function () { md.close(); } }, 'Done')] });
    }
    if (!auto) { open1(); return run(); }
    // at start: only when something needed is missing (once per device until it is fine)
    return api('GET', 'setup-check').then(function (d) {
      var bad = d.items.some(function (it) { return !it.ok && !it.optional; });
      if (bad) { open1(); run(); }
      return !bad;
    }).catch(function () { return false; });
  }
  // once at start, until everything needed is there (then never again on this device)
  setTimeout(function () { if (!localStorage.getItem('sw-setup-ok')) setupCheck(true).then(function (fine) { if (fine) localStorage.setItem('sw-setup-ok', '1'); }); }, 6000);

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
    var md = SW.openModal({ sticky: true,  title: 'Run on this computer?', size: 'sm', body: h('div', { class: 'pc-ask' },
      h('div', { class: 'muted' }, 'Claude (project ', h('b', null, a.project), ') wants to run:'),
      h('pre', { class: 'pc-cmd' }, a.cmd), a.warn ? h('div', { class: 'pc-warn' }, '⚠ ' + a.warn) : null, left),
      foot: [h('button', { class: 'btn danger', onclick: function () { answer('deny'); } }, 'Deny'),
        h('div', { class: 'pc-ask-btns' },
          a.warn ? null : h('button', { class: 'btn', title: 'Never ask again for this exact command in ' + a.project, onclick: function () { answer('always'); } }, 'Always allow this'),
          a.warn ? null : h('button', { class: 'btn', title: 'Everything in ' + a.project + ' runs without asking for 1 hour', onclick: function () { answer('hour'); } }, 'Allow all 1 hour'),
          h('button', { class: 'btn primary', autofocus: true, onclick: function () { answer('once'); } }, 'Allow once'))] });
    md.onclose = function () { delete shown[a.id]; };
    shown[a.id] = { a: a, left: [left], close: function () { md.close(); } };
    try {
      if (document.hidden && window.Notification && Notification.permission === 'granted') new Notification('Claude wants to run a command', { body: a.project + ': ' + a.cmd, icon: 'icon.svg' });
    } catch (e) { /* ignore */ }
  }

  // the installed app answers its own questions; the web version through the server (PC_BRIDGE_PY)
  // (allowing from the web needs that computer's MFA unlock; deny never does)
  function sendAnswer(a, v) {
    if (!S.web) return api('POST', 'pc/answer', { id: a.id, answer: v }).catch(function () { /* answered elsewhere / gone */ });
    var go = function () { return api('POST', 'pc/remote-answer', { dev: a.dev, id: a.id, answer: v }).catch(function () { /* answered elsewhere / gone */ }); };
    if (v === 'deny' || unlockedUntil(a.dev)) return go();
    return unlockDlg(a.dev, a.devName).then(function (ok) { return ok ? go() : null; });
  }

  // web version: a computer opens its terminal, run commands and "allow" answers only after ITS OWN
  // MFA code (Computers → Phone access on that computer); the unlock lasts 1 hour
  function hm(t) { return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  function unlockedUntil(dev) { var u = (S.pcUnlocked || {})[dev]; return u && u * 1000 > Date.now() ? u : 0; }
  function unlockDlg(dev, name) {
    return SW.promptDlg('Code of “' + (name || 'your computer') + '” from your authenticator app — unlocks it for 1 hour', '').then(function (c) {
      if (!c) return false;
      return api('POST', 'pc/unlock', { dev: dev, code: c.trim(), by: navigator.userAgent || '' }).then(function (r) {
        S.pcUnlocked = r.unlocked || {};
        toast('Unlocked for 1 hour', 'ok');
        return true;
      }).catch(function (e) { fail(e); return false; });
    });
  }
  // → Promise<bool>: some connected computer is unlocked (asks the code of the first one if none is)
  function ensureUnlock() {
    if (Object.keys(S.pcUnlocked || {}).some(unlockedUntil)) return Promise.resolve(true);
    var on = online(info).filter(function (d) { return d.id !== (info && info.self); });
    if (!on.length) { toast('None of your computers is connected', 'warn'); return Promise.resolve(false); }
    return unlockDlg(on[0].id, on[0].name);
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
        { sticky: true, onclick: function () { if (shown[a.id] || !toasted[a.id]) return; askModal(a); } });
      return;
    }
    var views = [];
    // a deny-list command (a.warn) is always asked, even in auto
    if (!a.warn && mode === 'chat' && tabs.some(function (t) { return AUTO_CHAT[t.mode]; })) {      // the chat is in auto: no question
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
        if (!a.warn && (v === 'all' || (v === 'chat' && tabs.some(function (t) { return AUTO_CHAT[t.mode]; })))) answer('once');
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
      var st = p ? buildState(p.name, null, t.info.spec.pc, t.info.spec.prefer || (S.web ? '' : undefined)) : null;
      var name = p ? p.name : '';
      // the badge at the top right of the chat: where its commands run; click → change it
      if (!st || st.on === 'server') {
        t.setWhere('▣ server', 'Commands run on the server\nclick: change', 'srv', function (ev) { whereMenu(t, name, ev.currentTarget, st); });
        return;
      }
      if (!infoFresh) { t.setWhere(st.on === 'local' ? '⌂ …' : '⚒ …', 'checking…', ''); return; }
      t.setWhere((st.on === 'local' ? '⌂ ' : '⚒ ') + (st.dev || 'PC') + (st.ok ? '' : ' · off') + ' ▾', st.text + '\nclick: change',
        st.ok ? (st.on === 'local' ? 'lo' : 'bd') : 'bad', function (ev) { whereMenu(t, name, ev.currentTarget, st); });
    });
  }

  function whereMenu(t, name, el, st) {
    if (st && st.on === 'computers') { chatPcMenu(t, name, el); return; }
    var r = el.getBoundingClientRect();
    SW.showMenu(r.left, r.bottom + 2, [{ header: st && st.on === 'local' ? 'Lives on ' + st.dev : 'Commands run on the server' },
      { label: name ? 'Run on my computers… (Computers settings)' : 'Computers settings…', action: function () { open(name || undefined); } }]);
  }

  // the ⚒ chip of a My computers chat: which computer runs this chat's commands
  function chatPcMenu(t, name, el) {
    var cur = t.info.spec.pc || '';
    var base = buildState(name, info, '', t.info.spec.prefer || (S.web ? '' : undefined));
    var items = [{ header: 'Commands of this chat run on' },
      { label: (cur ? '   ' : '✓ ') + 'Project setting (' + (base && base.dev ? base.dev : 'any connected') + ')', action: function () { pick(''); } }];
    // only connected computers that have this project's files (the one chosen stays, marked)
    var can = (info.devices || []).filter(function (x) { return (isOn(x) && hasProject(name, x.id)) || x.id === cur; });
    can.forEach(function (x) {
      items.push({ label: (cur === x.id ? '✓ ' : '   ') + x.name + (!isOn(x) ? ' (not connected)' : hasProject(name, x.id) ? '' : ' (project not synced there)'),
        action: function () { pick(x.id); } });
    });
    if (!can.length) items.push({ label: 'No connected computer has this project', disabled: true });
    items.push('-', { label: 'Project setting… (Computers)', action: function () { open(name); } });
    function pick(id) {
      if (id === cur) return;
      t.send({ t: 'pc', pc: id });
      if (id) t.info.spec.pc = id; else delete t.info.spec.pc;
      decorate();
    }
    var r = el.getBoundingClientRect();
    SW.showMenu(r.left, r.bottom + 2, items);
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
      guestAsks(x.guests || [], function (id, ans) { return api('POST', 'pc/guest', { id: id, answer: ans }); });
      devServers(x.bg || []);
      var bk = JSON.stringify((x.bg || []).map(function (b) { return [b.name, b.state, b.urls || b.url, b.start]; }));
      S.localBg = x.bg || [];
      if (bk !== localKey) { localKey = bk; if (SW.renderProjects) SW.renderProjects(); }
      devUp(x.bg || [], x.autoOpen);
      linkWatch(x.link);
    }).catch(function () {});
  }
  // ---- Share on your domain, "Ask me": someone opens a link without the sign-in → this window asks
  var guestShown = {};
  var GUEST = [['deny', 'Deny'], ['hour', 'Allow 1 hour'], ['day', 'Allow 24 hours'], ['always', 'Always']];
  function guestAsks(list, send) {
    var open = {};
    (list || []).forEach(function (g) {
      open[g.id] = 1;
      if (guestShown[g.id]) return;
      var text = g.text || ('Someone wants to open ' + g.t + ' — ' + g.label + ' · ' + g.ip);
      var md = SW.openModal({ title: 'Someone wants to open your link', size: 'sm', sticky: true, body: [h('p', null, text),
        h('p', { class: 'muted' }, h('small', null, 'Not allowed if nobody answers in 5 minutes.'))],
        foot: GUEST.map(function (b) {
          return h('button', { class: 'btn' + (b[0] === 'deny' ? '' : ' primary'), onclick: function () {
            md.close();
            send(g.id, b[0]).then(function () { toast(b[0] === 'deny' ? 'Not let in' : 'Let in', 'ok'); }).catch(fail);
          } }, b[1]);
        }) });
      guestShown[g.id] = md;
      if (window.SWSound && window.SWSound.play) { try { window.SWSound.play('question', 'link'); } catch (e) { /* no sound */ } }
    });
    Object.keys(guestShown).forEach(function (id) {
      if (!open[id]) { try { guestShown[id].close(); } catch (e) { /* closed */ } delete guestShown[id]; }
    });
  }
  // web / phone: someone opens a shared link with "Ask me" — answered here, the computer need not be near
  function guestPoll() {
    if (!S.web || document.hidden) return Promise.resolve();
    return api('GET', 'account/share/asks').then(function (d) {
      guestAsks(d.asks, function (id, ans) { return api('POST', 'account/share/answer', { id: id, answer: ans }); });
    }).catch(function () {});
  }
  setInterval(guestPoll, 3000);      // S.web is known only after boot: checked on every tick

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
  // Local only: how many file requests the server makes (each one is a round trip) and where to
  function fsLine(f, ops) {
    if (!f || !f.n) return '';
    return '\nFile requests: ' + (ops || 0) + '/s now, ' + f.n + ' in all (' + f.ms + ' ms each here)'
      + (f.hidden ? ', ' + f.hidden + ' to hidden heavy folders' : '')
      + (f.busy && f.busy.length ? '\nBusiest: ' + f.busy.map(function (b) { return b[0] + ' ' + b[1]; }).join(', ') : '');
  }
  function netBox(n) {
    // each Local only chat paints its own connection badge from this (chat.js paintLink)
    try { document.dispatchEvent(new CustomEvent('sw-pc-net', { detail: n })); } catch (e) { /* old browser */ }
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
      : 'Connection to the server: ' + (n.rtt == null ? 'no answer' : n.rtt + ' ms round trip') + '\nNow: ↓ ' + rate(n.down) + ' from the server, ↑ ' + rate(n.up) + ' to it' + fsLine(n.files, n.ops);
  }
  // a real speed test over ssh (installed app): click the bars, or ⚙ → Speed test…
  function speedTest() {
    var res = h('div', { class: 'speed-res' }, h('div', { class: 'muted' }, 'Press Start: about 8 MB down and 4 MB up between this computer and the server.'));
    var go = h('button', { class: 'btn primary', onclick: function () {
      go.disabled = true; go.textContent = 'Testing…';
      kids(res, [h('div', { class: 'muted' }, 'Measuring…')]);
      api('POST', 'pc/speed').then(function (r) {
        var tile = function (label, v, unit) { return h('div', { class: 'speed-tile' }, h('small', null, label), h('b', null, v == null ? '—' : String(v)), h('span', { class: 'muted' }, unit)); };
        kids(res, [h('div', { class: 'speed-tiles' }, tile('↓ Download', r.down, 'Mbit/s'), tile('↑ Upload', r.up, 'Mbit/s'), tile('Round trip', r.rtt, 'ms')),
          h('div', { class: 'muted', style: 'font-size:11.5px;margin-top:8px' }, (r.down || 0) < 5 || (r.rtt || 0) > 250
            ? 'Slow: Local only projects will feel slow (each file is read over this line).' : 'Good enough for Local only projects.')]);
      }).catch(function (e) { kids(res, [h('div', { class: 'pc-note err' }, e.message || String(e))]); })
        .then(function () { go.disabled = false; go.textContent = 'Test again'; });
    } }, 'Start');
    var md = SW.openModal({ title: 'Speed test — this computer ↔ server', size: 'sm', body: res,
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close'), go] });
    return md;
  }
  var netEl = document.getElementById('pc-net');
  if (netEl) { netEl.style.cursor = 'pointer'; netEl.addEventListener('click', function () { speedTest(); }); }

  // this computer's address on the Wi-Fi / LAN (installed app): a phone on the same network opens IP:PORT
  var lanEl = document.getElementById('lan-ip');
  function pollLan() {
    if (!lanEl || S.web || document.hidden) return;
    api('GET', 'lan-ip').then(function (d) {
      var ips = (d && d.ips) || [];
      lanEl.classList.toggle('hidden', !ips.length);
      lanEl.textContent = ips[0] || '';
      lanEl.title = 'This computer on your Wi-Fi / local network' + (ips.length > 1 ? ' (also ' + ips.slice(1).join(', ') + ')' : '')
        + '\nOn a phone on the same network open http://' + (ips[0] || '') + ':PORT'
        + '\n(the app must listen on 0.0.0.0, not only 127.0.0.1)\nClick to copy';
    }).catch(function () { /* next time */ });
  }
  if (lanEl) {
    lanEl.addEventListener('click', function () {
      var t = lanEl.textContent;
      if (t && navigator.clipboard) navigator.clipboard.writeText(t).then(function () { toast('Copied ' + t); }, function () {});
    });
    setTimeout(pollLan, 2000);
    setInterval(pollLan, 60000);
  }

  var webNetAt = 0;
  function pollNet() {
    var lo = (S.projects || []).some(function (p) { return p.localOnly; });
    if (S.web) {                     // web / phone: the chats' lines as the server measured them (every 3 s)
      if (!lo || Date.now() - webNetAt < 2900) return Promise.resolve();
      webNetAt = Date.now();
      return api('GET', 'pc/net').then(netBox).catch(function () { /* next time */ });
    }
    if (!S.pc || !S.pc.on || !lo) { netBox(null); return Promise.resolve(); }
    return api('GET', 'pc/net').then(netBox).catch(function () { netBox({ state: 'off', bars: 0, down: 0, up: 0 }); });
  }
  setInterval(function () { if (!document.hidden) pollNet(); }, 1000);

  // web version: run commands of a computer (its "loc" ones) from the phone, with live output and stop
  var remoteKey = '';
  function remoteRunning(p, c) {
    var proj = c.devProject || p.name;     // the name that computer gives the project
    return (S.remoteBg || []).filter(function (x) { return x.name === 'run-' + c.id && x.project === proj && (!c.fromDev || x.dev === c.fromDev) && x.state === 'running'; })[0];
  }
  function remoteRun(p, c) {
    var x = remoteRunning(p, c);
    if (x) return remoteLog(x.dev, x.name, c.name, x.devName);
    return ensureUnlock().then(function (ok) { return ok && api('POST', 'pc/remote-run', { project: c.devProject || p.name, id: c.id, dev: c.fromDev || '' }); }).then(function (r) {
      if (!r) return;
      toast(c.name + ' started on your computer', 'ok');
      webPoll();
      return remoteLog(r.dev, r.name, c.name, '');
    }).catch(fail);
  }
  function remoteLog(dev, name, label, devName) {
    var pre = h('pre', { class: 'pc-out', style: 'min-height:200px' }, 'loading…');
    var head = h('span', { class: 'muted' });
    var stop = h('button', { class: 'btn danger', onclick: function () {
      api('POST', 'pc/remote-stop', { dev: dev, name: name }).then(function () { toast(label + ' stopped', 'ok'); load(); webPoll(); }).catch(fail);
    } }, '■ Stop');
    var md = SW.openModal({ title: '▶ ' + label + (devName ? ' · on ' + devName : ' · on your computer'), size: 'wide', body: h('div', { class: 'rr' }, head, pre),
      foot: [stop, h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    function load() {
      return api('POST', 'pc/remote-log', { dev: dev, name: name }).then(function (d) {
        head.textContent = d.state === 'running' ? 'running' + (d.url ? ' · ' + d.url + ' (on that computer)' : '') : d.state + (d.code != null ? ' · exit ' + d.code : '');
        stop.disabled = d.state !== 'running';
        var bottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
        pre.textContent = d.out || '(no output yet)';
        if (bottom) pre.scrollTop = pre.scrollHeight;
      }).catch(function (e) { head.textContent = e.message || String(e); });
    }
    var t = setInterval(load, 1500);
    md.onclose = function () { clearInterval(t); };
    load();
    return md;
  }

  // web version: the questions of the user's computers, in the chat that asks (or one toast)
  function webPoll() {
    if (!S.web) return Promise.resolve();
    return api('GET', 'pc/remote-asks').then(function (x) {
      var now = x.now || Date.now() / 1000, openIds = {};
      var key = JSON.stringify([x.online, x.on, (x.bg || []).map(function (b) { return [b.name, b.state, b.dev, b.urls || b.url]; })]);
      S.remoteOnline = x.online || 0;
      S.remoteOn = x.on || [];
      S.remoteBg = x.bg || [];
      S.pcUnlocked = x.unlocked || {};
      if (key !== remoteKey) { remoteKey = key; if (SW.renderProjects) SW.renderProjects(); }
      (x.asks || []).forEach(function (a) { openIds[a.id] = 1; if (!shown[a.id]) ask(a, ''); });
      Object.keys(shown).forEach(function (id) {
        var s = shown[id];
        if (!openIds[id]) { s.close('Answered — see the chat'); delete shown[id]; return; }
        s.left.forEach(function (el) { el.textContent = 'on ' + (s.a.devName || 'your computer') + ' · not run if nobody answers in ' + Math.max(0, Math.round(s.a.until - now)) + 's'; });
      });
    }).catch(function () {});
  }
  setInterval(function () { if (S.web && !document.hidden) webPoll(); }, 3000);
  // web / phone: a chat's commands on my computers, live in their Bash card (onpc keeps a copy on the server)
  var webRunsDone = {}, webRunsSeen = {};
  function webRuns() {
    (S.tabs || []).forEach(function (t) {
      if (!t.pcRunView || t.closed || !t.busy || !t.info || !t.info.spec || !t.info.spec.relay) return;
      var p = (S.projects || []).filter(function (x) { return x.name === t.info.spec.project; })[0];
      if (!p || (p.buildsOn !== 'local' && p.buildsOn !== 'computers' && !p.localOnly)) return;
      api('GET', 'pc/webruns/' + t.info.spec.relay).then(function (d) {
        (d.runs || []).forEach(function (r) {
          if (webRunsDone[r.id]) return;
          if (r.state === 'done') {
            webRunsDone[r.id] = 1;
            if (!webRunsSeen[r.id]) return;          // finished before we saw it run: not this card's
          } else webRunsSeen[r.id] = 1;
          t.pcRunView(r, r.out || '', r.dev || 'your computer');
        });
      }).catch(function () { /* next time */ });
    });
  }
  setInterval(function () { if (S.web && !document.hidden) webRuns(); }, 2000);

  // dev servers (onpc --bg) in the chats of their project
  var DEV_ACT = {
    stop: function (x) { api('POST', 'pc/bgstop', { name: x.name }).then(poll).catch(fail); },
    logs: function (x) {
      api('GET', 'pc/bglog/' + encodeURIComponent(x.name)).then(function (d) {
        SW.openModal({ title: d.name + ' · ' + d.state + (d.url ? ' · ' + d.url : ''), size: 'wide', body: h('pre', { class: 'pc-out' }, d.out || '(no output yet)') });
      }).catch(fail);
    } };
  var dismissed = {}, localKey = '';
  // ---- the sidebar: every dev server of a project with its ports, ▤ live output, ■ stop
  function portOf(u) { var m = /:\/\/[^/]*:(\d+)/.exec(u || ''); return m ? m[1] : /^https/.test(u || '') ? '443' : '80'; }
  function devLog(x) {
    if (S.web) return remoteLog(x.dev, x.name, x.name, x.devName);
    var pre = h('pre', { class: 'pc-out', style: 'min-height:200px' }, 'loading…');
    var head = h('span', { class: 'muted' });
    var stop = h('button', { class: 'btn danger', onclick: function () {
      api('POST', 'pc/bgstop', { name: x.name }).then(function () { toast(x.name + ' stopped', 'ok'); load(); poll(); }).catch(fail);
    } }, '■ Stop');
    var md = SW.openModal({ title: '▶ ' + x.name + ' · ' + x.project, size: 'wide', body: h('div', { class: 'rr' }, h('code', null, x.cmd || ''), head, pre),
      foot: [stop, h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Close')] });
    function load() {
      return api('GET', 'pc/bglog/' + encodeURIComponent(x.name)).then(function (d) {
        head.textContent = d.state === 'running' ? 'running' + (d.url ? ' · ' + d.url : '') : d.state;
        stop.disabled = d.state !== 'running';
        var bottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
        pre.textContent = d.out || '(no output yet)';
        if (bottom) pre.scrollTop = pre.scrollHeight;
      }).catch(function (e) { head.textContent = e.message || String(e); });
    }
    var t = setInterval(load, 1500);
    md.onclose = function () { clearInterval(t); };
    load();
    return md;
  }
  function devStop(x) {
    if (x.state !== 'running') { dismissed[x.name + x.start] = 1; localKey = remoteKey = ''; if (SW.renderProjects) SW.renderProjects(); return; }
    var go = S.web ? ensureUnlock().then(function (ok) { return ok && api('POST', 'pc/remote-stop', { dev: x.dev, name: x.name }); })
      : api('POST', 'pc/bgstop', { name: x.name });
    go.then(function (r) { if (r) toast(x.name + ' stopped', 'ok'); return S.web ? webPoll() : poll(); }).catch(fail);
  }
  function devLines(p) {
    var cmdIds = {};
    if (S.web) (p.commands || []).forEach(function (c) { cmdIds['run-' + c.id] = 1; });     // shown as their run command
    return ((S.web ? S.remoteBg : S.localBg) || []).filter(function (x) {
      return x.project === p.name && !cmdIds[x.name] && (x.state === 'running' || (x.state === 'exited' && !dismissed[x.name + x.start]));
    }).map(function (x) {
      var on = x.state === 'running', urls = x.urls && x.urls.length ? x.urls : x.url ? [x.url] : [];
      var ports = urls.map(function (u) {
        return S.web ? h('span', { class: 'dev-port', title: u + ' on ' + (x.devName || 'your computer') }, ':' + portOf(u))
          : h('a', { class: 'dev-port', href: u, target: '_blank', rel: 'noopener', title: 'Open ' + u, onclick: function (e) { e.stopPropagation(); } }, ':' + portOf(u));
      });
      return h('div', { class: 'cmd dev-line' + (on ? ' running' : ' ended'), title: x.cmd + (x.devName ? '\non ' + x.devName : '') + '\nclick: live output',
        onclick: function () { devLog(x); } },
        h('span', { class: 'ic dev-dot' + (on ? ' on' : '') }, '●'), h('span', { class: 'nm' }, x.name),
        on ? (ports.length ? ports : h('span', { class: 'tag' }, 'no port yet')) : h('span', { class: 'res err', title: 'stopped by itself' }, '✗' + (x.code != null ? x.code : '')),
        h('button', { class: 'icon-btn dev-stop', title: on ? 'Stop ' + x.name : 'Hide', onclick: function (e) { e.stopPropagation(); devStop(x); } }, on ? '■' : '✕'));
    });
  }
  function devBadge(p) {
    var on = ((S.web ? S.remoteBg : S.localBg) || []).filter(function (x) { return x.project === p.name && x.state === 'running'; });
    if (!on.length) return null;
    var ports = [];
    on.forEach(function (x) { (x.urls && x.urls.length ? x.urls : x.url ? [x.url] : []).forEach(function (u) { ports.push(':' + portOf(u)); }); });
    return h('span', { class: 'gb dev', title: on.length + ' running: ' + on.map(function (x) { return x.name; }).join(', ') + (ports.length ? ' · ' + ports.join(' ') : '') }, '● ' + (ports.length ? ports.join(' ') : on.length));
  }
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
  var warned = {};
  function linkWatch(state) {
    if (!state) return;
    var was = lastLink;
    lastLink = state;
    if (was !== state && (was === 'connected' || state === 'connected')) refreshInfo(true);     // ask again at once
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
  window.SWComputers = { guestPoll: guestPoll, _fresh: function (v) { infoFresh = v; }, fresh: function () { return infoFresh; }, open: open, poll: poll, webPoll: webPoll, remoteRun: remoteRun, ensureUnlock: ensureUnlock, remoteRunning: remoteRunning, devLines: devLines, devLog: devLog, portOf: portOf, devBadge: devBadge, setupCheck: setupCheck, speedTest: speedTest, _info: function () { return info; }, decorate: decorate, pollNet: pollNet, bashWhere: bashWhere, _webRuns: webRuns, _linkWatch: linkWatch, badge: badge, menuLabel: menuLabel, buildState: buildState, refresh: refreshInfo,
    _setInfo: setInfo, _shown: shown, _live: function () { return live; } };
})();
