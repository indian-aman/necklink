package io.necklink.nltp;

import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.net.StandardProtocolFamily;
import java.net.StandardSocketOptions;
import java.nio.ByteBuffer;
import java.nio.channels.DatagramChannel;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Shared non-blocking NIO {@link DatagramChannel} I/O loop supporting DNS, IPv4/IPv6,
 * live RFC 5389 STUN, P2P hole-punching, and zero-knowledge {@link NLTPRelayHub}.
 */
public abstract class NLTPSocketBase implements AutoCloseable {
    public static final String[][] DEFAULT_STUN_SERVERS = {
            {"stun.cloudflare.com", "3478"},
            {"stun.l.google.com", "19302"},
            {"stun1.l.google.com", "19302"},
    };

    protected DatagramChannel channel;
    protected Selector selector;
    protected final ReentrantLock lock = new ReentrantLock();
    protected NLTPEndpoint<InetSocketAddress> engine;
    protected volatile boolean running = false;
    protected Thread workerThread;

    protected InetSocketAddress relayTarget;
    protected byte[] roomTag;
    protected boolean relayRegistered = false;
    protected double lastRelayRegS = 0.0;
    protected double lossSim = 0.0;
    protected final Random lossRng;
    protected final BlockingQueue<byte[]> stunInbox = new LinkedBlockingQueue<>();

    protected NLTPSocketBase(long rngSeed) {
        this.lossRng = new Random(rngSeed);
    }

    public static InetSocketAddress resolveUdpAddress(String host, int port) throws IOException {
        String h = (host == null || host.isEmpty()) ? "0.0.0.0" : host;
        InetAddress[] addrs = InetAddress.getAllByName(h);
        for (InetAddress a : addrs) {
            if (a instanceof Inet4Address) {
                return new InetSocketAddress(a, port);
            }
        }
        return new InetSocketAddress(addrs[0], port);
    }

    protected static DatagramChannel openUdpChannel(InetSocketAddress bindAddr) throws IOException {
        boolean v4 = bindAddr.getAddress() instanceof Inet4Address;
        DatagramChannel ch = DatagramChannel.open(v4 ? StandardProtocolFamily.INET : StandardProtocolFamily.INET6);
        try {
            ch.setOption(StandardSocketOptions.SO_RCVBUF, 2 * 1024 * 1024);
            ch.setOption(StandardSocketOptions.SO_SNDBUF, 2 * 1024 * 1024);
        } catch (IOException ignored) {
        }
        ch.bind(bindAddr);
        ch.configureBlocking(false);
        return ch;
    }

    protected void initCommon(
            InetSocketAddress bindAddr,
            Path saveDir,
            String psk,
            InetSocketAddress relayAddr,
            String room,
            Double maxTxKbps,
            double keepaliveS,
            double lossDropSim,
            boolean adaptiveRate,
            Integer initialBurstBytes
    ) throws IOException {
        if (relayAddr != null && (room == null || room.trim().isEmpty())) {
            throw new IllegalArgumentException("`room` name is required when `relayAddr` is enabled");
        }
        if (saveDir != null) {
            Files.createDirectories(saveDir);
        }
        this.channel = openUdpChannel(bindAddr);
        this.selector = Selector.open();
        this.channel.register(this.selector, SelectionKey.OP_READ);
        this.relayTarget = relayAddr;
        this.roomTag = (relayAddr != null) ? NLTPWire.makeRoomTag(room) : null;
        this.lossSim = lossDropSim;

        final Path finalSaveDir = saveDir;
        this.engine = new NLTPEndpoint<>(
                null,
                this::rawSendTo,
                (finalSaveDir != null)
                        ? (safeName, bytes) -> {
                            Path out = finalSaveDir.resolve(safeName);
                            try {
                                Files.write(out, bytes);
                            } catch (IOException e) {
                                throw new RuntimeException(e);
                            }
                            return out;
                        }
                        : null,
                32,
                NLTPWire.DEFAULT_SHARD_BYTES,
                NLTPWire.DEFAULT_GEN_K,
                psk,
                maxTxKbps,
                keepaliveS,
                adaptiveRate,
                initialBurstBytes
        );
    }

