/**
 * NLTP v1.2 Wire Protocol, GF(2^8) Algebraic Cauchy FEC, Pure-JS SHA-256,
 * RFC 8439 ChaCha20-Poly1305 AEAD, RFC 6479 ReplayWindow64, STUN & CGNAT Relay Framing.
 *
 * Pure ES2022 (`Uint8Array` / `DataView`, tested on Node.js 18+)
 * with zero external dependencies and byte-for-byte wire compatibility with Python (`necklink/nltp.py`),
 * ANSI C99 (`csrc/nltp_wire.h`), Arduino/ESP32 (`arduino/NLTP`), and Java (`io.necklink.nltp`).
 */

export const NLTP_MAGIC_0 = 0x4e; // 'N'
export const NLTP_MAGIC_1 = 0x4c; // 'L'
export const NLTP_VERSION = 1;
export const FLAG_ENCRYPTED = 0x80;

export const HEADER_SIZE = 16;      // magic(2) + ver_type(1) + flags(1) + conn_id(4) + crc32(4) + ts_ms(4)
export const ENC_HEADER_SIZE = 20;  // magic(2) + ver_type(1) + flags(1) + conn_id(4) + seq64(8) + ts_ms(4)
export const AEAD_TAG_SIZE = 16;    // 128-bit Poly1305 tag
export const SHARD_HDR_SIZE = 12;   // stream_id(4) + gen_id(4) + k_shards(1) + shard_idx(1) + row_width(2)

export const DEFAULT_SHARD_BYTES = 960;
export const DEFAULT_GEN_K = 8;

// Packet types (4-bit nibble in version_type)
export const PT_DATA_SHARD = 0x01;
export const PT_CRYPTIC_PARITY = 0x02;
export const PT_PROPRIOCEPTIVE_ACK = 0x03;
export const PT_STREAM_META = 0x04;
export const PT_STREAM_FIN_ACK = 0x05;
export const PT_REFLEX_MSG = 0x06;
export const PT_NEURAL_STATE = 0x07;
export const PT_PING = 0x08;
export const PT_PONG = 0x09;
export const PT_REFLEX_ACK = 0x0a; // receiver -> sender: u32 reflex seq (reliable reflex lane, v1.3.2)

// Relay & STUN constants
export const RELAY_MAGIC_0 = 0x4e; // 'N'
export const RELAY_MAGIC_1 = 0x52; // 'R'
export const RELAY_VER_DATA = 0x01;
export const RELAY_VER_REGISTER = 0x02;
export const RELAY_VER_REG_ACK = 0x03;
export const RELAY_HDR_SIZE = 19;

export const STUN_MAGIC_COOKIE = 0x2112a442;
export const STUN_BINDING_REQUEST = 0x0001;
export const STUN_BINDING_SUCCESS = 0x0101;
export const STUN_ATTR_MAPPED_ADDRESS = 0x0001;
export const STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020;

const TEXT_ENCODER = new TextEncoder();
const TEXT_DECODER = new TextDecoder('utf-8');

export function utf8Encode(str) {
  return TEXT_ENCODER.encode(str);
}

export function utf8Decode(bytes) {
  return TEXT_DECODER.decode(bytes);
}

export function toUint8Array(data) {
  if (data instanceof Uint8Array) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  if (data instanceof ArrayBuffer) {
    return new Uint8Array(data);
  }
  if (typeof data === 'string') {
    return utf8Encode(data);
  }
  throw new TypeError('Expected Uint8Array, ArrayBuffer, or string');
}

export function randomBytes(len) {
  // Keys, session nonces and conn_ids must be unpredictable: never fall back to Math.random().
  // Standard Web Crypto (globalThis.crypto) is built into Node 19+ and Web Crypto runtimes; the Node UDP module installs it on Node 18.
  if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== 'function') {
    throw new Error('nltp: no secure random source (globalThis.crypto.getRandomValues is missing)');
  }
  const out = new Uint8Array(len);
  globalThis.crypto.getRandomValues(out);
  return out;
}

// ---------------------------------------------------------------------------
// Little-Endian & Big-Endian Load/Store Helpers
// ---------------------------------------------------------------------------

export function readU16LE(buf, off) {
  return buf[off] | (buf[off + 1] << 8);
}

export function writeU16LE(buf, off, val) {
  buf[off] = val & 0xff;
  buf[off + 1] = (val >>> 8) & 0xff;
}

export function readU32LE(buf, off) {
  return (
    (buf[off] |
      (buf[off + 1] << 8) |
      (buf[off + 2] << 16) |
      (buf[off + 3] << 24)) >>>
    0
  );
}

export function writeU32LE(buf, off, val) {
  buf[off] = val & 0xff;
  buf[off + 1] = (val >>> 8) & 0xff;
  buf[off + 2] = (val >>> 16) & 0xff;
  buf[off + 3] = (val >>> 24) & 0xff;
}

export function readU64LE(buf, off) {
  const lo = BigInt(readU32LE(buf, off));
  const hi = BigInt(readU32LE(buf, off + 4));
  return lo | (hi << 32n);
}

