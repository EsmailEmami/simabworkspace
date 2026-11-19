// Dictation (gui/static/voice.js) under node:  node tests/js/voice_test.js
'use strict';
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..', '..');

// ---------------------------------------------------------------- a fake clock (setTimeout + rAF)
let now = 0, timers = [], tid = 0;
function fakeSet(f, ms) { const id = ++tid; timers.push({ id, at: now + (ms || 0), f }); return id; }
function fakeClear(id) { timers = timers.filter((t) => t.id !== id); }
function tick(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift();
    now = t.at;
    t.f();
  }
  now = end;
}
global.setTimeout = fakeSet;
global.clearTimeout = fakeClear;

// ---------------------------------------------------------------- a small DOM with closest/matches
function parseSel(sel) {
  return sel.split(',').map((one) => {
    const parts = []; const re = /\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]|^([a-zA-Z]+)/g; let m;
    one = one.trim();
    while ((m = re.exec(one))) parts.push(m[1] ? { cls: m[1] } : m[2] ? { attr: m[2], val: m[3] } : { tag: m[4] });
    return parts;
  });
}
class El {
  constructor(tag, attrs, ...kids) {
    this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this.style = {}; this.value = ''; this._text = '';
    this.disabled = false; this.readOnly = false; this.rect = { left: 100, top: 100, right: 400, bottom: 130, width: 300, height: 30 };
    const self = this;
    this.classList = {
      contains: (c) => self.cls().includes(c),
      toggle: (c, on) => { const a = self.cls().filter((x) => x !== c); if (on) a.push(c); self.className = a.join(' '); }
    };
    Object.entries(attrs || {}).forEach(([k, v]) => this.setAttribute(k, v));
    kids.forEach((k) => this.appendChild(k));
  }
  cls() { return (this.className || '').split(/\s+/).filter(Boolean); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get id() { return this.attrs.id || ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter((x) => x !== this); this.parentNode = null; } }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  get isConnected() { return global.document.body.contains(this); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  matches(sel) {
    return parseSel(sel).some((parts) => parts.length && parts.every((p) =>
      p.cls ? this.cls().includes(p.cls) : p.attr ? (p.attr in this.attrs && (p.val === undefined || this.attrs[p.attr] === p.val)) : this.tagName === p.tag.toUpperCase()));
  }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n.matches && n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    const out = []; const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); }); walk(this); return out;
  }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f); }
  dispatchEvent(ev) { ev.target = this; (this.listeners[ev.type] || []).slice().forEach((f) => f(ev)); return true; }
  emit(type) { let prevented = false; this.dispatchEvent({ type, preventDefault() { prevented = true; }, stopPropagation() {} }); return prevented; }
  setSelectionRange(a, b) { this.selStart = a; this.selEnd = b; }
  getBoundingClientRect() { return this.rect; }
  count(type) { return (this.fired || []).filter((x) => x === type).length; }
}
const el = (tag, attrs, ...kids) => new El(tag, attrs, ...kids);
global.Event = class { constructor(type, o) { this.type = type; this.bubbles = !!(o && o.bubbles); } };
global.document = { body: el('body'), activeElement: null, createElement: (t) => el(t) };
global.window = global;
global.innerWidth = 1200; global.innerHeight = 800;
global.requestAnimationFrame = (f) => fakeSet(f, 16);
global.cancelAnimationFrame = fakeClear;
const winL = {};
global.addEventListener = (t, f) => { (winL[t] = winL[t] || []).push(f); };
global.removeEventListener = (t, f) => { winL[t] = (winL[t] || []).filter((x) => x !== f); };
global.localStorage = { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } };
let observers = [];
global.MutationObserver = class { constructor(cb) { this.cb = cb; observers.push(this); } observe(t, o) { this.target = t; this.opts = o; } disconnect() { this.off = true; } };
global.ResizeObserver = class { observe() {} disconnect() {} };
// count the input/change events a field gets
function track(f) { f.fired = []; ['input', 'change'].forEach((t) => f.addEventListener(t, () => f.fired.push(t))); return f; }

