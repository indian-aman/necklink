/**
 * Encrypted NLTP sessions (wire version 2, NLTP 1.4). Same protocol as necklink/session.py:
 *
 *   initiator -> responder  HELLO      { n_i (16 B) }              sealed with the long-term key K
 *   responder -> initiator  HELLO_ACK  { n_i (16 B) || n_r (16 B) } sealed with K
 *   prk   = HMAC-SHA256(K,   "NLTP-1.4 session" || n_i || n_r || LE32(cid_i) || LE32(cid_r))
 *   k_i2r = HMAC-SHA256(prk, "i2r"),   k_r2i = HMAC-SHA256(prk, "r2i")
 *
 * Data frames keep the 20-byte header (version nibble 2) and are sealed with the session key for their
 * direction. Recorded traffic fails after any restart (the responder adds a fresh n_r to every session),
 * and per-session keys make counter restarts and conn_id collisions harmless.
 */
import {
  AEAD_TAG_SIZE,
  ENC_HEADER_SIZE,
  FLAG_ENCRYPTED,
  NLTP_MAGIC_0,
  NLTP_MAGIC_1,
  ReplayWindow64,
  chacha20Xor,
  deriveAeadKey,
  poly1305AeadTag,
  randomBytes,
  readU32LE,
  readU64LE,
  sha256Bytes,
  utf8Encode,
  writeU32LE,
  writeU64LE,
} from './wire.js';

export const SESSION_VERSION = 2;
export const PT_HELLO = 0x0b;
export const PT_HELLO_ACK = 0x0c;
export const PT_NO_SESSION = 0x0d;
export const NONCE_BYTES = 16;

const HELLO_FIRST_RETX_S = 0.06;
const HELLO_MAX_RETX_S = 0.20;
const HELLO_GIVE_UP_S = 10.0;
const TAKEOVER_SILENCE_S = 1.0;
const REKEY_SILENCE_S = 3.0;
const HINT_INTERVAL_S = 0.25;
const MAX_SESSIONS_PER_CID = 2;
const MAX_CIDS = 4096;
const MAX_QUEUED = 64;
const KDF_LABEL = utf8Encode('NLTP-1.4 session');
const LABEL_I2R = utf8Encode('i2r');
const LABEL_R2I = utf8Encode('r2i');

export const HANDLED = Symbol('nltp-session-handled');

function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

export function hmacSha256(key, msg) {
  let k = key.length > 64 ? sha256Bytes(key) : key;
  const kp = new Uint8Array(64);
  kp.set(k, 0);
  const ipad = new Uint8Array(64);
  const opad = new Uint8Array(64);
  for (let i = 0; i < 64; i++) {
    ipad[i] = kp[i] ^ 0x36;
    opad[i] = kp[i] ^ 0x5c;
  }
  return sha256Bytes(concat(opad, sha256Bytes(concat(ipad, msg))));
}

export function deriveSessionKeys(kLong, nI, nR, cidI, cidR) {
  const cids = new Uint8Array(8);
  writeU32LE(cids, 0, cidI >>> 0);
  writeU32LE(cids, 4, cidR >>> 0);
  const prk = hmacSha256(kLong, concat(KDF_LABEL, nI, nR, cids));
  return [hmacSha256(prk, LABEL_I2R), hmacSha256(prk, LABEL_R2I)];
}

