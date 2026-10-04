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
