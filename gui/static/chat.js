/* simabworkspace — Claude chat panel (like the Claude Code VS Code extension).
   Talks to ChatSession in server.py: Claude runs on the server in stream-json mode. */
'use strict';
(function () {
  var SW = window.SW;
  var h = SW.h, api = SW.api, toast = SW.toast, fail = SW.fail, S = SW.S, faNum = SW.faNum;

  var MODES = [
    ['default', 'ask', 'Asks before editing files or running commands'],
    ['acceptEdits', 'auto-edit', 'Edits files without asking; still asks for commands'],
    ['plan', 'plan', 'Reads and plans only; changes nothing'],
    ['auto', 'auto', 'Does low-risk things itself, asks for the rest']
  ];
  var MODELS = [['', 'default model'], ['opus', 'opus'], ['sonnet', 'sonnet'], ['haiku', 'haiku']];
  var TOOL_FA = {};   // tool names are shown as-is (Bash, Read, Edit…)

  // ------------------------------------------------------------------ markdown (DOM, no innerHTML)
  function inline(text) {
    var out = [];
    var re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))|(https?:\/\/[^\s)<]+)|(\*[^*\s][^*]*\*)/g;
    var last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1]) out.push(h('code', { class: 'ic', dir: 'ltr' }, m[1].slice(1, -1)));
      else if (m[2]) out.push(h('strong', null, inline(m[2].slice(2, -2))));
      else if (m[3]) out.push(h('a', { href: m[4], target: '_blank', rel: 'noopener' }, m[3].slice(1, m[3].indexOf(']'))));
      else if (m[5]) out.push(h('a', { href: m[5], target: '_blank', rel: 'noopener', dir: 'ltr' }, m[5]));
      else if (m[6]) out.push(h('em', null, m[6].slice(1, -1)));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function codeBlock(code, lang) {
    var pre = h('pre', { dir: 'ltr' }, h('code', null, code));
    return h('div', { class: 'codeblock' },
      h('div', { class: 'cb-head' }, h('span', null, lang || ''),
        h('button', { class: 'cb-copy', onclick: function (e) {
          navigator.clipboard.writeText(code).then(function () { e.target.textContent = 'copied'; setTimeout(function () { e.target.textContent = 'copy'; }, 1200); }).catch(fail);
        } }, 'copy')), pre);
  }

  function markdown(src) {
    var root = h('div', { class: 'md' });
    var lines = String(src || '').replace(/\r/g, '').split('\n');
    var i = 0, para = [];
    function flush() {
      if (para.length) { root.append(h('p', { dir: 'auto' }, inline(para.join('\n')))); para = []; }
    }
    while (i < lines.length) {
      var l = lines[i];
      var fence = l.match(/^\s*(```+|~~~+)\s*([\w+#.-]*)/);
      if (fence) {
        flush();
        var code = [], end = fence[1];
        i++;
        while (i < lines.length && lines[i].trim().indexOf(end) !== 0) { code.push(lines[i]); i++; }
        i++;
        root.append(codeBlock(code.join('\n'), fence[2]));
        continue;
      }
      var hd = l.match(/^(#{1,6})\s+(.*)$/);
      if (hd) { flush(); root.append(h('h' + Math.min(6, hd[1].length + 2), { dir: 'auto' }, inline(hd[2]))); i++; continue; }
      if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(l)) { flush(); root.append(h('hr')); i++; continue; }
      if (/^\s*>/.test(l)) {
        flush();
        var q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        root.append(h('blockquote', { dir: 'auto' }, markdown(q.join('\n'))));
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flush();
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i++; }
        var cells = function (r) { return r.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); }); };
        var head = cells(rows[0]);
        var table = h('table', { dir: 'auto' }, h('thead', null, h('tr', null, head.map(function (c) { return h('th', null, inline(c)); }))),
          h('tbody', null, rows.slice(2).map(function (r) { return h('tr', null, cells(r).map(function (c) { return h('td', null, inline(c)); })); })));
        root.append(h('div', { class: 'tablewrap' }, table));
        continue;
      }
      var li = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        flush();
        var ordered = /\d/.test(li[2]);
        var list = h(ordered ? 'ol' : 'ul', { dir: 'auto' });
        while (i < lines.length) {
          var mm = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          if (!mm) {
            if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && list.lastChild) { list.lastChild.append(h('br'), inline(lines[i].trim())); i++; continue; }
            break;
          }
          var txt = mm[3], box = txt.match(/^\[([ xX])\]\s+(.*)$/);
          var item = box ? h('li', { class: 'task' }, h('span', { class: 'tick' }, box[1] === ' ' ? '☐' : '☑'), inline(box[2])) : h('li', null, inline(txt));
          if (mm[1].length >= 2) item.classList.add('nested');
          list.append(item);
          i++;
        }
        root.append(list);
        continue;
      }
      if (!l.trim()) { flush(); i++; continue; }
      para.push(l);
      i++;
    }
    flush();
    return root;
  }

  function stripReminders(t) {
    return String(t || '').replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim();
  }

  // ------------------------------------------------------------------ helpers for tool cards
  function relPath(tab, p) {
    if (!p) return '';
    var root = tab.root;
    if (root && p.indexOf(root + '/') === 0) return p.slice(root.length + 1);
    return p;
  }

  function diffView(oldS, newS) {
    var a = String(oldS || '').split('\n'), b = String(newS || '').split('\n');
    // trim common prefix/suffix lines so only the change shows
    var pre = 0;
    while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
    var suf = 0;
    while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;
    var rows = [];
    var ctxFrom = Math.max(0, pre - 2);
    for (var i = ctxFrom; i < pre; i++) rows.push(['ctx', a[i]]);
    for (i = pre; i < a.length - suf; i++) rows.push(['del', a[i]]);
    for (i = pre; i < b.length - suf; i++) rows.push(['add', b[i]]);
    for (i = a.length - suf; i < Math.min(a.length, a.length - suf + 2); i++) rows.push(['ctx', a[i]]);
    if (rows.length > 400) rows = rows.slice(0, 400).concat([['ctx', '…']]);
    return h('div', { class: 'diff', dir: 'ltr' }, rows.map(function (r) {
      return h('div', { class: 'dl ' + r[0] }, h('span', { class: 'sg' }, r[0] === 'add' ? '+' : r[0] === 'del' ? '−' : ' '), r[1]);
    }));
  }

  function preBox(text, cls) {
    var t = String(text || '');
    if (t.length > 30000) t = t.slice(0, 30000) + '\n…';
    return h('pre', { class: 'tool-pre ' + (cls || ''), dir: 'auto' }, t);
  }

  function toolSummary(tab, name, input) {
    input = input || {};
    switch (name) {
      case 'Bash': return h('code', { class: 'ic', dir: 'ltr' }, input.command || '');
      case 'Read': case 'Write': case 'Edit': case 'MultiEdit': case 'NotebookEdit':
        return h('code', { class: 'ic path', dir: 'ltr' }, relPath(tab, input.file_path || input.notebook_path || ''));
      case 'Grep': return h('span', null, h('code', { class: 'ic', dir: 'ltr' }, input.pattern || ''), input.path ? [' in ', h('code', { class: 'ic', dir: 'ltr' }, relPath(tab, input.path))] : null);
      case 'Glob': return h('code', { class: 'ic', dir: 'ltr' }, input.pattern || '');
      case 'WebFetch': return h('code', { class: 'ic', dir: 'ltr' }, input.url || '');
      case 'WebSearch': return h('span', { dir: 'auto' }, input.query || '');
      case 'Task': case 'Agent': return h('span', { dir: 'auto' }, input.description || input.subagent_type || '');
      case 'TodoWrite': return h('span', null, (input.todos || []).length + ' tasks');
      case 'AskUserQuestion': return h('span', { dir: 'auto' }, ((input.questions || [])[0] || {}).question || '');
      case 'ExitPlanMode': return h('span', null, 'plan ready');
      default: return h('span', { class: 'muted', dir: 'ltr' }, Object.keys(input).slice(0, 3).join(', '));
    }
  }

  function toolDetail(tab, name, input) {
    input = input || {};
    switch (name) {
      case 'Bash': return [input.description ? h('div', { class: 'tool-desc', dir: 'auto' }, input.description) : null, preBox(input.command, 'cmd')];
      case 'Edit': return diffView(input.old_string, input.new_string);
      case 'MultiEdit': return (input.edits || []).map(function (e) { return diffView(e.old_string, e.new_string); });
      case 'Write': return diffView('', input.content);
      case 'TodoWrite': return todoList(input.todos);
      case 'AskUserQuestion': return h('div', null, (input.questions || []).map(function (q) {
        return h('div', { class: 'q-text', dir: 'auto' }, '• ' + q.question + '  (' + (q.options || []).map(function (o) { return o.label; }).join(' / ') + ')');
      }));
      case 'ExitPlanMode': return input.plan ? markdown(input.plan) : null;
      case 'Read': case 'Glob': case 'Grep': case 'WebSearch': return null;
      default: return preBox(JSON.stringify(input, null, 2), 'json');
    }
  }

  function todoList(todos) {
    return h('ul', { class: 'todos' }, (todos || []).map(function (t) {
      var st = t.status || 'pending';
      return h('li', { class: 'todo ' + st, dir: 'auto' },
        h('span', { class: 'tick' }, st === 'completed' ? '✓' : st === 'in_progress' ? '◐' : '○'),
        st === 'in_progress' && t.activeForm ? t.activeForm : t.content);
    }));
  }

  function resultText(content) {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) return content.map(function (c) { return c.type === 'text' ? c.text : c.type === 'image' ? '[image]' : ''; }).join('\n');
    return content ? JSON.stringify(content) : '';
  }

  function thumbs(srcs) {
    if (!srcs.length) return null;
    return h('div', { class: 'uthumbs' }, srcs.map(function (u) {
      return h('img', { src: u, alt: '', title: 'Click to view', onclick: function () { SW.viewImage(u); } });
    }));
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000), now = new Date();
    var opts = d.toDateString() === now.toDateString() ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' };
    try { return d.toLocaleString('en-GB', opts); } catch (e) { return d.toLocaleString(); }
  }

  // ------------------------------------------------------------------ ChatTab
  function ChatTab(info) {
    var self = this;
    this.id = info.id;
    this.info = info;
    this.alive = info.alive;
    this.exitCode = info.exitCode;
    this.isChat = true;
    this.opened = true;
    this.faOpen = false;
    this.closed = false;
    this.gone = false;
    this.retry = 0;
    this.busy = false;
    this.mode = 'default';
    this.model = '';
    this.cards = {};          // tool_use_id -> card
    this.agents = {};         // running sub-agents (Task/Agent tool calls) -> live info
    this.perms = {};          // request_id -> card
    this.groups = {};         // assistant message id -> element
    this.draft = null;        // live typing
    this.attachments = [];
    this.history = [];
    this.histPos = -1;
    this.commands = [];
    this.stderr = [];
    var p = SW.project(info.spec.project);
    this.root = p ? p.remote : '';
    SW.tabEl(this);

    // messages
    this.msgs = h('div', { class: 'chat-msgs' });
    this.scroller = h('div', { class: 'chat-scroll' }, this.msgs);
    this.scroller.addEventListener('scroll', function () {
      var s = self.scroller;
      if (s.scrollTop < 80) self.loadOlder();
      self.stick = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
      self.jumpBtn.classList.toggle('hidden', self.stick);
    });
    this.scroller.addEventListener('contextmenu', function (e) {
      if (window.getSelection && String(window.getSelection())) return;      // keep the browser menu for copying
      e.preventDefault();
      self.menu(e.clientX, e.clientY);
    });
    this.stick = true;
    this.jumpBtn = h('button', { class: 'jump hidden', title: 'Jump to the end', onclick: function () { self.stick = true; self.scrollEnd(); } }, '↓');

    // composer: everything for the chat lives here
    this.ta = h('textarea', { rows: 1, dir: 'auto', placeholder: 'Message Claude   ·   @ file   / command   Esc stop' });
    this.ta.addEventListener('keydown', function (e) { self.key(e); });
    this.ta.addEventListener('input', function () { self.autosize(); self.slashPopup(); });
    this.ta.addEventListener('click', function () { self.slashPopup(); });
    this.ta.addEventListener('blur', function () { setTimeout(function () { self.slashEl.classList.add('hidden'); }, 150); });
    this.ta.addEventListener('paste', function (e) { self.pasteFiles(e); });
    this.attachEl = h('div', { class: 'attach hidden' });
    this.slashEl = h('div', { class: 'slash hidden' });
    this.modeSel = h('select', { class: 'mini-sel mode', title: 'Permission mode (Shift+Tab)', onchange: function () { self.send({ t: 'mode', mode: self.modeSel.value }); } },
      MODES.map(function (m) { return h('option', { value: m[0], title: m[2] }, m[1]); }));
    this.modelSel = h('select', { class: 'mini-sel', title: 'Model', onchange: function () { self.send({ t: 'model', model: self.modelSel.value }); } },
      MODELS.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
    this.sendBtn = h('button', { class: 'send', title: 'Send (Enter)', onclick: function () { self.submit(); } }, '↑');
    this.stopBtn = h('button', { class: 'stopbtn hidden', title: 'Stop (Esc)', onclick: function () { self.interrupt(); } }, '■');
    this.stDot = h('i', { class: 'dot' });
    this.stText = h('span', { class: 'st-text' }, 'connecting…');
    this.stExtra = h('span', { class: 'st-extra' });
    this.statusEl = h('div', { class: 'cstatus', title: 'Live status' }, this.stDot, this.stText, this.stExtra);
    this.conn = 'connecting';
    this.timer = setInterval(function () {
      if (self.turnStart || self.conn !== 'online') self.renderStatus();
      if (self.agentsEl && Object.keys(self.agents || {}).length) self.renderAgents();
    }, 1000);
    this.agentsEl = h('div', { class: 'agents hidden' });
    var composer = h('div', { class: 'chat-composer' }, this.slashEl, this.agentsEl, this.statusEl, this.attachEl,
      h('div', { class: 'cbox' }, this.ta,
        h('div', { class: 'cbar' },
          this.modeSel, this.modelSel,
          h('label', { class: 'clip', title: 'Attach image' }, '+', h('input', { type: 'file', accept: 'image/*', multiple: true, class: 'hidden', onchange: function (e) { self.addFiles(e.target.files); e.target.value = ''; } })),
          h('span', { class: 'grow' }),
          h('button', { class: 'link', title: 'Past conversations', onclick: function () { self.historyMenu(); } }, 'history'),
          h('button', { class: 'link', title: 'Start a new conversation in this tab', onclick: function () { self.newChat(); } }, 'new'),
          this.stopBtn, this.sendBtn)));

    this.body = h('div', { class: 'pbody chat-body' }, this.scroller, this.jumpBtn);
    this.pane = h('div', { class: 'pane hidden-pane chat' }, this.body, composer);
    this.applyInfo();
    this.connect();
  }

  var P = ChatTab.prototype;

  P.focus = function () { this.ta.focus(); };
  P.ensureOpen = function () {};
  P.doFit = function () {};
  P.toggleFa = function () {};
  P.renderFa = function () {};
  P.toggleComposer = function () { this.ta.focus(); };

  P.send = function (msg) {
    if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
    toast('This tab is not connected', 'warn');
    return false;
  };

  P.connect = function () {
    var self = this;
    if (this.closed || this.gone) return;
    var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?id=' + encodeURIComponent(this.id) + '&t=' + encodeURIComponent(SW.token));
    this.ws = ws;
    ws.onopen = function () { self.retry = 0; self.conn = 'online'; self.renderStatus(); };
    ws.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      try { self.onMessage(m); } catch (err) { self.reportError(err, m.e || m); }
    };
    ws.onerror = function () {};
    this.onMessage = function (m) {
      if (m.t === 's') {
        self.reset();
        self.applyState(m.state);
        self.replaying = true;
        try {
          m.events.forEach(function (e) {
            try { self.onEvent(e); } catch (err) { self.reportError(err, e); }
          });
        } finally { self.replaying = false; }
        var st = m.state || {};
        if (Object.keys(self.perms).length) self.setPhase('wait', 'waiting for you');
        else if (st.busy) self.setPhase('work', 'working…');
        else if (!st.alive) self.setPhase('idle', 'not started — send a message');
        else self.setPhase('idle', 'ready');
        self.stick = true;
        self.scrollEnd();
      } else if (m.t === 'e') {
        self.onEvent(m.e);
      } else if (m.t === 'u') {
        window.dispatchEvent(new CustomEvent('sw-usage'));
      } else if (m.t === 'busy') {
        self.setBusy(m.busy);
        if (!m.busy && self.phaseKind === 'work') self.setPhase('idle', 'ready');
      } else if (m.t === 'clear') {
        self.reset();
      } else if (m.t === 'gone') {
        self.gone = true;
      }
    };
    ws.onclose = function () {
      if (self.closed || self.gone) return;
      self.conn = 'offline';
      self.renderStatus();
      self.retry++;
      setTimeout(function () {
        api('GET', 'state').then(function (st) {
          if (st.sessions.some(function (s) { return s.id === self.id; })) self.connect();
          else { self.gone = true; self.system('This conversation is no longer running. Use “history” to continue it.', 'err'); }
        }).catch(function () { self.connect(); });
      }, Math.min(5000, 300 * self.retry));
    };
  };

  P.reportError = function (err, ev) {
    console.error(err, ev);
    if (this.errShown) return;
    this.errShown = true;
    toast('Chat display error (' + ((ev && (ev.type || ev.t)) || '?') + '): ' + (err && err.message), 'err', { timeout: 20000 });
  };

  // Long conversations: only the last page is loaded; earlier pages load when you scroll up.
  P.setTopLabel = function () {
    if (!this.topEl) return;
    this.topEl.textContent = this.loadingOlder ? 'loading earlier messages…' : this.histStart > 0 ? '↑ scroll up for earlier messages' : 'beginning of conversation';
  };

  P.loadOlder = function () {
    var self = this;
    if (this.loadingOlder || !(this.histStart > 0) || this.gone) return;
    this.loadingOlder = true;
    this.setTopLabel();
    api('GET', 'chat-history/' + encodeURIComponent(this.id) + '?before=' + this.histStart).then(function (d) {
      self.loadingOlder = false;
      var entries = (d && d.entries) || [];
      if (!entries.length || d.start >= self.histStart) { self.histStart = 0; self.setTopLabel(); return; }
      self.prependEntries(entries);
      self.histStart = d.start;
      self.setTopLabel();
    }).catch(function (e) { self.loadingOlder = false; self.setTopLabel(); fail(e); });
  };

  P.prependEntries = function (entries) {
    var self = this;
    var sc = this.scroller;
    var fromBottom = sc.scrollHeight - sc.scrollTop;
    var real = this.msgs, temp = h('div'), wasReplaying = this.replaying;
    this.msgs = temp;
    this.replaying = true;
    try {
      entries.forEach(function (x) { try { self.onEvent(x); } catch (err) { self.reportError(err, x); } });
    } finally {
      this.msgs = real;
      this.replaying = wasReplaying;
    }
    var anchor = this.topEl && this.topEl.parentNode === real ? this.topEl.nextSibling : real.firstChild;
    Array.prototype.slice.call(temp.childNodes).forEach(function (n) { real.insertBefore(n, anchor); });
    sc.scrollTop = sc.scrollHeight - fromBottom;            // keep what you were looking at in place
  };

  P.renderAgents = function () {
    var self = this;
    var ids = Object.keys(this.agents);
    this.agentsEl.classList.toggle('hidden', !ids.length);
    if (!ids.length) { this.agentsEl.replaceChildren(); return; }
    this.agentsEl.replaceChildren.apply(this.agentsEl, [h('div', { class: 'ag-head' }, ids.length + (ids.length === 1 ? ' agent' : ' agents') + ' working')].concat(ids.map(function (id) {
      var a = self.agents[id];
      return h('div', { class: 'ag', title: 'Show it in the conversation', onclick: function () {
        a.card.classList.add('open');
        if (a.card.scrollIntoView) a.card.scrollIntoView({ block: 'center' });
      } },
        h('i', { class: 'spin' }), h('b', { dir: 'auto' }, a.title), h('span', { class: 'ag-type' }, a.type),
        h('span', { class: 'ag-last', dir: 'auto' }, a.last), h('span', { class: 'ag-time' }, fmtDur(Date.now() - a.start) + (a.steps ? ' · ' + a.steps + ' steps' : '')));
    })));
  };

  P.clearAgents = function () {
    if (this.agents && Object.keys(this.agents).length) { this.agents = {}; this.renderAgents(); }
  };

  P.reset = function () {
    this.msgs.replaceChildren();
    this.cards = {}; this.perms = {}; this.groups = {}; this.draft = null; this.stderr = [];
    this.lastUser = null;
    this.echoes = [];
    this.histStart = 0;
    this.topEl = null;
    this.agents = {};
    if (this.agentsEl) this.renderAgents();
  };

  P.applyState = function (st) {
    if (!st) return;
    if (st.sessionId) this.sessionId = st.sessionId;
    this.alive = st.alive;
    this.exitCode = st.code;
    this.setMode(st.mode);
    this.model = st.model || '';
    this.modelSel.value = this.model;
    this.setBusy(st.busy);
    if (st.init) this.applyInit(st.init);
    this.applyInfo();
  };

  P.applyInit = function (init) {
    var self = this;
    if (Array.isArray(init.commands)) this.commands = init.commands.map(function (c) { return typeof c === 'string' ? { name: c } : c; });
    if (Array.isArray(init.models) && init.models.length) {
      var cur = this.modelSel.value;
      this.modelSel.replaceChildren.apply(this.modelSel, [h('option', { value: '' }, 'default model')].concat(init.models.filter(function (m) { return m.value && m.value !== 'default'; }).map(function (m) {
        return h('option', { value: m.value, title: m.description || '' }, m.displayName || m.value);
      })));
      this.modelSel.value = cur;
      if (this.modelSel.value !== cur) { this.modelSel.append(h('option', { value: cur }, cur)); this.modelSel.value = cur; }
    }
    void self;
  };

  P.setMode = function (mode) {
    if (!mode) return;
    this.mode = mode;
    this.modeSel.value = mode;
    this.modeSel.className = 'mini-sel mode m-' + mode;
  };

  P.setBusy = function (b) {
    this.busy = !!b;
    this.stopBtn.classList.toggle('hidden', !this.busy);
    this.el.classList.toggle('busy', this.busy);
  };

  P.applyInfo = function () {
    this.ttlEl.textContent = this.info.title;
    this.el.title = this.info.title + (this.info.spec.project ? ' — ' + this.info.spec.project : '');
  };

  P.scrollEnd = function () {
    if (this.stick) this.scroller.scrollTop = this.scroller.scrollHeight;
  };

  P.add = function (el) {
    this.msgs.append(el);
    if (!this.replaying) this.scrollEnd();
    return el;
  };

  P.system = function (text, cls) {
    return this.add(h('div', { class: 'sysline ' + (cls || ''), dir: 'auto' }, text));
  };

  P.activity = function () {
    if (S.active !== this && !this.replaying) this.el.classList.add('activity');
  };

  // ------------------------------------------------------------ events
  // ------------------------------------------------------------ live status (what Claude is doing now)
  var PHASE_DOT = { idle: 'ok', work: 'busy', wait: 'warn', off: 'err' };

  function fmtDur(ms) { var s = Math.floor(ms / 1000); return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's'; }
  function fmtTok(n) { return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }

  function toolHint(tab, input) {
    input = input || {};
    var v = input.command || (input.file_path && relPath(tab, input.file_path)) || input.pattern || input.url || input.query || input.description || '';
    v = String(v).replace(/\s+/g, ' ').trim();
    return v ? ' ' + (v.length > 60 ? v.slice(0, 60) + '…' : v) : '';
  }

  P.setPhase = function (kind, text) {
    this.phaseKind = kind;
    this.phaseText = text;
    if ((kind === 'work' || kind === 'wait') && !this.turnStart) this.turnStart = Date.now();
    if (kind === 'idle') this.turnStart = 0;
    this.renderStatus();
  };

  P.renderStatus = function () {
    var conn = this.conn === 'online' ? null : (this.conn === 'connecting' ? 'connecting…' : 'disconnected — reconnecting…');
    var kind = conn ? 'off' : (this.phaseKind || 'idle');
    var extra = [];
    var waiting = (this.echoes || []).length - (kind === 'work' && this.phaseText === 'sending…' ? 1 : 0);
    if (!conn && waiting > 0) extra.push(waiting + ' queued');
    var nAgents = Object.keys(this.agents || {}).length;
    if (!conn && nAgents) extra.push(nAgents + (nAgents === 1 ? ' agent' : ' agents'));
    if (!conn && this.turnStart) extra.push(fmtDur(Date.now() - this.turnStart));
    if (!conn && kind === 'work' && this.outTokens) extra.push('↓ ' + fmtTok(this.outTokens) + ' tokens');
    if (!conn && kind === 'work' && this.lastEventAt && Date.now() - this.lastEventAt > 45000) extra.push('quiet for ' + fmtDur(Date.now() - this.lastEventAt));
    this.stDot.className = 'dot ' + PHASE_DOT[kind];
    this.statusEl.className = 'cstatus ' + kind;
    this.stText.textContent = conn || this.phaseText || 'ready';
    this.stExtra.textContent = extra.length ? ' · ' + extra.join(' · ') : '';
  };

  P.track = function (e) {
    this.lastEventAt = Date.now();
    var t = e.type;
    if (t === 'stream_event') {
      var ev = e.event || {};
      if (e.parent_tool_use_id) { this.setPhase('work', 'subagent working…'); return; }
      if (ev.type === 'message_start') { this.msgTok = 0; this.setPhase('work', 'thinking…'); }
      else if (ev.type === 'content_block_start') {
        var cb = ev.content_block || {};
        this.setPhase('work', cb.type === 'text' ? 'writing…' : cb.type === 'tool_use' ? 'preparing ' + cb.name + '…' : 'thinking…');
      } else if (ev.type === 'message_delta' && ev.usage && ev.usage.output_tokens) {
        this.msgTok = ev.usage.output_tokens;
        this.outTokens = (this.tokBase || 0) + this.msgTok;
        this.renderStatus();
      } else if (ev.type === 'message_stop') { this.tokBase = (this.tokBase || 0) + (this.msgTok || 0); this.msgTok = 0; }
    } else if (t === 'assistant' && !e.parent_tool_use_id) {
      var tools = ((e.message || {}).content || []).filter(function (b) { return b.type === 'tool_use'; });
      if (tools.length) { var b = tools[tools.length - 1]; this.setPhase('work', 'running ' + b.name + toolHint(this, b.input)); }
    } else if (t === 'user' && !e.parent_tool_use_id) {
      var c = (e.message || {}).content;
      if (Array.isArray(c) && c.some(function (x) { return x.type === 'tool_result'; })) this.setPhase('work', 'thinking…');
    } else if (t === 'system' && e.subtype === 'status' && e.status) {
      this.setPhase('work', String(e.status) + '…');
    } else if (t === 'control_request') {
      var name = (e.request || {}).tool_name;
      this.setPhase('wait', name === 'AskUserQuestion' ? 'waiting for your answer' : name === 'ExitPlanMode' ? 'waiting for you to approve the plan' : 'waiting for permission: ' + name);
    } else if (t === '_perm_done' || t === 'control_cancel_request') {
      this.setPhase('work', 'continuing…');
    } else if (t === 'result') {
      if ((this.echoes || []).length) { this.turnStart = 0; this.setPhase('work', 'next: your queued message…'); return; }
      this.setPhase('idle', e.is_error || (e.subtype && e.subtype !== 'success') ? 'stopped' : 'ready');
    } else if (t === '_exit') {
      this.setPhase('idle', 'Claude is not running — your next message starts it');
    } else if (t === '_error') {
      this.setPhase('idle', 'error');
    }
  };

  P.onEvent = function (e) {
    var self = this;
    if (e && typeof e.session_id === 'string' && /^[0-9a-f-]{36}$/.test(e.session_id)) this.sessionId = e.session_id;
    if (!this.replaying) this.track(e);
    switch (e.type) {
      case '_history':
        if (e.entries && e.entries.length) {
          this.histStart = e.start || 0;
          this.topEl = this.system('', 'hist top');
          this.setTopLabel();
          e.entries.forEach(function (x) { if (!x.isMeta) self.onEvent(x); });
          this.system('— resumed —', 'hist');
        }
        break;
      case 'stream_event': this.onStream(e); break;
      case 'assistant': this.onAssistant(e); this.activity(); break;
      case 'user': this.onUser(e); break;
      case 'result': this.onResult(e); break;
      case 'system':
        if (e.subtype === 'init') {
          if (e.permissionMode) this.setMode(e.permissionMode);
          if (Array.isArray(e.slash_commands) && !this.commands.length) this.commands = e.slash_commands.map(function (n) { return { name: n }; });
          if (e.model) this.modelName = e.model;
        } else if (e.subtype === 'compact_boundary') {
          this.system('— conversation compacted —', 'hist');
        }
        break;
      case 'control_request': this.onPermission(e); break;
      case 'control_cancel_request': this.closePerm(e.request_id, 'cancelled'); break;
      case '_perm_done':
        this.closePerm(e.request_id, e.answers
          ? '✓ answered: ' + Object.keys(e.answers).map(function (k) { return e.answers[k]; }).join(' · ')
          : e.allow ? (e.always ? '✓ allowed (always)' : '✓ allowed') : '✗ denied' + (e.message ? ': ' + e.message : ''));
        break;
      case '_init': this.applyInit(e.info || {}); break;
      case '_mode': this.setMode(e.mode); break;
      case '_model': this.model = e.model; this.modelSel.value = e.model; if (!this.replaying) this.system('model: ' + (e.model || 'default')); break;
      case '_stderr': this.stderr.push(e.text); if (this.stderr.length > 200) this.stderr.shift(); break;
      case '_error': this.system(e.text, 'err'); break;
      case '_restart': this.system('— restarted —', 'hist'); break;
      case '_exit':
        this.alive = false;
        this.setBusy(false);
        if (e.code !== 0 && e.code !== null) {
          var box = this.system('Claude stopped (exit ' + e.code + '). Your next message starts it again and continues the conversation.', 'err');
          if (this.stderr.length) box.append(h('details', null, h('summary', null, 'details'), preBox(this.stderr.slice(-40).join('\n'))));
          if (e.code === 97) box.append(h('div', null, 'The project folder was not found on the server.'));
        }
        break;
      default: break;
    }
  };

  P.onStream = function (e) {
    var ev = e.event || {};
    if (e.parent_tool_use_id) return;
    if (ev.type === 'message_start') {
      this.draft = { id: ev.message && ev.message.id, blocks: {} };
      this.setBusy(true);
      return;
    }
    if (!this.draft) return;
    if (ev.type === 'content_block_start') {
      var cb = ev.content_block || {};
      var g = this.msgGroup(this.draft.id);
      var el;
      if (cb.type === 'text') el = h('div', { class: 'live-text', dir: 'auto' });
      else if (cb.type === 'thinking') el = h('details', { class: 'thinking live', open: true }, h('summary', null, 'thinking…'), h('div', { class: 'think-body', dir: 'auto' }));
      else if (cb.type === 'tool_use') el = h('div', { class: 'tool-card pending' }, h('div', { class: 'tool-head' }, h('span', { class: 'tname' }, TOOL_FA[cb.name] || cb.name), h('i', { class: 'spin' })));
      if (el) { el.dataset.draft = '1'; el.dataset.type = cb.type; el._pending = ''; g.append(el); this.draft.blocks[ev.index] = { el: el, type: cb.type, done: false }; this.scrollEnd(); }
    } else if (ev.type === 'content_block_delta') {
      var b = this.draft.blocks[ev.index];
      if (!b) return;
      var d = ev.delta || {};
      if (d.type === 'text_delta') { this.type(b.el, d.text); this.turnText = true; }
      else if (d.type === 'thinking_delta') { b.el.lastChild.append(d.thinking); this.scrollEnd(); }
    } else if (ev.type === 'content_block_stop') {
      var bb = this.draft.blocks[ev.index];
      if (bb) bb.done = true;
    }
  };

  P.msgGroup = function (id) {
    var g = id && this.groups[id];
    if (!g) {
      g = h('div', { class: 'amsg' });
      if (id) this.groups[id] = g;
      this.add(g);
    }
    return g;
  };

  // Typewriter: deltas arrive in bursts; reveal them smoothly so the answer is
  // visibly written live. When the block is final it becomes markdown in place.
  P.type = function (el, text) {
    el._pending = (el._pending || '') + text;
    if (window.SWChat.instant) { this.flushTyping(el); return; }
    if (el._timer) return;
    var self = this;
    (function step() {
      var p = el._pending;
      if (!p) { el._timer = null; if (el._onDone) { var f = el._onDone; el._onDone = null; f(); } return; }
      var n = Math.max(2, Math.ceil(p.length / 8));
      el.append(p.slice(0, n));
      el._pending = p.slice(n);
      self.scrollEnd();
      el._timer = setTimeout(step, document.hidden ? 0 : 16);
    })();
  };

  P.flushTyping = function (el) {
    if (el._pending) { el.append(el._pending); el._pending = ''; }
    if (el._timer) { clearTimeout(el._timer); el._timer = null; }
    if (el._onDone) { var f = el._onDone; el._onDone = null; f(); }
    this.scrollEnd();
  };

  // the oldest live draft of this message and block type that has no final yet
  P.takeDraft = function (id, type) {
    var g = this.groups[id];
    if (!g) return null;
    var d = g.querySelectorAll('[data-draft]').find
      ? g.querySelectorAll('[data-draft]').find(function (x) { return x.dataset.type === type && !x.dataset.final; })
      : Array.prototype.find.call(g.querySelectorAll('[data-draft]'), function (x) { return x.dataset.type === type && !x.dataset.final; });
    if (d) d.dataset.final = '1';
    return d || null;
  };

  P.renderBlock = function (b) {
    if (b.type === 'text') { if (b.text && b.text.trim()) { this.turnText = true; return markdown(b.text); } return null; }
    if (b.type === 'thinking') return b.thinking && b.thinking.trim() ? h('details', { class: 'thinking' }, h('summary', null, 'thought'), h('div', { class: 'think-body', dir: 'auto' }, b.thinking)) : null;
    if (b.type === 'tool_use') return this.toolCard(b);
    return null;
  };

  P.onAssistant = function (e) {
    var self = this;
    var msg = e.message || {};
    if (e.parent_tool_use_id) { this.subagent(e.parent_tool_use_id, msg); return; }
    var g = this.msgGroup(msg.id);
    (msg.content || []).forEach(function (b) {
      var final = self.renderBlock(b);
      var draft = self.takeDraft(msg.id, b.type);
      var put = function () { if (final) { if (draft && draft.parentNode) draft.replaceWith(final); else g.append(final); } else if (draft) draft.remove(); self.scrollEnd(); };
      if (draft && (draft._pending || draft._timer)) draft._onDone = put;     // finish typing first
      else put();
    });
    if (msg.model && !this.replaying) this.modelName = msg.model;
    this.scrollEnd();
  };

  P.toolCard = function (b) {
    var self = this;
    var name = b.name || '?';
    var detail = toolDetail(this, name, b.input);
    var body = h('div', { class: 'tool-body' }, detail);
    var resBox = h('div', { class: 'tool-result hidden' });
    var status = h('i', { class: 'spin' });
    var open = name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'TodoWrite';
    var card = h('div', { class: 'tool-card t-' + name + (open ? ' open' : '') },
      h('div', { class: 'tool-head', onclick: function () { card.classList.toggle('open'); } },
        h('span', { class: 'tname' }, TOOL_FA[name] || name), h('span', { class: 'tsum' }, toolSummary(this, name, b.input)), status),
      body, resBox);
    card._status = status;
    card._res = resBox;
    card._sub = null;
    this.cards[b.id] = card;
    if ((name === 'Task' || name === 'Agent') && !this.replaying) {
      var inp = b.input || {};
      this.agents[b.id] = { title: inp.description || inp.subagent_type || 'agent', type: inp.subagent_type || 'agent',
        start: Date.now(), last: 'starting…', steps: 0, card: card };
      this.renderAgents();
    }
    if (name === 'TodoWrite') this.todos = b.input && b.input.todos;
    void self;
    return card;
  };

  P.subagent = function (parentId, msg) {
    var ag = this.agents[parentId];
    if (ag) {
      var self0 = this;
      (msg.content || []).forEach(function (b) {
        if (b.type === 'tool_use') { ag.steps++; ag.last = b.name + toolHint(self0, b.input); }
        else if (b.type === 'text' && b.text && b.text.trim()) ag.last = b.text.trim().split('\n')[0].slice(0, 90);
      });
      this.renderAgents();
    }
    var card = this.cards[parentId];
    if (!card) return;
    if (!card._sub) { card._sub = h('div', { class: 'subagent' }); card.querySelector('.tool-body').append(card._sub); }
    var self = this;
    (msg.content || []).forEach(function (b) {
      if (b.type === 'tool_use') card._sub.append(h('div', { class: 'subline' }, '↳ ', h('b', null, TOOL_FA[b.name] || b.name), ' ', toolSummary(self, b.name, b.input)));
      else if (b.type === 'text' && b.text.trim()) card._sub.append(h('div', { class: 'subline', dir: 'auto' }, b.text.slice(0, 300)));
    });
  };

  P.onUser = function (e) {
    var self = this;
    if (e.parent_tool_use_id) return;
    var msg = e.message || {};
    var c = msg.content;
    if (typeof c === 'string') c = [{ type: 'text', text: c }];
    var texts = [], images = 0, srcs = [];
    (c || []).forEach(function (b) {
      if (b.type === 'tool_result') {
        if (self.agents[b.tool_use_id]) { delete self.agents[b.tool_use_id]; self.renderAgents(); }
        var card = self.cards[b.tool_use_id];
        if (!card) return;
        card.classList.remove('pending');
        card._status.className = b.is_error ? 'st err' : 'st ok';
        card._status.textContent = b.is_error ? '✗' : '✓';
        var txt = resultText(b.content);
        if (b.is_error) card.classList.add('error', 'open');
        if (txt && txt.trim()) {
          card._res.classList.remove('hidden');
          card._res.replaceChildren(h('details', b.is_error ? { open: true } : null, h('summary', null, b.is_error ? 'error' : 'output'), preBox(txt, b.is_error ? 'err' : '')));
        }
      } else if (b.type === 'text') {
        var t = stripReminders(b.text);
        if (t && !/^<(command-|local-command|bash-|user-memory)/.test(t)) texts.push(t);
      } else if (b.type === 'image') {
        images++;
        var src = b.source || {};
        if (src.type === 'base64' && src.data) srcs.push('data:' + (src.media_type || 'image/png') + ';base64,' + src.data);
      }
    });
    if (texts.length || images) {
      this.turnText = false;
      var echo = this.echoes && this.echoes[0];
      if (echo && !this.replaying && (e.isReplay || echo.text === texts.join('\n\n').trim())) {   // the message we already showed
        this.echoes.shift();
        echo.el.classList.remove('pending', 'queued');
        var ql = echo.el.querySelector('.qlabel');
        if (ql) ql.remove();
        return;
      }
      var bubble = h('div', { class: 'umsg' }, h('div', { class: 'ubody', dir: 'auto' }, texts.join('\n\n')),
        srcs.length ? thumbs(srcs) : images ? h('div', { class: 'uimg' }, images + (images === 1 ? ' image' : ' images')) : null);
      this.add(bubble);
      this.lastUser = bubble;
      if (!this.replaying) { this.stick = true; this.scrollEnd(); }
    }
  };

  P.onResult = function (e) {
    var self = this;
    this.clearAgents();
    if (!this.replaying && !(this.echoes || []).length) this.setPhase('idle', e.is_error || (e.subtype && e.subtype !== 'success') ? 'stopped' : 'ready');
    Array.prototype.forEach.call(this.msgs.querySelectorAll('[data-draft]'), function (el) { self.flushTyping(el); });
    this.setBusy(false);
    this.draft = null;
    if (!this.turnText && e.result && typeof e.result === 'string' && e.result.trim() && !e.is_error) {
      this.add(h('div', { class: 'amsg' }, markdown(e.result)));     // safety net: always show the answer
    }
    this.turnText = false;
    var parts = [];
    if (e.duration_ms) parts.push((e.duration_ms / 1000).toFixed(1) + 's');
    if (e.num_turns) parts.push(e.num_turns + (e.num_turns === 1 ? ' turn' : ' turns'));
    if (typeof e.total_cost_usd === 'number') parts.push('$' + e.total_cost_usd.toFixed(3));
    var err = e.is_error || (e.subtype && e.subtype !== 'success');
    var line = this.add(h('div', { class: 'result-line' + (err ? ' err' : '') }, (err ? '✗ ' : '✓ ') + parts.join(' · ')));
    if (err && e.result) line.append(h('div', { dir: 'auto' }, String(e.result).slice(0, 500)));
    if (!this.replaying && S.active !== this) {
      var self = this;
      this.el.classList.add('bell');
      toast('Claude finished in ' + this.info.title, err ? 'err' : 'ok', { onclick: function () { SW.activate(self); } });
      SW.notify('Claude finished', this.info.title);
    }
  };

  // ------------------------------------------------------------ permissions
  P.onPermission = function (e) {
    var self = this;
    var req = e.request || {};
    if (req.subtype !== 'can_use_tool') return;
    var name = req.tool_name;
    var rid = e.request_id;
    if (name === 'AskUserQuestion') { this.questionCard(rid, req.input || {}); this.alertUser('Claude has a question'); return; }
    if (name === 'ExitPlanMode') { this.planCard(rid, req.input || {}); this.alertUser('Claude’s plan is ready'); return; }
    var reason = h('input', { class: 'inp deny-msg hidden', dir: 'auto', placeholder: 'Why not, or what to do instead (optional)' });
    var actions = h('div', { class: 'perm-actions' },
      h('button', { class: 'btn primary sm', onclick: function () { self.answer(rid, true, false); } }, 'Allow  1'),
      req.permission_suggestions && req.permission_suggestions.length
        ? h('button', { class: 'btn sm', title: 'Do not ask again in this conversation', onclick: function () { self.answer(rid, true, true); } }, 'Always allow  2') : null,
      h('button', { class: 'btn sm danger', onclick: function () {
        if (reason.classList.contains('hidden')) { reason.classList.remove('hidden'); reason.focus(); return; }
        self.answer(rid, false, false, reason.value);
      } }, 'Deny  3'));
    reason.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); self.answer(rid, false, false, reason.value); } });
    var card = h('div', { class: 'perm-card' },
      h('div', { class: 'perm-title' }, 'Allow ', h('b', null, TOOL_FA[name] || name), ' ', toolSummary(this, name, req.input)),
      req.decision_reason ? h('div', { class: 'perm-reason', dir: 'auto' }, String(req.decision_reason)) : null,
      h('div', { class: 'perm-detail' }, toolDetail(this, name, req.input)),
      actions, reason);
    this.perms[rid] = card;
    this.add(card);
    this.pendingPerm = rid;
    this.stick = true;
    this.scrollEnd();
    if (!this.replaying && S.active !== this) {
      this.el.classList.add('bell');
      toast('Claude needs permission in ' + this.info.title, 'warn', { onclick: function () { SW.activate(self); } });
      SW.notify('Claude needs permission', (TOOL_FA[name] || name) + ' — ' + this.info.title);
    }
  };

  P.answer = function (rid, allow, always, message, input, permissions) {
    var m = { t: 'perm', id: rid, allow: allow, always: always, message: message || '' };
    if (input) m.input = input;
    if (permissions) m.permissions = permissions;
    this.send(m);
  };

  P.alertUser = function (text) {
    this.stick = true;
    this.scrollEnd();
    if (this.replaying || S.active === this) return;
    var self = this;
    this.el.classList.add('bell');
    toast(text + ' — «' + this.info.title + '»', 'warn', { onclick: function () { SW.activate(self); } });
    SW.notify(text, this.info.title);
  };

  // Claude asks the user (AskUserQuestion): options as buttons + free answer
  P.questionCard = function (rid, input) {
    var self = this;
    var qs = Array.isArray(input.questions) ? input.questions : [];
    var state = qs.map(function () { return { sel: [], other: '' }; });
    var card = h('div', { class: 'perm-card q-card' }, h('div', { class: 'perm-title' }, 'Claude asks'));
    function mark(qi) {
      Array.prototype.forEach.call(state[qi].optsEl.children, function (b) { b.classList.toggle('on', state[qi].sel.indexOf(b._label) >= 0); });
    }
    function submit() {
      var answers = {};
      for (var i = 0; i < qs.length; i++) {
        var vals = state[i].sel.slice();
        if (state[i].other.trim()) vals.push(state[i].other.trim());
        if (!vals.length) { toast('Answer every question (or Skip)', 'warn'); return; }
        answers[qs[i].question] = vals.join(', ');
      }
      self.answer(rid, true, false, '', { answers: answers });
    }
    qs.forEach(function (q, qi) {
      var opts = h('div', { class: 'q-opts' });
      (q.options || []).forEach(function (o) {
        var b = h('button', { class: 'q-opt', dir: 'auto', onclick: function () {
          var st = state[qi];
          if (q.multiSelect) {
            var k = st.sel.indexOf(o.label);
            if (k >= 0) st.sel.splice(k, 1); else st.sel.push(o.label);
          } else { st.sel = [o.label]; st.other = ''; other.value = ''; }
          mark(qi);
          if (!q.multiSelect && qs.length === 1) submit();          // one simple question: answer on click
        } }, h('b', { dir: 'auto' }, o.label), o.description ? h('span', { class: 'qd', dir: 'auto' }, o.description) : null);
        b._label = o.label;
        opts.append(b);
      });
      var other = h('input', { class: 'inp q-other', dir: 'auto', placeholder: 'Other answer…' });
      other.addEventListener('input', function () {
        state[qi].other = other.value;
        if (other.value && !q.multiSelect) { state[qi].sel = []; mark(qi); }
      });
      other.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); submit(); } });
      state[qi].optsEl = opts;
      card.append(h('div', { class: 'q-block', dir: 'auto' },
        q.header ? h('span', { class: 'q-head' }, q.header) : null,
        h('div', { class: 'q-text', dir: 'auto' }, q.question),
        q.multiSelect ? h('div', { class: 'q-hint' }, 'choose one or more') : null,
        opts, other));
    });
    card.append(h('div', { class: 'perm-actions' },
      h('button', { class: 'btn primary sm', onclick: submit }, 'Answer'),
      h('button', { class: 'btn sm', onclick: function () { self.answer(rid, false, false, 'The user declined to answer these questions.'); } }, 'Skip')));
    this.perms[rid] = card;
    this.add(card);
    var first = card.querySelector('.q-opt');
    if (first && !this.replaying && S.active === this) setTimeout(function () { first.focus(); }, 50);
  };

  // plan mode finished: approve (and how to continue) or keep planning with feedback
  P.planCard = function (rid, input) {
    var self = this;
    var fb = h('textarea', { class: 'inp hidden', dir: 'auto', rows: 3, placeholder: 'What should change in the plan?' });
    var card = h('div', { class: 'perm-card plan-card' },
      h('div', { class: 'perm-title' }, 'Claude’s plan'),
      h('div', { class: 'plan-body' }, markdown(input.plan || '')),
      h('div', { class: 'perm-actions' },
        h('button', { class: 'btn primary sm', title: 'Start, editing files without asking', onclick: function () {
          self.answer(rid, true, false, '', null, [{ type: 'setMode', mode: 'acceptEdits', destination: 'session' }]);
        } }, 'Approve · auto-edit'),
        h('button', { class: 'btn sm', title: 'Start, but ask before each edit', onclick: function () {
          self.answer(rid, true, false, '', null, [{ type: 'setMode', mode: 'default', destination: 'session' }]);
        } }, 'Approve · ask'),
        h('button', { class: 'btn sm', onclick: function () {
          if (fb.classList.contains('hidden')) { fb.classList.remove('hidden'); fb.focus(); return; }
          self.answer(rid, false, false, fb.value.trim() || 'Keep planning; the user did not approve this plan yet.');
        } }, 'Keep planning…')),
      fb);
    fb.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); self.answer(rid, false, false, fb.value.trim() || 'Keep planning.'); }
    });
    this.perms[rid] = card;
    this.add(card);
  };

  P.closePerm = function (rid, text) {
    var card = this.perms[rid];
    if (!card) return;
    card.classList.add('done');
    var a = card.querySelector('.perm-actions');
    if (a) a.replaceChildren(h('span', { class: 'perm-done', dir: 'auto' }, text));
    Array.prototype.forEach.call(card.querySelectorAll('.deny-msg, .q-other, textarea'), function (x) { x.remove(); });
    Array.prototype.forEach.call(card.querySelectorAll('.q-opt'), function (x) { x.disabled = true; });
    delete this.perms[rid];
    if (this.pendingPerm === rid) this.pendingPerm = null;
  };

  // ------------------------------------------------------------ composer
  P.autosize = function () {
    this.ta.style.height = 'auto';
    this.ta.style.height = Math.min(260, this.ta.scrollHeight + 2) + 'px';
  };

  P.key = function (e) {
    if (e.isComposing) return;
    if (SW.isGlobalShortcut(e)) return;
    var popup = !this.slashEl.classList.contains('hidden') && (this.slashItems || []).some(function (x) { return x.kind === 'cmd' || x.kind === 'file'; });
    if (popup && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      var n = this.slashItems.length;
      this.slashSel = (this.slashSel + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      this.markSlash();
      return;
    }
    if (popup && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      this.pickSlash(this.slashSel);
      return;
    }
    if (this.pendingPerm && !this.ta.value && /^[123]$/.test(e.key)) {
      e.preventDefault();
      var card = this.perms[this.pendingPerm];
      if (e.key === '1') this.answer(this.pendingPerm, true, false);
      else if (e.key === '2' && card && card.querySelectorAll('.perm-actions .btn').length > 2) this.answer(this.pendingPerm, true, true);
      else if (e.key === '3') this.answer(this.pendingPerm, false, false);
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); this.submit(); return; }
    if (e.key === 'Escape') {
      e.preventDefault();
      if (popup) { this.slashEl.classList.add('hidden'); return; }
      if (this.busy) this.interrupt();
      return;
    }
    if (e.key === 'Tab' && e.shiftKey) {
      e.preventDefault();
      var i = MODES.findIndex(function (m) { return m[0] === this.mode; }, this);
      var next = MODES[(i + 1) % MODES.length][0];
      this.send({ t: 'mode', mode: next });
      return;
    }
    var ta = this.ta;
    if (e.key === 'ArrowUp' && this.history.length && (!ta.value || (ta.selectionStart === 0 && ta.selectionEnd === 0))) {
      e.preventDefault();
      this.histPos = this.histPos < 0 ? this.history.length - 1 : Math.max(0, this.histPos - 1);
      ta.value = this.history[this.histPos];
      this.autosize();
    } else if (e.key === 'ArrowDown' && this.histPos >= 0 && ta.selectionStart === ta.value.length) {
      e.preventDefault();
      this.histPos++;
      if (this.histPos >= this.history.length) { this.histPos = -1; ta.value = ''; } else ta.value = this.history[this.histPos];
      this.autosize();
    }
  };

  // popup under the input: "/" commands at the start, "@" project files anywhere
  P.slashPopup = function () {
    var self = this;
    var v = this.ta.value, pos = this.ta.selectionStart;
    var items = null;
    var sm = v.match(/^\/([\w:-]*)$/);
    if (sm && this.commands.length) {
      var q = sm[1].toLowerCase();
      items = this.commands.filter(function (c) { return c.name.toLowerCase().indexOf(q) === 0; }).slice(0, 12)
        .map(function (c) { return { kind: 'cmd', c: c }; });
    } else {
      var mm = v.slice(0, pos).match(/(^|\s)@([^\s@]*)$/);
      if (mm && this.info.spec.project) {
        this.mentionStart = pos - mm[2].length - 1;
        if (!this.files) { this.loadFiles(); items = [{ kind: 'loading' }]; }
        else items = this.matchFiles(mm[2]).map(function (f) { return { kind: 'file', path: f }; });
        if (items && !items.length) items = [{ kind: 'none' }];
      }
    }
    if (!items || !items.length) { this.slashEl.classList.add('hidden'); this.slashItems = []; return; }
    this.slashItems = items;
    this.slashSel = 0;
    this.slashEl.replaceChildren.apply(this.slashEl, items.map(function (it, i) {
      if (it.kind === 'loading') return h('div', { class: 'sl muted' }, 'loading files…');
      if (it.kind === 'none') return h('div', { class: 'sl muted' }, 'no match');
      var row = it.kind === 'cmd'
        ? [h('b', { dir: 'ltr' }, '/' + it.c.name), it.c.argumentHint ? h('span', { class: 'muted', dir: 'ltr' }, ' ' + it.c.argumentHint) : null,
          it.c.description ? h('span', { class: 'sd', dir: 'auto' }, it.c.description) : null]
        : [h('span', { class: 'fi' }, /\/$/.test(it.path) ? '📁' : '📄'), h('span', { class: 'fp', dir: 'ltr' }, it.path)];
      return h('div', { class: 'sl', onmousedown: function (e) { e.preventDefault(); self.pickSlash(i); } }, row);
    }));
    this.slashEl.classList.remove('hidden');
    this.markSlash();
  };

  P.loadFiles = function () {
    var self = this;
    if (this.loadingFiles) return;
    this.loadingFiles = true;
    api('GET', 'files/' + encodeURIComponent(this.info.spec.project)).then(function (d) {
      self.files = d.files || [];
      self.loadingFiles = false;
      if (document.activeElement === self.ta) self.slashPopup();
      setTimeout(function () { self.files = null; }, 120000);          // re-read later (new files)
    }).catch(function (e) { self.loadingFiles = false; self.files = []; fail(e); });
  };

  P.matchFiles = function (q) {
    q = q.toLowerCase();
    var files = this.files || [];
    if (!q) return files.filter(function (f) { return f.split('/').length <= (/\/$/.test(f) ? 2 : 1); }).slice(0, 40);
    var scored = [];
    for (var i = 0; i < files.length && scored.length < 4000; i++) {
      var f = files[i], fl = f.toLowerCase();
      var at = fl.indexOf(q);
      if (at < 0) continue;
      var base = fl.replace(/\/$/, '').split('/').pop();
      var score = base.indexOf(q) === 0 ? 0 : base.indexOf(q) > 0 ? 1 : fl.indexOf(q) === 0 ? 2 : 3;
      scored.push([score, f.length, f]);
    }
    scored.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    return scored.slice(0, 40).map(function (x) { return x[2]; });
  };

  P.markSlash = function () {
    var sel = this.slashSel;
    Array.prototype.forEach.call(this.slashEl.children, function (c, i) { c.classList.toggle('sel', i === sel); });
    var cur = this.slashEl.children[sel];
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  };

  P.pickSlash = function (i) {
    var it = this.slashItems[i];
    if (!it) return;
    if (it.kind === 'cmd') {
      this.ta.value = '/' + it.c.name + ' ';
      this.slashEl.classList.add('hidden');
    } else if (it.kind === 'file') {
      var v = this.ta.value, pos = this.ta.selectionStart;
      var isDir = /\/$/.test(it.path);
      var ins = '@' + it.path + (isDir ? '' : ' ');
      this.ta.value = v.slice(0, this.mentionStart) + ins + v.slice(pos);
      var caret = this.mentionStart + ins.length;
      this.ta.setSelectionRange(caret, caret);
      if (isDir) { this.slashPopup(); this.ta.focus(); return; }   // keep browsing inside the folder
      this.slashEl.classList.add('hidden');
    } else return;
    this.autosize();
    this.ta.focus();
  };

  P.pasteFiles = function (e) {
    var items = (e.clipboardData && e.clipboardData.files) || [];
    var imgs = Array.prototype.filter.call(items, function (f) { return /^image\//.test(f.type); });
    if (!imgs.length) return;
    e.preventDefault();
    this.addFiles(imgs);
  };

  P.addFiles = function (files) {
    var self = this;
    Array.prototype.forEach.call(files, function (f) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(f.type)) { toast('Images only (PNG, JPEG, GIF, WebP)', 'warn'); return; }
      if (f.size > 5 * 1024 * 1024) { toast('Image is larger than 5 MB', 'warn'); return; }
      var r = new FileReader();
      r.onload = function () {
        var data = String(r.result).split(',')[1];
        self.attachments.push({ type: f.type, data: data, url: r.result });
        self.renderAttach();
      };
      r.readAsDataURL(f);
    });
  };

  P.renderAttach = function () {
    var self = this;
    this.attachEl.classList.toggle('hidden', !this.attachments.length);
    this.attachEl.replaceChildren.apply(this.attachEl, this.attachments.map(function (a, i) {
      return h('div', { class: 'thumb' }, h('img', { src: a.url, alt: '', title: 'Click to view', onclick: function () { SW.viewImage(a.url); } }),
        h('button', { title: 'Remove', onclick: function () { self.attachments.splice(i, 1); self.renderAttach(); } }, '×'));
    }));
  };

  P.submit = function () {
    var queued = this.busy;              // Claude is working: the message waits for its turn
    if (!queued) {
      this.turnStart = Date.now();
      this.tokBase = 0; this.msgTok = 0; this.outTokens = 0;
    }
    var text = this.ta.value.trim();
    if (!text && !this.attachments.length) return;
    if (this.gone) { toast('This conversation has ended — start a new one', 'warn'); return; }
    var content = this.attachments.map(function (a) { return { type: 'image', source: { type: 'base64', media_type: a.type, data: a.data } }; });
    if (text) content.push({ type: 'text', text: text });
    if (!this.send({ t: 'send', content: content })) return;
    // show it right away; Claude echoes it back a few seconds later
    var el = h('div', { class: 'umsg pending' + (queued ? ' queued' : '') }, text ? h('div', { class: 'ubody', dir: 'auto' }, text) : null,
      thumbs(this.attachments.map(function (a) { return a.url; })),
      queued ? h('div', { class: 'qlabel', title: 'Claude reads it as soon as it can — usually right after the current step' }, 'queued') : null);
    this.add(el);
    (this.echoes = this.echoes || []).push({ text: text, el: el });
    if (text) { this.history.push(text); this.histPos = -1; }
    this.ta.value = '';
    this.attachments = [];
    this.renderAttach();
    this.autosize();
    this.slashEl.classList.add('hidden');
    this.stick = true;
    this.setBusy(true);
    if (!queued) this.setPhase('work', 'sending…');
    else this.renderStatus();
  };

  P.interrupt = function () { this.send({ t: 'interrupt' }); };

  // ------------------------------------------------------------ tab actions
  P.restart = function () {
    var self = this;
    api('POST', 'sessions/' + this.id + '/restart').then(function (info) { self.info = info; self.alive = true; }).catch(fail);
  };
  P.stop = function () { this.interrupt(); };

  P.newChat = function () {
    var self = this;
    var go = function () { api('POST', 'sessions/' + self.id + '/new-chat').then(function (info) { self.info = info; self.focus(); }).catch(fail); };
    if (this.busy) SW.confirmDlg('Claude is working. Start a new conversation anyway?', 'New conversation', true).then(function (ok) { if (ok) go(); });
    else go();
  };

  P.historyMenu = function (anchor) {
    var self = this;
    SW.pickClaudeSession(this.info.spec.project, function (sid, title) {
      SW.openSession({ kind: 'chat', project: self.info.spec.project, resume: sid, title: title ? 'Claude · ' + title : '' });
    }, anchor);
  };

  P.menu = function (x, y, extra) {
    var self = this;
    SW.showMenu(x, y, [
      { label: 'Rename…', action: function () { SW.renameTab(self); } },
      { label: 'New conversation', action: function () { self.newChat(); } },
      { label: 'Past conversations…', action: function () { self.historyMenu(); } },
      { label: 'Restart Claude (same conversation)', action: function () { self.restart(); } },
      { label: 'Copy whole conversation', action: function () { navigator.clipboard.writeText(self.msgs.innerText).then(function () { toast('Copied', 'ok'); }).catch(fail); } },
      { label: 'Open in Claude’s text terminal (TUI)', action: function () { SW.claudeInTerminal(self.info.spec.project, self.sessionId || self.info.spec.resume || '', self.info.title.replace(/^Claude · /, '')); } },
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { SW.closeTab(self); } }
    ].concat(extra || SW.layoutItemsFor(self)));
  };

  P.dispose = function () {
    this.closed = true;
    clearInterval(this.timer);
    if (this.ws) try { this.ws.close(); } catch (e) { /* ignore */ }
    this.el.remove();
    this.pane.remove();
  };

  window.SWChat = { ChatTab: ChatTab, markdown: markdown, fmtTime: fmtTime };
})();
