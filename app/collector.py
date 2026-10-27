"""
TCP log collector: raw sockets, one thread per connection, SQLite storage, heartbeats to the SDN controller.

    python3 app/collector.py --id c1 --db results/c1.db --heartbeat 10.0.0.250:9100

Messages are framed as [4-byte length][JSON] (see common/protocol.py).

    agent     -> collector   {"type":"batch","logs":[ {...}, {...} ]}
    collector -> agent       {"type":"ack","count":N}        the logs are COMMITTED to the database
                             {"type":"busy","retry_after":2} overloaded: send the same batch again later
                             {"type":"error","code":"invalid","reason":"..."}  the batch itself is bad
                             {"type":"error","code":"storage","reason":"..."}  temporary problem, retry
    anyone    -> collector   {"type":"ping"}   ->   {"type":"pong","pending":N}

Heartbeat (UDP, every 0.5 s, sent to the controller's virtual address and picked up by the SDN controller
as a packet-in):  {"id":"c1","pending":N,"conns":K,"logs":TOTAL,"t":TIME}
The controller uses it for health checks and load-aware choice of collector.

Why "ack after commit": the agent moves its checkpoint forward only after the ack, so a log is never
confirmed while it lives only in memory. Even if this process is killed abruptly, every log that was not
acknowledged is simply sent again, and duplicates are ignored by the database.
"""
import argparse
import json
import queue
import signal
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.protocol import parse_hostport, recv_msg, send_msg  # noqa: E402

LEVELS = {"INFO", "WARN", "ERROR"}
MAX_BATCH_LOGS = 5000        # largest batch we accept in one message
WRITER_BATCH_LOGS = 1000     # the writer groups waiting batches into one database transaction
IDLE_TIMEOUT = 60            # close a connection that sends nothing for 60 s (the agent reconnects)
COMMIT_TIMEOUT = 30          # how long a connection thread waits for the writer

SCHEMA = """
    CREATE TABLE IF NOT EXISTS logs (
        row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        id          TEXT UNIQUE NOT NULL,   -- UNIQUE: a resent log is ignored
        timestamp   TEXT NOT NULL,
        received_at TEXT NOT NULL,
        service     TEXT NOT NULL,
        level       TEXT NOT NULL,
        host        TEXT,
        trace_id    TEXT,
        seq         INTEGER,
        message     TEXT NOT NULL
    )
"""
INSERT_SQL = """
    INSERT OR IGNORE INTO logs
        (id, timestamp, received_at, service, level, host, trace_id, seq, message)
    VALUES
        (:id, :timestamp, :received_at, :service, :level, :host, :trace_id, :seq, :message)
"""


