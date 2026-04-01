#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Integration test for scaling recovery after quality dip.
# ABOUTME: Injects a quality override into a live xpra server via a temp file signal.

"""
Validates that the scaling recovery fix works on a live xpra server.

This script works in two parts:
1. A monkey-patch added to compress.py that reads a "force quality" signal file
2. This script that creates/removes the signal file and monitors xpra info

The signal file mechanism avoids the broken xpra control quality commands.

Usage:
  1. Apply the patch (done automatically by this script via the override)
  2. Restart xpra, reconnect client, start playing video
  3. Run: python3 tests/scripts/test_scaling_recovery.py
"""

import os
import subprocess
import sys
import re
import time

SIGNAL_FILE = "/tmp/xpra-force-quality"


def xpra_info(display=":100"):
    result = subprocess.run(["xpra", "info", display], capture_output=True, text=True)
    info = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k] = v
    return info


def get_window_state(info, wid):
    prefix = f"client.window.{wid}."
    scaling = info.get(f"{prefix}scaling", "?")
    quality = info.get(f"{prefix}encoding.quality.cur", "?")
    enc_w = info.get(f"{prefix}encoder.encoder_width", "?")
    enc_h = info.get(f"{prefix}encoder.encoder_height", "?")
    codec = info.get(f"{prefix}encoder.codec", "?")
    return scaling, quality, enc_w, enc_h, codec


def find_browser_window(info):
    for k, v in info.items():
        m = re.match(r"client\.window\.(\d+)\.content-type", k)
        if m and v == "browser":
            return int(m.group(1))
    return None


def parse_scaling(s):
    m = re.match(r"\((\d+),\s*(\d+)\)", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def main():
    print("=== Scaling Recovery Integration Test ===\n")

    # Check that the signal file patch is active
    info = xpra_info()
    wid = find_browser_window(info)
    if wid is None:
        print("SKIP: no browser window found")
        sys.exit(2)

    scaling, quality, enc_w, enc_h, codec = get_window_state(info, wid)
    print(f"Window {wid}: {codec} {enc_w}x{enc_h} scaling={scaling} quality={quality}")

    if parse_scaling(scaling) != (1, 1):
        print(f"SKIP: scaling is already {scaling}, expected (1, 1) to start")
        sys.exit(2)

    # Phase 1: Create signal file to force quality low
    print("\nPhase 1: Forcing quality=20 via signal file...")
    with open(SIGNAL_FILE, "w") as f:
        f.write("20")

    scaled = False
    for i in range(20):
        time.sleep(1)
        info = xpra_info()
        scaling, quality, enc_w, enc_h, codec = get_window_state(info, wid)
        print(f"  [{i+1:2d}s] scaling={scaling} quality={quality} {enc_w}x{enc_h}")
        if parse_scaling(scaling) != (1, 1):
            scaled = True
            print(f"  -> Downscaled to {scaling}")
            break

    if not scaled:
        print("FAIL: could not trigger downscaling (is the compress.py patch applied?)")
        os.unlink(SIGNAL_FILE)
        sys.exit(1)

    # Phase 2: Remove signal file to restore normal quality
    print("\nPhase 2: Removing signal file, waiting for recovery...")
    os.unlink(SIGNAL_FILE)

    recovered = False
    for i in range(30):
        time.sleep(1)
        info = xpra_info()
        scaling, quality, enc_w, enc_h, codec = get_window_state(info, wid)
        print(f"  [{i+1:2d}s] scaling={scaling} quality={quality} {enc_w}x{enc_h}")
        if parse_scaling(scaling) == (1, 1):
            recovered = True
            print(f"  -> Recovered to (1, 1)!")
            break

    if recovered:
        print("\nPASS: scaling recovered after quality improvement")
        sys.exit(0)
    else:
        print("\nFAIL: scaling did not recover within 30 seconds")
        sys.exit(1)


if __name__ == "__main__":
    main()
