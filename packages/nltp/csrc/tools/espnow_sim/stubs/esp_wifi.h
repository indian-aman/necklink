#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
typedef enum { WIFI_IF_STA = 0, WIFI_IF_AP = 1 } wifi_interface_t;
#define WIFI_PROTOCOL_11B 1
#define WIFI_PROTOCOL_11G 2
#define WIFI_PROTOCOL_11N 4
#define WIFI_PROTOCOL_LR 8
#define WIFI_SECOND_CHAN_NONE 0
typedef enum { WIFI_PHY_MODE_11B, WIFI_PHY_MODE_HT20, WIFI_PHY_MODE_LR } wifi_phy_mode_t;
typedef enum { WIFI_PHY_RATE_1M_L, WIFI_PHY_RATE_11M_S, WIFI_PHY_RATE_MCS4_LGI, WIFI_PHY_RATE_MCS7_SGI, WIFI_PHY_RATE_LORA_250K, WIFI_PHY_RATE_LORA_500K } wifi_phy_rate_t;
static inline esp_err_t esp_wifi_set_protocol(wifi_interface_t i, uint8_t b) { (void)i; (void)b; return ESP_OK; }
static inline esp_err_t esp_wifi_set_channel(uint8_t c, int s) { (void)c; (void)s; return ESP_OK; }
