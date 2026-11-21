// QR maker (gui/static/qr.js) under node:  node tests/js/qr_test.js
// Decodes what it draws with an independent reader: format bits, unmasking, RS syndromes, byte-mode text.
'use strict';
const path = require('path');
const assert = require('assert');
global.window = global;
require(path.resolve(__dirname, '..', '..', 'gui/static/qr.js'));
const Q = global.SWQR;

// known values (ISO 18004): format word for level M / mask 0, and total codewords + level-M blocks per version
assert.strictEqual(Q._formatBits(0), 0b101010000010010);
assert.strictEqual(Q._formatBits(5), 0b100000011001110);
const TOTAL = [0, 26, 44, 70, 100, 134, 172, 196, 242, 292, 346];
const EC = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
const NB = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
const ALIGN = { 2: [6, 18], 5: [6, 30], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50] };

// GF(256) for syndromes
const EXP = [], LOG = [];
for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 256) x ^= 0x11D; }
const mul = (a, b) => (a && b ? EXP[(LOG[a] + LOG[b]) % 255] : 0);

function decode(m) {
  const size = m.length, v = (size - 17) / 4;
  assert.ok(Number.isInteger(v) && v >= 1 && v <= 10, 'size ' + size);
  // finder patterns
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]])
    for (let dy = -3; dy <= 3; dy++) for (let dx = -3; dx <= 3; dx++)
      assert.strictEqual(m[cy + dy][cx + dx], Math.max(Math.abs(dx), Math.abs(dy)) !== 2, 'finder');
  for (let i = 8; i < size - 8; i++) { assert.strictEqual(m[6][i], i % 2 === 0); assert.strictEqual(m[i][6], i % 2 === 0); }
  assert.strictEqual(m[size - 8][8], true, 'dark module');
  // format: both copies agree, valid BCH word for level M
  let f1 = 0, f2 = 0;
  const a = [[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8], [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8]];
  a.forEach(([x, y], i) => { if (m[y][x]) f1 |= 1 << i; });
  for (let i = 0; i < 8; i++) if (m[8][size - 1 - i]) f2 |= 1 << i;
  for (let i = 8; i < 15; i++) if (m[size - 15 + i][8]) f2 |= 1 << i;
  assert.strictEqual(f1, f2, 'format copies');
  const mask = ((f1 ^ 0x5412) >> 10) & 7;
  assert.strictEqual((f1 ^ 0x5412) >> 13, 0, 'level M');
  assert.strictEqual(Q._formatBits(mask), f1);
  // function modules
  const fn = m.map((r) => r.map(() => false));
  const mark = (x0, y0, w, h) => { for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) if (x >= 0 && y >= 0 && x < size && y < size) fn[y][x] = true; };
  mark(0, 0, 9, 9); mark(size - 8, 0, 8, 9); mark(0, size - 8, 9, 8);
  mark(6, 0, 1, size); mark(0, 6, size, 1);
  const ap = v === 1 ? [] : ALIGN[v] || (assert.ok(v < 7, 'align table'), [6, size - 7]);
  ap.forEach((ax, i) => ap.forEach((ay, j) => {
    if ((i === 0 && j === 0) || (i === 0 && j === ap.length - 1) || (i === ap.length - 1 && j === 0)) return;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++)
      assert.strictEqual(m[ay + dy][ax + dx], Math.max(Math.abs(dx), Math.abs(dy)) !== 1, 'alignment');
    mark(ax - 2, ay - 2, 5, 5);
  }));
  if (v >= 7) { mark(size - 11, 0, 3, 6); mark(0, size - 11, 6, 3); }
  const maskFn = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
    (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => (((x + y) % 2) + (x * y) % 3) % 2 === 0][mask];
  // zigzag read
  const bits = [];
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    const up = ((right + 1) & 2) === 0;
    for (let k = 0; k < size; k++) for (let j = 0; j < 2; j++) {
      const x = right - j, y = up ? size - 1 - k : k;
      if (!fn[y][x]) bits.push(m[y][x] !== maskFn(x, y) ? 1 : 0);
    }
  }
  const cw = [];
  for (let i = 0; i + 8 <= bits.length && cw.length < TOTAL[v]; i += 8) cw.push(bits.slice(i, i + 8).reduce((s, b) => s * 2 + b, 0));
  assert.strictEqual(cw.length, TOTAL[v]);
  // de-interleave and check each block's syndromes
  const nb = NB[v], ec = EC[v], dataTotal = TOTAL[v] - ec * nb, nShort = nb - (dataTotal % nb), sd = Math.floor(dataTotal / nb);
  const blocks = []; for (let b = 0; b < nb; b++) blocks.push({ d: [], e: [], len: sd + (b < nShort ? 0 : 1) });
  let p = 0;
  for (let i = 0; i <= sd; i++) blocks.forEach((bl) => { if (i < bl.len) bl.d.push(cw[p++]); });
  for (let i = 0; i < ec; i++) blocks.forEach((bl) => bl.e.push(cw[p++]));
  blocks.forEach((bl) => {
    const all = bl.d.concat(bl.e);
    for (let i = 0; i < ec; i++) {
      let s = 0; for (const c of all) s = mul(s, EXP[i]) ^ c;
      assert.strictEqual(s, 0, 'RS syndrome');
    }
  });
  // byte mode text
  const data = [].concat(...blocks.map((b) => b.d));
  const db = []; data.forEach((c) => { for (let i = 7; i >= 0; i--) db.push((c >> i) & 1); });
  const rd = (n) => db.splice(0, n).reduce((s, b) => s * 2 + b, 0);
  assert.strictEqual(rd(4), 4, 'byte mode');
  const n = rd(v <= 9 ? 8 : 16), out = [];
  for (let i = 0; i < n; i++) out.push(rd(8));
  return { v, text: Buffer.from(out).toString('utf8') };
}

const cases = [
  'A', 'hello',
  'otpauth://totp/Simab%20computer%3Amy-pc?secret=LIPG43CZCJTOWM4IJC6TBJ4VIAELYGHQ&issuer=Simab',
  'otpauth://totp/Simab%20Workspace%3Asomeone%40example.com?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Simab%20Workspace',
  'x'.repeat(150), 'y'.repeat(200), 'é ünïcode ✓'];
const seen = new Set();
for (const t of cases) {
  const r = decode(Q.matrix(t));
  assert.strictEqual(r.text, t);
  seen.add(r.v);
}
assert.ok(seen.has(1) && [...seen].some((v) => v >= 7), 'covers small and version-info sizes: ' + [...seen]);

// svg: square, quiet zone, only safe characters in the data URL
const svg = Q.svg('hello');
assert.ok(/^<svg [^>]*viewBox="0 0 29 29"/.test(svg), svg.slice(0, 80));
assert.ok(Q.dataUrl('a"b<c').startsWith('data:image/svg+xml,%3Csvg'));
assert.throws(() => Q.matrix('z'.repeat(3000)));
console.log('qr ok');
