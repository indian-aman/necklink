/**
 * Transport-Agnostic NLTP v1.2 Endpoint Engine (`NLTPEndpoint`),
 * Router-Safe `TokenBucketPacer`, and 1-RTT BBR-Style `DeliveryRateController`.
 *
 * Runs identically in Node.js (over `node:dgram`), Browsers (WebRTC / WebTransport),
 * Deno, Bun, or custom serial/radio byte links with zero Node-specific imports.
 */

import {
  AEADCipherContext,
  DEFAULT_GEN_K,
  DEFAULT_SHARD_BYTES,
  ENC_HEADER_SIZE,
  AEAD_TAG_SIZE,
  HEADER_SIZE,
  PT_CRYPTIC_PARITY,
  PT_DATA_SHARD,
  PT_NEURAL_STATE,
  PT_PING,
  PT_PONG,
  PT_PROPRIOCEPTIVE_ACK,
  PT_REFLEX_MSG,
  PT_STREAM_FIN_ACK,
  PT_STREAM_META,
  SHARD_HDR_SIZE,
  cauchyRow,
  gf256Axpy,
  packNltpFrame,
  randomBytes,
  readU16LE,
  readU32LE,
  sha256Hex,
  solveLinearSystemGF256,
  toUint8Array,
  unpackNltpFrame,
  utf8Decode,
  utf8Encode,
  writeU16LE,
  writeU32LE,
} from './wire.js';

export function normalizePeerKey(peer) {
  if (typeof peer === 'string') return peer;
  if (typeof peer === 'number') return String(peer);
  if (Array.isArray(peer) && peer.length >= 2) {
    const h = String(peer[0]);
    return h.includes(':') ? `[${h}]:${peer[1]}` : `${h}:${peer[1]}`;
  }
  if (peer && typeof peer === 'object') {
    const h = String(peer.host ?? peer.address ?? '127.0.0.1');
    const p = Number(peer.port ?? 0);
    return h.includes(':') ? `[${h}]:${p}` : `${h}:${p}`;
  }
  return String(peer);
}

function basenameSafe(filepath) {
  if (!filepath) return '';
  const s = String(filepath).replace(/\\/g, '/');
  const idx = s.lastIndexOf('/');
  return idx >= 0 ? s.slice(idx + 1) : s;
}

// ---------------------------------------------------------------------------
// Router-Safe Token-Bucket UDP Packet Pacer
// ---------------------------------------------------------------------------

export class TokenBucketPacer {
  constructor(rateKbps = 25000.0, maxBurstBytes = null) {
    this.rateBytesPerS = Math.max(1000.0, (rateKbps * 1000.0) / 8.0);
    this.maxBurstBytes = maxBurstBytes === null ? this._autoBurst() : Math.max(2048.0, Number(maxBurstBytes));
    this.tokens = this.maxBurstBytes;
    this.lastRefill = null;
    this.bonusBytes = 0.0;
  }

  _autoBurst() {
    return Math.max(4 * 1024, Math.min(16 * 1024, Math.trunc(this.rateBytesPerS * 0.006)));
  }

  grantBurst(nBytes) {
    this.bonusBytes = Math.max(this.bonusBytes, Number(nBytes));
    this.tokens = 0.0;
  }

  setRateKbps(rateKbps, rescaleBurst = false) {
    this.rateBytesPerS = Math.max(1000.0, (rateKbps * 1000.0) / 8.0);
    if (rescaleBurst) {
      this.maxBurstBytes = this._autoBurst();
      this.tokens = Math.min(this.tokens, this.maxBurstBytes);
    }
  }

  consumeIfAvailable(nBytes, nowS) {
    if (this.bonusBytes >= nBytes) {
      this.bonusBytes -= nBytes;
      return true;
    }
    if (this.lastRefill === null) {
      this.lastRefill = nowS;
    } else {
      const dt = Math.max(0.0, nowS - this.lastRefill);
      this.lastRefill = nowS;
      this.tokens = Math.min(this.maxBurstBytes, this.tokens + dt * this.rateBytesPerS);
    }
    if (this.tokens >= nBytes) {
      this.tokens -= nBytes;
      return true;
    }
    return false;
  }
}

// ---------------------------------------------------------------------------
// 1-RTT Adaptive Delivery-Rate & Packet-Train Controller
// ---------------------------------------------------------------------------

export class DeliveryRateController {
  static STARTUP = 0;
  static CRUISE = 1;
  static INIT_KBPS = 2000.0;
  static MIN_KBPS = 64.0;
  static INIT_BURST_BYTES = 32 * 1024;

  constructor(capKbps) {
    this.capKbps = Number(capKbps);
    this.rateKbps = Math.min(DeliveryRateController.INIT_KBPS, this.capKbps);
    this.mode = DeliveryRateController.STARTUP;
    this.minRttMs = Infinity;
    this.roundMinRttMs = Infinity;
    this.nRttSamples = 0;
    this.startupExitPending = false;
    this.trackedStream = null;
    this.roundT0 = null;
    this.roundRx0 = 0;
    this.sentBytes = 0;
    this.roundSent0 = 0;
    this.prevRoundSent = 0;
    this.prevQueueMs = 0.0;
    this.bestDelivKbps = 0.0;
    this.plateauRounds = 0;
    this.randomLoss = 0.0;
    this.randomLossSamples = 0;
    this.lastDelivKbps = 0.0;
    this.lastQueueMs = 0.0;
    this.btlbwRounds = [];
    this.roundArrivalMax = 0.0;
    this.trainProbeDone = false;
    this.srttMs = 0.0;
    this.rttvarMs = 0.0;
    this.sentHistory = [];
    this.roundsDone = 0;
  }

  btlbwKbps() {
    let m = this.roundArrivalMax;
    for (const v of this.btlbwRounds) {
      if (v > m) m = v;
    }
    return m;
  }

  startStream(streamId, nowS) {
    this.trackedStream = streamId;
    this.trainProbeDone = this.mode === DeliveryRateController.CRUISE && this.btlbwRounds.length > 0;
    this.roundsDone = 0;
    this.roundT0 = nowS;
    this.roundRx0 = 0;
    this.roundSent0 = this.sentBytes;
    this.prevRoundSent = 0;
    this.roundMinRttMs = Infinity;
  }

  onSent(nBytes) {
    this.sentBytes += nBytes;
  }