// ---------------------------------------------------------------- a fake recognizer
let recs = [];
class FakeRec {
  constructor() { this.started = 0; this.stopped = 0; this.aborted = 0; recs.push(this); }
  start() { if (FakeRec.throwOnce) { FakeRec.throwOnce = false; throw new Error('InvalidStateError'); } this.started++; }
  stop() { this.stopped++; }
  abort() { this.aborted++; }
  // results: [['text', final?, ...alternatives]]
  say(results) {
    const list = results.map(([t, fin, ...alts]) => { const r = [{ transcript: t }].concat(alts.map((a) => ({ transcript: a }))); r.isFinal = !!fin; return r; });
    if (this.onresult) this.onresult({ resultIndex: list.length - 1, results: list });
  }
  fail(code) { if (this.onerror) this.onerror({ error: code }); }
  end() { if (this.onend) this.onend(); }
}
const last = () => recs[recs.length - 1];

require(path.join(ROOT, 'gui/static/voice.js'));
const V = global.SWVoice;
let supported = true;
const newSpeech = () => new V.Speech({ ctor: () => (supported ? FakeRec : null), setTimeout: fakeSet, clearTimeout: fakeClear });
function collect(stream) { const out = []; stream.subscribe((s) => out.push(s)); return out; }
const results = [];
function test(name, fn) {
  recs = []; timers = []; observers = []; supported = true; FakeRec.throwOnce = false;
  global.document.body = el('body'); global.document.activeElement = null;
  try { fn(); results.push('ok   ' + name); } catch (e) { results.push('FAIL ' + name); console.log(e.stack); process.exitCode = 1; }
}

// ================================================================= isEligible
test('eligible: free-text fields', () => {
  const yes = [el('textarea'), el('input'), el('input', { type: 'text' }), el('input', { type: 'search' }), el('input', { type: 'url' }),
    el('input', { type: 'TEXT' }), el('div', { contenteditable: 'true' }), el('div', { contenteditable: '' }), el('div', { contenteditable: 'plaintext-only' })];
  yes.forEach((f) => assert.ok(V.isEligible(f), f.tagName + JSON.stringify(f.attrs)));
  ['sharh', 'tozihat', 'name', 'address', 'title', 'description', 'idea', 'comment', 'message', 'notes', 'firstName', 'sharhKala', 'body', 'subject']
    .forEach((n) => {
      assert.ok(V.isEligible(el('input', { name: n })), 'name ' + n);
      assert.ok(V.isEligible(el('textarea', { formcontrolname: n })), 'formcontrolname ' + n);
      assert.ok(V.isEligible(el('input', { id: n })), 'id ' + n);
    });
});

test('not eligible: other types, modes, autocomplete', () => {
  ['number', 'date', 'password', 'email', 'tel', 'checkbox', 'file', 'color', 'time', 'datetime-local', 'hidden', 'range']
    .forEach((t) => assert.ok(!V.isEligible(el('input', { type: t })), t));
  ['numeric', 'decimal', 'tel'].forEach((m) => assert.ok(!V.isEligible(el('input', { inputmode: m })), m));
  ['one-time-code', 'tel', 'tel-national', 'cc-number', 'cc-csc', 'username', 'current-password', 'new-password', 'section-x username']
    .forEach((a) => assert.ok(!V.isEligible(el('input', { autocomplete: a })), a));
  assert.ok(!V.isEligible(el('div', { contenteditable: 'false' })));
  assert.ok(!V.isEligible(el('div')));
  assert.ok(!V.isEligible(el('select')));
  assert.ok(!V.isEligible(null));
});

