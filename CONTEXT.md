# NS2 Controller Emulator

An ESP32-S3 board that a Nintendo Switch 2 sees as a Pro Controller, driven over USB serial by a container that owns the macro and amiibo libraries.

## Language

### Cast

**Device**:
The ESP32-S3 board and the firmware on it: it answers to the container over the control link and presents a Pro Controller to the console over the console link.
_Avoid_: board, controller, emulator, MCU

**Container**:
The host-side service that owns the macro and amiibo libraries, seals tags, and drives the device by sending verbs over the control link.
_Avoid_: app, server, host, backend, web UI

**Console**:
The Nintendo Switch 2 the device presents itself to.
_Avoid_: NS2, Switch, host, client

### Modes and links

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

**Control link**:
The USB serial connection between the container and the device.
_Avoid_: connection, serial link, USB link

**Console link**:
The BLE connection between the device and the Nintendo Switch 2.
_Avoid_: BLE link, wireless link

**Bond**:
The durable pairing keys the device holds for one console; the only state that survives a power cycle.
_Avoid_: pairing, pair info, trust, session

**Boot id**:
The value identifying one device power cycle, so the container can tell a reconnect from a restart.
_Avoid_: session id, instance id, run id

**Stateless**:
Holding no durable state beyond the bond. Volatile execution state is allowed and is reported.
_Avoid_: memoryless, ephemeral, RAM-only, no state

### Macro and plan

**Macro**:
The canonical JSON recording of a controller session: an event list of button and stick changes. The container's input unit, which must be compiled into a plan before the device can run it.
_Avoid_: recording, sequence, script, macro file

**Plan**:
The compiled form of a macro: a self-describing sequence of plan frames plus a loop length, replayable by the device from RAM.
_Avoid_: binary, blob, macro (for the compiled bytes), program

**Plan frame**:
One record of a plan: a full controller state — buttons and both sticks — together with the hold for which it is held.
_Avoid_: step, keyframe, tick, line, event

**Hold**:
The time a plan frame lasts.
_Avoid_: duration, delay, interval

**Loop**:
One complete pass through a plan. It ends at a loop boundary, where the device emits neutral and immediately restarts.
_Avoid_: cycle, pass, iteration, repeat

**Plan hash**:
The identity of a compiled plan, derived from the plan's own bytes and computed by the container rather than the device.
_Avoid_: checksum, digest, plan id, fingerprint

**Committed**:
Held by the device as its active plan or tag, as opposed to staged. Only a committed plan can be replayed, and only a committed tag can be placed.
_Avoid_: active, loaded, saved, accepted

**Staging**:
Bulk bytes (a plan, a tag) held by the device but not yet committed. Not a mode: a plan is staged only while `IDLE`, a tag also while `AMIIBO`.
_Avoid_: loading, upload buffer, pending, temporary

**Neutral**:
The report in which every button is released and both sticks are centred.
_Avoid_: release, reset, idle report

### Amiibo

**Amiibo**:
The figure data the console consumes, and the subsystem that serves it: figures, identities and tags.
_Avoid_: NFC, tag (for the subsystem), nfp

**Figure**:
The model of amiibo that a tag reproduces: one character and series, independent of any particular tag. One figure has many identities.
_Avoid_: character, model, series, amiibo type

**Identity**:
The UID that tells two tags of the same figure apart, and that the console keys its per-amiibo bookkeeping on.
_Avoid_: uuid, serial, tag id

**Tag**:
A figure's data sealed under one identity: the image of a physical tag, as the console reads it.
_Avoid_: dump, file, bin, amiibo (for the bytes)

**Sealing**:
The act that turns a figure's data into a Tag: an identity is chosen and the data is encrypted and signed under it.
_Avoid_: packing, signing

**Key material**:
The secret input that sealing requires: the retail key file, supplied by the user and never distributed with the product.
_Avoid_: key, keys, secrets

**Rotation**:
The replacement of a placed tag with a fresh identity of the same figure.
_Avoid_: randomise, regenerate, refresh

**Placement**:
A period in which one tag is placed.
_Avoid_: scan, tap, session

**Scan**:
The console's read of a placed tag, within one placement.
_Avoid_: read, tap

### Control link

**Verb**:
One named request the container can send the device.
_Avoid_: command, opcode, method, action

**Frame**:
One message on the control link.
_Avoid_: packet, message, telegram, datagram

**Frame type**:
Whether a frame asks, answers, or announces: a request carries a verb, a reply answers one, and an event is unsolicited and marks an edge.
_Avoid_: direction, kind, category

**Kind**:
The identity of an `EVENT`: which edge fired. One byte in the event's payload, on a different axis from the frame type.
_Avoid_: event type, subtype

**Bulk window**:
The bytes a container may send in a bulk transfer before it must wait for an ACK. One window may be unacknowledged at a time, and an ACK names the next expected offset.
_Avoid_: credit, buffer, chunk (a chunk is one frame; a window is several frames)

**Panic stop**:
The one halt of the active mode that does not come from the container: a press at the board itself.
_Avoid_: emergency stop, abort, kill switch

## Spelling

Lowercase is the artifact, uppercase is the mode: **macro** is the JSON recording and `MACRO` is the mode replaying a **plan**; **amiibo** is the artifact and `AMIIBO` is the mode presenting a **tag**. _Plan_ never means the JSON, and _macro_ never means the compiled bytes.