  onRttSample(rttMs) {
    this.minRttMs = Math.min(this.minRttMs, rttMs);
    this.roundMinRttMs = Math.min(this.roundMinRttMs, rttMs);
    this.nRttSamples += 1;
    if (this.nRttSamples === 1) {
      this.srttMs = rttMs;
      this.rttvarMs = rttMs / 4.0;
    } else {
      this.rttvarMs = 0.75 * this.rttvarMs + 0.25 * Math.abs(rttMs - this.srttMs);
      this.srttMs = 0.875 * this.srttMs + 0.125 * rttMs;
    }
    if (this.mode === DeliveryRateController.STARTUP && this.nRttSamples >= 4) {
      if (rttMs > this.minRttMs + Math.max(4.0, this.minRttMs / 8.0, 2.0 * this.rttvarMs)) {
        this.startupExitPending = true;
      }
    }
  }

  _queueTargetMs() {
    if (!Number.isFinite(this.minRttMs)) return 4.0;
    return Math.max(4.0, 0.05 * this.minRttMs, 1.5 * this.rttvarMs);
  }

  _sentAt(t) {
    for (let i = this.sentHistory.length - 1; i >= 0; i--) {
      if (this.sentHistory[i][0] <= t) return this.sentHistory[i][1];
    }
    return this.sentHistory.length > 0 ? this.sentHistory[0][1] : 0;
  }

  initialBurst(coldBytes) {
    return this.mode === DeliveryRateController.STARTUP ? coldBytes : 0;
  }

  lossForFec(fallback) {
    return this.randomLossSamples >= 2 ? this.randomLoss : fallback;
  }

  _capacityKbps(delivKbps) {
    return delivKbps / Math.max(0.3, 1.0 - this.randomLoss);
  }

  onAck(nowS, streamId, rxBytes, srttMs, arrivalKbps = 0.0) {
    if (this.trackedStream !== streamId || this.roundT0 === null) return false;
    this.sentHistory.push([nowS, this.sentBytes]);
    if (this.sentHistory.length > 512) {
      this.sentHistory.splice(0, 256);
    }

    if (arrivalKbps > 0.0) {
      this.roundArrivalMax = Math.max(this.roundArrivalMax, arrivalKbps);
      if (this.mode === DeliveryRateController.STARTUP && !this.trainProbeDone) {
        this.trainProbeDone = true;
        this.mode = DeliveryRateController.CRUISE;
        const old = this.rateKbps;
        this.rateKbps = Math.max(
          DeliveryRateController.MIN_KBPS,
          Math.min(this.capKbps, Math.max(this.rateKbps, 1.05 * arrivalKbps))
        );
        return this.rateKbps !== old;
      }
    }

    const baseRtt = Number.isFinite(this.minRttMs) ? this.minRttMs : srttMs;
    const dt = nowS - this.roundT0;
    const earlyExit = this.mode === DeliveryRateController.STARTUP && this.startupExitPending && dt >= 0.005;
    if (dt < Math.max(0.02, baseRtt / 1000.0) && !earlyExit) return false;

    const delivered = (rxBytes - this.roundRx0) >>> 0;
    const delivKbps = (delivered * 8.0) / 1000.0 / dt;
    const roundSent = this.sentBytes - this.roundSent0;
    const queueMs = Number.isFinite(this.roundMinRttMs) ? this.roundMinRttMs - this.minRttMs : 0.0;
    const targetMs = this._queueTargetMs();
    const old = this.rateKbps;

    const lagS = (this.srttMs || baseRtt) / 1000.0;
    const sentWindow = this._sentAt(nowS - lagS) - this._sentAt(this.roundT0 - lagS);
    this.roundsDone += 1;
    if (this.roundsDone > 2 && sentWindow > 8 * 1024 && queueMs < targetMs && this.prevQueueMs < targetMs) {
      const sample = Math.max(0.0, Math.min(0.9, 1.0 - delivered / sentWindow));
      this.randomLoss = this.randomLossSamples === 0 ? sample : 0.8 * this.randomLoss + 0.2 * sample;
      this.randomLossSamples += 1;
    }

    if (this.mode === DeliveryRateController.STARTUP) {
      if (delivKbps >= 1.25 * this.bestDelivKbps) {
        this.bestDelivKbps = delivKbps;
        this.plateauRounds = 0;
      } else {
        this.plateauRounds += 1;
      }
      if (earlyExit || queueMs > targetMs) {
        this.mode = DeliveryRateController.CRUISE;
        const cap = this._capacityKbps(Math.max(delivKbps, this.bestDelivKbps));
        this.rateKbps = cap > 0 ? Math.max(0.95 * cap, 0.5 * this.rateKbps) : 0.5 * this.rateKbps;
      } else if (this.plateauRounds >= 3) {
        this.mode = DeliveryRateController.CRUISE;
      } else if (delivKbps >= 0.5 * this.rateKbps) {
        this.rateKbps = Math.max(2.0 * this.rateKbps, 2.0 * delivKbps);
      }
    } else {
      const cap = Math.max(this._capacityKbps(delivKbps), this.btlbwKbps());
      if (queueMs > 2.0 * targetMs) {
        this.rateKbps = cap > 0 ? Math.min(this.rateKbps * 0.9, 0.95 * cap) : this.rateKbps * 0.85;
      } else if (queueMs < targetMs) {
        const ceiling = cap > 0 ? 1.25 * cap : this.rateKbps * 1.1;
        this.rateKbps = Math.min(this.rateKbps * 1.1, Math.max(this.rateKbps, ceiling));
      }
    }

    this.rateKbps = Math.max(DeliveryRateController.MIN_KBPS, Math.min(this.capKbps, this.rateKbps));
    this.lastDelivKbps = delivKbps;
    this.lastQueueMs = queueMs;
    this.prevRoundSent = roundSent;
    this.prevQueueMs = queueMs;
    this.roundT0 = nowS;
    this.roundRx0 = rxBytes;
    this.roundSent0 = this.sentBytes;
    this.roundMinRttMs = Infinity;
    this.startupExitPending = false;
    if (this.roundArrivalMax > 0.0) {
      this.btlbwRounds.push(this.roundArrivalMax);
      if (this.btlbwRounds.length > 6) this.btlbwRounds.shift();
    }
    this.roundArrivalMax = 0.0;
    return this.rateKbps !== old;
  }
}