test('not eligible: ID / number / date names', () => {
  ['mobile', 'phoneNumber', 'tel', 'shomarehHesab', 'shomarehhesab', 'codePosti', 'codeposti', 'postal-code', 'user_id', 'userId', 'nationalCode',
    'melliCode', 'sheba', 'shabaNumber', 'iban', 'card-number', 'kartNo', 'otp', 'password', 'captcha', 'serial', 'pin', 'token', 'username',
    'email', 'id', 'shenase', 'apiKey', 'certificate', 'url', 'ip', 'mac', 'port', 'plaque', 'pelak', 'version', 'mablagh', 'price-total',
    'tedad', 'count', 'qty', 'amount', 'meghdar', 'nerkh', 'rate', 'darsad', 'percent', 'vazn', 'ghimat', 'fee', 'karmozd', 'mojodi',
    'hoghogh', 'takhfif', 'discount', 'number', 'tarikhTavalod', 'start_date', 'saat', 'time', 'host', 'remotePath', 'branch', 'cwd', 'item2Price']
    .forEach((n) => {
      assert.ok(!V.isEligible(el('input', { name: n })), 'name ' + n);
      assert.ok(!V.isEligible(el('textarea', { formcontrolname: n })), 'formcontrolname ' + n);
      assert.ok(!V.isEligible(el('input', { id: n })), 'id ' + n);
    });
});

test('not eligible: inside widgets, code fields, the terminal, opt-out', () => {
  const inside = (wrap) => { const f = el('input'); el('div', wrap, el('div', {}, f)); return f; };
  [{ 'data-voice-to-text': 'off' }, { class: 'xterm' }, { class: 'palette modal' }, { role: 'combobox' }, { role: 'listbox' },
    { class: 'ProseMirror' }, { class: 'ql-editor' }, { class: 'cm-editor' }]
    .forEach((w) => assert.ok(!V.isEligible(inside(w)), JSON.stringify(w)));
  assert.ok(!V.isEligible(el('input', { class: 'inp mono' })), 'code / path field');
  assert.ok(!V.isEligible(el('textarea', { class: 'files-edit' })), 'file editor');
  assert.ok(!V.isEligible(el('textarea', { 'data-voice-to-text': 'off' })));
  assert.ok(V.isEligible(inside({ class: 'modal' })), 'a normal dialog is fine');
  // disabled / read-only is checked at runtime by the attachment, not here
  const d = el('input'); d.disabled = true;
  assert.ok(V.isEligible(d));
});

// ================================================================= Speech
test('speech: interim replaced, final grows, auto-restart after Chrome ends the session', () => {
  const sp = newSpeech();
  const out = collect(sp.start());
  const r1 = last();
  assert.strictEqual(r1.lang, 'fa-IR'); assert.strictEqual(r1.continuous, true); assert.strictEqual(r1.interimResults, true);
  assert.strictEqual(r1.maxAlternatives, 3); assert.strictEqual(r1.started, 1);
  r1.say([['سلام', false]]);
  r1.say([['سلام دنیا', false]]);
  assert.deepStrictEqual(out[out.length - 1], { final: '', interim: 'سلام دنیا' });
  r1.say([['سلام دنیا', true], ['چطوری', false]]);
  assert.deepStrictEqual(out[out.length - 1], { final: 'سلام دنیا', interim: 'چطوری' });
  r1.say([['سلام دنیا', true], ['  چطوری ', true]]);
  assert.strictEqual(out[out.length - 1].final, 'سلام دنیا چطوری');
  r1.end();                                         // ~60 s later Chrome ends it
  assert.ok(sp.active);
  assert.deepStrictEqual(out[out.length - 1], { final: 'سلام دنیا چطوری', interim: '' });
  assert.strictEqual(recs.length, 1);
  tick(299); assert.strictEqual(recs.length, 1);
  tick(1); assert.strictEqual(recs.length, 2, 'restarted after 300 ms');
  const r2 = last();
  assert.strictEqual(r2.started, 1);
  r2.say([['خوبم', true]]);
  assert.strictEqual(out[out.length - 1].final, 'سلام دنیا چطوری خوبم', 'final keeps the earlier sessions');
  r1.say([['late', true]]);                         // the old recognizer: ignored
  assert.strictEqual(out[out.length - 1].final, 'سلام دنیا چطوری خوبم');
  assert.strictEqual(r1.onresult, null, 'old handlers dropped');
});

