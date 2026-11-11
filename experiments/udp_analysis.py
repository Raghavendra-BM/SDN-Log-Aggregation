"""
Packet loss and delivery time per severity, from the server database and the clients' sent-counts.

    python3 experiments/udp_analysis.py --run-dir results/demo

Looks in <run-dir> for  server.db  and  sent_*.json  (written by app/udp_client.py).
With --window T0 T1 only logs SENT in that time span are counted (used to measure the congested period only).

    sent        what the clients say they sent (ground truth)
    received    rows in the server database
    loss %      (sent - received) / sent
    delay       recv_at - sent_at in milliseconds: mean, median, 95th percentile, maximum
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.udp_protocol import SEVERITIES  # noqa: E402


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def read_sent(run_dir, window=None):
    """{severity: logs sent}. With window=(t0, t1) only logs sent in the seconds t0 <= second < t1 count."""
    sent = {s: 0 for s in SEVERITIES}
    for path in Path(run_dir).glob("sent_*.json"):
        data = json.loads(path.read_text())
        if window is None:
            for sev, n in data["sent"].items():
                sent[sev] = sent.get(sev, 0) + n
        else:
            for sev, seconds in data.get("sent_per_second", {}).items():
                sent[sev] = sent.get(sev, 0) + sum(n for sec, n in seconds.items() if window[0] <= int(sec) < window[1])
    return sent


def read_delays(db_path, window=None):
    """{severity: [delay ms, ...]} for logs whose SEND time lies in the window (so loss and delay match up)."""
    delays = {s: [] for s in SEVERITIES}
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path, timeout=10)
        sql, args = "SELECT severity, delay_ms FROM logs", ()
        if window is not None:
            sql, args = sql + " WHERE CAST(sent_at AS INTEGER) >= ? AND CAST(sent_at AS INTEGER) < ?", window
        for sev, d in conn.execute(sql, args):
            delays.setdefault(sev, []).append(d)
        conn.close()
    return delays


def analyse(run_dir, window=None):
    """One row per severity. window=(t0, t1) in whole seconds since 1970 limits the analysis to logs SENT then."""
    sent, delays = read_sent(run_dir, window), read_delays(Path(run_dir) / "server.db", window)
    rows = []
    for sev in reversed(SEVERITIES):                         # most important first
        got = len(delays.get(sev, []))
        d = delays.get(sev, [])
        n_sent = sent.get(sev, 0)
        rows.append({
            "severity": sev, "sent": n_sent, "received": got,
            "loss_pct": round(100 * (n_sent - got) / n_sent, 2) if n_sent else None,
            "delay_mean_ms": round(mean(d), 2) if d else None,
            "delay_median_ms": round(median(d), 2) if d else None,
            "delay_p95_ms": round(percentile(d, 95), 2) if d else None,
            "delay_max_ms": round(max(d), 2) if d else None,
        })
    return rows


def per_second(run_dir, severity, t0, t1):
    """[(second, sent, received, mean delay ms or None)] for one severity, by the second the log was SENT."""
    sent = {}
    for path in Path(run_dir).glob("sent_*.json"):
        for sec, n in json.loads(path.read_text()).get("sent_per_second", {}).get(severity, {}).items():
            sent[int(sec)] = sent.get(int(sec), 0) + n
    got, delay = {}, {}
    db = Path(run_dir) / "server.db"
    if db.exists():
        conn = sqlite3.connect(db, timeout=10)
        for sec, n, d in conn.execute("SELECT CAST(sent_at AS INTEGER), count(*), avg(delay_ms) FROM logs "
                                      "WHERE severity=? GROUP BY 1", (severity,)):
            got[sec], delay[sec] = n, d
        conn.close()
    return [(s, sent.get(s, 0), got.get(s, 0), round(delay[s], 2) if s in delay else None) for s in range(t0, t1)]


def print_table(rows):
    cols = ["severity", "sent", "received", "loss_pct", "delay_mean_ms", "delay_median_ms", "delay_p95_ms", "delay_max_ms"]
    print("  ".join(f"{c:>15}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(r[c]):>15}" for c in cols))


def main():
    p = argparse.ArgumentParser(description="Loss and delay per severity")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--csv", default=None, help="also write the table to this CSV file")
    p.add_argument("--window", nargs=2, type=int, default=None, metavar=("T0", "T1"),
                   help="only logs sent between these two times (seconds since 1970)")
    a = p.parse_args()
    rows = analyse(a.run_dir, tuple(a.window) if a.window else None)
    print_table(rows)
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
