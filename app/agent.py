"""
Log agent (TCP): reads <logdir>/*.log and sends new lines to the collector over a TCP socket.

    python3 app/agent.py --collector 10.0.0.100:9000 --logdir logs/h1

What it guarantees:
  * BATCHING     up to 100 logs per message instead of one message per log
  * CHECKPOINT   remembers how far it has read each file and moves forward only AFTER the
                 collector confirmed that the logs are committed to its database ("ack")
  * RECONNECT    one persistent TCP connection; if it breaks, connect again
  * RETRY        collector unreachable or busy -> wait 1, 2, 4 ... s (capped) and resend the SAME batch
  * NO LOSS      a crash can cause a resend, never a skip; the collector ignores repeated ids
  * ROTATION     optional: open a fresh connection every N batches, so the SDN controller
                 gets the chance to pick the least-loaded collector again
"""
import argparse
import json
import os
import random
import signal
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common.protocol import parse_hostport, recv_msg, send_msg  # noqa: E402


# ------------------------------------------------------------------ checkpoint
def load_checkpoint(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_checkpoint(path, offsets):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets))
    os.replace(tmp, path)  # atomic: the file is never half-written


# ------------------------------------------------------------------ reading the log files
def read_batch(log_dir, offsets, batch_size):
    """Read new COMPLETE lines from every *.log file. Returns (logs, new_offsets)."""
    batch, new_offsets = [], dict(offsets)
    for path in sorted(log_dir.glob("*.log")):
        offset = offsets.get(path.name, 0)
        size = path.stat().st_size
        if size < offset:          # file was replaced or emptied: start again
            offset = 0
            new_offsets[path.name] = 0
        if size == offset:
            continue
        with open(path, "rb") as f:
            f.seek(offset)
            while len(batch) < batch_size:
                line = f.readline()
                if not line or not line.endswith(b"\n"):   # empty, or a half-written last line
                    break
                offset += len(line)
                try:
                    batch.append(json.loads(line))
                except ValueError:
                    print(f"[agent] skipping a line that is not valid JSON in {path.name}", flush=True)
        new_offsets[path.name] = offset
        if len(batch) >= batch_size:
            break
    return batch, new_offsets


# ------------------------------------------------------------------ talking to the collector
class CollectorLink:
    """One persistent TCP connection to the collector. Reconnects when it breaks."""

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def request(self, msg):
        """Send one message and wait for the reply. Raises OSError / ValueError if the link is broken."""
        if self.sock is None:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            s.settimeout(self.timeout)
            self.sock = s
            print(f"[agent] connected to {self.host}:{self.port}", flush=True)
        try:
            send_msg(self.sock, msg)
            return recv_msg(self.sock)
        except (OSError, ValueError):
            self.close()      # never reuse a connection in an unknown state
            raise


def post_batch(link, batch, max_backoff):
    """Send one batch. Returns 'ok' or 'bad'. If the collector is down or busy, wait and retry forever."""
    delay = 1
    while True:
        try:
            reply = link.request({"type": "batch", "logs": batch})
            kind = reply.get("type")
            if kind == "ack":
                return "ok"                               # committed to the database
            if kind == "busy":                            # collector says: slow down
                wait = max(float(reply.get("retry_after", 1)), delay)
                print(f"[agent] collector busy, waiting {wait:.0f}s", flush=True)
            elif kind == "error" and reply.get("code") == "invalid":
                print(f"[agent] collector rejected the batch: {reply.get('reason')}", flush=True)
                return "bad"                              # the data itself is invalid
            else:                                         # temporary problem on the collector
                wait = delay
                print(f"[agent] collector error ({reply.get('reason', kind)}), retrying in {wait}s", flush=True)
        except (OSError, ValueError) as e:
            wait = delay
            print(f"[agent] collector unreachable ({type(e).__name__}), retrying in {wait}s", flush=True)
        time.sleep(wait + random.uniform(0, 0.3))
        delay = min(delay * 2, max_backoff)               # 1, 2, 4, ... up to max_backoff


def deliver(link, batch, rejected_path, max_backoff):
    """Send a batch; if the collector rejects it as invalid, send the logs one by one so that one
    bad line cannot block the good ones. Returns how many logs were accepted."""
    if post_batch(link, batch, max_backoff) == "ok":
        return len(batch)
    if len(batch) == 1:
        with open(rejected_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(batch[0]) + "\n")
        print(f"[agent] 1 invalid log saved to {rejected_path.name}", flush=True)
        return 0
    return sum(deliver(link, [log], rejected_path, max_backoff) for log in batch)


# ------------------------------------------------------------------ main loop
def raise_interrupt(signum, frame):
    """Turn SIGTERM / SIGINT into KeyboardInterrupt so the normal shutdown code runs."""
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="Log agent (TCP)")
    parser.add_argument("--collector", default=os.environ.get("COLLECTOR_ADDR", "127.0.0.1:9000"),
                        help="collector (or SDN virtual IP) as host:port")
    parser.add_argument("--logdir", default=str(ROOT / "logs"))
    parser.add_argument("--batch", type=int, default=100, help="max logs per message")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds to wait when nothing is new")
    parser.add_argument("--checkpoint", default=str(HERE / "checkpoint.json"))
    parser.add_argument("--metrics", default=None, help="write one JSON line per acknowledged batch")
    parser.add_argument("--timeout", type=float, default=3.0, help="socket timeout in seconds")
    parser.add_argument("--max-backoff", type=float, default=4.0, help="longest wait between retries")
    parser.add_argument("--rotate-batches", type=int, default=0,
                        help="open a new connection after this many batches (0 = never)")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, raise_interrupt)
    signal.signal(signal.SIGTERM, raise_interrupt)

    host, port = parse_hostport(args.collector)
    link = CollectorLink(host, port, args.timeout)
    log_dir = Path(args.logdir)
    log_dir.mkdir(parents=True, exist_ok=True)
    cp_path = Path(args.checkpoint)
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path = cp_path.with_name(cp_path.stem + ".rejected.jsonl")
    metrics = None
    if args.metrics:
        Path(args.metrics).parent.mkdir(parents=True, exist_ok=True)
        metrics = open(args.metrics, "a", buffering=1)

    offsets = load_checkpoint(cp_path)
    total, batches_on_conn = 0, 0
    print(f"[agent] watching {log_dir} -> {host}:{port}  (Ctrl+C to stop)", flush=True)
    if offsets:
        print(f"[agent] resuming from checkpoint: {offsets}", flush=True)

    try:
        while True:
            batch, new_offsets = read_batch(log_dir, offsets, args.batch)
            if batch:
                t_first = time.time()
                accepted = deliver(link, batch, rejected_path, args.max_backoff)
                t_ack = time.time()
                total += accepted
                if metrics:   # time of the ack, logs delivered, delay from first attempt to ack
                    metrics.write(json.dumps({"t": t_ack, "n": accepted, "lat_ms": round((t_ack - t_first) * 1000, 1)}) + "\n")
                batches_on_conn += 1
                if args.rotate_batches and batches_on_conn >= args.rotate_batches:
                    link.close()                      # next request opens a fresh connection
                    batches_on_conn = 0
            if new_offsets != offsets:                # only reached AFTER the batch was acknowledged
                offsets = new_offsets
                save_checkpoint(cp_path, offsets)
            if len(batch) < args.batch:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[agent] stopped after {total} logs. Checkpoint is saved.", flush=True)
    finally:
        link.close()
        if metrics:
            metrics.close()


if __name__ == "__main__":
    main()
