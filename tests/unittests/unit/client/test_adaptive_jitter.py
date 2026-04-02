#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests for the adaptive jitter buffer algorithm.
# ABOUTME: Validates sliding window p95, peak detection, and target calculation.

import unittest
from collections import deque
from time import monotonic


# constants mirrored from client/subsystem/audio.py:
AUDIO_PIPELINE_LATENCY_MS = 25
JITTER_MIN_SAMPLES = 16
MAX_JITTER_BUFFER = 500
JITTER_PERCENTILE = 0.95
PEAK_HOLD_SECONDS = 5


def record_peak_if_needed(D, cached_p95, delay_peaks, now):
    """Record a peak if D exceeds 2× the cached p95 (called per packet)."""
    if D > 2 * max(cached_p95, 10):
        delay_peaks.append((now, D))


def compute_target(delay_window, delay_peaks, mean_ms, stddev_ms, now=None):
    """
    Standalone version of the target calculation from _update_av_sync_target.
    Returns (target, jitter_component, av_component) or None if insufficient data.
    Peaks should already be recorded via record_peak_if_needed before calling this.
    """
    if now is None:
        now = monotonic()
    # video decode component:
    if mean_ms > 0:
        av_component = max(mean_ms - AUDIO_PIPELINE_LATENCY_MS, stddev_ms + 10, 0)
    else:
        av_component = 0
    # network jitter component:
    if len(delay_window) >= JITTER_MIN_SAMPLES:
        p95 = sorted(delay_window)[int(JITTER_PERCENTILE * len(delay_window))]
        jitter_component = min(p95, MAX_JITTER_BUFFER)
        # if a recent peak exceeds p95, hold the buffer at peak level:
        active_peaks = [amp for t, amp in delay_peaks if now - t < PEAK_HOLD_SECONDS]
        if active_peaks:
            jitter_component = max(jitter_component, min(max(active_peaks), MAX_JITTER_BUFFER))
    else:
        jitter_component = 0
    if av_component == 0 and jitter_component == 0:
        return None
    target = max(av_component, jitter_component)
    target = max(20, min(500, int(target)))
    return target, jitter_component, av_component


def measure_jitter(arrivals, send_times):
    """
    Simulate jitter measurement from a sequence of (client_arrival, server_time) pairs.
    Returns the delay_window deque.  Applies the same gap/burst filter as the real code.
    """
    window = deque(maxlen=100)
    last_arrival = 0.0
    last_server_time = 0
    for arrival, server_time in zip(arrivals, send_times):
        if last_arrival > 0 and last_server_time > 0:
            arrival_diff = (arrival - last_arrival) * 1000
            send_diff = server_time - last_server_time
            D = max(0.0, arrival_diff - send_diff)
            if 5 < arrival_diff < 2000:
                window.append(D)
        last_arrival = arrival
        last_server_time = server_time
    return window


class TestJitterMeasurement(unittest.TestCase):

    def test_perfect_network_zero_jitter(self):
        """Packets arriving exactly on schedule produce zero delay variation."""
        # start times at 1000 (not 0) since 0 is the "not set" sentinel:
        arrivals = [1.0 + i * 0.020 for i in range(50)]
        send_times = [1000 + i * 20 for i in range(50)]
        window = measure_jitter(arrivals, send_times)
        self.assertEqual(len(window), 49)
        for v in window:
            self.assertAlmostEqual(v, 0.0, places=5)

    def test_constant_delay_no_jitter(self):
        """Constant network delay doesn't produce jitter (offset cancels)."""
        delay_s = 0.050  # 50ms constant delay
        arrivals = [1.0 + i * 0.020 + delay_s for i in range(50)]
        send_times = [1000 + i * 20 for i in range(50)]
        window = measure_jitter(arrivals, send_times)
        for v in window:
            self.assertAlmostEqual(v, 0.0, places=5)

    def test_variable_jitter(self):
        """Variable arrival times produce non-zero delay variation."""
        import random
        random.seed(42)
        send_times = [1000 + i * 20 for i in range(100)]
        arrivals = [1.0 + i * 0.020 + random.uniform(-0.005, 0.005) for i in range(100)]
        window = measure_jitter(arrivals, send_times)
        self.assertEqual(len(window), 99)
        positive = [v for v in window if v > 0]
        self.assertGreater(len(positive), 0)

    def test_late_packet_spike(self):
        """A single late packet shows up as a large D value."""
        send_times = [1000 + i * 20 for i in range(30)]
        arrivals = [1.0 + i * 0.020 for i in range(30)]
        arrivals[15] += 0.100  # packet 15 arrives 100ms late
        window = measure_jitter(arrivals, send_times)
        spike_values = [v for v in window if v > 50]
        self.assertGreater(len(spike_values), 0)
        self.assertAlmostEqual(max(window), 100.0, delta=1.0)

    def test_negative_D_clipped_to_zero(self):
        """Early arrivals (negative D) are clipped to 0."""
        send_times = [1000, 1020, 1040]
        arrivals = [1.0, 1.020, 1.030]  # third packet arrives 10ms early
        window = measure_jitter(arrivals, send_times)
        self.assertEqual(len(window), 2)
        for v in window:
            self.assertGreaterEqual(v, 0.0)

    def test_tunnel_burst_filtered(self):
        """After a large gap, burst packets are filtered out of the window."""
        base = 1000
        send_times = [base + i * 20 for i in range(70)]
        arrivals = [1.0 + i * 0.020 for i in range(20)]
        burst_start = arrivals[-1] + 5.0
        arrivals += [burst_start + i * 0.001 for i in range(50)]
        window = measure_jitter(arrivals, send_times)
        # only the 19 normal measurements should be in the window:
        self.assertEqual(len(window), 19)

    def test_normal_tcp_batching_not_filtered(self):
        """Packets arriving 10-15ms apart (normal variance) are not filtered."""
        send_times = [1000 + i * 20 for i in range(30)]
        arrivals = [1.0 + i * 0.015 for i in range(30)]
        window = measure_jitter(arrivals, send_times)
        self.assertEqual(len(window), 29)


