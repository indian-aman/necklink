// NLTP test firmware for ESP32 (Arduino).
//
// - Joins Wi-Fi (include/secrets.h) and prints "IP <addr>" on the serial console.
// - NLTP endpoint on UDP 9700: receives files from a laptop (verified by SHA-256) and, on the serial
//   command "send <bytes>", sends a generated file back to whoever last sent it a packet.
// - Plain TCP sink on port 9702 (8-byte length + data, replies '1') to compare against TCP on the same radio.
// - "psk <key or passphrase>" turns on ChaCha20-Poly1305 encryption (same rules as Python psk=), "psk off" disables it.
// Results are printed as "RESULT rx|tx|tcp ..." lines; "stat" prints heap and signal strength.
#include <Arduino.h>
#include <WiFi.h>

#include <fcntl.h>

#include "lwip/sockets.h"
#include "nltp_core.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy include/secrets.h.example to include/secrets.h and fill in your Wi-Fi name and password."
#endif

static const uint16_t NLTP_PORT = 9700;
static const uint16_t TCP_PORT = 9702;
static const uint32_t RX_CAP = 64 * 1024;  // per test buffer; the ESP32 heap also has to fit Wi-Fi
static const uint32_t MAX_KBPS = 20000;

static nltp_endpoint_t *ep;
static uint8_t *rx_buf;
static uint8_t *tx_buf;
static int udp_sock = -1;
static struct sockaddr_in peer;
static bool have_peer = false;

static uint32_t rx_sid_seen = 0, rx_first_ms = 0, rx_reported_sid = 0;
static uint32_t tx_started_ms = 0, tx_len = 0;
static bool tx_reported = true;

static uint32_t now_cb(void *) { return millis(); }

static void send_cb(void *, const uint8_t *pkt, size_t len) {
    if (have_peer) sendto(udp_sock, pkt, len, 0, (struct sockaddr *)&peer, sizeof(peer));
}

static void fill_test_data(uint8_t *buf, uint32_t n) {
    uint32_t x = 0x9E3779B9u;  // xorshift32: deterministic, incompressible test data
    for (uint32_t i = 0; i < n; ++i) {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        buf[i] = (uint8_t)x;
    }
}

static void tcp_sink_task(void *) {
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {};
    a.sin_family = AF_INET;
    a.sin_port = htons(TCP_PORT);
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    bind(srv, (struct sockaddr *)&a, sizeof(a));
    listen(srv, 1);
    static uint8_t chunk[1460];
    for (;;) {
        int c = accept(srv, nullptr, nullptr);
        if (c < 0) continue;
        uint32_t t0 = millis();
        uint64_t want = 0;
        int got = 0;
        while (got < 8) {
            int n = recv(c, ((uint8_t *)&want) + got, 8 - got, 0);
            if (n <= 0) break;
            got += n;
        }
        uint64_t total = 0;
        while (got == 8 && total < want) {
            int n = recv(c, chunk, sizeof(chunk), 0);
            if (n <= 0) break;
            total += (uint64_t)n;
        }
        char ok = (got == 8 && total == want) ? '1' : '0';
        send(c, &ok, 1, 0);
        close(c);
        Serial.printf("RESULT tcp ok=%d bytes=%lu ms=%lu\n", ok == '1', (unsigned long)total, (unsigned long)(millis() - t0));
    }
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("HEAP free=%u largest_block=%u endpoint_needs=%u\n", (unsigned)ESP.getFreeHeap(),
                  (unsigned)ESP.getMaxAllocHeap(), (unsigned)sizeof(nltp_endpoint_t));
    ep = (nltp_endpoint_t *)malloc(sizeof(nltp_endpoint_t));
    // Receive and send tests run one after another, so they share one buffer.
    rx_buf = (uint8_t *)malloc(RX_CAP);
    tx_buf = rx_buf;
    if (!ep || !rx_buf) {
        Serial.printf("ERROR out of memory (endpoint %s, buffer %s) heap free=%u largest_block=%u\n",
                      ep ? "ok" : "failed", rx_buf ? "ok" : "failed", (unsigned)ESP.getFreeHeap(),
                      (unsigned)ESP.getMaxAllocHeap());
        for (;;) delay(1000);
    }
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);  // power save adds tens of ms of latency per packet
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.print("Connecting to Wi-Fi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(300);
        Serial.print('.');
    }
    Serial.printf("\nIP %s rssi=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());

    udp_sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in a = {};
    a.sin_family = AF_INET;
    a.sin_port = htons(NLTP_PORT);
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    bind(udp_sock, (struct sockaddr *)&a, sizeof(a));
    fcntl(udp_sock, F_SETFL, fcntl(udp_sock, F_GETFL, 0) | O_NONBLOCK);

    nltp_init(ep, esp_random(), MAX_KBPS, send_cb, now_cb, nullptr);
    nltp_set_rx_buffer(ep, rx_buf, RX_CAP);
    xTaskCreatePinnedToCore(tcp_sink_task, "tcp_sink", 4096, nullptr, 1, nullptr, 0);
    Serial.printf("READY nltp=udp/%u tcp=%u endpoint=%uB heap=%u\n", NLTP_PORT, TCP_PORT,
                  (unsigned)sizeof(nltp_endpoint_t), (unsigned)ESP.getFreeHeap());
}

