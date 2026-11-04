#!/usr/bin/env node
/**
 * Real-project example: shore control-room dashboard (Node.js, no dependencies).
 *
 * Receives NLTP traffic from vessels / robots / gateways (status JSON, sensor batches, files) and
 * serves a small web page listing each connected peer with an E-STOP button. The E-stop is an NLTP
 * reflex: it bypasses queued file traffic, is retransmitted until the peer acknowledges it, and the
 * page shows whether it was acknowledged and how long that took.
 *
 * Works with any NLTP peer: examples/python/05_vessel_shore_link.py (vessel role), the ESP32
 * gateway sketch, the Java/JS/C clients.
 *
 *   export NLTP_PSK="$(npx nltp-js gen-key)"           # same key on every peer
 *   node shore_dashboard.mjs --udp-port 9700 --http-port 8080 --save-dir ./inbox
 *   open http://127.0.0.1:8080
 *
 *   node shore_dashboard.mjs --demo        # self-test: starts a simulated vessel at 15% loss,
 *                                          # presses E-STOP through the HTTP API, exits 0 on success
 *
 * Safety: the HTTP side binds to 127.0.0.1 by default and has no login. Put it behind an
 * authenticated reverse proxy before exposing it, and never make NLTP the only stop path for
 * machinery: keep a hardware interlock / dead-man switch on the vessel.
 */
import http from 'node:http';
import { NLTPClient, NLTPServer, generateNltpKey } from '../../js/src/index.js';

