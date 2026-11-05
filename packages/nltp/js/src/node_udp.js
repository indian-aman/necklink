/**
 * Node.js Internet UDP Stack for NLTP v1.2 (`NLTPServer`, `NLTPClient`, `NLTPRelayHub`, STUN).
 *
 * Uses built-in `node:dgram`, `node:dns/promises`, `node:fs`, and `node:path` (zero npm dependencies).
 */

import { webcrypto } from 'node:crypto';
import dgram from 'node:dgram';
import dns from 'node:dns/promises';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

import { NLTPEndpoint, normalizePeerKey } from './endpoint.js';

// Node 18 has no globalThis.crypto by default; NLTP needs it for keys and session nonces.
if (!globalThis.crypto) globalThis.crypto = webcrypto;
import {
  RELAY_VER_DATA,
  RELAY_VER_REG_ACK,
  RELAY_VER_REGISTER,
  buildStunBindingRequest,
  bytesEqual,
  isStunPacket,
  makeRoomTag,
  parseStunBindingResponse,
  toUint8Array,
  unwrapRelayPacket,
  utf8Encode,
  wrapRelayPacket,
} from './wire.js';

export const DEFAULT_PUBLIC_STUN_SERVERS = [
  ['stun.cloudflare.com', 3478],
  ['stun.l.google.com', 19302],
  ['stun1.l.google.com', 19302],
];

export async function resolveUdpAddress(host, port, preferFamily = 0) {
  const h = String(host || '0.0.0.0');
  const p = Number(port);
  const ipVer = net.isIP(h);
  if (ipVer === 4) return { family: 4, host: h, port: p };
  if (ipVer === 6) return { family: 6, host: h, port: p };

  const records = await dns.lookup(h, { all: true, family: preferFamily || 0 });
  if (!records || records.length === 0) {
    throw new Error(`Could not resolve UDP endpoint ${h}:${p}`);
  }
  if (!preferFamily) {
    const v4 = records.find((r) => r.family === 4);
    if (v4) return { family: 4, host: v4.address, port: p };
  }
  return { family: records[0].family, host: records[0].address, port: p };
}

function createBoundUdpSocket({ family = 4, host = '0.0.0.0', port = 0 }) {
  return new Promise((resolve, reject) => {
    const sock = dgram.createSocket({
      type: family === 6 ? 'udp6' : 'udp4',
      recvBufferSize: 2 * 1024 * 1024,
      sendBufferSize: 2 * 1024 * 1024,
    });
    const onErr = (err) => {
      sock.removeListener('listening', onListen);
      reject(err);
    };
    const onListen = () => {
      sock.removeListener('error', onErr);
      sock.on('error', () => {
        // Ignore transient ICMP ECONNRESET / network errors on active UDP sockets
      });
      resolve(sock);
    };
    sock.once('error', onErr);
    sock.once('listening', onListen);
    sock.bind(port, host);
  });
}

// ---------------------------------------------------------------------------
// Standalone Zero-Knowledge CGNAT Relay Hub (`NLTPRelayHub`)
// ---------------------------------------------------------------------------

export class NLTPRelayHub {
  constructor({ host = '0.0.0.0', port = 9000, peerTtlS = 90.0 } = {}) {
    this._bindHost = host;
    this._bindPort = port;
    this.peerTtlS = peerTtlS;
    this.rooms = new Map(); // hexRoomTag -> Map<peerKey, { host, port, seenS }>
    this.forwardedPackets = 0;
    this.forwardedBytes = 0;
    this._sock = null;
    this._gcTimer = null;
    this._readyPromise = this._start();
  }

  static async listen(opts = {}) {
    const hub = new NLTPRelayHub(opts);
    await hub.ready();
    return hub;
  }

  async _start() {
    const resolved = await resolveUdpAddress(this._bindHost, this._bindPort);
    this._sock = await createBoundUdpSocket(resolved);
    this._sock.on('message', (msg, rinfo) => this._onMessage(msg, rinfo));
    this._gcTimer = setInterval(() => this._gc(), 15000);
    if (this._gcTimer.unref) this._gcTimer.unref();
  }

  ready() {
    return this._readyPromise;
  }

  address() {
    const a = this._sock.address();
    return { host: a.address, port: a.port, family: a.family };
  }

