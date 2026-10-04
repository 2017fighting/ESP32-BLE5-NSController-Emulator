# The register screen: the read pipeline opens, the pull gate falls, and the console's crash mode (issue #37's surface)

> **Superseded in part (spec §6.6, G-19).** Every build in this record served the superseded
> `0x15` shapes — the offset-echo head, the 64-byte chunk, the image at plain offset 0, and
> silence for out-of-range asks. The runs, the probe pattern and the crash ledger stand as the
> record of what happened; the *factors* they isolate are re-read in §12.3/G-18, and the shapes
> they were built on are now the defect G-19 names.

**Scope:** the operator-requested session on 系统设置 → amiibo → 添加所有者和昵称
(register owner and nickname), testing the operator's theory that the screen's read binds to
whichever controller pressed A to enter it. Run 2026-10-04 10:53–11:45, operator-coached, the
resident driver (`scripts/bench_press_buttons.py --fifo`) with this session's `--tee-all` flag
(every device log line, not just the NFC ones). Figure: Stitches (`[AC] AF1`), fresh identity
per placement. The bench builds each carry one variable over the last; the take numbers below
are the driver sessions.

**The one-line answer.** The theory held, the surface delivered, and the read pipeline
**opened for the first time in the project's history**: with the device as player 1 pressing
its own A into the register screen, the prompt binds to **our reader** (the Pro Controller
icon, operator-observed), the console re-arms the full read (`0x03 → 0x05(09) → 0x06`) every
~3.1 s continuously, and — once the device **pushes the tag data as `0x15`-shaped
notifications** — the console **pulls `0x15` for the first time ever**. A follow-up knob
answering the console's `0x05` status ask with the read-done state `04` was observed to
**trigger the first pull within 30 ms**. The remaining gate is the *continuation*: the console
pulls one chunk and stops, then rides out its 3 s deadline and retries — and three of the
frame combinations **crashed the console's amiibo module outright** (`2011-0301`, forced
reboot), which both proves how deep our frames reach and sets the safety constraint for the
next session.

---

## 1. The session's ledger (what each build changed, what the console did)

All builds: `NFC_TAG_BYTE_FOLLOWS_POLLING=1` (the byte follows the console's polling level —
`nfc byte:` logged per change), `NFC_TAG_READ_DONE_BYTE=0x04`.

