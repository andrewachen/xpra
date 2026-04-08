#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Measures QUIC head-of-line blocking by connecting to an xpra server
# ABOUTME: and timestamping packet arrivals with and without substreams enabled.

"""
QUIC Head-of-Line Blocking Test

Connects to a live xpra server via QUIC and measures inter-arrival jitter
of transport-level data chunks. Runs two passes:
  1. Single-stream (no quic.substreams capability)
  2. Multi-stream (with quic.substreams capability)

Generate traffic on the xpra display first (e.g., glxgears), then run:

    python3 test_quic_hol.py quic://HOST:PORT/ --password PASS

Options:
    --duration N      seconds per test pass (default: 10)
    --single-only     only run single-stream test
    --multi-only      only run multi-stream test
"""

import os
import sys
import time
import argparse
import statistics
from collections import defaultdict

# Add xpra source tree to path
script_dir = os.path.dirname(os.path.abspath(__file__))
src_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
if os.path.isdir(os.path.join(src_root, "xpra")):
    sys.path.insert(0, src_root)


class PacketTimingCollector:
    """Collects timestamps for each packet category."""

    def __init__(self):
        self.arrivals: dict[str, list[float]] = defaultdict(list)
        self.sizes: dict[str, list[int]] = defaultdict(list)
        self.start_time = 0.0
        self.collecting = False

    def start(self):
        self.arrivals.clear()
        self.sizes.clear()
        self.start_time = time.monotonic()
        self.collecting = True

    def stop(self):
        self.collecting = False

    def record(self, category: str, size: int):
        if self.collecting:
            self.arrivals[category].append(time.monotonic())
            self.sizes[category].append(size)

    def inter_arrival_ms(self, category: str) -> list[float]:
        times = self.arrivals.get(category, [])
        if len(times) < 2:
            return []
        return [(times[i] - times[i - 1]) * 1000 for i in range(1, len(times))]

    def report(self, label: str):
        elapsed = time.monotonic() - self.start_time if self.start_time else 0
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"  Duration: {elapsed:.1f}s")
        print(f"{'=' * 60}")

        for cat in sorted(self.arrivals.keys()):
            count = len(self.arrivals[cat])
            total_bytes = sum(self.sizes[cat])
            ia = self.inter_arrival_ms(cat)
            if not ia:
                print(f"\n  {cat}: {count} chunks ({total_bytes:,} bytes)")
                continue
            sorted_ia = sorted(ia)
            rate = count / elapsed if elapsed > 0 else 0
            print(f"\n  {cat}: {count} chunks ({total_bytes:,} bytes, {rate:.1f}/s)")
            print(f"    inter-arrival (ms): "
                  f"mean={statistics.mean(ia):.1f}  "
                  f"p50={sorted_ia[int(len(ia) * 0.50)]:.1f}  "
                  f"p95={sorted_ia[int(len(ia) * 0.95)]:.1f}  "
                  f"p99={sorted_ia[min(int(len(ia) * 0.99), len(ia) - 1)]:.1f}  "
                  f"max={max(ia):.1f}")
            if len(ia) > 1:
                print(f"    jitter (stdev): {statistics.stdev(ia):.1f} ms")


