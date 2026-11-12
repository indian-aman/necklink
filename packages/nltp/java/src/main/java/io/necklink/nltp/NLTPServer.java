package io.necklink.nltp;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Internet-ready standalone NLTP v1.2 Server ({@code NLTPServer}).
 *
 * Supports direct IPv4/IPv6 UDP listening, STUN NAT hole-punching, and zero-knowledge
 * {@link NLTPRelayHub} CGNAT traversal with optional 256-bit {@code ChaCha20-Poly1305} AEAD.
 */
public final class NLTPServer extends NLTPSocketBase {
    public static final class Builder {
        private String host = "0.0.0.0";
        private int port = 9000;
        private Path saveDir = null;
        private String psk = null;
        private InetSocketAddress relayAddr = null;
        private String room = null;
        private Double maxTxKbps = 50000.0;
        private double keepaliveS = 15.0;
        private double lossDropSim = 0.0;
        private boolean adaptiveRate = true;
        private Integer initialBurstBytes = null;

        public Builder bind(String host, int port) { this.host = host; this.port = port; return this; }
        public Builder saveDir(Path saveDir) { this.saveDir = saveDir; return this; }
        public Builder psk(String psk) { this.psk = psk; return this; }
        public Builder relay(InetSocketAddress relayAddr, String room) { this.relayAddr = relayAddr; this.room = room; return this; }
        public Builder maxTxKbps(Double maxTxKbps) { this.maxTxKbps = maxTxKbps; return this; }
        public Builder keepaliveS(double keepaliveS) { this.keepaliveS = keepaliveS; return this; }
        public Builder lossDropSim(double lossDropSim) { this.lossDropSim = lossDropSim; return this; }
        public Builder adaptiveRate(boolean adaptiveRate) { this.adaptiveRate = adaptiveRate; return this; }
        public Builder initialBurstBytes(Integer initialBurstBytes) { this.initialBurstBytes = initialBurstBytes; return this; }

        public NLTPServer build() throws IOException {
            return new NLTPServer(this);
        }
    }

    public static Builder builder() {
        return new Builder();
    }

    private NLTPServer(Builder b) throws IOException {
        super(2026L);
        InetSocketAddress bindSa = resolveUdpAddress(b.host, b.port);
        initCommon(
                bindSa,
                b.saveDir,
                b.psk,
                b.relayAddr,
                b.room,
                b.maxTxKbps,
                b.keepaliveS,
                b.lossDropSim,
                b.adaptiveRate,
                b.initialBurstBytes
        );
        startWorker("nltp-java-server");
    }

    /** Urgent reflex (e.g. an E-stop) to a connected client; retransmitted until acknowledged. */
    public int sendReflex(InetSocketAddress clientAddr, byte[] urgentBytes) {
        int seq;
        lock.lock();
        try {
            seq = engine.sendReflex(clientAddr, urgentBytes, true, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        return seq;
    }

    public boolean sendBytes(InetSocketAddress clientAddr, byte[] data, long timeoutMs) {
        return sendBytes(clientAddr, data, timeoutMs, false);
    }

    public boolean sendBytes(InetSocketAddress clientAddr, byte[] data, long timeoutMs, boolean compress) {
        NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer;
        lock.lock();
        try {
            xfer = engine.startTransfer(clientAddr, data, "bytes", "", compress, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        return awaitTransfer(xfer, timeoutMs);
    }

    public boolean sendFile(InetSocketAddress clientAddr, Path filepath, long timeoutMs) throws IOException {
        return sendFile(clientAddr, filepath, timeoutMs, false);
    }

    public boolean sendFile(InetSocketAddress clientAddr, Path filepath, long timeoutMs, boolean compress) throws IOException {
        byte[] raw = Files.readAllBytes(filepath);
        String name = filepath.getFileName().toString();
        NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer;
        lock.lock();
        try {
            xfer = engine.startTransfer(clientAddr, raw, "file", name, compress, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        return awaitTransfer(xfer, timeoutMs);
    }
}
