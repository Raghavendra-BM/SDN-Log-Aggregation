"""
Runs the experiments: every scenario with the STATIC baseline controller and with the DYNAMIC controller.

    sudo $(which python3) experiments/run_experiments.py                      # everything (about 12 minutes)
    sudo $(which python3) experiments/run_experiments.py --scenarios link_failure --modes static dynamic
    sudo $(which python3) experiments/run_experiments.py --datapath user      # if you have no OVS kernel module

Scenarios
  normal             nothing happens (control run)
  link_failure       at the event time the fast core link s1-s2 goes down and stays down
  collector_failure  at the event time collector c1 is killed (SIGKILL)
  flood              at the event time the untrusted host h4 floods the network with UDP for 20 s
  rogue              h4 tries to send logs through the virtual IP all the time (it is not authorised)

For every run the script starts the controller in the requested mode, builds the network, starts the
collectors, services and agents, applies the event, stops the services, lets the agents drain their backlog,
and then computes the metrics. Results go to results/summary.csv and results/timeline_<mode>_<scenario>.csv
"""
import argparse
import csv
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
sys.path.insert(0, str(ROOT / "experiments"))
from analysis import analyse_run  # noqa: E402
from model import load_config  # noqa: E402
from topo import Apps, start_network  # noqa: E402

CONFIG = ROOT / "config" / "topology.json"
SCENARIOS = ["normal", "link_failure", "collector_failure", "flood", "rogue"]
FLOOD_SECONDS = 20


def start_controller(mode, run_dir, port):
    env = dict(os.environ, SDN_MODE=mode, EVENT_LOG=str(run_dir / "controller_events.jsonl"), TOPO_FILE=str(CONFIG))
    exe = Path(sys.executable).parent / "ryu-manager"
    if exe.exists():
        cmd = [str(exe)]
    elif shutil.which("ryu-manager"):
        cmd = ["ryu-manager"]
    else:
        cmd = [sys.executable, "-c", "from ryu.cmd.manager import main; main()"]
    cmd += ["--ofp-tcp-listen-port", str(port), str(ROOT / "sdn" / "controller.py")]
    proc = subprocess.Popen(cmd, env=env, stdout=open(run_dir / "controller.out", "w"), stderr=subprocess.STDOUT)
    for _ in range(60):                                   # wait until the controller listens
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc
        except OSError:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"controller did not start, see {run_dir / 'controller.out'}")


def sleep_until(t):
    time.sleep(max(0, t - time.time()))


def run_one(mode, scenario, args, cfg):
    run_dir = ROOT / "results" / "runs" / f"{mode}_{scenario}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    subprocess.run(["mn", "-c"], capture_output=True)
    print(f"\n=== {mode} / {scenario} ===", flush=True)

    ctl = start_controller(mode, run_dir, cfg["controller"]["port"])
    net = apps = None
    try:
        net = start_network(cfg, args.datapath, not args.no_shaping)
        apps = Apps(net, cfg, run_dir, args.rate)
        apps.start_collectors()
        time.sleep(2.5)
        t0 = time.time()
        apps.start_sources()
        if scenario == "rogue":
            apps.start_rogue()

        sleep_until(t0 + args.event_at)
        t_event = time.time()
        if scenario == "link_failure":
            net.configLinkStatus("s1", "s2", "down")
        elif scenario == "collector_failure":
            apps.kill("collector_c1")
        elif scenario == "flood":
            apps.start_flood(seconds=FLOOD_SECONDS, mbps=args.flood_mbps)
        print(f"  event '{scenario}' at t={t_event - t0:.1f}s", flush=True)

        sleep_until(t0 + args.duration)
        apps.stop_services()
        print(f"  services stopped, waiting {args.drain}s for the agents to deliver what is left", flush=True)
        time.sleep(args.drain)
    finally:
        if apps:
            apps.stop_all()
        if net:
            net.stop()
        ctl.terminate()
        try:
            ctl.wait(5)
        except subprocess.TimeoutExpired:
            ctl.kill()

    row, series = analyse_run(run_dir, mode, scenario, t0, t_event, args.duration)
    print(f"  goodput before {row['baseline_logs_per_s']} logs/s, after event {row['after_event_logs_per_s']} logs/s, "
          f"recovery {row['recovery_s'] if not row['never_recovered'] else 'never'}, "
          f"undelivered {row['logs_undelivered']}/{row['logs_written']}", flush=True)
    with open(ROOT / "results" / f"timeline_{mode}_{scenario}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "logs_per_s"])
        w.writerows(enumerate(series))
    return row


def main():
    p = argparse.ArgumentParser(description="Static vs dynamic SDN experiments")
    p.add_argument("--modes", nargs="+", default=["static", "dynamic"], choices=["static", "dynamic"])
    p.add_argument("--scenarios", nargs="+", default=SCENARIOS, choices=SCENARIOS)
    p.add_argument("--duration", type=int, default=45, help="seconds of traffic per run")
    p.add_argument("--event-at", type=int, default=12, help="second at which the event happens")
    p.add_argument("--drain", type=int, default=8, help="seconds the agents get to deliver their backlog")
    p.add_argument("--rate", type=float, default=40, help="logs per second per source host")
    p.add_argument("--flood-mbps", type=float, default=15)
    p.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    p.add_argument("--no-shaping", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("Mininet needs root: sudo $(which python3) experiments/run_experiments.py")

    cfg = load_config(CONFIG)
    (ROOT / "results").mkdir(exist_ok=True)
    summary_path = ROOT / "results" / "summary.csv"
    results = {}                                               # (mode, scenario) -> row; earlier runs are kept
    if summary_path.exists():
        with open(summary_path) as f:
            results = {(r["mode"], r["scenario"]): r for r in csv.DictReader(f)}
    for scenario in args.scenarios:
        for mode in args.modes:
            row = run_one(mode, scenario, args, cfg)
            results[(mode, scenario)] = row
            ordered = sorted(results.values(), key=lambda r: (SCENARIOS.index(r["scenario"]), r["mode"]))
            with open(summary_path, "w", newline="") as f:      # rewritten after every run, nothing is lost
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                w.writeheader()
                w.writerows(ordered)
    print(f"\nDone. Results: {summary_path}\nNext: python3 experiments/plot_results.py")


if __name__ == "__main__":
    main()
