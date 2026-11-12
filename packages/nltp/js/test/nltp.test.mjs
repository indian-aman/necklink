import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

import {
  AEADCipherContext,
  NLTPClient,
  NLTPEndpoint,
  NLTPRelayHub,
  NLTPServer,
  PT_DATA_SHARD,
  ReplayWindow64,
  cauchyRow,
  chacha20Xor,
  crc32Ieee,
  deriveAeadKey,
  generateNltpKey,
  gf256Axpy,
  gf256Inv,
  gf256Mul,
  packNltpFrame,
  poly1305AeadTag,
  sha256Bytes,
  sha256Hex,
  solveLinearSystemGF256,
  unpackNltpFrame,
  x25519PublicFromPrivate,
  x25519ScalarMult,
} from '../src/index.js';

test('Pure-JS SHA-256 matches node:crypto across boundary lengths', () => {
  for (const len of [0, 1, 55, 56, 57, 63, 64, 65, 128, 8192]) {
    const buf = crypto.randomBytes(len);
    const expected = crypto.createHash('sha256').update(buf).digest('hex');
    assert.equal(sha256Hex(buf), expected, `mismatch at len=${len}`);
  }
});

test('Pure-JS RFC 8439 ChaCha20-Poly1305 matches node:crypto byte-for-byte', () => {
  const key = crypto.randomBytes(32);
  const nonce = crypto.randomBytes(12);
  const aad = crypto.randomBytes(20);
  const pt = crypto.randomBytes(973);

  const cipher = crypto.createCipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });
  cipher.setAAD(aad, { plaintextLength: pt.length });
  const nodeCt = Buffer.concat([cipher.update(pt), cipher.final()]);
  const nodeTag = cipher.getAuthTag();

  const jsCt = chacha20Xor(key, 1, nonce, pt);
  const jsTag = poly1305AeadTag(key, nonce, aad, jsCt);

  assert.deepEqual(Buffer.from(jsCt), nodeCt);
  assert.deepEqual(Buffer.from(jsTag), nodeTag);
});

test('Key derivation & ReplayWindow64 match Python/C99 specification', () => {
  const b64Key = generateNltpKey();
  assert.equal(b64Key.length, 44);
  const raw32 = deriveAeadKey(b64Key);
  assert.equal(raw32.length, 32);
  assert.deepEqual(Buffer.from(raw32), Buffer.from(b64Key, 'base64url'));

  // Trimmed whitespace around base64 key works identically
  assert.deepEqual(deriveAeadKey(`  ${b64Key}\n`), raw32);

  // Passphrase hashes with NLTP-v1-PASSPHRASE: prefix
  const pass = 'marine-buoy-passphrase';
  const expectedPassKey = crypto.createHash('sha256').update(`NLTP-v1-PASSPHRASE:${pass}`).digest();
  assert.deepEqual(Buffer.from(deriveAeadKey(pass)), expectedPassKey);

  // 44-char passphrase containing spaces is NOT misidentified as base64url
  const pass44 = 'this passphrase has exact forty four chars!!';
  assert.equal(pass44.length, 44);
  const expectedPass44 = crypto.createHash('sha256').update(`NLTP-v1-PASSPHRASE:${pass44}`).digest();
  assert.deepEqual(Buffer.from(deriveAeadKey(pass44)), expectedPass44);

  // ReplayWindow64 (512-packet sliding window) out-of-order & replay blocking
  const rw = new ReplayWindow64();
  assert.equal(rw.accept(0n), false);
  assert.equal(rw.accept(5n), true);
  assert.equal(rw.accept(5n), false);
  assert.equal(rw.accept(3n), true);
  assert.equal(rw.accept(3n), false);
  assert.equal(rw.accept(600n), true);
  assert.equal(rw.accept(88n), false); // 600 - 88 == 512 -> outside 512-pkt window
  assert.equal(rw.accept(89n), true);  // 600 - 89 == 511 -> inside 512-pkt window
  assert.equal(rw.accept(89n), false);

  // Pure-JS RFC 7748 Section 5.2 X25519 test vector + low-order point rejection
  const aliceSk = Buffer.from('a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4', 'hex');
  const bobPk = Buffer.from('e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c', 'hex');
  const expectedShared = Buffer.from('c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552', 'hex');
  assert.deepEqual(Buffer.from(x25519ScalarMult(aliceSk, bobPk)), expectedShared);
  assert.equal(x25519ScalarMult(aliceSk, new Uint8Array(32)), null);
  const alicePk = x25519PublicFromPrivate(aliceSk);
  assert.equal(alicePk.length, 32);
});

