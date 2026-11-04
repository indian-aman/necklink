/**
 * NLTP v1.2 JavaScript Example 2:
 * Zero-Knowledge 1-Port CGNAT Relay Hub (4G/5G SIM Traversal)
 *
 * Neither peer needs port forwarding or a public IP; the relay hub holds ZERO keys
 * and forwards only opaque ChaCha20-Poly1305 ciphertext by room tag.
 *
 * Run:
 *   node examples/javascript/cgnat_relay_demo.mjs
 */

import crypto from 'node:crypto';
import {
  NLTPClient,
  NLTPRelayHub,
  NLTPServer,
  generateNltpKey,
} from '../../js/src/index.js';

const key = generateNltpKey();

const hub = await NLTPRelayHub.listen({ host: '127.0.0.1', port: 0 });
const relayAddr = hub.address();

const shore = await NLTPServer.listen({
  host: '127.0.0.1',
  port: 0,
  psk: key,
  relayAddr,
  room: 'vessel-alpha-01',
  lossDropSim: 0.10,
});

const vessel = await NLTPClient.connect({
  host: '127.0.0.1',
  port: 9, // Unused in relay mode; traffic routes strictly via relayAddr + room
  psk: key,
  relayAddr,
  room: 'vessel-alpha-01',
  lossDropSim: 0.10,
});

try {
  const batch = crypto.randomBytes(64 * 1024);
  const [receipt, item] = await Promise.all([
    vessel.sendBytes(batch, { filename: 'cgnat_radar_64k.bin', timeoutMs: 30_000 }),
    shore.recv(30_000),
  ]);

  console.log(
    `CGNAT Relay 64 KB @ 10% loss: verified=${receipt.verifiedSha256}, ` +
    `bytes=${item.data.length}, hub_forwarded_pkts=${hub.relayedPackets}`
  );
} finally {
  await vessel.close();
  await shore.close();
  await hub.close();
}