static void handle_serial() {
    static char line[64];
    static size_t n = 0;
    while (Serial.available()) {
        char ch = (char)Serial.read();
        if (ch == '\r') continue;
        if (ch != '\n' && n + 1 < sizeof(line)) {
            line[n++] = ch;
            continue;
        }
        line[n] = '\0';
        n = 0;
        if (!strncmp(line, "send ", 5)) {
            uint32_t len = (uint32_t)strtoul(line + 5, nullptr, 10);
            if (len > RX_CAP) len = RX_CAP;
            if (!have_peer) {
                Serial.println("ERROR no peer yet (send any NLTP packet to this board first)");
            } else {
                fill_test_data(tx_buf, len);
                tx_len = len;
                tx_started_ms = millis();
                tx_reported = false;
                if (!nltp_send_buffer(ep, tx_buf, len, "esp32.bin")) Serial.println("ERROR send busy");
            }
        } else if (!strncmp(line, "psk ", 4)) {
            // Key set at run time over USB serial, never stored in the firmware; change it only between transfers
            if (!strcmp(line + 4, "off")) {
                nltp_set_key(ep, nullptr);
                Serial.println("PSK off");
            } else {
                uint8_t key[32];
                nltp_key_from_psk(line + 4, key);
                nltp_set_key(ep, key);
                memset(key, 0, sizeof(key));
                Serial.println("PSK on");
            }
        } else if (!strcmp(line, "stat")) {
            Serial.printf("STAT heap=%u rssi=%d pkts_tx=%u pkts_rx=%u crc_drops=%u auth_drops=%u encrypted=%u "
                          "rate_kbps=%u srtt=%u\n",
                          (unsigned)ESP.getFreeHeap(), WiFi.RSSI(), (unsigned)ep->pkts_tx, (unsigned)ep->pkts_rx,
                          (unsigned)ep->crc_drops, (unsigned)ep->auth_drops, (unsigned)ep->encrypted,
                          (unsigned)ep->rate.rate_kbps, (unsigned)ep->srtt_ms);
        }
    }
}

void loop() {
    uint8_t buf[1100];
    bool got = false;
    for (;;) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        int n = recvfrom(udp_sock, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
        if (n <= 0) break;
        got = true;
        // Replies made while processing go to the sender; keep its address only if the frame passed
        // CRC / authentication, so forged packets cannot redirect the board (roaming still works).
        uint32_t valid0 = ep->valid_rx;
        struct sockaddr_in prev = peer;
        bool had = have_peer;
        peer = from;
        have_peer = true;
        nltp_on_datagram(ep, buf, (size_t)n);
        if (ep->valid_rx == valid0) {
            peer = prev;
            have_peer = had;
        }
    }
    nltp_poll(ep);

    if (ep->rx.active && ep->rx.stream_id != rx_sid_seen) {
        rx_sid_seen = ep->rx.stream_id;
        rx_first_ms = millis();
    }
    if (ep->rx.completed && rx_reported_sid != ep->rx.stream_id) {
        rx_reported_sid = ep->rx.stream_id;
        Serial.printf("RESULT rx ok=%d bytes=%lu ms=%lu\n", nltp_rx_sha_ok(ep), (unsigned long)nltp_rx_len(ep),
                      (unsigned long)(millis() - rx_first_ms));
    }
    if (!tx_reported && nltp_tx_done(ep)) {
        tx_reported = true;
        Serial.printf("RESULT tx ok=%d bytes=%lu ms=%lu\n", nltp_tx_verified(ep), (unsigned long)tx_len,
                      (unsigned long)(millis() - tx_started_ms));
    }
    handle_serial();
    if (!got) delay(1);  // yield to Wi-Fi and the idle task when there is nothing to do
}
