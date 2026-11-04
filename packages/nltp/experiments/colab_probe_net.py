"""Probe a Colab VM for what scripts/fair_transport_benchmark.py needs (root, netns, veth, netem, BBR)."""
import subprocess


def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    print("$", c, "->", r.returncode, (r.stdout + r.stderr).strip()[:300])


sh("id; uname -r; nproc; nvidia-smi --query-gpu=name --format=csv,noheader")
sh("which tc ip iptables ethtool iperf3")
sh("ip netns add t1 && echo NETNS_OK; ip netns del t1")
sh("ip link add vx type veth peer name vy && echo VETH_OK; ip link del vx")
sh("tc qdisc add dev lo root netem delay 5ms && echo NETEM_OK; tc qdisc del dev lo root")
sh("sysctl net.ipv4.tcp_available_congestion_control")
sh("grep CapEff /proc/self/status")
