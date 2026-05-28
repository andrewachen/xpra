#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests R1's candidate-space tuple helpers detect transitions correctly.
# ABOUTME: Covers per-candidate pixel-format fingerprint + desired-scaling staleness.

import unittest
from unittest.mock import MagicMock, patch


class CandidateSpaceTest(unittest.TestCase):

    def _make_wvs(self, quality=50, encoding="auto",
                  common_video_encodings=("h264", "h265", "av1"),
                  pixel_format="BGRX",
                  window_dimensions=(1920, 1080)):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs._current_quality = quality
        wvs.encoding = encoding
        wvs.common_video_encodings = common_video_encodings
        wvs.pixel_format = pixel_format
        wvs.window_dimensions = window_dimensions
        wvs.width_mask = 0xFFFE
        wvs.height_mask = 0xFFFE
        wvs.max_w = 4096
        wvs.max_h = 4096
        wvs.content_type = "browser"
        wvs.full_csc_modes = {}
        wvs.video_subregion = MagicMock(rectangle=None)
        wvs._video_encoder = None
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 1)
        return wvs

    def test_fingerprint_stable_for_low_quality(self):
        wvs = self._make_wvs(quality=50)
        fp1 = wvs._compute_candidate_pixel_format_fingerprint()
        wvs._current_quality = 60
        fp2 = wvs._compute_candidate_pixel_format_fingerprint()
        self.assertEqual(fp1, fp2,
                         "quality changes within the same band must not change fingerprint")

    def test_fingerprint_changes_crossing_yuv444_threshold(self):
        wvs = self._make_wvs(quality=80)
        fp_low = wvs._compute_candidate_pixel_format_fingerprint()
        wvs._current_quality = 90
        fp_high = wvs._compute_candidate_pixel_format_fingerprint()
        self.assertNotEqual(fp_low, fp_high,
                            "crossing YUV444_THRESHOLD must change fingerprint")

    def test_candidate_space_changes_with_content_type(self):
        wvs = self._make_wvs()
        space1 = wvs._compute_candidate_space()
        wvs.content_type = "video"
        space2 = wvs._compute_candidate_space()
        self.assertNotEqual(space1, space2)

    def test_candidate_space_changes_with_dims(self):
        wvs = self._make_wvs(window_dimensions=(1920, 1080))
        space1 = wvs._compute_candidate_space()
        wvs.window_dimensions = (1280, 720)
        space2 = wvs._compute_candidate_space()
        self.assertNotEqual(space1, space2)

    def test_candidate_space_stable_with_quality_nudge(self):
        wvs = self._make_wvs(quality=50)
        space1 = wvs._compute_candidate_space()
        wvs._current_quality = 55
        space2 = wvs._compute_candidate_space()
        self.assertEqual(space1, space2,
                         "small quality nudges within band must not change candidate space")

    def test_desired_scaling_is_fresh(self):
        wvs = self._make_wvs()
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 2)
        scaling = wvs._compute_desired_scaling()
        self.assertEqual(scaling, (1, 2),
                         "desired scaling must use the fresh calculate_scaling result")


class UpdateEncodingOptionsGateTest(unittest.TestCase):

    def _make_wvs(self):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        wvs._csc_encoder = None
        wvs._video_encoder = None
        wvs.wid = 1
        wvs._current_quality = 50
        wvs._current_speed = 50
        wvs.encoding = "auto"
        wvs.common_video_encodings = ("h264", "h265")
        wvs.pixel_format = "BGRX"
        wvs.window_dimensions = (1920, 1080)
        wvs.width_mask = 0xFFFE
        wvs.height_mask = 0xFFFE
        wvs.max_w = 4096
        wvs.max_h = 4096
        wvs.content_type = "browser"
        wvs.full_csc_modes = {}
        wvs.video_subregion = MagicMock(rectangle=None)
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 1)
        wvs.update_encoding_video_subregion = MagicMock()
        wvs.update_pipeline_scores = MagicMock(
            side_effect=lambda fr: setattr(wvs, "last_pipeline_scores", (object(),))
        )
        wvs.verify_csc_and_encoder = MagicMock(return_value=True)
        wvs._last_candidate_space = None
        wvs.last_pipeline_scores = ()
        wvs._last_pipeline_check = 0
        wvs.call_in_encode_thread = MagicMock()
        wvs.cancel_video_encoder_flush = MagicMock()
        return wvs

    def _call_update(self, wvs, force_reload=False):
        """Call update_encoding_options bypassing super()."""
        from xpra.server.window.video_compress import WindowVideoSource
        parent = WindowVideoSource.__mro__[1]
        with patch.object(parent, "update_encoding_options", lambda self, fr: None):
            WindowVideoSource.update_encoding_options(wvs, force_reload)

    def test_first_call_runs_scoring(self):
        wvs = self._make_wvs()
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.assert_called_once_with(False)

    def test_unchanged_space_skips_scoring(self):
        wvs = self._make_wvs()
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.reset_mock()
        # call again, nothing changed
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.assert_not_called()

    def test_quality_nudge_in_band_skips_scoring(self):
        wvs = self._make_wvs()
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.reset_mock()
        wvs._current_quality = 55  # small nudge, no band change
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.assert_not_called()

    def test_content_type_change_triggers_scoring(self):
        wvs = self._make_wvs()
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.reset_mock()
        wvs.content_type = "video"
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.assert_called_once_with(False)

    def test_force_reload_always_runs_scoring(self):
        wvs = self._make_wvs()
        # Prime the cache with a normal call so _last_candidate_space is set.
        self._call_update(wvs, force_reload=False)
        wvs.update_pipeline_scores.reset_mock()
        wvs.cancel_video_encoder_flush.reset_mock()
        # force_reload must bypass the gate and re-score even though space is unchanged.
        self._call_update(wvs, force_reload=True)
        wvs.update_pipeline_scores.assert_called_once_with(True)
        # force_reload also tears down the codecs via cleanup_codecs().
        wvs.cancel_video_encoder_flush.assert_called()


if __name__ == "__main__":
    unittest.main()
