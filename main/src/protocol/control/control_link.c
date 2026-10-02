/*
 * The control link's shared TX lock and the ESP_LOG hook (spec §2.2, §7.3 step
 * 2). ESP-IDF side only; the rate policy itself is portable and host-tested in
 * `control_log.c`.
 */

#include "protocol/control/control_link.h"

#include <stdio.h>

#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "protocol/control/control_log.h"

#define CONTROL_LOG_LINE_MAX 256u

static SemaphoreHandle_t s_tx_lock;
static int s_control_port = -1;
static volatile bool s_bulk_active;
static control_log_policy_t s_log_policy;

int control_link_port(void)
{
    return s_control_port;
}

int control_link_write(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0 || s_control_port < 0) {
        return 0;
    }
    if (s_tx_lock == NULL) {
        return -1;
    }
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    int written = uart_write_bytes((uart_port_t)s_control_port, data, len);
    xSemaphoreGive(s_tx_lock);
    return written;
}

void control_link_set_bulk_active(bool active)
{
    s_bulk_active = active;
}

bool control_link_bulk_active(void)
{
    return s_bulk_active;
}

uint32_t control_link_log_dropped(void)
{
    return control_log_policy_dropped(&s_log_policy);
}

/*
 * The log hook. It formats the line, applies the bulk rate limit, and writes it
 * under the same lock the replies use — so a log line can never land inside a
 * frame's bytes (§2.2). A line dropped by the rate limit is counted, not lost
 * silently.
 */
static int control_link_vprintf(const char *fmt, va_list args)
{
    char buf[CONTROL_LOG_LINE_MAX];
    int n = vsnprintf(buf, sizeof(buf), fmt, args);
    if (n < 0) {
        return n;
    }

    size_t len = (size_t)n < sizeof(buf) ? (size_t)n : sizeof(buf) - 1;
    if (len == 0 || s_control_port < 0) {
        return n;
    }

    int64_t now_us = esp_timer_get_time();
    if (!control_log_policy_allow(&s_log_policy, s_bulk_active, now_us)) {
        return n;
    }

    if (s_tx_lock != NULL && xTaskGetSchedulerState() == taskSCHEDULER_RUNNING &&
        !xPortInIsrContext()) {
        xSemaphoreTake(s_tx_lock, portMAX_DELAY);
        uart_write_bytes((uart_port_t)s_control_port, buf, len);
        xSemaphoreGive(s_tx_lock);
    }
    /* Before the scheduler starts there is no safe way to take the lock, and a
     * log written raw into a frame would be worse than a lost log line. */
    return n;
}

int control_link_init(int uart_port)
{
    s_control_port = uart_port;
    control_log_policy_init(&s_log_policy);

    if (s_tx_lock == NULL) {
        s_tx_lock = xSemaphoreCreateMutex();
        if (s_tx_lock == NULL) {
            ESP_LOGE("control_link", "failed to create the control TX lock");
            return -1;
        }
    }

    esp_log_set_vprintf(control_link_vprintf);
    ESP_LOGI("control_link", "control link on UART%d, log routed through the shared TX lock",
             uart_port);
    return 0;
}
