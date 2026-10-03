# The console's NFC polling and the 540-byte read — the failure point, precisely (issue #36)

**Scope:** §12.2 validations 4 and 5 — *does the console poll a served tag, and does it read all
540 bytes?* — plus **G-12** and one lifecycle hazard nobody had seen before. **Date:** 2026-10-04.
**Hardware on test host:** the same ESP32-S3-N16R8 on the CH9102 bridge as #33–#38 (serial
`5C930639851`, `/dev/cu.usbmodem5C930639851`), a **real NS2** (bonded; re-bonded once mid-bench
after the operator unpaired it — §3), the INFO deployment build (`MCU_DEBUG` off,
`LOG_MAXIMUM_LEVEL=3`, UART0 115200, `CONFIG_FREERTOS_HZ=100`, `CONFIG_HID_REPORT_INTERVAL=15`),
app `.bin` 550,704 B, SHA-256 `026f0f38…` (the `61 12 50 10` probe build) and `eb14523c…` (the
`61 12 50 0d` variant, §2). Figure served: `Amiibo Bin/Super Mario Amiibo/Mario.bin`, sealed
under a fresh identity per placement through the container's real path (`place_figure`, §8.5);
key `KEY_OK` throughout.

**The answer in one line.** On every surface this bench could reach, the console **did** send NFC
subcommands beyond the probe — its `0x05` get-status, answered `0x09` with the UID, arriving the
instant the report byte moved to `0x02` — and it **never sent `0x03`, `0x06` or a single `0x15`**.
The read never began; validation 5's 9-round-trip exchange was never entered. The failure point,
at the exact subcommand: **between the console's `0x05` and the `0x06` the canonical capture shows
next** (`switch2_controller_research/commands.md:64`). And the decisive surface — a game reading
through *the controller's* reader — was never reached, because the surface the operator could
stand on longest turns out to use the console's **own** reader (§3).

---

## 1. The instrument

The device is the only party that sees both halves of "did the bytes arrive uncorrupted", so it
is the one that stamps them. This ticket added the trace in the repo's meter pattern — portable
core, host suite, device glue:

