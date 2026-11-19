// Chat state sounds (gui/static/sounds.js) under node:  node tests/js/sounds_test.js
'use strict';
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..', '..');
global.window = global;
const winL = {};
global.addEventListener = (t, f) => { (winL[t] = winL[t] || []).push(f); };
global.removeEventListener = (t, f) => { winL[t] = (winL[t] || []).filter((x) => x !== f); };
global.localStorage = { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; }, setItem(k, v) { this._d[k] = String(v); } };

// fake Web Audio: remembers the notes played
let notes = [], contexts = 0;
class FakeCtx {
  constructor() { contexts++; this.state = 'suspended'; this.currentTime = 10; this.destination = {}; }
  resume() { this.state = 'running'; }
  createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} }; }
  createOscillator() {
    const o = { frequency: { setValueAtTime: (f, t) => { o.f = f; o.t = t; } }, connect() {}, start() { notes.push({ f: o.f, t: o.t, wave: o.type }); }, stop() {} };
    return o;
  }
}
// fake speech synthesis
let spoken = [], voiceList = [];
global.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
global.speechSynthesis = { speak(u) { spoken.push(u); }, getVoices: () => voiceList };
global.AudioContext = FakeCtx;

require(path.join(ROOT, 'gui/static/sounds.js'));
const SS = global.SWSound;
const results = [];
function test(name, fn) {
  notes = []; spoken = []; voiceList = []; localStorage._d = {}; SS._reset();
  try { fn(); results.push('ok   ' + name); } catch (e) { results.push('FAIL ' + name); console.log(e.stack); process.exitCode = 1; }
}

test('defaults: an English voice for every kind; saved per device', () => {
  localStorage._d['sw-sounds'] = JSON.stringify({ done: 'chime' });          // saved by 1.43.0: not used
  const s = SS.get();
  assert.deepStrictEqual(SS.KINDS.map((k) => k[0]), ['done', 'question', 'permission', 'plan', 'error']);
  SS.KINDS.forEach((k) => assert.strictEqual(s[k[0]], 'voice'));
  assert.strictEqual(s.lang, 'en');
  SS.set({ done: 'chime', question: 'off', lang: 'fa', junk: 'x' });
  assert.deepStrictEqual(JSON.parse(localStorage._d['sw-sounds-2']), { done: 'chime', question: 'off', permission: 'voice', plan: 'voice', error: 'voice', lang: 'fa' });
  assert.strictEqual(SS.get().done, 'chime');
  localStorage._d['sw-sounds-2'] = '{broken';
  assert.strictEqual(SS.get().done, 'voice', 'a broken value falls back to the defaults');
});

test('each kind has its own tune', () => {
  SS.set({ done: 'chime', question: 'chime', permission: 'chime', plan: 'chime', error: 'chime' });
  const tunes = SS.KINDS.map((k) => {
    notes = []; SS._reset();
    assert.strictEqual(SS.play(k[0], 'shop'), 'chime');
    assert.ok(notes.length >= 2, k[0]);
    return notes.map((n) => n.f).join(',');
  });
  assert.strictEqual(new Set(tunes).size, tunes.length, 'all different');
  notes = []; SS._reset();
  SS.play('done'); const up = notes.map((n) => n.f);
  notes = []; SS._reset();
  SS.play('error'); const down = notes.map((n) => n.f);
  assert.ok(up[1] > up[0], 'finished goes up'); assert.ok(down[1] < down[0], 'error goes down');
});

test('two chats at once: the second tune waits for the first', () => {
  SS.play('done', '', { mode: 'chime' }); SS.play('question', '', { mode: 'chime' });
  const firstEnd = Math.max(...notes.slice(0, 2).map((n) => n.t));
  assert.ok(notes[2].t > firstEnd + 0.2, 'no overlap');
});

