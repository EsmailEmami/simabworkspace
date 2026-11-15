// Runs chat.js against a real recorded Claude stream:  node tests/js/chat_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { install } = require('./dom');

const ROOT = path.resolve(__dirname, '..', '..');
install();

// the real h() from app.js
const app = fs.readFileSync(path.join(ROOT, 'gui/static/app.js'), 'utf8');
const hSrc = app.slice(app.indexOf('  function h(tag, props) {'), app.indexOf('  var FA_RE'));
const h = new Function(hSrc + '; return h;')();

const toasts = [];
function makeSW() {
  return {
    h, api: (...a) => global.apiImpl(...a), toast: (m) => toasts.push(m), fail: (e) => { throw e; },
    S: { settings: {}, active: null }, faNum: String, token: 't', project: () => ({ remote: '/home/esi/p' }),
    closeTab() {}, activate() {}, setupDrag() {}, isGlobalShortcut: () => false,
    confirmDlg: () => Promise.resolve(true), openSession() {}, showMenu() {}, renameTab() {},
    notify() {}, pickClaudeSession() {}, layoutItemsFor: () => [], viewImage(src) { global.viewed = src; },
    tabEl(tab) { tab.ttlEl = h('span', { class: 'ttl' }, tab.info.title); tab.el = h('div', { class: 'tab' }, tab.ttlEl); }
  };
}
global.apiImpl = () => Promise.resolve({});
global.SW = makeSW();
require(path.join(ROOT, 'gui/static/chat.js'));
const { ChatTab } = global.SWChat;
global.SWChat.instant = true;     // no typewriter delay in most tests

function newTab() {
  const tab = new ChatTab({ id: 'c1', title: 'Claude · p', spec: { kind: 'chat', project: 'p', where: 'server' }, alive: true });
  global.SW.S.active = tab;
  const ws = global.sockets[global.sockets.length - 1];
  return { tab, feed: (m) => ws.onmessage({ data: JSON.stringify(m) }), ws };
}
const byClass = (el, c) => el.querySelectorAll('.' + c);

