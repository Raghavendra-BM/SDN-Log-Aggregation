"""
Test of the dashboard API against a real log server database (no Mininet):   python3 tests/test_query_api.py
Sends logs over loopback with 20% artificial loss, then checks /logs, /stats and /events.
"""
import json
import subprocess
import sys
import time
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
RUN = ROOT / "results" / "test_api"


def get(base, path):
    return json.loads(urllib.request.urlopen(base + path, timeout=5).read())


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        sys.exit(1)


def main():
    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(parents=True)
    (RUN / "events.jsonl").write_text(
        json.dumps({"t": 1, "event": "stats", "busiest_mbps": 1}) + "\n" +
        json.dumps({"t": 2, "event": "congestion_start", "mbps": 9.9, "reacted": True}) + "\n")
    server = subprocess.Popen([PY, str(ROOT / "app" / "log_server.py"), "--bind", "127.0.0.1", "--db", str(RUN / "server.db")],
                              stdout=subprocess.DEVNULL)
    time.sleep(1)
    subprocess.run([PY, str(ROOT / "app" / "udp_client.py"), "--server", "127.0.0.1", "--service", "payment", "--host-name", "h2",
                    "--rate", "150", "--duration", "4", "--test-drop", "0.2", "--seed", "3"], stdout=subprocess.DEVNULL)
    time.sleep(1)
    api = subprocess.Popen([PY, str(ROOT / "app" / "query_api.py"), "--db", str(RUN / "server.db"), "--events",
                            str(RUN / "events.jsonl"), "--port", "8765"])
    time.sleep(1)
    base = "http://127.0.0.1:8765"
    try:
        logs = get(base, "/logs?limit=50")
        check("/logs returns logs in the dashboard format", logs["count"] == 50 and
              all(k in logs["logs"][0] for k in ("row_id", "id", "timestamp", "received_at", "service", "level", "host", "seq", "message", "delay_ms")))
        check("newest first", logs["logs"][0]["row_id"] > logs["logs"][-1]["row_id"])
        crit = get(base, "/logs?level=critical&limit=1000")
        check("level filter works (case-insensitive)", crit["count"] > 0 and all(l["level"] == "CRITICAL" for l in crit["logs"]))
        warn = get(base, "/logs?level=WARN&limit=1000")
        check("WARN is accepted as WARNING", warn["count"] > 0 and all(l["level"] == "WARNING" for l in warn["logs"]))
        newest = logs["logs"][0]["row_id"]
        check("since_id returns only newer rows", get(base, f"/logs?since_id={newest}")["count"] == 0)
        check("search works", get(base, "/logs?q=zzzzqqq")["count"] == 0)
        st = get(base, "/stats")
        check("/stats has totals, per-severity counts, lost logs and delay",
              st["total_logs"] > 400 and set(st["by_level"]) <= {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
              and st["lost_logs"] > 50 and st["avg_delay_ms"] is not None and "logs_last_minute" in st, str(st))
        ev = get(base, "/events")["events"]
        check("/events hides the per-poll statistics lines", [e["event"] for e in ev] == ["congestion_start"])
        print("\nAll checks passed.")
    finally:
        api.terminate()
        server.terminate()
        server.wait(10)
        shutil.rmtree(RUN, ignore_errors=True)


if __name__ == "__main__":
    main()
