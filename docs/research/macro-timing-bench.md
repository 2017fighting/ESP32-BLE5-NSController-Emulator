# The macro's input-to-input latency at the console, and the no-console run (issue #35)

**Scope:** §12.2 validation 3 — *does the plan executor hold timing against the console?* — plus
**G-4**/*G-16*: what a macro run does with no console present, and whether the console's 5 ms link
holds under macro load. **Date:** 2026-10-03. **Hardware on test host:** the same ESP32-S3-N16R8 on
the CH9102 bridge as #33/#34 (serial `5C930639851`, `/dev/cu.usbmodem5C930639851`), a **real NS2**
(bonded, reconnecting from the bond, its own link measured in the same capture at **5.00 ms** —
`conn_itvl=4`), and the INFO deployment build (`MCU_DEBUG` off, `LOG_MAXIMUM_LEVEL=3`, UART0 at
115200, `CONFIG_FREERTOS_HZ=100`, `CONFIG_HID_REPORT_INTERVAL=15` → one 10 ms tick), app image
**`0x858f0` B** with this ticket's meter in it.

The three answers the ticket asked for:

| Question | Answer |
| --- | --- |
| The input-to-input latency, against a real console, at `INFO` | **the plan's hold, quantised to the report grid and never accumulating**: a 50 ms hold reads 49.98 ms p50, a 10 ms hold 10.01, a 120 ms hold 119.96, and a 25 ms hold alternates 20/30 (the two neighbouring grid values). The measured report cadence is **10.00 ms (100.0/s)**, 32% above the DEBUG build's log-budget ceiling, so the ceiling is excluded rather than absorbed |
| Can a 10 ms period carry a macro convincingly? | **Yes for holds much wider than the period** — 100% of the input changes delivered on a real 71-record library macro's first 34 records and on the 50 ms and 25 ms grids (repeat runs moved it by one input: 99.1–100%) — and **no for holds at or below it**: **90.1%** at a 10 ms hold, **5.1%** at 5 ms. ADR-0003 needed no `CONFIG_FREERTOS_HZ` change |
| With no console present (G-4) | **harmless and silent**: the run is allowed with a container warning, the executor walks every record, **zero** notifications go out, and the loop period is `loop_ms` *exactly* — min = mean = max on every plan whose loop is a multiple of the tick — so the handoff is vacuous, which is #24's code fix confirmed on the wire |

**G-5 is not answered here.** A console drop mid-run needs a reconnect inside a pass, which is
[Bench: link-drop behaviour — mid-press resume and the release-build reboot rate](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/38)'s.

---

## 1. The instrument, and why the executor alone cannot answer this

The executor knows when a record was **applied**; the console sees **notifications**, and only the
report task knows when one left the radio (`hid_controller.c` skips the swap entirely when nothing
is subscribed). §12.2 validation 3 lives exactly on that seam, so this ticket added one, in two
translation units the way every other portable module here is split:

| File | What it is |
| --- | --- |
| `main/include/protocol/control/control_meter.h` + `control_meter.c` | the **portable core**: a RAM ring of the newest distinct input states with their microsecond intervals, the exact extremes over the whole run, and the counters. No ESP-IDF; `test/host/test_control_meter.c` drives it directly |
| `control_meter_device.c` | the **device half** (`macro_meter_*`): the one instance, the INFO readout, and the no-ops a build without `CONFIG_PROTOCOL_LAYER_CONTROL` links instead |
| `hid_controller.[ch]` | the observation point: `controller_report_observer_t`, installed by the CONTROL layer at init. The base firmware installs none, so it carries no meter and no `#ifdef` about a layer it does not know |
| `scripts/bench_macro_timing.py` (+ `scripts/test_bench_macro_timing.py`) | the host side: upload, `START`, poll `STATUS`, `STOP`, and read the meter block out of the same serial stream |

Four things it records, per run:

| Field | Meaning |
| --- | --- |
| `applied` / `changes` | every write to the report, and how many of them **changed** the state — the executor side's ground truth |
| `inputs` | distinct states the console was actually notified, with the µs interval since the previous one |
| `handoff` | the wait §4.6 makes the walk pay at each loop boundary, measured where the executor feels it (the first `commit_idle` that says "not yet", to the one that says "taken") |
| `loop` | the interval between two record-0 applies — §5.4's loop period *as the report carries it* |

**Nothing is logged per report.** A DEBUG build's report rate is set by the UART log budget, not by
the report period (§7.5), so a per-report log line would be the measurement's floor rather than its
subject. The meter accumulates in RAM and the mode exit prints the block at `INFO`, bounded to 64
input lines (~0.4 s of the link at 115200) so it cannot outlast the container's 2 s request
timeout.

**The console's own link is measured in the same capture.** `main.c` raises only the `control` tag
above `WARN`, so the connection interval is logged there
(`control_notify_console_interval`, called from `gap.c`'s connect handler) rather than on
`ble_gap` — a comparison needs both halves in one INFO capture. It reads **`conn_itvl=4`, 5.00 ms**,
matching §9.1's 40/40 samples; the bench fails if the line is missing.

**Three instrument defects the ticket's own work found, all fixed and tested:**

- **512 ring entries printed 512 lines ≈ 3 s of UART inside the `STOP` reply's path**, which timed
  out the client. The ring is 64 entries; the counters and extremes — not the ring — carry the
  complete numbers.
- **Percentiles from a 1 ms histogram are a lie above 50 ms.** The first cut reported the overflow
  bucket's `max` as a "p50", which read 100.11 ms for a 5 ms macro whose real median was ~100 ms by
  aliasing and 120.11 ms for a 120 ms plan. The meter now prints the exact extremes over the whole
  run plus the newest 64 intervals, and the bench computes whatever distribution it wants from
  those — saying how big the sample is.
- **Byte-matching notified states back to plan records cannot work**, and the first cut of the bench
  analysis did it: a plan may revisit a state (the real 纠错宏 has **47 distinct states across 71
  records**; every synthetic grid has 2). A 5 ms macro aliases into a stream that still *looks* like
  an orderly walk through the plan while 95% of its inputs are gone. Delivery is therefore
  `inputs / changes`, counted on both sides of the seam.

Two more were caught by the change's own code review and fixed before this run: a stale
handoff-start timestamp that a run stopped mid-handoff could lend to the next run's first boundary,
and a firmware bring-up (ESP-IDF) leaked into the file the CI compiles as portable.

## 2. With a real console: the numbers

`container/.venv/bin/python scripts/bench_macro_timing.py --seconds 5`, the console connected and
subscribed **before the first case** (the script waits for `CONNECTED` and settles — a mid-case
subscribe is indistinguishable from the collapse this ticket is looking for, and the first attempt
at this measurement made exactly that mistake). `dropped=0 failed=0` in every row, and the report
cadence is 100.0/s (10.00 ms) in every row.

| Case | Plan | Measured intervals, newest ≤64 (min / p50 / p95 / max ms) | Every interval in the run (min–max, n) | Delivered input changes | Handoff mean (n) | Loop period at the report |
| --- | --- | --- | --- | --- | --- | --- |
| **library 纠错宏** | 71 rec, 26,205 ms loop, holds 0–5,462 ms (p50 54) | 9.87 / 20.00 / 910.03 / 1,660.01 | 9.87–1,660.01 (33) | **34/34 = 100%** | 9.99 ms (1) | — (no full loop in 5 s) |
| grid 50 ms | 12 rec, 550 ms, holds 50 | 9.86 / 49.98 / 50.14 / 50.22 | 9.86–50.22 (112) | 113/113 = **100%** | 9.96 ms (10) | 551.1 ms vs 550 (+1.1) |
| grid 25 ms | 20 rec, 475 ms, holds 25 | 9.77 / 20.17 / 30.24 / 30.34 | 9.77–30.44 (218) | 219/219 = **100%** | 9.93 ms (11) | 476.0 ms vs 475 (+1.0) |
| grid 10 ms | 20 rec, 190 ms, holds 10 | 9.86 / 10.01 / 19.95 / 20.04 | 9.85–20.07 (490) | 491/545 = **90.1%** | 9.96 ms (28) | 190.4 ms vs 190 (+0.4) |
| grid 5 ms | 20 rec, 95 ms, holds 5 | 9.84 / 99.97 / 100.12 / 100.19 | 9.84–100.19 (55) | 56/1,089 = **5.1%** | 9.97 ms (55) | 95.2 ms vs 95 (+0.2) |
| boundary | 4 rec, 360 ms, holds 120 | 9.90 / 119.96 / 120.07 / 120.09 | 9.90–120.09 (57) | 58/59 = **98.3%** | 9.96 ms (15) | 360.7 ms vs 360 (+0.7) |

Repeat runs of the same cases moved delivery by at most one input (grid 50 and 25 read 99.1% and
99.5% once each, 100% here); the deltas and the loop periods reproduced to the numbers quoted.

**What the intervals say.** A plan's holds are delivered as holds on the report grid: 50→49.98,
10→10.01, 120→119.96. A hold that is not a multiple of the period reads as the two neighbouring
grid values — 25 ms alternates 20.17 and 30.24 — which is why its *p50* sits below its nominal hold
while its mean and its loop period do not move. **The error never accumulates**: over 9–54 loops the
mean loop period is the plan's `loop_ms` + ≤ 1.1 ms, with ±1 tick of per-loop jitter (a 190 ms loop
measured min 189.9 / mean 190.4 / max 200.0). That is ADR-0009's absolute-deadline walk visible in
the data (`control_executor.c`: deadlines are `base_ms + Σhold`, never an accumulated tick count).

**What the delivery ratio says — the honest limit.** The console sees one state per report period, so
a state held for less than a period can be overwritten in the back buffer before the reporter samples
it, and is then never on the wire:

| Hold vs the 10 ms period | Delivered |
| --- | --- |
| 5× (50 ms) | 100% (99.1% on a repeat run) |
| 2.5× (25 ms) | 100% (99.5% on a repeat run) |
| 1× (10 ms) | 90.1% (three runs, 90.1–90.2%) |
| 0.5× (5 ms) | 5.1% |

So the fork the ticket names — raise `CONFIG_FREERTOS_HZ` — is **not needed**: every macro in the
pinned library has a median hold of 21–120 ms, and the one real macro measured here delivered 100%
of its input changes over the 34 records a five-second window covers. What *would* need it is a
macro with sub-period holds, and that is a macro-authoring limit rather than an executor defect: the
executor still walks a 5 ms plan exactly (95.2 ms loop, `loop_ms` + 0.2), it is the *report* that
cannot carry it. §7.5 carries the condition.

**The handoff, measured.** §4.6/§5.4 make the boundary commit the neutral and wait for the reporter
to take it; #24 could only bound that wait (≤ 50 ms, and it had to fix the absent-console case where
the bound was reachable). With a console subscribed it is **one report period, always**: 9.93–9.99
ms mean across 1–55 waits (the arm's plus one per loop boundary), min 9.60, max 10.03. It shows up as
the +0.2…+1.1 ms on the mean loop period and as ±1 tick of per-loop jitter — not as drift, because
the executor's deadline for the next boundary is the plan's, not "now".

## 3. With no console: G-4 observed

The same script with `--no-console`, the NS2 **powered off** (standby is not enough — a sleeping NS2
reconnects to a bonded controller by itself, §9.1, which is how the first attempt at this run was
lost: the console arrived mid-case). `console_link=ADVERTISING` in every case, `inputs=0 notified=0
dropped=0 failed=0`.

| Case | Applied / changes | Notified | Loop period at the report |
| --- | --- | --- | --- |
| library 纠错宏 | 35 / 34 | 0 | — (no full loop in 5 s) |
| grid 50 ms | 124 / 115 | 0 | **550.0 / 550.0 / 550.0 ms** (min/mean/max) vs 550 |
| grid 25 ms | 228 / 217 | 0 | 470.0 / 475.0 / 480.0 vs 475 |
| grid 10 ms | 572 / 545 | 0 | **190.0 / 190.0 / 190.0** vs 190 |
| grid 5 ms | 1,145 / 1,091 | 0 | 90.0 / 95.0 / 100.0 vs 95 |
| boundary | 73 / 59 | 0 | **360.0 / 360.0 / 360.0** vs 360 |

**The decisive fact is the loop period, and its zero jitter.** With a console the same plans pay ~10
ms per boundary (§2); with nobody subscribed they pay *nothing* — a 190 ms loop measures exactly
190.0 on every one of 26 loops. That is `executor_commit_idle`'s subscription check (the fix #24's
bench found, where a 1,000 ms loop had measured +51.9 ms) doing its job on the wire, and it is why
"a macro run is harmless against an absent console" is now an observation rather than a code
reading: the executor does identical work with the same `changes` count, and the inputs simply go
nowhere.

**Nothing else happens either.** No fault, no `dropped`, no `failed`, and `mode` returns `IDLE` with
`last_stop_reason=CONTAINER_STOP` (§3.4) — §9.4's "a console-side surprise, not corruption" with the
surprise now identified: there is nothing to surprise a console with, because nothing is transmitted.

**And the container's half, on the real container.** `python -m container.ns2container` on the host,
the board passed through as `/dev/cu.usbmodem5C930639851`, `POST /api/start
{"macroId":"纠错宏.json"}` with the console off:

```text
[warn/container] console: not connected — 纠错宏.json runs anyway and its inputs go nowhere until the console connects (§9.3)
[info/container] plan: uploading 纠错宏.json → 793 B
[info/device]    control: staging closed: total=793B ring hw=240/256 spins=230 backlog=118/256 drops=0
[info/container] plan: 纠错宏.json committed, identity 1f0a90d3ccabcb198cd236afc4c75726
[info/container] mode: MACRO started on 纠错宏.json
…
[info/device]    control: macro meter: applied=48 changes=48 inputs=0 notified=0 dropped=0 failed=0 ring=0/64 elapsed=8140003us
[info/container] mode: stopped from the app
```

The run was **allowed and warned**, `currentFrame` advanced (to 45 of 71) with the mode reading
`MACRO` throughout, and `STOP` returned `IDLE`. The device's own meter line reaches the container's
log through the `device` seam, so G-4 is observable end to end without a console at all. §9.3 names
`PLACE_AMIIBO` in the same sentence as `START`, and the container warns the same way when a figure is
placed with no console — asserted by `container/tests/test_ns2container_state.py` rather than on the
bench, because the retail key is not mounted on this host.

## 4. What this closes, and what it hands on

- **§12.2 validation 3 — answered.** The report period carries the plan's holds; the console's own
  5 ms link is not the constraint (it has 2× the period's headroom and the device never misses it:
  `notified` is exactly 100.0/s).
- **G-16 — closed.** "Does the 5 ms link hold under macro load?" The 5 ms link is not the thing under
  load: the *report period* is, it is measured at 10.00 ms, and it holds. ADR-0003 needs no
  report-period change for any macro whose holds are much wider than the period; the sub-period
  condition is recorded in §7.5 as what would reopen it.
- **G-4 — closed.** Harmless, silent, allowed-with-a-warning, and the handoff is vacuous.
- **Handed to #38:** **G-5**, the mid-press resume after a console drop, still needs a reconnect
  inside a pass — the one gap in this area that a *drop* (rather than an absence) creates. The
  release-build reboot rate (§12.2 row 8 / G-7) is that ticket's too.

## 5. Verification performed

| Step | Result |
| --- | --- |
| Host suite `test_control_meter.c`, plain | `control meter ok: 122 checks` |
| Host suite `test_control_meter.c`, ASan + UBSan | same, clean |
| Host suite `test_control_executor.c` (the new restart hook) | `control executor ok: 156 checks` |
| The other three CONTROL suites (regression) | framing 193, verbs 211, mode 264 — all pass |
| `scripts/test_bench_macro_timing.py` | 21 tests, OK |
| `python3 -m unittest discover -s scripts` | 77 tests, OK (1 skip: the reference library is not on this host) |
| Container suite (`.venv` python, aiohttp present) | 172 tests, OK |
| `ruff check container/ scripts/` | clean |
| `idf.py build` (esp32s3, `MCU_DEBUG` off, INFO) | rc 0, 0 warnings; app image **`0x858f0`** B, 83% of the 3 MB slot free |
| `idf.py -p … flash` | rc 0, hash verified |
| The with-console bench run | §2 — 6 cases, every one with a meter block, `dropped=0 failed=0`, the console's 5.00 ms link in the capture |
| The no-console bench run | §3 — 6 cases, `notified=0` and `console_link=ADVERTISING` throughout, on this same image |
| The real container, no console | §3 — the warning line, the run, the device meter line, `STOP` → `IDLE`, on this same image |

The bench captures are bench-local and not committed (the raw serial dumps are large); the commands
are below and the tables above are the whole of what they said. Re-run with:

```
source ~/esp/idf-env-5.5.5.sh
idf.py -p /dev/cu.usbmodem5C930639851 flash
container/.venv/bin/python scripts/bench_macro_timing.py --seconds 5            # with a console
container/.venv/bin/python scripts/bench_macro_timing.py --no-console --seconds 5
```