| Take | The one variable | The console's answer |
| --- | --- | --- |
| 1–2 | (pairing repair — §5) then the claim and the A | the register screen binds to **our reader**; `0x06 len=19` arrives **on the register screen** — #36's runs there never passed `0x05`; the console **re-arms the full read every cycle** (unlike BotW's error-then-no-rearm) |
| 3 | `--tee-all` (script flag, no reflash) | the 3 s window carries **zero** non-NFC traffic — the console waits on device pushes alone (`d0 07` reads as a 2000 ms deadline + ~1 s grace) |
| 4 | `NFC_TAG_NOTIFY_READ_DONE=1` — one unsolicited sub-0x05 status push, byte 0 = `04`, right after the `0x06` ACK | ignored — the console asks nothing until its deadline (unsolicited response-channel frames are dropped; its parser correlates by subcommand) |
| 5–6 | `NFC_TAG_PUSH_READ_DATA=1` — the whole tag pushed as `0x15`-shaped notifications (a `04` status first, then 64-byte chunks at plain image offsets) | **the `0x15` pulls begin** — 2–4 asks per cycle at scattered 64-aligned offsets (`0x0000/0x0040/0x0080/0x0100/0x0140/0x0180/0x01c0/0x0200` and beyond the image: `0x0240/0x0280/0x02c0`), ~30 ms apart, all inside ~120 ms of the `0x06`; then silence to the deadline |
| 7 | + `NFC_TAG_READ_PAD_TO=0x300` — zero-filled chunks beyond the image | the asks walk further (to `0x0300`, the pad's edge): the console **probes until an empty answer** — it is mapping the buffer size; the empty answer at the true end is not itself the abort |
| 8 | + `NFC_TAG_READ_DONE_MS=150` — the byte's `04` is a pulse, returning to `0x02` | no change |
| 9 | the pushes **paced** — one chunk per 10 ms tick (a real reader streams the pages off the RF) | no change — the burst-drop theory is falsified; pacing neither helps nor hurts |
| 10 | + `NFC_TAG_STATUS_DONE_WHEN_READ=1` — after the push completes, `0x05` **answers** carry byte 0 = `04` | **causality**: the console's `0x05` ask receives `04` and **pulls `0x15 off=0000` within 30 ms** — then stops after that one pull |
| 11 | the push order flipped to the NS1 order (data chunks, **trailer last**) | **console crash** `2011-0301` (forced reboot) — twice |
| 12 | `NFC_TAG_BUFFER_P1_PREFIX=1` — the served space becomes `[60 B P1 framing][540 B image]`, image at wire `0x3C` | the `0x0000` pull receives the framing: **no crash on that chunk** (against the image-at-0 crashes), but still one-pull-then-stop; a crash occurred later in the session all the same |
| 13 | byte holds `04` (`READ_DONE_MS=0`) — **flashed but never run**: the session stopped at the third crash | untested |

## 2. The five structural facts this session established

1. **The register screen binds to the controller that drove it into the flow.** The operator's
   Joy-Con navigated to the entry; the device's A pressed it; the prompt's icon is the Pro
   Controller and every `0x06` came to us. #36's conclusion ("the console's own reader") was
   an artefact of the operator navigating with their own pad — the binding follows the last
   input's controller, exactly as #39 saw with the BotW rune.
2. **`0x06` is the read command itself, and the console re-arms it every ~3.1 s on this
   surface.** The 19-byte payload is the NS1 read command verbatim
   (`ns1-nfc-read-decode.md` §1); the cycle is `0x03 (cfg 00e8032c01) → 0x05 (09+UID) → 0x06
   → [3.0 s wait] → 0x04 → repeat` — a self-resetting experiment bench, no re-claim needed.
3. **Unsolicited pushes open the pull phase.** Before this session no `0x15` had *ever* been
   sent by a console against this device. The whole-tag push — not the byte, not the status
   push, not pacing — is what unlocked it.
4. **The `04` status answer gates the first pull.** Ask → answer `04` → pull, 30 ms apart,
   twice observed (takes 10 and 12). The `04`-as-push variant was dropped by the console;
   the same bytes as an *answer* act. This is the NS1 state machine's `09 → 04` transition
   surviving into the NS2's status response — the answer's byte 0 *is* the state byte.
5. **The console's amiibo module crashes on some of our combinations (`2011-0301`).** A
   forced-reboot fatal, three times: twice with image-at-0 + the `04` answer + trailer-last,
   once with the P1-prefix build (late in the session). The exact trigger is unresolved —
   which makes it the binding safety constraint for the next bench, not a curiosity.

## 3. The open gate, precisely

After the first successful pull (`0x15 off=0000`, answered with either image bytes or P1
framing), the console asks **nothing more** and rides out its deadline. Candidates for the
continuation signal, in the order the evidence points:

1. **A per-chunk ready signal.** The first pull was gated on a state *transition* the console
   asked for (`0x05` → `04`); the next chunk may need another — a fresh `04` answer, a pushed
   chunk per pull, or the byte's edge per chunk. The `04`-hold build (take 13) tests the
   cheapest of these and was never run.
2. **The framing's content.** The 60-byte P1 prefix (the NS1 capture's framing minus its UID
   field — 15 header + 4 zeros + 32 magic + 9 echoed ranges = 60, putting the image at wire
   `0x3C`, exactly the shift the canonical capture's `0x46`→image-`0x0A` implies) did not
   crash the `0x0000` pull — but its magic blob and range echo are hardcoded from the NS1
   capture, and a content check failing there would look exactly like one-pull-then-stop.
3. **The trailing region.** The console's asks reach `0x0240–0x0340` — past any
   framing+image buffer (`0x3C + 540 = 0x258`). Whatever the real reader serves there
   (P3-trailer? NCI residue? empties-as-end-marker?) is unmodelled.

## 4. What this session changed in the tree

- `scripts/bench_press_buttons.py` — `--tee-all`: tee every device log line (the 3 s window's
  zero-traffic fact is this flag's product).
- `main/include/controller/nfc_tag.h` + `nfc_tag.c` + `control_parser.c` + `gatt.c` — the
  bench knobs, **all default OFF**, each carrying its own comment and falsification record:
  `NFC_TAG_READ_DONE_BYTE` (the post-`0x06` byte — a *value* knob, default `0x03` = the #39
  behaviour), `NFC_TAG_READ_DONE_MS` (the byte pulse), `NFC_TAG_PUSH_READ_DATA` (the whole-tag
  push, paced by the 10 ms tick through the GATT-registered sink — arming deliberately
  independent of the byte knob), `NFC_TAG_STATUS_DONE_WHEN_READ` (the `04` answer),
  `NFC_TAG_READ_PAD_TO` (the zero pad) and `NFC_TAG_BUFFER_P1_PREFIX` (the framing+image
  served view). The status-only push knob was **removed after falsification** (the console
  drops unsolicited sub-0x05 frames — its record is take 4 above). The push order defaults to
  **status-first** — the order takes 5–10 ran without a crash; the trailer-last order that
  crashed twice is not what the tree builds.
- The host suite passes under the plain, `PAD_TO`, `P1_PREFIX`, `STATUS_DONE` and `WIRE_BASE`
  compiles (CI runs them all).
- **Promotion debt, from the code review**: the push path is bench-shaped — `gatt.c` hand-rolls
  the response frame header and the plan-executor tick paces BLE notifications, both
  §7.1/§7.2 layering breaches that are dormant while the knobs are OFF. If a push graduates
  into the design, the frame construction moves to `ns2_codec.c` and the pacing to the GATT
  event path first.

## 5. Two bench-path lessons (not NFC, recorded so they are not relearned)

- **The console-side unpair leaves the device's NVS bond stale, and re-pairing then fails
  silently** — the grip-order screen never offers the controller. `esptool erase_flash` +
  reflash cleared the device's bond and the console paired on the first attempt. The §9.3
  sleep/wake loop was why the operator had unpaired in the first place (its placement-must-
  not-outlive-the-scan-window rule stands).
- **A bench build's knob flags accumulate in `build-bench/CMakeCache.txt`** — a later
  `-DCMAKE_C_FLAGS` does *not* replace an earlier one, so takes silently inherit variables
  (take 8 ran pad+pulse believing it ran pulse alone). `rm -rf build-bench` before each
  variable change is the discipline; the ledger above is corrected for it.

## 6. ~~The next session's opening move~~ — take 13 has now run; see §7

The crash mode argues for evidence over guessing: **capture a real Pro Controller 2 amiibo
read** with the canonical repo's own method (an nRF52840 sniffer + the extracted LTK —
`switch2_controller_research/captures/nrf52840/` proves the pipeline exists). One afternoon
of capture answers every open question at once — the push shapes, the framing bytes, the
per-chunk cadence, the trailing region — where another evening of knob sweeps risks another
`2011-0301`. If the hardware is unavailable, the take-13 build (the `04`-hold) is the one
prepared, unrun experiment.

---

## 7. The second session (2026-10-04 12:00–12:25): take 13 runs, the crash ledger hardens, and #37's measurement stays gated

**Scope:** the ticket that owns this surface's endgame — #37, the freshness measurement —
needed the read to *complete* first, so this session ran the one prepared, unrun experiment
(take 13, the `04`-hold) and then its corrected twin. No nRF52840 was attached, so the
capture-first recommendation of §6 was unavailable; the sniffer-free path was exactly two
runs. The operator stopped the bench after the second console crash, per the G-18 discipline.
**The freshness question was not reached** — no completed read, no game bookkeeping to watch.

**Setup.** The console had been unpaired console-side since the last session (the §5 lesson),
so the device's NVS bond was stale: `esptool erase_region 0x9000 0x6000` (the `nvs`
partition, `partitions_16mb_s3.csv`), hard reset, and the console — parked on its pairing
screen by the operator — **re-paired on the first attempt** (§5's recovery path, now
re-verified). Two further observations from the pairing state: the console connected and
resubscribed within a second of the driver attach, and while it sat on the pairing screen
consuming no input, the device logged `hid: msys low, dropped N reports` climbing ~100/s —
benign (the queue drains the moment the console reads input), but it is what an idle-console
attach looks like on the wire.

