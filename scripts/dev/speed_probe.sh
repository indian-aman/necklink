#!/usr/bin/env bash
# NLTP throughput ceiling on fast clean links (8 MB, 10 ms RTT, no loss), default settings.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for r in 50 200; do
  echo "== link ${r} Mbit"
  /opt/nlb/bin/python $REPO/scripts/dev/debug_nltp_stall.py --rate "$r" --rtt 10 --loss 0 --size 8388608 --timeout 60 2>&1 \
    | grep -E "RESULT|TRACE" | tail -3 | cut -c1-260
done
