package io.necklink.nltp;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Random;

/**
 * Comprehensive zero-dependency self-test suite for the NLTP v1.2 Java SDK.
 */
public final class NLTPSelfTest {
    private static int passed = 0;

    private static byte[] fromHex(String hex) {
        byte[] out = new byte[hex.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static void check(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError("ASSERTION FAILED: " + msg);
        }
    }

    private static void runTest(String name, ThrowingRunnable fn) throws Exception {
        long t0 = System.nanoTime();
        fn.run();
        double ms = (System.nanoTime() - t0) / 1_000_000.0;
        passed++;
        System.out.printf("  [PASS] %-72s (%.2f ms)%n", name, ms);
    }

    @FunctionalInterface
    interface ThrowingRunnable {
        void run() throws Exception;
    }

    public static void main(String[] args) throws Exception {
        System.out.println("=== NLTP v1.6 Java SDK Self-Test Suite ===");

        runTest("1. Pure-Java RFC 8439 ChaCha20-Poly1305 matches JCE Cipher byte-for-byte", () -> {
            byte[] key = NLTPWire.randomBytes(32);
            byte[] nonce = NLTPWire.randomBytes(12);
            byte[] aad = NLTPWire.randomBytes(20);
            byte[] pt = NLTPWire.randomBytes(973);

            Cipher jce = Cipher.getInstance("ChaCha20-Poly1305");
            jce.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "ChaCha20"), new IvParameterSpec(nonce));
            jce.updateAAD(aad);
            byte[] jceOut = jce.doFinal(pt);

            byte[] ourCt = NLTPWire.chacha20Xor(key, 1, nonce, pt, 0, pt.length);
            byte[] ourTag = NLTPWire.poly1305AeadTag(key, nonce, aad, 0, aad.length, ourCt, 0, ourCt.length);
            byte[] ourOut = new byte[ourCt.length + 16];
            System.arraycopy(ourCt, 0, ourOut, 0, ourCt.length);
            System.arraycopy(ourTag, 0, ourOut, ourCt.length, 16);

            check(Arrays.equals(ourOut, jceOut), "Pure-Java ChaCha20-Poly1305 differs from JCE");
        });

        runTest("2. Key derivation & ReplayWindow64 match Python/C99/JS specification", () -> {
            String b64 = NLTPWire.generateNltpKey();
            check(b64.length() == 44, "Expected 44-char urlsafe base64 key");
            byte[] k1 = NLTPWire.deriveAeadKey(b64);
            byte[] k2 = NLTPWire.deriveAeadKey("  " + b64 + "\n");
            check(k1.length == 32 && Arrays.equals(k1, k2), "Whitespace-trimmed base64 key mismatch");

            String pass = "marine-buoy-passphrase";
            byte[] expectedPass = MessageDigest.getInstance("SHA-256")
                    .digest(("NLTP-v1-PASSPHRASE:" + pass).getBytes(StandardCharsets.UTF_8));
            check(Arrays.equals(NLTPWire.deriveAeadKey(pass), expectedPass), "Passphrase SHA-256 mismatch");

            String pass44 = "this passphrase has exact forty four chars!!";
            byte[] expected44 = MessageDigest.getInstance("SHA-256")
                    .digest(("NLTP-v1-PASSPHRASE:" + pass44).getBytes(StandardCharsets.UTF_8));
            check(Arrays.equals(NLTPWire.deriveAeadKey(pass44), expected44), "44-char passphrase misparsed as base64");

            NLTPWire.ReplayWindow64 rw = new NLTPWire.ReplayWindow64();
            check(!rw.accept(0L), "seq 0 must be rejected");
            check(rw.accept(5L), "seq 5 accept");
            check(!rw.accept(5L), "seq 5 replay must be rejected");
            check(rw.accept(3L), "seq 3 out-of-order accept");
            check(!rw.accept(3L), "seq 3 replay reject");
            check(rw.accept(600L), "seq 600 advance");
            check(!rw.accept(88L), "seq 88 outside 512-pkt window");
            check(rw.accept(89L), "seq 89 inside 512-pkt window");
            check(!rw.accept(89L), "seq 89 replay reject");

            byte[] aliceSk = fromHex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4");
            byte[] bobPk = fromHex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c");
            byte[] expectedShared = fromHex("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552");
            check(Arrays.equals(NLTPWire.x25519ScalarMult(aliceSk, bobPk), expectedShared), "RFC 7748 X25519 vector mismatch");
            check(NLTPWire.x25519ScalarMult(aliceSk, new byte[32]) == null, "Expected null on all-zero X25519 point");
        });

