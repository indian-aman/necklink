# NeckLink / NLTP — Fair Benchmark Report (2026-11-03)

Goal: replace the earlier self-built comparisons with fair ones, and decide whether NeckLink/NLTP is worth building on.
Stop rule set in advance: *if simple standard methods come within ~20% of NeckLink, NeckLink isn't new.*

**Verdict:**
- **The telemetry codec failed the stop rule.** On real AQMS data, simple standard methods beat it on both bytes and error.
- **The NLTP file transfer passes in one narrow case:** small files and bursty-loss links. Even there, its two settings trade off — the default wastes bytes, and the rate-capped one sometimes stalls.

## How the tests were run

| | Telemetry codec | File transfer |
|---|---|---|
| Script | `scripts/fair_telemetry_benchmark.py` | `scripts/fair_transport_benchmark.py` |
| Where | Colab G4 (48 vCPU), 825 runs | WSL2, Linux 6.18 kernel, network namespaces + `netem` |
| Opponents | Absolute send-on-delta + keyframes, acked-baseline delta, 16-reading batch + zstd; same quantizer for all | Real kernel TCP CUBIC and BBR, QUIC (aioquic), NLTP default and rate-capped |
| Data / links | 6 real Campi H2S stations, real Tridel test unit, 30-day AQMS fixture, original synthetic workloads | 6 links; loss applied in both directions; bandwidth caps; bytes counted with iptables (includes dropped packets) |
| Results | `fair_telemetry_benchmark_full.json` | `fair_transport_benchmark.json`, `.log` |

Colab G4 can't run the transport test: it blocks creating virtual network links and `netem` (no NET_ADMIN), and it has no BBR (`experiments/colab_probe_net.py`).

## Part A — Telemetry codec

