#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests for the exponential-decay jitter histogram and sink-side
# ABOUTME: jitter measurement used by the adaptive audio buffer.

import unittest
from collections import deque
from time import monotonic

from xpra.audio.jitter import (
    DelayHistogram,
    JITTER_MIN_SAMPLES, JITTER_PERCENTILE, MAX_JITTER_BUFFER,
    JITTER_FORGET_FACTOR, JITTER_BUCKET_MS, JITTER_NUM_BUCKETS,
    PEAK_HOLD_SECONDS,
)


class TestDelayHistogram(unittest.TestCase):

    def test_empty_histogram(self):
        h = DelayHistogram()
        assert h.count == 0
        assert h.percentile(0.5) == 0.0
        assert h.percentile(0.97) == 0.0

    def test_single_sample(self):
        h = DelayHistogram()
        h.add(10.0)
        assert h.count == 1
        # 10ms → bucket 2 (index 2, range 10–15ms), centroid = 12.5ms
        p50 = h.percentile(0.5)
        assert p50 == 12.5, f"expected 12.5, got {p50}"

    def test_uniform_low_jitter(self):
        h = DelayHistogram()
        # add 100 samples all at ~5ms jitter:
        for _ in range(100):
            h.add(5.0)
        assert h.count == 100
        p97 = h.percentile(0.97)
        # all samples in bucket 1 (5–10ms), centroid = 7.5:
        assert p97 == 7.5, f"expected 7.5, got {p97}"

    def test_high_jitter_spike(self):
        h = DelayHistogram()
        # 80 low-jitter samples, then 20 high-jitter:
        for _ in range(80):
            h.add(5.0)
        for _ in range(20):
            h.add(100.0)
        p97 = h.percentile(0.97)
        # with 20% high jitter, p97 should be in the high bucket:
        assert p97 > 50, f"expected >50, got {p97}"

    def test_exponential_decay(self):
        h = DelayHistogram()
        # add old high-jitter samples:
        for _ in range(50):
            h.add(200.0)
        old_p97 = h.percentile(0.97)
        # now add many low-jitter samples to decay the old ones:
        for _ in range(200):
            h.add(5.0)
        new_p97 = h.percentile(0.97)
        assert new_p97 < old_p97, f"decay failed: old={old_p97}, new={new_p97}"
        # should be back near the low bucket:
        assert new_p97 < 50, f"expected <50 after decay, got {new_p97}"

    def test_clear(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(10.0)
        h.clear()
        assert h.count == 0
        assert h.percentile(0.5) == 0.0

    def test_max_bucket_clamp(self):
        h = DelayHistogram()
        h.add(9999.0)  # far beyond MAX_JITTER_BUFFER
        p50 = h.percentile(0.5)
        # should land in the last bucket:
        expected = (JITTER_NUM_BUCKETS - 1 + 0.5) * JITTER_BUCKET_MS
        assert p50 == expected, f"expected {expected}, got {p50}"


class TestSinkJitterMeasurement(unittest.TestCase):
    """Test jitter measurement logic equivalent to AudioSink._measure_jitter."""

    def _make_jitter_state(self):
        """Create the jitter state fields that AudioSink.__init__ sets."""
        class State:
            pass
        s = State()
        s._delay_histogram = DelayHistogram()
        s._delay_peaks = deque(maxlen=8)
        s._cached_p97 = 0.0
        s._last_audio_arrival = 0.0
        s._last_audio_server_time = 0
        return s

    def _measure_jitter(self, state, metadata, now):
        """Reimplementation of AudioSink._measure_jitter for testing without GStreamer."""
        pts_ns = metadata.get("timestamp", -1)
        if pts_ns > 0:
            server_time_ms = pts_ns // 1_000_000
        else:
            server_time_ms = metadata.get("time", 0)
        if not server_time_ms:
            return
        if state._last_audio_arrival > 0 and state._last_audio_server_time > 0:
            arrival_diff = (now - state._last_audio_arrival) * 1000
            send_diff = server_time_ms - state._last_audio_server_time
            D = max(0.0, arrival_diff - send_diff)
            if arrival_diff < 2000 and send_diff < 2000:
                state._delay_histogram.add(D)
                if D > 2 * max(state._cached_p97, 10):
                    state._delay_peaks.append((now, D))
        state._last_audio_arrival = now
        state._last_audio_server_time = server_time_ms

    def test_no_timestamp(self):
        s = self._make_jitter_state()
        self._measure_jitter(s, {}, monotonic())
        assert s._delay_histogram.count == 0

    def test_first_packet_no_jitter(self):
        s = self._make_jitter_state()
        self._measure_jitter(s, {"timestamp": 1_000_000_000}, monotonic())
        # first packet establishes baseline, no jitter sample:
        assert s._delay_histogram.count == 0
        assert s._last_audio_server_time == 1000

    def test_steady_stream(self):
        """Simulate 20ms Opus frames arriving at steady 20ms intervals."""
        s = self._make_jitter_state()
        base_time = monotonic()
        for i in range(50):
            now = base_time + i * 0.020  # 20ms intervals
            pts_ns = (1000 + i * 20) * 1_000_000  # 20ms PTS increments
            self._measure_jitter(s, {"timestamp": pts_ns}, now)
        # 49 jitter samples (first packet is baseline):
        assert s._delay_histogram.count == 49
        # jitter should be near zero for steady stream:
        p97 = s._delay_histogram.percentile(0.97)
        assert p97 < 10, f"expected near-zero jitter, got {p97}"

    def test_network_jitter(self):
        """Simulate varying arrival times (network jitter)."""
        s = self._make_jitter_state()
        base_time = monotonic()
        # packets arrive with 5–35ms intervals instead of steady 20ms:
        arrival_offsets = [0]
        for i in range(1, 60):
            jitter = 5 + (i * 7 % 30)  # 5–35ms arrivals
            arrival_offsets.append(arrival_offsets[-1] + jitter)
        for i in range(60):
            now = base_time + arrival_offsets[i] / 1000.0
            pts_ns = (1000 + i * 20) * 1_000_000  # steady 20ms PTS
            self._measure_jitter(s, {"timestamp": pts_ns}, now)
        p97 = s._delay_histogram.percentile(0.97)
        # jitter D = arrival_diff - send_diff; with varying arrivals, D should be >0:
        assert p97 > 5, f"expected measurable jitter, got {p97}"

    def test_fallback_to_time_key(self):
        """Falls back to 'time' (monotonic ms) when no PTS."""
        s = self._make_jitter_state()
        base_time = monotonic()
        self._measure_jitter(s, {"time": 5000}, base_time)
        self._measure_jitter(s, {"time": 5020}, base_time + 0.020)
        assert s._delay_histogram.count == 1

    def test_gap_filter(self):
        """Gaps > 2s are filtered out (outage recovery)."""
        s = self._make_jitter_state()
        base_time = monotonic()
        self._measure_jitter(s, {"timestamp": 1_000_000_000}, base_time)
        # 3 second gap:
        self._measure_jitter(s, {"timestamp": 4_000_000_000}, base_time + 3.0)
        assert s._delay_histogram.count == 0

    def test_peak_detection(self):
        """Large spikes should be recorded in _delay_peaks."""
        s = self._make_jitter_state()
        s._cached_p97 = 10.0  # pretend p97 is 10ms
        base_time = monotonic()
        self._measure_jitter(s, {"timestamp": 1_000_000_000}, base_time)
        # next packet arrives 50ms late (D = 30ms, > 2×10 = 20ms threshold):
        self._measure_jitter(s, {"timestamp": 1_020_000_000}, base_time + 0.050)
        assert len(s._delay_peaks) == 1
        _, peak_amp = s._delay_peaks[0]
        assert abs(peak_amp - 30.0) < 0.1, f"expected ~30.0, got {peak_amp}"


class TestComputeJitterTarget(unittest.TestCase):
    """Test the jitter target computation logic."""

    def _compute_jitter_target(self, histogram, cached_p97, delay_peaks):
        """Reimplementation of AudioSink._compute_jitter_target for testing."""
        if histogram.count < JITTER_MIN_SAMPLES:
            return 0
        p97 = histogram.percentile(JITTER_PERCENTILE)
        jitter_ms = min(p97, MAX_JITTER_BUFFER)
        now = monotonic()
        active_peaks = [amp for t, amp in delay_peaks if now - t < PEAK_HOLD_SECONDS]
        if active_peaks:
            jitter_ms = max(jitter_ms, min(max(active_peaks), MAX_JITTER_BUFFER))
        return max(20, int(jitter_ms)) if jitter_ms > 0 else 0

    def test_insufficient_samples(self):
        h = DelayHistogram()
        for _ in range(JITTER_MIN_SAMPLES - 1):
            h.add(50.0)
        target = self._compute_jitter_target(h, 0, deque())
        assert target == 0

    def test_low_jitter_target(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(5.0)
        target = self._compute_jitter_target(h, 0, deque())
        # p97 of 5ms samples → ~7.5ms, but minimum target is 20:
        assert target == 20, f"expected 20 (floor), got {target}"

    def test_moderate_jitter_target(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(80.0)
        target = self._compute_jitter_target(h, 0, deque())
        assert 75 <= target <= 85, f"expected ~80, got {target}"

    def test_peak_raises_target(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(30.0)
        # add a recent peak:
        peaks = deque(maxlen=8)
        peaks.append((monotonic(), 200.0))
        target = self._compute_jitter_target(h, 0, peaks)
        assert target >= 200, f"expected >=200 from peak, got {target}"

    def test_expired_peak_ignored(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(30.0)
        # add an expired peak:
        peaks = deque(maxlen=8)
        peaks.append((monotonic() - PEAK_HOLD_SECONDS - 1, 200.0))
        target = self._compute_jitter_target(h, 0, peaks)
        # should use histogram only, not the expired peak:
        assert target < 100, f"expected <100 without peak, got {target}"

    def test_cap_at_max(self):
        h = DelayHistogram()
        for _ in range(50):
            h.add(999.0)
        target = self._compute_jitter_target(h, 0, deque())
        assert target <= MAX_JITTER_BUFFER, f"expected <={MAX_JITTER_BUFFER}, got {target}"


def main():
    unittest.main()


if __name__ == '__main__':
    main()
