package io.necklink.nltp;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.channels.DatagramChannel;
import java.nio.channels.SelectionKey;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Internet-ready standalone NLTP v1.2 Client ({@code NLTPClient}).
 *
 * Connects in 0-RTT to any NLTP server (Python, C99, ESP32, JavaScript, or Java) with
 * {@code GF(2^8)} Cauchy FEC, 1-RTT packet-train pacing, {@code ChaCha20-Poly1305} AEAD,
 * Giant-Fiber {@code sendReflex}, STUN hole-punching, CGNAT relay, and live socket roaming.
 */
public final class NLTPClient extends NLTPSocketBase {
    public static final class Builder {
        private String host = null;
        private int port = 9000;
        private String bindHost = "0.0.0.0";
        private int bindPort = 0;
        private Path saveDir = null;
        private String psk = null;
        private InetSocketAddress relayAddr = null;
        private String room = null;
        private Double maxTxKbps = 50000.0;
        private double keepaliveS = 15.0;
        private double lossDropSim = 0.0;
        private boolean adaptiveRate = true;
        private Integer initialBurstBytes = null;

        public Builder server(String host, int port) { this.host = host; this.port = port; return this; }
        public Builder bind(String bindHost, int bindPort) { this.bindHost = bindHost; this.bindPort = bindPort; return this; }
        public Builder saveDir(Path saveDir) { this.saveDir = saveDir; return this; }
        public Builder psk(String psk) { this.psk = psk; return this; }
        public Builder relay(InetSocketAddress relayAddr, String room) { this.relayAddr = relayAddr; this.room = room; return this; }
        public Builder maxTxKbps(Double maxTxKbps) { this.maxTxKbps = maxTxKbps; return this; }
        public Builder keepaliveS(double keepaliveS) { this.keepaliveS = keepaliveS; return this; }
        public Builder lossDropSim(double lossDropSim) { this.lossDropSim = lossDropSim; return this; }
        public Builder adaptiveRate(boolean adaptiveRate) { this.adaptiveRate = adaptiveRate; return this; }
        public Builder initialBurstBytes(Integer initialBurstBytes) { this.initialBurstBytes = initialBurstBytes; return this; }

        public NLTPClient build() throws IOException {
            return new NLTPClient(this);
        }
    }

    public static Builder builder() {
        return new Builder();
    }

    private InetSocketAddress serverAddr;

    private NLTPClient(Builder b) throws IOException {
        super(9090L);
        if (b.host == null && b.relayAddr == null) {
            throw new IllegalArgumentException("Provide either `server(host, port)` or `relay(relayAddr, room)`");
        }
        this.serverAddr = (b.relayAddr != null) ? b.relayAddr : resolveUdpAddress(b.host, b.port);
        InetSocketAddress bindSa = resolveUdpAddress(b.bindHost, b.bindPort);
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
        this.engine.registerKeepalivePeer(this.serverAddr);
        startWorker("nltp-java-client");
    }

    public void setPeerEndpoint(String host, int port) throws IOException {
        InetSocketAddress sa = resolveUdpAddress(host, port);
        lock.lock();
        try {
            this.serverAddr = sa;
            this.engine.registerKeepalivePeer(sa);
        } finally {
            lock.unlock();
        }
    }

    public InetSocketAddress rebindSocket(String newBindHost, int newBindPort) throws IOException {
        InetSocketAddress bindSa = resolveUdpAddress(newBindHost, newBindPort);
        DatagramChannel newCh = openUdpChannel(bindSa);
        DatagramChannel oldCh;
        lock.lock();
        try {
            oldCh = this.channel;
            for (SelectionKey k : selector.keys()) {
                k.cancel();
            }
            this.channel = newCh;
            this.channel.register(this.selector, SelectionKey.OP_READ);
            for (NLTPEndpoint.OutboundTransfer<InetSocketAddress> x : engine.outboundMap().values()) {
                if (!x.completed) {
                    x.peerAddr = this.serverAddr;
                }
            }
            if (relayTarget != null) {
                relayRegistered = false;
                sendRelayRegister();
            }
            engine.sendPing(serverAddr, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        try {
            oldCh.close();
        } catch (IOException ignored) {
        }
        return localAddress();
    }

    public TransferReceipt sendBytes(byte[] data, long timeoutMs) {
        return sendBytes(data, "", timeoutMs);
    }

    public TransferReceipt sendBytes(byte[] data, String filename, long timeoutMs) {
        String safeName = (filename != null) ? filename : "";
        String kind = safeName.isEmpty() ? "bytes" : "file";
        NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer;
        lock.lock();
        try {
            xfer = engine.startTransfer(serverAddr, data, kind, safeName, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        boolean ok = awaitTransfer(xfer, timeoutMs);
        return buildReceipt(xfer, ok);
    }

    public TransferReceipt sendMessage(String text, long timeoutMs) {
        return sendBytes(text.getBytes(StandardCharsets.UTF_8), timeoutMs);
    }

    public TransferReceipt sendJson(String jsonString, long timeoutMs) {
        byte[] raw = jsonString.getBytes(StandardCharsets.UTF_8);
        NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer;
        lock.lock();
        try {
            xfer = engine.startTransfer(serverAddr, raw, "json", "", NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        boolean ok = awaitTransfer(xfer, timeoutMs);
        return buildReceipt(xfer, ok);
    }

    public TransferReceipt sendFile(Path filepath, long timeoutMs) throws IOException {
        byte[] raw = Files.readAllBytes(filepath);
        String name = filepath.getFileName().toString();
        NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer;
        lock.lock();
        try {
            xfer = engine.startTransfer(serverAddr, raw, "file", name, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        boolean ok = awaitTransfer(xfer, timeoutMs);
        return buildReceipt(xfer, ok);
    }

    public int sendReflex(String urgentMsg) {
        return sendReflex(urgentMsg.getBytes(StandardCharsets.UTF_8));
    }

    public int sendReflex(byte[] urgentBytes) {
        int seq;
        lock.lock();
        try {
            seq = engine.sendReflex(serverAddr, urgentBytes, true, NLTPEndpoint.monoNowS());
        } finally {
            lock.unlock();
        }
        selector.wakeup();
        return seq;
    }

    private TransferReceipt buildReceipt(NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer, boolean ok) {
        double endS = (xfer.completedAt != null) ? xfer.completedAt : NLTPEndpoint.monoNowS();
        double elapsed = Math.max(1e-6, endS - xfer.startedAt);
        double kbps = ((xfer.totalBytes * 8.0) / 1000.0) / elapsed;
        return new TransferReceipt(
                xfer.streamId,
                xfer.filename,
                xfer.totalBytes,
                xfer.sha256Hex,
                ok,
                elapsed,
                kbps,
                engine.peerLossEma() * 100.0,
                engine.isEncrypted()
        );
    }
}
