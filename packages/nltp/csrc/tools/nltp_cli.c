/*
 * nltp_cli.c — POSIX test driver for nltp_core + nltp_net (Linux/WSL), used for interop and NAT tests.
 *
 *   nltp_cli send <server_ip> <port> <file> [options]
 *   nltp_cli recv <bind_ip> <port> <out_file> [options]
 *
 * Options:
 *   --loss PCT          drop that percentage of datagrams in each direction (exercises repairs)
 *   --max-kbps N        pacing ceiling (default 50000); --max-bytes N receive buffer size
 *   --psk KEY           ChaCha20-Poly1305 encryption (same key/passphrase rules as Python psk=)
 *   --relay IP:PORT --room NAME   go through an NLTPRelayHub (send: <server_ip> <port> are ignored)
 *   --stun IP:PORT      learn this socket's public address first; prints "WAN ip:port"
 *   --punch             after --stun, read "PEER ip:port" from stdin and hole-punch to it (both sides);
 *                       with --relay/--room as well, falls back to the relay if no PONG within 3 s
 *   --punch-ttl N       IP TTL of the first punch pings (default 2, 0 = off): opens our NAT only
 *   --keepalive-ms N    send a PING after N ms without traffic (keeps NAT mappings open)
 *   --count N           send: send the file N times; recv: receive N files
 *   --idle-ms N         send: stay idle N ms between sends
 *   --bind-port N       send: local UDP port (default: any)
 * Prints "RESULT {...}". Build:
 *   gcc -O2 -std=c99 -I csrc -o nltp_cli csrc/tools/nltp_cli.c csrc/nltp_core.c csrc/nltp_net.c csrc/nltp_sha256.c
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "nltp_core.h"
#include "nltp_net.h"

static int g_sock = -1;
static struct sockaddr_in g_peer;
static int g_have_peer = 0;
static unsigned g_loss_pct = 0;
static nltp_endpoint_t g_ep;  /* large (receive slots): keep it off the stack */
static const char *g_psk = NULL;

/* relay mode */
static int g_relay = 0;
static struct sockaddr_in g_relay_addr;
static uint8_t g_room_tag[16];
static int g_relay_registered = 0;
static double g_last_register = -1.0;

/* stun */
static uint8_t g_stun_tid[12];
static int g_stun_done = 0;
static struct sockaddr_in g_wan;

static uint32_t now_ms_cb(void *user) {
    struct timespec ts;
    (void)user;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u);
}

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int dropped(void) { return g_loss_pct && (unsigned)(rand() % 100) < g_loss_pct; }

static int parse_addr(const char *s, struct sockaddr_in *out) {
    char host[64];
    const char *colon = strrchr(s, ':');
    size_t n;
    if (!colon || (n = (size_t)(colon - s)) >= sizeof(host)) return 0;
    memcpy(host, s, n);
    host[n] = '\0';
    memset(out, 0, sizeof(*out));
    out->sin_family = AF_INET;
    out->sin_port = htons((unsigned short)atoi(colon + 1));
    return inet_pton(AF_INET, host, &out->sin_addr) == 1;
}

static void send_relay(uint8_t cmd, const uint8_t *inner, size_t len) {
    uint8_t frame[NLTP_PKT_MAX + 128];
    size_t n = nltp_relay_wrap(frame, sizeof(frame), g_room_tag, cmd, inner, len);
    if (n) sendto(g_sock, frame, n, 0, (struct sockaddr *)&g_relay_addr, sizeof(g_relay_addr));
}

static void send_cb(void *user, const uint8_t *pkt, size_t len) {
    (void)user;
    if (dropped()) return;
    if (g_relay) {
        send_relay(NLTP_RELAY_CMD_DATA, pkt, len);
    } else if (g_have_peer) {
        sendto(g_sock, pkt, len, 0, (struct sockaddr *)&g_peer, sizeof(g_peer));
    }
}

