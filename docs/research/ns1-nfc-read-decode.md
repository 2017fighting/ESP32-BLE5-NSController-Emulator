# The NS1 read exchange decodes the NS2's `0x06` — and names the byte after it

**Scope:** the operator's article find
([elmagnifico.tech, 2022-09-07](https://elmagnifico.tech/2022/09/07/ESP32-Simulate-NS-JoyCon-Amiibo/))
pointed at three NS1 sources this effort had not read: the article author's own RE fork
(`elmagnificogi_nsre` — `NFC_notes.md` plus a **full real capture** of the read exchange),
`poohl_joycontrol` (the Python emulation of the same flow), and `libjoycon`. All three are
now pinned as context tier (`docs/references.md`). This note records what they decode that
the NS2 bench could not, and what it predicts for #39's three remaining suspects.

**Method:** byte-level comparison of the NS1 capture against the NS2 captures already in the
tree (`switch2_controller_research/commands.md`, `amiibo-game-surface-bench.md` §5). No NS2
claims rest on the NS1 sources alone — the predictions are bench hypotheses, and the bench is
the arbiter.

---

## 1. `0x06` is the NS1 "read the whole tag into your buffer" command, unchanged

The console's NS1 read command (`elmagnificogi_nsre` `nfc_debug/output_receive.txt`, the
`a2 11 … 2 6 00 00 08 13` exchange) and the NS2's `0x06` request carry the **same 19-byte
payload, field for field**:

| Bytes | NS1 (capture) | NS2 (capture) | Meaning |
| --- | --- | --- | --- |
| 0–1 | `d0 07` | `d0 07` | marker + UID length (7) |
| 2–8 | `00×7` | `00×7` | the UID field — all-zero means "read whatever is in the field" (a nonzero UID is the write setup, §4 below) |
| 9 | `01` | `01` | NTAG215-only (`elmagnificogi_nsre/NFC_notes.md` seventh step; `libjoycon/src/controller.cc` — 1: Ntag215 only, else error 0x48) |
| 10 | `03` | `03` | page-range count |
| 11–18 | `00 3b 3c 77 78 86 00 00` | `00 3b 3c 77 78 86 00 00` | ranges: pages 0x00–0x3b, 0x3c–0x77, 0x78–0x86 — **all 135 pages of the NTAG215** |

The ranges are why the read exists as a separate command: the console tells the *reader* to
pull the whole tag into the reader's buffer, and only then collects it. `libjoycon` builds the
same ranges for NTAG215 and widens them for NTAG216 (231 pages) — the layout is
page-pair-per-range, max four.

**So the NS2's `0x06` is not a handshake. It is the read command itself**, and the ~3 s wait
that follows it (#39's failure point) is the console waiting for the read to *complete*.

## 2. What "complete" looks like on NS1 — the `09 31 04` trailer

On NS1 the device does the whole transfer itself, in three pushed MCU input reports
(`elmagnificogi_nsre` capture; `poohl_joycontrol/joycontrol/mcu.py` reproduces all three
byte-for-byte):

| Packet | Opens with | Carries |
| --- | --- | --- |
| P1 | `3a 00 07 01 00 01 31 02 00 00 00 01 02 00 07` | the 7-byte UID, a 32-byte constant blob, the echoed page ranges, then image bytes 0–244 |
| P2 | `3a 00 07 02 00 09 27` | image bytes 245–539 |
| P3 | `2a 00 05 00 00 09 31 04 00 00 00 01 01 02 00 07` + UID | **the completion trailer — state `04`** |

The status line both sides speak is `2a 00 05 <seq> <ack> 09 31 <state> <flags> 07 <uid>`, and
the state vocabulary the capture walks is:

| State | Seen when |
| --- | --- |
| `00` | awaiting a command (NFC armed, idle) |
| `01` | polling, no tag (spammed continuously) |
| `09` | tag detected / still in field after a read (the "poll again" state) |
| `04` | **read complete** — P3, exactly once per read |
| `0b` | initialising/busy |

Poohl's enum names the same values (`NONE=00 POLL=01 PENDING_READ=02 WRITING=03
AWAITING_WRITE=04 PROCESSING_WRITE=05 POLL_AGAIN=09`) — note `04` is the *read* trailer state
even though his name for it says write.

## 3. What survives the transport change, and what the NS2 did with it

The NS2 BLE command protocol replaced the push with a pull — the console's `0x15` reads the
reader's buffer in 64-byte slices. Comparing the two status encodings:

- NS1 status line: `2a 00 05 seq ack 09 31 <state> 00 00 00 01 01 02 00 07 <uid>`
- NS2 `0x05` response: `09 <00 00 00 01 01 02 00> 07 <uid>`

The NS2 response is the NS1 status line with the MCU header and the `31 <state>` pair dropped.
Two consequences:

- **#39's suspect 1 (the `0x05` flags) drops to last.** The seven "undocumented flags" are
  literally the constant trailer bytes both Poohl and the real controller append after the
  state — `00 00 00 01 01 02 00`, sent identically in P1's header and P3's trailer. They are
  not reader-state encodings; echoing them was correct.
- **The state byte has nowhere to live in the response — so the completion signal must be the
  input report's NFC byte.** That byte is documented with values `0x00–0x07`
  (`switch2_controller_research/hid_reports.md:178`), which is exactly the NS1 state list
  minus `0x09` — and the console's 3 s wait with *no further `0x05` ask* (#39's trace) matches
  a console blocked on the input stream, not on a poll.

**Prediction — suspect 2 is now specific:** the byte after `0x06` completes the read is
**`0x04`**. The #39 build's `0x03` guess was one off — `0x03` is `WRITING` in the shared
vocabulary, a state a read never reaches. The knob `NFC_TAG_READ_DONE_BYTE`
(`main/include/controller/nfc_tag.h`, default `0x03`, bench build sets `0x04`) exists for the
A/B.

**The `0x15` offset-space hypothesis gains a mechanism.** P1 carries 106 bytes of framing
before image byte 0 — but the *image* bytes it serves begin at static-lock/CC (`0f e0 f1 10
ff ee`), byte-for-byte what the NS2's `0x15` at wire offset `0x46` returned. The NS2 reads the
reader's buffer; the buffer's layout descends from P1's, and the constant-shift hypothesis
(`wire = image + 0x3C`) stands unexercised until the first `0x15` answers.

## 4. A finding for the write path: the `0x14` payload is framed, not sliced

The canonical NS2 `0x14` write capture (`switch2_controller_research/commands.md`, the
`00 00 4c 00 …` exchange) does **not** carry a raw image slice: its data opens `d0 07 <uid7>
01 00 01 04 …` — the same marker/UID framing as the read command, i.e. NS1 write-setup data.
The current `0x14` handler (`nfc_tag.c`, offset+len into the raw image) fits the header fields
but would store framed bytes into the image. Nothing has exercised it (no write has ever
arrived), and the register screen — the next bench surface — both reads **and writes**. The
decode needed is small (Poohl's `com == 0x08` path is the NS1 reference) but it is not done;
recorded as G-17.

## 5. What this changes on the bench plan

1. **The knob build for the next session sets `NFC_TAG_READ_DONE_BYTE=0x04`** — one variable
   against #39's `0x03` run, same claim/rune path, fresh claim (the console stops re-arming
   after its first read error).
2. **The register screen stays worth its run** (the operator's request): the surface binding
   followed the controller that drove it in #39 (the prompt's icon), so an A pressed *by the
   device as player 1* may bind the register screen's read where the operator-navigated runs
   (#36 runs 4/5) never saw a `0x03`. Its write half needs G-17's decode first.
3. If `0x04` still yields no `0x15`, the remaining suspects are the `0x05` flags (weakened,
   §3) and the bare `0x06` ACK (weakened — the capture's ACK is bare too, and bare ACKs were
   accepted for `0x03`/`0x04`/`0x05` on BLE). The fallback question (#39's branch) opens with
   all three excluded.

## 6. Sources

- `elmagnificogi_nsre/nfc_debug/output_receive.txt` — the full real read exchange (the
  `2 6` command, P1/P2, the `09 31 04` trailer, the return to `09`).
- `elmagnificogi_nsre/NFC_notes.md` — the eight-step NS flow, the field meanings of the read
  command, the error codes (`0x48`/`0x3e`/`0x40`).
- `poohl_joycontrol/joycontrol/mcu.py` — the `NFC_state` enum, the three pushed packets'
  exact bytes, the write-accept state machine.
- `libjoycon/src/controller.cc` — the page-range construction for 213/215/216.
- `switch2_controller_research/commands.md` — the NS2 captures this note decodes against.