// ---------------------------------------------------------------------------
// Sender & Receiver GF(2^8) Generation State Machines
// ---------------------------------------------------------------------------

class TxGeneration {
  constructor(streamId, genId, rawSlice, shardBytes = DEFAULT_SHARD_BYTES) {
    this.streamId = streamId;
    this.genId = genId;
    const chunks = [];
    for (let i = 0; i < rawSlice.length; i += shardBytes) {
      chunks.push(rawSlice.subarray(i, Math.min(rawSlice.length, i + shardBytes)));
    }
    if (chunks.length === 0) {
      chunks.push(new Uint8Array(0));
    }
    this.kShards = chunks.length;
    let maxChunkLen = 0;
    for (const c of chunks) {
      if (c.length > maxChunkLen) maxChunkLen = c.length;
    }
    this.maxChunkLen = maxChunkLen;
    this.rowWidth = 2 + maxChunkLen;
    this.dataMat = new Array(this.kShards);
    for (let idx = 0; idx < this.kShards; idx++) {
      const row = new Uint8Array(this.rowWidth);
      const ch = chunks[idx];
      writeU16LE(row, 0, ch.length);
      if (ch.length > 0) row.set(ch, 2);
      this.dataMat[idx] = row;
    }

    this.nextSysIdx = 0;
    this.proactiveDone = false;
    this.proactiveTarget = -1;
    this.proactiveSent = 0;
    this.nextParityOffset = 0;
    this.ackedComplete = false;
    this.lastTxTime = 0.0;
    this.repairInFlight = 0;
    this.lastRepairTime = 0.0;
  }

  _packShardHeader(shardIdx, rowBytes) {
    const out = new Uint8Array(SHARD_HDR_SIZE + rowBytes.length);
    writeU32LE(out, 0, this.streamId >>> 0);
    writeU32LE(out, 4, this.genId >>> 0);
    out[8] = this.kShards & 0xff;
    out[9] = shardIdx & 0xff;
    writeU16LE(out, 10, this.rowWidth);
    out.set(rowBytes, SHARD_HDR_SIZE);
    return out;
  }

  makeSystematicPayload(sysIdx) {
    return this._packShardHeader(sysIdx, this.dataMat[sysIdx]);
  }

  makeCrypticParityPayload() {
    const pOff = this.nextParityOffset;
    this.nextParityOffset = (this.nextParityOffset + 1) % 192;
    const shardIdx = this.kShards + pOff;
    if (this.kShards === 1) {
      return this._packShardHeader(shardIdx, this.dataMat[0]);
    }
    const coeff = cauchyRow(pOff, this.kShards);
    const coded = new Uint8Array(this.rowWidth);
    for (let c = 0; c < this.kShards; c++) {
      gf256Axpy(coded, this.dataMat[c], coeff[c]);
    }
    return this._packShardHeader(shardIdx, coded);
  }
}

class RxGeneration {
  constructor(kShards, rowWidth) {
    this.kShards = kShards;
    this.rowWidth = rowWidth;
    this.seenIndices = new Set();
    this.rowsCoeff = [];
    this.rowsData = [];
    this.solvedBytes = null;
  }

  get neededDof() {
    if (this.solvedBytes !== null) return 0;
    return Math.max(0, this.kShards - this.rowsCoeff.length);
  }

  addShard(shardIdx, shardBuf) {
    if (this.solvedBytes !== null || shardBuf.length !== this.rowWidth) return false;
    if (this.seenIndices.has(shardIdx)) return false;
    this.seenIndices.add(shardIdx);

    let coeff;
    if (shardIdx < this.kShards) {
      coeff = new Uint8Array(this.kShards);
      coeff[shardIdx] = 1;
    } else {
      coeff = cauchyRow(shardIdx - this.kShards, this.kShards);
    }
    this.rowsCoeff.push(coeff);
    this.rowsData.push(new Uint8Array(shardBuf));

    if (this.rowsCoeff.length >= this.kShards) {
      return this._trySolve();
    }
    return false;
  }

  _trySolve() {
    const k = this.kShards;
    if (k === 1) {
      const rawRow = this.rowsData[0];
      const actualLen = readU16LE(rawRow, 0);
      if (actualLen > this.rowWidth - 2) return false;
      this.solvedBytes = rawRow.slice(2, 2 + actualLen);
      this.rowsCoeff.length = 0;
      this.rowsData.length = 0;
      return true;
    }

    const solvedRows = solveLinearSystemGF256(this.rowsCoeff.slice(0, k), this.rowsData.slice(0, k), k, this.rowWidth);
    if (solvedRows === null) {
      this.rowsCoeff.shift();
      this.rowsData.shift();
      return false;
    }

    let totalLen = 0;
    const slices = new Array(k);
    for (let i = 0; i < k; i++) {
      const actualLen = readU16LE(solvedRows[i], 0);
      if (actualLen > this.rowWidth - 2) return false;
      slices[i] = solvedRows[i].subarray(2, 2 + actualLen);
      totalLen += actualLen;
    }
    const out = new Uint8Array(totalLen);
    let pos = 0;
    for (let i = 0; i < k; i++) {
      out.set(slices[i], pos);
      pos += slices[i].length;
    }
    this.solvedBytes = out;
    this.rowsCoeff.length = 0;
    this.rowsData.length = 0;
    return true;
  }
}

export class ReceivedItem {
  constructor({ connId, peerAddr, streamId, kind, data, filename = '', savedPath = null, sha256Hex = '', sha256Verified = true }) {
    this.connId = connId;
    this.conn_id = connId;
    this.peerAddr = peerAddr;
    this.peer_addr = peerAddr;
    this.peer = peerAddr;
    this.streamId = streamId;
    this.stream_id = streamId;
    this.kind = kind;
    this.data = data;
    this.filename = filename;
    this.savedPath = savedPath;
    this.saved_path = savedPath;
    this.sha256Hex = sha256Hex;
    this.sha256_hex = sha256Hex;
    this.sha256Verified = sha256Verified;
    this.sha256_verified = sha256Verified;
  }

  asText() {
    return utf8Decode(this.data);
  }

  asJson() {
    return JSON.parse(this.asText());
  }
}

