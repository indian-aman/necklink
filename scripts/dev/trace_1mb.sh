#!/usr/bin/env bash
# Fine-grained (50 ms) sender trace of one 1 MB NLTP transfer on a given link: rate mbit, rtt ms, loss %.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export NLB_TRACE_DT=0.05
/opt/nlb/bin/python $REPO/scripts/dev/debug_nltp_stall.py --rate "$1" --rtt "$2" --loss "$3" --timeout 30 2>&1 | grep -E "^TRACE|RESULT"
