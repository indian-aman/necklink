import { NLTPEndpoint } from './engine/endpoint.js';
import {
  PT_CRYPTIC_PARITY,
  PT_DATA_SHARD,
  PT_PROPRIOCEPTIVE_ACK,
  PT_REFLEX_ACK,
  PT_REFLEX_MSG,
  PT_STREAM_FIN_ACK,
  PT_STREAM_META,
  randomBytes,
  utf8Decode,
  utf8Encode,
} from './engine/wire.js';

const PT_HELLO = 0x0b;
const PT_HELLO_ACK = 0x0c;

// ---------------------------------------------------------------------------
// UI Elements
// ---------------------------------------------------------------------------
const menuBtn = document.getElementById('menu-btn');
const navLinks = document.getElementById('nav-links');
const lossSlider = document.getElementById('loss-slider');
const lossVal = document.getElementById('loss-val');
const rttSlider = document.getElementById('rtt-slider');
const rttVal = document.getElementById('rtt-val');
const payloadSelect = document.getElementById('payload-select');
const shardSelect = document.getElementById('shard-select');
const chkEncrypt = document.getElementById('chk-encrypt');
const chkLz4 = document.getElementById('chk-lz4');

const btnSendStream = document.getElementById('btn-send-stream');
const btnSendReflex = document.getElementById('btn-send-reflex');
const btnTamper = document.getElementById('btn-tamper');

const labLed = document.getElementById('lab-led');
const labStatusText = document.getElementById('lab-status-text');
const roSize = document.getElementById('ro-size');
const roPkts = document.getElementById('ro-pkts');
const roParity = document.getElementById('ro-parity');
const roReflex = document.getElementById('ro-reflex');
const roSha = document.getElementById('ro-sha');
const cmpNltp = document.getElementById('cmp-nltp');
const cmpEspnow = document.getElementById('cmp-espnow');
const cmpTcp = document.getElementById('cmp-tcp');
const hexPreview = document.getElementById('hex-preview');
const shaFull = document.getElementById('sha-full');
const canvas = document.getElementById('wire-canvas');
const ctx = canvas.getContext('2d');

// ---------------------------------------------------------------------------
// Mobile Navigation & Copy Buttons & Tabs
// ---------------------------------------------------------------------------
if (menuBtn && navLinks) {
  menuBtn.addEventListener('click', () => {
    const expanded = menuBtn.getAttribute('aria-expanded') === 'true';
    menuBtn.setAttribute('aria-expanded', String(!expanded));
    navLinks.classList.toggle('open', !expanded);
  });
  navLinks.querySelectorAll('a').forEach((a) => {
    a.addEventListener('click', () => {
      menuBtn.setAttribute('aria-expanded', 'false');
      navLinks.classList.remove('open');
    });
  });
}

function wireCopyButtons() {
  document.querySelectorAll('[data-copy], [data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const direct = btn.getAttribute('data-copy');
      const targetId = btn.getAttribute('data-copy-target');
      const text = direct || (targetId ? document.getElementById(targetId)?.textContent : '') || '';
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text.trim());
        const prev = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => {
          btn.textContent = prev;
        }, 1400);
      } catch {
        // Clipboard unavailable in restricted context
      }
    });
  });
}
wireCopyButtons();

function wireTabs(attrName, panelPrefix) {
  const buttons = document.querySelectorAll(`[${attrName}]`);
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.getAttribute(attrName);
      buttons.forEach((b) => {
        const active = b === btn;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', String(active));
      });
      document.querySelectorAll(`[id^="${panelPrefix}-"]`).forEach((panel) => {
        panel.hidden = panel.id !== `${panelPrefix}-${key}`;
      });
    });
  });
}
wireTabs('data-bench-tab', 'bench');
wireTabs('data-sdk-tab', 'sdk');

