#ifndef TRANSPORT_H
#define TRANSPORT_H

#include "buffer/zc_buffer.h"

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LOG_TRANSPORT "transport_layer"

typedef struct {
  const char *name;
  int (*open)(void *instance, void *config);
  void (*close)(void *instance);

  /**
   *  @brief bind the buffer and start RX.
   */
  int (*activate_rx)(void *instance, zc_ringbuf_t *rb);

  /**
   * @brief pause (for flow control)
   */
  int (*deactivate_rx)(void *instance);

  /**
   * @brief send api, no blocking
   */
  int (*submit_tx)(void *instance, const uint8_t *data, uint32_t len);

  /**
   * @brief wait for tx done, use to key commands
   */
  int (*flush_tx)(void *instance, uint32_t timeout_ms);

  /**
   * @brief check if tx is ready
   */
  bool (*is_ready)(void *instance);
} transport_vtable_t;

typedef struct {
    const transport_vtable_t *ops;        // function table
    void *hardware_ctx;                   // backend hardware contxt
    zc_ringbuf_t *rx_buffer;              // rx ring buffer

    uint32_t tx_seq;
    uint32_t rx_seq;
    uint64_t stats_tx_bytes;
    uint64_t stats_rx_bytes;
    uint64_t stats_rx_overflow;
    /*
     * The §7.5/§12.2-row-2 ring instrument (bench #34). Under a §2.7 blast the
     * zc ring being full is *steady state*, not a hazard: `chunk_size = 256`
     * means one chunk frame out-sizes the ring, and the producer simply yields
     * (`pdMS_TO_TICKS(1)` is 0 ticks at 100 Hz) until the parser takes bytes.
     * The hazard lives one tier down — bytes the UART driver drops when its
     * own ring backs up — so the meter grades that distance:
     *   stats_rx_ring_hw     zc-ring occupancy high-water (wrap-aware, via
     *                        `zc_used` — contiguous free space is not
     *                        occupancy, see its comment);
     *   stats_rx_spin        producer iterations that found the zc ring full
     *                        (yield-spins; throughput context, not danger);
     *   stats_rx_backlog_hw  UART-driver RX backlog high-water,
     *                        0..stats_rx_backlog_cap: 0..tens is the parser
     *                        keeping pace, == cap is the §7.5 "data loss, not
     *                        backpressure" cliff.
     * One writer (the RX task), benign readers; reset per bulk transfer by
     * `transport_rx_ring_reset()`. The frame loss itself is counted where it
     * happens — the CONTROL decoder's silent drops (§2.8) — which the staging
     * close line also reports, as a delta.
     */
    uint32_t stats_rx_ring_hw;
    uint32_t stats_rx_spin;
    uint32_t stats_rx_backlog_hw;
    uint32_t stats_rx_backlog_cap;  /* the driver ring's size, set at activate */
} transport_handle_t;

typedef struct {
    uint32_t high_water;      /* zc-ring occupancy, bytes, wrap-aware */
    uint32_t spins;           /* producer iterations that found the zc ring full */
    uint32_t backlog_hw;      /* UART-driver RX backlog high-water, bytes */
    uint32_t backlog_capacity;/* the UART driver ring's size, bytes */
    uint32_t capacity;        /* the zc ring's size in bytes */
} transport_rx_ring_stats_t;

int transport_init(void);
int transport_start(void);

/* The #34 instrument: per-transfer RX-ring pressure, reset on staging open. */
void transport_rx_ring_reset(void);
void transport_rx_ring_get(transport_rx_ring_stats_t *out);

#ifdef __cplusplus
}
#endif

#endif // TRANSPORT_H
