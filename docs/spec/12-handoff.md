# 12 · Handoff

How to start implementing from here: the order, what must be validated on hardware first, and
the decisions most likely to be invalidated by the bench.

## 12.1 The order

The dependency order is not the chapter order. Three things gate everything else, and two of
them can be settled before any code is written.

**Stage 0 — settle the two protocol unknowns on paper (no hardware, no firmware).**

1. ~~**Fix the exact frame byte layout** for `HELLO`, the bulk announce/chunk/commit frames,
   and `STATUS` (§2.6, §2.7, §3.2). The chapters give every field, its width and its
   endianness, but not a byte diagram; two implementations built from prose will differ.~~
   **Done.** The layout is bytes in the owning sections — §2.2 (framing and the CRC), §2.4–§2.6
   (verbs, `ERROR`, `HELLO`), §2.7 (bulk), §2.8–§2.10, §3.2 (`STATUS`) and §3.3 (`EVENT`) —
   and the amendment mechanics are §00's "Amendment mechanics".
2. ~~**Build the golden fixture** (§5.7, G-8): one real macro → exact plan hex → SHA-256, checked
   in and asserted by both a Python test and a host-side C test. This is the single highest-value
   artifact in the whole effort, because it is what catches container/firmware drift without a
   board.~~
   **Done.** The fixture is `fixtures/plan/` (`correction.json` → `correction.plan.hex` →
   `correction.sha256`, the four rows of §5.7 reproducing exactly); the compiler and plan cache
   are `container/ns2plan/`; the firmware layout, with the G-13 `static_assert`s, is
   `main/include/protocol/plan.h`; and `.github/workflows/plan-fixture.yml` runs both sides.

**Stage 1 — bring the control plane up, with no modes.**

1. A UART0 transport instance (§7.3 step 1) and the shared TX lock with rate-suppressed logging
   (step 2). **Do this before the verbs**: the logging behaviour is what makes the wire usable,
   and retrofitting the lock later touches every send path.
2. The CONTROL protocol layer and `HELLO`/`STATUS` only. Two verbs are enough to prove framing,
   resynchronisation under a log flood, and CRC recovery.
3. The container's `serial` and `device` seams (§8.2) against those two verbs. The `web` seam
   can be a stub.

**Stage 2 — `MACRO`, end to end, without the console.**

1. The plan executor and the neutral template (§7.3 step 5), the state model and the transition
   table (chapter 4), the panic stop.
2. The compiler (chapter 5) against the golden fixture.

**Stage 3 — `AMIIBO`.**

1. The NFC state machine and tag server (§7.3 step 6) — these are firmware changes with no
   protocol surface, and they are the largest single unknown left.
2. The sealing module and the container's amiibo index (§6.4, §8.5), which can be built and
   unit-tested **before** the firmware serves anything: a sealed tag is verifiable offline
   against a library tag's HMACs.
3. Key policy and the three locks (§6.7, §8.6) — container-only work.

**Stage 4 — the console.** Everything in chapter 9 that starts with "the console" is only
observable here, and the container's policy is already written so that none of it needs to be
observed to ship safely.

## 12.2 What must be validated on hardware, in this order