test('voice says the project and what happened; Persian when there is a Persian voice', () => {
  assert.strictEqual(SS.play('done', 'avahesab-backend'), 'voice');
  assert.strictEqual(spoken[0].text, 'Your task in avahesab backend is done');
  assert.strictEqual(spoken[0].lang, 'en-US');
  assert.strictEqual(notes.length, 0, 'no tone with the voice');
  SS.set({ lang: 'fa' });
  voiceList = [{ lang: 'en-US', name: 'E' }, { lang: 'fa-IR', name: 'Dilara' }];
  SS.play('question', 'shop');
  assert.strictEqual(spoken[1].text, 'کلود توی shop ازت سؤال داره');
  assert.strictEqual(spoken[1].lang, 'fa-IR'); assert.strictEqual(spoken[1].voice.name, 'Dilara');
  assert.ok(SS.hasVoice('fa'));
  voiceList = [{ lang: 'en-US', name: 'E' }];                 // no Persian voice on this device
  SS.play('question', 'shop');
  assert.strictEqual(spoken[2].text, 'Claude has a question for you in shop'); assert.strictEqual(spoken[2].voice.name, 'E');
  assert.ok(!SS.hasVoice('fa'));
  assert.strictEqual(SS.phrase('plan', '', 'en'), 'Your plan is ready');
  assert.strictEqual(SS.phrase('done', '', 'en'), 'Your task is done');
  assert.strictEqual(SS.phrase('permission', 'shop', 'en'), 'Claude needs your permission in shop');
  assert.strictEqual(SS.phrase('error', 'shop', 'en'), 'Your task in shop stopped with an error');
});

test('off is quiet; no speech in the browser → the tone; trying a choice before saving', () => {
  SS.set({ done: 'off' });
  assert.strictEqual(SS.play('done', 'x'), 'off');
  assert.strictEqual(notes.length + spoken.length, 0);
  const ss = global.speechSynthesis; delete global.speechSynthesis;
  assert.strictEqual(SS.play('plan', 'x', { mode: 'voice' }), 'chime');
  global.speechSynthesis = ss;
  assert.strictEqual(SS.play('done', 'x', { mode: 'voice', lang: 'en' }), 'voice', 'opts win over the saved choice');
  const A = global.AudioContext; delete global.AudioContext; SS._reset();
  assert.strictEqual(SS.play('done', 'x', { mode: 'chime' }), 'off', 'no Web Audio: nothing breaks');
  global.AudioContext = A;
});

test('the first touch on the page wakes the audio up', () => {
  assert.strictEqual(winL.pointerdown.length, 1);
  winL.pointerdown[0]();
  assert.strictEqual(winL.pointerdown.length, 0, 'only once');
  SS.play('done', '', { mode: 'chime' });
  assert.ok(notes.length);
});

test('read aloud: markdown to plain words, code not read, each piece in its own language', () => {
  const md = '## Done\n\n**`ng serve`** must rebuild. See [the docs](https://x.y/z) or https://a.b/c\n\n```ts\nconst a = 1;\n```\n\n- item one\n\n| a | b |\n|---|---|\n\nاین‌جا نمی‌توانم کامپایل کنم. اگر خطایی آمد، متنش را بفرستید.';
  const plain = SS.plainText(md);
  assert.ok(!/const a|https|\*\*|`|##|\|/.test(plain), plain);
  assert.match(plain, /ng serve must rebuild\. See the docs/);
  voiceList = [{ lang: 'fa-IR', name: 'fa' }, { lang: 'en-US', name: 'en' }];
  let ended = 0;
  const pieces = SS.read(md, () => ended++);
  assert.ok(pieces.length >= 3);
  assert.strictEqual(spoken.length, pieces.length);
  const fa = spoken.find((u) => /کامپایل/.test(u.text));
  assert.strictEqual(fa.lang, 'fa-IR');
  assert.strictEqual(fa.voice.name, 'fa');
  assert.strictEqual(spoken.find((u) => /ng serve/.test(u.text)).lang, 'en-US');
  assert.ok(SS.isReading());
  spoken.forEach((u) => u.onend());
  assert.strictEqual(ended, 1);
  assert.ok(!SS.isReading());
});

test('read aloud: long text in short pieces; stop cancels and says it ended; no speech → false', () => {
  const long = Array(40).fill('This is one sentence of a long answer.').join(' ');
  assert.ok(SS.chunks(long).every((c) => c.length <= 220));
  assert.strictEqual(SS.chunks(long).join(' ').length, long.length);
  let cancelled = 0, ended = 0;
  global.speechSynthesis.cancel = () => cancelled++;
  SS.read(long, () => ended++);
  SS.stopReading();
  assert.ok(cancelled >= 1);
  assert.strictEqual(ended, 1);
  assert.ok(!SS.isReading());
  spoken.forEach((u) => u.onend());
  assert.strictEqual(ended, 1, 'late ends of a stopped reading change nothing');
  const sp = global.speechSynthesis; delete global.speechSynthesis;
  assert.strictEqual(SS.read('hi'), false);
  global.speechSynthesis = sp;
});

console.log(results.join('\n'));
if (results.some((r) => r.startsWith('FAIL'))) process.exitCode = 1;