def run_test(url: str, password: str, duration: float, substreams: bool) -> tuple:
    """Connect to the server, collect transport-level timing.

    Returns (parse_collector, quic_collector) — the parse collector timestamps
    packets after the parse thread processes them, the quic collector timestamps
    raw substream data as it arrives from the QUIC layer (before parse thread).
    """
    from xpra.net.packet_encoding import init_all as init_encoders
    from xpra.net.compression import init_all as init_compressors
    init_encoders()
    init_compressors()

    from gi.repository import GLib
    from xpra.client.base import features
    features.file = features.printer = features.control = features.debug = False

    from xpra.client.base.command import MonitorXpraClient
    from xpra.util.objects import typedict
    from xpra.scripts.config import make_defaults_struct
    from xpra.scripts.main import connect_to_server, do_pick_display
    from xpra.net.common import Packet

    collector = PacketTimingCollector()
    quic_collector = PacketTimingCollector()
    # map QUIC stream_id -> type name (populated when substream headers are parsed)
    stream_type_map: dict[int, str] = {}

    # stream_type_map is populated by the timed_put hook below:
    # first data on a new stream_id gets labeled by packet size heuristic,
    # then refined when the parse thread delivers the actual packet type.

    opts = make_defaults_struct()
    opts.ssl_server_verify_mode = "none"
    if password:
        opts.password = password

    class TimingClient(MonitorXpraClient):
        def __init__(self, opts):
            super().__init__(opts)
            # override MonitorXpraClient defaults — request full UI data
            self.hello_extra["ui_client"] = True
            self.hello_extra["windows"] = True
            self.hello_extra["keyboard"] = False
            self.hello_extra["pointer"] = False
            self.hello_extra["audio"] = {
                "receive": True,
                "decoders": ("opus+ogg", "vorbis+ogg", "flac", "wav"),
                "send": False,
            }
            self.hello_extra["wants"] = self.hello_extra.get("wants", []) + ["audio"]
            self.hello_extra["encodings"] = ("rgb32", "rgb24", "png", "jpeg")
            self.hello_extra["encoding.core"] = ("rgb32", "rgb24", "png", "jpeg")
            self.hello_extra["desktop_size"] = (1920, 1080)
            self.hello_extra["screen_sizes"] = [(1920, 1080, 508, 286)]
            # remove "request"="event" so server treats us as a UI client
            self.hello_extra.pop("request", None)
            # control substream capability
            if substreams:
                self.hello_extra["quic.substreams"] = True
            else:
                # explicitly disable — don't send the key at all
                self.hello_extra.pop("quic.substreams", None)
            print(f"  hello_extra quic.substreams = {self.hello_extra.get('quic.substreams', '<not set>')}")

        @staticmethod
        def handle_invalid_packet(proto, packet):
            # silently accept all packet types
            pass

        def do_command(self, caps):
            mode_str = "multi-stream" if substreams else "single-stream"
            qs = caps.boolget("quic.substreams")
            print(f"  Connected ({mode_str}), server quic.substreams={qs}")
            # hook QUIC substream arrival timing (before parse thread)
            self._hook_quic_timing()
            # request the server to start sending audio
            audio_caps = typedict(caps.dictget("audio") or {})
            if audio_caps.boolget("send"):
                codec = "opus+ogg"
                print(f"  Requesting audio: {codec}")
                self.send("sound-control", "start", codec)
            else:
                print(f"  Server audio send not available")
            print(f"  Collecting for {duration}s...")
            # now that hello is done, install our packet handler
            self._timing_mode = True
            collector.start()
            quic_collector.start()
            GLib.timeout_add(int(duration * 1000), self._stop_collecting)

        def _hook_quic_timing(self):
            """Wrap put_raw_substream_data to timestamp QUIC-level arrivals."""
            conn = getattr(self, "_protocol", None) and self._protocol._conn
            if not conn or not hasattr(conn, "put_raw_substream_data"):
                print("  (no QUIC substream timing — not a QUIC connection)")
                return
            original_put = conn.put_raw_substream_data

            def timed_put(data, stream_id=1):
                # label by stream_id — the log output shows which id is which type
                quic_collector.record(f"quic:stream-{stream_id}", len(data))
                return original_put(data, stream_id)

            conn.put_raw_substream_data = timed_put
            # also intercept the substream type detection from log output
            # by wrapping the _substream_map setter on the WebSocketClient;
            # simpler: just parse the "new substream N for 'type'" log messages
            # that are already printed — but those fire before do_command.
            # Instead, peek at what's already registered:
            print(f"  QUIC substream timing hooked on {conn}")

        def _stop_collecting(self):
            collector.stop()
            quic_collector.stop()
            print(f"  Done. Disconnecting...")
            self.quit(0)
            return False

        def process_packet(self, proto, packet):
            ptype = str(packet[0]) if len(packet) > 0 else ""

            if not getattr(self, "_timing_mode", False):
                # let normal hello/auth flow through
                return super().process_packet(proto, packet)

            # estimate packet size
            size = sum(
                len(x) if isinstance(x, (bytes, bytearray, memoryview)) else 8
                for x in packet
            )
            collector.record(ptype, size)

            if ptype == "ping":
                echotime = packet[1] if len(packet) > 1 else 0
                self.send("ping_echo", echotime, 0, 0, 0, -1)
            elif ptype == "draw":
                # ack draw packets so server keeps sending
                wid = packet[1] if len(packet) > 1 else 0
                width = packet[4] if len(packet) > 4 else 0
                height = packet[5] if len(packet) > 5 else 0
                packet_sequence = packet[8] if len(packet) > 8 else 0
                self.send("damage-sequence", packet_sequence, wid, width, height, 1, "")
            elif ptype in ("new-window", "new-override-redirect"):
                # map the window so server starts sending draws
                wid = packet[1] if len(packet) > 1 else 0
                x = packet[2] if len(packet) > 2 else 0
                y = packet[3] if len(packet) > 3 else 0
                w = packet[4] if len(packet) > 4 else 100
                h = packet[5] if len(packet) > 5 else 100
                self.send("map-window", wid, x, y, w, h)

    mode_str = "multi-stream" if substreams else "single-stream"
    print(f"\nConnecting to {url} ({mode_str})...")

    app = TimingClient(opts)
    display_desc = do_pick_display(
        lambda msg: sys.exit(msg), opts, [url], [sys.argv[0], url]
    )
    app.display_desc = display_desc

    try:
        connect_to_server(app, display_desc, opts)
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            app.cleanup()
        except Exception:
            pass

    return collector, quic_collector


