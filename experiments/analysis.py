"""
Metrics for the experiments, and a stand-alone delivery check.

    python3 experiments/analysis.py --run-dir results/demo           # did every log written arrive?

Goodput    = logs per second that the collectors confirmed (taken from the agents' acknowledgement records)
Recovery   = seconds after the event until the goodput is back above 80% of normal for two seconds in a row
Delivery   = every log written by the services (log files) compared with the ids stored in the collectors' databases
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean


# ------------------------------------------------------------------ reading the raw data
def read_written(log_root):
    """{log id: service} for every complete line in <log_root>/**/*.log"""
    written = {}
    for path in Path(log_root).rglob("*.log"):
        with open(path, "rb") as f:
            for line in f:
                if line.endswith(b"\n"):
                    try:
                        d = json.loads(line)
                        written[d["id"]] = d["service"]
                    except (ValueError, KeyError):
                        pass
    return written


def read_db(path):
    """{log id: service} stored in one collector database (empty if it does not exist)"""
    if not Path(path).exists():
        return {}
    conn = sqlite3.connect(path, timeout=10)
    try:
        return {i: s for i, s in conn.execute("SELECT id, service FROM logs")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def load_metrics(run_dir, hosts=("h1", "h2", "h3")):
    """[(ack time, logs, delay ms)] from the agents on the source hosts"""
    records = []
    for h in hosts:
        p = Path(run_dir) / f"metrics_{h}.jsonl"
        if p.exists():
            for line in open(p):
                try:
                    d = json.loads(line)
                    records.append((d["t"], d["n"], d["lat_ms"]))
                except (ValueError, KeyError):
                    pass
    return sorted(records)


def read_events(path):
    if not path or not Path(path).exists():
        return []
    return [json.loads(line) for line in open(path) if line.strip()]


# ------------------------------------------------------------------ computing metrics
def goodput_series(records, t0, seconds):
    series = [0] * seconds
    for t, n, _ in records:
        k = int(t - t0)
        if 0 <= k < seconds:
            series[k] += n
    return series


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0.0


def recovery_time(series, event_sec, baseline):
    """Seconds after the event until goodput is >= 80% of baseline for 2 s in a row.
    0.0 = no visible disruption, None = it never recovered."""
    after = series[event_sec:]
    if baseline <= 0:
        return None
    dips = [i for i, v in enumerate(after) if v < 0.5 * baseline]
    if not dips:
        return 0.0
    for k in range(dips[0], len(after) - 1):
        if after[k] >= 0.8 * baseline and after[k + 1] >= 0.8 * baseline:
            return float(k)
    return None


def reaction(events, scenario, t_event):
    """How fast did the controller notice and react? Taken from the controller's event log."""
    out = {"detect_s": "", "reaction_ms": "", "acl_blocks": 0}
    after = [e for e in events if e["t"] >= t_event - 0.5]
    if scenario == "link_failure":
        down = next((e for e in after if e["event"] == "link_state" and e.get("state") == "down"), None)
        if down:
            out["detect_s"] = round(down["t"] - t_event, 3)
            upd = next((e for e in after if e["event"] == "routes_updated" and e["t"] >= down["t"]), None)
            if upd:
                out["reaction_ms"] = upd["reaction_ms"]
    elif scenario == "collector_failure":
        e = next((e for e in after if e["event"] == "backend_down" and e.get("backend") == "c1"), None)
        if e:
            out["detect_s"] = round(e["t"] - t_event, 3)
    elif scenario == "flood":
        e = next((e for e in after if e["event"] == "flood_block"), None)
        if e:
            out["detect_s"] = round(e["t"] - t_event, 3)
    out["acl_blocks"] = sum(1 for e in events if e["event"] == "acl_block")
    return out


def analyse_run(run_dir, mode, scenario, t0, t_event, duration):
    run_dir = Path(run_dir)
    records = load_metrics(run_dir)
    series = goodput_series(records, t0, duration)
    E = max(1, int(t_event - t0))
    baseline = mean(series[5:E]) if E > 5 else mean(series[:E])
    window = series[E:E + 20]
    rec = recovery_time(series, E, baseline)

    lat_before = [l for t, _, l in records if t0 + 5 <= t < t_event]
    lat_after = [l for t, _, l in records if t_event <= t < t_event + 20]

    written = read_written(run_dir / "logs")
    legit = {i for i, s in written.items() if s != "rogue"}
    d1, d2 = read_db(run_dir / "c1.db"), read_db(run_dir / "c2.db")
    delivered = (set(d1) | set(d2)) & legit
    rogue = sum(1 for d in (d1, d2) for s in d.values() if s == "rogue")
    c1_share = len(set(d1) & legit)

    row = {
        "mode": mode, "scenario": scenario, "event_s": E,
        "baseline_logs_per_s": round(baseline, 1),
        "after_event_logs_per_s": round(mean(window), 1) if window else 0,
        "min_logs_per_s_after": min(window) if window else 0,
        "recovery_s": "" if rec is None else rec,
        "never_recovered": rec is None,
        "p95_delay_before_ms": round(percentile(lat_before, 0.95), 1),
        "p95_delay_after_ms": round(percentile(lat_after, 0.95), 1),
        "logs_written": len(legit), "logs_delivered": len(delivered),
        "logs_undelivered": len(legit) - len(delivered),
        "share_c1_pct": round(100 * c1_share / max(1, len(delivered))),
        "share_c2_pct": round(100 * len(set(d2) & legit) / max(1, len(delivered))),
        "rogue_logs_accepted": rogue,
    }
    row.update(reaction(read_events(run_dir / "controller_events.jsonl"), scenario, t_event))
    return row, series


# ------------------------------------------------------------------ stand-alone delivery check
def main():
    p = argparse.ArgumentParser(description="Check that every log written reached a collector")
    p.add_argument("--run-dir", required=True, help="folder with logs/ and c1.db, c2.db (e.g. results/demo)")
    args = p.parse_args()
    run = Path(args.run_dir)
    written = read_written(run / "logs")
    legit = {i for i, s in written.items() if s != "rogue"}
    stored = set(read_db(run / "c1.db")) | set(read_db(run / "c2.db"))
    missing = legit - stored
    print(f"logs written by the services: {len(legit)}")
    print(f"logs stored by c1 / c2:       {len(set(read_db(run / 'c1.db')) & legit)} / {len(set(read_db(run / 'c2.db')) & legit)}")
    print(f"missing:                      {len(missing)}")
    print("PASS: 0 missing" if not missing else f"FAIL: {len(missing)} logs are still missing (agents may still be sending)")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
