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

  root.SWSound = { KINDS: KINDS, MODES: MODES, LANGS: LANGS, DEFAULTS: DEFAULTS, TUNES: TUNES,
    get: get, set: set, play: play, phrase: phrase, hasVoice: hasVoice, unlock: unlock,
    _reset: function () { ctx = null; nextFree = 0; } };
})(typeof window !== 'undefined' ? window : this);