export function writeU64LE(buf, off, valBig) {
  const v = BigInt(valBig);
  writeU32LE(buf, off, Number(v & 0xffffffffn));
  writeU32LE(buf, off + 4, Number((v >> 32n) & 0xffffffffn));
}

function readU16BE(buf, off) {
  return (buf[off] << 8) | buf[off + 1];
}

function writeU16BE(buf, off, val) {
  buf[off] = (val >>> 8) & 0xff;
  buf[off + 1] = val & 0xff;
}

function readU32BE(buf, off) {
  return (
    ((buf[off] << 24) |
      (buf[off + 1] << 16) |
      (buf[off + 2] << 8) |
      buf[off + 3]) >>>
    0
  );
}

function writeU32BE(buf, off, val) {
  buf[off] = (val >>> 24) & 0xff;
  buf[off + 1] = (val >>> 16) & 0xff;
  buf[off + 2] = (val >>> 8) & 0xff;
  buf[off + 3] = val & 0xff;
}

// ---------------------------------------------------------------------------
// IEEE 802.3 CRC32 (Precomputed 256-entry table)
// ---------------------------------------------------------------------------

const CRC32_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = (c & 1) !== 0 ? (0xedb88320 ^ (c >>> 1)) >>> 0 : c >>> 1;
    }
    t[i] = c >>> 0;
  }
  return t;
})();

export function crc32Ieee(buf, initCrc = 0) {
  let crc = (~initCrc) >>> 0;
  for (let i = 0; i < buf.length; i++) {
    crc = (CRC32_TABLE[(crc ^ buf[i]) & 0xff] ^ (crc >>> 8)) >>> 0;
  }
  return (~crc) >>> 0;
}

// ---------------------------------------------------------------------------
// Pure-JS FIPS 180-4 SHA-256 (Synchronous, Portable Browser + Node)
// ---------------------------------------------------------------------------

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr32(x, n) {
  return ((x >>> n) | (x << (32 - n))) >>> 0;
}

export function sha256Bytes(msgInput) {
  const msg = toUint8Array(msgInput);
  const bitLenHi = Math.floor((msg.length * 8) / 0x100000000) >>> 0;
  const bitLenLo = (msg.length * 8) >>> 0;
  const padLen = ((msg.length + 9 + 63) & ~63);
  const padded = new Uint8Array(padLen);
  padded.set(msg, 0);
  padded[msg.length] = 0x80;
  writeU32BE(padded, padLen - 8, bitLenHi);
  writeU32BE(padded, padLen - 4, bitLenLo);

  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const w = new Uint32Array(64);

  for (let off = 0; off < padLen; off += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = readU32BE(padded, off + i * 4);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
      const ch = (e & f) ^ ((~e) & g);
      const temp1 = (h + S1 + ch + SHA256_K[i] + w[i]) >>> 0;
      const S0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;

      h = g; g = f; f = e;
      e = (d + temp1) >>> 0;
      d = c; c = b; b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }

  const out = new Uint8Array(32);
  writeU32BE(out, 0, h0); writeU32BE(out, 4, h1); writeU32BE(out, 8, h2); writeU32BE(out, 12, h3);
  writeU32BE(out, 16, h4); writeU32BE(out, 20, h5); writeU32BE(out, 24, h6); writeU32BE(out, 28, h7);
  return out;
}

const HEX_CHARS = '0123456789abcdef';
export function bytesToHex(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i];
    s += HEX_CHARS[b >>> 4] + HEX_CHARS[b & 0x0f];
  }
  return s;
}

export function sha256Hex(msgInput) {
  return bytesToHex(sha256Bytes(msgInput));
}

// ---------------------------------------------------------------------------
// GF(2^8) Algebraic Cauchy FEC over x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
// ---------------------------------------------------------------------------

const { GF_EXP, GF_LOG } = (() => {
  const exp = new Uint8Array(512);
  const log = new Uint8Array(256);
  let x = 1;
  for (let i = 0; i < 255; i++) {
    exp[i] = x;
    log[x] = i;
    x <<= 1;
    if ((x & 0x100) !== 0) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) {
    exp[i] = exp[i - 255];
  }
  return { GF_EXP: exp, GF_LOG: log };
})();

export function gf256Mul(a, b) {
  return a !== 0 && b !== 0 ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0;
}

export function gf256Inv(a) {
  if (a === 0) throw new RangeError('gf256Inv(0) is undefined');
  return GF_EXP[255 - GF_LOG[a]];
}

const GF_MUL_SCRATCH = new Uint8Array(256);

export function gf256Axpy(dst, src, factor) {
  if (factor === 0) return;
  const n = dst.length;
  let i = 0;
  if (factor === 1) {
    for (; i + 4 <= n; i += 4) {
      dst[i] ^= src[i];
      dst[i + 1] ^= src[i + 1];
      dst[i + 2] ^= src[i + 2];
      dst[i + 3] ^= src[i + 3];
    }
    for (; i < n; i++) dst[i] ^= src[i];
    return;
  }
  const lf = GF_LOG[factor];
  GF_MUL_SCRATCH[0] = 0;
  for (let x = 1; x < 256; x++) {
    GF_MUL_SCRATCH[x] = GF_EXP[lf + GF_LOG[x]];
  }
  for (; i + 4 <= n; i += 4) {
    dst[i] ^= GF_MUL_SCRATCH[src[i]];
    dst[i + 1] ^= GF_MUL_SCRATCH[src[i + 1]];
    dst[i + 2] ^= GF_MUL_SCRATCH[src[i + 2]];
    dst[i + 3] ^= GF_MUL_SCRATCH[src[i + 3]];
  }
  for (; i < n; i++) {
    dst[i] ^= GF_MUL_SCRATCH[src[i]];
  }
}

