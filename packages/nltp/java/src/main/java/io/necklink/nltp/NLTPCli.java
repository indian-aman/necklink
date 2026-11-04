package io.necklink.nltp;

import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.Locale;

/**
 * NLTP v1.2 Java CLI ({@code java -jar nltp-1.2.0.jar ...}).
 *
 * Supports both user commands ({@code gen-key}, {@code stun}, {@code relay}, {@code serve}, {@code send-file})
 * and cross-language interop driver commands ({@code send}, {@code recv}) matching {@code nltp_cli}.
 */
public final class NLTPCli {
    private NLTPCli() {}

    private static String getOpt(String[] args, String flag, String def) {
        for (int i = 0; i + 1 < args.length; i++) {
            if (flag.equals(args[i])) {
                return args[i + 1];
            }
        }
        return def;
    }

    private static boolean hasFlag(String[] args, String flag) {
        return Arrays.asList(args).contains(flag);
    }

    private static InetSocketAddress parseRelay(String spec) throws Exception {
        if (spec == null || spec.isEmpty()) return null;
        int idx = spec.lastIndexOf(':');
        if (idx < 0) throw new IllegalArgumentException("Expected HOST:PORT, got " + spec);
        String h = spec.substring(0, idx);
        int p = Integer.parseInt(spec.substring(idx + 1));
        return NLTPSocketBase.resolveUdpAddress(h, p);
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0 || "--help".equals(args[0]) || "-h".equals(args[0])) {
            System.out.println("NLTP v1.2 Java CLI\n"
                    + "Commands:\n"
                    + "  gen-key\n"
                    + "  stun [--show-ip]\n"
                    + "  relay [--host 0.0.0.0] [--port 9000] [--duration 0]\n"
                    + "  serve [--host 0.0.0.0] [--port 9000] [--psk KEY] [--save-dir ./inbox] [--relay IP:PORT --room NAME] [--max-items N]\n"
                    + "  send-file --file PATH [--host 127.0.0.1] [--port 9000] [--psk KEY] [--relay IP:PORT --room NAME]\n"
                    + "  send <host> <port> <file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]\n"
                    + "  recv <bind_ip> <port> <out_file> [--loss PCT] [--psk KEY] [--relay IP:PORT --room NAME]");
            return;
        }

        String cmd = args[0];
        if ("gen-key".equals(cmd)) {
            System.out.println(NLTPWire.generateNltpKey());
            return;
        }

        if ("stun".equals(cmd)) {
            boolean showIp = hasFlag(args, "--show-ip");
            try (NLTPServer srv = NLTPServer.builder().bind("0.0.0.0", 0).keepaliveS(0).build()) {
                StunDiscoveryResult res = srv.discoverWan(2500L);
                System.out.println(res.toJson(showIp));
            }
            return;
        }

        if ("relay".equals(cmd)) {
            String host = getOpt(args, "--host", "0.0.0.0");
            int port = Integer.parseInt(getOpt(args, "--port", "9000"));
            double duration = Double.parseDouble(getOpt(args, "--duration", "0"));
            try (NLTPRelayHub hub = new NLTPRelayHub(host, port)) {
                InetSocketAddress la = hub.localAddress();
                System.out.println("NLTPRelayHub listening on " + la.getAddress().getHostAddress() + ":" + la.getPort());
                System.out.flush();
                if (duration > 0.0) {
                    Thread.sleep((long) (duration * 1000.0));
                } else {
                    Thread.sleep(Long.MAX_VALUE);
                }
            }
            return;
        }

        if ("serve".equals(cmd)) {
            String host = getOpt(args, "--host", "0.0.0.0");
            int port = Integer.parseInt(getOpt(args, "--port", "9000"));
            String psk = getOpt(args, "--psk", null);
            Path saveDir = Paths.get(getOpt(args, "--save-dir", "./inbox"));
            InetSocketAddress relay = parseRelay(getOpt(args, "--relay", null));
            String room = getOpt(args, "--room", null);
            double loss = Double.parseDouble(getOpt(args, "--loss", "0"));
            int maxItems = Integer.parseInt(getOpt(args, "--max-items", "0"));

            try (NLTPServer srv = NLTPServer.builder()
                    .bind(host, port)
                    .psk(psk)
                    .saveDir(saveDir)
                    .relay(relay, room)
                    .lossDropSim(loss)
                    .build()) {
                System.out.println("NLTP Java Server listening on " + srv.localAddress());
                int count = 0;
                while (true) {
                    ReceivedItem<InetSocketAddress> item = srv.recv(1000L);
                    if (item != null) {
                        count++;
                        System.out.println("RECEIVED kind=" + item.kind()
                                + " bytes=" + item.byteLength()
                                + " sha256_ok=" + item.sha256Verified()
                                + " file=" + item.filename());
                        if (maxItems > 0 && count >= maxItems) break;
                    }
                }
            }
            return;
        }

