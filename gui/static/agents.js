/* Simab Workspace — other AI agents beside Claude (opencode, Gemini CLI, Codex, Qwen Code, your own).
   They run on the user's own computer (gui/acp_bridge.py speaks ACP to them) and show in the same chat
   panel as Claude. This file: Settings → AI (the list, Connect, default model, your own command) and the
   "New chat: <agent>" entries of the project menu / Ctrl+Shift+P. On the web: the agents of the
   connected computers (set up in the installed app there). */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail, toast = SW.toast, S = SW.S;
  var list = [];          // last GET agents
  var loaded = false;

  function load() {
    return api('GET', 'agents').then(function (r) { list = r.agents || []; loaded = true; return r; }).catch(function () { loaded = true; return { agents: list }; });
  }
  function ready() { return list.filter(function (a) { return a.on; }); }

  function open(project, a) {
    SW.openSession({ kind: 'chat', project: project || undefined, agent: a.id, agentName: a.name, dev: a.dev || undefined });
  }

  function openSettings() {
    try { localStorage.setItem('sw-set-tab', 'AI'); } catch (e) { /* private mode */ }
    SW.settingsDialog();
  }

  // project ⋯ menu / palette: one entry per agent that works; none yet → how to get one
  function items(project, prefix) {
    if (!loaded) load();
    var on = ready();
    if (!on.length) return [{ label: (prefix || '') + 'Other AI (opencode, …)…', k: 'connect', action: openSettings }];
    return on.map(function (a) {
      return { label: (prefix || '') + 'New chat: ' + a.name, k: S.web && a.devName ? '⌂ ' + a.devName : '', action: function () { open(project, a); } };
    });
  }

  function status(a) {
    if (a.on) return h('span', { class: 'ag-st ok' }, '● connected' + (a.models && a.models.length ? ' · ' + a.models.length + ' models' : ''));
    if (S.web) return h('span', { class: 'ag-st' }, '○');
    if (!a.installed) return h('span', { class: 'ag-st muted' }, 'not installed');
    return h('span', { class: 'ag-st' }, a.error ? '✗ not working' : '○ not connected');
  }

  // Settings → AI
  function rows() {
    var box = h('div', { class: 'ag-list' }, h('div', { class: 'muted' }, 'Loading…'));
    function paint() {
      box.replaceChildren();
      if (S.web) {
        box.append(h('div', { class: 'muted' }, 'AI tools of your connected computers. Set them up in the installed app there (Settings → AI); a chat needs that computer unlocked with your MFA code.'));
        if (!list.length) box.append(h('div', { class: 'muted' }, 'None yet.'));
        list.forEach(function (a) {
          box.append(h('div', { class: 'ag-row' }, h('b', null, a.name), h('span', { class: 'muted' }, ' ⌂ ' + (a.devName || '?')), status(a),
            h('button', { class: 'btn sm', onclick: function () { open('', a); } }, 'New chat')));
        });
        return;
      }
      box.append(h('div', { class: 'muted' }, 'AI tools on this computer, used in chats beside Claude. They work in the project’s folder here; Claude stays on the server.'));
      list.forEach(function (a) {
        var btns = h('span', { class: 'ag-btns' });
        var go = h('button', { class: 'btn sm' + (a.on ? '' : ' primary'), disabled: !a.installed && !a.custom }, a.on ? 'Check again' : 'Connect');
        go.addEventListener('click', function () {
          go.disabled = true; go.textContent = 'Connecting…';
          api('POST', 'agents', { op: 'connect', id: a.id }).then(function (r) {
            list = r.agents || list;
            var p = r.probe || {};
            if (p.ok) toast(a.name + ' connected', 'ok');
            else toast(a.name + ': ' + (p.error || 'not working'), 'err');
            paint();
          }).catch(function (e) { fail(e); paint(); });
        });
        btns.append(go);
        if (a.on) btns.append(h('button', { class: 'btn sm', onclick: function () { save({ op: 'off', id: a.id }); } }, 'Turn off'));
        if (a.custom) btns.append(h('button', { class: 'btn sm danger', onclick: function () { save({ op: 'remove', id: a.id }); } }, 'Remove'));
        var row = h('div', { class: 'ag-row' }, h('b', null, a.name), h('code', { class: 'ic', dir: 'ltr' }, a.command || ''), status(a), btns);
        var more = h('div', { class: 'ag-more' });
        if (a.on && a.models && a.models.length) {
          var sel = h('select', { class: 'inp mini', title: 'Default model of new chats', onchange: function () { save({ op: 'model', id: a.id, model: sel.value }); } },
            a.models.map(function (m) { return h('option', { value: m.value }, m.displayName || m.value); }));
          sel.value = a.model || '';
          more.append(h('label', { class: 'muted' }, 'Default model '), sel);
        }
        if (!a.on && a.error) more.append(h('div', { class: 'add-err', dir: 'auto' }, a.error));
        if (!a.on && !a.installed && !a.custom) more.append(h('div', { class: 'muted' }, 'Install it on this computer, sign in once (' + (a.login || a.name) + '), then Connect.'));
        box.append(h('div', { class: 'ag-item' }, row, more.childNodes.length ? more : null));
      });
      var name = h('input', { class: 'inp', placeholder: 'Name', style: 'flex:0 0 140px' });
      var cmd = h('input', { class: 'inp mono', placeholder: 'Command that speaks ACP, e.g. my-agent --acp', dir: 'ltr' });
      box.append(h('div', { class: 'sub', style: 'margin-top:10px' }, 'Your own'),
        h('div', { class: 'row' }, name, cmd, h('button', { class: 'btn', style: 'flex:none', onclick: function () {
          if (!name.value.trim() || !cmd.value.trim()) { toast('Give it a name and a command', 'warn'); return; }
          save({ op: 'add', name: name.value.trim(), cmd: cmd.value.trim() });
        } }, 'Add')));
    }
    function save(b) {
      api('POST', 'agents', b).then(function (r) { list = r.agents || list; paint(); }).catch(fail);
    }
    load().then(paint);
    return box;
  }

  window.SWAgents = { load: load, items: items, rows: rows, open: open, list: function () { return list; } };
  setTimeout(load, 1500);
})();
