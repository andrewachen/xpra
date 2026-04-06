#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Audio time-stretching backends (libsonic and SoundTouch) via ctypes,
# ABOUTME: plus a GstBaseTransform element for in-place buffer modification.

import ctypes
import os
import sys
from ctypes import c_void_p, c_float, c_uint, c_int, c_short, POINTER

from xpra.log import Logger

log = Logger("audio")

# "sonic" (default), "soundtouch", or "none":
TEMPO_BACKEND = os.environ.get("XPRA_TEMPO_BACKEND", "sonic").lower()

_load_errors: list[str] = []


def _find_lib_dlls(names: list[str]) -> list[str]:
    """Find full paths for DLLs in xpra's lib/ directory."""
    lib_dir = os.path.join(os.path.dirname(sys.executable), "lib")
    paths = []
    if os.path.isdir(lib_dir):
        # match by checking if any candidate name is a prefix of the filename:
        lower_names = [n.lower() for n in names]
        for dll in os.listdir(lib_dir):
            base = dll.lower().split(".")[0]  # "libSonic-0.dll" → "libsonic-0"
            if base in lower_names or any(base.startswith(n) for n in lower_names):
                paths.append(os.path.join(lib_dir, dll))
    return paths


def _try_load(names: list[str]):
    """Try loading a shared library from a list of names + lib/ scan."""
    all_names = _find_lib_dlls(names) + names
    for name in all_names:
        try:
            return ctypes.CDLL(name)
        except OSError as e:
            _load_errors.append(f"{name}: {e}")
    return None


# ---------------------------------------------------------------------------
# libsonic backend
# ---------------------------------------------------------------------------
_sonic_lib = None
_sonic_checked = False


def _load_sonic():
    global _sonic_lib, _sonic_checked
    if _sonic_checked:
        return _sonic_lib
    _sonic_checked = True
    lib = _try_load([
        "sonic", "libsonic", "libsonic-0",
        "libsonic.so.0", "libsonic.so",
    ])
    if not lib:
        return None
    try:
        lib.sonicCreateStream.restype = c_void_p
        lib.sonicCreateStream.argtypes = [c_int, c_int]
        lib.sonicDestroyStream.argtypes = [c_void_p]
        lib.sonicSetSpeed.argtypes = [c_void_p, c_float]
        lib.sonicWriteShortToStream.argtypes = [c_void_p, POINTER(c_short), c_int]
        lib.sonicWriteShortToStream.restype = c_int
        lib.sonicReadShortFromStream.argtypes = [c_void_p, POINTER(c_short), c_int]
        lib.sonicReadShortFromStream.restype = c_int
        lib.sonicFlushStream.argtypes = [c_void_p]
        lib.sonicFlushStream.restype = c_int
        lib.sonicSamplesAvailable.argtypes = [c_void_p]
        lib.sonicSamplesAvailable.restype = c_int
        _sonic_lib = lib
        log("libsonic loaded: %s", lib._name)
        return lib
    except AttributeError as e:
        _load_errors.append(f"sonic API: {e}")
        return None