        if ("send-file".equals(cmd)) {
            String host = getOpt(args, "--host", "127.0.0.1");
            int port = Integer.parseInt(getOpt(args, "--port", "9000"));
            String fileStr = getOpt(args, "--file", null);
            if (fileStr == null) throw new IllegalArgumentException("--file is required");
            String psk = getOpt(args, "--psk", null);
            InetSocketAddress relay = parseRelay(getOpt(args, "--relay", null));
            String room = getOpt(args, "--room", null);
            double loss = Double.parseDouble(getOpt(args, "--loss", "0"));

            try (NLTPClient cli = NLTPClient.builder()
                    .server(host, port)
                    .psk(psk)
                    .relay(relay, room)
                    .lossDropSim(loss)
                    .build()) {
                TransferReceipt r = cli.sendFile(Paths.get(fileStr), 90000L);
                System.out.println(r.toJson());
                System.exit(r.verifiedSha256() ? 0 : 1);
            }
            return;
        }

        if ("send-reflex".equals(cmd)) {
            String host = args[1];
            int port = Integer.parseInt(args[2]);
            String text = args.length > 3 ? args[3] : "ESTOP";
            String psk = getOpt(args, "--psk", null);
            try (NLTPClient cli = NLTPClient.builder().server(host, port).psk(psk).build()) {
                cli.sendReflex(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                Thread.sleep(30L);
                System.out.println("RESULT {\"ok\":true}");
            }
            return;
        }

        if ("send".equals(cmd)) {
            String host = args[1];
            int port = Integer.parseInt(args[2]);
            Path file = Paths.get(args[3]);
            double lossPct = Double.parseDouble(getOpt(args, "--loss", "0"));
            String psk = getOpt(args, "--psk", null);
            InetSocketAddress relay = parseRelay(getOpt(args, "--relay", null));
            String room = getOpt(args, "--room", null);
            double maxKbps = Double.parseDouble(getOpt(args, "--max-kbps", "50000"));

            try (NLTPClient cli = NLTPClient.builder()
                    .server(host, port)
                    .psk(psk)
                    .relay(relay, room)
                    .maxTxKbps(maxKbps)
                    .lossDropSim(lossPct / 100.0)
                    .build()) {
                TransferReceipt r = cli.sendFile(file, 90000L);
                System.out.println("RESULT " + r.toJson());
                System.exit(r.verifiedSha256() ? 0 : 1);
            }
            return;
        }

        if ("recv".equals(cmd)) {
            String bindHost = args[1];
            int bindPort = Integer.parseInt(args[2]);
            Path outFile = Paths.get(args[3]);
            double lossPct = Double.parseDouble(getOpt(args, "--loss", "0"));
            String psk = getOpt(args, "--psk", null);
            InetSocketAddress relay = parseRelay(getOpt(args, "--relay", null));
            String room = getOpt(args, "--room", null);
            double maxKbps = Double.parseDouble(getOpt(args, "--max-kbps", "50000"));
            boolean waitStdin = hasFlag(args, "--wait-stdin");

            try (NLTPServer srv = NLTPServer.builder()
                    .bind(bindHost, bindPort)
                    .psk(psk)
                    .relay(relay, room)
                    .maxTxKbps(maxKbps)
                    .lossDropSim(lossPct / 100.0)
                    .build()) {
                System.out.println("READY");
                System.out.flush();
                double t0 = NLTPEndpoint.monoNowS();
                ReceivedItem<InetSocketAddress> item = srv.recv(90000L);
                if (item == null || !item.sha256Verified()) {
                    System.out.println("RESULT {\"ok\":false}");
                    System.exit(1);
                }
                if (outFile.getParent() != null) {
                    Files.createDirectories(outFile.getParent());
                }
                Files.write(outFile, item.data());
                double elapsedS = NLTPEndpoint.monoNowS() - t0;
                if (waitStdin) {
                    while (System.in.read() != -1) {
                        // Keep server socket alive answering retransmitted FIN_ACKs until caller closes stdin
                    }
                } else {
                    Thread.sleep(250L);
                }
                System.out.println(String.format(
                        Locale.US,
                        "RESULT {\"ok\":true,\"bytes\":%d,\"sha256\":\"%s\",\"elapsed_s\":%.4f}",
                        item.byteLength(),
                        item.sha256Hex(),
                        elapsedS
                ));
                System.exit(0);
            }
            return;
        }

        throw new IllegalArgumentException("Unknown command: " + cmd);
    }
}
