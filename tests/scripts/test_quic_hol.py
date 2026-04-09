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
        self.network_jitter: list[float] = []
        self.start_time = 0.0
        self.collecting = False

    def start(self):
        self.arrivals.clear()
        self.sizes.clear()
        self.network_jitter.clear()
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

        if self.network_jitter and len(self.network_jitter) > 1:
            nj = sorted(self.network_jitter)
            print(f"\n  sound network jitter (arrival_diff - send_diff):")
            print(f"    samples={len(nj)}  "
                  f"mean={statistics.mean(nj):.1f}ms  "
                  f"p50={nj[int(len(nj) * 0.50)]:.1f}ms  "
                  f"p95={nj[int(len(nj) * 0.95)]:.1f}ms  "
                  f"max={max(nj):.1f}ms  "
                  f"stdev={statistics.stdev(nj):.1f}ms")


def run_test(url: str, password: str, duration: float, substreams: bool, sync_interval: int = 0) -> tuple:
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
            all_encodings = ("av1", "avif", "h264", "h265", "jpeg", "jpega",
                             "png", "png/L", "png/P", "rgb24", "rgb32",
                             "vp8", "vp9", "webp")
            self.hello_extra["encodings"] = all_encodings
            self.hello_extra["encoding.core"] = all_encodings
            self.hello_extra["desktop_size"] = (1920, 1080)
            self.hello_extra["screen_sizes"] = [(1920, 1080, 508, 286)]
            # remove "request"="event" so server treats us as a UI client
            self.hello_extra.pop("request", None)
            # allow coexisting with other clients for simultaneous A/B testing
            import uuid as _uuid
            self.hello_extra["share"] = True
            self.hello_extra["uuid"] = _uuid.uuid4().hex
            # control substream capability — must explicitly set False to
            # override get_network_caps() which adds True unconditionally
            self.hello_extra["quic.substreams"] = substreams
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
            # wait for clock sync if requested
            if sync_interval > 0:
                import math
                now = time.time()
                next_boundary = math.ceil(now / sync_interval) * sync_interval
                wait = next_boundary - now
                print(f"  Waiting {wait:.1f}s for next {sync_interval}s boundary...")
                GLib.timeout_add(int(wait * 1000), self._start_collecting)
                return
            self._start_collecting()

        def _start_collecting(self):
            print(f"  Collecting at {time.strftime('%H:%M:%S')} for {duration}s...")
            self._timing_mode = True
            collector.start()
            quic_collector.start()
            GLib.timeout_add(int(duration * 1000), self._stop_collecting)
            return False

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

            # for sound-data, compute network jitter from server timestamp
            # D = arrival_diff - send_diff isolates network transit variation
            # from server-side scheduling jitter (GLib batching)
            if ptype == "sound-data" and len(packet) > 3:
                now_ms = time.monotonic() * 1000
                try:
                    metadata = packet[3]
                    server_time = metadata.get("time", 0) if isinstance(metadata, dict) else 0
                    if server_time > 0:
                        if hasattr(self, "_last_server_time") and self._last_server_time > 0:
                            send_diff = server_time - self._last_server_time
                            arrival_diff = now_ms - self._last_arrival_ms
                            if 5 < send_diff < 2000:
                                collector.network_jitter.append(max(0.0, arrival_diff - send_diff))
                        self._last_server_time = server_time
                        self._last_arrival_ms = now_ms
                except Exception:
                    pass

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


def _run_worker(url, password, duration, substreams, sync_interval, result_dict, key):
    """Worker function for multiprocessing parallel runs."""
    import pickle
    c, qc = run_test(url, password, duration, substreams=substreams, sync_interval=sync_interval)
    # PacketTimingCollector uses defaultdict — pickle-safe
    result_dict[key] = (c, qc)


def main():
    parser = argparse.ArgumentParser(
        description="QUIC HOL blocking test",
        epilog="Generate draw traffic (e.g., glxgears) on the display before running.",
    )
    parser.add_argument("url", help="xpra server (e.g., host:10000 or quic://host:10000/)")
    parser.add_argument("--duration", type=float, default=10,
                        help="seconds per test pass (default: 10)")
    parser.add_argument("--password", default="",
                        help="authentication password")
    parser.add_argument("--single-only", action="store_true",
                        help="only run single-stream test")
    parser.add_argument("--multi-only", action="store_true",
                        help="only run multi-stream test")
    parser.add_argument("--parallel", action="store_true",
                        help="run all modes simultaneously (requires --sharing=yes on server)")
    parser.add_argument("--sync", type=int, default=0, metavar="SECONDS",
                        help="wait until next N-second clock boundary before collecting "
                             "(use same value in both instances for simultaneous A/B)")
    args = parser.parse_args()

    args.url = _normalize_url(args.url)
    if args.parallel:
        _run_parallel(args)
    else:
        _run_sequential(args)