export function gf256Scale(buf, factor) {
  if (factor === 1) return;
  if (factor === 0) {
    buf.fill(0);
    return;
  }
  const n = buf.length;
  const lf = GF_LOG[factor];
  GF_MUL_SCRATCH[0] = 0;
  for (let x = 1; x < 256; x++) {
    GF_MUL_SCRATCH[x] = GF_EXP[lf + GF_LOG[x]];
  }
  let i = 0;
  for (; i + 4 <= n; i += 4) {
    buf[i] = GF_MUL_SCRATCH[buf[i]];
    buf[i + 1] = GF_MUL_SCRATCH[buf[i + 1]];
    buf[i + 2] = GF_MUL_SCRATCH[buf[i + 2]];
    buf[i + 3] = GF_MUL_SCRATCH[buf[i + 3]];
  }
  for (; i < n; i++) {
    buf[i] = GF_MUL_SCRATCH[buf[i]];
  }
}

// ---------------------------------------------------------------------------
// Zero-Dependency Standard LZ4 Raw Block Codec (NLTP 1.6)
// ---------------------------------------------------------------------------

export const MAX_LZ4_RAW_BYTES = 64 * 1024 * 1024;

export function lz4Compress(srcInput, hashLog = 12) {
  const src = toUint8Array(srcInput);
  const srcLen = src.length;
  if (srcLen < 16 || hashLog < 8 || hashLog > 16) return null;

  const htab = new Int32Array(1 << hashLog);
  const shift = 32 - hashLog;
  const limit = srcLen - 12;
  const matchLimit = srcLen - 5;
  const out = new Uint8Array(srcLen);
  let op = 0;
  let anchor = 0;
  let ip = 1;

  while (ip <= limit) {
    let step = 1;
    let searchMatchNb = 1 << 6;
    let ref = 0;
    while (true) {
      const v = readU32LE(src, ip);
      const h = (Math.imul(v, 0x9e3779b1) >>> shift);
      ref = htab[h];
      htab[h] = ip;
      if (ip - ref <= 65535 && readU32LE(src, ref) === v) break;
      ip += step;
      if (ip > limit) break;
      searchMatchNb += 1;
      step = searchMatchNb >>> 6;
    }
    if (ip > limit) break;

    while (ip > anchor && ref > 0 && src[ip - 1] === src[ref - 1]) {
      ip -= 1;
      ref -= 1;
    }

    const litLen = ip - anchor;
    let mlen = 4;
    while (ip + mlen < matchLimit && src[ip + mlen] === src[ref + mlen]) {
      mlen += 1;
    }

    const mlCode = mlen - 4;
    const need = 1 + Math.trunc(litLen / 255) + 1 + litLen + 2 + Math.trunc(mlCode / 255) + 1;
    if (op + need >= srcLen) return null;

    out[op++] = ((litLen >= 15 ? 15 : litLen) << 4) | (mlCode >= 15 ? 15 : mlCode);
    if (litLen >= 15) {
      let rem = litLen - 15;
      while (rem >= 255) {
        out[op++] = 255;
        rem -= 255;
      }
      out[op++] = rem;
    }
    if (litLen > 0) {
      out.set(src.subarray(anchor, anchor + litLen), op);
      op += litLen;
    }

    const off = ip - ref;
    out[op++] = off & 0xff;
    out[op++] = (off >>> 8) & 0xff;

    if (mlCode >= 15) {
      let rem = mlCode - 15;
      while (rem >= 255) {
        out[op++] = 255;
        rem -= 255;
      }
      out[op++] = rem;
    }

    ip += mlen;
    anchor = ip;
    if (ip <= limit) {
      const vp = readU32LE(src, ip - 2);
      htab[Math.imul(vp, 0x9e3779b1) >>> shift] = ip - 2;
    }
  }

  const lastLit = srcLen - anchor;
  if (op + 1 + Math.trunc(lastLit / 255) + 1 + lastLit >= srcLen) return null;
  out[op++] = (lastLit >= 15 ? 15 : lastLit) << 4;
  if (lastLit >= 15) {
    let rem = lastLit - 15;
    while (rem >= 255) {
      out[op++] = 255;
      rem -= 255;
    }
    out[op++] = rem;
  }
  if (lastLit > 0) {
    out.set(src.subarray(anchor, anchor + lastLit), op);
    op += lastLit;
  }
  return op < srcLen ? out.slice(0, op) : null;
}