// ---------------------------------------------------------------------------
// Payload Builders
// ---------------------------------------------------------------------------
function buildPayload(kind) {
  if (kind === 'jsonl') {
    const lines = [];
    for (let i = 0; i < 155; i++) {
      const pm25 = (13.4 + (i % 9) * 0.3).toFixed(1);
      const h2s = (2.1 + (i % 5) * 0.2).toFixed(1);
      const nh3 = (6.8 + (i % 7) * 0.25).toFixed(2);
      lines.push(
        `{"seq":${1000 + i},"node":"aqms-baroda-01","pm25_ugm3":${pm25},"h2s_ppb":${h2s},"nh3_ppb":${nh3},"batt_v":12.84,"rssi":-67}`
      );
    }
    return { bytes: utf8Encode(lines.join('\n') + '\n'), filename: 'aqms_telemetry.jsonl' };
  }
  if (kind === 'nmea') {
    const rows = ['ts_ms,vessel,lat,lon,hs_m,tp_s,depth_m,sonde_do_mgl'];
    for (let i = 0; i < 130; i++) {
      rows.push(
        `${1731600000 + i * 2},mariner-usv-02,18.942${(i % 9)},72.825${(i % 7)},2.14,9.6,${(24.2 + (i % 11) * 0.4).toFixed(1)},7.82`
      );
    }
    return { bytes: utf8Encode(rows.join('\n') + '\n'), filename: 'usv_sonde_batch.csv' };
  }
  return { bytes: randomBytes(8192), filename: 'ota_firmware_chunk.bin' };
}

// ---------------------------------------------------------------------------
// Live NLTP v1.6.0 Engine State & Simulated Lossy Radio Channel
// ---------------------------------------------------------------------------
let sharedKey = randomBytes(32);
let senderEp = null;
let receiverEp = null;
let currentShardBytes = 960;
let currentEncrypted = true;

const stats = {
  txFrames: 0,
  droppedFrames: 0,
  parityRx: 0,
  dataRx: 0,
  reflexAcks: 0,
  spoofBlocked: 0,
  lastReflexMs: null,
  activeXfer: null,
  shieldFlash: 0,
  reflexFlash: 0,
};

const particles = [];

function formatHexPreview(u8) {
  const n = Math.min(22, u8.length);
  const hex = [];
  for (let i = 0; i < n; i++) {
    hex.push(u8[i].toString(16).padStart(2, '0'));
  }
  return `${hex.join(' ')}${u8.length > n ? ` … (${u8.length} B)` : ` (${u8.length} B)`}`;
}

function classifyPacket(raw) {
  const pktType = raw.length >= 3 ? raw[2] & 0x0f : 0;
  if (pktType === PT_HELLO || pktType === PT_HELLO_ACK) return { label: 'X25519', color: '#f5dda4', lane: 'fwd', radius: 4.5 };
  if (pktType === PT_STREAM_META) return { label: 'META', color: '#f8fafc', lane: 'fwd', radius: 4.5 };
  if (pktType === PT_DATA_SHARD) return { label: 'DATA', color: '#5fb490', lane: 'fwd', radius: 4.2 };
  if (pktType === PT_CRYPTIC_PARITY) return { label: 'PARITY', color: '#e4b45c', lane: 'fwd', radius: 4.8 };
  if (pktType === PT_REFLEX_MSG) return { label: 'REFLEX', color: '#ffffff', lane: 'express', radius: 5.4 };
  if (pktType === PT_PROPRIOCEPTIVE_ACK) return { label: 'ACK', color: '#94a3b8', lane: 'rev', radius: 3.4 };
  if (pktType === PT_STREAM_FIN_ACK) return { label: 'FIN_ACK', color: '#5fb490', lane: 'rev', radius: 4.6 };
  if (pktType === PT_REFLEX_ACK) return { label: 'R_ACK', color: '#e4b45c', lane: 'rev', radius: 4.2 };
  return { label: 'NL', color: '#94a3b8', lane: 'fwd', radius: 3.6 };
}

function transmitOverVirtualRadio(rawBytes, fromSender) {
  const copy = new Uint8Array(rawBytes);
  const lossPct = Number(lossSlider.value);
  const baseRtt = Number(rttSlider.value);
  const oneWayMs = Math.max(3, baseRtt * 0.5 + (Math.random() - 0.5) * Math.min(12, baseRtt * 0.25));
  const pktType = copy[2] & 0x0f;

  if (fromSender) {
    stats.txFrames += 1;
    hexPreview.textContent = formatHexPreview(copy);
  }

  // Symmetric packet drop check across channel
  const dropped = lossPct > 0 && Math.random() * 100 < lossPct;
  if (dropped && fromSender) {
    stats.droppedFrames += 1;
  }

  const info = classifyPacket(copy);
  particles.push({
    dir: fromSender ? 1 : -1,
    lane: info.lane,
    color: info.color,
    label: info.label,
    radius: info.radius,
    dropped,
    dropAt: 0.42 + Math.random() * 0.18,
    born: performance.now(),
    dur: Math.max(190, Math.min(540, oneWayMs * 5.2)),
  });

  if (dropped) {
    updateReadouts();
    return;
  }

  setTimeout(() => {
    if (!senderEp || !receiverEp) return;
    if (fromSender) {
      if (pktType === PT_CRYPTIC_PARITY) stats.parityRx += 1;
      if (pktType === PT_DATA_SHARD) stats.dataRx += 1;
      receiverEp.onDatagram(copy, 'edge-node');
      drainReceiverInbox();
    } else {
      senderEp.onDatagram(copy, 'shore-hub');
    }
    updateReadouts();
  }, oneWayMs);
}