static void handle_datagram(const uint8_t *buf, size_t n, const struct sockaddr_in *from) {
    if (nltp_is_stun(buf, n)) {
        uint32_t ip;
        uint16_t port;
        if (!g_stun_done && nltp_stun_parse_response(buf, n, g_stun_tid, &ip, &port)) {
            memset(&g_wan, 0, sizeof(g_wan));
            g_wan.sin_family = AF_INET;
            g_wan.sin_addr.s_addr = ip;
            g_wan.sin_port = port;
            g_stun_done = 1;
        }
        return;
    }
    if (g_relay) {
        uint8_t cmd;
        const uint8_t *inner;
        size_t inner_len;
        if (!nltp_relay_unwrap(buf, n, g_room_tag, &cmd, &inner, &inner_len)) return;
        if (cmd == NLTP_RELAY_CMD_REG_ACK) {
            g_relay_registered = 1;
        } else if (cmd == NLTP_RELAY_CMD_DATA && inner_len && !dropped()) {
            nltp_on_datagram(&g_ep, inner, inner_len);
        }
        return;
    }
    if (!dropped()) {
        /* Replies made while processing go to the sender; keep its address only if the frame passed
           CRC / authentication, so forged packets cannot redirect us (roaming and NAT rebinding still work). */
        uint32_t valid0 = g_ep.valid_rx;
        struct sockaddr_in prev = g_peer;
        int had = g_have_peer;
        g_peer = *from;
        g_have_peer = 1;
        nltp_on_datagram(&g_ep, buf, n);
        if (g_ep.valid_rx == valid0) {
            g_peer = prev;
            g_have_peer = had;
        }
    }
}