test('speech: backoff grows while nothing is heard, resets on a result', () => {
  const sp = newSpeech();
  collect(sp.start());
  const waits = [];
  for (let i = 0; i < 5; i++) {
    const n = recs.length;
    last().fail('no-speech'); last().end();
    let ms = 0; while (recs.length === n) { tick(100); ms += 100; assert.ok(ms < 5000); }
    waits.push(ms);
  }
  assert.deepStrictEqual(waits, [300, 600, 1200, 2000, 2000]);
  last().say([['x', false]]);
  const n = recs.length; last().end(); tick(300);
  assert.strictEqual(recs.length, n + 1, 'back to 300 ms after a real result');
});

test('speech: start() throwing (old one still winding down) schedules a restart', () => {
  const sp = newSpeech();
  FakeRec.throwOnce = true;
  const out = collect(sp.start());
  assert.strictEqual(recs.length, 1); assert.strictEqual(recs[0].started, 0);
  assert.ok(sp.active); assert.strictEqual(out.length, 0);
  tick(300);
  assert.strictEqual(recs.length, 2); assert.strictEqual(recs[1].started, 1);
});

test('speech: fatal errors end it with a message; non-fatal ones do not', () => {
  const sp = newSpeech();
  const out = collect(sp.start());
  last().fail('aborted'); last().fail('no-speech');
  assert.ok(sp.active); assert.strictEqual(out.length, 0);
  last().say([['done', true]]);
  last().fail('not-allowed');
  const s = out[out.length - 1];
  assert.ok(s.ended); assert.strictEqual(s.final, 'done');
  assert.strictEqual(s.error.code, 'not-allowed'); assert.ok(s.error.fatal); assert.ok(/Microphone/.test(s.error.message));
  assert.ok(!sp.active);
  assert.strictEqual(recs[0].aborted, 1);
  recs[0].end && recs[0].end(); tick(5000);
  assert.strictEqual(recs.length, 1, 'no restart after a fatal error');
  ['service-not-available', 'network', 'audio-capture', 'language-not-supported', 'bad-grammar'].forEach((code) => {
    const o = collect(sp.start()); last().fail(code);
    assert.ok(o[o.length - 1].ended && o[o.length - 1].error.code === code, code);
  });
});

test('speech: stop waits for the last finals and end; gives up after 1.5 s', () => {
  const sp = newSpeech();
  let out = collect(sp.start());
  last().say([['یک', true], ['دو', false]]);
  sp.stop();
  assert.strictEqual(last().stopped, 1); assert.ok(sp.active, 'still waiting for Chrome');
  last().say([['یک', true], ['دو سه', true]]);
  last().end();
  let s = out[out.length - 1];
  assert.deepStrictEqual(s, { final: 'یک دو سه', interim: '', ended: true });
  tick(5000); assert.strictEqual(recs.length, 1, 'no restart after stop');
  // a recognizer that never answers (permission prompt still open)
  out = collect(sp.start());
  sp.stop();
  tick(1499); assert.ok(sp.active);
  tick(1); assert.ok(!sp.active);
  s = out[out.length - 1];
  assert.ok(s.ended); assert.strictEqual(s.error, undefined);
});

test('speech: a second start takes the mic; the first owner ends with aborted', () => {
  const sp = newSpeech();
  const a = collect(sp.start());
  const ra = last();
  ra.say([['for A', true]]);
  const b = collect(sp.start({ lang: 'en-US' }));
  const sa = a[a.length - 1];
  assert.ok(sa.ended); assert.strictEqual(sa.error.code, 'aborted'); assert.strictEqual(sa.final, 'for A');
  assert.strictEqual(ra.aborted, 1); assert.strictEqual(ra.onend, null);
  assert.strictEqual(last().lang, 'en-US');
  ra.say([['ghost', true]]);
  assert.strictEqual(b.length, 0, 'the old recognizer cannot write into B');
  last().say([['for B', true]]);
  assert.strictEqual(b[b.length - 1].final, 'for B', 'B starts empty');
});

