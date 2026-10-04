#ifndef CONTROLLER_NFC_TAG_H
#define CONTROLLER_NFC_TAG_H

/*
 * The NFC state machine and the tag server (spec §4.9, §6.5, §6.6, §7.3 step 6;
 * issue #25).
 *
 * The device is a byte-sink here (ADR-0011): it holds one 540-byte NTAG215 image
 * in RAM, slices it when the console asks, and does no cryptography. Two halves
 * meet in this module:
 *
 *  - the **control plane's** `PLACE_AMIIBO`/`UNPLACE_AMIIBO` effects, which put
 *    the sealed bytes in and take them out (`nfc_tag_place`/`nfc_tag_unplace`);
 *  - the **console's** command `0x01` subcommands `0x03`/`0x04`/`0x05`/`0x06`/
 *    `0x14`/`0x15`, which are the reads and writes over that buffer
 *    (`nfc_tag_command`).
 *
 * It is portable C on purpose: the whole state machine — placement, the
 * tag-absent gap, the polling edges and the page-wise slice — is asserted on the
 * host (`test/host/test_nfc_tag.c`) instead of inferred from a bench capture.
 * The ESP-IDF glue is `control_parser.c` (the singleton, the report byte and the
 * `SCAN_ENDED` event) and `ns2_codec.c` (the command router).
 *
 * **Two outputs, and the difference matters (§4.9 against §3.2).** The module
 * produces:
 *
 *  - `nfc_tag_state()` — the **report byte**, HID input report `0x09` offset
 *    `0x0C`. It is the mode's physical expression, so it is `0x02` while a tag is
 *    placed and `0x00` otherwise. It moves only on `place`/`unplace`; a console
 *    that polls while the device is in `IDLE` or `MACRO` changes nothing here,
 *    which is what keeps "only one mode at a time" a property of the wire.
 *  - `nfc_tag_polling()` — `STATUS.console_polling`, the console's own level
 *    (`IDLE`/`POLLING`/`TAG_DETECTED`). It moves on the console's `0x03`/`0x04`/
 *    `0x05` regardless of mode, because a game may open its amiibo menu at any
 *    time, and the container rotates on its fall to `IDLE` (§3.2, §6.5).
 *
 * Collapsing the two would either lie about the mode or lose the container's
 * rotation edge; keeping them separate is why a `POLLING` byte value is legal in
 * the vocabulary and never on the wire (a placed tag is the only `AMIIBO` state,
 * so the byte is `0x00` or `0x02` and nothing between).
 *
 * **Two byte sequences are deliberately not decided here.** The console-facing
 * *offset spaces* of `0x14`/`0x15` are unverified against a real console — the
 * canonical capture (`switch2_controller_research/commands.md:64-66`) shows a
 * read at `0x46` and a write at `0x0000`, which are not the same coordinate; this
 * module serves and writes plain byte offsets into the 540-byte image and leaves
 * the mapping to bench validation 5 (§12.2). The shapes it does implement are the
 * captured ones:
 *
 *     0x15 request   offset u16 (LE)
 *     0x15 response  0x00 · offset u16 (LE) · up to 64 bytes of the image
 *     0x14 request   offset u16 (LE) · len u16 (LE) · len bytes
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* §6.2: the tag is the raw 540-byte NTAG215 image — 135 pages x 4 B. */
#define NFC_TAG_SIZE 540u

/* §7.7/§7.4: a `0x15` response is 8 + 3 + 64 = 75 bytes on the wire, so one
 * read carries 64 bytes — 16 pages — and the whole tag is 9 round trips. */
#define NFC_TAG_READ_CHUNK 64u

/* §6.6's open offset-space question, as the bench's one knob (§12.2 validation
 * 5, issue #36). The canonical capture reads at wire offset `0x46` the bytes
 * that sit at tag-image offset `0x0A` — the corpus's own page 2 carries
 * `0F E0`, so the capture's leading `0f e0` is the image verbatim and §6.6's
 * "first 16-bit word transposed" was a misreading of the lock bytes — which
 * makes the hypothesis *wire = image + 0x3C*, unverified. The spec's default
 * is **plain** (`0`): the console's wire offset is the image offset. A bench
 * build can define this to the hypothesised base and test the mapping in one
 * reflash; if validation 5 confirms a constant, the confirmed value becomes
 * the spec's and this default changes with a §6.6 amendment.
 *
 * Reads (`0x15`) only. The one captured write (`0x14` at wire `0x0000`) fits no
 * offset shift — its payload opens on the write counter, not the image — so
 * writes stay plain until a capture says otherwise. */
