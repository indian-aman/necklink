// Minimal UDT4 file transfer for scripts/fair_transport_benchmark.py (same protocol as kcp_xfer.c).
//
//   udt_xfer server <bind_ip> <port> <expected_file>
//   udt_xfer client <server_ip> <port> <file>
//
// The client sends an 8-byte length then the file over a UDT stream socket (UDT's own congestion
// control); the server compares byte-for-byte and answers 'K' (ok) or 'X'. Both print "RESULT {...}".
// Build: g++ -O2 -o udt_xfer udt_xfer.cpp -I/usr/include/udt -ludt -lpthread
#include <udt.h>

#include <arpa/inet.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <vector>

static double now_s() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static bool read_file(const char *path, std::vector<char> &out) {
    FILE *f = std::fopen(path, "rb");
    if (!f) return false;
    std::fseek(f, 0, SEEK_END);
    long n = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    out.resize(n > 0 ? n : 0);
    bool ok = n <= 0 || std::fread(out.data(), 1, n, f) == (size_t)n;
    std::fclose(f);
    return ok;
}

static sockaddr_in addr_of(const char *ip, int port) {
    sockaddr_in a;
    std::memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    inet_pton(AF_INET, ip, &a.sin_addr);
    return a;
}

static bool send_all(UDTSOCKET s, const char *buf, long len) {
    long off = 0;
    while (off < len) {
        int n = UDT::send(s, buf + off, (int)std::min<long>(len - off, 1 << 20), 0);
        if (n == UDT::ERROR) return false;
        off += n;
    }
    return true;
}

static bool recv_all(UDTSOCKET s, char *buf, long len) {
    long off = 0;
    while (off < len) {
        int n = UDT::recv(s, buf + off, (int)std::min<long>(len - off, 1 << 20), 0);
        if (n == UDT::ERROR) return false;
        off += n;
    }
    return true;
}

static int run_server(const char *ip, int port, const char *expected_path) {
    std::vector<char> expected;
    if (!read_file(expected_path, expected)) { std::fprintf(stderr, "cannot read %s\n", expected_path); return 2; }
    UDTSOCKET serv = UDT::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a = addr_of(ip, port);
    if (UDT::bind(serv, (sockaddr *)&a, sizeof(a)) == UDT::ERROR || UDT::listen(serv, 1) == UDT::ERROR) {
        std::fprintf(stderr, "bind/listen: %s\n", UDT::getlasterror().getErrorMessage());
        return 2;
    }
    std::printf("READY\n");
    std::fflush(stdout);
    sockaddr_in peer;
    int pl = sizeof(peer);
    UDTSOCKET s = UDT::accept(serv, (sockaddr *)&peer, &pl);
    unsigned long long want = 0;
    bool ok = recv_all(s, (char *)&want, 8) && want == expected.size();
    std::vector<char> got(ok ? want : 0);
    ok = ok && recv_all(s, got.data(), (long)want) && std::memcmp(got.data(), expected.data(), want) == 0;
    char reply = ok ? 'K' : 'X';
    UDT::send(s, &reply, 1, 0);
    timespec ts = {1, 500000000};
    nanosleep(&ts, nullptr);  // let the reply and acks drain before closing
    UDT::close(s);
    UDT::close(serv);
    std::printf("RESULT {\"ok\": %s}\n", ok ? "true" : "false");
    return 0;
}

static int run_client(const char *ip, int port, const char *path) {
    std::vector<char> data;
    if (!read_file(path, data)) { std::fprintf(stderr, "cannot read %s\n", path); return 2; }
    double t0 = now_s();
    UDTSOCKET s = UDT::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a = addr_of(ip, port);
    bool ok = UDT::connect(s, (sockaddr *)&a, sizeof(a)) != UDT::ERROR;
    unsigned long long len = data.size();
    ok = ok && send_all(s, (const char *)&len, 8) && send_all(s, data.data(), (long)len);
    char reply = 0;
    ok = ok && recv_all(s, &reply, 1) && reply == 'K';
    std::printf("RESULT {\"ok\": %s, \"elapsed_s\": %.6f}\n", ok ? "true" : "false", now_s() - t0);
    std::fflush(stdout);
    UDT::close(s);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        std::fprintf(stderr, "usage: %s server|client <ip> <port> <file>\n", argv[0]);
        return 2;
    }
    setvbuf(stdout, nullptr, _IOLBF, 0);
    UDT::startup();
    int rc = std::strcmp(argv[1], "server") == 0 ? run_server(argv[2], std::atoi(argv[3]), argv[4])
                                                 : run_client(argv[2], std::atoi(argv[3]), argv[4]);
    UDT::cleanup();
    return rc;
}
