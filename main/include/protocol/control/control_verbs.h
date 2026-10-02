#ifndef PROTOCOL_CONTROL_VERBS_H
#define PROTOCOL_CONTROL_VERBS_H

/*
 * The ten verbs' protocol semantics over the §2.7 bulk path (spec §2.4–§2.9,
 * §4.3, §5.3, chapter 7 step 3 continued).
 *
 * The framing and the decode-order rules live in `control_protocol.h`; this
 * header carries the *state* a trusted REQUEST moves. It stays free of ESP-IDF
 * so the whole verb surface — including the windowed bulk ACK, the atomic commit
 * and the plan structural check — is exercised on the host
 * (`test/host/test_control_verbs.c`) rather than on the bench.
 *
 * The mode axis and the §3.3 EVENT surface moved out of this file with #23:
 * `control_mode.{h,c}` is the only place a mode moves (so `MODE_CHANGED` has one
 * emission site) and owns the panic stop, and `control_events.{h,c}` owns the
 * event kinds and the `LOOP_COMPLETED` rate limit.
 *
 * The boundary with the plan executor (#24) and the amiibo tag server (#25) is
 * `control_effects_t`: the verb layer owns what the wire can observe (mode,
 * staging, plan/tag state, `last_error`, `last_stop_reason`) and calls a callback
 * for every physical effect it cannot perform. Those tickets supply the real
 * callbacks; the host tests supply a double.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol/control/control_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

/* §6.2: a tag is the raw 540-byte NTAG215 image — 135 pages x 4 B. */
#define CONTROL_TAG_SIZE 540u

/* §2.7: the ACK window. The device ACKs once it has consumed this many bytes
 * since its last ACK, and may ACK earlier (that is a permitted outcome, not a
 * protocol change). */
#define CONTROL_BULK_ACK_WINDOW 4096u

/* §2.9: `report_interval_ms` u16, `led` u8. */
#define CONTROL_CONFIG_SIZE 3u

/* §2.7: every bulk frame's payload begins with a one-byte `op`. */
enum {
    CONTROL_BULK_OP_ANNOUNCE = 1,
    CONTROL_BULK_OP_CHUNK = 2,
    CONTROL_BULK_OP_COMMIT = 3,
};

/* The staging axis of §4.1 is NONE / PLAN / TAG: one in-flight transfer at a
 * time, and a transfer is atomic from the container's side. */
typedef enum {
    CONTROL_STAGE_NONE = 0,
    CONTROL_STAGE_PLAN = 1,
    CONTROL_STAGE_TAG = 2,
} control_stage_t;

/*
 * The physical effects of a verb, delegated. A NULL callback is a no-op, which
 * is what the firmware glue uses until #23-#25 land; the host tests install a
 * double and assert the dispatch. `ctx` is the caller's.
 */
typedef struct {
    void *ctx;
    /* START: arm the executor at frame 0 and emit neutral first (§4.6). Returns
     * a §2.5 code: `CONTROL_ERR_NONE` once armed, or the refusal the plan earns
     * — a plan the device cannot replay (a hand-built zero-record plan, or bytes
     * that fail the §5.3 check) is a typed `ERROR` rather than a `MACRO` the
     * container believes started. A rejected arm touches nothing (§2.3). */
    uint8_t (*start_macro)(void *ctx);
    /* STOP: neutral release, stop the executor, unplace any tag, return to IDLE
     * (§4.3, §4.6). @p reason is the §3.2 `last_stop_reason`. */
    void (*stop)(void *ctx, uint8_t reason);
    /* PLACE_AMIIBO commit: drive the `nfc_state` byte and serve @p tag (§6.5). The
     * tag-absent gap on a replace lives here (chapter 6, #25). */
    void (*place_tag)(void *ctx, const uint8_t *tag, size_t len);
    /* UNPLACE_AMIIBO: stop answering, keep the bytes (§4.3). */
    void (*unplace_tag)(void *ctx);
    /* PAIR_UNPAIR: forget the bond and go pairable (ADR-0013). */
    void (*pair_unpair)(void *ctx);
    /* CONFIG applied at the boundary §2.9 fixes. */
    void (*apply_config)(void *ctx, uint16_t report_interval_ms, uint8_t led);
} control_effects_t;

