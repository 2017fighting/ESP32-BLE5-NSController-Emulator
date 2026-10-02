#ifndef PROTOCOL_CONTROL_LOG_H
#define PROTOCOL_CONTROL_LOG_H

/*
 * The pure half of the log rate limit (§2.2, §7.3 step 2): while a bulk transfer
 * is active, device logging is suppressed to a bounded rate. The rate limit is
 * time-based, not a line counter, so a long transfer cannot starve the log
 * entirely and a short one cannot burst.
 *
 * This file has no ESP-IDF dependency so the policy is unit-tested on the host;
 * `control/control_link.c` supplies the clock and calls it from the log hook.
 */

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* At most one log line per this interval while a bulk transfer is active. */
#define CONTROL_LOG_BULK_MIN_INTERVAL_MS 100u
#define CONTROL_LOG_BULK_MIN_INTERVAL_US (CONTROL_LOG_BULK_MIN_INTERVAL_MS * 1000)

typedef struct {
    int64_t bulk_last_emit_us;
    uint32_t dropped;
} control_log_policy_t;

void control_log_policy_init(control_log_policy_t *policy);

/* True when a line may be emitted now; updates the policy on an emit. */
bool control_log_policy_allow(control_log_policy_t *policy, bool bulk_active, int64_t now_us);

uint32_t control_log_policy_dropped(const control_log_policy_t *policy);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_LOG_H */