test('speech: unsupported browser → one ended snapshot, even for a late subscriber', () => {
  supported = false;
  const sp = newSpeech();
  assert.ok(!sp.isSupported());
  const stream = sp.start();
  const out = collect(stream);
  assert.strictEqual(out.length, 1);
  assert.ok(out[0].ended); assert.strictEqual(out[0].error.code, 'unsupported'); assert.ok(/Chrome/.test(out[0].error.message));
  assert.ok(!sp.active);
});

test('speech: rebuilds from all results; preferLatin picks the Latin alternative', () => {
  const sp = newSpeech();
  let out = collect(sp.start());
  last().onresult({ resultIndex: 1, results: [Object.assign([{ transcript: 'اول' }], { isFinal: true }), Object.assign([{ transcript: 'دوم' }], { isFinal: false })] });
  assert.deepStrictEqual(out[out.length - 1], { final: 'اول', interim: 'دوم' });
  out = collect(sp.start({ preferLatin: true }));
  last().say([['ری اکت', true, 'React', 'ریکت']]);
  assert.strictEqual(out[out.length - 1].final, 'React');
  out = collect(sp.start({ techWords: false }));
  last().say([['ری اکت', true, 'React']]);
  assert.strictEqual(out[out.length - 1].final, 'ری اکت');
});

test('tech words: English terms the Persian recognizer spelled in Persian letters become English', () => {
  assert.strictEqual(V.techWords('این کامپوننت ری اکت رو کامیت کن و پوش کن به گیت هاب'), 'این component React رو commit کن و push کن به GitHub');
  assert.strictEqual(V.techWords('ری‌اکت و داکر کامپوز، اند پوینت API'), 'React و docker compose، endpoint API');
  assert.strictEqual(V.techWords('پوشه گیتی پولی'), 'پوشه گیتی پولی', 'never inside a longer Persian word');
  assert.strictEqual(V.techWords('سرور و فایل و باگ'), 'سرور و فایل و باگ', 'Persian loanwords stay');
  assert.strictEqual(V.techWords('hello git'), 'hello git');
  const sp = newSpeech();
  let out = collect(sp.start());
  last().say([['دیتابیس پستگرس رو ریستارت کن', true]]);
  assert.strictEqual(out[out.length - 1].final, 'database Postgres رو ریستارت کن');
  out = collect(sp.start({ lang: 'en-US' }));
  last().say([['git push', true]]);
  assert.strictEqual(out[out.length - 1].final, 'git push');
});

test('speech: a guess that never becomes final is kept, and listening goes on', () => {
  const sp = newSpeech();
  const out = collect(sp.start());
  last().say([['سلام', true], ['اینو با ری', false]]);       // an English word: Chrome hangs on its guess
  tick(V.STALL_MS - 1);
  assert.strictEqual(recs.length, 1);
  tick(1);
  assert.deepStrictEqual(out[out.length - 1], { final: 'سلام اینو با ری', interim: '' });
  assert.ok(sp.active);
  tick(300);
  assert.strictEqual(recs.length, 2, 'listening again');
  last().say([['ادامه', true]]);
  assert.strictEqual(out[out.length - 1].final, 'سلام اینو با ری ادامه');
  tick(10000);
  assert.strictEqual(recs.length, 2, 'finals only: no stall restart');
  last().say([['ادامه', true], ['یه چیز', false]]);
  last().end();                                              // Chrome ended with only a guess: kept
  assert.strictEqual(out[out.length - 1].final, 'سلام اینو با ری ادامه یه چیز');
});

test('speech: a network drop after words were heard is retried, not the end of dictation', () => {
  const sp = newSpeech();
  const out = collect(sp.start());
  last().say([['یک', true]]);
  for (let i = 0; i < 3; i++) {
    last().fail('network');
    assert.ok(sp.active, 'drop ' + (i + 1) + ' retried');
    assert.ok(!out[out.length - 1].ended);
    tick(2000);
  }
  last().fail('network');                                  // 4th in a row with nothing heard between
  assert.ok(out[out.length - 1].ended);
  assert.strictEqual(out[out.length - 1].error.code, 'network');
  assert.strictEqual(out[out.length - 1].final, 'یک');
  // a word between the drops resets the count
  const o2 = collect(sp.start());
  last().say([['الف', true]]);
  for (let i = 0; i < 6; i++) { last().fail('network'); tick(2000); last().say([['ب' + i, true]]); }
  assert.ok(sp.active);
  assert.ok(!o2[o2.length - 1].ended);
});