  get relayedPackets() {
    return this.forwardedPackets;
  }

  get relayedBytes() {
    return this.forwardedBytes;
  }

  _onMessage(msg, rinfo) {
    const parsed = unwrapRelayPacket(msg);
    if (!parsed) return;
    const { cmd, roomTag, inner } = parsed;
    const roomHex = Buffer.from(roomTag).toString('hex');
    const nowS = performance.now() / 1000.0;
    let members = this.rooms.get(roomHex);
    if (!members) {
      members = new Map();
      this.rooms.set(roomHex, members);
    }
    const senderKey = `${rinfo.address}:${rinfo.port}`;
    members.set(senderKey, { host: rinfo.address, port: rinfo.port, seenS: nowS });

    if (cmd === RELAY_VER_REGISTER) {
      const ack = wrapRelayPacket(roomTag, new Uint8Array(0), RELAY_VER_REG_ACK);
      this._sock.send(ack, rinfo.port, rinfo.address);
      return;
    }

    if (cmd === RELAY_VER_DATA && inner.length > 0) {
      const fwd = wrapRelayPacket(roomTag, inner, RELAY_VER_DATA);
      for (const [peerKey, entry] of Array.from(members.entries())) {
        if (peerKey === senderKey) continue;
        if (nowS - entry.seenS > this.peerTtlS) {
          members.delete(peerKey);
          continue;
        }
        this._sock.send(fwd, entry.port, entry.host);
        this.forwardedPackets += 1;
        this.forwardedBytes += fwd.length;
      }
    }
  }

  _gc() {
    const nowS = performance.now() / 1000.0;
    for (const [roomHex, members] of Array.from(this.rooms.entries())) {
      for (const [peerKey, entry] of Array.from(members.entries())) {
        if (nowS - entry.seenS > this.peerTtlS) members.delete(peerKey);
      }
      if (members.size === 0) this.rooms.delete(roomHex);
    }
  }

  async close() {
    if (this._gcTimer) {
      clearInterval(this._gcTimer);
      this._gcTimer = null;
    }
    if (this._sock) {
      const s = this._sock;
      this._sock = null;
      await new Promise((resolve) => {
        try {
          s.close(() => resolve());
        } catch {
          resolve();
        }
      });
    }
  }
}

// ---------------------------------------------------------------------------
// Shared UDP Base for `NLTPServer` & `NLTPClient`
// ---------------------------------------------------------------------------

class NLTPSocketBase {
  _initCommon({
    saveDir = null,
    psk = null,
    relayAddr = null,
    room = null,
    maxTxKbps = 50000.0,
    keepaliveS = 15.0,
    lossDropSim = 0.0,
    adaptiveRate = true,
    initialBurstBytes = null,
  }) {
    this.saveDir = saveDir ? path.resolve(saveDir) : null;
    if (this.saveDir) {
      fs.mkdirSync(this.saveDir, { recursive: true });
    }
    this._lossSim = Number(lossDropSim || 0.0);
    this._stunWaiters = [];
    this._relaySpec = relayAddr;
    this._relayTarget = null;
    this._roomTag = null;
    this._relayRegistered = false;
    this._lastRelayRegS = 0.0;
    this._closed = false;
    this._tickTimer = null;

    if (relayAddr) {
      if (!room) throw new Error('`room` name is required when `relayAddr` is enabled');
      this._roomTag = makeRoomTag(room);
    }

    this.engine = new NLTPEndpoint({
      sendPacketFn: (pkt, peer) => this._rawSendTo(pkt, peer),
      onFileSaveFn: this.saveDir
        ? (safeName, bytes) => {
            const outPath = path.join(this.saveDir, safeName);
            fs.writeFileSync(outPath, bytes);
            return outPath;
          }
        : null,
      psk,
      maxTxKbps,
      keepaliveS,
      adaptiveRate,
      initialBurstBytes,
    });
  }

  _attachSocket(sock) {
    this._sock = sock;
    sock.on('message', (msg, rinfo) => this._onDatagramMessage(msg, rinfo));
  }