**Error (NRMSE, lower is better) and compression (higher is better).** Deadband 32 (NeckLink's default), mean of 5 seeds.

**Real data (6 Campi stations + Tridel test unit):**

| Codec | 0% loss | 10% | 30% | 50% | 30% bursty | Compression, data only | Compression incl. 28 B UDP/IP header |
|---|---|---|---|---|---|---|---|
| NeckLink | 6.7 | 23.1 | 38.7 | 51.0 | 47.1 | 2.0× | 1.3× |
| AbsSoD-KF32 | **4.6** | 20.0 | 35.7 | 49.0 | 42.3 | 2.9× | 1.5× |
| AckedDelta (needs return link) | 4.7 | **16.7** | **29.0** | **39.7** | **37.8** | 3.1× | 1.5× |
| Batch16-zstd | 4.6 | 26.1 | 46.6 | 62.8 | 56.8 | **3.7×** | **6.4×** |
| SendOnDelta (old strawman) | 7.9 | 661 | 1173 | 1204 | 832 | 2.9× | 1.5× |

**AQMS raw-count fixture (30 days, synthetic):** NeckLink has 35.5% error at **0% loss**, versus 4.8% for AbsSoD-KF32. Its compression is 2.2×, versus 4.7× for AbsSoD-KF32 and 6.9× for Batch16-zstd.

**Original synthetic workloads:**
- NeckLink gets the best data-only compression (15.2× vs 9.8×).
- Under loss, its error is 1.5–2× higher than AbsSoD-KF32 or AckedDelta.

**Dominance count** (a no-return-link baseline with fewer-or-equal bytes *and* lower-or-equal error, across all deadbands):

| Data | NeckLink dominated |
|---|---|
| Real | 102 of 105 configurations |
| Fixture | 15 of 15 |
| Synthetic | 27 of 45 |

**Root cause:** NeckLink's predictor lags behind noisy or drifting real channels, even with no packet loss. The earlier "35.7× AQMS" figure exists only on synthetic data. Real AQMS gives about 2×.

**Practical note:** once per-packet headers are counted, sending one packet per reading loses to batching on every dataset — Batch16-zstd uses 1.4–5.7× fewer total bytes than NeckLink.

## Part B — File transfer, real kernel TCP

**Format:** median time over 3 runs (total wire bytes ÷ file size). A fraction such as "2/3" means some runs failed the 90 s timeout. The capped NLTP setting caps pacing at 90% of the link rate.

### 64 KB

| Link | TCP CUBIC | TCP BBR | QUIC (aioquic) | NLTP default | NLTP rate-capped |
|---|---|---|---|---|---|
| clean 20 Mbit / 20 ms | 0.09 s (1.06×) | 0.09 s (1.06×) | 0.27 s (1.14×) | 0.13 s (1.19×) | 0.06 s (1.15×) |
| 20 Mbit / 40 ms / 2% | 0.17 s (1.06×) | 0.18 s (1.06×) | 0.41 s (1.15×) | 0.24 s (1.18×) | 0.12 s (1.19×) |
| 10 Mbit / 100 ms / 5% | 0.52 s (1.12×) | 0.51 s (1.10×) | 1.27 s (1.24×) | 0.26 s (1.16×) | 0.27 s (1.26×) |
| 5 Mbit / 200 ms / 10% | 1.05 s (1.15×) | 1.02 s (1.13×) | 4.13 s (1.41×) | 0.51 s (1.40×) | 0.50 s (1.30×) |
| 2 Mbit / 300 ms / 20% | 3.43 s (1.41×) | 3.27 s (1.36×) | 8.65 s (1.58×) | 0.82 s (1.58×) | 0.95 s (1.45×) |
| 10 Mbit / 100 ms / 10% bursty | 0.57 s (1.24×) 2/3 | 1.55 s (1.12×) | 0.86 s (1.19×) | 0.45 s (1.67×) | 0.27 s (1.28×) |

### 1 MB

| Link | TCP CUBIC | TCP BBR | QUIC (aioquic) | NLTP default | NLTP rate-capped |
|---|---|---|---|---|---|
| clean 20 Mbit / 20 ms | 0.50 s (1.04×) | 0.51 s (1.04×) | 0.96 s (1.15×) | 0.67 s (**2.53×**) | 0.52 s (1.08×) |
| 20 Mbit / 40 ms / 2% | 1.80 s (1.09×) | 0.65 s (1.07×) | 4.93 s (1.12×) | 0.67 s (1.83×) | 0.68 s (1.14×) |
| 10 Mbit / 100 ms / 5% | 13.50 s (1.10×) | 1.49 s (1.12×) | 18.73 s (1.17×) | 1.97 s (1.85×) | 1.52 s (1.23×) |
| 5 Mbit / 200 ms / 10% | 42.50 s (1.17×) | 3.45 s (1.15×) | 55.83 s (1.25×) | 4.16 s (1.94×) 2/3 | 3.02 s (1.32×) |
| 2 Mbit / 300 ms / 20% | FAIL 0/3 | 8.31 s (1.32×) | FAIL 0/3 | 8.32 s (2.75×) | FAIL 0/3 |
| 10 Mbit / 100 ms / 10% bursty | 11.02 s (1.16×) | 13.10 s (1.10×) 2/3 | 42.08 s (1.22×) 1/3 | 2.14 s (2.42×) | 1.92 s (1.35×) 2/3 |

### Fairness test

Setup: 10 Mbit link, 40 ms RTT, no loss. One TCP CUBIC flow runs for 20 s; a 4 MB NLTP upload runs during part of it.

| TCP flow shares the link with | TCP average over 20 s | Other flow |
|---|---|---|
| nothing | 9.5 Mbps | — |
| a second TCP CUBIC flow | 6.2 Mbps | 3.3 Mbps |
| NLTP default | 6.2 Mbps | 5.2 Mbps over its 6.4 s |
| NLTP rate-capped | 6.8 Mbps | 6.3 Mbps over its 5.4 s |

NLTP takes roughly half the link while it runs, so it doesn't starve TCP on a clean link. This is one rough run with unequal flow durations; repeat it with loss before relying on it.

## What holds up and what doesn't

| Claim from the Antigravity session | Fair result |
|---|---|
| NLTP "7–16× faster than TCP" | Against modern TCP (BBR): **about 2× for 64 KB at 5–10% loss, about 4× at 20%**, and **5–6× for 1 MB under bursty loss**. For 1 MB at 2–20% random loss: a tie with BBR, or slower. |
| NLTP "0% extra byte cost" | Default NLTP sends **1.8–2.75× the file** on 1 MB transfers, because its fixed 50 Mbps pacing overflows slower links. |
| TCP "collapses" on lossy links | True for CUBIC (and the old benchmark's TCP model). **BBR was 9–12× faster than CUBIC** at 5–10% loss and succeeded where CUBIC failed at 20%. |
| Codec "10.9× buoy / 35.7× AQMS" | Synthetic data only. Real AQMS: about 2×, beaten by simple methods. |

## Recommended fixes, by priority

1. **Separate repair budget in NLTP.** Repair and resent packets must not compete with new data under the rate cap. The capped setting stalled in 3 of 3 runs at 20% loss and 1 of 3 on the bursty link. Fixing this should give the capped setting's ~1.1–1.35× bytes together with the default's reliability.
2. **Measure the link rate instead of hard-coding 50 Mbps pacing.** Use a BBR-style delivery-rate estimate. This removes the 1.8–2.75× byte waste.
3. **Telemetry: drop the NeckLink predictor for real AQMS.** Use AbsSoD-KF32, or batching when latency allows. Both are simpler, smaller and more accurate on your data.
4. **Retest after 1–2.** NLTP's strongest case is short transfers over bursty links (commands, small files, config pushes). Measure on real radio links next.

## Update — NLTP after the stall and pacing fixes (same links, same harness)

### What was changed (`necklink/nltp.py`, `necklink/internet.py`)

1. **Root cause of the capped-mode stall:** proactive repair shards restarted from zero whenever the pacer ran dry mid-generation. With a rate cap this repeated forever on one generation. Proactive repairs are now counted and resumed.
2. **Silent receiver:** the receiver sent no ACK when only repairs for already-solved generations arrived, so the sender never learned it was stuck. It now always ACKs (rate-limited as before).
3. Repair requests are no longer double-decremented when the pacer cuts a burst short.
4. **Measured pacing instead of a fixed 50 Mbps** (`_DeliveryRateController`):
   - Starts at 2 Mbps and doubles per RTT while queueing delay stays low.
   - Then holds near the point where queueing delay begins; random loss does not slow it.
   - `max_tx_kbps` is now a ceiling. `max_tx_kbps=None` is still unpaced.
   - ACKs carry an optional 4-byte "bytes received" trailer; old parsers ignore it.

Raw results: `fair_transport_benchmark_after_fix.json`. Old code: `backup/`. All 20 existing tests pass.

### Before vs after (median of 3; time and wire bytes ÷ file size)

**64 KB:**

| Link | TCP BBR | NLTP default (before) | NLTP default (after) | NLTP capped (before) | NLTP capped (after) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.09 s 1.06× | 0.13 s 1.19× | 0.16 s 1.15× | 0.06 s 1.15× | 0.16 s 1.14× |
| 20M/40ms/2% | 0.18 s 1.06× | 0.24 s 1.18× | 0.25 s 1.19× | 0.12 s 1.19× | 0.31 s 1.23× |
| 10M/100ms/5% | 0.51 s 1.10× | 0.26 s 1.16× | 0.53 s 1.31× | 0.27 s 1.26× | 0.45 s 1.21× |
| 5M/200ms/10% | 1.02 s 1.13× | 0.51 s 1.40× | 0.80 s 1.38× | 0.50 s 1.30× | 0.64 s 1.35× |
| 2M/300ms/20% | 3.27 s 1.36× | 0.82 s 1.58× | 1.54 s 1.71× | 0.95 s 1.45× | 1.30 s 1.47× |
| 10M/100ms/10% bursty | 1.55 s 1.12× | 0.45 s 1.67× | 0.44 s 1.27× | 0.27 s 1.28× | 0.38 s 1.19× |

**1 MB:**

| Link | TCP BBR | NLTP default (before) | NLTP default (after) | NLTP capped (before) | NLTP capped (after) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.51 s 1.04× | 0.67 s 2.53× | 0.69 s 1.40× | 0.52 s 1.08× | 0.63 s 1.08× |
| 20M/40ms/2% | 0.65 s 1.07× | 0.67 s 1.83× | 0.80 s 1.18× | 0.68 s 1.14× | 0.81 s 1.14× |
| 10M/100ms/5% | 1.49 s 1.12× | 1.97 s 1.85× | 1.62 s 1.25× | 1.52 s 1.23× | 1.63 s 1.20× |
| 5M/200ms/10% | 3.45 s 1.15× | 4.16 s 1.94× 2/3 | 3.25 s 1.38× | 3.02 s 1.32× | 3.37 s 1.36× |
| 2M/300ms/20% | 8.31 s 1.32× | 8.32 s 2.75× | **6.91 s 1.61×** | FAIL 0/3 | **7.63 s 1.52×** |
| 10M/100ms/10% bursty | 13.10 s 1.10× 2/3 | 2.14 s 2.42× | **1.89 s 1.36×** | 1.92 s 1.35× 2/3 | **1.77 s 1.35×** |

**Fairness** (10 Mbit, 40 ms, a TCP CUBIC flow alone gets 9.5 Mbps):

| Sharing with | TCP CUBIC | Other flow |
|---|---|---|
| a 2nd CUBIC flow | 3.9 Mbps | 5.6 Mbps |
| NLTP default | 8.3 Mbps | 1.6 Mbps |
| NLTP capped (9 Mbps ceiling) | 7.1 Mbps | 7.0 Mbps |

NLTP no longer takes more than its share. With the default ceiling it backs off too far against CUBIC, as delay-based pacing usually does.

### Verdict on the fix

- **Reliability:** fixed. NLTP completed 72 of 72 transfers. Before the fix, 5 runs failed: one NLTP default run on the 10% link, three capped runs on the 20% link and one capped run on the bursty link.
- **Data efficiency:** fixed. 1 MB wire overhead dropped from 1.8–2.75× to 1.08–1.61×, close to TCP's 1.04–1.32×.
- **Better than TCP (BBR):**

  | Where | Result |
  |---|---|
  | Bursty loss | **About 7×** faster (1 MB), about 4× (64 KB) |
  | 20% loss | About 1.1–1.2× faster (1 MB), about 2–2.5× (64 KB) |
  | 10% loss | A tie (1 MB), about 1.3–1.6× faster (64 KB) |
  | Clean, 2% and 5% loss | **5–25% slower** |

- **Below the targets estimated beforehand**, which were about 1.4–1.5× at 5–20% random loss.
- **Regression:** small files on light-loss links are slower than before, because pacing now starts at 2 Mbps instead of 50 Mbps.

## Update 2 — small-file tuning

### Changes

1. **Initial window.** A transfer that starts on an idle connection may send its first 32 KB unpaced, like TCP's initial window. This is `TokenBucketPacer.grant_burst`; the 32 KB is `_DeliveryRateController.INIT_BURST_BYTES`.
2. **Faster startup.** The startup ramp now doubles from the *measured* delivery rate (`max(2 × rate, 2 × delivered)`), so fast links reach full speed in fewer round trips.

Raw results: `fair_transport_benchmark_tuned.json`. All 20 tests pass.

**64 KB** (wire bytes ÷ file size after each time):

| Link | TCP BBR | NLTP default (fix) | NLTP default (tuned) | NLTP capped (fix) | NLTP capped (tuned) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.09 s 1.06× | 0.16 s 1.15× | 0.11 s 1.10× | 0.16 s 1.14× | 0.11 s 1.09× |
| 20M/40ms/2% | 0.18 s 1.06× | 0.25 s 1.19× | 0.18 s 1.20× | 0.31 s 1.23× | 0.15 s 1.18× |
| 10M/100ms/5% | 0.51 s 1.10× | 0.53 s 1.31× | 0.41 s 1.26× | 0.45 s 1.21× | 0.40 s 1.29× |
| 5M/200ms/10% | 1.02 s 1.13× | 0.80 s 1.38× | 0.95 s 1.39× | 0.64 s 1.35× | 0.55 s 1.35× |
| 2M/300ms/20% | 3.27 s 1.36× | 1.54 s 1.71× | 1.58 s 1.70× | 1.30 s 1.47× | 1.40 s 1.62× |
| 10M/100ms/10% bursty | 1.55 s 1.12× | 0.44 s 1.27× | 0.52 s 1.50× | 0.38 s 1.19× | 0.58 s 1.38× |

**1024 KB** (wire bytes ÷ file size after each time):

| Link | TCP BBR | NLTP default (fix) | NLTP default (tuned) | NLTP capped (fix) | NLTP capped (tuned) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.51 s 1.04× | 0.69 s 1.40× | 0.63 s 1.42× | 0.63 s 1.08× | 0.56 s 1.07× |
| 20M/40ms/2% | 0.65 s 1.07× | 0.80 s 1.18× | 0.69 s 1.13× | 0.81 s 1.14× | 0.67 s 1.12× |
| 10M/100ms/5% | 1.49 s 1.12× | 1.62 s 1.25× | 1.46 s 1.25× | 1.63 s 1.20× | 1.49 s 1.22× |
| 5M/200ms/10% | 3.45 s 1.15× | 3.25 s 1.38× | 3.24 s 1.37× | 3.37 s 1.36× | 3.17 s 1.33× |
| 2M/300ms/20% | 8.31 s 1.32× | 6.91 s 1.61× | 8.13 s 1.58× | 7.63 s 1.52× | 7.67 s 1.59× |
| 10M/100ms/10% bursty | 13.10 s 1.10× 2/3 | 1.89 s 1.36× | 1.69 s 1.47× | 1.77 s 1.35× | 1.76 s 1.32× |

**Fairness:** unchanged. Sharing with TCP CUBIC on 10 Mbit / 40 ms:

| NLTP setting | TCP CUBIC | NLTP |
|---|---|---|
| default | 8.9 Mbps | 1.5 Mbps (yields) |
| capped | 7.1 Mbps | 6.8 Mbps (even share) |

### Final standing vs TCP BBR

**Reliability:** 72 of 72 transfers succeeded. TCP BBR itself failed 1 of 3 on the bursty link.

**64 KB:**

| Link | NLTP (tuned) | TCP BBR | Result |
|---|---|---|---|
| Clean | 0.11 s | 0.09 s | Tie |
| 2% loss | 0.15–0.18 s | 0.18 s | Tie or faster |
| 5% loss | 0.40–0.41 s | 0.51 s | About 1.25× faster |
| 10% loss | 0.55–0.95 s | 1.02 s | 1.1–1.9× faster |
| 20% loss | 1.4–1.6 s | 3.27 s | About 2.2× faster |
| Bursty | 0.52–0.58 s | 1.55 s | About 2.8× faster |

**1 MB:**

| Link | NLTP (tuned) | TCP BBR | Result |
|---|---|---|---|
| Clean | 0.56–0.63 s | 0.51 s | 10–25% slower |
| 2% and 5% loss | 0.67–0.69 s and 1.46–1.49 s | 0.65 s and 1.49 s | Tie |
| 10% loss | 3.17–3.24 s | 3.45 s | About 7% faster |
| 20% loss | 7.7–8.1 s | 8.31 s | About 3–8% faster |
| Bursty | 1.69–1.76 s | 13.1 s | About 7.5× faster |

**Data use:**
- Capped setting: 1.07–1.6× the file size.
- Default setting: up to 1.7×. On clean 1 MB transfers it still sends about 1.4× (vs 1.07× capped), because the 50 Mbps ceiling lets startup overshoot.

**Tuning trade-offs, measured:**
- Bursty-loss 64 KB transfers got slightly slower than right after the fix: 0.38–0.44 s became 0.52–0.58 s.
- On the 20%-loss 1 MB link, the default setting went from 6.9 s to 8.1 s. That is within run-to-run noise for 3 runs, or it may be startup overshoot on a very slow link.

**Bottom line:** NLTP is now never meaningfully worse than modern TCP, except clean 1 MB transfers (10–25% slower). It is clearly better for small transfers on lossy links and for any size under bursty loss. It is not better than TCP across the board.

## Update 3 — speed tuning (measured first, then changed)

### What the measurements showed

- **CPU is not the limit.** NLTP reached its own 50 Mbps ceiling on a 200 Mbps link.
- **Time was lost at the start:** about 3 round trips of slow ramp-up from 2 Mbps.
- **Time was lost at the end:** a final repair round trip, because no proactive repairs were sent below 8% loss.
- **On clean links, startup overshoot was mistaken for link loss**, which triggered pointless repair packets.

### Changes (`necklink/nltp.py`)

1. **Packet-train bandwidth probe.**
   - The receiver measures the arrival rate of each 16-shard train.
   - It reports that rate as a second 4-byte ACK trailer field; old parsers ignore it.
   - The initial 32 KB burst reaches the receiver spaced at the bottleneck rate, so the sender learns the link speed after **one** round trip and jumps straight to it.
   - In cruise, the highest arrival rate over the last 6 rounds caps how far the pacer may probe.
2. **Queue-free loss estimate.** Random loss is estimated as sent-versus-delivered bytes, only in rounds without a standing queue. Proactive repairs and repair sizing use it, so self-inflicted drops no longer inflate repairs.
3. **HyStart-style startup exit.** Startup ends on the first RTT sample showing queueing, instead of waiting for the end of the round.
4. **Cruise drains to measured capacity** instead of cutting 15% again and again.
5. **Proactive repairs from 3% real loss upward**, at ⌈k·p·1.1⌉ per generation. The last round trip of data also gets 1–2 extra shards, so the transfer does not wait an extra round trip at the end.

Raw results: `fair_transport_benchmark_tuned2.json`. All 20 tests pass.

**64 KB:**

| Link | TCP BBR | NLTP default (tuning 1) | NLTP default (tuning 2) | NLTP capped (tuning 1) | NLTP capped (tuning 2) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.09 s 1.06× | 0.11 s 1.10× | 0.07 s 1.23× | 0.11 s 1.09× | 0.08 s 1.23× |
| 20M/40ms/2% | 0.18 s 1.06× | 0.18 s 1.20× | 0.11 s 1.25× | 0.15 s 1.18× | 0.11 s 1.22× |
| 10M/100ms/5% | 0.51 s 1.10× | 0.41 s 1.26× | 0.45 s 1.39× | 0.40 s 1.29× | 0.31 s 1.21× |
| 5M/200ms/10% | 1.02 s 1.13× | 0.95 s 1.39× | 0.52 s 1.29× | 0.55 s 1.35× | 0.71 s 1.25× |
| 2M/300ms/20% | 3.27 s 1.36× | 1.58 s 1.70× | 0.85 s 1.53× | 1.40 s 1.62× | 2.35 s 2.07× |
| 10M/100ms/10% bursty | 1.55 s 1.12× | 0.52 s 1.50× | 0.70 s 1.45× | 0.58 s 1.38× | 0.23 s 1.22× |

**1024 KB:**

| Link | TCP BBR | NLTP default (tuning 1) | NLTP default (tuning 2) | NLTP capped (tuning 1) | NLTP capped (tuning 2) |
|---|---|---|---|---|---|
| clean 20M/20ms | 0.51 s 1.04× | 0.63 s 1.42× | 0.57 s 1.20× | 0.56 s 1.07× | 0.55 s 1.12× |
| 20M/40ms/2% | 0.65 s 1.07× | 0.69 s 1.13× | 0.65 s 1.23× | 0.67 s 1.12× | 0.71 s 1.22× |
| 10M/100ms/5% | 1.49 s 1.12× | 1.46 s 1.25× | 1.31 s 1.28× | 1.49 s 1.22× | 1.32 s 1.27× |
| 5M/200ms/10% | 3.45 s 1.15× | 3.24 s 1.37× | 2.91 s 1.40× | 3.17 s 1.33× | 2.86 s 1.36× |
| 2M/300ms/20% | 8.31 s 1.32× | 8.13 s 1.58× | 6.79 s 1.61× | 7.67 s 1.59× | 7.97 s 1.60× |
| 10M/100ms/10% bursty | 13.10 s 1.10× 2/3 | 1.69 s 1.47× | 1.71 s 1.50× | 1.76 s 1.32× | 1.62 s 1.51× |

**Fairness changed.** Sharing 10 Mbit / 40 ms with one TCP CUBIC flow, both NLTP settings now back off to about 1.5 Mbps while TCP keeps 8.3–8.8 Mbps. The capped setting had an even share after tuning 1.

The cause is the cruise rule: when the queue grows it drains to measured capacity, and a loss-based TCP flow keeps the queue full. So NLTP is safe for other traffic, but it is starved on a shared bottleneck. That is fine on a dedicated radio link and a real limitation on a shared internet path.

### Standing vs TCP BBR after tuning 2

**Reliability:** 72 of 72 transfers succeeded.

**64 KB:**

| Link | NLTP | TCP BBR | Result |
|---|---|---|---|
| Clean | 0.075 s | 0.093 s | About 20% faster |
| 2% loss | 0.11 s | 0.18 s | About 1.6× faster |
| 5% loss | 0.31–0.45 s | 0.51 s | 1.1–1.7× faster |
| 10% loss | 0.52–0.71 s | 1.02 s | 1.4–2× faster |
| 20% loss, default | 0.85 s | 3.27 s | About 3.8× faster |
| 20% loss, capped | 2.36 s, 2.07× bytes | 3.27 s | A regression; see open issues |
| Bursty | 0.23–0.70 s | 1.55 s | 2.2–6.7× faster (high variance) |

**1 MB:**

| Link | NLTP | TCP BBR | Result |
|---|---|---|---|
| Clean | 0.55–0.57 s | 0.51 s | 8–12% slower |
| 2% loss | 0.65–0.71 s | 0.65 s | Tie |
| 5% loss | 1.31 s | 1.49 s | About 1.14× faster |
| 10% loss | 2.86–2.91 s | 3.45 s | About 1.2× faster |
| 20% loss | 6.8–8.0 s | 8.31 s | 1.04–1.22× faster |
| Bursty | 1.62–1.71 s | 13.1 s | About 8× faster |

**Data use:** NLTP sends 1.12–1.61× the file size, versus BBR's 1.04–1.32×. That is the cost of proactive and tail repair shards.

### Open issues

- The capped setting on the 20%-loss 64 KB transfer regressed: 2.36 s and 2.07× bytes, against 1.30 s and 1.47× in tuning 1.
- NLTP is starved when it shares a bottleneck with loss-based TCP (see fairness above).
- Only 3 runs per cell: the bursty-link numbers swing by about 3×.

## Update 4 — NLTP vs existing fast-UDP tools (KCP, kcptun)

### Contenders

- **KCP** 2.1.1 (skywind3000/kcp, MIT) in its documented "fast mode" (nodelay=1, interval=10 ms, fast resend=2, no congestion control). It runs through `bench_kcp/kcp_xfer.c`, tested with two windows:
  - 1,024 packets, common in tuned setups;
  - 128 packets, the KCP README example.
- **kcptun** (Ubuntu package 20210922): KCP plus Reed-Solomon FEC (10 data + 3 parity), `--mode fast`, encryption and compression off. Kernel TCP runs through the tunnel.
- **KCP with congestion control** (`nc=0`) was dropped part-way. It took 2–28 s where the others took under 3 s.

Same netem links, same byte counting, 3 runs per cell. NLTP figures are from tuning 2. Setup: `scripts/wsl/setup_kcp.sh`. Raw logs: `fair_transport_benchmark_kcp*.log` and `*_kcp_rest.json` / `*_kcp128.json`.

**64 KB** — time (data sent ÷ file size); **bold** = fastest

| Link | TCP BBR | NLTP default | NLTP capped | KCP fast w1024 | KCP fast w128 | kcptun (KCP+FEC) |
|---|---|---|---|---|---|---|
| clean | 0.09 s (1.06×) | 0.07 s (1.23×) | 0.08 s (1.23×) | **0.05 s (1.06×)** | 0.06 s (1.06×) | 0.06 s (1.39×) |
| 2% loss | 0.18 s (1.06×) | 0.11 s (1.25×) | 0.11 s (1.22×) | **0.07 s (1.13×)** | 0.25 s (1.09×) | 0.10 s (1.39×) |
| 5% loss | 0.51 s (1.10×) | 0.45 s (1.39×) | 0.31 s (1.21×) | 0.36 s (1.12×) | 0.24 s (1.24×) | **0.23 s (1.40×)** |
| 10% loss | 1.02 s (1.13×) | 0.52 s (1.29×) | 0.71 s (1.25×) | **0.50 s (2.15×)** | 0.50 s (2.15×) | 0.74 s (2.28×) |
| 20% loss | 3.27 s (1.36×) | 0.85 s (1.53×) | 2.35 s (2.07×) | 1.18 s (2.61×) | **0.77 s (2.56×)** | 1.37 s (2.54×) |
| bursty 10% | 1.55 s (1.12×) | 0.70 s (1.45×) | 0.23 s (1.22×) | **0.17 s (1.07×)** | 0.31 s (1.32×) | 0.33 s (1.70×) |

**1024 KB** — time (data sent ÷ file size); **bold** = fastest

| Link | TCP BBR | NLTP default | NLTP capped | KCP fast w1024 | KCP fast w128 | kcptun (KCP+FEC) |
|---|---|---|---|---|---|---|
| clean | **0.51 s (1.04×)** | 0.57 s (1.20×) | 0.55 s (1.12×) | 6.22 s (6.16×) | 0.60 s (1.54×) | 0.74 s (2.25×) |
| 2% loss | **0.65 s (1.07×)** | **0.65 s (1.23×)** | 0.71 s (1.22×) | 1.29 s (3.90×) | 0.85 s (1.25×) | 0.81 s (1.62×) |
| 5% loss | 1.49 s (1.12×) | **1.31 s (1.28×)** | 1.32 s (1.27×) | 2.65 s (3.10×) | 1.74 s (1.45×) | 1.52 s (1.75×) |
| 10% loss | 3.45 s (1.15×) | 2.91 s (1.40×) | **2.86 s (1.36×)** | 5.65 s (3.26×) | 4.08 s (1.61×) | 3.15 s (1.72×) |
| 20% loss | 8.31 s (1.32×) | **6.79 s (1.61×)** | 7.97 s (1.60×) | 70.48 s (4.54×) | 8.98 s (2.11×) | 9.38 s (2.25×) |
| bursty 10% | 13.10 s (1.10×) 2/3 | 1.71 s (1.50×) | **1.62 s (1.51×)** | 2.87 s (3.31×) | 2.16 s (1.63×) | 2.07 s (1.71×) |

### Verdict

**Small transfers (64 KB): the KCP family wins.**
- KCP or kcptun is fastest on every link. NLTP is never fastest at this size.
- At 10–20% loss NLTP sends far less data (1.29–1.53×) than KCP or kcptun (2.15–2.6×), at similar speed. The one exception is NLTP capped at 20% loss, which regressed (2.35 s, 2.07×).

**Larger transfers (1 MB): NLTP wins on every lossy link.**
- It is fastest at 5%, 10%, 20% and bursty loss, and level with TCP BBR at 2% loss.
- It also sends less data than every KCP variant: 1.12–1.61× against 1.25–6.16×.
- KCP with a 1,024 window overflows router queues and resends the file 3–6 times over.
- **TCP BBR is still best on a clean link.**

**What this means:**
- For moving **files or firmware over lossy or bursty radio links**, NLTP is the best of the tools tested here, on both speed and data cost.
- For **small messages**, KCP or kcptun is as fast or faster.
- This does not make NLTP "the best thing out there". Other contenders are untested: UDT, QUIC with FEC extensions, Aspera FASP and others. The links were emulated, and the kcptun package dates from 2021.

## Update 5 — closing the small-file gap, and every obtainable competitor

### Closing the small-file gap (64 KB)

| Change | Result | Decision |
|---|---|---|
| 64 KB cold-start burst | Slower everywhere (e.g. 0.13 s vs 0.075 s on clean). NLTP's 960-byte shards make 64 KB about 68 packets, which overflows a ~50-packet router queue; KCP's 1,400-byte packets make it about 47. It also broke the 32-packet router-safety test (25 tail drops). | **Rejected.** A 64 KB burst is available only as the opt-in `initial_burst_bytes`. |
| Warm connection with a larger burst | Faster on lossy links, but it overflowed the queue on clean links and then waited a repair timeout. | **Rejected.** |
| Warm connection: no burst, resume at the learned rate | Clean 0.053 s (KCP 0.051), 5% loss 0.17 s (best of all tools). | **Kept.** |
| Tail insurance: one parity shard on the last generation | Avoids a repair-timeout stall when the final packets are lost. Bursty 1 MB improved from 1.71 s to 1.34 s. | **Kept.** |

**Verdict:** the gap is partly closed. On an open connection NLTP ties KCP on clean links and at 10% loss, and beats everything at 5%. KCP is still faster for small messages at 2% loss, 20% loss and on bursty links.

### Competitors added

| Tool | What it is | Version | Setting |
|---|---|---|---|
| UDT | `bench_kcp/udt_xfer.cpp` on UDT4 | 4.11 | Its own congestion control |
| SRT | `srt-file-transmit` | 1.5.3 | File mode |
| QUIC | `gtlsclient` / `gtlsserver` (ngtcp2, C) | 0.12.1 | BBR and CUBIC; HTTP/3 download over the symmetric link |

All installed from the Ubuntu archive. Per-transfer timeout: 30 s.

- **UFTP 4.10.2** was dropped: 3–5 s even on a clean link, because of its multi-phase session setup.
- **Not obtainable:** Aspera FASP (commercial licence), QUIC with FEC extensions (no released implementation), Tsunami-UDP (not packaged).

Raw logs: `fair_transport_benchmark_competitors.log`, `_warm64.log`, `_tuned3.log`.

### All tools on the same links

**64 KB** — median time; data sent ÷ file size in brackets; **bold** = fastest; NLTP warm measured at 64 KB only

| Link | TCP BBR | NLTP cold | NLTP warm | KCP w128 | KCP w1024 | kcptun | QUIC ngtcp2 BBR | QUIC ngtcp2 CUBIC | UDT | SRT |
|---|---|---|---|---|---|---|---|---|---|---|
| clean | 0.09 (1.06×) | 0.07 (1.25×) | 0.05 (2.36×) | 0.06 (1.06×) | **0.05 (1.06×)** | 0.06 (1.39×) | 0.17 (1.21×) | 0.17 (1.22×) | 0.12 (1.05×) | 0.32 (1.05×) |
| 2% loss | 0.18 (1.06×) | 0.11 (1.22×) | 0.08 (2.60×) | 0.25 (1.09×) | **0.07 (1.13×)** | 0.10 (1.39×) | 0.27 (1.34×) | 0.21 (1.25×) | 0.22 (1.07×) | 0.37 (1.08×) |
| 5% loss | 0.51 (1.10×) | 0.25 (1.26×) | **0.17 (2.66×)** | 0.24 (1.24×) | 0.36 (1.12×) | 0.23 (1.40×) | 0.52 (1.36×) | 1.47 (1.49×) | 0.53 (1.08×) | 0.77 (1.14×) |
| 10% loss | 1.02 (1.13×) | 0.72 (1.29×) | 0.53 (2.80×) | 0.50 (2.15×) | **0.50 (2.15×)** | 0.74 (2.28×) | 1.17 (1.23×) | 1.02 (1.29×) | 3.54 (1.31×) | 4.18 (1.41×) 1/3 |
| 20% loss | 3.27 (1.36×) | 0.82 (1.56×) | 0.97 (3.46×) | **0.77 (2.56×)** | 1.18 (2.61×) | 1.37 (2.54×) | 3.23 (1.79×) | 2.73 (1.45×) | 8.10 (1.80×) | 4.41 (1.34×) 2/3 |
| bursty | 1.55 (1.12×) | 0.36 (1.54×) | 0.34 (2.93×) | 0.31 (1.32×) | **0.17 (1.07×)** | 0.33 (1.70×) | 1.17 (1.50×) | 0.52 (1.21×) | 0.64 (1.18×) | 2.52 (1.71×) |

**1024 KB** — median time; data sent ÷ file size in brackets; **bold** = fastest; NLTP warm measured at 64 KB only

| Link | TCP BBR | NLTP cold | NLTP warm | KCP w128 | KCP w1024 | kcptun | QUIC ngtcp2 BBR | QUIC ngtcp2 CUBIC | UDT | SRT |
|---|---|---|---|---|---|---|---|---|---|---|
| clean | **0.51 (1.04×)** | 0.58 (1.24×) | — | 0.60 (1.54×) | 6.22 (6.16×) | 0.74 (2.25×) | 0.57 (1.05×) | 0.62 (1.05×) | 0.95 (1.15×) | 0.87 (1.15×) |
| 2% loss | 0.65 (1.07×) | **0.62 (1.25×)** | — | 0.85 (1.25×) | 1.29 (3.90×) | 0.81 (1.62×) | 0.67 (1.07×) | 0.72 (1.07×) | 3.02 (1.07×) | 1.37 (1.05×) |
| 5% loss | 1.49 (1.12×) | **1.35 (1.31×)** | — | 1.74 (1.45×) | 2.65 (3.10×) | 1.52 (1.75×) | 1.57 (1.07×) | 1.57 (1.09×) | 9.98 (1.19×) | 6.49 (1.24×) |
| 10% loss | 3.45 (1.15×) | **2.79 (1.41×)** | — | 4.08 (1.61×) | 5.65 (3.26×) | 3.15 (1.72×) | 4.42 (1.16×) | 7.74 (1.15×) | 26.47 (1.37×) | 14.73 (1.50×) 1/3 |
| 20% loss | 8.31 (1.32×) | **6.70 (1.64×)** | — | 8.98 (2.11×) | 70.48 (4.54×) | 9.38 (2.25×) | 11.87 (1.26×) | 24.40 (1.26×) 1/3 | FAIL 0/3 | FAIL 0/3 |
| bursty | 13.10 (1.10×) 2/3 | **1.34 (1.35×)** | — | 2.16 (1.63×) | 2.87 (3.31×) | 2.07 (1.71×) | 8.90 (1.21×) | 3.48 (1.05×) | 9.55 (1.34×) | 4.11 (1.39×) 2/3 |

The NLTP warm data figures include the untimed warm-up transfer on the same connection, so the real per-transfer figure is roughly half (about 1.2–1.5×).

### Verdict

**1 MB (files, firmware): NLTP is fastest at 2%, 5%, 10%, 20% and bursty loss, against all 9 alternatives.**

| Link | NLTP | Best alternative | NLTP advantage |
|---|---|---|---|
| 5% loss | 1.35 s | kcptun 1.52 s | about 13% |
| 10% loss | 2.79 s | kcptun 3.15 s | about 13% |
| 20% loss | 6.70 s | TCP BBR 8.31 s | about 24% |
| Bursty | 1.34 s | QUIC CUBIC 3.48 s | about 2.6× |

- Production QUIC (ngtcp2 + BBR) is the strongest of the new tools. It is level with NLTP up to 2% loss and sends less data: about 1.05–1.26× against 1.24–1.64×.
- **On a clean link, TCP BBR is fastest** (0.51 s). NLTP and QUIC are about 12–14% slower.
- UDT and SRT collapse on lossy links: 10–26 s at 5–10% loss, and both failed every 20%-loss transfer.

**64 KB (short messages): KCP fast mode is still fastest on most links.** NLTP warm is fastest at 5% loss. NLTP beats UDT, SRT, QUIC and TCP on every link at this size.

## Update 6 — extended tests, jitter fix, C core and ESP32 hardware

### Extended link tests (3 runs per cell, 30 s timeout)

Raw logs: `fair_transport_benchmark_extended.log`, `_10mb.log`, `_jitterfix*.log`.

| Link | NLTP | Best rival | Verdict |
|---|---|---|---|
| Jitter ±20 ms (reordering), 1 MB | before fix: 1/3 ok (default), 0/3 (encrypted); **after fix: 3/3** | kcptun 1.39 s, QUIC 1.52 s | Reliable now, still behind on this link |
| Reorder 10%, 1 MB | 1.19–1.24 s | TCP BBR 1.12 s | About 10% behind |
| **GEO satellite 600 ms / 3%, 64 KB** | **0.91–1.02 s** | KCP 1.12 s, TCP 2.77 s | **NLTP fastest** |
| **GEO satellite 600 ms / 3%, 1 MB** | **6.05 s** | QUIC 7.29 s, TCP 8.85 s | **NLTP fastest** |
| Asymmetric 1 Mbit up, 1 MB | 11.0 s | TCP 9.85 s, KCP 10.8 s | TCP about 10% faster (\*) |
| 10 MB, clean | 5.64 s | TCP 4.45 s | TCP about 27% faster |
| 10 MB, 5% loss | 11.3 s | TCP / QUIC 9.8 s | TCP about 15% faster |
| **10 MB, bursty** | **13.8 s, 3/3** | TCP and QUIC 1/3 each (60 s timeout) | **NLTP the only reliable one** |

(\*) The QUIC adapter downloads, so in the asymmetric test QUIC moved data over the fast 10 Mbit direction. Its 2.6 s there is not comparable.

Encrypted NLTP (ChaCha20-Poly1305) costs about 0–10% in time.

**Conclusion:**
- NLTP's advantage is in **small and medium transfers** over lossy, bursty and long-delay links (satellite included).
- On **long transfers with random loss**, modern TCP and QUIC are faster.

### Jitter fix (Python and C)

**Cause:** with ±20 ms of jitter, three things went wrong together:
- Per-round delay variation was read as queueing, so the rate was cut every round (10.7 → 2.4 Mbit).
- Reordered packets were counted as lost (a 64–70% estimate on a 5% link).
- That estimate then drove about 6 repair shards per 8 data shards.

**Fixes:**
- The controller tracks RTT variance; the queue threshold and the startup exit now allow for it.
- Random loss compares deliveries with what was sent **one smoothed RTT earlier** (sent-bytes history), and skips the first two rounds.
- Proactive repair is capped at half a generation.

**Regression check** (`_jitterfix.log`): 72/72 ok.
- 1 MB improved at 5%, 10% and 20% loss (1.28 / 2.78 / 6.36 s).
- Two cells got worse: 64 KB at 20% (0.83 → 1.42 s) and bursty 1 MB (1.34 → 1.84 s). Short transfers now fall back to the older receiver-reported loss estimate early on.

### Portable C core

Files: `csrc/nltp_core.{h,c}`, `csrc/nltp_sha256.{h,c}`.

- C99, no heap allocation, no floating point. Compiles clean with `-std=c99 -Wall -Wextra -pedantic -Werror`.
- Sender and receiver include:
  - GF(2^8) Cauchy repair with incremental Gauss-Jordan decoding (never stuck on a singular set);
  - generation window, DOF repair requests, proactive and tail repair;
  - ACKs with the bytes / arrival-rate trailer;
  - SHA-256 verification;
  - an integer port of the rate controller (cold-start burst, packet-train probe, jitter-aware queue detection).
- Memory is fixed at compile time: `NLTP_RX_SLOTS` × ~7.8 KB plus a 32-generation send window.
- **Not yet in C:** encryption (the AEAD code already exists in `nltp_wire.h`), relay/STUN, reflex and telemetry lanes, and more than one stream per direction.
- **Interop** (`scripts/c_interop_test.py`, Linux loopback): **24/24 pairings verified**.
  - Pairings: C→C, C→Python, Python→C, Python→Python.
  - Conditions: 0% and 15% loss; 1 KB (single short generation), 64 KB and 1 MB.

### ESP32 hardware

Board: ESP32-D0WDQ6 rev 1.1 on COM9. Firmware: `esp32/` (PlatformIO, Arduino-ESP32 3.3.12).

- The core compiles unchanged for the ESP32. Static RAM 14.9%, flash 68%.
- At run time: the endpoint takes 65 KB with 8 receive slots, and 88–90 KB of heap stays free alongside Wi-Fi.
- Test: `scripts/esp32_test.py`, home Wi-Fi, RSSI −38 to −42 dBm, 3 runs. **27/27 transfers verified**, 0 CRC drops.

| Size | NLTP laptop → ESP32 | TCP laptop → ESP32 (ESP32 lwIP) | NLTP ESP32 → laptop |
|---|---|---|---|
| 16 KB | 0.084 s | **0.061 s** | 0.076 s |
| 32 KB | **0.111 s** | 0.117 s | 0.089 s |
| 60 KB | **0.161 s** | 0.203 s | 0.130 s |

This was a clean, strong-signal link. NLTP has not yet been measured on a lossy radio link on the ESP32.

### Update 7 — encryption in the C core

**Changes:**
- `csrc/nltp_core.{h,c}` now support optional ChaCha20-Poly1305 encryption with the RFC 6479 replay window (`nltp_set_key`, `nltp_key_from_psk`). It reuses the existing `nltp_wire.h` primitives.
- Byte-compatible with Python `psk=`: same key derivation (a 43/44-char `generate_nltp_key()` key is used as-is, a passphrase is trimmed and hashed), same nonce (conn_id + 64-bit sequence starting at 1), same authenticated 20-byte header.
- With a key set, unencrypted frames are rejected, as in Python.
- A frame from a new peer conn_id is checked against a fresh replay window. That window is adopted only after the frame authenticates, so forged packets cannot reset the current peer's window.

**Security self-test** (`csrc/tools/nltp_selftest.c`, two endpoints over a simulated link): **9/9 passed.**
- Key derivation matches Python for a base64 key and for a passphrase.
- Plaintext and encrypted 200 KB transfers complete; the encrypted one also completes at 10% loss.
- 5% bit-flipped packets: all 12 rejected, file intact (repaired).
- Every 7th packet replayed: all 32 rejected, file intact.
- Wrong key: 0 accepted (668 rejected), no completion.
- Unencrypted sender to an encrypted receiver: refused (671 rejected).

**Interop** (`scripts/c_interop_test.py`): **72/72 pairings verified.**
- Pairings: C→C, C→Python, Python→C, Python→Python.
- Key modes: none, a `generate_nltp_key()` key, a passphrase.
- Conditions: 0% / 15% loss; 1 KB, 64 KB, 1 MB.

**ESP32, encrypted, home Wi-Fi** (`scripts/esp32_test.py --psk auto`):
- The key is sent over USB serial at run time and never stored in the firmware.
- **27/27 verified**, 0 CRC drops, 0 authentication drops.

| Median time | NLTP plain | NLTP encrypted | TCP (plain, same run) |
|---|---|---|---|
| Laptop → ESP32, 16 KB | 0.084 s | 0.126 s | 0.131 s |
| Laptop → ESP32, 32 KB | 0.111 s | 0.134 s | 0.123 s |
| Laptop → ESP32, 60 KB | 0.161 s | 0.216 s | 0.212 s |
| ESP32 → laptop, 16 KB | 0.076 s | 0.061 s | — |
| ESP32 → laptop, 60 KB | 0.130 s | 0.212 s | — |

Encryption costs roughly 25–60% per transfer on the ESP32, which has no ChaCha20 hardware. Encrypted NLTP stays level with unencrypted TCP. Wi-Fi conditions varied between runs (TCP 16 KB: 0.061 s vs 0.131 s), so the percentages are rough.

### Field-test kit

`scripts/field_test.py` runs NLTP vs kernel TCP (and KCP when built) between any two machines you control, for example over the boat or buoy radio. It needs one side to be reachable directly.

## Update 8 — virtual radio links, and relay / NAT traversal in C

### Radio link emulator + ESP32

`scripts/radio_emulator.py` sits between the laptop and the ESP32 as a UDP relay and applies a radio profile in each direction: random loss, Gilbert-Elliott fades (bursts), delay with jitter, and a rate limit. `scripts/esp32_test.py --radio PROFILE --psk auto` runs every transfer through it, encrypted.

| Profile | Emulated link | Measured loss (up / down) |
|---|---|---|
| wifi-far | 5% loss, 10 ± 5 ms, 5 Mbit | 5.3% / 6.4% |
| 4g-weak | 3% + 4% fades, 60 ± 20 ms, 2 Mbit | 8.5% / 12.9% |
| marine-radio | 5% + 6% fades, 150 ± 30 ms, 1 Mbit | 18.1% / 6.4% |
| very-bad | 20%, 250 ± 40 ms, 1 Mbit | 20.8% / 20.8% |

**48/48 encrypted transfers verified** across the four profiles. After the firmware change described below, a marine-radio recheck passed 8/8.

Median time, real ESP32 over the emulated link, encrypted:

| Profile | 16 KB laptop → ESP32 | 16 KB ESP32 → laptop | 58 KB laptop → ESP32 | 58 KB ESP32 → laptop |
|---|---|---|---|---|
| wifi-far | 0.13 s | 0.15 s | 0.25 s | 0.24 s |
| 4g-weak | 0.31 s | 0.39 s | 0.85 s | 0.63 s |
| marine-radio | 0.53 s | 0.55 s | 1.53 s | 0.89 s |
| very-bad | 1.52 s | 0.94 s | 1.68 s | 2.80 s |

TCP is not measured through this emulator, because it is a UDP relay. The netem tables above remain the head-to-head comparison.

**Firmware fix found by this test:** the board used to adopt the source address of *any* packet as its peer, so one stray or forged datagram could redirect its replies. Now the address is followed only when the frame passes CRC or authentication. Roaming and NAT rebinding still work.

### Relay, STUN, hole punching and keepalive in C

New `csrc/nltp_net.{h,c}` (C99, no heap), wire-compatible with Python's `NLTPRelayHub` and `discover_wan`:

- **Relay framing:** "NR" + command + 16-byte room tag (SHA-256 of the room name).
- **STUN:** an RFC 5389 binding request, sent from the NLTP socket itself, so the discovered mapping is the one data will use. Requests are retransmitted every 0.5 s; the Python side now does the same.
- **Core keepalive:** `nltp_send_ping` / PONG counting, and `ep->keepalive_ms` sends a PING after that much idle time, keeping NAT mappings open.
- **`ep->valid_rx`:** apps follow a peer's new address only on authenticated frames.

`csrc/tools/nltp_cli.c` gained `--stun`, `--punch`, `--relay/--room`, `--keepalive-ms` and `--count/--idle-ms`:

- **Automatic relay fallback:** if punching fails within 3 s, both sides continue through the relay.
- **Low-TTL pre-punch** (`--punch-ttl`, default 2; also in Python `punch_p2p`): the first pings open our own NAT mapping but expire before the peer's NAT. This avoids a Linux conntrack port clash, where the peer's NAT re-maps its port because our early packet arrived first. That clash made 9 of 9 direct punches fail before this fix.

### Virtual internet test

`scripts/virtual_nat_test.py` builds a small internet out of Linux network namespaces:

```
hostA 10.0.1.2 -- [NAT A] 198.51.100.2 --+
                                         [core router] -- 203.0.113.10 (STUN + relay hub)
hostB 10.0.2.2 -- [NAT B] 192.0.2.2 -----+
```

- Each last mile has **40 ms delay and 5% loss in each direction**; files are 1 MB.
- Symmetric NAT is MASQUERADE with `--random-fully`.
- NAT expiry is a 4 s UDP conntrack timeout.

**Result: 18/18 checks passed** (`results/virtual_nat_test.json`).

| Test | Result |
|---|---|
| STUN from behind NAT (C) | correct public address |
| Hole punch + fallback, C → C encrypted, 3 runs | 3/3 direct, 1.31–1.99 s |
| Hole punch + fallback, C → Python encrypted, 3 runs | 3/3 direct, 1.45–2.26 s |
| Hole punch + fallback, Python → C, 3 runs | 3/3 direct, 1.22–1.53 s |
| Relay: C → Python enc, Python → C enc, C → C | 3/3, 1.31–2.19 s |
| Symmetric NAT: punch must fail | fails, as it should |
| Symmetric NAT: punch + automatic relay fallback | works, 1.80 s |
| Symmetric NAT: relay C → Python | works, 1.77 s |
| 4 s NAT expiry, 8 s idle, no keepalive: 2nd file must fail | fails, as it should |
| Same with 1 s keepalives: 2nd file | works, 1.59 s |

**Bugs found and fixed on the way:**
- Before the low-TTL pre-punch, direct punching failed 9/9 (Linux NAT port clash).
- Python STUN sent only one request, so a 5% lossy link sometimes lost it. It now retransmits.
- The C receiver attached its file buffer only after punching. When the sender finished punching first, early generations were "solved" with nowhere to store them, and the SHA check failed (about 1 run in 30). The buffer is now attached first, and the core ignores data until a buffer exists, so the sender repairs it later.

**Regression check after these changes:**
- C self-test: 9/9.
- C/Python interop: 72/72 (3 key modes × 2 loss rates × 3 sizes × 4 pairings).

Moving 1 MB over two 5%-loss, 40 ms last miles took 1.2–2.3 s, direct or relayed. That is the same range as the plain 5%-loss benchmark (1.35 s), so NAT traversal adds the punch time (~0.55 s) and nothing per byte.

### Where NLTP stands now

**Fastest measured:**
- 1 MB-class files over lossy, bursty and satellite links. It beat TCP BBR, production QUIC (ngtcp2), KCP, kcptun, UDT and SRT at 2–20% loss, on bursty loss (about 2.6× faster than the next tool), and on GEO satellite.
- It is the only tool that moved 10 MB over a bursty link reliably.

**Not fastest:**
- Clean links: TCP BBR is about 12% faster at 1 MB.
- Long transfers with random loss: TCP and QUIC are 15–27% faster at 10 MB.
- Short messages: KCP wins most 64 KB cells.
- It always sends more data than TCP or QUIC: 1.25–1.6× the file size against about 1.05–1.3×.

**What makes it usable on real hardware now:**
- One wire format across Python and a no-heap C core, running on an ESP32.
- Optional ChaCha20-Poly1305 encryption.
- NAT traversal (STUN, hole punching, relay fallback, keepalive).
- Verified on emulated radio links up to 21% loss.

**What it is not:** new transport theory. Each part (systematic network coding, delivery-rate pacing, STUN and hole punching, AEAD with a replay window) is known. The contribution is the combination, tuned for small-to-medium transfers on bad radio links, and measured fairly against what exists.

**Next real step:** a field test over the actual boat or buoy radio (`scripts/field_test.py`). Every loss figure here so far is emulated.

## Caveats

- In the first tables QUIC is the Python aioquic library. From Update 5 on, production ngtcp2 (BBR and CUBIC) is included as well.
- Loss is emulated by `netem`, not recorded from real Iridium/LoRa/4G links.
- The Tridel test-unit recording is short (560 readings).
- The NLTP timer starts at client creation (0-RTT), while the TCP timer includes the connection handshake. That is how each protocol actually behaves, but it favours NLTP by one round trip on small files.