### 7.1 The two runs

Both: the register screen (设置 → amiibo → 添加所有者和昵称), the **operator's Joy-Con
parking the cursor and the device's own `A` pressing into it** (the §2 binding rule, now
the standing mechanics), `place` of Mario under a fresh identity, and `unplace` to force
the trace drain. The console's cycle both times: the one-shot `0x03` probe → `0x05`
answered `00` **during the §6.5 gap** → `0x05` answered `09` after it → `0x04` → the
1000 ms `0x03` re-arm → `0x06` → the probes → `0x05` answered `04` → the ~3 s deadline
→ `0x04`.

| Run | Build (SHA-256 head) | The knobs | The console's answer | The screen |
| --- | --- | --- | --- | --- |
| A (12:14) | `2a25eae2…` | the session's rebuild of §1's take-12 set — `FOLLOWS+PUSH+STATUS_DONE+P1_PREFIX` — which had **silently lost `READ_DONE_BYTE=0x04`** to the post-review reconfigure, so the byte held `03`; no pad | `0x06` (t=91308) → **three `0x15` pulls** — `off=0040`, `0140`, `02c0` (`n=67`, `67`, **`0`** — `0x2c0` falls outside the 600 B P1-served space) → `0x05` → answered `04` → silence past the deadline | **crash** (`2011-0301`, forced reboot; the device saw the reboot as a disconnect + reconnect) |
| B (12:20) | `d8b279ba…` | the **corrected take 13**: `FOLLOWS+PUSH+STATUS_DONE+PAD_TO=0x300+BYTE=04`, image at 0, byte **held** (`READ_DONE_MS=0`) — one variable over take 10 (the last no-crash causality build) | `0x06` (t=54419) → **the same three pulls** — `0040`, `0140`, `02c0` (now `n=67` of pad zeros) → `0x05` → answered `04` → the byte returned `04→00` at the console's clean `0x04` stop (t+3.0 s) | **crash again** (the operator watched the forced reboot; the link cycled `531`-drop/reconnect through the aftermath) |

