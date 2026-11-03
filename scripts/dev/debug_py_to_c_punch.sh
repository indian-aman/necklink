#!/usr/bin/env bash
# Re-runs only the "hole punch, Python -> C" case of virtual_nat_test.py with both sides' output.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd $REPO/scripts && /opt/nlb/bin/python - <<'PY'
import os, sys, time, json
sys.argv = ["x"]
import virtual_nat_test as V
V.BLOB.write_bytes(os.urandom(1024 * 1024))
V.build(symmetric=False)
svc = [V.Proc(V.spawn("vpub", V.py_cmd(r))) for r in ("stun-server", "relay")]
for s in svc: s.wait_for("READY", 15)
stun = ["--stun", f"{V.PUB_IP}:{V.STUN_PORT}", "--punch"]
rx = V.Proc(V.spawn("vhostB", V.c_cmd("recv", "/tmp/vnat_rx.bin", stun)))
tx = V.Proc(V.spawn("vhostA", V.py_cmd("py-send", "punch", "")))
w_rx, w_tx = rx.wait_for("WAN "), tx.wait_for("WAN ")
print("rx WAN", w_rx, "| tx WAN", w_tx)
rx.write("PEER " + w_tx.split()[1]); tx.write("PEER " + w_rx.split()[1])
time.sleep(12)
print("C receiver :", rx.lines[-6:])
print("Py sender  :", tx.lines[-6:])
print(V.nsx("vrtrA", "conntrack -L -p udp 2>/dev/null | head -5", check=False) or V.nsx("vrtrA", "cat /proc/net/nf_conntrack 2>/dev/null | head -5", check=False))
for p in (rx, tx, *svc): p.kill()
V.teardown()
PY
