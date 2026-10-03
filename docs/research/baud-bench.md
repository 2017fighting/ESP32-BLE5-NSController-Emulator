# The G-1 baud bench: 921600 fails the bulk path, 115200 survives (issue #33)

**Scope:** §12.2 validation row 1 and known gap G-1 — *does the control link work at 921600
with `ESP_LOG` on the same wire?* — answered by measurement, and the fallback the ticket
pre-authorised is taken: **the baud is 115200**. **Date:** 2026-10-03.
**Hardware on test host:** ESP32-S3-N16R8 on the CH9102 bridge, serial `5C930639851`
(`/dev/cu.usbmodem5C930639851`, resolved by `scripts/find_serial_port.py`), arm64 macOS host
plus the OrbStack-forwarded Linux container path. The board was **not bonded** during the
runs (`STATUS`: `console_link=0 bond=0`), so no console reconnect noise is in any number
below.

The one-line answer the ticket asked for: **921600 carries the control plane and the log
flood without a single corrupted frame, but the §2.7 window blast into the 256 B RX ring
degenerates every multi-window bulk transfer — 13–15 window retries per 4093 B, ~30 KB
resent per 4 KB plan, 0.1–0.3 KiB/s — and the failure is the rate, not the logs: the INFO
build fails identically. 115200 survives everything, on both host stacks, at every size up
to the 65528 B capacity maximum.**

---

## 1. The instrument

`scripts/bench_baud_flood.py` (+ `scripts/test_bench_baud_flood.py`, 8 checks): a stdlib +
pyserial client whose uploader is the **synchronous mirror of the container's real
`FrameIO.bulk`** (`container/ns2serial/frame_io.py`) — same window fill (4096 B of un-ACKed
chunks, sent back-to-back), same resume-from-the-ACK'd-offset retry at window granularity,
the same 2.0 s request / 5.0 s bulk timeouts and 3-retry ceiling the deployment carries. The
retry counts below are therefore the deployment's, not an idealised client's. The unit tests
pin the accounting against a fake device that encodes §2.7: offset-keyed resume ACKs, window
ACKs, the permitted early ACK, and §2.8's silence on an untrusted frame — which is why a
lost chunk surfaces as an ACK stall (a window retry) or as the offset-mismatch ACK the
*next* chunk draws, never as a CRC error report.

What is counted, and what each thing means:

| Counter | Meaning |
| --- | --- |
| `window_retries` | an ACK stall timed out and the uploader resent from the ACK'd offset (§2.7 rule 5) — **the ticket's "CRC retries"**: §2.8 makes a CRC-failed frame silent, so a retry is how frame loss is visible |
| `dup_acks` / `early_acks` | ACKs naming an offset already known / mid-window progress — the device-side loss signal, and §2.7's permitted early ACK respectively |
| `rx_bad_crc` | segments with the right 7+len geometry whose CRC failed — a reply split by a log line; this is what the shared TX lock exists to make impossible |
| `boot_ids` | one value per run means the link never reset |

Two builds were flashed, both from `scripts/sdkconfig.flood` or the tree defaults: the
**flood build** (`CONFIG_MCU_DEBUG=y`, `CONFIG_LOG_MAXIMUM_LEVEL=4` — the #21 §4.3 recipe:
every received frame draws DEBUG lines through the shared log hook, and the HID task's
"skipping report send" line floods at report rate whenever no console is subscribed; on the
wire that is ~4.5 KiB/s of ambient log text in idle) and the **INFO build** (the shipped
tree: `MCU_DEBUG` off, `LOG_MAXIMUM_LEVEL=3`, no ambient flood). During a transfer the §2.2
policy drops those lines after formatting them and lets one per 100 ms onto the wire.

## 2. The measurements

Ladder: plans of 540, 4093, 16358, 32715 and 65528 B (the last is the largest plan the
65536 B capacity accepts — 5956 records; 12+11·5957 = 65539 would not fit), ×2 repeats,
commit verified against `STATUS` (`plan_hash` echo, `frame_count`, `IDLE`) after each.
"blast" = the container's back-to-back window; "pace" is bench-only mechanism evidence
(`--pace-ms`).