class InboundStreamState {
  constructor(streamId) {
    this.streamId = streamId;
    this.kind = null;
    this.filename = '';
    this.totalBytes = 0;
    this.totalGens = 0;
    this.expectedSha256 = '';
    this.metaReceived = false;
    this.gens = new Map();
    this.solvedGens = new Map();
    this.baseGen = 0;
    this.completed = false;
    this.sha256Ok = false;
    this.shardsRcvdCount = 0;
    this.bytesRcvd = 0;
    this.arrivalKbps = 0.0;
    this.arrT0 = null;
    this.arrB0 = 0;
    this.arrN0 = 0;
    this.sysShardsRcvd = 0;
    this.genKMap = new Map();
    this.genMaxSysSeen = new Map();
    this.maxGenSeen = -1;
  }

  ingestMeta(meta) {
    this.kind = String(meta.kind);
    this.filename = String(meta.filename ?? '');
    this.totalBytes = Number(meta.total_bytes);
    this.totalGens = Number(meta.total_gens);
    this.expectedSha256 = String(meta.sha256);
    this.metaReceived = true;
  }

  advanceBase() {
    while (this.solvedGens.has(this.baseGen)) {
      this.baseGen += 1;
    }
  }

  tryFinalize() {
    if (this.completed || !this.metaReceived) return null;
    if (this.totalGens === 0) {
      this.completed = true;
      this.sha256Ok = sha256Hex(new Uint8Array(0)) === this.expectedSha256;
      return { fullBytes: new Uint8Array(0), shaOk: this.sha256Ok };
    }
    if (this.solvedGens.size < this.totalGens) return null;

    let rawTotal = 0;
    for (let g = 0; g < this.totalGens; g++) {
      const b = this.solvedGens.get(g);
      if (!b) return null;
      rawTotal += b.length;
    }
    const cat = new Uint8Array(rawTotal);
    let pos = 0;
    for (let g = 0; g < this.totalGens; g++) {
      const b = this.solvedGens.get(g);
      cat.set(b, pos);
      pos += b.length;
    }
    const full = cat.subarray(0, this.totalBytes);
    const digest = sha256Hex(full);
    this.completed = true;
    this.sha256Ok = digest === this.expectedSha256;
    this.gens.clear();
    this.solvedGens.clear();
    return { fullBytes: new Uint8Array(full), shaOk: this.sha256Ok };
  }
}

// ---------------------------------------------------------------------------
// Core Transport Endpoint Engine (`NLTPEndpoint`)
// ---------------------------------------------------------------------------

export class NLTPEndpoint {
  constructor({
    connId = null,
    sendPacketFn,
    onFileSaveFn = null,
    windowGenerations = 32,
    shardBytes = DEFAULT_SHARD_BYTES,
    genK = DEFAULT_GEN_K,
    psk = null,
    maxTxKbps = 50000.0,
    keepaliveS = 15.0,
    adaptiveRate = true,
    initialBurstBytes = null,
  }) {
    if (typeof sendPacketFn !== 'function') {
      throw new TypeError('sendPacketFn(rawUint8Array, peerAddr) is required');
    }
    const rand4 = randomBytes(4);
    this.connId = (connId !== null ? Number(connId) : (readU32LE(rand4, 0) | 1)) >>> 0;
    this._sendPkt = sendPacketFn;
    this._onFileSave = onFileSaveFn;
    this.windowGenerations = windowGenerations;
    this.shardBytes = shardBytes;
    this.genK = genK;
    this._aead = psk !== null && psk !== undefined && psk !== '' ? new AEADCipherContext(psk) : null;

    this.pacer = maxTxKbps !== null && maxTxKbps > 0 ? new TokenBucketPacer(maxTxKbps) : null;
    this.rateCtl = null;
    if (this.pacer !== null && adaptiveRate) {
      this.rateCtl = new DeliveryRateController(maxTxKbps);
      this.pacer.setRateKbps(this.rateCtl.rateKbps, true);
    }
    this.initialBurstBytes = initialBurstBytes ? Number(initialBurstBytes) : DeliveryRateController.INIT_BURST_BYTES;
    this.keepaliveS = Number(keepaliveS);
    this._keepaliveTargets = new Map(); // peerKey -> { peer, lastTx }

    this._nextStreamId = 1;
    this._reflexSeq = 0;
    this._seenReflexIds = new Set();

    this.connRoutes = new Map();       // peerConnId -> peer
    this.outbound = new Map();         // streamId -> OutboundTransfer
    this.inbound = new Map();          // `${peerConnId}:${streamId}` -> InboundStreamState
    this.completedInbox = [];
    this._inboxWaiters = [];

    this.srttMs = 150.0;
    this._hasRttSample = false;
    this.minRttMs = 999999.0;
    this.peerLossEma = 0.0;
    this.wireBytesTx = 0;
    this.wireBytesRx = 0;
    this.authDropsRx = 0;
    this.roamEvents = 0;
    this.pktsTx = 0;
    this.pktsRx = 0;
    this.pongsRx = 0;
    this._pongPeerKeys = new Set();
    this._lastAckEmit = new Map();
    this._pendingAcks = new Map();
  }

  _nowS() {
    return performance.now() / 1000.0;
  }

  registerKeepalivePeer(peer) {
    const k = normalizePeerKey(peer);
    if (!this._keepaliveTargets.has(k)) {
      this._keepaliveTargets.set(k, { peer, lastTx: 0.0 });
    } else {
      this._keepaliveTargets.get(k).peer = peer;
    }
  }

  _emit(pktType, peer, payload, flags = 0, nowS = null) {
    const tRef = nowS === null ? this._nowS() : nowS;
    const tsMs = Math.floor(tRef * 1000.0) >>> 0;
    const raw = packNltpFrame(pktType, this.connId, payload, { flags, tsMs, aead: this._aead });
    this.wireBytesTx += raw.length;
    this.pktsTx += 1;
    const k = normalizePeerKey(peer);
    const kt = this._keepaliveTargets.get(k);
    if (kt) kt.lastTx = tRef;
    this._sendPkt(raw, peer);
  }

  sendPing(peer, nowS = null) {
    const tRef = nowS === null ? this._nowS() : nowS;
    const tsMs = Math.floor(tRef * 1000.0) >>> 0;
    const pl = new Uint8Array(4);
    writeU32LE(pl, 0, tsMs);
    this._emit(PT_PING, peer, pl, 0, tRef);
  }

  step(nowS = null) {
    this.stepTx(nowS);
  }

  popReceived() {
    return this.completedInbox.length > 0 ? this.completedInbox.shift() : null;
  }

