#!/usr/bin/env bash
# Extended NLTP testing: harder links (jitter/reorder/GEO/asymmetric) incl. encrypted NLTP, then 10 MB files.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY=/opt/nlb/bin/python
B=$REPO/scripts/fair_transport_benchmark.py
R=$REPO/results
$PY $B --extended --protocols nltp-default nltp-enc tcp-bbr quic-ngtcp2-bbr kcptun kcp-fast128 --timeout 30 \
       --tag extended --fairness no > $R/fair_transport_benchmark_extended.log 2>&1
$PY $B --conditions clean_20M_20ms cell_10M_100ms_5pct burst_10M_100ms_10pct --sizes 10485760 \
       --protocols nltp-default tcp-bbr quic-ngtcp2-bbr kcptun --timeout 60 --tag 10mb --fairness no > $R/fair_transport_benchmark_10mb.log 2>&1
echo done
