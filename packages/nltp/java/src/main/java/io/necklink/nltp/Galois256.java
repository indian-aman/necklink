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
    private static final byte[][] GF_MUL_TAB = new byte[256][256];

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
        for (int f = 1; f < 256; f++) {
            int lf = GF_LOG[f];
            byte[] row = GF_MUL_TAB[f];
            for (int v = 1; v < 256; v++) {
                row[v] = (byte) GF_EXP[lf + GF_LOG[v]];
            }
        }
    }

    private Galois256() {}

    public static int mul(int a, int b) {
        return GF_MUL_TAB[a & 0xFF][b & 0xFF] & 0xFF;
    }

    public static int inv(int a) {
        int ua = a & 0xFF;
        if (ua == 0) {
            throw new IllegalArgumentException("GF(256) inverse of 0 is undefined");
        }
        return GF_EXP[255 - GF_LOG[ua]];
    }

    /** dst ^= factor * src (branchless 4x-unrolled table lookup) */
    public static void axpy(byte[] dst, byte[] src, int factor) {
        int f = factor & 0xFF;
        if (f == 0) {
            return;
        }
        int n = dst.length;
        int n4 = n & ~3;
        if (f == 1) {
            int i = 0;
            for (; i < n4; i += 4) {
                dst[i] ^= src[i];
                dst[i + 1] ^= src[i + 1];
                dst[i + 2] ^= src[i + 2];
                dst[i + 3] ^= src[i + 3];
            }
            for (; i < n; i++) {
                dst[i] ^= src[i];
            }
            return;
        }
        byte[] mulRow = GF_MUL_TAB[f];
        int i = 0;
        for (; i < n4; i += 4) {
            dst[i] ^= mulRow[src[i] & 0xFF];
            dst[i + 1] ^= mulRow[src[i + 1] & 0xFF];
            dst[i + 2] ^= mulRow[src[i + 2] & 0xFF];
            dst[i + 3] ^= mulRow[src[i + 3] & 0xFF];
        }
        for (; i < n; i++) {
            dst[i] ^= mulRow[src[i] & 0xFF];
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
        byte[] mulRow = GF_MUL_TAB[f];
        int n = buf.length;
        int n4 = n & ~3;
        int i = 0;
        for (; i < n4; i += 4) {
            buf[i] = mulRow[buf[i] & 0xFF];
            buf[i + 1] = mulRow[buf[i + 1] & 0xFF];
            buf[i + 2] = mulRow[buf[i + 2] & 0xFF];
            buf[i + 3] = mulRow[buf[i + 3] & 0xFF];
        }
        for (; i < n; i++) {
            buf[i] = mulRow[buf[i] & 0xFF];
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
