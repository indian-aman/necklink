/*
 * nltp_net.h — NAT traversal helpers for nltp_core (C99, no heap, no floats).
 *
 * Wire-compatible with necklink/internet.py:
 *   - Relay framing for NLTPRelayHub: "NR" + cmd(1) + room_tag(16) + inner NLTP packet, where
 *     room_tag = SHA-256("NLTP-RELAY-ROOM:" + room)[:16]. Clients REGISTER (empty inner) until REG_ACK.
 *   - STUN (RFC 5389) Binding Request / Success Response with XOR-MAPPED-ADDRESS (IPv4), to learn the
 *     public address a NAT gives this socket.
 * Hole punching uses nltp_send_ping() from nltp_core.h: both peers ping each other's public address
 * until a PONG arrives (ep->pongs_rx increases).
 * Addresses are IPv4 in network byte order (as in struct sockaddr_in).
 */
#ifndef NLTP_NET_H
#define NLTP_NET_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NLTP_RELAY_HDR_BYTES 19u
#define NLTP_RELAY_CMD_DATA 0x01u
#define NLTP_RELAY_CMD_REGISTER 0x02u
#define NLTP_RELAY_CMD_REG_ACK 0x03u
#define NLTP_RELAY_REREGISTER_MS 250u    /* until acknowledged */
#define NLTP_RELAY_REFRESH_MS 5000u      /* once registered */

#define NLTP_STUN_REQUEST_BYTES 20u

void nltp_room_tag(const char *room, uint8_t out_tag16[16]);

/* Writes "NR" + cmd + tag + inner into out (cap bytes). Returns the frame length, or 0 if it does not fit. */
size_t nltp_relay_wrap(uint8_t *out, size_t cap, const uint8_t tag16[16], uint8_t cmd,
                       const uint8_t *inner, size_t inner_len);

/* Returns 1 for a relay frame carrying `tag16` (sets cmd / inner); 0 for anything else. */
int nltp_relay_unwrap(const uint8_t *raw, size_t len, const uint8_t tag16[16], uint8_t *cmd,
                      const uint8_t **inner, size_t *inner_len);

/* STUN Binding Request with transaction id tid12 into out20. */
void nltp_stun_build_request(uint8_t out20[NLTP_STUN_REQUEST_BYTES], const uint8_t tid12[12]);
int nltp_is_stun(const uint8_t *raw, size_t len);
/* Parses a Binding Success Response for tid12; returns 1 and the mapped IPv4 address/port (network order). */
int nltp_stun_parse_response(const uint8_t *raw, size_t len, const uint8_t tid12[12],
                             uint32_t *ip_be, uint16_t *port_be);

#ifdef __cplusplus
}
#endif

#endif /* NLTP_NET_H */