  _startTickLoop() {
    if (this._relayTarget) {
      this._sendRelayRegister();
    }
    this._tickTimer = setInterval(() => {
      if (this._closed || !this._sock) return;
      const nowS = performance.now() / 1000.0;
      if (this._relayTarget) {
        const regInterval = this._relayRegistered ? Math.max(5.0, this.engine.keepaliveS) : 0.25;
        if (nowS - this._lastRelayRegS >= regInterval) {
          this._sendRelayRegister();
        }
      }
      this.engine.stepTx(nowS);
    }, 2);
    if (this._tickTimer.unref) this._tickTimer.unref();
  }

  async _awaitInitialRelayReg(maxWaitMs = 300) {
    if (!this._relayTarget) return;
    const deadline = performance.now() + maxWaitMs;
    while (!this._relayRegistered && !this._closed && performance.now() < deadline) {
      await new Promise((r) => setTimeout(r, 5));
    }
  }

  _sendRelayRegister() {
    if (this._sock && this._relayTarget && this._roomTag) {
      const regPkt = wrapRelayPacket(this._roomTag, new Uint8Array(0), RELAY_VER_REGISTER);
      this._sock.send(regPkt, this._relayTarget.port, this._relayTarget.host);
      this._lastRelayRegS = performance.now() / 1000.0;
    }
  }

  _rawSendTo(pkt, peer) {
    if (this._closed || !this._sock) return;
    if (this._lossSim > 0.0 && Math.random() < this._lossSim) return;
    if (this._relayTarget && this._roomTag) {
      const wrapped = wrapRelayPacket(this._roomTag, pkt, RELAY_VER_DATA);
      this._sock.send(wrapped, this._relayTarget.port, this._relayTarget.host);
    } else if (peer) {
      const h = Array.isArray(peer) ? peer[0] : peer.host ?? peer.address;
      const p = Array.isArray(peer) ? peer[1] : peer.port;
      this._sock.send(pkt, p, h);
    }
  }

  _onDatagramMessage(msg, rinfo) {
    if (this._closed) return;
    if (isStunPacket(msg)) {
      for (const w of Array.from(this._stunWaiters)) {
        w(msg, rinfo);
      }
      return;
    }
    const peer = { host: rinfo.address, port: rinfo.port };
    if (this._roomTag) {
      const unwrapped = unwrapRelayPacket(msg);
      if (!unwrapped) return;
      if (!bytesEqual(unwrapped.roomTag, this._roomTag)) return;
      if (unwrapped.cmd === RELAY_VER_REG_ACK) {
        this._relayRegistered = true;
        return;
      }
      if (unwrapped.cmd === RELAY_VER_DATA && unwrapped.inner.length > 0) {
        if (this._lossSim > 0.0 && Math.random() < this._lossSim) return;
        this.engine.onDatagram(unwrapped.inner, peer);
        this.engine.stepTx();
      }
      return;
    }
    if (this._lossSim > 0.0 && Math.random() < this._lossSim) return;
    this.engine.onDatagram(msg, peer);
    this.engine.stepTx();
  }

  address() {
    const a = this._sock.address();
    return { host: a.address, port: a.port, family: a.family };
  }

