// Sounds when a chat changes state — a different one per kind (finished, question, permission,
// plan ready, error), or a voice that says it ("simab: task done"). Per device (localStorage):
// the laptop can talk while the tablet stays quiet. No files: the tones are made with Web Audio,
// the voice is the browser's own speechSynthesis.
(function (root) {
  'use strict';

  var KINDS = [
    ['done', 'Task done'],
    ['question', 'Claude has a question'],
    ['permission', 'Claude needs permission'],
    ['plan', 'Plan ready'],
    ['error', 'Stopped with an error']
  ];
  var MODES = [['off', 'Off'], ['chime', 'Sound'], ['voice', 'Voice']];
  var LANGS = [['en', 'English'], ['fa', 'Persian']];
  var DEFAULTS = { done: 'voice', question: 'voice', permission: 'voice', plan: 'voice', error: 'voice', lang: 'en' };
  var KEY = 'sw-sounds-2';         // 1.43.1: voice by default — choices saved by 1.43.0 (tunes) are not kept

  // [frequency Hz, start s, length s] — each kind its own little tune
  var TUNES = {
    done: { wave: 'sine', notes: [[660, 0, 0.12], [880, 0.13, 0.24]] },                         // up: finished
    question: { wave: 'sine', notes: [[880, 0, 0.09], [880, 0.14, 0.09], [1175, 0.28, 0.26]] },  // ding ding DING?
    permission: { wave: 'triangle', notes: [[988, 0, 0.11], [740, 0.15, 0.11], [988, 0.3, 0.11], [740, 0.45, 0.11]] },
    plan: { wave: 'sine', notes: [[523, 0, 0.11], [659, 0.12, 0.11], [784, 0.24, 0.28]] },       // arpeggio
    error: { wave: 'triangle', notes: [[440, 0, 0.18], [330, 0.2, 0.32]] }                       // down
  };

  // {in} = " in <project>" (or nothing)
  var PHRASES = {
    en: { done: 'Your task{in} is done', question: 'Claude has a question for you{in}', permission: 'Claude needs your permission{in}',
      plan: 'Your plan{in} is ready', error: 'Your task{in} stopped with an error' },
    fa: { done: 'کارت{in} تموم شد', question: 'کلود{in} ازت سؤال داره', permission: 'کلود{in} اجازه می‌خواد',
      plan: 'پلن{in} آماده‌ست', error: 'کارت{in} با خطا متوقف شد' }
  };
  var IN = { en: ' in ', fa: ' توی ' };

  function get() {
    var out = {};
    Object.keys(DEFAULTS).forEach(function (k) { out[k] = DEFAULTS[k]; });
    try {
      var saved = JSON.parse((root.localStorage && root.localStorage.getItem(KEY)) || '{}') || {};
      Object.keys(DEFAULTS).forEach(function (k) { if (typeof saved[k] === 'string') out[k] = saved[k]; });
    } catch (e) { /* broken value: defaults */ }
    return out;
  }

  function set(s) {
    var cur = get();
    Object.keys(DEFAULTS).forEach(function (k) { if (s && typeof s[k] === 'string') cur[k] = s[k]; });
    try { root.localStorage.setItem(KEY, JSON.stringify(cur)); } catch (e) { /* private mode */ }
    return cur;
  }

  // ---- tones
  var ctx = null, nextFree = 0;
  function audio() {
    if (ctx) return ctx;
    var C = root.AudioContext || root.webkitAudioContext;
    if (!C) return null;
    try { ctx = new C(); } catch (e) { ctx = null; }
    return ctx;
  }

  // browsers allow sound only after the user touched the page once: wake the audio up then
  function unlock() {
    var c = audio();
    if (c && c.state === 'suspended' && c.resume) { try { c.resume(); } catch (e) { /* ignore */ } }
  }

  function chime(kind) {
    var c = audio(), tune = TUNES[kind];
    if (!c || !tune) return false;
    if (c.state === 'suspended' && c.resume) { try { c.resume(); } catch (e) { /* ignore */ } }
    // several chats at once: one after the other, never on top of each other
    var t0 = Math.max(c.currentTime + 0.02, nextFree), end = t0;
    tune.notes.forEach(function (n) {
      var osc = c.createOscillator(), g = c.createGain();
      osc.type = tune.wave;
      osc.frequency.setValueAtTime(n[0], t0 + n[1]);
      g.gain.setValueAtTime(0.0001, t0 + n[1]);
      g.gain.exponentialRampToValueAtTime(0.25, t0 + n[1] + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + n[1] + n[2]);
      osc.connect(g); g.connect(c.destination);
      osc.start(t0 + n[1]); osc.stop(t0 + n[1] + n[2] + 0.02);
      end = Math.max(end, t0 + n[1] + n[2]);
    });
    nextFree = end + 0.15;
    return true;
  }

  // ---- voice
  function voices() {
    var ss = root.speechSynthesis;
    try { return (ss && ss.getVoices && ss.getVoices()) || []; } catch (e) { return []; }
  }
  function voiceFor(lang) {
    return voices().filter(function (v) { return String(v.lang || '').toLowerCase().indexOf(lang) === 0; })[0] || null;
  }
  // a Persian voice is there? (often not on Linux desktops — then English is used)
  function hasVoice(lang) { return !!voiceFor(lang); }

  // "avahesab-backend" → "avahesab backend": reads better
  function speakable(name) { return String(name || '').replace(/[-_./]+/g, ' ').replace(/\s+/g, ' ').trim(); }

  function phrase(kind, name, lang) {
    if (!PHRASES[lang]) lang = 'en';
    var p = PHRASES[lang][kind] || kind;
    name = speakable(name);
    return p.replace('{in}', name ? IN[lang] + name : '');
  }

  function say(kind, name, lang) {
    var ss = root.speechSynthesis, U = root.SpeechSynthesisUtterance;
    if (!ss || !U) return false;
    if (lang === 'fa' && voices().length && !hasVoice('fa')) lang = 'en';     // no Persian voice here
    var u = new U(phrase(kind, name, lang));
    u.lang = lang === 'fa' ? 'fa-IR' : 'en-US';
    var v = voiceFor(lang);
    if (v) u.voice = v;
    u.rate = 1.05;
    try { ss.speak(u); } catch (e) { return false; }
    return u;
  }

  // kind: done | question | permission | plan | error; name: the project (spoken with the voice)
  // opts.mode / opts.lang: try a choice before it is saved (the ▶ buttons in Settings)
  function play(kind, name, opts) {
    var s = get();
    var mode = (opts && opts.mode) || s[kind] || 'off';
    var lang = (opts && opts.lang) || s.lang;
    if (mode === 'chime') return chime(kind) ? 'chime' : 'off';
    if (mode === 'voice') {
      if (say(kind, name, lang)) return 'voice';
      return chime(kind) ? 'chime' : 'off';                                    // no speech here: the tone
    }
    return 'off';
  }

  if (root.addEventListener) {
    var first = function () {
      unlock();
      voices();                       // Chrome loads the voice list on first use
      root.removeEventListener('pointerdown', first, true);
      root.removeEventListener('keydown', first, true);
    };
    root.addEventListener('pointerdown', first, true);
    root.addEventListener('keydown', first, true);
  }

  // ---- read a chat answer aloud (text to speech)
  // markdown → plain sentences: code blocks are not read (too long, mostly symbols), links keep
  // their words, URLs and table/heading/list marks go
  function plainText(md) {
    return String(md || '')
      .replace(/```[\s\S]*?(```|$)/g, '\n')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, ' ')
      .replace(/`([^`]*)`/g, '$1')
      .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, '')
      .replace(/^\s*\|?\s*:?-{3,}.*$/gm, '')
      .replace(/[|*_~#]+/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{2,}/g, '\n')
      .trim();
  }

  var FA_CH = /[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]/g, LATIN = /[A-Za-z]/g;
  function langOf(text) {
    var fa = (text.match(FA_CH) || []).length, en = (text.match(LATIN) || []).length;
    return fa && fa >= en / 3 ? 'fa' : 'en';
  }

  // Chrome stops long utterances after ~15 s: speak in short pieces, cut at sentence ends
  function chunks(text, max) {
    max = max || 220;
    var out = [];
    text.split(/\n+/).forEach(function (para) {
      var parts = para.match(/[^.!?؟。\n]+[.!?؟。]*\s*/g) || [];
      var cur = '';
      parts.forEach(function (p) {
        while (p.length > max) {                              // one very long sentence: at a comma or space
          var cut = Math.max(p.lastIndexOf('، ', max), p.lastIndexOf(', ', max), p.lastIndexOf(' ', max));
          if (cut < max / 3) cut = max;
          if (cur) { out.push(cur); cur = ''; }
          out.push(p.slice(0, cut + 1));
          p = p.slice(cut + 1);
        }
        if ((cur + p).length > max && cur) { out.push(cur); cur = ''; }
        cur += p;
      });
      if (cur) out.push(cur);
    });
    return out.map(function (c) { return c.trim(); }).filter(function (c) { return /[\w\u0600-\u06FF]/.test(c); });
  }

  var reading = null;            // {id, onEnd}
  function stopReading() {
    var r = reading;
    reading = null;
    var ss = root.speechSynthesis;
    if (ss && ss.cancel) { try { ss.cancel(); } catch (e) { /* ignore */ } }
    if (r && r.onEnd) r.onEnd();
  }

  // read(markdown, onEnd) → false when this browser cannot speak; otherwise the pieces read
  // (each in its own language: a Persian answer with a few English words is read in Persian)
  function read(md, onEnd) {
    var ss = root.speechSynthesis, U = root.SpeechSynthesisUtterance;
    stopReading();
    if (!ss || !U) return false;
    var list = chunks(plainText(md));
    if (!list.length) { if (onEnd) onEnd(); return []; }
    var me = { onEnd: onEnd };
    reading = me;
    var left = list.length;
    list.forEach(function (c) {
      var lang = langOf(c), u = new U(c);
      u.lang = lang === 'fa' ? 'fa-IR' : 'en-US';
      var v = voiceFor(lang);
      if (v) u.voice = v;
      u.rate = 1;
      u.onend = u.onerror = function () {
        if (--left === 0 && reading === me) { reading = null; if (onEnd) onEnd(); }
      };
      try { ss.speak(u); } catch (e) { /* ignore */ }
    });
    return list;
  }

  root.SWSound = {
    read: read, stopReading: stopReading, isReading: function () { return !!reading; },
    plainText: plainText, chunks: chunks, langOf: langOf, KINDS: KINDS, MODES: MODES, LANGS: LANGS, DEFAULTS: DEFAULTS, TUNES: TUNES,
    get: get, set: set, play: play, phrase: phrase, hasVoice: hasVoice, unlock: unlock,
    _reset: function () { ctx = null; nextFree = 0; } };
})(typeof window !== 'undefined' ? window : this);
