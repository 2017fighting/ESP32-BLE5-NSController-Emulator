#ifndef PROTOCOL_CONTROL_LINK_H
#define PROTOCOL_CONTROL_LINK_H

/*
 * The control link's shared TX lock and the ESP_LOG hook (spec §2.2, §7.3 step 2).
 *
 * `ESP_LOG` and control replies share one wire and must not interleave. Both go
 * through `control_link_write()`, which takes the one lock; the log hook is the
 * `esp_log_set_vprintf` half of that. While a bulk transfer is active the log
 * hook is rate-suppressed through `control_log_policy`, so a large upload is not
 * competing with a log flood for the same wire.
 *
 * The transport's `submit_tx` routes here for the control port (see
 * `transport_uart.c`), so a reply written by the router task and a log line
 * written by any other task are serialised on the same mutex.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Creates the lock and installs the log hook. Returns 0 on success. Call after
 * the UART driver is installed (so the hook has somewhere to write) and before
 * the protocol task starts. */
int control_link_init(int uart_port);

/* The one port the control plane rides; -1 until init. */
int control_link_port(void);

/* Writes @p len bytes to the control port under the shared lock. Returns the
 * number of bytes written, or -1. */
int control_link_write(const uint8_t *data, size_t len);

/* Bulk-transfer log suppression (§2.2, §7.3 step 2). The bulk verbs of #22 call
 * these; stage 1 exposes them so the policy is wired and tested. */
void control_link_set_bulk_active(bool active);
bool control_link_bulk_active(void);

/* Log lines suppressed by the bulk rate limit, for diagnostics. */
uint32_t control_link_log_dropped(void);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_LINK_H */
