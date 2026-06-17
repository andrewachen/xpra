#!/bin/bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Builds xpra .deb packages in Docker against a persistent host-side cache for incremental rebuilds.
# ABOUTME: Usage: ./tests/docker/build-deb.sh [--deploy]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="xpra-deb-build"
CACHE_DIR="$REPO_DIR/build-deb-cache"
OUT_DIR="$REPO_DIR/build-deb-out"
DEPLOY=false

# Only these .deb files get copied to OUT_DIR. Headless server install set per
# memory + Andrew's "linux client pointless" decision. Adjust as needed.
KEEP_DEBS=(
    xpra
    xpra-common
    xpra-server
    xpra-x11
    xpra-codecs
    xpra-codecs-nvidia
    xpra-audio
    xpra-audio-server
    # xpra-client + xpra-client-gtk3 are pulled in by the xpra meta-package's
    # Depends, so they must be installed at the same version. They're small
    # and harmless on a headless server.
    xpra-client
    xpra-client-gtk3
)

if [ "$1" = "--deploy" ]; then
    DEPLOY=true
fi

NVENC_IMAGE="xpra-nvenc-build"
# Auto-build base image if missing. To force a rebuild (e.g. after editing
# the Dockerfile), run: docker rmi $NVENC_IMAGE
if ! docker image inspect "$NVENC_IMAGE" >/dev/null 2>&1; then
    echo "Building $NVENC_IMAGE image (one-time, ~5-8 min)..."
    docker build -t "$NVENC_IMAGE" -f "$SCRIPT_DIR/Dockerfile.nvenc" "$SCRIPT_DIR"
fi

# Auto-build deb image if missing. To force a rebuild: docker rmi $IMAGE_NAME
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building $IMAGE_NAME image (one-time, ~2-3 min)..."
    docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.deb" "$SCRIPT_DIR"
fi

# Cuda-kernels enablement requires nvcc in the deb image. An older cached
# $IMAGE_NAME (or one whose base $NVENC_IMAGE was rebuilt without also
# rebuilding the deb image) would silently be reused and fail later inside
# debuild with the unhelpful "rebuilding XRGB_to_NV12: no file" message.
# Probe the deb image (which is what actually runs the build) and rebuild
# both it and its base if nvcc is missing.
if ! docker run --rm "$IMAGE_NAME" which nvcc >/dev/null 2>&1; then
    echo "Rebuilding images: $IMAGE_NAME (or its base) is missing nvcc..."
    docker rmi "$IMAGE_NAME" "$NVENC_IMAGE" >/dev/null 2>&1 || true
    docker build -t "$NVENC_IMAGE" -f "$SCRIPT_DIR/Dockerfile.nvenc" "$SCRIPT_DIR"
    docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.deb" "$SCRIPT_DIR"
fi

GIT_SHA=$(git -C "$REPO_DIR" rev-parse --short HEAD)
GIT_BRANCH=$(git -C "$REPO_DIR" branch --show-current)
GIT_LOCAL_MODS=$(git -C "$REPO_DIR" diff --shortstat 2>/dev/null | wc -l)
DIST=$(lsb_release -cs)
# Derive the base version from the source tree (xpra/__init__.py __version__)
# so the package version tracks the branch's actual release base (6.5, 6.5.x,
# ...) instead of a hardcoded value.
BASE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_DIR/xpra/__init__.py")
# Sorts above xpra-org's "<base>-r0-1" naming so apt prefers our build.
VERSION="${BASE_VERSION}-achen-${GIT_SHA}~${DIST}"

