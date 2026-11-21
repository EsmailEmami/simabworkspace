// Simab Workspace — Share on your domain: the first script of every page of a shared port.
// Keeps this page on its project: WebSockets go to it, cookies and localStorage are its own.
(function () {
  'use strict';
  var me = document.currentScript, t = me && me.getAttribute('data-t');
  if (!t || window.__simabShim) return;
  window.__simabShim = t;
  try { sessionStorage.setItem('__simabT', t); } catch (e) { /* storage off */ }

  var swc = navigator.serviceWorker;
  if (swc) {
    swc.addEventListener('message', function (e) {
      if (e.data && e.data.simabWho && e.ports && e.ports[0]) e.ports[0].postMessage(t);
    });
    if (swc.startMessages) swc.startMessages();
    if (swc.controller) swc.controller.postMessage({ simabTab: t });
    var reg = swc.register.bind(swc);      // the page's own service worker would push ours out
    swc.register = function () {
      return swc.getRegistration('/').then(function (r) { return r || reg('/__simab/sw.js', { scope: '/' }); });
    };
  }

  var WS = window.WebSocket;
  if (WS) {
    var P = function (url, protocols) {
      var u = new URL(url, location.href);
      if (u.protocol === 'http:') u.protocol = 'ws:';
      if (u.protocol === 'https:') u.protocol = 'wss:';
      if (u.host === location.host && u.pathname.indexOf('/__simab/') !== 0) u.pathname = '/__simab/p/' + t + u.pathname;
      return protocols === undefined ? new WS(u.href) : new WS(u.href, protocols);
    };
    P.prototype = WS.prototype;
    ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) { P[k] = WS[k]; });
    window.WebSocket = P;
  }

  var CP = t.replace('/', '.') + '.';
  var cd = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  if (cd && cd.get && cd.set) {
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: function () {
        return cd.get.call(document).split('; ').filter(function (c) { return c.indexOf(CP) === 0 && c.length > CP.length; })
          .map(function (c) { return c.slice(CP.length); }).join('; ');
      },
      set: function (v) {
        var parts = String(v).split(';'), head = parts.shift().trim();
        if (head.indexOf('=') < 1) return;
        var attrs = parts.filter(function (a) { return a.trim() && !/^\s*(path|domain)\s*=/i.test(a); });
        cd.set.call(document, CP + head + (attrs.length ? ';' + attrs.join(';') : '') + '; path=/');
      }
    });
  }

  function wrap(name) {
    var real;
    try { real = window[name]; } catch (e) { return; }
    if (!real) return;
    var K = t + ':';
    function keys() {
      var o = [];
      for (var i = 0; i < real.length; i++) { var k = real.key(i); if (k && k.indexOf(K) === 0) o.push(k.slice(K.length)); }
      return o;
    }
    var api = {
      getItem: function (k) { return real.getItem(K + k); },
      setItem: function (k, v) { real.setItem(K + k, String(v)); },
      removeItem: function (k) { real.removeItem(K + k); },
      clear: function () { keys().forEach(function (k) { real.removeItem(K + k); }); },
      key: function (i) { var a = keys(); return i >= 0 && i < a.length ? a[i] : null; }
    };
    var p = new Proxy(api, {
      get: function (o, k) {
        if (k === 'length') return keys().length;
        if (typeof k !== 'string' || k in o) return o[k];
        var v = real.getItem(K + k);
        return v === null ? undefined : v;
      },
      set: function (o, k, v) { real.setItem(K + String(k), String(v)); return true; },
      has: function (o, k) { return k in o || real.getItem(K + String(k)) !== null; },
      deleteProperty: function (o, k) { real.removeItem(K + String(k)); return true; },
      ownKeys: function () { return keys(); },
      getOwnPropertyDescriptor: function (o, k) {
        var v = real.getItem(K + String(k));
        return v === null ? undefined : { value: v, writable: true, enumerable: true, configurable: true };
      }
    });
    try { Object.defineProperty(window, name, { configurable: true, get: function () { return p; } }); } catch (e) { /* kept */ }
  }
  wrap('localStorage');
})();
