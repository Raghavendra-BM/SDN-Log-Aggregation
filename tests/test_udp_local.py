"""
Local test of the UDP client, the log server and the loss/delay analysis. No Mininet needed:

    python3 tests/test_udp_local.py

1. Without artificial loss every log must arrive (loopback does not drop at this rate).
2. With --test-drop 0.2 the measured loss must be close to 20%, and the sequence-gap method used by the
   server must agree with the clients' own sent-counts.
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
from udp_analysis import analyse  # noqa: E402

PY = sys.executable


def run(run_dir, drop):
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    server = subprocess.Popen([PY, str(ROOT / "app" / "log_server.py"), "--bind", "127.0.0.1",
                               "--db", str(run_dir / "server.db"), "--summary", str(run_dir / "server_summary.json")],
                              stdout=subprocess.DEVNULL)
    time.sleep(1.0)
    clients = [subprocess.Popen([PY, str(ROOT / "app" / "udp_client.py"), "--server", "127.0.0.1",
                                 "--service", svc, "--host-name", host, "--rate", "150", "--duration", "6",
                                 "--summary", str(run_dir / f"sent_{host}.json"), "--test-drop", str(drop),
                                 "--seed", str(i)], stdout=subprocess.DEVNULL)
               for i, (host, svc) in enumerate([("h1", "order"), ("h2", "payment"), ("h3", "inventory")])]
    for c in clients:
        c.wait()
    time.sleep(1.0)
    server.terminate()
    server.wait(10)
    return analyse(run_dir), json.loads((run_dir / "server_summary.json").read_text())


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        sys.exit(1)


def main():
    base = ROOT / "results" / "test_udp"
    rows, summ = run(base / "no_loss", 0.0)
    total_sent = sum(r["sent"] for r in rows)
    check("clients sent a reasonable number of logs", total_sent > 2000, f"({total_sent})")
    check("no loss without artificial loss", all(r["loss_pct"] == 0 for r in rows if r["sent"]),
          str({r["severity"]: r["loss_pct"] for r in rows}))
    check("every severity appears", all(r["sent"] > 0 for r in rows))
    check("delays are small on loopback", all(r["delay_p95_ms"] is not None and r["delay_p95_ms"] < 100 for r in rows))
    check("server saw no malformed datagrams", summ["bad_datagrams"] == 0)

    rows, summ = run(base / "loss20", 0.2)
    sent = sum(r["sent"] for r in rows)
    got = sum(r["received"] for r in rows)
    pct = 100 * (sent - got) / sent
    check("measured loss is close to the 20% that was injected", 17 <= pct <= 23, f"({pct:.1f}%)")
    gaps = sum(summ["lost_by_severity_from_gaps"].values())
    check("sequence-gap loss estimate agrees with the sent-counts", abs(gaps - (sent - got)) <= 0.03 * sent,
          f"(gaps {gaps}, sent-received {sent - got})")
    # windowed analysis: the first 2 seconds of the run must hold fewer logs than the whole run
    first = min(json.loads(f.read_text())["start"] for f in (base / "loss20").glob("sent_*.json"))
    win = analyse(base / "loss20", (int(first), int(first) + 2))
    wsent = sum(r["sent"] for r in win)
    check("windowed analysis counts only the logs sent inside the window", 0 < wsent < 0.6 * sent, f"({wsent} of {sent})")
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
