// Minimal DOM for testing the GUI scripts under node (no npm packages needed).
'use strict';

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.className || '').split(/\s+/).filter(Boolean); }
  _set(a) { this.el.className = a.join(' '); }
  add(...c) { const a = this._get(); c.forEach((x) => { if (!a.includes(x)) a.push(x); }); this._set(a); }
  remove(...c) { this._set(this._get().filter((x) => !c.includes(x))); }
  contains(c) { return this._get().includes(c); }
  toggle(c, force) {
    const has = this.contains(c);
    const want = force === undefined ? !has : !!force;
    if (want && !has) this.add(c);
    if (!want && has) this.remove(c);
    return want;
  }
}

class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get children() { return this.childNodes.filter((n) => n instanceof Element); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextElementSibling() { return this._sib(1); }
  get nextSibling() { if (!this.parentNode) return null; const c = this.parentNode.childNodes; return c[c.indexOf(this) + 1] || null; }
  get previousElementSibling() { return this._sib(-1); }
  _sib(d) {
    if (!this.parentNode) return null;
    const els = this.parentNode.children;
    return els[els.indexOf(this) + d] || null;
  }
  _adopt(n) {
    if (typeof n === 'string') n = new Text(n);
    if (n.parentNode) n.remove();
    n.parentNode = this;
    return n;
  }
  append(...ns) { ns.forEach((n) => this.childNodes.push(this._adopt(n))); }
  prepend(...ns) { ns.reverse().forEach((n) => this.childNodes.unshift(this._adopt(n))); }
  insertBefore(n, ref) {
    n = this._adopt(n);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n);
  }
  replaceChildren(...ns) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; this.append(...ns); }
  replaceWith(n) {
    const p = this.parentNode;
    if (!p) return;
    n = p._adopt(n);
    p.childNodes[p.childNodes.indexOf(this)] = n;
    this.parentNode = null;
  }
  remove() {
    if (!this.parentNode) return;
    const p = this.parentNode;
    p.childNodes.splice(p.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.replaceChildren(String(v)); }
}

class Text extends Node {
  constructor(t) { super(); this.data = String(t); }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

let ids = 0;
class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.attrs = {};
    this.dataset = {};
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    this.classList = new ClassList(this);
    this.listeners = {};
    this.value = '';
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.offsetHeight = 30;
    this.selectionStart = 0; this.selectionEnd = 0;
    this._id = ++ids;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  dispatch(type, extra) {
    const ev = Object.assign({ type, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} }, extra || {});
    (this.listeners[type] || []).forEach((f) => f(ev));
    return ev;
  }
  click() { this.dispatch('click'); }
  focus() { global.document.activeElement = this; }
  select() {}
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  scrollIntoView() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 }; }
  get innerText() { return this.textContent; }
  _matches(sel) {
    sel = sel.trim();
    if (sel[0] === '#') return this.id === sel.slice(1) || this.attrs.id === sel.slice(1);
    if (sel[0] === '.') return sel.slice(1).split('.').every((c) => this.classList.contains(c));
    if (sel[0] === '[') return this.dataset[sel.slice(6, -1)] !== undefined || sel.slice(1, -1) in this.attrs;
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) {
    const parts = sel.split(',');
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (parts.some((p) => c._matches(p))) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let e = this; e && e._matches; e = e.parentNode) if (sel.split(',').some((p) => e._matches(p))) return e; return null; }
}

function install() {
  const document = {
    activeElement: null,
    body: new Element('body'),
    documentElement: new Element('html'),
    hidden: false,
    createElement: (t) => new Element(t),
    createDocumentFragment: () => new Element('fragment'),
    addEventListener() {},
    querySelector: (s) => document.body.querySelector(s),
    querySelectorAll: (s) => document.body.querySelectorAll(s)
  };
  global.document = document;
  global.Node = Node;
  global.window = global;
  global.requestAnimationFrame = (f) => f();
  const winListeners = {};
  global.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
  global.removeEventListener = (t, fn) => { winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn); };
  global.dispatchEvent = (ev) => { (winListeners[ev.type] || []).slice().forEach((f) => f(ev)); return true; };
  if (typeof global.CustomEvent !== 'function') global.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
  global.innerWidth = 1400; global.innerHeight = 900;
  global.ResizeObserver = class { observe() {} disconnect() {} };
  global.history = { replaceState() {} };
  document.fonts = { load: () => Promise.resolve() };
  document.getElementById = (id) => document.body.querySelector('#' + id);
  global.location = { host: '127.0.0.1:1', pathname: '/', hash: '' };
  global.localStorage = { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } };
  global.sessionStorage = { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } };
  Object.defineProperty(global, 'navigator', { configurable: true, value: { clipboard: { writeText: () => Promise.resolve(), readText: () => Promise.resolve('') } } });
  global.sockets = [];
  global.WebSocket = class {
    constructor(url) { this.url = url; this.sent = []; this.readyState = 1; global.sockets.push(this); }
    send(d) { this.sent.push(JSON.parse(d)); }
    close() { this.readyState = 3; }
  };
  return document;
}

module.exports = { install, Element, Text };
