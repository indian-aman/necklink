import io.necklink.nltp.NLTPClient;
import io.necklink.nltp.NLTPRelayHub;
import io.necklink.nltp.NLTPServer;
import io.necklink.nltp.NLTPWire;
import io.necklink.nltp.ReceivedItem;
import io.necklink.nltp.TransferReceipt;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.concurrent.CompletableFuture;

/**
 * NLTP v1.2 Java Example:
 * Encrypted File Transfer + Out-of-Line Reflex + Zero-Knowledge CGNAT Relay under 10% Loss.
 *
 * Build & Run:
 *   javac -cp java/target/nltp-1.3.2.jar examples/java/EncryptedFileAndReflexExample.java
 *   java -cp "java/target/nltp-1.3.2.jar;examples/java" EncryptedFileAndReflexExample
 */
public final class EncryptedFileAndReflexExample {
    public static void main(String[] args) throws Exception {
        String key = NLTPWire.generateNltpKey();
        Path inbox = Files.createTempDirectory("nltp-java-demo-");
        byte[] payload = new byte[128 * 1024];
        new SecureRandom().nextBytes(payload);

        // Part 1: Direct encrypted UDP + urgent reflex + reverse reply at 10% packet loss
        try (NLTPServer srv = NLTPServer.builder()
                .bind("127.0.0.1", 0)
                .psk(key)
                .saveDir(inbox)
                .maxTxKbps(25_000.0)
                .lossDropSim(0.10)
                .build();
             NLTPClient cli = NLTPClient.builder()
                .server("127.0.0.1", srv.localAddress().getPort())
                .psk(key)
                .maxTxKbps(25_000.0)
                .lossDropSim(0.10)
                .build()) {

            cli.sendReflex("ESTOP_CHECK".getBytes(StandardCharsets.UTF_8));
            ReceivedItem<InetSocketAddress> reflex = srv.recv(3000L);
            System.out.println("[1/3] Server received urgent reflex: "
                    + new String(reflex.data(), StandardCharsets.UTF_8));

            CompletableFuture<ReceivedItem<InetSocketAddress>> rxFuture =
                    CompletableFuture.supplyAsync(() -> {
                        try {
                            return srv.recv(30_000L);
                        } catch (Exception e) {
                            throw new RuntimeException(e);
                        }
                    });

            TransferReceipt receipt = cli.sendBytes(payload, "buoy_telemetry_128k.bin", 30_000L);
            ReceivedItem<InetSocketAddress> rxFile = rxFuture.join();
            if (!receipt.verifiedSha256() || !Arrays.equals(payload, rxFile.data())) {
                throw new IllegalStateException("Direct transfer verification failed");
            }
            System.out.printf("[2/3] Direct 128 KB @ 10%% loss: verified=%s, elapsed=%.3fs, goodput=%.1f kbps%n",
                    receipt.verifiedSha256(), receipt.elapsedS(), receipt.goodputKbps());
        }

        // Part 2: Zero-knowledge 1-port CGNAT Relay Hub at 10% packet loss
        try (NLTPRelayHub hub = new NLTPRelayHub("127.0.0.1", 0);
             NLTPServer shore = NLTPServer.builder()
                .bind("127.0.0.1", 0)
                .psk(key)
                .relay(hub.localAddress(), "vessel-alpha-01")
                .lossDropSim(0.10)
                .build();
             NLTPClient vessel = NLTPClient.builder()
                .server("127.0.0.1", 9)
                .psk(key)
                .relay(hub.localAddress(), "vessel-alpha-01")
                .lossDropSim(0.10)
                .build()) {

            byte[] radarBatch = Arrays.copyOf(payload, 64 * 1024);
            CompletableFuture<ReceivedItem<InetSocketAddress>> shoreFuture =
                    CompletableFuture.supplyAsync(() -> {
                        try {
                            return shore.recv(30_000L);
                        } catch (Exception e) {
                            throw new RuntimeException(e);
                        }
                    });

            TransferReceipt relayReceipt = vessel.sendBytes(radarBatch, "cgnat_radar_64k.bin", 30_000L);
            ReceivedItem<InetSocketAddress> shoreItem = shoreFuture.join();
            if (!relayReceipt.verifiedSha256() || !Arrays.equals(radarBatch, shoreItem.data())) {
                throw new IllegalStateException("Relay transfer verification failed");
            }
            System.out.printf("[3/3] CGNAT Relay 64 KB @ 10%% loss: verified=%s, relayed_pkts=%d%n",
                    relayReceipt.verifiedSha256(), hub.relayedPackets());
        }
    }
}
