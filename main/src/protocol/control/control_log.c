#include "protocol/control/control_log.h"

#include <string.h>

void control_log_policy_init(control_log_policy_t *policy)
{
    if (policy != NULL) {
        memset(policy, 0, sizeof(*policy));
    }
}

bool control_log_policy_allow(control_log_policy_t *policy, bool bulk_active, int64_t now_us)
{
    if (policy == NULL) {
        return true;
    }

    if (!bulk_active) {
        /* Leaving bulk re-arms the first line of the next transfer. */
        policy->bulk_last_emit_us = 0;
        return true;
    }

    if (policy->bulk_last_emit_us == 0 ||
        now_us - policy->bulk_last_emit_us >= CONTROL_LOG_BULK_MIN_INTERVAL_US) {
        policy->bulk_last_emit_us = now_us;
        return true;
    }

    policy->dropped++;
    return false;
}

uint32_t control_log_policy_dropped(const control_log_policy_t *policy)
{
    return policy == NULL ? 0 : policy->dropped;
}
