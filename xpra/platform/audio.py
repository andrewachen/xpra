# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Cross-platform stubs for audio device change notifications.
# ABOUTME: Win32 implementation uses MMDeviceAPI COM notifications.

from collections.abc import Callable

from xpra.platform import platform_import
from xpra.log import Logger

log = Logger("audio")

_audio_device_change_callbacks: list[Callable] = []


def add_audio_device_change_callback(_callback: Callable) -> None:
    # overridden by platform-specific implementation
    pass


def remove_audio_device_change_callback(_callback: Callable) -> None:
    # overridden by platform-specific implementation
    pass


def is_recoverable_audio_error(_error_str: str) -> bool:
    # overridden by platform-specific implementation
    return False


def _fire_audio_device_change() -> None:
    for callback in _audio_device_change_callbacks:
        try:
            callback()
        except Exception as e:
            log("error on %s", callback, exc_info=True)
            log.error("Error: audio device change callback error")
            log.estr(e)


platform_import(globals(), "audio", False,
                "add_audio_device_change_callback",
                "remove_audio_device_change_callback",
                "is_recoverable_audio_error")