function bytesLess(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

function bytesEq(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}

function randomSeq() {
  const b = randomBytes(8);
  b[0] |= 1;
  return readU64LE(b, 0);
}

function nonceFor(cid, seq) {
  const n = new Uint8Array(12);
  writeU32LE(n, 0, cid >>> 0);
  writeU64LE(n, 4, BigInt(seq));
  return n;
}

function aeadSeal(key, nonce, aad, pt) {
  const ct = chacha20Xor(key, 1, nonce, pt);
  return concat(ct, poly1305AeadTag(key, nonce, aad, ct));
}

function aeadOpen(key, nonce, aad, body) {
  if (body.length < AEAD_TAG_SIZE) return null;
  const ctLen = body.length - AEAD_TAG_SIZE;
  const ct = body.subarray(0, ctLen);
  const calc = poly1305AeadTag(key, nonce, aad, ct);
  let d = 0;
  for (let i = 0; i < 16; i++) d |= calc[i] ^ body[ctLen + i];
  if (d !== 0) return null;
  return chacha20Xor(key, 1, nonce, ct);
}

class Session {
  constructor(role, peer, peerKey, nI, now) {
    this.role = role;
    this.peer = peer;
    this.peerKey = peerKey;
    this.peerCid = null;
    this.nI = nI;
    this.nR = null;
    this.kTx = null;
    this.kRx = null;
    this.txSeq = 0n;
    this.replay = new ReplayWindow64();
    this.established = false;
    this.estS = now;
    this.lastRxS = now;
    this.helloNextS = now;
    this.helloInterval = HELLO_FIRST_RETX_S;
    this.deadlineS = now + HELLO_GIVE_UP_S;
    this.ackFrame = null;
    this.queued = [];
  }

  setKeys([kI2R, kR2I], now) {
    if (this.role === 'i') {
      this.kTx = kI2R;
      this.kRx = kR2I;
    } else {
      this.kTx = kR2I;
      this.kRx = kI2R;
    }
    this.established = true;
    this.estS = now;
    this.lastRxS = now;
  }
}

/** Per-peer session state for one endpoint (sending is by peer address, receiving by conn_id). */
export class SessionTable {
  constructor(psk, myCid, sendRaw, peerKeyFn) {
    this.kLong = deriveAeadKey(psk);
    this.myCid = myCid >>> 0;
    this._sendRaw = sendRaw;
    this._peerKey = peerKeyFn;
    this.byAddr = new Map();   // peerKey -> Session used for sending
    this.byCid = new Map();    // conn_id -> [Session] accepted for receiving (slot 0 = live)
    this.pending = new Map();  // peerKey -> Session awaiting HELLO_ACK
    this.hellosTx = 0;
    this.acksTx = 0;
    this.established = 0;
    this.legacyDrops = 0;
    this.hintsTx = 0;
    this.rekeysFromHint = 0;
    this._hintTx = new Map();
    this._hintRx = new Map();
  }

  _header(pktType, flags, seq, tsMs) {
    const h = new Uint8Array(ENC_HEADER_SIZE);
    h[0] = NLTP_MAGIC_0;
    h[1] = NLTP_MAGIC_1;
    h[2] = ((SESSION_VERSION & 0x0f) << 4) | (pktType & 0x0f);
    h[3] = (flags & 0x7f) | FLAG_ENCRYPTED;
    writeU32LE(h, 4, this.myCid);
    writeU64LE(h, 8, BigInt(seq));
    writeU32LE(h, 16, tsMs >>> 0);
    return h;
  }

  _seal(key, pktType, flags, seq, tsMs, payload) {
    const hdr = this._header(pktType, flags, seq, tsMs);
    return concat(hdr, aeadSeal(key, nonceFor(this.myCid, seq), hdr, payload));
  }

  _sendHello(s, tsMs) {
    this._sendRaw(this._seal(this.kLong, PT_HELLO, 0, randomSeq(), tsMs, s.nI), s.peer);
    this.hellosTx += 1;
  }

  _startHandshake(peer, key, now) {
    const s = new Session('i', peer, key, randomBytes(NONCE_BYTES), now);
    this.pending.set(key, s);
    this._sendHello(s, Math.floor(now * 1000));
    s.helloNextS = now + s.helloInterval;
    return s;
  }

  /** Frames waiting for a handshake to finish (close() lingers briefly so they still go out). */
  queuedCount() {
    let n = 0;
    for (const p of this.pending.values()) n += p.queued.length;
    return n;
  }

  ready(peer, now) {
    const key = this._peerKey(peer);
    if (this.byAddr.has(key)) return true;
    if (!this.pending.has(key)) this._startHandshake(peer, key, now);
    return false;
  }

  seal(peer, pktType, payload, flags, tsMs, now) {
    const key = this._peerKey(peer);
    const s = this.byAddr.get(key);
    if (s === undefined) {
      this.ready(peer, now);
      const p = this.pending.get(key);
      if (p !== undefined && p.queued.length < MAX_QUEUED) p.queued.push([pktType, payload, flags, tsMs]);
      return null;
    }
    s.txSeq += 1n;
    return this._seal(s.kTx, pktType, flags, s.txSeq, tsMs, payload);
  }

  service(now, busyPeers) {
    for (const [key, s] of this.pending) {
      if (now >= s.deadlineS) {
        this.pending.delete(key);
      } else if (now >= s.helloNextS) {
        this._sendHello(s, Math.floor(now * 1000));
        s.helloInterval = Math.min(HELLO_MAX_RETX_S, s.helloInterval * 1.5);
        s.helloNextS = now + s.helloInterval;
      }
    }
    for (const peer of busyPeers) {
      const key = this._peerKey(peer);
      const s = this.byAddr.get(key);
      if (s !== undefined && !this.pending.has(key) && now - s.lastRxS > REKEY_SILENCE_S && now - s.estS > REKEY_SILENCE_S) {
        this._startHandshake(peer, key, now);
      }
    }
  }

  _remember(cid, s, active) {
    let lst = this.byCid.get(cid);
    if (lst === undefined) {
      if (this.byCid.size >= MAX_CIDS) this.byCid.delete(this.byCid.keys().next().value);
      lst = [];
      this.byCid.set(cid, lst);
    }
    const idx = lst.indexOf(s);
    if (idx >= 0) lst.splice(idx, 1);
    lst.splice(active || lst.length === 0 ? 0 : 1, 0, s);
    lst.length = Math.min(lst.length, MAX_SESSIONS_PER_CID);
  }

  /** Returns {pktType, flags, connId, tsMs, payload}, HANDLED for handshake frames, or null. */
  open(raw, peer, now) {
    if (raw.length < ENC_HEADER_SIZE + AEAD_TAG_SIZE || raw[0] !== NLTP_MAGIC_0 || raw[1] !== NLTP_MAGIC_1) return null;
    const verType = raw[2];
    const flags = raw[3];
    if ((flags & FLAG_ENCRYPTED) === 0) return null;
    if ((verType >>> 4) !== SESSION_VERSION) {
      this.legacyDrops += 1;
      return null;
    }
    const pktType = verType & 0x0f;
    const cid = readU32LE(raw, 4);
    const seq = readU64LE(raw, 8);
    const tsMs = readU32LE(raw, 16);
    const hdr = raw.subarray(0, ENC_HEADER_SIZE);
    const body = raw.subarray(ENC_HEADER_SIZE);
    const key = this._peerKey(peer);

    if (pktType === PT_NO_SESSION) {
      this._onNoSession(body, peer, key, now);
      return HANDLED;
    }
    const nonce = nonceFor(cid, seq);
    if (pktType === PT_HELLO || pktType === PT_HELLO_ACK) {
      const pt = aeadOpen(this.kLong, nonce, hdr, body);
      if (pt === null) return null;
      if (pktType === PT_HELLO && pt.length >= NONCE_BYTES) {
        this._onHello(cid, pt.slice(0, NONCE_BYTES), peer, key, now, tsMs);
        return HANDLED;
      }
      if (pktType === PT_HELLO_ACK && pt.length >= 2 * NONCE_BYTES) {
        this._onAck(cid, pt.slice(0, NONCE_BYTES), pt.slice(NONCE_BYTES, 2 * NONCE_BYTES), peer, key, now);
        return HANDLED;
      }
      return null;
    }

    const lst = this.byCid.get(cid);
    if (lst !== undefined) {
      for (const s of [...lst]) {
        if (!s.replay.wouldAccept(seq)) continue;
        const pt = aeadOpen(s.kRx, nonce, hdr, body);
        if (pt === null) continue;
        s.replay.accept(seq);
        s.lastRxS = now;
        s.peer = peer;
        s.peerKey = key;
        this._remember(cid, s, true);
        if (this.byAddr.get(key) !== s) this.byAddr.set(key, s);
        return { pktType, flags: flags & 0x7f, connId: cid, tsMs, payload: pt };
      }
    } else {
      this._sendNoSession(cid, seq, peer, key, now, tsMs);
    }
    return null;
  }

  _flush(s, queued, peer) {
    for (const [pktType, payload, flags, tsMs] of queued) {
      s.txSeq += 1n;
      this._sendRaw(this._seal(s.kTx, pktType, flags, s.txSeq, tsMs, payload), peer);
    }
  }

  _onHello(cid, nI, peer, key, now, tsMs) {
    for (const s of this.byCid.get(cid) || []) {
      if (s.role === 'r' && bytesEq(s.nI, nI) && s.ackFrame !== null) {
        this._sendRaw(s.ackFrame, peer);
        this.acksTx += 1;
        return;
      }
    }
    const mine = this.pending.get(key);
    if (mine !== undefined) {
      if (bytesLess(mine.nI, nI)) return; // simultaneous open: our HELLO wins
      this.pending.delete(key);
    }
    const s = new Session('r', peer, key, nI, now);
    s.peerCid = cid;
    s.nR = randomBytes(NONCE_BYTES);
    s.setKeys(deriveSessionKeys(this.kLong, nI, s.nR, cid, this.myCid), now);
    this._remember(cid, s, false);
    const cur = this.byAddr.get(key);
    if (cur === undefined || now - cur.lastRxS >= TAKEOVER_SILENCE_S) this.byAddr.set(key, s);
    s.ackFrame = this._seal(this.kLong, PT_HELLO_ACK, 0, randomSeq(), tsMs, concat(nI, s.nR));
    this._sendRaw(s.ackFrame, peer);
    this.acksTx += 1;
    this.established += 1;
    if (mine !== undefined && mine.queued.length && this.byAddr.get(key) === s) this._flush(s, mine.queued, peer);
  }

  _onAck(cid, nI, nR, peer, key, now) {
    let s = this.pending.get(key);
    if (s === undefined || !bytesEq(s.nI, nI)) {
      s = undefined;
      for (const p of this.pending.values()) {
        if (bytesEq(p.nI, nI)) s = p;
      }
      if (s === undefined) return;
    }
    for (const [k, v] of this.pending) {
      if (v === s) this.pending.delete(k);
    }
    s.peerCid = cid;
    s.nR = nR;
    s.peer = peer;
    s.peerKey = key;
    s.setKeys(deriveSessionKeys(this.kLong, nI, nR, this.myCid, cid), now);
    this._remember(cid, s, true);
    this.byAddr.set(key, s);
    this.established += 1;
    const queued = s.queued;
    s.queued = [];
    this._flush(s, queued, peer);
  }

  _sendNoSession(cid, seq, peer, key, now, tsMs) {
    if (now - (this._hintTx.get(key) ?? -1e9) < HINT_INTERVAL_S) return;
    if (this._hintTx.size > MAX_CIDS) this._hintTx.clear();
    this._hintTx.set(key, now);
    const body = new Uint8Array(16);
    writeU32LE(body, 0, cid >>> 0);
    writeU64LE(body, 4, BigInt(seq));
    this._sendRaw(concat(this._header(PT_NO_SESSION, 0, 0n, tsMs), body), peer);
    this.hintsTx += 1;
  }

  _onNoSession(body, peer, key, now) {
    if (body.length < 12) return;
    const echoCid = readU32LE(body, 0);
    const echoSeq = readU64LE(body, 4);
    const s = this.byAddr.get(key);
    if (s === undefined || echoCid !== this.myCid || !(echoSeq > 0n && echoSeq <= s.txSeq) || this.pending.has(key)) return;
    if (now - (this._hintRx.get(key) ?? -1e9) < HINT_INTERVAL_S) return;
    this._hintRx.set(key, now);
    this.rekeysFromHint += 1;
    this._startHandshake(peer, key, now);
  }
}