export function lz4Decompress(srcInput, expectedRawLen) {
  const src = toUint8Array(srcInput);
  if (expectedRawLen === 0) return src.length === 0 ? new Uint8Array(0) : null;
  if (!Number.isInteger(expectedRawLen) || expectedRawLen < 0 || expectedRawLen > MAX_LZ4_RAW_BYTES || src.length === 0) {
    return null;
  }
  const srcLen = src.length;
  const out = new Uint8Array(expectedRawLen);
  let ip = 0;
  let op = 0;

  while (ip < srcLen) {
    const token = src[ip++];
    let litLen = (token >>> 4) & 0x0f;
    if (litLen === 15) {
      while (true) {
        if (ip >= srcLen) return null;
        const s = src[ip++];
        litLen += s;
        if (op + litLen > expectedRawLen) return null;
        if (s !== 255) break;
      }
    }
    if (ip + litLen > srcLen || op + litLen > expectedRawLen) return null;
    if (litLen > 0) {
      out.set(src.subarray(ip, ip + litLen), op);
      ip += litLen;
      op += litLen;
    }
    if (ip === srcLen) break;
    if (ip + 2 > srcLen) return null;
    const off = src[ip] | (src[ip + 1] << 8);
    ip += 2;
    if (off === 0 || off > op) return null;

    let mlen = (token & 0x0f) + 4;
    if ((token & 0x0f) === 15) {
      while (true) {
        if (ip >= srcLen) return null;
        const s = src[ip++];
        mlen += s;
        if (op + mlen > expectedRawLen) return null;
        if (s !== 255) break;
      }
    }
    if (op + mlen > expectedRawLen) return null;

    let ref = op - off;
    if (off >= mlen) {
      out.copyWithin(op, ref, ref + mlen);
      op += mlen;
    } else {
      for (let i = 0; i < mlen; i++) {
        out[op++] = out[ref++];
      }
    }
  }
  return op === expectedRawLen ? out : null;
}

export function cauchyRow(parityOffset, kShards) {
  const xR = 64 + (((parityOffset % 192) + 192) % 192);
  const row = new Uint8Array(kShards);
  for (let c = 0; c < kShards; c++) {
    row[c] = gf256Inv(xR ^ (c & 0x3f));
  }
  return row;
}

export function solveLinearSystemGF256(coeffRows, dataRows, k, rowWidth) {
  // Gauss-Jordan elimination over GF(2^8): transforms [A | Y] -> [I | X]
  const a = new Array(k);
  const y = new Array(k);
  for (let r = 0; r < k; r++) {
    a[r] = new Uint8Array(coeffRows[r]);
    y[r] = new Uint8Array(dataRows[r]);
  }

  for (let col = 0; col < k; col++) {
    let pivot = -1;
    for (let r = col; r < k; r++) {
      if (a[r][col] !== 0) {
        pivot = r;
        break;
      }
    }
    if (pivot < 0) return null;
    if (pivot !== col) {
      const tmpA = a[col]; a[col] = a[pivot]; a[pivot] = tmpA;
      const tmpY = y[col]; y[col] = y[pivot]; y[pivot] = tmpY;
    }

    const invPivot = gf256Inv(a[col][col]);
    gf256Scale(a[col], invPivot);
    gf256Scale(y[col], invPivot);

    for (let r = 0; r < k; r++) {
      if (r !== col && a[r][col] !== 0) {
        const factor = a[r][col];
        gf256Axpy(a[r], a[col], factor);
        gf256Axpy(y[r], y[col], factor);
      }
    }
  }
  return y;
}

// ---------------------------------------------------------------------------
// RFC 8439 ChaCha20-Poly1305 AEAD & Key Derivation (Exact Python/C99 Parity)
// ---------------------------------------------------------------------------

function rotl32(x, n) {
  return ((x << n) | (x >>> (32 - n))) >>> 0;
}

export function chacha20Block(key32, counter, nonce12, out64) {
  const st = new Uint32Array(16);
  const x = new Uint32Array(16);
  st[0] = 0x61707865;
  st[1] = 0x3320646e;
  st[2] = 0x79622d32;
  st[3] = 0x6b206574;
  for (let i = 0; i < 8; i++) st[4 + i] = readU32LE(key32, i * 4);
  st[12] = counter >>> 0;
  st[13] = readU32LE(nonce12, 0);
  st[14] = readU32LE(nonce12, 4);
  st[15] = readU32LE(nonce12, 8);

  x.set(st);
  const qr = (ia, ib, ic, id) => {
    x[ia] = (x[ia] + x[ib]) >>> 0; x[id] = rotl32(x[id] ^ x[ia], 16);
    x[ic] = (x[ic] + x[id]) >>> 0; x[ib] = rotl32(x[ib] ^ x[ic], 12);
    x[ia] = (x[ia] + x[ib]) >>> 0; x[id] = rotl32(x[id] ^ x[ia], 8);
    x[ic] = (x[ic] + x[id]) >>> 0; x[ib] = rotl32(x[ib] ^ x[ic], 7);
  };

  for (let i = 0; i < 10; i++) {
    qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15);
    qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14);
  }
  for (let i = 0; i < 16; i++) {
    writeU32LE(out64, i * 4, (x[i] + st[i]) >>> 0);
  }
}

export function chacha20Xor(key32, initCounter, nonce12, input) {
  const out = new Uint8Array(input.length);
  const ks = new Uint8Array(64);
  let ctr = initCounter >>> 0;
  let off = 0;
  while (off < input.length) {
    const take = Math.min(64, input.length - off);
    chacha20Block(key32, ctr, nonce12, ks);
    for (let i = 0; i < take; i++) {
      out[off + i] = input[off + i] ^ ks[i];
    }
    off += take;
    ctr = (ctr + 1) >>> 0;
  }
  return out;
}

