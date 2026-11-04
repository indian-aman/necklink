#!/usr/bin/env bash
# Finishes the KCP comparison: missing 1 MB cells for KCP fast + kcptun, then KCP fast with a 128 window.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY=/opt/nlb/bin/python
B=$REPO/scripts/fair_transport_benchmark.py
R=$REPO/results
$PY $B --protocols kcp-fast kcptun --conditions radio_5M_200ms_10pct bad_2M_300ms_20pct burst_10M_100ms_10pct \
       --sizes 1048576 --tag kcp_rest --fairness no > $R/fair_transport_benchmark_kcp_rest.log 2>&1
$PY $B --protocols kcp-fast128 --tag kcp128 --fairness no > $R/fair_transport_benchmark_kcp128.log 2>&1
echo done