function rebuildEndpoints(forceReset = false) {
  const wantShard = Number(shardSelect.value);
  const wantEnc = chkEncrypt.checked;
  if (!forceReset && senderEp && receiverEp && wantShard === currentShardBytes && wantEnc === currentEncrypted) {
    return;
  }
  currentShardBytes = wantShard;
  currentEncrypted = wantEnc;
  sharedKey = randomBytes(32);
  const psk = wantEnc ? sharedKey : null;

  senderEp = new NLTPEndpoint({
    shardBytes: wantShard,
    psk,
    maxTxKbps: 28000,
    sendPacketFn: (pkt) => transmitOverVirtualRadio(pkt, true),
  });

  receiverEp = new NLTPEndpoint({
    shardBytes: wantShard,
    psk,
    maxTxKbps: 28000,
    sendPacketFn: (pkt) => transmitOverVirtualRadio(pkt, false),
  });
}

function drainReceiverInbox() {
  if (!receiverEp) return;
  let item = receiverEp.popReceived();
  while (item !== null) {
    if (item.kind === 'reflex') {
      stats.reflexFlash = 1.0;
      const text = utf8Decode(item.data);
      labStatusText.textContent = `PRIORITY REFLEX DELIVERED OUT-OF-BAND: "${text}" (ZERO HOL BLOCKING)`;
    } else {
      shaFull.textContent = `${item.sha256Hex} (${item.sha256Verified ? '100% MATCH' : 'MISMATCH'})`;
      roSha.textContent = item.sha256Verified ? 'VERIFIED ✓' : 'FAILED';
      roSha.style.color = item.sha256Verified ? '#5fb490' : '#d87a68';
    }
    item = receiverEp.popReceived();
  }
}

function updateCompetitorCards() {
  const loss = Number(lossSlider.value);
  const shard = Number(shardSelect.value);
  if (loss === 0) {
    cmpEspnow.innerHTML = shard === 192
      ? 'At <code>0%</code> loss: delivers <code>250 B</code> v1 frames cleanly (no built-in file hash or PFS).'
      : 'At <code>0%</code> loss: fast L2 delivery (~15 Mbps), capped at 6–17 encrypted peers.';
    cmpTcp.innerHTML = 'At <code>0%</code> clean link: kernel <code>TCP BBR</code> is ~12–15% faster on raw uncompressed binary.';
  } else if (loss < 10) {
    cmpEspnow.innerHTML = `At <code>${loss}%</code> loss: near L2 retry border; a single unrecovered chunk corrupts the whole file.`;
    cmpTcp.innerHTML = `At <code>${loss}%</code> loss: congestion window halves on drops; MQTT risks head-of-line stalls.`;
  } else {
    cmpEspnow.innerHTML = `At <code>${loss}%</code> loss: <strong style="color:#d87a68;display:inline">0% multi-KB file delivery</strong> on hardware bench (missing chunks).`;
    cmpTcp.innerHTML = `At <code>${loss}%</code> loss: multi-RTT retransmit stalls (<code>2.5×–6.9×</code> slower than NLTP + LZ4).`;
  }
}

function updateReadouts() {
  roPkts.textContent = `${stats.txFrames} / ${stats.droppedFrames} drop`;
  roParity.textContent = `${stats.parityRx} Cauchy shards`;
  const blocked = receiverEp ? receiverEp.authDropsRx : 0;
  const rAck = senderEp ? senderEp.reflexAcksRx : 0;
  roReflex.textContent = `${rAck} ack · ${blocked} blocked`;
}

// Step protocol timers every 12 ms
setInterval(() => {
  if (senderEp) senderEp.step();
  if (receiverEp) {
    receiverEp.step();
    drainReceiverInbox();
  }
  updateReadouts();
}, 12);