#ifndef NFC_TAG_READ_WIRE_BASE
#define NFC_TAG_READ_WIRE_BASE 0u
#endif

/* The #39 game-surface bench's one firmware question (#36's record §4): does
 * the console's read flow require the report byte to follow the *reader's*
 * lifecycle — `0x01` while the console polls, `0x02` when the tag is detected
 * — rather than §4.9's placement-only byte (`0x00`/`0x02`, never `0x01`)?
 * Both of §12.2 row 4's orderings errored with the placement-only byte: the
 * console polled (`0x03`), was told a tag existed (`0x05`→`09`+UID), and
 * aborted with the game's read-error chime before any `0x06`/`0x15`. This knob
 * makes the byte the polling level's image (IDLE→0x00, POLLING→0x01,
 * TAG_DETECTED→0x02 — the enum's own values), written from the 10 ms tick so
 * every polling edge reaches the wire. Default OFF: §4.9's split stands until
 * this bench says otherwise. */
#ifndef NFC_TAG_BYTE_FOLLOWS_POLLING
#define NFC_TAG_BYTE_FOLLOWS_POLLING 0
#endif

/* The byte's value while `0x06`'s read is armed (#39's suspect 2, now with the
 * NS1 decode behind it): a real controller reports the read's *completion* on
 * its status channel, and the NS1 capture of the whole exchange ends the data
 * phase with state `0x04` — the `2a 00 05 00 00 09 31 04 …` trailer after the
 * two pushed data packets (`elmagnificogi_nsre` `nfc_debug/output_receive.txt`,
 * the same trailer Poohl's `mcu.py` queues). The NS2 replaced the push with the
 * console's `0x15` pulls, and the input report's NFC byte (values 0x00–0x07)
 * is the only completion signal left in the input stream — so the bench's
 * first try is `0x04` (read done), replacing the earlier blind `0x03` guess.
 * Default `0x03`: the value the #39 build shipped, so nothing changes unless
 * a bench build asks for the hypothesis. */
#ifndef NFC_TAG_READ_DONE_BYTE
#define NFC_TAG_READ_DONE_BYTE 0x03u
#endif

/* The follow-up push after `0x06` (the register-screen session, 2026-10-04):
 * with the byte at `0x04` and a full-log tee, the console sent *nothing* for
 * the whole 3.0 s window (`d0 07` reads as a 2000 ms deadline) — it waits for
 * a device-initiated notification, and the NS1 P3 trailer says that
 * notification is a *status line* with the read-complete state. Hypothesis:
 * the NS2 `0x05` payload's byte 0 *is* the state (09 = tag/POLL_AGAIN,
 * 00 = none), so the completion push is a sub-0x05 notification whose payload
 * opens `NFC_TAG_NOTIFY_READ_DONE_STATE` (default `04`, the NS1 read-complete
 * value). Default OFF; device-side only. */
#ifndef NFC_TAG_NOTIFY_READ_DONE
#define NFC_TAG_NOTIFY_READ_DONE 0
#endif
#ifndef NFC_TAG_NOTIFY_READ_DONE_STATE
#define NFC_TAG_NOTIFY_READ_DONE_STATE 0x04u
#endif

/* The push variant after the status-only push was ignored (same session):
 * the NS1 answer to the read command was the *data itself* — P1/P2 pushed in
 * the input stream — so this knob pushes the whole 540-byte tag as sub-0x15-
 * shaped notifications right after the `0x06` ACK (one `0x05` read-done status
 * first, then 64-byte chunks at plain image offsets 0, 64, … 512). Default
 * OFF; device-side only; the console's own `0x15` asks still take the normal
 * response path. */
#ifndef NFC_TAG_PUSH_READ_DATA
#define NFC_TAG_PUSH_READ_DATA 0
#endif

/* The served-space pad (register-screen session, cycle 2): once pulling, the
 * console samples 64-aligned offsets across a space larger than the image —
 * asks at 0x240/0x280/0x2c0 were observed against the 540-byte tag — and an
 * out-of-range ask currently answers *nothing*, which aborts the cycle. This
 * knob pads the served space to `NFC_TAG_READ_PAD_TO` bytes, image verbatim at
 * offset 0 and zeros beyond, so every in-space ask gets a well-formed chunk.
 * Default 0 (the spec's plain 540-byte space). */
#ifndef NFC_TAG_READ_PAD_TO
#define NFC_TAG_READ_PAD_TO 0
#endif
#if NFC_TAG_READ_PAD_TO != 0 && NFC_TAG_READ_PAD_TO <= NFC_TAG_SIZE
#error "NFC_TAG_READ_PAD_TO must exceed the image size or be 0"
#endif

