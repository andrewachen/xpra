#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests for resize increment snapping in manual moveresize.
# ABOUTME: Verifies that alt-drag resize respects size hint increments (eg terminal cells).

import unittest

from xpra.client.gtk3.window.base import snap_to_increment


class TestSnapToIncrement(unittest.TestCase):

    def test_no_increment(self):
        # no increment hints: size unchanged
        assert snap_to_increment(803, 605, {}) == (803, 605)

    def test_increment_of_one(self):
        # increment of 1 is effectively no snapping
        hints = {"width_inc": 1, "height_inc": 1}
        assert snap_to_increment(803, 605, hints) == (803, 605)

    def test_snap_width_only(self):
        # xterm-like: 10px wide cells, 4px base (scrollbar/border)
        hints = {"width_inc": 10, "height_inc": 1, "base_width": 4, "base_height": 0}
        # 803 -> 4 + 79*10 = 794 (floor to grid)
        assert snap_to_increment(803, 605, hints) == (794, 605)

    def test_snap_height_only(self):
        hints = {"width_inc": 1, "height_inc": 20, "base_width": 0, "base_height": 2}
        # 605 -> 2 + 30*20 = 602
        assert snap_to_increment(400, 605, hints) == (400, 602)

    def test_snap_both(self):
        # typical terminal: 10x20 cells, 4x2 base
        hints = {"width_inc": 10, "height_inc": 20, "base_width": 4, "base_height": 2}
        assert snap_to_increment(803, 605, hints) == (794, 602)

    def test_exact_grid_unchanged(self):
        hints = {"width_inc": 10, "height_inc": 20, "base_width": 4, "base_height": 2}
        # 4 + 80*10 = 804, 2 + 30*20 = 602 — already on grid
        assert snap_to_increment(804, 602, hints) == (804, 602)

    def test_base_size_defaults_to_zero(self):
        hints = {"width_inc": 10, "height_inc": 20}
        # no base_width/base_height → defaults to 0
        # 803 -> 0 + 80*10 = 800
        # 605 -> 0 + 30*20 = 600
        assert snap_to_increment(803, 605, hints) == (800, 600)

    def test_size_smaller_than_base(self):
        hints = {"width_inc": 10, "height_inc": 20, "base_width": 50, "base_height": 40}
        # size below base: unchanged (remainder is 0)
        assert snap_to_increment(30, 20, hints) == (30, 20)

    def test_one_increment_above_base(self):
        hints = {"width_inc": 10, "height_inc": 20, "base_width": 4, "base_height": 2}
        # 14 = 4 + 1*10, 22 = 2 + 1*20
        assert snap_to_increment(14, 22, hints) == (14, 22)
        # just shy of next increment
        assert snap_to_increment(23, 41, hints) == (14, 22)


if __name__ == "__main__":
    unittest.main()
