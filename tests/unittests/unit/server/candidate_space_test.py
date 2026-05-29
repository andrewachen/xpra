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
        wvs._last_video_pixel_format = ""
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

    def test_deadband_engages_when_currently_yuv444(self):
        """R1 Y2 deadband: if the last installed encoder was in YUV444P, a
        small quality dip below the raw threshold must stay in YUV444P
        (the fingerprint must match the high-quality fingerprint, not the
        low-quality NV12 one). The helper reads the cached pixel format
        from _last_video_pixel_format, populated by setup_pipeline_option
        whenever a new encoder is installed; the cache survives encoder
        teardown so the deadband still engages on rebuild paths."""
        # quality 90 above threshold (85): both should produce YUV444 for nvenc.
        wvs = self._make_wvs(quality=90)
        fp_yuv444 = wvs._compute_candidate_pixel_format_fingerprint()
        # quality 70 well below threshold, no prior YUV444 encoder: NV12 baseline.
        wvs_nv12 = self._make_wvs(quality=70)
        fp_nv12 = wvs_nv12._compute_candidate_pixel_format_fingerprint()
        self.assertNotEqual(fp_yuv444, fp_nv12,
                            "high vs low quality without deadband must differ")
        # quality 82, but the cache records the prior encoder was in YUV444P.
        # Deadband (threshold 85 - deadband 5 = 80) means we stay in YUV444 ⇒
        # fingerprint matches the high-quality YUV444 case.
        wvs_dead = self._make_wvs(quality=82)
        wvs_dead._last_video_pixel_format = "YUV444P"
        fp_dead = wvs_dead._compute_candidate_pixel_format_fingerprint()
        self.assertEqual(fp_dead, fp_yuv444,
                         "deadband must keep YUV444 active when prior encoder was YUV444P")

    def test_deadband_cache_is_source_of_truth(self):
        """The helper reads _last_video_pixel_format directly; it does NOT
        probe self._video_encoder. This guarantees the deadband engages on
        rebuild paths where the live encoder is already None (cleanup ran)
        or has been .clean()'ed (its internal pixel_format reset to "")."""
        # Cache says YUV444P, but _video_encoder is None (post-cleanup state).
        wvs = self._make_wvs(quality=82)
        wvs._video_encoder = None
        wvs._last_video_pixel_format = "YUV444P"
        # Pre-compute the YUV444 fingerprint via the above-threshold path.
        wvs_hi = self._make_wvs(quality=90)
        fp_hi = wvs_hi._compute_candidate_pixel_format_fingerprint()
        fp_dead = wvs._compute_candidate_pixel_format_fingerprint()
        self.assertEqual(fp_dead, fp_hi,
                         "cache must engage deadband even when _video_encoder is None")

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

    def test_candidate_space_changes_crossing_lossless_threshold(self):
        """Crossing the lossless threshold (default 100) must invalidate the
        candidate space so R1 schedules teardown. nvEncReconfigureEncoder
        cannot change lossless mode on a live encoder; only a full teardown
        and re-init can switch between lossy and lossless presets."""
        wvs = self._make_wvs(quality=95)
        space_lossy = wvs._compute_candidate_space()
        wvs._current_quality = 100
        space_lossless = wvs._compute_candidate_space()
        self.assertNotEqual(space_lossy, space_lossless,
                            "crossing LOSSLESS_THRESHOLD must change candidate space")

    def test_candidate_space_stable_within_lossless_mode(self):
        """Quality nudges while already in lossless mode (quality >= 100) must
        not change the candidate space — there is nothing further to reconfigure."""
        wvs = self._make_wvs(quality=100)
        space1 = wvs._compute_candidate_space()
        wvs._current_quality = 100  # unchanged, sanity
        space2 = wvs._compute_candidate_space()
        self.assertEqual(space1, space2,
                         "quality unchanged at lossless must leave candidate space stable")

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
        wvs._last_video_pixel_format = ""
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


class SafetyValveTest(unittest.TestCase):

    def _make_wvs(self, with_encoders=False):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        if with_encoders:
            wvs._csc_encoder = MagicMock()
            wvs._video_encoder = MagicMock()
        else:
            wvs._csc_encoder = None
            wvs._video_encoder = None
        wvs._last_video_pixel_format = ""
        wvs.wid = 1
        wvs._consecutive_encode_failures = 0
        wvs._last_candidate_space = ("placeholder",)
        wvs.call_in_encode_thread = MagicMock()
        return wvs

    def test_success_resets_counter(self):
        wvs = self._make_wvs()
        wvs._consecutive_encode_failures = 3
        wvs._r1_note_encode_outcome(True)
        self.assertEqual(wvs._consecutive_encode_failures, 0)

    def test_failure_below_threshold_does_not_invalidate(self):
        wvs = self._make_wvs()
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER - 1):
            wvs._r1_note_encode_outcome(False)
        self.assertEqual(wvs._consecutive_encode_failures,
                         wvs.R1_FORCE_RESELECT_AFTER - 1)
        self.assertEqual(wvs._last_candidate_space, ("placeholder",),
                         "candidate space cache must persist")

    def test_failure_at_threshold_invalidates(self):
        wvs = self._make_wvs(with_encoders=True)
        csce = wvs._csc_encoder
        ve = wvs._video_encoder
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER):
            wvs._r1_note_encode_outcome(False)
        self.assertEqual(wvs._consecutive_encode_failures, 0)
        self.assertIsNone(wvs._last_candidate_space,
                          "safety valve must invalidate cache")
        # cleanup must happen synchronously on the calling (encode) thread
        # so no queued damage work can open a replacement before the
        # failed encoder is torn down.
        self.assertIsNone(wvs._video_encoder,
                          "safety valve must null _video_encoder")
        self.assertIsNone(wvs._csc_encoder,
                          "safety valve must null _csc_encoder")
        csce.clean.assert_called_once()
        ve.clean.assert_called_once()
        # reinit_count must be bumped (semantic equivalent to video_context_clean)
        self.assertEqual(wvs.reinit_count, 1,
                         "reinit_count must bump on safety-valve cleanup")
        # cleanup is synchronous, not deferred via call_in_encode_thread
        wvs.call_in_encode_thread.assert_not_called()

    def test_failure_at_threshold_with_no_encoders(self):
        # If both encoders are already None, the valve still resets state
        # but does not bump reinit_count.
        wvs = self._make_wvs(with_encoders=False)
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER):
            wvs._r1_note_encode_outcome(False)
        self.assertEqual(wvs._consecutive_encode_failures, 0)
        self.assertIsNone(wvs._last_candidate_space)
        self.assertEqual(wvs.reinit_count, 0)

    def test_cleanup_exception_does_not_propagate(self):
        # If .clean() raises, the safety valve must still complete:
        # we are explicitly cleaning a possibly-bad encoder.
        wvs = self._make_wvs(with_encoders=True)
        wvs._video_encoder.clean.side_effect = RuntimeError("boom")
        wvs._csc_encoder.clean.side_effect = RuntimeError("kaboom")
        # Should not raise.
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER):
            wvs._r1_note_encode_outcome(False)
        self.assertIsNone(wvs._video_encoder)
        self.assertIsNone(wvs._csc_encoder)
        self.assertIsNone(wvs._last_candidate_space)


if __name__ == "__main__":
    unittest.main()