class TestP95Calculation(unittest.TestCase):

    def test_uniform_jitter(self):
        """95th percentile of uniform 0-10ms jitter ≈ 9.5ms."""
        window = deque(range(100), maxlen=100)  # 0, 1, 2, ..., 99 → scaled to 0-10
        window = deque([v / 10.0 for v in range(100)], maxlen=100)
        p95 = sorted(window)[int(0.95 * len(window))]
        self.assertAlmostEqual(p95, 9.5, places=1)

    def test_mostly_zero_with_spikes(self):
        """A few spikes push p95 up only when there are enough of them."""
        window = deque([0.0] * 95 + [100.0] * 5, maxlen=100)
        p95 = sorted(window)[int(0.95 * len(window))]
        self.assertEqual(p95, 100.0)

    def test_all_zero(self):
        """All zeros produce p95 = 0."""
        window = deque([0.0] * 100, maxlen=100)
        p95 = sorted(window)[int(0.95 * len(window))]
        self.assertEqual(p95, 0.0)


class TestTargetCalculation(unittest.TestCase):

    def test_cold_start_returns_none(self):
        """With fewer than JITTER_MIN_SAMPLES, returns None (keep static buffer)."""
        window = deque([1.0] * 10, maxlen=100)
        peaks = deque(maxlen=8)
        result = compute_target(window, peaks, mean_ms=0, stddev_ms=0)
        self.assertIsNone(result)

    def test_lan_av_component_dominates(self):
        """On LAN (low jitter), video decode component dominates."""
        window = deque([1.0] * 50, maxlen=100)  # 1ms jitter
        peaks = deque(maxlen=8)
        target, jitter, av = compute_target(window, peaks, mean_ms=35, stddev_ms=5)
        # av = max(35-25, 5+10, 0) = max(10, 15, 0) = 15
        # jitter = min(1.0, 500) = 1.0
        # target = max(15, 1) = 15 → clamped to 20 (floor)
        self.assertEqual(av, 15)
        self.assertAlmostEqual(jitter, 1.0, places=1)
        self.assertEqual(target, 20)

    def test_cellular_jitter_dominates(self):
        """On cellular (high jitter), jitter component dominates."""
        # simulate 80ms p95 jitter:
        window = deque([10.0] * 45 + [80.0] * 5, maxlen=100)
        # ensure we have enough samples:
        while len(window) < 50:
            window.appendleft(10.0)
        peaks = deque(maxlen=8)
        target, jitter, av = compute_target(window, peaks, mean_ms=35, stddev_ms=5)
        # av = 15, jitter = p95 ≈ 80
        self.assertGreater(jitter, av)
        self.assertEqual(target, int(jitter))

    def test_max_jitter_cap(self):
        """Jitter component is capped at MAX_JITTER_BUFFER (500ms)."""
        window = deque([600.0] * 50, maxlen=100)
        peaks = deque(maxlen=8)
        target, jitter, av = compute_target(window, peaks, mean_ms=0, stddev_ms=0)
        self.assertEqual(jitter, 500)
        self.assertEqual(target, 500)

    def test_floor_at_20ms(self):
        """Target never goes below 20ms."""
        window = deque([0.5] * 50, maxlen=100)
        peaks = deque(maxlen=8)
        target, jitter, av = compute_target(window, peaks, mean_ms=30, stddev_ms=2)
        self.assertGreaterEqual(target, 20)

    def test_no_video_stats_jitter_only(self):
        """Without video decode stats, jitter alone drives the target."""
        window = deque([40.0] * 50, maxlen=100)
        peaks = deque(maxlen=8)
        target, jitter, av = compute_target(window, peaks, mean_ms=0, stddev_ms=0)
        self.assertEqual(av, 0)
        self.assertAlmostEqual(jitter, 40.0, places=1)
        self.assertEqual(target, 40)


