# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Reads xpra wire-format audio packets from an OS pipe and dispatches
# ABOUTME: them to an AudioSink, bypassing the main process parse thread.

import os
from queue import Queue, Full
from collections.abc import Callable

from xpra.net.protocol.header import HEADER_SIZE, unpack_header
from xpra.net.packet_encoding import decode
from xpra.net.compression import decompress
from xpra.os_util import gi_import
from xpra.util.str_fn import bytestostr, memoryview_to_bytes
from xpra.util.thread import start_thread
from xpra.log import Logger

GLib = gi_import("GLib")

log = Logger("audio")

MAX_RAW_PACKETS = 4
MAX_PACKET_INDEX = 16
WRITE_QUEUE_SIZE = 24  # ~480ms of audio at 20ms Opus frames


class PipeWriter:
    """Non-blocking write side of the audio pipe.

    The asyncio callback calls write() which never blocks — it puts data on a
    bounded queue. A daemon thread drains the queue with blocking os.write().
    If the queue is full (subprocess stalled), packets are dropped silently
    since audio is loss-tolerant.
    """

    def __init__(self, fd: int):
        self.fd = fd
        self._queue: Queue[bytes | None] = Queue(maxsize=WRITE_QUEUE_SIZE)
        self._closed = False
        self._packet_count = 0
        self._thread = start_thread(self._write_loop, "audio-pipe-writer", daemon=True)
        log("audio pipe writer started on fd %s", fd)

    def write(self, data: bytes) -> bool:
        """Queue data for writing. Returns False if the pipe is dead."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(data)
        except Full:
            log("audio pipe writer: queue full, dropping %d bytes", len(data))
        return True

    def close(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except Full:
            pass

    def _write_loop(self) -> None:
        try:
            while not self._closed:
                data = self._queue.get()
                if data is None:
                    break
                view = memoryview(data)
                while view:
                    written = os.write(self.fd, view)
                    view = view[written:]
                self._packet_count += 1
                if self._packet_count == 1:
                    log.info("audio pipe writer: first packet written (%d bytes)", len(data))
                elif self._packet_count % 500 == 0:
                    log.info("audio pipe writer: %d packets written", self._packet_count)
        except OSError:
            log("audio pipe writer: pipe broken after %d packets", self._packet_count, exc_info=True)
        self._closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass
        log("audio pipe writer exiting after %d packets", self._packet_count)


class AudioPipeReader:
    """Reads xpra wire-format packets from a pipe fd and dispatches audio data.

    Runs a blocking reader thread in the audio sink subprocess. The pipe carries
    the same xpra wire bytes that flow over QUIC substreams — standard 8-byte
    headers, multi-chunk packets, rencodeplus encoding. No QUIC-specific code.
    """

    def __init__(self, pipe_fd: int, add_data_cb: Callable):
        self.pipe_fd = pipe_fd
        self.add_data_cb = add_data_cb
        self._buf = b""
        self._raw_packets: dict[int, bytes] = {}
        self._closed = False
        self._packet_count = 0
        self._thread = start_thread(self._read_loop, "audio-pipe-reader", daemon=True)

    def close(self) -> None:
        self._closed = True
        try:
            os.close(self.pipe_fd)
        except OSError:
            pass

    def _read_loop(self) -> None:
        log("audio pipe reader started on fd %s", self.pipe_fd)
        try:
            while not self._closed:
                try:
                    data = os.read(self.pipe_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self._buf += data
                self._parse()
        except Exception:
            log("audio pipe reader error after %d packets", self._packet_count, exc_info=True)
        log("audio pipe reader exiting after %d packets", self._packet_count)

    def _parse(self) -> None:
        while len(self._buf) >= HEADER_SIZE:
            # peek at the header
            pchar, protocol_flags, compression_level, packet_index, data_size = unpack_header(self._buf)
            if pchar != b"P":
                log.warn("Warning: invalid audio pipe header byte: %s", hex(self._buf[0]))
                self._buf = b""
                self._raw_packets.clear()
                return
            total = HEADER_SIZE + data_size
            if len(self._buf) < total:
                # incomplete packet, wait for more data
                return
            payload = self._buf[HEADER_SIZE:total]
            self._buf = self._buf[total:]
            if isinstance(payload, memoryview):
                payload = memoryview_to_bytes(payload)

            if packet_index > 0:
                if packet_index >= MAX_PACKET_INDEX or len(self._raw_packets) >= MAX_RAW_PACKETS:
                    log.warn("Warning: invalid audio pipe packet index %s", packet_index)
                    self._raw_packets.clear()
                    continue
                # raw inline chunk (audio data, packet metadata) — store for assembly
                self._raw_packets[packet_index] = payload
                continue

            # main packet (index 0): decompress, decode, assemble
            if compression_level > 0:
                try:
                    payload = decompress(payload, compression_level)
                except Exception:
                    log("audio pipe decompression error", exc_info=True)
                    self._raw_packets.clear()
                    continue

            try:
                packet = list(decode(payload, protocol_flags))
            except Exception:
                log("audio pipe decode error", exc_info=True)
                self._raw_packets.clear()
                continue

            # replace placeholders with raw chunks
            if self._raw_packets:
                for idx, raw_data in self._raw_packets.items():
                    if idx < len(packet):
                        packet[idx] = raw_data
                self._raw_packets.clear()

            self._dispatch(packet)

    def _dispatch(self, packet: list) -> None:
        packet_type = bytestostr(packet[0])
        if packet_type not in ("sound-data", "audio-data"):
            log("audio pipe: ignoring packet type %r", packet_type)
            return
        data = packet[2] if len(packet) > 2 else b""
        metadata = packet[3] if len(packet) > 3 else {}
        packet_metadata = packet[4] if len(packet) > 4 else ()
        if isinstance(metadata, (list, tuple)):
            metadata = dict(metadata)
        self._packet_count += 1
        if self._packet_count == 1:
            log.info("audio pipe reader: first packet dispatched (%s, %d bytes)", packet_type, len(data))
        elif self._packet_count % 500 == 0:
            log.info("audio pipe reader: %d packets dispatched", self._packet_count)
        GLib.idle_add(self.add_data_cb, data, metadata, packet_metadata)
