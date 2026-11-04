#!/usr/bin/env bash
# Both sides of the first 0.4 s of a 1 MB NLTP transfer: rate mbit, rtt ms, loss %.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export NLB_TRACE_DT=0.02
/opt/nlb/bin/python $REPO/scripts/dev/debug_nltp_stall.py --rate "$1" --rtt "$2" --loss "$3" --timeout 30 2>&1 | grep -E "^TRACE|^STRACE|RESULT"
