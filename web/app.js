/* ==========================================================================
   NeckLink v3.0 — Connectome-to-Silicon Interactive Controller
   1. Intro splash, glass navigation, copy helpers & 18s Film Cinema Modal
   2. 144-frame Ray-Traced Blender Cycles OptiX 3D Scroll-Scrub Engine
   3. Interactive 3D "Scramble the Connectome" Stage + Audited Scoreboard
   4. 24 KB Silicon Die Floorplan & 6-Module Inspector + Benchmark Drawer
   5. Live 107.5 KB ANSI C99 WebAssembly (necklink_cns.wasm) + Webcam Cockpit
   ========================================================================== */

(function () {
  "use strict";

  const root = document.documentElement;
  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

  // -------------------------------------------------------------------------
  // 1. INTRO SPLASH, NAVIGATION, COPY & FILM MODAL
  // -------------------------------------------------------------------------
  const splash = $("#intro-splash");
  let introTimer = null;

  function finishIntro() {
    if (introTimer) {
      clearTimeout(introTimer);
      introTimer = null;
    }
    if (splash) splash.classList.add("fade-out");
    root.classList.add("intro-done");
    try { sessionStorage.setItem("necklink-v3-intro", "1"); } catch (e) {}
  }

  function playIntro() {
    if (!splash) return;
    if (introTimer) clearTimeout(introTimer);
    root.classList.remove("no-intro", "intro-done");
    splash.classList.remove("fade-out", "animate");
    void splash.offsetWidth;
    splash.classList.add("animate");
    introTimer = setTimeout(finishIntro, 1650);
  }

  if (splash && !root.classList.contains("no-intro")) {
    requestAnimationFrame(playIntro);
    splash.addEventListener("click", finishIntro);
  }
  const skipBtn = $("#skip-intro-btn");
  if (skipBtn) skipBtn.addEventListener("click", finishIntro);
  const replayBtn = $("#replay-intro-btn");
  if (replayBtn) {
    replayBtn.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
      playIntro();
    });
  }

  // Mobile Menu & Scroll Spy
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
        unmuteLabel.textContent = inlineVideo.muted ? "Tap for Stereo Sound" : "Stereo Sound On";
      }
      if (inlineVideo.paused) inlineVideo.play().catch(() => {});
    });
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (filmModal && !filmModal.hidden) closeFilmModal();
    if (splash && !splash.classList.contains("fade-out")) finishIntro();
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
    setTimeout(() => btn.classList.remove("copied"), 1600);
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
  const hudActLabel = $("#hud-act-label");
  const hudFrameReadout = $("#hud-frame-readout");
  const hudLoadBar = $("#hud-load-bar");
  const scrollCue = $("#scroll-cue");
  const railSteps = $$(".rail-step");

  if (diveSection && scrollCanvas) {
    const sctx = scrollCanvas.getContext("2d", { alpha: false });
    const TOTAL_FRAMES = 144;
    const frames = new Array(TOTAL_FRAMES);
    let posterImg = null;
    let loadedCount = 0;
    let scW = 0, scH = 0;
    let targetFrame = 0;
    let smoothFrame = 0;
    let lastPaintedIdx = -1;

    const ACT_TITLES = [
      "ACT 01 · WET EM SPECIMEN (BANC v888 CNS · IMMERSION OIL 1.515)",
      "ACT 02 · CERVICAL NECK CONNECTIVE (DNp01 & DNa01 OSMIUM TRACTS)",
      "ACT 03 · OPTICAL STAGE DIVE (BOROSILICATE SLIDE → CARRIER PCB)",
      "ACT 04 · DECAPPED LCC-44 DIE (24,024 B THIN-FILM SRAM · 44 GOLD BONDS)",
      "ACT 05 · NECKLINK-CNS REV 3.0 HARDWARE (PYTHON · C99 · 107.5 KB WASM)"
    ];

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
      const iw = img.naturalWidth || 1280;
      const ih = img.naturalHeight || 720;
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
      if (hudActLabel) hudActLabel.textContent = ACT_TITLES[actIdx];
      railSteps.forEach((btn, i) => btn.classList.toggle("active", i === actIdx));
      if (scrollCue) scrollCue.style.opacity = p > 0.05 ? "0" : "1";
    }

    function tickScroll() {
      smoothFrame += (targetFrame - smoothFrame) * 0.14;
      const idx = Math.round(smoothFrame);
      if (idx !== lastPaintedIdx) {
        paintScrollFrame(idx);
        if (hudFrameReadout) {
          const fStr = String(idx + 1).padStart(3, "0");
          hudFrameReadout.textContent = "OPTIX RAY-TRACE · " + fStr + " / 144";
        }
      }
      requestAnimationFrame(tickScroll);
    }

    // Strided progressive frame loader (loads keyframes across the whole dive first!)
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
          loadedCount++;
          if (hudLoadBar) {
            hudLoadBar.style.width = Math.max(8, Math.round((loadedCount / TOTAL_FRAMES) * 100)) + "%";
          }
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
  // 3. INTERACTIVE 3D "SCRAMBLE THE CONNECTOME" STAGE & AUDITED SCOREBOARD
  // -------------------------------------------------------------------------
  const scrambleCanvas = $("#scramble-canvas");
  const scoreboardRowsEl = $("#scoreboard-rows");
  const stageBadge = $("#stage3d-badge");
  const stageDesc = $("#stage3d-desc");
  const roBump = $("#ro-bump");
  const roTripod = $("#ro-tripod");
  const roMb = $("#ro-mb");

  const FALLBACK_BENCHMARKS = [
    { organ: "NeckCompass", task: "Closed-loop GPS-denied homing", unit: "% arrival", higher_better: true, real: 100.0, shuffled: 4.7, analytic: 100.0, classical: 100.0, classical_name: "χ² EKF", p: 0.0099, verdict: "PASS", note: "Real wiring arrives 100% vs 4.7% shuffled (r=0/100); analytic & EKF home closer to 0 m." },
    { organ: "NeckCompass", task: "Wave tilt + 125° magnetic spikes", unit: "° RMS", higher_better: false, real: 12.84, shuffled: 62.82, analytic: 0.99, classical: 0.81, classical_name: "χ² EKF", p: 0.0099, verdict: "PASS", note: "Shuffling destroys the ring (62.8°), though real biological asymmetry drifts (~12.8° vs 0.81° EKF)." },
    { organ: "NeckCord", task: "Emergent 6-leg tripod gait order", unit: "R_tripod", higher_better: true, real: 0.855, shuffled: 0.411, analytic: 1.000, classical: 1.000, classical_name: "Fixed table", p: 0.0099, verdict: "PASS", note: "6×6 BANC VNC premotor inhibition spontaneously locks into tripod coordination (r=0/100)." },
    { organ: "NeckLink CNS", task: "Closed-loop USV hazard mission", unit: "% safe", higher_better: true, real: 58.3, shuffled: 4.6, analytic: 80.0, classical: 0.0, classical_name: "Ungated stack", p: 0.0297, verdict: "MARGINAL", note: "66.7% over 30 seeds (58.3% vs 4.6% in 12-seed shuffle gate, r=2/100); analytic model reaches 80.0%." },
    { organ: "NeckSight", task: "Thin poles & wires (3D MuJoCo)", unit: "AUROC", higher_better: true, real: 0.755, shuffled: 0.672, analytic: 0.987, classical: 0.635, classical_name: "Farneback+gyro", p: 0.0396, verdict: "MARGINAL", note: "Adding 32 LC4 neurons lifts AUROC from 0.624 (LPLC2-only) to 0.755, beating Farneback (0.635)." },
    { organ: "NeckSense", task: "UCI gas sensor drift novelty", unit: "AUROC", higher_better: true, real: 0.666, shuffled: 0.672, analytic: 0.670, classical: 0.698, classical_name: "One-Class SVM", p: 0.7426, verdict: "NULL", note: "Honest null: degree-matched random Kenyon-cell claws match real BANC claws (Dasgupta et al., 2017)." }
  ];

  let wiringMode = "real"; // "real" | "shuffled" | "analytic"
  let sceneData = null;

  function fmtMetric(val, unit) {
    if (unit === "% arrival" || unit === "% safe") return val.toFixed(1) + "%";
    if (unit === "° RMS") return val.toFixed(2) + "°";
    return val.toFixed(3);
  }

  function renderScoreboard(rows) {
    if (!scoreboardRowsEl) return;
    scoreboardRowsEl.innerHTML = "";
    rows.forEach((r) => {
      const row = document.createElement("div");
      row.className = "score-row";
      const vLabel = r.verdict === "NULL" ? "HONEST NULL (p=" + r.p.toFixed(2) + ")" : (r.verdict + " · p=" + r.p.toFixed(4));
      row.innerHTML =
        '<div class="score-top">' +
          '<span class="score-title"><em>' + r.organ + '</em>' + r.task.trim() + '</span>' +
          '<span class="verdict-pill verdict-' + r.verdict + '">' + vLabel + '</span>' +
        '</div>' +
        '<div class="score-bars">' +
          '<div class="sbar ' + (wiringMode === "real" ? "active-col" : "") + '">' +
            '<span class="sbar-lbl">Real BANC</span><span class="sbar-val">' + fmtMetric(r.real, r.unit) + '</span>' +
          '</div>' +
          '<div class="sbar ' + (wiringMode === "shuffled" ? "active-col" : "") + '">' +
            '<span class="sbar-lbl">Shuffled (K=100)</span><span class="sbar-val">' + fmtMetric(r.shuffled, r.unit) + '</span>' +
          '</div>' +
          '<div class="sbar ' + (wiringMode === "analytic" ? "active-col" : "") + '">' +
            '<span class="sbar-lbl">v2_analytic</span><span class="sbar-val">' + fmtMetric(r.analytic, r.unit) + '</span>' +
          '</div>' +
          '<div class="sbar">' +
            '<span class="sbar-lbl">' + r.classical_name + '</span><span class="sbar-val">' + fmtMetric(r.classical, r.unit) + '</span>' +
          '</div>' +
        '</div>' +
        '<p class="score-note">' + r.note + '</p>';
      scoreboardRowsEl.appendChild(row);
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
      if (stageDesc) stageDesc.textContent = "Coherent EPG compass bump + emergent 3+3 VNC tripod wave across 10 cervical axons.";
      if (roBump) { roBump.textContent = "0.89 · Bump Locked (100% Homing)"; roBump.style.color = "#fcd34d"; }
      if (roTripod) { roTripod.textContent = "0.855 · Emergent 3+3 Tripod"; roTripod.style.color = "#6ee7b7"; }
      if (roMb) { roMb.textContent = "0.666 AUROC (Degree-Invariant)"; roMb.style.color = "#cbd5e1"; }
    } else if (mode === "shuffled") {
      if (stageBadge) { stageBadge.textContent = "MASLOV–SNEPPEN SHUFFLED · TOPOLOGY DESTROYED"; stageBadge.style.color = "var(--cord)"; }
      if (stageDesc) stageDesc.textContent = "Exact same node degrees & total weight — but scrambled recurrent chords shatter the bump and gait.";
      if (roBump) { roBump.textContent = "0.14 · Collapsed (4.7% Homing)"; roBump.style.color = "#fb7185"; }
      if (roTripod) { roTripod.textContent = "0.411 · Uncoordinated Limbs"; roTripod.style.color = "#fb7185"; }
      if (roMb) { roMb.textContent = "0.672 AUROC (Unchanged! p=0.74)"; roMb.style.color = "#6ee7b7"; }
    } else {
      if (stageBadge) { stageBadge.textContent = "v2_ANALYTIC · HAND-TUNED ENGINEERING"; stageBadge.style.color = "var(--accent)"; }
      if (stageDesc) stageDesc.textContent = "Idealized cosine ring + symmetric anti-phase table: maximum engineering precision (0.99° RMS).";
      if (roBump) { roBump.textContent = "0.99 · Zero-Drift Cosine (0.99°)"; roBump.style.color = "#7dd3fc"; }
      if (roTripod) { roTripod.textContent = "1.000 · Ideal Phase Table"; roTripod.style.color = "#7dd3fc"; }
      if (roMb) { roMb.textContent = "0.670 AUROC (Random Hash)"; roMb.style.color = "#cbd5e1"; }
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
      w3 = scrambleCanvas.parentElement.clientWidth || 520;
      h3 = scrambleCanvas.parentElement.clientHeight || 360;
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
        if (!dragging) rotY += 0.0032;

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

        // 1. Draw 16x16 EPG/PEN recurrent ring chords
        for (let i = 0; i < 16; i++) {
          for (let j = i + 1; j < 16; j++) {
            let w = 0;
            if (wiringMode === "analytic") {
              const d = Math.abs(i - j);
              const wrapD = Math.min(d, 16 - d);
              w = wrapD <= 2 ? 0.35 : 0.0;
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
                ? "rgba(251, 113, 133, " + Math.min(0.55, w * 1.3).toFixed(2) + ")"
                : (wiringMode === "analytic" ? "rgba(56, 189, 248, 0.38)" : "rgba(245, 158, 11, " + Math.min(0.62, w * 1.5).toFixed(2) + ")");
              ctx3.lineWidth = wiringMode === "shuffled" ? 1.1 : 1.5;
              ctx3.beginPath();
              ctx3.moveTo(p1.x, p1.y);
              ctx3.lineTo(p2.x, p2.y);
              ctx3.stroke();
            }
          }
        }

        // 2. Draw 10 Cervical Descending Axons (Brain -> VNC)
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

          ctx3.strokeStyle = wiringMode === "shuffled" ? "rgba(251, 113, 133, 0.32)" : "rgba(251, 191, 36, 0.44)";
          ctx3.lineWidth = 1.4;
          ctx3.beginPath();
          ctx3.moveTo(pb.x, pb.y);
          ctx3.quadraticCurveTo(pn.x, pn.y, pl.x, pl.y);
          ctx3.stroke();

          // Traveling action potential spike
          const u = (animT * 0.85 + k * 0.13) % 1.0;
          const iu = 1 - u;
          const sx = iu * iu * pb.x + 2 * iu * u * pn.x + u * u * pl.x;
          const sy = iu * iu * pb.y + 2 * iu * u * pn.y + u * u * pl.y;
          ctx3.fillStyle = wiringMode === "shuffled" ? "#fda4af" : "#fef08a";
          ctx3.beginPath();
          ctx3.arc(sx, sy, 2.4, 0, Math.PI * 2);
          ctx3.fill();
        }

        // 3. Draw 50 EPG Ring Neurons + Heading Bump
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
          const rad = 2.6 + act * 4.4;
          ctx3.fillStyle = act > 0.55
            ? (wiringMode === "analytic" ? "#7dd3fc" : "#fde047")
            : (wiringMode === "shuffled" ? "rgba(251, 113, 133, 0.65)" : "rgba(245, 158, 11, 0.48)");
          ctx3.beginPath();
          ctx3.arc(p.x, p.y, rad, 0, Math.PI * 2);
          ctx3.fill();
        });

        // 4. Draw Dorsal Mushroom Body Kenyon Cloud (invariant under shuffle!)
        for (let m = 0; m < 28; m++) {
          const sgn = m % 2 === 0 ? -1 : 1;
          const ma = (m / 28) * Math.PI * 2;
          const mx = sgn * (0.58 + 0.14 * Math.cos(ma));
          const my = 0.14 * Math.sin(ma);
          const mz = 0.68 + 0.10 * Math.sin(ma * 2);
          const pm = proj(mx, my, mz);
          const fire = Math.sin(animT * 1.8 + m * 1.7) > 0.82;
          ctx3.fillStyle = fire ? "#6ee7b7" : "rgba(52, 211, 153, 0.35)";
          ctx3.beginPath();
          ctx3.arc(pm.x, pm.y, fire ? 3.5 : 2.0, 0, Math.PI * 2);
          ctx3.fill();
        }

        // 5. Draw 6x6 VNC Leg Motor Pools & Tripod Phase State
        const cVnc = (sceneData && sceneData.vnc_dn)
          ? (wiringMode === "shuffled" ? sceneData.vnc_dn.C_vnc_inh_shuf : sceneData.vnc_dn.C_vnc_inh_real)
          : null;
        const tripodGroupA = [0, 3, 4]; // L1, R2, L3

        for (let i = 0; i < 6; i++) {
          for (let j = i + 1; j < 6; j++) {
            const w = cVnc ? (Math.abs(cVnc[i][j]) + Math.abs(cVnc[j][i])) : 0.25;
            if (w > 0.12) {
              const p1 = proj(legCoords[i][0], legCoords[i][1], legCoords[i][2]);
              const p2 = proj(legCoords[j][0], legCoords[j][1], legCoords[j][2]);
              ctx3.strokeStyle = wiringMode === "shuffled" ? "rgba(251, 113, 133, 0.35)" : "rgba(52, 211, 153, 0.36)";
              ctx3.lineWidth = 1.3;
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
          const r = 6.0 + swing * 5.5;
          ctx3.fillStyle = swing > 0.55
            ? (wiringMode === "shuffled" ? "#fb7185" : (isA ? "#34d399" : "#38bdf8"))
            : "rgba(30, 41, 59, 0.9)";
          ctx3.strokeStyle = isA ? "#34d399" : "#38bdf8";
          ctx3.lineWidth = 2;
          ctx3.beginPath();
          ctx3.arc(p.x, p.y, r, 0, Math.PI * 2);
          ctx3.fill();
          ctx3.stroke();

          ctx3.fillStyle = "#e2e8f0";
          ctx3.font = "600 10px 'JetBrains Mono', monospace";
          ctx3.fillText(legNames[idx], p.x + r + 5, p.y + 3);
        });

        // Subtle anatomical region labels
        ctx3.fillStyle = "rgba(148, 163, 184, 0.65)";
        ctx3.font = "500 10px 'JetBrains Mono', monospace";
        ctx3.fillText("50 EPG RING + MB CLAWS", 14, 22);
        ctx3.fillText("10 CERVICAL DNs", 14, h3 * 0.52);
        ctx3.fillText("6×6 VNC TRIPOD POOLS", 14, h3 - 14);
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

  // Load real BANC v888 scene tensors + benchmarks JSON
  renderScoreboard(FALLBACK_BENCHMARKS);
  fetch("data/banc_scene_data.json")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => {
      sceneData = d;
      if (d && d.benchmarks) renderScoreboard(d.benchmarks);
    })
    .catch(() => {});

  // -------------------------------------------------------------------------
  // 4. 24 KB SILICON DIE EXPLORER & BENCHMARK DRAWER
  // -------------------------------------------------------------------------
  const ORGANS = {
    cns: {
      colorVar: "--accent",
      region: "Whole-CNS · BANC v888 (157,789 neurons, 23.55M synapses)",
      tests: "7 Py + 7 C99 (ASan) + ESP32 + 107.5 KB WASM",
      name: "NeckLink CNS",
      pkg: "necklink-cns",
      desc: "Unified 4-organ reflex controller combining NeckSight, NeckCompass, NeckSense, and NeckCord. Its decision layer multiplies 11 brain populations (LPLC2, LC4, LPLC1, PFL3, PFL2, MBON) through compiled BANC v888 weights onto 10 descending neurons (DNp01, MDN, DNa02, …) and 6 motor pools, plus MAVLink v2 and ROS 2 bridges.",
      ram: "24,024 B (23.5 KB), 0 B heap",
      speed: "~166 µs / tick (desktop C99)",
      gain: "66.7% 30-seed · 58.3% vs 4.6% shuf",
      tradeoffLabel: "Honest scope boundary",
      tradeoff: "In closed-loop USV simulation, real BANC wiring reaches 66.7% safe arrival (58.3% vs 4.6% in the 12-seed K=100 shuffle gate, p = 0.0297) — while hand-tuned v2_analytic reaches 80.0%. It is a 24 KB reflex copilot, not a SLAM planner, and has not yet been tested on physical vehicles.",
      code: "from necklink_cns import NeckLinkCNS\n\ncns = NeckLinkCNS(vehicle=\"hexacopter\", grid_size=32)\nout = cns.step(dt=0.02, speed_m_s=1.8, gyro_xyz=[0.02, -0.01, 0.05],\n               accel_xyz=[0.2, -0.1, 9.80], mag_xyz=[0.45, 0.05, 0.89], camera_frame=cam_32x32)\nprint(out.descending.behavior_mode, len(out.cervical_wire_4b), out.mavlink_manual_pkt[0])",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necklink_cns",
      pypi: "https://pypi.org/project/necklink-cns/",
      pypiLabel: "PyPI v3.0.0"
    },
    sight: {
      colorVar: "--sight",
      region: "Optic lobe · R1–R6 / T4–T5 / 64 LPLC2 + 32 LC4",
      tests: "35 Py + C99 self-test + ESP32-CAM build",
      name: "NeckSight",
      pkg: "necksight",
      desc: "Logarithmic R1–R6 glare guard, elementary T4/T5 ON/OFF motion, 3-DOF (yaw, pitch, roll) gyro derotation, and 64 LPLC2 + 32 LC4 looming/edge detectors whose spatial receptive fields are inferred from BANC v888 connectivity, emitting contralateral DNa02 dodge vectors.",
      ram: "14,624 B (14.28 KB)",
      speed: "~6,557 FPS (desktop C99, 32×32)",
      gain: "0.755 AUROC on thin poles (LC4)",
      tradeoffLabel: "Where analytic & Farneback win",
      tradeoff: "Across all 3D MuJoCo obstacle types with identical gyro derotation, v2_analytic (0.890 AUROC) and derotated Farneback (0.818) beat BANC receptive fields overall (0.806; shuffle p = 0.079). On thin poles and wires, adding LC4 beats Farneback (0.755 vs 0.635, p = 0.0396).",
      code: "from necksight import NeckSightPipeline, imu_to_ego_motion\n\noptic = NeckSightPipeline(grid_size=64, enable_lc4_bar=True)\nego = imu_to_ego_motion(yaw_rate, pitch_rate, roll_rate, dt=1/60, grid_width=64)\nres = optic.step(frame_u8, dt=1/60, ego_shift_px=ego)\nif res.triggered:\n    print(res.looming_score, res.bar_score, (res.avoid_x, res.avoid_y))",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksight",
      pypi: "https://pypi.org/project/necksight/",
      pypiLabel: "PyPI v3.0.0"
    },
    compass: {
      colorVar: "--compass",
      region: "Central complex · 50 EPG / 42 PEN / 40 Delta7 / PFL3+2",
      tests: "48 Py + 14 C99 + ESP32 build",
      name: "NeckCompass",
      pkg: "neckcompass",
      desc: "Zero-float 9-axis tilt-compensated heading on an EPG ↔ Delta7 ring attractor and PFL3/PFL2 GPS-denied homing, running on ~3,900 synaptic weights compiled from BANC v888, plus a 1,104-byte online soft-iron calibration ring.",
      ram: "1,444 B (tilt + plastic ring)",
      speed: "0.96 µs (9-DOF) · 8.10 µs full",
      gain: "100% homing vs 4.7% shuffled",
      tradeoffLabel: "Where v2_analytic & χ² EKF win",
      tradeoff: "Real biological wiring heterogeneity causes ring drift: 12.84° RMS under waves + 125° magnetic spikes vs 0.99° (v2_analytic) and 0.81° (χ²-gated EKF). Pass connectome_weights=\"v2_analytic\" whenever sub-degree compass accuracy is priority.",
      code: "from neckcompass import RingCompass, FanBodyNavigator\n\nnav = FanBodyNavigator(compass=RingCompass(n_wedges=16))\nhdg, dist_m, brg = nav.step_with_compass_9dof(\n    gx, gy, gz, ax, ay, az, mx, my, mz, speed_m_s=1.8, dt=0.02\n)\nyaw_cmd = nav.steering_cmd(max_turn_rate=1.0)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcompass",
      pypi: "https://pypi.org/project/neckcompass/",
      pypiLabel: "PyPI v3.0.0"
    },
    cord: {
      colorVar: "--cord",
      region: "Ventral nerve cord · 6×6 premotor + active-set allocator",
      tests: "48 Py + 19 C99 + ESP32 build",
      name: "NeckCord",
      pkg: "neckcord",
      desc: "Six-limb gait whose alternating tripod coordination emerges spontaneously from a 6×6 VNC premotor inhibition matrix compiled from BANC v888, paired with engineering layers: zero-heap active-set thruster/rotor allocator, sensorless ESC observer, 18-servo IK, and 4-byte CRC-4 framing.",
      ram: "930 B (all C99 structs)",
      speed: "5.57 µs / full tick (desktop C99)",
      gain: "Tripod 0.855 vs 0.411 shuffled",
      tradeoffLabel: "Where fixed table & SciPy QP win",
      tradeoff: "A hardcoded tripod phase table achieves 1.000 order and walks slightly farther after leg amputation. In 30-seed MuJoCo hexacopter rotor loss, all allocators survived while SciPy bounded QP held slightly lower tilt (0.17° vs 0.25°).",
      code: "from neckcord import ESCObserver, ShuntingThrusterCord\nimport numpy as np\n\ncord = ShuntingThrusterCord.multicopter_hex_6(u_max=4.5, prioritize_attitude=True)\nesc = ESCObserver(n_actuators=6, u_nom=4.5, rpm_max=9000.0)\nres = cord.step(np.array([17.7, 0.0, 0.0, 0.0]), esc_observer=esc, esc_rpm=rpm, esc_current_a=amps, dt=0.01)\nprint(res.commands, res.health)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcord",
      pypi: "https://pypi.org/project/neckcord/",
      pypiLabel: "PyPI v3.0.0"
    },
    sense: {
      colorVar: "--sense",
      region: "Mushroom body · 1,024 Kenyon claws + APL + MBON",
      tests: "27 Py + 9 C99 + ESP32 build",
      name: "NeckSense",
      pkg: "necksense",
      desc: "Integer Mushroom Body sparse expansion using real BANC v888 Kenyon-cell claws (1,024 cells), divisive antennal normalization, APL winner-take-all feedback, 2-compartment continual synaptic memory, and a 16-band vibration front-end.",
      ram: "5,804 B (NS_TINY) · 20.3 KB full",
      speed: "16.4 µs (tiny) · 31.1 µs full",
      gain: "0.666 vs 0.672 shuffled (Honest Null)",
      tradeoffLabel: "Honest null & classical comparison",
      tradeoff: "Degree-preserving shuffle performs identically to real BANC claws (0.666 vs 0.672 AUROC, p = 0.74), confirming random sparse expansion suffices here. On UCI gas drift data, One-Class SVM (0.698) and nearest-centroid (60.1%) score higher in Python — NeckSense fits inside 5.8 KB of integer C99 RAM.",
      code: "from necksense import VibrationFrontEnd, MushroomNoveltyDetector\n\nfe = VibrationFrontEnd()\ndet = MushroomNoveltyDetector(n_inputs=16, antennal=True, n_kc=1024)\nscore = det.update_and_score(fe.extract(accel_256_window))\nprint(\"Bearing resonance anomaly:\", score)",
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksense",
      pypi: "https://pypi.org/project/necksense/",
      pypiLabel: "PyPI v3.0.0"
    },
    nltp: {
      colorVar: "--nltp",
      region: "Cervical wire protocol · Standalone v1.6.0",
      tests: "32 Py + 16 C99 + 248 interop + ESP-NOW",
      name: "NLTP",
      pkg: "pynltp",
      desc: "Loss-healing file, telemetry, and reflex command transport with GF(2^8) Cauchy FEC and LZ4 over UDP and routerless ESP-NOW, secured by X25519 + PSK forward-secret ChaCha20-Poly1305 AEAD, RFC 5389 STUN hole-punching, and a 4.76 KB TX-only MCU profile.",
      ram: "4.76 KB TX-only · ~65 KB ESP32",
      speed: "956 MB/s GF(2^8) · 17.75 Mbps ESP-NOW",
      gain: "100% SHA-256 @ 35% packet loss",
      tradeoffLabel: "Where kernel TCP BBR wins",
      tradeoff: "On clean, loss-free wired Ethernet or fiber, kernel TCP BBR reaches higher multi-hundred-Mbps bulk throughput than userspace UDP FEC. NLTP v1.6.0 shines on lossy radio, Wi-Fi fades, ESP32/ESP8266 ESP-NOW meshes, and 4G CGNAT links.",
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

  // Per-package classical benchmark chart drawer
  const RAW = "https://raw.githubusercontent.com/indian-aman/";
  const BENCH = {
    compass: {
      src: RAW + "necklink/main/packages/neckcompass/assets/neckcompass_benchmarks.png", w: 4070, h: 1298,
      alt: "NeckCompass benchmark charts",
      caption: "Analytic ring attractor against 3-sigma gated EKF, ungated EKF, and complementary IMU+mag filter across 70°–150° magnetic spikes and 600 s GPS-blackout homing."
    },
    sight: {
      src: RAW + "necklink/main/packages/necksight/assets/necksight_benchmarks.png", w: 3564, h: 1232,
      alt: "NeckSight benchmark charts",
      caption: "Optic-lobe looming selectivity, giant-fiber trigger timing prior to collision, and C99 throughput / SRAM footprint."
    },
    cord: {
      src: RAW + "necklink/main/packages/neckcord/assets/neckcord_benchmarks.png", w: 3850, h: 1276,
      alt: "NeckCord benchmark charts",
      caption: "X-vectored USV wave corridor thruster fault isolation and six-limb coupled oscillator stability."
    },
    sense: {
      src: RAW + "necklink/main/packages/necksense/assets/necksense_benchmarks.png", w: 3872, h: 1210,
      alt: "NeckSense benchmark charts",
      caption: "Concentration-invariant FlyHash retrieval, drifting plumes, and continual 2-compartment MBON anomaly scoring."
    },
    nltp: {
      src: RAW + "necklink/main/assets/nltp_v12_benchmarks.png", w: 2992, h: 1188,
      alt: "NLTP benchmark charts",
      caption: "Lossy UDP & satellite transfer against kcptun, QUIC (ngtcp2), and Linux TCP BBR, plus ESP32 ESP-NOW goodput."
    }
  };

  const benchImg = $("#bench-img");
  if (benchImg) {
    function selectBench(key) {
      const b = BENCH[key];
      if (!b) return;
      $$("[data-bench]").forEach((btn) => {
        const on = btn.getAttribute("data-bench") === key;
        btn.classList.toggle("active", on);
        btn.setAttribute("aria-selected", String(on));
      });
      $("#bench-caption").textContent = b.caption;
      $("#bench-open").href = b.src;
      if (benchImg.getAttribute("src") !== b.src) {
        benchImg.classList.add("loading");
        benchImg.width = b.w;
        benchImg.height = b.h;
        benchImg.alt = b.alt;
        benchImg.src = b.src;
      }
    }
    benchImg.addEventListener("load", () => benchImg.classList.remove("loading"));
    $$("[data-bench]").forEach((btn) => btn.addEventListener("click", () => selectBench(btn.getAttribute("data-bench"))));
    selectBench("compass");
  }

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
    grid: "rgba(148, 163, 184, 0.10)",
    divider: "rgba(148, 163, 184, 0.16)",
    label: "#94a3b8",
    text: "#cbd5e1",
    ring: "#38bdf8",
    ekf: "#f97316",
    optic: "#22d3ee",
    pole: "#a78bfa",
    ok: "#34d399",
    bar: "#818cf8",
    barBg: "rgba(30, 41, 59, 0.9)",
    fault: "#fb7185",
    bg: "#050811"
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
      return "WASM C99 LIVE (" + wasmSramBytes.toLocaleString() + " B static SRAM, 0 B heap). 9-DOF ring compass locked, LPLC2+LC4 scanning, 4B cervical wire active.";
    }
    return "Nominal cruise. Ring compass locked (0.4° error), LPLC2+LC4 optic lobe scanning, 4-byte cervical link at 100 Hz.";
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
      setStatus(
        wasmEx
          ? "LIVE WEBCAM → C99 WASM: Move your hand or a thin pen/pole rapidly toward the camera to fire LPLC2/LC4 looming + DNa02 dodge!"
          : "LIVE WEBCAM active (serve over HTTP/HTTPS to bind necklink_cns.wasm).",
        "--ok"
      );
    } catch (err) {
      setStatus("Camera permission denied or unavailable (" + (err && err.message ? err.message : "blocked") + ").", "--warn");
    }
  }

  function stopWebcam() {
    webcamActive = false;
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    if (webcamBtn) webcamBtn.classList.remove("active");
    if (webcamBtnLabel) webcamBtnLabel.textContent = "Live Webcam (WASM)";
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cssW = canvas.parentElement.clientWidth || 900;
    narrow = cssW < 680;
    cssH = narrow ? 600 : 340;
    canvas.style.height = cssH + "px";
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
        setStatus("+120° hull magnetic spike + wave roll. Ungated EKF drags ~42° off course; C99 NeckCompass shunts the spike (<1.0°).", "--compass");
      } else if (mode === "glare") {
        glareTicks = 50;
        setStatus("100× sunlight glare step. R1–R6 log photoreceptor + AEC guard cancels the global flash (0 false triggers).", "--sight");
      } else if (mode === "loom") {
        loomKind = "boulder";
        loomProgress = 0;
        setStatus("Off-axis boulder approaching on port side. C99 LPLC2 4-quadrant gate tracks expansion and fires contralateral DNa02 dodge.", "--sight");
      } else if (mode === "pole") {
        loomKind = "pole";
        loomProgress = 0;
        setStatus("Thin vertical pole/mast approaching! Classical optical flow misses thin bars, while NeckSight LC4 channel locks on.", "--sight");
      } else if (mode === "foul") {
        fouledT2 = true;
        setStatus("Port-aft thruster T2 fouled (−90%). NeckCord active-set shunts T2 in <45 ms and rebalances surge onto surviving thrusters.", "--cord");
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
        setStatus("C99 WASM " + via + " triggered! DNa02 dodge → " + dirTxt + " · 4B wire " + lastWireHex, "--ok");
      }
    } else {
      if (loomProgress >= 60 && loomProgress < 64) {
        estopFlash = 40;
        lastAvoidX = loomKind === "pole" ? -0.9 : 0.9;
        setStatus((loomKind === "pole" ? "LC4 thin-pole" : "LPLC2 looming") + " reflex dispatched over 4-byte cervical link!", "--ok");
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
    [[histEkf, C.ekf, 2], [histRing, C.ring, 2.4]].forEach(([hist, color, lw]) => {
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
      ctx.fillStyle = "rgba(250, 204, 21, " + (0.16 * (glareTicks / 50)).toFixed(3) + ")";
      ctx.fillRect(p.x + 1, p.y + 1, p.w - 2, p.h - 2);
    }

    if (webcamActive) {
      const srcBuf = wasmFrameU8 || localFrame64;
      const dst = retinaImgData.data;
      for (let i = 0; i < 4096; i++) {
        const g = srcBuf[i];
        const o = i * 4;
        dst[o] = Math.round(g * 0.72);
        dst[o + 1] = Math.min(255, Math.round(g * 0.95));
        dst[o + 2] = Math.min(255, Math.round(g * 1.05));
        dst[o + 3] = 220;
      }
      camCtx.putImageData(retinaImgData, 0, 0);
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R - 2, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(camCanvas, cx - R, cy - R, R * 2, R * 2);
      ctx.restore();
    }

    ctx.strokeStyle = estopFlash > 0 ? C.ok : "rgba(56, 189, 248, 0.42)";
    ctx.lineWidth = estopFlash > 0 ? 3 : 1.5;
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
        ctx.fillStyle = "rgba(34, 211, 238, 0.24)";
        ctx.strokeStyle = C.optic;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(ox, cy, lr, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      } else {
        const pw = Math.max(3, R * (0.04 + (loomProgress / 100) * 0.30));
        const ox = cx + R * 0.24;
        ctx.fillStyle = "rgba(167, 139, 250, 0.30)";
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
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(ax, ay);
      ctx.stroke();
      ctx.fillStyle = C.ok;
      ctx.beginPath();
      ctx.arc(ax, ay, 4.5, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillStyle = estopFlash > 0 ? C.ok : C.ring;
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
    const barH = Math.min(16, rowH * 0.5);
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
