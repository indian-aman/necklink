/*
 * NLTP.h — Arduino IDE & PlatformIO C++ wrapper for the portable C99 NLTP v1.2 core.
 *
 * Works out of the box with ESP32, ESP32-S3, ESP32-C3, RP2040-W, and any board
 * providing Arduino's standard <WiFiUdp.h> interface, while remaining 100%
 * wire-compatible with Python `NLTPServer`, `NLTPClient`, and `NLTPRelayHub`.
 */
#ifndef NLTP_ARDUINO_H
#define NLTP_ARDUINO_H

#include <Arduino.h>
#include <WiFiUdp.h>
#include <stdlib.h>
#include <string.h>

#include "nltp_core.h"
#include "nltp_net.h"

class NLTPWiFiNode {
public:
    NLTPWiFiNode()
        : _ep(nullptr),
          _rxBuf(nullptr),
          _rxCap(0),
          _peerPort(0),
          _hasPeer(false),
          _relayMode(false),
          _relayPort(0),
          _relayAcked(false),
          _lastRelayRegMs(0),
          _lastReportedRxSid(0),
          _fileReady(false) {
        memset(_roomTag, 0, sizeof(_roomTag));
    }

    ~NLTPWiFiNode() {
        if (_ep) free(_ep);
        if (_rxBuf) free(_rxBuf);
    }

    /*
     * Starts a direct UDP NLTP node on `localPort`.
     *   psk        : Shared key or passphrase matching Python `psk=` (pass nullptr or "" for unencrypted).
     *   rxCapBytes : Max received file size buffered in RAM (default 32 KB).
     *   maxKbps    : Pacing ceiling in kbps (default 20000 = 20 Mbps; actual rate adapts below ceiling).
     */
    bool begin(uint16_t localPort,
               const char *psk = nullptr,
               uint32_t rxCapBytes = 32u * 1024u,
               uint32_t maxKbps = 20000u) {
        if (!_alloc(rxCapBytes)) return false;
        if (!_udp.begin(localPort)) return false;

        _relayMode = false;
        uint32_t cid = ((uint32_t)micros() ^ 0xA5A55A5Au) | 1u;
        nltp_init(_ep, cid, maxKbps, &_sendThunk, &_nowThunk, this);
        nltp_set_rx_buffer(_ep, _rxBuf, _rxCap);
        _applyPsk(psk);
        return true;
    }

    /*
     * Starts an NLTP node behind a 4G/5G SIM router connected via a zero-knowledge `NLTPRelayHub` room.
     * No public IP or port forwarding required on either side.
     */
    bool beginRelay(uint16_t localPort,
                    IPAddress relayIp,
                    uint16_t relayPort,
                    const char *roomName,
                    const char *psk,
                    uint32_t rxCapBytes = 32u * 1024u,
                    uint32_t maxKbps = 10000u) {
        if (!roomName || !roomName[0]) return false;
        if (!begin(localPort, psk, rxCapBytes, maxKbps)) return false;

        _relayMode = true;
        _relayIp = relayIp;
        _relayPort = relayPort;
        _peerIp = relayIp;
        _peerPort = relayPort;
        _hasPeer = true;
        _relayAcked = false;
        _lastRelayRegMs = 0;
        nltp_room_tag(roomName, _roomTag);
        _sendRelayRegister();
        return true;
    }

    void setPeer(IPAddress ip, uint16_t port) {
        _peerIp = ip;
        _peerPort = port;
        _hasPeer = true;
    }

    void setKeepaliveMs(uint32_t intervalMs) {
        if (_ep) _ep->keepalive_ms = intervalMs;
    }

    /*
     * Starts an asynchronous reliable transfer of `data[0..len-1]`.
     * IMPORTANT: `data` must stay valid until `isSendComplete()` returns true!
     */
    bool sendBytes(const uint8_t *data, uint32_t len, const char *filename = "payload.bin") {
        if (!_ep || !_hasPeer) return false;
        return nltp_send_buffer(_ep, data, len, filename) != 0u;
    }

