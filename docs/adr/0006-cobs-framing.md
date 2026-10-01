# COBS framing with a CRC'd header, one framing for control and bulk

Every control-link message is a frame: a small binary header (protocol version, frame type,
verb, payload length) followed by a payload, with a CRC over the header, terminated by a
single `0x00` COBS delimiter. One framing carries control verbs and bulk upload alike.

The alternative was a text protocol — line-delimited JSON, or a log-friendly ASCII command
set — which is far easier to debug by eye and is what most hobby firmware does. It was
rejected because the control plane shares a wire with `ESP_LOG` (ADR-0001) and a text
protocol cannot be told apart from a log line without ambiguity, and because bulk upload of
plans and tags wants binary framing rather than a second escaping scheme.

## Consequences

- Frames are self-synchronising: a receiver scans forward for `0x00`, decodes, checks the
  CRC, and on failure advances to the next delimiter. A log line never contains `0x00`, so it
  arrives as CRC-failing noise and is discarded. Resynchronisation never resets the link.
- A bulk transfer is a sequence of ordinary frames carrying offsets, so upload needs no
  second protocol and no second parser.
- Two implementations of the framing must be kept in step — one in C, one in the container —
  and neither a golden fixture nor a shared header exists yet.
