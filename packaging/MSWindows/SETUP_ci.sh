#!/bin/bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: MSYS2 dependency installer for CI builds of the Xpra Windows client.
# ABOUTME: Skips CUDA/NVIDIA, Kerberos/GSSAPI, and AES packages not needed for plain TCP.
#
# Works with any MSYS2 msystem (MINGW64, CLANGARM64, etc.) — package names are
# derived from $MINGW_PACKAGE_PREFIX which MSYS2 sets per-environment.
# On failure of optional packages a warning is printed and the install continues.

set -e

XPKG="${MINGW_PACKAGE_PREFIX}-"
PACMAN="pacman --noconfirm --needed -S"

# Architecture-specific compiler flags for all C/C++ compilation
# (Cython extensions, libsonic, etc.):
if [ "$MSYSTEM_CARCH" = "aarch64" ]; then
    export CFLAGS="-mcpu=oryon-1 -O2"
    export CXXFLAGS="-mcpu=oryon-1 -O2"
else
    export CFLAGS="-march=native -O2"
    export CXXFLAGS="-march=native -O2"
fi
echo "Compiler flags: CFLAGS=${CFLAGS}"

# Full system update before installing packages.
# The workflow caches the entire msys64 prefix, so this only runs on cache miss.
pacman --noconfirm -Syu

# Core: Python runtime, GTK3 UI toolkit, desktop notifications
$PACMAN ${XPKG}python ${XPKG}libnotify ${XPKG}gtk3

# Media libraries (libspng present in 6.4 branch; harmless on master)
$PACMAN ${XPKG}libspng ${XPKG}libavif ${XPKG}gst-plugins-good ${XPKG}gst-plugins-bad ${XPKG}gst-plugins-ugly

# libyuv: prefer the -git package, fall back to stable, skip if neither exists
$PACMAN ${XPKG}libyuv-git || $PACMAN ${XPKG}libyuv || echo "Warning: libyuv not available, skipping"

# Network layer (openssh/sshpass not installed — TCP-only client, no SSH transport)
$PACMAN ${XPKG}lz4 ${XPKG}xxhash heimdal-libs ${XPKG}libsodium

$PACMAN ${XPKG}pinentry || echo "Warning: pinentry not available, skipping"

$PACMAN ${XPKG}dbus-glib
$PACMAN ${XPKG}gst-python

# Build toolchain
# git is needed by add_build_info.py for version/revision stamping
$PACMAN base-devel gcc git zip ${XPKG}gcc ${XPKG}pkgconf ${XPKG}libffi

# Python extension packages.
# nvidia-ml: NVIDIA GPU management — CI has no GPU, skip.
# winkerberos: Kerberos auth — not needed for plain TCP password auth, skip.
# amf-headers: AMD GPU capture — irrelevant for a remote desktop client, skip.
for x in cryptography cffi pycparser numpy pillow appdirs paramiko comtypes netifaces \
          setproctitle pyu2f ldap ldap3 bcrypt pynacl pyopengl pyopengl-accelerate \
          zeroconf certifi yaml py-cpuinfo coverage psutil oauthlib pysocks pyopenssl \
          importlib_resources pylsqpack aioquic service_identity pyvda watchdog \
          pyqt6 wmi winloop pyglet; do
    $PACMAN ${XPKG}python-${x} || echo "Warning: python-${x} not available, skipping"
done

# cx_Freeze: bundles Python + all extensions into a standalone dist/ tree.
if ! $PACMAN ${XPKG}python-cx-freeze 2>/dev/null; then
    echo "cx_Freeze not in pacman — installing via pip..."
    pip3 install "cx_Freeze>=7.0"
fi

# gssapi: Kerberos auth — not needed for plain TCP password auth, skip entirely.
# pycryptodome / pycryptodomex: AES encryption — not needed for unencrypted TCP, skip.
# pdfium: remote printing — not needed, skip.
# openssh/putty/paexec: SSH transport, remote exec — TCP-only client, skip.
# openssl CLI: cert management tool — not needed, SSL libraries are bundled separately.

# Remaining Python build deps; pip fallback for anything not yet ported.
for x in mako markupsafe typing_extensions platformdirs pip keyring idna; do
    $PACMAN ${XPKG}python-${x} || pip3 install "$x"
done

$PACMAN ${XPKG}cython
$PACMAN openssl-devel || true

# pip-only packages (pytools excluded: it's a pycuda dep, not needed for client)
for x in browser-cookie3 pyaes pbkdf2; do
    pip3 install "$x"
done

# verpatch: stamps version info into the installer EXE.
# x86 binary — runs natively on x64, under emulation on ARM64.
$PACMAN unzip
curl -sL https://api.nuget.org/v3-flatcontainer/verpatch/1.0.14/verpatch.1.0.14.nupkg -o /tmp/verpatch.nupkg
unzip -oj /tmp/verpatch.nupkg lib/win/verpatch.exe -d "$MINGW_PREFIX/bin/"
rm /tmp/verpatch.nupkg

# libsonic: audio tempo control for jitter buffer (https://github.com/waywardgeek/sonic)
# Single C file, Apache 2.0 license, no dependencies.
# Pinned to a specific commit for reproducible builds.
SONIC_COMMIT="b93885d"
echo "libsonic: building from waywardgeek/sonic@${SONIC_COMMIT} (${MSYSTEM_CARCH})"
curl -sL "https://raw.githubusercontent.com/waywardgeek/sonic/${SONIC_COMMIT}/sonic.c" -o /tmp/sonic.c
curl -sL "https://raw.githubusercontent.com/waywardgeek/sonic/${SONIC_COMMIT}/sonic.h" -o /tmp/sonic.h
${MINGW_PREFIX}/bin/gcc -shared -O2 -I/tmp -o "${MINGW_PREFIX}/bin/libsonic.dll" /tmp/sonic.c
rm /tmp/sonic.c /tmp/sonic.h

# Clean pacman's download cache to reduce the size of the CI cache archive.
# Installed packages are unaffected — this only removes the .pkg.tar.zst files.
pacman --noconfirm -Scc

echo "${MSYSTEM} CI setup complete."