The two builds' shared mechanics: the push (status-first, then 64 B chunks across the served
space) streamed during the console's probe window, and `STATUS_DONE_WHEN_READ` answered the
console's `0x05` with `04` — the take-10 causality held both times (ask → `04` → the pulls
ride inside ~125 ms of the `0x06`).

### 7.2 What the two runs established

1. **The byte-hold is a crash trigger in conjunction with the `04` answer — take 13's
   variable is falsified in its own context.** Run B differs from take 10 (no crash,
   several cycles) by exactly one knob: the done byte holds `04` instead of pulsing back
   to `02` after 150 ms. Run A crashed with the byte held at `03`, so the *hold*, not the
   value, is the discriminator. The first session's takes 5–7 held the byte too (the
   pulse knob only arrived at take 8) and never crashed — but they predate the `04`
   *answer* (take 10), so the honest statement is the **conjunction**: a byte pinned at
   read-done *while `0x05` answers also say `04` forever* is the shape that kills, and
   neither half alone ever did. The NS1 lifecycle's own shape agrees: after the data
   phase the state **returns to `09`** — tag still in field — it never rests at `04`
   (`ns1-nfc-read-decode.md` §2); two completion signals that never resolve back to
   tag-in-field is a state the console's module does not survive. The knob's default is
   now the pulse.
2. **The crash ledger, consolidated — no single variable explains all of it.** Five
   crashes across four configs: the trailer-last push order (§1 takes 11, ×2), the
   P1-prefix content (§1 take 12 late; run A), and the byte-hold **with** the `04` answer
   (runs A and B). The no-crash configs: takes 5–7 (byte held, no `04` answer) and takes
   8–10 (pulse, `04` answer from take 10, image-at-0, status-first, pad from take 7).