const POLY1305_P = (1n << 130n) - 5n;

function readU128LEBig(buf, off, len) {
  let acc = 0n;
  for (let i = len - 1; i >= 0; i--) {
    acc = (acc << 8n) | BigInt(buf[off + i]);
  }
  return acc;
}

export function poly1305AeadTag(key32, nonce12, aad, ct) {
  const block0 = new Uint8Array(64);
  chacha20Block(key32, 0, nonce12, block0);

  const rBytes = block0.slice(0, 16);
  rBytes[3] &= 15; rBytes[7] &= 15; rBytes[11] &= 15; rBytes[15] &= 15;
  rBytes[4] &= 252; rBytes[8] &= 252; rBytes[12] &= 252;

  const r = readU128LEBig(rBytes, 0, 16);
  const s = readU128LEBig(block0, 16, 16);
  let h = 0n;

  const feedPadded16 = (buf) => {
    let off = 0;
    while (off + 16 <= buf.length) {
      const n = readU128LEBig(buf, off, 16) | (1n << 128n);
      h = ((h + n) * r) % POLY1305_P;
      off += 16;
    }
    if (off < buf.length) {
      const rem = buf.length - off;
      const tail = new Uint8Array(16);
      tail.set(buf.subarray(off, buf.length), 0);
      const n = readU128LEBig(tail, 0, 16) | (1n << 128n);
      h = ((h + n) * r) % POLY1305_P;
    }
  };

  feedPadded16(aad);
  feedPadded16(ct);

  const lengths16 = new Uint8Array(16);
  writeU64LE(lengths16, 0, BigInt(aad.length));
  writeU64LE(lengths16, 8, BigInt(ct.length));
  const nLen = readU128LEBig(lengths16, 0, 16) | (1n << 128n);
  h = ((h + nLen) * r) % POLY1305_P;

  const mac = (h + s) & ((1n << 128n) - 1n);
  const tag = new Uint8Array(16);
  let m = mac;
  for (let i = 0; i < 16; i++) {
    tag[i] = Number(m & 0xffn);
    m >>= 8n;
  }
  return tag;
}

const B64URL_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

export function base64UrlEncode(bytes) {
  let out = '';
  let i = 0;
  while (i + 3 <= bytes.length) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += B64URL_ALPHABET[(n >>> 18) & 63] +
           B64URL_ALPHABET[(n >>> 12) & 63] +
           B64URL_ALPHABET[(n >>> 6) & 63] +
           B64URL_ALPHABET[n & 63];
    i += 3;
  }
  const rem = bytes.length - i;
  if (rem === 1) {
    const n = bytes[i] << 16;
    out += B64URL_ALPHABET[(n >>> 18) & 63] + B64URL_ALPHABET[(n >>> 12) & 63] + '==';
  } else if (rem === 2) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8);
    out += B64URL_ALPHABET[(n >>> 18) & 63] + B64URL_ALPHABET[(n >>> 12) & 63] + B64URL_ALPHABET[(n >>> 6) & 63] + '=';
  }
  return out;
}

function tryDecodeUrlSafeKey32(s) {
  if (s.length !== 43 && s.length !== 44) return null;
  if (!/^[A-Za-z0-9_-]{43}=?$/.test(s)) return null;
  const out = new Uint8Array(32);
  let acc = 0;
  let bits = 0;
  let pos = 0;
  for (let i = 0; i < 43; i++) {
    const code = s.charCodeAt(i);
    let v = -1;
    if (code >= 65 && code <= 90) v = code - 65;
    else if (code >= 97 && code <= 122) v = code - 97 + 26;
    else if (code >= 48 && code <= 57) v = code - 48 + 52;
    else if (code === 45) v = 62;
    else if (code === 95) v = 63;
    if (v < 0) return null;
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (pos < 32) out[pos++] = (acc >>> bits) & 0xff;
    }
  }
  return pos === 32 ? out : null;
}

export function generateNltpKey() {
  return base64UrlEncode(randomBytes(32));
}

export function deriveAeadKey(psk) {
  if (psk instanceof Uint8Array || ArrayBuffer.isView(psk) || psk instanceof ArrayBuffer) {
    const raw = toUint8Array(psk);
    if (raw.length === 32) return new Uint8Array(raw);
    const pref = utf8Encode('NLTP-v1-RAW-KEY:');
    const cat = new Uint8Array(pref.length + raw.length);
    cat.set(pref, 0);
    cat.set(raw, pref.length);
    return sha256Bytes(cat);
  }
  if (typeof psk !== 'string') {
    throw new TypeError('psk must be a string or Uint8Array');
  }
  const s = psk.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, '');
  const decoded32 = tryDecodeUrlSafeKey32(s);
  if (decoded32 !== null) return decoded32;

  const pref = utf8Encode('NLTP-v1-PASSPHRASE:');
  const body = utf8Encode(s);
  const cat = new Uint8Array(pref.length + body.length);
  cat.set(pref, 0);
  cat.set(body, pref.length);
  return sha256Bytes(cat);
}

