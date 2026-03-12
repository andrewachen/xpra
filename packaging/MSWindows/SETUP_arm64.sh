#!/bin/bash
# ABOUTME: MSYS2 CLANGARM64 dependency installer for building Xpra ARM64 Windows client.
# ABOUTME: Skips CUDA/NVIDIA, Kerberos/GSSAPI, and AES packages not needed for plain TCP.
#
# Run inside a CLANGARM64 MSYS2 shell (MSYSTEM=CLANGARM64 must be set).
# On failure of optional packages a warning is printed and the install continues.

set -e

XPKG="${MINGW_PACKAGE_PREFIX}-"
PACMAN="pacman --noconfirm --needed -S"

# Core: Python runtime, GTK3 UI toolkit, desktop notifications
$PACMAN ${XPKG}python ${XPKG}libnotify ${XPKG}gtk3

# Media libraries (libspng present in 6.4 branch; harmless on master)
$PACMAN ${XPKG}libspng ${XPKG}libavif ${XPKG}gst-plugins-good ${XPKG}gst-plugins-bad ${XPKG}gst-plugins-ugly

# libyuv: prefer the -git package, fall back to stable, skip if neither exists
$PACMAN ${XPKG}libyuv-git || $PACMAN ${XPKG}libyuv || echo "Warning: libyuv not available for clangarm64, skipping"

# Network layer (openssh/sshpass not installed — TCP-only client, no SSH transport)
$PACMAN ${XPKG}lz4 ${XPKG}xxhash heimdal-libs ${XPKG}libsodium

# pinentry: not available on all clangarm64 repositories yet
$PACMAN ${XPKG}pinentry || echo "Warning: pinentry not available for clangarm64, skipping"

$PACMAN ${XPKG}dbus-glib
$PACMAN ${XPKG}gst-python

# Build toolchain
$PACMAN base-devel ${XPKG}yasm ${XPKG}nasm gcc groff subversion rsync zip gtk-doc git \
        ${XPKG}cmake ${XPKG}gcc ${XPKG}pkgconf ${XPKG}libffi ${XPKG}python-pandocfilters

# Python extension packages.
# nvidia-ml: NVIDIA GPU management — no NVIDIA on ARM, skip.
# winkerberos: Kerberos auth — not needed for plain TCP password auth, skip.
# amf-headers: AMD GPU capture — irrelevant for a remote desktop client, skip.
for x in cryptography cffi pycparser numpy pillow appdirs paramiko comtypes netifaces \
          setproctitle pyu2f ldap ldap3 bcrypt pynacl pyopengl pyopengl-accelerate \
          zeroconf certifi yaml py-cpuinfo coverage psutil oauthlib pysocks pyopenssl \
          importlib_resources pylsqpack aioquic service_identity pyvda watchdog \
          pyqt6 wmi winloop pyglet; do
    $PACMAN ${XPKG}python-${x} || echo "Warning: python-${x} not in clangarm64 repo, skipping"
done

# cx_Freeze: bundles Python + all extensions into a standalone dist/ tree.
# Try the pacman package first; fall back to pip which ships ARM64 Windows wheels
# for cx_Freeze 7.x via PyPI.
if ! $PACMAN ${XPKG}python-cx-freeze 2>/dev/null; then
    echo "cx_Freeze not in pacman for clangarm64 — installing via pip (ARM64 wheel)..."
    pip3 install --break-system-packages "cx_Freeze>=7.0"
fi

# gssapi: Kerberos auth — not needed for plain TCP password auth, skip entirely.
# pycryptodome / pycryptodomex: AES encryption — not needed for unencrypted TCP, skip.
# pdfium: remote printing — not needed, skip.
# openssh/putty/paexec: SSH transport, remote exec — TCP-only client, skip.
# openssl CLI: cert management tool — not needed, SSL libraries are bundled separately.

# Remaining Python build deps; pip fallback for anything not yet ported.
for x in mako markupsafe typing_extensions platformdirs pip keyring idna; do
    $PACMAN ${XPKG}python-${x} || pip3 install --break-system-packages "$x"
done

$PACMAN ${XPKG}cython
$PACMAN openssl-devel || true

# pip-only packages (pytools excluded: it's a pycuda dep and pulls in siphash24 which has no ARM64 wheel)
for x in browser-cookie3 pyaes pbkdf2; do
    pip3 install --break-system-packages "$x"
done

echo "CLANGARM64 setup complete."
