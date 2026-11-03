/*
 * necklink_socket.h — Drop-In C99 UDP & Custom-Radio Socket Wrapper for NeckLink
 *
 * Works identically across:
 *   1. Windows (`_WIN32` -> Winsock2 non-blocking `SOCK_DGRAM`)
 *   2. Linux / macOS / POSIX (`sys/socket.h` + `fcntl(O_NONBLOCK)`)
 *   3. Bare-Metal MCUs (`#define NL_SOCK_NO_OS_UDP` -> zero-OS `tx_fn`/`rx_fn` callbacks
 *      for UART, SPI LoRa SX1276, ESP-NOW, BLE, or Iridium SBD modems!)
 *
 * Usage (3 lines in any C/C++ program):
 *   #include "necklink_buoy16_int8.h"
 *   #define NL_CODEC_PREFIX necklink_buoy16
 *   #define NL_CODEC_UPPER  NECKLINK_BUOY16
 *   #include "necklink_socket.h"
 *
 *   nl_socket_t tx;
 *   nl_udp_connect(&tx, "127.0.0.1", 9100);
 *   nl_send_q8(&tx, sensor_q8);        // Sends 0 bytes when calm!
 *
 *   nl_socket_t rx;
 *   nl_udp_bind(&rx, "0.0.0.0", 9100);
 *   nl_recv_tick_q8(&rx, state_q8);    // Non-blocking 0-ms self-healing!
 */
#ifndef NECKLINK_SOCKET_H
#define NECKLINK_SOCKET_H

#include <stdint.h>
#include <stddef.h>

#if !defined(NL_CODEC_PREFIX) || !defined(NL_CODEC_UPPER)
#error "Include a generated necklink_*_int8.h and define NL_CODEC_PREFIX and NL_CODEC_UPPER before necklink_socket.h"
#endif

#define NL_CAT2_(a, b) a##b
#define NL_CAT_(a, b)  NL_CAT2_(a, b)
#define NL_FN(suf)     NL_CAT_(NL_CODEC_PREFIX, suf)
#define NL_CONST(suf)  NL_CAT_(NL_CODEC_UPPER, suf)

#ifndef NL_SOCK_NO_OS_UDP
  #ifdef _WIN32
    #ifndef WIN32_LEAN_AND_MEAN
    #define WIN32_LEAN_AND_MEAN
    #endif
    #include <winsock2.h>
    #include <ws2tcpip.h>
    typedef SOCKET nl_os_fd_t;
    #define NL_INVALID_FD INVALID_SOCKET
  #else
    #include <sys/types.h>
    #include <sys/socket.h>
    #include <netinet/in.h>
    #include <arpa/inet.h>
    #include <fcntl.h>
    #include <unistd.h>
    #include <errno.h>
    typedef int nl_os_fd_t;
    #define NL_INVALID_FD (-1)
  #endif
#else
  typedef int nl_os_fd_t;
  #define NL_INVALID_FD (-1)
#endif

typedef int (*nl_tx_callback_t)(const uint8_t *buf, int len, void *user_ctx);
typedef int (*nl_rx_callback_t)(uint8_t *buf, int max_len, void *user_ctx);

typedef struct {
    NL_FN(_tx_state_t) tx;
    NL_FN(_rx_state_t) rx;
    nl_os_fd_t fd;
#ifndef NL_SOCK_NO_OS_UDP
    struct sockaddr_in peer_addr;
    int has_peer;
#endif
    nl_tx_callback_t custom_tx;
    nl_rx_callback_t custom_rx;
    void *user_ctx;
    uint32_t wire_bytes_sent;
    uint32_t wire_bytes_rcvd;
    uint32_t tx_packets;
    uint32_t silent_ticks;
} nl_socket_t;

static inline void nl_sock_init_StateOnly(nl_socket_t *s) {
    NL_FN(_tx_init)(&s->tx);
    NL_FN(_rx_init)(&s->rx);
    s->fd = NL_INVALID_FD;
#ifndef NL_SOCK_NO_OS_UDP
    s->has_peer = 0;
#endif
    s->custom_tx = 0;
    s->custom_rx = 0;
    s->user_ctx = 0;
    s->wire_bytes_sent = 0;
    s->wire_bytes_rcvd = 0;
    s->tx_packets = 0;
    s->silent_ticks = 0;
}