| # | Question | How | If it fails |
| --- | --- | --- | --- |
| 1 | Does the control link work at **921600** with `ESP_LOG` on the same wire? | flood a log and a bulk transfer at once; measure CRC retries | **Answered** — no, and the fallback is taken: the logs cost zero corrupted frames at either baud, but the §2.7 window burst overruns the device's ≈14–20 KiB/s drain (12–15 retries/4 KB at 921600); **115200 survives** every size to the 65528 B maximum on both host stacks, so the design's baud is 115200 and nothing else changed (§2.1, `baud-bench.md`) |
| 2 | Does the 256 B RX ring at a 100 Hz tick actually absorb the ACK window? | upload the largest plan and count stalls | **Answered** — yes, at the design baud, with the ring and the window both unchanged: **0 stalls and 0 untrusted frames** on every transfer to the **65,528 B capacity maximum**, on both host stacks (macOS native and the OrbStack-forwarded container path), with the real library's largest plan (杏仁, 3,356 B) among the rows; the measured margin is a driver-ring backlog high-water ≤ 248/256 B (the zc ring running near full is the blast's steady state, not a hazard — a chunk frame out-sizes it, and `pdMS_TO_TICKS(1)` is 0 ticks at 100 Hz). At 921600 the instrument reads the cliff directly: hundreds of silently-untrusted frames per large transfer and the backlog at 256/256 (§7.5, `rx-ring-bench.md`). Neither the raise-the-ring nor the shrink-the-window fallback was needed |
| 3 | Does the **plan executor** hold timing against the console? | run a macro with the console connected and compare input-to-input latency at `LOG_MAXIMUM_LEVEL=INFO` | **Answered** — yes, and ADR-0003 needs no report-period change for any macro whose holds are much wider than the period. At `INFO` the report cadence measures **10.00 ms (100.0/s)** — 32% above the DEBUG build's log-budget ceiling, so the log is excluded rather than absorbed — and the console (its own link measured in the same capture at **5.00 ms**) sees the plan's holds on that grid: 50→49.98 ms p50, 10→10.01, 120→119.96, a 25 ms hold alternating 20.2/30.2, with ±1 tick of jitter and **no accumulation** (mean loop period = `loop_ms` + ≤ 1.1 ms over 9–54 loops). The boundary neutral's handoff is **one report period, measured** (9.93–9.99 ms mean over 1–55 waits). Delivery of the executor's input changes is 100% on a real 71-record library macro's first 34 records and on the 50/25 ms grids (99.1–100% across runs), **90.1% at a 10 ms hold and 5.1% at 5 ms** — so the fork (`CONFIG_FREERTOS_HZ`) is open only for sub-period holds (§7.5, `macro-timing-bench.md`) |
| 4 | Does driving the **NFC state byte** make the console start polling? | flash the NFC path, place a tag, watch for `0x01/0x03` | this is the whole `AMIIBO` half; the earlier research says the console only probes `0x01/0x0C` today |
| 5 | Does the console read the 540 bytes? | the 9-round-trip read of §6.4 | |
| 6 | **Does freshness key on the UID on NS2?** | two rotations, two scans, one figure, watch the game's per-amiibo bookkeeping | the feasibility branch: fall back to physical tags or a PN532 (the research's documented fallbacks) |
| 7 | Is a pass resumed mid-press a problem? | start a macro, drop the console link, reconnect | **Answered** — no, and the stop-on-drop policy is what makes it so. A macro holding A was dropped 42 s in, on the press record: **exactly one `STOP`**, the device left `IDLE` with a plain `CONTAINER_STOP`, the stop's neutral the run's last write, and no resume or second `START` on reconnect. The console's own re-subscribe ran the executor's RESUME path earlier in the same run, so the mechanism was present and reachable (`link-drop-bench.md` §3) |
| 8 | Does the device still reboot across sleep/wake cycles after the §7.6 fix? | sleep/wake cycles on the DEBUG build the 8/19 figure came from, before and after counts; [Fix the gap.c timer-stack overflow](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/27) owns it | **Answered** — §7.6 holds the before/after counts and the margins (`advertise-restart-bench.md` §1, §3), and the release-build half is measured too: **0 reboots and 0 overflows in 21 disconnects** on the shipped `INFO` release build (`link-drop-bench.md` §4). §9.2's "expect a reboot" policy holds either way |

## 12.3 Known gaps

Recorded, not hidden. Each is a place where the design proceeds on a recommendation, an
inference, or a decision to leave something unowned.

