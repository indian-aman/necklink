package io.necklink.nltp;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.function.BiConsumer;
import java.util.function.BiFunction;

/**
 * Transport-agnostic NLTP v1.2 protocol engine ({@code NLTPEndpoint<P>}).
 *
 * @param <P> Peer address type (e.g., {@code InetSocketAddress} for UDP or {@code String} for custom links).
 */
public final class NLTPEndpoint<P> {
    public static final class OutboundTransfer<P> {
        public final int streamId;
        public P peerAddr;
        public Integer peerConnId;
        public final String kind;
        public final String filename;
        public final int totalBytes;
        public final int wirePayloadBytes;
        public final boolean compressed;
        public final String sha256Hex;
        final List<TxGeneration> generations;
        final byte[] metaPayload;
        public int baseUnackedGen = 0;
        public boolean metaAcked = false;
        public volatile boolean completed = false;
        public volatile boolean verifiedSha256ByPeer = false;
        public final double startedAt;
        public volatile Double completedAt = null;
        double lastMetaTx = 0.0;
        final Map<Integer, Integer> pendingDofRequests = new LinkedHashMap<>();
        public final CompletableFuture<Boolean> doneFuture = new CompletableFuture<>();

        OutboundTransfer(
                int streamId,
                P peerAddr,
                Integer peerConnId,
                String kind,
                String filename,
                int totalBytes,
                String sha256Hex,
                List<TxGeneration> generations,
                byte[] metaPayload,
                double startedAt
        ) {
            this(streamId, peerAddr, peerConnId, kind, filename, totalBytes, totalBytes, false, sha256Hex, generations, metaPayload, startedAt);
        }

        OutboundTransfer(
                int streamId,
                P peerAddr,
                Integer peerConnId,
                String kind,
                String filename,
                int totalBytes,
                int wirePayloadBytes,
                boolean compressed,
                String sha256Hex,
                List<TxGeneration> generations,
                byte[] metaPayload,
                double startedAt
        ) {
            this.streamId = streamId;
            this.peerAddr = peerAddr;
            this.peerConnId = peerConnId;
            this.kind = kind;
            this.filename = filename;
            this.totalBytes = totalBytes;
            this.wirePayloadBytes = wirePayloadBytes;
            this.compressed = compressed;
            this.sha256Hex = sha256Hex;
            this.generations = generations;
            this.metaPayload = metaPayload;
            this.startedAt = startedAt;
        }
    }

    static final class TxGeneration {
        final int streamId;
        final int genId;
        final int kShards;
        final int rowWidth;
        final byte[] src;
        final int off;
        final int len;
        final int shardBytes;
        byte[][] dataMat = null;

        int nextSysIdx = 0;
        boolean proactiveDone = false;
        int proactiveTarget = -1;
        int proactiveSent = 0;
        int nextParityOffset = 0;
        boolean ackedComplete = false;
        double lastTxTime = 0.0;
        int repairInFlight = 0;
        double lastRepairTime = 0.0;

        TxGeneration(int streamId, int genId, byte[] src, int off, int len, int shardBytes) {
            this.streamId = streamId;
            this.genId = genId;
            this.src = src;
            this.off = off;
            this.len = len;
            this.shardBytes = shardBytes;
            int chunksCount = (len == 0) ? 1 : ((len + shardBytes - 1) / shardBytes);
            this.kShards = chunksCount;
            int maxChunk = 0;
            for (int i = 0; i < chunksCount; i++) {
                int cOff = off + i * shardBytes;
                int cLen = (len == 0) ? 0 : Math.min(shardBytes, Math.max(0, (off + len) - cOff));
                if (cLen > maxChunk) maxChunk = cLen;
            }
            this.rowWidth = 2 + maxChunk;
        }

        private byte[][] ensureDataMat() {
            if (dataMat == null) {
                dataMat = new byte[kShards][rowWidth];
                for (int i = 0; i < kShards; i++) {
                    int cOff = off + i * shardBytes;
                    int cLen = (len == 0) ? 0 : Math.min(shardBytes, Math.max(0, (off + len) - cOff));
                    NLTPWire.writeU16LE(dataMat[i], 0, cLen);
                    if (cLen > 0) {
                        System.arraycopy(src, cOff, dataMat[i], 2, cLen);
                    }
                }
            }
            return dataMat;
        }

        private byte[] packShard(int shardIdx, byte[] rowBytes) {
            byte[] out = new byte[NLTPWire.SHARD_HDR_SIZE + rowBytes.length];
            NLTPWire.writeU32LE(out, 0, streamId);
            NLTPWire.writeU32LE(out, 4, genId);
            out[8] = (byte) (kShards & 0xFF);
            out[9] = (byte) (shardIdx & 0xFF);
            NLTPWire.writeU16LE(out, 10, rowWidth);
            System.arraycopy(rowBytes, 0, out, NLTPWire.SHARD_HDR_SIZE, rowBytes.length);
            return out;
        }

        byte[] makeSystematicPayload(int sysIdx) {
            if (dataMat != null) {
                return packShard(sysIdx, dataMat[sysIdx]);
            }
            byte[] out = new byte[NLTPWire.SHARD_HDR_SIZE + rowWidth];
            NLTPWire.writeU32LE(out, 0, streamId);
            NLTPWire.writeU32LE(out, 4, genId);
            out[8] = (byte) (kShards & 0xFF);
            out[9] = (byte) (sysIdx & 0xFF);
            NLTPWire.writeU16LE(out, 10, rowWidth);
            int cOff = off + sysIdx * shardBytes;
            int cLen = (len == 0) ? 0 : Math.min(shardBytes, Math.max(0, (off + len) - cOff));
            NLTPWire.writeU16LE(out, NLTPWire.SHARD_HDR_SIZE, cLen);
            if (cLen > 0) {
                System.arraycopy(src, cOff, out, NLTPWire.SHARD_HDR_SIZE + 2, cLen);
            }
            return out;
        }

        byte[] makeCrypticParityPayload() {
            byte[][] mat = ensureDataMat();
            int pOff = nextParityOffset;
            nextParityOffset = (nextParityOffset + 1) % 192;
            int shardIdx = kShards + pOff;
            if (kShards == 1) {
                return packShard(shardIdx, mat[0]);
            }
            byte[] coeff = Galois256.cauchyRow(pOff, kShards);
            byte[] coded = new byte[rowWidth];
            for (int c = 0; c < kShards; c++) {
                Galois256.axpy(coded, mat[c], coeff[c] & 0xFF);
            }
            return packShard(shardIdx, coded);
        }
    }

    static final class RxGeneration {
        final int kShards;
        final int rowWidth;
        final Set<Integer> seenIndices = new HashSet<>();
        boolean sysOnly = true;
        final byte[][] sysRows;
        int sysCount = 0;
        final List<byte[]> rowsCoeff = new ArrayList<>();
        final List<byte[]> rowsData = new ArrayList<>();
        byte[] solvedBytes = null;

