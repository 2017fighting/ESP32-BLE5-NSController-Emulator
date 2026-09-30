# NS2 Controller Emulator

An ESP32-S3 board that a Nintendo Switch 2 sees as a Pro Controller, driven over USB serial by a container that owns the macro and amiibo libraries.

## Language

**Mode**:
The one of `IDLE`, `MACRO` or `AMIIBO` that the device is currently performing. Exactly one is active at a time, and `IDLE` is the absence of a mode rather than a third one.
_Avoid_: state, function, program

**MACRO**:
The mode in which the device replays the committed plan on a loop.
_Avoid_: replay, playback, macro mode

**AMIIBO**:
The mode in which the device presents one tag at a time to the console.
_Avoid_: tag mode, NFC mode

**IDLE**:
The absence of a mode: no plan is replaying and no tag is placed.
_Avoid_: stopped, ready, neutral

**Figure**:
The model of amiibo that a tag reproduces: one character and series, independent of any particular tag.
_Avoid_: character, model, series, amiibo type

**Identity**:
The seven-byte UID that tells two tags of the same figure apart, and that the console keys its per-amiibo bookkeeping on.
_Avoid_: uuid, serial, tag id

**Tag**:
One NTAG215 image: a figure's data signed under one identity.
_Avoid_: dump, file, bin, amiibo (for the bytes)

**Rotation**:
The replacement of a placed tag with a fresh identity of the same figure.
_Avoid_: randomise, regenerate, refresh

**Placement**:
A period in which one tag is placed.
_Avoid_: scan, tap, session

**Scan**:
The console's read of a placed tag, within one placement.
_Avoid_: read, tap

**Control link**:
The USB serial connection between the container and the device.
_Avoid_: connection, serial link, USB link

**Console link**:
The BLE connection between the device and the Nintendo Switch 2.
_Avoid_: BLE link, wireless link

**Staging**:
Bulk bytes (a plan, a tag) held by the device but not yet committed. Not a mode: a plan is staged only while IDLE, a tag also while AMIIBO.
_Avoid_: loading, upload buffer, pending

**Neutral**:
The report in which every button is released and both sticks are centred.
_Avoid_: release, reset, idle report

**Panic stop**:
The one halt of the active mode that does not come from the container: a press of the BOOT button at the board.
_Avoid_: emergency stop, abort, kill switch
