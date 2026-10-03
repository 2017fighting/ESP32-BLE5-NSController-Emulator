# The #34 RX-ring bench: the 256 B ring absorbs the ACK window at the design baud (issue #34)

**Scope:** §12.2 validation row 2 — *does the 256 B RX ring at a 100 Hz tick actually absorb the
§2.7 ACK window?* — answered by measurement, with both fallbacks (raise the ring, shrink the
window) **not needed**: the ring stays 256 B, the window stays 4096 B. **Date:** 2026-10-03.
**Hardware on test host:** the same ESP32-S3-N16R8 on the CH9102 bridge as #33 (serial
`5C930639851`, `/dev/cu.usbmodem5C930639851`, resolved by serial number), arm64 macOS host plus
the OrbStack-forwarded Linux container path (§10.7), the **INFO deployment build**
(`MCU_DEBUG` off, `LOG_MAXIMUM_LEVEL=3`, 115200). The device was **not bonded** during the runs
(`STATUS`: `console_link=0 bond=0`).

The one-line answer the ticket asked for: **at 115200 the 256 B ring absorbs the 4096 B window
with the ring full being the blast's steady state rather than a hazard — 0 ACK stalls, 0
untrusted frames, every transfer clean to the 65,528 B capacity maximum on both host stacks,
with the real library's largest plan (杏仁巢穴宏, 3,356 B) among the rows — while at 921600 the
same instrument reads the cliff directly: 21 untrusted frames per 4,093 B and 542 per
65,528 B, with the driver ring at its full 256/256 B.**

The four numbers the ticket asked to record:

| What | Answer |
| --- | --- |
| The ring size | **256 B, unchanged** (zc ring 256 B + driver ring 256 B + the HW FIFO behind it) |
| The window that holds | **4096 B, unchanged** — §2.7's constant; at the design baud it never depends on early ACKs |
| The measured stall count | **0** — 0 window retries across all 28 transfers of rows 1–2 (the 115200 rows) and every 115200 confirmation run, 0 dup-ACK storms, host-side bad-CRC 0 |
| The largest plan that transfers cleanly | **65,528 B** (5,956 records — the largest the advertised 65,536 B capacity accepts), ×2 per row on both host stacks |

---

## 1. The instrument

Two meters, one answer — a host-side stall count alone proves less than it seems, because a
zero needs a meter that demonstrably *can* read the failure.

**The device side** is a per-transfer meter added to the firmware (#34's own change, in the
deployment image): one INFO line, printed as staging closes —

```text
I (618932) control: staging closed: total=65528B ring hw=254/256 spins=180842 backlog=232/256 drops=0
```

- `ring hw` — the zc ring's occupancy high-water, **wrap-aware** (`zc_used`): the obvious
  computation — capacity minus `zc_reserve`'s return — measures the *contiguous* free run,
  and an empty ring whose head sits on the last byte reads as full under it; the review of
  this diff caught exactly that artifact in the meter's first cut, and the numbers below are
  from the corrected one. **A near-full ring is the
  blast's steady state, not a hazard**: one 256 B chunk frame out-sizes the 256 B ring, so
  during a §2.7 blast the producer runs ahead of the parser by construction.
- `spins` — producer iterations that found the ring full. The discovery baked into this
  number: the producer's backoff is `vTaskDelay(pdMS_TO_TICKS(1))`, and **`pdMS_TO_TICKS(1)`
  is 0 ticks at 100 Hz** — a yield, not a block. The "100 Hz tick" of the ticket's question
  therefore does not gate the RX path at all; the producer spins at core speed until the
  parser takes bytes. `spins` is throughput context (≈3 µs each), never a danger signal.
- `backlog` — the UART driver's RX backlog high-water (`uart_get_buffered_data_len`, sampled
  every producer pass). **The tier that actually drops bytes**: 0–248 of 256 B is the parser
  keeping pace; == 256 B is the §7.5 "data loss, not backpressure" cliff.
- `drops` — the CONTROL decoder's silent-drop delta for the transfer (§2.8): frames that
  arrived but could not be trusted, a corrupted chunk included. This is the number that
  separates "ring full but nothing lost" from data loss.

