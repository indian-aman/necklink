package io.necklink.nltp;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BiConsumer;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Encrypted NLTP sessions (wire version 2, NLTP 1.4); same protocol as {@code necklink/session.py}:
 *
 * <pre>
 *   initiator -> responder  HELLO      { n_i (16 B) }              sealed with the long-term key K
 *   responder -> initiator  HELLO_ACK  { n_i (16 B) || n_r (16 B) } sealed with K
 *   prk   = HMAC-SHA256(K,   "NLTP-1.4 session" || n_i || n_r || LE32(cid_i) || LE32(cid_r))
 *   k_i2r = HMAC-SHA256(prk, "i2r"),   k_r2i = HMAC-SHA256(prk, "r2i")
 * </pre>
 *
 * Data frames keep the 20-byte header (version nibble 2) and are sealed with the session key for their
 * direction. Recorded traffic fails after any restart; counter restarts and conn_id collisions are harmless.
 */
final class NLTPSession<P> {
    static final int SESSION_VERSION = 2;
    static final int PT_HELLO = 0x0B;
    static final int PT_HELLO_ACK = 0x0C;
    static final int PT_NO_SESSION = 0x0D;
    static final int NONCE_BYTES = 16;

    private static final double HELLO_FIRST_RETX_S = 0.12;
    private static final double HELLO_MAX_RETX_S = 0.50;
    private static final double HELLO_GIVE_UP_S = 10.0;
    private static final double TAKEOVER_SILENCE_S = 1.0;
    private static final double REKEY_SILENCE_S = 3.0;
    private static final double HINT_INTERVAL_S = 0.25;
    private static final int MAX_SESSIONS_PER_CID = 2;
    private static final int MAX_CIDS = 4096;
    private static final int MAX_QUEUED = 64;
    private static final byte[] KDF_LABEL = "NLTP-1.4 session".getBytes(StandardCharsets.US_ASCII);

    /** Result of {@link #open} for a handshake frame that was handled (nothing to deliver). */
    static final NLTPWire.Frame HANDLED = new NLTPWire.Frame(0, 0, 0, 0, new byte[0]);

    private static final class Queued {
        final int pktType, flags, tsMs;
        final byte[] payload;
        Queued(int pktType, byte[] payload, int flags, int tsMs) {
            this.pktType = pktType; this.payload = payload; this.flags = flags; this.tsMs = tsMs;
        }
    }

    private static final class Session<P> {
        final char role;
        P peer;
        int peerCid;
        final byte[] nI;
        byte[] nR;
        byte[] kTx, kRx;
        long txSeq = 0L;
        final NLTPWire.ReplayWindow64 replay = new NLTPWire.ReplayWindow64();
        double estS, lastRxS, helloNextS, helloInterval = HELLO_FIRST_RETX_S, deadlineS;
        byte[] ackFrame;
        List<Queued> queued = new ArrayList<>();

        Session(char role, P peer, byte[] nI, double now) {
            this.role = role; this.peer = peer; this.nI = nI;
            this.estS = now; this.lastRxS = now; this.helloNextS = now; this.deadlineS = now + HELLO_GIVE_UP_S;
        }

        void setKeys(byte[][] k, double now) {
            if (role == 'i') { kTx = k[0]; kRx = k[1]; } else { kTx = k[1]; kRx = k[0]; }
            estS = now; lastRxS = now;
        }
    }

    private final byte[] kLong;
    private final int myCid;
    private final BiConsumer<byte[], P> sendRaw;
    private final Map<P, Session<P>> byAddr = new HashMap<>();
    private final LinkedHashMap<Integer, List<Session<P>>> byCid = new LinkedHashMap<>();
    private final Map<P, Session<P>> pending = new HashMap<>();
    private final Map<P, Double> hintTx = new HashMap<>();
    private final Map<P, Double> hintRx = new HashMap<>();
    long hellosTx, acksTx, established, legacyDrops, hintsTx, rekeysFromHint;

    NLTPSession(String psk, int myCid, BiConsumer<byte[], P> sendRaw) {
        this.kLong = NLTPWire.deriveAeadKey(psk);
        this.myCid = myCid;
        this.sendRaw = sendRaw;
    }

