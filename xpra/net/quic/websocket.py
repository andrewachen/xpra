# This file is part of Xpra.
# Copyright (C) 2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from typing import Any
from collections.abc import Callable

from aioquic.h3.events import HeadersReceived, H3Event
from aioquic.quic.packet import QuicErrorCode

from xpra.net.bytestreams import pretty_socket
from xpra.net.quic.connection import XpraQuicConnection
from xpra.net.quic.common import SERVER_NAME, http_date
from xpra.net.websockets.header import close_packet
from xpra.util.str_fn import Ellipsizer
from xpra.log import Logger

log = Logger("quic")

# route these packet type prefixes to independent QUIC streams
SUBSTREAM_PACKET_TYPES = tuple(x.strip() for x in os.environ.get(
    "XPRA_QUIC_SUBSTREAM_PACKET_TYPES",
    "sound,draw"
).split(",") if x.strip())


class ServerWebSocketConnection(XpraQuicConnection):
    def __init__(self, connection, scope: dict,
                 stream_id: int, transmit: Callable[[], None],
                 register_substream: Callable[[int, "ServerWebSocketConnection"], None] | None = None):
        super().__init__(connection, stream_id, transmit, "", 0, "wss", info=None, options=None)
        self.scope: dict = scope
        self._packet_type_streams: dict[str, int] = {}
        self._substream_ids: set[int] = set()
        self._pending_substreams: set[str] = set()
        self._use_substreams = bool(SUBSTREAM_PACKET_TYPES)
        self._register_substream = register_substream

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info.setdefault("quic", {})["scope"] = self.scope
        return info

    def __repr__(self):
        try:
            return f"QuicConnection({pretty_socket(self.endpoint)}, {self.stream_id})"
        except AttributeError:
            return f"ServerWebSocketConnection<{self.stream_id}>"

    def http_event_received(self, event: H3Event) -> None:
        log("ws:http_event_received(%s)", Ellipsizer(event))
        if self.closed:
            return
        if isinstance(event, HeadersReceived):
            subprotocols = self.scope.get("subprotocols", ())
            if "xpra" not in subprotocols:
                message = f"unsupported websocket subprotocols {subprotocols}"
                log.warn(f"Warning: {message}")
                self.close(QuicErrorCode.APPLICATION_ERROR, message)
                return
            log.info("websocket request at %s", self.scope.get("path", "/"))
            self.accepted = True
            self.send_accept(self.stream_id)
            self.transmit()
            return
        super().http_event_received(event)

    def send_accept(self, stream_id: int) -> None:
        self.send_headers(stream_id=stream_id, headers={
            ":status": 200,
            "server": SERVER_NAME,
            "date": http_date(),
            "sec-websocket-protocol": "xpra",
        })

    def send_close(self, code=QuicErrorCode.NO_ERROR, reason="") -> None:
        log(f"send_close({code}, {reason})")
        wscode = 1000 if code == QuicErrorCode.NO_ERROR else 4000 + int(code)
        self.send_ws_close(wscode, reason)
        super().send_close(code, reason)

    def send_ws_close(self, code: int = 1000, reason: str = "") -> None:
        if self.accepted:
            data = close_packet(code, reason)
            self.write(data, "close")
        else:
            self.send_headers(self.stream_id, headers={":status": code})
            self.transmit()

    def get_packet_stream_id(self, packet_type: str) -> int:
        if self.closed or not self._use_substreams or not packet_type:
            return self.stream_id
        if not any(packet_type.startswith(x) for x in SUBSTREAM_PACKET_TYPES):
            return self.stream_id
        # ie: "sound-data" -> "sound"
        stream_type = packet_type.split("-", 1)[0]
        stream_id = self._packet_type_streams.get(stream_type)
        if stream_id is not None:
            # already allocated substream:
            return stream_id
        # reserve the stream type so we don't retry on the next packet;
        # actual allocation happens on the asyncio loop in _allocate_substream()
        self._packet_type_streams[stream_type] = self.stream_id
        self._pending_substreams.add(stream_type)
        return self.stream_id

    def _allocate_substream(self, stream_type: str) -> None:
        """Allocate a raw QUIC stream for a packet type. Must run on the asyncio loop."""
        log(f"_allocate_substream({stream_type!r})")
        quic = self.connection._quic
        try:
            stream_id = quic.get_next_available_stream_id()
        except Exception:
            log(f"unable to allocate new stream-id for {stream_type!r}", exc_info=True)
            log.warn(f"Warning: unable to allocate a new stream-id for {stream_type!r}")
            self._use_substreams = False
            return
        # send type prefix as first bytes (QUIC guarantees in-order per-stream)
        header = f"xpra:{stream_type}\n".encode()
        log(f"sending substream header on stream {stream_id}: {header!r}")
        quic.send_stream_data(stream_id, header)
        self._substream_ids.add(stream_id)
        self._packet_type_streams[stream_type] = stream_id
        if self._register_substream:
            self._register_substream(stream_id, self)
        log.info(f"new substream {stream_id} for {stream_type!r}")

    def do_write(self, stream_id: int, data: bytes) -> None:
        # process any pending substream allocations (we're on the asyncio loop now)
        if self._pending_substreams:
            pending = list(self._pending_substreams)
            self._pending_substreams.clear()
            log(f"allocating pending substreams: {pending}")
            for stream_type in pending:
                self._allocate_substream(stream_type)
        if stream_id in self._substream_ids:
            # raw QUIC stream — bypass H3 framing
            log(f"substream {stream_id}: writing {len(data)} bytes")
            self.connection._quic.send_stream_data(stream_id=stream_id, data=data, end_stream=self.closed)
        else:
            # main WebSocket stream — use H3 DATA frames
            super().do_write(stream_id, data)
