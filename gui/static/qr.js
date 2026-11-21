// Tiny QR code maker (byte mode, error level M, versions 1-40) — no library.
// SWQR.matrix(text) -> rows of booleans (true = dark); SWQR.dataUrl(text) -> SVG data: URL for an <img>.
(function (root) {
  'use strict';
  // per version: EC codewords per block, number of blocks (level M)
  var EC_LEN = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28,
    28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28];
  var BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29,
    31, 33, 35, 37, 38, 40, 43, 45, 47, 49];

  function rawModules(v) {
    var r = (16 * v + 128) * v + 64;
    if (v >= 2) { var n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; }
    return r;
  }
  function dataCodewords(v) { return Math.floor(rawModules(v) / 8) - EC_LEN[v] * BLOCKS[v]; }
  function alignPos(v) {
    if (v === 1) return [];
    var n = Math.floor(v / 7) + 2, size = v * 4 + 17;
    var step = Math.floor((v * 8 + n * 3 + 5) / (n * 4 - 4)) * 2, out = [];
    for (var i = 0, pos = size - 7; i < n - 1; i++, pos -= step) out.unshift(pos);
    out.unshift(6);
    return out;
  }
  function gmul(x, y) {
    var z = 0;
    for (var i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11D); z ^= ((y >>> i) & 1) * x; }
    return z & 0xFF;
  }
  function rsDivisor(deg) {
    var d = []; for (var i = 0; i < deg; i++) d.push(0);
    d[deg - 1] = 1;
    var r = 1;
    for (i = 0; i < deg; i++) {
      for (var j = 0; j < deg; j++) { d[j] = gmul(d[j], r); if (j + 1 < deg) d[j] ^= d[j + 1]; }
      r = gmul(r, 2);
    }
    return d;
  }
  function rsRemainder(data, div) {
    var res = div.map(function () { return 0; });
    data.forEach(function (b) {
      var f = b ^ res.shift(); res.push(0);
      for (var i = 0; i < div.length; i++) res[i] ^= gmul(div[i], f);
    });
    return res;
  }
  function utf8(s) {
    var u = unescape(encodeURIComponent(s)), out = [];
    for (var i = 0; i < u.length; i++) out.push(u.charCodeAt(i));
    return out;
  }

  // data + error correction codewords, interleaved, for the smallest version that fits
  function codewords(bytes) {
    for (var v = 1; v <= 40; v++) {
      var cnt = v <= 9 ? 8 : 16;
      if (4 + cnt + bytes.length * 8 <= dataCodewords(v) * 8) break;
    }
    if (v > 40) throw new Error('Text too long for a QR code');
    var cap = dataCodewords(v) * 8, bits = [];
    function put(val, n) { for (var i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); }
    put(4, 4); put(bytes.length, cnt);
    bytes.forEach(function (b) { put(b, 8); });
    put(0, Math.min(4, cap - bits.length));
    put(0, (8 - bits.length % 8) % 8);
    for (var pad = 0xEC; bits.length < cap; pad ^= 0xEC ^ 0x11) put(pad, 8);
    var data = [];
    for (var i = 0; i < bits.length; i += 8) { var b = 0; for (var k = 0; k < 8; k++) b = (b << 1) | bits[i + k]; data.push(b); }
    // split into blocks: short ones first, long ones one data byte longer
    var nb = BLOCKS[v], ecl = EC_LEN[v], raw = Math.floor(rawModules(v) / 8);
    var nShort = nb - raw % nb, shortData = Math.floor(raw / nb) - ecl, div = rsDivisor(ecl), blocks = [], at = 0;
    for (i = 0; i < nb; i++) {
      var d = data.slice(at, at + shortData + (i < nShort ? 0 : 1)); at += d.length;
      blocks.push({ d: d, e: rsRemainder(d, div) });
    }
    var out = [];
    for (i = 0; i <= shortData; i++) blocks.forEach(function (bl) { if (i < bl.d.length) out.push(bl.d[i]); });
    for (i = 0; i < ecl; i++) blocks.forEach(function (bl) { out.push(bl.e[i]); });
    return { v: v, cw: out };
  }

  var MASKS = [
    function (x, y) { return (x + y) % 2 === 0; },
    function (x, y) { return y % 2 === 0; },
    function (x) { return x % 3 === 0; },
    function (x, y) { return (x + y) % 3 === 0; },
    function (x, y) { return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; },
    function (x, y) { return x * y % 2 + x * y % 3 === 0; },
    function (x, y) { return (x * y % 2 + x * y % 3) % 2 === 0; },
    function (x, y) { return ((x + y) % 2 + x * y % 3) % 2 === 0; }
  ];
  function formatBits(mask) {   // level M = 0b00
    var data = mask, rem = data;
    for (var i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    return ((data << 10) | rem) ^ 0x5412;
  }

  function build(v, cw, mask) {
    var size = v * 4 + 17, m = [], fn = [], x, y, i;
    for (y = 0; y < size; y++) { m.push([]); fn.push([]); for (x = 0; x < size; x++) { m[y].push(false); fn[y].push(false); } }
    function set(x, y, dark) { m[y][x] = dark; fn[y][x] = true; }
    for (i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    [[3, 3], [size - 4, 3], [3, size - 4]].forEach(function (c) {
      for (var dy = -4; dy <= 4; dy++) for (var dx = -4; dx <= 4; dx++) {
        var xx = c[0] + dx, yy = c[1] + dy, dist = Math.max(Math.abs(dx), Math.abs(dy));
        if (xx >= 0 && xx < size && yy >= 0 && yy < size) set(xx, yy, dist !== 2 && dist !== 4);
      }
    });
    var ap = alignPos(v), n = ap.length;
    for (i = 0; i < n; i++) for (var j = 0; j < n; j++) {
      if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue;
      for (var dy = -2; dy <= 2; dy++) for (var dx = -2; dx <= 2; dx++)
        set(ap[i] + dx, ap[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }
    var f = formatBits(mask);
    function fb(k) { return ((f >>> k) & 1) === 1; }
    for (i = 0; i <= 5; i++) set(8, i, fb(i));
    set(8, 7, fb(6)); set(8, 8, fb(7)); set(7, 8, fb(8));
    for (i = 9; i < 15; i++) set(14 - i, 8, fb(i));
    for (i = 0; i < 8; i++) set(size - 1 - i, 8, fb(i));
    for (i = 8; i < 15; i++) set(8, size - 15 + i, fb(i));
    set(8, size - 8, true);
    if (v >= 7) {
      var rem = v;
      for (i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
      var vb = (v << 12) | rem;
      for (i = 0; i < 18; i++) {
        var bit = ((vb >>> i) & 1) === 1, a = size - 11 + i % 3, b = Math.floor(i / 3);
        set(a, b, bit); set(b, a, bit);
      }
    }
    var k = 0, total = cw.length * 8;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (var vert = 0; vert < size; vert++) for (j = 0; j < 2; j++) {
        x = right - j;
        y = ((right + 1) & 2) === 0 ? size - 1 - vert : vert;
        if (fn[y][x]) continue;
        var dark = k < total && ((cw[k >>> 3] >>> (7 - (k & 7))) & 1) === 1;
        k++;
        m[y][x] = dark !== MASKS[mask](x, y);
      }
    }
    return m;
  }

  // lower = better: long runs, 2x2 blocks, dark/light imbalance
  function penalty(m) {
    var size = m.length, p = 0, dark = 0, x, y;
    for (var t = 0; t < 2; t++) for (y = 0; y < size; y++) {
      var run = 1;
      for (x = 1; x <= size; x++) {
        var cur = x < size ? (t ? m[x][y] : m[y][x]) : null, prev = t ? m[x - 1][y] : m[y][x - 1];
        if (cur === prev) run++; else { if (run >= 5) p += run - 2; run = 1; }
      }
    }
    for (y = 0; y < size; y++) for (x = 0; x < size; x++) {
      if (m[y][x]) dark++;
      if (x < size - 1 && y < size - 1 && m[y][x] === m[y][x + 1] && m[y][x] === m[y + 1][x] && m[y][x] === m[y + 1][x + 1]) p += 3;
    }
    return p + Math.floor(Math.abs(dark * 20 - size * size * 10) / (size * size)) * 10;
  }

  function matrix(text) {
    var c = codewords(utf8(String(text))), best = null, bp = Infinity;
    for (var mask = 0; mask < 8; mask++) {
      var m = build(c.v, c.cw, mask), p = penalty(m);
      if (p < bp) { bp = p; best = m; }
    }
    return best;
  }
  function svg(text) {
    var m = matrix(text), n = m.length + 8, d = '';
    for (var y = 0; y < m.length; y++) for (var x = 0; x < m.length; x++) if (m[y][x]) d += 'M' + (x + 4) + ' ' + (y + 4) + 'h1v1h-1z';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + n + ' ' + n + '" shape-rendering="crispEdges">' +
      '<rect width="' + n + '" height="' + n + '" fill="#fff"/><path fill="#000" d="' + d + '"/></svg>';
  }
  function dataUrl(text) { return 'data:image/svg+xml,' + encodeURIComponent(svg(text)); }

  root.SWQR = { matrix: matrix, svg: svg, dataUrl: dataUrl, _formatBits: formatBits, _codewords: codewords, _gmul: gmul };
})(typeof window !== 'undefined' ? window : globalThis);