| # | Image | Baud | Host | Send | flood-idle answered / bad-CRC | 4093 B: retries (resent) | 65528 B: retries | Throughput |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | INFO | 921600 | macOS | blast | 185/185, 0 | 15 (36.6 KB), 13 (30.7 KB) | not reached¹ | 0.1–0.3 KiB/s |
| 2 | INFO | 921600 | macOS | blast, 1 s stall budget | 87/87, 0 | 13 (26.6 KB), 14 (28.9 KB) | — | 0.3 KiB/s |
| 3 | flood | 921600 | macOS | blast | 189/189, 0 | 13, 14 (≈30 KB each) | not reached¹ | 0.1 KiB/s |
| 4 | flood | 115200 | macOS | blast | 135/135, 0 | **0 (0 B)** ×2 | **0, 0** | 9.0–9.1 KiB/s |
| 5 | INFO | 115200 | macOS | blast | 175/175, 0 | **0** ×2 | **0, 0** | 8.9 KiB/s |
| 6 | flood | 115200 | OrbStack Linux² | blast | 149/149, 0 | **0** ×2 | **0, 0** | 8.8 KiB/s |
| 7 | INFO | 921600 | macOS | pace 20 ms | 53/53, 0 | **0** | **0** | 7.7 KiB/s |
| 8 | INFO | 921600 | macOS | pace 10 ms | 54/54, 0 | 3 (5.6 KB) | — (16358 B: 16) | 0.2 KiB/s |
| 9 | INFO | 921600 | macOS | pace 5 ms | 54/54, 0 | 11 (23.5 KB) | — | 0.1 KiB/s |

¹ The 5 s stall per retry means a degenerate 65528 B transfer would outlast the bench
window; rows 1–3's 4093 B rows are complete and repeatable (six transfers across two builds,
13–15 retries every time). Every degenerate transfer still **completed** — hash verified,
link never reset — the cost is retries, not correctness.