  /**
   * Resolves true once reflex `seq` is acknowledged, false on timeout (or with a peer older than
   * v1.3.2). Keep the socket open until then: closing it stops the retransmissions.
   */
  async waitReflexAck(seq, timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (this.engine.reflexAcked(seq)) return true;
      await new Promise((r) => setTimeout(r, 2));
    }
    return false;
  }

  recv(timeoutMs = 5000) {
    return this.engine.waitInbox(timeoutMs);
  }

  async discoverWan({ stunServers = DEFAULT_PUBLIC_STUN_SERVERS, timeoutMs = 2500 } = {}) {
    await this.ready();
    const mappedResults = [];
    const family = this._sock.address().family === 'IPv6' ? 6 : 4;

    for (const [host, port] of stunServers) {
      try {
        const sa = await resolveUdpAddress(host, port, family);
        const { packet, txId } = buildStunBindingRequest();
        const t0 = performance.now();

        const ans = await new Promise((resolve) => {
          let done = false;
          const finish = (val) => {
            if (done) return;
            done = true;
            clearInterval(resendTimer);
            clearTimeout(deadlineTimer);
            const idx = this._stunWaiters.indexOf(onStun);
            if (idx >= 0) this._stunWaiters.splice(idx, 1);
            resolve(val);
          };
          const onStun = (raw) => {
            const parsed = parseStunBindingResponse(raw, txId);
            if (parsed) finish(parsed);
          };
          this._stunWaiters.push(onStun);
          this._sock.send(packet, sa.port, sa.host);
          const resendTimer = setInterval(() => {
            if (!this._closed && this._sock) {
              this._sock.send(packet, sa.port, sa.host);
            }
          }, 500);
          const deadlineTimer = setTimeout(() => finish(null), timeoutMs);
        });

        if (ans) {
          const rttMs = performance.now() - t0;
          mappedResults.push({ server: `${host}:${port}`, ans, rttMs });
          if (mappedResults.length >= 2) break;
        }
      } catch {
        // Try next STUN server
      }
    }

    if (mappedResults.length === 0) {
      throw new Error('No STUN response received on NLTP socket — outbound UDP may be blocked');
    }

    const first = mappedResults[0];
    const pubIp = first.ans.ip;
    const pubPort = first.ans.port;
    const localPort = this._sock.address().port;
    const eim = mappedResults.length >= 2 ? mappedResults.every((m) => m.ans.port === pubPort) : true;
    const natSummary =
      mappedResults.length >= 2
        ? eim
          ? 'Endpoint-Independent NAT (Direct P2P UDP Hole-Punching Supported)'
          : 'Symmetric CGNAT Detected (Use `NLTPRelayHub` `--relay` Mode)'
        : `Single STUN server reachable (${first.server}); WAN mapping verified`;

    return {
      publicIp: pubIp,
      publicPort: pubPort,
      localPort,
      rttMs: Number(first.rttMs.toFixed(2)),
      endpointIndependentNat: eim,
      natSummary,
      serversQueried: mappedResults.map((m) => m.server),
    };
  }

  async punchP2p(peerWanAddr, { timeoutMs = 5000, prePunchTtl = 2 } = {}) {
    await this.ready();
    const h = Array.isArray(peerWanAddr) ? peerWanAddr[0] : peerWanAddr.host;
    const p = Array.isArray(peerWanAddr) ? peerWanAddr[1] : peerWanAddr.port;
    const family = this._sock.address().family === 'IPv6' ? 6 : 4;
    const targetSa = await resolveUdpAddress(h, p, family);
    const peerKey = normalizePeerKey(targetSa);
    const basePongs = this.engine.pongsRx;
    this.engine.registerKeepalivePeer(targetSa);

    if (prePunchTtl > 0 && family === 4) {
      try {
        this._sock.setTTL(prePunchTtl);
        for (let i = 0; i < 3; i++) {
          this.engine.sendPing(targetSa);
          await new Promise((r) => setTimeout(r, 30));
        }
        this._sock.setTTL(64);
        await new Promise((r) => setTimeout(r, 300));
      } catch {
        try { this._sock.setTTL(64); } catch {}
      }
    }

    const deadline = performance.now() + timeoutMs;
    while (performance.now() < deadline) {
      if (this.engine.pongsRx > basePongs || this.engine._pongPeerKeys.has(peerKey)) {
        return true;
      }
      this.engine.sendPing(targetSa);
      await new Promise((r) => setTimeout(r, 55));
    }
    return this.engine.pongsRx > basePongs || this.engine._pongPeerKeys.has(peerKey);
  }

  async _awaitTransfer(xfer, timeoutMs) {
    let timer;
    const ok = await Promise.race([
      xfer.donePromise,
      new Promise((resolve) => {
        timer = setTimeout(() => resolve(false), timeoutMs);
      }),
    ]);
    clearTimeout(timer);
    return Boolean(ok);
  }

  _buildReceipt(xfer, ok) {
    const nowS = performance.now() / 1000.0;
    const elapsedS = Math.max(1e-6, (xfer.completedAt ?? nowS) - xfer.startedAt);
    const goodputKbps = Number((((xfer.totalBytes * 8.0) / 1000.0) / elapsedS).toFixed(2));
    const estLoss = Number((this.engine.peerLossEma * 100.0).toFixed(2));
    const enc = this.engine._aead !== null;
    return {
      streamId: xfer.streamId,
      stream_id: xfer.streamId,
      filename: xfer.filename,
      bytes: xfer.totalBytes,
      sha256: xfer.sha256Hex,
      verifiedSha256: ok,
      verified_sha256: ok,
      elapsedS: Number(elapsedS.toFixed(4)),
      elapsed_s: Number(elapsedS.toFixed(4)),
      goodputKbps,
      goodput_kbps: goodputKbps,
      estimatedLinkLossPct: estLoss,
      estimated_link_loss_pct: estLoss,
      encrypted: enc,
    };
  }

  async close() {
    // Frames sent just before close() may still wait for a session handshake: give it up to 1 s.
    const lingerT0 = Date.now();
    while (this._sock && this.engine._sessions && this.engine._sessions.queuedCount() > 0 && Date.now() - lingerT0 < 1000) {
      await new Promise((r) => setTimeout(r, 5));
    }
    this._closed = true;
    if (this._tickTimer) {
      clearInterval(this._tickTimer);
      this._tickTimer = null;
    }
    if (this._sock) {
      const s = this._sock;
      this._sock = null;
      await new Promise((resolve) => {
        try {
          s.close(() => resolve());
        } catch {
          resolve();
        }
      });
    }
  }
}

