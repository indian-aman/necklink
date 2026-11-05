/*
 * NLTP.h — Arduino IDE & PlatformIO C++ wrapper for the portable C99 NLTP core (library v1.4.0).
 *
 * Hardware-tested on ESP32 (D0WDQ6 / D0WD-V3; UDP and ESP-NOW). UDP mode uses
 * Arduino's standard <WiFiUdp.h> interface (other Wi-Fi boards are not yet
 * hardware-tested), while remaining 100%
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
#if defined(ESP_PLATFORM) || defined(ESP32)
#include "esp_random.h"
#endif

/* Hardware random numbers for session IDs and (where the C core has no default) session nonces.
 * ESP32 and ESP8266 use their hardware RNG; RP2040 (arduino-pico) uses rp2040.hwrand32(). Other boards
 * have no secure source here: they can run unencrypted, or call nltp_set_random() on the endpoint. */
static inline uint32_t nltpRandom32() {
#if defined(ESP_PLATFORM) || defined(ESP32)
    return esp_random();
#elif defined(ESP8266)
    return RANDOM_REG32;
#elif defined(ARDUINO_ARCH_RP2040)
    return rp2040.hwrand32();
#else
    return (uint32_t)micros() ^ ((uint32_t)analogRead(0) << 16) ^ 0xA5A55A5Au;   /* not secure */
#endif
}

#if defined(ARDUINO_ARCH_RP2040)
static void nltpRandomFill(void *, uint8_t *out, size_t len) {
    for (size_t i = 0; i < len; i += 4) {
        uint32_t r = rp2040.hwrand32();
        for (size_t j = 0; j < 4 && i + j < len; ++j) out[i + j] = (uint8_t)(r >> (8 * j));
    }
}
#endif
/* NLTP over ESP-NOW needs ESP-IDF 5.x (Arduino-ESP32 core 3.x). On older cores the UDP classes
 * below still work; NLTPESPNowNode is simply not available. */