// ---------------------------------------------------------------------------
// Interactive Actions: Send Stream, Fire Priority Reflex, Spoof Attack
// ---------------------------------------------------------------------------
async function launchStream() {
  rebuildEndpoints(false);
  stats.txFrames = 0;
  stats.droppedFrames = 0;
  stats.parityRx = 0;
  stats.dataRx = 0;
  roSha.textContent = 'STREAMING…';
  roSha.style.color = '#e4b45c';

  const { bytes, filename } = buildPayload(payloadSelect.value);
  const useLz4 = chkLz4.checked;
  const loss = Number(lossSlider.value);

  const t0 = performance.now();
  const xfer = senderEp.startTransfer('shore-hub', bytes, {
    kind: 'file',
    filename,
    compress: useLz4,
  });
  stats.activeXfer = xfer;

  const ratioStr = xfer.compressed
    ? `${bytes.length.toLocaleString()} B → ${xfer.wirePayloadBytes.toLocaleString()} B (${(bytes.length / xfer.wirePayloadBytes).toFixed(1)}× LZ4)`
    : `${bytes.length.toLocaleString()} B (raw auto-bypass)`;
  roSize.textContent = ratioStr;
  labStatusText.textContent = `TRANSMITTING "${filename}" (${xfer.generations.length} gen${xfer.generations.length > 1 ? 's' : ''}) @ ${loss}% RF LOSS…`;
  labLed.style.background = '#e4b45c';

  const ok = await xfer.donePromise;
  const elapsedMs = Math.round(performance.now() - t0);
  labLed.style.background = ok ? '#5fb490' : '#d87a68';
  labStatusText.textContent = ok
    ? `STREAM VERIFIED IN ${elapsedMs} ms · SHA-256 100% INTACT (${stats.droppedFrames} DROPPED FRAMES HEALED BY GF(2^8) CAUCHY FEC)`
    : 'TRANSFER FAILED';
  cmpNltp.innerHTML = ok
    ? `<strong style="color:#5fb490;display:inline">100% SHA-256 Verified</strong> in <code>${elapsedMs} ms</code> (${xfer.compressed ? `${(bytes.length / xfer.wirePayloadBytes).toFixed(1)}× LZ4 + ` : ''}${stats.parityRx} Cauchy parity shards).`
    : 'Transfer incomplete.';
  updateReadouts();
}

function firePriorityReflex() {
  rebuildEndpoints(false);
  const t0 = performance.now();
  const seq = senderEp.sendReflex('shore-hub', 'E_STOP_ACTUATOR');
  const poll = setInterval(() => {
    if (senderEp && senderEp.reflexAcked(seq)) {
      clearInterval(poll);
      const dt = Math.round(performance.now() - t0);
      cmpNltp.innerHTML = `Priority <code>PT_REFLEX_MSG #${seq}</code> confirmed via <code>PT_REFLEX_ACK</code> in <code>${dt} ms</code> (0 HOL blocking).`;
      updateReadouts();
    }
  }, 10);
  setTimeout(() => clearInterval(poll), 5000);
}

function injectSpoofAttack() {
  if (!chkEncrypt.checked) {
    chkEncrypt.checked = true;
    rebuildEndpoints(true);
  } else {
    rebuildEndpoints(false);
  }
  const forged = randomBytes(68);
  forged[0] = 0x4e; // 'N'
  forged[1] = 0x4c; // 'L'
  forged[2] = 0x21; // version 2 | PT_DATA_SHARD
  forged[3] = 0x80; // FLAG_ENCRYPTED with invalid Poly1305 tag
  hexPreview.textContent = `FORGED: ${formatHexPreview(forged)}`;

  particles.push({
    dir: 1,
    lane: 'spoof',
    color: '#d87a68',
    label: 'SPOOF',
    radius: 5.2,
    dropped: false,
    dropAt: 1.0,
    born: performance.now(),
    dur: 260,
  });

  setTimeout(() => {
    if (!receiverEp) return;
    receiverEp.onDatagram(forged, 'attacker-mac');
    stats.shieldFlash = 1.0;
    labStatusText.textContent = `ADVERSARIAL PACKET REJECTED PRE-DECODE · Poly1305 Tag Invalid (authDropsRx = ${receiverEp.authDropsRx})`;
    updateReadouts();
  }, 250);
}

btnSendStream.addEventListener('click', launchStream);
btnSendReflex.addEventListener('click', firePriorityReflex);
btnTamper.addEventListener('click', injectSpoofAttack);