    protected void startWorker(String threadName) {
        this.running = true;
        this.workerThread = new Thread(this::ioLoop, threadName);
        this.workerThread.setDaemon(true);
        this.workerThread.start();
        if (this.relayTarget != null) {
            long deadline = System.nanoTime() + 300_000_000L;
            while (!this.relayRegistered && this.running && System.nanoTime() < deadline) {
                try {
                    Thread.sleep(5L);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
    }

    protected void sendRelayRegister() {
        if (relayTarget != null && roomTag != null) {
            byte[] reg = NLTPWire.wrapRelayPacket(roomTag, null, NLTPWire.RELAY_VER_REGISTER);
            try {
                channel.send(ByteBuffer.wrap(reg), relayTarget);
                lastRelayRegS = NLTPEndpoint.monoNowS();
            } catch (IOException ignored) {
            }
        }
    }

    protected void rawSendTo(byte[] pkt, InetSocketAddress peer) {
        if (lossSim > 0.0 && lossRng.nextDouble() < lossSim) {
            return;
        }
        try {
            if (relayTarget != null && roomTag != null) {
                byte[] wrapped = NLTPWire.wrapRelayPacket(roomTag, pkt, NLTPWire.RELAY_VER_DATA);
                channel.send(ByteBuffer.wrap(wrapped), relayTarget);
            } else if (peer != null) {
                channel.send(ByteBuffer.wrap(pkt), peer);
            }
        } catch (IOException ignored) {
        }
    }

    private void ioLoop() {
        ByteBuffer rxBuf = ByteBuffer.allocateDirect(65535);
        byte[] tmp = new byte[65535];

        if (relayTarget != null) {
            lock.lock();
            try {
                sendRelayRegister();
            } finally {
                lock.unlock();
            }
        }

        while (running) {
            boolean gotPkt = false;
            lock.lock();
            try {
                double now = NLTPEndpoint.monoNowS();
                if (relayTarget != null) {
                    double regInterval = relayRegistered ? Math.max(5.0, engine.keepaliveS()) : 0.25;
                    if ((now - lastRelayRegS) >= regInterval) {
                        sendRelayRegister();
                    }
                }

                for (int i = 0; i < 128; i++) {
                    rxBuf.clear();
                    SocketAddress sa;
                    try {
                        sa = channel.receive(rxBuf);
                    } catch (IOException e) {
                        if (!running) return;
                        break;
                    }
                    if (sa == null) {
                        break;
                    }
                    gotPkt = true;
                    rxBuf.flip();
                    int n = rxBuf.remaining();
                    rxBuf.get(tmp, 0, n);

                    if (NLTPWire.isStunPacket(tmp, n)) {
                        stunInbox.offer(Arrays.copyOf(tmp, n));
                        continue;
                    }
                    if (!(sa instanceof InetSocketAddress)) {
                        continue;
                    }
                    InetSocketAddress peer = (InetSocketAddress) sa;

                    if (roomTag != null) {
                        NLTPWire.RelayFrame rf = NLTPWire.unwrapRelayPacket(tmp, n);
                        if (rf == null || !Arrays.equals(rf.roomTag, roomTag)) {
                            continue;
                        }
                        if (rf.cmd == NLTPWire.RELAY_VER_REG_ACK) {
                            relayRegistered = true;
                            continue;
                        }
                        if (rf.cmd == NLTPWire.RELAY_VER_DATA && rf.inner.length > 0) {
                            if (lossSim > 0.0 && lossRng.nextDouble() < lossSim) continue;
                            engine.onDatagram(rf.inner, rf.inner.length, peer, NLTPEndpoint.monoNowS());
                        }
                        continue;
                    }

                    if (lossSim > 0.0 && lossRng.nextDouble() < lossSim) continue;
                    engine.onDatagram(tmp, n, peer, NLTPEndpoint.monoNowS());
                }

                engine.stepTx(NLTPEndpoint.monoNowS());
            } finally {
                lock.unlock();
            }

            if (!gotPkt && running) {
                try {
                    selector.select(1L);
                    selector.selectedKeys().clear();
                } catch (IOException ignored) {
                }
            }
        }
    }

    public InetSocketAddress localAddress() {
        try {
            return (InetSocketAddress) channel.getLocalAddress();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * Blocks until reflex {@code seq} is acknowledged; false on timeout (or with a peer older than v1.3.2).
     * Keep the socket open until then: closing it stops the retransmissions.
     */
    public boolean waitReflexAck(int seq, long timeoutMs) throws InterruptedException {
        long deadline = System.nanoTime() + timeoutMs * 1_000_000L;
        while (System.nanoTime() < deadline) {
            lock.lock();
            try {
                if (engine.reflexAcked(seq)) {
                    return true;
                }
            } finally {
                lock.unlock();
            }
            Thread.sleep(2L);
        }
        return false;
    }

    public NLTPEndpoint<InetSocketAddress> engine() {
        return engine;
    }

    public ReceivedItem<InetSocketAddress> recv(long timeoutMs) throws InterruptedException {
        return engine.inboxQueue().poll(timeoutMs, TimeUnit.MILLISECONDS);
    }

    protected boolean awaitTransfer(NLTPEndpoint.OutboundTransfer<InetSocketAddress> xfer, long timeoutMs) {
        try {
            return Boolean.TRUE.equals(xfer.doneFuture.get(timeoutMs, TimeUnit.MILLISECONDS));
        } catch (Exception e) {
            return false;
        }
    }

    public StunDiscoveryResult discoverWan(long timeoutMs) throws IOException, InterruptedException {
        List<String> queried = new ArrayList<>();
        List<NLTPWire.MappedEndpoint> mapped = new ArrayList<>();
        double firstRtt = 0.0;

        for (String[] srv : DEFAULT_STUN_SERVERS) {
            try {
                InetSocketAddress sa = resolveUdpAddress(srv[0], Integer.parseInt(srv[1]));
                byte[] tid = NLTPWire.randomBytes(12);
                byte[] req = NLTPWire.buildStunBindingRequest(tid);
                double t0 = NLTPEndpoint.monoNowS();
                lock.lock();
                try {
                    channel.send(ByteBuffer.wrap(req), sa);
                } finally {
                    lock.unlock();
                }
                double deadline = t0 + (timeoutMs / 1000.0);
                double nextResend = t0 + 0.5;
                while (NLTPEndpoint.monoNowS() < deadline) {
                    double now = NLTPEndpoint.monoNowS();
                    if (now >= nextResend) {
                        lock.lock();
                        try {
                            channel.send(ByteBuffer.wrap(req), sa);
                        } finally {
                            lock.unlock();
                        }
                        nextResend = now + 0.5;
                    }
                    long waitMs = Math.max(10L, (long) ((Math.min(deadline, nextResend) - NLTPEndpoint.monoNowS()) * 1000.0));
                    byte[] raw = stunInbox.poll(waitMs, TimeUnit.MILLISECONDS);
                    if (raw == null) continue;
                    NLTPWire.MappedEndpoint ans = NLTPWire.parseStunBindingResponse(raw, raw.length, tid);
                    if (ans != null) {
                        double rtt = (NLTPEndpoint.monoNowS() - t0) * 1000.0;
                        if (mapped.isEmpty()) firstRtt = rtt;
                        queried.add(srv[0] + ":" + srv[1]);
                        mapped.add(ans);
                        break;
                    }
                }
                if (mapped.size() >= 2) break;
            } catch (IOException ignored) {
            }
        }

        if (mapped.isEmpty()) {
            throw new IOException("No STUN response received on NLTP socket — outbound UDP may be blocked");
        }

        NLTPWire.MappedEndpoint first = mapped.get(0);
        int localPort = localAddress().getPort();
        boolean eim = true;
        for (NLTPWire.MappedEndpoint m : mapped) {
            if (m.port != first.port) {
                eim = false;
                break;
            }
        }
        String summary = (mapped.size() >= 2)
                ? (eim
                    ? "Endpoint-Independent NAT (Direct P2P UDP Hole-Punching Supported)"
                    : "Symmetric CGNAT Detected (Use `NLTPRelayHub` `--relay` Mode)")
                : ("Single STUN server reachable (" + queried.get(0) + "); WAN mapping verified");

        return new StunDiscoveryResult(first.ip, first.port, localPort, firstRtt, eim, summary, queried);
    }

    public boolean punchP2p(InetSocketAddress peerWanAddr, long timeoutMs) throws InterruptedException {
        int basePongs;
        lock.lock();
        try {
            basePongs = engine.pongsRx();
            engine.registerKeepalivePeer(peerWanAddr);
        } finally {
            lock.unlock();
        }
        double deadline = NLTPEndpoint.monoNowS() + (timeoutMs / 1000.0);
        while (NLTPEndpoint.monoNowS() < deadline) {
            lock.lock();
            try {
                if (engine.pongsRx() > basePongs || engine.hasPongFrom(peerWanAddr)) {
                    return true;
                }
                engine.sendPing(peerWanAddr, NLTPEndpoint.monoNowS());
            } finally {
                lock.unlock();
            }
            if (selector != null) selector.wakeup();
            Thread.sleep(55L);
        }
        lock.lock();
        try {
            return engine.pongsRx() > basePongs || engine.hasPongFrom(peerWanAddr);
        } finally {
            lock.unlock();
        }
    }

    @Override
    public void close() {
        running = false;
        if (selector != null) {
            selector.wakeup();
        }
        if (workerThread != null) {
            try {
                workerThread.join(1000L);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        try {
            if (selector != null) selector.close();
        } catch (IOException ignored) {
        }
        try {
            if (channel != null) channel.close();
        } catch (IOException ignored) {
        }
    }
}