def _normalize_url(url: str) -> str:
    """Normalize bare host:port to quic://host:port/"""
    if "://" not in url:
        url = f"quic://{url}/"
    return url


def _run_parallel(args):
    """Run all modes simultaneously using multiprocessing."""
    from multiprocessing import Process, Manager

    quic_url = _normalize_url(args.url)
    tcp_url = quic_url.replace("quic://", "tcp://", 1)

    # use 30-second sync boundary so all processes start collecting together
    sync = args.sync or 30

    manager = Manager()
    results = manager.dict()

    modes = []
    if not args.multi_only:
        modes.append(("tcp", tcp_url, False, sync))
        modes.append(("quic-single", quic_url, False, sync))
    if not args.single_only:
        modes.append(("quic-multi", quic_url, True, sync))

    processes = []
    for key, url, substreams, sync_val in modes:
        p = Process(target=_run_worker,
                    args=(url, args.password, args.duration, substreams, sync_val, results, key))
        p.start()
        processes.append((key, p))

    for key, p in processes:
        p.join()

    # report results
    for key, _, _, _ in modes:
        if key in results:
            c, qc = results[key]
            c.report(key)
            if qc.arrivals:
                qc.report(f"{key} — QUIC arrival")

    # comparison table
    all_keys = [key for key, _, _, _ in modes if key in results]
    if len(all_keys) >= 2:
        _print_parallel_comparison({k: results[k][0] for k in all_keys}, all_keys)


def _run_sequential(args):
    """Run modes one at a time (original behavior)."""
    results = {}
    quic_results = {}

    if not args.multi_only:
        c, qc = run_test(args.url, args.password, args.duration, substreams=False, sync_interval=args.sync)
        c.report("Single-stream — parse thread")
        if qc.arrivals:
            qc.report("Single-stream — QUIC arrival")
        results["single"] = c
        quic_results["single"] = qc

    if not args.single_only:
        if not args.multi_only:
            print("\n--- waiting 2s before next test ---")
            time.sleep(2)
        c, qc = run_test(args.url, args.password, args.duration, substreams=True, sync_interval=args.sync)
        c.report("Multi-stream — parse thread")
        if qc.arrivals:
            qc.report("Multi-stream — QUIC arrival")
        results["multi"] = c
        quic_results["multi"] = qc

    _print_comparison(results, "Parse thread")
    _print_comparison(quic_results, "QUIC arrival")


def _print_parallel_comparison(results: dict, keys: list[str]):
    """Print a comparison table across all parallel modes."""
    # find packet types common to all modes
    common = None
    for key in keys:
        cats = set(results[key].arrivals.keys())
        common = cats if common is None else common & cats
    if not common:
        return

    print(f"\n{'=' * 70}")
    print(f"  Parallel comparison")
    print(f"{'=' * 70}")

    for cat in sorted(common):
        ias = {}
        for key in keys:
            ia = results[key].inter_arrival_ms(cat)
            if len(ia) < 2:
                continue
            ias[key] = ia
        if len(ias) < 2:
            continue

        print(f"\n  {cat}:")
        header = "    {:>12s}".format("")
        for key in keys:
            if key in ias:
                header += f"  {key:>14s}"
        print(header)

        for metric_name, metric_fn in [
            ("p95", lambda ia: sorted(ia)[int(len(ia) * 0.95)]),
            ("max", lambda ia: max(ia)),
            ("jitter", lambda ia: statistics.stdev(ia)),
        ]:
            row = f"    {metric_name:>12s}"
            for key in keys:
                if key in ias:
                    val = metric_fn(ias[key])
                    row += f"  {val:>12.1f}ms"
            print(row)

    # network jitter comparison (sound only)
    nj_data = {k: results[k].network_jitter for k in keys if results[k].network_jitter}
    if len(nj_data) >= 2:
        print(f"\n  sound network jitter (arrival_diff - send_diff):")
        active_keys = [k for k in keys if k in nj_data]
        header = "    {:>12s}".format("")
        for key in active_keys:
            header += f"  {key:>14s}"
        print(header)
        for metric_name, metric_fn in [
            ("p95", lambda nj: sorted(nj)[int(len(nj) * 0.95)]),
            ("max", lambda nj: max(nj)),
            ("stdev", lambda nj: statistics.stdev(nj)),
        ]:
            row = f"    {metric_name:>12s}"
            for key in active_keys:
                val = metric_fn(nj_data[key])
                row += f"  {val:>12.1f}ms"
            print(row)


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
    # required for multiprocessing on Windows (spawn) and cx_Freeze
    import multiprocessing
    multiprocessing.freeze_support()
    main()
