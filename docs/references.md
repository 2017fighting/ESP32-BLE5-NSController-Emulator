# External references

The single source of truth for the external material this effort reads: which repos are
canonical inputs, which are context only, and which are untrusted. Every citation in
`docs/research/`, `CONTEXT.md`, `docs/adr/` and the spec resolves through this file.

## Resolution

A reference is written as an **alias** plus an **`owner/repo@sha` pin**:

| where        | form                                            | example                                          |
| ------------ | ----------------------------------------------- | ------------------------------------------------ |
| prose        | `<alias>/<path>[:<lines>][#<anchor>]`           | `switch2_controller_research/hid_reports.md#input-report-0x09` |
| this file    | `<alias>` → `github.com/<owner>/<repo> @ <sha>` | see the tables below                             |

- The alias resolves against **`$REFERENCE_ROOT`**, default `~/clone`, as
  `$REFERENCE_ROOT/<alias>`. Two aliases are git remotes of this repo rather than
  clones, and are marked as such below.
- A trailing **`#<anchor>`** names a section in the rendered file. It is used only when
  building the `open at` permalink; it is not part of the filesystem path.
- The `owner/repo@sha` pin is the source of truth, **not** the local clone. Any claim can
  be re-verified by cloning the pin, so the line numbers in a citation are meaningful
  only against that pin. Each row also carries an **`open at`** base URL, so a citation
  resolves without a clone: `<open at><path>` is a permalink to the pinned file.
- **Never write an absolute path into repo docs.** `/home/zhao/clone/...` breaks on any
  other machine; the alias does not.
- When the local clone is absent, say so in the citation
  (`switch2_controller_research/commands.md:47-100 — not available locally; source:
  ndeadly/switch2_controller_research@a3306b4`). A pin that cannot be fetched is a
  defect to raise, not to paper over.

## Tiers

- **Canonical** — trusted, pinned and quotable. A canonical source may be cited by line
  number, and may carry a claim on its own. Code comments and ADRs may rest on it.
- **Context** — background and shape only. Read to understand the space (NS1-era
  protocol, other control planes, other futures), but **never** the basis of an NS2
  claim. Pinned so "the revision we read" stays identifiable.
- **Untrusted** — read for ideas and failure modes only. Its code and its written
  conclusions can both be wrong; reuse nothing (map Q13). Deliberately **not** pinned,
  because a pin would imply the working tree is a reproducible artifact.

## Canonical inputs

| alias                         | repo                                                                  | pin                                        | open at                                                                                          | what it is                                                                 |
| ----------------------------- | --------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `switch2_controller_research` | `ndeadly/switch2_controller_research`                                 | `a3306b473acff0d6844fb1e288883a3940df0baf` | https://github.com/ndeadly/switch2_controller_research/blob/a3306b473acff0d6844fb1e288883a3940df0baf/ | **The primary NS2 protocol source**; the basis for code comments and ADRs. |
| `switch-controller-macro`     | `Orangeeeeeeeeeeeeeeeeee/switch-controller-macro`                     | `202e512206ab955361aa40e8e2a2735cbe142175` | https://github.com/Orangeeeeeeeeeeeeeeeeee/switch-controller-macro/blob/202e512206ab955361aa40e8e2a2735cbe142175/ | Macro library `宏/*.json` and `web_ui.py::_play`, the reference replay semantics. |
| `Amiibo`                      | `AmiiboDB/Amiibo`                                                     | `58cf4558de56863575421844bc27695ef1ba56ad` | https://github.com/AmiiboDB/Amiibo/blob/58cf4558de56863575421844bc27695ef1ba56ad/ | Tag images (`.bin`, `.nfc`) and `!Essential Files/key_retail.bin`.          |
| `emuiibo`                     | `XorTroll/emuiibo`                                                    | `28b357d5ce4aa373891c5294127f79137e0917ff` | https://github.com/XorTroll/emuiibo/blob/28b357d5ce4aa373891c5294127f79137e0917ff/ | Virtual-amiibo identity model: 10-byte uuid, `random_uuid`, per-game areas. |
| `UARTSwitchCon`               | `nullstalgia/UARTSwitchCon`                                           | `ad772477c19306feae61f9d4d3538c1ec0a6feb6` | https://github.com/nullstalgia/UARTSwitchCon/blob/ad772477c19306feae61f9d4d3538c1ec0a6feb6/ | `Protocol.md`, a second independent protocol reference.                     |
| `EasyMCU_ESP32S3`             | `EasyConNS/EasyMCU_ESP32S3`                                           | `1a39ca50d50f06a1a7c523c775f551dda75b996f` | https://github.com/EasyConNS/EasyMCU_ESP32S3/blob/1a39ca50d50f06a1a7c523c775f551dda75b996f/ | The S3 EasyCon line: N16R8 board, 16 MB partition layout, `sdkconfig.n16r8`. |
| `amiitool`                    | `socram8888/amiitool`                                                 | `4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3` | https://github.com/socram8888/amiitool/blob/4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3/ | Reverse-engineered amiibo cryptography: the tag/data HMAC and AES-CTR re-signing flow. **Not cloned locally.** |