static void pump(int wait_ms) {
    fd_set rf;
    struct timeval tv = {0, wait_ms * 1000};
    if (g_relay) {
        double t = now_s(), every = g_relay_registered ? NLTP_RELAY_REFRESH_MS / 1000.0 : NLTP_RELAY_REREGISTER_MS / 1000.0;
        if (g_last_register < 0 || t - g_last_register >= every) {
            send_relay(NLTP_RELAY_CMD_REGISTER, NULL, 0);
            g_last_register = t;
        }
    }
    FD_ZERO(&rf);
    FD_SET(g_sock, &rf);
    if (select(g_sock + 1, &rf, NULL, NULL, &tv) > 0) {
        uint8_t buf[2048];
        for (;;) {
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            ssize_t n = recvfrom(g_sock, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
            if (n <= 0) break;
            handle_datagram(buf, (size_t)n, &from);
        }
    }
    nltp_poll(&g_ep);
}

static int open_udp(const char *bind_ip, int bind_port) {
    struct sockaddr_in a;
    int big = 4 << 20;
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    setsockopt(s, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
    setsockopt(s, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
    fcntl(s, F_SETFL, O_NONBLOCK);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)bind_port);
    inet_pton(AF_INET, bind_ip, &a.sin_addr);
    bind(s, (struct sockaddr *)&a, sizeof(a));
    return s;
}

/* Queries the STUN server from the NLTP socket; prints "WAN ip:port". */
static int do_stun(const char *server) {
    struct sockaddr_in srv;
    uint8_t req[NLTP_STUN_REQUEST_BYTES];
    double t0 = now_s(), last = -1.0;
    size_t i;
    char ip[INET_ADDRSTRLEN];
    if (!parse_addr(server, &srv)) return 0;
    for (i = 0; i < sizeof(g_stun_tid); ++i) g_stun_tid[i] = (uint8_t)rand();
    nltp_stun_build_request(req, g_stun_tid);
    while (!g_stun_done && now_s() - t0 < 5.0) {
        if (last < 0 || now_s() - last > 0.5) {
            sendto(g_sock, req, sizeof(req), 0, (struct sockaddr *)&srv, sizeof(srv));
            last = now_s();
        }
        pump(10);
    }
    if (!g_stun_done) return 0;
    inet_ntop(AF_INET, &g_wan.sin_addr, ip, sizeof(ip));
    printf("WAN %s:%u\n", ip, (unsigned)ntohs(g_wan.sin_port));
    return 1;
}

static int g_punch_ttl = 2;
static uint8_t *g_rx_buf;

/* Reads "PEER ip:port" from stdin and pings it until a PONG arrives. The first pings use a low IP TTL:
   they open our own NAT mapping but expire before the peer's NAT, so they cannot leave an unanswered
   inbound entry there that would make the peer's NAT re-map its port (a race seen with Linux routers). */
static int do_punch(double *punch_ms) {
    char line[128];
    double t0, last = -1.0;
    uint32_t pongs0 = g_ep.pongs_rx;
    int k;
    if (!fgets(line, sizeof(line), stdin) || strncmp(line, "PEER ", 5) != 0) return 0;
    line[strcspn(line, "\r\n")] = '\0';
    if (!parse_addr(line + 5, &g_peer)) return 0;
    g_have_peer = 1;
    t0 = now_s();
    if (g_punch_ttl > 0) {
        int old_ttl = 64, ttl = g_punch_ttl;
        socklen_t ol = sizeof(old_ttl);
        getsockopt(g_sock, IPPROTO_IP, IP_TTL, &old_ttl, &ol);
        setsockopt(g_sock, IPPROTO_IP, IP_TTL, &ttl, sizeof(ttl));
        for (k = 0; k < 3; ++k) {
            nltp_send_ping(&g_ep);
            pump(30);
        }
        setsockopt(g_sock, IPPROTO_IP, IP_TTL, &old_ttl, sizeof(old_ttl));
        while (now_s() - t0 < 0.39) pump(10);
    }
    while (g_ep.pongs_rx == pongs0 && now_s() - t0 < 3.0) {
        if (last < 0 || now_s() - last > 0.05) {
            nltp_send_ping(&g_ep);
            last = now_s();
        }
        pump(5);
    }
    *punch_ms = (now_s() - t0) * 1000.0;
    return g_ep.pongs_rx != pongs0;
}

static unsigned parse_loss_pct(const char *v) {
    double d = atof(v);
    if (strchr(v, '.') && d > 0.0 && d < 1.0) return (unsigned)(d * 100.0 + 0.5);
    return (unsigned)atoi(v);
}

int main(int argc, char **argv) {
    uint32_t max_kbps = 50000, max_bytes = 16u << 20, keepalive_ms = 0;
    int i, count = 1, idle_ms = 0, punch = 0, bind_port = 0, sending, compress_lz4 = 0;
    const char *relay = NULL, *room = NULL, *stun = NULL;
    double punch_ms = 0;
    {
        struct timespec ts_seed;
        clock_gettime(CLOCK_MONOTONIC, &ts_seed);
        srand((unsigned)getpid() ^ (unsigned)time(NULL) ^ (unsigned)ts_seed.tv_nsec);
    }
    setvbuf(stdout, NULL, _IOLBF, 0);

    if (argc >= 2 && !strcmp(argv[1], "serve")) {
        const char *bind_ip = "127.0.0.1";
        int port = 0, max_items = 0, got = 0;
        uint32_t last_sid = 0, last_cid = 0;
        double t_start, t_done;
        for (i = 2; i < argc; ++i) {
            const char *v = i + 1 < argc ? argv[i + 1] : "";
            if (!strcmp(argv[i], "--host")) bind_ip = v, ++i;
            else if (!strcmp(argv[i], "--port")) port = atoi(v), ++i;
            else if (!strcmp(argv[i], "--psk")) g_psk = v, ++i;
            else if (!strcmp(argv[i], "--max-items")) max_items = atoi(v), ++i;
            else if (!strcmp(argv[i], "--loss")) g_loss_pct = parse_loss_pct(v), ++i;
            else if (!strcmp(argv[i], "--max-kbps")) max_kbps = (uint32_t)atoi(v), ++i;
            else if (!strcmp(argv[i], "--max-bytes")) max_bytes = (uint32_t)atoi(v), ++i;
        }
        if (port <= 0) return 2;
        g_sock = open_udp(bind_ip, port);
        nltp_init(&g_ep, ((uint32_t)rand() << 1) | 1u, max_kbps, send_cb, now_ms_cb, NULL);
        g_rx_buf = malloc(max_bytes);
        if (!g_rx_buf) return 2;
        nltp_set_rx_buffer(&g_ep, g_rx_buf, max_bytes);
        if (g_psk && g_psk[0]) {
            uint8_t key[32];
            nltp_key_from_psk(g_psk, key);
            nltp_set_key(&g_ep, key);
        }
        printf("NLTP C99 server listening on %s:%d\n", bind_ip, port);
        t_start = now_s();
        while ((max_items <= 0 || got < max_items) && now_s() - t_start < 120.0) {
            uint8_t rbuf[NLTP_REFLEX_MAX_LEN];
            uint16_t rlen = 0;
            uint32_t rcid = 0;
            pump(2);
            while (nltp_pop_reflex(&g_ep, rbuf, sizeof(rbuf), &rlen, &rcid)) {
                got++;
                printf("RECEIVED kind=reflex bytes=%u sha256_ok=true\n", (unsigned)rlen);
                if (max_items > 0 && got >= max_items) break;
            }
            if ((max_items <= 0 || got < max_items) && nltp_rx_done(&g_ep) &&
                (g_ep.rx.stream_id != last_sid || g_ep.rx.peer_cid != last_cid)) {
                last_sid = g_ep.rx.stream_id;
                last_cid = g_ep.rx.peer_cid;
                got++;
                printf("RECEIVED kind=file bytes=%u sha256_ok=%s\n",
                       (unsigned)nltp_rx_len(&g_ep), nltp_rx_sha_ok(&g_ep) ? "true" : "false");
            }
        }
        t_done = now_s();
        while (now_s() - t_done < 1.5) pump(2);
        printf("RESULT {\"ok\": %s, \"items\": %d}\n",
               (max_items <= 0 || got >= max_items) ? "true" : "false", got);
        return (max_items <= 0 || got >= max_items) ? 0 : 1;
    }

    if (argc >= 5 && !strcmp(argv[1], "send-reflex")) {
        const char *msg = argv[4];
        size_t mlen = strlen(msg);
        uint32_t seq;
        double t0, el;
        int acked = 0;
        if (mlen > NLTP_REFLEX_MAX_LEN) return 2;
        for (i = 5; i < argc; ++i) {
            const char *v = i + 1 < argc ? argv[i + 1] : "";
            if (!strcmp(argv[i], "--loss")) g_loss_pct = parse_loss_pct(v), ++i;
            else if (!strcmp(argv[i], "--psk")) g_psk = v, ++i;
            else if (!strcmp(argv[i], "--bind-port")) bind_port = atoi(v), ++i;
        }
        g_sock = open_udp("0.0.0.0", bind_port);
        nltp_init(&g_ep, ((uint32_t)rand() << 1) | 1u, max_kbps, send_cb, now_ms_cb, NULL);
        if (g_psk && g_psk[0]) {
            uint8_t key[32];
            nltp_key_from_psk(g_psk, key);
            nltp_set_key(&g_ep, key);
        }
        memset(&g_peer, 0, sizeof(g_peer));
        g_peer.sin_family = AF_INET;
        g_peer.sin_port = htons((unsigned short)atoi(argv[3]));
        inet_pton(AF_INET, argv[2], &g_peer.sin_addr);
        g_have_peer = 1;
        t0 = now_s();
        seq = nltp_send_reflex(&g_ep, (const uint8_t *)msg, (uint16_t)mlen);
        while (seq != 0u && !nltp_reflex_acked(&g_ep, seq) && now_s() - t0 < 10.0) {
            pump(2);
        }
        acked = seq != 0u && nltp_reflex_acked(&g_ep, seq);
        el = now_s() - t0;
        printf("RESULT {\"ok\": %s, \"acked\": %s, \"seq\": %u, \"elapsed_s\": %.6f}\n",
               acked ? "true" : "false", acked ? "true" : "false", (unsigned)seq, el);
        return acked ? 0 : 1;
    }

    if (argc < 5) {
        fprintf(stderr, "usage: %s send|recv|send-reflex|serve <ip> <port> <file|msg> [options]\n", argv[0]);
        return 2;
    }
    for (i = 5; i < argc; ++i) {
        const char *v = i + 1 < argc ? argv[i + 1] : "";
        if (!strcmp(argv[i], "--loss")) g_loss_pct = parse_loss_pct(v), ++i;
        else if (!strcmp(argv[i], "--max-kbps")) max_kbps = (uint32_t)atoi(v), ++i;
        else if (!strcmp(argv[i], "--max-bytes")) max_bytes = (uint32_t)atoi(v), ++i;
        else if (!strcmp(argv[i], "--psk")) g_psk = v, ++i;
        else if (!strcmp(argv[i], "--relay")) relay = v, ++i;
        else if (!strcmp(argv[i], "--room")) room = v, ++i;
        else if (!strcmp(argv[i], "--stun")) stun = v, ++i;
        else if (!strcmp(argv[i], "--keepalive-ms")) keepalive_ms = (uint32_t)atoi(v), ++i;
        else if (!strcmp(argv[i], "--count")) count = atoi(v), ++i;
        else if (!strcmp(argv[i], "--idle-ms")) idle_ms = atoi(v), ++i;
        else if (!strcmp(argv[i], "--bind-port")) bind_port = atoi(v), ++i;
        else if (!strcmp(argv[i], "--punch")) punch = 1;
        else if (!strcmp(argv[i], "--punch-ttl")) g_punch_ttl = atoi(v), ++i;
        else if (!strcmp(argv[i], "--compress") || !strcmp(argv[i], "-z")) compress_lz4 = 1;
    }
    sending = !strcmp(argv[1], "send");

    g_sock = sending ? open_udp("0.0.0.0", bind_port) : open_udp(argv[2], atoi(argv[3]));
    nltp_init(&g_ep, (uint32_t)rand(), max_kbps, send_cb, now_ms_cb, NULL);
    g_ep.keepalive_ms = keepalive_ms;
    if (!sending) {
        /* Before any packet is processed: the sender may finish punching first and start streaming
           while we are still waiting for our own PONG. */
        g_rx_buf = malloc(max_bytes);
        if (!g_rx_buf) return 2;
        nltp_set_rx_buffer(&g_ep, g_rx_buf, max_bytes);
    }
    if (g_psk) {
        uint8_t key[32];
        nltp_key_from_psk(g_psk, key);
        nltp_set_key(&g_ep, key);
    }
    if (relay) {
        if (!room || !parse_addr(relay, &g_relay_addr)) return 2;
        nltp_room_tag(room, g_room_tag);
    }
    if (relay && !punch) {
        g_relay = 1;
        while (!g_relay_registered) pump(10);
        printf("RELAY registered\n");
    } else if (sending) {
        memset(&g_peer, 0, sizeof(g_peer));
        g_peer.sin_family = AF_INET;
        g_peer.sin_port = htons((unsigned short)atoi(argv[3]));
        inet_pton(AF_INET, argv[2], &g_peer.sin_addr);
        g_have_peer = 1;
    }
    if (stun && !do_stun(stun)) {
        printf("RESULT {\"ok\": false, \"error\": \"stun\"}\n");
        return 1;
    }
    if (punch) {
        int ok = do_punch(&punch_ms);
        printf("PUNCH %s ms=%.1f\n", ok ? "ok" : "failed", punch_ms);
        if (!ok && relay) {
            /* Direct path blocked (NAT port clash, symmetric NAT): continue through the relay */
            g_relay = 1;
            while (!g_relay_registered) pump(10);
            printf("FALLBACK relay\n");
        } else if (!ok) {
            printf("RESULT {\"ok\": false, \"error\": \"punch\"}\n");
            return 1;
        }
    } else if (!sending && !relay) {
        printf("READY\n");
    }

    if (sending) {
        FILE *f = fopen(argv[4], "rb");
        long n;
        uint8_t *data, *comp_scratch = NULL;
        uint32_t comp_cap = 0;
        int k, all_ok = 1;
        double t0 = 0, last_el = 0;
        const char *fname = strrchr(argv[4], '/') ? strrchr(argv[4], '/') + 1 : argv[4];
        if (!f) return 2;
        fseek(f, 0, SEEK_END);
        n = ftell(f);
        fseek(f, 0, SEEK_SET);
        data = malloc(n > 0 ? (size_t)n : 1u);
        if (n > 0 && fread(data, 1, (size_t)n, f) != (size_t)n) return 2;
        fclose(f);
        if (compress_lz4 && n > 0) {
            comp_cap = (uint32_t)n;
            comp_scratch = malloc(comp_cap);
        }
        for (k = 0; k < count; ++k) {
            if (k > 0 && idle_ms > 0) {
                double ti = now_s();
                while ((now_s() - ti) * 1000.0 < idle_ms) pump(10);
            }
            t0 = now_s();
            if (compress_lz4 && comp_scratch) {
                nltp_send_buffer_lz4(&g_ep, data, (uint32_t)n, comp_scratch, comp_cap, fname);
            } else {
                nltp_send_buffer(&g_ep, data, (uint32_t)n, fname);
            }
            while (!nltp_tx_done(&g_ep) && now_s() - t0 < 60.0) pump(1);
            last_el = now_s() - t0;
            if (!nltp_tx_verified(&g_ep)) all_ok = 0;
            printf("SENT %d ok=%d elapsed=%.3f\n", k + 1, nltp_tx_verified(&g_ep), last_el);
        }
        int was_compressed = (compress_lz4 && comp_scratch != NULL && g_ep.tx.data == comp_scratch);
        uint32_t wire_payload_bytes = g_ep.tx.len;
        free(comp_scratch);
        free(data);
        printf("RESULT {\"ok\": %s, \"bytes\": %ld, \"wire_payload_bytes\": %u, \"compressed\": %s, \"elapsed_s\": %.6f, \"pkts_tx\": %u, \"punch_ms\": %.1f}\n",
               all_ok ? "true" : "false", n, (unsigned)wire_payload_bytes, was_compressed ? "true" : "false", last_el, (unsigned)g_ep.pkts_tx, punch_ms);
        return 0;
    } else {
        uint8_t *buf = g_rx_buf;
        int got = 0, all_ok = 1;
        uint32_t last_sid = 0;
        double t_done = 0, t_start = now_s();
        while (got < count && now_s() - t_start < 120.0) {
            pump(1);
            if (nltp_rx_done(&g_ep) && g_ep.rx.stream_id != last_sid) {
                last_sid = g_ep.rx.stream_id;
                got++;
                if (!nltp_rx_sha_ok(&g_ep)) all_ok = 0;
                printf("RECEIVED %d ok=%d bytes=%u\n", got, nltp_rx_sha_ok(&g_ep), (unsigned)nltp_rx_len(&g_ep));
                if (got == count) {  /* written even on a SHA mismatch, so a test can see what went wrong */
                    FILE *f = fopen(argv[4], "wb");
                    if (f) {
                        fwrite(buf, 1, nltp_rx_len(&g_ep), f);
                        fclose(f);
                    }
                }
            }
        }
        t_done = now_s();
        while (now_s() - t_done < 1.5) pump(1);  /* keep answering the sender's final acks */
        printf("RESULT {\"ok\": %s, \"bytes\": %u, \"files\": %d}\n", (got == count && all_ok) ? "true" : "false",
               (unsigned)nltp_rx_len(&g_ep), got);
        return 0;
    }
}
