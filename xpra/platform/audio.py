# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Cross-platform audio platform support.
# ABOUTME: Win32 implementation identifies recoverable WASAPI errors.

from xpra.platform import platform_import


def is_recoverable_audio_error(_error_str: str) -> bool:
    # overridden by platform-specific implementation
    return False


platform_import(globals(), "audio", False,
                "is_recoverable_audio_error")