// ---------------------------------------------------------------------------
// `NLTPServer` & `NLTPClient`
// ---------------------------------------------------------------------------

export class NLTPServer extends NLTPSocketBase {
  constructor(opts = {}) {
    super();
    const {
      host = '0.0.0.0',
      port = 9000,
      relayAddr = null,
      ...rest
    } = opts;
    this._bindHost = host;
    this._bindPort = port;
    this._initCommon({ relayAddr, ...rest });
    this._readyPromise = this._start();
  }

  static async listen(opts = {}) {
    const srv = new NLTPServer(opts);
    await srv.ready();
    return srv;
  }

  async _start() {
    let preferFamily = 0;
    if (this._relaySpec) {
      const rHost = Array.isArray(this._relaySpec) ? this._relaySpec[0] : this._relaySpec.host;
      const rPort = Array.isArray(this._relaySpec) ? this._relaySpec[1] : this._relaySpec.port;
      this._relayTarget = await resolveUdpAddress(rHost, rPort);
      preferFamily = this._relayTarget.family;
    }
    const bindResolved = await resolveUdpAddress(this._bindHost, this._bindPort, preferFamily);
    const sock = await createBoundUdpSocket(bindResolved);
    this._attachSocket(sock);
    this._startTickLoop();
    await this._awaitInitialRelayReg(300);
  }

  ready() {
    return this._readyPromise;
  }

  /** Urgent reflex (e.g. an E-stop) to a connected client; retransmitted until acknowledged. */
  async sendReflex(clientAddr, urgentMsg) {
    const raw = typeof urgentMsg === 'string' ? new TextEncoder().encode(urgentMsg) : urgentMsg;
    return this.engine.sendReflex(clientAddr, raw);
  }

  async sendBytes(clientAddr, data, { filename = '', timeoutMs = 15000 } = {}) {
    await this.ready();
    const kind = filename ? 'file' : 'bytes';
    const xfer = this.engine.startTransfer(clientAddr, data, {
      kind,
      filename: filename ? path.basename(filename) : '',
    });
    return this._awaitTransfer(xfer, timeoutMs);
  }

  async sendFile(clientAddr, filepath, { timeoutMs = 60000 } = {}) {
    await this.ready();
    const raw = fs.readFileSync(filepath);
    const xfer = this.engine.startTransfer(clientAddr, raw, { kind: 'file', filename: path.basename(filepath) });
    return this._awaitTransfer(xfer, timeoutMs);
  }
}

export class NLTPClient extends NLTPSocketBase {
  constructor(opts = {}) {
    super();
    const {
      host = null,
      port = null,
      serverAddr = null,
      bindHost = null,
      bindPort = 0,
      relayAddr = null,
      ...rest
    } = opts;

    const effServer = serverAddr ?? (host !== null && port !== null ? { host, port } : null);
    if (!effServer && !relayAddr) {
      throw new Error('Provide either `host`+`port` (`serverAddr`) or `relayAddr`');
    }
    this._serverSpec = effServer;
    this._bindHost = bindHost;
    this._bindPort = bindPort;
    this._initCommon({ relayAddr, ...rest });
    this._readyPromise = this._start();
  }

