package io.necklink.nltp;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.DatagramChannel;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Stateless zero-knowledge 1-port CGNAT cloud relay ({@code NLTPRelayHub}).
 * Pairs devices behind strict 4G/5G Symmetric CGNATs under a 16-byte {@code roomTag}
 * and forwards {@code ChaCha20-Poly1305} ciphertext with zero decryption keys.
 */
public final class NLTPRelayHub implements AutoCloseable {
    private final DatagramChannel channel;
    private final Selector selector;
    private final double peerTtlS;
    private final Map<String, Map<InetSocketAddress, Double>> rooms = new HashMap<>();
    private final AtomicLong forwardedPackets = new AtomicLong();
    private final AtomicLong forwardedBytes = new AtomicLong();
    private volatile boolean running = true;
    private final Thread worker;

    public NLTPRelayHub(String bindHost, int bindPort) throws IOException {
        this(bindHost, bindPort, 90.0);
    }

    public NLTPRelayHub(String bindHost, int bindPort, double peerTtlS) throws IOException {
        InetSocketAddress bindSa = NLTPSocketBase.resolveUdpAddress(bindHost, bindPort);
        this.channel = NLTPSocketBase.openUdpChannel(bindSa);
        this.selector = Selector.open();
        this.channel.register(this.selector, SelectionKey.OP_READ);
        this.peerTtlS = peerTtlS;
        this.worker = new Thread(this::loop, "nltp-java-relay-hub");
        this.worker.setDaemon(true);
        this.worker.start();
    }

    public InetSocketAddress localAddress() {
        try {
            return (InetSocketAddress) channel.getLocalAddress();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    public long forwardedPackets() { return forwardedPackets.get(); }
    public long forwardedBytes() { return forwardedBytes.get(); }
    public long relayedPackets() { return forwardedPackets.get(); }
    public long relayedBytes() { return forwardedBytes.get(); }

    private void loop() {
        ByteBuffer buf = ByteBuffer.allocateDirect(65535);
        byte[] tmp = new byte[65535];
        double lastGc = NLTPEndpoint.monoNowS();

        while (running) {
            try {
                selector.select(50L);
                selector.selectedKeys().clear();
            } catch (IOException e) {
                if (!running) break;
            }

            double now = NLTPEndpoint.monoNowS();
            if (now - lastGc > 15.0) {
                gc(now);
                lastGc = now;
            }

            while (running) {
                buf.clear();
                SocketAddress sa;
                try {
                    sa = channel.receive(buf);
                } catch (IOException e) {
                    break;
                }
                if (sa == null) break;
                if (!(sa instanceof InetSocketAddress)) continue;
                InetSocketAddress sender = (InetSocketAddress) sa;

                buf.flip();
                int n = buf.remaining();
                buf.get(tmp, 0, n);

                NLTPWire.RelayFrame rf = NLTPWire.unwrapRelayPacket(tmp, n);
                if (rf == null) continue;
                String roomHex = NLTPWire.bytesToHex(rf.roomTag);
                double t = NLTPEndpoint.monoNowS();
                Map<InetSocketAddress, Double> members = rooms.computeIfAbsent(roomHex, k -> new HashMap<>());
                members.put(sender, t);

                if (rf.cmd == NLTPWire.RELAY_VER_REGISTER) {
                    byte[] ack = NLTPWire.wrapRelayPacket(rf.roomTag, null, NLTPWire.RELAY_VER_REG_ACK);
                    try {
                        channel.send(ByteBuffer.wrap(ack), sender);
                    } catch (IOException ignored) {
                    }
                    continue;
                }

                if (rf.cmd == NLTPWire.RELAY_VER_DATA && rf.inner.length > 0) {
                    byte[] fwd = NLTPWire.wrapRelayPacket(rf.roomTag, rf.inner, NLTPWire.RELAY_VER_DATA);
                    for (Map.Entry<InetSocketAddress, Double> e : new ArrayList<>(members.entrySet())) {
                        InetSocketAddress peer = e.getKey();
                        if (peer.equals(sender)) continue;
                        if (t - e.getValue() > peerTtlS) {
                            members.remove(peer);
                            continue;
                        }
                        try {
                            channel.send(ByteBuffer.wrap(fwd), peer);
                            forwardedPackets.incrementAndGet();
                            forwardedBytes.addAndGet(fwd.length);
                        } catch (IOException ignored) {
                        }
                    }
                }
            }
        }
    }

    private void gc(double now) {
        for (String roomHex : new ArrayList<>(rooms.keySet())) {
            Map<InetSocketAddress, Double> members = rooms.get(roomHex);
            for (Map.Entry<InetSocketAddress, Double> e : new ArrayList<>(members.entrySet())) {
                if (now - e.getValue() > peerTtlS) {
                    members.remove(e.getKey());
                }
            }
            if (members.isEmpty()) {
                rooms.remove(roomHex);
            }
        }
    }

    @Override
    public void close() {
        running = false;
        selector.wakeup();
        try {
            worker.join(1000L);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        try {
            selector.close();
        } catch (IOException ignored) {
        }
        try {
            channel.close();
        } catch (IOException ignored) {
        }
    }
}
