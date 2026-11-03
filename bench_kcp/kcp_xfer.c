/*
 * Minimal KCP file transfer for scripts/fair_transport_benchmark.py.
 *
 *   kcp_xfer server <bind_ip> <port> <expected_file> <mode>
 *   kcp_xfer client <server_ip> <port> <file> <mode>
 *
 * mode: "fast"    = KCP fast mode (nodelay=1, interval=10, resend=2, nc=1 -> no congestion control), window 1024
 *       "fast128" = same with the 128-packet window from the KCP README example
 *       "fast-cc" = fast mode with KCP congestion control enabled (nc=0)
 * The client sends an 8-byte length then the file; the server compares it byte-for-byte with
 * <expected_file> and answers one byte ('K' ok / 'X' mismatch). Both print
 * "RESULT {...}" like the Python roles. Build: gcc -O2 -o kcp_xfer kcp_xfer.c ikcp.c
 */
#include "ikcp.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define CONV 0x4e4c5450u
#define TIMEOUT_MS 90000u

static int g_sock = -1;
static struct sockaddr_in g_peer;
static int g_have_peer = 0;

static IUINT32 now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (IUINT32)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int udp_output(const char *buf, int len, ikcpcb *kcp, void *user) {
    (void)kcp; (void)user;
    if (!g_have_peer) return 0;
    sendto(g_sock, buf, (size_t)len, 0, (struct sockaddr *)&g_peer, sizeof(g_peer));
    return 0;
}

static unsigned char *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char *buf = malloc((size_t)n > 0 ? (size_t)n : 1);
    if (n > 0 && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); fclose(f); return NULL; }
    fclose(f);
    *len = (size_t)n;
    return buf;
}

static ikcpcb *make_kcp(const char *mode) {
    ikcpcb *kcp = ikcp_create(CONV, NULL);
    kcp->output = udp_output;
    if (strcmp(mode, "fast128") == 0) ikcp_wndsize(kcp, 128, 128);  /* KCP README example window */
    else ikcp_wndsize(kcp, 1024, 1024);
    if (strcmp(mode, "fast-cc") == 0) ikcp_nodelay(kcp, 1, 10, 2, 0);
    else ikcp_nodelay(kcp, 1, 10, 2, 1);
    return kcp;
}

/* Pump socket -> kcp and kcp timers for up to wait_ms. */
static void pump(ikcpcb *kcp, int wait_ms) {
    IUINT32 t = now_ms();
    ikcp_update(kcp, t);
    IUINT32 next = ikcp_check(kcp, t);
    int ms = (int)(next - t);
    if (ms > wait_ms) ms = wait_ms;
    if (ms < 0) ms = 0;
    fd_set rf;
    FD_ZERO(&rf);
    FD_SET(g_sock, &rf);
    struct timeval tv = { ms / 1000, (ms % 1000) * 1000 };
    if (select(g_sock + 1, &rf, NULL, NULL, &tv) > 0) {
        char buf[2048];
        for (;;) {
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            ssize_t n = recvfrom(g_sock, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
            if (n <= 0) break;
            if (!g_have_peer) { g_peer = from; g_have_peer = 1; }
            ikcp_input(kcp, buf, (long)n);
        }
    }
    ikcp_update(kcp, now_ms());
}

static int open_socket(const char *ip, int port, int bind_it) {
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    int big = 8 << 20;
    setsockopt(s, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
    setsockopt(s, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
    fcntl(s, F_SETFL, O_NONBLOCK);
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    inet_pton(AF_INET, ip, &a.sin_addr);
    if (bind_it) {
        bind(s, (struct sockaddr *)&a, sizeof(a));
    } else {
        g_peer = a;
        g_have_peer = 1;
    }
    return s;
}

static int run_server(const char *ip, int port, const char *expected_path, const char *mode) {
    size_t exp_len = 0;
    unsigned char *expected = read_file(expected_path, &exp_len);
    if (!expected) { fprintf(stderr, "cannot read %s\n", expected_path); return 2; }
    g_sock = open_socket(ip, port, 1);
    ikcpcb *kcp = make_kcp(mode);
    printf("READY\n");
    fflush(stdout);

    unsigned char *got = malloc(exp_len + 8 + 65536);
    size_t have = 0, want = (size_t)-1;
    IUINT32 t0 = now_ms();
    while (now_ms() - t0 < TIMEOUT_MS + 10000u) {
        pump(kcp, 5);
        int n;
        while ((n = ikcp_recv(kcp, (char *)got + have, 65536)) > 0) {
            have += (size_t)n;
            if (want == (size_t)-1 && have >= 8) {
                unsigned long long w;
                memcpy(&w, got, 8);
                want = (size_t)w + 8;
            }
        }
        if (want != (size_t)-1 && have >= want) break;
    }
    int ok = (want == exp_len + 8) && have >= want && memcmp(got + 8, expected, exp_len) == 0;
    char reply = ok ? 'K' : 'X';
    ikcp_send(kcp, &reply, 1);
    IUINT32 t1 = now_ms();
    while (now_ms() - t1 < 1500u) pump(kcp, 5);  /* keep acking until the client has the reply */
    printf("RESULT {\"ok\": %s}\n", ok ? "true" : "false");
    return 0;
}

static int run_client(const char *ip, int port, const char *path, const char *mode) {
    size_t len = 0;
    unsigned char *data = read_file(path, &len);
    if (!data) { fprintf(stderr, "cannot read %s\n", path); return 2; }
    double t0 = now_s();
    g_sock = open_socket(ip, port, 0);
    ikcpcb *kcp = make_kcp(mode);

    unsigned long long hdr = (unsigned long long)len;
    ikcp_send(kcp, (const char *)&hdr, 8);
    for (size_t off = 0; off < len; off += 32768) {
        size_t chunk = len - off < 32768 ? len - off : 32768;
        ikcp_send(kcp, (const char *)data + off, (int)chunk);
    }
    ikcp_flush(kcp);

    int ok = 0;
    char reply = 0;
    while ((now_s() - t0) * 1000.0 < TIMEOUT_MS) {
        pump(kcp, 5);
        if (ikcp_recv(kcp, &reply, 1) == 1) { ok = reply == 'K'; break; }
    }
    printf("RESULT {\"ok\": %s, \"elapsed_s\": %.6f}\n", ok ? "true" : "false", now_s() - t0);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 6) {
        fprintf(stderr, "usage: %s server|client <ip> <port> <file> <fast|fast-cc>\n", argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (strcmp(argv[1], "server") == 0) return run_server(argv[2], atoi(argv[3]), argv[4], argv[5]);
    return run_client(argv[2], atoi(argv[3]), argv[4], argv[5]);
}
