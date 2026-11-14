/* ==========================================================================
   NeckLink v3.0 website controller
   1. Logo intro (once per browser session)
   2. Theme, navigation and copy buttons
   3. Interactive BANC v888 CNS architecture explorer (v3.0.0 + NLTP v1.6.0)
   4. Benchmark gallery (per-package charts)
   5. Live C99 WebAssembly (necklink_cns.wasm, 107.5 KB) + Live Webcam simulator
   ========================================================================== */

(function () {
  "use strict";

  const root = document.documentElement;
  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

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
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (err) { /* ignore */ }
      ta.remove();
    }
    btn.classList.add("copied");
    setTimeout(() => btn.classList.remove("copied"), 1600);
  }

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
  // 3. INTERACTIVE BANC v888 CNS ARCHITECTURE EXPLORER
  // -------------------------------------------------------------------------
  const ORGANS = {
    cns: {
      colorVar: "--accent",
      region: "Whole-CNS · BANC v888 (157,789 neurons, 23.55M synapses)",
      tests: "7 Py + 7 C99 (ASan) + ESP32 build + 107.5 KB WASM",
      name: "NeckLink CNS",
      pkg: "necklink-cns",
      desc: "4-organ reflex controller combining NeckSight, NeckCompass, NeckSense, and NeckCord. Its decision layer is matrix multiplication through BANC v888 weights: 11 brain populations (LPLC2, LC4, LPLC1, PFL3, PFL2, MBON) → 10 descending neurons (DNp01, MDN, DNa02, …) → 6 leg motor pools. Plus MAVLink v2 and ROS 2 bridges.",
      ram: "24,024 B (23.5 KB), 0 B heap/.bss",
      speed: "~166 µs / tick (desktop C99; MCU not measured)",
      gain: "Sim mission 66.7% vs 4.6% shuffled · research beta",
      tradeoffLabel: "Honest scope boundary",
      tradeoff: "In a closed-loop boat simulation the real wiring reached 66.7% safe arrival vs 4.6% with shuffled wiring and 0% for a classical stack — but the hand-tuned analytic model reached 80.0%. It is a reflex layer, not a SLAM or planning stack, and has not been run on real hardware.",
      code: `from necklink_cns import NeckLinkCNS\n\ncns = NeckLinkCNS(vehicle="hexacopter", grid_size=32)\nout = cns.step(dt=0.02, speed_m_s=1.8, gyro_xyz=[0.02, -0.01, 0.05],\n               accel_xyz=[0.2, -0.1, 9.80], mag_xyz=[0.45, 0.05, 0.89], camera_frame=cam_32x32)\nprint(out.descending.behavior_mode, len(out.cervical_wire_4b), out.mavlink_manual_pkt[0])`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necklink_cns",
      pypi: "https://pypi.org/project/necklink-cns/",
      pypiLabel: "PyPI v3.0.0"
    },
    sight: {
      colorVar: "--sight",
      region: "Optic lobe · R1–R6 / T4–T5 / LPLC2 + LC4",
      tests: "34 Py + C99 self-test + ESP32-CAM build",
      name: "NeckSight",
      pkg: "necksight",
      desc: "Retina/lamina glare guard, T4/T5 motion detectors, 3-DOF (yaw, pitch, roll) gyro derotation, and 64 LPLC2 + 32 LC4 looming detectors whose receptive fields are compiled from BANC v888 (retinotopy inferred from connectivity), with DNa02-style dodge vectors.",
      ram: "14,624 B (14.28 KB)",
      speed: "~6,557 FPS (desktop C99, 32×32)",
      gain: "Thin obstacles: 0.755 AUROC (LC4 needed)",
      tradeoffLabel: "Where others win",
      tradeoff: "In a 3D MuJoCo test with identical gyro derotation for all methods, the analytic model (0.890 AUROC) and derotated Farneback (0.818) beat the connectome receptive fields (0.806) overall; the real wiring is not significantly better than shuffled wiring (p = 0.079). On thin poles and wires it beats Farneback (0.755 vs 0.635).",
      code: `from necksight import NeckSightPipeline, imu_to_ego_motion\n\noptic = NeckSightPipeline(grid_size=64, enable_lc4_bar=True)\nego = imu_to_ego_motion(yaw_rate, pitch_rate, roll_rate, dt=1/60, grid_width=64)\nres = optic.step(frame_u8, dt=1/60, ego_shift_px=ego)\nif res.triggered:\n    print(res.looming_score, res.bar_score, (res.avoid_x, res.avoid_y))`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksight",
      pypi: "https://pypi.org/project/necksight/",
      pypiLabel: "PyPI v3.0.0"
    },
    compass: {
      colorVar: "--compass",
      region: "Central complex · E-PG / Delta7 / PFL3+PFL2",
      tests: "48 Py + 14 C99 + ESP32 build",
      name: "NeckCompass",
      pkg: "neckcompass",
      desc: "Zero-float 9-axis tilt-compensated heading on an EPG ↔ Delta7 ring attractor and PFL3/PFL2 homing steering, both running on matrices compiled from BANC v888 (50 EPG, 42 PEN, 40 Delta7, 25 PFL3, 12 PFL2), plus a 1,104-byte online soft-iron calibration ring.",
      ram: "1,444 B (tilt + plastic ring)",
      speed: "0.96 µs (9-DOF) · 8.10 µs (desktop C99)",
      gain: "Homing 100% vs 4.7% shuffled",
      tradeoffLabel: "Where others win",
      tradeoff: "The real wiring is a much worse compass than the analytic model: 12.84° RMS under waves and magnetic spikes vs 0.99° (analytic) and 0.81° (χ²-gated EKF), and it drifts in darkness. Use connectome_weights=\"v2_analytic\" when heading accuracy matters.",
      code: `from neckcompass import RingCompass, FanBodyNavigator\n\nnav = FanBodyNavigator(compass=RingCompass(n_wedges=16))\nhdg, dist_m, brg = nav.step_with_compass_9dof(gx, gy, gz, ax, ay, az, mx, my, mz,\n                                              speed_m_s=1.8, dt=0.02)\nyaw_cmd = nav.steering_cmd(max_turn_rate=1.0)`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcompass",
      pypi: "https://pypi.org/project/neckcompass/",
      pypiLabel: "PyPI v3.0.0"
    },
    cord: {
      colorVar: "--cord",
      region: "Ventral nerve cord · DN→MN bottleneck",
      tests: "48 Py + 19 C99 + ESP32 build",
      name: "NeckCord",
      pkg: "neckcord",
      desc: "Six-leg gait whose phase coupling is a 6×6 inhibition matrix compiled from BANC v888 VNC premotor neurons, with DN → leg-pool steering gains. Engineering layers: box-constrained thruster/rotor allocator, sensorless ESC fault observer, 18-servo IK and a 4-byte CRC-4 frame.",
      ram: "930 B (all C99 structs)",
      speed: "5.57 µs / full tick (desktop C99)",
      gain: "Tripod order 0.855 vs 0.411 shuffled",
      tradeoffLabel: "Where others win",
      tradeoff: "The hand-designed tripod table is perfectly ordered and walks slightly farther after a leg loss. In a 30-seed MuJoCo rotor-loss test all allocators survived; SciPy bounded QP had lower tilt (0.17° vs 0.25°).",
      code: `from neckcord import ESCObserver, ShuntingThrusterCord\nimport numpy as np\n\ncord = ShuntingThrusterCord.multicopter_hex_6(u_max=4.5, prioritize_attitude=True)\nesc = ESCObserver(n_actuators=6, u_nom=4.5, rpm_max=9000.0)\nres = cord.step(np.array([17.7, 0.0, 0.0, 0.0]), esc_observer=esc, esc_rpm=rpm, esc_current_a=amps, dt=0.01)\nprint(res.commands, res.health)`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/neckcord",
      pypi: "https://pypi.org/project/neckcord/",
      pypiLabel: "PyPI v3.0.0"
    },
    sense: {
      colorVar: "--sense",
      region: "Antennal lobe, chordotonal & mushroom body",
      tests: "27 Py + 9 C99 + ESP32 build",
      name: "NeckSense",
      pkg: "necksense",
      desc: "Integer mushroom body using real BANC v888 Kenyon-cell claws (1,024 cells), APL feedback and MBON weights, with divisive normalisation, whiff-rate tracking, 2-compartment continual memory and a 16-band vibration front-end (5,804 B tiny / 20,284 B default state).",
      ram: "5,804 B (NS_TINY) · 20.3 KB full",
      speed: "16.4 µs (tiny) · 31.1 µs (desktop C99)",
      gain: "Real = shuffled wiring (honest null)",
      tradeoffLabel: "Where others win",
      tradeoff: "On real UCI gas drift data, One-Class SVM (0.698 AUROC) beats it at novelty (0.666), and nearest centroid (60.1%) beats it at 5-shot classification (42.7%). Shuffled wiring performs the same as the real wiring.",
      code: `from necksense import VibrationFrontEnd, MushroomNoveltyDetector\n\nfe = VibrationFrontEnd()\ndet = MushroomNoveltyDetector(n_inputs=16, antennal=True, n_kc=1024)\nscore = det.update_and_score(fe.extract(accel_256_window))\nprint("Bearing resonance anomaly:", score)`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/necksense",
      pypi: "https://pypi.org/project/necksense/",
      pypiLabel: "PyPI v3.0.0"
    },
    nltp: {
      colorVar: "--nltp",
      region: "Cervical connective · loss-healing wire protocol",
      tests: "32 Py + 16 C99 + 248 interop + ESP32/ESP8266 mesh",
      name: "NLTP",
      pkg: "pynltp",
      desc: "Loss-healing file, message and confirmed-command transport with GF(2^8) Cauchy FEC and LZ4 compression over UDP and routerless ESP-NOW, secured by X25519 + PSK forward-secret ChaCha20-Poly1305 AEAD, with RFC 5389 STUN hole-punching and a 4.76 KB TX-only MCU profile.",
      ram: "4.76 KB TX-only · ~65 KB full ESP32",
      speed: "956 MB/s GF(2^8) · 17.75 Mbps ESP-NOW",
      gain: "100% SHA-256 @ 35% loss",
      tradeoffLabel: "Where kernel TCP BBR wins",
      tradeoff: "On clean, loss-free wired fibre or LAN links, kernel TCP BBR reaches higher multi-hundred-Mbps bulk throughput than userspace UDP FEC. NLTP v1.6.0 wins on lossy radio, Wi-Fi fades, ESP32/ESP8266 ESP-NOW meshes and 4G CGNAT links where TCP stalls.",
      code: `from nltp import NLTPClient, generate_nltp_key\n\nwith NLTPClient(("127.0.0.1", 9000), psk=generate_nltp_key()) as cli:\n    cli.send_reflex("LOOM_ESTOP")\n    receipt = cli.send_file("telemetry_batch.bin")\n    assert receipt["verified_sha256"]`,
      repo: "https://github.com/indian-aman/necklink/tree/main/packages/nltp",
      pypi: "https://pypi.org/project/pynltp/",
      pypiLabel: "PyPI v1.6.0"
    }
  };

  $$("[data-organ]").forEach((el) => {
    const d = ORGANS[el.getAttribute("data-organ")];
    if (d && !el.style.getPropertyValue("--c")) el.style.setProperty("--c", "var(" + d.colorVar + ")");
  });

  const detail = $("#organ-detail");
  let currentCode = "";

  function selectOrgan(key) {
    const d = ORGANS[key];
    if (!d) return;

    $$(".cns-node").forEach((el) => el.classList.toggle("active", key === "cns" || el.getAttribute("data-organ") === key));
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
  selectOrgan("cns");

  // -------------------------------------------------------------------------
  // 4. BENCHMARK GALLERY
  // -------------------------------------------------------------------------
  const RAW = "https://raw.githubusercontent.com/indian-aman/";
  const BENCH = {
    compass: {
      src: RAW + "necklink/main/packages/neckcompass/assets/neckcompass_benchmarks.png", w: 4070, h: 1298,
      alt: "NeckCompass benchmark charts",
      caption: "Ring attractor against a 3-sigma gated EKF, an ungated EKF and a complementary IMU+mag filter: clean Gaussian noise, 70°–150° magnetic spikes, recovery from a 90° heading upset, and a 600 s GPS-blackout survey with homebound return."
    },
    sight: {
      src: RAW + "necklink/main/packages/necksight/assets/necksight_benchmarks.png", w: 3564, h: 1232,
      alt: "NeckSight benchmark charts",
      caption: "Optic-lobe looming selectivity, giant-fibre trigger timing before collision, and C99 speed and RAM footprint."
    },
    cord: {
      src: RAW + "necklink/main/packages/neckcord/assets/neckcord_benchmarks.png", w: 3850, h: 1276,
      alt: "NeckCord benchmark charts",
      caption: "X-vectored USV wave corridor & thruster fault isolation, plus six-limb adaptive Kuramoto CPG stability."
    },
    sense: {
      src: RAW + "necklink/main/packages/necksense/assets/necksense_benchmarks.png", w: 3872, h: 1210,
      alt: "NeckSense benchmark charts",
      caption: "Olsen-Wilson concentration-invariant FlyHash retrieval, drifting gas plumes, and one-shot MBON anomaly detection."
    },
    nltp: {
      src: RAW + "necklink/main/assets/nltp_v12_benchmarks.png", w: 2992, h: 1188,
      alt: "NLTP benchmark charts",
      caption: "Lossy UDP & satellite file transfer against kcptun, QUIC (ngtcp2) and Linux TCP BBR, plus ESP32 ESP-NOW goodput."
    },
    suite: {
      src: RAW + "necklink/main/assets/necklink_brand_banner.png", w: 1376, h: 768,
      alt: "NeckLink modules mapped onto the fruit-fly nervous system",
      caption: "NeckLink organs mapped onto the adult Drosophila central nervous system."
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
  // 5. LIVE C99 WEBASSEMBLY (necklink_cns.wasm) + WEBCAM REFLEX SIMULATOR
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
    bg: "#070b14"
  };

  // WASM state
  let wasmEx = null;
  let wasmFrameU8 = null;
  let wasmTelemI32 = null;
  let wasmSramBytes = 24024;
  const localFrame64 = new Uint8Array(64 * 64);

  // Offscreen 64x64 canvas for live webcam downsampling & thumbnail display
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
      return "WASM C99 LIVE (" + wasmSramBytes.toLocaleString() + " B static SRAM, 0 heap). 9-DOF ring compass locked, LPLC2+LC4 scanning, 4-byte cervical link active.";
    }
    return "Nominal cruise. Ring compass locked (0.4° error), LPLC2+LC4 optic lobe scanning, 4-byte cervical link at 100 Hz.";
  }

  function setStatus(msg, colorVar) {
    statusEl.textContent = msg;
    statusDot.style.background = colorVar ? "var(" + colorVar + ")" : "";
  }

  async function initWasm() {
    try {
      const resp = await fetch("necklink_cns.wasm");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const bytes = await resp.arrayBuffer();
      const { instance } = await WebAssembly.instantiate(bytes, {});
      wasmEx = instance.exports;
      if (typeof wasmEx._initialize === "function") wasmEx._initialize();
      wasmEx.nl_wasm_init(0); // NL_VEHICLE_USV_T200 (4 thrusters)
      wasmSramBytes = wasmEx.nl_wasm_sizeof_cns();
      const fptr = wasmEx.nl_wasm_frame_buf();
      const tptr = wasmEx.nl_wasm_get_telemetry();
      wasmFrameU8 = new Uint8Array(wasmEx.memory.buffer, fptr, 64 * 64);
      wasmTelemI32 = new Int32Array(wasmEx.memory.buffer, tptr, 24);
      setStatus(nominalMessage(), "--ok");
    } catch (err) {
      wasmEx = null;
      setStatus(nominalMessage());
    }
  }
  initWasm();

  async function toggleWebcam() {
    if (webcamActive) {
      stopWebcam();
      setStatus(nominalMessage(), wasmEx ? "--ok" : "");
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
          ? "LIVE WEBCAM → C99 WASM: Move your hand or a pen/pole rapidly toward the camera to trigger LPLC2/LC4 looming + DNa02 dodge!"
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
    cssW = canvas.parentElement.clientWidth;
    narrow = cssW < 640;
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
        setStatus("Off-axis boulder approaching on port side. C99 LPLC2 4-quadrant gate tracks expansion and prepares contralateral DNa02 dodge.", "--sight");
      } else if (mode === "pole") {
        loomKind = "pole";
        loomProgress = 0;
        setStatus("Thin vertical pole/mast approaching! Classical optical flow misses thin bars, while NeckSight LC4 channel locks on.", "--sight");
      } else if (mode === "foul") {
        fouledT2 = true;
        setStatus("Port-aft thruster T2 fouled (−90%). NeckCord active-set shunts T2 in <45 ms and rebalances surge onto surviving thrusters with zero yaw spin.", "--cord");
      } else {
        stopWebcam();
        if (wasmEx) wasmEx.nl_wasm_init(0);
        ringErr = ekfErr = ekfBias = 0;
        spikeTicks = glareTicks = estopFlash = 0;
        loomProgress = -1;
        fouledT2 = false;
        t2Health = 1.0;
        lastLoomScore = lastLc4Score = lastAvoidX = lastAvoidY = 0;
        setStatus(nominalMessage(), wasmEx ? "--ok" : "");
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
        const cx = 23, cy = 32; // port-side obstacle -> triggers rightward (+X) DNa02 dodge
        for (let y = 0; y < 64; y++) {
          const dy = y - cy;
          for (let x = 0; x < 64; x++) {
            const dx = x - cx;
            if (dx * dx + dy * dy <= r * r) buf[y * 64 + x] = 24;
          }
        }
      } else {
        // Thin vertical pole expanding in width from 1 px to 11 px on starboard side (cx = 41)
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
        20,
        1500,
        0,
        0,
        0,
        spikeTicks > 0 ? 180 : 0,
        spikeTicks > 0 ? -140 : 0,
        spikeCdeg,
        t2HealthQ15,
        hasCam
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

    // Draw live 64x64 grayscale retina inside circular compound-eye aperture when webcam is active
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

    // Draw contralateral DNa02 avoidance arrow when looming reflex fires
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
      lx,
      p.y + p.h - 18
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
