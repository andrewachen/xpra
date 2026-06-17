#!/usr/bin/env bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Cross-compile-check a WIN32-only Cython .pyx (cythonize -> $CC -c) on
# ABOUTME: arm64 + x64 via dockcross, as a fast local pre-gate before Windows CI.
#
# Usage: ./tests/docker/win-compile-check.sh xpra/codecs/mf/decoder.pyx
#
# This is a COMPILE-ONLY pre-gate: it cythonizes the .pyx to C and compiles
# that C with the dockcross mingw cross-compilers (-c, no link). It cannot
# link -- there are no MediaFoundation / libvpl import libraries in the
# dockcross sysroots -- so "success" means the generated C *compiles*, not
# that it links into a working .pyd.
#
# KNOWN LIMITATION (S4D-UNAVAILABLE): Cython-generated C #includes "Python.h".
# The dockcross/windows-{arm64,static-x64} images only ship the Linux *host*
# CPython headers; their pyconfig.h is multiarch-Linux and is rejected by the
# mingw cross-target ("unknown multiarch location for pyconfig.h", "Must define
# SIZEOF_WCHAR_T", native-thread errors). No Windows/mingw CPython headers exist
# in the mingw sysroots, and supplying a matching mingw-w64 CPython header+config
# set for both arches is exactly the heavy MSYS2 toolchain the Windows CI already
# provides. The Windows SDK headers themselves (windows.h, mfapi.h, ...) DO
# cross-compile cleanly here; only Python.h blocks the full check.
#
# Therefore this script detects the Python.h incompatibility and exits non-zero
# with an "S4D-UNAVAILABLE" diagnostic rather than reporting a misleading pass.
# It does NOT fake a passing compile. If a future dockcross image ships
# mingw-compatible CPython headers (or you point PYTHON_INCLUDE at a set of
# them), the compile will proceed and the script will report the real result.

set -euo pipefail

ARM64_IMAGE="dockcross/windows-arm64:latest"
X64_IMAGE="dockcross/windows-static-x64:latest"
UIDGID="$(id -u):$(id -g)"

if [ $# -lt 1 ]; then
    echo "usage: $0 <path/to/file.pyx>" >&2
    exit 2
fi

PYX="$1"
if [ ! -f "$PYX" ]; then
    echo "error: no such .pyx: $PYX" >&2
    exit 2
fi
case "$PYX" in
    *.pyx) ;;
    *) echo "error: argument must be a .pyx file: $PYX" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_DIR"

# Optional override: a directory containing mingw-compatible CPython headers.
# When unset the script uses the dockcross image's own python3 headers, which
# triggers the S4D-UNAVAILABLE path described above.
PYTHON_INCLUDE="${PYTHON_INCLUDE:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STEM="$(basename "${PYX%.pyx}")"
CFILE="$WORK/${STEM}.c"

echo "==> cythonize $PYX"
# -I . so cimports of xpra .pxd files resolve against the repo root.
if command -v cython >/dev/null 2>&1; then
    cython -3 --fast-fail -I . -o "$CFILE" "$PYX"
else
    "${PYTHON:-python3}" -m cython -3 --fast-fail -I . -o "$CFILE" "$PYX"
fi
echo "    generated $(wc -l < "$CFILE") lines of C"

# Copy any sibling C headers the .pyx pulls in (e.g. mf_decode.h) next to the
# generated C so the cross-compiler finds them with -I.
PYX_DIR="$(dirname "$PYX")"
shopt -s nullglob
for h in "$PYX_DIR"/*.h; do
    cp "$h" "$WORK/"
done
shopt -u nullglob

# Compile-check the generated C inside one dockcross image.
# Returns: 0 = compiled, 3 = S4D-UNAVAILABLE (Python.h header gap), 1 = real
# compile error in our code.
compile_check() {
    local image="$1"
    echo "==> compile-check via $image"
    local logf="$WORK/${image//[:\/]/_}.log"
    set +e
    docker run --rm --user "$UIDGID" \
        -v "$WORK":/work -w /work \
        -e PYTHON_INCLUDE="$PYTHON_INCLUDE" \
        "$image" bash -c '
            set -e
            # Prefer an explicitly-supplied mingw-compatible CPython include dir;
            # otherwise fall back to the image host headers (known-incompatible).
            if [ -n "${PYTHON_INCLUDE:-}" ]; then
                PYINC="$PYTHON_INCLUDE"
            else
                PYINC="$(dirname "$(find /usr/include -name Python.h 2>/dev/null | head -1)")"
            fi
            echo "    CC=$CC"
            echo "    PYINC=$PYINC"
            $CC -c -I. -I"$PYINC" -o /dev/null '"${STEM}"'.c
        ' >"$logf" 2>&1
    local rc=$?
    set -e
    cat "$logf"
    if [ "$rc" -eq 0 ]; then
        echo "    OK: compiled clean"
        return 0
    fi
    # Distinguish the known Python.h header gap from a genuine code error.
    if grep -qE 'unknown multiarch location for pyconfig\.h|Must define SIZEOF_WCHAR_T|Require native threads' "$logf"; then
        return 3
    fi
    return 1
}

overall=0
for image in "$ARM64_IMAGE" "$X64_IMAGE"; do
    if compile_check "$image"; then
        :
    else
        rc=$?
        if [ "$rc" -eq 3 ]; then
            overall=3
        else
            # A real compile error trumps the header-gap status.
            echo "FAIL: $STEM.c failed to compile on $image (see errors above)" >&2
            exit 1
        fi
    fi
done

if [ "$overall" -eq 3 ]; then
    echo ""
    echo "S4D-UNAVAILABLE: dockcross images lack mingw-compatible CPython headers"
    echo "  (Python.h pyconfig.h is Linux-multiarch and rejected by the mingw"
    echo "  cross-target). The Windows SDK headers compile fine; only Python.h"
    echo "  blocks the Cython compile-check. Set PYTHON_INCLUDE to a directory of"
    echo "  mingw-w64 CPython headers to enable this gate, or rely on Windows CI"
    echo "  (S4W) for WIN32 .pyx verification."
    exit 3
fi

echo ""
echo "PASS: $STEM.c compiled on both arm64 and x64 (compile-only, not linked)"
