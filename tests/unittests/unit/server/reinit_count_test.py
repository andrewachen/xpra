#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests that WindowVideoSource.video_context_clean() bumps reinit_count
# ABOUTME: only when there is real teardown work (csce or ve is not None).

import unittest
from unittest.mock import MagicMock


class ReinitCountTest(unittest.TestCase):

    def _make_wvs(self):
        """Construct a minimal WindowVideoSource for counter-only testing."""
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        wvs._csc_encoder = None
        wvs._video_encoder = None
        wvs.wid = 1
        wvs.call_in_encode_thread = MagicMock()
        return wvs

    def test_noop_clean_does_not_bump(self):
        wvs = self._make_wvs()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 0,
                         "no-op cleanup (both encoders None) must not bump reinit_count")

    def test_clean_with_csce_bumps(self):
        wvs = self._make_wvs()
        wvs._csc_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1)
        wvs.call_in_encode_thread.assert_called_once()

    def test_clean_with_ve_bumps(self):
        wvs = self._make_wvs()
        wvs._video_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1)
        wvs.call_in_encode_thread.assert_called_once()

    def test_clean_with_both_bumps_once(self):
        wvs = self._make_wvs()
        wvs._csc_encoder = MagicMock()
        wvs._video_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1, "single cleanup is one event, not two")

    def test_repeated_noop_calls_do_not_bump(self):
        wvs = self._make_wvs()
        for _ in range(5):
            wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 0)


if __name__ == "__main__":
    unittest.main()