| # | Gap | Where | Consequence if wrong |
| --- | --- | --- | --- |
| **G-1** | **Closed.** 921600 measured and **rejected for the bulk path**: §2.7's back-to-back window overruns the 256 B RX ring at 92 KiB/s of line — 12–15 window retries per 4093 B, ~30 KB resent per 4 KB plan — while the failure is the rate, not the logs (the INFO build fails identically; multiplexed `ESP_LOG` cost zero corrupted frames at either baud). **115200 survives**: zero retries on every transfer to the 65528 B capacity maximum, on macOS and through the OrbStack-forwarded container path, host-side `bad-CRC` 0 everywhere, the link never reset. Retired by [Bench: 921600 with ESP_LOG on the same wire (G-1)](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/33) | §2.1, §10.5 | — |
| **G-2** | **Closed.** The exact frame byte layout was fixed in prose and no byte diagram; it is now a byte table in the owning sections (§2.2, §2.4–§2.10, §3.2, §3.3). Retired by [Fix the exact frame byte layout (G-2) and how the locked spec absorbs it](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/19) — the first gap closed rather than carried | §2, §3 | — |
| **G-3** | **The ~10 MB `storage` partition is unowned**, and `ota_1` is unused because OTA is out of scope | §7.8 | 13 MB of a 16 MB module sits idle; if nothing claims either, drop them |
| **G-4** | **Closed.** Is a macro run harmless against an absent console? It is: the container allows the run and warns before the upload, the executor walks every record with the same `changes` count, **zero** notifications go out, the handoff is vacuous — the loop period is `loop_ms` *exactly*, min = mean = max — and the mode returns `IDLE` with `CONTAINER_STOP`. No fault, no drop, nothing a console could be surprised by. Retired by [Bench: plan-executor timing, with and without a console (G-16, G-4)](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/35) | §9.3, §9.4, §7.5 | — |
| **G-5** | **Closed.** The condition was presented on the bench and the policy held: a run holding A dropped on the press record, the container sent exactly one `STOP`, the device was left `IDLE` with the neutral as its last write, and nothing resumed on reconnect. The *unwatched* case — a drop the container's own link never saw — stays §4.7's device rule (the pass continues at its current frame) and was not exercised here. Retired by [Bench: link-drop behaviour — mid-press resume and the release-build reboot rate](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/38) | §4.7, §9.4 | — |
| **G-6** | **Two amiibo facts are NS1 inferences, not NS2 measurements:** that freshness keys on the UID, and that an observable unplace is required. The console never enters `.nfp` on this firmware, so neither is confirmed *or* falsified | §6.5, §9.4 | the feasibility branch; the design emits the gap anyway, which is the safe superset |
| **G-7** | **Closed.** The advertise-restart work moved off the timer task (§7.6) and was benched: the fix holds across sleep/wake cycles, and the pre-fix rate turned out to describe the base firmware's console path rather than `HEAD`, where the same callback is latent. Retired by [Fix the gap.c timer-stack overflow](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/27) | §7.6, §9.2, `advertise-restart-bench.md` §1, §3 | — |
| **G-8** | **Closed.** The fixture exists at `fixtures/plan/`, asserted by both the Python compiler test and the host-side C test, and CI runs both. Retired by [Macro compiler, plan cache and the golden fixture (G-8, G-13)](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/20) — the second gap closed rather than carried | §5.7 | — |
| **G-9** | **The NTAG215 region map exists in two coordinate systems** (tag-image vs internal/decrypted), and the sealing routine's library uses its own for the plaintext cache | §6.3 | a wrong index in the sealing module; nothing in the protocol depends on it, and the fixture-plus-library comparison catches it |
| **G-10** | **`scripts/package_firmware.py` fails with exit 0** and ships an `n8` name for a 16 MB module | §10.1 | use `package_firmware_v5.py`; the broken script should be deleted or fixed |
| **G-11** | **The `gap.c` `ble_gap_update_params` `EINVAL` is tolerated rather than fixed.** The other defect this gap used to carry — the advertise-restart timer-stack overflow — is **fixed** by [Fix the gap.c timer-stack overflow](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/27) (§7.6) | §7.6, §9.2 | the remaining `EINVAL` is harmless; the fix does not retire `boot_id` recovery, which must tolerate a reboot from any cause |
| **G-12** | **The PN7160 identification-response difference** (`61 12 50 10` in firmware vs `61 12 50 0d` documented) is unresolved as to whether it matters | `ns2-amiibo-path.md` §2 Q6 | both register successfully with the console; it is cosmetic until proven otherwise |
| **G-13** | **Closed.** `main/include/protocol/plan.h` carries the `static_assert`s on the header and record sizes, the field offsets and the magic; the host-side C test compiles it. Retired by the same ticket as G-8 | §5.3, §5.7 | — |
| **G-14** | **The `0x01/0x0C` response asymmetry** (`0x91`→`0x01` rewritten for this subcommand, not for `0x02/0x04`) is unexplained, and it touches both the NFC probe and calibration | §7.3 step 8 | pulling it into the NFC work as an explicit decision rather than an inherited line |
| **G-15** | **Player slot / multi-controller behaviour is untested upstream** and is probably a non-goal — but it has not been *ruled* out | map fog | nothing in the protocol depends on it; a second board is a second container, and the console is the thing that would have to accept one |
| **G-16** | **Closed.** Does the 5 ms link hold under macro load? The link is not the constraint — the report period is, and it is measured at **10.00 ms (100.0/s)** with a real console subscribed, so the console sees every hold the period can carry (100% of a real macro's input changes over the records a five-second window covers) and the loop period is the plan's own. What does *not* survive is a hold **at or below** the period: 90.1% delivered at a 10 ms hold and 5.1% at 5 ms, against 99–100% at ≥ 25 ms — the one condition that would justify raising `CONFIG_FREERTOS_HZ` (§7.5). Retired by the same ticket | §9.4, §7.5 | — |

## 12.4 What is most likely to be invalidated by the bench

Ranked, with the cost of being wrong:

1. **The NFC path's usefulness** (G-6, validation 4–6). If the console does not start polling
   when the NFC state byte moves, `AMIIBO` stops at the first step and the feasibility branch
   opens. This is the design's largest single risk and it is *unbuilt*, not merely unverified.
   Mitigation: it is stage 3, after a complete `MACRO` half, and the fallbacks are already
   documented.
2. **The baud rate** (G-1). Cheap to be wrong; a config change.
3. **~~The report period's adequacy~~** (validation 3) — **retired.** Measured against a real
   console at `INFO`: the period is 10.00 ms, the console sees the plan's holds on that grid with
   no accumulation, and delivery is 100% on the records a real library macro's 5 s window covers and
   99–100% for holds ≥ 25 ms. A 10 ms period cannot carry a macro whose holds are at or below it
   (90.1% delivered at 10 ms, 5.1% at 5 ms), and that is the only condition under which
   `CONFIG_FREERTOS_HZ` would have to rise — a macro-authoring limit, not an executor defect (§7.5,
   `macro-timing-bench.md` §2).
4. **~~The frame layout~~** (G-2) — **retired.** Stage 0 fixed the bytes in §2 and §3 (§00's
   "Amendment mechanics"), so there is no longer a risk of two prose-built implementations.
   The lesson it demonstrated is the reason stage 0 existed: settle the layout on paper, or pay
   for it twice.
5. **The storage/OTA reservation** (G-3). Trivially reversible.
6. **~~The reboot rate~~** (G-7) — **retired.** The fix is benched and the pre-fix callback did
   not reproduce at `HEAD` at all (§7.6, `advertise-restart-bench.md` §1). Nothing in the design
   depended on the rate either way, and the release build makes it explicit: **0 reboots in 21
   disconnects** (`link-drop-bench.md` §4).

## 12.5 What is deliberately not decided here

Not gaps — settled refusals, recorded so they are not re-opened by accident:

- Whether a macro keeps running when a link drops: **yes**, both links (ADR-0008).
- Whether pairing can be driven from the container: **no** (ADR-0013).
- Whether the key can be uploaded through the UI: **no** (ADR-0012).
- Whether the device can hold a library or a plan across power: **no** (ADR-0004).
- Whether a verb may implicitly change the mode: **no** (ADR-0007).
- Whether `TELEMETRY`, `RESET`, OTA or authentication join the verb surface: **no** (§2.4).
- Whether amiibo mode may serve a precomputed variant set: **no** (§6.4).

Reopening any of these is a scoping act, not an implementation detail, and each has an ADR or
a chapter section that says why.