test('Encrypted & plaintext frame packing rejects tampering, replays, and downgrades', () => {
  const key = generateNltpKey();
  const txAead = new AEADCipherContext(key);
  const rxAead = new AEADCipherContext(key);
  const wrongAead = new AEADCipherContext('wrong-key');

  const pt = new Uint8Array([10, 20, 30, 40, 50]);
  const encFrame = packNltpFrame(PT_DATA_SHARD, 0x12345679, pt, { tsMs: 777, aead: txAead });
  const plainFrame = packNltpFrame(PT_DATA_SHARD, 0x12345679, pt, { tsMs: 777 });

  // Downgrade rejected by encrypted receiver
  assert.equal(unpackNltpFrame(plainFrame, rxAead), null);
  // Encrypted frame rejected by plaintext receiver
  assert.equal(unpackNltpFrame(encFrame, null), null);
  // Wrong key rejected
  assert.equal(unpackNltpFrame(encFrame, wrongAead), null);

  // Authentic frame accepted once, replay rejected
  const unpacked = unpackNltpFrame(encFrame, rxAead);
  assert.ok(unpacked);
  assert.equal(unpacked.pktType, PT_DATA_SHARD);
  assert.equal(unpacked.connId, 0x12345679);
  assert.equal(unpacked.tsMs, 777);
  assert.deepEqual(unpacked.payload, pt);
  assert.equal(unpackNltpFrame(encFrame, rxAead), null);

  // Bit-flip rejected without advancing replay window
  const encFrame2 = packNltpFrame(PT_DATA_SHARD, 0x12345679, pt, { tsMs: 778, aead: txAead });
  const tampered = new Uint8Array(encFrame2);
  tampered[22] ^= 0x01;
  assert.equal(unpackNltpFrame(tampered, rxAead), null);
  assert.ok(unpackNltpFrame(encFrame2, rxAead));
});

test('GF(2^8) Cauchy FEC reconstructs 8 shards after arbitrary 4-shard erasures', () => {
  for (let a = 1; a < 256; a++) {
    assert.equal(gf256Mul(a, gf256Inv(a)), 1);
  }
  const k = 8;
  const w = 128;
  const orig = Array.from({ length: k }, () => new Uint8Array(crypto.randomBytes(w)));

  // Keep systematic shards 0, 2, 5, 7 and replace 4 erased shards with Cauchy parity offsets 0, 3, 9, 41
  const coeffRows = [];
  const dataRows = [];
  for (const sysIdx of [0, 2, 5, 7]) {
    const c = new Uint8Array(k);
    c[sysIdx] = 1;
    coeffRows.push(c);
    dataRows.push(new Uint8Array(orig[sysIdx]));
  }
  for (const pOff of [0, 3, 9, 41]) {
    const c = cauchyRow(pOff, k);
    const y = new Uint8Array(w);
    for (let i = 0; i < k; i++) gf256Axpy(y, orig[i], c[i]);
    coeffRows.push(c);
    dataRows.push(y);
  }

  const solved = solveLinearSystemGF256(coeffRows, dataRows, k, w);
  assert.ok(solved);
  for (let i = 0; i < k; i++) {
    assert.deepEqual(solved[i], orig[i]);
  }
});