def main():
    parser = argparse.ArgumentParser(
        description="QUIC HOL blocking test",
        epilog="Generate draw traffic (e.g., glxgears) on the display before running.",
    )
    parser.add_argument("url", help="xpra QUIC URL (e.g., quic://host:10000/)")
    parser.add_argument("--duration", type=float, default=10,
                        help="seconds per test pass (default: 10)")
    parser.add_argument("--password", default="",
                        help="authentication password")
    parser.add_argument("--single-only", action="store_true",
                        help="only run single-stream test")
    parser.add_argument("--multi-only", action="store_true",
                        help="only run multi-stream test")
    args = parser.parse_args()

    results = {}
    quic_results = {}

    if not args.multi_only:
        c, qc = run_test(args.url, args.password, args.duration, substreams=False)
        c.report("Single-stream — parse thread")
        if qc.arrivals:
            qc.report("Single-stream — QUIC arrival")
        results["single"] = c
        quic_results["single"] = qc

    if not args.single_only:
        if not args.multi_only:
            print("\n--- waiting 2s before next test ---")
            time.sleep(2)
        c, qc = run_test(args.url, args.password, args.duration, substreams=True)
        c.report("Multi-stream — parse thread")
        if qc.arrivals:
            qc.report("Multi-stream — QUIC arrival")
        results["multi"] = c
        quic_results["multi"] = qc

    # Comparison (parse thread level)
    _print_comparison(results, "Parse thread")
    # Comparison (QUIC arrival level)
    _print_comparison(quic_results, "QUIC arrival")


def _print_comparison(results: dict, label: str):
    if "single" not in results or "multi" not in results:
        return
    common = sorted(
        set(results["single"].arrivals.keys()) & set(results["multi"].arrivals.keys())
    )
    if not common:
        return
    print(f"\n{'=' * 60}")
    print(f"  Comparison — {label}")
    print(f"{'=' * 60}")
    for cat in common:
        s_ia = results["single"].inter_arrival_ms(cat)
        m_ia = results["multi"].inter_arrival_ms(cat)
        if len(s_ia) < 2 or len(m_ia) < 2:
            continue
        s_p95 = sorted(s_ia)[int(len(s_ia) * 0.95)]
        m_p95 = sorted(m_ia)[int(len(m_ia) * 0.95)]
        s_max = max(s_ia)
        m_max = max(m_ia)
        s_jitter = statistics.stdev(s_ia)
        m_jitter = statistics.stdev(m_ia)
        p95_change = ((s_p95 - m_p95) / s_p95 * 100) if s_p95 > 0 else 0
        max_change = ((s_max - m_max) / s_max * 100) if s_max > 0 else 0
        jitter_change = ((s_jitter - m_jitter) / s_jitter * 100) if s_jitter > 0 else 0
        print(f"\n  {cat}:")
        print(f"    p95:    {s_p95:.1f}ms -> {m_p95:.1f}ms ({p95_change:+.0f}%)")
        print(f"    max:    {s_max:.1f}ms -> {m_max:.1f}ms ({max_change:+.0f}%)")
        print(f"    jitter: {s_jitter:.1f}ms -> {m_jitter:.1f}ms ({jitter_change:+.0f}%)")


if __name__ == "__main__":
    main()
