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
// dictation: a fake Chrome recognizer
const recs = [];
global.webkitSpeechRecognition = class {
  constructor() { this.started = 0; this.aborted = 0; recs.push(this); }
  start() { this.started++; } stop() { if (this.onend) this.onend(); } abort() { this.aborted++; }
  say(text, fin) { const r = [{ transcript: text }]; r.isFinal = fin; this.onresult({ resultIndex: 0, results: [r] }); }
};
require(path.join(ROOT, 'gui/static/voice.js'));
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
  'read aloud: ▶ read under an answer reads that turn (no tools, no code); again = stop; menu reads a message'() {
    const read = [];
    let stopped = 0, onEnd = null;
    global.speechSynthesis = {};
    global.SWSound = global.window.SWSound = { read(t, f) { read.push(t); onEnd = f; return ['x']; }, stopReading() { stopped++; if (onEnd) { const f = onEnd; onEnd = null; f(); } },
      isReading: () => !!onEnd, langOf: () => 'en', hasVoice: () => true };
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    feed({ t: 'e', e: { type: 'user', message: { role: 'user', content: 'go' } } });
    feed({ t: 'e', e: { type: 'assistant', message: { id: 'a1', content: [{ type: 'text', text: 'First part.' }, { type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'ls' } }] } } });
    feed({ t: 'e', e: { type: 'assistant', message: { id: 'a2', content: [{ type: 'thinking', thinking: 'hmm' }, { type: 'text', text: 'Second **part**.' }] } } });
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'Second part.', duration_ms: 1000 } });
    const btn = byClass(tab.msgs, 'rl-read')[0];
    assert.ok(btn, 'a read button on the result line');
    btn.onclick();
    assert.deepStrictEqual(read, ['First part.\n\nSecond **part**.']);
    assert.strictEqual(btn.textContent, '■ stop');
    btn.onclick();
    assert.strictEqual(stopped, 1);
    assert.strictEqual(btn.textContent, '▶ read');
    // right click on one message: only that one
    let items = null;
    global.SW.showMenu = (x, y, it) => { items = it; };
    const second = byClass(tab.msgs, 'amsg')[1];
    tab.menu(0, 0, [], second.querySelector('.md') || second);
    assert.strictEqual(items[0].label, 'Read aloud');
    items[0].action();
    assert.strictEqual(read[1], 'Second **part**.');
    tab.menu(0, 0, [], null);
    assert.strictEqual(items[0].label, 'Stop reading');
    items[0].action();
    tab.menu(0, 0, [], null);
    assert.strictEqual(items[0].label, 'Read the last answer aloud');
    global.SW.showMenu = () => {};
    delete global.speechSynthesis; delete global.SWSound;
  },

  'sounds: a different one per state, only for a chat you are not looking at'() {
    const played = [];
    global.SW.sound = (kind, tab) => played.push([kind, tab.info.spec.project]);
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'x' } });
    assert.deepStrictEqual(played, [], 'in view: quiet');
    global.SW.S.active = null;                                    // looking at another tab
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'x' } });
    feed({ t: 'e', e: { type: 'result', subtype: 'error_during_execution', is_error: true } });
    feed({ t: 'e', e: { type: 'control_request', request_id: 'b1', request: { subtype: 'can_use_tool', tool_name: 'Bash', input: { command: 'ls' } } } });
    feed({ t: 'e', e: { type: '_perm_done', request_id: 'b1', allow: true } });
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q9', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion',
      input: { questions: [{ question: 'Which?', options: [{ label: 'A' }, { label: 'B' }] }] } } } });
    feed({ t: 'e', e: { type: '_perm_done', request_id: 'q9', allow: true } });
    feed({ t: 'e', e: { type: 'control_request', request_id: 'p9', request: { subtype: 'can_use_tool', tool_name: 'ExitPlanMode', input: { plan: 'x' } } } });
    assert.deepStrictEqual(played.map((x) => x[0]), ['done', 'error', 'permission', 'question', 'plan']);
    assert.strictEqual(played[0][1], 'p');
    delete global.SW.sound;
    tab.dispose();
  },
  'composer mic: dictation fills the message box, sending stops it'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    ws.onopen && ws.onopen();
    const mic = tab.composerEl.querySelector('.cb-mic');
    assert.ok(mic, 'a mic in the composer bar');
    assert.strictEqual(mic.nextElementSibling, tab.actionBtn, 'right before Send');
    assert.strictEqual(tab.ta.getAttribute('data-voice-to-text'), '1', 'no second floating mic on the chat box');
    tab.ta.value = 'متن';
    mic.click();
    const rec = recs[recs.length - 1];
    assert.strictEqual(rec.lang, 'fa-IR');
    assert.ok(mic.classList.contains('vtt-rec'));
    tab.ta.scrollHeight = 80;
    rec.say('سلام کلود', false);
    assert.strictEqual(tab.ta.value, 'متن سلام Claude', 'live text in the box');
    assert.strictEqual(tab.ta.style.height, '82px', 'the box grows with it');
    rec.say('سلام کلود', true);
    tab.submit();
    const sent = ws.sent.filter((m) => m.t === 'send').pop();
    assert.strictEqual(sent.content[0].text, 'متن سلام Claude');
    assert.strictEqual(tab.ta.value, '', 'nothing written back after sending');
    assert.ok(!mic.classList.contains('vtt-rec'), 'stopped');
    assert.strictEqual(rec.aborted, 1);
    tab.dispose();
  },
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
  'the tab shows the name Claude gave the conversation'() {
    const { tab, feed } = newTab();
    feed({ t: 's', events: [], state: { alive: true, busy: false } });
    let renders = 0;
    global.SW.renderTitle = () => { renders++; };
    feed({ t: 'title', title: 'Login page focus on Safari' });
    assert.strictEqual(tab.info.title, 'Login page focus on Safari');
    assert.strictEqual(tab.ttlEl.textContent, 'Login page focus on Safari');
    assert.ok(tab.el.title.indexOf('Login page focus on Safari') === 0);
    assert.strictEqual(renders, 1);
    delete global.SW.renderTitle;
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

  'several questions: one step at a time, back and forth, then review and submit'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q2', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion',
      input: { questions: [
        { question: 'رنگ؟', header: 'Colour', options: [{ label: 'سبز' }, { label: 'آبی' }] },
        { question: 'Features?', header: 'Feat', multiSelect: true, options: [{ label: 'A' }, { label: 'B' }] }] } } } });
    const card = byClass(tab.msgs, 'q-card')[0];
    const blocks = byClass(card, 'q-block').filter((b) => !b.classList.contains('q-review'));
    const shown = () => blocks.map((b, i) => b.classList.contains('hidden') ? null : i).filter((x) => x !== null);
    const steps = byClass(card, 'q-step');
    assert.strictEqual(steps.length, 3, 'a step per question + Submit');
    assert.deepStrictEqual(shown(), [0], 'only the first question is shown');
    assert.ok(card.querySelector('.q-back').disabled);
    const sentBefore = ws.sent.length;
    byClass(blocks[0], 'q-opt')[1].click();                 // single choice: on to the next step
    assert.deepStrictEqual(shown(), [1]);
    assert.ok(steps[0].classList.contains('ok') && steps[1].classList.contains('on'));
    card.querySelector('.q-back').click();                   // back: the choice is kept
    assert.deepStrictEqual(shown(), [0]);
    assert.ok(byClass(blocks[0], 'q-opt')[1].classList.contains('on'));
    card.dispatch('keydown', { key: 'ArrowRight' });         // keys move between steps too
    assert.deepStrictEqual(shown(), [1]);
    byClass(blocks[1], 'q-opt')[0].click();                  // multi: stays on the step
    byClass(blocks[1], 'q-opt')[1].click();
    assert.deepStrictEqual(shown(), [1]);
    card.querySelector('.q-next').click();
    assert.deepStrictEqual(shown(), [], 'review step');
    const rows = byClass(card, 'q-row');
    assert.strictEqual(rows.length, 2);
    assert.match(rows[0].textContent, /آبی/);
    assert.match(rows[1].textContent, /A, B/);
    assert.strictEqual(ws.sent.length, sentBefore, 'nothing sent before the last step');
    rows[0].click();                                          // jump back from the review
    assert.deepStrictEqual(shown(), [0]);
    steps[2].click();
    card.querySelector('.q-next').click();                    // "Answer"
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1].input, { answers: { 'رنگ؟': 'آبی', 'Features?': 'A, B' } });
    feed({ t: 'e', e: { type: '_perm_done', request_id: 'q2', allow: true, answers: { 'رنگ؟': 'آبی', 'Features?': 'A, B' } } });
    assert.deepStrictEqual(shown(), [0, 1], 'answered: every question visible in the history');
    assert.strictEqual(byClass(card, 'q-steps').length, 0);
  },

  'several questions: submitting with one unanswered goes to it'() {
    const { tab, feed, ws } = newTab();
    feed({ t: 'e', e: { type: 'control_request', request_id: 'q3', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion',
      input: { questions: [{ question: 'one', options: [{ label: 'x' }] }, { question: 'two', options: [{ label: 'y' }] }] } } } });
    const card = byClass(tab.msgs, 'q-card')[0];
    const n = ws.sent.length;
    byClass(card, 'q-step')[2].click();
    assert.ok(byClass(card, 'q-row')[0].classList.contains('missing'));
    card.querySelector('.q-next').click();
    assert.strictEqual(ws.sent.length, n, 'not sent');
    assert.ok(!byClass(card, 'q-block')[0].classList.contains('hidden'), 'back at the unanswered question');
    card.dispatch('keydown', { key: '1' });                    // number keys pick an option
    assert.ok(!byClass(card, 'q-block')[1].classList.contains('hidden'));
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
    assert.match(tab.stText.textContent, /no connection/);
  },

  async 'no connection: the box says so; after an app restart the tab opens the same Claude again'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    feed({ t: 's', events: [{ type: 'system', subtype: 'init', session_id: '11111111-2222-3333-4444-555555555555' }], state: { alive: true, busy: true } });
    tab.info.spec.relay = 'abcdef0123456789';
    // the server lost its ssh to Claude: the status says it is reconnecting, not "working"
    feed({ t: 'e', e: { type: '_reconnect', code: 255 } });
    assert.match(tab.stText.textContent, /reconnecting to Claude/);
    feed({ t: 'e', e: { type: '_reconnected', running: true } });
    assert.doesNotMatch(tab.stText.textContent, /reconnecting/);
    // the browser lost the app: nothing can be sent, the text stays in the box
    const calls = [];
    global.apiImpl = (m, u, b) => {
      calls.push([m, u, b]);
      if (u === 'state') return Promise.resolve({ sessions: [] });             // restarted: this session is gone
      if (m === 'POST' && u === 'sessions') return Promise.resolve({ id: 'c9', title: 'Claude · p', alive: true, spec: Object.assign({ kind: 'chat', project: 'p', where: 'server' }, { relay: b.relay, resume: b.resume }) });
      return Promise.resolve({});
    };
    ws.onclose();
    assert.ok(tab.composerEl.classList.contains('offline'));
    assert.ok(tab.actionBtn.disabled);
    tab.ta.value = 'still here';
    toasts.length = 0;
    tab.submit();
    assert.strictEqual(tab.ta.value, 'still here');
    assert.match(toasts.pop(), /No connection/);
    await new Promise((r) => setTimeout(r, 450));
    const post = calls.find((c) => c[0] === 'POST' && c[1] === 'sessions');
    assert.ok(post, 'the conversation is opened again');
    assert.deepStrictEqual([post[2].resume, post[2].relay, post[2].project], ['11111111-2222-3333-4444-555555555555', 'abcdef0123456789', 'p']);
    assert.strictEqual(tab.id, 'c9');
    assert.ok(!tab.gone);
    const ws2 = global.sockets[global.sockets.length - 1];
    assert.match(ws2.url, /id=c9/);
    ws2.onopen();
    assert.ok(!tab.composerEl.classList.contains('offline') && !tab.actionBtn.disabled);
    global.apiImpl = () => Promise.resolve({});
  },

  async 'a dropped connection reconnects with ONE socket: no message or tool shows twice'() {
    const { tab, ws } = newTab();
    ws.onopen();
    global.apiImpl = (m, u) => Promise.resolve(u === 'state' ? { sessions: [{ id: 'c1' }] } : {});
    const before = global.sockets.length;
    ws.readyState = 3;
    ws.onclose();                         // the tab's own reconnect is scheduled…
    tab.connect();                        // …and "connection back" reconnects at once
    tab.connect();
    await new Promise((r) => setTimeout(r, 450));
    assert.strictEqual(global.sockets.length - before, 1, 'one new socket only');
    const ws2 = global.sockets[global.sockets.length - 1];
    ws2.onopen();
    const user = { type: 'user', message: { role: 'user', content: 'hello there' } };
    ws2.onmessage({ data: JSON.stringify({ t: 's', state: { alive: true }, events: [user] }) });
    if (ws.onmessage) ws.onmessage({ data: JSON.stringify({ t: 'e', e: user }) });   // a late frame from the old one
    assert.strictEqual((tab.msgs.textContent.match(/hello there/g) || []).length, 1);
    global.apiImpl = () => Promise.resolve({});
  },

  async 'a new chat with no conversation yet just ends after an app restart'() {
    const { tab, ws } = newTab();
    ws.onopen();
    global.apiImpl = (m, u) => Promise.resolve(u === 'state' ? { sessions: [] } : {});
    ws.onclose();
    await new Promise((r) => setTimeout(r, 450));
    assert.ok(tab.gone);
    assert.match(tab.msgs.textContent, /no longer running/);
    assert.strictEqual(tab.stText.textContent, 'not running');
    global.apiImpl = () => Promise.resolve({});
  },

  'after a turn: the files Claude changed, with Review changes'() {
    const { tab, feed } = newTab();
    feed({ t: 's', events: [], state: { alive: true, busy: false } });
    feed({ t: 'e', e: { type: 'assistant', parent_tool_use_id: null, message: { id: 'm1', content: [
      { type: 'tool_use', id: 't1', name: 'Edit', input: { file_path: '/home/esi/p/web/app.ts', old_string: 'a', new_string: 'b' } },
      { type: 'tool_use', id: 't2', name: 'Write', input: { file_path: '/home/esi/p/README.md', content: 'x' } },
      { type: 'tool_use', id: 't3', name: 'Edit', input: { file_path: '/home/esi/p/web/app.ts', old_string: 'b', new_string: 'c' } },
      { type: 'tool_use', id: 't4', name: 'Read', input: { file_path: '/home/esi/p/x.go' } }] } } });
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'done' } });
    const line = byClass(tab.msgs, 'turn-files')[0];
    assert.ok(line, 'a line after the turn');
    assert.match(line.textContent, /2 files changed:app\.tsREADME\.md/);
    let opened = null;
    const old = global.SW.openSession;
    global.SW.openSession = (spec) => { opened = spec; return Promise.resolve(null); };
    line.querySelector('button').click();
    global.SW.openSession = old;
    assert.deepStrictEqual(opened, { kind: 'changes', project: 'p', focus: ['/home/esi/p/web/app.ts', '/home/esi/p/README.md'] });
    feed({ t: 'e', e: { type: 'result', subtype: 'success', result: 'again' } });
    assert.strictEqual(byClass(tab.msgs, 'turn-files').length, 1, 'only for turns that changed files');
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
    // tablet (touch): only the active chat keeps its box — a tap leaves "hover" on, it must not count
    document.activeElement = null;
    tab.touch = true;
    tab.hover = true;
    tab.awaySince = Date.now() - 9000;
    tab.updateAway();
    assert.ok(tab.pane.classList.contains('away'), 'inactive chat on a tablet: hidden');
    S.active = tab;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'), 'the active chat on a tablet: shown');
    // phone (one section): never hidden
    S.active = { other: true };
    document.body.classList.add('compact');
    tab.awaySince = Date.now() - 9000;
    tab.updateAway();
    assert.ok(!tab.pane.classList.contains('away'));
    document.body.classList.remove('compact');
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

  'reopened while Claude works: "reconnecting", then "working" — never a dead-looking "ready"'() {
    const { tab, feed, ws } = newTab();
    ws.onopen && ws.onopen();
    feed({ t: 's', state: { alive: true, busy: false, linking: true }, events: [] });
    assert.match(tab.stText.textContent, /reconnecting to Claude on the server/);
    assert.notStrictEqual(tab.stText.textContent, 'ready');
    // the relay answers: Claude is in the middle of work
    feed({ t: 'link', linking: false, busy: true });
    feed({ t: 'e', e: { type: '_reattached', busy: true } });
    assert.ok(tab.busy);
    assert.ok(tab.el.classList.contains('busy'), 'the tab shows it works');
    assert.match(tab.stText.textContent, /working… \(picked up again\)/);
    assert.ok(byClass(tab.msgs, 'sysline').some((l) => /kept working on the server; following it again/.test(l.textContent)));
    // another tab: Claude was idle → plain "ready"
    const b = newTab();
    b.ws.onopen && b.ws.onopen();
    b.feed({ t: 's', state: { alive: true, busy: false, linking: true }, events: [] });
    b.feed({ t: 'link', linking: false, busy: false });
    assert.strictEqual(b.tab.stText.textContent, 'ready');
    assert.ok(!b.tab.busy);
  },

  'a project starter message fills the box of every new chat, not a continued one'() {
    const old = global.SW.project;
    global.SW.project = () => ({ remote: '/home/esi/p', starter: 'Read memory/README.md first. پاسخ فارسی.' });
    const { tab, feed } = newTab();
    feed({ t: 's', state: { alive: true }, events: [] });
    assert.strictEqual(tab.ta.value, 'Read memory/README.md first. پاسخ فارسی.\n\n');
    assert.strictEqual(tab.ta.selectionStart, tab.ta.value.length, 'you type your request after it');
    // what you wrote is never replaced
    tab.ta.value = 'my own text';
    feed({ t: 's', state: { alive: true }, events: [] });
    assert.strictEqual(tab.ta.value, 'my own text');
    // a continued conversation: nothing
    const b = newTab();
    b.tab.info.spec.resume = '11111111-2222-3333-4444-555555555555';
    b.feed({ t: 's', state: { alive: true }, events: [{ type: '_history', start: 0, total: 1, entries: [{ type: 'user', message: { content: 'old' } }] }] });
    assert.strictEqual(b.tab.ta.value, '');
    // "new" conversation in that tab: the box starts with it again
    b.feed({ t: 'clear' });
    assert.match(b.tab.ta.value, /^Read memory\/README\.md first/);
    // no starter: nothing
    global.SW.project = () => ({ remote: '/home/esi/p', starter: '' });
    const c = newTab();
    c.feed({ t: 's', state: { alive: true }, events: [] });
    assert.strictEqual(c.tab.ta.value, '');
    global.SW.project = old;
  },

  async 'stuck chat: after a minute without a sign of life, Interrupt and Restart Claude are offered'() {
    const { tab, feed, ws } = newTab();
    ws.onopen && ws.onopen();
    feed({ t: 's', state: { alive: true, busy: false }, events: [] });
    tab.ta.value = 'چی شد'; tab.submit();
    tab.renderStatus();
    assert.ok(tab.stuckEl.classList.contains('hidden'), 'not at once');
    tab.lastEventAt = Date.now() - 70000;
    tab.renderStatus();
    assert.ok(!tab.stuckEl.classList.contains('hidden'));
    const [intr, restart] = tab.stuckEl.querySelectorAll('button');
    intr.click();
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1], { t: 'interrupt' });
    const asked = [];
    global.apiImpl = (m, p) => { asked.push(p); return Promise.resolve({ id: 'c1', title: 'Claude · p', spec: { kind: 'chat', project: 'p' } }); };
    restart.click(); await new Promise((r) => setTimeout(r, 5));
    assert.ok(asked.includes('sessions/c1/restart'));
    assert.strictEqual(tab.echoes.length, 0, 'the queued messages are dropped');
    // a fresh event: Claude lives — the buttons go
    feed({ t: 'e', e: { type: 'stream_event', event: { type: 'message_start', message: { id: 'z' } } } });
    tab.renderStatus();
    assert.ok(tab.stuckEl.classList.contains('hidden'));
    global.apiImpl = () => Promise.resolve({});
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

  'narrow chat pane (tablet / laptop split) also gets the one chip'() {
    const { tab } = newTab();
    const el = tab.composerEl;
    Object.defineProperty(el, 'clientWidth', { configurable: true, value: 430 });
    tab.fitBar();
    assert.ok(el.classList.contains('cb-narrow'), 'narrow pane -> chip');
    Object.defineProperty(el, 'clientWidth', { configurable: true, value: 900 });
    tab.fitBar();
    assert.ok(!el.classList.contains('cb-narrow'), 'wide pane -> three selects');
    const css = fs.readFileSync(path.join(__dirname, '../../gui/static/app.css'), 'utf8');
    assert.match(css, /:is\(body\.compact, \.cb-narrow\) \.cb-opts \{ display: inline-flex/);
    assert.match(css, /:is\(body\.compact, \.cb-narrow\) \.cbar \.mini-sel \{ display: none/);
  },
  'phone: one chip for mode / model / effort, its menu changes them'() {
    const { tab, feed, ws } = newTab();
    ws.onopen();
    feed({ t: 's', events: [], state: { alive: true, busy: false, mode: 'auto', model: 'opus', effort: 'high' } });
    const chip = tab.optsBtn;
    assert.ok(tab.actionBtn.parentNode === chip.parentNode, 'chip and send share the bar');
    assert.match(chip.textContent, /auto.*Opus 5\.5.*high/);
    assert.match(chip.className, /m-auto/);
    let items = null;
    global.SW.showMenu = (x, y, it) => { items = it; };
    chip.click();
    assert.deepStrictEqual(items.filter((i) => i.header).map((i) => i.header), ['Mode', 'Model', 'Effort']);
    assert.ok(items.some((i) => i.label === '✓ high'), 'current choice ticked');
    items.find((i) => i.label && /Sonnet/.test(i.label)).action();
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1], { t: 'model', model: 'sonnet' });
    assert.match(chip.textContent, /Sonnet/);
    items.find((i) => i.label && /plan$/.test(i.label)).action();
    assert.deepStrictEqual(ws.sent[ws.sent.length - 1], { t: 'mode', mode: 'plan' });
    feed({ t: 'e', e: { type: '_effort', effort: 'low' } });
    assert.match(chip.textContent, /low/);
    global.SW.showMenu = () => {};
  },

  'markdown: a code block inside a list item is a code block with copy, not text'() {
    const md = global.SWChat.markdown('- بعد این دستور را اجرا کنید:\n   ```bash\n   tax-api-service/build-release.sh --publish\n     indented\n   ```\n- بعدی\n\n1. یک\n\n   ```\n   ls -la\n   ```\n\nمتن');
    const blocks = md.querySelectorAll('.codeblock');
    assert.strictEqual(blocks.length, 2);
    assert.strictEqual(blocks[0].querySelector('code').textContent, 'tax-api-service/build-release.sh --publish\n  indented');
    assert.strictEqual(blocks[0].querySelector('.cb-copy').textContent, 'copy');
    assert.strictEqual(md.querySelectorAll('li')[0].querySelectorAll('.codeblock').length, 1, 'inside its list item');
    assert.strictEqual(md.querySelectorAll('li').length, 3, 'the list goes on after the code');
    assert.strictEqual(blocks[1].querySelector('code').textContent, 'ls -la');
    assert.ok(!/```/.test(md.textContent), 'no raw fences');
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