3. **The console's probe pattern is deterministic and independent of our variables.**
   `0x40`, `0x140`, `0x2c0` — identical across P1/image-at-0, byte `03`/`04`, pad on/off,
   ~30 ms apart, all within ~125 ms of the `0x06`. The third offset always falls past the
   540 B image (and past the 600 B P1 space); with the pad it reads zeros (`n=67`),
   without it reads nothing (`n=0`) — and the console stops asking either way, polls
   `0x05`, takes the `04`, and rides to its deadline. **The pad/empty-answer difference is
   not the crash discriminator** (run B crashed with the pad answering). What the console
   is looking for at those three offsets — extent-mapping samples, HMAC-region checks,
   reader-buffer framing — is not observable from this side, which is §6's capture case
   restated as a measurement.
4. **The read's furthest point is now three probes + the `04` answer, then the deadline.**
   The continuation suspects of §3 survive unchanged: the per-chunk ready signal (weakened
   — the `04`-hold that was its cheapest test is crash-correlated in the take-10 context,
   so if a per-chunk signal exists it must be a *pulse*), the framing content, the
   trailing region. One suspect is *narrowed away*: the empty-vs-zeros answer at the
   space's edge.

### 7.3 The freshness measurement this session came for — prepared, not run

#37's question (does the console's per-amiibo bookkeeping key on the UID?) needs a surface
with **console-side** bookkeeping. The register screen has none — owner and nickname live in
the tag's own bytes, so a rotated identity re-reads the same owner and nickname; nothing
console-side distinguishes the scans. The NS1 evidence's exact analog is a **game's
once-per-day scan limit** (games index daily scan records by UID,
`ns2-amiibo-path.md` §Q3), and the bench surface for that is the BotW rune this effort
already drives (`amiibo-game-surface-bench.md` §1). The prepared protocol, for whenever the
read gate opens:

1. **Scan identity A → reward granted.** The rune path, `place` (fresh mint), the read
   completes, the game records the scan.
2. **Negative control — scan identity A again → refusal expected.** The *same* tag
   re-placed byte-for-byte (`place` must therefore gain a fixed-identity or re-place form:
   `seal()` already accepts `identity=`, so the driver-level command is the small seam).
   A refusal here proves the day's bookkeeping is armed and keyed on *something* the same
   identity shares.
3. **Rotate to identity B (same figure) → scan.** Reward again ⇒ the key is the UID —
   freshness-by-UID measured true, the design's premise lands. Refusal ⇒ the key is the
   figure data — **the §6.8 branch opens with the evidence attached**, and #39 decides the
   fallback.

The forbidden outcome stays forbidden either way (ADR-0011): no path above serves a stored
tag unchanged — every placement is a re-seal under the minted (or fixed-for-control)
identity.

### 7.4 Where this leaves the tickets

- **The read gate still blocks #37.** Validation 5's continuation is unchanged in kind —
  further along than any pre-push run, still short of one whole tag — and the G-18 ledger
  now has three isolated factors and an empty prepared-experiment queue. The next move is
  §6's capture (nRF52840 + LTK), or the §6.8 branch decision with this record attached.
