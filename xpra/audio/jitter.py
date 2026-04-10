# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Exponential-decay jitter histogram for adaptive audio buffering.
# ABOUTME: Used by AudioSink to measure network jitter and size the playback buffer.

from xpra.util.env import envint

# adaptive jitter buffer sizing (NetEQ-inspired exponential-decay histogram):
JITTER_MIN_SAMPLES = envint("XPRA_JITTER_MIN_SAMPLES", 16)
MAX_JITTER_BUFFER = 500        # cap jitter buffer at 500ms
JITTER_PERCENTILE = 0.97       # NetEQ default: 97th percentile
JITTER_FORGET_FACTOR = 0.983   # NetEQ default: each sample decays old data by ~1.7%
JITTER_BUCKET_MS = 5           # histogram resolution
JITTER_NUM_BUCKETS = MAX_JITTER_BUFFER // JITTER_BUCKET_MS
PEAK_HOLD_SECONDS = 5          # how long peak detection stays active after a spike


class DelayHistogram:
    """Exponential-decay histogram for jitter estimation (NetEQ-inspired).

    Each new sample decays all bucket counts by JITTER_FORGET_FACTOR before
    incrementing. This naturally weights recent observations more heavily —
    after ~60 samples, old data has decayed to ~36% weight. No cliff-edge
    when old samples "fall out" of a fixed window.
    """
    def __init__(self):
        self._buckets = [0.0] * JITTER_NUM_BUCKETS
        self._count = 0

    def add(self, delay_ms: float) -> None:
        # decay all buckets:
        for i in range(len(self._buckets)):
            self._buckets[i] *= JITTER_FORGET_FACTOR
        # increment the appropriate bucket:
        idx = min(int(delay_ms / JITTER_BUCKET_MS), len(self._buckets) - 1)
        self._buckets[idx] += 1.0
        self._count += 1

    def percentile(self, pct: float) -> float:
        """Return the delay value at the given percentile (0.0 to 1.0)."""
        total = sum(self._buckets)
        if total == 0:
            return 0.0
        threshold = total * pct
        cumulative = 0.0
        for i, count in enumerate(self._buckets):
            cumulative += count
            if cumulative >= threshold:
                return (i + 0.5) * JITTER_BUCKET_MS
        return MAX_JITTER_BUFFER

    @property
    def count(self) -> int:
        return self._count

    def clear(self) -> None:
        self._buckets = [0.0] * JITTER_NUM_BUCKETS
        self._count = 0
