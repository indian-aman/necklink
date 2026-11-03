#!/usr/bin/env bash
# One-time WSL setup for the KCP contenders in scripts/fair_transport_benchmark.py (run as root).
#   kcptun  : Ubuntu package (KCP + Reed-Solomon FEC tunnel)
#   kcp_xfer: bench_kcp/kcp_xfer.c built against KCP 2.1.1 (skywind3000/kcp, MIT)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq kcptun gcc >/dev/null
mkdir -p /opt/kcpbench/dl /opt/kcpbench/bin
cd /opt/kcpbench/dl
for f in ikcp.c ikcp.h; do
  [ -f "$f" ] || curl -fsSLO "https://raw.githubusercontent.com/skywind3000/kcp/2.1.1/$f"
done
gcc -O2 -I/opt/kcpbench/dl -o /opt/kcpbench/bin/kcp_xfer $REPO/bench_kcp/kcp_xfer.c /opt/kcpbench/dl/ikcp.c
echo "kcp bench env ok"