        runTest("3. Frame AEAD rejects tampering, replays, wrong keys & plaintext downgrades", () -> {
            String key = NLTPWire.generateNltpKey();
            NLTPWire.AEADCipherContext tx = new NLTPWire.AEADCipherContext(key);
            NLTPWire.AEADCipherContext rx = new NLTPWire.AEADCipherContext(key);
            NLTPWire.AEADCipherContext wrong = new NLTPWire.AEADCipherContext("wrong-key");

            byte[] pt = new byte[]{10, 20, 30, 40, 50};
            byte[] enc = NLTPWire.packFrame(NLTPWire.PT_DATA_SHARD, 0x12345679, pt, 0, 777, tx);
            byte[] plain = NLTPWire.packFrame(NLTPWire.PT_DATA_SHARD, 0x12345679, pt, 0, 777, null);

            check(NLTPWire.unpackFrame(plain, plain.length, rx) == null, "Downgrade must be rejected");
            check(NLTPWire.unpackFrame(enc, enc.length, null) == null, "Encrypted on plain must be rejected");
            check(NLTPWire.unpackFrame(enc, enc.length, wrong) == null, "Wrong key must be rejected");

            NLTPWire.Frame f = NLTPWire.unpackFrame(enc, enc.length, rx);
            check(f != null && Arrays.equals(f.payload, pt), "Authentic frame failed");
            check(NLTPWire.unpackFrame(enc, enc.length, rx) == null, "Replay must be rejected");

            byte[] enc2 = NLTPWire.packFrame(NLTPWire.PT_DATA_SHARD, 0x12345679, pt, 0, 778, tx);
            byte[] tampered = Arrays.copyOf(enc2, enc2.length);
            tampered[22] ^= 0x01;
            check(NLTPWire.unpackFrame(tampered, tampered.length, rx) == null, "Tampered frame must be rejected");
            check(NLTPWire.unpackFrame(enc2, enc2.length, rx) != null, "Tampered frame poisoned replay window");
        });

        runTest("4. GF(2^8) Cauchy FEC reconstructs 8 shards after 4 erasures", () -> {
            for (int a = 1; a < 256; a++) {
                check(Galois256.mul(a, Galois256.inv(a)) == 1, "GF256 inverse failed for " + a);
            }
            int k = 8, w = 128;
            byte[][] orig = new byte[k][];
            for (int i = 0; i < k; i++) orig[i] = NLTPWire.randomBytes(w);

            byte[][] cRows = new byte[k][];
            byte[][] dRows = new byte[k][];
            int idx = 0;
            for (int sys : new int[]{0, 2, 5, 7}) {
                byte[] c = new byte[k];
                c[sys] = 1;
                cRows[idx] = c;
                dRows[idx] = Arrays.copyOf(orig[sys], w);
                idx++;
            }
            for (int pOff : new int[]{0, 3, 9, 41}) {
                byte[] c = Galois256.cauchyRow(pOff, k);
                byte[] y = new byte[w];
                for (int i = 0; i < k; i++) Galois256.axpy(y, orig[i], c[i] & 0xFF);
                cRows[idx] = c;
                dRows[idx] = y;
                idx++;
            }
            byte[][] solved = Galois256.solveLinearSystem(cRows, dRows, k);
            check(solved != null, "Singular Cauchy matrix");
            for (int i = 0; i < k; i++) {
                check(Arrays.equals(solved[i], orig[i]), "Shard " + i + " mismatch");
            }
        });