        RxGeneration(int kShards, int rowWidth) {
            this.kShards = kShards;
            this.rowWidth = rowWidth;
            this.sysRows = new byte[kShards][];
        }

        int haveShards() {
            if (solvedBytes != null) return kShards;
            return sysOnly ? sysCount : rowsCoeff.size();
        }

        int neededDof() {
            if (solvedBytes != null) return 0;
            return Math.max(0, kShards - haveShards());
        }

        boolean addShard(int shardIdx, byte[] shardBuf) {
            if (solvedBytes != null || shardBuf.length != rowWidth) return false;
            if (!seenIndices.add(shardIdx)) return false;

            if (sysOnly && shardIdx < kShards) {
                sysRows[shardIdx] = Arrays.copyOf(shardBuf, shardBuf.length);
                sysCount++;
                if (sysCount >= kShards) {
                    return finishSystematic();
                }
                return false;
            }

            if (sysOnly) {
                sysOnly = false;
                for (int i = 0; i < kShards; i++) {
                    if (sysRows[i] != null) {
                        byte[] c = new byte[kShards];
                        c[i] = 1;
                        rowsCoeff.add(c);
                        rowsData.add(sysRows[i]);
                        sysRows[i] = null;
                    }
                }
            }

            byte[] coeff;
            if (shardIdx < kShards) {
                coeff = new byte[kShards];
                coeff[shardIdx] = 1;
            } else {
                coeff = Galois256.cauchyRow(shardIdx - kShards, kShards);
            }
            rowsCoeff.add(coeff);
            rowsData.add(Arrays.copyOf(shardBuf, shardBuf.length));

            if (rowsCoeff.size() >= kShards) {
                return trySolve();
            }
            return false;
        }

        private boolean finishSystematic() {
            int total = 0;
            int[] lens = new int[kShards];
            for (int i = 0; i < kShards; i++) {
                byte[] row = sysRows[i];
                if (row == null) return false;
                int actualLen = NLTPWire.readU16LE(row, 0);
                if (actualLen > rowWidth - 2) return false;
                lens[i] = actualLen;
                total += actualLen;
            }
            byte[] out = new byte[total];
            int pos = 0;
            for (int i = 0; i < kShards; i++) {
                System.arraycopy(sysRows[i], 2, out, pos, lens[i]);
                pos += lens[i];
                sysRows[i] = null;
            }
            solvedBytes = out;
            return true;
        }

        private boolean trySolve() {
            if (kShards == 1) {
                byte[] rawRow = rowsData.get(0);
                int actualLen = NLTPWire.readU16LE(rawRow, 0);
                if (actualLen > rowWidth - 2) return false;
                solvedBytes = Arrays.copyOfRange(rawRow, 2, 2 + actualLen);
                rowsCoeff.clear();
                rowsData.clear();
                return true;
            }

            byte[][] cArr = new byte[kShards][];
            byte[][] dArr = new byte[kShards][];
            for (int i = 0; i < kShards; i++) {
                cArr[i] = rowsCoeff.get(i);
                dArr[i] = rowsData.get(i);
            }
            byte[][] solved = Galois256.solveLinearSystem(cArr, dArr, kShards);
            if (solved == null) {
                rowsCoeff.remove(0);
                rowsData.remove(0);
                return false;
            }

            int total = 0;
            int[] lens = new int[kShards];
            for (int i = 0; i < kShards; i++) {
                int actualLen = NLTPWire.readU16LE(solved[i], 0);
                if (actualLen > rowWidth - 2) return false;
                lens[i] = actualLen;
                total += actualLen;
            }
            byte[] out = new byte[total];
            int pos = 0;
            for (int i = 0; i < kShards; i++) {
                System.arraycopy(solved[i], 2, out, pos, lens[i]);
                pos += lens[i];
            }
            solvedBytes = out;
            rowsCoeff.clear();
            rowsData.clear();
            return true;
        }
    }

    static final class InboundStreamState {
        final int streamId;
        String kind = null;
        String filename = "";
        int totalBytes = 0;
        int totalGens = 0;
        String expectedSha256 = "";
        boolean isLz4 = false;
        int rawBytes = -1;
        boolean codecInvalid = false;
        boolean metaReceived = false;
        final Map<Integer, RxGeneration> gens = new HashMap<>();
        final Map<Integer, byte[]> solvedGens = new HashMap<>();
        int baseGen = 0;
        boolean completed = false;
        boolean sha256Ok = false;
        int shardsRcvdCount = 0;
        long bytesRcvd = 0L;
        double arrivalKbps = 0.0;
        Double arrT0 = null;
        long arrB0 = 0L;
        int arrN0 = 0;
        int sysShardsRcvd = 0;
        final Map<Integer, Integer> genKMap = new HashMap<>();
        final Map<Integer, Integer> genMaxSysSeen = new HashMap<>();
        int maxGenSeen = -1;
        int maxSysOrd = -1;
        int reorderWnd = 0;

        InboundStreamState(int streamId) {
            this.streamId = streamId;
        }

        void ingestMeta(String kind, String filename, int totalBytes, int totalGens, String sha256) {
            ingestMeta(kind, filename, totalBytes, totalGens, sha256, "", -1L);
        }

        void ingestMeta(String kind, String filename, int totalBytes, int totalGens, String sha256, String codec, long rawBytesLong) {
            this.kind = kind;
            this.filename = filename;
            this.totalBytes = totalBytes;
            this.totalGens = totalGens;
            this.expectedSha256 = sha256;
            if (codec == null || codec.isEmpty() || "none".equals(codec)) {
                this.isLz4 = false;
                this.codecInvalid = false;
            } else if ("lz4".equals(codec)) {
                this.isLz4 = true;
                if (rawBytesLong < 0L || rawBytesLong > NLTPWire.MAX_LZ4_RAW_BYTES) {
                    this.codecInvalid = true;
                } else {
                    this.rawBytes = (int) rawBytesLong;
                    this.codecInvalid = false;
                }
            } else {
                this.codecInvalid = true;
            }
            this.metaReceived = true;
        }

        void advanceBase() {
            while (solvedGens.containsKey(baseGen)) {
                baseGen++;
            }
        }

        byte[] tryFinalize() {
            if (completed || !metaReceived) return null;
            if (codecInvalid) {
                completed = true;
                sha256Ok = false;
                gens.clear();
                solvedGens.clear();
                return new byte[0];
            }
            if (totalGens == 0) {
                byte[] empty = new byte[0];
                completed = true;
                if (isLz4 && rawBytes != 0) {
                    sha256Ok = false;
                    return empty;
                }
                sha256Ok = NLTPWire.sha256Hex(empty).equals(expectedSha256);
                return empty;
            }
            if (solvedGens.size() < totalGens) return null;

            int rawLen = 0;
            for (int g = 0; g < totalGens; g++) {
                byte[] b = solvedGens.get(g);
                if (b == null) return null;
                rawLen += b.length;
            }
            byte[] cat = new byte[rawLen];
            int pos = 0;
            for (int g = 0; g < totalGens; g++) {
                byte[] b = solvedGens.get(g);
                System.arraycopy(b, 0, cat, pos, b.length);
                pos += b.length;
            }
            byte[] wireFull = (cat.length == totalBytes) ? cat : Arrays.copyOf(cat, totalBytes);
            byte[] full = wireFull;
            if (isLz4) {
                byte[] dec = NLTPWire.lz4Decompress(wireFull, rawBytes);
                if (dec == null) {
                    completed = true;
                    sha256Ok = false;
                    gens.clear();
                    solvedGens.clear();
                    return new byte[0];
                }
                full = dec;
            }
            String digest = NLTPWire.sha256Hex(full);
            completed = true;
            sha256Ok = digest.equals(expectedSha256);
            gens.clear();
            solvedGens.clear();
            return full;
        }
    }