class SonicProcessor:
    """Wraps libsonic C API for int16 PCM time-stretching.

    process_fixed_size() always returns exactly len(input) bytes.
    WSOLA may produce more output (tempo < 1.0) or less (tempo > 1.0)
    than input — the overflow buffer bridges the difference across calls.
    """

    def __init__(self, sample_rate: int, channels: int):
        lib = _load_sonic()
        if not lib:
            raise RuntimeError("libsonic not available")
        self._lib = lib
        self._rate = sample_rate
        self._channels = channels
        self._handle = lib.sonicCreateStream(sample_rate, channels)
        self._out_buf = (c_short * (8192 * channels))()
        self._overflow = b""
        self.padding_count = 0

    def set_tempo(self, tempo: float) -> None:
        self._lib.sonicSetSpeed(self._handle, c_float(tempo))

    def process_fixed_size(self, data: bytes) -> bytes:
        """Feed int16 PCM, return same-size output for in-place buffer mod."""
        target_len = len(data)
        num_values = target_len // 2
        num_frames = num_values // self._channels
        in_buf = (c_short * num_values).from_buffer_copy(data)
        self._lib.sonicWriteShortToStream(self._handle, in_buf, num_frames)
        # read all available output:
        avail = self._lib.sonicSamplesAvailable(self._handle)
        if avail > 0:
            max_frames = len(self._out_buf) // self._channels
            read = self._lib.sonicReadShortFromStream(
                self._handle, self._out_buf, min(avail, max_frames))
            if read > 0:
                out_values = read * self._channels
                raw = bytes(ctypes.cast(self._out_buf,
                                        POINTER(c_short * out_values)).contents)
            else:
                raw = b""
        else:
            raw = b""
        # fixed-size output via overflow management:
        available = self._overflow + raw
        if len(available) >= target_len:
            result = available[:target_len]
            self._overflow = available[target_len:]
            return result
        # not enough output — pad with original data (splice artifact possible):
        self.padding_count += 1
        result = available + data[len(available):]
        self._overflow = b""
        return result[:target_len]

    def clear(self) -> None:
        # sonic has no clear — recreate the stream:
        self._lib.sonicDestroyStream(self._handle)
        self._handle = self._lib.sonicCreateStream(self._rate, self._channels)
        self._overflow = b""
        self.padding_count = 0

    def destroy(self) -> None:
        if self._handle:
            self._lib.sonicDestroyStream(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# SoundTouch backend
# ---------------------------------------------------------------------------
_st_lib = None
_st_checked = False


def _load_soundtouch():
    global _st_lib, _st_checked
    if _st_checked:
        return _st_lib
    _st_checked = True
    lib = _try_load([
        "SoundTouchDll", "libSoundTouchDll-0", "libSoundTouchDll",
        "libSoundTouchDll.so.0", "libSoundTouchDll.so",
    ])
    if not lib:
        return None
    try:
        lib.soundtouch_createInstance.restype = c_void_p
        lib.soundtouch_destroyInstance.argtypes = [c_void_p]
        lib.soundtouch_setTempo.argtypes = [c_void_p, c_float]
        lib.soundtouch_setChannels.argtypes = [c_void_p, c_uint]
        lib.soundtouch_setSampleRate.argtypes = [c_void_p, c_uint]
        lib.soundtouch_putSamples_i16.argtypes = [c_void_p, POINTER(c_short), c_uint]
        lib.soundtouch_receiveSamples_i16.argtypes = [c_void_p, POINTER(c_short), c_uint]
        lib.soundtouch_receiveSamples_i16.restype = c_uint
        lib.soundtouch_flush.argtypes = [c_void_p]
        lib.soundtouch_clear.argtypes = [c_void_p]
        _st_lib = lib
        log("SoundTouch loaded: %s", lib._name)
        return lib
    except AttributeError as e:
        _load_errors.append(f"soundtouch API: {e}")
        return None


class SoundTouchProcessor:
    """Wraps SoundTouch C API for int16 PCM time-stretching.

    Same interface as SonicProcessor — process_fixed_size() returns
    exactly len(input) bytes via the same overflow buffer approach.
    """

    def __init__(self, sample_rate: int, channels: int):
        lib = _load_soundtouch()
        if not lib:
            raise RuntimeError("SoundTouch library not available")
        self._lib = lib
        self._handle = lib.soundtouch_createInstance()
        self._channels = channels
        lib.soundtouch_setSampleRate(self._handle, sample_rate)
        lib.soundtouch_setChannels(self._handle, channels)
        self._out_buf = (c_short * (8192 * channels))()
        self._overflow = b""

    def set_tempo(self, tempo: float) -> None:
        self._lib.soundtouch_setTempo(self._handle, c_float(tempo))

    def process_fixed_size(self, data: bytes) -> bytes:
        """Feed int16 PCM, return same-size output for in-place buffer mod."""
        target_len = len(data)
        num_values = target_len // 2
        num_frames = num_values // self._channels
        in_buf = (c_short * num_values).from_buffer_copy(data)
        self._lib.soundtouch_putSamples_i16(self._handle, in_buf, num_frames)
        max_frames = len(self._out_buf) // self._channels
        received = self._lib.soundtouch_receiveSamples_i16(
            self._handle, self._out_buf, max_frames)
        if received > 0:
            out_values = received * self._channels
            raw = bytes(ctypes.cast(self._out_buf,
                                    POINTER(c_short * out_values)).contents)
        else:
            raw = b""
        available = self._overflow + raw
        if len(available) >= target_len:
            result = available[:target_len]
            self._overflow = available[target_len:]
            return result
        result = available + data[len(available):]
        self._overflow = b""
        return result[:target_len]

    def clear(self) -> None:
        self._lib.soundtouch_clear(self._handle)
        self._overflow = b""

    def destroy(self) -> None:
        if self._handle:
            self._lib.soundtouch_destroyInstance(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def tempo_available() -> bool:
    """Check if any tempo backend is loadable."""
    if TEMPO_BACKEND == "none":
        return False
    if TEMPO_BACKEND == "sonic":
        return _load_sonic() is not None or _load_soundtouch() is not None
    if TEMPO_BACKEND == "soundtouch":
        return _load_soundtouch() is not None or _load_sonic() is not None
    return _load_sonic() is not None or _load_soundtouch() is not None


def get_load_error() -> str:
    """Return description of why tempo backends failed to load."""
    return "; ".join(_load_errors) if _load_errors else "unknown"


def get_backend_name() -> str:
    """Return which backend will be used."""
    if TEMPO_BACKEND == "none":
        return "none"
    if TEMPO_BACKEND == "sonic" and _load_sonic():
        return "sonic"
    if TEMPO_BACKEND == "soundtouch" and _load_soundtouch():
        return "soundtouch"
    # fallback to whatever is available:
    if _load_sonic():
        return "sonic"
    if _load_soundtouch():
        return "soundtouch"
    return "none"


def create_processor(sample_rate: int, channels: int):
    """Create a tempo processor using the configured backend."""
    backend = get_backend_name()
    if backend == "sonic":
        return SonicProcessor(sample_rate, channels)
    if backend == "soundtouch":
        return SoundTouchProcessor(sample_rate, channels)
    raise RuntimeError(f"no tempo backend available (tried {TEMPO_BACKEND})")


# ---------------------------------------------------------------------------
# GStreamer BaseTransform element — registers "tempo" for use in pipelines.
# BaseTransform handles pad activation, state changes, caps negotiation,
# and buffer writability. do_transform_ip receives a writable buffer.
# ---------------------------------------------------------------------------
_element_registered = False
try:
    from xpra.os_util import gi_import
    Gst = gi_import("Gst")
    GstBase = gi_import("GstBase", "1.0")
    GObject = gi_import("GObject")

    class TempoTransform(GstBase.BaseTransform):
        """In-place audio tempo adjustment via libsonic or SoundTouch.

        At tempo=1.0: returns OK without touching the buffer (near-zero cost).
        At tempo!=1.0: maps buffer writable, processes PCM through the WSOLA
        backend, writes modified samples back. BaseTransform guarantees the
        buffer is writable when do_transform_ip is called.
        """
        __gstmetadata__ = (
            "TempoTransform",
            "Audio/Effect",
            "Time-stretches audio via libsonic/SoundTouch",
            "Netflix",
        )
        __gsttemplates__ = (
            Gst.PadTemplate.new("src", Gst.PadDirection.SRC,
                                Gst.PadPresence.ALWAYS,
                                Gst.Caps.from_string("audio/x-raw,format=S16LE")),
            Gst.PadTemplate.new("sink", Gst.PadDirection.SINK,
                                Gst.PadPresence.ALWAYS,
                                Gst.Caps.from_string("audio/x-raw,format=S16LE")),
        )

        def __init__(self):
            super().__init__()
            self._tempo = 1.0
            self._processor = None
            self._rate = 0
            self._channels = 0
            # cache last 2 buffers for priming — see sink.py for derivation:
            self._last_pcm = []
            self.tempo_count = 0
            self.probe_errors = 0
            self.tempo_status = ""

        def set_tempo(self, tempo: float) -> None:
            """Called from the timer thread to change playback speed.

            Between non-1.0 speeds (e.g. 1.05→0.95): just changes the speed.
            The overflow buffer may have ~20ms of data at the old speed, but
            a ±5% mismatch over 20ms is imperceptible.

            Returning to 1.0: clears processor state so stale data doesn't
            leak into the next tempo event (which could be seconds later).

            Leaving 1.0: re-primes sonic with cached PCM so it has pitch
            context for immediate output. Priming output is discarded
            (not added to overflow) to avoid echoing already-played audio.
            """
            was_normal = self._tempo == 1.0
            self._tempo = tempo
            if not self._processor:
                return
            if tempo == 1.0:
                self._processor.clear()
            elif was_normal:
                # transitioning from pass-through to active stretching:
                self._processor.set_tempo(tempo)
                self._prime_processor()
            else:
                self._processor.set_tempo(tempo)

        def _prime_processor(self):
            """Feed cached PCM to give sonic pitch context for immediate output.

            Discards the priming output so already-played audio isn't echoed.
            """
            for pcm in self._last_pcm:
                self._processor.process_fixed_size(pcm)
            self._processor._overflow = b""

        def do_set_caps(self, incaps, outcaps):
            s = incaps.get_structure(0)
            _, self._rate = s.get_int("rate")
            _, self._channels = s.get_int("channels")
            log("tempo caps: rate=%i, channels=%i", self._rate, self._channels)
            return True

        def _ensure_processor(self):
            if self._processor or self._rate == 0 or self._channels == 0:
                return
            try:
                self._processor = create_processor(self._rate, self._channels)
                self._processor.set_tempo(self._tempo)
                self._prime_processor()
                self.tempo_status = "ready (%s, %iHz %ich)" % (
                    get_backend_name(), self._rate, self._channels)
            except RuntimeError as e:
                self.tempo_status = "create failed: %s" % e

        def do_transform_ip(self, buf):
            # read buffer for priming cache (cheap — just a read-map + copy):
            ok, map_info = buf.map(Gst.MapFlags.READ)
            if ok:
                data = bytes(map_info.data)
                buf.unmap(map_info)
                self._last_pcm = (self._last_pcm + [data])[-2:]
            else:
                data = None

            if self._tempo == 1.0 or not data:
                return Gst.FlowReturn.OK

            if not self._processor:
                self._ensure_processor()
                if not self._processor:
                    return Gst.FlowReturn.OK

            try:
                output = self._processor.process_fixed_size(data)
                ok, map_info = buf.map(Gst.MapFlags.WRITE)
                if ok:
                    map_info.data[:len(output)] = output
                    buf.unmap(map_info)
                    self.tempo_count += 1
                else:
                    self.probe_errors += 1
                    self.tempo_status = "write map failed"
            except Exception as e:
                self.probe_errors += 1
                if self.probe_errors <= 3:
                    self.tempo_status = "error: %s" % e
            return Gst.FlowReturn.OK

        def do_stop(self):
            if self._processor:
                self._processor.destroy()
                self._processor = None
            return True

    GObject.type_register(TempoTransform)
    _element_registered = Gst.Element.register(
        None, "tempo", Gst.Rank.NONE, TempoTransform.__gtype__)
    log("tempo GStreamer element registered: %s", _element_registered)
except Exception as e:
    log("failed to register tempo element: %s", e)
    _element_registered = False
