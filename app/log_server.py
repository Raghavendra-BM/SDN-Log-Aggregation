"""
Central UDP log server: receives the logs of all hosts, timestamps them, stores them and counts losses.

    python3 app/log_server.py --db results/demo/server.db --bind 0.0.0.0

It listens on one UDP port per severity class (5001 critical, 5002 important, 5003 normal, see
common/udp_protocol.py) and on the sink port 6000, where the congestion generator sends its traffic.
The sink only counts what arrives, so we know how much background traffic really got through.

For every log it stores   sent_at (from the client)   recv_at (now)   delay_ms = recv_at - sent_at
and it keeps, per (host, service, severity) stream, the highest sequence number seen. A gap in the sequence
numbers is a lost log, so   lost = highest seq - number received   (logs lost at the very end of a stream are
invisible to this method; the experiments therefore also use the client's own sent-counts).

The network flow statistics come from the SDN controller (port and flow counters); this server reports the
application's point of view: who sent what, what arrived, how fast.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.udp_protocol import CLASS_OF_PORT, FLOOD_PORT, LOG_PORTS, decode  # noqa: E402

SCHEMA = """
    CREATE TABLE IF NOT EXISTS logs (
        row_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        host     TEXT NOT NULL,
        service  TEXT NOT NULL,
        severity TEXT NOT NULL,
        seq      INTEGER NOT NULL,
        sent_at  REAL NOT NULL,      -- seconds since 1970, written by the client
        recv_at  REAL NOT NULL,      -- seconds since 1970, written by this server
        delay_ms REAL NOT NULL,      -- recv_at - sent_at
        port     INTEGER NOT NULL,   -- destination port the log arrived on (= its priority class)
        message  TEXT NOT NULL
    )
"""
INSERT = ("INSERT INTO logs (host, service, severity, seq, sent_at, recv_at, delay_ms, port, message) "
          "VALUES (?,?,?,?,?,?,?,?,?)")


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    for col in ("severity", "recv_at", "host"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_logs_{col} ON logs({col})")
    conn.commit()
    conn.close()


class LogServer:
    def __init__(self, db_path, bind, rcvbuf):
        self.db_path, self.bind, self.rcvbuf = db_path, bind, rcvbuf
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.received = {"critical": 0, "important": 0, "normal": 0}
        self.bad = 0
        self.flood_packets = self.flood_bytes = 0
        self.streams = {}                       # (host, svc, sev) -> [highest seq, number received]

    # ------------------------------------------------------------ one receiving thread per port
    def listen(self, port, is_sink=False):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)   # a bigger buffer = fewer drops in the OS
        sock.bind((self.bind, port))
        sock.settimeout(0.3)
        cls = CLASS_OF_PORT.get(port)
        while not self.stop.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.time()                   # the receive timestamp is taken as early as possible
            if is_sink:
                with self.lock:
                    self.flood_packets += 1
                    self.flood_bytes += len(data)
                continue
            try:
                d = decode(data)
            except ValueError:
                with self.lock:
                    self.bad += 1
                continue
            with self.lock:
                self.received[cls] += 1
                st = self.streams.setdefault((d["h"], d["svc"], d["sev"]), [0, 0])
                st[0] = max(st[0], d["seq"])
                st[1] += 1
            self.q.put((d["h"], d["svc"], d["sev"], d["seq"], d["t"], now, round((now - d["t"]) * 1000, 3),
                        port, d["msg"]))
        sock.close()

    # ------------------------------------------------------------ one writer thread (SQLite is single writer)
    def writer(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        rows = []
        last_flush = time.time()
        while True:
            try:
                rows.append(self.q.get(timeout=0.2))
            except queue.Empty:
                if self.stop.is_set() and not rows:
                    break
            if rows and (len(rows) >= 500 or time.time() - last_flush > 0.5 or self.stop.is_set()):
                with conn:
                    conn.executemany(INSERT, rows)
                rows, last_flush = [], time.time()
        conn.close()

    # ------------------------------------------------------------ numbers
    def lost_by_severity(self):
        out = {}
        with self.lock:
            for (_h, _s, sev), (highest, got) in self.streams.items():
                out[sev] = out.get(sev, 0) + max(0, highest - got)
        return out

    def summary(self):
        with self.lock:
            return {"received_by_class": dict(self.received), "bad_datagrams": self.bad,
                    "sink_packets": self.flood_packets, "sink_bytes": self.flood_bytes,
                    "streams": {f"{h}/{s}/{v}": {"highest_seq": a, "received": b}
                                for (h, s, v), (a, b) in self.streams.items()}}

    def serve(self, summary_path, report_every):
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        init_db(self.db_path)
        threads = [threading.Thread(target=self.listen, args=(p,), daemon=True) for p in LOG_PORTS]
        threads.append(threading.Thread(target=self.listen, args=(FLOOD_PORT, True), daemon=True))
        threads.append(threading.Thread(target=self.writer))
        for t in threads:
            t.start()
        print(f"[server] listening on {self.bind} UDP ports {LOG_PORTS} (+ sink {FLOOD_PORT}), database {self.db_path}",
              flush=True)
        last = time.time()
        while not self.stop.is_set():
            time.sleep(0.2)
            if report_every and time.time() - last >= report_every:
                last = time.time()
                with self.lock:
                    print(f"[server] received {self.received}  sink {self.flood_packets} pkts", flush=True)
        for t in threads:
            t.join(timeout=3)
        s = self.summary()
        s["lost_by_severity_from_gaps"] = self.lost_by_severity()
        print(f"[server] stopped: {s['received_by_class']}, lost (from sequence gaps) {s['lost_by_severity_from_gaps']}",
              flush=True)
        if summary_path:
            Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
            Path(summary_path).write_text(json.dumps(s, indent=1))


def main():
    p = argparse.ArgumentParser(description="Central UDP log server")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--db", default=str(ROOT / "results" / "server.db"))
    p.add_argument("--summary", default=None, help="write a JSON summary when the server stops")
    p.add_argument("--rcvbuf", type=int, default=4 * 1024 * 1024)
    p.add_argument("--report-every", type=float, default=0, help="print a status line every N seconds")
    a = p.parse_args()
    LogServer(a.db, a.bind, a.rcvbuf).serve(a.summary, a.report_every)


if __name__ == "__main__":
    main()
