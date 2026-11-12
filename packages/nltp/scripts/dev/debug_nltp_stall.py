"""
Reproduces an NLTP transfer over a netem link (WSL/Linux, root) and traces the sender's state every 0.5 s.

  wsl -u root -- /opt/nlb/bin/python scripts/dev/debug_nltp_stall.py --rate 2 --rtt 300 --loss 20 --cap 1800
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fair_transport_benchmark as F  # noqa: E402

TRACE_DT = float(os.environ.get("NLB_TRACE_DT", "0.5"))


def client_trace(port: int, path: str, cap: float | None, timeout: float, warm: bool = False) -> None:
    from necklink.nltp import NLTPClient

    kw = {} if cap is None else {"max_tx_kbps": cap}
    with NLTPClient((F.IP_B, port), **kw) as cli:
        stop = threading.Event()

        def tracer() -> None:
            t0 = time.perf_counter()
            while not stop.is_set():
                with cli._lock:
                    e = cli.engine
                    xs = [x for x in e.outbound.values() if not x.completed]
                    x = xs[0] if xs else None
                    row = {
                        "t": round(time.perf_counter() - t0, 2),
                        "pkts_tx": e.pkts_tx, "pkts_rx": e.pkts_rx,
                        "srtt": round(e.srtt_ms, 1), "min_rtt": round(e.min_rtt_ms, 1),
                        "loss_ema": round(e.peer_loss_ema, 3), "win": e._effective_window_generations(),
                        "tokens": round(e.pacer.tokens, 0) if e.pacer else None,
                    }
                    if e.rate_ctl is not None:
                        rc = e.rate_ctl
                        row.update({"rate_kbps": round(rc.rate_kbps), "mode": "startup" if rc.mode == rc.STARTUP else "cruise",
                                    "deliv_kbps": round(rc.last_deliv_kbps), "queue_ms": round(rc.last_queue_ms, 1), "btlbw": round(rc.btlbw_kbps()), "p_rand": round(rc.random_loss, 3)})
                    if x is not None:
                        g = x.generations
                        row.update({
                            "base": x.base_unacked_gen, "n_gens": len(g),
                            "sent_sys_gens": sum(1 for q in g if q.next_sys_idx >= q.k_shards),
                            "pending_dof": dict(list(x.pending_dof_requests.items())[:4]),
                            "base_rif": g[x.base_unacked_gen].repair_in_flight if x.base_unacked_gen < len(g) else None,
                            "meta_acked": x.meta_acked,
                            "last_gen": [g[-1].next_sys_idx, g[-1].k_shards, g[-1].proactive_target, g[-1].proactive_sent,
                                         g[-1].proactive_done, g[-1].acked_complete],
                        })
                print("TRACE " + json.dumps(row), flush=True)
                time.sleep(TRACE_DT)

        if warm:
            cli.send_file(path, timeout=timeout)
            time.sleep(0.2)
            print("WARM-DONE", flush=True)
        th = threading.Thread(target=tracer, daemon=True)
        th.start()
        t0 = time.perf_counter()
        rcpt = cli.send_file(path, timeout=timeout)
        stop.set()
        print("RESULT " + json.dumps({"ok": rcpt["verified_sha256"], "elapsed_s": time.perf_counter() - t0}), flush=True)


def server_trace(port: int, cap: float | None, timeout: float) -> None:
    from necklink.nltp import NLTPServer

    kw = {} if cap is None else {"max_tx_kbps": cap}
    with NLTPServer((F.IP_B, port), save_dir="/tmp/nlb_inbox", **kw) as srv:
        print("READY", flush=True)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout + 5:
            with srv._lock:
                e = srv.engine
                st = next(iter(e.inbound.values()), None)
                row = {"t": round(time.perf_counter() - t0, 2), "pkts_rx": e.pkts_rx, "pkts_tx": e.pkts_tx,
                       "auth_drops": e.auth_drops_rx, "worker_alive": srv._worker.is_alive()}
                if st is not None:
                    row.update({"base_gen": st.base_gen, "solved": len(st.solved_gens), "open_gens": len(st.gens),
                                "max_gen_seen": st.max_gen_seen, "shards_rcvd": st.shards_rcvd_count,
                                "completed": st.completed, "pending_acks": len(e._pending_acks)})
            print("STRACE " + json.dumps(row), file=sys.stderr, flush=True)
            time.sleep(TRACE_DT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2)
    ap.add_argument("--rtt", type=float, default=300)
    ap.add_argument("--loss", type=float, default=20)
    ap.add_argument("--mode", default="random")
    ap.add_argument("--jitter", type=float, default=0.0, help="netem delay jitter in ms (causes reordering)")
    ap.add_argument("--cap", type=float, default=None, help="max_tx_kbps for both ends (omit for library default)")
    ap.add_argument("--size", type=int, default=1024 * 1024)
    ap.add_argument("--timeout", type=float, default=40)
    ap.add_argument("--role", default="orchestrate")
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--path", default="")
    ap.add_argument("--warm", action="store_true", help="send once untimed, then trace a second transfer")
    a = ap.parse_args()
    if a.role == "client":
        client_trace(a.port, a.path, a.cap, a.timeout, a.warm)
        return
    if a.role == "server":
        server_trace(a.port, a.cap, a.timeout)
        return

    F.BLOB_DIR.mkdir(parents=True, exist_ok=True)
    blob = F.BLOB_DIR / f"dbg_{a.size}.bin"
    blob.write_bytes(os.urandom(a.size))
    F.setup_topology()
    try:
        F.set_link(a.rate, a.rtt, a.loss, a.mode, {"jitter_ms": a.jitter} if a.jitter else None)
        cap_args = [] if a.cap is None else ["--cap", str(a.cap)]
        srv = subprocess.Popen(["ip", "netns", "exec", F.NS_B, F.PY, __file__, "--role", "server", "--port", str(a.port),
                                "--timeout", str(a.timeout), *cap_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        F._wait_ready(srv)
        cli_args = [F.PY, __file__, "--role", "client", "--port", str(a.port), "--path", str(blob), "--timeout", str(a.timeout), *cap_args]
        if a.warm:
            cli_args.append("--warm")
        cli = subprocess.Popen(["ip", "netns", "exec", F.NS_A, *cli_args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in cli.stdout:
            print(line.rstrip(), flush=True)
        cli.wait()
        srv.kill()
        _out, err = srv.communicate()
        print("SERVER STDERR:\n" + (err[-3000:] or "(empty)"), flush=True)
    finally:
        F.teardown_topology()


if __name__ == "__main__":
    main()