const REPLAY_WINDOW_BITS = 512n;
const REPLAY_WINDOW_MASK = (1n << 512n) - 1n;

export class ReplayWindow64 {
  constructor() {
    this.highestSeq = 0n;
    this.bitmap = 0n;
  }

  wouldAccept(seqInput) {
    const seq = BigInt(seqInput);
    if (seq <= 0n) return false;
    if (seq > this.highestSeq) return true;
    const diff = this.highestSeq - seq;
    if (diff >= REPLAY_WINDOW_BITS) return false;
    return (this.bitmap & (1n << diff)) === 0n;
  }

  accept(seqInput) {
    const seq = BigInt(seqInput);
    if (!this.wouldAccept(seq)) return false;
    if (seq > this.highestSeq) {
      const shift = seq - this.highestSeq;
      if (shift >= REPLAY_WINDOW_BITS) {
        this.bitmap = 1n;
      } else {
        this.bitmap = ((this.bitmap << shift) | 1n) & REPLAY_WINDOW_MASK;
      }
      this.highestSeq = seq;
    } else {
      const diff = this.highestSeq - seq;
      this.bitmap |= 1n << diff;
    }
    return true;
  }
}

export class AEADCipherContext {
  constructor(psk) {
    this.key = deriveAeadKey(psk);
    this.txSeq = 0n;
    this.replayWindows = new Map();
  }

  nextSeq() {
    this.txSeq += 1n;
    return this.txSeq;
  }

  seal(connId, seq64, aadHdr20, plaintext) {
    const nonce = new Uint8Array(12);
    writeU32LE(nonce, 0, connId >>> 0);
    writeU64LE(nonce, 4, seq64);
    const ct = chacha20Xor(this.key, 1, nonce, plaintext);
    const tag = poly1305AeadTag(this.key, nonce, aadHdr20, ct);
    const out = new Uint8Array(ct.length + 16);
    out.set(ct, 0);
    out.set(tag, ct.length);
    return out;
  }

  openAndCheckReplay(peerConnId, seq64, aadHdr20, ciphertextWithTag) {
    const seq = BigInt(seq64);
    if (seq <= 0n || ciphertextWithTag.length < AEAD_TAG_SIZE) return null;
    const cid = peerConnId >>> 0;
    const win = this.replayWindows.get(cid);
    if (win !== undefined && !win.wouldAccept(seq)) return null;

    const ctLen = ciphertextWithTag.length - AEAD_TAG_SIZE;
    const ct = ciphertextWithTag.subarray(0, ctLen);
    const rxTag = ciphertextWithTag.subarray(ctLen, ctLen + AEAD_TAG_SIZE);

    const nonce = new Uint8Array(12);
    writeU32LE(nonce, 0, cid);
    writeU64LE(nonce, 4, seq);

    const calcTag = poly1305AeadTag(this.key, nonce, aadHdr20, ct);
    let diff = 0;
    for (let i = 0; i < 16; i++) diff |= calcTag[i] ^ rxTag[i];
    if (diff !== 0) return null;

    let activeWin = win;
    if (activeWin === undefined) {
      activeWin = new ReplayWindow64();
      this.replayWindows.set(cid, activeWin);
    }
    if (!activeWin.accept(seq)) return null;
    return chacha20Xor(this.key, 1, nonce, ct);
  }
}

// ---------------------------------------------------------------------------
// NLTP v1 Frame Pack / Unpack
// ---------------------------------------------------------------------------

export function packNltpFrame(pktType, connId, payloadInput, { flags = 0, tsMs = null, aead = null } = {}) {
  const payload = toUint8Array(payloadInput);
  const verType = ((NLTP_VERSION & 0x0f) << 4) | (pktType & 0x0f);
  const tVal = (tsMs === null ? Math.floor(performance.now()) : tsMs) >>> 0;
  const cid32 = connId >>> 0;

  if (aead !== null) {
    const encFlags = (flags & 0x7f) | FLAG_ENCRYPTED;
    const seq64 = aead.nextSeq();
    const aadHdr = new Uint8Array(ENC_HEADER_SIZE);
    aadHdr[0] = NLTP_MAGIC_0;
    aadHdr[1] = NLTP_MAGIC_1;
    aadHdr[2] = verType;
    aadHdr[3] = encFlags;
    writeU32LE(aadHdr, 4, cid32);
    writeU64LE(aadHdr, 8, seq64);
    writeU32LE(aadHdr, 16, tVal);

    const ctAndTag = aead.seal(cid32, seq64, aadHdr, payload);
    const out = new Uint8Array(ENC_HEADER_SIZE + ctAndTag.length);
    out.set(aadHdr, 0);
    out.set(ctAndTag, ENC_HEADER_SIZE);
    return out;
  }

  const cleanFlags = flags & 0x7f;
  const out = new Uint8Array(HEADER_SIZE + payload.length);
  out[0] = NLTP_MAGIC_0;
  out[1] = NLTP_MAGIC_1;
  out[2] = verType;
  out[3] = cleanFlags;
  writeU32LE(out, 4, cid32);
  writeU32LE(out, 8, 0);
  writeU32LE(out, 12, tVal);
  if (payload.length > 0) out.set(payload, HEADER_SIZE);

  const crc = crc32Ieee(out, 0);
  writeU32LE(out, 8, crc);
  return out;
}