static inline void nl_sock_open_callbacks(
    nl_socket_t *s,
    nl_tx_callback_t tx_fn,
    nl_rx_callback_t rx_fn,
    void *user_ctx
) {
    nl_sock_init_StateOnly(s);
    s->custom_tx = tx_fn;
    s->custom_rx = rx_fn;
    s->user_ctx = user_ctx;
}

#ifndef NL_SOCK_NO_OS_UDP
static inline int nl_set_nonblocking_(nl_os_fd_t fd) {
#ifdef _WIN32
    u_long mode = 1;
    return (ioctlsocket(fd, FIONBIO, &mode) == 0) ? 0 : -1;
#else
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return -1;
    return (fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0) ? 0 : -1;
#endif
}

static inline int nl_udp_connect(nl_socket_t *s, const char *host_ip, uint16_t port) {
    nl_sock_init_StateOnly(s);
    s->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (s->fd == NL_INVALID_FD) return -1;
    if (nl_set_nonblocking_(s->fd) != 0) return -1;
    s->peer_addr.sin_family = AF_INET;
    s->peer_addr.sin_port = htons(port);
    if (inet_pton(AF_INET, host_ip, &s->peer_addr.sin_addr) != 1) return -1;
    s->has_peer = 1;
    return 0;
}

static inline int nl_udp_bind(nl_socket_t *s, const char *bind_ip, uint16_t port) {
    struct sockaddr_in local_addr;
    nl_sock_init_StateOnly(s);
    s->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (s->fd == NL_INVALID_FD) return -1;
    if (nl_set_nonblocking_(s->fd) != 0) return -1;
    local_addr.sin_family = AF_INET;
    local_addr.sin_port = htons(port);
    if (inet_pton(AF_INET, bind_ip, &local_addr.sin_addr) != 1) return -1;
    if (bind(s->fd, (const struct sockaddr *)&local_addr, sizeof(local_addr)) != 0) return -1;
    return 0;
}

static inline void nl_udp_close(nl_socket_t *s) {
    if (s->fd != NL_INVALID_FD) {
#ifdef _WIN32
        closesocket(s->fd);
#else
        close(s->fd);
#endif
        s->fd = NL_INVALID_FD;
    }
}
#endif /* NL_SOCK_NO_OS_UDP */

static inline int nl_send_q8(nl_socket_t *s, const int32_t *z_q8) {
    uint8_t pkt[NL_CONST(_MAX_PKT_BYTES)];
    int len = NL_FN(_encode_q8)(&s->tx, z_q8, pkt);
    if (len <= 0) {
        s->silent_ticks += 1u;
        return 0;
    }
    if (s->custom_tx != 0) {
        int rc = s->custom_tx(pkt, len, s->user_ctx);
        if (rc < 0) return rc;
    }
#ifndef NL_SOCK_NO_OS_UDP
    else if (s->fd != NL_INVALID_FD && s->has_peer) {
        int sent = (int)sendto(
            s->fd,
            (const char *)pkt,
            len,
            0,
            (const struct sockaddr *)&s->peer_addr,
            sizeof(s->peer_addr)
        );
        if (sent < 0) return -1;
    }
#endif
    s->wire_bytes_sent += (uint32_t)len;
    s->tx_packets += 1u;
    return len;
}

static inline int nl_recv_tick_q8(nl_socket_t *s, int32_t *out_z_q8) {
    uint8_t pkt[128];
    int got_any = 0;

    for (;;) {
        int rlen = 0;
        if (s->custom_rx != 0) {
            rlen = s->custom_rx(pkt, (int)sizeof(pkt), s->user_ctx);
        }
#ifndef NL_SOCK_NO_OS_UDP
        else if (s->fd != NL_INVALID_FD) {
            struct sockaddr_in src;
#ifdef _WIN32
            int slen = (int)sizeof(src);
#else
            socklen_t slen = (socklen_t)sizeof(src);
#endif
            rlen = (int)recvfrom(s->fd, (char *)pkt, (int)sizeof(pkt), 0, (struct sockaddr *)&src, &slen);
        }
#endif
        if (rlen <= 0) {
            break;
        }
        got_any = 1;
        s->wire_bytes_rcvd += (uint32_t)rlen;
        NL_FN(_decode_q8)(&s->rx, pkt, rlen, out_z_q8);
    }

    if (!got_any) {
        NL_FN(_decode_q8)(&s->rx, 0, 0, out_z_q8);
    }
    return got_any;
}

#endif /* NECKLINK_SOCKET_H */