    private static final class PendingAck {
        final int peerCid;
        final int sid;
        final int echoTsMs;
        final boolean forceAck;

        PendingAck(int peerCid, int sid, int echoTsMs, boolean forceAck) {
            this.peerCid = peerCid;
            this.sid = sid;
            this.echoTsMs = echoTsMs;
            this.forceAck = forceAck;
        }
    }

    private final int connId;
    private final BiConsumer<byte[], P> sendPacketFn;
    private final BiFunction<String, byte[], Path> onFileSaveFn;
    private final int windowGenerations;
    private final int shardBytes;
    private final int genK;
    private final NLTPWire.AEADCipherContext aead;
    /** Encrypted mode (NLTP 1.4): per-session keys from a 1-RTT handshake ({@link NLTPSession}). */
    private final NLTPSession<P> sessions;
    private final Pacing.TokenBucketPacer pacer;
    private final Pacing.DeliveryRateController rateCtl;
    private final int initialBurstBytes;
    private final double keepaliveS;

    private final Map<P, Double> keepaliveTargets = new LinkedHashMap<>();
    private int nextStreamId = 1;
    private int reflexSeq = 0;
    // Reliable reflex lane (v1.3.2): retransmit until PT_REFLEX_ACK; receivers de-duplicate by (connId, seq)
    private static final double REFLEX_ACK_TIMEOUT_S = 5.0;
    private static final double REFLEX_RETX_MIN_S = 0.015;
    private static final double REFLEX_RETX_FIRST_MAX_S = 0.06;
    private static final double REFLEX_RETX_MAX_S = 0.25;
    private static final int REFLEX_SEEN_MAX = 8192;
    private final Set<String> seenReflexIds = new LinkedHashSet<>();
    private final Map<Integer, PendingReflex<P>> reflexPending = new LinkedHashMap<>();
    private final Set<Integer> reflexAckedSeqs = new LinkedHashSet<>();
    private long reflexRetransmits = 0;
    private long reflexAcksRx = 0;
    private long reflexExpired = 0;

    private static final class PendingReflex<P> {
        final P peer;
        final byte[] payload;
        double nextS;
        double intervalS;
        final double deadlineS;

        PendingReflex(P peer, byte[] payload, double nextS, double intervalS, double deadlineS) {
            this.peer = peer;
            this.payload = payload;
            this.nextS = nextS;
            this.intervalS = intervalS;
            this.deadlineS = deadlineS;
        }
    }

    private final Map<Integer, P> connRoutes = new HashMap<>();
    private final Map<Integer, OutboundTransfer<P>> outbound = new LinkedHashMap<>();
    private final Map<String, InboundStreamState> inbound = new HashMap<>();
    private final BlockingQueue<ReceivedItem<P>> completedInbox = new LinkedBlockingQueue<>();

    private double srttMs = 150.0;
    private boolean hasRttSample = false;
    private double minRttMs = 999999.0;
    private double peerLossEma = 0.0;
    private long wireBytesTx = 0L;
    private long wireBytesRx = 0L;
    private long authDropsRx = 0L;
    private int roamEvents = 0;
    private long pktsTx = 0L;
    private long pktsRx = 0L;
    private int pongsRx = 0;
    private final Set<P> pongPeers = new HashSet<>();
    private final Map<String, Double> lastAckEmit = new HashMap<>();
    private final Map<String, PendingAck> pendingAcks = new LinkedHashMap<>();

    public NLTPEndpoint(
            Integer explicitConnId,
            BiConsumer<byte[], P> sendPacketFn,
            BiFunction<String, byte[], Path> onFileSaveFn,
            int windowGenerations,
            int shardBytes,
            int genK,
            String psk,
            Double maxTxKbps,
            double keepaliveS,
            boolean adaptiveRate,
            Integer initialBurstBytes
    ) {
        this.sendPacketFn = Objects.requireNonNull(sendPacketFn, "sendPacketFn");
        this.onFileSaveFn = onFileSaveFn;
        byte[] r4 = NLTPWire.randomBytes(4);
        this.connId = (explicitConnId != null) ? explicitConnId : (NLTPWire.readU32LE(r4, 0) | 1);
        this.windowGenerations = windowGenerations;
        this.shardBytes = shardBytes;
        this.genK = genK;
        this.aead = (psk != null && !psk.isEmpty()) ? new NLTPWire.AEADCipherContext(psk) : null;
        this.sessions = (aead != null) ? new NLTPSession<>(psk, connId, this::sendHandshake) : null;

        if (maxTxKbps != null && maxTxKbps > 0.0) {
            this.pacer = new Pacing.TokenBucketPacer(maxTxKbps);
            if (adaptiveRate) {
                this.rateCtl = new Pacing.DeliveryRateController(maxTxKbps);
                this.pacer.setRateKbps(this.rateCtl.rateKbps(), true);
            } else {
                this.rateCtl = null;
            }
        } else {
            this.pacer = null;
            this.rateCtl = null;
        }
        this.initialBurstBytes = (initialBurstBytes != null && initialBurstBytes > 0)
                ? initialBurstBytes
                : Pacing.DeliveryRateController.INIT_BURST_BYTES;
        this.keepaliveS = keepaliveS;
    }

    public static double monoNowS() {
        return System.nanoTime() / 1_000_000_000.0;
    }

    public int connId() { return connId; }
    public boolean isEncrypted() { return aead != null; }
    public double srttMs() { return srttMs; }
    public double peerLossEma() { return peerLossEma; }
    public double keepaliveS() { return keepaliveS; }
    public long wireBytesTx() { return wireBytesTx; }
    public long wireBytesRx() { return wireBytesRx; }
    public long authDropsRx() { return authDropsRx; }
    public int roamEvents() { return roamEvents; }
    public int pongsRx() { return pongsRx; }
    public boolean hasPongFrom(P peer) { return pongPeers.contains(peer); }
    public BlockingQueue<ReceivedItem<P>> inboxQueue() { return completedInbox; }
    public Map<Integer, OutboundTransfer<P>> outboundMap() { return outbound; }

    public void registerKeepalivePeer(P peer) {
        keepaliveTargets.putIfAbsent(peer, 0.0);
    }

    private void sendHandshake(byte[] raw, P peer) {
        wireBytesTx += raw.length;
        pktsTx++;
        sendPacketFn.accept(raw, peer);
    }