/* The P1-prefix buffer view (register-screen session, cycle 5 — the session's
 * synthesis): the console's `0x15` pull at wire 0x0000 follows our `04` status
 * answer within 30 ms, and the canonical capture's wire 0x46 maps to image
 * 0x0A — a constant 0x3C=60-byte shift. The NS1 P1 packet's framing minus its
 * UID field is exactly 60 bytes (15 header + 4 zeros + 32 magic + 9 echoed
 * page-ranges), so the served space becomes [60 B framing][540 B image] and
 * the image starts at wire 0x3C. Default OFF: the plain image-at-0 view. */
#ifndef NFC_TAG_BUFFER_P1_PREFIX
#define NFC_TAG_BUFFER_P1_PREFIX 0
#endif
#if NFC_TAG_BUFFER_P1_PREFIX != 0
#define NFC_TAG_P1_PREFIX_SIZE 60u
#define NFC_TAG_SERVED_SIZE (NFC_TAG_P1_PREFIX_SIZE + NFC_TAG_SIZE)
#else
#define NFC_TAG_SERVED_SIZE NFC_TAG_SIZE
#endif

/* The `0x05` answer's lifecycle (register-screen session, cycle 4): the console
 * re-asks `0x05` with repeats through the whole read window — it is polling
 * for the read to complete — and the NS1 lifecycle carries that on the status
 * line's state byte (`09` tag → `04` read done). The 04-as-a-push variant was
 * discarded as unrequested; this knob flips the *answer* to a pending `0x05`
 * to `NFC_TAG_NOTIFY_READ_DONE_STATE` once the armed read's data has been
 * served, back to `09` on the next poll cycle. Default OFF. */
#ifndef NFC_TAG_STATUS_DONE_WHEN_READ
#define NFC_TAG_STATUS_DONE_WHEN_READ 0
#endif

/* How long the read-done byte is held before the byte returns to the tag-
 * present `0x02` (register-screen session, cycle 3): the NS1 lifecycle ends
 * its read at `04` only *between* the data phase and the return to `09`
 * (tag still in field) — the console's asks land in the first ~120 ms after
 * `0x06`, then it waits out its ~3 s deadline, which smells like a console
 * blocked on the byte returning. Default 0 holds the done byte until the
 * console restarts or stops polling (the previous behaviour). */
#ifndef NFC_TAG_READ_DONE_MS
#define NFC_TAG_READ_DONE_MS 0
#endif

/* §6.1/§6.3: the identity is the seven-byte NFC UID. */
#define NFC_TAG_UID_SIZE 7u

/* §3.3's `0x05` response: status, flags, UID length and UID in the captured
 * layout (`switch2_controller_research/commands.md:33`). */
#define NFC_STATUS_RESPONSE_SIZE 61u

/* §6.5's tag-absent gap, held on the wire before the new tag answers. Two report
 * periods at the 10 ms grid (`CONFIG_FREERTOS_HZ=100`, §7.5), so the console
 * necessarily samples `0x00` — a gap the reporter can lose is not a guarantee.
 * The designer's note that the requirement is unproven (G-6) is about the console
 * needing the gap, not about whether this device emits one. */
#define NFC_TAG_GAP_MS 20u

/* §4.9/§3.2: one vocabulary for the report byte and for `console_polling`. The
 * byte's reachable values are `IDLE` and `TAG_DETECTED`. */
enum {
    NFC_STATE_IDLE = 0,         /* no tag placed / the console is not asking */
    NFC_STATE_POLLING = 1,      /* the console is asking, no tag placed */
    NFC_STATE_TAG_DETECTED = 2, /* a tag is placed and answering */
};

/* §3.3's `0x05` first byte: `0x09` = tag detected, `0x00` = no tag. */
#define NFC_STATUS_TAG_DETECTED 0x09u
#define NFC_STATUS_NO_TAG 0x00u

/* §2's command `0x01` subcommands this server answers. `0x0C` (the PN7160
 * capability probe) stays with `ns2_codec.c` — it is a constant, not state. */
enum {
    NFC_CMD_START_POLLING = 0x03,
    NFC_CMD_STOP_POLLING = 0x04,
    NFC_CMD_GET_STATUS = 0x05,
    NFC_CMD_READ_DEVICE = 0x06,
    NFC_CMD_WRITE_BUFFER = 0x14,
    NFC_CMD_READ_BUFFER = 0x15,
};

