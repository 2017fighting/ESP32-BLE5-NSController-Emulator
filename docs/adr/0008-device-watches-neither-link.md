# The device watches neither link

Neither the control link nor the console link can stop a mode. If the container dies
mid-macro, the macro keeps replaying. If the console sleeps and drops the BLE link, the
macro keeps replaying. The device has no heartbeat, no watchdog and no stop-on-link-loss.

The reflex is to stop when the commander disappears, and it is what most firmware does. It
was rejected because the device cannot distinguish "the console is asleep" from "the console
is slow", so inventing a difference creates a state it cannot observe; because a device that
stops when its commander vanishes needs a heartbeat the protocol was designed not to have;
and because the layering is cleaner if the device is dumb and the container is opinionated.
Console-lifecycle policy is the container's, and it can see link state in `STATUS`.

## Consequences

- A macro can keep looping after the driver has died, and only a person at the board will
  notice. The mitigation is the BOOT panic stop, not a watchdog — an accepted risk.
- On a console re-subscribe the pass **continues at its current frame** rather than
  restarting; restarting would silently produce a shorter loop and make the frame counter
  appear to jump backward, indistinguishable from a bug. Neutral is re-armed on re-subscribe.
- Whether the console is content with a pass resumed mid-press is unverified and needs a
  macro mode on the bench; the container's answer is to stop the run on a console link drop
  rather than take that risk.
- The container owns the choice to stop; the device reports `CONTAINER_STOP` and never learns
  that a console link was the reason.
