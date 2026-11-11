"""
Smoke test: does the whole system work inside Mininet?   (needs root and a running DYNAMIC controller)

    terminal 1:  SDN_MODE=dynamic ryu-manager sdn/controller.py
    terminal 2:  sudo $(which python3) tests/smoke_mininet.py [--datapath user] [--no-shaping]

It takes about 1 minute and checks: routing, the priority queues, UDP log delivery, and that the controller
switches the priority rules on during congestion and off again afterwards.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
sys.path.insert(0, str(ROOT / "experiments"))
sys.path.insert(0, str(ROOT))
from model import load_config  # noqa: E402
from topo import Apps, quick_cleanup, run, start_network  # noqa: E402
from udp_analysis import analyse  # noqa: E402


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}")
    return ok


def by_severity(rows):
    return {r["severity"]: r for r in rows}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--datapath", default="kernel")
    p.add_argument("--no-shaping", action="store_true")
    args = p.parse_args()
    quick_cleanup()
    cfg = load_config(ROOT / "config" / "topology.json")
    net = start_network(cfg, args.datapath, not args.no_shaping)
    runs = ROOT / "results" / "smoke"
    subprocess.run(["rm", "-rf", str(runs)])
    apps = Apps(net, cfg, runs, rate=100)
    srv_ip = cfg["hosts"]["srv"]["ip"]
    results = []
    try:
        print("\n1. Connectivity through the controller's routes")
        for a, b in (("h1", "srv"), ("h4", "srv"), ("srv", "h3")):
            out = net.get(a).cmd(f"ping -c 2 -W 2 {cfg['hosts'][b]['ip']}")
            results.append(check(f"{a} -> {b} ping", " 0% packet loss" in out))

        print("\n2. Priority queues on the core ports")
        tc = run(["tc", "class", "show", "dev", "s1-eth5"]).stdout
        results.append(check("s1-eth5 has an HTB queue discipline with 3+ classes", tc.count("class htb") >= 3,
                             f"({tc.count('class htb')} classes)"))

        print("\n3. UDP logs and a flood (clients send for 26 s, h4 floods from second 6 to 18)")
        apps.start_server()
        time.sleep(1.5)
        t0 = time.time()
        apps.start_clients(duration=26)
        time.sleep(5)
        flows_quiet = net.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        results.append(check("no priority rules while the network is quiet", "set_queue" not in flows_quiet))
        time.sleep(1)
        t_flood = time.time()
        apps.start_flood(seconds=12, mbps=15)
        time.sleep(6)
        flows_busy = net.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        results.append(check("controller switched the priority rules on (set_queue:2 on s1)", "set_queue:2" in flows_busy))
        time.sleep(12)                                   # flood over (12 s) + the controller's calm-down time
        flows_after = net.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        results.append(check("controller switched them off again after the congestion", "set_queue" not in flows_after))
        apps.wait_clients(30)
        time.sleep(1.5)
    finally:
        apps.stop_all()
        net.stop()

    quiet = by_severity(analyse(runs, (int(t0) + 1, int(t0) + 5)))
    busy = by_severity(analyse(runs, (int(t_flood) + 2, int(t_flood) + 12)))
    results.append(check("quiet period: logs arrive, loss under 2%", all(
        r["sent"] > 0 and r["loss_pct"] is not None and r["loss_pct"] < 2 for r in quiet.values() if r["sent"] > 20),
        str({k: v["loss_pct"] for k, v in quiet.items()})))
    results.append(check("congestion is real: INFO logs lose more than 5%", (busy["INFO"]["loss_pct"] or 0) > 5,
                         f"(INFO loss {busy['INFO']['loss_pct']}%)"))
    results.append(check("CRITICAL logs survive the congestion (loss under 5%)", (busy["CRITICAL"]["loss_pct"] is not None
                                                                                   and busy["CRITICAL"]["loss_pct"] < 5),
                         f"(CRITICAL loss {busy['CRITICAL']['loss_pct']}%, p95 delay {busy['CRITICAL']['delay_p95_ms']} ms)"))
    print(f"\n{sum(results)} of {len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