const tests = {
  'model: Opus 5.5 by default, no vague "default model"; effort medium'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 's', events: [], state: { alive: true, busy: false, model: '', effort: 'medium' } });
    assert.strictEqual(tab.modelSel.value, 'opus');
    assert.strictEqual(tab.effortSel.value, 'medium');
    tab.applyInit({ models: [{ value: 'default', displayName: 'Default (recommended)' }, { value: 'opus', displayName: 'Opus 5.5' }, { value: 'sonnet', displayName: 'Sonnet 5.5' }] });
    const opts = tab.modelSel.querySelectorAll('option').map(o => o.value);
    assert.deepStrictEqual(opts, ['opus', 'sonnet']);
    assert.strictEqual(tab.modelSel.value, 'opus');
    tab.effortSel.value = 'high';
    tab.effortSel.listeners.change.forEach(f => f({}));
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1], { t: 'effort', effort: 'high' });
    feed({ t: 'e', e: { type: '_effort', effort: 'xhigh' } });
    assert.strictEqual(tab.effortSel.value, 'xhigh');
  },
  'reply of a real stream is shown once'() {
    const { tab, feed } = newTab();
    const stream = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/chat_stream_fa.json'), 'utf8'));
    stream.forEach(feed);
    const answers = byClass(tab.msgs, 'amsg');
    assert.strictEqual(answers.length, 1, 'one assistant block');
    assert.match(answers[0].textContent, /سلام/);
    assert.strictEqual(byClass(answers[0], 'live-text').length, 0, 'live draft replaced by the final text');
    assert.strictEqual(byClass(answers[0], 'md').length, 1);
    assert.strictEqual(byClass(tab.msgs, 'umsg').length, 1);
    assert.strictEqual(byClass(tab.msgs, 'result-line').length, 1);
    assert.strictEqual(tab.busy, false);
  },

  'text streams in while typing (before the final message)'() {
    const { tab, feed } = newTab();
    const se = (event) => feed({ t: 'e', e: { type: 'stream_event', event, parent_tool_use_id: null } });
    se({ type: 'message_start', message: { id: 'm1' } });
    se({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } });
    se({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'سلام، ' } });
    se({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'خوبم' } });
    assert.strictEqual(byClass(tab.msgs, 'live-text')[0].textContent, 'سلام، خوبم');
    assert.strictEqual(tab.busy, true);
  },

  'thinking + text + tool: final message replaces drafts, tool result fills the card'() {
    const { tab, feed } = newTab();
    const se = (event) => feed({ t: 'e', e: { type: 'stream_event', event, parent_tool_use_id: null } });
    const asst = (content) => feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm1', content } } });
    se({ type: 'message_start', message: { id: 'm1' } });
    se({ type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } });
    asst([{ type: 'thinking', thinking: '' }]);
    se({ type: 'content_block_stop', index: 0 });
    se({ type: 'content_block_start', index: 1, content_block: { type: 'text', text: '' } });
    se({ type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: 'بررسی می‌کنم' } });
    asst([{ type: 'text', text: 'بررسی می‌کنم' }]);
    se({ type: 'content_block_stop', index: 1 });
    se({ type: 'content_block_start', index: 2, content_block: { type: 'tool_use', name: 'Bash' } });
    asst([{ type: 'tool_use', id: 'tu1', name: 'Bash', input: { command: 'ls' } }]);
    se({ type: 'content_block_stop', index: 2 });
    feed({ t: 'e', e: { type: 'user', parent_tool_use_id: null, message: { content: [{ type: 'tool_result', tool_use_id: 'tu1', content: 'a.txt' }] } } });
    const g = byClass(tab.msgs, 'amsg')[0];
    assert.strictEqual(byClass(g, 'live-text').length, 0);
    assert.strictEqual(byClass(g, 'md').length, 1);
    assert.strictEqual(byClass(g, 'tool-card').length, 1, 'one tool card, the draft card is gone');
    assert.match(byClass(g, 'tool-result')[0].textContent, /a\.txt/);
    assert.strictEqual(byClass(g, 'thinking').length, 0, 'empty thinking is not shown');
  },

  'question card answers with the extension format'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q1', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion',
      input: { questions: [{ question: 'کدام؟', header: 'انتخاب', multiSelect: false, options: [{ label: 'اول' }, { label: 'دوم' }] }] } } } });
    const opts = byClass(tab.msgs, 'q-opt');
    assert.strictEqual(opts.length, 2);
    opts[1].click();
    const sent = ws.sent[ws.sent.length - 1];
    assert.deepStrictEqual(sent, { t: 'perm', id: 'q1', allow: true, always: false, message: '', input: { answers: { 'کدام؟': 'دوم' } } });
  },

  'plan card: approve with auto-edit'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 'e', e: { type: 'control_request', request_id: 'p1', request: { subtype: 'can_use_tool', tool_name: 'ExitPlanMode', input: { plan: '## برنامه\n- یک' } } } });
    const card = byClass(tab.msgs, 'plan-card')[0];
    assert.match(card.textContent, /برنامه/);
    card.querySelector('.btn').click();
    const sent = ws.sent[ws.sent.length - 1];
    assert.strictEqual(sent.allow, true);
    assert.deepStrictEqual(sent.permissions, [{ type: 'setMode', mode: 'acceptEdits', destination: 'session' }]);
  },

  'answer is shown from the result if no text block arrived'() {
    const { tab, feed } = newTab();
    feed({ t: 'e', e: { type: 'user', parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'سلام' }] } } });
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'سلام! خوبم.', duration_ms: 3100, num_turns: 1 } });
    const g = byClass(tab.msgs, 'amsg');
    assert.strictEqual(g.length, 1);
    assert.match(g[0].textContent, /خوبم/);
  },

  'no duplicate answer from the result when text was shown'() {
    const { tab, feed } = newTab();
    const stream = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/chat_stream_fa.json'), 'utf8'));
    stream.forEach(feed);
    assert.strictEqual(byClass(tab.msgs, 'amsg').length, 1);
  },

  'live status follows what Claude is doing'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    feed({ t: 's', events: [], state: { alive: true, busy: false } });
    assert.strictEqual(tab.stText.textContent, 'ready');
    const se = (event) => feed({ t: 'e', e: { type: 'stream_event', event, parent_tool_use_id: null } });
    feed({ t: 'e', e: { type: 'system', subtype: 'status', status: 'requesting' } });
    assert.strictEqual(tab.stText.textContent, 'requesting…');
    assert.strictEqual(byClass(tab.msgs, 'sysline').length, 0, 'status is not written into the chat');
    se({ type: 'message_start', message: { id: 'm1' } });
    se({ type: 'content_block_start', index: 0, content_block: { type: 'text' } });
    assert.strictEqual(tab.stText.textContent, 'writing…');
    se({ type: 'message_delta', usage: { output_tokens: 1500 } });
    assert.match(tab.stExtra.textContent, /1\.5k tokens/);
    feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm1', content: [{ type: 'tool_use', id: 't', name: 'Bash', input: { command: 'npm test' } }] } } });
    assert.strictEqual(tab.stText.textContent, 'running Bash npm test');
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion', input: { questions: [] } } } });
    assert.strictEqual(tab.stText.textContent, 'waiting for your answer');
    assert.match(tab.statusEl.className, /wait/);
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'x' } });
    assert.strictEqual(tab.stText.textContent, 'ready');
    ws.onclose();
    assert.match(tab.stText.textContent, /disconnected/);
  },

  'Persian question cards are right-to-left'() {
    const { tab, feed } = newTab();
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q1', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion',
      input: { questions: [{ question: 'کدام؟', options: [{ label: 'اول' }] }] } } } });
    assert.strictEqual(byClass(tab.msgs, 'q-block')[0].getAttribute('dir'), 'auto');
    assert.strictEqual(byClass(tab.msgs, 'q-opt')[0].getAttribute('dir'), 'auto');
  },

  'your message shows at once and the echo does not duplicate it'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    tab.ta.value = 'سلام، یک تست';
    tab.submit();
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1].content, [{ type: 'text', text: 'سلام، یک تست' }]);
    let bubbles = byClass(tab.msgs, 'umsg');
    assert.strictEqual(bubbles.length, 1);
    assert.ok(bubbles[0].classList.contains('pending'));
    assert.strictEqual(tab.stText.textContent, 'sending…');
    feed({ t: 'e', e: { type: 'user', isReplay: true, parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'سلام، یک تست' }] } } });
    bubbles = byClass(tab.msgs, 'umsg');
    assert.strictEqual(bubbles.length, 1, 'echo merged into the bubble');
    assert.ok(!bubbles[0].classList.contains('pending'));
  },

  async 'text is typed out live, then becomes markdown in place'() {
    global.SWChat.instant = false;
    try {
      const { tab, feed } = newTab();
      const se = (event) => feed({ t: 'e', e: { type: 'stream_event', event, parent_tool_use_id: null } });
      se({ type: 'message_start', message: { id: 'm9' } });
      se({ type: 'content_block_start', index: 0, content_block: { type: 'thinking' } });
      const th = byClass(tab.msgs, 'thinking')[0];
      assert.strictEqual(th.open, true, 'thinking is open while it streams');
      feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm9', content: [{ type: 'thinking', thinking: 'hmm' }] } } });
      se({ type: 'content_block_start', index: 1, content_block: { type: 'text' } });
      const long = 'یک جملهٔ نسبتاً طولانی برای دیدن تایپ زنده. '.repeat(6);
      se({ type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: long } });
      const live = byClass(tab.msgs, 'live-text')[0];
      assert.ok(live.textContent.length < long.length, 'not all at once');
      feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm9', content: [{ type: 'text', text: long }] } } });
      assert.strictEqual(byClass(tab.msgs, 'md').length, 0, 'final waits until typing is done');
      await new Promise((r) => setTimeout(r, 600));
      assert.strictEqual(byClass(tab.msgs, 'live-text').length, 0);
      assert.strictEqual(byClass(tab.msgs, 'md').length, 1);
      const g = byClass(tab.msgs, 'amsg')[0];
      assert.ok(g.children[0].classList.contains('thinking') && g.children[1].classList.contains('md'), 'order kept');
    } finally { global.SWChat.instant = true; }
  },

  'a broken stored event does not break echo matching or the status'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    // snapshot with a corrupt event in the middle (content block is null)
    feed({ t: 's', state: { alive: true, busy: false }, events: [
      { type: 'user', message: { content: [{ type: 'text', text: 'قبلی' }] } },
      { type: 'user', message: { content: [null] } },
      { type: 'result', subtype: 'success', result: 'ok' }] });
    assert.strictEqual(tab.replaying, false, 'not stuck in replay');
    tab.ta.value = 'چطوی';
    tab.submit();
    // the echo may differ slightly (e.g. normalised text) — it is matched by order
    feed({ t: 'e', e: { type: 'user', isReplay: true, parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'چطوی ' }] } } });
    const bubbles = byClass(tab.msgs, 'umsg').map((b) => b.textContent);
    assert.deepStrictEqual(bubbles, ['قبلی', 'چطوی']);
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'خوبم' } });
    assert.strictEqual(tab.stText.textContent, 'ready');
    assert.strictEqual(tab.turnStart, 0, 'timer stopped');
  },

  'images in messages are shown and open large on click'() {
    const { tab, feed } = newTab();
    feed({ t: 'e', e: { type: 'user', parent_tool_use_id: null, message: { content: [
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } }, { type: 'text', text: 'این چیه؟' }] } } });
    const img = byClass(tab.msgs, 'uthumbs')[0].querySelector('img');
    img.click();
    assert.strictEqual(global.viewed, 'data:image/png;base64,iVBORw0KGgo=');
    tab.attachments = [{ type: 'image/png', data: 'AAA=', url: 'data:image/png;base64,AAA=' }];
    tab.renderAttach();
    tab.attachEl.querySelector('img').click();
    assert.strictEqual(global.viewed, 'data:image/png;base64,AAA=');
  },

  async 'long chats load the last page first and earlier pages on scroll-up'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    const msg = (i) => [{ type: 'user', message: { content: [{ type: 'text', text: 'q' + i }] } },
      { type: 'assistant', message: { id: 'm' + i, content: [{ type: 'text', text: 'a' + i }] } }];
    let entries = [];
    for (let i = 80; i < 100; i++) entries = entries.concat(msg(i));
    feed({ t: 's', state: { alive: true }, events: [{ type: '_history', entries, start: 160, total: 200 }] });
    assert.strictEqual(byClass(tab.msgs, 'umsg').length, 20);
    assert.match(tab.topEl.textContent, /scroll up/);
    let older = [];
    for (let i = 60; i < 80; i++) older = older.concat(msg(i));
    const asked = [];
    global.apiImpl = (method, path) => { asked.push(path); return Promise.resolve({ total: 200, start: 120, entries: older }); };
    // the browser scrolling by itself (the chat put back into the page) loads nothing
    tab.scroller.scrollTop = 0;
    tab.scroller.dispatch('scroll');
    await new Promise((r) => setTimeout(r, 10));
    assert.deepStrictEqual(asked, []);
    // the user scrolling up does
    tab.scroller.dispatch('wheel', { deltaY: -100 });
    tab.scroller.scrollTop = 0;
    tab.scroller.dispatch('scroll');
    await new Promise((r) => setTimeout(r, 10));
    assert.deepStrictEqual(asked, ['chat-history/c1?before=160']);
    const texts = byClass(tab.msgs, 'umsg').map((b) => b.textContent);
    assert.strictEqual(texts.length, 40);
    assert.strictEqual(texts[0], 'q60', 'older messages are added above');
    assert.strictEqual(texts[20], 'q80');
    assert.strictEqual(tab.histStart, 120);
    global.apiImpl = () => Promise.resolve({});
  },

  'switching tabs or a layout change never jumps to the first message'() {
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    const sc = tab.scroller;
    sc.scrollHeight = 5000; sc.clientHeight = 500; sc.scrollTop = 4500;
    assert.strictEqual(tab.stick, true);
    // the browser resets the scroll when the chat is put back into the page
    sc.scrollTop = 0;
    sc.dispatch('scroll');
    assert.strictEqual(sc.scrollTop, 5000, 'pinned to the end again');
    assert.strictEqual(tab.stick, true);
    // the user scrolls up to read: that place is kept over a reset, and after switching back
    sc.dispatch('wheel', { deltaY: -300 });
    sc.scrollTop = 2000;
    sc.dispatch('scroll');
    assert.strictEqual(tab.stick, false);
    tab.userScrollAt = 0;
    sc.scrollTop = 0;
    sc.dispatch('scroll');
    assert.strictEqual(sc.scrollTop, 2000);
    sc.scrollTop = 0;
    tab.ensureOpen();
    assert.strictEqual(sc.scrollTop, 2000);
    // back to the end with the ↓ button; switching tabs keeps it at the end
    tab.jumpBtn.click();
    assert.strictEqual(tab.stick, true);
    sc.scrollTop = 0;
    tab.ensureOpen();
    assert.strictEqual(sc.scrollTop, 5000);
  },

  'the message box hides after 3 seconds away and is back at once'() {
    const { tab } = newTab();
    const S = global.SW.S;
    S.active = tab;
    document.hasFocus = () => true;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'));
    // another tab is active: nothing yet, then hidden after 3 s
    S.active = { other: true };
    document.activeElement = null;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'), 'not at once');
    tab.awaySince = Date.now() - 3100;
    tab.updateAway();
    assert.ok(tab.pane.classList.contains('away'));
    // the mouse over the chat: back at once
    tab.pane.dispatch('mouseenter');
    assert.ok(!tab.pane.classList.contains('away'));
    tab.pane.dispatch('mouseleave');
    tab.awaySince = Date.now() - 3100;
    tab.updateAway();
    assert.ok(tab.pane.classList.contains('away'));
    // the window in the background hides the active chat's box too
    S.active = tab;
    document.hasFocus = () => false;
    tab.awaySince = Date.now() - 3100;
    tab.updateAway();
    assert.ok(tab.pane.classList.contains('away'));
    // coming back to the tab (activate → ensureOpen) shows it
    document.hasFocus = () => true;
    tab.ensureOpen();
    assert.ok(!tab.pane.classList.contains('away'));
    // typing in it keeps it, whatever is active
    S.active = { other: true };
    tab.ta.focus();
    tab.awaySince = Date.now() - 9000;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'));
    // never on touch screens
    document.activeElement = null;
    tab.touch = true;
    tab.awaySince = Date.now() - 9000;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'));
    delete document.hasFocus;
  },

  'finished elsewhere: a steady dot, not blinking; asking: blinks until answered; in view: nothing'() {
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    const S = global.SW.S;
    document.hasFocus = () => true;
    const result = { type: 'result', subtype: 'success', duration_ms: 1000 };
    // you look at another tab: finishing leaves a steady mark, never the blinking one
    S.active = { other: true };
    tab.group = { active: { other: true } };
    feed({ t: 'e', e: { type: 'stream_event', event: { type: 'message_start', message: { id: 'm1' } } } });
    assert.ok(tab.el.classList.contains('busy'));
    feed({ t: 'e', e: result });
    assert.ok(!tab.el.classList.contains('busy'));
    assert.ok(tab.el.classList.contains('done'));
    assert.ok(!tab.el.classList.contains('bell'), 'ready does not blink');
    // it works again: the mark goes
    feed({ t: 'e', e: { type: 'stream_event', event: { type: 'message_start', message: { id: 'm2' } } } });
    assert.ok(!tab.el.classList.contains('done'));
    // visible in its own section (not the focused one): nothing to mark
    tab.group = { active: tab };
    feed({ t: 'e', e: result });
    assert.ok(!tab.el.classList.contains('done') && !tab.el.classList.contains('bell'));
    // a permission question while you look elsewhere: blinks — answered: stops
    tab.group = { active: { other: true } };
    feed({ t: 'e', e: { type: 'control_request', request_id: 'r1', request: { subtype: 'can_use_tool', tool_name: 'Bash', input: { command: 'ls' } } } });
    assert.ok(tab.el.classList.contains('bell'));
    feed({ t: 'e', e: { type: '_perm_done', request_id: 'r1', allow: true } });
    assert.ok(!tab.el.classList.contains('bell'), 'answered: no more blinking');
    // switching to the tab clears the steady mark
    feed({ t: 'e', e: result });
    assert.ok(tab.el.classList.contains('done'));
    tab.ensureOpen();
    assert.ok(!tab.el.classList.contains('done'));
    delete document.hasFocus;
    S.active = tab;
  },

  async 'typing ~/ or /home/… lists the server folder, like @ for the project'() {
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    const asked = [];
    global.apiImpl = (method, path) => {
      asked.push(path);
      const dir = decodeURIComponent(path.split('path=')[1].split('&')[0]);
      const listing = { '~/': [{ name: 'projects', dir: true }, { name: 'notes.md', dir: false }, { name: '.ssh', dir: true }],
        '~/projects/': [{ name: 'shop', dir: true }, { name: 'todo.txt', dir: false }],
        '/home/': [{ name: 'esi', dir: true }] }[dir];
      return listing ? Promise.resolve({ entries: listing, home: '/home/esi' }) : Promise.reject(new Error('No such file or directory'));
    };
    const type = async (text) => {
      tab.ta.value = text; tab.ta.selectionStart = tab.ta.selectionEnd = text.length;
      tab.ta.focus(); tab.slashPopup(); await new Promise((r) => setTimeout(r, 5));
    };
    const shown = () => tab.slashEl.classList.contains('hidden') ? [] : byClass(tab.slashEl, 'sl').map((e) => e.textContent.replace(/^[📁📄]/u, ''));
    await type('look at ~/');
    assert.deepStrictEqual(shown(), ['projects/', 'notes.md'], 'dot files only when asked for');
    assert.match(asked[0], /^server-ls\?path=~%2F&project=p$/);
    // a folder: goes in and keeps listing
    tab.pickSlash(0); await new Promise((r) => setTimeout(r, 5));
    assert.strictEqual(tab.ta.value, 'look at ~/projects/');
    assert.deepStrictEqual(shown(), ['shop/', 'todo.txt']);
    // filter, then a file ends the path
    await type('look at ~/projects/to');
    assert.deepStrictEqual(shown(), ['todo.txt']);
    tab.pickSlash(0);
    assert.strictEqual(tab.ta.value, 'look at ~/projects/todo.txt ');
    assert.ok(tab.slashEl.classList.contains('hidden'));
    // the same folder again: no second request
    const n = asked.length;
    await type('and ~/pro');
    assert.strictEqual(asked.length, n);
    assert.deepStrictEqual(shown(), ['projects/']);
    await type('cat ~/.s');
    assert.deepStrictEqual(shown(), ['.ssh/']);
    // absolute paths too; at the start of a message only with a second "/" (else it is a command)
    await type('/home/');
    assert.deepStrictEqual(shown(), ['esi/']);
    await type('/hel');
    assert.ok(!shown().some((t) => /esi/.test(t)), 'a command, not a path');
    await type('x /nope/');
    assert.match(shown()[0], /No such file/);
    global.apiImpl = () => Promise.resolve({});
  },

  'an agent report or a skill text is folded into its card, never shown as if the user wrote it'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    // a message of the user is waiting to be picked up (queued) — a report must not take its place
    ws.onopen && ws.onopen();
    tab.ta.value = 'my question'; tab.submit();
    const bubbles = () => byClass(tab.msgs, 'umsg').map((b) => b.textContent);
    // Agent started in the background
    feed({ t: 'e', e: { type: 'assistant', message: { id: 'a1', content: [{ type: 'tool_use', id: 'toolu_A', name: 'Agent', input: { description: 'Review Task 2', subagent_type: 'general-purpose' } }] } } });
    feed({ t: 'e', e: { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_A', content: 'Async agent launched successfully. agentId: a34d12aeca56395c2 (internal ID)' }] } } });
    // its report comes back as a synthetic "user" message (the real shape)
    feed({ t: 'e', e: { type: 'user', isReplay: true, isSynthetic: true, parent_tool_use_id: null,
      origin: { kind: 'peer', from: 'a34d12aeca56395c2', name: 'general-purpose' },
      message: { role: 'user', content: 'Another Claude session sent a message: <agent-message from="a34d12aeca56395c2">\n[Subagent hand-back] The text below is the final report. The report follows:\n  ### Spec Compliance\n\n  - ✅ Spec compliant.\n</agent-message>' } } });
    assert.deepStrictEqual(bubbles(), ['my question'], 'the report is not a user message');
    const card = tab.cards.toolu_A;
    const rep = card._res.querySelectorAll('.fold');
    assert.strictEqual(rep.length, 1);
    assert.strictEqual(rep[0].querySelector('summary').textContent, 'report');
    assert.match(rep[0].textContent, /Spec Compliance/);
    assert.doesNotMatch(rep[0].textContent, /Subagent hand-back/);
    assert.ok(!card.classList.contains('open'), 'folded until clicked');
    // the queued message is still waiting for its echo
    assert.ok(byClass(tab.msgs, 'umsg')[0].classList.contains('pending') || byClass(tab.msgs, 'umsg')[0].classList.contains('queued'));
    // a skill: its instructions go into the Skill card
    feed({ t: 'e', e: { type: 'assistant', message: { id: 'a2', content: [{ type: 'tool_use', id: 'toolu_S', name: 'Skill', input: { skill: 'subagent-driven-development' } }] } } });
    feed({ t: 'e', e: { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_S', content: 'Launching skill: subagent-driven-development' }] } } });
    feed({ t: 'e', e: { type: 'user', isSynthetic: true, message: { role: 'user', content: [{ type: 'text', text: 'Base directory for this skill: /home/u/.claude/skills/x\n\n# Subagent-Driven Development\n\nExecute plan…' }] } } });
    assert.deepStrictEqual(bubbles(), ['my question']);
    const sk = tab.cards.toolu_S._res.querySelectorAll('.fold');
    assert.strictEqual(sk.length, 1);
    assert.strictEqual(sk[0].querySelector('summary').textContent, 'skill instructions');
    // a report without its card (e.g. after a reload): its own folded card
    feed({ t: 'e', e: { type: 'user', isSynthetic: true, origin: { kind: 'peer', from: 'zzz', name: 'reviewer' },
      message: { content: '<agent-message from="zzz">done</agent-message>' } } });
    const own = byClass(tab.msgs, 'synthetic').pop();
    assert.match(own.textContent, /Agent.*report · reviewer/);
    assert.deepStrictEqual(bubbles(), ['my question']);
    // what the user really types is still a bubble
    feed({ t: 'e', e: { type: 'user', message: { content: [{ type: 'text', text: 'my question' }] } } });
    feed({ t: 'e', e: { type: 'user', message: { content: [{ type: 'text', text: 'typed in the TUI' }] } } });
    assert.deepStrictEqual(bubbles(), ['my question', 'typed in the TUI']);
  },

  'the "resumed" line is a thin line, not the History list box (class clash)'() {
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [{ type: '_history', start: 0, total: 1, entries: [{ type: 'user', message: { content: 'hi' } }] }] });
    const lines = byClass(tab.msgs, 'sysline');
    assert.ok(lines.some((l) => l.textContent === '— resumed —'));
    lines.forEach((l) => assert.ok(!l.classList.contains('hist'), 'never the class of the History dialog list'));
    const css = fs.readFileSync(path.join(ROOT, 'gui/static/app.css'), 'utf8');
    assert.ok(/\.hist\s*\{[^}]*min-height/.test(css), 'that class is the big History list');
  },

  'a message sent while Claude works is queued, then picked up'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    tab.ta.value = 'first';
    tab.submit();
    feed({ t: 'e', e: { type: 'user', isReplay: true, parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'first' }] } } });
    feed({ t: 'e', e: { type: 'stream_event', parent_tool_use_id: null, event: { type: 'message_start', message: { id: 'm1' } } } });
    assert.strictEqual(tab.busy, true);
    tab.ta.value = 'also do this';
    tab.submit();
    const sent = ws.sent.filter((m) => m.t === 'send');
    assert.strictEqual(sent.length, 2, 'sent right away (Claude queues it)');
    const q2 = byClass(tab.msgs, 'umsg').pop();
    assert.ok(q2.classList.contains('queued'));
    assert.match(q2.textContent, /queued/);
    assert.match(tab.stExtra.textContent, /1 queued/);
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'done 1' } });
    assert.strictEqual(tab.stText.textContent, 'next: your queued message…');
    feed({ t: 'e', e: { type: 'user', isReplay: true, parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'also do this' }] } } });
    assert.ok(!q2.classList.contains('queued'));
    assert.ok(!/queued/.test(q2.textContent));
    assert.strictEqual(byClass(tab.msgs, 'umsg').length, 2, 'no duplicate bubbles');
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'done 2' } });
    assert.strictEqual(tab.stText.textContent, 'ready');
  },

  'running agents are listed with what they are doing'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm1', content: [
      { type: 'tool_use', id: 'ag1', name: 'Task', input: { description: 'Find payment bug', subagent_type: 'Explore' } },
      { type: 'tool_use', id: 'ag2', name: 'Agent', input: { description: 'بررسی فرانت', subagent_type: 'general-purpose' } }] } } });
    assert.ok(!tab.agentsEl.classList.contains('hidden'));
    assert.strictEqual(byClass(tab.agentsEl, 'ag').length, 2);
    assert.match(tab.agentsEl.textContent, /2 agents working/);
    feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: 'ag1', message: { id: 's1', content: [{ type: 'tool_use', id: 'x', name: 'Grep', input: { pattern: 'refund' } }] } } });
    assert.match(byClass(tab.agentsEl, 'ag')[0].textContent, /Grep refund/);
    assert.match(byClass(tab.agentsEl, 'ag')[0].textContent, /1 steps/);
    feed({ t: 'e', e: { type: 'user', parent_tool_use_id: null, message: { content: [{ type: 'tool_result', tool_use_id: 'ag1', content: 'found it' }] } } });
    assert.strictEqual(byClass(tab.agentsEl, 'ag').length, 1);
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'ok' } });
    assert.ok(tab.agentsEl.classList.contains('hidden'), 'cleared when the turn ends');
  },

  'markdown: code, lists, tables, links'() {
    const md = global.SWChat.markdown('# عنوان\n\nمتن `کد` و **پررنگ** https://x.dev\n\n- یک\n- دو\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```go\nfmt.Println(1)\n```');
    assert.strictEqual(md.querySelectorAll('h3').length, 1);
    assert.strictEqual(md.querySelectorAll('li').length, 2);
    assert.strictEqual(md.querySelectorAll('td').length, 2);
    assert.strictEqual(md.querySelectorAll('a').length, 1);
    assert.match(md.querySelector('.codeblock').textContent, /fmt\.Println/);
  }
};

(async () => {
let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e.stack || e).toString().split('\n').slice(0, 4).join('\n     ')); }
}
process.exit(failed ? 1 : 0);
})();