- **The §6.5 gap is compatible with the register surface's cycle** — both runs show the
  one-shot probe's `0x05` landing in the gap (`status=00`) and the re-arm cycle proceeding
  normally once the tag answers. Compatibility, not necessity (G-6's other half).
- **The re-pair recovery path is re-verified** (§5's lesson, second success), and the
  crash aftermath is characterised: the console force-reboots, the device auto-reconnects
  and re-answers the `0x0C` probe, and an idle console then cycles a `531` drop/reconnect
  pattern until input or sleep settles it.

---

## 8. The third session, prepared: the corrected combination (issue #45)

**Status: prepared, flashed, and run — the result is §9.** Every build in §1 and §7 served shapes that are now
G-19's defect, and the three tickets that carried the corrections landed (#46 the served shapes,
#47 the `0x14` write path, #48 the reader contract and the report byte). That changes what "the
continuation's variable" even is: this take is not another knob over take 13, it is the
reference's own shape with **one** bench knob on top, and the edge/level question is postponed
until the read is judged on a faithful wire.

### 8.1 Why this take is different in kind

Relative to take 10 — §1's last no-crash causality build — the corrected combination moves four
things at once, and they are not variables to search over: they are what a real Pro Controller 2
serves.

| Layer | The earlier builds (§1, §7) | The corrected build |
| --- | --- | --- |
| served space | 540 B with the image at wire 0, or the NS1 P1 prefix (600 B), 64-byte chunks, offset-echo head | 600 B `[60 B framing][540 B image]`, image at wire `0x3C`, 70-byte chunks, `last` · `len` head, the bare `01 00 00` marker past the space |
| status answer | `04` only when `NFC_TAG_STATUS_DONE_WHEN_READ` was set; else `09` | `09` → `04` (**a level**, the whole armed window) → `09`, by default (#48) |
| report byte | pinned (`NFC_TAG_BYTE_FOLLOWS_POLLING`, `READ_DONE_BYTE`/`MS`) | the reader's **event counter** `0x01`–`0x07`, by default (§4.9, ADR-0016) |
| the knob | up to five accumulated flags | `NFC_TAG_PUSH_READ_DATA=1`, and nothing else |

The byte-hold and trailer-last variants that crashed the console are **gone from the tree**
(#48 retired the knob family, #46 deleted the obsolete view knobs), so this build cannot inherit
them — which is also why the build below starts from a deleted `build-bench/` (§5's CMakeCache
accumulation lesson). The one comparison compile that survives is the plain-offset view
(`NFC_TAG_READ_PLAIN_VIEW`); it is not part of this take.

### 8.2 The build, flashed

From the repository root, ESP-IDF v5.5.5 active (`source ~/esp/idf-env-5.5.5.sh`):

```bash
rm -rf build-bench
idf.py -B build-bench -DCMAKE_C_FLAGS=-DNFC_TAG_PUSH_READ_DATA=1 build
idf.py -B build-bench -p /dev/cu.usbmodem5C930639851 flash
```

The one flag is the whole delta; `build-bench/toolchain/cflags` is the check:

```text
-DNFC_TAG_PUSH_READ_DATA=1
-mlongcalls
-fno-builtin-memcpy
-fno-builtin-memset
-fno-builtin-bzero
```

The last four are ESP-IDF's esp32s3 defaults, not knobs. Built and flashed 2026-10-04: app
image `0x86fb0` (552,880 B, 82% of the 3 MB slot free), SHA-256
`2de66430445ca227415464378f5af36f854421331066e466386de0ce3f1e7dca`, every segment
`Hash of data verified.` The container is **down** (`python3 scripts/start_container.py --stop`):
the resident driver is the port's one holder (§10.2), and it replaces the container's placement
role for the session.

### 8.3 The setup, and the one non-NFC prerequisite

1. **Pairing.** The console's own unpair leaves the device's NVS bond stale, and re-pairing then
   fails silently (§5). If the console was unpaired console-side since §7, erase the `nvs`
   partition before the run (`esptool erase_region 0x9000 0x6000`), hard reset, and let the
   operator re-pair from the console's pairing screen — it pairs on the first attempt.
2. **Bind the read to our reader** (§2 fact 1, §7 mechanics): the operator's Joy-Con navigates
   to **设置 → amiibo → 添加所有者和昵称** and parks the cursor; the device's own `A` presses into
   it. The prompt's icon must be the Pro Controller.
3. **Attach the resident driver with the full tee** — it is what makes either answer legible:

   ```bash
   container/.venv/bin/python scripts/bench_press_buttons.py \
       --fifo /tmp/bench-fifo --tee-all --figure '<the Mario tag §7 placed>'
   ```

   `--tee-all` tees every device log line, not just the NFC ones — the ~3 s window's
   zero-traffic fact is this flag's product, and the `console nfc: …` lines are the measurement.
   Rotation is off in resident mode deliberately (the operator owns placement timing), so the
   session drives placement by hand:

   ```bash
   echo place   > /tmp/bench-fifo
   echo unplace > /tmp/bench-fifo
   ```

### 8.4 What to watch, in order

The console's cycle is §7's: the one-shot `0x03` probe → `0x05` (`00` in the §6.5 gap, then
`09`) → `0x04` → the 1000 ms `0x03` re-arm → `0x06` → the probes → `0x05` answered `04` → the
~3 s deadline → `0x04`. Under the corrected shapes the questions are:

1. **Does the console pull more than the three deterministic probes?** §7 saw exactly
   `0x40`/`0x140`/`0x2c0` on every variable. A sequential read of the corrected 600-byte space
   is nine 70-byte chunks at wire offsets `0x0000`, `0x0046`, `0x008C`, `0x00D2`, `0x0118`,
   `0x015E`, `0x01A4`, `0x01EA`, `0x0230` (the last serving 40 bytes, `n=43`, `last=1`). More
   than three distinct `0x15` offsets, or offsets that advance sequentially by 70 (`0x46`) rather
   than the old 64-aligned probe samples (`0x40`/`0x140`/`0x2c0`), is the first evidence the
   correction reached the console.
2. **Does the console get past its ~3 s deadline?** A completed read is the register screen
   moving on to the figure's own data instead of the read-error chime and the next re-arm. The
   deadline riding out unchanged is the failure.
3. **Does the report byte move?** The `nfc byte:` lines should show the counter stepping
   `0x01`–`0x07` across the read's stages rather than holding one value (§4.9's amendment; a
   hold is the crash flank G-18 isolates).
4. **Does the console crash?** `2011-0301` is the field to grep for. This build is the
   reference's own no-crash shape — the level `04` **and** the moving byte **and** the safe
   push order — so a crash here is new information, not a re-run of a falsified variant.

**A whole read is the goal** (issue #45's "Done when"): all 540 image bytes across the `0x15`
pulls. The trace's distinct `0x15` entries should cover the image (wire `0x3C`–`0x257`), and the
`last=1` chunk should cross. If the console instead stops at three probes again, the continuation
is still unanswered and the next variable is the one Route 1 always named.

### 8.5 The abort rule (G-18's discipline, unchanged)

**Two consecutive console crashes stop the bench.** The ledger is five crashes across four
configs with three isolated factors and no single explanation; a sixth is not free. One crash is
a datum — note the trace and the screen state, and continue only if the operator agrees. A second
consecutive crash ends the session, whatever the trace shows.

### 8.6 What each outcome decides

- **A completed read** releases the freshness measurement (#37): the two-rotation protocol with
  its same-identity negative control (§7.3) is already written, and the read gate it waits on is
  this one.
- **A stop at three probes again** leaves the continuation open. The next variable is the
  **edge vs level** question Route 1 was built for — the ns1-faithful lifecycle answers `04`
  once and returns to `09`, where #48's default answers `04` as a level for the window. That
  knob no longer exists in the tree (retired by #48), so re-preparing it is itself a small
  change; the decisive route remains the real-controller capture (Route 2, nRF52840 + LTK) that
  answers the push shapes, the framing and the trailing region together.
- **Either way, if nothing completes**, the fallback question (#39) decides what replaces the
  read, with §7's and this session's evidence attached.

---

## 9. The third session, run (2026-10-04 23:50 – 2026-10-05 00:25): the console pulls the served space's tail, and the same-UID re-presentation crashes it

**Scope.** §8's take, on the operator's console: the corrected combination
(`NFC_TAG_PUSH_READ_DATA=1`; app `0x86fb0`, SHA-256 `2de66430…`) first, then — after the console
re-prompted — the reference's continue-for-write as a second build
(`NFC_TAG_PUSH_READ_DATA=1 NFC_TAG_DEFER_READ_EJECT=1`; app `0xd8700`, SHA-256 `d38e7cdb…`) with
the driver's new `place-same` re-presenting the *same* identity. The session stopped on the
operator's call after the console's `2011-0301` (G-18's discipline: one crash is a datum, and
this run had no progress to weigh against a second crash).

Setup both times: the operator's Joy-Con parks the cursor on 设置 → amiibo → 添加所有者和昵称,
the device's own `A` enters (the §2 binding rule; the prompt's icon is the Pro Controller), the
resident driver running `--tee-all`, and `place` of Mario (`Super Mario Amiibo/Mario.bin`) under
a fresh identity.

### 9.1 The full pull: the corrected shapes are accepted

Fresh identity `04c13b1e066980`. The console's cycle, verbatim from the trace:

    0x03 (cfg 0000002c01) → 0x05 status=09 → 0x04      ← the one-shot probe
    [push: 0x05 (61 B) + 9×0x15 (8×70 B + 40 B), the whole 600-byte space]
    0x03 (cfg 00e8032c01) → 0x05 status=09 → 0x06 len=19
    → 0x05 status=04                                   ← the armed-level gate
    → 0x15 off=0046, 008c, 00d2, 0118, 015e, 01a4, 01ea   (n=73, 70 data bytes each)
    → 0x15 off=0230 n=43 (last=1)                      ← the served space's end
    → 0x04 → 0x03 → 0x05 status=07 → 0x04
    scan cmds=17 [03=2 04=3 05=3 06=1 14=0 15=8] reps=2 drops=0

**The console pulled every chunk it asked for.** Eight sequential chunks from wire `0x46` to
`0x230`, the final one `last=1` — 530 bytes (7×70 + 40) — the canonical capture's own `0x46`
start, whose first 70 bytes had already been consumed (here by the push, which serves the whole
space from wire `0`). §7's three deterministic probes (`0x40`/`0x140`/`0x2c0`) are gone and the
step is 70. The report byte walked `01`–`07` across the read. **No crash.** `0x14`/`0x08` never
arrived. What the device cannot observe is whether the console *used* the pushed first chunk;
the pulls alone are 530 of the space's 600 bytes (and 530 of the image's 540, since the image
starts at wire `0x3C`).

### 9.2 The console does not advance; it re-prompts

After the read the console re-armed once (`0x03` → `0x05 status=07`, the post-eject answer →
`0x04`) and stopped, back on the 读取 amiibo prompt: no owner/nickname editor, no error chime.

That re-prompt is what the reference's flow predicts. Its *first* presentation also ejects after
the completed read (`defer_read_eject=false`, `ns_pc_control/server/src/s2_nfc_codec.cpp:764-769`);
the console's second **placement** is the "write what you just read" step, and only the **same
identity** placed again inside 30 s sets `defer_read_eject`, so that read's stop keeps the tag in
the field for the write (`ns_pc_control/server/src/virtual_controller.cpp:324-331`). §8's second
take ported exactly that, and the driver gained `place-same`.

### 9.3 The same-UID re-presentation, and the crash

`place-same` (identity `04c13b1e066980` again, 12 s after the first placement) produced:

    0x03 → 0x05 status=09 → 0x04          ← one probe cycle; the drain reads 06=0
    nfc byte: 00→06→07
    nfc byte: 07→01                       ← operation-ready: the console DID arm a read (0x06)
    [push: 0x05 (61 B) + 9×0x15]          ← the device streams the whole tag
    0x15 pulls: none. 0x04: none.
    +95 s:  hid: msys low, dropped 101 reports
            console link: disconnected (reason=520)   ← the console's 2011-0301 forced reboot
    +7 s:   reconnected; the container rotated to 04ddc414225de4 (the drop policy)

**This is the session's crash and it is a new factor.** On the fresh-identity placement the
console pulled all eight chunks and did not crash. On the same-identity re-presentation it armed
the read (`0x06` — the report byte's operation-ready edge is consistent with it, though the
trace had not drained and the summary is not a witness) and then **pulled nothing**, while the
device pushed the whole tag as unrequested `0x15` notifications. ~95 s later the console's
amiibo module crashed and force-rebooted.

Two candidates, **not separated**: the repeated identity (the console had already read this
UID), and the unsolicited whole-tag push with no pull. The defer itself did not execute — there
was no `0x04` on that placement — so `NFC_TAG_DEFER_READ_EJECT` is not implicated by this run
and it stays default OFF.

### 9.4 Where this leaves the read gate

- **The wire shapes are settled.** The console accepts the corrected 600-byte space, the
  70-byte `last` · `len` chunks and the level `04`: it pulls every chunk it asks for and does
  not crash. §3's "one to three chunks, then the deadline" is closed.
- **The continuation is a presentation/lifecycle question, not a framing one.** The console
  pulled the space and still did not advance: it wants the second placement of the *same*
  amiibo, and that placement is where it broke.
- **The isolation that remains**, in order: (a) the same-UID re-presentation **with the push
  off**, to separate the repeated identity from the unrequested stream; (b) the deferred stop
  itself, once a read reaches it without a push. One bench build each, under G-18's
  two-consecutive-crash stop.
- The freshness measurement (#37) is still blocked: the register screen has no console-side
  bookkeeping and the read gate is not past the editor.