lossSlider.addEventListener('input', () => {
  lossVal.textContent = `${lossSlider.value}%`;
  updateCompetitorCards();
});
rttSlider.addEventListener('input', () => {
  rttVal.textContent = `${rttSlider.value} ms`;
});
shardSelect.addEventListener('change', () => {
  rebuildEndpoints(true);
  updateCompetitorCards();
});
chkEncrypt.addEventListener('change', () => {
  rebuildEndpoints(true);
});

const PRESETS = {
  clean: { loss: 0, rtt: 14, shard: '960', enc: true, lz4: true },
  cgnat: { loss: 12, rtt: 40, shard: '960', enc: true, lz4: true },
  espnow: { loss: 25, rtt: 18, shard: '192', enc: true, lz4: true },
  storm: { loss: 35, rtt: 70, shard: '960', enc: true, lz4: true },
};

document.querySelectorAll('[data-preset]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const key = btn.getAttribute('data-preset');
    const p = PRESETS[key];
    if (!p) return;
    document.querySelectorAll('[data-preset]').forEach((b) => b.classList.toggle('active', b === btn));
    lossSlider.value = String(p.loss);
    lossVal.textContent = `${p.loss}%`;
    rttSlider.value = String(p.rtt);
    rttVal.textContent = `${p.rtt} ms`;
    shardSelect.value = p.shard;
    chkEncrypt.checked = p.enc;
    chkLz4.checked = p.lz4;
    rebuildEndpoints(true);
    updateCompetitorCards();
    launchStream();
  });
});

