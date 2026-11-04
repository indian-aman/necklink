/* Minimal Arduino-ESP32 API for running the example sketches on the host (sketch_runner.cpp).
 * Time comes from the simulated radio clock (g_us); Serial output is prefixed with the device name. */
#pragma once
#include <stdarg.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern "C" int64_t g_us;
extern "C" int g_cur;
extern const char *sim_device_name(int dev);

inline unsigned long millis(void) { return (unsigned long)(g_us / 1000); }
inline unsigned long micros(void) { return (unsigned long)g_us; }
inline void delay(unsigned long) {}          /* the runner owns time */
inline void delayMicroseconds(unsigned int) {}
uint32_t analogReadMilliVolts(uint8_t pin);

class String {
public:
    String(const char *s = "") { snprintf(b, sizeof b, "%s", s); }
    const char *c_str() const { return b; }
private:
    char b[64];
};

class IPAddress {
public:
    IPAddress() : v{0, 0, 0, 0} {}
    IPAddress(uint8_t a, uint8_t b, uint8_t c, uint8_t d) : v{a, b, c, d} {}
    uint8_t operator[](int i) const { return v[i]; }
    bool operator==(const IPAddress &o) const { return memcmp(v, o.v, 4) == 0; }
    bool operator!=(const IPAddress &o) const { return !(*this == o); }
    operator uint32_t() const { uint32_t r; memcpy(&r, v, 4); return r; }
    String toString() const {
        char s[16];
        snprintf(s, sizeof s, "%u.%u.%u.%u", v[0], v[1], v[2], v[3]);
        return String(s);
    }
    uint8_t v[4];
};

class HardwareSerial {
public:
    void begin(unsigned long) {}
    size_t println(const char *s) { return printf("%s\n", s); }
    size_t print(const char *s) { return printf("%s", s); }
    __attribute__((format(printf, 2, 3))) size_t printf(const char *fmt, ...) {
        char line[512];
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(line, sizeof line, fmt, ap);
        va_end(ap);
        if (atLineStart) fprintf(stdout, "[%7.2f s %-7s] ", (double)g_us / 1e6, sim_device_name(g_cur));
        fputs(line, stdout);
        size_t n = strlen(line);
        atLineStart = n > 0 && line[n - 1] == '\n';
        fflush(stdout);
        return n;
    }
private:
    bool atLineStart = true;
};
extern HardwareSerial Serial;
