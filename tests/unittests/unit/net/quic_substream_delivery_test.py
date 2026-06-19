#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# ABOUTME: Tests for raw QUIC substream data delivery — verifies that an empty
# ABOUTME: payload (FIN-only frame) is dropped rather than tripping the parser's close sentinel.

import unittest

try:
    import aioquic
    HAVE_AIOQUIC = bool(aioquic)
except ImportError:
    HAVE_AIOQUIC = False


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic not available")
class TestPutRawSubstreamData(unittest.TestCase):
    """put_raw_substream_data must not forward empty payloads.

    The packet parser treats an empty buffer as its close sentinel, so an
    empty StreamDataReceived (e.g. a FIN-only frame on a half-closed
    substream) must be dropped rather than delivered to the parser callback.
    """

    def _make_conn(self):
        from xpra.net.quic.connection import XpraQuicConnection
        conn = object.__new__(XpraQuicConnection)
        received: list[tuple[bytes, int]] = []
        conn._raw_read_cb = lambda data, sid: received.append((data, sid))
        return conn, received

    def test_empty_data_not_delivered(self):
        conn, received = self._make_conn()
        conn.put_raw_substream_data(b"", 5)
        self.assertEqual(received, [],
                         "empty substream payload must not be delivered (would trip the close sentinel)")

    def test_nonempty_data_delivered(self):
        conn, received = self._make_conn()
        conn.put_raw_substream_data(b"hello", 5)
        self.assertEqual(received, [(b"hello", 5)])


if __name__ == "__main__":
    unittest.main()
