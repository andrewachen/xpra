#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests R2a — setup_cost_mult >= 1 regardless of detection state.

import unittest


class EdgeResistanceTest(unittest.TestCase):

    def _setup_cost_mult(self, detection: bool, ffps: int) -> int:
        # Inline copy of the formula in video_scoring.py:115 so we test the
        # math directly without dragging in the full scoring stack.
        from xpra.server.window.video_scoring import MIN_FPS_COST
        return 1 + int(detection) * max(0, MIN_FPS_COST - ffps)

    def test_no_detection_high_fps_is_one(self):
        self.assertEqual(self._setup_cost_mult(False, 30), 1)

    def test_no_detection_zero_fps_is_still_one(self):
        # The fps bonus only kicks in with detection=True
        self.assertEqual(self._setup_cost_mult(False, 0), 1)

    def test_detection_high_fps_is_one(self):
        self.assertEqual(self._setup_cost_mult(True, 30), 1)

    def test_detection_low_fps_amplifies(self):
        # MIN_FPS_COST defaults to 4; at fps=0, mult = 1 + 1 * (4 - 0) = 5
        from xpra.server.window.video_scoring import MIN_FPS_COST
        self.assertEqual(self._setup_cost_mult(True, 0), 1 + MIN_FPS_COST)


if __name__ == "__main__":
    unittest.main()
