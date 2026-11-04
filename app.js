/* ==========================================================================
   NeckLink Interactive Brand Website Controller
   1. SVG Logo Stroke-Draw -> Ring Ignition -> Smooth Fade-to-Site Intro
   2. Interactive 5-Organ Drosophila CNS Explorer
   3. Real-Time 60 Hz Closed-Loop Reflex Canvas Simulator
   4. Theme Switcher, Install Bar & Benchmark Gallery
   ========================================================================== */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // 1. CINEMATIC LOGO INTRO -> FADE INTO WEBSITE
  // -------------------------------------------------------------------------
  const splash = document.getElementById("intro-splash");
  const siteShell = document.getElementById("site-shell");
  const skipBtn = document.getElementById("skip-intro-btn");
  const replayBtn = document.getElementById("replay-intro-btn");
  let introTimer = null;

  function finishIntro() {
    if (introTimer) {
      clearTimeout(introTimer);
      introTimer = null;
    }
    splash.classList.add("fade-out");
    siteShell.classList.add("revealed");
    document.body.classList.remove("intro-active");
  }

  function playIntro() {
    if (introTimer) clearTimeout(introTimer);
    document.body.classList.add("intro-active");
    siteShell.classList.remove("revealed");
    splash.classList.remove("fade-out", "animate");
    // Force reflow so SVG stroke-dashoffset animations restart cleanly
    void splash.offsetWidth;
    splash.classList.add("animate");
    introTimer = setTimeout(finishIntro, 2350);
  }

  if (skipBtn) skipBtn.addEventListener("click", finishIntro);
  if (replayBtn) replayBtn.addEventListener("click", playIntro);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !splash.classList.contains("fade-out")) {
      finishIntro();
    }
  });

  // Respect prefers-reduced-motion while still showing a brief clean fade
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    finishIntro();
  } else {
    requestAnimationFrame(playIntro);
  }

  // -------------------------------------------------------------------------
  // 2. DARK / LIGHT THEME TOGGLE
  // -------------------------------------------------------------------------
  const themeBtn = document.getElementById("theme-toggle-btn");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const html = document.documentElement;
      const next = html.getAttribute("data-theme") === "light" ? "dark" : "light";
      html.setAttribute("data-theme", next);
      themeBtn.textContent = next === "light" ? "☾ Dark" : "☀ Light";
    });
  }

  // -------------------------------------------------------------------------
  // 3. ONE-CLICK INSTALL TABS & CLIPBOARD COPY
  // -------------------------------------------------------------------------
  const installCode = document.getElementById("install-code");
  const copyBtn = document.getElementById("copy-cmd-btn");
  let currentCmd = "pip install pynltp neckcompass necksight neckcord";

  document.querySelectorAll(".install-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".install-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentCmd = btn.getAttribute("data-cmd") || currentCmd;
      installCode.textContent = "$ " + currentCmd;
    });
  });

  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(currentCmd);
        copyBtn.textContent = "Copied ✓";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
      } catch (_) {
        copyBtn.textContent = "Select & Copy";
      }
    });
  }

  // -------------------------------------------------------------------------
  // 4. INTERACTIVE 5-ORGAN DROSOPHILA CNS EXPLORER
  // -------------------------------------------------------------------------
  const ORGANS = {
    compass: {
      color: "#F59E0B",
      region: "Central Complex · E-PG / CPU4 / PFL3",
      tests: "19 Py + 6 C99 Tests Passed",
      title: "NeckCompass (neckcompass)",
      desc: "Topological 16-wedge ring-attractor AHRS and Fan-Shaped Body path integrator. Asymmetric P-EN velocity shifters integrate gyro yaw rate with zero ±180° wrap-around seam while coincidence-gated R-neurons shunt 70°–150° steel-hull magnetic spikes before they corrupt gyro bias.",
      ram: "316 B (Q15)",
      speed: "0.83 µs / step",
      gain: "10.2× Spike Rejection",
      tradeoff: "Honest Baseline Trade-Off: Under pure textbook Gaussian noise with zero ferromagnetic anomalies, a 2-State Circular EKF edges out the 16-wedge ring (0.39° vs 0.43° RMSE). NeckCompass wins under intermittent hull/motor spikes (1.70° vs 17.42° RMSE) and 1.12 km GPS-blackout homing (0.84 m miss).",
      code: `from neckcompass import RingCompass, FanBodyNavigator\n\ncompass = RingCompass(n_wedges=16)\nnav = FanBodyNavigator(n_columns=16, compass=compass)\n\nhdg, home_dist_m, home_brg = nav.step_with_compass(\n    omega_rad_s=0.12, speed_m_s=1.8, dt=0.02, mag_heading_rad=2.1\n)\nyaw_cmd = nav.steering_cmd(max_turn_rate=1.0)`,
      repo: "https://github.com/indian-aman/neckcompass",
      pypi: "https://pypi.org/project/neckcompass/",
      pypiLabel: "PyPI v1.0.0 ↗"
    },
    sight: {
      color: "#06B6D4",
      region: "Optic Lobe · R1–R6 / T4–T5 / LPLC2",
      tests: "19 Py + 6 C99 Tests Passed",
      title: "NeckSight (necksight)",
      desc: "Logarithmic R1–R6 photoreceptor glare cancellation, ON/OFF lamina split, 3-arm Reichardt/Barlow-Levick motion detectors, and a 4-quadrant LPLC2 radial looming gate that dispatches Giant Fiber E-stop reflexes ~403 ms before collision.",
      ram: "14.27 KB (32×32)",
      speed: "12,463 FPS C99",
      gain: "0.9988 AUROC (0% FAR)",
      tradeoff: "Honest Baseline Trade-Off: On slow 0.05–0.35 px/frame sub-pixel visual odometry, Windowed Lucas-Kanade achieves 2.4× lower metric velocity MAE (0.055 vs 0.133 px/fr). NeckSight wins on pre-collision reflex gating under 100× sunlight glare steps and fast camera snap-pans (0.0% vs 55.6% FAR).",
      code: `from necksight import NeckSightPipeline\n\noptic = NeckSightPipeline(grid_size=64, threshold=0.045)\ntrigger = optic.step(camera_frame_u8, dt=1/60)\nif trigger.triggered:\n    print(f"LOOM E-STOP! score={trigger.looming_score:.3f} focus={trigger.focus_xy}")`,
      repo: "https://github.com/indian-aman/necksight",
      pypi: "https://pypi.org/project/necksight/",
      pypiLabel: "PyPI v1.0.0 ↗"
    },
    cord: {
      color: "#F43F5E",
      region: "Ventral Nerve Cord · BANC / MANC",
      tests: "22 Py + 10 C99 Tests Passed",
      title: "NeckCord (neckcord)",
      desc: "Compresses descending brain motor intent into a 4-byte CRC-4 frame (400 B/s @ 100 Hz) driving a self-healing 6-limb Kuramoto CPG and an online presynaptic-shunting thruster wrench allocator that isolates fouled propulsors in 42 ms.",
      ram: "304 B (Q15/Q14)",
      speed: "42 ms Fault Isolation",
      gain: "5.1× Less USV Spin-Out",
      tradeoff: "Honest Baseline Parity: When 100% of thrusters are healthy, Static Moore-Penrose Pseudo-Inverse matches NeckCord within 1 mm RMS cross-track error (0.108 m vs 0.109 m). NeckCord wins the instant a thruster fouls or jams (0.02 N wasted drive vs 22.7 N; 100% 5-leg CoM stability).",
      code: `from neckcord import DescendingFrame, ShuntingThrusterCord, encode_descending_4b\n\nwire_4b = encode_descending_4b(DescendingFrame(drive_fwd=0.7, turn_yaw=-0.2))\nres = allocator.step(wire_4b, feedback_thrust=esc_thrust_n, dt=0.02)\nprint("Thruster commands (N):", res.commands, "Health:", res.health)`,
      repo: "https://github.com/indian-aman/neckcord",
      pypi: "https://pypi.org/project/neckcord/",
      pypiLabel: "PyPI v1.0.0 ↗"
    },
    sense: {
      color: "#10B981",
      region: "Antennal Lobe & Mushroom Body · KC / DAN / MBON",
      tests: "14 Py + 5 C99 Tests Passed",
      title: "NeckSense (necksense)",
      desc: "Dual-timescale ORN thermal drift removal + Olsen-Wilson divisive normalization feeding a multiplication-free 5% Winner-Take-All Kenyon FlyHash, Popcount index, and 1-shot anti-Hebbian MBON novelty & associative valence memory.",
      ram: "8.8 KB (M=1024)",
      speed: "23.0 µs / sample",
      gain: "99.5% 1-Shot Plume ID",
      tradeoff: "Honest Baseline Trade-Off: At equal uncompressed dense bitword bytes (128 B = 1,024 bits) with 256 KB FPU weight RAM, 50%-dense Gaussian SimHash packs higher entropy than 5%-sparse FlyHash (50.6% vs 27.5% mAP). FlyHash wins by 1.6×–1.9× at equal active spikes k with 0 float multiplications.",
      code: `from necksense import MushroomNoveltyDetector\n\ndet = MushroomNoveltyDetector(n_inputs=8, stateful_antennal=True, n_kc=1024)\nnovelty = det.update_and_score(enose_8ch_reading)\nif novelty > 0.75:\n    print(f"1-shot chemical plume anomaly detected: {novelty:.3f}")`,
      repo: "https://github.com/indian-aman/necksense",
      pypi: "https://github.com/indian-aman/necksense/releases/tag/v1.0.0",
      pypiLabel: "GitHub Wheel v1.0.0 ↗"
    },
    nltp: {
      color: "#0EA5E9",
      region: "Cervical Connective · Loss-Healing Wire Protocol",
      tests: "23 Py + 210 Interop + 48 ESP32",
      title: "NLTP (pynltp / necklink)",
      desc: "Zero-ACK loss-healing telemetry codec + GF(2^8) systematic Cauchy FEC file & Giant-Fiber reflex UDP transport secured by ChaCha20-Poly1305 AEAD, RFC 5389 STUN hole-punching, and a zero-knowledge 1-port 4G CGNAT relay.",
      ram: "~65 KB on ESP32",
      speed: "5 Languages Interop",
      gain: "20% Loss Zero-Stall",
      tradeoff: "Honest Baseline Trade-Off: On clean, zero-loss wired fiber/LAN links, kernel TCP BBR achieves higher multi-hundred-Mbps bulk throughput than userspace UDP FEC. NLTP wins on lossy marine radio, Wi-Fi fades, ESP32 nodes, and 4G CGNAT links where TCP stalls.",
      code: `from pynltp import NLTPClient, generate_nltp_key\n\nwith NLTPClient(("127.0.0.1", 9000), psk=generate_nltp_key()) as cli:\n    cli.send_reflex("LOOM_ESTOP")\n    receipt = cli.send_file("telemetry_batch.bin")\n    assert receipt["verified_sha256"]`,
      repo: "https://github.com/indian-aman/necklink",
      pypi: "https://pypi.org/project/pynltp/",
      pypiLabel: "PyPI v1.3.1 ↗"
    }
  };

  function selectOrgan(key) {
    const d = ORGANS[key];
    if (!d) return;

    document.querySelectorAll(".cns-node").forEach((el) => {
      el.classList.toggle("active", el.getAttribute("data-organ") === key);
    });
    document.querySelectorAll("#explorer .organ-pill").forEach((btn) => {
      const isAct = btn.getAttribute("data-organ") === key;
      btn.classList.toggle("active", isAct);
      btn.style.background = isAct ? d.color : "";
    });

    const reg = document.getElementById("det-region");
    reg.textContent = d.region;
    reg.style.background = d.color + "22";
    reg.style.color = d.color;

    document.getElementById("det-tests").textContent = d.tests;
    document.getElementById("det-title").textContent = d.title;
    document.getElementById("det-desc").textContent = d.desc;
    document.getElementById("det-ram").textContent = d.ram;
    document.getElementById("det-speed").textContent = d.speed;
    document.getElementById("det-gain").textContent = d.gain;
    document.getElementById("det-tradeoff").textContent = d.tradeoff;
    document.getElementById("det-code").textContent = d.code;
    document.getElementById("det-repo").href = d.repo;
    const pypiEl = document.getElementById("det-pypi");
    pypiEl.href = d.pypi;
    pypiEl.textContent = d.pypiLabel;
  }

  document.querySelectorAll("[data-organ]").forEach((el) => {
    el.addEventListener("click", () => selectOrgan(el.getAttribute("data-organ")));
  });
  selectOrgan("compass");

  // -------------------------------------------------------------------------
  // 5. BENCHMARK GALLERY SWITCHER
  // -------------------------------------------------------------------------
  const benchImg = document.getElementById("bench-preview-img");
  document.querySelectorAll("[data-bench]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-bench]").forEach((b) => {
        b.classList.remove("active");
        b.style.background = "";
      });
      btn.classList.add("active");
      btn.style.background = "#0EA5E9";
      if (benchImg) benchImg.src = btn.getAttribute("data-bench");
    });
  });
  const firstBench = document.querySelector("[data-bench].active");
  if (firstBench) firstBench.style.background = "#0EA5E9";

  // -------------------------------------------------------------------------
  // 6. REAL-TIME 60 HZ CLOSED-LOOP REFLEX SIMULATOR
  // -------------------------------------------------------------------------
  const canvas = document.getElementById("reflex-canvas");
  const statusEl = document.getElementById("sim-status");
  if (canvas) {
    const ctx = canvas.getContext("2d");
    const N_HIST = 220;
    const histRing = new Array(N_HIST).fill(0);
    const histEkf = new Array(N_HIST).fill(0);

    let ringErr = 0.0;
    let ekfErr = 0.0;
    let ekfBias = 0.0;
    let spikeTicks = 0;
    let glareTicks = 0;
    let loomProgress = -1;
    let fouledT2 = false;
    let t2Health = 1.0;
    let estopFlash = 0;
    let tick = 0;

    function setSimStatus(msg, color) {
      if (!statusEl) return;
      statusEl.textContent = msg;
      statusEl.style.color = color || "#38bdf8";
    }

    document.querySelectorAll("[data-sim]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.getAttribute("data-sim");
        if (mode === "spike") {
          spikeTicks = 55;
          setSimStatus("⚡ +120° FERROMAGNETIC HULL SPIKE — EKF bias corrupted (~42° yank); NeckCompass Ring shunts spike (< 1.8°)!", "#f59e0b");
        } else if (mode === "glare") {
          glareTicks = 50;
          setSimStatus("☀️ 100× SUNLIGHT GLARE STEP — R1–R6 log photoreceptor adaptation cancels global flash (0.0% False Alarm)!", "#38bdf8");
        } else if (mode === "loom") {
          loomProgress = 0;
          setSimStatus("🪨 HEAD-ON OBSTACLE APPROACHING — LPLC2 4-quadrant radial gate tracking expansion...", "#22d3ee");
        } else if (mode === "foul") {
          fouledT2 = true;
          setSimStatus("🌿 PORT-AFT THRUSTER T2 FOULED (-90%) — NeckCord isolates T2 in 42 ms (0.02 N) & rebalances T1/T3/T4!", "#f43f5e");
        } else if (mode === "reset") {
          ringErr = 0;
          ekfErr = 0;
          ekfBias = 0;
          spikeTicks = 0;
          glareTicks = 0;
          loomProgress = -1;
          fouledT2 = false;
          t2Health = 1.0;
          estopFlash = 0;
          setSimStatus("NOMINAL CRUISE — RingCompass locked (0.4° err) · T4/T5 Optic Lobe scanning · 4B Cervical link @ 100 Hz", "#38bdf8");
        }
      });
    });

    function stepSim() {
      tick++;
      const noise = (Math.random() - 0.5) * 0.7;

      // 1. Heading dynamics
      if (spikeTicks > 0) {
        spikeTicks--;
        ringErr = ringErr * 0.82 + 0.32 + noise * 0.3;
        ekfErr = Math.min(46, ekfErr * 0.84 + 7.4);
        ekfBias = Math.min(14, ekfBias + 0.38);
      } else {
        ringErr = ringErr * 0.86 + noise * 0.35;
        ekfBias *= 0.985;
        ekfErr = ekfErr * 0.94 + ekfBias * 0.45 + noise * 0.4;
      }
      histRing.push(ringErr);
      histRing.shift();
      histEkf.push(ekfErr);
      histEkf.shift();

      // 2. Glare & Looming
      if (glareTicks > 0) glareTicks--;
      if (loomProgress >= 0) {
        loomProgress += 1.65;
        if (loomProgress >= 62 && loomProgress < 65) {
          estopFlash = 45;
          setSimStatus("🚨 GIANT FIBER LOOM_ESTOP DISPATCHED (403 ms pre-impact) over ChaCha20-Poly1305 NLTP!", "#10b981");
        }
        if (loomProgress > 100) loomProgress = -1;
      }
      if (estopFlash > 0) estopFlash--;

      // 3. Thruster health shunting
      if (fouledT2) {
        t2Health = Math.max(0.02, t2Health * 0.72);
      } else {
        t2Health = Math.min(1.0, t2Health + 0.08);
      }

      drawSim();
      requestAnimationFrame(stepSim);
    }

    function drawSim() {
      const W = canvas.width;
      const H = canvas.height;
      ctx.clearRect(0, 0, W, H);

      // Subtle background flash if glare active
      ctx.fillStyle = glareTicks > 0 ? `rgba(250, 204, 21, ${0.14 * (glareTicks / 50)})` : "#070c18";
      ctx.fillRect(0, 0, W, H);

      // Panel dividers
      ctx.strokeStyle = "rgba(148, 163, 184, 0.16)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(430, 18); ctx.lineTo(430, H - 18);
      ctx.moveTo(775, 18); ctx.lineTo(775, H - 18);
      ctx.stroke();

      // --- PANEL 1: Heading Error Trace (NeckCompass vs EKF) ---
      ctx.fillStyle = "#94a3b8";
      ctx.font = "600 13px Inter, sans-serif";
      ctx.fillText("1. CENTRAL COMPLEX COMPASS ERROR (deg)", 24, 32);

      const gx0 = 28, gy0 = 56, gw = 380, gh = 248;
      ctx.strokeStyle = "rgba(148, 163, 184, 0.10)";
      for (let i = 0; i <= 4; i++) {
        const y = gy0 + (gh * i) / 4;
        ctx.beginPath(); ctx.moveTo(gx0, y); ctx.lineTo(gx0 + gw, y); ctx.stroke();
      }

      // EKF trace (orange-red)
      ctx.strokeStyle = "#f97316";
      ctx.lineWidth = 2.2;
      ctx.beginPath();
      histEkf.forEach((v, i) => {
        const x = gx0 + (i / (N_HIST - 1)) * gw;
        const y = gy0 + gh - 28 - Math.min(gh - 36, (v / 50) * (gh - 42));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();

      // NeckCompass trace (cyan)
      ctx.strokeStyle = "#0ea5e9";
      ctx.lineWidth = 2.8;
      ctx.beginPath();
      histRing.forEach((v, i) => {
        const x = gx0 + (i / (N_HIST - 1)) * gw;
        const y = gy0 + gh - 28 - Math.max(-18, Math.min(gh - 36, (v / 50) * (gh - 42)));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();

      ctx.font = "600 12px JetBrains Mono, monospace";
      ctx.fillStyle = "#0ea5e9";
      ctx.fillText(`NeckCompass: ${Math.abs(ringErr).toFixed(1)}°`, gx0, H - 20);
      ctx.fillStyle = "#f97316";
      ctx.fillText(`2-State EKF: ${Math.abs(ekfErr).toFixed(1)}°`, gx0 + 195, H - 20);

      // --- PANEL 2: Optic Lobe Retinal Grid & LPLC2 Gate ---
      ctx.fillStyle = "#94a3b8";
      ctx.font = "600 13px Inter, sans-serif";
      ctx.fillText("2. OPTIC LOBE (T4/T5 + LPLC2 GATE)", 454, 32);

      const cx = 602, cy = 172, R = 94;
      ctx.strokeStyle = estopFlash > 0 ? "#10b981" : "rgba(56, 189, 248, 0.35)";
      ctx.lineWidth = estopFlash > 0 ? 3.5 : 1.8;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

      // Cruciform quadrant axes
      ctx.strokeStyle = "rgba(148, 163, 184, 0.18)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
      ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
      ctx.stroke();

      if (loomProgress >= 0) {
        const lr = 8 + (loomProgress / 100) * 78;
        ctx.fillStyle = "rgba(6, 182, 212, 0.28)";
        ctx.strokeStyle = "#22d3ee";
        ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(cx, cy, lr, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      }

      ctx.font = "600 12px JetBrains Mono, monospace";
      ctx.fillStyle = estopFlash > 0 ? "#10b981" : "#38bdf8";
      const opticLabel = estopFlash > 0
        ? "LPLC2: LOOM_ESTOP FIRED!"
        : (glareTicks > 0 ? "100x Glare Shunted (Loom=0.00)" : "Scanning 4 Quadrants (FAR=0%)");
      ctx.fillText(opticLabel, 464, H - 20);

      // --- PANEL 3: NeckCord 4B + VNC Thruster Allocation ---
      ctx.fillStyle = "#94a3b8";
      ctx.font = "600 13px Inter, sans-serif";
      ctx.fillText("3. NECKCORD THRUSTERS & NLTP WIRE", 798, 32);

      const thrusters = estopFlash > 0
        ? [0, 0, 0, 0]
        : (fouledT2
            ? [0.86, 0.02 * t2Health, 0.88, 0.78]
            : [0.62, 0.62, 0.62, 0.62]);
      const labels = ["T1 Port-Fwd", "T2 Port-Aft", "T3 Stbd-Fwd", "T4 Stbd-Aft"];

      thrusters.forEach((val, idx) => {
        const by = 68 + idx * 52;
        ctx.fillStyle = "#cbd5e1";
        ctx.font = "500 12px JetBrains Mono, monospace";
        ctx.fillText(labels[idx], 802, by + 14);

        ctx.fillStyle = "rgba(30, 41, 59, 0.9)";
        ctx.fillRect(905, by, 185, 20);

        const isShunted = idx === 1 && fouledT2;
        ctx.fillStyle = isShunted ? "#f43f5e" : "#6366f1";
        ctx.fillRect(905, by, Math.max(3, val * 185), 20);
      });

      // NLTP animated wire pulses
      ctx.fillStyle = "#10b981";
      ctx.font = "600 12px JetBrains Mono, monospace";
      const hexFrame = estopFlash > 0 ? "0x53F0009C [FREEZE]" : "0x53000093 [4B CRC4]";
      ctx.fillText(`NLTP AEAD: ${hexFrame}`, 802, H - 20);
    }

    requestAnimationFrame(stepSim);
  }
})();