// ---------------------------------------------------------------------------
// 60 FPS Hardware Canvas Renderer
// ---------------------------------------------------------------------------
function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(320, Math.floor(rect.width * dpr));
  canvas.height = Math.max(180, Math.floor(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resizeCanvas);
resizeCanvas();

function drawRoundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

function renderCanvas(now) {
  const w = canvas.clientWidth || 720;
  const h = canvas.clientHeight || 236;
  ctx.clearRect(0, 0, w, h);

  // Subtle oscilloscope grid
  ctx.strokeStyle = 'rgba(255,255,255,0.035)';
  ctx.lineWidth = 1;
  for (let gx = 40; gx < w; gx += 48) {
    ctx.beginPath();
    ctx.moveTo(gx, 0);
    ctx.lineTo(gx, h);
    ctx.stroke();
  }
  for (let gy = 36; gy < h; gy += 42) {
    ctx.beginPath();
    ctx.moveTo(0, gy);
    ctx.lineTo(w, gy);
    ctx.stroke();
  }

  const leftX = 96;
  const rightX = w - 96;
  const yExpress = 56;
  const yFwd = 114;
  const yRev = 172;

  // Channel conduits
  ctx.setLineDash([5, 6]);
  ctx.strokeStyle = 'rgba(228, 180, 92, 0.25)';
  ctx.beginPath();
  ctx.moveTo(leftX + 42, yExpress);
  ctx.lineTo(rightX - 42, yExpress);
  ctx.stroke();

  ctx.strokeStyle = 'rgba(95, 180, 144, 0.28)';
  ctx.beginPath();
  ctx.moveTo(leftX + 42, yFwd);
  ctx.lineTo(rightX - 42, yFwd);
  ctx.stroke();

  ctx.strokeStyle = 'rgba(148, 163, 184, 0.22)';
  ctx.beginPath();
  ctx.moveTo(leftX + 42, yRev);
  ctx.lineTo(rightX - 42, yRev);
  ctx.stroke();
  ctx.setLineDash([]);

  // Lane labels
  ctx.font = '600 10px "JetBrains Mono", monospace';
  ctx.fillStyle = 'rgba(228,180,92,0.75)';
  ctx.textAlign = 'center';
  ctx.fillText('OUT-OF-BAND PRIORITY REFLEX LANE (0 HOL BLOCKING)', w * 0.5, yExpress - 12);

  const loss = Number(lossSlider.value);
  ctx.fillStyle = loss > 0 ? 'rgba(216,122,104,0.85)' : 'rgba(148,163,184,0.7)';
  ctx.fillText(
    `BULK GF(2^8) CAUCHY FEC STREAM (${currentShardBytes} B SHARDS · ${loss}% RF LOSS)`,
    w * 0.5,
    yFwd - 13
  );
  ctx.fillStyle = 'rgba(148,163,184,0.62)';
  ctx.fillText('1-RTT PROPRIOCEPTIVE DOF ACKs & SHA-256 FIN_ACK', w * 0.5, yRev + 22);

  // Endpoint Node Cards
  drawRoundRect(16, 42, 118, h - 84, 10);
  ctx.fillStyle = '#0e1118';
  ctx.fill();
  ctx.strokeStyle = 'rgba(228, 180, 92, 0.48)';
  ctx.lineWidth = 1.3;
  ctx.stroke();

  ctx.fillStyle = '#e4b45c';
  ctx.font = '700 10px "JetBrains Mono", monospace';
  ctx.textAlign = 'left';
  ctx.fillText('EDGE TX NODE', 28, 68);
  ctx.fillStyle = '#f8fafc';
  ctx.font = '700 12px "Inter", sans-serif';
  ctx.fillText(currentShardBytes === 192 ? 'ESP8266 v1' : 'ESP32 / Python', 28, 88);
  ctx.fillStyle = '#8e95a4';
  ctx.font = '500 10px "JetBrains Mono", monospace';
  ctx.fillText(currentEncrypted ? 'X25519 + AEAD' : 'CRC-32 Plain', 28, 112);
  ctx.fillText(chkLz4.checked ? 'LZ4: ENABLED' : 'LZ4: OFF', 28, 128);
  ctx.fillText(`MTU: ${currentShardBytes === 192 ? '242 B' : '1,010 B'}`, 28, 144);

  drawRoundRect(w - 134, 42, 118, h - 84, 10);
  ctx.fillStyle = '#0e1118';
  ctx.fill();
  ctx.strokeStyle = stats.shieldFlash > 0.05
    ? `rgba(216, 122, 104, ${0.5 + 0.5 * stats.shieldFlash})`
    : 'rgba(95, 180, 144, 0.48)';
  ctx.lineWidth = stats.shieldFlash > 0.05 ? 2.2 : 1.3;
  ctx.stroke();

  ctx.fillStyle = '#5fb490';
  ctx.font = '700 10px "JetBrains Mono", monospace';
  ctx.fillText('SHORE / CLOUD', w - 122, 68);
  ctx.fillStyle = '#f8fafc';
  ctx.font = '700 12px "Inter", sans-serif';
  ctx.fillText('NLTP Receiver', w - 122, 88);
  ctx.fillStyle = '#8e95a4';
  ctx.font = '500 10px "JetBrains Mono", monospace';
  ctx.fillText('Gauss-Jordan', w - 122, 112);
  ctx.fillText('512-Pkt Replay', w - 122, 128);
  ctx.fillStyle = stats.shieldFlash > 0.05 ? '#f3b5a8' : '#5fb490';
  ctx.fillText(stats.shieldFlash > 0.05 ? 'SPOOF BLOCKED!' : 'SHA-256 Gate', w - 122, 144);

  stats.shieldFlash *= 0.93;
  stats.reflexFlash *= 0.93;

  // Draw flying "NL" wire packets
  const startX = 138;
  const endX = w - 138;
  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i];
    const t = (now - p.born) / p.dur;
    if (t >= 1.18) {
      particles.splice(i, 1);
      continue;
    }

    const prog = Math.min(1, t);
    const x = p.dir === 1 ? startX + (endX - startX) * prog : endX - (endX - startX) * prog;
    const y = p.lane === 'express' ? yExpress : p.lane === 'rev' ? yRev : yFwd;

    if (p.dropped && prog >= p.dropAt) {
      const fade = Math.max(0, 1 - (t - p.dropAt) * 2.2);
      if (fade <= 0.02) {
        particles.splice(i, 1);
        continue;
      }
      const dx = startX + (endX - startX) * p.dropAt;
      ctx.save();
      ctx.globalAlpha = fade;
      ctx.strokeStyle = '#d87a68';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(dx - 5, y - 5);
      ctx.lineTo(dx + 5, y + 5);
      ctx.moveTo(dx + 5, y - 5);
      ctx.lineTo(dx - 5, y + 5);
      ctx.stroke();
      ctx.restore();
      continue;
    }

    if (t > 1) continue;

    ctx.beginPath();
    ctx.arc(x, y, p.radius, 0, Math.PI * 2);
    ctx.fillStyle = p.color;
    ctx.fill();
  }

  requestAnimationFrame(renderCanvas);
}
requestAnimationFrame(renderCanvas);

// Boot endpoints and run an initial live demonstration transfer
updateCompetitorCards();
rebuildEndpoints(true);
setTimeout(() => {
  launchStream();
}, 320);