  _canSendBulk(nBytes, nowS) {
    if (this.pacer === null) return true;
    const overhead = this._aead !== null ? ENC_HEADER_SIZE + AEAD_TAG_SIZE : HEADER_SIZE;
    const ok = this.pacer.consumeIfAvailable(nBytes + overhead, nowS);
    if (ok && this.rateCtl !== null) {
      this.rateCtl.onSent(nBytes + overhead);
    }
    return ok;
  }

  _fecLoss() {
    return this.rateCtl !== null ? this.rateCtl.lossForFec(this.peerLossEma) : this.peerLossEma;
  }

  startTransfer(peer, dataInput, { kind = 'bytes', filename = '', nowS = null } = {}) {
    const data = toUint8Array(dataInput);
    const tNow = nowS === null ? this._nowS() : nowS;
    const sid = this._nextStreamId++;
    const genSliceLen = this.shardBytes * this.genK;
    const gens = [];
    if (data.length === 0) {
      gens.push(new TxGeneration(sid, 0, new Uint8Array(0), this.shardBytes));
    } else {
      let gIdx = 0;
      for (let off = 0; off < data.length; off += genSliceLen) {
        gens.push(new TxGeneration(sid, gIdx++, data.subarray(off, Math.min(data.length, off + genSliceLen)), this.shardBytes));
      }
    }

    const shaHex = sha256Hex(data);
    const cleanFilename = basenameSafe(filename);
    const metaObj = {
      stream_id: sid,
      kind,
      filename: cleanFilename,
      total_bytes: data.length,
      total_gens: gens.length,
      sha256: shaHex,
    };
    const metaBytes = utf8Encode(JSON.stringify(metaObj));

    const targetKey = normalizePeerKey(peer);
    let knownCid = null;
    for (const [cid, addr] of this.connRoutes.entries()) {
      if (normalizePeerKey(addr) === targetKey) {
        knownCid = cid;
        break;
      }
    }

    let resolveDone;
    const donePromise = new Promise((resolve) => {
      resolveDone = resolve;
    });

    const xfer = {
      streamId: sid,
      peerAddr: peer,
      peerConnId: knownCid,
      kind,
      filename: cleanFilename,
      totalBytes: data.length,
      sha256Hex: shaHex,
      generations: gens,
      metaPayload: metaBytes,
      baseUnackedGen: 0,
      metaAcked: false,
      completed: false,
      verifiedSha256ByPeer: false,
      startedAt: tNow,
      completedAt: null,
      lastMetaTx: 0.0,
      pendingDofRequests: new Map(),
      _resolveDone: resolveDone,
      donePromise,
    };

    let anyActive = false;
    for (const x of this.outbound.values()) {
      if (!x.completed) {
        anyActive = true;
        break;
      }
    }
    if (this.rateCtl !== null && this.pacer !== null && !anyActive) {
      const burst = this.rateCtl.initialBurst(this.initialBurstBytes);
      if (burst > 0) {
        this.pacer.grantBurst(Math.min(burst, data.length + 2 * this.shardBytes));
      }
      this.rateCtl.startStream(sid, tNow);
    }

    this.outbound.set(sid, xfer);
    this.registerKeepalivePeer(peer);
    this.stepTx(tNow);
    return xfer;
  }

  sendReflex(peer, msgInput, { crypticTwin = true, nowS = null } = {}) {
    const msgBytes = toUint8Array(msgInput);
    this._reflexSeq = (this._reflexSeq + 1) >>> 0;
    const payload = new Uint8Array(6 + msgBytes.length);
    writeU32LE(payload, 0, this._reflexSeq);
    writeU16LE(payload, 4, msgBytes.length);
    payload.set(msgBytes, 6);
    this._emit(PT_REFLEX_MSG, peer, payload, 0x02, nowS);
    if (crypticTwin || this.peerLossEma > 0.05) {
      this._emit(PT_REFLEX_MSG, peer, payload, 0x02, nowS);
    }
    return this._reflexSeq;
  }

  _proactiveShards(xfer, gen) {
    const p = this._fecLoss();
    let n = p >= 0.03 ? Math.ceil(gen.kShards * p * 1.1) : 0;
    if (p >= 0.005 && this.rateCtl !== null) {
      const genBytes = gen.kShards * this.shardBytes;
      const bdpBytes = ((this.rateCtl.rateKbps * 1000.0) / 8.0) * (this.srttMs / 1000.0);
      const tailGens = Math.max(1, Math.ceil(bdpBytes / Math.max(1, genBytes)));
      if (gen.genId >= xfer.generations.length - tailGens) {
        n = Math.max(n + 1, p >= 0.1 ? 2 : 1);
      }
    }
    if (gen.genId === xfer.generations.length - 1 && gen.kShards > 1) {
      n = Math.max(n, 1);
    }
    return Math.min(n, Math.max(1, Math.floor(gen.kShards / 2)));
  }

  _effectiveWindowGenerations() {
    if (this._hasRttSample && this.minRttMs < 900000.0) {
      const queueThresh = Math.max(this.minRttMs * 2.0, this.minRttMs + 35.0);
      if (this.srttMs > queueThresh) {
        return Math.max(3, Math.floor(this.windowGenerations / 4));
      }
    }
    return this.windowGenerations;
  }

