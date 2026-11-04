#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
int64_t esp_timer_get_time(void);
void esp_rom_delay_us(uint32_t us);
#ifdef __cplusplus
}
#endif