² `docker run --device "$NS2_PORT:/dev/ttyACM0" python:3.12-slim` — OrbStack's forwarded
serial (§10.7's path), the container the deployment actually uses.

**Flood-idle latency** (`HELLO` blast, 2.0 s request timeout — the container's): at 921600,
min/median/p95/max = 50.3–50.9 / 52.6–58.3 / 55.5–60.5 / 56.1–60.8 ms on both builds; at
115200, 50.3 / 58.0–60.0 / 60.5 / 60.7 ms on INFO and 50.3 / 59.2 / **113.4** / 115.2 ms on
the flood build. The ~50 ms floor is the transport's (consistent with #30's ~60 ms `STATUS`
floor; #21's 19.6 ms predates the verbs), not the log drain: the drain shows in the tail —
the flood build's p95 at 115200 is ~2× the 921600 one, which is #31 §3.1's "at 921600 the
flood drains 8× faster" made precise. Steady-state flood never approached a 2 s timeout at
either baud; #31's 2 s event was a console-reconnect *burst* (thousands of lines at once),
not steady drain, and no console was connected in these runs.

**`boot_ids` = 1 in every row.** Across every flood, every ladder, both bauds: the link
never reset. Host-side `rx_bad_crc` = **0 in all nine rows** — the shared TX lock held at
8× the log rate; not one reply was split by a log line.

## 3. Why 921600 fails, and why the logs are exonerated

Rows 1–3 are the same failure with and without the flood: the DEBUG logging that G-1
suspected changes nothing material (the §2.2 bulk policy suppresses it to 10 lines/s on the
wire during a transfer, and the formatting cost is invisible next to the stall). What fails
is the **burst**: §2.7 sends a whole 4096 B window back-to-back — ~4.4 KB of wire bytes,
~47 ms of line at 921600 — into a device whose end-to-end bulk drain the pace rows bracket
at **≈14–20 KiB/s of wire bytes** (pace 20 ms = 13.7 KiB/s clean, rows 7; pace 10 ms =
27 KiB/s degenerate, row 8; pace 5 ms = 52 KiB/s degenerate, row 9). The 256 B driver ring
is 2.8 ms of line at 921600; whenever the drain chain (driver ring → zc ring → protocol
task, which answers one frame per poll) falls behind even briefly, bytes are lost without
flow control, the affected chunks fail CRC silently (§2.8), and the sender learns only when
the ACK stalls — then resends a window that overruns the same way. The observed cycle in
every degenerate row: the device consumes ~512 B, early-ACKs (§2.7's permitted outcome —
the device does its part; `early=7–9` per transfer), the rest of the window is lost, a 5 s
stall, a resend nets another ~512 B.

At 115200 the line itself is the throttle: 11.5 KiB/s of wire rate sits *below* the drain
ceiling, so the ring never overruns — rows 4–6 show zero early ACKs at all, because there
was no ring pressure to answer.

**The verdict, in the ticket's terms.** The baud that survives is **115200**. The retry
count there is **0** across every transfer (30 bulk transfers across rows 4–7 including the
65528 B maximum twice per row). The largest clean transfer is **65528 B** (5956 records,
the capacity maximum). macOS and the OrbStack-forwarded Linux container path behave
**identically** (rows 4 vs 6); no bare-Linux 921600 session exists to compare against —
`s3-bringup.md`'s Linux facts are enumeration and 460800 flashing, no bulk — and §2.1's
"Linux's `cdc_acm` drives it" was a claim about host capability, which is not where the
failure is: the failure is device-side drain, on a wire any host can fill.

## 4. What this settles for the neighbours

- **#34 (the 256 B ring under the largest plan)** — its measurement is effectively taken
  here as a side effect: the ring does **not** absorb the §2.7 window at 921600 (13–15
  retries per 4 KB), and at 115200 the question is moot because the line throttles below
  the drain rate (0 stalls at every size). Whether that retires #34 or it re-runs after a
  ring/window change is its owner's call; this record's numbers are the before-picture.
- **#31 §3.1** — its "the 2 s request timeout under a reconnect-time log flood is a fact
  for #33" is answered: steady-state flood at either baud never approached 2 s (p95 ≤
  115 ms); the 2 s event needs a console reconnect *burst*, which these runs (no console)
  do not reproduce.
- **The ~50 ms request floor** on the current build (vs #21's 19.6 ms) predates this ticket
  (#30 already quoted ~60 ms) and is the transport's RX timeout + poll cadence; a 2 Hz
  `STATUS` does not notice it.

## 5. Verification performed

| Step | Result |
| --- | --- |
| `python3 scripts/test_bench_baud_flood.py` | **8 tests, OK** (§2.7 semantics + retry accounting against the fake device) |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | 27 tests, OK |
| `ruff check scripts/` | clean |
| Builds: `scripts/sdkconfig.flood` @921600, same +115200, tree defaults @921600 and @115200 | 4 × `idf.py build` rc 0; `sdkconfig.h` verified per build; flashed with hash verified |
| The nine bench rows above | 30 clean bulk transfers + 12 degenerate ones, all commits hash-verified, `boot_ids`=1 per run |
| Board left on | the shipped tree defaults at the **new** default baud 115200 (this ticket's fallback), `LOG_MAXIMUM_LEVEL=3`, `MCU_DEBUG` off |

## 6. Still open

- **#34** — the ring/window decision this record feeds (raise the ring, shrink the window,
  or pace the sender — all names §12.2 row 2 already lists). Nothing here changes §2.7.
- **921600 as a future baud** — the pace rows show the wire itself is clean at 92 KiB/s;
  what is missing is device-side absorption. If a later ticket raises the drain (bigger
  ring, RX-side batching), this bench re-runs unchanged: `scripts/sdkconfig.flood` +
  `scripts/bench_baud_flood.py` reproduce the whole matrix.
