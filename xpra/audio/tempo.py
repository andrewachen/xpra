#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Audio time-stretching backends (libsonic and SoundTouch) via ctypes.
# ABOUTME: Used via pad probe in sink.py for jitter buffer tempo control.

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
    """Wraps libsonic C API for int16 PCM time-stretching."""

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
        # priming — pad with original data:
        result = available + data[len(available):]
        self._overflow = b""
        return result[:target_len]

    def clear(self) -> None:
        # sonic has no clear — recreate the stream:
        self._lib.sonicDestroyStream(self._handle)
        self._handle = self._lib.sonicCreateStream(self._rate, self._channels)
        self._overflow = b""

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
    """Wraps SoundTouch C API for int16 PCM time-stretching."""

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
