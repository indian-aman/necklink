# NLTP v1.3.2 — JavaScript & TypeScript SDK (`nltp`)

Zero-dependency **JavaScript / TypeScript** implementation of the **NeckLink Transport Protocol (`NLTP v1.3.2`)**, byte-for-byte wire-compatible with the **Python**, **ANSI C99**, **Java 11+**, and **Arduino / `ESP32` / `ESP8266`** stacks.

- **Zero External Dependencies**: Pure ES2022 (`ESM` + `CommonJS` + `.d.ts` TypeScript declarations) — tested on **Node.js 18+** (`Node.js 20` in CI). The `nltp/core` entrypoint uses only standard `Uint8Array` / `DataView` with zero `node:*` imports for custom byte callbacks *(browser, Deno, and Bun runtimes are not yet tested in CI)*.
- **Full NLTP v1.3.2 Wire Parity**:
  - Systematic `GF(2^8)` Cauchy forward error correction (`0x11D` primitive polynomial, `8`-shard generations, incremental Gauss-Jordan decoding).
  - Pure-JS + `node:crypto` `ChaCha20-Poly1305` (256-bit AEAD, `RFC 8439`) with `20 B` AAD header and `RFC 6479` 64-packet sliding anti-replay window.
  - Exact `deriveAeadKey()` parity with Python & C99 (44-char URL-safe Base64 raw keys and `SHA-256("NLTP-v1-PASSPHRASE:" + psk)` passphrases).
  - Out-of-line urgent reflex channel (`sendReflex`) with zero head-of-line blocking behind bulk file transfers.
  - 1-RTT packet-train bottleneck pacing (`DeliveryRateController` + `TokenBucketPacer`).
  - Built-in `RFC 5389` STUN discovery (`discoverWanEndpoint`), bidirectional UDP hole-punching (`punchP2p`), seamless IP roaming (`rebindSocket`), and zero-knowledge 1-port CGNAT relay (`NLTPRelayHub`).

---

## Quick Start (Node.js UDP)

```javascript
import { NLTPServer, NLTPClient, generateNltpKey } from './src/index.js';

const key = generateNltpKey(); // 256-bit ChaCha20-Poly1305 key

const srv = await NLTPServer.listen({
  host: '0.0.0.0',
  port: 9000,
  psk: key,
  saveDir: './inbox',
});

const cli = await NLTPClient.connect({
  host: '127.0.0.1',
  port: 9000,
  psk: key,
  maxTxKbps: 10_000,
});

// 1. Out-of-line reflex command (bypasses bulk file queue)
cli.sendReflex('ESTOP_CHECK');
const reflex = await srv.recv(2000);
console.log('Reflex:', new TextDecoder().decode(reflex.data));

// 2. Cauchy-FEC + ChaCha20-Poly1305 + SHA-256 verified file transfer
const receipt = await cli.sendBytes(new Uint8Array(65536).fill(0x42), {
  filename: 'sensor_batch.bin',
});
console.log('Verified SHA-256:', receipt.verifiedSha256, 'Goodput kbps:', receipt.goodputKbps);

await cli.close();
await srv.close();
```

### CommonJS (`require`)

```javascript
const { NLTPServer, NLTPClient, generateNltpKey } = require('./src/index.cjs');
```

---

## Transport-Agnostic Core (`nltp/core` — Custom Byte Callbacks)

Import `nltp/core` (`./src/endpoint.js` + `./src/wire.js`) without `node:dgram` *(tested on Node.js; browser/WebRTC/WebTransport runtimes not yet tested in CI)*:

```javascript
import { NLTPEndpoint, generateNltpKey } from './src/endpoint.js';

const key = generateNltpKey();
const ep = new NLTPEndpoint({
  psk: key,
  maxTxKbps: 5000,
  sendPacketFn: (frameBytes, peer) => myDataChannel.send(frameBytes),
});

myDataChannel.onmessage = (ev) => {
  ep.onDatagram(new Uint8Array(ev.data), 'peer');
  let item;
  while ((item = ep.popReceived()) !== null) {
    console.log('Received:', item.kind, item.filename, item.sha256Verified);
  }
};

// Drive pacing & FEC repairs every ~5 ms:
setInterval(() => ep.step(), 5);
```

---

## CLI (`bin/nltp.mjs`)

```bash
node js/bin/nltp.mjs gen-key
node js/bin/nltp.mjs stun
node js/bin/nltp.mjs relay --host 0.0.0.0 --port 9000
node js/bin/nltp.mjs serve --host 0.0.0.0 --port 9000 --psk "<KEY>" --save-dir ./inbox
node js/bin/nltp.mjs send-file --host 127.0.0.1 --port 9000 --psk "<KEY>" --file firmware.bin
```

---

## Run Tests

```bash
node --test js/test/nltp.test.mjs
```
