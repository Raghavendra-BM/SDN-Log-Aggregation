"""
Runs the experiments: every congestion level with the STATIC baseline controller and with the DYNAMIC controller.

    sudo $(which python3) experiments/run_experiments.py                       # everything (about 8 minutes)
    sudo $(which python3) experiments/run_experiments.py --flood-rates 15      # one congestion level only
    sudo $(which python3) experiments/run_experiments.py --modes dynamic --flood-rates 0 15

A run (about 55 s):
    start the controller in the given mode, build the network, start the log server and the three UDP clients
    (they send for --duration seconds), start the congestion generator h4 at --event-at seconds for
    --flood-seconds seconds, wait for the clients to finish, stop everything, and analyse.

The numbers in results/summary.csv are for the congested period only (logs SENT while h4 was flooding), in three
phases: "all" (the whole flood), "onset" (the first --onset-seconds, before the controller's rules are in
place) and "sustained" (the rest):
    sent / received / loss %      per severity, from the clients' own counters and the server database
    delay mean / median / p95 / max (ms)   from the send timestamp in each log to its arrival at the server
    reaction_s                    seconds from the start of the flood until the controller declared congestion
    flood_delivered_mbps          how much of the background traffic reached the server (the link is 10 Mbit/s)
Per run there is also results/timeline_<mode>_flood<rate>.csv with one row per second.
"""
import argparse
import csv
import json
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
sys.path.insert(0, str(ROOT))
from model import load_config  # noqa: E402
from topo import Apps, quick_cleanup, start_network  # noqa: E402
from udp_analysis import analyse, per_second  # noqa: E402

CONFIG = ROOT / "config" / "topology.json"


def port_in_use(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def start_controller(mode, run_dir, port):
    if port_in_use(port):
        raise RuntimeError(f"something is already listening on port {port}, probably a controller you started by hand. "
                           f"Stop it (Ctrl+C in its terminal, or: sudo pkill -f ryu-manager) and run again.")
    env = dict(os.environ, SDN_MODE=mode, EVENT_LOG=str(run_dir / "controller_events.jsonl"), TOPO_FILE=str(CONFIG))
    exe = Path(sys.executable).parent / "ryu-manager"
    if exe.exists():
        cmd = [str(exe)]
    elif shutil.which("ryu-manager"):
        cmd = ["ryu-manager"]
    else:
        cmd = [sys.executable, "-c", "from ryu.cmd.manager import main; main()"]
    cmd += ["--ofp-tcp-listen-port", str(port), str(ROOT / "sdn" / "controller.py")]
    out_path = run_dir / "controller.out"
    proc = subprocess.Popen(cmd, env=env, stdout=open(out_path, "w"), stderr=subprocess.STDOUT)

    def tail():
        lines = out_path.read_text(errors="replace").splitlines()[-15:]
        return "\n".join(lines) or "(the controller printed nothing)"

    for _ in range(80):                                   # wait until the controller listens (up to 20 s)
        if proc.poll() is not None:
            raise RuntimeError(f"the controller exited at once (command: {' '.join(cmd)}). Its output:\n{tail()}")
        if port_in_use(port):
            return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"the controller did not open port {port} within 20 s. Its output:\n{tail()}")


