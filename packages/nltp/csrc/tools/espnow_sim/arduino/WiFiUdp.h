/* Host WiFiUDP backed by a real non-blocking UDP socket.
 * Every datagram the sketch sends (to any IP, e.g. the shore server's public address) goes to
 * 127.0.0.1:<sim_shore_port>, and replies appear to come from the address the sketch used, as a
 * router/NAT would make it look. This lets a sketch talk to a real `nltp serve` on the host. */
#pragma once
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include "Arduino.h"

extern uint16_t sim_shore_port;
extern int64_t sim_udp_last_activity_us;

class WiFiUDP {
public:
    uint8_t begin(uint16_t) {
        fd_ = socket(AF_INET, SOCK_DGRAM, 0);
        if (fd_ < 0) return 0;
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        a.sin_port = 0;
        if (bind(fd_, (sockaddr *)&a, sizeof a) != 0) return 0;
        fcntl(fd_, F_SETFL, O_NONBLOCK);
        return 1;
    }
    int beginPacket(IPAddress ip, uint16_t port) {
        virtIp_ = ip;
        virtPort_ = port;
        txLen_ = 0;
        return 1;
    }
    size_t write(const uint8_t *d, size_t n) {
        if (txLen_ + n > sizeof tx_) n = sizeof tx_ - txLen_;
        memcpy(tx_ + txLen_, d, n);
        txLen_ += n;
        return n;
    }
    int endPacket() {
        sockaddr_in to{};
        to.sin_family = AF_INET;
        to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        to.sin_port = htons(sim_shore_port);
        sim_udp_last_activity_us = g_us;
        return sendto(fd_, tx_, txLen_, 0, (sockaddr *)&to, sizeof to) == (ssize_t)txLen_;
    }
    int parsePacket() {
        rxLen_ = recv(fd_, rx_, sizeof rx_, 0);
        if (rxLen_ <= 0) return rxLen_ = 0;
        sim_udp_last_activity_us = g_us;
        return (int)rxLen_;
    }
    int read(uint8_t *d, size_t n) {
        if ((ssize_t)n > rxLen_) n = (size_t)rxLen_;
        memcpy(d, rx_, n);
        return (int)n;
    }
    IPAddress remoteIP() { return virtIp_; }
    uint16_t remotePort() { return virtPort_; }

private:
    int fd_ = -1;
    IPAddress virtIp_;
    uint16_t virtPort_ = 0;
    uint8_t tx_[1600];
    size_t txLen_ = 0;
    uint8_t rx_[1600];
    ssize_t rxLen_ = 0;
};