function opt(name, def) {
  const i = process.argv.indexOf(name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}
const DEMO = process.argv.includes('--demo');

const PAGE = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>NLTP shore dashboard</title><style>
body{font:15px system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#1d2330}
table{border-collapse:collapse;width:100%;background:#fff}td,th{padding:8px;border-bottom:1px solid #e3e6eb;text-align:left}
button{background:#c62828;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer}
.ok{color:#2e7d32}.bad{color:#c62828}code{font-size:13px}</style></head><body>
<h1>NLTP shore dashboard</h1><p id="summary">loading…</p>
<table><thead><tr><th>Peer</th><th>Last seen</th><th>Last status</th><th>Files</th><th>E-stop</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
async function estop(key, btn) {
  btn.disabled = true; btn.textContent = 'sending…';
  const r = await (await fetch('/api/estop?peer=' + encodeURIComponent(key), { method: 'POST' })).json();
  btn.textContent = r.acked ? 'ACKED in ' + r.ms + ' ms' : 'NOT ACKED';
  btn.disabled = false;
}
async function refresh() {
  const s = await (await fetch('/api/status')).json();
  document.getElementById('summary').textContent = s.peers.length + ' peer(s), ' + s.items + ' items received';
  const tb = document.getElementById('rows'); tb.replaceChildren();
  for (const p of s.peers) {
    const tr = document.createElement('tr');
    for (const v of [p.key, ((Date.now() - p.lastSeenMs) / 1000).toFixed(1) + ' s ago', JSON.stringify(p.lastStatus), String(p.files)]) {
      const td = document.createElement('td'); const c = document.createElement('code');
      c.textContent = v; td.append(c); tr.append(td);            // textContent: peer data is untrusted
    }
    const td = document.createElement('td'); const b = document.createElement('button');
    b.textContent = p.estop ? p.estop : 'E-STOP'; b.onclick = () => estop(p.key, b);
    td.append(b); tr.append(td); tb.append(tr);
  }
}
refresh(); setInterval(refresh, 1000);
</script></body></html>`;

export async function startDashboard({ psk, udpPort = 9700, httpPort = 8080, httpHost = '127.0.0.1', saveDir = './inbox', lossDropSim = 0 }) {
  const srv = await NLTPServer.listen({ host: '0.0.0.0', port: udpPort, psk, saveDir, lossDropSim });
  const peers = new Map();
  let items = 0;
  let running = true;

  (async () => {
    while (running) {
      const item = await srv.recv(250);
      if (!item) continue;
      items += 1;
      const key = `${item.peerAddr.host}:${item.peerAddr.port}`;
      const p = peers.get(key) ?? { key, addr: item.peerAddr, lastSeenMs: 0, lastStatus: null, files: 0, estop: null };
      p.addr = item.peerAddr;
      p.lastSeenMs = Date.now();
      if (item.kind === 'json') {
        try { p.lastStatus = item.asJson(); } catch { p.lastStatus = '(invalid JSON)'; }
      } else if (item.kind === 'file' || item.kind === 'bytes') {
        if (item.sha256Verified) p.files += 1;
      }
      peers.set(key, p);
    }
  })();

  const web = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    const json = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
    if (req.method === 'GET' && url.pathname === '/') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(PAGE);
    } else if (req.method === 'GET' && url.pathname === '/api/status') {
      json(200, { items, peers: [...peers.values()].map(({ addr, ...rest }) => rest) });
    } else if (req.method === 'POST' && url.pathname === '/api/estop') {
      const p = peers.get(url.searchParams.get('peer') ?? '');
      if (!p) return json(404, { acked: false, error: 'unknown peer' });
      const t0 = performance.now();
      const seq = await srv.sendReflex(p.addr, 'ESTOP');
      const acked = await srv.waitReflexAck(seq, 3000);
      const ms = Math.round(performance.now() - t0);
      p.estop = acked ? `ACKED in ${ms} ms` : 'NOT ACKED';
      console.log(`[shore] E-STOP -> ${p.key}: ${p.estop}`);
      json(200, { acked, ms });
    } else {
      json(404, { error: 'not found' });
    }
  });
  await new Promise((r) => web.listen(httpPort, httpHost, r));

  return {
    srv,
    httpPort: web.address().port,
    udpPort: srv.address().port,
    async close() {
      running = false;
      web.close();
      await srv.close();
    },
  };
}

async function demo() {
  const psk = generateNltpKey();
  const loss = 0.15;
  const dash = await startDashboard({ psk, udpPort: 0, httpPort: 0, saveDir: null, lossDropSim: loss });
  console.log(`demo: dashboard on http://127.0.0.1:${dash.httpPort}, simulated vessel at ${loss * 100}% loss each way`);

  // Simulated vessel: status JSON every 300 ms, stops its thrusters when the E-stop reflex arrives.
  const vessel = await NLTPClient.connect({ host: '127.0.0.1', port: dash.udpPort, psk, lossDropSim: loss });
  let thrusters = true;
  let vesselRunning = true;
  const vesselRx = (async () => {
    while (vesselRunning) {
      const item = await vessel.recv(100);
      if (item?.kind === 'reflex' && item.asText() === 'ESTOP') {
        thrusters = false;
        console.log('[vessel] E-STOP received: thrusters disabled');
      }
    }
  })();
  const vesselTx = (async () => {
    for (let i = 0; vesselRunning; i++) {
      await vessel.sendJson({ seq: i, thrusters, battery_v: 12.4 }, { timeoutMs: 3000 });
      await new Promise((r) => setTimeout(r, 300));
    }
  })();

  const base = `http://127.0.0.1:${dash.httpPort}`;
  let status;
  for (let i = 0; i < 50; i++) {
    status = await (await fetch(`${base}/api/status`)).json();
    if (status.peers.length > 0) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  const page = await (await fetch(base)).text();
  const key = status.peers[0]?.key;
  const r = await (await fetch(`${base}/api/estop?peer=${encodeURIComponent(key)}`, { method: 'POST' })).json();
  // Wait for the vessel's own status report to confirm the stop (reports travel the lossy link too).
  let reported;
  for (let i = 0; i < 50; i++) {
    status = await (await fetch(`${base}/api/status`)).json();
    reported = status.peers[0]?.lastStatus;
    if (reported?.thrusters === false) break;
    await new Promise((res) => setTimeout(res, 100));
  }

  const ok = page.includes('E-STOP') && r.acked && !thrusters && reported?.thrusters === false;
  console.log(`result: page served=${page.includes('E-STOP')}, E-stop acked=${r.acked} (${r.ms} ms), ` +
    `vessel thrusters=${thrusters}, vessel's own status report thrusters=${reported?.thrusters}`);
  vesselRunning = false;
  await Promise.all([vesselRx, vesselTx]);
  await vessel.close();
  await dash.close();
  return ok ? 0 : 1;
}

async function main() {
  if (DEMO) return demo();
  const psk = process.env.NLTP_PSK;
  if (!psk) {
    console.error('Set NLTP_PSK (create one with `npx nltp-js gen-key`; the same key on every peer).');
    return 2;
  }
  const dash = await startDashboard({
    psk,
    udpPort: Number(opt('--udp-port', '9700')),
    httpPort: Number(opt('--http-port', '8080')),
    httpHost: opt('--http-host', '127.0.0.1'),
    saveDir: opt('--save-dir', './inbox'),
  });
  console.log(`NLTP on UDP ${dash.udpPort}; dashboard on http://127.0.0.1:${dash.httpPort}`);
  await new Promise(() => {});
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => process.exit(c), (e) => { console.error(e); process.exit(1); });
}