test('Transport-agnostic NLTPEndpoint heals 20% packet loss in-memory (Browser/WebRTC mode)', async () => {
  const key = generateNltpKey();
  let epA, epB;
  let rng = 1234567;
  const nextRand = () => {
    rng = (rng * 1664525 + 1013904223) >>> 0;
    return rng / 0x100000000;
  };
  const queueAB = [];
  const queueBA = [];

  epA = new NLTPEndpoint({
    psk: key,
    maxTxKbps: null,
    sendPacketFn: (pkt) => {
      if (nextRand() >= 0.20) queueAB.push(new Uint8Array(pkt));
    },
  });
  epB = new NLTPEndpoint({
    psk: key,
    maxTxKbps: null,
    sendPacketFn: (pkt) => {
      if (nextRand() >= 0.20) queueBA.push(new Uint8Array(pkt));
    },
  });

  const payload = new Uint8Array(crypto.randomBytes(45000));
  let nowS = 1.0;
  const xfer = epA.startTransfer('peerB', payload, { kind: 'bytes', nowS });

  for (let step = 0; step < 600 && !xfer.completed; step++) {
    nowS += 0.010;
    while (queueAB.length > 0) epB.onDatagram(queueAB.shift(), 'peerA', nowS);
    epB.stepTx(nowS);
    while (queueBA.length > 0) epA.onDatagram(queueBA.shift(), 'peerB', nowS);
    epA.stepTx(nowS);
  }

  assert.equal(xfer.completed, true);
  assert.equal(xfer.verifiedSha256ByPeer, true);
  const item = await epB.waitInbox(10);
  assert.ok(item);
  assert.equal(item.sha256Verified, true);
  assert.deepEqual(Buffer.from(item.data), Buffer.from(payload));
});

