"""
The decision logic of the SDN controller, written as plain Python (no OpenFlow in here),
so that it can be unit tested without a network:

  Topology     the network graph, which links are up, shortest-path routes
  BackendPool  the collectors behind the virtual IP: health from heartbeats, least-loaded choice
  RateMonitor  flood detection from port byte counters
"""
import heapq
import json
import re
import time

INF = float("inf")


def load_config(path):
    with open(path) as f:
        return json.load(f)


def parse_ms(text):
    """'5ms' -> 5.0 (used as the link weight: the path with the smallest total delay wins)"""
    m = re.match(r"^\s*([\d.]+)\s*ms\s*$", str(text))
    return float(m.group(1)) if m else 1.0


# ====================================================================== topology and routing
class Topology:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dpid_of = {name: int(dpid) for name, dpid in cfg["switches"].items()}
        self.name_of = {dpid: name for name, dpid in self.dpid_of.items()}
        self.hosts = cfg["hosts"]
        self.links = [{"a": l["a"], "ap": l["a_port"], "b": l["b"], "bp": l["b_port"],
                       "w": parse_ms(l.get("delay", "1ms"))} for l in cfg["links"]]
        self.down = set()          # (dpid, port) pairs that the switches reported as down

    def link_up(self, link):
        return ((self.dpid_of[link["a"]], link["ap"]) not in self.down
                and (self.dpid_of[link["b"]], link["bp"]) not in self.down)

    def set_port_state(self, dpid, port, up):
        """Record a port going up or down. Returns True if the state of some link changed."""
        before = [self.link_up(l) for l in self.links]
        if up:
            self.down.discard((dpid, port))
        else:
            self.down.add((dpid, port))
        return before != [self.link_up(l) for l in self.links]

    def compute_routes(self):
        """{dpid: {host_ip: out_port}}  Shortest paths (weight = link delay) over the links that are up.
        A switch that cannot reach a host simply has no entry for it."""
        adj = {name: [] for name in self.dpid_of}
        for l in self.links:
            if self.link_up(l):
                adj[l["a"]].append((l["b"], l["w"], l["bp"]))   # (neighbour, weight, port ON THE NEIGHBOUR toward us)
                adj[l["b"]].append((l["a"], l["w"], l["ap"]))
        routes = {dpid: {} for dpid in self.dpid_of.values()}
        for h in self.hosts.values():
            dest = h["switch"]
            dist, out_port = {dest: 0.0}, {}
            heap = [(0.0, dest)]
            while heap:                                           # Dijkstra, starting at the destination
                d, u = heapq.heappop(heap)
                if d > dist.get(u, INF):
                    continue
                for v, w, port_on_v in adj[u]:
                    if d + w < dist.get(v, INF):
                        dist[v] = d + w
                        out_port[v] = port_on_v                   # v sends toward u through this port
                        heapq.heappush(heap, (d + w, v))
            for sw in dist:
                routes[self.dpid_of[sw]][h["ip"]] = h["port"] if sw == dest else out_port[sw]
        return routes

    def path(self, src_switch, dst_switch):
        """List of switch names a packet follows (for logs and tests)."""
        routes = self.compute_routes()
        dst_ip = next((h["ip"] for h in self.hosts.values() if h["switch"] == dst_switch), None)
        path, cur = [src_switch], src_switch
        while cur != dst_switch and len(path) <= len(self.dpid_of):
            port = routes[self.dpid_of[cur]].get(dst_ip)
            if port is None:
                return None
            nxt = next((l["b"] if l["a"] == cur and l["ap"] == port else l["a"]
                        for l in self.links if (l["a"] == cur and l["ap"] == port)
                        or (l["b"] == cur and l["bp"] == port)), None)
            if nxt is None:
                return None
            path.append(nxt)
            cur = nxt
        return path if cur == dst_switch else None


# ====================================================================== the collectors behind the virtual IP
class BackendPool:
    """Tracks which collectors are alive (heartbeats) and picks the least-loaded one for a new connection."""

    def __init__(self, backends, timeout, now=None):
        """backends: list of (name, ip, mac)"""
        t = time.time() if now is None else now
        self.timeout = timeout
        self.b = {}
        for i, (name, ip, mac) in enumerate(backends):
            self.b[name] = {"ip": ip, "mac": mac, "cookie": 0x100 + i, "last_hb": t, "alive": True,
                            "pending": 0, "conns": 0, "assigned": 0, "served": 0}

    def heartbeat(self, name, info, now):
        """Returns True if this collector was considered dead until now (it came back)."""
        b = self.b.get(name)
        if b is None:
            return False
        was_dead = not b["alive"]
        b["last_hb"] = now
        b["alive"] = True
        b["pending"] = int(info.get("pending", 0))
        b["conns"] = int(info.get("conns", 0))
        return was_dead

    def is_alive(self, name, now):
        b = self.b[name]
        return b["alive"] and (now - b["last_hb"]) <= self.timeout

    def expire(self, now):
        """Mark collectors without a recent heartbeat as dead. Returns the names that just died."""
        died = []
        for name, b in self.b.items():
            if b["alive"] and (now - b["last_hb"]) > self.timeout:
                b["alive"] = False
                died.append(name)
        return died

    def load(self, name):
        b = self.b[name]
        return b["assigned"] + b["pending"] / 500.0

    def choose(self, now):
        """Least-loaded live collector, or None. Counts the new connection against the choice."""
        live = [n for n in self.b if self.is_alive(n, now)]
        if not live:
            return None
        name = min(live, key=lambda n: (self.load(n), self.b[n]["served"], n))
        self.b[name]["assigned"] += 1
        self.b[name]["served"] += 1
        return name

    def release(self, name):
        self.b[name]["assigned"] = max(0, self.b[name]["assigned"] - 1)


# ====================================================================== flood detection
class RateMonitor:
    """Turns port byte counters into Mbit/s and decides when a port has been too busy for too long."""

    def __init__(self, threshold_mbps, samples_needed):
        self.threshold = threshold_mbps
        self.needed = samples_needed
        self.last = {}
        self.over = {}

    def update(self, key, rx_bytes, now):
        """Returns (mbps, should_block)."""
        prev = self.last.get(key)
        self.last[key] = (now, rx_bytes)
        if prev is None or now <= prev[0]:
            return 0.0, False
        mbps = (rx_bytes - prev[1]) * 8 / (now - prev[0]) / 1e6
        self.over[key] = self.over.get(key, 0) + 1 if mbps > self.threshold else 0
        if self.over[key] >= self.needed:
            self.over[key] = 0
            return mbps, True
        return mbps, False
