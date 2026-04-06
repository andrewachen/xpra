# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Win32 audio device change detection via WM_DEVICECHANGE.
# ABOUTME: Fires callbacks when audio output devices are added or removed.

from xpra.platform.win32.constants import WM_DEVICECHANGE
from xpra.platform.win32.events import get_win32_event_listener

from xpra.log import Logger

log = Logger("audio")


def _device_change_callback(*args) -> None:
    log("audio device_change(%s)", args)
    from xpra.platform.audio import _fire_audio_device_change
    _fire_audio_device_change()


def add_audio_device_change_callback(callback) -> None:
    from xpra.platform.audio import _audio_device_change_callbacks
    if len(_audio_device_change_callbacks) == 0:
        el = get_win32_event_listener()
        if el:
            el.add_event_callback(WM_DEVICECHANGE, _device_change_callback)
    _audio_device_change_callbacks.append(callback)


def remove_audio_device_change_callback(callback) -> None:
    from xpra.platform.audio import _audio_device_change_callbacks
    if callback in _audio_device_change_callbacks:
        _audio_device_change_callbacks.remove(callback)
    if len(_audio_device_change_callbacks) == 0:
        el = get_win32_event_listener(False)
        if el:
            el.remove_event_callback(WM_DEVICECHANGE, _device_change_callback)


def is_recoverable_audio_error(error_str: str) -> bool:
    upper = error_str.upper()
    return "DEVICE_INVALIDATED" in upper or "88890004" in upper