def raise_interrupt(signum, frame):
    """Turn SIGTERM / SIGINT into KeyboardInterrupt so the normal shutdown code runs."""
    raise KeyboardInterrupt


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    for col in ("timestamp", "service", "level", "trace_id"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_logs_{col} ON logs({col})")
    conn.commit()
    conn.close()


def validate(log):
    """Check one log and return a clean row. Raises ValueError with a readable reason."""
    if not isinstance(log, dict):
        raise ValueError("a log must be a JSON object")
    for key in ("id", "timestamp", "service", "level", "message"):
        if not isinstance(log.get(key), str) or not log[key].strip():
            raise ValueError(f"missing or empty field '{key}'")
    level = log["level"].strip().upper()
    if level == "WARNING":
        level = "WARN"
    if level not in LEVELS:
        raise ValueError(f"invalid level '{log['level']}' (use INFO, WARN or ERROR)")
    seq = log.get("seq")
    if seq is not None and not isinstance(seq, int):
        raise ValueError("seq must be an integer")
    return {
        "id": log["id"], "timestamp": log["timestamp"], "service": log["service"],
        "level": level, "host": str(log.get("host") or "unknown"),
        "trace_id": log.get("trace_id"), "seq": seq, "message": log["message"],
    }


class Pending:
    """A validated batch waiting for the writer thread to commit it."""

    def __init__(self, rows):
        self.rows = rows
        self.done = threading.Event()
        self.ok = False


class Collector:
    def __init__(self, name, db_path, max_pending):
        self.name = name
        self.db_path = db_path
        self.max_pending = max_pending
        self.queue = queue.Queue()
        self.pending_logs = 0                  # logs accepted but not yet committed
        self.active_conns = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.stats = {"connections": 0, "batches": 0, "logs": 0, "busy": 0, "rejected": 0}

    # ---------------------------------------------------------------- writer thread
    def writer(self):
        """Takes waiting batches, saves them in ONE transaction, then wakes the connection threads."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        while True:
            try:
                first = self.queue.get(timeout=0.2)
            except queue.Empty:
                if self.stop.is_set():
                    break
                continue
            items, total = [first], len(first.rows)
            while total < WRITER_BATCH_LOGS:
                try:
                    nxt = self.queue.get_nowait()
                except queue.Empty:
                    break
                items.append(nxt)
                total += len(nxt.rows)
            try:
                with conn:                      # commits on success, rolls back on error
                    conn.executemany(INSERT_SQL, [row for it in items for row in it.rows])
                ok = True
            except sqlite3.Error as e:
                print(f"[{self.name}] database error: {e}", flush=True)
                ok = False
            for it in items:
                it.ok = ok
                it.done.set()                   # only now does the agent get its ack
            with self.lock:
                self.pending_logs -= total
        conn.close()

    # ---------------------------------------------------------------- heartbeat to the SDN controller
    def heartbeat(self, addr, interval):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self.stop.is_set():
            beat = {"id": self.name, "pending": self.pending_logs, "conns": self.active_conns,
                    "logs": self.stats["logs"], "t": time.time()}
            try:
                sock.sendto(json.dumps(beat).encode(), addr)
            except OSError:
                pass                            # network not ready yet: try again next time
            self.stop.wait(interval)
        sock.close()

    # ---------------------------------------------------------------- one message
    def dispatch(self, msg):
        if not isinstance(msg, dict):
            return {"type": "error", "code": "invalid", "reason": "message must be a JSON object"}
        kind = msg.get("type")
        if kind == "ping":
            return {"type": "pong", "pending": self.pending_logs}
        if kind != "batch":
            return {"type": "error", "code": "invalid", "reason": f"unknown message type '{kind}'"}

        logs = msg.get("logs")
        if not isinstance(logs, list):
            return {"type": "error", "code": "invalid", "reason": "'logs' must be a list"}
        if len(logs) > min(MAX_BATCH_LOGS, self.max_pending):
            return {"type": "error", "code": "invalid", "reason": f"batch too large ({len(logs)} logs)"}
        received_at = now_iso()
        rows = []
        for i, log in enumerate(logs):
            try:
                row = validate(log)
            except ValueError as e:
                with self.lock:
                    self.stats["rejected"] += 1
                return {"type": "error", "code": "invalid", "reason": f"log #{i}: {e}"}
            row["received_at"] = received_at
            rows.append(row)
        if not rows:
            return {"type": "ack", "count": 0}

        with self.lock:                          # backpressure: refuse instead of queueing forever
            if self.pending_logs + len(rows) > self.max_pending:
                self.stats["busy"] += 1
                return {"type": "busy", "retry_after": 2}
            self.pending_logs += len(rows)
        item = Pending(rows)
        self.queue.put(item)
        if not item.done.wait(COMMIT_TIMEOUT) or not item.ok:
            return {"type": "error", "code": "storage", "reason": "could not save the logs, retry"}
        with self.lock:
            self.stats["batches"] += 1
            self.stats["logs"] += len(rows)
        return {"type": "ack", "count": len(rows)}

    # ---------------------------------------------------------------- one connection
    def handle(self, conn, addr):
        who = f"{addr[0]}:{addr[1]}"
        conn.settimeout(IDLE_TIMEOUT)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            self.stats["connections"] += 1
            self.active_conns += 1
        print(f"[{self.name}] {who} connected", flush=True)
        try:
            while not self.stop.is_set():
                send_msg(conn, self.dispatch(recv_msg(conn)))
        except (ConnectionError, socket.timeout):
            pass                                 # the agent left, or was idle for too long
        except ValueError as e:                  # bad frame or invalid JSON: we lost track of the stream
            print(f"[{self.name}] {who} sent a bad message ({e}), closing the connection", flush=True)
            try:
                send_msg(conn, {"type": "error", "code": "invalid", "reason": "bad frame"})
            except OSError:
                pass
        except OSError:
            pass
        finally:
            conn.close()
            with self.lock:
                self.active_conns -= 1
            print(f"[{self.name}] {who} disconnected", flush=True)

    # ---------------------------------------------------------------- the server
    def serve(self, host, port, heartbeat_addr, hb_interval):
        signal.signal(signal.SIGINT, raise_interrupt)
        signal.signal(signal.SIGTERM, raise_interrupt)
        init_db(self.db_path)
        threading.Thread(target=self.writer, daemon=True).start()
        if heartbeat_addr:
            threading.Thread(target=self.heartbeat, args=(heartbeat_addr, hb_interval), daemon=True).start()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(128)
        srv.settimeout(0.5)                      # wake up regularly so Ctrl+C works
        print(f"[{self.name}] listening on {host}:{port}, database {self.db_path}", flush=True)
        try:
            while True:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            print(f"\n[{self.name}] shutting down, finishing the logs that are waiting ...", flush=True)
        finally:
            srv.close()
            self.stop.set()
            time.sleep(0.3)
            print(f"[{self.name}] summary: {self.stats}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="TCP log collector")
    parser.add_argument("--id", default="c1", help="collector name (c1, c2)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--db", default=str(ROOT / "results" / "collector.db"))
    parser.add_argument("--max-pending", type=int, default=10000,
                        help="logs allowed to wait for the database before answering 'busy'")
    parser.add_argument("--heartbeat", default=None, help="controller address host:port for UDP heartbeats")
    parser.add_argument("--hb-interval", type=float, default=0.5)
    args = parser.parse_args()
    hb = parse_hostport(args.heartbeat, 9100) if args.heartbeat else None
    Collector(args.id, args.db, args.max_pending).serve(args.host, args.port, hb, args.hb_interval)


if __name__ == "__main__":
    main()
