# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Win32 audio platform support.
# ABOUTME: Identifies recoverable WASAPI errors for device invalidation handling.


def is_recoverable_audio_error(error_str: str) -> bool:
    upper = error_str.upper()
    return "DEVICE_INVALIDATED" in upper or "88890004" in upper
