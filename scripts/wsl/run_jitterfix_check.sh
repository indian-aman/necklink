#!/usr/bin/env bash
# Regression check after the jitter fix: standard links + jitter/reorder links, NLTP default only.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY=/opt/nlb/bin/python
B=$REPO/scripts/fair_transport_benchmark.py
R=$REPO/results
$PY $B --protocols nltp-default --tag jitterfix --fairness no --timeout 30 > $R/fair_transport_benchmark_jitterfix.log 2>&1
$PY $B --extended --conditions jitter_10M_100ms_5pct_j20 reorder_10M_60ms_2pct_r10 --protocols nltp-default --timeout 30 \
       --tag jitterfix_ext --fairness no > $R/fair_transport_benchmark_jitterfix_ext.log 2>&1
echo done