    // ------------------------------------------------------------------ crypto helpers
    static byte[] hmacSha256(byte[] key, byte[]... parts) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            for (byte[] p : parts) mac.update(p);
            return mac.doFinal();
        } catch (Exception e) {
            throw new IllegalStateException("HmacSHA256 unavailable", e);
        }
    }

    /** Returns {k_i2r, k_r2i}. */
    static byte[][] deriveSessionKeys(byte[] kLong, byte[] nI, byte[] nR, int cidI, int cidR) {
        byte[] cids = new byte[8];
        NLTPWire.writeU32LE(cids, 0, cidI);
        NLTPWire.writeU32LE(cids, 4, cidR);
        byte[] prk = hmacSha256(kLong, KDF_LABEL, nI, nR, cids);
        return new byte[][] {
            hmacSha256(prk, "i2r".getBytes(StandardCharsets.US_ASCII)),
            hmacSha256(prk, "r2i".getBytes(StandardCharsets.US_ASCII)),
        };
    }

    private static byte[] nonce(int cid, long seq) {
        byte[] n = new byte[12];
        NLTPWire.writeU32LE(n, 0, cid);
        NLTPWire.writeU64LE(n, 4, seq);
        return n;
    }

    private static long randomSeq() {
        return NLTPWire.readU64LE(NLTPWire.randomBytes(8), 0) | 1L;
    }

    private static boolean lessUnsigned(byte[] a, byte[] b) {
        for (int i = 0; i < a.length; i++) {
            int x = a[i] & 0xFF, y = b[i] & 0xFF;
            if (x != y) return x < y;
        }
        return false;
    }

    private byte[] header(int pktType, int flags, long seq, int tsMs) {
        byte[] h = new byte[NLTPWire.ENC_HEADER_SIZE];
        h[0] = NLTPWire.NLTP_MAGIC_0;
        h[1] = NLTPWire.NLTP_MAGIC_1;
        h[2] = (byte) (((SESSION_VERSION & 0x0F) << 4) | (pktType & 0x0F));
        h[3] = (byte) ((flags & 0x7F) | NLTPWire.FLAG_ENCRYPTED);
        NLTPWire.writeU32LE(h, 4, myCid);
        NLTPWire.writeU64LE(h, 8, seq);
        NLTPWire.writeU32LE(h, 16, tsMs);
        return h;
    }

    private byte[] seal(byte[] key, int pktType, int flags, long seq, int tsMs, byte[] payload) {
        byte[] hdr = header(pktType, flags, seq, tsMs);
        byte[] n = nonce(myCid, seq);
        byte[] ct = NLTPWire.chacha20Xor(key, 1, n, payload, 0, payload.length);
        byte[] tag = NLTPWire.poly1305AeadTag(key, n, hdr, 0, hdr.length, ct, 0, ct.length);
        byte[] out = new byte[hdr.length + ct.length + NLTPWire.AEAD_TAG_SIZE];
        System.arraycopy(hdr, 0, out, 0, hdr.length);
        System.arraycopy(ct, 0, out, hdr.length, ct.length);
        System.arraycopy(tag, 0, out, hdr.length + ct.length, NLTPWire.AEAD_TAG_SIZE);
        return out;
    }

    private static byte[] open(byte[] key, byte[] raw, int rawLen, int cid, long seq) {
        int hdr = NLTPWire.ENC_HEADER_SIZE;
        int ctLen = rawLen - hdr - NLTPWire.AEAD_TAG_SIZE;
        if (ctLen < 0) return null;
        byte[] n = nonce(cid, seq);
        byte[] calc = NLTPWire.poly1305AeadTag(key, n, raw, 0, hdr, raw, hdr, ctLen);
        byte[] rx = Arrays.copyOfRange(raw, hdr + ctLen, rawLen);
        if (!MessageDigest.isEqual(calc, rx)) return null;
        return NLTPWire.chacha20Xor(key, 1, n, raw, hdr, ctLen);
    }

    // ------------------------------------------------------------------ TX side
    private void sendHello(Session<P> s, int tsMs) {
        sendRaw.accept(seal(kLong, PT_HELLO, 0, randomSeq(), tsMs, s.nI), s.peer);
        hellosTx++;
    }

    private Session<P> startHandshake(P peer, double now) {
        Session<P> s = new Session<>('i', peer, NLTPWire.randomBytes(NONCE_BYTES), now);
        pending.put(peer, s);
        sendHello(s, (int) (long) (now * 1000.0));
        s.helloNextS = now + s.helloInterval;
        return s;
    }

    /** Frames waiting for a handshake to finish (close() lingers briefly so they still go out). */
    int queuedCount() {
        int n = 0;
        for (Session<P> p : pending.values()) n += p.queued.size();
        return n;
    }

    boolean ready(P peer, double now) {
        if (byAddr.containsKey(peer)) return true;
        if (!pending.containsKey(peer)) startHandshake(peer, now);
        return false;
    }

    /** Sealed frame for {@code peer}, or null while a handshake is running (the frame is queued). */
    byte[] seal(P peer, int pktType, byte[] payload, int flags, int tsMs, double now) {
        Session<P> s = byAddr.get(peer);
        if (s == null) {
            ready(peer, now);
            Session<P> p = pending.get(peer);
            if (p != null && p.queued.size() < MAX_QUEUED) p.queued.add(new Queued(pktType, payload, flags, tsMs));
            return null;
        }
        s.txSeq++;
        return seal(s.kTx, pktType, flags, s.txSeq, tsMs, payload);
    }

    void service(double now, Iterable<P> busyPeers) {
        Iterator<Map.Entry<P, Session<P>>> it = pending.entrySet().iterator();
        while (it.hasNext()) {
            Session<P> s = it.next().getValue();
            if (now >= s.deadlineS) {
                it.remove();
            } else if (now >= s.helloNextS) {
                sendHello(s, (int) (long) (now * 1000.0));
                s.helloInterval = Math.min(HELLO_MAX_RETX_S, s.helloInterval * 1.5);
                s.helloNextS = now + s.helloInterval;
            }
        }
        for (P peer : busyPeers) {
            Session<P> s = byAddr.get(peer);
            if (s != null && !pending.containsKey(peer) && now - s.lastRxS > REKEY_SILENCE_S && now - s.estS > REKEY_SILENCE_S) {
                startHandshake(peer, now);
            }
        }
    }

    // ------------------------------------------------------------------ RX side
    private void remember(int cid, Session<P> s, boolean active) {
        List<Session<P>> lst = byCid.get(cid);
        if (lst == null) {
            if (byCid.size() >= MAX_CIDS) {
                Iterator<Integer> k = byCid.keySet().iterator();
                k.next();
                k.remove();
            }
            lst = new ArrayList<>();
            byCid.put(cid, lst);
        }
        lst.remove(s);
        lst.add((active || lst.isEmpty()) ? 0 : 1, s);
        while (lst.size() > MAX_SESSIONS_PER_CID) lst.remove(lst.size() - 1);
    }

    /** Returns the frame, {@link #HANDLED} for handshake frames, or null when rejected. */
    NLTPWire.Frame open(byte[] raw, int rawLen, P peer, double now) {
        int hdrLen = NLTPWire.ENC_HEADER_SIZE;
        if (rawLen < hdrLen + NLTPWire.AEAD_TAG_SIZE || raw[0] != NLTPWire.NLTP_MAGIC_0 || raw[1] != NLTPWire.NLTP_MAGIC_1) return null;
        int verType = raw[2] & 0xFF;
        int flags = raw[3] & 0xFF;
        if ((flags & NLTPWire.FLAG_ENCRYPTED) == 0) return null;
        if ((verType >>> 4) != SESSION_VERSION) {
            legacyDrops++;
            return null;
        }
        int pktType = verType & 0x0F;
        int cid = NLTPWire.readU32LE(raw, 4);
        long seq = NLTPWire.readU64LE(raw, 8);
        int tsMs = NLTPWire.readU32LE(raw, 16);

        if (pktType == PT_NO_SESSION) {
            onNoSession(raw, rawLen, peer, now);
            return HANDLED;
        }
        if (pktType == PT_HELLO || pktType == PT_HELLO_ACK) {
            byte[] pt = open(kLong, raw, rawLen, cid, seq);
            if (pt == null) return null;
            if (pktType == PT_HELLO && pt.length >= NONCE_BYTES) {
                onHello(cid, Arrays.copyOf(pt, NONCE_BYTES), peer, now, tsMs);
                return HANDLED;
            }
            if (pktType == PT_HELLO_ACK && pt.length >= 2 * NONCE_BYTES) {
                onAck(cid, Arrays.copyOf(pt, NONCE_BYTES), Arrays.copyOfRange(pt, NONCE_BYTES, 2 * NONCE_BYTES), peer, now);
                return HANDLED;
            }
            return null;
        }

        List<Session<P>> lst = byCid.get(cid);
        if (lst == null) {
            sendNoSession(cid, seq, peer, now, tsMs);
            return null;
        }
        for (Session<P> s : new ArrayList<>(lst)) {
            if (!s.replay.wouldAccept(seq)) continue;
            byte[] pt = open(s.kRx, raw, rawLen, cid, seq);
            if (pt == null) continue;
            s.replay.accept(seq);
            s.lastRxS = now;
            s.peer = peer;
            remember(cid, s, true);
            if (byAddr.get(peer) != s) byAddr.put(peer, s);
            return new NLTPWire.Frame(pktType, flags & 0x7F, cid, tsMs, pt);
        }
        return null;
    }

    private void flush(Session<P> s, List<Queued> queued, P peer) {
        for (Queued q : queued) {
            s.txSeq++;
            sendRaw.accept(seal(s.kTx, q.pktType, q.flags, s.txSeq, q.tsMs, q.payload), peer);
        }
    }

    private void onHello(int cid, byte[] nI, P peer, double now, int tsMs) {
        List<Session<P>> lst = byCid.get(cid);
        if (lst != null) {
            for (Session<P> s : lst) {
                if (s.role == 'r' && Arrays.equals(s.nI, nI) && s.ackFrame != null) {
                    sendRaw.accept(s.ackFrame, peer);
                    acksTx++;
                    return;
                }
            }
        }
        Session<P> mine = pending.get(peer);
        if (mine != null) {
            if (lessUnsigned(mine.nI, nI)) return;   // simultaneous open: our HELLO wins
            pending.remove(peer);
        }
        Session<P> s = new Session<>('r', peer, nI, now);
        s.peerCid = cid;
        s.nR = NLTPWire.randomBytes(NONCE_BYTES);
        s.setKeys(deriveSessionKeys(kLong, nI, s.nR, cid, myCid), now);
        remember(cid, s, false);
        Session<P> cur = byAddr.get(peer);
        if (cur == null || now - cur.lastRxS >= TAKEOVER_SILENCE_S) byAddr.put(peer, s);
        byte[] body = new byte[2 * NONCE_BYTES];
        System.arraycopy(nI, 0, body, 0, NONCE_BYTES);
        System.arraycopy(s.nR, 0, body, NONCE_BYTES, NONCE_BYTES);
        s.ackFrame = seal(kLong, PT_HELLO_ACK, 0, randomSeq(), tsMs, body);
        sendRaw.accept(s.ackFrame, peer);
        acksTx++;
        established++;
        if (mine != null && !mine.queued.isEmpty() && byAddr.get(peer) == s) flush(s, mine.queued, peer);
    }

    private void onAck(int cid, byte[] nI, byte[] nR, P peer, double now) {
        Session<P> s = pending.get(peer);
        if (s == null || !Arrays.equals(s.nI, nI)) {
            s = null;
            for (Session<P> p : pending.values()) if (Arrays.equals(p.nI, nI)) s = p;
            if (s == null) return;
        }
        pending.values().remove(s);
        s.peerCid = cid;
        s.nR = nR;
        s.peer = peer;
        s.setKeys(deriveSessionKeys(kLong, nI, nR, myCid, cid), now);
        remember(cid, s, true);
        byAddr.put(peer, s);
        established++;
        List<Queued> q = s.queued;
        s.queued = new ArrayList<>();
        flush(s, q, peer);
    }

    // ------------------------------------------------------------------ restart recovery
    private void sendNoSession(int cid, long seq, P peer, double now, int tsMs) {
        Double last = hintTx.get(peer);
        if (last != null && now - last < HINT_INTERVAL_S) return;
        if (hintTx.size() > MAX_CIDS) hintTx.clear();
        hintTx.put(peer, now);
        byte[] hdr = header(PT_NO_SESSION, 0, 0L, tsMs);
        byte[] out = new byte[hdr.length + 16];
        System.arraycopy(hdr, 0, out, 0, hdr.length);
        NLTPWire.writeU32LE(out, hdr.length, cid);
        NLTPWire.writeU64LE(out, hdr.length + 4, seq);
        sendRaw.accept(out, peer);
        hintsTx++;
    }

    private void onNoSession(byte[] raw, int rawLen, P peer, double now) {
        int off = NLTPWire.ENC_HEADER_SIZE;
        if (rawLen < off + 12) return;
        int echoCid = NLTPWire.readU32LE(raw, off);
        long echoSeq = NLTPWire.readU64LE(raw, off + 4);
        Session<P> s = byAddr.get(peer);
        if (s == null || echoCid != myCid || echoSeq == 0L || Long.compareUnsigned(echoSeq, s.txSeq) > 0 || pending.containsKey(peer)) return;
        Double last = hintRx.get(peer);
        if (last != null && now - last < HINT_INTERVAL_S) return;
        hintRx.put(peer, now);
        rekeysFromHint++;
        startHandshake(peer, now);
    }
}
