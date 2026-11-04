/**
 * NLTP v1.2 JavaScript Example 1:
 * Encrypted File Transfer + Out-of-Line Reflex Command under 10% Packet Loss
 *
 * Run:
 *   node examples/javascript/encrypted_file_and_reflex.mjs
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { NLTPClient, NLTPServer, generateNltpKey } from '../../js/src/index.js';

const key = generateNltpKey();
const inboxDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nltp-js-demo-'));

const srv = await NLTPServer.listen({
  host: '127.0.0.1',
  port: 0,
  psk: key,
  saveDir: inboxDir,
  maxTxKbps: 25_000,
  lossDropSim: 0.10, // 10% simulated wireless packet loss
});

const { port } = srv.address();
const cli = await NLTPClient.connect({
  host: '127.0.0.1',
  port,
  psk: key,
  maxTxKbps: 25_000,
  lossDropSim: 0.10,
});

try {
  // 1. Urgent reflex frame bypasses any bulk file queue (zero head-of-line blocking)
  cli.sendReflex('ESTOP_CHECK');
  const reflexItem = await srv.recv(3000);
  console.log('[1/3] Server received urgent reflex:', new TextDecoder().decode(reflexItem.data));

  // 2. Client -> Server: 128 KB encrypted payload over 10% lossy UDP
  const payload = crypto.randomBytes(128 * 1024);
  const [receipt, rxFile] = await Promise.all([
    cli.sendBytes(payload, { filename: 'buoy_telemetry_128k.bin', timeoutMs: 30_000 }),
    srv.recv(30_000),
  ]);

  console.log(
    `[2/3] Client -> Server 128 KB @ 10% loss: verified=${receipt.verifiedSha256}, ` +
    `elapsed=${receipt.elapsedS}s, goodput=${receipt.goodputKbps} kbps, saved=${rxFile.savedPath}`
  );

  // 3. Server -> Client: reverse command/config payload over the same UDP port
  const configJson = new TextEncoder().encode(JSON.stringify({ sample_hz: 50, mode: 'storm' }));
  const [revOk, cliRx] = await Promise.all([
    srv.sendBytes(rxFile.peerAddr, configJson, { filename: 'storm_config.json', timeoutMs: 15_000 }),
    cli.recv(15_000),
  ]);

  console.log(
    `[3/3] Server -> Client reverse config: verified=${revOk}, ` +
    `payload=${new TextDecoder().decode(cliRx.data)}`
  );
} finally {
  await cli.close();
  await srv.close();
  fs.rmSync(inboxDir, { recursive: true, force: true });
}
