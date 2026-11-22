/* simabworkspace — Claude chat panel (like the Claude Code VS Code extension).
   Talks to ChatSession in server.py: Claude runs on the server in stream-json mode. */
'use strict';
(function () {
  var SW = window.SW;
  var h = SW.h, api = SW.api, toast = SW.toast, fail = SW.fail, S = SW.S, faNum = SW.faNum;

  var AWAY_MS = 3000;
  var STUCK_MS = 60000;          // offer Interrupt / Restart after this long without a sign of life          // hide the message box after this long away from the chat
  var MODES = [
    ['default', 'ask', 'Asks before editing files or running commands'],
    ['acceptEdits', 'auto-edit', 'Edits files without asking; still asks for commands'],
    ['plan', 'plan', 'Reads and plans only; changes nothing'],
    ['auto', 'auto', 'Does low-risk things itself, asks for the rest']
  ];
  // until Claude sends its own list: the current models (no vague "default")
  var MODELS = [['opus', 'Opus 5.5'], ['fable', 'Fable 5.2'], ['sonnet', 'Sonnet 5.5'], ['haiku', 'Haiku 5.5']];
  var EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  function modelLabel(sel, v) {
    var o = Array.prototype.filter.call(sel.options || [], function (x) { return x.value === v; })[0];
    return o ? o.textContent : v;
  }
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
        var code = [], end = fence[1], fin = l.length - l.replace(/^ +/, '').length;
        i++;
        while (i < lines.length && lines[i].trim().indexOf(end) !== 0) {
          var cl0 = lines[i], k0 = 0;
          while (k0 < fin && cl0[k0] === ' ') k0++;
          code.push(cl0.slice(k0)); i++;
        }
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
          // a code block inside a list item (indented ```): a real code block with copy, not text
          var lf = !mm && lines[i].match(/^(\s+)(```+|~~~+)\s*([\w+#.-]*)\s*$/);
          if (lf) {
            var ind = lf[1].length, cl = [];
            i++;
            while (i < lines.length && lines[i].trim().indexOf(lf[2]) !== 0) {
              var ln = lines[i], k = 0;
              while (k < ind && ln[k] === ' ') k++;
              cl.push(ln.slice(k)); i++;
            }
            i++;
            var cbk = codeBlock(cl.join('\n'), lf[3]);
            if (list.lastChild) list.lastChild.append(cbk); else root.append(cbk);
            continue;
          }
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

  // Local only projects: Grep / Glob / Read / Edit / Write run on the user's computer (MCP server "pc"
  // of onpc) — shown as Claude's own cards
  function pcTool(n) { return String(n || '').replace(/^mcp__pc__(Grep|Glob|Read|Edit|Write)$/, '$1'); }

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
    // Only scrolling done by the user moves the view. The browser also scrolls by itself (to the top
    // when the chat is put back into the page: switching or moving tabs, splitting, a reconnect…);
    // that must not unpin the end or load older messages.
    this.userScrollAt = 0;
    this.savedTop = 0;
    var byUser = function (e) {
      self.userScrollAt = Date.now();
      if (e && e.type === 'wheel' && e.deltaY < 0 && self.scroller.scrollTop < 80) self.loadOlder();   // short chats do not scroll
    };
    ['wheel', 'touchmove', 'pointerdown', 'keydown'].forEach(function (ev) { self.scroller.addEventListener(ev, byUser, { passive: true }); });
    this.scroller.addEventListener('scroll', function () {
      var s = self.scroller;
      if (Date.now() - self.userScrollAt < 1500) {
        if (s.scrollTop < 80) self.loadOlder();
        self.stick = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
        self.savedTop = s.scrollTop;
        self.jumpBtn.classList.toggle('hidden', self.stick);
        return;
      }
      if (self.stick) { if (s.scrollHeight - s.scrollTop - s.clientHeight > 2) s.scrollTop = s.scrollHeight; }
      else if (s.scrollTop === 0 && self.savedTop > 0) s.scrollTop = self.savedTop;        // put back where you were
      else self.savedTop = s.scrollTop;
    });
    this.scroller.addEventListener('contextmenu', function (e) {
      if (window.getSelection && String(window.getSelection())) return;      // keep the browser menu for copying
      e.preventDefault();
      self.menu(e.clientX, e.clientY, undefined, e.target);
    });
    this.stick = true;
    this.jumpBtn = h('button', { class: 'jump hidden', title: 'Jump to the end', onclick: function () { self.stick = true; self.scrollEnd(); } }, '↓');

    // composer: everything for the chat lives here
    var touch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    this.touch = touch;
    this.ta = h('textarea', { rows: 1, dir: 'auto', enterkeyhint: touch ? 'enter' : 'send',
      placeholder: touch ? 'Message Claude  ·  @ file  / command' : 'Message Claude   ·   @ file   / command   Esc stop' });
    this.ta.addEventListener('keydown', function (e) { self.key(e); });
    this.ta.addEventListener('input', function () { self.autosize(); self.slashPopup(); self._updateActionBtn(); });
    this.ta.addEventListener('click', function () { self.slashPopup(); });
    this.ta.addEventListener('blur', function () { setTimeout(function () { self.slashEl.classList.add('hidden'); }, 150); });
    this.ta.addEventListener('paste', function (e) { self.pasteFiles(e); });
    this.ta.addEventListener('dragover', function (e) { if (hasImages(e)) { e.preventDefault(); self.ta.classList.add('drag-over'); } });
    this.ta.addEventListener('dragleave', function () { self.ta.classList.remove('drag-over'); });
    this.ta.addEventListener('drop', function (e) { self.ta.classList.remove('drag-over'); if (!hasImages(e)) return; e.preventDefault(); self.addFiles(e.dataTransfer.files); });
    this.attachEl = h('div', { class: 'attach hidden' });
    // dictation: its own mic in the composer bar (voice.js); the live text grows the box
    this.micBtn = window.SWVoice ? h('button', { class: 'cb-mic' }) : null;
    if (this.micBtn) {
      this.voice = window.SWVoice.attach(this.ta, { button: this.micBtn, notify: function (m) { toast(m, 'warn'); },
        onWrite: function () { self.autosize(); self._updateActionBtn(); } });
    }
    this.slashEl = h('div', { class: 'slash hidden' });
    this.modeSel = h('select', { class: 'mini-sel mode', title: 'Permission mode (Shift+Tab)', onchange: function () { self.send({ t: 'mode', mode: self.modeSel.value }); } },
      MODES.map(function (m) { return h('option', { value: m[0], title: m[2] }, m[1]); }));
    this.modelSel = h('select', { class: 'mini-sel model', title: 'Model', onchange: function () { self.send({ t: 'model', model: self.modelSel.value }); } },
      MODELS.map(function (m) { return h('option', { value: m[0] }, m[1]); }));
    this.effortSel = h('select', { class: 'mini-sel effort', title: 'Thinking effort', onchange: function () { self.send({ t: 'effort', effort: self.effortSel.value }); } },
      EFFORTS.map(function (e) { return h('option', { value: e }, e); }));
    this.effortSel.value = 'medium';
    // phones: the three selects fold into one chip ("auto · Opus 5.5 · medium") that opens a menu
    this.optsBtn = h('button', { class: 'cb-opts', title: 'Mode, model and effort', onclick: function () { self.optsMenu(); } });
    [this.modeSel, this.modelSel, this.effortSel].forEach(function (sel) { sel.addEventListener('change', function () { self.paintOpts(); }); });
    this.actionBtn = h('button', { class: 'send', title: 'Send (Enter)', onclick: function () {
      if (self._stopMode) self.interrupt(); else self.submit();
    } }, '↑');
    this.stDot = h('i', { class: 'dot' });
    this.stText = h('span', { class: 'st-text' }, 'connecting…');
    this.stExtra = h('span', { class: 'st-extra' });
    // stuck? (Claude silent for a long time while working, or messages waiting in the queue)
    this.stuckEl = h('span', { class: 'stuck hidden' },
      h('button', { class: 'link', title: 'Stop what Claude is doing now — the conversation stays', onclick: function () { self.interrupt(); toast('Interrupt sent', 'ok'); } }, 'Interrupt'),
      h('button', { class: 'link', title: 'Start Claude again on the same conversation (queued messages are dropped — send them again)', onclick: function () {
        SW.confirmDlg('Restart Claude on the same conversation? Use this when it is stuck. Queued messages are dropped — send them again after.', 'Restart').then(function (ok) {
          if (ok) { self.echoes = []; self.restart(); }
        });
      } }, 'Restart Claude'));
    this.statusEl = h('div', { class: 'cstatus', title: 'Live status' }, this.stDot, this.stText, this.stExtra, this.stuckEl);
    this.conn = 'connecting';
    this.timer = setInterval(function () {
      self.updateAway();
      if (self.turnStart || self.conn !== 'online') self.renderStatus();
      if (self.agentsEl && Object.keys(self.agents || {}).length) self.renderAgents();
    }, 1000);
    this.agentsEl = h('div', { class: 'agents hidden' });
    this.devsEl = h('div', { class: 'pc-devs hidden' });      // dev servers of this project on the user's computer
    this.watchBar = h('div', { class: 'watchbar hidden' });
    var composer = this.composerEl = h('div', { class: 'chat-composer' }, this.slashEl, this.devsEl, this.agentsEl, this.statusEl, this.watchBar, this.attachEl,
      h('div', { class: 'cbox' }, this.ta,
        h('div', { class: 'cbar' },
          this.modeSel, this.modelSel, this.effortSel, this.optsBtn,
          h('label', { class: 'clip', title: 'Attach image' }, '+', h('input', { type: 'file', accept: 'image/*', multiple: true, class: 'hidden', onchange: function (e) { self.addFiles(e.target.files); e.target.value = ''; } })),
          h('span', { class: 'grow' }),
          h('button', { class: 'link cb-hist', title: 'Past conversations', onclick: function () { self.historyMenu(); } }, h('span', { class: 'lbl' }, 'history'), h('span', { class: 'ico' }, '⟲')),
          h('button', { class: 'link cb-new', title: 'Start a new conversation in this tab', onclick: function () { self.newChat(); } }, h('span', { class: 'lbl' }, 'new'), h('span', { class: 'ico' }, '✎')),
          this.micBtn, this.actionBtn)));

    this.linkEl = h('div', { class: 'chat-link hidden' });      // Local only: this chat's line to the computer
    document.addEventListener('sw-pc-net', function (ev) { self.paintLink(ev.detail); });
    this.topRight = h('div', { class: 'chat-tr' }, this.linkEl);   // top right, side by side: line badge, where chip
    this.body = h('div', { class: 'pbody chat-body' }, this.scroller, this.jumpBtn, this.topRight);
    this.pane = h('div', { class: 'pane hidden-pane chat' }, this.body, composer);
    // a narrow chat (tablet split, laptop side pane) gets the phone's one chip instead of three selects
    if (window.ResizeObserver) new window.ResizeObserver(function () { self.fitBar(); }).observe(composer);
    this.pane.addEventListener('mouseenter', function () { self.hover = true; self.updateAway(); });
    this.pane.addEventListener('mouseleave', function () { self.hover = false; });
    // a tap on the chat makes it the active one: its message box comes back at once
    this.pane.addEventListener('pointerdown', function () { setTimeout(function () { self.updateAway(); }, 0); });
    this.applyInfo();
    this.connect();
  }

  var P = ChatTab.prototype;

  P.focus = function () { this.awaySince = 0; this.setAway(false); this.ta.focus(); };
  P.ensureOpen = function () {
    var self = this;
    this.el.classList.remove('done');
    this.awaySince = 0;
    this.setAway(false);
    this.restoreScroll();
    requestAnimationFrame(function () { self.restoreScroll(); });
  };
  P.doFit = function () { this.restoreScroll(); };
  P.toggleFa = function () {};
  P.renderFa = function () {};
  P.toggleComposer = function () { this.ta.focus(); };

  P.send = function (msg) {
    if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
    toast('This tab is not connected', 'warn');
    return false;
  };

  // a socket that is no longer the tab's own: it must not deliver anything or reconnect
  function retireWs(ws) {
    if (!ws) return;
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch (e) { /* already closed */ }
  }

  P.connect = function () {
    var self = this;
    if (this.closed || this.gone) return;
    // only ONE socket per tab: two reconnect paths (the onclose timer and "connection back")
    // could both open one, and every event then showed twice in the chat
    if (this.ws && !this.ws.swDead && this.ws.readyState <= 1) return;
    retireWs(this.ws);
    var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?id=' + encodeURIComponent(this.id) + '&t=' + encodeURIComponent(SW.token));
    this.ws = ws;
    ws.onopen = function () { if (self.ws !== ws) return; self.retry = 0; self.lastRx = Date.now(); self.pingAt = 0; self.conn = 'online'; self.renderStatus(); };
    ws.onmessage = function (ev) {
      var m;
      if (self.ws !== ws) { retireWs(ws); return; }
      self.lastRx = Date.now();
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
        else if (st.linking) self.setPhase('work', 'reconnecting to Claude on the server…');
        else if (st.busy) self.setPhase('work', 'working…');
        else if (!st.alive) self.setPhase('idle', 'not started — send a message');
        else self.setPhase('idle', 'ready');
        self.stick = true;
        self.scrollEnd();
        self.prefillStarter();
      } else if (m.t === 'e') {
        self.onEvent(m.e);
      } else if (m.t === 'u') {
        window.dispatchEvent(new CustomEvent('sw-usage'));
      } else if (m.t === 'link') {
        // the relay answered: Claude still runs (maybe in the middle of work) or it was started
        self.setBusy(m.busy);
        if (Object.keys(self.perms).length) self.setPhase('wait', 'waiting for you');
        else if (m.busy) self.setPhase('work', 'working… (picked up again)');
        else if (self.phaseKind === 'work' && /^reconnecting/.test(self.phaseText || '')) self.setPhase('idle', 'ready');
      } else if (m.t === 'busy') {
        self.setBusy(m.busy);
        if (!m.busy && self.phaseKind === 'work') self.setPhase('idle', 'ready');
      } else if (m.t === 'clear') {
        self.reset();
        self.sessionId = '';                       // a new conversation: the old one is gone from this tab
        if (self.info.spec) delete self.info.spec.resume;
        self.prefillStarter();
      } else if (m.t === 'title') {
        // Claude named the conversation (or it was renamed): the tab shows that name, like in VS Code
        self.info.title = m.title;
        self.applyInfo();
        if (SW.renderTitle) SW.renderTitle();
      } else if (m.t === 'gone') {
        self.gone = true;
      }
    };
    ws.onclose = function () {
      ws.swDead = true;
      if (self.closed || self.gone || self.ws !== ws) return;
      self.conn = 'offline';
      self.renderStatus();
      self.retry++;
      setTimeout(function () {
        if (self.ws !== ws) return;               // another path already reconnected
        api('GET', 'state').then(function (st) {
          if (self.ws !== ws) return;
          if (st.sessions.some(function (s) { return s.id === self.id; })) self.connect();
          else self.reopen();
        }).catch(function () { self.connect(); });
      }, Math.min(5000, 300 * self.retry));
    };
  };

  // The app was restarted (an update, the web server restarted): this tab's session is gone,
  // but Claude keeps running on the server under its relay. Open the same conversation again
  // and keep using this tab.
  P.reopen = function () {
    var self = this, sp = this.info.spec || {};
    var conv = this.sessionId || sp.resume;
    function ended() { self.gone = true; self.conn = 'gone'; self.renderStatus(); self.system('This conversation is no longer running. Use “history” to continue it.', 'err'); }
    if (!conv || sp.watch || this.closed) { ended(); return; }
    var spec = { kind: 'chat', project: sp.project || '', resume: conv, title: this.info.title };
    if (sp.relay) spec.relay = sp.relay;
    var wid = SW.wid ? SW.wid() : '';
    if (wid) spec.win = wid;
    api('POST', 'sessions', spec).then(function (info) {
      if (self.closed) return;
      self.id = info.id;
      self.info = info;
      self.retry = 0;
      self.applyInfo();
      if (SW.saveLayout) SW.saveLayout();
      self.connect();
    }).catch(function (e) {
      if (self.closed) return;
      if (e && e.status && e.status < 500) ended();
      else setTimeout(function () { self.reopen(); }, 3000);
    });
  };

  P.reportError = function (err, ev) {
    console.error(err, ev);
    if (SW.reportError) SW.reportError('chat: ' + (err && err.message), 'event ' + ((ev && (ev.type || ev.t)) || '?'), err && err.stack);
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
    this.savedTop = sc.scrollTop;
  };

  P.renderAgents = function () {
    var self = this;
    var ids = Object.keys(this.agents);
    this.agentsEl.classList.toggle('hidden', !ids.length);
    if (!ids.length) { this.agentsEl.replaceChildren(); return; }
    this.agentsEl.replaceChildren.apply(this.agentsEl, [h('div', { class: 'ag-head' }, ids.length + (ids.length === 1 ? ' agent' : ' agents') + ' working')].concat(ids.map(function (id) {
      var a = self.agents[id];
      return h('div', { class: 'ag' + (a.bg ? ' bg' : ''), title: a.bg ? 'Working in the background' : 'Show it in the conversation', onclick: function () {
        if (!a.card) return;
        a.card.classList.add('open');
        if (a.card.scrollIntoView) a.card.scrollIntoView({ block: 'center' });
      } },
        h('i', { class: 'spin' }), h('b', { dir: 'auto' }, a.title), h('span', { class: 'ag-type' }, a.type),
        h('span', { class: 'ag-last', dir: 'auto' }, a.last), h('span', { class: 'ag-time' }, fmtDur(Date.now() - a.start) + (a.steps ? ' · ' + a.steps + ' steps' : '')));
    })));
  };

  // all=false (turn ended): background agents keep working after the turn, so they stay listed
  P.clearAgents = function (all) {
    var self = this, ids = Object.keys(this.agents || {}), gone = 0;
    ids.forEach(function (id) { if (all || !self.agents[id].bg) { delete self.agents[id]; gone++; } });
    if (gone) { this.renderAgents(); this.renderStatus(); }
  };

  // Background agents (Agent run_in_background): their tool result comes back at once, so they are
  // followed through Claude's system task_started / task_progress / task_notification events instead.
  P.onTask = function (e) {
    var id = e.tool_use_id || e.task_id;
    if (!id || e.task_type === 'local_bash') return;
    var a = this.agents[id];
    if (e.subtype === 'task_notification') {
      if (a) { delete this.agents[id]; this.renderAgents(); this.renderStatus(); }
      return;
    }
    var u = e.usage || {};
    if (!a) {
      a = this.agents[id] = { title: e.description || e.subagent_type || 'agent', type: e.subagent_type || e.task_type || 'agent',
        start: Date.now(), last: 'starting…', steps: 0, card: this.cards[id] || null };
    }
    a.bg = a.bg || e.is_backgrounded !== false;
    if (u.duration_ms) a.start = Date.now() - u.duration_ms;
    if (u.tool_uses) a.steps = u.tool_uses;
    if (e.summary || e.last_tool_name) a.last = String(e.summary || e.last_tool_name).split('\n')[0].slice(0, 90);
    this.renderAgents();
    this.renderStatus();
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
    this.model = st.model || 'opus';
    this.effort = st.effort || 'medium';
    this.effortSel.value = this.effort;
    this.paintOpts();
    this.setBusy(st.busy);
    if (st.init) this.applyInit(st.init);
    if (st.watch) this.setWatch(st.watch);
    this.setModelValue(this.model);
    this.applyInfo();
  };

  P.applyInit = function (init) {
    var self = this;
    if (Array.isArray(init.commands)) this.commands = init.commands.map(function (c) { return typeof c === 'string' ? { name: c } : c; });
    if (Array.isArray(init.models) && init.models.length) {
      var cur = this.model || this.modelSel.value || 'opus';
      var list = init.models.filter(function (m) { return m.value && m.value !== 'default'; });
      if (list.length) {
        this.modelSel.replaceChildren.apply(this.modelSel, list.map(function (m) {
          return h('option', { value: m.value, title: m.description || '' }, m.displayName || m.value);
        }));
      }
      this.setModelValue(cur);
    }
    void self;
  };

  P.setModelValue = function (v) {
    v = v && v !== 'default' ? v : 'opus';
    this.modelSel.value = v;
    if (this.modelSel.value !== v) { this.modelSel.append(h('option', { value: v }, v)); this.modelSel.value = v; }
    this.paintOpts();
  };

  // the phone chip: what is chosen now, coloured like the mode select
  function selText(sel) {
    var o = Array.prototype.filter.call(sel.children || [], function (x) { return x.value === sel.value; })[0];
    return o ? o.textContent : sel.value;
  }
  // the three selects + buttons need ~600px; below that one chip, like on phones
  P.fitBar = function () {
    var w = this.composerEl.clientWidth;
    if (w) this.composerEl.classList.toggle('cb-narrow', w < 600);
  };
  P.paintOpts = function () {
    if (!this.optsBtn) return;
    this.optsBtn.className = 'cb-opts m-' + this.modeSel.value;
    this.optsBtn.replaceChildren(
      h('span', { class: 'o-mode' }, selText(this.modeSel)),
      h('span', { class: 'o-model' }, selText(this.modelSel)),
      h('span', { class: 'o-effort' }, selText(this.effortSel)), h('span', { class: 'o-car' }, '▾'));
  };
  P.optsMenu = function () {
    var self = this;
    var items = [];
    [['Mode', this.modeSel, 'mode'], ['Model', this.modelSel, 'model'], ['Effort', this.effortSel, 'effort']].forEach(function (g, i) {
      if (i) items.push('-');
      items.push({ header: g[0] });
      Array.prototype.forEach.call(g[1].children, function (o) {
        items.push({ label: (o.value === g[1].value ? '✓ ' : '   ') + o.textContent, action: function () {
          if (g[1].value === o.value) return;
          g[1].value = o.value;
          var m = { t: g[2] }; m[g[2]] = o.value;
          self.send(m);
          self.paintOpts();
        } });
      });
    });
    var r = this.optsBtn.getBoundingClientRect();
    SW.showMenu(r.left, r.top, items);
    var menu = document.querySelector('#menu-root .menu');     // open upwards, just above the chip
    if (menu) menu.style.top = Math.max(4, r.top - menu.getBoundingClientRect().height - 6) + 'px';
  };

  P.setMode = function (mode) {
    if (!mode) return;
    this.mode = mode;
    this.modeSel.value = mode;
    this.modeSel.className = 'mini-sel mode m-' + mode;
    this.paintOpts();
  };

  // the user can see this chat now: it is the active tab of its section and the window is in front
  P.inView = function () {
    var g = this.group;
    var front = typeof document.hasFocus === 'function' ? document.hasFocus() : true;
    return (SW.S.active === this || !!(g && g.active === this)) && front && !document.hidden;
  };
  // done: finished while you looked elsewhere (a steady dot) · bell: Claude waits for you (blinks)
  P.mark = function (kind) {
    if (this.replaying || this.inView()) return false;
    if (kind === 'bell') this.el.classList.remove('done');
    if (kind === 'done' && this.el.classList.contains('bell')) return true;
    this.el.classList.add(kind);
    return true;
  };

  P.setBusy = function (b) {
    this.busy = !!b;
    if (this.busy) this.el.classList.remove('done');
    this.el.classList.toggle('busy', this.busy);
    this._updateActionBtn();
  };

  P._updateActionBtn = function () {
    var stopMode = this.busy && !this.ta.value.trim();
    this._stopMode = stopMode;
    this.actionBtn.textContent = stopMode ? '■' : '↑';
    this.actionBtn.className = stopMode ? 'stopbtn' : 'send';
    this.actionBtn.title = stopMode ? 'Stop (Esc)' : 'Send (Enter)';
  };

  P.applyInfo = function () {
    this.ttlEl.textContent = this.info.title;
    this.el.title = this.info.title + (this.info.spec.project ? ' — ' + this.info.spec.project : '');
  };

  P.scrollEnd = function () {
    if (this.stick) this.scroller.scrollTop = this.scroller.scrollHeight;
  };

  // back in view (tab switched to, moved, layout changed): at the end, or where you were reading
  P.restoreScroll = function () {
    var sc = this.scroller;
    if (this.stick) sc.scrollTop = sc.scrollHeight;
    else if (this.savedTop && sc.scrollTop !== this.savedTop) sc.scrollTop = this.savedTop;
    this.jumpBtn.classList.toggle('hidden', this.stick);
  };

  // The message box hides when you are not working in this chat for 3 seconds (another tab or
  // section is active, or the window is in the background) and the mouse is not over it.
  // It comes back at once when you return. Tablets too (only the active chat keeps its box;
  // a touch screen has no hover); not on phones — one section, the chat you see is the active one.
  P.isPresent = function () {
    var S = SW.S, b = document.body;
    if (b && b.classList.contains('compact')) return true;
    if (this.composerEl.contains(document.activeElement)) return true;
    if (this.touch) return S.active === this && !document.hidden;
    if (this.hover) return true;
    var focused = typeof document.hasFocus === 'function' ? document.hasFocus() : true;
    return S.active === this && focused;
  };
  P.updateAway = function () {
    if (this.isPresent()) { this.awaySince = 0; this.setAway(false); return; }
    if (!this.awaySince) this.awaySince = Date.now();
    if (Date.now() - this.awaySince >= AWAY_MS) this.setAway(true);
  };
  P.setAway = function (on) {
    if (!!this.away === on) return;
    this.away = on;
    this.pane.classList.toggle('away', on);
    this.restoreScroll();          // the chat got taller / shorter: stay at the end
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
    var conn = this.conn === 'online' ? null : this.conn === 'connecting' ? 'connecting…'
      : this.conn === 'gone' ? 'not running' : 'no connection — reconnecting…';
    // no connection: nothing can be sent, so the message box says so (the text you type is kept)
    var off = !!conn && !this.watching;
    if (this.composerEl) this.composerEl.classList.toggle('offline', off);
    if (this.actionBtn) this.actionBtn.disabled = off;
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
    // no sign of life for a minute while it should be working, or queued messages not taken
    var quiet = this.lastEventAt ? Date.now() - this.lastEventAt : (this.turnStart ? Date.now() - this.turnStart : 0);
    var stuck = !conn && !this.watching && (kind === 'work' || waiting > 0) && quiet > STUCK_MS;
    this.stuckEl.classList.toggle('hidden', !stuck);
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
        this.setPhase('work', cb.type === 'text' ? 'writing…' : cb.type === 'tool_use' ? 'preparing ' + pcTool(cb.name) + '…' : 'thinking…');
      } else if (ev.type === 'message_delta' && ev.usage && ev.usage.output_tokens) {
        this.msgTok = ev.usage.output_tokens;
        this.outTokens = (this.tokBase || 0) + this.msgTok;
        this.renderStatus();
      } else if (ev.type === 'message_stop') { this.tokBase = (this.tokBase || 0) + (this.msgTok || 0); this.msgTok = 0; }
    } else if (t === 'assistant' && !e.parent_tool_use_id) {
      var tools = ((e.message || {}).content || []).filter(function (b) { return b.type === 'tool_use'; });
      if (tools.length) { var b = tools[tools.length - 1]; this.setPhase('work', 'running ' + pcTool(b.name) + toolHint(this, b.input)); }
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
          this.topEl = this.system('', 'mark top');
          this.setTopLabel();
          e.entries.forEach(function (x) { if (!x.isMeta) self.onEvent(x); });
          this.system('— resumed —', 'mark');
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
        } else if (/^task_(started|progress|notification)$/.test(e.subtype || '')) {
          this.onTask(e);
        } else if (e.subtype === 'compact_boundary') {
          this.system('— conversation compacted —', 'mark');
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
      case '_watch': this.setWatch(e); break;
      case '_mode': this.setMode(e.mode); break;
      case '_pc':                          // the computer of this chat (computers.js ⚒ chip)
        if (this.info && this.info.spec) { if (e.pc) this.info.spec.pc = e.pc; else delete this.info.spec.pc; }
        if (window.SWComputers) SWComputers.decorate();
        break;
      case '_model': this.model = e.model || 'opus'; this.setModelValue(this.model); if (!this.replaying) this.system('model: ' + modelLabel(this.modelSel, this.model)); break;
      case '_effort': this.effort = e.effort; this.effortSel.value = e.effort; this.paintOpts(); if (!this.replaying) this.system('effort: ' + e.effort); break;
      case '_stderr': this.stderr.push(e.text); if (this.stderr.length > 200) this.stderr.shift(); break;
      case '_error': this.system(e.text, 'err'); break;
      case '_restart': this.system('— restarted —', 'mark'); break;
      case '_reattached': this.system(e.busy ? '— Claude kept working on the server; following it again —' : '— reconnected to the same Claude —', 'mark'); break;
      case '_reconnect':
        if (this.replaying) break;
        this.system('Connection to the server lost — Claude keeps working there. Reconnecting…', 'mark');
        this.setPhase('work', 'reconnecting to Claude on the server…');
        break;
      case '_reconnected':
        if (this.replaying) break;
        this.system(e.running ? 'Reconnected — picked up where it was' : 'Reconnected', 'mark');
        if (/^reconnecting/.test(this.phaseText || '')) this.setPhase(this.busy ? 'work' : 'idle', this.busy ? 'working…' : 'ready');
        break;
      case '_exit':
        this.alive = false;
        this.clearAgents(true);
        this.setBusy(false);
        if (e.code !== 0 && e.code !== null) {
          var box = this.system('Claude stopped (exit ' + e.code + '). Your next message starts it again and continues the conversation.', 'err');
          if (this.stderr.length) box.append(h('details', null, h('summary', null, 'details'), preBox(this.stderr.slice(-40).join('\n'))));
          if (e.code === 97) box.append(h('div', null, 'The project folder was not found on the server.'));
          if (e.code === 98) box.append(h('div', null, 'This project lives on your computer and it is not connected: open Simab Workspace there (⚙ → Computers → Connect this computer).'));
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
      else if (cb.type === 'tool_use') el = h('div', { class: 'tool-card pending' }, h('div', { class: 'tool-head' }, h('span', { class: 'tname' }, TOOL_FA[pcTool(cb.name)] || pcTool(cb.name)), h('i', { class: 'spin' })));
      if (el) { el.dataset.draft = '1'; el.dataset.type = cb.type; el._pending = ''; g.append(el); this.draft.blocks[ev.index] = { el: el, type: cb.type, done: false }; this.scrollEnd(); }
    } else if (ev.type === 'content_block_delta') {
      var b = this.draft.blocks[ev.index];
      if (!b) return;
      var d = ev.delta || {};
      if (d.type === 'text_delta') { this.liveMarkdown(b.el, d.text); this.turnText = true; }
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
    if (el._mdTimer) {
      clearTimeout(el._mdTimer); el._mdTimer = null;
      if (el._fullText !== undefined) el.replaceChildren(markdown(el._fullText));
    }
    if (el._onDone) { var f = el._onDone; el._onDone = null; f(); }
    this.scrollEnd();
  };

  // Stream text as markdown incrementally: re-render every 80 ms so headings, bold etc.
  // appear in formatted form while typing, not all at once at the end.
  P.liveMarkdown = function (el, text) {
    el._fullText = (el._fullText || '') + text;
    var self = this;
    if (window.SWChat.instant) {
      el.replaceChildren(markdown(el._fullText));
      self.scrollEnd();
      return;
    }
    if (el._mdTimer) return;
    el._mdTimer = setTimeout(function () {
      el._mdTimer = null;
      el.replaceChildren(markdown(el._fullText));
      self.scrollEnd();
      if (el._onDone) { var f = el._onDone; el._onDone = null; f(); }
    }, 80);
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
    if (b.type === 'text') {
      if (!b.text || !b.text.trim()) return null;
      this.turnText = true;
      (this.turnSpeech = this.turnSpeech || []).push(b.text);
      var md = markdown(b.text);
      md._md = b.text;                                   // "Read aloud" reads the words, not the layout
      return md;
    }
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
      if (draft && (draft._pending || draft._timer || draft._mdTimer)) draft._onDone = put;     // finish typing first
      else put();
    });
    if (msg.model && !this.replaying) this.modelName = msg.model;
    this.scrollEnd();
  };

  // a tiny PC / server tag on Bash cards of a chat whose work runs on the user's computer (computers.js)
  P.whereTag = function (name, input) {
    if (name !== 'Bash' || !window.SWComputers || !SWComputers.bashWhere) return null;
    var w = SWComputers.bashWhere(this, (input || {}).command);
    return w ? h('span', { class: 'where-tag ' + w, title: w === 'pc' ? 'Ran on your computer' : 'Ran on the server' }, w === 'pc' ? 'PC' : 'server') : null;
  };

  P.toolCard = function (b) {
    var self = this;
    var name = pcTool(b.name || '?');
    var detail = toolDetail(this, name, b.input);
    var body = h('div', { class: 'tool-body' }, detail);
    var resBox = h('div', { class: 'tool-result hidden' });
    var status = h('i', { class: 'spin' });
    var open = name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'TodoWrite';
    var card = h('div', { class: 'tool-card t-' + name + (open ? ' open' : '') },
      h('div', { class: 'tool-head', onclick: function () { card.classList.toggle('open'); } },
        h('span', { class: 'tname' }, TOOL_FA[name] || name), this.whereTag(name, b.input), h('span', { class: 'tsum' }, toolSummary(this, name, b.input)), status),
      body, resBox);
    card._status = status;
    card._res = resBox;
    card._sub = null;
    this.cards[b.id] = card;
    if (/^(Write|Edit|MultiEdit|NotebookEdit)$/.test(name) && b.input) {      // remembered for "N files changed" after the turn
      var fp = b.input.file_path || b.input.notebook_path;
      if (typeof fp === 'string' && fp) (this.turnFiles = this.turnFiles || {})[fp] = 1;
    }
    if ((name === 'Task' || name === 'Agent') && !this.replaying) {
      var inp = b.input || {};
      this.agents[b.id] = { title: inp.description || inp.subagent_type || 'agent', type: inp.subagent_type || 'agent',
        start: Date.now(), last: 'starting…', steps: 0, card: card };
      this.renderAgents();
    }
    if (name === 'TodoWrite') this.todos = b.input && b.input.todos;
    if (name === 'Skill') this.lastSkill = card;
    card._name = name;
    void self;
    return card;
  };

  P.subagent = function (parentId, msg) {
    var ag = this.agents[parentId];
    if (ag) {
      var self0 = this;
      (msg.content || []).forEach(function (b) {
        if (b.type === 'tool_use') { ag.steps++; ag.last = pcTool(b.name) + toolHint(self0, b.input); }
        else if (b.type === 'text' && b.text && b.text.trim()) ag.last = b.text.trim().split('\n')[0].slice(0, 90);
      });
      this.renderAgents();
    }
    var card = this.cards[parentId];
    if (!card) return;
    if (!card._sub) { card._sub = h('div', { class: 'subagent' }); card.querySelector('.tool-body').append(card._sub); }
    var self = this;
    (msg.content || []).forEach(function (b) {
      if (b.type === 'tool_use') card._sub.append(h('div', { class: 'subline' }, '↳ ', h('b', null, TOOL_FA[pcTool(b.name)] || pcTool(b.name)), ' ', toolSummary(self, pcTool(b.name), b.input)));
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
        var ag = self.agents[b.tool_use_id];
        if (ag && ((e.tool_use_result || {}).isAsync || /^Async agent launched/.test(resultText(b.content) || ''))) {
          ag.bg = true; if (ag.last === 'starting…') ag.last = 'working in the background'; self.renderAgents();
        } else if (ag) { delete self.agents[b.tool_use_id]; self.renderAgents(); }
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
    // not typed by the user: an agent's report, a skill's instructions, a system note —
    // shown folded into the card it belongs to, opened with a click (like a command's output)
    if (texts.length && this.isSynthetic(e, texts.join('\n\n'))) {
      this.attachSynthetic(e, texts.join('\n\n'));
      return;
    }
    if (texts.length || images) {
      this.turnText = false;
      this.turnSpeech = [];
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

  var AGENT_RE = /^\s*(?:Another Claude session sent a message:\s*)?<agent-message\b[^>]*?(?:from="([^"]*)")?[^>]*>/;
  var SKILL_RE = /^\s*Base directory for this skill:/;
  P.isSynthetic = function (e, text) {
    return e.isSynthetic === true || e.isMeta === true || !!(e.origin && e.origin.kind && e.origin.kind !== 'human')
      || AGENT_RE.test(text) || SKILL_RE.test(text);
  };

  function foldBox(label, text, md) {
    return h('details', { class: 'fold' }, h('summary', null, label),
      md ? h('div', { class: 'fold-md', dir: 'auto' }, markdown(text)) : preBox(text));
  }

  P.attachSynthetic = function (e, text) {
    var self = this, m = text.match(AGENT_RE);
    if (m) {
      var id = (e.origin && e.origin.from) || m[1] || '';
      var who = (e.origin && e.origin.name) || 'agent';
      var body = text.replace(AGENT_RE, '').replace(/<\/agent-message>\s*$/, '').trim();
      var at = body.indexOf('The report follows:');
      if (at >= 0) body = body.slice(at + 'The report follows:'.length).replace(/^\s*\n/, '');
      body = body.replace(/^ {2}/gm, '');                     // the harness indents every line of the report
      // the Agent card that started it: its launch result names the agent id
      var card = null;
      Object.keys(this.cards).forEach(function (k) {
        var c = self.cards[k];
        if (!card && (c._name === 'Agent' || c._name === 'Task') && id && c._res && c._res.textContent.indexOf(id) >= 0) card = c;
      });
      if (card) {
        card._status.className = 'st ok';
        card._status.textContent = '✓';
        card._res.classList.remove('hidden');
        card._res.append(foldBox('report', body, true));
        return;
      }
      this.add(this.foldCard('Agent', 'report · ' + who, foldBox('report', body, true)));
      return;
    }
    if (SKILL_RE.test(text) && this.lastSkill && !this.lastSkill._skillText) {
      this.lastSkill._skillText = true;
      this.lastSkill._res.classList.remove('hidden');
      this.lastSkill._res.append(foldBox('skill instructions', text, true));
      return;
    }
    var first = text.split('\n')[0].slice(0, 90);
    this.add(this.foldCard(SKILL_RE.test(text) ? 'Skill' : 'Note', SKILL_RE.test(text) ? 'instructions' : first, foldBox('full text', text, true)));
  };

  // a closed card like a tool's, for text Claude Code adds by itself
  P.foldCard = function (name, summary, content) {
    var card = h('div', { class: 'tool-card t-' + name + ' synthetic' },
      h('div', { class: 'tool-head', onclick: function () { card.classList.toggle('open'); } },
        h('span', { class: 'tname' }, name), h('span', { class: 'tsum', dir: 'auto' }, summary), h('i', { class: 'st ok' }, '✓')),
      h('div', { class: 'tool-body' }, content));
    return card;
  };

  // Local only chat: its connections to the user's computer, its own measured round trip, and a colour
  // (green fast < 150 ms, yellow slow < 400 ms, red bad / offline). Before its first ping: the link's.
  P.paintLink = function (n) {
    var el = this.linkEl, p = SW.project((this.info.spec || {}).project);
    if (!el) return;
    var show = !!(n && p && p.localOnly);
    el.classList.toggle('hidden', !show);
    if (!show) return;
    // is the computer the project lives on connected? (the web version only knows it from the server)
    var bs = window.SWComputers && SWComputers.buildState ? SWComputers.buildState(p.name) : null;
    var up = n.state === 'connected' && !(bs && bs.on === 'local' && !bs.ok);
    var me = (n.chats || {})[(this.info.spec || {}).relay] || { n: 0, rtt: null };
    var rtt = !up ? null : me.rtt != null ? me.rtt : n.rtt != null ? Math.round(n.rtt) : null;
    var q = !up ? 'off' : rtt == null ? 'wait' : rtt < 150 ? 'fast' : rtt < 400 ? 'slow' : 'bad';
    var word = { off: 'offline', wait: 'checking', fast: 'fast', slow: 'slow', bad: 'bad' }[q];
    el.className = 'chat-link q-' + q;
    el.replaceChildren(h('i', { class: 'dot' }), h('span', null,
      me.n + (me.n === 1 ? ' conn' : ' conns') + (rtt != null ? ' · ' + rtt + ' ms' : '') + ' · ' + word));
    el.title = q === 'off' ? 'Your computer is not connected — this chat cannot reach the project files'
      : 'Connections of this chat to your computer: ' + me.n + ' (opens with the first file tool, stays open)\n'
        + 'Round trip: ' + (rtt == null ? 'not measured yet' : rtt + ' ms' + (me.rtt != null ? ' (this chat)' : ' (the link)'))
        + '\nGreen < 150 ms · yellow < 400 ms · red slower';
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
      this.turnSpeech = [e.result];
    }
    this.turnText = false;
    var speech = (this.turnSpeech || []).join('\n\n');
    this.turnSpeech = [];
    if (speech.trim()) this.lastSpeech = speech;
    var parts = [];
    // the model's share (duration_api_ms) next to the whole: the rest is tools, the link, start-up
    if (e.duration_ms) parts.push((e.duration_ms / 1000).toFixed(1) + 's' + (e.duration_api_ms > 0
      ? ' (model ' + (Math.min(e.duration_api_ms, e.duration_ms) / 1000).toFixed(1) + 's)' : ''));
    if (e.num_turns) parts.push(e.num_turns + (e.num_turns === 1 ? ' turn' : ' turns'));
    if (typeof e.total_cost_usd === 'number') parts.push('$' + e.total_cost_usd.toFixed(3));
    var err = e.is_error || (e.subtype && e.subtype !== 'success');
    var line = this.add(h('div', { class: 'result-line' + (err ? ' err' : '') }, (err ? '✗ ' : '✓ ') + parts.join(' · ')));
    if (speech.trim() && window.SWSound && window.speechSynthesis) line.append(this.readBtn(speech));
    if (err && e.result) line.append(h('div', { dir: 'auto' }, String(e.result).slice(0, 500)));
    this.turnFilesLine();
    if (!Object.keys(this.perms).length) this.el.classList.remove('bell');      // nothing waits for you any more
    if (this.mark('done')) {
      var self = this;
      toast('Claude finished in ' + this.info.title, err ? 'err' : 'ok', { onclick: function () { SW.activate(self); } });
      SW.notify('Claude finished', this.info.title);
      if (SW.sound) SW.sound(err ? 'error' : 'done', this);
    }
  };

  // ▶ read: Claude's answer of this turn read aloud (text to speech); again = stop
  P.readBtn = function (text) {
    var self = this;
    var btn = h('button', { class: 'rl-read', title: 'Read the answer aloud' }, '▶ read');
    btn.onclick = function () { self.readAloud(text, btn); };
    return btn;
  };
  P.readAloud = function (text, btn) {
    var S = window.SWSound;
    if (!S) return;
    if (btn && btn.classList.contains('on')) { S.stopReading(); return; }
    var started = S.read(text, function () { if (btn) { btn.classList.remove('on'); btn.textContent = '▶ read'; } },
      function (kind, msg) {
        toast(kind === 'installing' ? 'Installing the Persian voice on the server (a few minutes) — this device\'s voice for now'
          : 'Persian voice on the server: ' + msg, 'warn');
      });
    if (started === false) { toast('This browser cannot read aloud', 'warn'); return; }
    if (btn && started.length) { btn.classList.add('on'); btn.textContent = '■ stop'; }
    if (started.length && S.langOf(text) === 'fa' && !S.hasVoice('fa') && !S.serverEngine() && !this.faVoiceWarned) {
      this.faVoiceWarned = true;
      toast('No Persian voice on this device — the browser may read it badly', 'warn');
    }
  };

  // the words of the message under the pointer (the text blocks of that Claude message)
  P.speechAt = function (target) {
    var g = target;
    while (g && !(g.classList && g.classList.contains('amsg'))) g = g.parentNode;
    if (!g) return '';
    return Array.prototype.map.call(g.children, function (c) { return c._md || (c.classList.contains('md') ? c.textContent : ''); })
      .filter(Boolean).join('\n\n');
  };

  // the files Claude wrote in this turn, with a link to review them in the Changes tab
  P.turnFilesLine = function () {
    var files = Object.keys(this.turnFiles || {});
    this.turnFiles = {};
    if (!files.length) return;
    var self = this, project = this.info.spec.project || '';
    var names = files.map(function (f) { return f.slice(f.lastIndexOf('/') + 1); });
    var shown = names.slice(0, 4).map(function (n) { return h('span', { class: 'tf-name' }, n); });
    this.add(h('div', { class: 'turn-files' }, h('span', null, files.length === 1 ? '1 file changed:' : files.length + ' files changed:'),
      shown, names.length > 4 ? h('span', null, '+' + (names.length - 4)) : null,
      project ? h('button', { onclick: function () { SW.openSession({ kind: 'changes', project: project, focus: files }); } }, 'Review changes') : null));
  };

  // ------------------------------------------------------------ permissions
  P.onPermission = function (e) {
    var self = this;
    var req = e.request || {};
    if (req.subtype !== 'can_use_tool') return;
    var name = pcTool(req.tool_name);
    var rid = e.request_id;
    if (name === 'AskUserQuestion') { this.questionCard(rid, req.input || {}); this.alertUser('Claude has a question', 'question'); return; }
    if (name === 'ExitPlanMode') { this.planCard(rid, req.input || {}); this.alertUser('Claude’s plan is ready', 'plan'); return; }
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
    if (this.mark('bell')) {
      toast('Claude needs permission in ' + this.info.title, 'warn', { onclick: function () { SW.activate(self); } });
      SW.notify('Claude needs permission', (TOOL_FA[name] || name) + ' — ' + this.info.title);
      if (SW.sound) SW.sound('permission', this);
    }
  };

  P.answer = function (rid, allow, always, message, input, permissions) {
    var m = { t: 'perm', id: rid, allow: allow, always: always, message: message || '' };
    if (input) m.input = input;
    if (permissions) m.permissions = permissions;
    this.send(m);
  };

  // a command Claude wants to run on this computer (onpc, computers.js): asked in the chat that runs it,
  // like Claude's own permission cards. answer(v): once | always | hour | deny.
  P.pcAskCard = function (a, answer, modes) {
    var self = this, key = 'pc:' + a.id;
    var left = h('span', { class: 'pc-left' });
    var modeSel = null;
    if (modes && modes.list) {         // the mode of this computer, right here (like the chat's mode chip)
      modeSel = h('select', { class: 'mini-sel pc-mode', title: 'What Claude may run on this computer without asking' },
        modes.list.map(function (m) { return h('option', { value: m[0], title: m[2] }, m[1]); }));
      modeSel.value = modes.value;
      modeSel.addEventListener('change', function () { modes.set(modeSel.value); });
    }
    function b(label, v, cls, tip) { return h('button', { class: 'btn sm' + (cls ? ' ' + cls : ''), title: tip || '', onclick: function () { answer(v); } }, label); }
    var card = h('div', { class: 'perm-card pc-perm' },
      h('div', { class: 'perm-title' }, h('span', { class: 'pc-perm-ic' }, '⌂'), 'Run on ', h('b', null, 'this computer'),
        h('span', { class: 'muted' }, '· ' + a.project)),
      h('pre', { class: 'pc-cmd' }, a.cmd), a.warn ? h('div', { class: 'pc-warn' }, '⚠ ' + a.warn) : null,
      h('div', { class: 'perm-actions' },
        b('Allow once', 'once', 'primary'),
        a.warn ? null : b('Always allow this', 'always', '', 'Never ask again for this exact command in ' + a.project),
        a.warn ? null : b('Allow all 1 hour', 'hour', '', 'Everything in ' + a.project + ' runs without asking for 1 hour'),
        b('Deny', 'deny', 'danger'), modeSel ? h('span', { class: 'pc-mode-l' }, 'mode', modeSel) : null, left));
    this.perms[key] = card;
    this.add(card);
    this.alertUser('Claude wants to run a command on this computer', 'permission');
    if (this.inView() && SW.sound) SW.sound('permission', this);     // in view too: the user asked to hear it
    return { left: left, close: function (text) { self.closePerm(key, text); } };
  };

  // dev servers on the user's computer (onpc --bg, computers.js): ● name :port ↗ ■ above the message box
  P.setDevServers = function (list, act) {
    var key = JSON.stringify((list || []).map(function (x) { return [x.name, x.state, x.urls || x.url, x.code]; }));
    if (key === this.devsKey) return;
    this.devsKey = key;
    var els = (list || []).map(function (x) {
      var urls = x.urls && x.urls.length ? x.urls : x.url ? [x.url] : [];    // every port it serves (API + panel …)
      var run = x.state === 'running';
      var ports = urls.map(function (u) {
        var port = (/:\/\/[^/]*:(\d+)/.exec(u) || [])[1] || (/^https/.test(u) ? '443' : '80');
        return run ? h('a', { class: 'pc-port', href: u, target: '_blank', rel: 'noopener', title: 'Open ' + u }, ':' + port + ' ↗')
          : h('span', { class: 'muted' }, ':' + port);
      });
      return h('span', { class: 'pc-dev-chip ' + (run ? 'on' : 'off'), title: x.cmd + (urls.length ? '\n' + urls.join('\n') : '') + '\nclick: output' },
        [h('i', { class: 'pc-dot ' + (run ? 'on' : 'err') }), h('b', { onclick: function () { act.logs(x); } }, x.name)].concat(ports,
        run ? null : h('span', { class: 'err' }, ' exited ' + (x.code == null ? '' : x.code)),
        h('button', { class: 'icon-btn', title: run ? 'Stop it' : 'Remove', onclick: function () { act.stop(x); } }, run ? '■' : '✕')));
    });
    this.devsEl.classList.toggle('hidden', !els.length);
    this.devsEl.replaceChildren.apply(this.devsEl, els.length ? [h('span', { class: 'muted' }, 'on your computer')].concat(els) : []);
  };

  // Computers (computers.js): where this chat's work runs, shown next to the status
  P.setWhere = function (text, title, cls, onclick) {
    if (!this.whereEl) { this.whereEl = h('span', { class: 'pc-where' }); this.topRight.append(this.whereEl); }   // top right, after the line badge
    this.whereEl.textContent = text || '';
    this.whereEl.title = title || '';
    this.whereEl.onclick = onclick || null;
    this.whereEl.className = 'pc-where' + (cls ? ' ' + cls : '') + (onclick ? ' pick' : '') + (text ? '' : ' hidden');
  };
  // the live output of a command that runs on the user's computer, inside its Bash card
  P.pcRunView = function (run, out, who) {
    var self = this, v = (this.pcRuns = this.pcRuns || {})[run.id];
    if (!v) {
      var cards = Object.keys(this.cards).map(function (k) { return self.cards[k]; })
        .filter(function (c) { return c._name === 'Bash' && c._status && !/\b(ok|err)\b/.test(c._status.className); });   // not finished yet
      var card = cards[cards.length - 1];
      if (!card) return false;
      var head = h('div', { class: 'pc-run-h' }), pre = h('pre', { class: 'pc-out' });
      card.querySelector('.tool-body').append(h('div', { class: 'pc-run-live' }, head, pre));
      card.classList.add('open');
      v = this.pcRuns[run.id] = { head: head, pre: pre };
    }
    var done = run.state === 'done';
    v.head.textContent = '⌂ ' + (done ? (run.code === 0 ? 'ran on ' : 'failed on ') + who + (run.code ? ' · exit ' + run.code : '')
      : run.state === 'running' ? 'running on ' + who + '…' : run.state + ' on ' + who);
    v.head.className = 'pc-run-h ' + (done ? (run.code === 0 ? 'ok' : 'err') : run.state === 'running' ? 'run' : 'err');
    if (typeof out === 'string') {
      var bottom = v.pre.scrollTop + v.pre.clientHeight >= v.pre.scrollHeight - 4;
      v.pre.textContent = out.length > 6000 ? '…' + out.slice(-6000) : out;
      if (bottom) v.pre.scrollTop = v.pre.scrollHeight;
    }
    return true;
  };

  P.alertUser = function (text, kind) {
    this.stick = true;
    this.scrollEnd();
    if (!this.mark('bell')) return;
    var self = this;
    toast(text + ' — «' + this.info.title + '»', 'warn', { onclick: function () { SW.activate(self); } });
    SW.notify(text, this.info.title);
    if (SW.sound && kind) SW.sound(kind, this);
  };

  // Claude asks the user (AskUserQuestion), like Claude Code itself: one question at a time,
  // a step bar on top (jump back and forth), a single choice moves on by itself, and with
  // several questions a last "Submit" step lists every answer before it is sent.
  P.questionCard = function (rid, input) {
    var self = this;
    var qs = Array.isArray(input.questions) ? input.questions : [];
    var many = qs.length > 1;
    var state = qs.map(function () { return { sel: [], other: '' }; });
    var cur = 0;
    var last = many ? qs.length : Math.max(0, qs.length - 1);   // index of the review step (many) or the only question
    var stepsEl = many ? h('div', { class: 'q-steps' }) : null;
    var review = many ? h('div', { class: 'q-block q-review hidden' }) : null;
    var blocks = [];
    var back = h('button', { class: 'btn sm q-back', onclick: function () { go(cur - 1); } }, '‹ Back');
    var next = h('button', { class: 'btn primary sm q-next', onclick: function () { if (cur >= last) submit(); else go(cur + 1); } }, 'Next ›');
    var card = h('div', { class: 'perm-card q-card', tabindex: '-1' },
      h('div', { class: 'perm-title' }, 'Claude asks', many ? h('span', { class: 'q-count' }) : null), stepsEl);

    function value(i) {
      var vals = state[i].sel.slice();
      if (state[i].other.trim()) vals.push(state[i].other.trim());
      return vals.join(', ');
    }
    function mark(qi) {
      Array.prototype.forEach.call(state[qi].optsEl.children, function (b) { b.classList.toggle('on', state[qi].sel.indexOf(b._label) >= 0); });
    }
    function paint() {
      blocks.forEach(function (b, i) { b.classList.toggle('hidden', i !== cur); });
      if (many) {
        review.classList.toggle('hidden', cur !== qs.length);
        Array.prototype.forEach.call(stepsEl.children, function (b, i) {
          b.classList.toggle('on', i === cur);
          b.classList.toggle('ok', i < qs.length && !!value(i));
        });
        var cnt = card.querySelector('.q-count');
        if (cnt) cnt.textContent = cur < qs.length ? (cur + 1) + ' / ' + qs.length : 'review';
        if (cur === qs.length) fillReview();
      }
      back.disabled = cur === 0;
      back.classList.toggle('hidden', !many);
      next.textContent = cur >= last ? 'Answer' : 'Next ›';
    }
    function fillReview() {
      review.replaceChildren.apply(review, qs.map(function (q, i) {
        var v = value(i);
        return h('button', { class: 'q-row' + (v ? '' : ' missing'), dir: 'auto', onclick: function () { go(i); } },
          h('span', { class: 'q-row-q', dir: 'auto' }, q.header || q.question),
          h('b', { dir: 'auto' }, v || 'not answered'));
      }));
    }
    function focusStep() {
      if (self.replaying || S.active !== self) return;
      var blk = cur < blocks.length ? blocks[cur] : null;
      var el = blk ? (blk.querySelector('.q-opt.on') || blk.querySelector('.q-opt') || blk.querySelector('.q-other')) : next;
      if (el) setTimeout(function () { el.focus(); }, 30);
    }
    function go(i) {
      if (!self.perms[rid]) return;
      cur = Math.max(0, Math.min(last, i));
      paint();
      focusStep();
    }
    function submit() {
      var answers = {};
      for (var i = 0; i < qs.length; i++) {
        var v = value(i);
        if (!v) { toast('Answer every question (or Skip)', 'warn'); go(i); return; }
        answers[qs[i].question] = v;
      }
      self.answer(rid, true, false, '', { answers: answers });
    }
    function done(qi) { if (qi >= last) submit(); else go(qi + 1); }   // one choice made: on to the next step

    if (many) qs.forEach(function (q, qi) {
      stepsEl.append(h('button', { class: 'q-step', dir: 'auto', title: q.question, onclick: function () { go(qi); } }, q.header || ('Q' + (qi + 1))));
    });
    if (many) stepsEl.append(h('button', { class: 'q-step q-step-end', onclick: function () { go(qs.length); } }, 'Submit'));

    qs.forEach(function (q, qi) {
      var opts = h('div', { class: 'q-opts' });
      (q.options || []).forEach(function (o, oi) {
        var b = h('button', { class: 'q-opt', dir: 'auto', onclick: function () {
          var st = state[qi];
          if (q.multiSelect) {
            var k = st.sel.indexOf(o.label);
            if (k >= 0) st.sel.splice(k, 1); else st.sel.push(o.label);
            mark(qi);
            paint();
            return;
          }
          st.sel = [o.label]; st.other = ''; other.value = '';
          mark(qi);
          done(qi);
        } }, h('b', { dir: 'auto' }, oi < 9 ? h('span', { class: 'q-num' }, (oi + 1) + '') : null, o.label), o.description ? h('span', { class: 'qd', dir: 'auto' }, o.description) : null);
        b._label = o.label;
        opts.append(b);
      });
      var other = h('input', { class: 'inp q-other', dir: 'auto', placeholder: 'Other answer…' });
      other.addEventListener('input', function () {
        state[qi].other = other.value;
        if (other.value && !q.multiSelect) { state[qi].sel = []; mark(qi); }
      });
      other.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); ev.stopPropagation(); if (value(qi)) done(qi); else submit(); }
      });
      state[qi].optsEl = opts;
      var blk = h('div', { class: 'q-block', dir: 'auto' },
        q.header ? h('span', { class: 'q-head' }, q.header) : null,
        h('div', { class: 'q-text', dir: 'auto' }, q.question),
        q.multiSelect ? h('div', { class: 'q-hint' }, 'choose one or more') : null,
        opts, other);
      blocks.push(blk);
      card.append(blk);
    });
    if (review) card.append(review);
    card.append(h('div', { class: 'perm-actions' }, back, next,
      h('button', { class: 'btn sm', onclick: function () { self.answer(rid, false, false, 'The user declined to answer these questions.'); } }, 'Skip')));

    // keys like Claude Code: ←/→ (or Tab) between steps, 1–9 picks an option
    card.addEventListener('keydown', function (ev) {
      if (!self.perms[rid] || ev.isComposing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var inInput = ev.target && ev.target.tagName === 'INPUT';
      if (inInput) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        if (!many) return;
        ev.preventDefault();
        go(cur + (ev.key === 'ArrowRight' ? 1 : -1));
        return;
      }
      if (/^[1-9]$/.test(ev.key) && cur < blocks.length) {
        var b = state[cur].optsEl.children[+ev.key - 1];
        if (b) { ev.preventDefault(); b.click(); }
      }
    });

    this.perms[rid] = card;
    paint();
    this.add(card);
    focusStep();
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
    Array.prototype.forEach.call(card.querySelectorAll('.q-opt, .q-step, .q-row'), function (x) { x.disabled = true; });
    if (card.classList.contains('q-card')) {         // in the history: every question at once, no step bar
      Array.prototype.forEach.call(card.querySelectorAll('.q-steps, .q-review, .q-count'), function (x) { x.remove(); });
      Array.prototype.forEach.call(card.querySelectorAll('.q-block'), function (x) { x.classList.remove('hidden'); });
    }
    delete this.perms[rid];
    if (this.pendingPerm === rid) this.pendingPerm = null;
    if (!Object.keys(this.perms).length) this.el.classList.remove('bell');          // answered (here or elsewhere)
  };

  // ------------------------------------------------------------ composer
  P.autosize = function () {
    this.ta.style.height = 'auto';
    this.ta.style.height = Math.min(260, this.ta.scrollHeight + 2) + 'px';
  };

  P.key = function (e) {
    if (e.isComposing) return;
    if (SW.isGlobalShortcut(e)) return;
    var popup = !this.slashEl.classList.contains('hidden') && (this.slashItems || []).some(function (x) { return x.kind === 'cmd' || x.kind === 'file' || x.kind === 'path'; });
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
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !(this.touch && !e.metaKey)) { e.preventDefault(); this.submit(); return; }
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
    var items = null, pt;
    var sm = v.match(/^\/([\w:-]*)$/);
    var saved = sm && window.SWPrompts ? SWPrompts.match(sm[1], this.info.spec.project || '') : [];
    if (sm && (this.commands.length || saved.length || window.SWPrompts)) {
      var q = sm[1].toLowerCase();
      items = saved.map(function (p) { return { kind: 'prompt', p: p }; }).concat(this.commands.filter(function (c) { return c.name.toLowerCase().indexOf(q) === 0; }).slice(0, 12)
        .map(function (c) { return { kind: 'cmd', c: c }; }));
      if (window.SWPrompts && !q) items.push({ kind: 'manage' });
    } else if ((pt = this.pathToken(v, pos))) {
      // a path on the server: ~/… or /… — the SSH user's files, like @ for the project
      this.mentionStart = pos - pt.length;
      var dir = pt.slice(0, pt.lastIndexOf('/') + 1), pre = pt.slice(dir.length);
      var list = this.serverDir(dir);
      if (!list) items = [{ kind: 'loading', text: 'loading ' + dir + '…' }];
      else if (list.error) items = [{ kind: 'none', text: list.error }];
      else {
        var pl = pre.toLowerCase();
        var ents = list.entries.filter(function (e) { return (pre[0] === '.' || e.name[0] !== '.') && e.name.toLowerCase().indexOf(pl) >= 0; });
        ents.sort(function (a, b) { return (b.name.toLowerCase().indexOf(pl) === 0) - (a.name.toLowerCase().indexOf(pl) === 0) || (b.dir - a.dir); });
        items = ents.slice(0, 60).map(function (e) { return { kind: 'path', path: dir + e.name + (e.dir ? '/' : ''), name: e.name + (e.dir ? '/' : '') }; });
        if (!items.length) items = [{ kind: 'none' }];
      }
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
      if (it.kind === 'loading') return h('div', { class: 'sl muted' }, it.text || 'loading files…');
      if (it.kind === 'none') return h('div', { class: 'sl muted' }, it.text || 'no match');
      if (it.kind === 'path') {
        return h('div', { class: 'sl', title: it.path, onmousedown: function (e) { e.preventDefault(); self.pickSlash(i); } },
          h('span', { class: 'fi' }, /\/$/.test(it.path) ? '📁' : '📄'), h('span', { class: 'fp', dir: 'ltr' }, it.name));
      }
      if (it.kind === 'prompt' || it.kind === 'manage') {
        return h('div', { class: 'sl' + (it.kind === 'manage' ? ' muted' : ''), onmousedown: function (e) { e.preventDefault(); self.pickSlash(i); } },
          it.kind === 'manage' ? h('span', null, '★ saved prompts…')
            : [h('span', { class: 'fi' }, '★'), h('b', { dir: 'auto' }, '/' + it.p.name), h('span', { class: 'sd', dir: 'auto' }, it.p.text.slice(0, 80))]);
      }
      var row = it.kind === 'cmd'
        ? [h('b', { dir: 'ltr' }, '/' + it.c.name), it.c.argumentHint ? h('span', { class: 'muted', dir: 'ltr' }, ' ' + it.c.argumentHint) : null,
          it.c.description ? h('span', { class: 'sd', dir: 'auto' }, it.c.description) : null]
        : [h('span', { class: 'fi' }, /\/$/.test(it.path) ? '📁' : '📄'), h('span', { class: 'fp', dir: 'ltr' }, it.path)];
      return h('div', { class: 'sl', onmousedown: function (e) { e.preventDefault(); self.pickSlash(i); } }, row);
    }));
    this.slashEl.classList.remove('hidden');
    this.markSlash();
  };

  // the word before the caret, when it is a path on the server: ~/… or /… (a "/" at the very
  // start of the message without a second "/" stays a slash command)
  P.pathToken = function (v, pos) {
    var m = v.slice(0, pos).match(/(^|\s)((?:~\/|\/)[^\s]*)$/);
    if (!m) return null;
    var tok = m[2], atStart = m.index === 0 && !m[1];
    if (tok[0] === '/' && atStart && tok.indexOf('/', 1) < 0) return null;
    return tok;
  };

  // folder listings on the server, kept for 30 s (no SSH call for every key)
  var DIRS = {};
  P.serverDir = function (dir) {
    var self = this, key = (this.info.spec.project || '') + '|' + dir, c = DIRS[key];
    if (c && (c.loading || Date.now() - c.t < 30000)) return c.data;
    DIRS[key] = { loading: true, data: null, t: Date.now() };
    api('GET', 'server-ls?path=' + encodeURIComponent(dir) + '&project=' + encodeURIComponent(this.info.spec.project || '')).then(function (d) {
      DIRS[key] = { data: { entries: d.entries || [] }, t: Date.now() };
    }).catch(function (e) {
      DIRS[key] = { data: { error: e.message }, t: Date.now() };
    }).then(function () { if (document.activeElement === self.ta) self.slashPopup(); });
    return null;
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
    if (it.kind === 'prompt') {                 // a saved prompt: its text into the message box
      this.ta.value = it.p.text;
      this.slashEl.classList.add('hidden');
    } else if (it.kind === 'manage') {
      this.ta.value = '';
      this.slashEl.classList.add('hidden');
      SWPrompts.dialog(this.info.spec.project || '');
      return;
    } else if (it.kind === 'cmd') {
      this.ta.value = '/' + it.c.name + ' ';
      this.slashEl.classList.add('hidden');
    } else if (it.kind === 'file' || it.kind === 'path') {
      var v = this.ta.value, pos = this.ta.selectionStart;
      var isDir = /\/$/.test(it.path);
      var ins = (it.kind === 'file' ? '@' : '') + it.path + (isDir ? '' : ' ');
      this.ta.value = v.slice(0, this.mentionStart) + ins + v.slice(pos);
      var caret = this.mentionStart + ins.length;
      this.ta.setSelectionRange(caret, caret);
      if (isDir) { this.slashPopup(); this.ta.focus(); return; }   // keep browsing inside the folder
      this.slashEl.classList.add('hidden');
    } else return;
    this.autosize();
    this.ta.focus();
  };

  function hasImages(e) {
    var items = e.dataTransfer && e.dataTransfer.items;
    if (!items) return false;
    return Array.prototype.some.call(items, function (i) { return i.kind === 'file' && /^image\//.test(i.type); });
  }

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

  // Watching a conversation another Claude runs (VS Code, a terminal): read-only, with a way in.
  var WHERE = { simab: 'this app', vscode: 'VS Code', terminal: 'a terminal', other: 'another program' };
  P.setWatch = function (w) {
    var self = this, project = this.info.spec.project, conv = this.info.spec.resume || this.sessionId;
    this.watching = w;
    this.pane.classList.add('watching');
    this.setBusy(w.status === 'busy');
    var where = WHERE[w.owner] || 'another program';
    var cont = h('button', { class: 'btn sm primary', onclick: function () {
      var x = SW.runningConv(project, conv);
      var go = function () {
        SW.closeTab(self, true);
        SW.openSession({ kind: 'chat', project: project, resume: conv, title: self.info.title.replace(/^Watch · /, 'Claude · '), force: true });
      };
      if (!x) { go(); return; }
      SW.confirmDlg('Stop it in ' + where + ' and continue here?', 'Continue here').then(function (ok) {
        if (ok) SW.claudeStop(project, x).then(go).catch(fail);
      });
    } }, 'Continue here');
    var stop = w.running ? h('button', { class: 'btn sm danger', onclick: function () {
      var x = SW.runningConv(project, conv);
      if (!x) return;
      SW.confirmDlg('Stop the Claude running this conversation in ' + where + '?', 'Stop', true).then(function (ok) {
        if (ok) SW.claudeStop(project, x).then(function () { toast('Stopped', 'ok'); }).catch(fail);
      });
    } }, 'Stop it') : null;
    this.watchBar.classList.remove('hidden');
    this.watchBar.replaceChildren.apply(this.watchBar, [
      h('span', null, 'Watching — ', w.running ? 'runs in ' + where + (w.status === 'busy' ? ', working' : w.status ? ', ' + w.status : '') : 'not running any more'),
      h('span', { class: 'grow' }), stop, cont].filter(Boolean));
  };

  P.submit = function () {
    if (this.watching) return;
    var text = this.ta.value.trim();
    if (!text && !this.attachments.length) return;
    if (this.gone) { toast('This conversation has ended — start a new one', 'warn'); return; }
    if (this.conn !== 'online') { toast('No connection yet — your message stays here; send it when it is back', 'warn'); return; }
    var queued = this.busy;              // Claude is working: the message waits for its turn
    if (!queued) {
      this.turnStart = Date.now();
      this.tokBase = 0; this.msgTok = 0; this.outTokens = 0;
    }
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
    if (this.voice) this.voice.abort();      // sent (with the live text): dictation is done
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

  // a new, empty conversation of a project with a starter message: the box starts with it
  P.prefillStarter = function () {
    var p = SW.project(this.info.spec.project);
    if (!p || !p.starter || this.info.spec.watch || this.watching || this.ta.value.trim()) return;
    if (this.sessionId || this.info.spec.resume || this.msgs.querySelector('.umsg') || this.msgs.querySelector('.amsg')) return;
    this.ta.value = p.starter.replace(/\s+$/, '') + '\n\n';
    this.ta.setSelectionRange(this.ta.value.length, this.ta.value.length);
    this.autosize();
  };

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

  P.menu = function (x, y, extra, target) {
    var self = this, Snd = window.SWSound, read = [];
    if (Snd && window.speechSynthesis) {
      var here = this.speechAt(target);
      if (Snd.isReading()) read.push({ label: 'Stop reading', action: function () { Snd.stopReading(); } });
      else if (here) read.push({ label: 'Read aloud', action: function () { self.readAloud(here); } });
      else if (this.lastSpeech) read.push({ label: 'Read the last answer aloud', action: function () { self.readAloud(self.lastSpeech); } });
    }
    SW.showMenu(x, y, read.concat([
      { label: 'Rename…', action: function () { SW.renameTab(self); } },
      { label: 'New conversation', action: function () { self.newChat(); } },
      { label: 'Past conversations…', action: function () { self.historyMenu(); } },
      { label: 'Restart Claude (same conversation)', action: function () { self.restart(); } },
      { label: 'Copy whole conversation', action: function () { navigator.clipboard.writeText(self.msgs.innerText).then(function () { toast('Copied', 'ok'); }).catch(fail); } },
      { label: 'Open in Claude’s text terminal (TUI)', action: function () { SW.claudeInTerminal(self.info.spec.project, self.sessionId || self.info.spec.resume || '', self.info.title.replace(/^Claude · /, '')); } },
      '-',
      { label: 'Close', k: 'Alt+W', action: function () { SW.closeTab(self); } }
    ]).concat(extra || SW.layoutItemsFor(self)));
  };

  P.dispose = function () {
    this.closed = true;
    clearInterval(this.timer);
    if (this.voice) this.voice.destroy();
    if (window.SWSound && window.SWSound.isReading() && this.msgs.querySelector('.rl-read.on')) window.SWSound.stopReading();
    if (this.ws) try { this.ws.close(); } catch (e) { /* ignore */ }
    this.el.remove();
    this.pane.remove();
  };

  // back to this window: the message box of the chat you are in shows at once
  window.addEventListener('focus', function () {
    ((SW.S && SW.S.tabs) || []).forEach(function (t) { if (t.isChat && t.updateAway) t.updateAway(); });
  });

  window.SWChat = { ChatTab: ChatTab, markdown: markdown, fmtTime: fmtTime };
})();
