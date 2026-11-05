/* ==========================================================================
   NeckLink website controller
   1. Logo intro (once per browser session)
   2. Theme, navigation and copy buttons
   3. Interactive CNS architecture explorer
   4. Benchmark gallery
   5. Closed-loop reflex simulator (responsive, DPR-aware canvas)
   ========================================================================== */

(function () {
  "use strict";

  const root = document.documentElement;
  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
  const cssVar = (name) => getComputedStyle(root).getPropertyValue(name).trim();

  // -------------------------------------------------------------------------
  // 1. LOGO INTRO -> FADE INTO WEBSITE
  // -------------------------------------------------------------------------
  const splash = $("#intro-splash");
  let introTimer = null;

  function finishIntro() {
    if (introTimer) {
      clearTimeout(introTimer);
      introTimer = null;
    }
    splash.classList.add("fade-out");
    root.classList.add("intro-done");
    try { sessionStorage.setItem("necklink-intro-seen", "1"); } catch (e) { /* storage blocked */ }
  }

  function playIntro() {
    if (introTimer) clearTimeout(introTimer);
    root.classList.remove("no-intro", "intro-done");
    splash.classList.remove("fade-out", "animate");
    // Force a reflow so the stroke-dashoffset animations restart cleanly
    void splash.offsetWidth;
    splash.classList.add("animate");
    introTimer = setTimeout(finishIntro, 2350);
  }

  if (!root.classList.contains("no-intro")) requestAnimationFrame(playIntro);
  splash.addEventListener("click", finishIntro);
  $("#replay-intro-btn").addEventListener("click", () => {
    window.scrollTo({ top: 0 });
    playIntro();
  });

  // -------------------------------------------------------------------------
  // 2. THEME, NAVIGATION, COPY
  // -------------------------------------------------------------------------
  const themeBtn = $("#theme-toggle-btn");
  function syncThemeButton() {
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    themeBtn.setAttribute("aria-label", "Switch to " + next + " theme");
  }
  themeBtn.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("necklink-theme", next); } catch (e) { /* storage blocked */ }
    syncThemeButton();
  });
  syncThemeButton();

  const nav = $(".nav");
  const menuBtn = $("#menu-btn");
  function setMenu(open) {
    nav.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    menuBtn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }
  menuBtn.addEventListener("click", () => setMenu(!nav.classList.contains("open")));
  $$("#nav-links a").forEach((a) => a.addEventListener("click", () => setMenu(false)));

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!splash.classList.contains("fade-out") && !root.classList.contains("no-intro")) finishIntro();
    setMenu(false);
  });

  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Highlight the nav link of the section in view: the nearest target whose top has passed 40% of the viewport
  const spyLinks = $$("#nav-links a")
    .map((a) => ({ a: a, el: document.getElementById(a.getAttribute("href").slice(1)) }))
    .filter((x) => x.el);
  let spyQueued = false;
  function updateSpy() {
    spyQueued = false;
    const line = window.innerHeight * 0.4;
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
  window.addEventListener("scroll", () => {
    if (!spyQueued) { spyQueued = true; requestAnimationFrame(updateSpy); }
  }, { passive: true });
  updateSpy();

  async function copyText(btn, text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // Fallback for browsers that block the async clipboard API
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (err) { /* nothing else to try */ }
      ta.remove();
    }
    btn.classList.add("copied");
    setTimeout(() => btn.classList.remove("copied"), 1600);
  }

  // Install terminal
  const installCode = $("#install-code");
  let currentCmd = installCode.textContent;
  $$(".terminal-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".terminal-tab").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", String(t === tab));
      });
      currentCmd = tab.getAttribute("data-cmd");
      installCode.textContent = currentCmd;
    });
  });
  $("#copy-cmd-btn").addEventListener("click", (e) => copyText(e.currentTarget, currentCmd));
  $$("[data-copy]").forEach((btn) => btn.addEventListener("click", () => copyText(btn, btn.getAttribute("data-copy"))));

  // -------------------------------------------------------------------------
  // 3. INTERACTIVE CNS ARCHITECTURE EXPLORER
  // -------------------------------------------------------------------------
  const ORGANS = {
    compass: {
      colorVar: "--compass",
      region: "Central complex · E-PG / CPU4 / PFL3",
      tests: "30 Py + 7 C99 tests passed",
      name: "NeckCompass",
      pkg: "neckcompass",
      desc: "Topological 16-wedge ring-attractor compass and fan-shaped body path integrator. P-EN velocity shifters integrate gyro yaw rate with no ±180° seam; a noise-sized attractor basin shunts 70°–150° magnetic spikes, and a consistent off-basin cue re-anchors the compass after a real heading upset.",
      ram: "324 B (Q15)",
      speed: "~1 µs / step (desktop)",
      gain: "0.44° RMSE under spikes",
      tradeoffLabel: "Where the baseline ties or wins",
      tradeoff: "A 3-sigma innovation-gated EKF is as accurate on spiky magnetometer data (0.41° vs 0.44° RMSE). NeckCompass wins after a 90° heading upset, where the gated EKF stays stuck (0.43° vs 31.9° RMSE), and its compass + homing core fits in 324 B of integer-only C.",
      code: `from neckcompass import RingCompass, FanBodyNavigator\n\ncompass = RingCompass(n_wedges=16)\nnav = FanBodyNavigator(n_columns=16, compass=compass)\n\nhdg, home_dist_m, home_brg = nav.step_with_compass(\n    omega_rad_s=0.12, speed_m_s=1.8, dt=0.02, mag_heading_rad=2.1\n)\nyaw_cmd = nav.steering_cmd(max_turn_rate=1.0)`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcompass",
      pypi: "https://pypi.org/project/neckcompass/",
      pypiLabel: "PyPI v1.1.0"
    },
    sight: {
      colorVar: "--sight",
      region: "Optic lobe · R1–R6 / T4–T5 / LPLC2",
      tests: "24 Py + 7 C99 checks passed",
      name: "NeckSight",
      pkg: "necksight",
      desc: "Logarithmic R1–R6 photoreceptor glare cancellation, an ON/OFF lamina split, 3-arm Reichardt / Barlow-Levick motion detectors, and a four-quadrant LPLC2 radial looming gate that dispatches giant-fibre E-stop reflexes about 403 ms before collision.",
      ram: "14.27 KB (32×32)",
      speed: "~12,000 FPS (C99, desktop)",
      gain: "0.9988 AUROC (synthetic)",
      tradeoffLabel: "Where the baseline wins",
      tradeoff: "On slow 0.05–0.35 px/frame sub-pixel visual odometry, Lucas-Kanade achieves 2.4× lower velocity MAE (0.055 vs 0.133 px/frame). NeckSight wins on looming detection under glare and fast pans (0% vs 42% false alarms for compensated pyramidal Lucas-Kanade). Feed it a gyro on fast-turning platforms.",
      code: `from necksight import NeckSightPipeline\n\noptic = NeckSightPipeline(grid_size=64, threshold=0.045)\ntrigger = optic.step(camera_frame_u8, dt=1/60)\nif trigger.triggered:\n    print(f"LOOM E-STOP! score={trigger.looming_score:.3f} focus={trigger.focus_xy}")`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksight",
      pypi: "https://pypi.org/project/necksight/",
      pypiLabel: "PyPI v1.1.0"
    },
    cord: {
      colorVar: "--cord",
      region: "Ventral nerve cord · BANC / MANC",
      tests: "31 Py + 11 C99 tests passed",
      name: "NeckCord",
      pkg: "neckcord",
      desc: "Compresses descending brain motor intent into a 4-byte frame (400 B/s at 100 Hz) that drives a self-healing six-limb Kuramoto CPG and a fault-tolerant thruster allocator, which isolates fouled propulsors in about 42 ms and can probe them back into service.",
      ram: "322 B (Q15/Q14)",
      speed: "42 ms fault isolation",
      gain: "100% five-leg stability",
      tradeoffLabel: "Where they tie",
      tradeoff: "A pseudo-inverse with a simple threshold fault detector on the same thrust feedback performs the same (40 ms vs 42 ms isolation, 4.84° vs 4.77° peak yaw). NeckCord's value is a tested package: graded health, recovery probing and a zero-float C99 port.",
      code: `from neckcord import DescendingFrame, ShuntingThrusterCord, encode_descending_4b\n\nwire_4b = encode_descending_4b(DescendingFrame(drive_fwd=0.7, turn_yaw=-0.2))\nres = allocator.step(wire_4b, feedback_thrust=esc_thrust_n, dt=0.02)\nprint("Thruster commands (N):", res.commands, "Health:", res.health)`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcord",
      pypi: "https://pypi.org/project/neckcord/",
      pypiLabel: "PyPI v1.1.0"
    },
    sense: {
      colorVar: "--sense",
      region: "Antennal lobe & mushroom body · KC / DAN / MBON",
      tests: "17 Py + 5 C99 tests passed",
      name: "NeckSense",
      pkg: "necksense",
      desc: "Dual-timescale ORN thermal-drift removal and Olsen-Wilson divisive normalisation feed a multiplication-free 5% winner-take-all Kenyon FlyHash, a popcount index, and one-shot anti-Hebbian MBON novelty and associative valence memory.",
      ram: "8.8 KB (M = 1024)",
      speed: "~25–30 µs / sample (C99, desktop)",
      gain: "0.994 novelty AUROC (4 KB)",
      tradeoffLabel: "Where the baseline wins",
      tradeoff: "At equal tag size SimHash beats FlyHash on recall (50.5% vs 27.5%), and a baseline-corrected k-NN matches NeckSense on drifting gas plumes (100% vs 99.5%). NeckSense wins on one-shot novelty at equal 4 KB memory (0.994 vs 0.940 AUROC) with no weights and no float multiplications.",
      code: `from necksense import MushroomNoveltyDetector\n\ndet = MushroomNoveltyDetector(n_inputs=8, stateful_antennal=True, n_kc=1024)\nnovelty = det.update_and_score(enose_8ch_reading)\nif novelty > 0.75:\n    print(f"1-shot chemical plume anomaly detected: {novelty:.3f}")`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksense",
      pypi: "https://pypi.org/project/necksense/",
      pypiLabel: "PyPI v1.1.0"
    },
    nltp: {
      colorVar: "--nltp",
      region: "Cervical connective · loss-healing wire protocol",
      tests: "32 Py + 16 C99 + 216 interop + ESP32/ESP8266 mesh",
      name: "NLTP",
      pkg: "pynltp",
      desc: "Loss-healing file, message and confirmed-command transport with GF(2^8) Cauchy FEC over UDP and routerless ESP-NOW, secured by ChaCha20-Poly1305 per-session AEAD (1-RTT handshake), with RFC 5389 STUN hole-punching and a zero-knowledge single-port 4G CGNAT relay.",
      ram: "~65 KB on ESP32",
      speed: "~7 Mbps typical ESP-NOW v2 (best 17.75)",
      gain: "100% SHA-256 @ 35% loss",
      tradeoffLabel: "Where the baseline wins",
      tradeoff: "On clean, loss-free wired fibre or LAN links, kernel TCP BBR reaches higher multi-hundred-Mbps bulk throughput than userspace UDP FEC. NLTP wins on lossy radio, Wi-Fi fades, ESP32/ESP8266 ESP-NOW meshes and 4G CGNAT links where TCP stalls.",
      code: `from nltp import NLTPClient, generate_nltp_key\n\nwith NLTPClient(("127.0.0.1", 9000), psk=generate_nltp_key()) as cli:\n    cli.send_reflex("LOOM_ESTOP")\n    receipt = cli.send_file("telemetry_batch.bin")\n    assert receipt["verified_sha256"]`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/nltp",
      pypi: "https://pypi.org/project/pynltp/",
      pypiLabel: "PyPI v1.4.0"
    }
  };

  // Each explorer tab and map node gets its module colour
  $$("[data-organ]").forEach((el) => {
    const d = ORGANS[el.getAttribute("data-organ")];
    if (d && !el.style.getPropertyValue("--c")) el.style.setProperty("--c", "var(" + d.colorVar + ")");
  });

  const detail = $("#organ-detail");
  let currentCode = "";

  function selectOrgan(key) {
    const d = ORGANS[key];
    if (!d) return;

    $$(".cns-node").forEach((el) => el.classList.toggle("active", el.getAttribute("data-organ") === key));
    $$(".seg-btn[data-organ]").forEach((btn) => {
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

  $$(".cns-node, .seg-btn[data-organ]").forEach((el) => {
    el.addEventListener("click", () => selectOrgan(el.getAttribute("data-organ")));
  });
  $$("[data-show-organ]").forEach((a) => {
    a.addEventListener("click", () => selectOrgan(a.getAttribute("data-show-organ")));
  });
  $("#copy-code-btn").addEventListener("click", (e) => copyText(e.currentTarget, currentCode));
  selectOrgan("compass");

  // -------------------------------------------------------------------------
  // 4. BENCHMARK GALLERY
  // -------------------------------------------------------------------------
  const RAW = "https://raw.githubusercontent.com/indian-aman/";
  const BENCH = {
    compass: {
      src: RAW + "necklink/main/packages/neckcompass/assets/neckcompass_benchmarks.png", w: 4070, h: 1298,
      alt: "NeckCompass benchmark charts",
      caption: "Ring attractor against a 3-sigma gated EKF, an ungated EKF and a complementary IMU+mag filter: clean Gaussian noise, 70°–150° magnetic spikes, recovery from a 90° heading upset, and a 600 s GPS-blackout survey with homebound return (5 seeds at 50 Hz).",
    },
    sight: {
      src: RAW + "necklink/main/packages/necksight/assets/necksight_benchmarks.png", w: 3564, h: 1232,
      alt: "NeckSight benchmark charts",
      caption: "Looming reflex on 60 synthetic cluttered 64×64 sequences (24 looming obstacles against 36 fast yaw pans, 100× glare steps and cross-traffic): selectivity, giant-fibre trigger timing, and C99 speed and RAM."
    },
    cord: {
      src: RAW + "necklink/main/packages/neckcord/assets/neckcord_benchmarks.png", w: 3850, h: 1276,
      alt: "NeckCord benchmark charts",
      caption: "A 3-DOF X-vectored four-thruster USV over 10 irregular wave seeds (vs pseudo-inverse with and without fault detection), plus a six-limb hexapod: waypoint corridor, spin-out and fault isolation, and single-leg jam stability."
    },
    sense: {
      src: RAW + "necklink/main/packages/necksense/assets/necksense_benchmarks.png", w: 3872, h: 1210,
      alt: "NeckSense benchmark charts",
      caption: "Five-seed means: retrieval against dense Gaussian SimHash, an 8-sensor e-nose under 250× plume swings and drift (vs baseline-corrected k-NN), and a 20-mode one-shot anomaly stream (vs normalised nearest-neighbour memories)."
    },
    nltp: {
      src: RAW + "necklink/main/assets/nltp_v12_benchmarks.png", w: 2992, h: 1188,
      alt: "NLTP benchmark charts",
      caption: "1 MB file transfer across lossy and satellite links against kcptun, QUIC (ngtcp2) and Linux TCP BBR, and ESP32 goodput against ESP-NOW (about 60 KB verified payload)."
    },
    suite: {
      src: RAW + "necklink/main/assets/necklink_brand_banner.png", w: 1376, h: 768,
      alt: "The five NeckLink modules mapped onto the fruit-fly nervous system",
      caption: "The five modules mapped onto the fruit-fly central nervous system."
    }
  };

  const benchImg = $("#bench-img");
  const benchFig = $(".bench-figure");
  function selectBench(key) {
    const b = BENCH[key];
    if (!b) return;
    $$("[data-bench]").forEach((btn) => {
      const on = btn.getAttribute("data-bench") === key;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", String(on));
    });
    benchFig.setAttribute("data-kind", key);
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

  // -------------------------------------------------------------------------
  // 5. CLOSED-LOOP REFLEX SIMULATOR
  // -------------------------------------------------------------------------
  const canvas = $("#reflex-canvas");
  const statusEl = $("#sim-status");
  const statusDot = $("#sim-dot");
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
    ok: "#34d399",
    bar: "#818cf8",
    barBg: "rgba(30, 41, 59, 0.9)",
    fault: "#fb7185",
    bg: "#070b14"
  };
  const NOMINAL = "Nominal cruise. Ring compass locked (0.4° error), optic lobe scanning, 4-byte cervical link at 100 Hz.";

  let cssW = 0, cssH = 0, narrow = false;
  let ringErr = 0, ekfErr = 0, ekfBias = 0;
  let spikeTicks = 0, glareTicks = 0, loomProgress = -1, estopFlash = 0;
  let fouledT2 = false, t2Health = 1.0;
  let running = false;

  function setStatus(msg, colorVar) {
    statusEl.textContent = msg;
    statusDot.style.background = colorVar ? "var(" + colorVar + ")" : "";
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cssW = canvas.parentElement.clientWidth;
    narrow = cssW < 640;
    cssH = narrow ? 600 : 340;
    canvas.style.height = cssH + "px";
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Three instrument panels: side by side on wide screens, stacked on phones
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
      if (mode === "spike") {
        spikeTicks = 55;
        setStatus("+120° hull magnetic spike. An EKF without outlier gating is dragged off (about 42° error); the NeckCompass ring ignores the spike (under 1.8°), as does a properly gated EKF.", "--compass");
      } else if (mode === "glare") {
        glareTicks = 50;
        setStatus("100× sunlight glare step. R1–R6 log photoreceptor adaptation cancels the global flash (0.0% false alarms).", "--sight");
      } else if (mode === "loom") {
        loomProgress = 0;
        setStatus("Head-on obstacle approaching. The LPLC2 four-quadrant radial gate is tracking the expansion.", "--sight");
      } else if (mode === "foul") {
        fouledT2 = true;
        setStatus("Port-aft thruster fouled (−90%). NeckCord isolates it in about 42 ms (0.02 N) and rebalances surge onto the surviving thrusters with no yaw.", "--cord");
      } else {
        ringErr = ekfErr = ekfBias = 0;
        spikeTicks = glareTicks = estopFlash = 0;
        loomProgress = -1;
        fouledT2 = false;
        t2Health = 1.0;
        setStatus(NOMINAL);
      }
    });
  });

  function step() {
    const noise = (Math.random() - 0.5) * 0.7;

    // Heading dynamics
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
    histRing.push(ringErr); histRing.shift();
    histEkf.push(ekfErr); histEkf.shift();

    // Glare and looming
    if (glareTicks > 0) glareTicks--;
    if (loomProgress >= 0) {
      loomProgress += 1.65;
      if (loomProgress >= 62 && loomProgress < 65) {
        estopFlash = 45;
        setStatus("Giant-fibre LOOM_ESTOP dispatched 403 ms before impact over ChaCha20-Poly1305 NLTP.", "--ok");
      }
      if (loomProgress > 100) loomProgress = -1;
    }
    if (estopFlash > 0) estopFlash--;

    // Thruster health shunting
    t2Health = fouledT2 ? Math.max(0.02, t2Health * 0.72) : Math.min(1.0, t2Health + 0.08);
  }

  function title(p, text) {
    ctx.fillStyle = C.label;
    ctx.font = "600 11px Inter, sans-serif";
    ctx.fillText(text, p.x + 20, p.y + 26);
  }

  function drawCompass(p) {
    title(p, "CENTRAL COMPLEX · HEADING ERROR (°)");
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
    ctx.fillText("EKF (ungated) " + Math.abs(ekfErr).toFixed(1) + "°", gx + Math.min(190, gw * 0.52), p.y + p.h - 18);
  }

  function drawOptic(p) {
    title(p, "OPTIC LOBE · T4/T5 + LPLC2 GATE");
    const cx = p.x + p.w / 2, cy = p.y + 38 + (p.h - 76) / 2;
    const R = Math.max(30, Math.min(p.w * 0.36, (p.h - 84) / 2));
    if (glareTicks > 0) {
      ctx.fillStyle = "rgba(250, 204, 21, " + (0.16 * (glareTicks / 50)).toFixed(3) + ")";
      ctx.fillRect(p.x + 1, p.y + 1, p.w - 2, p.h - 2);
    }
    ctx.strokeStyle = estopFlash > 0 ? C.ok : "rgba(56, 189, 248, 0.38)";
    ctx.lineWidth = estopFlash > 0 ? 3 : 1.5;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    if (loomProgress >= 0) {
      const lr = R * (0.08 + (loomProgress / 100) * 0.82);
      ctx.fillStyle = "rgba(34, 211, 238, 0.22)";
      ctx.strokeStyle = C.optic;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(cx, cy, lr, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillStyle = estopFlash > 0 ? C.ok : C.ring;
    const label = estopFlash > 0 ? "LPLC2: LOOM_ESTOP fired"
      : (glareTicks > 0 ? "100× glare shunted (loom 0.00)" : "Scanning 4 quadrants (FAR 0%)");
    ctx.fillText(label, p.x + 20, p.y + p.h - 18);
  }

  function drawCord(p) {
    title(p, "NECKCORD · THRUSTERS + NLTP WIRE");
    const thrust = estopFlash > 0 ? [0, 0, 0, 0]
      : (fouledT2 ? [0.86, 0.02 * t2Health, 0.88, 0.78] : [0.62, 0.62, 0.62, 0.62]);
    const labels = ["T1 port-fwd", "T2 port-aft", "T3 stbd-fwd", "T4 stbd-aft"];
    const top = p.y + 46, avail = p.h - 92, rowH = avail / 4;
    const lx = p.x + 20, bx = p.x + 120, bw = Math.max(40, p.w - 140);
    const barH = Math.min(16, rowH * 0.5);
    thrust.forEach((v, i) => {
      const y = top + i * rowH + (rowH - barH) / 2;
      ctx.fillStyle = C.text;
      ctx.font = "500 12px 'JetBrains Mono', monospace";
      ctx.fillText(labels[i], lx, y + barH - 3);
      ctx.fillStyle = C.barBg;
      ctx.fillRect(bx, y, bw, barH);
      ctx.fillStyle = (i === 1 && fouledT2) ? C.fault : C.bar;
      ctx.fillRect(bx, y, Math.max(2, v * bw), barH);
    });
    ctx.fillStyle = C.ok;
    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.fillText("NLTP AEAD " + (estopFlash > 0 ? "0x53F0009C freeze" : "0x53000093 4B CRC-4"), lx, p.y + p.h - 18);
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

  // Only animate while the simulator is on screen
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) start(); else running = false; });
    }).observe(canvas);
  } else {
    start();
  }
})();