    /** Frames waiting for a session handshake to finish (encrypted mode). */
    public int sessionQueuedFrames() {
        return sessions == null ? 0 : sessions.queuedCount();
    }

    /** Handshake counters (encrypted mode): {hellos sent, sessions established, re-keys after a NO_SESSION hint}. */
    public long[] sessionStats() {
        return sessions == null ? new long[3] : new long[] {sessions.hellosTx, sessions.established, sessions.rekeysFromHint};
    }

    private void emit(int pktType, P peer, byte[] payload, int flags, double tRef) {
        int tsMs = (int) ((long) (tRef * 1000.0));
        byte[] raw;
        if (sessions != null) {
            raw = sessions.seal(peer, pktType, payload, flags, tsMs, tRef);
            if (raw == null) return; // handshake running; the frame is queued or retried by its sender
        } else {
            raw = NLTPWire.packFrame(pktType, connId, payload, flags, tsMs, null);
        }
        wireBytesTx += raw.length;
        pktsTx++;
        if (keepaliveTargets.containsKey(peer)) {
            keepaliveTargets.put(peer, tRef);
        }
        sendPacketFn.accept(raw, peer);
    }

    public void sendPing(P peer, double nowS) {
        int tsMs = (int) ((long) (nowS * 1000.0));
        byte[] pl = new byte[4];
        NLTPWire.writeU32LE(pl, 0, tsMs);
        emit(NLTPWire.PT_PING, peer, pl, 0, nowS);
    }

    private boolean canSendBulk(int nBytes, double nowS) {
        if (pacer == null) return true;
        int overhead = (aead != null) ? (NLTPWire.ENC_HEADER_SIZE + NLTPWire.AEAD_TAG_SIZE) : NLTPWire.HEADER_SIZE;
        boolean ok = pacer.consumeIfAvailable(nBytes + overhead, nowS);
        if (ok && rateCtl != null) {
            rateCtl.onSent(nBytes + overhead);
        }
        return ok;
    }

    private double fecLoss() {
        return (rateCtl != null) ? rateCtl.lossForFec(peerLossEma) : peerLossEma;
    }

    private static String safeBasename(String filename) {
        if (filename == null || filename.isEmpty()) return "";
        String norm = filename.replace('\\', '/');
        int idx = norm.lastIndexOf('/');
        return idx >= 0 ? norm.substring(idx + 1) : norm;
    }

    public OutboundTransfer<P> startTransfer(P peer, byte[] data, String kind, String filename, double tNow) {
        return startTransfer(peer, data, kind, filename, false, tNow);
    }

    public OutboundTransfer<P> startTransfer(P peer, byte[] data, String kind, String filename, boolean compress, double tNow) {
        int sid = nextStreamId++;
        String shaHex = NLTPWire.sha256Hex(data);
        byte[] comp = compress ? NLTPWire.lz4Compress(data) : null;
        boolean useLz4 = (comp != null && comp.length < data.length);
        byte[] wireData = useLz4 ? comp : Arrays.copyOf(data, data.length);

        int genSliceLen = shardBytes * genK;
        List<TxGeneration> gens = new ArrayList<>();
        if (wireData.length == 0) {
            gens.add(new TxGeneration(sid, 0, wireData, 0, 0, shardBytes));
        } else {
            int gIdx = 0;
            for (int off = 0; off < wireData.length; off += genSliceLen) {
                int rem = Math.min(genSliceLen, wireData.length - off);
                gens.add(new TxGeneration(sid, gIdx++, wireData, off, rem, shardBytes));
            }
        }

        String cleanName = safeBasename(filename);
        String metaTail = useLz4 ? (",\"codec\":\"lz4\",\"raw_bytes\":" + data.length + "}") : "}";
        String metaJson = "{\"stream_id\":" + sid
                + ",\"kind\":\"" + NLTPWire.jsonEscape(kind) + "\""
                + ",\"filename\":\"" + NLTPWire.jsonEscape(cleanName) + "\""
                + ",\"total_bytes\":" + wireData.length
                + ",\"total_gens\":" + gens.size()
                + ",\"sha256\":\"" + shaHex + "\""
                + metaTail;
        byte[] metaBytes = metaJson.getBytes(StandardCharsets.UTF_8);

        Integer knownCid = null;
        for (Map.Entry<Integer, P> e : connRoutes.entrySet()) {
            if (Objects.equals(e.getValue(), peer)) {
                knownCid = e.getKey();
                break;
            }
        }

        OutboundTransfer<P> xfer = new OutboundTransfer<>(
                sid, peer, knownCid, kind, cleanName, data.length, wireData.length, useLz4, shaHex, gens, metaBytes, tNow
        );

        boolean anyActive = false;
        for (OutboundTransfer<P> existing : outbound.values()) {
            if (!existing.completed) {
                anyActive = true;
                break;
            }
        }
        if (rateCtl != null && pacer != null && !anyActive) {
            int burst = rateCtl.initialBurst(initialBurstBytes);
            if (burst > 0) {
                pacer.grantBurst(Math.min(burst, wireData.length + 2 * shardBytes));
            }
            rateCtl.startStream(sid, tNow);
        }

        outbound.put(sid, xfer);
        registerKeepalivePeer(peer);
        stepTx(tNow);
        return xfer;
    }

    public int sendReflex(P peer, byte[] msgBytes, boolean crypticTwin, double tNow) {
        reflexSeq++;
        byte[] payload = new byte[6 + msgBytes.length];
        NLTPWire.writeU32LE(payload, 0, reflexSeq);
        NLTPWire.writeU16LE(payload, 4, msgBytes.length);
        System.arraycopy(msgBytes, 0, payload, 6, msgBytes.length);
        emit(NLTPWire.PT_REFLEX_MSG, peer, payload, 0x02, tNow);
        if (crypticTwin || peerLossEma > 0.05) {
            emit(NLTPWire.PT_REFLEX_MSG, peer, payload, 0x02, tNow);
        }
        double intervalS = Math.min(REFLEX_RETX_FIRST_MAX_S, Math.max(REFLEX_RETX_MIN_S, 1.25 * srttMs / 1000.0));
        reflexPending.put(reflexSeq, new PendingReflex<>(peer, payload, tNow + intervalS, intervalS, tNow + REFLEX_ACK_TIMEOUT_S));
        return reflexSeq;
    }

    /** True once the receiver acknowledged reflex {@code seq} (peers older than v1.3.2 never acknowledge). */
    public boolean reflexAcked(int seq) {
        return reflexAckedSeqs.contains(seq);
    }

    /** Number of reflexes still being retransmitted while waiting for an acknowledgement. */
    public int reflexPending() {
        return reflexPending.size();
    }

    public long reflexRetransmits() {
        return reflexRetransmits;
    }

    public long reflexAcksRx() {
        return reflexAcksRx;
    }

    public long reflexExpired() {
        return reflexExpired;
    }