class TestPeakDetection(unittest.TestCase):

    def test_spike_recorded_as_peak(self):
        """D > 2×p95 is recorded as a peak."""
        window = deque([5.0] * 49 + [200.0], maxlen=100)
        peaks = deque(maxlen=8)
        now = 1000.0
        # record peak from measurement side (as _process_audio_data would):
        cached_p95 = 5.0  # from previous timer tick
        record_peak_if_needed(200.0, cached_p95, peaks, now)
        self.assertEqual(len(peaks), 1)
        self.assertEqual(peaks[0][1], 200.0)
        # target calculation should use the peak:
        target, jitter, av = compute_target(window, peaks, mean_ms=0, stddev_ms=0, now=now)
        self.assertEqual(jitter, 200.0)

    def test_peak_holds_buffer(self):
        """Peak keeps buffer high even after p95 drops back."""
        peaks = deque(maxlen=8)
        now = 1000.0
        # record a peak:
        record_peak_if_needed(200.0, 5.0, peaks, now)
        # later call with low jitter but within PEAK_HOLD_SECONDS:
        window = deque([5.0] * 50, maxlen=100)
        target, jitter, av = compute_target(window, peaks, mean_ms=0, stddev_ms=0, now=now + 2)
        self.assertEqual(jitter, 200.0)
        self.assertEqual(target, 200)

    def test_peak_expires(self):
        """Peak stops holding after PEAK_HOLD_SECONDS."""
        peaks = deque(maxlen=8)
        now = 1000.0
        record_peak_if_needed(200.0, 5.0, peaks, now)
        # call after peak expires:
        window = deque([5.0] * 50, maxlen=100)
        target, jitter, av = compute_target(window, peaks, mean_ms=0, stddev_ms=0,
                                            now=now + PEAK_HOLD_SECONDS + 1)
        self.assertAlmostEqual(jitter, 5.0, places=1)

    def test_peak_max_8_entries(self):
        """Peak deque doesn't exceed 8 entries."""
        peaks = deque(maxlen=8)
        now = 1000.0
        for i in range(12):
            record_peak_if_needed(200.0 + i, 5.0, peaks, now + i)
        self.assertLessEqual(len(peaks), 8)

    def test_small_D_not_recorded_as_peak(self):
        """D below 2×max(p95, 10) is not recorded as a peak."""
        peaks = deque(maxlen=8)
        # with p95=0, floor is max(0, 10)=10, so D must exceed 20:
        record_peak_if_needed(15.0, 0.0, peaks, 1000.0)
        self.assertEqual(len(peaks), 0)


class TestAsymmetricConvergence(unittest.TestCase):

    def test_grow_jumps_immediately(self):
        """When current_max < target_max, new_max jumps to target."""
        current_max = 80
        target_max = 200
        # the logic from _av_sync_tick:
        if current_max < target_max:
            new_max = target_max
        else:
            new_max = current_max  # would use proportional controller
        self.assertEqual(new_max, 200)

    def test_shrink_uses_proportional(self):
        """When current_max > target_max, proportional controller limits the step."""
        current_max = 200
        target_max = 80
        AV_SYNC_GAIN = 0.3
        AV_SYNC_MAX_STEP_MS = 30
        AV_SYNC_HEADROOM_MS = 30
        if current_max < target_max:
            new_max = target_max
        else:
            max_correction = max(-AV_SYNC_MAX_STEP_MS, min(AV_SYNC_MAX_STEP_MS,
                                                            (current_max - target_max) * AV_SYNC_GAIN))
            new_max = max(AV_SYNC_HEADROOM_MS, int(current_max - max_correction))
        # correction = min(30, (200-80)*0.3) = min(30, 36) = 30
        # new_max = max(30, 200-30) = 170
        self.assertEqual(new_max, 170)
        self.assertGreater(new_max, target_max)  # didn't jump — slow convergence


if __name__ == "__main__":
    unittest.main()
