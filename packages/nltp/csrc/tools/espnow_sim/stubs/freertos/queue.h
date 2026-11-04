#pragma once
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct sim_queue *QueueHandle_t;
QueueHandle_t xQueueCreate(unsigned depth, size_t item);
int xQueueSend(QueueHandle_t q, const void *item, unsigned ticks);
int xQueueReceive(QueueHandle_t q, void *item, unsigned ticks);
#ifdef __cplusplus
}
#endif
