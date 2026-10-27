"""
Fake service: writes one JSON log per line into <logdir>/<service>.log

    python3 app/service.py order --logdir logs/h1 --host-name h1 --rate 40
    python3 app/service.py order payment inventory --count 200 --rate 100

Press Ctrl+C (or send SIGTERM) to stop. After a restart, seq continues where it stopped.
"""
import argparse
import json
import os
import random
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MESSAGES = {
    "order": {
        "INFO": ["Order {n} created", "Order {n} confirmed", "Cart updated for order {n}"],
        "WARN": ["Order {n} is taking longer than usual", "Retrying stock check for order {n}"],
        "ERROR": ["Order {n} failed: database timeout", "Could not create order {n}"],
    },
    "payment": {
        "INFO": ["Payment received for order {n}", "Payment started for order {n}", "Refund issued for order {n}"],
        "WARN": ["Slow response from card gateway for order {n}", "Retrying payment for order {n}"],
        "ERROR": ["Card payment failed for order {n}", "Payment gateway timeout for order {n}"],
    },
    "inventory": {
        "INFO": ["Stock reserved for order {n}", "Stock level checked for item {n}", "Stock updated for item {n}"],
        "WARN": ["Low stock for item {n}", "Stock sync delayed for item {n}"],
        "ERROR": ["Out of stock for item {n}", "Inventory database unreachable for item {n}"],
    },
}
GENERIC = {"INFO": ["Request {n} handled"], "WARN": ["Request {n} was slow"], "ERROR": ["Request {n} failed"]}
LEVELS = ["INFO", "WARN", "ERROR"]
WEIGHTS = [80, 15, 5]       # percent INFO / WARN / ERROR


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def last_seq(path):
    """seq of the last complete line, so a restarted service continues the numbering."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            return int(json.loads(line)["seq"])
        except (ValueError, KeyError, TypeError):
            continue
    return 0


def run_service(name, rate, count, stop, log_dir, host):
    path = log_dir / f"{name}.log"
    seq = last_seq(path)
    msgs = MESSAGES.get(name, GENERIC)
    written = 0
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        while not stop.is_set() and (count is None or written < count):
            level = random.choices(LEVELS, WEIGHTS)[0]
            seq += 1
            entry = {
                "id": str(uuid.uuid4()),          # created HERE, so a resent log keeps the same id
                "timestamp": now_iso(),
                "service": name,
                "level": level,
                "host": host,
                "seq": seq,
                "message": random.choice(msgs[level]).format(n=random.randint(1000, 9999)),
            }
            f.write(json.dumps(entry) + "\n")
            f.flush()
            written += 1
            stop.wait(random.uniform(0.5, 1.5) / rate)
    print(f"[{name}] stopped after {written} logs (last seq {seq})", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Fake service that writes logs")
    parser.add_argument("names", nargs="+", help="service names, e.g. order payment inventory")
    parser.add_argument("--logdir", default=str(ROOT / "logs"))
    parser.add_argument("--host-name", default="node-1")
    parser.add_argument("--rate", type=float, default=2.0, help="logs per second, per service")
    parser.add_argument("--count", type=int, default=None, help="stop after this many logs")
    args = parser.parse_args()

    log_dir = Path(args.logdir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=run_service, args=(n, args.rate, args.count, stop, log_dir, args.host_name))
               for n in args.names]
    for t in threads:
        t.start()
    print(f"Writing logs to {log_dir} for: {', '.join(args.names)}", flush=True)
    while any(t.is_alive() for t in threads):
        time.sleep(0.2)


if __name__ == "__main__":
    main()
