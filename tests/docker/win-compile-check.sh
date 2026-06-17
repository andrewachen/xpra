#!/usr/bin/env bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Local pre-gate that cross-compile-checks WIN32-only Cython .pyx files
# ABOUTME: on Linux via dockcross + MSYS2 mingw CPython headers (compile-only).
#
# WHAT THIS DOES
# --------------
# Cythonizes a WIN32-only .pyx to C and compiles that C with the dockcross mingw
# cross-compilers for BOTH Windows targets (aarch64 + x86_64), with no link step.
# It is a fast developer-iteration tool: instead of waiting ~15 minutes for the
# Windows CI round-trip, you get a type/syntax compile-check locally in
# seconds-to-minutes (and faster still on repeat runs thanks to the header cache).
#
# WHY COMPILE-ONLY
# ----------------
# The check runs `$CC -c -o /dev/null` — it never links or loads the result, so
# the MSVC-vs-MinGW runtime ABI mismatch is IRRELEVANT here. We only need headers
# the mingw cross-compiler accepts so the Cython-generated C compiles and surfaces
# genuine type/syntax errors in our code. There are no MediaFoundation / libvpl
# import libraries in the dockcross sysroots, so a real link is impossible anyway.
#
# Windows CI (the `windows.yml` workflow) remains the AUTHORITATIVE link + runtime
# gate. This script catches the large class of errors that show up at compile time
# without paying the CI round-trip; it does not replace CI.
#
# THE MSYS2-HEADER MECHANISM
# --------------------------
# Cython-generated C #includes "Python.h". The dockcross images only ship the
# Linux *host* CPython headers, whose multiarch pyconfig.h the mingw cross-target
# rejects. The fix is to supply Windows/mingw-targeting CPython headers from MSYS2:
# we download the same `mingw-w64-<arch>-python` package the Windows CI installs
# (see packaging/MSWindows/SETUP_ci.sh: it installs `${MINGW_PACKAGE_PREFIX}-python`),
# extract its include/pythonX.Y/ tree, and point `-I` at it. That means we
# compile-check against the IDENTICAL headers CI uses.
#
#   - arm64 (CLANGARM64): mingw-w64-clang-aarch64-python  -> clangarm64/include/pythonX.Y/
#   - x64   (MINGW64):    mingw-w64-x86_64-python         -> mingw64/include/pythonX.Y/
#
# The dockcross toolchains match those MSYS2 environments:
#   - dockcross/windows-arm64       = mstorsjo/llvm-mingw clang (aarch64-w64-mingw32),
#                                     the same toolchain MSYS2 clangarm64 uses.
#   - dockcross/windows-static-x64  = mingw-w64 gcc (x86_64-w64-mingw32).
# In each container $CC is the cross-compiler.
#
# BUMPING THE PYTHON VERSION
# --------------------------
# The MSYS2 live repo keeps only the LATEST build of each package; a pinned
# version that has rolled off returns 404. If a download 404s, the script prints
# an actionable message telling you to bump the version. To bump: set PY_PKG_VER
# (and PY_MINOR if the minor changed) near the top to the current version. Find it
# by listing the repo index, e.g.:
#   curl -sL https://mirror.msys2.org/mingw/mingw64/ | grep -oE \
#     'mingw-w64-x86_64-python-[0-9][^"]*\.pkg\.tar\.zst'
# Keep PY_PKG_VER in step with what SETUP_ci.sh / windows.yml pull, so the local
# check stays aligned with CI.
#
# USAGE
# -----
#   ./tests/docker/win-compile-check.sh                       # default WIN32 .pyx set
#   ./tests/docker/win-compile-check.sh xpra/codecs/mf/decoder.pyx
#   ./tests/docker/win-compile-check.sh path/a.pyx path/b.pyx # multiple files
#
# EXIT CODES
#   0  all requested .pyx compiled clean on both arches
#   1  a genuine compile error in our code (compiler errors are printed)
#   2  usage error (bad/missing/non-.pyx argument)
#   3  infra/unavailable (docker missing, header download/extract failed) with an
#      actionable message — never reported as a code error

set -euo pipefail

# --- Tunables --------------------------------------------------------------
# Bump these together with the Windows CI Python (see "BUMPING" above).
PY_PKG_VER="3.14.6-1"   # MSYS2 mingw-python package version
PY_MINOR="3.14"         # CPython minor -> include/python${PY_MINOR}/

ARM64_IMAGE="dockcross/windows-arm64:latest"
X64_IMAGE="dockcross/windows-static-x64:latest"

