#!/usr/bin/env bash
# Final NLTP numbers after closing the small-file gap: warm 64 KB (5 runs) + cold default, both sizes (3 runs).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY=/opt/nlb/bin/python
B=$REPO/scripts/fair_transport_benchmark.py
R=$REPO/results
$PY $B --protocols nltp-warm --sizes 65536 --reps 5 --tag warm64 --fairness no > $R/fair_transport_benchmark_warm64.log 2>&1
$PY $B --protocols nltp-default --tag tuned3 --fairness no > $R/fair_transport_benchmark_tuned3.log 2>&1
echo done