| File | What it is |
| --- | --- |
| `main/include/protocol/control/nfc_trace.h` + `nfc_trace.c` | the **portable core**: a 16-slot RAM ring of distinct `(subcommand, offset)` events with per-key repeat counts, drop accounting, and `nfc_trace_format()` — the exact log text, so the host suite asserts the text, not a struct the device renders differently |
| `control_parser.c` | the **device glue**: feeds every console NFC exchange (CRC over exactly the bytes served, §2.2's call), and drains the ring as INFO at the *scan's* edge — the console's `0x04` or the container's unplace — one line per lock hold, on the tick after the edge |
| `ns2_codec.c` | the `0x0C` probe's one inline line (`console nfc: sub=0c rsp=…`): once per console connect, never inside a scan, so it costs no timing — and it is G-12's witness |
| `scripts/bench_amiibo_read.py` (+ `test_bench_amiibo_read.py`, 21 checks) | the host half: seal-and-place through the container's real path, capture every placed image, parse the trace, reconstruct each `0x15` response and CRC it against the image, report coverage under both §6.6 candidate offset spaces |
| `nfc_tag.h` `NFC_TAG_READ_WIRE_BASE` | the §6.6 offset-space knob: default `0` (plain, the spec's), a bench build can set `0x3C` to test the capture's `0x46`→`0x0A` hypothesis in one reflash; the host suite runs under both compiles in CI |

**Nothing is logged while the console reads.** A scan is ~9 round trips the ticket wants timed
(~90 ms, §6.4); one INFO line at 115200 is ~4 ms of UART, so per-command logging would add ~40 ms
*inside* the window being measured. Events accumulate in RAM and drain when the wire is quiet —
the same lesson as §7.5's meter, applied before the distortion could happen. (Runs 4 and 5 put
exactly one `0x05` line and one summary on the wire per scan window: ~30 ms of UART, all after
the console had gone silent.)

**Three instrument defects the runs themselves found, all fixed and pinned:**

1. The container's log demux keeps the ESP-IDF tag (`control: console nfc: …`), and the bench's
   anchored match lost every line — run 1's tally was empty while the raw log held the evidence.
   The parser now matches on the `console nfc:` marker wherever it sits, and a test pins the
   prefixed form.
2. The scan-phase gate fired on the console's *first* `0x05` — which run 1 proved arrives on its
   own the moment the byte moves — and unplaced 4 s into run 2, before the operator reached the
   screen. The completion witness is now a traced `0x15` or the trace's summary line (the drain
   the console's `0x04` owes), never an ask.
3. The unplace's backstop drain lands on the device's next 10 ms tick *after* the verb returns;
   run 2's teardown lost it. The analysis now waits out the drain before reading the tally.

## 2. The five runs

| # | Surface (operator) | Window | `0x0C` probes | `0x05` asks (all `status=09`, n=61) | `0x03`/`0x06`/`0x15` | Link drops (`531`) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | home menu, **no scan screen** (validation 4a) | 60 s | 1 per reconnect | **4** (one entry, `reps=3`) | **0** | **4** — the §3 loop |
| 2 | navigating toward settings | 4 s (gate bug) | 1 | 1 | 0 | 0 |
| 3 | System Settings → amiibo area | 90 s | **6** (one per reconnect) | **6** (one entry, `reps=5`) | **0** | **7** — placements rotated #1→#6 |
| 4 | **"添加所有者和昵称" register screen, waiting** | 75 s | 1 | 1 (at placement, `t=2383 ms`) | **0** | **0** |
| 5 | = run 4, probe answers `61 12 50 0d` | 75 s | 1 | 1 (`t=2398 ms`) | **0** | **0** |

Every `0x05` in every run was answered `status=09` — *tag detected* — with the 61-byte captured
shape and the placement's minted UID. The console was never lied to and never left waiting: it
asked, it was told a tag was there, it did not read. Evidence: `/tmp/amiibo-bench-run{1..5}.json`
from this session; the trace lines as logged are quoted in §5.

**G-12, narrowed by A/B — still open at the one gate that matters.** Runs 4 and 5 are the same
surface, same figure, same placement mechanics, one byte different: the `0x0C` probe's fourth
payload byte. With `61 12 50 10` and with `61 12 50 0d` the console's behaviour is **identical
to the subcommand**: probe accepted, one status ask at placement, then silence. So the probe
byte does not gate anything *reached* by this bench — the probe/status path. What no surface
here reaches is the gate the `AMIIBO` half hangs on, the console's `0x03` read start in a game
reading through this controller; until that runs, G-12 stays open with its question narrowed.
The firmware now answers the documented `0d` (`ns2_codec.c`): both values measured identical,
and emulating the documented controller is the side to be on.

## 3. Two findings beyond the ticket's question

**A placed tag outside a placement window destabilises the console link — and the rotation policy
amplifies it into a wake loop.** Run 1: 48 s after the placement, on an attended home menu, the
console dropped the link (`reason=531`, console-ended); the device re-advertised (the §10.3 wake
advert), the console reconnected, §9.3's rotate-on-reconnect policy re-placed a fresh identity,
the resubscribe re-asserted byte `0x02` (#25), the console asked `0x05`, and dropped again —
**four cycles in 60 s**, and the operator watched the console sleep and wake in a loop (each
reconnect's wake advert wakes a sleeping console; ~13 s later it slept again). Run 3 was worse:
**seven** cycles in 90 s. The operator eventually unpaired the controller to stop it — the bond
erase + re-pair is §10.3's documented path and restored a quiet link. For contrast, #38's bench
put 20 sleep/wake cycles on this same link with **no tag ever placed** and no such looping; and
runs 4/5, tag placed but console fully awake on a screen, show **zero** drops in 75 s each. The
correlation is with a *sleeping* console and a *placed* tag together; the causal step inside the
console is not observable from here. Recorded as a §9.2/§9.3 hazard: **a placement must not
outlive its scan window on the wire**, and rotate-on-reconnect's re-placement is what re-arms the
loop — the follow-up policy question (unplace-on-drop, or hold rotation until an ask) is a
scoping change, named here, not made here.

**The system-settings register screen reads with the console's own reader.** The operator read
the screen: it instructs touching the amiibo to *the console / Joy-Con* — not to the controller.
So runs 3–5 could never produce `0x03` from that surface regardless of firmware; the ambient
`0x05`s it elicited are the console's bookkeeping, not a scan attempt. The `0x03`-first flow the
canonical capture shows is a **game** reading through **the controller's** reader — and a game
binds its reader to player 1, which the console was mid-prompt about (“请在需要使用的手柄上按下
L+R键”) when the bench ended. Claiming player 1 from this device means macro-driving the game's
menus blind; that experiment is deliberately not run on the operator's save at 03:00.

## 4. What this closes, and what it hands on

- **Validation 4, split honest.** *Does moving the NFC state byte make the console do anything?*
  **Yes — measured:** byte `0x02` on the wire draws a `0x05` within one status poll (runs 4/5:
  `t=2383`/`t=2398 ms` device clock, i.e. at placement), answered `0x09` + UID, on the home menu
  and on settings screens alike. The byte path, the `0x05` shape and the UID plumbing work end to
  end. *Does the console start a read?* **No, never, on any surface reached** — not `0x03`, not
  `0x06`, not one `0x15`, across five runs, two probe values and three surfaces. The earlier
  research's "only ever `0x0C`" is superseded: the console also sends `0x05` on its own once a
  tag claims to exist.
- **Validation 5: not entered.** Zero read exchanges means the 9-round-trip question, the ~90 ms
  timing and §6.6's offset space are all **still open** — the `NFC_TAG_READ_WIRE_BASE` knob and
  the bench's CRC/coverage analysis are in the tree waiting for the first `0x15`. The §6.6
  "first 16-bit word transposed" note *is* settled on paper: the corpus's own page 2 carries
  `0F E0`, the capture's leading `0f e0` is the image verbatim, and the hypothesis is a constant
  `wire = image + 0x3C`.
- **G-12: narrowed, still open** — A/B-neutral on the probe/status path (identical behaviour to
  the subcommand with `10` and `0d`); the read-start gate is the untested half, and it rides
  with #39's game-surface run. The firmware answers the documented `0d` meanwhile.
- **Handed on — the one experiment that decides the `AMIIBO` half:** a **game** surface with
  this device as **player 1** (claim it with an `L+R` macro — the console was literally asking),
  open the game's amiibo menu, and watch for `0x03`. If `0x03` arrives, place *after* it and the
  `0x15`s follow; if it does not, the feasibility branch (§6.8) opens with this record as its
  evidence. That is [#39](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/39)'s
  to run, with this ticket's instrument already flashed.
- **Handed on — the placement-lifetime hazard** (§3): a policy decision on §9.3's
  rotate-on-reconnect vs unplace-on-drop, with the wake-loop evidence above.

## 5. The trace lines, as the device wrote them

```text
run 1 (home menu, 60 s, tag placed; drained by the unplace)
control: console nfc: sub=0c rsp=61125010          (×4 — one per reconnect)
control: console nfc: t=6867 sub=05 status=09 n=61 crc=eb95 reps=3
control: console nfc: scan cmds=4 [03=0 04=0 05=4 06=0 14=0 15=0] reps=3 drops=0

run 3 (settings → amiibo, 90 s; drained by the unplace)
control: console nfc: sub=0c rsp=61125010          (×6 — one per reconnect)
control: console nfc: t=5243 sub=05 status=09 n=61 crc=8de5 reps=5
control: console nfc: scan cmds=6 [03=0 04=0 05=6 06=0 14=0 15=0] reps=5 drops=0

run 4 (register screen, 75 s, probe build 61 12 50 10)
control: console nfc: sub=0c rsp=61125010
control: console nfc: t=2383 sub=05 status=09 n=61 crc=ee54 reps=0
control: console nfc: scan cmds=1 [03=0 04=0 05=1 06=0 14=0 15=0] reps=0 drops=0

run 5 (= run 4, probe variant 61 12 50 0d — byte-for-byte the same behaviour)
control: console nfc: sub=0c rsp=6112500d
control: console nfc: t=2398 sub=05 status=09 n=61 crc=da5a reps=0
control: console nfc: scan cmds=1 [03=0 04=0 05=1 06=0 14=0 15=0] reps=0 drops=0
```

The `t=` values are the device's control clock; the `0x05` at `t=2383`/`t=2398` is the placement
instant — the console's status ask follows the byte within one 2 Hz status poll.
