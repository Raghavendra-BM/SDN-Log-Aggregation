"""
Read-only HTTP API for the web dashboard. It merges the databases of both collectors
(c1.db and c2.db) and answers in the same format as the old single-collector API, so the
existing dashboard works unchanged. It also serves the controller's event log.

Run it on the Ubuntu machine itself (NOT inside Mininet):

    python3 app/query_api.py --run-dir results/demo --events results/demo/controller_events.jsonl

    GET /logs?service=&level=&q=&from=&to=&since_id=&limit=   ->  {"count": n, "logs": [...]}   newest first
    GET /stats                                                ->  totals, counts per level and per collector
    GET /events?limit=50                                      ->  controller events (link down, backend down, flood block ...)
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

COLUMNS = "id, timestamp, received_at, service, level, host, trace_id, seq, message"
COLLECTORS = ("c1", "c2")


def to_ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_row_id(received_at, local_rowid, collector_index):
    """One id that grows with receive time across BOTH databases (needed for live tail with since_id).
    Stays below 2^53, so JavaScript can hold it exactly."""
    return to_ms(received_at) * 1000 + (local_rowid % 500) * 2 + collector_index


class Store:
    def __init__(self, run_dir, events_path):
        self.run_dir = Path(run_dir)
        self.events_path = Path(events_path) if events_path else None

    def _connect(self, name):
        path = self.run_dir / f"{name}.db"
        if not path.exists():
            return None
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def logs(self, q):
        service, level, text = q.get("service"), q.get("level"), q.get("q")
        t_from, t_to = q.get("from"), q.get("to")
        since = int(q["since_id"]) if q.get("since_id") else None
        limit = max(1, min(int(q.get("limit", 100)), 1000))
        out = []
        for index, name in enumerate(COLLECTORS):
            conn = self._connect(name)
            if conn is None:
                continue
            where, params = [], []
            if service:
                where.append("service = ?"); params.append(service)
            if level:
                where.append("level = ?"); params.append(level.upper())
            if text:
                where.append("message LIKE ?"); params.append(f"%{text}%")
            if t_from:
                where.append("timestamp >= ?"); params.append(t_from)
            if t_to:
                where.append("timestamp <= ?"); params.append(t_to)
            if since is not None:
                where.append("received_at >= ?"); params.append(from_ms(since // 1000))
            sql = f"SELECT row_id, {COLUMNS} FROM logs" + (" WHERE " + " AND ".join(where) if where else "")
            sql += " ORDER BY row_id DESC LIMIT ?"
            try:
                for r in conn.execute(sql, params + [limit]):
                    d = dict(r)
                    d["collector"] = name
                    d["row_id"] = make_row_id(d["received_at"], d["row_id"], index)
                    if since is None or d["row_id"] > since:
                        out.append(d)
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        out.sort(key=lambda d: d["row_id"], reverse=True)
        return {"count": len(out[:limit]), "logs": out[:limit]}

    def stats(self):
        total, by_level, per_collector = 0, {}, {}
        for name in COLLECTORS:
            conn = self._connect(name)
            per_collector[name] = 0
            if conn is None:
                continue
            try:
                for lvl, n in conn.execute("SELECT level, COUNT(*) FROM logs GROUP BY level"):
                    by_level[lvl] = by_level.get(lvl, 0) + n
                    total += n
                    per_collector[name] += n
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        return {"queue_size": 0, "total_logs": total, "by_level": by_level, "by_collector": per_collector}

    def events(self, limit):
        if not self.events_path or not self.events_path.exists():
            return {"events": []}
        lines = self.events_path.read_text().splitlines()[-limit:]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        return {"events": list(reversed(events))}


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/logs":
                    self._send(200, store.logs(q))
                elif url.path == "/stats":
                    self._send(200, store.stats())
                elif url.path == "/events":
                    self._send(200, store.events(int(q.get("limit", 50))))
                elif url.path == "/":
                    self._send(200, {"message": "SDN log query API", "endpoints": ["/logs", "/stats", "/events"]})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})

        def log_message(self, *args):
            pass                                              # keep the terminal quiet

    return Handler


def main():
    p = argparse.ArgumentParser(description="Read-only API for the dashboard")
    p.add_argument("--run-dir", default="results/demo", help="folder that contains c1.db and c2.db")
    p.add_argument("--events", default=None, help="controller event log (jsonl)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Store(args.run_dir, args.events)))
    print(f"[query-api] http://{args.host}:{args.port}  reading {args.run_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
