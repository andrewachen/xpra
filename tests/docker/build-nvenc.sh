#!/bin/bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Compiles all xpra Cython modules in Docker with -march=native and copies .so files out.
# ABOUTME: Usage: ./tests/docker/build-nvenc.sh [--deploy]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="xpra-nvenc-build"
OUT_DIR="$REPO_DIR/build-nvenc-out"
DEPLOY=false

if [ "$1" = "--deploy" ]; then
    DEPLOY=true
fi

if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Docker image '$IMAGE_NAME' not found. Build it first with:" >&2
    echo "  docker build -t $IMAGE_NAME -f $SCRIPT_DIR/Dockerfile.nvenc $SCRIPT_DIR" >&2
    exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

echo "Compiling all Cython modules (-march=native)..."
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$REPO_DIR:/xpra:ro" \
    -v "$OUT_DIR:/out" \
    --tmpfs /tmp:exec \
    "$IMAGE_NAME" \
    bash -c '
        set -e
        cp -a /xpra /tmp/build 2>/dev/null || true
        cd /tmp/build
        CFLAGS="-march=native -O2" python3 setup.py build_ext \
            --with-nvidia \
            --with-nvenc \
            --without-cuda_kernels \
            --without-cuda_rebuild \
            --without-nvjpeg_encoder \
            --without-nvjpeg_decoder \
            --without-nvdec \
            --without-nvfbc
        # Preserve directory structure so deploy can mirror it
        cd build
        LIBDIR=$(find . -name "lib.*" -type d | head -1)
        if [ -z "$LIBDIR" ]; then
            echo "ERROR: no lib directory found in build/" >&2
            exit 1
        fi
        cd "$LIBDIR"
        find . -name "*.so" | while read f; do
            mkdir -p "/out/$(dirname "$f")"
            cp "$f" "/out/$f"
        done
        echo "Built $(find /out -name "*.so" | wc -l) modules"
    '

echo ""
echo "Output in: $OUT_DIR/"
find "$OUT_DIR" -name "*.so" | wc -l
echo "modules built"

if [ "$DEPLOY" = true ]; then
    INSTALLED="/usr/lib/python3/dist-packages"
    DEST="$HOME/.local/xpra-fixes"
    # Only deploy .so files that differ from the installed versions
    count=0
    find "$OUT_DIR" -name "*.so" | while read f; do
        relpath="${f#$OUT_DIR/}"
        installed="$INSTALLED/$relpath"
        destfile="$DEST/$relpath"
        if [ -f "$installed" ]; then
            mkdir -p "$(dirname "$destfile")"
            cp "$f" "$destfile"
            count=$((count + 1))
        fi
    done
    echo ""
    echo "Deployed to: $DEST/"
    find "$DEST" -name "*.so" | wc -l
    echo "modules deployed"
fi
