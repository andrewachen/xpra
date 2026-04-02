#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests for NetEQ-inspired tempo decision logic.
# ABOUTME: Validates threshold-based state transitions and cooldown behavior.

import unittest

from xpra.audio.sink import (
    compute_tempo,
    TEMPO_NORMAL, TEMPO_PREEMPTIVE_EXPAND,
    TEMPO_ACCELERATE, TEMPO_FAST_ACCELERATE,
    TEMPO_COOLDOWN_TICKS, TEMPO_MIN_TARGET_MS,
)


class TestTempoThresholds(unittest.TestCase):
    """Test the threshold math for various target values."""

    def test_threshold_values_target_100(self):
        # target=100: lower=75, higher=max(100, 95)=100, fast=400
        target = 100
        lower = target * 3 // 4
        higher = max(target, lower + 20)
        self.assertEqual(lower, 75)
        self.assertEqual(higher, 100)
        self.assertEqual(higher * 4, 400)

    def test_threshold_values_target_40(self):
        # target=40: lower=30, higher=max(40, 50)=50, fast=200
        target = 40
        lower = target * 3 // 4
        higher = max(target, lower + 20)
        self.assertEqual(lower, 30)
        self.assertEqual(higher, 50)
        self.assertEqual(higher * 4, 200)

    def test_threshold_values_target_200(self):
        # target=200: lower=150, higher=max(200, 170)=200, fast=800
        target = 200
        lower = target * 3 // 4
        higher = max(target, lower + 20)
        self.assertEqual(lower, 150)
        self.assertEqual(higher, 200)
        self.assertEqual(higher * 4, 800)


class TestComputeTempoNormal(unittest.TestCase):
    """Test normal playback state."""

    def test_level_at_target(self):
        # level equals target — should be normal
        self.assertEqual(compute_tempo(100, 100, 1.0, 10), TEMPO_NORMAL)

    def test_level_between_limits(self):
        # level=80, target=100: lower=75, higher=100 → between → normal
        self.assertEqual(compute_tempo(80, 100, 1.0, 10), TEMPO_NORMAL)

    def test_level_just_above_lower(self):
        # level=76, target=100: lower=75 → just above → normal
        self.assertEqual(compute_tempo(76, 100, 1.0, 10), TEMPO_NORMAL)

    def test_level_just_below_higher(self):
        # level=99, target=100: higher=100 → just below → normal
        self.assertEqual(compute_tempo(99, 100, 1.0, 10), TEMPO_NORMAL)


class TestComputeTempoPreemptiveExpand(unittest.TestCase):
    """Test slow-down when buffer is draining."""

    def test_level_at_lower_limit(self):
        # level=75, target=100: lower=75 → at limit → slow down
        self.assertEqual(compute_tempo(75, 100, 1.0, 10), TEMPO_PREEMPTIVE_EXPAND)

    def test_level_well_below_lower(self):
        # level=20, target=100: well below → slow down
        self.assertEqual(compute_tempo(20, 100, 1.0, 10), TEMPO_PREEMPTIVE_EXPAND)

    def test_level_zero(self):
        # empty buffer → slow down
        self.assertEqual(compute_tempo(0, 100, 1.0, 10), TEMPO_PREEMPTIVE_EXPAND)

    def test_small_target_level_at_limit(self):
        # target=80: lower=60, level=60 → slow down
        self.assertEqual(compute_tempo(60, 80, 1.0, 10), TEMPO_PREEMPTIVE_EXPAND)


class TestComputeTempoAccelerate(unittest.TestCase):
    """Test speed-up when buffer has excess."""

    def test_level_at_higher_limit(self):
        # level=100, target=100: higher=100 → at limit → still normal (conservative)
        self.assertEqual(compute_tempo(100, 100, 1.0, 10), TEMPO_NORMAL)

    def test_level_above_higher(self):
        # level=150, target=100: higher=100 → above → speed up
        self.assertEqual(compute_tempo(150, 100, 1.0, 10), TEMPO_ACCELERATE)

    def test_level_just_below_fast_limit(self):
        # level=400, target=100: fast=400 → at limit → regular accelerate (not fast)
        self.assertEqual(compute_tempo(400, 100, 1.0, 10), TEMPO_ACCELERATE)


class TestComputeTempoFastAccelerate(unittest.TestCase):
    """Test aggressive speed-up for large buffer excess."""

    def test_level_at_fast_limit(self):
        # level=401, target=100: fast=400 → above limit → fast accelerate
        self.assertEqual(compute_tempo(401, 100, 1.0, 10), TEMPO_FAST_ACCELERATE)

    def test_level_well_above_fast(self):
        # level=800, target=100 → way above → fast accelerate
        self.assertEqual(compute_tempo(800, 100, 1.0, 10), TEMPO_FAST_ACCELERATE)


