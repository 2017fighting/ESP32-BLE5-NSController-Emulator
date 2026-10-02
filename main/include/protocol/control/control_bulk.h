#ifndef PROTOCOL_CONTROL_BULK_H
#define PROTOCOL_CONTROL_BULK_H

/*
 * The §2.7 bulk path: `LOAD_PLAN` and `PLACE_AMIIBO` are one windowed stream of
 * chunks with different verbs and sizes. This is the private seam between the
 * dispatcher (`control_dispatch.c`) and the staging machine (`control_bulk.c`);
 * the public verb surface is `control_verbs.h`.
 */

#include <stddef.h>
#include <stdint.h>

#include "protocol/control/control_verbs.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Handles one trusted `LOAD_PLAN`/`PLACE_AMIIBO` REQUEST — announce, chunk or
 * commit. Returns the wire length of the reply, or 0 when the chunk is inside an
 * ACK window and carries none (§2.7).
 */
size_t control_bulk_request(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                            size_t out_cap);

/*
 * The device-side structural check of §5.3: magic, `format_version`,
 * `record_size == 11`, and `payload_len == 12 + 11·record_count`. Returns
 * `CONTROL_ERR_NONE` when the bytes are a plan, or the §2.5 code to raise —
 * `CONTROL_ERR_BAD_PLAN`, or `CONTROL_ERR_PLAN_TOO_LARGE` when the transfer
 * exceeds @p capacity. On success @p record_count_out receives `record_count`;
 * on failure it is zeroed.
 *
 * This is what stops a truncated-but-CRC-valid transfer from replaying as
 * garbage, so it is asserted directly on the host.
 */
uint8_t control_plan_check(const uint8_t *bytes, size_t len, size_t capacity,
                           uint16_t *record_count_out);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_BULK_H */
