#!/usr/bin/env bash
# Lists which candidate transports are installable from the Ubuntu archive.
for pk in srt-tools uftp libudt-dev libudt0 tsunami-udp ngtcp2-client libngtcp2-dev libmsquic python3-aioquic cargo golang-go rustc; do
  cand=$(apt-cache policy "$pk" 2>/dev/null | awk '/Candidate:/ {print $2}')
  printf "%-18s %s\n" "$pk" "${cand:-not in archive}"
done
