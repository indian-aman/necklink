package io.necklink.nltp;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import java.util.zip.CRC32;

/**
 * NLTP v1.2 Wire Framing, IEEE CRC32, SHA-256, Pure-Java Constant-Time RFC 8439
 * ChaCha20-Poly1305 AEAD, RFC 6479 ReplayWindow64, STUN & CGNAT Relay Framing.
 */
public final class NLTPWire {
    public static final byte NLTP_MAGIC_0 = 0x4E; // 'N'
    public static final byte NLTP_MAGIC_1 = 0x4C; // 'L'
    public static final int NLTP_VERSION = 1;
    public static final int FLAG_ENCRYPTED = 0x80;

    public static final int HEADER_SIZE = 16;
    public static final int ENC_HEADER_SIZE = 20;
    public static final int AEAD_TAG_SIZE = 16;
    public static final int SHARD_HDR_SIZE = 12;

    public static final int DEFAULT_SHARD_BYTES = 960;
    public static final int DEFAULT_GEN_K = 8;

    public static final int PT_DATA_SHARD = 0x01;
    public static final int PT_CRYPTIC_PARITY = 0x02;
    public static final int PT_PROPRIOCEPTIVE_ACK = 0x03;
    public static final int PT_STREAM_META = 0x04;
    public static final int PT_STREAM_FIN_ACK = 0x05;
    public static final int PT_REFLEX_MSG = 0x06;
    public static final int PT_NEURAL_STATE = 0x07;
    public static final int PT_PING = 0x08;
    public static final int PT_PONG = 0x09;
    /** Receiver to sender: u32 reflex seq (reliable reflex lane, v1.3.2). */
    public static final int PT_REFLEX_ACK = 0x0A;

    public static final byte RELAY_MAGIC_0 = 0x4E; // 'N'
    public static final byte RELAY_MAGIC_1 = 0x52; // 'R'
    public static final int RELAY_VER_DATA = 0x01;
    public static final int RELAY_VER_REGISTER = 0x02;
    public static final int RELAY_VER_REG_ACK = 0x03;
    public static final int RELAY_HDR_SIZE = 19;

    public static final int STUN_MAGIC_COOKIE = 0x2112A442;
    public static final int STUN_BINDING_REQUEST = 0x0001;
    public static final int STUN_BINDING_SUCCESS = 0x0101;
    public static final int STUN_ATTR_MAPPED_ADDRESS = 0x0001;
    public static final int STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020;

    private static final SecureRandom RNG = new SecureRandom();
    private static final char[] HEX = "0123456789abcdef".toCharArray();

    private NLTPWire() {}

    public static byte[] randomBytes(int len) {
        byte[] b = new byte[len];
        RNG.nextBytes(b);
        return b;
    }

    // -----------------------------------------------------------------------
    // Little-Endian & Big-Endian Helpers
    // -----------------------------------------------------------------------

    public static int readU16LE(byte[] b, int off) {
        return (b[off] & 0xFF) | ((b[off + 1] & 0xFF) << 8);
    }

    public static void writeU16LE(byte[] b, int off, int v) {
        b[off] = (byte) (v & 0xFF);
        b[off + 1] = (byte) ((v >>> 8) & 0xFF);
    }

    public static int readU32LE(byte[] b, int off) {
        return (b[off] & 0xFF)
                | ((b[off + 1] & 0xFF) << 8)
                | ((b[off + 2] & 0xFF) << 16)
                | ((b[off + 3] & 0xFF) << 24);
    }

    public static void writeU32LE(byte[] b, int off, int v) {
        b[off] = (byte) (v & 0xFF);
        b[off + 1] = (byte) ((v >>> 8) & 0xFF);
        b[off + 2] = (byte) ((v >>> 16) & 0xFF);
        b[off + 3] = (byte) ((v >>> 24) & 0xFF);
    }

    public static long readU64LE(byte[] b, int off) {
        long lo = Integer.toUnsignedLong(readU32LE(b, off));
        long hi = Integer.toUnsignedLong(readU32LE(b, off + 4));
        return lo | (hi << 32);
    }

    public static void writeU64LE(byte[] b, int off, long v) {
        writeU32LE(b, off, (int) v);
        writeU32LE(b, off + 4, (int) (v >>> 32));
    }