  static async connect(opts = {}) {
    const cli = new NLTPClient(opts);
    await cli.ready();
    return cli;
  }

  async _start() {
    const targetSpec = this._relaySpec ?? this._serverSpec;
    const tHost = Array.isArray(targetSpec) ? targetSpec[0] : targetSpec.host;
    const tPort = Array.isArray(targetSpec) ? targetSpec[1] : targetSpec.port;
    const resolvedTarget = await resolveUdpAddress(tHost, tPort);
    if (this._relaySpec) {
      this._relayTarget = resolvedTarget;
    }
    this.serverAddr = resolvedTarget;

    const bHost = this._bindHost ?? (resolvedTarget.family === 6 ? '::' : '0.0.0.0');
    const bindResolved = await resolveUdpAddress(bHost, this._bindPort, resolvedTarget.family);
    const sock = await createBoundUdpSocket(bindResolved);
    this._attachSocket(sock);
    this.engine.registerKeepalivePeer(this.serverAddr);
    this._startTickLoop();
    await this._awaitInitialRelayReg(300);
  }

  ready() {
    return this._readyPromise;
  }

  async setPeerEndpoint(host, port) {
    await this.ready();
    const family = this._sock.address().family === 'IPv6' ? 6 : 4;
    const sa = await resolveUdpAddress(host, port, family);
    this.serverAddr = sa;
    this.engine.registerKeepalivePeer(sa);
  }

  async rebindSocket({ host = null, port = 0 } = {}) {
    await this.ready();
    const family = this.serverAddr.family || 4;
    const bHost = host ?? (family === 6 ? '::' : '0.0.0.0');
    const bindResolved = await resolveUdpAddress(bHost, port, family);
    const newSock = await createBoundUdpSocket(bindResolved);
    const oldSock = this._sock;
    this._attachSocket(newSock);

    for (const xfer of this.engine.outbound.values()) {
      if (!xfer.completed) xfer.peerAddr = this.serverAddr;
    }
    if (this._relayTarget) {
      this._relayRegistered = false;
      this._sendRelayRegister();
    }
    this.engine.sendPing(this.serverAddr);
    try {
      oldSock.close();
    } catch {}
    return this.address();
  }

  async sendBytes(data, { filename = '', timeoutMs = 20000 } = {}) {
    await this.ready();
    const kind = filename ? 'file' : 'bytes';
    const xfer = this.engine.startTransfer(this.serverAddr, data, {
      kind,
      filename: filename ? path.basename(filename) : '',
    });
    const ok = await this._awaitTransfer(xfer, timeoutMs);
    return this._buildReceipt(xfer, ok);
  }

  async sendMessage(text, { timeoutMs = 15000 } = {}) {
    return this.sendBytes(utf8Encode(String(text)), { timeoutMs });
  }

  async sendJson(obj, { timeoutMs = 15000 } = {}) {
    await this.ready();
    const raw = utf8Encode(JSON.stringify(obj));
    const xfer = this.engine.startTransfer(this.serverAddr, raw, { kind: 'json' });
    const ok = await this._awaitTransfer(xfer, timeoutMs);
    return this._buildReceipt(xfer, ok);
  }

  async sendFile(filepath, { timeoutMs = 60000 } = {}) {
    await this.ready();
    const raw = fs.readFileSync(filepath);
    const xfer = this.engine.startTransfer(this.serverAddr, raw, { kind: 'file', filename: path.basename(filepath) });
    const ok = await this._awaitTransfer(xfer, timeoutMs);
    return this._buildReceipt(xfer, ok);
  }

  async sendReflex(urgentMsg) {
    await this.ready();
    const raw = typeof urgentMsg === 'string' ? utf8Encode(urgentMsg) : toUint8Array(urgentMsg);
    return this.engine.sendReflex(this.serverAddr, raw);
  }

}

export async function discoverWanEndpoint({ stunServers = DEFAULT_PUBLIC_STUN_SERVERS, timeoutMs = 2500 } = {}) {
  const srv = await NLTPServer.listen({ host: '0.0.0.0', port: 0, keepaliveS: 0 });
  try {
    return await srv.discoverWan({ stunServers, timeoutMs });
  } finally {
    await srv.close();
  }
}
