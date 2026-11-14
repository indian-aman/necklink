/* ==========================================================================
   NeckLink v3.0 — Locked-Boundary Interactive Controller
   1. Navigation, Copy Helpers & 18s Film Cinema Modal
   2. 144-Frame Ray-Traced Blender Cycles OptiX 3D Scroll-Scrub Engine
   3. Interactive 3D "Scramble the Connectome" Stage + Fixed Audit Table
   4. 24 KB Silicon Die Floorplan & 6-Module Inspector (Zero Reflow)
   5. Live 107.5 KB ANSI C99 WebAssembly (necklink_cns.wasm) + Webcam Cockpit
   ========================================================================== */

(function () {
  "use strict";

  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

  // -------------------------------------------------------------------------
  // 1. NAVIGATION, COPY HELPERS & FILM MODAL
  // -------------------------------------------------------------------------
  const nav = $(".nav");
  const menuBtn = $("#menu-btn");
  function setMenu(open) {
    if (!nav || !menuBtn) return;
    nav.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    menuBtn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }
  if (menuBtn) menuBtn.addEventListener("click", () => setMenu(!nav.classList.contains("open")));
  $$("#nav-links a").forEach((a) => a.addEventListener("click", () => setMenu(false)));

  const spyLinks = $$("#nav-links a")
    .map((a) => ({ a: a, el: document.getElementById(a.getAttribute("href").slice(1)) }))
    .filter((x) => x.el);

  function onNavScroll() {
    if (nav) nav.classList.toggle("scrolled", window.scrollY > 18);
    const line = window.innerHeight * 0.38;
    let current = null, best = -Infinity;
    spyLinks.forEach((x) => {
      const top = x.el.getBoundingClientRect().top;
      if (top <= line && top > best) { best = top; current = x; }
    });
    spyLinks.forEach((x) => {
      if (x === current) x.a.setAttribute("aria-current", "true");
      else x.a.removeAttribute("aria-current");
    });
  }
  window.addEventListener("scroll", onNavScroll, { passive: true });
  onNavScroll();

  // Cinema Film Lightbox Modal & Inline Stereo Control
  const filmModal = $("#film-modal");
  const modalVideo = $("#modal-video");
  const inlineVideo = $("#inline-promo-video");
  const unmutePill = $("#inline-unmute-pill");
  const unmuteLabel = $("#inline-unmute-label");

  function openFilmModal() {
    if (!filmModal || !modalVideo) return;
    if (inlineVideo && !inlineVideo.paused) inlineVideo.pause();
    filmModal.hidden = false;
    modalVideo.muted = false;
    modalVideo.currentTime = 0;
    modalVideo.play().catch(() => {});
  }

  function closeFilmModal() {
    if (!filmModal || !modalVideo) return;
    modalVideo.pause();
    filmModal.hidden = true;
    if (inlineVideo) inlineVideo.play().catch(() => {});
  }

  ["#hero-watch-btn", "#nav-watch-btn", "#inline-sound-btn"].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener("click", openFilmModal);
  });
  const closeModalBtn = $("#close-film-modal");
  const modalBackdrop = $("#film-modal-backdrop");
  if (closeModalBtn) closeModalBtn.addEventListener("click", closeFilmModal);
  if (modalBackdrop) modalBackdrop.addEventListener("click", closeFilmModal);

  if (unmutePill && inlineVideo) {
    unmutePill.addEventListener("click", () => {
      inlineVideo.muted = !inlineVideo.muted;
      unmutePill.classList.toggle("sound-on", !inlineVideo.muted);
      if (unmuteLabel) {
        unmuteLabel.textContent = inlineVideo.muted ? "Stereo Sound Off" : "Stereo Sound On";
      }
      if (inlineVideo.paused) inlineVideo.play().catch(() => {});
    });
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (filmModal && !filmModal.hidden) closeFilmModal();
    setMenu(false);
  });

  // Clipboard Copy Helper
  async function copyText(btn, text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (err) {}
      ta.remove();
    }
    btn.classList.add("copied");
    setTimeout(() => btn.classList.remove("copied"), 1500);
  }

  const installCode = $("#install-code");
  let currentCmd = installCode ? installCode.textContent : "";
  $$(".terminal-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".terminal-tab").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", String(t === tab));
      });
      currentCmd = tab.getAttribute("data-cmd");
      if (installCode) installCode.textContent = currentCmd;
    });
  });
  const copyCmdBtn = $("#copy-cmd-btn");
  if (copyCmdBtn) copyCmdBtn.addEventListener("click", (e) => copyText(e.currentTarget, currentCmd));

  // -------------------------------------------------------------------------
  // 2. 144-FRAME RAY-TRACED BLENDER CYCLES OPTIX SCROLLYTELLING ENGINE
  // -------------------------------------------------------------------------
  const diveSection = $("#dive");
  const scrollCanvas = $("#scroll-canvas");
  const scrollCue = $("#scroll-cue");
  const railSteps = $$(".rail-step");

  if (diveSection && scrollCanvas) {
    const sctx = scrollCanvas.getContext("2d", { alpha: false });
    const TOTAL_FRAMES = 144;
    const frames = new Array(TOTAL_FRAMES);
    let posterImg = null;
    let scW = 0, scH = 0;
    let targetFrame = 0;
    let smoothFrame = 0;
    let lastPaintedIdx = -1;

    function resizeScrollCanvas() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      scW = scrollCanvas.clientWidth || window.innerWidth;
      scH = scrollCanvas.clientHeight || window.innerHeight;
      scrollCanvas.width = Math.round(scW * dpr);
      scrollCanvas.height = Math.round(scH * dpr);
      sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      lastPaintedIdx = -1;
      paintScrollFrame(Math.round(smoothFrame));
    }

    function drawCoverImage(img) {
      const iw = img.naturalWidth || 1600;
      const ih = img.naturalHeight || 900;
      const scale = Math.max(scW / iw, scH / ih);
      const dw = iw * scale;
      const dh = ih * scale;
      const dx = (scW - dw) * 0.5;
      const dy = (scH - dh) * 0.48;
      sctx.drawImage(img, dx, dy, dw, dh);
    }

    function nearestLoadedFrame(idx) {
      if (frames[idx] && frames[idx].complete && frames[idx].naturalWidth > 0) return frames[idx];
      for (let d = 1; d < TOTAL_FRAMES; d++) {
        if (idx - d >= 0 && frames[idx - d] && frames[idx - d].complete && frames[idx - d].naturalWidth > 0) {
          return frames[idx - d];
        }
        if (idx + d < TOTAL_FRAMES && frames[idx + d] && frames[idx + d].complete && frames[idx + d].naturalWidth > 0) {
          return frames[idx + d];
        }
      }
      return posterImg;
    }

    function paintScrollFrame(idx) {
      const clamped = Math.max(0, Math.min(TOTAL_FRAMES - 1, idx));
      const img = nearestLoadedFrame(clamped);
      if (!img || !img.complete || img.naturalWidth === 0) {
        sctx.fillStyle = "#08090b";
        sctx.fillRect(0, 0, scW, scH);
        return;
      }
      drawCoverImage(img);
      lastPaintedIdx = clamped;
    }

    function computeDiveProgress() {
      const rect = diveSection.getBoundingClientRect();
      const scrollable = Math.max(1, diveSection.offsetHeight - window.innerHeight);
      const p = Math.max(0, Math.min(1, -rect.top / scrollable));
      targetFrame = p * (TOTAL_FRAMES - 1);

      const actIdx = Math.min(4, Math.floor(p * 5));
      railSteps.forEach((btn, i) => btn.classList.toggle("active", i === actIdx));
      if (scrollCue) scrollCue.style.opacity = p > 0.05 ? "0" : "1";
    }

    function tickScroll() {
      smoothFrame += (targetFrame - smoothFrame) * 0.14;
      const idx = Math.round(smoothFrame);
      if (idx !== lastPaintedIdx) {
        paintScrollFrame(idx);
      }
      requestAnimationFrame(tickScroll);
    }

    function buildStridedOrder(n) {
      const seen = new Uint8Array(n);
      const order = [];
      [8, 4, 2, 1].forEach((stride) => {
        for (let i = 0; i < n; i += stride) {
          if (!seen[i]) {
            seen[i] = 1;
            order.push(i);
          }
        }
      });
      return order;
    }

    function preloadScrollSequence() {
      posterImg = new Image();
      posterImg.decoding = "async";
      posterImg.onload = () => {
        if (lastPaintedIdx <= 0) paintScrollFrame(Math.round(smoothFrame));
      };
      posterImg.src = "assets/scroll/poster_hero.webp";

      const order = buildStridedOrder(TOTAL_FRAMES);
      let cursor = 0;
      const CONCURRENCY = 6;

      function next() {
        if (cursor >= order.length) return;
        const fIdx = order[cursor++];
        const img = new Image();
        img.decoding = "async";
        frames[fIdx] = img;
        const done = () => {
          if (Math.abs(fIdx - Math.round(smoothFrame)) <= 4) {
            paintScrollFrame(Math.round(smoothFrame));
          }
          next();
        };
        img.onload = done;
        img.onerror = done;
        img.src = "assets/scroll/frame_" + String(fIdx + 1).padStart(3, "0") + ".webp";
      }

      for (let k = 0; k < CONCURRENCY; k++) next();
    }

    railSteps.forEach((btn) => {
      btn.addEventListener("click", () => {
        const jump = parseFloat(btn.getAttribute("data-dive-jump") || "0");
        const scrollable = Math.max(1, diveSection.offsetHeight - window.innerHeight);
        const top = diveSection.offsetTop + jump * scrollable;
        window.scrollTo({ top: top, behavior: "smooth" });
      });
    });

    window.addEventListener("resize", resizeScrollCanvas);
    window.addEventListener("scroll", computeDiveProgress, { passive: true });
    resizeScrollCanvas();
    computeDiveProgress();
    preloadScrollSequence();
    requestAnimationFrame(tickScroll);
  }

  // -------------------------------------------------------------------------
  // 3. INTERACTIVE 3D "SCRAMBLE THE CONNECTOME" STAGE & FIXED AUDIT TABLE
  // -------------------------------------------------------------------------
  const scrambleCanvas = $("#scramble-canvas");
  const scoreboardRowsEl = $("#scoreboard-rows");
  const stageBadge = $("#stage3d-badge");
  const stageDesc = $("#stage3d-desc");
  const roBump = $("#ro-bump");
  const roTripod = $("#ro-tripod");
  const roMb = $("#ro-mb");

  const FALLBACK_BENCHMARKS = [
    { organ: "NeckCompass", task: "GPS-denied homing", unit: "% arrival", real: 100.0, shuffled: 4.7, analytic: 100.0, classical: 100.0, classical_name: "χ² EKF", p: 0.0099, verdict: "PASS" },
    { organ: "NeckCompass", task: "Waves + 125° mag spikes", unit: "° RMS", real: 12.84, shuffled: 62.82, analytic: 0.99, classical: 0.81, classical_name: "χ² EKF", p: 0.0099, verdict: "PASS" },
    { organ: "NeckCord", task: "Emergent 6-leg tripod", unit: "R_tripod", real: 0.855, shuffled: 0.411, analytic: 1.000, classical: 1.000, classical_name: "Fixed table", p: 0.0099, verdict: "PASS" },
    { organ: "NeckLink CNS", task: "USV hazard survival", unit: "% safe", real: 58.3, shuffled: 4.6, analytic: 80.0, classical: 0.0, classical_name: "Ungated", p: 0.0297, verdict: "MARGINAL" },
    { organ: "NeckSight", task: "Thin poles & wires", unit: "AUROC", real: 0.755, shuffled: 0.672, analytic: 0.987, classical: 0.635, classical_name: "Farneback", p: 0.0396, verdict: "MARGINAL" },
    { organ: "NeckSense", task: "Gas drift novelty", unit: "AUROC", real: 0.666, shuffled: 0.672, analytic: 0.670, classical: 0.698, classical_name: "OC-SVM", p: 0.7426, verdict: "NULL" }
  ];

  let wiringMode = "real";
  let sceneData = null;

  function fmtMetric(val, unit) {
    if (unit === "% arrival" || unit === "% safe") return val.toFixed(1) + "%";
    if (unit === "° RMS") return val.toFixed(2) + "°";
    return val.toFixed(3);
  }

  function renderScoreboard(rows) {
    if (!scoreboardRowsEl) return;
    $$(".score-table thead th[data-col]").forEach((th) => {
      th.classList.toggle("active-col", th.getAttribute("data-col") === wiringMode);
    });

    scoreboardRowsEl.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      const vLabel = r.verdict === "NULL" ? "NULL" : r.verdict;
      tr.innerHTML =
        '<td><span class="task-organ">' + r.organ + '</span><span class="task-name">' + r.task.trim() + '</span></td>' +
        '<td class="' + (wiringMode === "real" ? "active-col" : "") + '">' + fmtMetric(r.real, r.unit) + '</td>' +
        '<td class="' + (wiringMode === "shuffled" ? "active-col" : "") + '">' + fmtMetric(r.shuffled, r.unit) + '</td>' +
        '<td class="' + (wiringMode === "analytic" ? "active-col" : "") + '">' + fmtMetric(r.analytic, r.unit) + '</td>' +
        '<td>' + fmtMetric(r.classical, r.unit) + '<span class="sub-lbl">' + r.classical_name + '</span></td>' +
        '<td><span class="verdict-pill verdict-' + r.verdict + '">' + vLabel + '</span></td>';
      scoreboardRowsEl.appendChild(tr);
    });
  }

  function setWiringMode(mode) {
    wiringMode = mode;
    $$(".mode-btn").forEach((b) => {
      const on = b.getAttribute("data-wiring") === mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", String(on));
    });
    if (mode === "real") {
      if (stageBadge) { stageBadge.textContent = "LIVE 3D TOPOLOGY · REAL BANC v888"; stageBadge.style.color = "var(--compass)"; }
      if (stageDesc) stageDesc.textContent = "Coherent EPG compass bump + emergent 3+3 VNC tripod wave.";
      if (roBump) { roBump.textContent = "0.89 · Locked"; roBump.style.color = "#f3d285"; }
      if (roTripod) { roTripod.textContent = "0.855 · 3+3 Gait"; roTripod.style.color = "#8fd8b8"; }
      if (roMb) { roMb.textContent = "0.666 AUROC"; roMb.style.color = "#d2d6de"; }
    } else if (mode === "shuffled") {
      if (stageBadge) { stageBadge.textContent = "MASLOV–SNEPPEN SHUFFLE · SCRAMBLED"; stageBadge.style.color = "var(--cord)"; }
      if (stageDesc) stageDesc.textContent = "Same node degrees — scrambled chords collapse bump and gait.";
      if (roBump) { roBump.textContent = "0.14 · Collapsed"; roBump.style.color = "#e88c7d"; }
      if (roTripod) { roTripod.textContent = "0.411 · Uncoupled"; roTripod.style.color = "#e88c7d"; }
      if (roMb) { roMb.textContent = "0.672 (Invariant)"; roMb.style.color = "#8fd8b8"; }
    } else {
      if (stageBadge) { stageBadge.textContent = "v2_ANALYTIC · HAND-TUNED BASELINE"; stageBadge.style.color = "var(--sight)"; }
      if (stageDesc) stageDesc.textContent = "Symmetric cosine ring + fixed tripod phase table (0.99° RMS).";
      if (roBump) { roBump.textContent = "0.99 · Cosine"; roBump.style.color = "#8fd0df"; }
      if (roTripod) { roTripod.textContent = "1.000 · Fixed"; roTripod.style.color = "#8fd0df"; }
      if (roMb) { roMb.textContent = "0.670 AUROC"; roMb.style.color = "#d2d6de"; }
    }
    renderScoreboard(sceneData && sceneData.benchmarks ? sceneData.benchmarks : FALLBACK_BENCHMARKS);
  }

  $$(".mode-btn").forEach((b) => b.addEventListener("click", () => setWiringMode(b.getAttribute("data-wiring"))));

  // Interactive 3D Canvas Renderer for Scramble Proof
  if (scrambleCanvas) {
    const ctx3 = scrambleCanvas.getContext("2d");
    let w3 = 0, h3 = 0;
    let rotY = 0.42, rotX = 0.24;
    let dragging = false, lastX = 0, lastY = 0;
    let stageVisible = true;
    let animT = 0;

    function resize3D() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w3 = scrambleCanvas.parentElement.clientWidth || 500;
      h3 = scrambleCanvas.parentElement.clientHeight || 326;
      scrambleCanvas.width = Math.round(w3 * dpr);
      scrambleCanvas.height = Math.round(h3 * dpr);
      ctx3.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    const wrap = scrambleCanvas.parentElement;
    wrap.addEventListener("pointerdown", (e) => {
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      wrap.setPointerCapture(e.pointerId);
    });
    wrap.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      rotY += (e.clientX - lastX) * 0.008;
      rotX = Math.max(-0.65, Math.min(0.75, rotX + (e.clientY - lastY) * 0.006));
      lastX = e.clientX;
      lastY = e.clientY;
    });
    wrap.addEventListener("pointerup", () => { dragging = false; });
    wrap.addEventListener("pointercancel", () => { dragging = false; });

    function proj(x, y, z) {
      const cy = Math.cos(rotY), sy = Math.sin(rotY);
      const cx = Math.cos(rotX), sx = Math.sin(rotX);
      const x1 = x * cy - y * sy;
      const y1 = x * sy + y * cy;
      const z2 = z * cx - y1 * sx;
      const y2 = z * sx + y1 * cx;
      const fov = Math.min(w3, h3) * 0.92;
      const dist = 2.55 + y2;
      const s = fov / Math.max(0.6, dist);
      return { x: w3 * 0.5 + x1 * s, y: h3 * 0.50 - z2 * s, s: s, depth: y2 };
    }

    function drawStage3D() {
      if (stageVisible) {
        animT += 0.028;
        if (!dragging) rotY += 0.0030;

        ctx3.clearRect(0, 0, w3, h3);

        const thetaEpg = (sceneData && sceneData.cx && sceneData.cx.theta_epg_rad)
          ? sceneData.cx.theta_epg_rad
          : Array.from({ length: 50 }, (_, i) => (i / 50) * Math.PI * 2);
        const wAngles = (sceneData && sceneData.cx && sceneData.cx.theta_w_rad)
          ? sceneData.cx.theta_w_rad
          : Array.from({ length: 16 }, (_, i) => -Math.PI + (i + 0.5) * (Math.PI / 8));

        const mExc = (sceneData && sceneData.cx)
          ? (wiringMode === "shuffled" ? sceneData.cx.M_exc_shuf : sceneData.cx.M_exc_real)
          : null;

        const rRing = 0.42;
        const zRing = 0.52;
        const bumpAngle = (animT * 0.9) % (Math.PI * 2);

        for (let i = 0; i < 16; i++) {
          for (let j = i + 1; j < 16; j++) {
            let w = 0;
            if (wiringMode === "analytic") {
              const d = Math.abs(i - j);
              w = Math.min(d, 16 - d) <= 2 ? 0.35 : 0.0;
            } else if (mExc) {
              w = 0.5 * (mExc[i][j] + mExc[j][i]);
            } else {
              w = wiringMode === "shuffled" ? (((i * 7 + j * 13) % 11) > 7 ? 0.28 : 0) : (Math.min(Math.abs(i - j), 16 - Math.abs(i - j)) <= 2 ? 0.3 : 0);
            }
            if (w > 0.14) {
              const a1 = wAngles[i], a2 = wAngles[j];
              const p1 = proj(rRing * Math.cos(a1), rRing * 0.55 * Math.sin(a1), zRing + rRing * 0.55 * Math.sin(a1));
              const p2 = proj(rRing * Math.cos(a2), rRing * 0.55 * Math.sin(a2), zRing + rRing * 0.55 * Math.sin(a2));
              ctx3.strokeStyle = wiringMode === "shuffled"
                ? "rgba(216, 122, 104, " + Math.min(0.52, w * 1.3).toFixed(2) + ")"
                : (wiringMode === "analytic" ? "rgba(103, 183, 201, 0.38)" : "rgba(228, 180, 92, " + Math.min(0.58, w * 1.4).toFixed(2) + ")");
              ctx3.lineWidth = wiringMode === "shuffled" ? 1.1 : 1.4;
              ctx3.beginPath();
              ctx3.moveTo(p1.x, p1.y);
              ctx3.lineTo(p2.x, p2.y);
              ctx3.stroke();
            }
          }
        }

        const legCoords = [
          [-0.34, 0.10, -0.24], [0.34, 0.10, -0.24],
          [-0.38, 0.06, -0.48], [0.38, 0.06, -0.48],
          [-0.32, 0.02, -0.72], [0.32, 0.02, -0.72]
        ];
        for (let k = 0; k < 10; k++) {
          const ang = (k / 10) * Math.PI * 2;
          const bx = 0.22 * Math.cos(ang), by = 0.14 * Math.sin(ang), bz = 0.36;
          const nx = 0.08 * Math.cos(ang + 0.4), ny = 0.08 * Math.sin(ang + 0.4), nz = 0.04;
          const tgtIdx = wiringMode === "shuffled" ? ((k * 3 + 1) % 6) : (k % 6);
          const [lx, ly, lz] = legCoords[tgtIdx];
          const pb = proj(bx, by, bz), pn = proj(nx, ny, nz), pl = proj(lx * 0.65, ly * 0.65, lz);

          ctx3.strokeStyle = wiringMode === "shuffled" ? "rgba(216, 122, 104, 0.30)" : "rgba(228, 180, 92, 0.40)";
          ctx3.lineWidth = 1.3;
          ctx3.beginPath();
          ctx3.moveTo(pb.x, pb.y);
          ctx3.quadraticCurveTo(pn.x, pn.y, pl.x, pl.y);
          ctx3.stroke();

          const u = (animT * 0.85 + k * 0.13) % 1.0;
          const iu = 1 - u;
          const sx = iu * iu * pb.x + 2 * iu * u * pn.x + u * u * pl.x;
          const sy = iu * iu * pb.y + 2 * iu * u * pn.y + u * u * pl.y;
          ctx3.fillStyle = wiringMode === "shuffled" ? "#e88c7d" : "#f3d285";
          ctx3.beginPath();
          ctx3.arc(sx, sy, 2.2, 0, Math.PI * 2);
          ctx3.fill();
        }

        thetaEpg.forEach((ang, idx) => {
          const px = rRing * Math.cos(ang);
          const py = rRing * 0.55 * Math.sin(ang);
          const pz = zRing + rRing * 0.55 * Math.sin(ang);
          const p = proj(px, py, pz);
          let act = 0;
          if (wiringMode === "shuffled") {
            act = 0.25 + 0.28 * Math.sin(animT * 3.7 + idx * 2.9);
          } else {
            const dAng = Math.atan2(Math.sin(ang - bumpAngle), Math.cos(ang - bumpAngle));
            const sigma = wiringMode === "analytic" ? 0.42 : 0.52;
            act = Math.exp(-(dAng * dAng) / (2 * sigma * sigma));
          }
          const rad = 2.4 + act * 4.2;
          ctx3.fillStyle = act > 0.55
            ? (wiringMode === "analytic" ? "#8fd0df" : "#f3d285")
            : (wiringMode === "shuffled" ? "rgba(216, 122, 104, 0.62)" : "rgba(228, 180, 92, 0.45)");
          ctx3.beginPath();
          ctx3.arc(p.x, p.y, rad, 0, Math.PI * 2);
          ctx3.fill();
        });

        for (let m = 0; m < 28; m++) {
          const sgn = m % 2 === 0 ? -1 : 1;
          const ma = (m / 28) * Math.PI * 2;
          const mx = sgn * (0.58 + 0.14 * Math.cos(ma));
          const my = 0.14 * Math.sin(ma);
          const mz = 0.68 + 0.10 * Math.sin(ma * 2);
          const pm = proj(mx, my, mz);
          const fire = Math.sin(animT * 1.8 + m * 1.7) > 0.82;
          ctx3.fillStyle = fire ? "#8fd8b8" : "rgba(95, 180, 144, 0.34)";
          ctx3.beginPath();
          ctx3.arc(pm.x, pm.y, fire ? 3.2 : 1.9, 0, Math.PI * 2);
          ctx3.fill();
        }

        const cVnc = (sceneData && sceneData.vnc_dn)
          ? (wiringMode === "shuffled" ? sceneData.vnc_dn.C_vnc_inh_shuf : sceneData.vnc_dn.C_vnc_inh_real)
          : null;
        const tripodGroupA = [0, 3, 4];

        for (let i = 0; i < 6; i++) {
          for (let j = i + 1; j < 6; j++) {
            const w = cVnc ? (Math.abs(cVnc[i][j]) + Math.abs(cVnc[j][i])) : 0.25;
            if (w > 0.12) {
              const p1 = proj(legCoords[i][0], legCoords[i][1], legCoords[i][2]);
              const p2 = proj(legCoords[j][0], legCoords[j][1], legCoords[j][2]);
              ctx3.strokeStyle = wiringMode === "shuffled" ? "rgba(216, 122, 104, 0.34)" : "rgba(95, 180, 144, 0.34)";
              ctx3.lineWidth = 1.2;
              ctx3.beginPath();
              ctx3.moveTo(p1.x, p1.y);
              ctx3.lineTo(p2.x, p2.y);
              ctx3.stroke();
            }
          }
        }

        const legNames = ["L1", "R1", "L2", "R2", "L3", "R3"];
        legCoords.forEach((co, idx) => {
          const p = proj(co[0], co[1], co[2]);
          const isA = tripodGroupA.indexOf(idx) !== -1;
          let swing = 0;
          if (wiringMode === "shuffled") {
            swing = 0.5 + 0.5 * Math.sin(animT * 2.6 + idx * 1.15);
          } else {
            const phase = isA ? 0 : Math.PI;
            const jitter = wiringMode === "real" ? (idx * 0.12) : 0;
            swing = 0.5 + 0.5 * Math.sin(animT * 3.2 + phase + jitter);
          }
          const r = 5.5 + swing * 5.0;
          ctx3.fillStyle = swing > 0.55
            ? (wiringMode === "shuffled" ? "#d87a68" : (isA ? "#5fb490" : "#67b7c9"))
            : "rgba(24, 28, 36, 0.95)";
          ctx3.strokeStyle = isA ? "#5fb490" : "#67b7c9";
          ctx3.lineWidth = 1.8;
          ctx3.beginPath();
          ctx3.arc(p.x, p.y, r, 0, Math.PI * 2);
          ctx3.fill();
          ctx3.stroke();

          ctx3.fillStyle = "#d2d6de";
          ctx3.font = "600 10px 'JetBrains Mono', monospace";
          ctx3.fillText(legNames[idx], p.x + r + 5, p.y + 3);
        });

        ctx3.fillStyle = "rgba(139, 146, 160, 0.72)";
        ctx3.font = "500 10px 'JetBrains Mono', monospace";
        ctx3.fillText("50 EPG RING + MB CLAWS", 14, 20);
        ctx3.fillText("10 CERVICAL DNs", 14, h3 * 0.52);
        ctx3.fillText("6×6 VNC TRIPOD POOLS", 14, h3 - 12);
      }
      requestAnimationFrame(drawStage3D);
    }

    resize3D();
    window.addEventListener("resize", resize3D);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        entries.forEach((en) => { stageVisible = en.isIntersecting; });
      }).observe(scrambleCanvas);
    }
    requestAnimationFrame(drawStage3D);
  }

  renderScoreboard(FALLBACK_BENCHMARKS);
  fetch("data/banc_scene_data.json")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => {
      sceneData = d;
      if (d && d.benchmarks) renderScoreboard(d.benchmarks);
    })
    .catch(() => {});

  // -------------------------------------------------------------------------
  // 4. 24 KB SILICON DIE EXPLORER (100% Locked Panel Heights)
  // -------------------------------------------------------------------------
  const ORGANS = {
    cns: {
      colorVar: "--accent",
      region: "Whole-CNS · BANC v888",
      tests: "7 Py + 7 C99 + WASM",
      name: "NeckLink CNS",
      pkg: "necklink-cns",
      desc: "Unified 4-organ reflex copilot projecting 11 brain populations onto 10 cervical descending channels and 6 motor pools with MAVLink v2.",
      ram: "24,024 B (0 B heap)",
      speed: "166.3 µs / tick",
      gain: "58.3% vs 4.6% shuf",
      tradeoffLabel: "Honest scope boundary",
      tradeoff: "Reaches 66.7% 30-seed survival (58.3% vs 4.6% shuffle gate; v2_analytic reaches 80.0%). Simulation-verified reflex copilot, not flight-tested.",
      code: "from necklink_cns import NeckLinkCNS\n\ncns = NeckLinkCNS(vehicle=\"hexacopter\", grid_size=32)\nout = cns.step(dt=0.02, speed_m_s=1.8, gyro_xyz=[0.02, -0.01, 0.05],\n               accel_xyz=[0.2, -0.1, 9.8], mag_xyz=[0.45, 0.05, 0.89], camera_frame=cam)\nprint(out.descending.behavior_mode, len(out.cervical_wire_4b))",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necklink_cns",
      pypi: "https://pypi.org/project/necklink-cns/",
      pypiLabel: "PyPI v3.0.0"
    },
    sight: {
      colorVar: "--sight",
      region: "Optic Lobe · 64 LPLC2 + 32 LC4",
      tests: "35 Py + C99 + ESP32",
      name: "NeckSight",
      pkg: "necksight",
      desc: "Logarithmic R1–R6 glare guard, T4/T5 motion, 3-DOF gyro derotation, and BANC-mapped LPLC2/LC4 looming & thin-wire dodge vectors.",
      ram: "14,624 B (14.28 KB)",
      speed: "6,557 FPS (32×32)",
      gain: "0.755 AUROC (poles)",
      tradeoffLabel: "Where analytic & Farneback win",
      tradeoff: "v2_analytic (0.890) and Farneback (0.818) lead across all obstacles; adding 32 LC4 channels beats Farneback on thin poles (0.755 vs 0.635).",
      code: "from necksight import NeckSightPipeline, imu_to_ego_motion\n\noptic = NeckSightPipeline(grid_size=64, enable_lc4_bar=True)\nego = imu_to_ego_motion(yaw_r, pitch_r, roll_r, dt=1/60, grid_width=64)\nres = optic.step(frame_u8, dt=1/60, ego_shift_px=ego)\nprint(res.triggered, res.looming_score, res.bar_score)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksight",
      pypi: "https://pypi.org/project/necksight/",
      pypiLabel: "PyPI v3.0.0"
    },
    compass: {
      colorVar: "--compass",
      region: "Central Complex · 50 EPG Ring",
      tests: "48 Py + 14 C99 + ESP32",
      name: "NeckCompass",
      pkg: "neckcompass",
      desc: "Zero-float 9-axis tilt-compensated heading on a 50-EPG / 40-Delta7 ring attractor and PFL3/PFL2 GPS-denied homing (~3,900 weights).",
      ram: "1,444 B static",
      speed: "0.96 µs / 8.10 µs",
      gain: "100% vs 4.7% homing",
      tradeoffLabel: "Where v2_analytic & χ² EKF win",
      tradeoff: "Biological ring heterogeneity drifts 12.84° RMS under waves + spikes vs 0.99° (v2_analytic) and 0.81° (χ² EKF). Pass v2_analytic for sub-degree lock.",
      code: "from neckcompass import RingCompass, FanBodyNavigator\n\nnav = FanBodyNavigator(compass=RingCompass(n_wedges=16))\nhdg, dist_m, brg = nav.step_with_compass_9dof(\n    gx, gy, gz, ax, ay, az, mx, my, mz, speed_m_s=1.8, dt=0.02\n)\nprint(hdg, nav.steering_cmd(max_turn_rate=1.0))",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcompass",
      pypi: "https://pypi.org/project/neckcompass/",
      pypiLabel: "PyPI v3.0.0"
    },
    cord: {
      colorVar: "--cord",
      region: "Ventral Nerve Cord · 6×6 CPG",
      tests: "48 Py + 19 C99 + ESP32",
      name: "NeckCord",
      pkg: "neckcord",
      desc: "Emergent 6-limb alternating tripod gait from BANC premotor inhibition, paired with active-set thruster allocation and ESC fault shunting.",
      ram: "930 B static",
      speed: "5.57 µs / tick",
      gain: "0.855 vs 0.411 gait",
      tradeoffLabel: "Where fixed table & SciPy QP win",
      tradeoff: "A fixed phase table hits 1.000 tripod order; in MuJoCo rotor loss, all allocators survive while SciPy QP holds 0.17° vs 0.25° tilt.",
      code: "from neckcord import ESCObserver, ShuntingThrusterCord\nimport numpy as np\n\ncord = ShuntingThrusterCord.multicopter_hex_6(u_max=4.5)\nesc = ESCObserver(n_actuators=6, u_nom=4.5, rpm_max=9000.0)\nres = cord.step(np.array([17.7, 0.0, 0.0, 0.0]), esc_observer=esc, dt=0.01)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcord",
      pypi: "https://pypi.org/project/neckcord/",
      pypiLabel: "PyPI v3.0.0"
    },
    sense: {
      colorVar: "--sense",
      region: "Mushroom Body · 1,024 KC",
      tests: "27 Py + 9 C99 + ESP32",
      name: "NeckSense",
      pkg: "necksense",
      desc: "Integer sparse expansion over 1,024 Kenyon-cell claws, divisive antennal normalization, APL inhibition, and 2-compartment continual memory.",
      ram: "5,804 B (NS_TINY)",
      speed: "16.4 µs / sample",
      gain: "0.666 AUROC (Null)",
      tradeoffLabel: "Honest null & classical baseline",
      tradeoff: "Degree-matched shuffles match real BANC claws (0.666 vs 0.672, p = 0.74). One-Class SVM (0.698) scores higher on desktop; NeckSense fits in 5.8 KB.",
      code: "from necksense import VibrationFrontEnd, MushroomNoveltyDetector\n\nfe = VibrationFrontEnd()\ndet = MushroomNoveltyDetector(n_inputs=16, antennal=True, n_kc=1024)\nscore = det.update_and_score(fe.extract(accel_256_window))\nprint(\"Novelty score:\", score)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksense",
      pypi: "https://pypi.org/project/necksense/",
      pypiLabel: "PyPI v3.0.0"
    },
    nltp: {
      colorVar: "--nltp",
      region: "Cervical Wire Bus · v1.6.0",
      tests: "248 Interop + ESP-NOW",
      name: "NLTP",
      pkg: "pynltp",
      desc: "Loss-healing telemetry & reflex transport with GF(2^8) Cauchy FEC and LZ4 over UDP and ESP-NOW, secured by X25519 + ChaCha20-Poly1305.",
      ram: "4.76 KB TX MCU",
      speed: "956 MB/s GF(2^8)",
      gain: "100% @ 35% loss",
      tradeoffLabel: "Where kernel TCP BBR wins",
      tradeoff: "On clean wired Ethernet, kernel TCP BBR reaches higher bulk throughput; NLTP v1.6.0 targets lossy radio, ESP-NOW meshes, and 4G CGNAT links.",
      code: "from nltp import NLTPClient, generate_nltp_key\n\nwith NLTPClient((\"127.0.0.1\", 9000), psk=generate_nltp_key()) as cli:\n    cli.send_reflex(\"LOOM_ESTOP\")\n    receipt = cli.send_file(\"telemetry_batch.bin\")\n    assert receipt[\"verified_sha256\"]",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/nltp",
      pypi: "https://pypi.org/project/pynltp/",
      pypiLabel: "PyPI v1.6.0"
    }
  };

  const detail = $("#organ-detail");
  let currentCode = "";

  function selectOrgan(key) {
    const d = ORGANS[key];
    if (!d || !detail) return;

    $$(".die-block").forEach((btn) => {
      const on = btn.getAttribute("data-organ") === key;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", String(on));
    });

    detail.style.setProperty("--c", "var(" + d.colorVar + ")");
    $("#det-region").textContent = d.region;
    $("#det-tests").textContent = d.tests;
    const title = $("#det-title");
    title.textContent = d.name + " ";
    const pkg = document.createElement("code");
    pkg.textContent = d.pkg;
    title.appendChild(pkg);
    $("#det-desc").textContent = d.desc;
    $("#det-ram").textContent = d.ram;
    $("#det-speed").textContent = d.speed;
    $("#det-gain").textContent = d.gain;
    $("#det-tradeoff-label").textContent = d.tradeoffLabel;
    $("#det-tradeoff").textContent = d.tradeoff;
    $("#det-code").textContent = d.code;
    currentCode = d.code;
    $("#det-repo").href = d.repo;
    const pypiEl = $("#det-pypi");
    pypiEl.href = d.pypi;
    pypiEl.textContent = d.pypiLabel;
  }

  $$(".die-block").forEach((el) => el.addEventListener("click", () => selectOrgan(el.getAttribute("data-organ"))));
  const copyCodeBtn = $("#copy-code-btn");
  if (copyCodeBtn) copyCodeBtn.addEventListener("click", (e) => copyText(e.currentTarget, currentCode));
  selectOrgan("cns");

  // -------------------------------------------------------------------------
  // 5. LIVE C99 WEBASSEMBLY (necklink_cns.wasm) + WEBCAM HARDWARE COCKPIT
  // -------------------------------------------------------------------------
  const canvas = $("#reflex-canvas");
  const statusEl = $("#sim-status");
  const statusDot = $("#sim-dot");
  const webcamBtn = $("#webcam-btn");
  const webcamBtnLabel = $("#webcam-btn-label");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  const N_HIST = 220;
  const histRing = new Array(N_HIST).fill(0);
  const histEkf = new Array(N_HIST).fill(0);
  const C = {
    grid: "rgba(255, 255, 255, 0.06)",
    divider: "rgba(255, 255, 255, 0.10)",
    label: "#8b92a0",
    text: "#d2d6de",
    ring: "#e4b45c",
    ekf: "#d87a68",
    optic: "#67b7c9",
    pole: "#9ba5c9",
    ok: "#5fb490",
    bar: "#e4b45c",
    barBg: "rgba(255, 255, 255, 0.06)",
    fault: "#d87a68",
    bg: "#090b0e"
  };

  let wasmEx = null;
  let wasmFrameU8 = null;
  let wasmTelemI32 = null;
  let wasmSramBytes = 24024;
  const localFrame64 = new Uint8Array(64 * 64);

  const camCanvas = document.createElement("canvas");
  camCanvas.width = 64;
  camCanvas.height = 64;
  const camCtx = camCanvas.getContext("2d", { willReadFrequently: true });
  const retinaImgData = camCtx.createImageData(64, 64);
  let videoEl = null;
  let mediaStream = null;
  let webcamActive = false;

  let cssW = 0, cssH = 0, narrow = false;
  let ringErr = 0, ekfErr = 0, ekfBias = 0;
  let spikeTicks = 0, glareTicks = 0, loomProgress = -1, loomKind = "boulder", estopFlash = 0;
  let fouledT2 = false, t2Health = 1.0;
  let lastLoomScore = 0, lastLc4Score = 0, lastAvoidX = 0, lastAvoidY = 0;
  let lastThrust = [0.62, 0.62, 0.62, 0.62];
  let lastWireHex = "0x53000093";
  let running = false;

  function nominalMessage() {
    if (wasmEx) {
      return "WASM C99 LIVE (" + wasmSramBytes.toLocaleString() + " B static SRAM, 0 B heap) · 9-DOF ring compass locked.";
    }
    return "Nominal cruise · Ring compass locked (0.4° error), LPLC2+LC4 scanning, 4B cervical wire active.";
  }

  function setStatus(msg, colorVar) {
    if (statusEl) statusEl.textContent = msg;
    if (statusDot) statusDot.style.background = colorVar ? "var(" + colorVar + ")" : "var(--ok)";
  }

  async function initWasm() {
    try {
      const resp = await fetch("necklink_cns.wasm");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const bytes = await resp.arrayBuffer();
      const { instance } = await WebAssembly.instantiate(bytes, {});
      wasmEx = instance.exports;
      if (typeof wasmEx._initialize === "function") wasmEx._initialize();
      wasmEx.nl_wasm_init(0);
      wasmSramBytes = wasmEx.nl_wasm_sizeof_cns();
      const fptr = wasmEx.nl_wasm_frame_buf();
      const tptr = wasmEx.nl_wasm_get_telemetry();
      wasmFrameU8 = new Uint8Array(wasmEx.memory.buffer, fptr, 64 * 64);
      wasmTelemI32 = new Int32Array(wasmEx.memory.buffer, tptr, 24);
      setStatus(nominalMessage(), "--ok");
    } catch (err) {
      wasmEx = null;
      setStatus(nominalMessage(), "--ok");
    }
  }
  initWasm();

  async function toggleWebcam() {
    if (webcamActive) {
      stopWebcam();
      setStatus(nominalMessage(), "--ok");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("Webcam requires HTTPS or localhost with browser camera support.", "--warn");
      return;
    }
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 160 }, height: { ideal: 120 }, facingMode: "user" },
        audio: false
      });
      if (!videoEl) {
        videoEl = document.createElement("video");
        videoEl.setAttribute("playsinline", "");
        videoEl.muted = true;
      }
      videoEl.srcObject = mediaStream;
      await videoEl.play();
      webcamActive = true;
      if (webcamBtn) webcamBtn.classList.add("active");
      if (webcamBtnLabel) webcamBtnLabel.textContent = "Stop Webcam";
      setStatus("LIVE WEBCAM → C99 WASM · Move hand or pen rapidly toward camera to trigger LPLC2/LC4 dodge.", "--ok");
    } catch (err) {
      setStatus("Camera permission denied or unavailable.", "--warn");
    }
  }

  function stopWebcam() {
    webcamActive = false;
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    if (webcamBtn) webcamBtn.classList.remove("active");
    if (webcamBtnLabel) webcamBtnLabel.textContent = "Live Webcam";
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cssW = canvas.parentElement.clientWidth || 900;
    cssH = canvas.parentElement.clientHeight || 340;
    narrow = cssW < 680;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function panels() {
    if (!narrow) {
      const w1 = Math.round(cssW * 0.38), w2 = Math.round(cssW * 0.31);
      return [
        { x: 0, y: 0, w: w1, h: cssH },
        { x: w1, y: 0, w: w2, h: cssH },
        { x: w1 + w2, y: 0, w: cssW - w1 - w2, h: cssH }
      ];
    }
    const h = cssH / 3;
    return [0, 1, 2].map((i) => ({ x: 0, y: i * h, w: cssW, h: h }));
  }

  $$("[data-sim]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.getAttribute("data-sim");
      if (mode === "webcam") {
        toggleWebcam();
        return;
      }
      if (mode === "spike") {
        spikeTicks = 55;
        setStatus("+120° magnetic spike · Ungated EKF drags ~42°; C99 NeckCompass shunts spike (<1.0°).", "--compass");
      } else if (mode === "glare") {
        glareTicks = 50;
        setStatus("100× sunlight glare · R1–R6 log photoreceptor + AEC cancels global flash (0 false alarms).", "--sight");
      } else if (mode === "loom") {
        loomKind = "boulder";
        loomProgress = 0;
        setStatus("Boulder approaching port · C99 LPLC2 4-quadrant gate fires contralateral DNa02 dodge.", "--sight");
      } else if (mode === "pole") {
        loomKind = "pole";
        loomProgress = 0;
        setStatus("Thin pole approaching · NeckSight LC4 high-acuity edge channel locks on.", "--sight");
      } else if (mode === "foul") {
        fouledT2 = true;
        setStatus("Thruster T2 fouled (−90%) · NeckCord active-set shunts T2 and rebalances surviving rotors.", "--cord");
      } else {
        stopWebcam();
        if (wasmEx) wasmEx.nl_wasm_init(0);
        ringErr = ekfErr = ekfBias = 0;
        spikeTicks = glareTicks = estopFlash = 0;
        loomProgress = -1;
        fouledT2 = false;
        t2Health = 1.0;
        lastLoomScore = lastLc4Score = lastAvoidX = lastAvoidY = 0;
        setStatus(nominalMessage(), "--ok");
      }
    });
  });

  function synthesizeRetinaFrame(buf) {
    if (webcamActive && videoEl && videoEl.readyState >= 2) {
      camCtx.drawImage(videoEl, 0, 0, 64, 64);
      const rgba = camCtx.getImageData(0, 0, 64, 64).data;
      for (let i = 0; i < 4096; i++) {
        const p = i * 4;
        buf[i] = (rgba[p] * 77 + rgba[p + 1] * 150 + rgba[p + 2] * 29) >> 8;
      }
      return true;
    }

    const baseBg = glareTicks > 0 ? 248 : 186;
    for (let y = 0; y < 64; y++) {
      for (let x = 0; x < 64; x++) {
        const checker = ((x >> 3) ^ (y >> 3)) & 1 ? 8 : -8;
        buf[y * 64 + x] = Math.max(0, Math.min(255, baseBg + checker));
      }
    }

    if (loomProgress >= 0) {
      if (loomKind === "boulder") {
        const r = 2.5 + (loomProgress / 100) * 24.0;
        const cx = 23, cy = 32;
        for (let y = 0; y < 64; y++) {
          const dy = y - cy;
          for (let x = 0; x < 64; x++) {
            const dx = x - cx;
            if (dx * dx + dy * dy <= r * r) buf[y * 64 + x] = 24;
          }
        }
      } else {
        const halfW = 0.6 + (loomProgress / 100) * 5.8;
        const cx = 41;
        for (let y = 4; y < 60; y++) {
          for (let x = 0; x < 64; x++) {
            if (Math.abs(x - cx) <= halfW) buf[y * 64 + x] = 20;
          }
        }
      }
    }
    return true;
  }

  function step() {
    const noise = (Math.random() - 0.5) * 0.65;
    if (glareTicks > 0) glareTicks--;
    if (loomProgress >= 0) {
      loomProgress += 2.1;
      if (loomProgress > 100) loomProgress = -1;
    }

    const targetFrame = wasmFrameU8 || localFrame64;
    const hasCam = synthesizeRetinaFrame(targetFrame) ? 1 : 0;

    if (wasmEx && wasmTelemI32) {
      const spikeCdeg = spikeTicks > 0 ? 12000 : Math.round(noise * 70);
      const t2HealthQ15 = fouledT2 ? 2621 : 32767;
      const baseHdgCdeg = wasmTelemI32[0];

      wasmEx.nl_wasm_step_webcam_and_imu(
        20, 1500, 0, 0, 0,
        spikeTicks > 0 ? 180 : 0,
        spikeTicks > 0 ? -140 : 0,
        spikeCdeg, t2HealthQ15, hasCam
      );

      const newHdgCdeg = wasmTelemI32[0];
      let dDeg = Math.abs(newHdgCdeg - baseHdgCdeg) / 100.0;
      if (dDeg > 180) dDeg = 360 - dDeg;
      ringErr = ringErr * 0.75 + Math.min(2.2, dDeg + Math.abs(noise) * 0.35) * 0.25;

      lastLoomScore = wasmTelemI32[2] / 1024.0;
      lastLc4Score = wasmTelemI32[3] / 1024.0;
      const trig = wasmTelemI32[5] !== 0;
      lastAvoidX = wasmTelemI32[6] / 127.0;
      lastAvoidY = wasmTelemI32[7] / 127.0;
      t2Health = Math.max(0.0, wasmTelemI32[18] / 32767.0);
      lastThrust = [
        Math.max(0, wasmTelemI32[14] / 32767.0),
        Math.max(0, wasmTelemI32[15] / 32767.0),
        Math.max(0, wasmTelemI32[16] / 32767.0),
        Math.max(0, wasmTelemI32[17] / 32767.0)
      ];
      lastWireHex = "0x" + (wasmTelemI32[22] >>> 0).toString(16).padStart(8, "0").toUpperCase();

      if (trig && estopFlash === 0) {
        estopFlash = 38;
        const via = lastLc4Score > 0.04 && loomKind === "pole" ? "LC4 thin-pole" : "LPLC2 looming";
        const dirTxt = lastAvoidX > 0.15 ? "stbd (+X)" : (lastAvoidX < -0.15 ? "port (-X)" : "brake");
        setStatus("C99 WASM " + via + " triggered · DNa02 dodge → " + dirTxt + " · 4B wire " + lastWireHex, "--ok");
      }
    } else {
      if (loomProgress >= 60 && loomProgress < 64) {
        estopFlash = 40;
        lastAvoidX = loomKind === "pole" ? -0.9 : 0.9;
        setStatus((loomKind === "pole" ? "LC4 thin-pole" : "LPLC2 looming") + " reflex dispatched over 4-byte cervical link.", "--ok");
      }
      t2Health = fouledT2 ? Math.max(0.02, t2Health * 0.72) : Math.min(1.0, t2Health + 0.08);
      lastThrust = estopFlash > 0 ? [0.12, 0.0, 0.85, 0.85]
        : (fouledT2 ? [0.86, 0.02 * t2Health, 0.88, 0.78] : [0.62, 0.62, 0.62, 0.62]);
    }

    if (spikeTicks > 0) {
      spikeTicks--;
      if (!wasmEx) ringErr = ringErr * 0.82 + 0.30 + noise * 0.25;
      ekfErr = Math.min(46, ekfErr * 0.84 + 7.4);
      ekfBias = Math.min(14, ekfBias + 0.38);
    } else {
      if (!wasmEx) ringErr = ringErr * 0.86 + noise * 0.32;
      ekfBias *= 0.985;
      ekfErr = ekfErr * 0.94 + ekfBias * 0.45 + noise * 0.4;
    }

    histRing.push(ringErr); histRing.shift();
    histEkf.push(ekfErr); histEkf.shift();
    if (estopFlash > 0) estopFlash--;
  }

  function title(p, text) {
    ctx.fillStyle = C.label;
    ctx.font = "600 11px Inter, sans-serif";
    ctx.fillText(text, p.x + 20, p.y + 26);
  }

  function drawCompass(p) {
    title(p, "CENTRAL COMPLEX · 9-DOF HEADING ERROR (°)");
    const gx = p.x + 20, gy = p.y + 42, gw = p.w - 40, gh = p.h - 82;
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = Math.round(gy + (gh * i) / 4) + 0.5;
      ctx.beginPath(); ctx.moveTo(gx, y); ctx.lineTo(gx + gw, y); ctx.stroke();
    }
    const yOf = (v) => gy + gh - Math.max(0, Math.min(gh, (Math.abs(v) / 50) * gh));
    [[histEkf, C.ekf, 1.8], [histRing, C.ring, 2.2]].forEach(([hist, color, lw]) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = lw;
      ctx.lineJoin = "round";
      ctx.beginPath();
      hist.forEach((v, i) => {
        const x = gx + (i / (N_HIST - 1)) * gw;
        if (i === 0) ctx.moveTo(x, yOf(v)); else ctx.lineTo(x, yOf(v));
      });
      ctx.stroke();
    });
    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillStyle = C.ring;
    ctx.fillText("NeckCompass " + Math.abs(ringErr).toFixed(1) + "°", gx, p.y + p.h - 18);
    ctx.fillStyle = C.ekf;
    ctx.fillText("Ungated EKF " + Math.abs(ekfErr).toFixed(1) + "°", gx + Math.min(195, gw * 0.54), p.y + p.h - 18);
  }

  function drawOptic(p) {
    title(p, webcamActive ? "OPTIC LOBE · LIVE WEBCAM 64×64 (WASM)" : "OPTIC LOBE · LPLC2 + LC4 + DNa02 DODGE");
    const cx = p.x + p.w / 2, cy = p.y + 38 + (p.h - 76) / 2;
    const R = Math.max(30, Math.min(p.w * 0.36, (p.h - 84) / 2));

    if (glareTicks > 0) {
      ctx.fillStyle = "rgba(228, 180, 92, " + (0.16 * (glareTicks / 50)).toFixed(3) + ")";
      ctx.fillRect(p.x + 1, p.y + 1, p.w - 2, p.h - 2);
    }

    if (webcamActive) {
      const srcBuf = wasmFrameU8 || localFrame64;
      const dst = retinaImgData.data;
      for (let i = 0; i < 4096; i++) {
        const g = srcBuf[i];
        const o = i * 4;
        dst[o] = Math.round(g * 0.88);
        dst[o + 1] = Math.round(g * 0.90);
        dst[o + 2] = Math.round(g * 0.92);
        dst[o + 3] = 225;
      }
      camCtx.putImageData(retinaImgData, 0, 0);
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R - 2, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(camCanvas, cx - R, cy - R, R * 2, R * 2);
      ctx.restore();
    }

    ctx.strokeStyle = estopFlash > 0 ? C.ok : "rgba(228, 180, 92, 0.42)";
    ctx.lineWidth = estopFlash > 0 ? 2.5 : 1.4;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();

    if (!webcamActive && loomProgress >= 0) {
      if (loomKind === "boulder") {
        const lr = R * (0.08 + (loomProgress / 100) * 0.68);
        const ox = cx - R * 0.24;
        ctx.fillStyle = "rgba(103, 183, 201, 0.22)";
        ctx.strokeStyle = C.optic;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(ox, cy, lr, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      } else {
        const pw = Math.max(3, R * (0.04 + (loomProgress / 100) * 0.30));
        const ox = cx + R * 0.24;
        ctx.fillStyle = "rgba(155, 165, 201, 0.26)";
        ctx.strokeStyle = C.pole;
        ctx.lineWidth = 2;
        ctx.fillRect(ox - pw / 2, cy - R * 0.78, pw, R * 1.56);
        ctx.strokeRect(ox - pw / 2, cy - R * 0.78, pw, R * 1.56);
      }
    }

    if (estopFlash > 0 && (Math.abs(lastAvoidX) > 0.08 || Math.abs(lastAvoidY) > 0.08)) {
      const ax = cx + lastAvoidX * R * 0.62;
      const ay = cy + lastAvoidY * R * 0.45;
      ctx.strokeStyle = C.ok;
      ctx.lineWidth = 2.8;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(ax, ay);
      ctx.stroke();
      ctx.fillStyle = C.ok;
      ctx.beginPath();
      ctx.arc(ax, ay, 4.2, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillStyle = estopFlash > 0 ? C.ok : C.optic;
    let label = "LPLC2 " + lastLoomScore.toFixed(2) + " · LC4 " + lastLc4Score.toFixed(2);
    if (estopFlash > 0) {
      label = "DODGE! LPLC2 " + lastLoomScore.toFixed(2) + " LC4 " + lastLc4Score.toFixed(2);
    } else if (glareTicks > 0) {
      label = "100× glare shunted (0 false alarms)";
    }
    ctx.fillText(label, p.x + 20, p.y + p.h - 18);
  }

  function drawCord(p) {
    title(p, "NECKCORD · ACTIVE-SET + 4B CERVICAL");
    const labels = ["T1 port-fwd", "T2 port-aft", "T3 stbd-fwd", "T4 stbd-aft"];
    const top = p.y + 46, avail = p.h - 92, rowH = avail / 4;
    const lx = p.x + 20, bx = p.x + 120, bw = Math.max(40, p.w - 140);
    const barH = Math.min(15, rowH * 0.48);
    lastThrust.forEach((v, i) => {
      const y = top + i * rowH + (rowH - barH) / 2;
      ctx.fillStyle = C.text;
      ctx.font = "500 12px 'JetBrains Mono', monospace";
      ctx.fillText(labels[i], lx, y + barH - 3);
      ctx.fillStyle = C.barBg;
      ctx.fillRect(bx, y, bw, barH);
      ctx.fillStyle = (i === 1 && fouledT2) ? C.fault : C.bar;
      ctx.fillRect(bx, y, Math.max(2, Math.min(1, v) * bw), barH);
    });
    ctx.fillStyle = C.ok;
    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillText(
      (wasmEx ? "WASM 4B " : "Cervical 4B ") + lastWireHex + (fouledT2 ? " · T2 shunted" : " · CRC-4 OK"),
      lx, p.y + p.h - 18
    );
  }

  function draw() {
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, cssW, cssH);
    const ps = panels();
    ctx.strokeStyle = C.divider;
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (narrow) {
      ctx.moveTo(16, Math.round(ps[1].y) + 0.5); ctx.lineTo(cssW - 16, Math.round(ps[1].y) + 0.5);
      ctx.moveTo(16, Math.round(ps[2].y) + 0.5); ctx.lineTo(cssW - 16, Math.round(ps[2].y) + 0.5);
    } else {
      ctx.moveTo(ps[1].x + 0.5, 18); ctx.lineTo(ps[1].x + 0.5, cssH - 18);
      ctx.moveTo(ps[2].x + 0.5, 18); ctx.lineTo(ps[2].x + 0.5, cssH - 18);
    }
    ctx.stroke();
    drawCompass(ps[0]);
    drawOptic(ps[1]);
    drawCord(ps[2]);
  }

  function frame() {
    if (!running) return;
    step();
    draw();
    requestAnimationFrame(frame);
  }

  function start() {
    if (running) return;
    running = true;
    requestAnimationFrame(frame);
  }

  resize();
  draw();
  window.addEventListener("resize", () => { resize(); draw(); });

  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) start(); else running = false; });
    }).observe(canvas);
  } else {
    start();
  }
})();
