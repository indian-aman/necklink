#!/usr/bin/env bash
# Sender + receiver trace of a 1 MB NLTP transfer on the jitter link that stalled in the extended benchmark.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export NLB_TRACE_DT=0.25
/opt/nlb/bin/python $REPO/scripts/dev/debug_nltp_stall.py --rate 10 --rtt 100 --loss 5 --jitter 20 --timeout 20 2>&1 | grep -E "^TRACE|^STRACE|RESULT"
