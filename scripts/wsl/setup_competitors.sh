#!/usr/bin/env bash
# Installs extra transports for the comparison from the Ubuntu archive (run as root in WSL).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq srt-tools uftp libudt-dev g++ ngtcp2-client ngtcp2-server >/dev/null
for b in srt-file-transmit srt-live-transmit uftp uftpd; do printf "%-20s %s\n" "$b" "$(command -v $b || echo missing)"; done
dpkg -L ngtcp2-client ngtcp2-server | grep -E "/bin/" || true
ls /usr/include/udt* 2>/dev/null | head -3; ls /usr/lib/x86_64-linux-gnu/libudt* 2>/dev/null | head -3