        runTest("5. Transport-agnostic NLTPEndpoint heals 20% packet loss in-memory", () -> {
            String key = NLTPWire.generateNltpKey();
            Random rng = new Random(12345L);
            List<byte[]> qAB = new ArrayList<>();
            List<byte[]> qBA = new ArrayList<>();

            NLTPEndpoint<String> epA = new NLTPEndpoint<>(
                    null,
                    (pkt, peer) -> { if (rng.nextDouble() >= 0.20) qAB.add(Arrays.copyOf(pkt, pkt.length)); },
                    null, 32, NLTPWire.DEFAULT_SHARD_BYTES, NLTPWire.DEFAULT_GEN_K, key, null, 0.0, false, null
            );
            NLTPEndpoint<String> epB = new NLTPEndpoint<>(
                    null,
                    (pkt, peer) -> { if (rng.nextDouble() >= 0.20) qBA.add(Arrays.copyOf(pkt, pkt.length)); },
                    null, 32, NLTPWire.DEFAULT_SHARD_BYTES, NLTPWire.DEFAULT_GEN_K, key, null, 0.0, false, null
            );

            byte[] payload = NLTPWire.randomBytes(45000);
            double nowS = 1.0;
            NLTPEndpoint.OutboundTransfer<String> xfer = epA.startTransfer("peerB", payload, "bytes", "", nowS);

            for (int step = 0; step < 600 && !xfer.completed; step++) {
                nowS += 0.010;
                while (!qAB.isEmpty()) {
                    byte[] p = qAB.remove(0);
                    epB.onDatagram(p, p.length, "peerA", nowS);
                }
                epB.stepTx(nowS);
                while (!qBA.isEmpty()) {
                    byte[] p = qBA.remove(0);
                    epA.onDatagram(p, p.length, "peerB", nowS);
                }
                epA.stepTx(nowS);
            }
            check(xfer.completed && xfer.verifiedSha256ByPeer, "In-memory 20% loss transfer failed");
            ReceivedItem<String> item = epB.inboxQueue().poll();
            check(item != null && item.sha256Verified() && Arrays.equals(item.data(), payload), "Payload mismatch");
        });

        runTest("6. Live UDP NLTPServer <-> NLTPClient encrypted file, reflex, roaming & bidirectional at 15% loss", () -> {
            Path tmpDir = Files.createTempDirectory("nltp-java-test-");
            Path srcFile = tmpDir.resolve("firmware.bin");
            byte[] srcBytes = NLTPWire.randomBytes(96 * 1024);
            Files.write(srcFile, srcBytes);
            Path rxDir = tmpDir.resolve("inbox");
            String key = NLTPWire.generateNltpKey();

            try (NLTPServer srv = NLTPServer.builder()
                    .bind("127.0.0.1", 0)
                    .psk(key)
                    .saveDir(rxDir)
                    .lossDropSim(0.15)
                    .build();
                 NLTPClient cli = NLTPClient.builder()
                         .server("127.0.0.1", srv.localAddress().getPort())
                         .psk(key)
                         .lossDropSim(0.15)
                         .build()) {

                cli.sendReflex("ESTOP_NOW");
                ReceivedItem<InetSocketAddress> reflex = srv.recv(5000L);
                check(reflex != null && "reflex".equals(reflex.kind()) && "ESTOP_NOW".equals(reflex.asText()), "Reflex failed");

                TransferReceipt r = cli.sendFile(srcFile, 25000L);
                check(r.verifiedSha256() && r.encrypted(), "Encrypted 96KB file transfer failed");
                ReceivedItem<InetSocketAddress> fileItem = srv.recv(5000L);
                check(fileItem != null && fileItem.sha256Verified() && Arrays.equals(fileItem.data(), srcBytes), "RX file mismatch");
                check(Arrays.equals(Files.readAllBytes(rxDir.resolve("firmware.bin")), srcBytes), "Saved disk file mismatch");

                int oldPort = cli.localAddress().getPort();
                InetSocketAddress newLocal = cli.rebindSocket("127.0.0.1", 0);
                check(newLocal.getPort() != oldPort, "Socket did not rebind to new port");
                TransferReceipt jr = cli.sendJson("{\"buoy\":7}", 15000L);
                check(jr.verifiedSha256(), "Post-roam JSON send failed");
                ReceivedItem<InetSocketAddress> jsonItem = srv.recv(5000L);
                check(jsonItem != null && "{\"buoy\":7}".equals(jsonItem.asText()), "Post-roam JSON content mismatch");
                check(srv.engine().roamEvents() >= 1, "Expected roam event recorded");

                byte[] reply = NLTPWire.randomBytes(16384);
                check(srv.sendBytes(jsonItem.peerAddr(), reply, 15000L), "Reverse Server->Client transfer failed");
                ReceivedItem<InetSocketAddress> cliRx = cli.recv(5000L);
                check(cliRx != null && Arrays.equals(cliRx.data(), reply), "Reverse payload mismatch");
            } finally {
                Files.walk(tmpDir)
                        .sorted(Comparator.reverseOrder())
                        .forEach(p -> { try { Files.deleteIfExists(p); } catch (Exception ignored) {} });
            }
        });