  stepTx(nowS = null) {
    const tNow = nowS === null ? this._nowS() : nowS;
    const rtoS = Math.max(0.055, Math.min(2.0, (this.srttMs / 1000.0) * 1.85));
    const inflightGuardS = Math.max(0.035, (this.srttMs / 1000.0) * 1.4);

    if (this._pendingAcks.size > 0) {
      const pending = Array.from(this._pendingAcks.entries());
      this._pendingAcks.clear();
      for (const [keyStr, { peerCid, sid, echoTsMs, forceAck }] of pending) {
        const st = this.inbound.get(keyStr);
        const peerAddr = this.connRoutes.get(peerCid);
        if (st && peerAddr && !st.completed) {
          this._checkStreamCompleteOrAck(peerAddr, peerCid, st, echoTsMs, tNow, forceAck);
        }
      }
    }

    const effWin = this._effectiveWindowGenerations();
    let pacerExhausted = false;

    for (const xfer of this.outbound.values()) {
      if (xfer.completed) continue;
      if (xfer.peerConnId !== null && this.connRoutes.has(xfer.peerConnId)) {
        xfer.peerAddr = this.connRoutes.get(xfer.peerConnId);
      }
      const peer = xfer.peerAddr;

      const needFinProbe = xfer.baseUnackedGen >= xfer.generations.length && !xfer.completed;
      if ((!xfer.metaAcked || needFinProbe) && (xfer.lastMetaTx === 0.0 || tNow - xfer.lastMetaTx >= rtoS)) {
        this._emit(PT_STREAM_META, peer, xfer.metaPayload, 0, tNow);
        xfer.lastMetaTx = tNow;
      }

      if (pacerExhausted) continue;

      if (xfer.pendingDofRequests.size > 0) {
        for (const [gId, dof] of Array.from(xfer.pendingDofRequests.entries())) {
          if (gId >= xfer.generations.length || xfer.generations[gId].ackedComplete) {
            xfer.pendingDofRequests.delete(gId);
            continue;
          }
          const gen = xfer.generations[gId];
          if (tNow - gen.lastRepairTime >= inflightGuardS) {
            gen.repairInFlight = 0;
          }
          const additionalNeeded = Math.max(0, dof - gen.repairInFlight);
          if (additionalNeeded <= 0) {
            xfer.pendingDofRequests.delete(gId);
            continue;
          }
          const extra = Math.ceil(additionalNeeded / Math.max(0.45, 1.0 - this._fecLoss() * 0.7));
          const nSend = Math.min(extra, gen.kShards + 2);
          let sentNow = 0;
          for (let i = 0; i < nSend; i++) {
            if (!this._canSendBulk(SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
              pacerExhausted = true;
              break;
            }
            this._emit(PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
            sentNow += 1;
          }
          if (sentNow > 0) {
            gen.repairInFlight += sentNow;
            gen.lastRepairTime = tNow;
            gen.lastTxTime = tNow;
          }
          if (sentNow >= additionalNeeded) {
            xfer.pendingDofRequests.delete(gId);
          }
          if (pacerExhausted) break;
        }
      }

      if (pacerExhausted) continue;

      const winEnd = Math.min(xfer.generations.length, xfer.baseUnackedGen + effWin);
      for (let gId = xfer.baseUnackedGen; gId < winEnd; gId++) {
        const gen = xfer.generations[gId];
        if (gen.ackedComplete) continue;

        if (gen.nextSysIdx < gen.kShards || !gen.proactiveDone) {
          while (gen.nextSysIdx < gen.kShards) {
            if (!this._canSendBulk(SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
              pacerExhausted = true;
              break;
            }
            this._emit(PT_DATA_SHARD, peer, gen.makeSystematicPayload(gen.nextSysIdx), 0, tNow);
            gen.nextSysIdx += 1;
          }
          if (pacerExhausted) break;

          if (!gen.proactiveDone) {
            if (gen.proactiveTarget < 0) {
              gen.proactiveTarget = this._proactiveShards(xfer, gen);
            }
            while (gen.proactiveSent < gen.proactiveTarget) {
              if (!this._canSendBulk(SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
                pacerExhausted = true;
                break;
              }
              this._emit(PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
              gen.proactiveSent += 1;
            }
            if (!pacerExhausted) {
              gen.proactiveDone = true;
              gen.lastTxTime = tNow;
            } else {
              break;
            }
          }
        } else if (tNow - gen.lastTxTime >= rtoS) {
          const nTail = this.peerLossEma < 0.22 ? 1 : 2;
          let sentTail = 0;
          for (let i = 0; i < Math.min(nTail, gen.kShards); i++) {
            if (!this._canSendBulk(SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
              pacerExhausted = true;
              break;
            }
            this._emit(PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
            sentTail += 1;
          }
          if (sentTail > 0) {
            gen.repairInFlight = 0;
            gen.lastTxTime = tNow;
          }
          if (pacerExhausted) break;
        }
      }
    }

    if (this.keepaliveS > 0.0 && this._keepaliveTargets.size > 0) {
      for (const entry of this._keepaliveTargets.values()) {
        if (entry.lastTx === 0.0 || tNow - entry.lastTx >= this.keepaliveS) {
          this.sendPing(entry.peer, tNow);
        }
      }
    }
  }

  _updatePeerRoute(peerConnId, peer) {
    const prev = this.connRoutes.get(peerConnId);
    if (prev !== undefined && normalizePeerKey(prev) !== normalizePeerKey(peer)) {
      this.roamEvents += 1;
    }
    this.connRoutes.set(peerConnId, peer);
  }

  _recordRttSample(rttMs) {
    const effRtt = Math.max(1.0, rttMs);
    this.minRttMs = Math.min(this.minRttMs, effRtt);
    if (this.rateCtl !== null) {
      this.rateCtl.onRttSample(effRtt);
    }
    if (!this._hasRttSample) {
      this.srttMs = effRtt;
      this._hasRttSample = true;
    } else {
      this.srttMs = 0.8 * this.srttMs + 0.2 * effRtt;
    }
  }

  _pushInbox(item) {
    if (this._inboxWaiters.length > 0) {
      const waiter = this._inboxWaiters.shift();
      waiter(item);
    } else {
      this.completedInbox.push(item);
    }
  }

  waitInbox(timeoutMs = 5000) {
    if (this.completedInbox.length > 0) {
      return Promise.resolve(this.completedInbox.shift());
    }
    return new Promise((resolve) => {
      let settled = false;
      const onItem = (item) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(item);
      };
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        const idx = this._inboxWaiters.indexOf(onItem);
        if (idx >= 0) this._inboxWaiters.splice(idx, 1);
        resolve(null);
      }, timeoutMs);
      this._inboxWaiters.push(onItem);
    });
  }

  onDatagram(rawInput, peer, nowS = null) {
    const raw = toUint8Array(rawInput);
    const tNow = nowS === null ? this._nowS() : nowS;
    this.wireBytesRx += raw.length;
    this.pktsRx += 1;

    const parsed = unpackNltpFrame(raw, this._aead);
    if (parsed === null) {
      this.authDropsRx += 1;
      return;
    }

    const { pktType, connId: peerConnId, tsMs: pktTsMs, payload } = parsed;
    this._updatePeerRoute(peerConnId, peer);

    if (pktType === PT_REFLEX_MSG && payload.length >= 6) {
      const rSeq = readU32LE(payload, 0);
      const mLen = readU16LE(payload, 4);
      const key = `${peerConnId}:${rSeq}`;
      if (!this._seenReflexIds.has(key)) {
        this._seenReflexIds.add(key);
        const msg = payload.slice(6, 6 + mLen);
        this._pushInbox(new ReceivedItem({
          connId: peerConnId,
          peerAddr: peer,
          streamId: 0,
          kind: 'reflex',
          data: msg,
        }));
      }
      return;
    }

    if (pktType === PT_NEURAL_STATE) {
      return;
    }

    if (pktType === PT_PING) {
      const echo = new Uint8Array(4);
      if (payload.length >= 4) {
        echo.set(payload.subarray(0, 4), 0);
      } else {
        writeU32LE(echo, 0, pktTsMs);
      }
      this._emit(PT_PONG, peer, echo, 0, tNow);
      return;
    }

    if (pktType === PT_PONG && payload.length >= 4) {
      const origTs = readU32LE(payload, 0);
      const nowMs = Math.floor(tNow * 1000.0) >>> 0;
      const rtt = ((nowMs - origTs) >>> 0);
      if (rtt >= 0 && rtt < 30000) {
        this._recordRttSample(rtt);
      }
      this.pongsRx += 1;
      this._pongPeerKeys.add(normalizePeerKey(peer));
      return;
    }

    if (pktType === PT_STREAM_META) {
      let meta;
      try {
        meta = JSON.parse(utf8Decode(payload));
      } catch {
        return;
      }
      const sid = Number(meta.stream_id);
      if (!Number.isFinite(sid)) return;
      const keyStr = `${peerConnId}:${sid}`;
      let st = this.inbound.get(keyStr);
      if (!st) {
        st = new InboundStreamState(sid);
        this.inbound.set(keyStr, st);
      }
      if (st.completed) {
        const finPl = new Uint8Array(5);
        writeU32LE(finPl, 0, sid >>> 0);
        finPl[4] = st.sha256Ok ? 1 : 0;
        this._emit(PT_STREAM_FIN_ACK, peer, finPl, 0, tNow);
        return;
      }
      st.ingestMeta(meta);
      this._checkStreamCompleteOrAck(peer, peerConnId, st, pktTsMs, tNow, true);
      return;
    }

    if ((pktType === PT_DATA_SHARD || pktType === PT_CRYPTIC_PARITY) && payload.length >= SHARD_HDR_SIZE) {
      const sid = readU32LE(payload, 0);
      const genId = readU32LE(payload, 4);
      const kShards = payload[8];
      const shardIdx = payload[9];
      const rowW = readU16LE(payload, 10);
      const shardBuf = payload.subarray(SHARD_HDR_SIZE, SHARD_HDR_SIZE + rowW);
      if (shardBuf.length !== rowW || kShards === 0) return;

      const keyStr = `${peerConnId}:${sid}`;
      let st = this.inbound.get(keyStr);
      if (!st) {
        st = new InboundStreamState(sid);
        this.inbound.set(keyStr, st);
      }
      if (st.completed) {
        const finPl = new Uint8Array(5);
        writeU32LE(finPl, 0, sid >>> 0);
        finPl[4] = st.sha256Ok ? 1 : 0;
        this._emit(PT_STREAM_FIN_ACK, peer, finPl, 0, tNow);
        return;
      }

      st.shardsRcvdCount += 1;
      st.bytesRcvd += payload.length;
      if (st.arrT0 === null) {
        st.arrT0 = tNow;
        st.arrB0 = st.bytesRcvd;
        st.arrN0 = st.shardsRcvdCount;
      } else if (st.shardsRcvdCount - st.arrN0 >= 16 && tNow - st.arrT0 >= 0.005) {
        st.arrivalKbps = ((st.bytesRcvd - st.arrB0) * 8.0) / 1000.0 / (tNow - st.arrT0);
        st.arrT0 = tNow;
        st.arrB0 = st.bytesRcvd;
        st.arrN0 = st.shardsRcvdCount;
      }

      st.genKMap.set(genId, kShards);
      if (pktType === PT_DATA_SHARD && shardIdx < kShards) {
        const rxExisting = st.gens.get(genId);
        const alreadySeen = st.solvedGens.has(genId) || (rxExisting !== undefined && rxExisting.seenIndices.has(shardIdx));
        if (!alreadySeen) st.sysShardsRcvd += 1;
        const prevMax = st.genMaxSysSeen.get(genId) ?? -1;
        if (shardIdx > prevMax) st.genMaxSysSeen.set(genId, shardIdx);
      } else if (pktType === PT_CRYPTIC_PARITY) {
        st.genMaxSysSeen.set(genId, kShards - 1);
      }
      if (genId > st.maxGenSeen) st.maxGenSeen = genId;

      if (!st.solvedGens.has(genId)) {
        let rxGen = st.gens.get(genId);
        if (!rxGen) {
          rxGen = new RxGeneration(kShards, rowW);
          st.gens.set(genId, rxGen);
        }
        const newlySolved = rxGen.addShard(shardIdx, shardBuf);
        if (newlySolved && rxGen.solvedBytes !== null) {
          st.solvedGens.set(genId, rxGen.solvedBytes);
          st.gens.delete(genId);
          st.advanceBase();
          if (st.metaReceived && st.solvedGens.size >= st.totalGens) {
            this._checkStreamCompleteOrAck(peer, peerConnId, st, pktTsMs, tNow, true);
          } else {
            this._pendingAcks.set(keyStr, { peerCid: peerConnId, sid, echoTsMs: pktTsMs, forceAck: true });
          }
          return;
        }
      }

      const lastA = this._lastAckEmit.get(keyStr) ?? 0.0;
      if (st.shardsRcvdCount % 8 === 0 || tNow - lastA >= 0.025) {
        this._pendingAcks.set(keyStr, { peerCid: peerConnId, sid, echoTsMs: pktTsMs, forceAck: true });
      }
      return;
    }

    if (pktType === PT_PROPRIOCEPTIVE_ACK && payload.length >= 14) {
      const sid = readU32LE(payload, 0);
      const baseGen = readU32LE(payload, 4);
      const echoTs = readU32LE(payload, 8);
      const lossQ8 = payload[12];
      const nRep = payload[13];

      const xfer = this.outbound.get(sid);
      if (!xfer || xfer.completed) return;
      if (xfer.peerConnId === null) {
        xfer.peerConnId = peerConnId;
      } else if (xfer.peerConnId !== peerConnId) {
        return;
      }
      xfer.peerAddr = peer;
      xfer.metaAcked = true;

      const nowMs = Math.floor(tNow * 1000.0) >>> 0;
      const rtt = (nowMs - echoTs) >>> 0;
      if (rtt >= 0 && rtt < 30000) {
        this._recordRttSample(rtt);
      }
      const repLoss = lossQ8 / 255.0;
      this.peerLossEma = 0.65 * this.peerLossEma + 0.35 * repLoss;

      const lim = Math.min(baseGen, xfer.generations.length);
      for (let g = xfer.baseUnackedGen; g < lim; g++) {
        xfer.generations[g].ackedComplete = true;
      }
      xfer.baseUnackedGen = Math.max(xfer.baseUnackedGen, baseGen);

      let pos = 14;
      for (let i = 0; i < nRep; i++) {
        if (pos + 4 > payload.length) break;
        const gOff = readU16LE(payload, pos);
        const neededDof = payload[pos + 3];
        pos += 4;
        const gAbs = baseGen + gOff;
        if (neededDof > 0 && gAbs < xfer.generations.length && !xfer.generations[gAbs].ackedComplete) {
          xfer.pendingDofRequests.set(gAbs, neededDof);
        }
      }

      if (this.rateCtl !== null && this.pacer !== null && pos + 4 <= payload.length) {
        const rxBytes = readU32LE(payload, pos);
        const arrivalKbps = pos + 8 <= payload.length ? readU32LE(payload, pos + 4) : 0;
        if (this.rateCtl.onAck(tNow, sid, rxBytes, this.srttMs, arrivalKbps)) {
          this.pacer.setRateKbps(this.rateCtl.rateKbps, true);
        }
      }

      this.stepTx(tNow);
      return;
    }

    if (pktType === PT_STREAM_FIN_ACK && payload.length >= 5) {
      const sid = readU32LE(payload, 0);
      const shaOk = payload[4];
      const xfer = this.outbound.get(sid);
      if (xfer && !xfer.completed) {
        if (xfer.peerConnId !== null && xfer.peerConnId !== peerConnId) return;
        xfer.peerConnId = peerConnId;
        xfer.peerAddr = peer;
        xfer.metaAcked = true;
        xfer.completed = true;
        xfer.verifiedSha256ByPeer = shaOk === 1;
        xfer.completedAt = tNow;
        for (const g of xfer.generations) g.ackedComplete = true;
        xfer._resolveDone(xfer.verifiedSha256ByPeer);
      }
    }
  }

  _checkStreamCompleteOrAck(peer, peerConnId, st, echoTsMs, tNow, forceAck) {
    const fin = st.tryFinalize();
    if (fin !== null) {
      const { fullBytes, shaOk } = fin;
      let savedPath = null;
      if (st.kind === 'file' && this._onFileSave !== null) {
        const safeName = basenameSafe(st.filename) || `stream_${st.streamId}.bin`;
        savedPath = this._onFileSave(safeName, fullBytes);
      }
      this._pushInbox(new ReceivedItem({
        connId: peerConnId,
        peerAddr: peer,
        streamId: st.streamId,
        kind: st.kind || 'bytes',
        data: fullBytes,
        filename: st.filename,
        savedPath,
        sha256Hex: sha256Hex(fullBytes),
        sha256Verified: shaOk,
      }));
      const finPl = new Uint8Array(5);
      writeU32LE(finPl, 0, st.streamId >>> 0);
      finPl[4] = shaOk ? 1 : 0;
      this._emit(PT_STREAM_FIN_ACK, peer, finPl, 0, tNow);
      this._emit(PT_STREAM_FIN_ACK, peer, finPl, 0, tNow);
      return;
    }

    const maxG = st.maxGenSeen;
    let expectedSys = 0;
    for (let gI = 0; gI < maxG; gI++) {
      expectedSys += st.genKMap.get(gI) ?? this.genK;
    }
    if (maxG >= 0) {
      expectedSys += (st.genMaxSysSeen.get(maxG) ?? 0) + 1;
    }
    expectedSys = Math.max(1, expectedSys);
    const estLoss = Math.max(0.0, Math.min(0.92, 1.0 - st.sysShardsRcvd / expectedSys));
    const lossQ8 = Math.round(estLoss * 255.0) & 0xff;

    const reports = [];
    for (let gId = st.baseGen; gId <= maxG; gId++) {
      if (st.solvedGens.has(gId)) continue;
      const rxG = st.gens.get(gId);
      const have = rxG ? rxG.rowsCoeff.length : 0;
      const need = rxG ? rxG.neededDof : this.genK;
      if (gId < maxG && need > 0) {
        reports.push([gId - st.baseGen, have, need]);
      } else if (gId === maxG && rxG && rxG.seenIndices.size > 0) {
        let maxIdx = 0;
        for (const idx of rxG.seenIndices) {
          if (idx > maxIdx) maxIdx = idx;
        }
        const maxSeenIdx = Math.min(rxG.kShards, maxIdx + 1);
        const confirmedMissing = Math.min(need, Math.max(0, maxSeenIdx - have));
        if (confirmedMissing > 0) {
          reports.push([gId - st.baseGen, have, confirmedMissing]);
        }
      }
      if (reports.length >= 16) break;
    }

    if (forceAck || reports.length > 0) {
      const ackBuf = new Uint8Array(14 + reports.length * 4 + 8);
      writeU32LE(ackBuf, 0, st.streamId >>> 0);
      writeU32LE(ackBuf, 4, st.baseGen >>> 0);
      writeU32LE(ackBuf, 8, echoTsMs >>> 0);
      ackBuf[12] = lossQ8;
      ackBuf[13] = reports.length;

      let pos = 14;
      for (const [gOff, have, need] of reports) {
        writeU16LE(ackBuf, pos, gOff);
        ackBuf[pos + 2] = have & 0xff;
        ackBuf[pos + 3] = need & 0xff;
        pos += 4;
      }
      writeU32LE(ackBuf, pos, st.bytesRcvd >>> 0);
      writeU32LE(ackBuf, pos + 4, Math.min(0xffffffff, Math.trunc(st.arrivalKbps)) >>> 0);

      this._emit(PT_PROPRIOCEPTIVE_ACK, peer, ackBuf, 0, tNow);
      this._lastAckEmit.set(`${peerConnId}:${st.streamId}`, tNow);
    }
  }
}
