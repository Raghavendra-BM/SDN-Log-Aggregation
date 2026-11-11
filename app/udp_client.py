"""
UDP log client: a fake service that sends its logs straight to the central log server, one datagram per log.

    python3 app/udp_client.py --server 10.0.0.11 --service payment --host-name h2 --rate 40 --duration 30 \
                              --summary results/demo/sent_h2.json

Each log gets a severity (DEBUG ... CRITICAL), a send timestamp and a sequence number. The severity decides
the destination port (see common/udp_protocol.py), which is what lets the SDN controller prioritise.

UDP gives no delivery guarantee, so this client does not wait for answers and never retries. When it stops it
writes a summary of exactly how many logs it sent per severity (in total and per second): the experiments compare that with what the
server received to compute packet loss.
"""
import argparse
import json
import random
import signal
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.udp_protocol import SEVERITIES, SEVERITY_WEIGHTS, port_for, encode  # noqa: E402

MESSAGES = {
    "DEBUG":    ["Cache lookup for item {n}", "Query plan chosen for request {n}", "Entering handler for request {n}"],
    "INFO":     ["Order {n} created", "Payment received for order {n}", "Stock reserved for order {n}"],
    "WARNING":  ["Order {n} is taking longer than usual", "Slow response from card gateway for order {n}"],
    "ERROR":    ["Card payment failed for order {n}", "Could not create order {n}"],
    "CRITICAL": ["Database unreachable, order {n} lost", "Payment gateway down, order {n} cannot be charged",
                 "Out of memory while handling order {n}"],
}


def main():
    p = argparse.ArgumentParser(description="UDP log client (fake service)")
    p.add_argument("--server", required=True, help="IP of the central log server")
    p.add_argument("--service", default="order", help="service name written into every log")
    p.add_argument("--host-name", default="node-1", help="host name written into every log")
    p.add_argument("--rate", type=float, default=20.0, help="logs per second")
    p.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    p.add_argument("--count", type=int, default=None, help="stop after this many logs")
    p.add_argument("--summary", default=None, help="write what was sent (per severity) to this JSON file")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--test-drop", type=float, default=0.0,
                   help="TEST ONLY: pretend to send but silently skip this fraction of logs (checks the loss maths)")
    a = p.parse_args()
    if a.seed is not None:
        random.seed(a.seed)

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = {s: 0 for s in SEVERITIES}                 # one counter per severity
    sent = {s: 0 for s in SEVERITIES}
    per_second = {s: {} for s in SEVERITIES}         # severity -> {second since 1970: logs sent in that second}
    errors = 0
    start = time.time()
    next_send = start
    print(f"[client {a.host_name}/{a.service}] sending {a.rate} logs/s to {a.server}", flush=True)

    while not stop["now"]:
        now = time.time()
        if a.duration is not None and now - start >= a.duration:
            break
        if a.count is not None and sum(sent.values()) >= a.count:
            break
        if now < next_send:
            time.sleep(min(next_send - now, 0.05))
            continue
        sev = random.choices(SEVERITIES, SEVERITY_WEIGHTS)[0]
        seq[sev] += 1
        sent[sev] += 1                               # counted as sent even if the network later loses it
        t_send = time.time()
        sec = int(t_send)
        per_second[sev][sec] = per_second[sev].get(sec, 0) + 1
        text = random.choice(MESSAGES[sev]).format(n=random.randint(1000, 9999))
        if random.random() >= a.test_drop:
            try:
                sock.sendto(encode(a.host_name, a.service, sev, seq[sev], t_send, text),
                            (a.server, port_for(sev)))
            except OSError:
                errors += 1                          # e.g. no route yet; UDP does not retry
        next_send += random.uniform(0.5, 1.5) / a.rate
        if next_send < now - 1:                      # we fell far behind: do not try to catch up with a burst
            next_send = now

    end = time.time()
    total = sum(sent.values())
    print(f"[client {a.host_name}/{a.service}] stopped: {total} logs sent {sent}, send errors {errors}", flush=True)
    if a.summary:
        Path(a.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(a.summary).write_text(json.dumps({
            "host": a.host_name, "service": a.service, "server": a.server, "start": start, "end": end,
            "sent": sent, "sent_per_second": per_second, "send_errors": errors}))


if __name__ == "__main__":
    main()
