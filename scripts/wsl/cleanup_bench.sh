#!/usr/bin/env bash
# Stops any running benchmark/trace processes and removes the test namespaces.
pkill -f "fair_transport_benchmark[.]py" ; pkill -f "debug_nltp_stall[.]py" ; pkill -f "kcp_xfer" ; pkill -f "kcptun-"
sleep 1
ip netns del nla 2>/dev/null; ip netns del nlb 2>/dev/null
echo "namespaces left: $(ip netns list | wc -l)"