    public static int readU16BE(byte[] b, int off) {
        return ((b[off] & 0xFF) << 8) | (b[off + 1] & 0xFF);
    }

    public static void writeU16BE(byte[] b, int off, int v) {
        b[off] = (byte) ((v >>> 8) & 0xFF);
        b[off + 1] = (byte) (v & 0xFF);
    }

    public static int readU32BE(byte[] b, int off) {
        return ((b[off] & 0xFF) << 24)
                | ((b[off + 1] & 0xFF) << 16)
                | ((b[off + 2] & 0xFF) << 8)
                | (b[off + 3] & 0xFF);
    }

    public static void writeU32BE(byte[] b, int off, int v) {
        b[off] = (byte) ((v >>> 24) & 0xFF);
        b[off + 1] = (byte) ((v >>> 16) & 0xFF);
        b[off + 2] = (byte) ((v >>> 8) & 0xFF);
        b[off + 3] = (byte) (v & 0xFF);
    }

    // -----------------------------------------------------------------------
    // CRC32 & SHA-256
    // -----------------------------------------------------------------------

    public static int crc32Ieee(byte[] buf, int off, int len) {
        CRC32 crc = new CRC32();
        crc.update(buf, off, len);
        return (int) crc.getValue();
    }

    public static byte[] sha256Bytes(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return md.digest(data);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    public static String bytesToHex(byte[] bytes) {
        char[] out = new char[bytes.length * 2];
        for (int i = 0; i < bytes.length; i++) {
            int v = bytes[i] & 0xFF;
            out[i * 2] = HEX[v >>> 4];
            out[i * 2 + 1] = HEX[v & 0x0F];
        }
        return new String(out);
    }

    public static String sha256Hex(byte[] data) {
        return bytesToHex(sha256Bytes(data));
    }

    // -----------------------------------------------------------------------
    // Pure-Java Constant-Time RFC 8439 ChaCha20-Poly1305 AEAD
    // (Matches `csrc/nltp_wire.h` 26-bit limb arithmetic cleanly on Java/Android)
    // -----------------------------------------------------------------------

    public static void chacha20Block(byte[] key32, int counter, byte[] nonce12, byte[] out64) {
        int[] st = new int[16];
        int[] x = new int[16];
        st[0] = 0x61707865;
        st[1] = 0x3320646e;
        st[2] = 0x79622d32;
        st[3] = 0x6b206574;
        for (int i = 0; i < 8; i++) {
            st[4 + i] = readU32LE(key32, i * 4);
        }
        st[12] = counter;
        st[13] = readU32LE(nonce12, 0);
        st[14] = readU32LE(nonce12, 4);
        st[15] = readU32LE(nonce12, 8);

        System.arraycopy(st, 0, x, 0, 16);
        for (int i = 0; i < 10; i++) {
            quarterRound(x, 0, 4, 8, 12);
            quarterRound(x, 1, 5, 9, 13);
            quarterRound(x, 2, 6, 10, 14);
            quarterRound(x, 3, 7, 11, 15);
            quarterRound(x, 0, 5, 10, 15);
            quarterRound(x, 1, 6, 11, 12);
            quarterRound(x, 2, 7, 8, 13);
            quarterRound(x, 3, 4, 9, 14);
        }
        for (int i = 0; i < 16; i++) {
            writeU32LE(out64, i * 4, x[i] + st[i]);
        }
    }

    private static void quarterRound(int[] x, int a, int b, int c, int d) {
        x[a] += x[b]; x[d] = Integer.rotateLeft(x[d] ^ x[a], 16);
        x[c] += x[d]; x[b] = Integer.rotateLeft(x[b] ^ x[c], 12);
        x[a] += x[b]; x[d] = Integer.rotateLeft(x[d] ^ x[a], 8);
        x[c] += x[d]; x[b] = Integer.rotateLeft(x[b] ^ x[c], 7);
    }

    public static byte[] chacha20Xor(byte[] key32, int initCounter, byte[] nonce12, byte[] in, int inOff, int len) {
        byte[] out = new byte[len];
        byte[] ks = new byte[64];
        int ctr = initCounter;
        int off = 0;
        while (off < len) {
            int take = Math.min(64, len - off);
            chacha20Block(key32, ctr, nonce12, ks);
            for (int i = 0; i < take; i++) {
                out[off + i] = (byte) (in[inOff + off + i] ^ ks[i]);
            }
            off += take;
            ctr++;
        }
        return out;
    }

    private static void poly1305Block16(long[] r, long[] h, byte[] b, int off) {
        long r0 = r[0], r1 = r[1], r2 = r[2], r3 = r[3], r4 = r[4];
        long s1 = r1 * 5L, s2 = r2 * 5L, s3 = r3 * 5L, s4 = r4 * 5L;

        long h0 = h[0] + (Integer.toUnsignedLong(readU32LE(b, off)) & 0x3FFFFFFL);
        long h1 = h[1] + ((Integer.toUnsignedLong(readU32LE(b, off + 3)) >>> 2) & 0x3FFFFFFL);
        long h2 = h[2] + ((Integer.toUnsignedLong(readU32LE(b, off + 6)) >>> 4) & 0x3FFFFFFL);
        long h3 = h[3] + ((Integer.toUnsignedLong(readU32LE(b, off + 9)) >>> 6) & 0x3FFFFFFL);
        long h4 = h[4] + ((Integer.toUnsignedLong(readU32LE(b, off + 12)) >>> 8) | (1L << 24));

        long d0 = h0 * r0 + h1 * s4 + h2 * s3 + h3 * s2 + h4 * s1;
        long d1 = h0 * r1 + h1 * r0 + h2 * s4 + h3 * s3 + h4 * s2;
        long d2 = h0 * r2 + h1 * r1 + h2 * r0 + h3 * s4 + h4 * s3;
        long d3 = h0 * r3 + h1 * r2 + h2 * r1 + h3 * r0 + h4 * s4;
        long d4 = h0 * r4 + h1 * r3 + h2 * r2 + h3 * r1 + h4 * r0;

        long c;
        c = d0 >>> 26; h0 = d0 & 0x3FFFFFFL; d1 += c;
        c = d1 >>> 26; h1 = d1 & 0x3FFFFFFL; d2 += c;
        c = d2 >>> 26; h2 = d2 & 0x3FFFFFFL; d3 += c;
        c = d3 >>> 26; h3 = d3 & 0x3FFFFFFL; d4 += c;
        c = d4 >>> 26; h4 = d4 & 0x3FFFFFFL; h0 += c * 5L;
        c = h0 >>> 26; h0 &= 0x3FFFFFFL;     h1 += c;

        h[0] = h0; h[1] = h1; h[2] = h2; h[3] = h3; h[4] = h4;
    }

    private static void poly1305FeedPadded16(long[] r, long[] h, byte[] buf, int off, int len) {
        int p = 0;
        while (p + 16 <= len) {
            poly1305Block16(r, h, buf, off + p);
            p += 16;
        }
        if (p < len) {
            byte[] tail = new byte[16];
            System.arraycopy(buf, off + p, tail, 0, len - p);
            poly1305Block16(r, h, tail, 0);
        }
    }

    public static byte[] poly1305AeadTag(byte[] key32, byte[] nonce12, byte[] aad, int aadOff, int aadLen, byte[] ct, int ctOff, int ctLen) {
        byte[] block0 = new byte[64];
        chacha20Block(key32, 0, nonce12, block0);

        byte[] rClamped = Arrays.copyOf(block0, 16);
        rClamped[3] &= 15; rClamped[7] &= 15; rClamped[11] &= 15; rClamped[15] &= 15;
        rClamped[4] &= (byte) 252; rClamped[8] &= (byte) 252; rClamped[12] &= (byte) 252;

        long[] r = new long[5];
        r[0] = Integer.toUnsignedLong(readU32LE(rClamped, 0)) & 0x3FFFFFFL;
        r[1] = (Integer.toUnsignedLong(readU32LE(rClamped, 3)) >>> 2) & 0x3FFFFFFL;
        r[2] = (Integer.toUnsignedLong(readU32LE(rClamped, 6)) >>> 4) & 0x3FFFFFFL;
        r[3] = (Integer.toUnsignedLong(readU32LE(rClamped, 9)) >>> 6) & 0x3FFFFFFL;
        r[4] = (Integer.toUnsignedLong(readU32LE(rClamped, 12)) >>> 8) & 0x3FFFFFFL;

        long[] s = new long[4];
        for (int i = 0; i < 4; i++) {
            s[i] = Integer.toUnsignedLong(readU32LE(block0, 16 + i * 4));
        }
        long[] h = new long[5];

        poly1305FeedPadded16(r, h, aad, aadOff, aadLen);
        poly1305FeedPadded16(r, h, ct, ctOff, ctLen);

        byte[] lengths16 = new byte[16];
        writeU64LE(lengths16, 0, aadLen);
        writeU64LE(lengths16, 8, ctLen);
        poly1305Block16(r, h, lengths16, 0);

        long h0 = h[0], h1 = h[1], h2 = h[2], h3 = h[3], h4 = h[4];
        long c;
        c = h1 >>> 26; h1 &= 0x3FFFFFFL; h2 += c;
        c = h2 >>> 26; h2 &= 0x3FFFFFFL; h3 += c;
        c = h3 >>> 26; h3 &= 0x3FFFFFFL; h4 += c;
        c = h4 >>> 26; h4 &= 0x3FFFFFFL; h0 += c * 5L;
        c = h0 >>> 26; h0 &= 0x3FFFFFFL; h1 += c;

        long g0 = h0 + 5L; c = g0 >>> 26; g0 &= 0x3FFFFFFL;
        long g1 = h1 + c;  c = g1 >>> 26; g1 &= 0x3FFFFFFL;
        long g2 = h2 + c;  c = g2 >>> 26; g2 &= 0x3FFFFFFL;
        long g3 = h3 + c;  c = g3 >>> 26; g3 &= 0x3FFFFFFL;
        long g4 = (h4 + c - (1L << 26)) & 0xFFFFFFFFL;

        long mask = ((g4 >>> 31) - 1L) & 0xFFFFFFFFL;
        g0 &= mask; g1 &= mask; g2 &= mask; g3 &= mask; g4 &= mask;
        long invMask = (~mask) & 0xFFFFFFFFL;
        h0 = (h0 & invMask) | g0;
        h1 = (h1 & invMask) | g1;
        h2 = (h2 & invMask) | g2;
        h3 = (h3 & invMask) | g3;
        h4 = (h4 & invMask) | g4;

        long w0 = (h0 | (h1 << 26)) & 0xFFFFFFFFL;
        long w1 = ((h1 >>> 6) | (h2 << 20)) & 0xFFFFFFFFL;
        long w2 = ((h2 >>> 12) | (h3 << 14)) & 0xFFFFFFFFL;
        long w3 = ((h3 >>> 18) | (h4 << 8)) & 0xFFFFFFFFL;

        long f;
        f = w0 + s[0];               w0 = f & 0xFFFFFFFFL;
        f = w1 + s[1] + (f >>> 32);  w1 = f & 0xFFFFFFFFL;
        f = w2 + s[2] + (f >>> 32);  w2 = f & 0xFFFFFFFFL;
        f = w3 + s[3] + (f >>> 32);  w3 = f & 0xFFFFFFFFL;

        byte[] tag = new byte[16];
        writeU32LE(tag, 0, (int) w0);
        writeU32LE(tag, 4, (int) w1);
        writeU32LE(tag, 8, (int) w2);
        writeU32LE(tag, 12, (int) w3);
        return tag;
    }

    // -----------------------------------------------------------------------
    // Key Generation, Derivation & RFC 6479 ReplayWindow64
    // -----------------------------------------------------------------------

    public static String generateNltpKey() {
        return Base64.getUrlEncoder().encodeToString(randomBytes(32));
    }

    public static byte[] deriveAeadKey(byte[] rawKey) {
        if (rawKey.length == 32) {
            return Arrays.copyOf(rawKey, 32);
        }
        byte[] pref = "NLTP-v1-RAW-KEY:".getBytes(StandardCharsets.UTF_8);
        byte[] cat = new byte[pref.length + rawKey.length];
        System.arraycopy(pref, 0, cat, 0, pref.length);
        System.arraycopy(rawKey, 0, cat, pref.length, rawKey.length);
        return sha256Bytes(cat);
    }

    public static byte[] deriveAeadKey(String psk) {
        String s = trimAsciiWhitespace(psk);
        if (s.length() == 43 || s.length() == 44) {
            if (isUrlSafeBase64Key(s)) {
                try {
                    String unpadded = s.endsWith("=") ? s.substring(0, 43) : s;
                    byte[] decoded = Base64.getUrlDecoder().decode(unpadded);
                    if (decoded.length == 32) {
                        return decoded;
                    }
                } catch (IllegalArgumentException ignored) {
                }
            }
        }
        byte[] pref = "NLTP-v1-PASSPHRASE:".getBytes(StandardCharsets.UTF_8);
        byte[] body = s.getBytes(StandardCharsets.UTF_8);
        byte[] cat = new byte[pref.length + body.length];
        System.arraycopy(pref, 0, cat, 0, pref.length);
        System.arraycopy(body, 0, cat, pref.length, body.length);
        return sha256Bytes(cat);
    }

    private static String trimAsciiWhitespace(String s) {
        int start = 0;
        int end = s.length();
        while (start < end && isAsciiWs(s.charAt(start))) start++;
        while (end > start && isAsciiWs(s.charAt(end - 1))) end--;
        return s.substring(start, end);
    }

    private static boolean isAsciiWs(char c) {
        return c == ' ' || c == '\t' || c == '\r' || c == '\n';
    }

    private static boolean isUrlSafeBase64Key(String s) {
        for (int i = 0; i < 43; i++) {
            char c = s.charAt(i);
            boolean ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
            if (!ok) return false;
        }
        return s.length() == 43 || s.charAt(43) == '=';
    }

    public static final class ReplayWindow64 {
        private long highestSeq = 0L;
        private long bitmap = 0L;

        public boolean wouldAccept(long seq) {
            if (seq == 0L) return false;
            if (Long.compareUnsigned(seq, highestSeq) > 0) return true;
            long diff = highestSeq - seq;
            if (Long.compareUnsigned(diff, 64L) >= 0) return false;
            return (bitmap & (1L << (int) diff)) == 0L;
        }

        public boolean accept(long seq) {
            if (!wouldAccept(seq)) return false;
            if (Long.compareUnsigned(seq, highestSeq) > 0) {
                long shift = seq - highestSeq;
                if (Long.compareUnsigned(shift, 64L) >= 0) {
                    bitmap = 1L;
                } else {
                    bitmap = (bitmap << (int) shift) | 1L;
                }
                highestSeq = seq;
            } else {
                long diff = highestSeq - seq;
                bitmap |= (1L << (int) diff);
            }
            return true;
        }
    }

    public static final class AEADCipherContext {
        private final byte[] key;
        private long txSeq = 0L;
        private final Map<Integer, ReplayWindow64> replayWindows = new HashMap<>();

        public AEADCipherContext(String psk) {
            this.key = deriveAeadKey(psk);
        }

        public AEADCipherContext(byte[] rawKey) {
            this.key = deriveAeadKey(rawKey);
        }

        public byte[] keyBytes() {
            return Arrays.copyOf(key, 32);
        }

        public long nextSeq() {
            txSeq += 1L;
            return txSeq;
        }

        public byte[] seal(int connId, long seq64, byte[] aadHdr20, byte[] plaintext) {
            byte[] nonce = new byte[12];
            writeU32LE(nonce, 0, connId);
            writeU64LE(nonce, 4, seq64);
            byte[] ct = chacha20Xor(key, 1, nonce, plaintext, 0, plaintext.length);
            byte[] tag = poly1305AeadTag(key, nonce, aadHdr20, 0, ENC_HEADER_SIZE, ct, 0, ct.length);
            byte[] out = new byte[ct.length + AEAD_TAG_SIZE];
            System.arraycopy(ct, 0, out, 0, ct.length);
            System.arraycopy(tag, 0, out, ct.length, AEAD_TAG_SIZE);
            return out;
        }

        public byte[] openAndCheckReplay(int peerConnId, long seq64, byte[] rawPkt, int rawLen) {
            if (seq64 == 0L || rawLen < ENC_HEADER_SIZE + AEAD_TAG_SIZE) {
                return null;
            }
            ReplayWindow64 win = replayWindows.get(peerConnId);
            if (win != null && !win.wouldAccept(seq64)) {
                return null;
            }
            int ctLen = rawLen - ENC_HEADER_SIZE - AEAD_TAG_SIZE;
            byte[] nonce = new byte[12];
            writeU32LE(nonce, 0, peerConnId);
            writeU64LE(nonce, 4, seq64);

            byte[] calcTag = poly1305AeadTag(key, nonce, rawPkt, 0, ENC_HEADER_SIZE, rawPkt, ENC_HEADER_SIZE, ctLen);
            byte[] rxTag = Arrays.copyOfRange(rawPkt, ENC_HEADER_SIZE + ctLen, rawLen);
            if (!MessageDigest.isEqual(calcTag, rxTag)) {
                return null;
            }

            if (win == null) {
                win = new ReplayWindow64();
                replayWindows.put(peerConnId, win);
            }
            if (!win.accept(seq64)) {
                return null;
            }
            return chacha20Xor(key, 1, nonce, rawPkt, ENC_HEADER_SIZE, ctLen);
        }
    }

    // -----------------------------------------------------------------------
    // Frame Pack / Unpack
    // -----------------------------------------------------------------------

    public static final class Frame {
        public final int pktType;
        public final int flags;
        public final int connId;
        public final int tsMs;
        public final byte[] payload;

        public Frame(int pktType, int flags, int connId, int tsMs, byte[] payload) {
            this.pktType = pktType;
            this.flags = flags;
            this.connId = connId;
            this.tsMs = tsMs;
            this.payload = payload;
        }
    }

    public static byte[] packFrame(int pktType, int connId, byte[] payload, int flags, int tsMs, AEADCipherContext aead) {
        int verType = ((NLTP_VERSION & 0x0F) << 4) | (pktType & 0x0F);
        if (aead != null) {
            int encFlags = (flags & 0x7F) | FLAG_ENCRYPTED;
            long seq64 = aead.nextSeq();
            byte[] aadHdr = new byte[ENC_HEADER_SIZE];
            aadHdr[0] = NLTP_MAGIC_0;
            aadHdr[1] = NLTP_MAGIC_1;
            aadHdr[2] = (byte) verType;
            aadHdr[3] = (byte) encFlags;
            writeU32LE(aadHdr, 4, connId);
            writeU64LE(aadHdr, 8, seq64);
            writeU32LE(aadHdr, 16, tsMs);

            byte[] ctAndTag = aead.seal(connId, seq64, aadHdr, payload);
            byte[] out = new byte[ENC_HEADER_SIZE + ctAndTag.length];
            System.arraycopy(aadHdr, 0, out, 0, ENC_HEADER_SIZE);
            System.arraycopy(ctAndTag, 0, out, ENC_HEADER_SIZE, ctAndTag.length);
            return out;
        }

        int cleanFlags = flags & 0x7F;
        byte[] out = new byte[HEADER_SIZE + payload.length];
        out[0] = NLTP_MAGIC_0;
        out[1] = NLTP_MAGIC_1;
        out[2] = (byte) verType;
        out[3] = (byte) cleanFlags;
        writeU32LE(out, 4, connId);
        writeU32LE(out, 8, 0);
        writeU32LE(out, 12, tsMs);
        if (payload.length > 0) {
            System.arraycopy(payload, 0, out, HEADER_SIZE, payload.length);
        }
        int crc = crc32Ieee(out, 0, out.length);
        writeU32LE(out, 8, crc);
        return out;
    }

    public static Frame unpackFrame(byte[] raw, int rawLen, AEADCipherContext aead) {
        if (rawLen < HEADER_SIZE || raw[0] != NLTP_MAGIC_0 || raw[1] != NLTP_MAGIC_1) {
            return null;
        }
        int verType = raw[2] & 0xFF;
        int flags = raw[3] & 0xFF;
        if ((verType >>> 4) != NLTP_VERSION) {
            return null;
        }
        boolean isEnc = (flags & FLAG_ENCRYPTED) != 0;
        if (aead != null) {
            if (!isEnc || rawLen < ENC_HEADER_SIZE + AEAD_TAG_SIZE) {
                return null;
            }
            int connId = readU32LE(raw, 4);
            long seq64 = readU64LE(raw, 8);
            int tsMs = readU32LE(raw, 16);
            byte[] pt = aead.openAndCheckReplay(connId, seq64, raw, rawLen);
            if (pt == null) return null;
            return new Frame(verType & 0x0F, flags & 0x7F, connId, tsMs, pt);
        }

        if (isEnc) {
            return null;
        }
        int connId = readU32LE(raw, 4);
        int rxCrc = readU32LE(raw, 8);
        int tsMs = readU32LE(raw, 12);

        byte[] copy = Arrays.copyOf(raw, rawLen);
        writeU32LE(copy, 8, 0);
        int calcCrc = crc32Ieee(copy, 0, rawLen);
        if (calcCrc != rxCrc) {
            return null;
        }
        byte[] payload = Arrays.copyOfRange(raw, HEADER_SIZE, rawLen);
        return new Frame(verType & 0x0F, flags, connId, tsMs, payload);
    }

    // -----------------------------------------------------------------------
    // Zero-Knowledge CGNAT Relay & RFC 5389 STUN Wire Framing
    // -----------------------------------------------------------------------

    public static byte[] makeRoomTag(String roomName) {
        byte[] pref = "NLTP-RELAY-ROOM:".getBytes(StandardCharsets.UTF_8);
        byte[] body = roomName.trim().getBytes(StandardCharsets.UTF_8);
        byte[] cat = new byte[pref.length + body.length];
        System.arraycopy(pref, 0, cat, 0, pref.length);
        System.arraycopy(body, 0, cat, pref.length, body.length);
        return Arrays.copyOf(sha256Bytes(cat), 16);
    }

    public static byte[] wrapRelayPacket(byte[] roomTag16, byte[] inner, int cmd) {
        int innerLen = inner != null ? inner.length : 0;
        byte[] out = new byte[RELAY_HDR_SIZE + innerLen];
        out[0] = RELAY_MAGIC_0;
        out[1] = RELAY_MAGIC_1;
        out[2] = (byte) (cmd & 0xFF);
        System.arraycopy(roomTag16, 0, out, 3, 16);
        if (innerLen > 0) {
            System.arraycopy(inner, 0, out, RELAY_HDR_SIZE, innerLen);
        }
        return out;
    }

    public static final class RelayFrame {
        public final int cmd;
        public final byte[] roomTag;
        public final byte[] inner;

        public RelayFrame(int cmd, byte[] roomTag, byte[] inner) {
            this.cmd = cmd;
            this.roomTag = roomTag;
            this.inner = inner;
        }
    }

    public static RelayFrame unwrapRelayPacket(byte[] raw, int rawLen) {
        if (rawLen < RELAY_HDR_SIZE || raw[0] != RELAY_MAGIC_0 || raw[1] != RELAY_MAGIC_1) {
            return null;
        }
        int cmd = raw[2] & 0xFF;
        byte[] tag = Arrays.copyOfRange(raw, 3, 19);
        byte[] inner = Arrays.copyOfRange(raw, 19, rawLen);
        return new RelayFrame(cmd, tag, inner);
    }

    public static byte[] buildStunBindingRequest(byte[] txId12) {
        byte[] req = new byte[20];
        writeU16BE(req, 0, STUN_BINDING_REQUEST);
        writeU16BE(req, 2, 0);
        writeU32BE(req, 4, STUN_MAGIC_COOKIE);
        System.arraycopy(txId12, 0, req, 8, 12);
        return req;
    }

    public static boolean isStunPacket(byte[] raw, int rawLen) {
        if (rawLen < 20) return false;
        int mlen = readU16BE(raw, 2);
        int cookie = readU32BE(raw, 4);
        return cookie == STUN_MAGIC_COOKIE && rawLen >= 20 + mlen;
    }

    public static final class MappedEndpoint {
        public final String ip;
        public final int port;

        public MappedEndpoint(String ip, int port) {
            this.ip = ip;
            this.port = port;
        }
    }

    public static MappedEndpoint parseStunBindingResponse(byte[] raw, int rawLen, byte[] expectedTxId12) {
        if (rawLen < 20) return null;
        int mtype = readU16BE(raw, 0);
        int mlen = readU16BE(raw, 2);
        int cookie = readU32BE(raw, 4);
        if (mtype != STUN_BINDING_SUCCESS || cookie != STUN_MAGIC_COOKIE) return null;
        if (expectedTxId12 != null) {
            for (int i = 0; i < 12; i++) {
                if (raw[8 + i] != expectedTxId12[i]) return null;
            }
        }
        int pos = 20;
        int end = Math.min(rawLen, 20 + mlen);
        MappedEndpoint fallback = null;
        while (pos + 4 <= end) {
            int attrType = readU16BE(raw, pos);
            int attrLen = readU16BE(raw, pos + 2);
            pos += 4;
            if (pos + attrLen > end) break;
            if (attrType == STUN_ATTR_XOR_MAPPED_ADDRESS && attrLen >= 8) {
                int family = raw[pos + 1] & 0xFF;
                int port = readU16BE(raw, pos + 2) ^ (STUN_MAGIC_COOKIE >>> 16);
                if (family == 0x01) {
                    int xip = readU32BE(raw, pos + 4) ^ STUN_MAGIC_COOKIE;
                    String ip = ((xip >>> 24) & 0xFF) + "." + ((xip >>> 16) & 0xFF) + "." + ((xip >>> 8) & 0xFF) + "." + (xip & 0xFF);
                    return new MappedEndpoint(ip, port);
                }
            } else if (attrType == STUN_ATTR_MAPPED_ADDRESS && attrLen >= 8 && (raw[pos + 1] & 0xFF) == 0x01) {
                int port = readU16BE(raw, pos + 2);
                String ip = (raw[pos + 4] & 0xFF) + "." + (raw[pos + 5] & 0xFF) + "." + (raw[pos + 6] & 0xFF) + "." + (raw[pos + 7] & 0xFF);
                fallback = new MappedEndpoint(ip, port);
            }
            pos += (attrLen + 3) & ~3;
        }
        return fallback;
    }

    // Minimal zero-dependency JSON parser for STREAM_META payloads
    // (`{"stream_id":1,"kind":"file","filename":"x.bin","total_bytes":100,"total_gens":1,"sha256":"..."}`)
    public static String jsonGetString(String json, String key) {
        String needle = "\"" + key + "\"";
        int idx = json.indexOf(needle);
        if (idx < 0) return "";
        int colon = json.indexOf(':', idx + needle.length());
        if (colon < 0) return "";
        int q1 = json.indexOf('"', colon + 1);
        if (q1 < 0) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = q1 + 1; i < json.length(); i++) {
            char c = json.charAt(i);
            if (c == '\\' && i + 1 < json.length()) {
                char nxt = json.charAt(++i);
                if (nxt == '"' || nxt == '\\' || nxt == '/') sb.append(nxt);
                else if (nxt == 'n') sb.append('\n');
                else if (nxt == 'r') sb.append('\r');
                else if (nxt == 't') sb.append('\t');
                else sb.append(nxt);
            } else if (c == '"') {
                return sb.toString();
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    public static long jsonGetLong(String json, String key, long def) {
        String needle = "\"" + key + "\"";
        int idx = json.indexOf(needle);
        if (idx < 0) return def;
        int colon = json.indexOf(':', idx + needle.length());
        if (colon < 0) return def;
        int p = colon + 1;
        while (p < json.length() && (json.charAt(p) == ' ' || json.charAt(p) == '\t')) p++;
        int start = p;
        if (p < json.length() && json.charAt(p) == '-') p++;
        while (p < json.length() && Character.isDigit(json.charAt(p))) p++;
        if (p == start) return def;
        try {
            return Long.parseLong(json.substring(start, p));
        } catch (NumberFormatException e) {
            return def;
        }
    }

    public static String jsonEscape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') sb.append("\\\"");
            else if (c == '\\') sb.append("\\\\");
            else if (c == '\n') sb.append("\\n");
            else if (c == '\r') sb.append("\\r");
            else if (c == '\t') sb.append("\\t");
            else sb.append(c);
        }
        return sb.toString();
    }
}
