package io.necklink.nltp;

import java.util.Arrays;

/**
 * GF(2^8) Algebraic Cauchy FEC over irreducible polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D).
 * Byte-for-byte compatible with Python (`necklink/nltp.py`), ANSI C99 (`csrc/nltp_wire.h`),
 * Arduino/ESP32 (`arduino/NLTP`), and JavaScript (`js/src/wire.js`).
 */
public final class Galois256 {
    private static final int[] GF_EXP = new int[512];
    private static final int[] GF_LOG = new int[256];

    static {
        int x = 1;
        for (int i = 0; i < 255; i++) {
            GF_EXP[i] = x;
            GF_LOG[x] = i;
            x <<= 1;
            if ((x & 0x100) != 0) {
                x ^= 0x11D;
            }
        }
        for (int i = 255; i < 512; i++) {
            GF_EXP[i] = GF_EXP[i - 255];
        }
    }

    private Galois256() {}

    public static int mul(int a, int b) {
        int ua = a & 0xFF;
        int ub = b & 0xFF;
        return (ua != 0 && ub != 0) ? GF_EXP[GF_LOG[ua] + GF_LOG[ub]] : 0;
    }

    public static int inv(int a) {
        int ua = a & 0xFF;
        if (ua == 0) {
            throw new IllegalArgumentException("GF(256) inverse of 0 is undefined");
        }
        return GF_EXP[255 - GF_LOG[ua]];
    }

    /** dst ^= factor * src */
    public static void axpy(byte[] dst, byte[] src, int factor) {
        int f = factor & 0xFF;
        if (f == 0) {
            return;
        }
        int n = dst.length;
        if (f == 1) {
            for (int i = 0; i < n; i++) {
                dst[i] ^= src[i];
            }
            return;
        }
        int lf = GF_LOG[f];
        for (int i = 0; i < n; i++) {
            int sv = src[i] & 0xFF;
            if (sv != 0) {
                dst[i] ^= (byte) GF_EXP[lf + GF_LOG[sv]];
            }
        }
    }

    public static void scale(byte[] buf, int factor) {
        int f = factor & 0xFF;
        if (f == 1) {
            return;
        }
        if (f == 0) {
            Arrays.fill(buf, (byte) 0);
            return;
        }
        int lf = GF_LOG[f];
        for (int i = 0; i < buf.length; i++) {
            int v = buf[i] & 0xFF;
            if (v != 0) {
                buf[i] = (byte) GF_EXP[lf + GF_LOG[v]];
            }
        }
    }

    public static byte[] cauchyRow(int parityOffset, int kShards) {
        int mod = ((parityOffset % 192) + 192) % 192;
        int xR = 64 + mod;
        byte[] row = new byte[kShards];
        for (int c = 0; c < kShards; c++) {
            row[c] = (byte) inv(xR ^ (c & 0x3F));
        }
        return row;
    }

    /**
     * Solves A * X = Y over GF(2^8) via Gauss-Jordan elimination.
     * Returns solved k x rowWidth byte matrix, or null if singular.
     */
    public static byte[][] solveLinearSystem(byte[][] coeffRows, byte[][] dataRows, int k) {
        byte[][] a = new byte[k][];
        byte[][] y = new byte[k][];
        for (int r = 0; r < k; r++) {
            a[r] = Arrays.copyOf(coeffRows[r], k);
            y[r] = Arrays.copyOf(dataRows[r], dataRows[r].length);
        }

        for (int col = 0; col < k; col++) {
            int pivot = -1;
            for (int r = col; r < k; r++) {
                if ((a[r][col] & 0xFF) != 0) {
                    pivot = r;
                    break;
                }
            }
            if (pivot < 0) {
                return null;
            }
            if (pivot != col) {
                byte[] tmpA = a[col]; a[col] = a[pivot]; a[pivot] = tmpA;
                byte[] tmpY = y[col]; y[col] = y[pivot]; y[pivot] = tmpY;
            }

            int invPivot = inv(a[col][col] & 0xFF);
            scale(a[col], invPivot);
            scale(y[col], invPivot);

            for (int r = 0; r < k; r++) {
                if (r != col && (a[r][col] & 0xFF) != 0) {
                    int factor = a[r][col] & 0xFF;
                    axpy(a[r], a[col], factor);
                    axpy(y[r], y[col], factor);
                }
            }
        }
        return y;
    }
}