export function unpackNltpFrame(rawInput, aead = null) {
  const raw = toUint8Array(rawInput);
  if (raw.length < HEADER_SIZE || raw[0] !== NLTP_MAGIC_0 || raw[1] !== NLTP_MAGIC_1) {
    return null;
  }
  const verType = raw[2];
  const flags = raw[3];
  if ((verType >>> 4) !== NLTP_VERSION) {
    return null;
  }

  const isEnc = (flags & FLAG_ENCRYPTED) !== 0;
  if (aead !== null) {
    if (!isEnc || raw.length < ENC_HEADER_SIZE + AEAD_TAG_SIZE) {
      return null;
    }
    const connId = readU32LE(raw, 4);
    const seq64 = readU64LE(raw, 8);
    const tsMs = readU32LE(raw, 16);
    const pt = aead.openAndCheckReplay(connId, seq64, raw.subarray(0, ENC_HEADER_SIZE), raw.subarray(ENC_HEADER_SIZE));
    if (pt === null) return null;
    return {
      pktType: verType & 0x0f,
      flags: flags & 0x7f,
      connId,
      tsMs,
      payload: pt,
    };
  }

  if (isEnc) return null;
  const connId = readU32LE(raw, 4);
  const rxCrc = readU32LE(raw, 8);
  const tsMs = readU32LE(raw, 12);

  const hdrZero = new Uint8Array(HEADER_SIZE);
  hdrZero.set(raw.subarray(0, HEADER_SIZE), 0);
  writeU32LE(hdrZero, 8, 0);
  const payload = raw.subarray(HEADER_SIZE);
  let calcCrc = crc32Ieee(hdrZero, 0);
  if (payload.length > 0) calcCrc = crc32Ieee(payload, calcCrc);
  if (calcCrc !== rxCrc) return null;

  return {
    pktType: verType & 0x0f,
    flags,
    connId,
    tsMs,
    payload: new Uint8Array(payload),
  };
}

// ---------------------------------------------------------------------------
// Zero-Knowledge CGNAT Relay & RFC 5389 STUN Wire Framing
// ---------------------------------------------------------------------------

export function makeRoomTag(roomName) {
  const pref = utf8Encode('NLTP-RELAY-ROOM:');
  const body = utf8Encode(String(roomName).trim());
  const cat = new Uint8Array(pref.length + body.length);
  cat.set(pref, 0);
  cat.set(body, pref.length);
  return sha256Bytes(cat).slice(0, 16);
}

export function wrapRelayPacket(roomTag16, innerPktInput, cmd = RELAY_VER_DATA) {
  const inner = innerPktInput ? toUint8Array(innerPktInput) : new Uint8Array(0);
  const out = new Uint8Array(RELAY_HDR_SIZE + inner.length);
  out[0] = RELAY_MAGIC_0;
  out[1] = RELAY_MAGIC_1;
  out[2] = cmd & 0xff;
  out.set(roomTag16.subarray(0, 16), 3);
  if (inner.length > 0) out.set(inner, RELAY_HDR_SIZE);
  return out;
}

export function unwrapRelayPacket(rawInput) {
  const raw = toUint8Array(rawInput);
  if (raw.length < RELAY_HDR_SIZE || raw[0] !== RELAY_MAGIC_0 || raw[1] !== RELAY_MAGIC_1) {
    return null;
  }
  return {
    cmd: raw[2],
    roomTag: raw.slice(3, 19),
    inner: raw.subarray(19),
  };
}

export function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}

// ---------------------------------------------------------------------------
// 7. Pure-JS RFC 7748 X25519 Ephemeral Key Exchange (Zero Dependencies)
// ---------------------------------------------------------------------------

const P25519 = (1n << 255n) - 19n;
const A24_25519 = 121665n;
const X25519_BASEPOINT = (() => {
  const b = new Uint8Array(32);
  b[0] = 9;
  return b;
})();

function add25519(a, b) {
  const s = a + b;
  return s >= P25519 ? s - P25519 : s;
}

function sub25519(a, b) {
  const d = a - b;
  return d < 0n ? d + P25519 : d;
}

function mul25519(a, b) {
  return (a * b) % P25519;
}

function inv25519(z) {
  let res = 1n;
  let base = z % P25519;
  let exp = P25519 - 2n;
  while (exp > 0n) {
    if ((exp & 1n) === 1n) res = (res * base) % P25519;
    base = (base * base) % P25519;
    exp >>= 1n;
  }
  return res;
}