/*
 * The protocol-visible truth plus the §2.7 staging state.
 *
 * The staging buffers are caller-owned. A plan stages *directly* into
 * `plan_stage` and the commit makes those bytes the active plan with no copy
 * (§7.4), so `plan` aliases `plan_stage`. A tag stages into `tag_stage` and the
 * commit copies 540 B into `tag`, which is what makes the atomic replace of
 * §4.3 not expose a half-written tag.
 *
 * `plan_slots = 1` (§2.6) is why a plan announce *supersedes* the previous plan:
 * there is one plan buffer, so staging a replacement over it cannot leave the
 * old plan intact — and §2.7 rule 6 says exactly that, `STATUS` reports
 * `plan=none` until the commit lands.
 */
typedef struct {
    control_hello_t hello;
    control_status_t status;

    /* §2.7 staging, one transfer at a time. */
    control_stage_t stage;
    uint8_t *plan_stage;   /* caller-owned, up to CONTROL_PLAN_CAPACITY_BYTES */
    size_t plan_stage_cap;
    uint8_t tag_stage[CONTROL_TAG_SIZE];
    uint32_t stage_total;
    uint32_t stage_next;   /* next expected offset, what an ACK carries */
    uint32_t stage_acked;  /* offset of the last ACK */
    uint8_t stage_hash[16];
    bool stage_has_hash;   /* LOAD_PLAN carries one; PLACE_AMIIBO does not */

    /* The committed plan (§5.3). `plan` aliases `plan_stage`; no copy. */
    bool plan_committed;
    const uint8_t *plan;
    uint32_t plan_len;
    uint8_t plan_hash[16];
    uint16_t plan_records;

    /* The placed tag (§6): the active image, not the staging one. */
    bool tag_placed;
    uint8_t tag[CONTROL_TAG_SIZE];
    uint8_t tag_identity[7]; /* the UID, §6.1/§6.3 */

    /* §2.9 CONFIG is volatile and applies at the next loop boundary in `MACRO`,
     * and at once in `IDLE`/`AMIIBO`. A value accepted during `MACRO` is held
     * here, unapplied, until the executor crosses a boundary. */
    uint16_t config_report_interval_ms;
    uint8_t config_led;
    bool config_pending;

    /* §3.3: where EVENT frames go, and the LOOP_COMPLETED rate limit's one bit.
     * A loop boundary emits only when the previous event has been superseded by
     * a STATUS reply; the poll is the clock, so a short macro cannot saturate
     * the link (§3.3). */
    control_event_sink_t events;
    bool loop_event_pending;

    control_effects_t fx;
} control_state_t;

/*
 * Brings up the boot state of §4.8 — `IDLE`, nothing staged, nothing placed,
 * `ADVERTISING`/`UNPAIRED` — and points the plan staging at a caller-owned
 * buffer. `boot_id` is fresh on every power cycle (§2.6).
 */
void control_state_init(control_state_t *st, uint32_t boot_id, uint8_t *plan_stage,
                        size_t plan_stage_cap);

/* Installs the physical-effect callbacks (#23-#25, or a host double). */
void control_state_set_effects(control_state_t *st, const control_effects_t *fx);

/*
 * Handles one trusted REQUEST (§2.10 step 7). Returns the wire length of the
 * reply written to @p out, or 0 when there is no reply — which happens for one
 * case only: a chunk in the middle of an ACK window carries no reply (§2.7).
 */
size_t control_handle_request(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                              size_t out_cap);

/*
 * The canonical rejection: encodes `ERROR(code, detail)` (§2.5) and records the
 * identical pair in `STATUS.last_error`, so a rejection and a stop reason are
 * read the same way from either surface (§3.2).
 */
size_t control_reject(control_state_t *st, uint8_t code, uint32_t detail, uint8_t *out,
                      size_t out_cap);

/*
 * An accepted reply: encodes `REPLY(verb, payload)` and clears `last_error`
 * (§3.2 — it is cleared by the next successful verb, not by a read).
 */
size_t control_ack(control_state_t *st, uint8_t verb, const uint8_t *payload, size_t len,
                   uint8_t *out, size_t out_cap);

/* Clears `STATUS.last_error`. A bulk chunk inside an ACK window is accepted
 * without a reply, so it clears the pair without going through control_ack(). */
void control_clear_error(control_state_t *st);

/*
 * §2.9: applies a CONFIG that was deferred because `MACRO` was running. The
 * executor (#24) calls this at the loop boundary; a no-op when nothing is
 * pending. `IDLE`/`AMIIBO` never leave anything pending — they apply at once.
 */
void control_config_apply_at_boundary(control_state_t *st);

/* Installs the §3.3 EVENT sink. The firmware points it at `control_link_write`;
 * a NULL sink is a no-op, which is what the host's verb-only suites use. */
void control_state_set_event_sink(control_state_t *st, const control_event_sink_t *sink);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_VERBS_H */
