# The game surface: player 1, the rune, and how far the read gets (issue #39's experiment)

**Scope:** the one experiment #36 handed on — a **game** reading through **this device as player
1**, which is the only surface that sends `0x03` unprompted. Run early, 2026-10-04 03:31–04:10,
under operator direction (the operator was in Breath of the Wild with the amiibo rune ready and
coached every screen; the device drove the inputs). **Hardware:** the same board/console pair as
`amiibo-read-bench.md`; the INFO build of #36 plus this session's two bench builds
(`bc620366…` and `dcab0a46…`, §3). Figure: Mario, fresh identity per placement.

**The one-line answer.** The game surface works, and the read opens: with the device as player 1
and the amiibo rune fired **from the device**, the console binds the read to **our reader** (the
prompt's icon says Pro Controller, not Joy-Con — operator-observed), sends **`0x03`** with the
captured poll configuration, asks **`0x05`** and is told the tag is there, and — once — sent
**`0x06`**, the read-device handshake no prior run had ever reached. Then it waits **~3 s**,
aborts with the game's read-error chime, and retries. **`0x15` has still never been sent.** The
failure point moved three subcommands in one night: `beyond 0x0C` → `beyond 0x05` → `beyond 0x06`.

---

## 1. The path that works (the operator-coached sequence)

Every step below was confirmed on the console's screen by the operator before the next ran
(`scripts/bench_press_buttons.py --fifo`, §2):

| Step | Action | What the console did |
| --- | --- | --- |
| 1 | `L+R` (claim player 1) | the controller-select prompt reacts — **and needs an `A`** to finish |
| 2 | *(2.5 s settle — §2's finding)* then `A` | control transfers to the device |
| 3 | `B` | exits a stale amiibo prompt left from an earlier attempt |
| 4 | `Up` | opens the rune list; amiibo is the selected rune; the list auto-closes |
| 5 | `L` | fires the rune: Link poses, the read prompt opens, **its icon is the Pro Controller** |
| 6 | *(0.4–1.5 s)* then `place` | `0x03` (poll start) arrives within a second of the prompt; the placement's `0x05` answers `status=09` + UID |

The claim is fragile in two observed ways: a controller change is a §9.1 full re-init that drops
input for the first moments (step 2), and after several consecutive read errors the game stopped
binding the read to our reader at all — one later rune firing produced **no reader icon and zero
NFC traffic**; only a fresh claim restored the binding once.

## 2. What the runs taught, in order

- **Runs 6/7 — the claim is a re-init.** `L+R` landed; the `A` 230 ms later never did. The
  console re-initialises its input pipeline on a controller change (§9.1), so the follow-up keys
  go out only after it settles: `--claim` and `--after-claim` are now separate sequences with
  `CLAIM_SETTLE_S = 2.5` between them, and the split's A/B both landed.
- **Run 9 — a menu animation eats keys too.** `B` (cancel) then `L` 80 ms later: the L never
  reached the game. The sequence grammar gained a per-item gap (`b:300:800`) and the stick items
  (`rstick:1/0:900`) for the rune list (Up opens it, the right stick moves the selection, amiibo
  sits last — the documented path).
- **The attach used to reboot the board.** pyserial raises RTS on construction and RTS is EN on
  this board, so *every* attach pulsed a reset — dropping the console link and the player
  binding, which is why "drive the menus" and "place the tag" could never be separate processes.
  `ns2serial/port.py` now constructs closed, applies the line state, and opens (§8.3's
  "deasserted on open" made literal; the port test asserts the new sequence). After the fix, a
  resident restart keeps the link and the claim.
- **The §6.5 rotation fought the console's retries.** The trace caught `0x05 status=00` — the
  tag-absent gap — landing **mid-retry**: the console errored, the container rotated on
  `SCAN_ENDED`, and the console's next ask hit an empty field. Resident mode now disables
  rotation (the operator owns placement timing); the gap-vs-retry interaction is a real finding
  for §6.5/§9.3 policy, not just bench noise.
- **Mode exclusivity forces the ordering.** A press macro needs `IDLE`; a placement means
  `AMIIBO` (§4.3) — so the placement is necessarily the **last** act before the read, and the
  L→place gap must fit inside the console's poll window. Two `0x03` configurations were
  observed: `00 e8 03 2c 01` (a ~1000 ms poll — bytes 1–2 look like a LE duration) and
  `00 00 00 2c 01` (a ~500 ms one-shot probe). A 1.5 s L→place missed the short probe; 400 ms
  fit inside it.

## 3. The firmware bench knobs (default OFF; the spec's design stands)

| Knob | What it does | The build |
| --- | --- | --- |
| `NFC_TAG_READ_WIRE_BASE` | §6.6's offset-space knob — still unexercised, no `0x15` has ever arrived | landed in #36's commit |
| `NFC_TAG_BYTE_FOLLOWS_POLLING` | the report byte becomes the polling level's image — `0x01` while the console polls, `0x02` on detection (the documented lifecycle), `0x03` after `0x06` — written from the 10 ms tick, one writer, **one INFO line per change** (`nfc byte: 00 -> 01`) so the wire's value is never a guess again | `bc620366…` (level image), `dcab0a46…` (+ `0x03` after `0x06`, + the byte log) |

Both builds were flashed and driven through §1's path. The **only** `0x06` ever observed arrived
on `bc620366…`: `0x05(09)` at t=147072, **`0x06 len=19` at t=147087 — 15 ms later** — then a 3 s
wait, `0x04`, the error chime, and retries (`0x03 → 0x05 → 0x04`) that never repeated the `0x06`.
On `dcab0a46…` the `0x06` did not reappear before the game stopped binding the reader (§1). One
observation is one observation: the byte extension is **not** vindicated or falsified — it is
untested against a second `0x06`.

## 4. The exact failure point, and what is left to try

**The console polls our reader, is told a tag exists, arms the read once — and then waits ~3 s
for something that never comes, errors out, and retries without arming again.**

Four hypotheses are eliminated by measurement: the probe byte (`10`/`0d`, identical — #36), the
placement ordering (tag before the poll and tag during the poll both reached `0x05(09)`), the
rotation gap (disabled; the `status=00` mid-retry was real but not the blocker), and — weakly —
the byte's polling lifecycle (the `0x01`/edge semantics did not prevent the error; the
`0x03`-after-`0x06` extension never got its test). What remains, in the order to try:

1. **The `0x05` response's undocumented flags.** We echo the capture's
   `00 00 00 01 01 02 00` verbatim — captured *mid-session*, on a real PN7160, after its own
   `0x03`/`0x06`. If the flags encode the reader/tag state, the console may abort on values that
   are right for a real reader mid-read and wrong for ours pre-read. Needs a fresh capture of a
   real scan to compare, or an NCI reading of what those bytes mean.
2. **The byte after `0x06`.** The 3 s wait smells like a state the console expects the reader to
   enter (the vocabulary has 0x03–0x07). The `dcab0a46…` build carries the `0x03` guess; it needs
   one more `0x06` to test against — repeat §1's path on a fresh claim, not the post-error state.
3. **The `0x06` ACK's shape.** We ACK bare (the captured reply is bare too, over USB) — verify
   the BLE variant byte-for-byte if another capture appears.

## 5. Evidence

The resident driver's full log for the decisive window (03:51–04:03), quoted from the session:

```text
claim:  L+R (03:51:04) → settle → A (03:51:30) → operator: “控制权到手”
B (03:51:56) · Up (03:52:19) · L (03:52:15) → operator: “图标显示的就是pro手柄 不是joycon了”
place (03:52:51) → the trace drains:
  console nfc: t=142315 sub=03 len=5 cfg=0000002c01     (the rune's one-shot probe)
  console nfc: t=177613 sub=05 status=09 n=61 crc=be3b  (the tag, seen)
  console nfc: t=177633 sub=04                          (stop — error)
  console nfc: t=177653 sub=03 len=5 cfg=00e8032c01     (the 1000 ms poll)
  console nfc: t=177913 sub=05 status=09 n=61 crc=8cd2
  console nfc: t=177933 sub=04
… and the one 0x06, from the same build one sequence earlier:
  console nfc: t=147072 sub=05 status=09 n=61 crc=5ce4
  console nfc: t=147087 sub=06 len=19
  console nfc: t=150087 sub=04                          (a 3.0 s wait, then abort)
  operator: “有个读取错误的提示音” · “两次读取失败的提示音”
```

The instrument (`scripts/bench_press_buttons.py`, the `--claim`/`--after-claim` split in
`scripts/bench_amiibo_read.py`, the two knob builds) is in the tree; the resident mode's
`press`/`place`/`unplace`/`status`/`sleep`/`quit` grammar is the operator-coached bench from
now on.