/*
 * The two effects this module cannot perform itself. Both are edges, never
 * levels, and a NULL callback is a no-op (the host's state-only tests).
 */
typedef struct {
    void *ctx;
    /* The report byte changed: write it into the HID report (§4.9). Fires only
     * on `place`/`unplace`, because only those move the byte. */
    void (*state_changed)(void *ctx, uint8_t state);
    /* The console stopped asking for a tag (§3.3 `SCAN_ENDED`, §6.5) — the
     * container's cue to mint and push the next identity. */
    void (*scan_ended)(void *ctx);
} nfc_tag_events_t;

typedef struct {
    uint8_t tag[NFC_TAG_SIZE];
    bool placed;     /* the tag is answering reads */
#if NFC_TAG_STATUS_DONE_WHEN_READ
    bool read_done;  /* the armed read has been served: `0x05` answers 04 */
#endif
    bool staged;     /* a replacement's bytes are committed and the gap is open */
    uint8_t state;   /* the report byte: NFC_STATE_IDLE or NFC_STATE_TAG_DETECTED */
    uint8_t polling; /* the console's level: NFC_STATE_IDLE/POLLING/TAG_DETECTED */
    uint32_t gap_until_ms;
    nfc_tag_events_t events;
} nfc_tag_t;

/* §4.8's boot state: nothing placed, the byte `0x00`, the console silent. */
void nfc_tag_init(nfc_tag_t *nfc);

void nfc_tag_set_events(nfc_tag_t *nfc, const nfc_tag_events_t *events);

/*
 * §4.3's `PLACE_AMIIBO` physical half: commit @p tag. @p len must be
 * `NFC_TAG_SIZE`; anything else is refused. @p now_ms is the tick clock the gap's
 * deadline is measured on (`nfc_tag_tick`).
 *
 * **The gap lives here (§6.5).** A replace drops the report byte to `0x00` — and
 * `0x05`/`0x15` answer "no tag" — for `NFC_TAG_GAP_MS` before the new tag
 * answers, so the console samples a tag-absent field rather than an atomic
 * swap. A device with no tag yet has nothing to remove, so a first placement has
 * no gap. Whether the console *needs* the gap is an open question (G-6); the
 * design's answer is to emit it as the safe superset, and this is that emission.
 */
void nfc_tag_place(nfc_tag_t *nfc, const uint8_t *tag, size_t len, uint32_t now_ms);

/*
 * Advances the gap. Called from the firmware's 10 ms control task
 * (`control_parser.c`); a no-op when no replace is pending. The deadline is
 * `now_ms`-based, not an accumulated tick count, so a late tick cannot extend the
 * gap, only observe it as over.
 */
void nfc_tag_tick(nfc_tag_t *nfc, uint32_t now_ms);

/*
 * §4.3's `UNPLACE_AMIIBO`/mode-exit physical half: the tag stops answering and the
 * report byte returns to `0x00`. The bytes are retained (a later `place`
 * overwrites them) and any `0x14` write-back is discarded with them.
 */
void nfc_tag_unplace(nfc_tag_t *nfc);

/* The HID report's `nfc_state` byte (§4.9). */
uint8_t nfc_tag_state(const nfc_tag_t *nfc);

/* `STATUS.console_polling` (§3.2): the console's own level. */
uint8_t nfc_tag_polling(const nfc_tag_t *nfc);

bool nfc_tag_placed(const nfc_tag_t *nfc);

#if NFC_TAG_STATUS_DONE_WHEN_READ
/* Mark the armed read as served (or clear it): `0x05` answers flip to the
 * read-done state until the next poll cycle. See `nfc_tag.h`'s knob. */
void nfc_tag_set_read_done(nfc_tag_t *nfc, bool done);
#endif

/* §6.3: `UID[0..2]` then `UID[3..6]`, skipping the `BCC0` check byte at byte 3. */
void nfc_tag_identity(const nfc_tag_t *nfc, uint8_t out[NFC_TAG_UID_SIZE]);

/*
 * Handles one command `0x01` subcommand. @p payload is the bytes *after* the
 * 8-byte command header and @p len is how many of them arrived. Returns the
 * response length written to @p out, or 0 when the subcommand has no response or
 * is not one this server owns.
 */
size_t nfc_tag_command(nfc_tag_t *nfc, uint8_t subcmd, const uint8_t *payload, size_t len,
                       uint8_t *out, size_t out_cap);

#ifdef __cplusplus
}
#endif

#endif /* CONTROLLER_NFC_TAG_H */