mkdir -p "$CACHE_DIR" "$OUT_DIR"
rm -f "$OUT_DIR"/*.deb "$OUT_DIR"/*.buildinfo "$OUT_DIR"/*.changes 2>/dev/null || true

echo "Building xpra .deb (version: $VERSION)"
echo "Cache dir: $CACHE_DIR  (delete to force clean rebuild)"
echo ""

docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$REPO_DIR:/xpra:ro" \
    -v "$CACHE_DIR:/cache" \
    -v "$OUT_DIR:/out" \
    -e GIT_SHA="$GIT_SHA" \
    -e GIT_BRANCH="$GIT_BRANCH" \
    -e GIT_LOCAL_MODS="$GIT_LOCAL_MODS" \
    -e VERSION="$VERSION" \
    "$IMAGE_NAME" \
    bash -c '
        set -e
        SRC=/cache/xpra-src
        mkdir -p "$SRC"

        # Sync source from read-only mount into cache. --delete handles file
        # removals (renames, deletes). --checksum compares file content (md5)
        # instead of size+mtime, so cache .pyx files keep their old mtimes when
        # content is unchanged. Without this, a fresh git checkout updates
        # source mtimes to "now" and Cython recompiles every module even
        # though no content changed. Cost: ~1-2s to checksum ~50MB of source;
        # win: 5-10 min saved on incremental rebuilds.
        rsync -a --delete --checksum \
            --exclude=".git/" \
            --exclude=".claude/" \
            --exclude=".codex-reviews/" \
            --exclude="build-deb-cache/" \
            --exclude="build-deb-out/" \
            --exclude="build-nvenc-out/" \
            --exclude="__pycache__/" \
            --exclude="test-file-auth-*" \
            --exclude="*.deb" \
            --exclude="*.buildinfo" \
            --exclude="*.changes" \
            --exclude="*.fatbin" \
            /xpra/ "$SRC/"

        cd "$SRC"

        # Write src_info.py with branch/commit captured from the host before
        # rsync stripped .git/. setup.py only writes this file if it does not
        # already exist (see fs/bin/add_build_info.py check_file guard), so
        # this overrides what would otherwise be "unknown" or a stale value
        # cached from a prior build. End result: `xpra info build.branch`
        # reflects the actual built tree.
        cat > xpra/src_info.py <<EOF
BRANCH = "${GIT_BRANCH}"
COMMIT = "${GIT_SHA}"
LOCAL_MODIFICATIONS = ${GIT_LOCAL_MODS:-0}
REVISION = 0
EOF

        # Drop --with-qt6_client (Andrew does not use the Qt6 client).
        # Disable nvfbc/nvdec/nvjpeg — only nvenc is wanted, and
        # nvfbc/nvjpeg need libnvidia-fbc1 which we do not stub.
        # Keep cuda_kernels: nvenc loads BGRX_to_{NV12,YUV444}.fatbin at
        # runtime for non-NATIVE_RGB encode paths.
        # idempotent: re-running on a clean cache produces the same file.
        cp debian/rules debian/rules.orig 2>/dev/null || true
        cp debian/rules.orig debian/rules 2>/dev/null || true
        sed -i \
            -e "s/ --with-qt6_client//" \
            -e "s|^BUILDOPTS := \$(EXTRA_BUILDOPTS).*|BUILDOPTS := \$(EXTRA_BUILDOPTS) --without-qt6_client --without-pyglet_client --without-amf --without-nvdec --without-nvfbc --without-nvjpeg_encoder --without-nvjpeg_decoder --without-docs --without-pandoc_lua|" \
            debian/rules

        # --without-docs skips generating /usr/share/doc/xpra/ but
        # xpra-common.files still references it, which fails dh_movefiles.
        # Strip the doc directory entry from the package manifest.
        sed -i "\|usr/share/doc/xpra/|d" debian/xpra-common.files

        # Prepend a changelog entry so the resulting .deb gets our version
        # string. Writing the entry directly (vs dch) avoids needing tty/env.
        TS=$(date -R)
        NEW_ENTRY="xpra (${VERSION}) UNRELEASED; urgency=low\n\n  * Build from ${GIT_BRANCH} ${GIT_SHA}\n\n -- ${DEBFULLNAME} <${DEBEMAIL}>  ${TS}\n\n"
        printf "$NEW_ENTRY" > /tmp/changelog.new
        cat debian/changelog >> /tmp/changelog.new
        mv /tmp/changelog.new debian/changelog

        # Clear stale .deb / .buildinfo / .changes from prior runs in the
        # cache parent dir (where debuild writes its output). Otherwise
        # every rebuild leaves the previous versions behind and the cp
        # loop below copies all of them to /out, piling up across runs.
        rm -f ../*.deb ../*.buildinfo ../*.changes 2>/dev/null || true

        # Build binary packages only (-b), no source archive.
        # -us -uc: skip signing source / changes (no GPG key in container).
        # -d: skip checking Build-Depends (we baked them into the image).
        # -nc: skip the pre-build clean (preserves build/temp.* .o files for
        #      incremental rebuilds). Without this, dh_auto_clean runs
        #      `setup.py clean` which wipes object files and forces full
        #      gcc recompilation even when nothing changed.
        # -j$(nproc): parallel build across all cores.
        # DEB_BUILD_OPTIONS=nostrip: skip dh_strip. Keeps full symbol tables
        # in the installed Cython .so files so gdb on a core dump resolves
        # nvenc/codec frames to real function names (__pyx_pf_..._compress_image
        # etc.) instead of `??`. Cost: +20-50 MB across all codec .debs;
        # zero runtime cost. Counterpart for build-nvenc.sh: distutils
        # already produces unstripped .so by default.
        DEB_BUILD_OPTIONS=nostrip debuild -nc -b -us -uc -d -j$(nproc)

        # Copy only the .deb files Andrew actually installs. Quiet on misses
        # so an empty xpra-client-qt6 stanza does not break the script.
        cd ..
        for pkg in '"${KEEP_DEBS[*]}"'; do
            for f in "${pkg}"_*.deb; do
                if [ -f "$f" ]; then
                    cp -v "$f" /out/
                fi
            done
        done
    '

echo ""
echo "Output in: $OUT_DIR/"
ls -la "$OUT_DIR"/*.deb 2>/dev/null | wc -l | xargs echo "Packages built:"

# Always print the install command so you can install later without re-running
# the build. Use the actual filenames so it works even if KEEP_DEBS changes.
DEB_FILES=("$OUT_DIR"/*.deb)
if [ -e "${DEB_FILES[0]}" ]; then
    echo ""
    echo "To install:"
    echo "  sudo dpkg -i ${DEB_FILES[*]}"
fi

if [ "$DEPLOY" = true ]; then
    echo ""
    echo "Installing with sudo dpkg -i..."
    sudo dpkg -i "$OUT_DIR"/*.deb
    echo ""
    echo "Installed versions:"
    dpkg-query -W -f='${Package} ${Version}\n' "${KEEP_DEBS[@]}" 2>/dev/null
fi
