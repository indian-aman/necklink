#!/usr/bin/env node
/**
 * NLTP v1.2 JavaScript CLI (`nltp` / `nltp-js`)
 *
 * Supports both user commands (`gen-key`, `stun`, `relay`, `serve`, `send-file`)
 * and cross-language interop driver commands (`send`, `recv`) matching `nltp_cli`.
 */

import fs from 'node:fs';
import path from 'node:path';
import {
  NLTPClient,
  NLTPRelayHub,
  NLTPServer,
  discoverWanEndpoint,
  generateNltpKey,
} from '../src/index.js';

function parseHostPort(spec) {
  if (!spec) return null;
  const idx = spec.lastIndexOf(':');
  if (idx < 0) throw new Error(`Expected HOST:PORT, got ${spec}`);
  return { host: spec.slice(0, idx), port: Number(spec.slice(idx + 1)) };
}

function getOpt(args, flag, def = null) {
  const idx = args.indexOf(flag);
  if (idx >= 0 && idx + 1 < args.length) return args[idx + 1];
  return def;
}

async function main() {
  const argv = process.argv.slice(2);
  const cmd = argv[0];

  if (!cmd || cmd === '--help' || cmd === '-h') {
    console.log(`NLTP v1.2 JavaScript CLI
Commands:
  nltp-js gen-key
  nltp-js stun [--show-ip]
  nltp-js relay [--host 0.0.0.0] [--port 9000] [--duration 0]
  nltp-js serve [--host 0.0.0.0] [--port 9000] [--psk KEY] [--save-dir ./inbox] [--relay IP:PORT --room NAME] [--max-items N]
  nltp-js send-file --file PATH [--host 127.0.0.1] [--port 9000] [--psk KEY] [--relay IP:PORT --room NAME]
  nltp-js send <host> <port> <file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]
  nltp-js recv <bind_ip> <port> <out_file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]`);
    return 0;
  }

  if (cmd === 'gen-key') {
    console.log(generateNltpKey());
    return 0;
  }

  if (cmd === 'stun') {
    const showIp = argv.includes('--show-ip');
    const res = await discoverWanEndpoint();
    const ipStr = showIp ? res.publicIp : 'xxx.xxx.xxx.xxx';
    console.log(JSON.stringify({ ...res, publicIp: ipStr }, null, 2));
    return 0;
  }

  if (cmd === 'relay') {
    const host = getOpt(argv, '--host', '0.0.0.0');
    const port = Number(getOpt(argv, '--port', '9000'));
    const duration = Number(getOpt(argv, '--duration', '0'));
    const hub = await NLTPRelayHub.listen({ host, port });
    const a = hub.address();
    console.log(`NLTPRelayHub listening on ${a.host}:${a.port}`);
    if (duration > 0) {
      await new Promise((r) => setTimeout(r, duration * 1000));
      await hub.close();
      return 0;
    }
    await new Promise(() => {});
  }

  if (cmd === 'serve') {
    const host = getOpt(argv, '--host', '0.0.0.0');
    const port = Number(getOpt(argv, '--port', '9000'));
    const psk = getOpt(argv, '--psk', null);
    const saveDir = getOpt(argv, '--save-dir', './inbox');
    const relaySpec = getOpt(argv, '--relay', null);
    const room = getOpt(argv, '--room', null);
    const loss = Number(getOpt(argv, '--loss', '0'));
    const maxItems = Number(getOpt(argv, '--max-items', '0'));
    const srv = await NLTPServer.listen({
      host,
      port,
      psk,
      saveDir,
      relayAddr: parseHostPort(relaySpec),
      room,
      lossDropSim: loss,
    });
    const a = srv.address();
    console.log(`NLTP JS Server listening on ${a.host}:${a.port}`);
    let count = 0;
    try {
      while (true) {
        const item = await srv.recv(1000);
        if (item) {
          count += 1;
          console.log(`RECEIVED kind=${item.kind} bytes=${item.data.length} sha256_ok=${item.sha256Verified} file=${item.filename}`);
          if (maxItems > 0 && count >= maxItems) break;
        }
      }
    } finally {
      await srv.close();
    }
    return 0;
  }

  if (cmd === 'send-file') {
    const host = getOpt(argv, '--host', '127.0.0.1');
    const port = Number(getOpt(argv, '--port', '9000'));
    const filepath = getOpt(argv, '--file', null);
    if (!filepath) throw new Error('--file is required');
    const psk = getOpt(argv, '--psk', null);
    const relaySpec = getOpt(argv, '--relay', null);
    const room = getOpt(argv, '--room', null);
    const loss = Number(getOpt(argv, '--loss', '0'));
    const cli = await NLTPClient.connect({
      host,
      port,
      psk,
      relayAddr: parseHostPort(relaySpec),
      room,
      lossDropSim: loss,
    });
    try {
      const receipt = await cli.sendFile(filepath, { timeoutMs: 90000 });
      console.log(JSON.stringify(receipt, null, 2));
      return receipt.verifiedSha256 ? 0 : 1;
    } finally {
      await cli.close();
    }
  }

  if (cmd === 'send-reflex') {
    const host = argv[1];
    const port = Number(argv[2]);
    const text = argv[3] ?? 'ESTOP';
    const psk = getOpt(argv, '--psk', null);
    const cli = await NLTPClient.connect({ host, port, psk });
    try {
      await cli.sendReflex(text);
      await new Promise((r) => setTimeout(r, 30));
      console.log(`RESULT ${JSON.stringify({ ok: true, reflex: text })}`);
      return 0;
    } finally {
      await cli.close();
    }
  }

  // Interop driver mode matching `csrc/tools/nltp_cli.c`:
  //   send <host> <port> <file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]
  //   recv <bind_ip> <port> <out_file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]
  if (cmd === 'send') {
    const host = argv[1];
    const port = Number(argv[2]);
    const filepath = argv[3];
    const lossPct = Number(getOpt(argv, '--loss', '0'));
    const psk = getOpt(argv, '--psk', null);
    const relaySpec = getOpt(argv, '--relay', null);
    const room = getOpt(argv, '--room', null);
    const maxKbps = Number(getOpt(argv, '--max-kbps', '50000'));
    const cli = await NLTPClient.connect({
      host,
      port,
      psk,
      relayAddr: parseHostPort(relaySpec),
      room,
      maxTxKbps: maxKbps,
      lossDropSim: lossPct / 100.0,
    });
    try {
      const receipt = await cli.sendFile(filepath, { timeoutMs: 90000 });
      console.log(`RESULT ${JSON.stringify({
        ok: receipt.verifiedSha256,
        bytes: receipt.bytes,
        elapsed_s: receipt.elapsedS,
        goodput_kbps: receipt.goodputKbps,
        encrypted: receipt.encrypted,
      })}`);
      return receipt.verifiedSha256 ? 0 : 1;
    } finally {
      await cli.close();
    }
  }

  if (cmd === 'recv') {
    const bindHost = argv[1];
    const bindPort = Number(argv[2]);
    const outFile = argv[3];
    const lossPct = Number(getOpt(argv, '--loss', '0'));
    const psk = getOpt(argv, '--psk', null);
    const relaySpec = getOpt(argv, '--relay', null);
    const room = getOpt(argv, '--room', null);
    const maxKbps = Number(getOpt(argv, '--max-kbps', '50000'));
    const waitStdin = argv.includes('--wait-stdin');
    const srv = await NLTPServer.listen({
      host: bindHost,
      port: bindPort,
      psk,
      relayAddr: parseHostPort(relaySpec),
      room,
      maxTxKbps: maxKbps,
      lossDropSim: lossPct / 100.0,
    });
    console.log('READY');
    try {
      const t0 = performance.now();
      const item = await srv.recv(90000);
      if (!item || !item.sha256Verified) {
        console.log(`RESULT ${JSON.stringify({ ok: false })}`);
        return 1;
      }
      fs.mkdirSync(path.dirname(path.resolve(outFile)), { recursive: true });
      fs.writeFileSync(outFile, item.data);
      const elapsedS = Number(((performance.now() - t0) / 1000.0).toFixed(4));
      if (waitStdin) {
        await new Promise((resolve) => {
          process.stdin.on('end', resolve);
          process.stdin.on('close', resolve);
          process.stdin.on('error', resolve);
          process.stdin.resume();
        });
      } else {
        await new Promise((r) => setTimeout(r, 250));
      }
      console.log(`RESULT ${JSON.stringify({
        ok: true,
        bytes: item.data.length,
        sha256: item.sha256Hex,
        elapsed_s: elapsedS,
      })}`);
      return 0;
    } finally {
      await srv.close();
    }
  }

  throw new Error(`Unknown command: ${cmd}`);
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
