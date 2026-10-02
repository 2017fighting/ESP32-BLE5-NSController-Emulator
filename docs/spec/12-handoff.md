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
| 1 | Does the control link work at **921600** with `ESP_LOG` on the same wire? | flood a log and a bulk transfer at once; measure CRC retries | fall back to 115200 (G-1); the design does not change |
| 2 | Does the 256 B RX ring at a 100 Hz tick actually absorb the ACK window? | upload the largest plan and count stalls | raise the ring or shrink the window; `chunk_size` is advertised (`HELLO`), so the container adapts |
| 3 | Does the **plan executor** hold timing against the console? | run a macro with the console connected and compare input-to-input latency at `LOG_MAXIMUM_LEVEL=INFO` | this is where ADR-0003 is either vindicated or shown to need a report-period change |
| 4 | Does driving the **NFC state byte** make the console start polling? | flash the NFC path, place a tag, watch for `0x01/0x03` | this is the whole `AMIIBO` half; the earlier research says the console only probes `0x01/0x0C` today |
| 5 | Does the console read the 540 bytes? | the 9-round-trip read of §6.4 | |
| 6 | **Does freshness key on the UID on NS2?** | two rotations, two scans, one figure, watch the game's per-amiibo bookkeeping | the feasibility branch: fall back to physical tags or a PN532 (the research's documented fallbacks) |
| 7 | Is a pass resumed mid-press a problem? | start a macro, drop the console link, reconnect | the container's stop-on-drop policy (chapter 9) already avoids needing this answered |
| 8 | Does the device reboot on a release build at a remotely similar rate? | sleep/wake cycles at `INFO` | §9.2's "expect it" becomes cheaper, not wrong |

## 12.3 Known gaps

Recorded, not hidden. Each is a place where the design proceeds on a recommendation, an
inference, or a decision to leave something unowned.

| # | Gap | Where | Consequence if wrong |
| --- | --- | --- | --- |
| **G-1** | **921600 baud is a recommendation, not a measurement.** The CH9102 supports it and Linux's `cdc_acm` should drive it, but the board has never been run that fast with logs multiplexed | §2.1, §10.5 | fall back to 115200; nothing else changes |
| **G-2** | **Closed.** The exact frame byte layout was fixed in prose and no byte diagram; it is now a byte table in the owning sections (§2.2, §2.4–§2.10, §3.2, §3.3). Retired by [Fix the exact frame byte layout (G-2) and how the locked spec absorbs it](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/19) — the first gap closed rather than carried | §2, §3 | — |
| **G-3** | **The ~10 MB `storage` partition is unowned**, and `ota_1` is unused because OTA is out of scope | §7.8 | 13 MB of a 16 MB module sits idle; if nothing claims either, drop them |
| **G-4** | **Is a macro run harmless against an absent console?** Unobservable — the base firmware has no `MACRO` mode | §9.4 | the container allows it and warns; a wrong answer is a console-side surprise, not corruption |
| **G-5** | **Is the console content with a pass resumed mid-press after a link drop?** Unobservable for the same reason | §4.7, §9.4 | the container stops the run on a console drop, so the gap cannot bite |
| **G-6** | **Two amiibo facts are NS1 inferences, not NS2 measurements:** that freshness keys on the UID, and that an observable unplace is required. The console never enters `.nfp` on this firmware, so neither is confirmed *or* falsified | §6.5, §9.4 | the feasibility branch; the design emits the gap anyway, which is the safe superset |
| **G-7** | **The 8/19 reboot rate is a DEBUG-build figure.** The structural stack fragility generalises; the rate does not | §9.2 | the rate drops on a release build; nothing in the design depends on it |
| **G-8** | **Closed.** The fixture exists at `fixtures/plan/`, asserted by both the Python compiler test and the host-side C test, and CI runs both. Retired by [Macro compiler, plan cache and the golden fixture (G-8, G-13)](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/20) — the second gap closed rather than carried | §5.7 | — |
| **G-9** | **The NTAG215 region map exists in two coordinate systems** (tag-image vs internal/decrypted), and the sealing routine's library uses its own for the plaintext cache | §6.3 | a wrong index in the sealing module; nothing in the protocol depends on it, and the fixture-plus-library comparison catches it |
| **G-10** | **`scripts/package_firmware.py` fails with exit 0** and ships an `n8` name for a 16 MB module | §10.1 | use `package_firmware_v5.py`; the broken script should be deleted or fixed |
| **G-11** | **Two firmware defects the design tolerates rather than fixes:** the `gap.c` timer-stack overflow (~half of sleep/wake cycles) and the `gap.c` `ble_gap_update_params` `EINVAL` on every connection | §7.6, §9.2 | both belong to whoever owns `gap.c`; the second is harmless, the first is why `boot_id` recovery is normal operation |
| **G-12** | **The PN7160 identification-response difference** (`61 12 50 10` in firmware vs `61 12 50 0d` documented) is unresolved as to whether it matters | `ns2-amiibo-path.md` §2 Q6 | both register successfully with the console; it is cosmetic until proven otherwise |
| **G-13** | **Closed.** `main/include/protocol/plan.h` carries the `static_assert`s on the header and record sizes, the field offsets and the magic; the host-side C test compiles it. Retired by the same ticket as G-8 | §5.3, §5.7 | — |
| **G-14** | **The `0x01/0x0C` response asymmetry** (`0x91`→`0x01` rewritten for this subcommand, not for `0x02/0x04`) is unexplained, and it touches both the NFC probe and calibration | §7.3 step 8 | pulling it into the NFC work as an explicit decision rather than an inherited line |
| **G-15** | **Player slot / multi-controller behaviour is untested upstream** and is probably a non-goal — but it has not been *ruled* out | map fog | nothing in the protocol depends on it; a second board is a second container, and the console is the thing that would have to accept one |
| **G-16** | **Does the 5 ms link hold under macro load?** The link itself is measured (§9.1); the load question needs a `MACRO` mode to generate traffic | §9.4 | the plan executor's report cadence is the thing to check (validation 3), and ADR-0009 keeps a fix cheap |

## 12.4 What is most likely to be invalidated by the bench

Ranked, with the cost of being wrong:

1. **The NFC path's usefulness** (G-6, validation 4–6). If the console does not start polling
   when the NFC state byte moves, `AMIIBO` stops at the first step and the feasibility branch
   opens. This is the design's largest single risk and it is *unbuilt*, not merely unverified.
   Mitigation: it is stage 3, after a complete `MACRO` half, and the fallbacks are already
   documented.
2. **The baud rate** (G-1). Cheap to be wrong; a config change.
3. **The report period's adequacy** (validation 3). If a 10 ms report period cannot carry a
   macro convincingly, `CONFIG_FREERTOS_HZ` must rise — which is a firmware change that touches
   the plan executor's timing base and nothing in the protocol (holds are milliseconds,
   ADR-0009, precisely so this stays cheap).
4. **~~The frame layout~~** (G-2) — **retired.** Stage 0 fixed the bytes in §2 and §3 (§00's
   "Amendment mechanics"), so there is no longer a risk of two prose-built implementations.
   The lesson it demonstrated is the reason stage 0 existed: settle the layout on paper, or pay
   for it twice.
5. **The storage/OTA reservation** (G-3). Trivially reversible.
6. **The reboot rate** (G-7). Does not invalidate anything; only the container's error-wording
   budget.

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