MSYS2_MIRROR="https://mirror.msys2.org/mingw"
# arch key -> "subrepo:pkgprefix"
declare -A REPO_SUBPATH=( [arm64]="clangarm64" [x64]="mingw64" )
declare -A PKG_PREFIX=( [arm64]="mingw-w64-clang-aarch64-python" [x64]="mingw-w64-x86_64-python" )
declare -A DOCKER_IMAGE=( [arm64]="$ARM64_IMAGE" [x64]="$X64_IMAGE" )
# x64 safety net: official pyconfig.h only defines MS_WIN64 under _MSC_VER; MSYS2
# patches it into pyport.h, but pass it explicitly so we don't depend on that.
declare -A EXTRA_CFLAGS=( [arm64]="" [x64]="-DMS_WIN64" )

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_DIR"

# Persistent, gitignored header cache (only download/extract on a cache miss).
CACHE_DIR="$SCRIPT_DIR/.win-headers-cache"

UIDGID="$(id -u):$(id -g)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- argument handling -----------------------------------------------------
PYX_FILES=()
if [ $# -eq 0 ]; then
    # Default to the known WIN32 codec .pyx set present in the tree (glob; skip
    # absent). Keep this list in sync with WIN32-only codecs as they land.
    shopt -s nullglob
    for cand in xpra/codecs/mf/*.pyx xpra/codecs/vpl/*.pyx; do
        PYX_FILES+=("$cand")
    done
    shopt -u nullglob
    if [ ${#PYX_FILES[@]} -eq 0 ]; then
        echo "error: no default WIN32 .pyx files found; pass an explicit path" >&2
        exit 2
    fi
    echo "==> no args: checking default WIN32 .pyx set:"
    printf '      %s\n' "${PYX_FILES[@]}"
else
    for arg in "$@"; do
        if [ ! -f "$arg" ]; then
            echo "error: no such .pyx: $arg" >&2
            exit 2
        fi
        case "$arg" in
            *.pyx) ;;
            *) echo "error: argument must be a .pyx file: $arg" >&2; exit 2 ;;
        esac
        PYX_FILES+=("$arg")
    done
fi

# --- preflight: docker present ---------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "S4D-UNAVAILABLE: docker not found on PATH. Install docker to run the" >&2
    echo "  cross-compile pre-gate, or rely on Windows CI for .pyx verification." >&2
    exit 3
fi

# --- preflight: cython present ---------------------------------------------
cython_cmd() {
    if command -v cython >/dev/null 2>&1; then
        cython "$@"
    else
        "${PYTHON:-python3}" -m cython "$@"
    fi
}
if ! command -v cython >/dev/null 2>&1 && ! "${PYTHON:-python3}" -m cython --version >/dev/null 2>&1; then
    echo "S4D-UNAVAILABLE: cython not found (no 'cython' on PATH and" >&2
    echo "  '${PYTHON:-python3} -m cython' is unavailable). Install Cython." >&2
    exit 3
fi

# --- header cache ----------------------------------------------------------
# Ensure the MSYS2 mingw CPython headers for $arch are extracted under
# $CACHE_DIR/<arch>-<ver>/include/python<minor>/ and echo that include dir.
# Downloads + extracts only on a cache miss. Exits 3 on any infra failure.
ensure_headers() {
    local arch="$1"
    local subpath="${REPO_SUBPATH[$arch]}"
    local pkg="${PKG_PREFIX[$arch]}-${PY_PKG_VER}-any.pkg.tar.zst"
    local url="$MSYS2_MIRROR/$subpath/$pkg"
    local dest="$CACHE_DIR/${arch}-${PY_PKG_VER}"
    local incdir="$dest/$subpath/include/python${PY_MINOR}"

    if [ -f "$incdir/Python.h" ]; then
        echo "$incdir"
        return 0
    fi

    echo "==> [$arch] fetching MSYS2 mingw CPython headers ($PY_PKG_VER)" >&2
    local tmp="$WORK/dl-$arch"
    mkdir -p "$tmp"
    local zst="$tmp/$pkg"
    local http
    http="$(curl -sL -w '%{http_code}' -o "$zst" "$url" || true)"
    if [ "$http" = "404" ]; then
        echo "S4D-UNAVAILABLE: $url -> HTTP 404." >&2
        echo "  The MSYS2 live repo keeps only the LATEST package build, so the" >&2
        echo "  pinned PY_PKG_VER=$PY_PKG_VER has likely rolled off. Bump PY_PKG_VER" >&2
        echo "  (and PY_MINOR if the minor changed) near the top of this script to" >&2
        echo "  the current version. List it with:" >&2
        echo "    curl -sL $MSYS2_MIRROR/$subpath/ | grep -oE '${PKG_PREFIX[$arch]}-[0-9][^\"]*\\.pkg\\.tar\\.zst'" >&2
        exit 3
    fi
    if [ "$http" != "200" ] || [ ! -s "$zst" ]; then
        echo "S4D-UNAVAILABLE: failed to download headers ($url -> HTTP ${http:-?})." >&2
        echo "  Check network/mirror availability, then retry." >&2
        exit 3
    fi

    # Extract into a fresh dir, then atomically move into place so a partial
    # extraction can't be mistaken for a cache hit.
    local stage="$WORK/extract-$arch"
    mkdir -p "$stage"
    if ! tar --zstd -xf "$zst" -C "$stage" "$subpath/include/python${PY_MINOR}" 2>/dev/null; then
        # Fall back to full extract if the selective path differs.
        if ! tar --zstd -xf "$zst" -C "$stage" 2>/dev/null; then
            echo "S4D-UNAVAILABLE: failed to extract $zst (zstd/tar error)." >&2
            exit 3
        fi
    fi
    if [ ! -f "$stage/$subpath/include/python${PY_MINOR}/Python.h" ]; then
        echo "S4D-UNAVAILABLE: extracted package has no" >&2
        echo "  $subpath/include/python${PY_MINOR}/Python.h — PY_MINOR=$PY_MINOR may be" >&2
        echo "  wrong for package version $PY_PKG_VER. Bump PY_MINOR to match." >&2
        exit 3
    fi
    mkdir -p "$dest"
    rm -rf "${dest:?}/$subpath"
    mv "$stage/$subpath" "$dest/$subpath"
    echo "    cached -> $incdir" >&2
    echo "$incdir"
}

# --- cythonize each .pyx once ----------------------------------------------
# Stage generated C + sibling .h into $WORK/src so dockcross -I. resolves them.
SRC="$WORK/src"
mkdir -p "$SRC"
STEMS=()
for pyx in "${PYX_FILES[@]}"; do
    stem="$(basename "${pyx%.pyx}")"
    echo "==> cythonize $pyx"
    # -I . so cimports of xpra .pxd files resolve against the repo root.
    cython_cmd -3 --fast-fail -I . -o "$SRC/${stem}.c" "$pyx"
    echo "    generated $(wc -l < "$SRC/${stem}.c") lines of C"
    # Copy sibling C headers the .pyx pulls in (e.g. mf_decode.h) so -I. finds them.
    pyxdir="$(dirname "$pyx")"
    shopt -s nullglob
    for h in "$pyxdir"/*.h; do
        cp "$h" "$SRC/"
    done
    shopt -u nullglob
    STEMS+=("$stem")
done

# --- compile-check one arch (all stems) ------------------------------------
# Returns 0 if every stem compiled clean, 1 if any genuine compile error.
compile_arch() {
    local arch="$1"
    local incdir image extra
    incdir="$(ensure_headers "$arch")"  # may exit 3 on infra failure
    image="${DOCKER_IMAGE[$arch]}"
    extra="${EXTRA_CFLAGS[$arch]}"

    echo "==> [$arch] compile-check via $image"
    local arch_rc=0
    local stem logf
    for stem in "${STEMS[@]}"; do
        logf="$WORK/${arch}-${stem}.log"
        set +e
        docker run --rm --user "$UIDGID" \
            -v "$SRC":/work/src:ro \
            -v "$incdir":/work/pyinc:ro \
            -w /work/src \
            "$image" bash -c '
                set -e
                "$CC" -c -I. -I/work/pyinc '"$extra"' -o /dev/null '"${stem}"'.c
            ' >"$logf" 2>&1
        local rc=$?
        set -e
        if [ "$rc" -eq 0 ]; then
            echo "    PASS  $stem.c"
        else
            echo "    FAIL  $stem.c" >&2
            cat "$logf" >&2
            arch_rc=1
        fi
    done
    return "$arch_rc"
}

# --- run both arches -------------------------------------------------------
declare -A ARCH_RESULT
overall=0
for arch in arm64 x64; do
    if compile_arch "$arch"; then
        ARCH_RESULT[$arch]="PASS"
    else
        ARCH_RESULT[$arch]="FAIL"
        overall=1
    fi
done

# --- summary ---------------------------------------------------------------
echo ""
echo "==> compile-check summary (compile-only, not linked):"
for arch in arm64 x64; do
    echo "      $arch: ${ARCH_RESULT[$arch]}"
done
echo "      files: ${STEMS[*]}"

if [ "$overall" -ne 0 ]; then
    echo ""
    echo "FAIL: at least one .pyx failed to cross-compile (see errors above)." >&2
    exit 1
fi

echo ""
echo "PASS: all requested .pyx compiled clean on arm64 + x64."
echo "      (Windows CI remains the authoritative link + runtime gate.)"
