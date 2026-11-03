#!/usr/bin/env bash
# 10 ms sender trace of a second 64 KB transfer on an open NLTP connection: rate mbit, rtt ms, loss %.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export NLB_TRACE_DT=${NLB_TRACE_DT:-0.005}
/opt/nlb/bin/python $REPO/scripts/dev/debug_nltp_stall.py --rate "$1" --rtt "$2" --loss "$3" --size 65536 --timeout 30 --warm 2>&1 | grep -E "^TRACE|RESULT"
