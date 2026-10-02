#ifndef PROTOCOL_PLAN_H
#define PROTOCOL_PLAN_H

#include <assert.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * The plan binary format (§5.3), little-endian throughout.
 *
 * A plan is a 12-byte header followed by one 11-byte record per distinct
 * controller state. The device validates the header structurally on commit
 * (§5.3) and the executor memcpy()s the nine state bytes of a record straight
 * into the Pro2 report.
 *
 * The static_asserts below are the point of this header (G-13: they were a
 * design intention, not code). They pin the byte layout that
 * `fixtures/plan/correction.plan.hex` freezes, so a struct drift fails the
 * host-side C test instead of replaying as garbage on the bench.
 */

#define PLAN_MAGIC 0x4C50534Eu /* little-endian bytes 4E 53 50 4C = "NSPL" */
#define PLAN_FORMAT_VERSION 1u
#define PLAN_RECORD_SIZE 11u
#define PLAN_HEADER_SIZE 12u

/** Payload length in bytes for a plan holding @p record_count records. */
#define PLAN_PAYLOAD_SIZE(record_count) \
    (PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * (uint32_t)(record_count))

typedef struct __attribute__((packed)) {
    uint32_t magic;          /* 0x00 u32 LE  PLAN_MAGIC */
    uint8_t format_version;  /* 0x04 u8      PLAN_FORMAT_VERSION */
    uint8_t record_size;     /* 0x05 u8      PLAN_RECORD_SIZE */
    uint16_t record_count;   /* 0x06 u16 LE */
    uint32_t loop_ms;        /* 0x08 u32 LE  == sum(hold_ms) (§5.4) */
} plan_header_t;

typedef struct __attribute__((packed)) {
    uint8_t buttons[3]; /* 0x00 3 B  btn_bits_pro2_t order (§5.1) */
    uint8_t left[3];    /* 0x03 3 B  pack_stick_data(lx, ly) bit order */
    uint8_t right[3];   /* 0x06 3 B  pack_stick_data(rx, ry) bit order */
    uint16_t hold_ms;   /* 0x09 u16 LE */
} plan_record_t;

static_assert(sizeof(plan_header_t) == PLAN_HEADER_SIZE,
              "plan header must be exactly 12 bytes");
static_assert(sizeof(plan_record_t) == PLAN_RECORD_SIZE,
              "plan record must be exactly 11 bytes");

static_assert(offsetof(plan_header_t, magic) == 0, "header.magic must be at 0");
static_assert(offsetof(plan_header_t, format_version) == 4,
              "header.format_version must be at 4");
static_assert(offsetof(plan_header_t, record_size) == 5,
              "header.record_size must be at 5");
static_assert(offsetof(plan_header_t, record_count) == 6,
              "header.record_count must be at 6");
static_assert(offsetof(plan_header_t, loop_ms) == 8, "header.loop_ms must be at 8");

static_assert(offsetof(plan_record_t, buttons) == 0, "record.buttons must be at 0");
static_assert(offsetof(plan_record_t, left) == 3, "record.left must be at 3");
static_assert(offsetof(plan_record_t, right) == 6, "record.right must be at 6");
static_assert(offsetof(plan_record_t, hold_ms) == 9, "record.hold_ms must be at 9");

static_assert(PLAN_MAGIC == 0x4C50534Eu, "plan magic must be 0x4C50534E");
static_assert((PLAN_MAGIC & 0xFFu) == 'N', "plan magic byte 0 must be 'N'");
static_assert(((PLAN_MAGIC >> 8) & 0xFFu) == 'S', "plan magic byte 1 must be 'S'");
static_assert(((PLAN_MAGIC >> 16) & 0xFFu) == 'P', "plan magic byte 2 must be 'P'");
static_assert(((PLAN_MAGIC >> 24) & 0xFFu) == 'L', "plan magic byte 3 must be 'L'");

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_PLAN_H */