    /*
     * Call inside Arduino `loop()` every iteration (~1 ms) to process incoming UDP packets,
     * emit Cauchy repair shards, and maintain NAT keepalives / relay registration.
     */
    void poll() {
        if (!_ep) return;

        if (_relayMode) {
            uint32_t now = millis();
            uint32_t period = _relayAcked ? NLTP_RELAY_REFRESH_MS : NLTP_RELAY_REREGISTER_MS;
            if ((uint32_t)(now - _lastRelayRegMs) >= period) {
                _sendRelayRegister();
            }
        }

        uint8_t raw[NLTP_PKT_MAX + 96u];
        for (;;) {
            int pktLen = _udp.parsePacket();
            if (pktLen <= 0) break;
            int n = _udp.read(raw, (size_t)pktLen > sizeof(raw) ? sizeof(raw) : (size_t)pktLen);
            if (n <= 0) continue;

            if (_relayMode) {
                uint8_t cmd = 0;
                const uint8_t *inner = nullptr;
                size_t innerLen = 0;
                if (!nltp_relay_unwrap(raw, (size_t)n, _roomTag, &cmd, &inner, &innerLen)) {
                    continue;
                }
                if (cmd == NLTP_RELAY_CMD_REG_ACK) {
                    _relayAcked = true;
                    continue;
                }
                if (cmd == NLTP_RELAY_CMD_DATA && innerLen > 0) {
                    nltp_on_datagram(_ep, inner, innerLen);
                }
            } else {
                uint32_t validBefore = _ep->valid_rx;
                IPAddress prevIp = _peerIp;
                uint16_t prevPort = _peerPort;
                bool hadPeer = _hasPeer;

                _peerIp = _udp.remoteIP();
                _peerPort = _udp.remotePort();
                _hasPeer = true;
                nltp_on_datagram(_ep, raw, (size_t)n);

                // Only adopt sender address if CRC / ChaCha20-Poly1305 authentication succeeded
                if (_ep->valid_rx == validBefore) {
                    _peerIp = prevIp;
                    _peerPort = prevPort;
                    _hasPeer = hadPeer;
                }
            }
        }

        nltp_poll(_ep);

        if (_ep->rx.completed && _ep->rx.stream_id != _lastReportedRxSid) {
            _lastReportedRxSid = _ep->rx.stream_id;
            _fileReady = true;
        }
    }

    bool hasReceivedFile() const { return _fileReady; }
    const uint8_t *receivedData() const { return _rxBuf; }
    uint32_t receivedLength() const { return _ep ? nltp_rx_len(_ep) : 0u; }
    const char *receivedFilename() const { return _ep ? _ep->rx.filename : ""; }
    bool isReceivedShaOk() const { return _ep ? (nltp_rx_sha_ok(_ep) != 0) : false; }

    void clearReceived() { _fileReady = false; }

    bool isSendComplete() const { return _ep ? (nltp_tx_done(_ep) != 0) : true; }
    bool isSendVerified() const { return _ep ? (nltp_tx_verified(_ep) != 0) : false; }
    bool isRelayRegistered() const { return _relayAcked; }
    nltp_endpoint_t *endpoint() { return _ep; }

private:
    WiFiUDP _udp;
    nltp_endpoint_t *_ep;
    uint8_t *_rxBuf;
    uint32_t _rxCap;

    IPAddress _peerIp;
    uint16_t _peerPort;
    bool _hasPeer;

    bool _relayMode;
    IPAddress _relayIp;
    uint16_t _relayPort;
    uint8_t _roomTag[16];
    bool _relayAcked;
    uint32_t _lastRelayRegMs;

    uint32_t _lastReportedRxSid;
    bool _fileReady;

    bool _alloc(uint32_t rxCapBytes) {
        if (!_ep) {
            _ep = (nltp_endpoint_t *)malloc(sizeof(nltp_endpoint_t));
            if (!_ep) return false;
        }
        if (!_rxBuf || _rxCap != rxCapBytes) {
            if (_rxBuf) free(_rxBuf);
            _rxBuf = (uint8_t *)malloc(rxCapBytes);
            if (!_rxBuf) return false;
            _rxCap = rxCapBytes;
        }
        return true;
    }

    void _applyPsk(const char *psk) {
        if (psk && psk[0]) {
            uint8_t key32[32];
            nltp_key_from_psk(psk, key32);
            nltp_set_key(_ep, key32);
            memset(key32, 0, sizeof(key32));
        } else {
            nltp_set_key(_ep, nullptr);
        }
    }

    void _sendRelayRegister() {
        uint8_t frame[NLTP_RELAY_HDR_BYTES];
        size_t n = nltp_relay_wrap(frame, sizeof(frame), _roomTag, NLTP_RELAY_CMD_REGISTER, nullptr, 0u);
        if (n > 0) {
            _udp.beginPacket(_relayIp, _relayPort);
            _udp.write(frame, n);
            _udp.endPacket();
            _lastRelayRegMs = millis();
        }
    }

    void _sendPacket(const uint8_t *pkt, size_t len) {
        if (!_hasPeer) return;
        if (_relayMode) {
            uint8_t wrapped[NLTP_RELAY_HDR_BYTES + NLTP_PKT_MAX + 64u];
            size_t wn = nltp_relay_wrap(wrapped, sizeof(wrapped), _roomTag, NLTP_RELAY_CMD_DATA, pkt, len);
            if (wn > 0) {
                _udp.beginPacket(_relayIp, _relayPort);
                _udp.write(wrapped, wn);
                _udp.endPacket();
            }
        } else {
            _udp.beginPacket(_peerIp, _peerPort);
            _udp.write(pkt, len);
            _udp.endPacket();
        }
    }

    static void _sendThunk(void *user, const uint8_t *pkt, size_t len) {
        ((NLTPWiFiNode *)user)->_sendPacket(pkt, len);
    }

    static uint32_t _nowThunk(void *) { return millis(); }
};

#endif /* NLTP_ARDUINO_H */