The line rides the log noise §2.2's framing exists to survive (written under the shared TX
lock, after the staging window closes so the §2.2 bulk rate limit no longer applies), and it
is why `main.c` raises the `control` tag to INFO: the deployment build silences `"*"` to WARN
at boot, so an INFO meter would never print. One line per bulk transfer is the entire INFO
budget the deployment build keeps.

**The host side** is `scripts/bench_rx_ring.py` (+ `scripts/test_bench_rx_ring.py`, 19 checks):
#33's counting uploader (`upload_plan_counted` — the synchronous mirror of the container's
`FrameIO.bulk`) carrying the stall count, plus the harvest of the device line out of the link's
captured noise segments and a verdict that states the ticket's four numbers with the failures
behind them. Two expectations encode the two kinds of row: `clean` (the design point — any
stall, any untrusted frame, any silent meter, or a backlog at capacity is a failure) and
`degenerate` (the 921600 positive control — it *fails* if the meter reads no loss, because a
meter that cannot see saturation cannot certify absorption either).

Two bench-side defects the on-board runs caught, both now pinned by tests:

- **A glued noise segment hid the run's first meter line** — the boot banner's last segment is
  cut mid-line by a frame delimiter and has no trailing newline, so concatenating noise
  segments glued it onto the meter line and broke the line-start skin check. Noise segments
  join with a newline now; a log line is written atomically under the TX lock, so a line is
  never itself split across segments.
- **A lying `STATUS` echo must fail the row** (§8.5's never-trust rule), which is also why
  every commit is verified against `plan_hash` + `frame_count` + `IDLE`, and `boot_id` is read
  before and after every run: one value means the link never reset.

## 2. The measurements

All rows on the final image (the instrument + this ticket's firmware, tree defaults, 115200;
`2×` = two repeats). Real plans compiled from the reference mount (`$REFERENCE_ROOT/
switch-controller-macro/宏`) — the loader asserts §5.7's anchor (杏仁: 304 records, 3,356 B,
`d2717773e32486c9`) before the run counts.

| # | Host | Baud | Rows | Stalls | Drops | Backlog hw | Ring hw | Largest clean |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | macOS native | 115200 | 4 real plans ×2 (793–3,356 B) + 540/4,093/65,528 B ×2 | **0** | **0** | **232/256** | 254/256 | 65,528 B |
| 2 | OrbStack Linux | 115200 | same ladder ×2 (the macro mount passed in) | **0** | **0** | **248/256** | 254/256 | 65,528 B |
| 3 | macOS native | 921600 (control) | 4,093 B; 65,528 B | 14; 214 | **25; 602** | 253/256; **256/256** | 241–254/256 | 0 B¹ |
| 4 | macOS native | 115200 | device-left-on confirmation | **0** | **0** | 230/256 | 253/256 | — |

