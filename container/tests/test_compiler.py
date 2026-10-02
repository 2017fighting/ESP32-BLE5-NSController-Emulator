#!/usr/bin/env python3
"""Compiler semantics to §5: ingestion, keep-filter, merge and encoding.

Run:  python3 -m unittest discover -s container/tests -p 'test_*.py'
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2plan import (  # noqa: E402
    BUTTON_BITS,
    PLAN_HEADER_SIZE,
    PLAN_RECORD_SIZE,
    MacroRejected,
    PlanHashMismatch,
    assert_echo,
    compile_json,
    compile_macro,
    encode_stick,
    encode_stick_axis,
    iter_events,
    pack_stick_data,
    payload_size,
    plan_identity,
)


def button(t: float, name: str, pressed: bool) -> dict:
    return {"t": t, "ev": {"type": "button", "name": name, "pressed": pressed}}


def stick(t: float, side: str, h: float, v: float) -> dict:
    return {"t": t, "ev": {"type": "stick", "stick": side, "h": h, "v": v}}


class Ingestion(unittest.TestCase):
    def assert_rejected(self, events, code=None, fragment=None):
        with self.assertRaises(MacroRejected) as ctx:
            compile_macro(events)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        if fragment is not None:
            self.assertIn(fragment, ctx.exception.reason)

    def test_empty_macro_rejected(self):
        self.assert_rejected([], code="EMPTY")

    def test_non_array_rejected(self):
        self.assert_rejected({"t": 0}, code="EMPTY")

    def test_unknown_event_type_rejected(self):
        self.assert_rejected(
            [{"t": 0, "ev": {"type": "motion", "x": 1}}],
            code="BAD_EVENT",
            fragment="unknown event type",
        )

    def test_unknown_button_name_rejected(self):
        self.assert_rejected(
            [button(0, "turbo", True)], code="BAD_EVENT", fragment="unknown button name"
        )

    def test_unknown_stick_side_rejected(self):
        self.assert_rejected(
            [stick(0, "middle", 0.5, 0.5)],
            code="BAD_EVENT",
            fragment="unknown stick side",
        )

    def test_out_of_range_stick_rejected(self):
        self.assert_rejected(
            [stick(0, "left", 1.5, 0.0)], code="OUT_OF_RANGE", fragment="out of range"
        )
        self.assert_rejected(
            [stick(0, "left", 0.0, -1.01)], code="OUT_OF_RANGE", fragment="out of range"
        )

    def test_non_finite_t_rejected(self):
        self.assert_rejected(
            [button(float("inf"), "a", True)], code="BAD_EVENT", fragment="finite"
        )
        self.assert_rejected(
            [button(float("nan"), "a", True)], code="BAD_EVENT", fragment="finite"
        )

    def test_missing_t_rejected(self):
        self.assert_rejected(
            [{"ev": {"type": "button", "name": "a", "pressed": True}}],
            code="BAD_EVENT",
            fragment="missing 't'",
        )

    def test_backward_t_rejected(self):
        self.assert_rejected(
            [button(10, "a", True), button(9, "a", False)],
            code="NON_MONOTONIC",
            fragment="backward",
        )

    def test_duplicate_t_allowed(self):
        plan = compile_macro([button(0, "a", True), button(0, "b", True)])
        self.assertEqual(plan.record_count, 1)
        self.assertEqual(plan.loop_ms, 0)

    def test_unknown_keys_rejected(self):
        self.assert_rejected(
            [{"t": 0, "ev": {"type": "button", "name": "a", "pressed": True, "x": 1}}],
            code="BAD_EVENT",
            fragment="unrecognised key",
        )
        self.assert_rejected(
            [
                {
                    "t": 0,
                    "extra": 1,
                    "ev": {"type": "button", "name": "a", "pressed": True},
                }
            ],
            code="BAD_EVENT",
            fragment="unrecognised key",
        )

    def test_non_bool_pressed_rejected(self):
        self.assert_rejected(
            [{"t": 0, "ev": {"type": "button", "name": "a", "pressed": 1}}],
            code="BAD_EVENT",
            fragment="boolean",
        )

    def test_invalid_json_rejected(self):
        with self.assertRaises(MacroRejected) as ctx:
            compile_json("{not json")
        self.assertEqual(ctx.exception.code, "BAD_JSON")


class Buttons(unittest.TestCase):
    def test_every_known_name_sets_its_bit(self):
        for name, (byte, bit) in BUTTON_BITS.items():
            plan = compile_macro([button(0, name, True)])
            state, _ = next(iter(iter_events(plan)))
            self.assertEqual(state[byte], 1 << bit, name)

    def test_buttons_are_never_filtered(self):
        # Two button changes 1 ms apart: both land, unlike the stick filter.
        plan = compile_macro([button(0, "a", True), button(1, "a", False)])
        self.assertEqual(plan.record_count, 2)
        states = list(iter_events(plan))
        self.assertEqual(states[0][0][0], 0x02)
        self.assertEqual(states[1][0][0], 0x00)

    def test_press_and_release_round_trip(self):
        plan = compile_macro([button(0, "plus", True), button(125.0, "plus", False)])
        states = list(iter_events(plan))
        self.assertEqual(len(states), 2)
        self.assertEqual(states[0][1], 125)
        self.assertEqual(states[1][0][0], 0x00)  # plus is byte 0 bit 6 -> 0x40
        self.assertEqual(states[0][0][0], 0x40)


class StickFilterAndMerge(unittest.TestCase):
    def test_keep_filter_uses_only_kept_events_for_the_counter(self):
        # Per §5.2 step 3: keep at 0, 10, 20; the 5 and 15 events are dropped
        # and do *not* reset the counter.
        events = [
            stick(t, "left", 0.1 * (i + 1), 0.0)
            for i, t in enumerate((0, 5, 10, 15, 20))
        ]
        plan = compile_macro(events)
        holds = [hold for _, hold in iter_events(plan)]
        self.assertEqual(plan.record_count, 3)
        self.assertEqual(holds, [10, 10, 0])
        self.assertEqual(plan.loop_ms, 20)

    def test_sides_have_independent_counters(self):
        events = [
            stick(0, "left", 0.5, 0.0),
            stick(5, "right", 0.5, 0.0),  # right's first kept event
            stick(5, "left", 0.6, 0.0),  # filtered: 5 - 0 < 10
        ]
        plan = compile_macro(events)
        self.assertEqual(plan.record_count, 2)

    def test_noop_state_is_folded(self):
        plan = compile_macro([button(0, "a", True), button(100, "a", True)])
        self.assertEqual(plan.record_count, 1)
        self.assertEqual(next(iter_events(plan))[1], 100)

    def test_same_millisecond_merges_into_the_later_state(self):
        plan = compile_macro([button(0.1, "a", True), button(0.4, "b", True)])
        self.assertEqual(plan.record_count, 1)
        state, hold = next(iter_events(plan))
        self.assertEqual(state[0], 0x03)  # a (bit 1) | b (bit 0)
        self.assertEqual(hold, 0)

    def test_no_trailing_neutral_frame(self):
        # Neutral is executor-owned (§4.6); the last authored state ends the plan.
        plan = compile_macro([button(0, "a", True), button(10, "a", False)])
        states = list(iter_events(plan))
        self.assertEqual(len(states), 2)
        self.assertEqual(states[-1][0][0], 0x00)
        self.assertEqual(states[-1][1], 0)  # runs to loop_ms, may be 0

    def test_loop_ms_is_the_rounded_span(self):
        plan = compile_macro(
            [button(1241.1, "plus", True), button(27445.8, "plus", False)]
        )
        self.assertEqual(plan.loop_ms, 26205)

    def test_hold_above_u16_rejected(self):
        with self.assertRaises(MacroRejected) as ctx:
            compile_macro([button(0, "a", True), button(70000, "a", False)])
        self.assertEqual(ctx.exception.code, "HOLD_TOO_LONG")


class StickEncoding(unittest.TestCase):
    def test_centring_rule(self):
        self.assertEqual(encode_stick(0.005, -0.005), (2048, 2048))
        self.assertEqual(encode_stick(0.0, 0.0), (2048, 2048))
        # One axis outside the epsilon means neither axis is forced to centre.
        self.assertNotEqual(encode_stick(0.005, 0.5), (2048, 2048))

    def test_axis_formula(self):
        self.assertEqual(encode_stick_axis(0.0), 2048)
        self.assertEqual(encode_stick_axis(0.5), 3072)
        self.assertEqual(encode_stick_axis(-0.5), 1024)
        self.assertEqual(encode_stick_axis(1.0), 4095)  # clamped
        self.assertEqual(encode_stick_axis(-1.0), 0)
        self.assertEqual(encode_stick_axis(2.0), 4095)  # values are pre-clamped too

    def test_pack_stick_data_layout(self):
        self.assertEqual(pack_stick_data(0x800, 0x800), bytes((0x00, 0x08, 0x80)))
        self.assertEqual(pack_stick_data(0x123, 0x456), bytes((0x23, 0x61, 0x45)))


class HeaderAndIdentity(unittest.TestCase):
    def test_payload_size_formula(self):
        plan = compile_macro([button(0, "a", True)])
        self.assertEqual(len(plan.payload), payload_size(1))
        self.assertEqual(payload_size(1), PLAN_HEADER_SIZE + PLAN_RECORD_SIZE)

    def test_identity_is_truncated_sha256_of_the_payload(self):
        plan = compile_macro([button(0, "a", True)])
        self.assertEqual(plan.identity, hashlib.sha256(plan.payload).digest()[:16])
        self.assertEqual(len(plan.identity), 16)
        self.assertEqual(plan.identity, plan_identity(plan.payload))

    def test_echo_mismatch_raises(self):
        plan = compile_macro([button(0, "a", True)])
        assert_echo(plan.identity, plan.identity)  # agrees
        with self.assertRaises(PlanHashMismatch):
            assert_echo(plan.identity, b"\x00" * 16)

    def test_capacity_is_a_container_side_precheck(self):
        events = [button(0, "a", True)]
        compile_macro(events, capacity_bytes=23)  # exactly fits
        with self.assertRaises(MacroRejected) as ctx:
            compile_macro(events, capacity_bytes=22)
        self.assertEqual(ctx.exception.code, "PLAN_TOO_LARGE")


class FixtureIngestion(unittest.TestCase):
    def test_correction_macro_compiles_without_reference_clone(self):
        plan = compile_json(support.correction_macro_path().read_bytes())
        self.assertEqual(plan.record_count, 71)
        self.assertEqual(plan.loop_ms, 26205)
        self.assertEqual(plan.size, 793)


if __name__ == "__main__":
    unittest.main(verbosity=2)
