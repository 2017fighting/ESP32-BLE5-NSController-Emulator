# The register screen: the read pipeline opens, the pull gate falls, and the console's crash mode (issue #37's surface)

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

## 6. The next session's opening move

The crash mode argues for evidence over guessing: **capture a real Pro Controller 2 amiibo
read** with the canonical repo's own method (an nRF52840 sniffer + the extracted LTK —
`switch2_controller_research/captures/nrf52840/` proves the pipeline exists). One afternoon
of capture answers every open question at once — the push shapes, the framing bytes, the
per-chunk cadence, the trailing region — where another evening of knob sweeps risks another
`2011-0301`. If the hardware is unavailable, the take-13 build (the `04`-hold) is the one
prepared, unrun experiment.
