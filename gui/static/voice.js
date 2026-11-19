// Dictation (speech to text) for every free-text field — the browser's own Web Speech API
// (Chrome/Edge `webkitSpeechRecognition`), no server, no other service.
//
// Four separate parts:
//   1. Speech        — ONE recognizer for the whole app; whoever starts it owns the mic.
//   2. Attachment    — the behaviour on one field: a small round mic button + writing the text.
//   3. isEligible    — which fields get it at all (free text only).
//   4. attachAll     — watches a subtree and attaches to every eligible field, now and later.
//
// The recognizer object (lib.dom has no types for it), as used here:
//   rec.lang, rec.continuous, rec.interimResults, rec.maxAlternatives
//   rec.onstart / onresult(e: {resultIndex, results: [[{transcript}], isFinal]}) / onerror(e: {error}) / onend
//   rec.start(), rec.stop(), rec.abort()
//   error codes: aborted, audio-capture, bad-grammar, language-not-supported, network, no-speech,
//                not-allowed, service-not-available (+ our own 'unsupported')
(function (root) {
  'use strict';

  // ------------------------------------------------------------------ messages
  var MESSAGES = {
    'not-allowed': 'Microphone blocked — allow it for this site in the browser settings',
    'service-not-available': 'Speech service not available — dictation needs Chrome or Edge',
    'network': 'Dictation needs the internet (Chrome sends the audio to its speech service)',
    'audio-capture': 'No microphone found',
    'language-not-supported': 'This browser cannot dictate this language',
    'bad-grammar': 'Dictation failed',
    'unsupported': 'This browser cannot dictate — use Chrome or Edge',
    'no-speech': 'Nothing heard',
    'aborted': 'Dictation stopped'
  };
  var FATAL = { 'not-allowed': 1, 'service-not-available': 1, 'network': 1, 'audio-capture': 1,
    'language-not-supported': 1, 'bad-grammar': 1, 'unsupported': 1 };
  // the speech service dropped for a moment (a phone between networks, a slow link to Google):
  // once something was heard, these are retried a few times before giving up
  var FLAKY = { 'network': 1, 'service-not-available': 1 };
  var FLAKY_TRIES = 3;
  // Chrome sometimes shows a guess (interim) and then goes silent for good — typically after
  // a word it cannot place, like an English term inside Persian speech. After this long the
  // guess is kept as text and the recognizer is started again.
  var STALL_MS = 4000;

  // ------------------------------------------------------------------ tech words
  // The Persian recognizer writes English terms in Persian letters ("ری اکت", "کامیت").
  // Programmers want them in English. Only real English terms — Persian loanwords that are
  // written in Persian anyway (سرور، فایل، باگ، تست…) are left alone.
  var TECH = [
    ['GitHub', 'گیت هاب|گیتهاب|گیت‌هاب'], ['GitLab', 'گیت لب|گیتلب'], ['git', 'گیت'],
    ['pull request', 'پول ریکوئست|پول رکوئست|پول ریکوست'], ['commit', 'کامیت'], ['push', 'پوش'],
    ['merge', 'مرج'], ['branch', 'برنچ'], ['rebase', 'ریبیس'], ['checkout', 'چک اوت|چک‌اوت'], ['stash', 'استش'],
    ['deploy', 'دیپلوی'], ['build', 'بیلد'], ['release', 'ریلیز'], ['pipeline', 'پایپ لاین|پایپلاین|پایپ‌لاین'],
    ['docker compose', 'داکر کامپوز'], ['Docker', 'داکر'], ['container', 'کانتینر'], ['Kubernetes', 'کوبرنتیز|کوبرنتیس|کوبرنیتیز'],
    ['nginx', 'انجین ایکس|اینجین ایکس|انجینکس'], ['localhost', 'لوکال هاست|لوکالهاست'],
    ['React', 'ری اکت|ریاکت|ری‌اکت'], ['Angular', 'انگولار|آنگولار|انگیولار'], ['Next.js', 'نکست جی اس'], ['NestJS', 'نست جی اس'],
    ['Node.js', 'نود جی اس|نودجی اس'], ['npm', 'ان پی ام'], ['ng serve', 'ان جی سرو'],
    ['JavaScript', 'جاوا اسکریپت|جاوااسکریپت'], ['TypeScript', 'تایپ اسکریپت|تایپ‌اسکریپت|تایپاسکریپت'],
    ['Python', 'پایتون'], ['Golang', 'گولنگ'], ['Django', 'جنگو'], ['Laravel', 'لاراول'], ['PHP', 'پی اچ پی'],
    ['Flutter', 'فلاتر'], ['Kotlin', 'کاتلین'], ['Tailwind', 'تیلویند|تیل ویند'],
    ['API', 'ای پی آی|ای‌پی‌آی|ای پی ای'], ['endpoint', 'اند پوینت|اندپوینت|اند‌پوینت'],
    ['backend', 'بک اند|بکند|بک‌اند'], ['frontend', 'فرانت اند|فرانتند|فرانت‌اند'],
    ['database', 'دیتابیس|دیتا بیس'], ['PostgreSQL', 'پستگرس کیو ال|پوستگرس کیو ال'], ['Postgres', 'پستگرس|پوستگرس'],
    ['MySQL', 'مای اس کیو ال'], ['SQL', 'اس کیو ال'], ['Redis', 'ردیس'], ['MongoDB', 'مانگو دی بی|مونگو دی بی'],
    ['GraphQL', 'گراف کیو ال'], ['query', 'کوئری|کوئیری'], ['migration', 'مایگریشن'], ['schema', 'اسکیما'],
    ['component', 'کامپوننت'], ['function', 'فانکشن'], ['interface', 'اینترفیس'], ['middleware', 'میدلور|میدل ور'],
    ['callback', 'کال بک|کالبک'], ['promise', 'پرامیس'], ['async', 'ای سینک|اسینک'], ['await', 'اویت'],
    ['null', 'نال'], ['undefined', 'آندیفایند|اندیفایند'], ['string', 'استرینگ'], ['object', 'آبجکت'],
    ['boolean', 'بولین'], ['integer', 'اینتیجر'], ['state', 'استیت'], ['hook', 'هوک'], ['props', 'پراپس'],
    ['framework', 'فریمورک|فریم ورک'], ['webhook', 'وب هوک|وبهوک'], ['WebSocket', 'وب سوکت|وبسوکت'],
    ['refactor', 'ریفکتور|ری فکتور'], ['debug', 'دیباگ'], ['regex', 'رجکس|رگکس'],
    ['JSON', 'جیسون|جی سان'], ['YAML', 'یامل'], ['HTML', 'اچ تی ام ال'], ['CSS', 'سی اس اس'], ['URL', 'یو آر ال'],
    ['HTTPS', 'اچ تی تی پی اس'], ['HTTP', 'اچ تی تی پی'], ['SSH', 'اس اس اچ'], ['VS Code', 'وی اس کد'],
    ['Claude', 'کلاد|کلود'], ['Opus', 'اوپوس'], ['prompt', 'پرامپت'], ['agent', 'ایجنت'],
    ['issue', 'ایشو'], ['review', 'ریویو'], ['workspace', 'ورک اسپیس|ورکاسپیس']
  ];
  var TECH_RE = (function () {
    var all = [];
    TECH.forEach(function (t) { t[1].split('|').forEach(function (fa) { all.push([fa, t[0]]); }); });
    all.sort(function (a, b) { return b[0].length - a[0].length; });          // longest first: "گیت هاب" before "گیت"
    var map = {};
    all.forEach(function (x) { map[x[0]] = x[1]; });
    // a space in a term may also be a half-space (ZWNJ) or nothing at all
    var alt = all.map(function (x) { return x[0].replace(/[ \u200c]/g, '[ \\u200c]?'); }).join('|');
    var norm = function (w) { return w.replace(/[ \u200c]/g, ''); };
    var byNorm = {};
    all.forEach(function (x) { byNorm[norm(x[0])] = x[1]; });
    // whole words only: not inside a longer Persian word (پوشه stays پوشه)
    // letters only — the Persian comma/question mark (،؟) are in the same Unicode block
    var L = '\\u0621-\\u065F\\u066E-\\u06D3\\u06FA-\\u06FF\\u200c';
    return { re: new RegExp('(^|[^' + L + '])(' + alt + ')(?![' + L + '])', 'g'), byNorm: byNorm, norm: norm };
  })();
  function techWords(text) {
    if (!text || !/[\u0600-\u06FF]/.test(text)) return text;
    return text.replace(TECH_RE.re, function (m, pre, w) { return pre + (TECH_RE.byNorm[TECH_RE.norm(w)] || w); });
  }
  var QUIET = { 'aborted': 1, 'no-speech': 1 };      // never worth a toast
  var BACKOFF = [300, 600, 1200, 2000];
  var STOP_GRACE = 1500;

  function errorOf(code) {
    return { code: code, message: MESSAGES[code] || ('Dictation failed (' + code + ')'), fatal: !!FATAL[code] };
  }

  // "a" + "b" with exactly one space between (keeps a newline / space the user already ended with)
  function join(a, b) {
    a = a || ''; b = (b || '').trim();
    if (!b) return a;
    if (!a.trim()) return b;
    return /\s$/.test(a) ? a + b : a + ' ' + b;
  }

  // ------------------------------------------------------------------ a tiny replay stream
  // Keeps the last value so a subscriber that comes late (e.g. start() failed synchronously)
  // still gets it. Never errors: the last value has `ended: true`, then it completes.
  function Stream() { this.subs = []; this.last = null; this.done = false; }
  Stream.prototype.next = function (v) {
    if (this.done) return;
    this.last = v;
    this.subs.slice().forEach(function (f) { f(v); });
  };
  Stream.prototype.complete = function () { this.done = true; this.subs = []; };
  Stream.prototype.subscribe = function (fn) {
    var self = this;
    if (this.last) fn(this.last);
    if (this.done) return function () {};
    this.subs.push(fn);
    return function () { self.subs = self.subs.filter(function (f) { return f !== fn; }); };
  };

  // ------------------------------------------------------------------ 1. Speech (one for the app)
  // env (for tests): { ctor: () => Recognizer constructor | null, setTimeout, clearTimeout }
  function Speech(env) {
    env = env || {};
    this.env = {
      ctor: env.ctor || function () { return root.SpeechRecognition || root.webkitSpeechRecognition || null; },
      setTimeout: env.setTimeout || function (f, ms) { return setTimeout(f, ms); },
      clearTimeout: env.clearTimeout || function (t) { clearTimeout(t); }
    };
    this.gen = 0;
    this.stream = null;      // the owner's stream; null = idle
    this.rec = null;
    this.wanted = false;     // the owner still wants to listen (restart when Chrome ends a session)
    this.committed = '';     // finals of the sessions that already ended
    this.sessionFinal = '';
    this.interim = '';
    this.tries = 0;          // index into BACKOFF; back to 0 on each real result
    this.restartT = null;
    this.stopT = null;
    this.opts = null;
    this.stallT = null;
  }
  var SP = Speech.prototype;

  SP.isSupported = function () { return !!this.env.ctor(); };
  Object.defineProperty(SP, 'active', { get: function () { return !!this.stream; } });

  SP.start = function (opts) {
    if (this.stream) this._finish(errorOf('aborted'));         // the previous owner loses the mic
    this.opts = {
      lang: (opts && opts.lang) || 'fa-IR',
      continuous: !(opts && opts.continuous === false),
      interimResults: !(opts && opts.interimResults === false),
      maxAlternatives: (opts && opts.maxAlternatives) || 3,
      preferLatin: !!(opts && opts.preferLatin),
      techWords: !(opts && opts.techWords === false)
    };
    var stream = this.stream = new Stream();
    this.gen++;
    this.wanted = true;
    this.committed = this.sessionFinal = this.interim = '';
    this.tries = 0;
    this.heard = false;      // something was recognised in this recording
    this.flaky = 0;          // service drops in a row
    if (!this.isSupported()) this._finish(errorOf('unsupported'));
    else this._launch();
    return stream;
  };

  // stop listening; Chrome still sends the last finals and then `end` — or we give up after a moment
  // (a recognizer still waiting on the permission prompt may never answer)
  SP.stop = function () {
    if (!this.stream) return;
    var self = this, gen = this.gen;
    this.wanted = false;
    this._clearRestart();
    if (!this.rec) { this._finish(); return; }
    try { this.rec.stop(); } catch (e) { this._finish(); return; }
    this.env.clearTimeout(this.stopT);
    this.stopT = this.env.setTimeout(function () { if (gen === self.gen) self._finish(); }, STOP_GRACE);
  };

  // stop now, drop what is not final yet
  SP.abort = function () { if (this.stream) this._finish(); };

  SP._launch = function () {
    var self = this, gen = this.gen;
    var Ctor = this.env.ctor();
    var rec;
    try {
      rec = new Ctor();
      rec.lang = this.opts.lang;
      rec.continuous = this.opts.continuous;
      rec.interimResults = this.opts.interimResults;
      rec.maxAlternatives = this.opts.maxAlternatives;
    } catch (e) { this._finish(errorOf('unsupported')); return; }
    var mine = function () { return gen === self.gen && rec === self.rec; };
    rec.onresult = function (e) { if (mine()) self._result(e); };
    rec.onerror = function (e) { if (mine()) self._error(e && e.error); };
    rec.onend = function () { if (mine()) self._ended(); };
    this.rec = rec;
    try { rec.start(); } catch (e) {
      // the previous instance is still winding down: try again in a moment
      this._drop(rec);
      this._scheduleRestart();
    }
  };

  SP._pick = function (r) {
    var best = r[0];
    if (this.opts.preferLatin) {
      for (var i = 0; i < r.length; i++) if (r[i] && /[A-Za-z]/.test(r[i].transcript || '')) { best = r[i]; break; }
    }
    var t = ((best && best.transcript) || '').trim();
    if (this.opts.techWords && /^fa/i.test(this.opts.lang)) t = techWords(t);
    return t;
  };

  SP._result = function (e) {
    // rebuild from ALL results — Chrome may revise earlier ones, resultIndex is not enough
    var fin = [], mid = [], res = (e && e.results) || [];
    for (var i = 0; i < res.length; i++) {
      var t = this._pick(res[i]);
      if (!t) continue;
      if (res[i].isFinal) {
        // Chrome on Android may repeat the earlier finals inside the next one ("a", "a b"):
        // keep only the longer one, never "a a b"
        var prev = fin[fin.length - 1];
        if (prev && t.indexOf(prev) === 0) fin[fin.length - 1] = t;
        else if (!(prev && prev.indexOf(t) === 0 && prev.length >= t.length)) fin.push(t);
      } else mid.push(t);
    }
    this.sessionFinal = fin.join(' ').replace(/\s+/g, ' ').trim();
    this.interim = mid.join(' ').replace(/\s+/g, ' ').trim();
    this.tries = 0;
    this.flaky = 0;
    if (this.sessionFinal || this.interim) this.heard = true;
    this._armStall();
    this._emit();
  };

  // a guess that never becomes final: keep it and listen again (see STALL_MS)
  SP._armStall = function () {
    var self = this, gen = this.gen, rec = this.rec;
    this._clearStall();
    if (!this.interim || !this.wanted) return;
    this.stallT = this.env.setTimeout(function () {
      self.stallT = null;
      if (gen !== self.gen || rec !== self.rec || !self.wanted || !self.interim) return;
      self._keepSession();
      self._drop(rec);
      self._emit();
      self._scheduleRestart();
    }, STALL_MS);
  };
  SP._clearStall = function () { if (this.stallT != null) { this.env.clearTimeout(this.stallT); this.stallT = null; } };

  // the session is over: its finals AND its last guess become text (a guess is better than losing words)
  SP._keepSession = function () {
    this.committed = join(join(this.committed, this.sessionFinal), this.interim);
    this.sessionFinal = '';
    this.interim = '';
  };

  SP._error = function (code) {
    code = code || 'unknown';
    if (QUIET[code]) return;                          // Chrome fires `end` next → restart
    if (FLAKY[code] && this.heard && this.wanted && this.flaky < FLAKY_TRIES) {
      this.flaky++;
      this._clearStall();
      this._keepSession();
      this._drop(this.rec);
      this._emit();
      this._scheduleRestart();
      return;
    }
    var err = errorOf(code);
    if (err.fatal) { this._finish(err); return; }
    this._emit(err);
  };

  SP._ended = function () {
    this._clearStall();
    this._keepSession();
    this._drop(this.rec);
    if (this.wanted) { this._emit(); this._scheduleRestart(); }
    else this._finish();
  };

  SP._scheduleRestart = function () {
    var self = this, gen = this.gen;
    this._clearRestart();
    var ms = BACKOFF[Math.min(this.tries, BACKOFF.length - 1)];
    this.tries++;
    this.restartT = this.env.setTimeout(function () {
      self.restartT = null;
      if (gen === self.gen && self.wanted && self.stream) self._launch();
    }, ms);
  };

  SP._clearRestart = function () { if (this.restartT != null) { this.env.clearTimeout(this.restartT); this.restartT = null; } };

  SP._drop = function (rec) {
    if (!rec) return;
    rec.onresult = rec.onerror = rec.onend = rec.onstart = null;
    try { rec.abort(); } catch (e) { /* already gone */ }
    if (this.rec === rec) this.rec = null;
  };

  SP._snapshot = function (extra) {
    var s = { final: join(this.committed, this.sessionFinal), interim: this.interim };
    if (extra && extra.error) s.error = extra.error;
    if (extra && extra.ended) s.ended = true;
    return s;
  };

  SP._emit = function (err) { if (this.stream) this.stream.next(this._snapshot({ error: err })); };

  SP._finish = function (err) {
    var stream = this.stream;
    if (!stream) return;
    this.committed = join(this.committed, this.sessionFinal);
    this.sessionFinal = '';
    this.interim = '';
    this.gen++;                          // late callbacks of this session are ignored from now on
    this.wanted = false;
    this._clearRestart();
    this._clearStall();
    this.env.clearTimeout(this.stopT);
    this.stopT = null;
    this._drop(this.rec);
    this.stream = null;
    stream.next(this._snapshot({ error: err, ended: true }));
    stream.complete();
  };

  // ------------------------------------------------------------------ 3. eligibility
  // ID-like words: a field named like this holds a code/number, not prose
  var ID_WORDS = ['mobile', 'tel', 'phone', 'code', 'kod', 'barcode', 'sheba', 'shaba', 'iban', 'card', 'kart',
    'national', 'melli', 'otp', 'password', 'passwd', 'captcha', 'shomare', 'shomareh', 'serial', 'pin', 'token',
    'username', 'user', 'login', 'email', 'mail', 'id', 'tid', 'uid', 'shenase', 'key', 'secret', 'certificate',
    'url', 'uri', 'link', 'ip', 'mac', 'host', 'hostname', 'port', 'path', 'dir', 'cwd', 'folder', 'file', 'branch',
    'sha', 'hash', 'commit', 'extension', 'ext', 'plaque', 'pelak', 'version', 'cmd', 'command', 'regex', 'glob'];
  var NUM_WORDS = ['mablagh', 'price', 'tedad', 'count', 'qty', 'quantity', 'amount', 'meghdar', 'nerkh', 'rate',
    'darsad', 'percent', 'vazn', 'weight', 'ghimat', 'fee', 'karmozd', 'mojodi', 'balance', 'hoghogh', 'salary',
    'takhfif', 'discount', 'number', 'num', 'no', 'tarikh', 'date', 'saat', 'time', 'year', 'month', 'day', 'age',
    'size', 'width', 'height', 'min', 'max', 'zip', 'postal'];
  // also when the name only STARTS with one of these (shomarehHesab written all lower-case, codeposti)
  var ID_PREFIX = ['shomare', 'code', 'mobile', 'phone', 'sheba', 'shaba', 'iban', 'barcode', 'national', 'melli',
    'password', 'captcha', 'serial', 'shenase', 'certificate', 'username', 'email', 'otp'];
  var BAD = {};
  ID_WORDS.concat(NUM_WORDS).forEach(function (w) { BAD[w] = 1; });

  var TEXT_TYPES = { '': 1, text: 1, search: 1, url: 1 };
  var BAD_INPUTMODE = { numeric: 1, decimal: 1, tel: 1 };
  var BAD_AUTOCOMPLETE = { 'one-time-code': 1, tel: 1, 'tel-national': 1, 'cc-number': 1, 'cc-csc': 1,
    username: 1, 'current-password': 1, 'new-password': 1 };

  // widgets that own their inputs, code/path fields (.mono), the terminal, the command palette…
  var SKIP_SELECTOR = [
    '[data-voice-to-text="off"]', '.vtt-btn', '.xterm', '.mono', '.files-edit', '.palette',
    '[role="combobox"]', '[role="listbox"]', '[role="searchbox"][aria-autocomplete]',
    '.ProseMirror', '.ql-editor', '.tiptap', '.cm-editor', '.monaco-editor', '.CodeMirror'
  ].join(', ');

  function tokens(s) {
    return String(s || '').replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
      .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }

  function badName(v) {
    if (!v) return false;
    if (tokens(v).some(function (t) { return BAD[t] || BAD[t.replace(/\d+$/, '')]; })) return true;
    var low = String(v).toLowerCase();
    return ID_PREFIX.some(function (w) { return low.indexOf(w) === 0; });
  }

  function attr(el, k) { return el.getAttribute ? el.getAttribute(k) : null; }

  function inSkip(el) {
    if (el.closest) { try { return !!el.closest(SKIP_SELECTOR); } catch (e) { return false; } }
    return false;
  }

  function isEligible(el) {
    if (!el || !el.tagName) return false;
    var tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      var type = (attr(el, 'type') || '').toLowerCase();
      if (!TEXT_TYPES[type]) return false;
    } else if (tag !== 'textarea') {
      var ce = attr(el, 'contenteditable');
      if (ce == null || String(ce).toLowerCase() === 'false') return false;
    }
    if (BAD_INPUTMODE[(attr(el, 'inputmode') || '').toLowerCase()]) return false;
    var ac = (attr(el, 'autocomplete') || '').toLowerCase().split(/\s+/);
    if (ac.some(function (a) { return BAD_AUTOCOMPLETE[a]; })) return false;
    if (badName(attr(el, 'name')) || badName(attr(el, 'formcontrolname')) || badName(attr(el, 'formControlName')) || badName(el.id || attr(el, 'id'))) return false;
    if (inSkip(el)) return false;
    return true;
  }

  // ------------------------------------------------------------------ language (Persian ⇄ English)
  var LANGS = [['fa-IR', 'Persian'], ['en-US', 'English']];
  var LANG_KEY = 'sw-voice-lang';
  function savedLang() {
    try { var v = root.localStorage && root.localStorage.getItem(LANG_KEY); if (v) return v; } catch (e) { /* private mode */ }
    return 'fa-IR';
  }
  function langName(code) { var x = LANGS.filter(function (l) { return l[0] === code; })[0]; return x ? x[1] : code; }
  function toggleLang() {
    var next = savedLang() === 'fa-IR' ? 'en-US' : 'fa-IR';
    try { root.localStorage.setItem(LANG_KEY, next); } catch (e) { /* private mode */ }
    return next;
  }

  // ------------------------------------------------------------------ 2. Attachment (one field)
  var ICONS = {
    mic: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
    stop: '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>',
    off: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M3 3l18 18"/></svg>'
  };
  var SIZE = 28, GAP = 6, HIDE_DELAY = 160;

  function isTouch() {
    return !!(root.matchMedia && root.matchMedia('(pointer: coarse)').matches);
  }

  function fire(el, type) {
    if (typeof root.Event === 'function' && el.dispatchEvent) el.dispatchEvent(new root.Event(type, { bubbles: true }));
  }

  // hooks: { speech, document, notify(msg), onCommit(value), onWrite(value, final), button }
  //   button given = it lives in the page already (chat composer): no floating, always visible
  // settings (read when a recording starts): { lang(), preferLatin(), append(), enabled() }
  function Attachment(el, hooks, settings) {
    var self = this;
    this.el = el;
    this.hooks = hooks || {};
    this.speech = this.hooks.speech || shared();
    this.doc = this.hooks.document || root.document;
    settings = settings || {};
    this.settings = {
      lang: settings.lang || savedLang,
      preferLatin: settings.preferLatin || function () { return false; },
      append: settings.append || function () { return true; },
      enabled: settings.enabled || function () { return true; }
    };
    this.recording = false;
    this.unsupported = false;
    this.floating = !this.hooks.button;
    this.focused = false; this.overField = false; this.overBtn = false;
    this.visible = false;
    this.hideT = null; this.raf = 0;
    this.listeners = [];

    var btn = this.btn = this.hooks.button || this.doc.createElement('button');
    btn.setAttribute('type', 'button');
    btn.setAttribute('tabindex', '-1');
    if (this.floating) { btn.className = 'vtt-btn'; btn.style.display = 'none'; this.doc.body.appendChild(btn); }
    // never steal the focus from the field
    var keep = function (e) { e.preventDefault(); };
    this.on(btn, 'pointerdown', keep);
    this.on(btn, 'mousedown', keep);
    this.on(btn, 'click', function (e) { e.preventDefault(); e.stopPropagation(); self.toggle(); });
    this.on(btn, 'contextmenu', function (e) { e.preventDefault(); e.stopPropagation(); self.switchLang(); });
    if (this.floating) {
      this.on(btn, 'pointerenter', function () { self.overBtn = true; self.update(); });
      this.on(btn, 'pointerleave', function () { self.overBtn = false; self.update(); });
      this.on(el, 'focus', function () { self.focused = true; self.update(); });
      this.on(el, 'blur', function () { self.focused = false; self.update(); });
      this.on(el, 'pointerenter', function () { self.overField = true; self.update(); });
      this.on(el, 'pointerleave', function () { self.overField = false; self.update(); });
      if (this.doc.activeElement === el) this.focused = true;
    }
    this.onMove = function () { self.schedulePlace(); };
    this.paint();
    this.update();
  }
  var AP = Attachment.prototype;

  AP.on = function (target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    this.listeners.push([target, type, fn, opts]);
  };

  AP.usable = function () {
    var el = this.el;
    if (el.disabled || el.readOnly) return false;
    if (attr(el, 'disabled') != null || attr(el, 'readonly') != null) return false;
    return !!this.settings.enabled();
  };

  AP.paint = function () {
    var b = this.btn, lang = langName(this.settings.lang());
    var state = this.recording ? 'stop' : this.unsupported ? 'off' : 'mic';
    b.innerHTML = ICONS[state];
    b.classList.toggle('vtt-rec', this.recording);
    b.classList.toggle('vtt-unsup', !this.recording && this.unsupported);
    var title = this.recording ? 'Stop dictation'
      : this.unsupported ? 'Dictation needs Chrome or Edge'
      : 'Dictate (' + lang + ') — right-click to switch language';
    b.setAttribute('title', title);
    b.setAttribute('aria-label', title);
    b.setAttribute('aria-pressed', this.recording ? 'true' : 'false');
  };

  // ---- visibility + position (floating button only)
  AP.wantVisible = function () {
    if (this.recording) return true;
    if (!this.focused || !this.usable()) return false;
    return this.overField || this.overBtn || isTouch();
  };

  AP.update = function () {
    var self = this;
    if (!this.floating) return;
    if (this.wantVisible()) {
      clearTimeout(this.hideT); this.hideT = null;
      if (!this.visible) this.show();
      else this.schedulePlace();
    } else if (this.visible && !this.hideT) {
      // a moment, so the pointer can cross the gap from the field to the button
      this.hideT = setTimeout(function () { self.hideT = null; if (!self.wantVisible()) self.hide(); }, HIDE_DELAY);
    }
  };

  AP.show = function () {
    var self = this;
    this.visible = true;
    this.btn.style.display = '';
    root.addEventListener('scroll', this.onMove, { capture: true, passive: true });
    root.addEventListener('resize', this.onMove);
    if (root.ResizeObserver) { this.ro = new root.ResizeObserver(function () { self.schedulePlace(); }); this.ro.observe(this.el); }
    this.place();
  };

  AP.hide = function () {
    this.visible = false;
    this.btn.style.display = 'none';
    root.removeEventListener('scroll', this.onMove, { capture: true, passive: true });
    root.removeEventListener('scroll', this.onMove, true);
    root.removeEventListener('resize', this.onMove);
    if (this.ro) { this.ro.disconnect(); this.ro = null; }
    if (this.raf && root.cancelAnimationFrame) root.cancelAnimationFrame(this.raf);
    this.raf = 0;
  };

  AP.schedulePlace = function () {
    var self = this;
    if (this.raf || !this.visible) return;
    var raf = root.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
    this.raf = raf(function () { self.raf = 0; self.place(); }) || 0;
  };

  // just outside the right edge, centred on the first line; no room → outside the left edge;
  // still no room → inside the right edge
  AP.place = function () {
    if (!this.visible) return;
    var r = this.el.getBoundingClientRect();
    if (!r.width && !r.height) { this.btn.style.display = 'none'; return; }
    this.btn.style.display = '';
    var p = placeFor(r, root.innerWidth || 0);
    this.btn.style.left = p.left + 'px';
    this.btn.style.top = p.top + 'px';
  };

  function placeFor(r, vw) {
    var top = Math.round(r.top + Math.min(r.height, 40) / 2 - SIZE / 2);
    var left = r.right + GAP;
    if (left + SIZE > vw - 4) left = r.left - GAP - SIZE;
    if (left < 4) left = r.right - SIZE - 4;
    return { left: Math.round(left), top: top };
  }

  // ---- recording
  AP.toggle = function () { if (this.recording) this.stop(); else this.start(); };

  AP.start = function () {
    var self = this;
    if (this.recording || !this.usable()) return;
    if (!this.speech.isSupported()) {
      this.unsupported = true;
      this.paint();
      this.notify(MESSAGES.unsupported);
      return;
    }
    var cur = this.read();
    this.base = this.settings.append() ? cur : '';
    this.lastWritten = cur;
    this.lastCommitted = this.base;
    this.writtenFinal = '';      // the final text (as the stream gives it) the field already holds
    this.used = '';              // the part of the final text that is already inside `base`
    this.recording = true;
    this.paint();
    this.update();
    var stream = this.speech.start({ lang: this.settings.lang(), preferLatin: this.settings.preferLatin() });
    var sub = stream.subscribe(function (s) { self.snap(s); });
    if (this.recording) this.unsub = sub; else sub();        // it may have ended at once
  };

  AP.stop = function () { if (this.recording) this.speech.stop(); };
  AP.abort = function () { if (this.recording) this.speech.abort(); };

  AP.switchLang = function () {
    var next = toggleLang();
    this.notify('Dictation: ' + langName(next));
    if (this.recording) { this.abort(); this.start(); }
    this.paint();
  };

  AP.snap = function (s) {
    if (!this.recording) return;
    var cur = this.read();
    if (cur !== this.lastWritten) {
      // the user (or the page, e.g. the message was sent) changed the text: build on what is there
      // now; the finals written so far are part of it (or were deleted on purpose)
      this.base = cur;
      this.lastCommitted = cur;
      this.used = this.writtenFinal;
    }
    var fin = s.final || '', used = this.used;
    var fresh = fin.indexOf(used) === 0 ? fin.slice(used.length) : fin.slice(Math.min(used.length, fin.length));
    this._pendingFinal = fin;
    var committed = join(this.base, fresh);
    if (committed !== this.lastCommitted || (s.ended && committed !== cur)) {
      this.write(committed, true);
      this.lastCommitted = committed;
      if (this.hooks.onCommit) this.hooks.onCommit(committed);
    }
    if (!s.ended) {
      var shown = join(committed, s.interim);
      if (shown !== this.read()) this.write(shown, false);      // live text: the DOM only, no events
    }
    this.lastWritten = this.read();
    if (s.ended) {
      this.recording = false;
      if (this.unsub) { this.unsub(); this.unsub = null; }
      if (s.error) {
        if (s.error.code === 'unsupported') this.unsupported = true;
        if (!QUIET[s.error.code]) this.notify(s.error.message);
      }
      this.paint();
      this.update();
    } else if (s.error && !QUIET[s.error.code]) {
      this.notify(s.error.message);
    }
  };

  AP.read = function () {
    var el = this.el;
    if (el.tagName && /^(INPUT|TEXTAREA)$/i.test(el.tagName)) return el.value || '';
    return el.textContent || '';
  };

  AP.write = function (v, final) {
    var el = this.el;
    if (final) this.writtenFinal = this._pendingFinal;
    if (el.tagName && /^(INPUT|TEXTAREA)$/i.test(el.tagName)) {
      // the native setter, so React-style controlled inputs notice the change
      var proto = /^TEXTAREA$/i.test(el.tagName) ? root.HTMLTextAreaElement : root.HTMLInputElement;
      var desc = proto && Object.getOwnPropertyDescriptor(proto.prototype, 'value');
      if (desc && desc.set) desc.set.call(el, v); else el.value = v;
      try { el.setSelectionRange(v.length, v.length); } catch (e) { /* url inputs have no selection */ }
    } else {
      el.textContent = v;
    }
    if (final) { fire(el, 'input'); fire(el, 'change'); }
    if (this.hooks.onWrite) this.hooks.onWrite(v, !!final);
    if (this.floating) this.schedulePlace();
  };

  AP.notify = function (msg) { if (this.hooks.notify) this.hooks.notify(msg); };

  AP.refresh = function () {
    if (this.recording && !this.usable()) this.abort();
    this.paint();
    this.update();
  };

  AP.destroy = function () {
    if (this.recording) this.abort();
    if (this.unsub) { this.unsub(); this.unsub = null; }
    clearTimeout(this.hideT);
    if (this.floating && this.visible) this.hide();
    this.listeners.forEach(function (l) { l[0].removeEventListener(l[1], l[2], l[3]); });
    this.listeners = [];
    if (this.floating && this.btn.parentNode) this.btn.remove();
    this.destroyed = true;
  };

  // ------------------------------------------------------------------ 4. attachAll (a subtree)
  var FIELDS = 'input, textarea, [contenteditable]';

  // opts: { notify, enabled(), lang(), preferLatin(), append() }
  function attachAll(rootEl, opts) {
    opts = opts || {};
    var map = new Map();
    var pending = [], removed = false, rafId = 0, full = false;
    var settings = {
      lang: opts.lang || savedLang,
      preferLatin: opts.preferLatin || function () { return false; },
      append: opts.append || function () { return true; },
      enabled: opts.enabled || function () { return true; }
    };

    function consider(el) {
      if (map.has(el) || attr(el, 'data-voice-to-text') === '1') return;
      if (!isEligible(el)) return;
      var a = new Attachment(el, { notify: opts.notify, speech: opts.speech }, {
        lang: function () { return attr(el, 'data-voice-lang') || settings.lang(); },
        preferLatin: function () { return attr(el, 'data-voice-latin') != null || settings.preferLatin(); },
        append: function () { return attr(el, 'data-voice-append') !== 'false' && settings.append(); },
        enabled: settings.enabled
      });
      map.set(el, a);
    }

    function scan(node) {
      if (!node || node.nodeType !== 1) return;
      if (inSkip(node) && !/^(INPUT|TEXTAREA)$/i.test(node.tagName)) return;       // never go inside
      if (node.matches && node.matches(FIELDS)) consider(node);
      if (node.querySelectorAll) Array.prototype.forEach.call(node.querySelectorAll(FIELDS), consider);
    }

    function flush() {
      rafId = 0;
      if (removed) {
        removed = false;
        map.forEach(function (a, el) { if (!el.isConnected) { a.destroy(); map.delete(el); } });
      }
      if (full) { full = false; pending = []; scan(rootEl); return; }
      var nodes = pending; pending = [];
      var seen = [];
      nodes.forEach(function (n) {
        if (seen.indexOf(n) >= 0) return;
        if (seen.some(function (s) { return s.contains && s.contains(n); })) return;
        seen = seen.filter(function (s) { return !(n.contains && n.contains(s)); });
        seen.push(n);
      });
      seen.forEach(scan);
    }

    function schedule() {
      if (rafId) return;
      var raf = root.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
      rafId = raf(flush) || 1;
    }

    var mo = null;
    if (root.MutationObserver) {
      mo = new root.MutationObserver(function (list) {
        list.forEach(function (m) {
          if (m.removedNodes && m.removedNodes.length) removed = true;
          var added = m.addedNodes || [];
          if (pending.length + added.length > 200) full = true;
          else for (var i = 0; i < added.length; i++) if (added[i].nodeType === 1) pending.push(added[i]);
        });
        if (full || removed || pending.length) schedule();
      });
      mo.observe(rootEl, { childList: true, subtree: true });
    }
    scan(rootEl);

    return {
      attachments: map,
      flush: flush,
      refresh: function () { map.forEach(function (a) { a.refresh(); }); },
      destroy: function () {
        if (mo) mo.disconnect();
        map.forEach(function (a) { a.destroy(); });
        map.clear();
      }
    };
  }

  // one field the page renders itself (the chat composer): its own button, no scanning
  function attach(el, hooks, settings) {
    el.setAttribute('data-voice-to-text', '1');
    return new Attachment(el, hooks, settings);
  }

  var sharedSpeech = null;
  function shared() { return sharedSpeech || (sharedSpeech = new Speech()); }

  root.SWVoice = {
    Speech: Speech, Attachment: Attachment, isEligible: isEligible, attachAll: attachAll, attach: attach,
    speech: shared, join: join, placeFor: placeFor, tokens: tokens, lang: savedLang, toggleLang: toggleLang,
    SKIP_SELECTOR: SKIP_SELECTOR, MESSAGES: MESSAGES, techWords: techWords, STALL_MS: STALL_MS
  };
})(typeof window !== 'undefined' ? window : this);
