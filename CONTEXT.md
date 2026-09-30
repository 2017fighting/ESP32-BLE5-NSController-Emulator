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
The mode in which the device presents one placed tag to the console.
_Avoid_: tag mode, NFC mode

**IDLE**:
The mode in which the device is performing neither function: no plan is replaying and no tag is placed.
_Avoid_: stopped, ready, neutral

**Control link**:
The USB serial connection between the container and the device.
_Avoid_: connection, serial link, USB link

**Console link**:
The BLE connection between the device and the Nintendo Switch 2.
_Avoid_: BLE link, wireless link

**Staging**:
Bulk bytes (a plan, a tag, key material) held by the device but not yet committed. Staging is not a mode, and never coexists with one.
_Avoid_: loading, upload buffer, pending

**Neutral**:
The report in which every button is released and both sticks are centred.
_Avoid_: release, reset, idle report

**Panic stop**:
A halt of the active mode performed at the board with the BOOT button, independent of the container.
_Avoid_: emergency stop, abort, kill switch