    private void serviceReflexes(double tNow) {
        Iterator<Map.Entry<Integer, PendingReflex<P>>> it = reflexPending.entrySet().iterator();
        while (it.hasNext()) {
            PendingReflex<P> r = it.next().getValue();
            if (tNow >= r.deadlineS) {
                it.remove();
                reflexExpired++;
                continue;
            }
            if (tNow >= r.nextS) {
                if (sessions != null && !sessions.ready(r.peer, tNow)) {
                    r.nextS = tNow + r.intervalS;
                    continue;
                }
                emit(NLTPWire.PT_REFLEX_MSG, r.peer, r.payload, 0x02, tNow);
                reflexRetransmits++;
                r.intervalS = Math.min(REFLEX_RETX_MAX_S, r.intervalS * 1.5);
                r.nextS = tNow + r.intervalS;
            }
        }
    }

    private static <T> void trimOldest(Set<T> set, int max) {
        Iterator<T> it = set.iterator();
        while (set.size() > max && it.hasNext()) {
            it.next();
            it.remove();
        }
    }

    private int proactiveShards(OutboundTransfer<P> xfer, TxGeneration gen) {
        double p = fecLoss();
        int n = (p >= 0.03) ? (int) Math.ceil(gen.kShards * p * 1.1) : 0;
        if (p >= 0.005 && rateCtl != null) {
            int genBytes = gen.kShards * shardBytes;
            double bdpBytes = ((rateCtl.rateKbps() * 1000.0) / 8.0) * (srttMs / 1000.0);
            int tailGens = Math.max(1, (int) Math.ceil(bdpBytes / Math.max(1, genBytes)));
            if (gen.genId >= xfer.generations.size() - tailGens) {
                n = Math.max(n + 1, (p >= 0.1) ? 2 : 1);
            }
        }
        if (gen.genId == xfer.generations.size() - 1 && gen.kShards > 1) {
            n = Math.max(n, 1);
        }
        return Math.min(n, Math.max(1, gen.kShards / 2));
    }

    private int effectiveWindowGenerations() {
        int eff = windowGenerations;
        if (hasRttSample && minRttMs < 900000.0) {
            double base = (rateCtl != null && rateCtl.minMeanRttMs() < 900000.0) ? rateCtl.minMeanRttMs() : minRttMs;
            double jitterPad = (rateCtl != null) ? 2.5 * rateCtl.rttvarMs() : 0.0;
            double queueThresh = Math.max(base * 2.0, base + 35.0 + jitterPad);
            if (srttMs > queueThresh) {
                eff = Math.max(3, windowGenerations / 4);
            }
            if (rateCtl != null && rateCtl.mode() == Pacing.DeliveryRateController.CRUISE) {
                double bwKbps = Math.max(rateCtl.rateKbps(), rateCtl.btlbwKbps());
                int genBytes = Math.max(1, shardBytes * genK);
                double bdpBytes = ((bwKbps * 1000.0) / 8.0) * (base / 1000.0);
                int bdpGens = Math.max(4, (int) Math.ceil((2.2 * bdpBytes) / genBytes) + 2);
                eff = Math.min(eff, bdpGens);
            }
        }
        return eff;
    }