def give_results_to_user():
    """The experiments run as root (sudo), so results/ would belong to root and the plot script could not write
    into it. Hand it back to the user who typed sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        subprocess.run(["chown", "-R", f"{uid}:{gid}", str(ROOT / "results")], capture_output=True)


def sleep_until(t):
    time.sleep(max(0, t - time.time()))


def read_events(run_dir):
    path = Path(run_dir) / "controller_events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def run_one(mode, flood_mbps, args, cfg):
    run_dir = ROOT / "results" / "runs" / f"{mode}_flood{flood_mbps:g}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    quick_cleanup()
    print(f"\n=== {mode} controller, congestion generator {flood_mbps:g} Mbit/s ===", flush=True)

    ctl = start_controller(mode, run_dir, cfg["controller"]["port"])
    net = apps = None
    try:
        net = start_network(cfg, args.datapath, not args.no_shaping)
        apps = Apps(net, cfg, run_dir, args.rate)
        apps.start_server()
        time.sleep(1.5)
        t0 = time.time()
        apps.start_clients(duration=args.duration)
        sleep_until(t0 + args.event_at)
        t_event = time.time()
        if flood_mbps > 0:
            apps.start_flood(seconds=args.flood_seconds, mbps=flood_mbps)
            print(f"  congestion generator started at t={t_event - t0:.1f}s", flush=True)
        apps.wait_clients(args.duration + 15)
        time.sleep(2.0)                                    # let the last datagrams arrive
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

    # ---- analysis: only the congested period (the same windows are used when the generator is off)
    #   all        the whole congested period
    #   onset      the first --onset-seconds: the controller has not reacted yet, queues are filling
    #   sustained  the rest: the controller's rules are in place
    w0 = int(t_event)
    window = (w0, w0 + args.flood_seconds)
    phases = {"all": window, "onset": (w0, w0 + args.onset_seconds),
              "sustained": (w0 + args.onset_seconds, w0 + args.flood_seconds)}
    phase_rows = {name: analyse(run_dir, win) for name, win in phases.items()}
    rows = phase_rows["all"]
    events = read_events(run_dir)
    starts = [e["t"] for e in events if e["event"] == "congestion_start" and e["t"] >= t_event - 1]
    reaction = round(starts[0] - t_event, 2) if starts and flood_mbps > 0 else None
    server = json.loads((run_dir / "server_summary.json").read_text()) if (run_dir / "server_summary.json").exists() else {}
    flood_mbps_delivered = round(server.get("sink_bytes", 0) * 8 / 1e6 / args.flood_seconds, 2)
    extra = {"mode": mode, "flood_mbps": flood_mbps, "reaction_s": reaction, "flood_delivered_mbps": flood_mbps_delivered}
    out = [dict(extra, phase=name, **r) for name, rs in phase_rows.items() for r in rs]

    stats = {int(e["t"]): e for e in events if e["event"] == "stats"}
    with open(ROOT / "results" / f"timeline_{mode}_flood{flood_mbps:g}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "flood_on", "link_mbps", "priority_on", "critical_sent", "critical_received",
                    "critical_delay_ms", "info_sent", "info_received", "info_delay_ms"])
        crit = per_second(run_dir, "CRITICAL", int(t0), int(t0) + args.duration)
        info = per_second(run_dir, "INFO", int(t0), int(t0) + args.duration)
        for (sec, cs, cr, cd), (_, i_s, ir, idl) in zip(crit, info):
            st = stats.get(sec) or stats.get(sec + 1) or {}
            on = flood_mbps > 0 and window[0] <= sec < window[1]
            w.writerow([sec - int(t0), int(on), st.get("busiest_mbps", ""), int(bool(st.get("priority"))),
                        cs, cr, cd if cd is not None else "", i_s, ir, idl if idl is not None else ""])
    by = {r["severity"]: r for r in rows}
    c = by["CRITICAL"]
    on = {r["severity"]: r for r in phase_rows["onset"]}["CRITICAL"]
    su = {r["severity"]: r for r in phase_rows["sustained"]}["CRITICAL"]
    print(f"  CRITICAL logs: loss {c['loss_pct']}%  delay mean {c['delay_mean_ms']} ms  p95 {c['delay_p95_ms']} ms   "
          f"INFO logs: loss {by['INFO']['loss_pct']}%   reaction {reaction} s   background delivered {flood_mbps_delivered} Mbit/s",
          flush=True)
    print(f"  CRITICAL onset (first {args.onset_seconds} s): loss {on['loss_pct']}%, p95 {on['delay_p95_ms']} ms   "
          f"sustained: loss {su['loss_pct']}%, p95 {su['delay_p95_ms']} ms", flush=True)
    return out


def main():
    p = argparse.ArgumentParser(description="Static vs dynamic SDN experiments (UDP logs, priority under congestion)")
    p.add_argument("--modes", nargs="+", default=["static", "dynamic"], choices=["static", "dynamic"])
    p.add_argument("--flood-rates", nargs="+", type=float, default=[0, 8, 15, 20], help="Mbit/s from h4 (0 = no congestion)")
    p.add_argument("--duration", type=int, default=40, help="seconds the clients send")
    p.add_argument("--event-at", type=int, default=10, help="second at which the congestion generator starts")
    p.add_argument("--flood-seconds", type=int, default=20)
    p.add_argument("--onset-seconds", type=int, default=3, help="length of the 'onset' phase after the flood starts")
    p.add_argument("--rate", type=float, default=100, help="logs per second per source host")
    p.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    p.add_argument("--no-shaping", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("Mininet needs root: sudo $(which python3) experiments/run_experiments.py")

    cfg = load_config(CONFIG)
    (ROOT / "results").mkdir(exist_ok=True)
    summary_path = ROOT / "results" / "summary.csv"
    results = {}                                               # (mode, flood, severity) -> row; earlier runs are kept
    if summary_path.exists():
        with open(summary_path) as f:
            old = list(csv.DictReader(f))
        if old and "flood_mbps" in old[0] and "severity" in old[0] and "phase" in old[0]:
            results = {(r["mode"], float(r["flood_mbps"]), r["phase"], r["severity"]): r for r in old}
        else:                                                  # a summary in an older format: keep it aside
            summary_path.rename(summary_path.with_name("summary_older_format.csv"))
    for flood in args.flood_rates:
        for mode in args.modes:
            for row in run_one(mode, flood, args, cfg):
                results[(row["mode"], float(row["flood_mbps"]), row["phase"], row["severity"])] = row
            ordered = sorted(results.values(), key=lambda r: (float(r["flood_mbps"]), r["mode"], r["phase"]))
            with open(summary_path, "w", newline="") as f:      # rewritten after every run, nothing is lost
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                w.writeheader()
                w.writerows(ordered)
            give_results_to_user()
    print(f"\nDone. Results: {summary_path}\nNext: python3 experiments/plot_results.py")


if __name__ == "__main__":
    main()
