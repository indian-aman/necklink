package io.necklink.nltp;

import java.util.ArrayList;
import java.util.List;

/**
 * Router-safe TokenBucketPacer and 1-RTT BBR-style DeliveryRateController.
 */
public final class Pacing {
    private Pacing() {}

    public static final class TokenBucketPacer {
        private double rateBytesPerS;
        private double maxBurstBytes;
        private double tokens;
        private Double lastRefill;
        private double bonusBytes;

        public TokenBucketPacer(double rateKbps) {
            this(rateKbps, -1);
        }

        public TokenBucketPacer(double rateKbps, int explicitMaxBurstBytes) {
            this.rateBytesPerS = Math.max(1000.0, (rateKbps * 1000.0) / 8.0);
            this.maxBurstBytes = explicitMaxBurstBytes > 0
                    ? Math.max(2048.0, explicitMaxBurstBytes)
                    : autoBurst();
            this.tokens = this.maxBurstBytes;
            this.lastRefill = null;
            this.bonusBytes = 0.0;
        }

        private double autoBurst() {
            return Math.max(4 * 1024, Math.min(16 * 1024, (int) (rateBytesPerS * 0.006)));
        }

        public void grantBurst(double nBytes) {
            this.bonusBytes = Math.max(this.bonusBytes, nBytes);
            this.tokens = 0.0;
        }

        public void setRateKbps(double rateKbps, boolean rescaleBurst) {
            this.rateBytesPerS = Math.max(1000.0, (rateKbps * 1000.0) / 8.0);
            if (rescaleBurst) {
                this.maxBurstBytes = autoBurst();
                this.tokens = Math.min(this.tokens, this.maxBurstBytes);
            }
        }

        public boolean consumeIfAvailable(int nBytes, double nowS) {
            if (bonusBytes >= nBytes) {
                bonusBytes -= nBytes;
                return true;
            }
            if (lastRefill == null) {
                lastRefill = nowS;
            } else {
                double dt = Math.max(0.0, nowS - lastRefill);
                lastRefill = nowS;
                tokens = Math.min(maxBurstBytes, tokens + dt * rateBytesPerS);
            }
            if (tokens >= nBytes) {
                tokens -= nBytes;
                return true;
            }
            return false;
        }
    }

    public static final class DeliveryRateController {
        public static final int STARTUP = 0;
        public static final int CRUISE = 1;
        public static final double INIT_KBPS = 2000.0;
        public static final double MIN_KBPS = 64.0;
        public static final int INIT_BURST_BYTES = 32 * 1024;

        private final double capKbps;
        private double rateKbps;
        private int mode = STARTUP;
        private double minRttMs = Double.POSITIVE_INFINITY;
        private double roundMinRttMs = Double.POSITIVE_INFINITY;
        private int nRttSamples = 0;
        private boolean startupExitPending = false;
        private Integer trackedStream = null;
        private Double roundT0 = null;
        private long roundRx0 = 0L;
        private long sentBytes = 0L;
        private long roundSent0 = 0L;
        private double prevQueueMs = 0.0;
        private double bestDelivKbps = 0.0;
        private int plateauRounds = 0;
        private double randomLoss = 0.0;
        private int randomLossSamples = 0;
        private final List<Double> btlbwRounds = new ArrayList<>();
        private double roundArrivalMax = 0.0;
        private boolean trainProbeDone = false;
        private double srttMs = 0.0;
        private double rttvarMs = 0.0;
        private final List<double[]> sentHistory = new ArrayList<>();
        private int roundsDone = 0;

        public DeliveryRateController(double capKbps) {
            this.capKbps = capKbps;
            this.rateKbps = Math.min(INIT_KBPS, capKbps);
        }

        public double rateKbps() {
            return rateKbps;
        }

        public double btlbwKbps() {
            double m = roundArrivalMax;
            for (double v : btlbwRounds) {
                if (v > m) m = v;
            }
            return m;
        }

        public void startStream(int streamId, double nowS) {
            this.trackedStream = streamId;
            this.trainProbeDone = (mode == CRUISE) && !btlbwRounds.isEmpty();
            this.roundsDone = 0;
            this.roundT0 = nowS;
            this.roundRx0 = 0L;
            this.roundSent0 = sentBytes;
            this.roundMinRttMs = Double.POSITIVE_INFINITY;
        }

        public void onSent(int nBytes) {
            sentBytes += nBytes;
        }

        public void onRttSample(double rttMs) {
            minRttMs = Math.min(minRttMs, rttMs);
            roundMinRttMs = Math.min(roundMinRttMs, rttMs);
            nRttSamples++;
            if (nRttSamples == 1) {
                srttMs = rttMs;
                rttvarMs = rttMs / 4.0;
            } else {
                rttvarMs = 0.75 * rttvarMs + 0.25 * Math.abs(rttMs - srttMs);
                srttMs = 0.875 * srttMs + 0.125 * rttMs;
            }
            if (mode == STARTUP && nRttSamples >= 4) {
                if (rttMs > minRttMs + Math.max(4.0, Math.max(minRttMs / 8.0, 2.0 * rttvarMs))) {
                    startupExitPending = true;
                }
            }
        }

