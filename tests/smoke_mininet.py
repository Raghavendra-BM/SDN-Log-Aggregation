"""
Smoke test: does the whole system work inside Mininet?   (needs root and a running controller)

    terminal 1:  SDN_MODE=dynamic ryu-manager sdn/controller.py
    terminal 2:  sudo python3 tests/smoke_mininet.py [--datapath user] [--no-shaping]
"""
import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
from model import load_config  # noqa: E402
from topo import Apps, quick_cleanup, start_network  # noqa: E402


def count(db, where="1=1"):
    try:
        return sqlite3.connect(db).execute(f"SELECT count(*) FROM logs WHERE {where}").fetchone()[0]
    except sqlite3.Error:
        return 0


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}")
    return ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--datapath", default="kernel")
    p.add_argument("--no-shaping", action="store_true")
    args = p.parse_args()
    quick_cleanup()
    cfg = load_config(ROOT / "config" / "topology.json")
    net = start_network(cfg, args.datapath, not args.no_shaping)
    run = ROOT / "results" / "smoke"
    subprocess.run(["rm", "-rf", str(run)])
    apps = Apps(net, cfg, run, rate=40)
    results = []
    try:
        print("\n1. Connectivity through the controller's routes")
        for a, b in (("h1", "c1"), ("h2", "c2"), ("h4", "c1"), ("c1", "h3")):
            out = net.get(a).cmd(f"ping -c 2 -W 2 {cfg['hosts'][b]['ip']}")
            results.append(check(f"{a} -> {b} ping", " 0% packet loss" in out))

        print("\n2. Logs through the virtual IP (load balancing + NAT)")
        apps.start_collectors()
        time.sleep(2.5)
        apps.start_sources()
        time.sleep(8)
        c1, c2 = count(run / "c1.db"), count(run / "c2.db")
        results.append(check("collectors received logs", c1 + c2 > 200, f"(c1={c1}, c2={c2})"))
        results.append(check("both collectors are used", c1 > 0 and c2 > 0))
        flows = net.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        results.append(check("NAT rules are installed on s1", "mod_nw_dst" in flows or "set_field:10.0.0" in flows))

        print("\n3. Collector failure: c1 is killed")
        before = count(run / "c1.db") + count(run / "c2.db")
        apps.kill("collector_c1")
        time.sleep(7)
        after_c2 = count(run / "c2.db")
        mid = after_c2
        time.sleep(3)
        results.append(check("logs keep arriving at c2", count(run / "c2.db") > mid, f"(c2 {mid} -> {count(run / 'c2.db')})"))

        print("\n4. Link failure: s1-s2 goes down")
        net.configLinkStatus("s1", "s2", "down")
        time.sleep(1)
        mid = count(run / "c2.db")
        time.sleep(4)
        results.append(check("logs still arrive over the backup path", count(run / "c2.db") > mid,
                             f"(c2 {mid} -> {count(run / 'c2.db')})"))
        net.configLinkStatus("s1", "s2", "up")

        print("\n5. Unauthorised host h4 tries to use the virtual IP")
        apps.start_rogue()
        time.sleep(6)
        rogue = count(run / "c1.db", "service='rogue'") + count(run / "c2.db", "service='rogue'")
        results.append(check("no rogue logs were accepted", rogue == 0, f"(rogue rows: {rogue})"))
    finally:
        apps.stop_all()
        net.stop()
    print(f"\n{sum(results)} of {len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