¹ Row 3's `--expect degenerate` runs the control, not a design claim; #33 measured the same
shape from the host side (12–15 retries per 4,093 B — this bench's 14 matches). The control
transfer **still completed** (hash verified, `boot_id` stable) at 0.3 KiB/s — the cost of a
saturated ring is retries and resends, never correctness, which is §2.7's offset-keyed resume
doing exactly its job.

Throughput at 115200: 2.4–3.6 KiB/s on 540–793 B, 6.1–6.9 KiB/s on the real plans,
7.5 KiB/s at 4,093 B, 9.2–9.3 KiB/s at 65,528 B — the same ~9 KiB/s ceiling #33 measured,
with the same shape (the per-request floor dominates small transfers).

**The OrbStack forwarder question the ticket's comment raised** — *a stall through the
forwarder may be the forwarder, not the ring* — is answered the strong way: there was no stall
to attribute. Row 2 is byte-for-byte as clean as row 1, the forwarder only slowing the line
(3.3–4.0 KiB/s at 65,528 B), and the device-side meter agrees on every row (drops=0,
backlog ≤ 248/256) — a forwarder stall would read as a host-side timeout with the device meter
silent and the backlog low, which no row shows.

## 3. Why the ring absorbs at 115200, and what the meter added

#33 established the rate picture from the host side: the 115200 line self-throttles below the
device's ≈14–20 KiB/s end-to-end drain, while the §2.7 window blast at 921600 does not. This
bench adds the device-side mechanism numbers:

- **At 115200, the tick is not on the critical path.** The producer's full-ring backoff is a
  0-tick yield (§1), so it never waits a tick; the parser sleeps at most 2 ticks (20 ms)
  between partial frames, during which 230 B can land — inside the 256 B zc ring alone, and
  the 256 B driver ring rides behind it. The measured worst case across every 115200 transfer
  is a **driver backlog of 248/256 B** — 8 B of margin at the worst instant of the worst
  transfer, and `drops=0` everywhere: the wire never outran the drain.
- **At 921600, the loss is at the driver ring, measured.** Row 3 shows what #33 could only
  infer: the zc ring near full (`hw` 241–254) *and* the driver backlog at its cap (256/256 on the
  65,528 B row) *and* the decoder discarding whole frames (542 silently-untrusted frames) —
  bytes lost without flow control exactly as §7.5 warns. The 5 s ACK stall → window resend →
  saturate-again cycle #33 observed is this meter's numbers in slow motion.

So the design conclusion is the one §12.2 row 2 pre-authorised only *if it failed*: **no ring
raise, no window shrink, no pacing** — the 256 B ring with the 4096 B window is measured
sufficient at the design baud, and the failure mode at 921600 is a drain-rate fact (#33's
G-1), not a window fact.

## 4. The bench-process incident, recorded for the next bench

One hour of this session chased a phantom "wedged device": after building a 921600 control
image by editing `sdkconfig` with `sed`, the "restored" 115200 build **silently kept
921600** — the CMake cache stayed authoritative over the edited file, `idf.py build` reported
success, and the device then "wedge-booted" at 115200 with a garbled banner… because it was
booting perfectly *at 921600*. The tell, once found: `esptool --before no_reset` syncs with
the "wedged" chip (it is in the ROM loader), and the boot log reads perfectly at 921600 —
including instrumentation breadcrumbs that "never ran" at 115200. **The rule for the next
bench: change `sdkconfig` only through a clean configure — `idf.py fullclean` between config
changes, and verify `build/config/sdkconfig.h` carries the value you think you built.** No
firmware defect was involved; `transport_uart.c`'s vendor-example call order
(`driver_install` → `param_config` → `set_pin`) was suspected and left exactly as it was.

## 5. Verification performed

| Step | Result |
| --- | --- |
| `python3 scripts/test_bench_rx_ring.py` | **19 tests, OK** (the line contract, per-transfer rows, both verdicts, the real-plan loader; the glued-segment regression) |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | 30 tests, OK |
| `python3 -m unittest discover -s container/tests -p 'test_*.py'` (the project venv) | 169 tests, OK |
| `ruff check container/ scripts/` | clean |
| Host C suites (`test_control_verbs/framing/mode/executor`, `cc -Werror`, per `control-framing.yml`) | 815 checks, OK — the portable control layer is untouched by the instrument |
| Builds: instrument @115200 (×2, one after a deliberate `fullclean`), 921600 control, 115200 restore — each verified via `build/config/sdkconfig.h` | 4 × `idf.py build` rc 0, flashed with hash verified |
| The four bench rows + the device-left-on confirmation | 31 bulk transfers total (14 + 14 + 2 + 1), every commit hash-verified, `boot_ids`=1 per run |
| Device left on | tree defaults @115200, `LOG_MAXIMUM_LEVEL=3`, `MCU_DEBUG` off — the deployment image, confirmed clean by row 4 |
## 6. Still open

- Nothing for #34 — the row is answered and §7.5 carries the number. If a later ticket raises
  the drain (a bigger ring, RX-side batching — #33's §6 candidate), this bench re-runs
  unchanged: `scripts/bench_rx_ring.py` reproduces rows 1–4, and the 921600 control row is
  one `--baud`/`--expect degenerate` away.
- The `spins` counter is now known to be yield-volume, not pressure (§1); if it earns a
  permanent place in the line, a later tidy might drop it — it is kept because it dates the
  `pdMS_TO_TICKS(1)`-is-zero discovery, which is exactly the kind of fact a future reader
  will not believe without the number.