    public void stepTx(double tNow) {
        double rtoS = Math.max(0.055, Math.min(2.0, (srttMs / 1000.0) * 1.85));
        double inflightGuardS = Math.max(0.035, (srttMs / 1000.0) * 1.40);

        if (sessions != null) {
            List<P> busy = new ArrayList<>();
            for (OutboundTransfer<P> x : outbound.values()) if (!x.completed) busy.add(x.peerAddr);
            for (PendingReflex<P> r : reflexPending.values()) busy.add(r.peer);
            sessions.service(tNow, busy);
        }

        if (!reflexPending.isEmpty()) {
            serviceReflexes(tNow);
        }

        if (!pendingAcks.isEmpty()) {
            List<Map.Entry<String, PendingAck>> copy = new ArrayList<>(pendingAcks.entrySet());
            pendingAcks.clear();
            for (Map.Entry<String, PendingAck> e : copy) {
                PendingAck pa = e.getValue();
                InboundStreamState st = inbound.get(e.getKey());
                P peerAddr = connRoutes.get(pa.peerCid);
                if (st != null && peerAddr != null && !st.completed) {
                    checkStreamCompleteOrAck(peerAddr, pa.peerCid, st, pa.echoTsMs, tNow, pa.forceAck);
                }
            }
        }

        int effWin = effectiveWindowGenerations();
        boolean pacerExhausted = false;

        for (OutboundTransfer<P> xfer : new ArrayList<>(outbound.values())) {
            if (xfer.completed) continue;
            if (xfer.peerConnId != null && connRoutes.containsKey(xfer.peerConnId)) {
                xfer.peerAddr = connRoutes.get(xfer.peerConnId);
            }
            P peer = xfer.peerAddr;
            if (sessions != null && !sessions.ready(peer, tNow)) continue; // wait for the session handshake

            boolean needFinProbe = (xfer.baseUnackedGen >= xfer.generations.size()) && !xfer.completed;
            if ((!xfer.metaAcked || needFinProbe) && (xfer.lastMetaTx == 0.0 || (tNow - xfer.lastMetaTx) >= rtoS)) {
                emit(NLTPWire.PT_STREAM_META, peer, xfer.metaPayload, 0, tNow);
                xfer.lastMetaTx = tNow;
            }

            if (pacerExhausted) continue;

            if (!xfer.pendingDofRequests.isEmpty()) {
                for (Map.Entry<Integer, Integer> req : new ArrayList<>(xfer.pendingDofRequests.entrySet())) {
                    int gId = req.getKey();
                    int dof = req.getValue();
                    if (gId >= xfer.generations.size() || xfer.generations.get(gId).ackedComplete) {
                        xfer.pendingDofRequests.remove(gId);
                        continue;
                    }
                    TxGeneration gen = xfer.generations.get(gId);
                    if ((tNow - gen.lastRepairTime) >= inflightGuardS) {
                        gen.repairInFlight = 0;
                    }
                    int additionalNeeded = Math.max(0, dof - gen.repairInFlight);
                    if (additionalNeeded <= 0) {
                        xfer.pendingDofRequests.remove(gId);
                        continue;
                    }
                    int extra = (int) Math.ceil(additionalNeeded / Math.max(0.45, 1.0 - fecLoss() * 0.70));
                    int nSend = Math.min(extra, gen.kShards + 2);
                    int sentNow = 0;
                    for (int i = 0; i < nSend; i++) {
                        if (!canSendBulk(NLTPWire.SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
                            pacerExhausted = true;
                            break;
                        }
                        emit(NLTPWire.PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
                        sentNow++;
                    }
                    if (sentNow > 0) {
                        gen.repairInFlight += sentNow;
                        gen.lastRepairTime = tNow;
                        gen.lastTxTime = tNow;
                    }
                    if (sentNow >= additionalNeeded) {
                        xfer.pendingDofRequests.remove(gId);
                    }
                    if (pacerExhausted) break;
                }
            }

            if (pacerExhausted) continue;

            int winEnd = Math.min(xfer.generations.size(), xfer.baseUnackedGen + effWin);
            for (int gId = xfer.baseUnackedGen; gId < winEnd; gId++) {
                TxGeneration gen = xfer.generations.get(gId);
                if (gen.ackedComplete) continue;

                if (gen.nextSysIdx < gen.kShards || !gen.proactiveDone) {
                    while (gen.nextSysIdx < gen.kShards) {
                        if (!canSendBulk(NLTPWire.SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
                            pacerExhausted = true;
                            break;
                        }
                        emit(NLTPWire.PT_DATA_SHARD, peer, gen.makeSystematicPayload(gen.nextSysIdx), 0, tNow);
                        gen.nextSysIdx++;
                    }
                    if (pacerExhausted) break;

                    if (!gen.proactiveDone) {
                        if (gen.proactiveTarget < 0) {
                            gen.proactiveTarget = proactiveShards(xfer, gen);
                        }
                        while (gen.proactiveSent < gen.proactiveTarget) {
                            if (!canSendBulk(NLTPWire.SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
                                pacerExhausted = true;
                                break;
                            }
                            emit(NLTPWire.PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
                            gen.proactiveSent++;
                        }
                        if (!pacerExhausted) {
                            gen.proactiveDone = true;
                            gen.lastTxTime = tNow;
                        } else {
                            break;
                        }
                    }
                } else if ((tNow - gen.lastTxTime) >= rtoS) {
                    int nTail = (peerLossEma < 0.22) ? 1 : 2;
                    int sentTail = 0;
                    for (int i = 0; i < Math.min(nTail, gen.kShards); i++) {
                        if (!canSendBulk(NLTPWire.SHARD_HDR_SIZE + gen.rowWidth, tNow)) {
                            pacerExhausted = true;
                            break;
                        }
                        emit(NLTPWire.PT_CRYPTIC_PARITY, peer, gen.makeCrypticParityPayload(), 0, tNow);
                        sentTail++;
                    }
                    if (sentTail > 0) {
                        gen.repairInFlight = 0;
                        gen.lastTxTime = tNow;
                    }
                    if (pacerExhausted) break;
                }
            }
        }

        if (keepaliveS > 0.0 && !keepaliveTargets.isEmpty()) {
            for (Map.Entry<P, Double> e : new ArrayList<>(keepaliveTargets.entrySet())) {
                if (sessions != null && !sessions.ready(e.getKey(), tNow)) continue;
                if (e.getValue() == 0.0 || (tNow - e.getValue()) >= keepaliveS) {
                    sendPing(e.getKey(), tNow);
                }
            }
        }
    }

    private void updatePeerRoute(int peerConnId, P peer) {
        P prev = connRoutes.get(peerConnId);
        if (prev != null && !Objects.equals(prev, peer)) {
            roamEvents++;
        }
        connRoutes.put(peerConnId, peer);
    }

    private void recordRttSample(double rttMs) {
        double effRtt = Math.max(1.0, rttMs);
        minRttMs = Math.min(minRttMs, effRtt);
        if (rateCtl != null) {
            rateCtl.onRttSample(effRtt);
        }
        if (!hasRttSample) {
            srttMs = effRtt;
            hasRttSample = true;
        } else {
            srttMs = 0.80 * srttMs + 0.20 * effRtt;
        }
    }

    /** An ACK belongs to a transfer if it comes from the receiver's conn_id, or from the transfer's address
     *  under a new conn_id (the receiver restarted; the conn_id pre-filled at start is then stale). */
    private boolean ackFromTransferPeer(OutboundTransfer<P> xfer, int peerConnId, P peer) {
        return xfer.peerConnId == null || xfer.peerConnId == peerConnId || peer.equals(xfer.peerAddr);
    }

    public void onDatagram(byte[] raw, int rawLen, P peer, double tNow) {
        wireBytesRx += rawLen;
        pktsRx++;

        NLTPWire.Frame parsed;
        if (sessions != null) {
            parsed = sessions.open(raw, rawLen, peer, tNow);
            if (parsed == NLTPSession.HANDLED) return;
        } else {
            parsed = NLTPWire.unpackFrame(raw, rawLen, null);
        }
        if (parsed == null) {
            authDropsRx++;
            return;
        }

        int pktType = parsed.pktType;
        int pktFlags = parsed.flags;
        int peerConnId = parsed.connId;
        int pktTsMs = parsed.tsMs;
        byte[] payload = parsed.payload;

        updatePeerRoute(peerConnId, peer);

        if (pktType == NLTPWire.PT_REFLEX_MSG && payload.length >= 6) {
            int rSeq = NLTPWire.readU32LE(payload, 0);
            int mLen = Math.min(NLTPWire.readU16LE(payload, 4), payload.length - 6);
            String key = peerConnId + ":" + Integer.toUnsignedString(rSeq);
            if (seenReflexIds.add(key)) {
                trimOldest(seenReflexIds, REFLEX_SEEN_MAX);
                byte[] msg = Arrays.copyOfRange(payload, 6, 6 + mLen);
                completedInbox.offer(new ReceivedItem<>(peerConnId, peer, 0, "reflex", msg, "", null, "", true));
            }
            // Acknowledge every copy: the sender retransmits until one ACK gets through
            byte[] ack = new byte[4];
            NLTPWire.writeU32LE(ack, 0, rSeq);
            emit(NLTPWire.PT_REFLEX_ACK, peer, ack, 0x02, tNow);
            return;
        }

        if (pktType == NLTPWire.PT_REFLEX_ACK && payload.length >= 4) {
            int aSeq = NLTPWire.readU32LE(payload, 0);
            if (reflexPending.remove(aSeq) != null) {
                reflexAcksRx++;
                reflexAckedSeqs.add(aSeq);
                trimOldest(reflexAckedSeqs, REFLEX_SEEN_MAX);
            }
            return;
        }

        if (pktType == NLTPWire.PT_NEURAL_STATE) {
            return;
        }

        if (pktType == NLTPWire.PT_PING) {
            byte[] echo = new byte[4];
            if (payload.length >= 4) {
                System.arraycopy(payload, 0, echo, 0, 4);
            } else {
                NLTPWire.writeU32LE(echo, 0, pktTsMs);
            }
            emit(NLTPWire.PT_PONG, peer, echo, 0, tNow);
            return;
        }

        if (pktType == NLTPWire.PT_PONG && payload.length >= 4) {
            int origTs = NLTPWire.readU32LE(payload, 0);
            int nowMs = (int) ((long) (tNow * 1000.0));
            long rtt = Integer.toUnsignedLong(nowMs - origTs);
            if (rtt < 30000L) {
                recordRttSample((double) rtt);
            }
            pongsRx++;
            pongPeers.add(peer);
            return;
        }

        if (pktType == NLTPWire.PT_STREAM_META) {
            String js = new String(payload, StandardCharsets.UTF_8);
            long sidLong = NLTPWire.jsonGetLong(js, "stream_id", -1L);
            if (sidLong < 0L) return;
            int sid = (int) sidLong;
            String keyStr = peerConnId + ":" + sid;
            InboundStreamState st = inbound.computeIfAbsent(keyStr, k -> new InboundStreamState(sid));
            if (st.completed) {
                byte[] fin = new byte[5];
                NLTPWire.writeU32LE(fin, 0, sid);
                fin[4] = (byte) (st.sha256Ok ? 1 : 0);
                emit(NLTPWire.PT_STREAM_FIN_ACK, peer, fin, 0, tNow);
                return;
            }
            String kind = NLTPWire.jsonGetString(js, "kind");
            String filename = NLTPWire.jsonGetString(js, "filename");
            int totalBytes = (int) NLTPWire.jsonGetLong(js, "total_bytes", 0L);
            int totalGens = (int) NLTPWire.jsonGetLong(js, "total_gens", 0L);
            String sha = NLTPWire.jsonGetString(js, "sha256");
            String codec = NLTPWire.jsonGetString(js, "codec");
            long rawBytes = NLTPWire.jsonGetLong(js, "raw_bytes", -1L);
            st.ingestMeta(kind.isEmpty() ? "bytes" : kind, filename, totalBytes, totalGens, sha, codec, rawBytes);
            checkStreamCompleteOrAck(peer, peerConnId, st, pktTsMs, tNow, true);
            return;
        }

        if ((pktType == NLTPWire.PT_DATA_SHARD || pktType == NLTPWire.PT_CRYPTIC_PARITY)
                && payload.length >= NLTPWire.SHARD_HDR_SIZE) {
            int sid = NLTPWire.readU32LE(payload, 0);
            int genId = NLTPWire.readU32LE(payload, 4);
            int kShards = payload[8] & 0xFF;
            int shardIdx = payload[9] & 0xFF;
            int rowW = NLTPWire.readU16LE(payload, 10);
            if (kShards == 0 || payload.length < NLTPWire.SHARD_HDR_SIZE + rowW) return;
            byte[] shardBuf = Arrays.copyOfRange(payload, NLTPWire.SHARD_HDR_SIZE, NLTPWire.SHARD_HDR_SIZE + rowW);

            String keyStr = peerConnId + ":" + sid;
            InboundStreamState st = inbound.computeIfAbsent(keyStr, k -> new InboundStreamState(sid));
            if (st.completed) {
                byte[] fin = new byte[5];
                NLTPWire.writeU32LE(fin, 0, sid);
                fin[4] = (byte) (st.sha256Ok ? 1 : 0);
                emit(NLTPWire.PT_STREAM_FIN_ACK, peer, fin, 0, tNow);
                return;
            }

            st.shardsRcvdCount++;
            st.bytesRcvd += payload.length;
            if (st.arrT0 == null) {
                st.arrT0 = tNow;
                st.arrB0 = st.bytesRcvd;
                st.arrN0 = st.shardsRcvdCount;
            } else if ((st.shardsRcvdCount - st.arrN0) >= 16 && (tNow - st.arrT0) >= 0.005) {
                st.arrivalKbps = ((st.bytesRcvd - st.arrB0) * 8.0) / 1000.0 / (tNow - st.arrT0);
                st.arrT0 = tNow;
                st.arrB0 = st.bytesRcvd;
                st.arrN0 = st.shardsRcvdCount;
            }

            st.genKMap.put(genId, kShards);
            if (pktType == NLTPWire.PT_DATA_SHARD && shardIdx < kShards) {
                RxGeneration rxExisting = st.gens.get(genId);
                boolean alreadySeen = st.solvedGens.containsKey(genId)
                        || (rxExisting != null && rxExisting.seenIndices.contains(shardIdx));
                if (!alreadySeen) {
                    st.sysShardsRcvd++;
                    int sysOrd = genId * genK + shardIdx;
                    if (sysOrd < st.maxSysOrd) {
                        st.reorderWnd = Math.min(16, Math.max(st.reorderWnd, st.maxSysOrd - sysOrd));
                    } else {
                        st.maxSysOrd = sysOrd;
                    }
                }
                int prevMax = st.genMaxSysSeen.getOrDefault(genId, -1);
                if (shardIdx > prevMax) st.genMaxSysSeen.put(genId, shardIdx);
            } else if (pktType == NLTPWire.PT_CRYPTIC_PARITY) {
                st.genMaxSysSeen.put(genId, kShards - 1);
            }
            if (genId > st.maxGenSeen) st.maxGenSeen = genId;

            if (!st.solvedGens.containsKey(genId)) {
                RxGeneration rxGen = st.gens.computeIfAbsent(genId, g -> new RxGeneration(kShards, rowW));
                boolean newlySolved = rxGen.addShard(shardIdx, shardBuf);
                if (newlySolved && rxGen.solvedBytes != null) {
                    st.solvedGens.put(genId, rxGen.solvedBytes);
                    st.gens.remove(genId);
                    st.advanceBase();
                    if (st.metaReceived && st.solvedGens.size() >= st.totalGens) {
                        checkStreamCompleteOrAck(peer, peerConnId, st, pktTsMs, tNow, true);
                    } else {
                        pendingAcks.put(keyStr, new PendingAck(peerConnId, sid, pktTsMs, true));
                    }
                    return;
                }
            }

            double lastA = lastAckEmit.getOrDefault(keyStr, 0.0);
            if ((st.shardsRcvdCount % 8 == 0) || (tNow - lastA >= 0.025)) {
                pendingAcks.put(keyStr, new PendingAck(peerConnId, sid, pktTsMs, true));
            }
            return;
        }

        if (pktType == NLTPWire.PT_PROPRIOCEPTIVE_ACK && payload.length >= 14) {
            int sid = NLTPWire.readU32LE(payload, 0);
            int baseGen = NLTPWire.readU32LE(payload, 4);
            int echoTs = NLTPWire.readU32LE(payload, 8);
            int lossQ8 = payload[12] & 0xFF;
            int nRep = payload[13] & 0xFF;

            OutboundTransfer<P> xfer = outbound.get(sid);
            if (xfer == null || xfer.completed) return;
            if (!ackFromTransferPeer(xfer, peerConnId, peer)) return;
            xfer.peerConnId = peerConnId;
            xfer.peerAddr = peer;
            if ((pktFlags & 0x02) != 0) {
                if ((pktFlags & 0x01) != 0) xfer.metaAcked = true;
            } else {
                xfer.metaAcked = true;
            }

            int nowMs = (int) ((long) (tNow * 1000.0));
            long rtt = Integer.toUnsignedLong(nowMs - echoTs);
            if (rtt < 30000L) {
                recordRttSample((double) rtt);
            }
            double repLoss = lossQ8 / 255.0;
            peerLossEma = 0.65 * peerLossEma + 0.35 * repLoss;

            int lim = Math.min(baseGen, xfer.generations.size());
            for (int g = xfer.baseUnackedGen; g < lim; g++) {
                xfer.generations.get(g).ackedComplete = true;
            }
            xfer.baseUnackedGen = Math.max(xfer.baseUnackedGen, baseGen);

            int pos = 14;
            for (int i = 0; i < nRep; i++) {
                if (pos + 4 > payload.length) break;
                int gOff = NLTPWire.readU16LE(payload, pos);
                int neededDof = payload[pos + 3] & 0xFF;
                pos += 4;
                int gAbs = baseGen + gOff;
                if (neededDof > 0 && gAbs < xfer.generations.size() && !xfer.generations.get(gAbs).ackedComplete) {
                    xfer.pendingDofRequests.put(gAbs, neededDof);
                }
            }

            if (rateCtl != null && pacer != null && pos + 4 <= payload.length) {
                long rxBytes = Integer.toUnsignedLong(NLTPWire.readU32LE(payload, pos));
                double arrivalKbps = (pos + 8 <= payload.length)
                        ? (double) Integer.toUnsignedLong(NLTPWire.readU32LE(payload, pos + 4))
                        : 0.0;
                if (rateCtl.onAck(tNow, sid, rxBytes, srttMs, arrivalKbps)) {
                    pacer.setRateKbps(rateCtl.rateKbps(), true);
                }
            }

            stepTx(tNow);
            return;
        }

        if (pktType == NLTPWire.PT_STREAM_FIN_ACK && payload.length >= 5) {
            int sid = NLTPWire.readU32LE(payload, 0);
            int shaOk = payload[4] & 0xFF;
            OutboundTransfer<P> xfer = outbound.get(sid);
            if (xfer != null && !xfer.completed) {
                if (!ackFromTransferPeer(xfer, peerConnId, peer)) return;
                xfer.peerConnId = peerConnId;
                xfer.peerAddr = peer;
                xfer.metaAcked = true;
                xfer.completed = true;
                xfer.verifiedSha256ByPeer = (shaOk == 1);
                xfer.completedAt = tNow;
                for (TxGeneration g : xfer.generations) {
                    g.ackedComplete = true;
                }
                xfer.doneFuture.complete(xfer.verifiedSha256ByPeer);
            }
        }
    }

    private void checkStreamCompleteOrAck(
            P peer,
            int peerConnId,
            InboundStreamState st,
            int echoTsMs,
            double tNow,
            boolean forceAck
    ) {
        byte[] fullBytes = st.tryFinalize();
        if (fullBytes != null) {
            Path savedPath = null;
            if ("file".equals(st.kind) && onFileSaveFn != null) {
                String safeName = safeBasename(st.filename);
                if (safeName.isEmpty()) safeName = "stream_" + st.streamId + ".bin";
                savedPath = onFileSaveFn.apply(safeName, fullBytes);
            }
            completedInbox.offer(new ReceivedItem<>(
                    peerConnId,
                    peer,
                    st.streamId,
                    st.kind != null ? st.kind : "bytes",
                    fullBytes,
                    st.filename,
                    savedPath,
                    NLTPWire.sha256Hex(fullBytes),
                    st.sha256Ok
            ));
            byte[] fin = new byte[5];
            NLTPWire.writeU32LE(fin, 0, st.streamId);
            fin[4] = (byte) (st.sha256Ok ? 1 : 0);
            emit(NLTPWire.PT_STREAM_FIN_ACK, peer, fin, 0, tNow);
            emit(NLTPWire.PT_STREAM_FIN_ACK, peer, fin, 0, tNow);
            return;
        }

        int maxG = st.maxGenSeen;
        boolean isFinalGen = st.metaReceived && (maxG >= st.totalGens - 1);
        int rGuard = isFinalGen ? 0 : Math.min(st.reorderWnd, 6);
        int expectedSys = 0;
        for (int gI = 0; gI < maxG; gI++) {
            expectedSys += st.genKMap.getOrDefault(gI, genK);
        }
        if (maxG >= 0) {
            expectedSys += st.genMaxSysSeen.getOrDefault(maxG, 0) + 1;
        }
        int adjExpected = Math.max(1, Math.max(st.sysShardsRcvd, expectedSys - rGuard));
        double estLoss = Math.max(0.0, Math.min(0.92, 1.0 - (st.sysShardsRcvd / (double) adjExpected)));
        int lossQ8 = (int) Math.round(estLoss * 255.0) & 0xFF;

        int maxGLead = (maxG >= 0) ? (st.genMaxSysSeen.getOrDefault(maxG, -1) + 1) : 0;
        List<int[]> reports = new ArrayList<>();
        for (int gId = st.baseGen; gId <= maxG; gId++) {
            if (st.solvedGens.containsKey(gId)) continue;
            RxGeneration rxG = st.gens.get(gId);
            int have = (rxG != null) ? rxG.haveShards() : 0;
            int need = (rxG != null) ? rxG.neededDof() : genK;
            if (gId < maxG && need > 0) {
                if (rGuard == 0 || gId <= maxG - 2 || maxGLead >= rGuard) {
                    reports.add(new int[]{gId - st.baseGen, have, need});
                }
            } else if (gId == maxG && rxG != null && !rxG.seenIndices.isEmpty()) {
                int maxIdx = 0;
                for (int idx : rxG.seenIndices) {
                    if (idx > maxIdx) maxIdx = idx;
                }
                int maxSeenIdx = Math.min(rxG.kShards, maxIdx + 1);
                int effSeen = Math.max(0, maxSeenIdx - Math.min(rGuard, 4));
                int confirmedMissing = Math.min(need, Math.max(0, effSeen - have));
                if (confirmedMissing > 0) {
                    reports.add(new int[]{gId - st.baseGen, have, confirmedMissing});
                }
            }
            if (reports.size() >= 16) break;
        }

        if (forceAck || !reports.isEmpty()) {
            byte[] ackBuf = new byte[14 + reports.size() * 4 + 8];
            NLTPWire.writeU32LE(ackBuf, 0, st.streamId);
            NLTPWire.writeU32LE(ackBuf, 4, st.baseGen);
            NLTPWire.writeU32LE(ackBuf, 8, echoTsMs);
            ackBuf[12] = (byte) lossQ8;
            ackBuf[13] = (byte) reports.size();

            int pos = 14;
            for (int[] rep : reports) {
                NLTPWire.writeU16LE(ackBuf, pos, rep[0]);
                ackBuf[pos + 2] = (byte) (rep[1] & 0xFF);
                ackBuf[pos + 3] = (byte) (rep[2] & 0xFF);
                pos += 4;
            }
            NLTPWire.writeU32LE(ackBuf, pos, (int) (st.bytesRcvd & 0xFFFFFFFFL));
            long arrClamped = Math.min(0xFFFFFFFFL, Math.max(0L, (long) st.arrivalKbps));
            NLTPWire.writeU32LE(ackBuf, pos + 4, (int) arrClamped);

            emit(NLTPWire.PT_PROPRIOCEPTIVE_ACK, peer, ackBuf, st.metaReceived ? 0x03 : 0x02, tNow);
            lastAckEmit.put(peerConnId + ":" + st.streamId, tNow);
        }
    }
}