test('speech: Android repeating earlier finals inside the next one is not doubled', () => {
  const sp = newSpeech();
  const out = collect(sp.start());
  last().say([['سلام', true], ['سلام خوبی', true], ['سلام خوبی چطوری', true]]);
  assert.strictEqual(out[out.length - 1].final, 'سلام خوبی چطوری');
  last().say([['یک', true], ['دو', true]]);
  assert.strictEqual(out[out.length - 1].final, 'یک دو');
});

test('join keeps one space and a trailing newline', () => {
  assert.strictEqual(V.join('a', 'b'), 'a b');
  assert.strictEqual(V.join('a ', 'b'), 'a b');
  assert.strictEqual(V.join('a\n', 'b'), 'a\nb');
  assert.strictEqual(V.join('', ' b '), 'b');
  assert.strictEqual(V.join('a', ''), 'a');
  assert.strictEqual(V.join('  ', 'b'), 'b');
});

// ================================================================= Attachment
function setup(value, opts) {
  const sp = newSpeech();
  const f = track(el('textarea'));
  f.value = value || '';
  global.document.body.appendChild(f);
  const notes = [];
  const a = new V.Attachment(f, Object.assign({ speech: sp, notify: (m) => notes.push(m) }, opts || {}), {});
  return { sp, f, a, notes };
}

test('field: appends with a space; live text without events; finals fire input + change', () => {
  const { f, a } = setup('نوشته قبلی');
  a.btn.emit('click');
  assert.ok(a.recording); assert.ok(a.btn.classList.contains('vtt-rec'));
  assert.strictEqual(a.btn.getAttribute('aria-pressed'), 'true');
  last().say([['سلام', false]]);
  assert.strictEqual(f.value, 'نوشته قبلی سلام');
  assert.strictEqual(f.fired.length, 0, 'interim never reaches the form model');
  last().say([['سلام', true], ['خوبی', false]]);
  assert.strictEqual(f.value, 'نوشته قبلی سلام خوبی');
  assert.deepStrictEqual(f.fired, ['input', 'change']);
  assert.strictEqual(f.selStart, f.value.length, 'caret at the end');
  last().end(); tick(300);                          // Chrome restarts: the last guess is kept, nothing doubled
  assert.strictEqual(f.value, 'نوشته قبلی سلام خوبی');
  last().say([['چطوری', true]]);
  assert.strictEqual(f.value, 'نوشته قبلی سلام خوبی چطوری');
  a.btn.emit('click');                               // stop
  last().end();
  assert.ok(!a.recording); assert.ok(!a.btn.classList.contains('vtt-rec'));
  assert.strictEqual(f.value, 'نوشته قبلی سلام خوبی چطوری');
  assert.strictEqual(f.count('input'), 3);
});

test('field: append=false replaces; a field cleared by the page is not refilled', () => {
  const sp = newSpeech();
  const f = track(el('input')); f.value = 'old'; global.document.body.appendChild(f);
  const a = new V.Attachment(f, { speech: sp }, { append: () => false });
  a.start();
  last().say([['new', true]]);
  assert.strictEqual(f.value, 'new');
  f.value = '';                                      // e.g. the chat sent the message
  last().say([['new', true], ['more', false]]);
  assert.strictEqual(f.value, 'more', 'old finals are not written back');
  last().say([['new', true], ['more', true]]);
  assert.strictEqual(f.value, 'more');
  f.value = 'typed';                                 // the user typed in between
  last().say([['new', true], ['more', true], ['again', true]]);
  assert.strictEqual(f.value, 'typed again');
});