        runTest("7. Zero-knowledge NLTPRelayHub traverses CGNAT with E2E encryption at 10% loss", () -> {
            String key = "cgnat-buoy-secret";
            try (NLTPRelayHub hub = new NLTPRelayHub("127.0.0.1", 0);
                 NLTPServer srv = NLTPServer.builder()
                         .bind("127.0.0.1", 0)
                         .psk(key)
                         .relay(new InetSocketAddress("127.0.0.1", hub.localAddress().getPort()), "buoy-room-42")
                         .lossDropSim(0.10)
                         .build();
                 NLTPClient cli = NLTPClient.builder()
                         .psk(key)
                         .relay(new InetSocketAddress("127.0.0.1", hub.localAddress().getPort()), "buoy-room-42")
                         .lossDropSim(0.10)
                         .build()) {
                byte[] payload = NLTPWire.randomBytes(32768);
                TransferReceipt r = cli.sendBytes(payload, 20000L);
                check(r.verifiedSha256(), "Relay transfer failed");
                ReceivedItem<InetSocketAddress> rx = srv.recv(5000L);
                check(rx != null && Arrays.equals(rx.data(), payload), "Relay RX payload mismatch");
                check(hub.forwardedPackets() > 0, "Expected relay hub to forward packets");
            }
        });

        runTest("8. NLTP 1.6 LZ4 block codec, RLE overlap, auto-bypass & encrypted E2E at 12% loss", () -> {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 600; i++) {
                sb.append("{\"station\":\"AQMS-VADODARA-07\",\"seq\":")
                        .append(i)
                        .append(",\"pm25\":41.8,\"pm10\":88.2,\"so2\":11.4,\"no2\":23.7,\"co\":0.84}\n");
            }
            byte[] jsonl = sb.toString().getBytes(StandardCharsets.UTF_8);
            byte[] comp = NLTPWire.lz4Compress(jsonl);
            check(comp != null && comp.length * 4 < jsonl.length, "Expected >4x LZ4 compression on structured JSONL");
            byte[] dec = NLTPWire.lz4Decompress(comp, jsonl.length);
            check(dec != null && Arrays.equals(dec, jsonl), "LZ4 JSONL roundtrip mismatch");

            byte[] rle = new byte[12000];
            for (int i = 0; i < rle.length; i++) rle[i] = (byte) ("ABCD".charAt(i % 4));
            byte[] rleComp = NLTPWire.lz4Compress(rle);
            check(rleComp != null && rleComp.length < 150, "Expected extreme RLE overlap compression");
            check(Arrays.equals(NLTPWire.lz4Decompress(rleComp, rle.length), rle), "LZ4 overlapping copy mismatch");

            byte[] rnd = NLTPWire.randomBytes(8192);
            check(NLTPWire.lz4Compress(rnd) == null, "Expected smart auto-bypass (null) on incompressible random bytes");
            check(NLTPWire.lz4Compress(new byte[16]) == null, "Expected auto-bypass on tiny <32B payload");

            for (int f = 0; f < 64; f++) {
                byte[] junk = NLTPWire.randomBytes(1 + (f % 40));
                byte[] res = NLTPWire.lz4Decompress(junk, 256);
                check(res == null || res.length == 256, "Fuzz decompressor violated bounds");
            }

            String key = NLTPWire.generateNltpKey();
            try (NLTPServer srv = NLTPServer.builder()
                    .bind("127.0.0.1", 0)
                    .psk(key)
                    .lossDropSim(0.12)
                    .build();
                 NLTPClient cli = NLTPClient.builder()
                         .server("127.0.0.1", srv.localAddress().getPort())
                         .psk(key)
                         .lossDropSim(0.12)
                         .build()) {
                TransferReceipt rLz4 = cli.sendBytes(jsonl, "aqms.jsonl", 20000L, true);
                check(rLz4.verifiedSha256() && rLz4.compressed(), "Expected compressed=true receipt");
                check(rLz4.bytes() == jsonl.length && rLz4.wirePayloadBytes() == comp.length, "Receipt byte lengths mismatch");
                ReceivedItem<InetSocketAddress> rx1 = srv.recv(5000L);
                check(rx1 != null && rx1.sha256Verified() && Arrays.equals(rx1.data(), jsonl), "Encrypted LZ4 RX mismatch");

                TransferReceipt rBypass = cli.sendBytes(rnd, "random.bin", 20000L, true);
                check(rBypass.verifiedSha256() && !rBypass.compressed(), "Expected auto-bypass compressed=false on random");
                check(rBypass.wirePayloadBytes() == rnd.length, "Expected zero inflation on random bytes");
                ReceivedItem<InetSocketAddress> rx2 = srv.recv(5000L);
                check(rx2 != null && rx2.sha256Verified() && Arrays.equals(rx2.data(), rnd), "Auto-bypass RX mismatch");
            }
        });

        System.out.printf("%nALL %d/8 NLTP JAVA SELF-TESTS PASSED!%n", passed);
    }
}
