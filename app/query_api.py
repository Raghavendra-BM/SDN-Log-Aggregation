"""
Read-only HTTP API for the web dashboard (ui/). It reads the log server's database (server.db) and the
controller's event log, and answers in the format the dashboard expects.

Run it on the Ubuntu machine itself (NOT inside Mininet), while the demo is running:

    python3 app/query_api.py --db results/demo/server.db --events results/demo/controller_events.jsonl

    GET /logs?service=&level=&q=&from=&to=&since_id=&limit=   ->  {"count": n, "logs": [...]}   newest first
    GET /stats                                                ->  totals, counts per severity, estimated lost logs
    GET /events?limit=50                                      ->  controller events (congestion start/end, link failures)

Every log carries delay_ms (delivery time) so the dashboard can show it.
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LEVEL_ALIASES = {"WARN": "WARNING"}          # the old dashboard said WARN


def to_epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def to_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    def __init__(self, db_path, events_path):
        self.db_path = Path(db_path)
        self.events_path = Path(events_path) if events_path else None

    def _connect(self):
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def logs(self, q):
        conn = self._connect()
        if conn is None:
            return {"count": 0, "logs": []}
        where, params = [], []
        if q.get("service"):
            where.append("service = ?"); params.append(q["service"])
        if q.get("level"):
            level = q["level"].upper()
            where.append("severity = ?"); params.append(LEVEL_ALIASES.get(level, level))
        if q.get("q"):
            where.append("message LIKE ?"); params.append(f"%{q['q']}%")
        if q.get("from"):
            where.append("sent_at >= ?"); params.append(to_epoch(q["from"]))
        if q.get("to"):
            where.append("sent_at <= ?"); params.append(to_epoch(q["to"]))
        if q.get("since_id"):
            where.append("row_id > ?"); params.append(int(q["since_id"]))
        limit = max(1, min(int(q.get("limit", 100)), 1000))
        sql = "SELECT row_id, host, service, severity, seq, sent_at, recv_at, delay_ms, message FROM logs"
        sql += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY row_id DESC LIMIT ?"
        try:
            rows = conn.execute(sql, params + [limit]).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        logs = [{"row_id": r["row_id"], "id": f"{r['host']}/{r['service']}/{r['severity']}/{r['seq']}",
                 "timestamp": to_iso(r["sent_at"]), "received_at": to_iso(r["recv_at"]), "service": r["service"],
                 "level": r["severity"], "host": r["host"], "seq": r["seq"], "message": r["message"],
                 "delay_ms": r["delay_ms"]} for r in rows]
        return {"count": len(logs), "logs": logs}

    def stats(self):
        conn = self._connect()
        by_level, total, lost, avg_delay, last_minute = {}, 0, 0, None, 0
        if conn is not None:
            try:
                for sev, n in conn.execute("SELECT severity, COUNT(*) FROM logs GROUP BY severity"):
                    by_level[sev] = n
                    total += n
                # a gap in the sequence numbers of one stream = a lost log
                for (gap,) in conn.execute("SELECT MAX(seq) - COUNT(*) FROM logs GROUP BY host, service, severity"):
                    lost += max(0, gap)
                avg_delay = conn.execute("SELECT AVG(delay_ms) FROM logs").fetchone()[0]
                last_minute = conn.execute("SELECT COUNT(*) FROM logs WHERE recv_at >= ?", (time.time() - 60,)).fetchone()[0]
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        return {"total_logs": total, "by_level": by_level, "lost_logs": lost, "logs_last_minute": last_minute,
                "avg_delay_ms": round(avg_delay, 2) if avg_delay is not None else None}

    def events(self, limit):
        if not self.events_path or not self.events_path.exists():
            return {"events": []}
        events = []
        for line in self.events_path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") != "stats":                  # the per-poll statistics lines are too many for a list
                events.append(e)
        return {"events": list(reversed(events[-limit:]))}


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
                    self._send(200, {"message": "UDP log query API", "endpoints": ["/logs", "/stats", "/events"]})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})

        def log_message(self, *args):
            pass                                              # keep the terminal quiet

    return Handler


def main():
    p = argparse.ArgumentParser(description="Read-only API for the dashboard")
    p.add_argument("--db", default="results/demo/server.db", help="the log server's database")
    p.add_argument("--events", default=None, help="controller event log (jsonl)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Store(args.db, args.events)))
    print(f"[query-api] http://{args.host}:{args.port}  reading {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