test('field: starting on B stops A cleanly', () => {
  const sp = newSpeech();
  const A = track(el('textarea')), B = track(el('textarea'));
  const a = new V.Attachment(A, { speech: sp }, {}), b = new V.Attachment(B, { speech: sp }, {});
  a.start(); last().say([['for a', true], ['half', false]]);
  assert.strictEqual(A.value, 'for a half');
  b.start();
  assert.ok(!a.recording); assert.ok(b.recording);
  assert.strictEqual(A.value, 'for a', 'A keeps its final text, the half word is gone');
  last().say([['for b', true]]);
  assert.strictEqual(B.value, 'for b'); assert.strictEqual(A.value, 'for a');
});

test('field: denied mic → one message, back to idle; no API → mic-slash + message', () => {
  let { a, notes } = setup('');
  a.start(); last().fail('not-allowed'); last().end && last().end();
  assert.strictEqual(notes.length, 1); assert.ok(/Microphone/.test(notes[0]));
  assert.ok(!a.recording); assert.strictEqual(a.btn.getAttribute('aria-pressed'), 'false');
  ({ a, notes } = setup(''));
  a.start(); last().fail('no-speech'); last().fail('aborted');
  assert.strictEqual(notes.length, 0, 'no toast for silence');
  supported = false;
  ({ a, notes } = setup(''));
  a.btn.emit('click');
  assert.ok(!a.recording); assert.ok(a.btn.classList.contains('vtt-unsup'));
  assert.strictEqual(notes.length, 1); assert.ok(/Chrome/.test(notes[0]));
  assert.ok(/line x1="3"|M3 3l18 18/.test(a.btn.innerHTML), 'mic-slash icon');
});

test('field: the button never takes the focus; disabled fields do not record', () => {
  const { f, a } = setup('');
  assert.strictEqual(a.btn.getAttribute('type'), 'button');
  assert.strictEqual(a.btn.getAttribute('tabindex'), '-1');
  assert.ok(a.btn.emit('pointerdown'), 'pointerdown prevented');
  assert.ok(a.btn.emit('mousedown'), 'mousedown prevented');
  f.disabled = true;
  a.start(); assert.ok(!a.recording);
  f.disabled = false; a.start(); assert.ok(a.recording);
  f.readOnly = true; a.refresh();
  assert.ok(!a.recording, 'became read-only while recording → aborted');
});

test('field: floating button shows on focus + hover, hides a moment after, follows the field', () => {
  const { f, a } = setup('');
  const scrolls = () => (winL.scroll || []).filter((x) => x === a.onMove).length;
  assert.ok(global.document.body.contains(a.btn)); assert.strictEqual(a.btn.className, 'vtt-btn');
  assert.strictEqual(a.btn.style.display, 'none');
  f.emit('pointerenter'); assert.ok(!a.visible, 'hover alone: no');
  f.emit('focus'); assert.ok(a.visible);
  assert.strictEqual(a.btn.style.left, '406px'); assert.strictEqual(a.btn.style.top, '101px');
  assert.strictEqual(scrolls(), 1, 'scroll listener only while visible');
  f.emit('pointerleave'); assert.ok(a.visible, 'not at once');
  a.btn.emit('pointerenter'); tick(200); assert.ok(a.visible, 'pointer reached the button');
  a.btn.emit('pointerleave'); tick(159); assert.ok(a.visible);
  tick(1); assert.ok(!a.visible);
  assert.strictEqual(scrolls(), 0);
  f.emit('pointerenter'); assert.ok(a.visible);
  a.start(); f.emit('blur'); f.emit('pointerleave'); tick(500);
  assert.ok(a.visible, 'always visible while recording');
  f.rect = { left: 100, top: 300, right: 400, bottom: 330, width: 300, height: 30 };
  a.onMove(); tick(20);
  assert.strictEqual(a.btn.style.top, '301px', 'follows on scroll');
  a.destroy();
  assert.ok(!a.recording); assert.ok(!global.document.body.contains(a.btn));
  assert.strictEqual((f.listeners.focus || []).length, 0);
});