        private double queueTargetMs() {
            if (Double.isInfinite(minRttMs)) return 4.0;
            return Math.max(4.0, Math.max(0.05 * minRttMs, 1.5 * rttvarMs));
        }

        private long sentAt(double t) {
            for (int i = sentHistory.size() - 1; i >= 0; i--) {
                if (sentHistory.get(i)[0] <= t) {
                    return (long) sentHistory.get(i)[1];
                }
            }
            return sentHistory.isEmpty() ? 0L : (long) sentHistory.get(0)[1];
        }

        public int initialBurst(int coldBytes) {
            return mode == STARTUP ? coldBytes : 0;
        }

        public double lossForFec(double fallback) {
            return randomLossSamples >= 2 ? randomLoss : fallback;
        }

        private double capacityKbps(double delivKbps) {
            return delivKbps / Math.max(0.3, 1.0 - randomLoss);
        }

        public boolean onAck(double nowS, int streamId, long rxBytesU32, double epSrttMs, double arrivalKbps) {
            if (trackedStream == null || trackedStream != streamId || roundT0 == null) {
                return false;
            }
            sentHistory.add(new double[]{nowS, sentBytes});
            if (sentHistory.size() > 512) {
                sentHistory.subList(0, 256).clear();
            }

            if (arrivalKbps > 0.0) {
                roundArrivalMax = Math.max(roundArrivalMax, arrivalKbps);
                if (mode == STARTUP && !trainProbeDone) {
                    trainProbeDone = true;
                    mode = CRUISE;
                    double old = rateKbps;
                    rateKbps = Math.max(MIN_KBPS, Math.min(capKbps, Math.max(rateKbps, 1.05 * arrivalKbps)));
                    return rateKbps != old;
                }
            }

            double baseRtt = Double.isFinite(minRttMs) ? minRttMs : epSrttMs;
            double dt = nowS - roundT0;
            boolean earlyExit = (mode == STARTUP) && startupExitPending && dt >= 0.005;
            if (dt < Math.max(0.02, baseRtt / 1000.0) && !earlyExit) {
                return false;
            }

            long delivered = (rxBytesU32 - roundRx0) & 0xFFFFFFFFL;
            double delivKbps = (delivered * 8.0) / 1000.0 / dt;
            double queueMs = Double.isFinite(roundMinRttMs) ? (roundMinRttMs - minRttMs) : 0.0;
            double targetMs = queueTargetMs();
            double old = rateKbps;

            double lagS = ((srttMs > 0.0) ? srttMs : baseRtt) / 1000.0;
            long sentWindow = sentAt(nowS - lagS) - sentAt(roundT0 - lagS);
            roundsDone++;
            if (roundsDone > 2 && sentWindow > 8192L && queueMs < targetMs && prevQueueMs < targetMs) {
                double sample = Math.max(0.0, Math.min(0.9, 1.0 - (delivered / (double) sentWindow)));
                randomLoss = (randomLossSamples == 0) ? sample : (0.8 * randomLoss + 0.2 * sample);
                randomLossSamples++;
            }

            if (mode == STARTUP) {
                if (delivKbps >= 1.25 * bestDelivKbps) {
                    bestDelivKbps = delivKbps;
                    plateauRounds = 0;
                } else {
                    plateauRounds++;
                }
                if (earlyExit || queueMs > targetMs) {
                    mode = CRUISE;
                    double cap = capacityKbps(Math.max(delivKbps, bestDelivKbps));
                    rateKbps = cap > 0.0 ? Math.max(0.95 * cap, 0.5 * rateKbps) : 0.5 * rateKbps;
                } else if (plateauRounds >= 3) {
                    mode = CRUISE;
                } else if (delivKbps >= 0.5 * rateKbps) {
                    rateKbps = Math.max(2.0 * rateKbps, 2.0 * delivKbps);
                }
            } else {
                double cap = Math.max(capacityKbps(delivKbps), btlbwKbps());
                if (queueMs > 2.0 * targetMs) {
                    rateKbps = cap > 0.0 ? Math.min(rateKbps * 0.9, 0.95 * cap) : rateKbps * 0.85;
                } else if (queueMs < targetMs) {
                    double ceiling = cap > 0.0 ? 1.25 * cap : rateKbps * 1.10;
                    rateKbps = Math.min(rateKbps * 1.10, Math.max(rateKbps, ceiling));
                }
            }

            rateKbps = Math.max(MIN_KBPS, Math.min(capKbps, rateKbps));
            prevQueueMs = queueMs;
            roundT0 = nowS;
            roundRx0 = rxBytesU32;
            roundSent0 = sentBytes;
            roundMinRttMs = Double.POSITIVE_INFINITY;
            startupExitPending = false;
            if (roundArrivalMax > 0.0) {
                btlbwRounds.add(roundArrivalMax);
                while (btlbwRounds.size() > 6) {
                    btlbwRounds.remove(0);
                }
            }
            roundArrivalMax = 0.0;
            return rateKbps != old;
        }
    }
}