test('Node UDP NLTPServer <-> NLTPClient encrypted file, reflex, roaming & bidirectional transfer at 15% loss', async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nltp-js-test-'));
  const srcFile = path.join(tmpDir, 'firmware.bin');
  const srcBytes = crypto.randomBytes(96 * 1024);
  fs.writeFileSync(srcFile, srcBytes);
  const rxDir = path.join(tmpDir, 'inbox');
  const key = generateNltpKey();

  const srv = await NLTPServer.listen({
    host: '127.0.0.1',
    port: 0,
    psk: key,
    saveDir: rxDir,
    lossDropSim: 0.15,
  });
  const cli = await NLTPClient.connect({
    host: '127.0.0.1',
    port: srv.address().port,
    psk: key,
    lossDropSim: 0.15,
  });

  try {
    // 1. Urgent reflex command
    await cli.sendReflex('ESTOP_NOW');
    const reflexItem = await srv.recv(5000);
    assert.ok(reflexItem);
    assert.equal(reflexItem.kind, 'reflex');
    assert.equal(reflexItem.asText(), 'ESTOP_NOW');

    // 2. Encrypted file transfer under 15% bidirectional packet loss
    const receipt = await cli.sendFile(srcFile, { timeoutMs: 25000 });
    assert.equal(receipt.verifiedSha256, true);
    assert.equal(receipt.encrypted, true);

    const fileItem = await srv.recv(5000);
    assert.ok(fileItem);
    assert.equal(fileItem.kind, 'file');
    assert.equal(fileItem.sha256Verified, true);
    assert.deepEqual(Buffer.from(fileItem.data), srcBytes);
    assert.deepEqual(fs.readFileSync(path.join(rxDir, 'firmware.bin')), srcBytes);

    // 3. Socket roaming (`rebindSocket`) + JSON message
    const oldPort = cli.address().port;
    const newAddr = await cli.rebindSocket({ host: '127.0.0.1', port: 0 });
    assert.notEqual(newAddr.port, oldPort);
    const jsonReceipt = await cli.sendJson({ buoy: 7, hs_m: 2.41 });
    assert.equal(jsonReceipt.verifiedSha256, true);
    const jsonItem = await srv.recv(5000);
    assert.deepEqual(jsonItem.asJson(), { buoy: 7, hs_m: 2.41 });
    assert.ok(srv.engine.roamEvents >= 1);

    // 4. Reverse direction: Server -> Client
    const replyBytes = crypto.randomBytes(16384);
    const okBack = await srv.sendBytes(jsonItem.peerAddr, replyBytes, { timeoutMs: 15000 });
    assert.equal(okBack, true);
    const clientRx = await cli.recv(5000);
    assert.ok(clientRx);
    assert.deepEqual(Buffer.from(clientRx.data), replyBytes);
  } finally {
    await cli.close();
    await srv.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('Zero-knowledge NLTPRelayHub traverses simulated CGNAT with E2E encryption', async () => {
  const key = 'cgnat-buoy-secret';
  const hub = await NLTPRelayHub.listen({ host: '127.0.0.1', port: 0 });
  const relayAddr = { host: '127.0.0.1', port: hub.address().port };

  const srv = await NLTPServer.listen({
    host: '127.0.0.1',
    port: 0,
    psk: key,
    relayAddr,
    room: 'buoy-room-42',
    lossDropSim: 0.10,
  });
  const cli = await NLTPClient.connect({
    psk: key,
    relayAddr,
    room: 'buoy-room-42',
    lossDropSim: 0.10,
  });

  try {
    const payload = crypto.randomBytes(32768);
    const receipt = await cli.sendBytes(payload, { timeoutMs: 20000 });
    assert.equal(receipt.verifiedSha256, true);
    const rx = await srv.recv(5000);
    assert.ok(rx);
    assert.deepEqual(Buffer.from(rx.data), payload);
    assert.ok(hub.forwardedPackets > 0);
  } finally {
    await cli.close();
    await srv.close();
    await hub.close();
  }
});

test('CommonJS require("./src/index.cjs") works cleanly', () => {
  const require = createRequire(import.meta.url);
  const cjs = require('../src/index.cjs');
  assert.equal(typeof cjs.NLTPClient, 'function');
  assert.equal(typeof cjs.NLTPServer, 'function');
  assert.equal(cjs.generateNltpKey().length, 44);
  assert.equal(crc32Ieee(new TextEncoder().encode('123456789')), 0xcbf43926);
});

test('NLTP 1.6 LZ4 block codec, RLE overlap, incompressible auto-bypass & encrypted E2E under 12% loss', async () => {
  const { lz4Compress, lz4Decompress } = await import('../src/index.js');

  // 1. Repetitive AQMS JSONL log (>6x compression)
  let logText = '';
  for (let i = 0; i < 350; i++) {
    logText += `{"ts":"2026-11-12T03:${String(Math.trunc(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}Z","station":"AQMS-01","pm25":23.4,"pm10":51.0,"status":"OK"}\n`;
  }
  const raw = new TextEncoder().encode(logText);
  const comp = lz4Compress(raw);
  assert.ok(comp !== null && comp.length < raw.length / 5);
  assert.deepEqual(lz4Decompress(comp, raw.length), raw);

  // 2. Overlapping RLE matches (off=1 and off=3 < mlen)
  const rle = new TextEncoder().encode('A'.repeat(500) + 'XYZ'.repeat(300) + 'tail-literals');
  const compRle = lz4Compress(rle);
  assert.ok(compRle !== null && compRle.length < 80);
  assert.deepEqual(lz4Decompress(compRle, rle.length), rle);

  // 3. Incompressible payload returns null + fuzz guards reject malformed input
  const rnd = crypto.randomBytes(16384);
  assert.equal(lz4Compress(rnd), null);
  assert.equal(lz4Decompress(comp.subarray(0, comp.length - 3), raw.length), null);
  assert.equal(lz4Decompress(new Uint8Array([0x1f, 0x41, 0x00, 0x00]), 20), null);

  // 4. Encrypted X25519+PSK + LZ4 E2E transfer under 12% loss + incompressible fallback
  const psk = generateNltpKey();
  const srv = await NLTPServer.listen({ host: '127.0.0.1', port: 0, psk, lossDropSim: 0.12 });
  const cli = await NLTPClient.connect({ host: '127.0.0.1', port: srv.address().port, psk, lossDropSim: 0.12 });
  try {
    const rec1 = await cli.sendBytes(raw, { compress: true, timeoutMs: 15000 });
    assert.equal(rec1.verifiedSha256, true);
    assert.equal(rec1.compressed, true);
    assert.equal(rec1.bytes, raw.length);
    assert.ok(rec1.wirePayloadBytes < raw.length / 5);

    const rx1 = await srv.recv(5000);
    assert.ok(rx1 && rx1.sha256Verified);
    assert.deepEqual(rx1.data, raw);

    const rec2 = await cli.sendBytes(rnd, { compress: true, timeoutMs: 15000 });
    assert.equal(rec2.verifiedSha256, true);
    assert.equal(rec2.compressed, false);
    assert.equal(rec2.wirePayloadBytes, rnd.length);

    const rx2 = await srv.recv(5000);
    assert.ok(rx2 && rx2.sha256Verified);
    assert.deepEqual(Buffer.from(rx2.data), rnd);
  } finally {
    await cli.close();
    await srv.close();
  }
});