test('placement: right side, left near the edge, inside as last resort; tall textareas use the first line', () => {
  assert.deepStrictEqual(V.placeFor({ left: 100, top: 100, right: 400, height: 30, width: 300 }, 1200), { left: 406, top: 101 });
  assert.deepStrictEqual(V.placeFor({ left: 100, top: 100, right: 1190, height: 30, width: 1090 }, 1200), { left: 66, top: 101 });
  assert.deepStrictEqual(V.placeFor({ left: 0, top: 100, right: 1200, height: 30, width: 1200 }, 1200), { left: 1168, top: 101 });
  assert.deepStrictEqual(V.placeFor({ left: 100, top: 100, right: 400, height: 300, width: 300 }, 1200), { left: 406, top: 106 });
});

test('inline button (chat composer): no floating, onWrite called, language switch', () => {
  const sp = newSpeech();
  const f = track(el('textarea')); const btn = el('button', { class: 'cb-mic' });
  const writes = [], notes = [];
  const a = V.attach(f, { speech: sp, button: btn, notify: (m) => notes.push(m), onWrite: (v, fin) => writes.push([v, fin]) });
  assert.strictEqual(f.getAttribute('data-voice-to-text'), '1');
  assert.strictEqual(a.btn, btn); assert.ok(!global.document.body.contains(btn));
  assert.ok(/Persian/.test(btn.getAttribute('title')));
  btn.emit('click'); assert.strictEqual(last().lang, 'fa-IR');
  last().say([['hi', false]]); last().say([['hi', true]]);
  assert.deepStrictEqual(writes, [['hi', false], ['hi', true]]);
  btn.emit('contextmenu');
  assert.strictEqual(V.lang(), 'en-US'); assert.ok(/English/.test(notes[0]));
  assert.ok(a.recording); assert.strictEqual(last().lang, 'en-US', 'restarted in the new language');
  a.stop(); last().end();
  assert.ok(/English/.test(btn.getAttribute('title')));
  btn.emit('contextmenu'); assert.strictEqual(V.lang(), 'fa-IR');
});

// ================================================================= attachAll
test('attachAll: every eligible field, now and later; never inside skipped widgets', () => {
  const sp = newSpeech();
  const body = global.document.body;
  const ta = el('textarea'), name = el('input', { class: 'inp' }), code = el('input', { class: 'inp mono' }), pw = el('input', { type: 'password' });
  const own = el('textarea', { 'data-voice-to-text': '1' });
  const term = el('textarea', { class: 'xterm-helper-textarea' });
  body.appendChild(el('div', {}, ta, name, code, pw, own, el('div', { class: 'xterm' }, term)));
  const all = V.attachAll(body, { speech: sp });
  assert.deepStrictEqual([...all.attachments.keys()], [ta, name]);
  const mo = observers[0];
  assert.strictEqual(mo.target, body); assert.deepStrictEqual(mo.opts, { childList: true, subtree: true });
  // a dialog opens
  const q = el('input', { placeholder: 'Other answer…' }), num = el('input', { inputmode: 'numeric' });
  const dlg = el('div', { class: 'overlay' }, el('div', { class: 'modal' }, q, num));
  body.appendChild(dlg);
  mo.cb([{ addedNodes: [dlg, dlg.children[0]], removedNodes: [] }]);
  assert.ok(!all.attachments.has(q), 'batched to the next frame');
  tick(16);
  assert.ok(all.attachments.has(q)); assert.ok(!all.attachments.has(num));
  assert.strictEqual(all.attachments.size, 3);
  // it closes
  const qa = all.attachments.get(q);
  dlg.remove();
  mo.cb([{ addedNodes: [], removedNodes: [dlg] }]); tick(16);
  assert.ok(!all.attachments.has(q)); assert.ok(qa.destroyed);
  // a huge batch: one full rescan
  const many = []; for (let i = 0; i < 250; i++) many.push(el('span'));
  const late = el('textarea'); body.appendChild(late);
  mo.cb([{ addedNodes: many.concat([late]), removedNodes: [] }]); tick(16);
  assert.ok(all.attachments.has(late));
  all.destroy();
  assert.ok(mo.off); assert.strictEqual(all.attachments.size, 0);
});

console.log(results.join('\n'));
if (results.some((r) => r.startsWith('FAIL'))) process.exitCode = 1;
