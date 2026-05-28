#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Regression tests for R2a — setup_cost_mult >= 1 regardless of
# ABOUTME: detection state — by calling get_pipeline_score directly.

import unittest

from xpra.util.objects import AdHocStruct
from xpra.server.window.video_scoring import get_pipeline_score, MIN_FPS_COST


def _make_encoder_spec(codec_type: str = "test codec", setup_cost: int = 50) -> AdHocStruct:
    """Minimal VideoSpec-shaped struct suitable for get_pipeline_score.

    Field set mirrors tests/unittests/unit/server/video_scoring_test.py:52-67.
    """
    spec = AdHocStruct()
    spec.width_mask = 0xfffe
    spec.height_mask = 0xfffe
    spec.quality = 100
    spec.speed = 100
    spec.size_efficiency = 50
    spec.min_w = 32
    spec.min_h = 32
    spec.max_w = 3840
    spec.max_h = 2160
    spec.setup_cost = setup_cost
    spec.score_boost = 0
    spec.gpu_cost = 0
    spec.cpu_cost = 10
    spec.codec_type = codec_type
    spec.has_lossless_mode = False
    spec.can_scale = True
    spec.get_runtime_factor = lambda: 1
    return spec


def _make_current_ve(codec_type: str, src_format: str = "BGRA",
                     width: int = 1920, height: int = 1080) -> AdHocStruct:
    """Minimal "current video encoder" shim used by get_pipeline_score."""
    ve = AdHocStruct()
    ve.get_type = lambda: codec_type
    ve.get_src_format = lambda: src_format
    ve.get_width = lambda: width
    ve.get_height = lambda: height
    return ve


def _score(spec, current_ve, detection: bool, ffps: int) -> int:
    result = get_pipeline_score(
        "BGRA", None, spec,
        1920, 1080, (1, 1),
        100, 10,
        100, 10,
        None, current_ve,
        0, ffps, detection,
    )
    assert result is not None, "get_pipeline_score returned None for valid inputs"
    return result[0]


class EdgeResistanceTest(unittest.TestCase):
    """R2a: edge resistance (setup_cost penalty) must apply even when
    video-subregion detection is off. Regression-tested by observing
    get_pipeline_score outputs rather than re-implementing the formula.
    """

    def test_edge_resistance_independent_of_detection_when_fps_high(self):
        """With ffps >= MIN_FPS_COST the detection flag must not change
        the score: the base setup_cost penalty (mult=1) applies either way."""
        spec = _make_encoder_spec()
        high_fps = MIN_FPS_COST + 10
        s_off = _score(spec, None, detection=False, ffps=high_fps)
        s_on = _score(spec, None, detection=True, ffps=high_fps)
        self.assertEqual(
            s_off, s_on,
            "with ffps>=MIN_FPS_COST, detection flag should not change score "
            "(would differ if setup_cost_mult fell back to int(detection))",
        )

    def test_fps_bump_still_amplifies_with_detection_true(self):
        """At detection=True, low ffps must amplify the setup_cost penalty,
        yielding a strictly lower score than high ffps."""
        spec = _make_encoder_spec()
        high_fps = MIN_FPS_COST + 10
        low_fps = 0
        s_high = _score(spec, None, detection=True, ffps=high_fps)
        s_low = _score(spec, None, detection=True, ffps=low_fps)
        self.assertLess(
            s_low, s_high,
            "low ffps with detection=True must amplify setup_cost penalty",
        )

    def test_edge_resistance_now_applies_without_detection(self):
        """With detection=False, switching to a different encoder type must
        cost more than staying on the same one. Before R2a, mult=0 made the
        two scores identical (no switching penalty)."""
        spec = _make_encoder_spec(codec_type="h264")
        high_fps = MIN_FPS_COST + 10
        same_ve = _make_current_ve("h264")
        other_ve = _make_current_ve("vp9")
        s_same = _score(spec, same_ve, detection=False, ffps=high_fps)
        s_switch = _score(spec, other_ve, detection=False, ffps=high_fps)
        self.assertLess(
            s_switch, s_same,
            "switching encoder type must incur setup_cost penalty even when "
            "detection=False (R2a regression check)",
        )


if __name__ == "__main__":
    unittest.main()