class TestComputeTempoCooldown(unittest.TestCase):
    """Test cooldown prevents rapid tempo oscillation."""

    def test_cooldown_blocks_change(self):
        # want to accelerate but only 2 ticks at current tempo → stay
        result = compute_tempo(150, 100, TEMPO_NORMAL, 2)
        self.assertEqual(result, TEMPO_NORMAL)

    def test_cooldown_at_threshold(self):
        # exactly at cooldown threshold → allowed
        result = compute_tempo(150, 100, TEMPO_NORMAL, TEMPO_COOLDOWN_TICKS)
        self.assertEqual(result, TEMPO_ACCELERATE)

    def test_cooldown_blocks_return_to_normal(self):
        # was accelerating, level dropped to normal range, but cooldown not met
        result = compute_tempo(90, 100, TEMPO_ACCELERATE, 3)
        self.assertEqual(result, TEMPO_ACCELERATE)

    def test_cooldown_allows_return_to_normal(self):
        # was accelerating, level normal, cooldown met → return to normal
        result = compute_tempo(90, 100, TEMPO_ACCELERATE, 5)
        self.assertEqual(result, TEMPO_NORMAL)

    def test_same_tempo_no_cooldown_needed(self):
        # already at desired tempo → no change regardless of ticks
        result = compute_tempo(150, 100, TEMPO_ACCELERATE, 0)
        self.assertEqual(result, TEMPO_ACCELERATE)

    def test_cooldown_blocks_preemptive_expand(self):
        # want to slow down but cooldown not met
        result = compute_tempo(50, 100, TEMPO_NORMAL, 1)
        self.assertEqual(result, TEMPO_NORMAL)

    def test_cooldown_allows_preemptive_expand(self):
        result = compute_tempo(50, 100, TEMPO_NORMAL, 5)
        self.assertEqual(result, TEMPO_PREEMPTIVE_EXPAND)


class TestComputeTempoEdgeCases(unittest.TestCase):
    """Test edge cases and disabled state."""

    def test_target_zero_returns_normal(self):
        # av sync disabled → always normal
        self.assertEqual(compute_tempo(50, 0, 1.0, 10), TEMPO_NORMAL)

    def test_target_negative_returns_normal(self):
        self.assertEqual(compute_tempo(50, -1, 1.0, 10), TEMPO_NORMAL)

    def test_target_below_minimum(self):
        # target below TEMPO_MIN_TARGET_MS → always normal (LAN protection)
        self.assertEqual(compute_tempo(10, 20, 1.0, 10), TEMPO_NORMAL)
        self.assertEqual(compute_tempo(0, 40, 1.0, 10), TEMPO_NORMAL)
        self.assertEqual(compute_tempo(100, TEMPO_MIN_TARGET_MS - 1, 1.0, 10), TEMPO_NORMAL)

    def test_target_at_minimum(self):
        # target=TEMPO_MIN_TARGET_MS: lower=45, higher=max(60, 65)=65
        # level=40 → below lower → slow down
        self.assertEqual(compute_tempo(40, TEMPO_MIN_TARGET_MS, 1.0, 10),
                         TEMPO_PREEMPTIVE_EXPAND)


class TestTempoStateTransitions(unittest.TestCase):
    """Test realistic sequences of state transitions."""

    def test_normal_to_preemptive_to_normal(self):
        # start normal, buffer drains, slow down, recovers, return to normal
        tempo = TEMPO_NORMAL
        ticks = 10

        # buffer drains below threshold
        tempo = compute_tempo(50, 100, tempo, ticks)
        self.assertEqual(tempo, TEMPO_PREEMPTIVE_EXPAND)
        ticks = 0

        # still low during cooldown
        tempo = compute_tempo(60, 100, tempo, ticks + 3)
        self.assertEqual(tempo, TEMPO_PREEMPTIVE_EXPAND)  # still slow (same desired)

        # buffer recovers, cooldown met
        ticks = TEMPO_COOLDOWN_TICKS
        tempo = compute_tempo(85, 100, tempo, ticks)
        self.assertEqual(tempo, TEMPO_NORMAL)

    def test_normal_to_accelerate_to_normal(self):
        tempo = TEMPO_NORMAL
        ticks = 10

        # buffer overfills
        tempo = compute_tempo(150, 100, tempo, ticks)
        self.assertEqual(tempo, TEMPO_ACCELERATE)
        ticks = 0

        # buffer drains back to normal, cooldown met
        ticks = TEMPO_COOLDOWN_TICKS
        tempo = compute_tempo(85, 100, tempo, ticks)
        self.assertEqual(tempo, TEMPO_NORMAL)

    def test_accelerate_to_fast_accelerate(self):
        tempo = TEMPO_ACCELERATE
        ticks = TEMPO_COOLDOWN_TICKS

        # buffer grows even more
        tempo = compute_tempo(500, 100, tempo, ticks)
        self.assertEqual(tempo, TEMPO_FAST_ACCELERATE)


if __name__ == "__main__":
    unittest.main()