## Context

| alias                                    | repo / remote                                              | pin                                        | open at                                                                                          | what it is                                                     |
| ---------------------------------------- | ---------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| `Nintendo_Switch_Reverse_Engineering`    | `dekuNukem/Nintendo_Switch_Reverse_Engineering`            | `b354f21ae81f7b0d1d060b2b61e66ad0d9bc1756` | https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering/blob/b354f21ae81f7b0d1d060b2b61e66ad0d9bc1756/ | NS1-era HID/NFC/SPI notes.                                     |
| `switchnotes`                            | `timmeh87/switchnotes`                                     | `a4bdfcb182939001ab0b118eee251dee03d0ea60` | https://github.com/timmeh87/switchnotes/blob/a4bdfcb182939001ab0b118eee251dee03d0ea60/ | NS1 protocol notes.                                            |
| `joycontrol`                             | `mart1nro/joycontrol`                                      | `18a09da1a04306534ff9e1df8a1a69c0192a3244` | https://github.com/mart1nro/joycontrol/blob/18a09da1a04306534ff9e1df8a1a69c0192a3244/ | NS1 controller emulation in Python.                            |
| `jc_toolkit`                             | `CTCaer/jc_toolkit`                                        | `9d0cc455aebd07930b557840b47cb26df9eb4a1f` | https://github.com/CTCaer/jc_toolkit/blob/9d0cc455aebd07930b557840b47cb26df9eb4a1f/ | Joy-Con hardware/SPI reference.                                |
| `fix`                                    | `xioxin/ESP32-BLE5-NSController-Emulator`                  | `f5e43dbaf347cd63d0e0190ea65f82edf6187492` | https://github.com/xioxin/ESP32-BLE5-NSController-Emulator/blob/f5e43dbaf347cd63d0e0190ea65f82edf6187492/ | Reference fork (git remote, not a clone): WiFi HTTP/WebSocket control plane, step scripts. |
| `easycon`                                | `EasyConNS/EasyMCU_ESP32C61`                               | `c86c150435a2f917b7ad7aaf3b95685827c00a97` | https://github.com/EasyConNS/EasyMCU_ESP32C61/blob/c86c150435a2f917b7ad7aaf3b95685827c00a97/ | Related C61 project (git remote, not a clone); a second command-router shape.            |

`fix` and `easycon` are remotes of **this** repo, not `$REFERENCE_ROOT` clones; reach them
with `git show <remote>/<branch>:<path>`. See `docs/agents/issue-tracker.md` for the full
remote convention.

## Untrusted

| alias             | repo                                   | observed HEAD (not a pin)                  | open at                                                                                     | why                                                                        |
| ----------------- | -------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `esp32-joycontrol` | `2017fighting/esp32-joycontrol`        | `c89452d23676495448991146a07d97dfa6f0f8fa` | https://github.com/2017fighting/esp32-joycontrol/blob/c89452d23676495448991146a07d97dfa6f0f8fa/ | Prior attempt at the same goal; AI-generated, tested on NS1, **never worked**. Read for ideas and failure modes only; do not vendor from it. |

The working tree was dirty when last read, so the recorded HEAD is not reproducible — part
of why it is left unpinned.
