#ifndef PROTOCOL_CONTROL_NFC_TRACE_H
#define PROTOCOL_CONTROL_NFC_TRACE_H

/*
 * The console's NFC traffic, recorded and read out as text (spec §6.6, §12.2
 * validations 4 and 5; issue #36).
 *
 * §12.2's rows 4 and 5 ask three things no offline suite can answer: does the
 * console ever send an NFC subcommand beyond the `0x0C` probe, which offsets
 * does it read, and did the bytes it got match the tag the container placed.
 * The device is the only party that sees both halves of that last question —
 * what it was asked and what it served — so it is the one that can stamp a CRC
 * on the answer.
 *
 * **Nothing is logged while the console is reading.** A scan is ~9 `0x15` round
 * trips the ticket wants timed (~90 ms, §6.4), and one INFO line at 115200 is
 * ~4 ms of UART, so per-command logging would add ~40 ms *inside* the window
 * being measured — the instrument would be the measurement's floor, which is
 * exactly the mistake §7.5's meter was built to avoid. Events accumulate in a
 * RAM ring instead and are drained as text at the *scan's* edge (the console's
 * `0x04`, the container's `UNPLACE_AMIIBO`), when the wire is quiet.
 *
 * **Repeats coalesce.** A scan screen held open with no tag in the field makes
 * the console ask `0x05` over and over; the ring keeps one entry per distinct
 * `(subcommand, offset)` — `0x05` keys on the status byte it was served — and
 * counts the repeats in `reps`, so a 30 s wait does not overflow 16 slots the
 * way it would overflow a naive ring. Distinct events beyond capacity are
 * counted in `drops` rather than silently evicted: a lost offset is a lost
 * measurement, and the count says so.
 *
 * Portable C, no ESP-IDF: `nfc_trace_format()` produces the exact log text the
 * bench parses, so the *text* is what the host suite asserts
 * (`test/host/test_nfc_trace.c`), not a struct the device then renders
 * differently.
 *
 * The line grammar, one line per `nfc_trace_format()` call (`k=v` pairs after
 * the `console nfc:` prefix, so the host parser is one dictionary scan):
 *
 *     console nfc: t=4312 sub=03 len=5 cfg=00e8032c01
 *     console nfc: t=4319 sub=04
 *     console nfc: t=4320 sub=05 status=09 n=61 crc=a1b2 reps=12
 *     console nfc: t=4322 sub=06 len=19
 *     console nfc: t=4330 sub=14 off=0000 want=76 got=76 crc=c3d4 data=…
 *     console nfc: t=4331 sub=15 off=0046 n=67 crc=e5f6 reps=1
 *     console nfc: scan cmds=15 [03=1 04=1 05=13 06=1 14=1 15=9] reps=12 drops=0
 *
 * `t` is milliseconds on the control clock; `off` is the *wire* offset the
 * console asked for (four hex digits); `n` is the response payload length;
 * `crc` is CRC-16/CCITT-FALSE (`control_crc16_update`, the §2.2 call) over the
 * whole response payload for `0x05`/`0x15` — the echo included — and over the
 * captured write bytes for `0x14`. The summary line is the last one a drain
 * produces, and only when at least one command was traced.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* One distinct (subcommand, offset) event. 16 slots cover a full scan — `03`,
 * two `05` statuses, `06`, nine `15` offsets, `04`, and a write or two — with
 * room for the console's retries. */
#define NFC_TRACE_RING 16u

/* A `0x14` write-back is `offset` + `len` + data, and the captured request is
 * 0x50 bytes of payload (`switch2_controller_research/commands.md:62`), so 76
 * bytes of captured data plus slack covers it. */
#define NFC_TRACE_DATA_MAX 80u

/* The `0x03` poll configuration, logged verbatim (the capture's five bytes). */
#define NFC_TRACE_CFG_MAX 5u

/* Worst line: the `0x14` shape with 80 bytes of hex. */
#define NFC_TRACE_LINE_MAX 200u

/* The six subcommands the tag server owns (§6.6). `0x0C` is not here: it is a
 * constant answered in `ns2_codec.c`, which logs its own single line per probe
 * with the same `console nfc:` prefix. */
typedef enum {
    NFC_TRACE_SUB_03 = 0,
    NFC_TRACE_SUB_04,
    NFC_TRACE_SUB_05,
    NFC_TRACE_SUB_06,
    NFC_TRACE_SUB_14,
    NFC_TRACE_SUB_15,
    NFC_TRACE_SUB_COUNT,
} nfc_trace_slot_t;

typedef struct {
    uint8_t sub;  /* the console's subcommand, verbatim */
    uint8_t status; /* `0x05` only: the status byte served */
    uint16_t off;   /* `0x14`/`0x15`: the wire offset asked */
    uint16_t want;  /* `0x14` only: the declared write length */
    uint16_t got;   /* `0x14` only: the bytes actually captured */
    uint16_t n;     /* `0x05`/`0x15`: the response payload length served */
    uint16_t crc;   /* over the response payload, or the captured write bytes */
    uint16_t reps;  /* same-key repeats folded into this entry */
    uint32_t t_ms;  /* the control clock at the first occurrence */
    uint8_t cfg[NFC_TRACE_CFG_MAX]; /* `0x03` only: the poll configuration */
    uint8_t cfg_len;
    uint16_t data_len;              /* `0x14` only: bytes in `data` */
    uint8_t data[NFC_TRACE_DATA_MAX];
} nfc_trace_event_t;

typedef struct {
    nfc_trace_event_t ring[NFC_TRACE_RING];
    size_t head;    /* the pop cursor: the oldest live entry */
    size_t used;
    uint16_t drops; /* distinct events that did not fit */
    uint32_t counts[NFC_TRACE_SUB_COUNT];
    uint32_t total; /* every traced command, repeats included */
    uint32_t reps;  /* every repeat folded into a surviving entry */
    bool dirty;    /* a summary is owed (fed at least once since the drain) */
} nfc_trace_t;

/* The boot state: empty, nothing owed. */
void nfc_trace_init(nfc_trace_t *t);

/*
 * Records one console NFC subcommand. @p payload/@p len are the request bytes
 * after the 8-byte command header; @p rsp/@p rsp_len are what `nfc_tag_command`
 * served (already built, so the CRC is over exactly the bytes that left). A
 * subcommand this module does not trace is ignored.
 */
void nfc_trace_feed(nfc_trace_t *t, uint8_t sub, const uint8_t *payload, size_t len,
                    const uint8_t *rsp, size_t rsp_len, uint32_t t_ms);

/*
 * Formats the next line into @p out and returns its length. Repeated calls
 * drain the ring oldest-first; the summary is the last line, and the call after
 * it returns 0 and leaves the trace reset. Returns 0 immediately when nothing
 * is owed. The formatting never needs more than one slot's worth of state, so
 * a caller may hold its state lock only across this call, not across the
 * drain — the device half does exactly that.
 */
size_t nfc_trace_format(nfc_trace_t *t, char *out, size_t cap);

/* The slot a subcommand belongs in, or `NFC_TRACE_SUB_COUNT` when untraced. */
nfc_trace_slot_t nfc_trace_slot(uint8_t sub);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_NFC_TRACE_H */
