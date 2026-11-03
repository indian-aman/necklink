#!/usr/bin/env bash
# One-time WSL2 setup for scripts/fair_transport_benchmark.py (run as root inside WSL).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq python3-venv iproute2 ethtool iptables >/dev/null
[ -x /opt/nlb/bin/python ] || python3 -m venv /opt/nlb
/opt/nlb/bin/pip install -q numpy cryptography aioquic zstandard pandas
/opt/nlb/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu
modprobe tcp_bbr || true
/opt/nlb/bin/python -c "import sys; sys.path.insert(0, '$REPO'); import necklink.nltp, aioquic; print('bench env ok')"