#if defined(ESP_PLATFORM) || defined(ARDUINO_ARCH_ESP32) || defined(ESP32)
#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR >= 5
#define NLTP_HAS_ESPNOW 1
#include "nltp_espnow.h"
#endif
#endif

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
          _lastReportedRxCid(0),
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
    /* Boards without a hardware RNG known to NLTP (not ESP32 / ESP8266 / RP2040): supply a secure random
     * source (e.g. a TRNG or crypto chip) before begin(), or encrypted begin() returns false. */
    void setRandomSource(nltp_random_fn fn) { _userRng = fn; }

    bool begin(uint16_t localPort,
               const char *psk = nullptr,
               uint32_t rxCapBytes = 32u * 1024u,
               uint32_t maxKbps = 20000u) {
        if (!_alloc(rxCapBytes)) return false;
        if (!_udp.begin(localPort)) return false;

        _relayMode = false;
        /* Random session ID from the hardware RNG (1.3.x used the boot clock, so reboots could repeat it). */
        uint32_t cid = nltpRandom32() | 1u;
        if (_userRng) _userRng(nullptr, (uint8_t *)&cid, sizeof(cid));
        nltp_init(_ep, cid | 1u, maxKbps, &_sendThunk, &_nowThunk, this);
#if defined(ARDUINO_ARCH_RP2040)
        nltp_set_random(_ep, &nltpRandomFill);
#endif
        if (_userRng) nltp_set_random(_ep, _userRng);
        nltp_set_rx_buffer(_ep, _rxBuf, _rxCap);
        _applyPsk(psk);
        /* Encryption needs a secure random source for session nonces: refuse rather than run without one. */
        if (_ep->encrypted && !_ep->rng) return false;
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

        if (_ep->rx.completed &&
            (_ep->rx.stream_id != _lastReportedRxSid || _ep->rx.peer_cid != _lastReportedRxCid)) {
            _lastReportedRxSid = _ep->rx.stream_id;
            _lastReportedRxCid = _ep->rx.peer_cid;
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
    nltp_random_fn _userRng = nullptr;
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
    uint32_t _lastReportedRxCid;
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

#ifdef NLTP_HAS_ESPNOW
/*
 * NLTPESPNowNode — Zero-wrapper ESP-NOW v2 (1,470 B MTU) Arduino C++ wrapper.
 * Wire-interoperable byte-for-byte with C99 `nltp_espnow_node_t`, `nltp_espnow_hub_t`,
 * and bridged Python `pynltp` / C `nltp_cli` endpoints.
 */
class NLTPESPNowNode {
public:
    NLTPESPNowNode()
        : _node(nullptr),
          _rxBuf(nullptr),
          _rxCap(0),
          _lastReportedSid(0),
          _lastReportedCid(0),
          _fileReady(false) {}

    ~NLTPESPNowNode() {
        if (_node) free(_node);
        if (_rxBuf) free(_rxBuf);
    }

    bool begin(uint8_t channel = 11,
               const char *psk = nullptr,
               nltp_espnow_rate_t rateMode = NLTP_ESPNOW_RATE_AUTO,
               uint32_t rxCapBytes = 32u * 1024u,
               uint32_t maxKbps = 20000u) {
        if (!_node) {
            _node = (nltp_espnow_node_t *)malloc(sizeof(nltp_espnow_node_t));
            if (!_node) return false;
        }
        if (!_rxBuf || _rxCap != rxCapBytes) {
            if (_rxBuf) free(_rxBuf);
            _rxBuf = (uint8_t *)malloc(rxCapBytes);
            if (!_rxBuf) return false;
            _rxCap = rxCapBytes;
        }
        _lastReportedSid = 0;
        _lastReportedCid = 0;
        _fileReady = false;
        return nltp_espnow_node_init(_node, channel, rateMode, psk, _rxBuf, _rxCap, maxKbps) == ESP_OK;
    }

    void setPeerMac(const uint8_t mac[6]) {
        if (_node) nltp_espnow_node_set_peer(_node, mac);
    }

    void setRateMode(nltp_espnow_rate_t rateMode) {
        if (_node) nltp_espnow_node_set_rate(_node, rateMode);
    }

    void setEspNowV1Mode(bool enableV1) {
        if (_node) nltp_espnow_node_set_v1_mode(_node, enableV1 ? 1 : 0);
    }

    bool isEspNowV1Mode() const {
        return _node ? (_node->v1_mode != 0u) : false;
    }

    bool sendBytes(const uint8_t *data, uint32_t len, const char *filename = "payload.bin") {
        if (!_node || !_node->has_peer) return false;
        if (_node->ep.tx.active && !_node->ep.tx.completed) return false;
        return nltp_send_buffer(&_node->ep, data, len, filename) != 0u;
    }

    void poll() {
        if (!_node) return;
        nltp_espnow_node_poll(_node);
        if (_node->ep.rx.completed &&
            (_node->ep.rx.stream_id != _lastReportedSid || _node->ep.rx.peer_cid != _lastReportedCid)) {
            _lastReportedSid = _node->ep.rx.stream_id;
            _lastReportedCid = _node->ep.rx.peer_cid;
            _fileReady = true;
        }
    }

    bool hasReceivedFile() const { return _fileReady; }
    const uint8_t *receivedData() const { return _rxBuf; }
    uint32_t receivedLength() const { return _node ? nltp_rx_len(&_node->ep) : 0u; }
    const char *receivedFilename() const { return _node ? _node->ep.rx.filename : ""; }
    bool isReceivedShaOk() const { return _node ? (nltp_rx_sha_ok(&_node->ep) != 0) : false; }
    int8_t lastRssi() const { return _node ? _node->last_rssi : -100; }
    uint32_t espNowVersion() const { return g_nltp_espnow_bus.espnow_version; }

    void clearReceived() { _fileReady = false; }

    bool isSendComplete() const { return _node ? (nltp_tx_done(&_node->ep) != 0) : true; }
    bool isSendVerified() const { return _node ? (nltp_tx_verified(&_node->ep) != 0) : false; }
    nltp_endpoint_t *endpoint() { return _node ? &_node->ep : nullptr; }
    nltp_espnow_node_t *rawNode() { return _node; }

private:
    nltp_espnow_node_t *_node;
    uint8_t *_rxBuf;
    uint32_t _rxCap;
    uint32_t _lastReportedSid;
    uint32_t _lastReportedCid;
    bool _fileReady;
};
#endif /* NLTP_HAS_ESPNOW */

#endif /* NLTP_ARDUINO_H */