export function x25519ScalarMult(scalar32Input, u32Input) {
  const kBytes = toUint8Array(scalar32Input);
  const uBytes = toUint8Array(u32Input);
  if (kBytes.length !== 32 || uBytes.length !== 32) return null;

  const k = new Uint8Array(kBytes);
  k[0] &= 248;
  k[31] &= 127;
  k[31] |= 64;

  const uArr = new Uint8Array(uBytes);
  uArr[31] &= 127;

  let scalar = 0n;
  let u = 0n;
  for (let i = 31; i >= 0; i--) {
    scalar = (scalar << 8n) | BigInt(k[i]);
    u = (u << 8n) | BigInt(uArr[i]);
  }
  k.fill(0);
  u %= P25519;

  const x1 = u;
  let x2 = 1n;
  let z2 = 0n;
  let x3 = u;
  let z3 = 1n;
  let swap = 0n;

  for (let t = 254n; t >= 0n; t--) {
    const kt = (scalar >> t) & 1n;
    swap ^= kt;
    if (swap === 1n) {
      let tmp = x2; x2 = x3; x3 = tmp;
      tmp = z2; z2 = z3; z3 = tmp;
    }
    swap = kt;

    const A = add25519(x2, z2);
    const AA = mul25519(A, A);
    const B = sub25519(x2, z2);
    const BB = mul25519(B, B);
    const E = sub25519(AA, BB);
    const C = add25519(x3, z3);
    const D = sub25519(x3, z3);
    const DA = mul25519(D, A);
    const CB = mul25519(C, B);
    const sum = add25519(DA, CB);
    const diff = sub25519(DA, CB);
    x3 = mul25519(sum, sum);
    z3 = mul25519(x1, mul25519(diff, diff));
    x2 = mul25519(AA, BB);
    z2 = mul25519(E, add25519(AA, mul25519(A24_25519, E)));
  }

  if (swap === 1n) {
    let tmp = x2; x2 = x3; x3 = tmp;
    tmp = z2; z2 = z3; z3 = tmp;
  }

  let outVal = mul25519(x2, inv25519(z2));
  const out = new Uint8Array(32);
  let nonZero = 0;
  for (let i = 0; i < 32; i++) {
    const b = Number(outVal & 0xffn);
    out[i] = b;
    nonZero |= b;
    outVal >>= 8n;
  }
  return nonZero === 0 ? null : out;
}

export function x25519PublicFromPrivate(scalar32Input) {
  return x25519ScalarMult(scalar32Input, X25519_BASEPOINT);
}

export function buildStunBindingRequest(txId = null) {
  const tid = txId && txId.length === 12 ? new Uint8Array(txId) : randomBytes(12);
  const req = new Uint8Array(20);
  writeU16BE(req, 0, STUN_BINDING_REQUEST);
  writeU16BE(req, 2, 0);
  writeU32BE(req, 4, STUN_MAGIC_COOKIE);
  req.set(tid, 8);
  return { packet: req, txId: tid };
}

export function isStunPacket(rawInput) {
  const raw = toUint8Array(rawInput);
  if (raw.length < 20) return false;
  const mlen = readU16BE(raw, 2);
  const cookie = readU32BE(raw, 4);
  return cookie === STUN_MAGIC_COOKIE && raw.length >= 20 + mlen;
}

export function parseStunBindingResponse(rawInput, expectedTxId = null) {
  const raw = toUint8Array(rawInput);
  if (raw.length < 20) return null;
  const mtype = readU16BE(raw, 0);
  const mlen = readU16BE(raw, 2);
  const cookie = readU32BE(raw, 4);
  if (mtype !== STUN_BINDING_SUCCESS || cookie !== STUN_MAGIC_COOKIE) return null;
  const rxTid = raw.subarray(8, 20);
  if (expectedTxId !== null && !bytesEqual(rxTid, expectedTxId)) return null;

  let pos = 20;
  const end = Math.min(raw.length, 20 + mlen);
  let fallbackMapped = null;

  while (pos + 4 <= end) {
    const attrType = readU16BE(raw, pos);
    const attrLen = readU16BE(raw, pos + 2);
    pos += 4;
    if (pos + attrLen > end) break;
    const val = raw.subarray(pos, pos + attrLen);
    pos += (attrLen + 3) & ~3;

    if (attrType === STUN_ATTR_XOR_MAPPED_ADDRESS && val.length >= 8) {
      const family = val[1];
      const port = readU16BE(val, 2) ^ (STUN_MAGIC_COOKIE >>> 16);
      if (family === 0x01) {
        const xip = (readU32BE(val, 4) ^ STUN_MAGIC_COOKIE) >>> 0;
        const ip = `${(xip >>> 24) & 0xff}.${(xip >>> 16) & 0xff}.${(xip >>> 8) & 0xff}.${xip & 0xff}`;
        return { ip, port };
      }
      if (family === 0x02 && val.length >= 20) {
        const mask = new Uint8Array(16);
        writeU32BE(mask, 0, STUN_MAGIC_COOKIE);
        mask.set(rxTid, 4);
        const groups = [];
        for (let i = 0; i < 16; i += 2) {
          const hi = val[4 + i] ^ mask[i];
          const lo = val[4 + i + 1] ^ mask[i + 1];
          groups.push(((hi << 8) | lo).toString(16));
        }
        return { ip: groups.join(':'), port };
      }
    } else if (attrType === STUN_ATTR_MAPPED_ADDRESS && val.length >= 8 && val[1] === 0x01) {
      const port = readU16BE(val, 2);
      const ip = `${val[4]}.${val[5]}.${val[6]}.${val[7]}`;
      fallbackMapped = { ip, port };
    }
  }
  return fallbackMapped;
}
