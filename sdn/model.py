"""
The decision logic of the SDN controller, written as plain Python (no OpenFlow in here),
so that it can be unit tested without a network:

  Topology            the network graph, which links are up, shortest-path routes
  CongestionDetector  turns port byte counters into Mbit/s and decides when the network is congested
                      (and when the congestion is over)
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


# ====================================================================== congestion detection
class CongestionDetector:
    """Looks at the transmit counters of the core ports and decides whether the network is congested.

    * every call to update() turns a byte counter into Mbit/s for that port
    * the network becomes CONGESTED when some port stays above `congestion_mbps` for `congestion_samples`
      polls in a row
    * it becomes CLEAR again only after ALL ports stayed below `clear_mbps` for `clear_samples` polls in a
      row. The gap between the two thresholds (hysteresis) stops the controller from flapping on and off.
    """

    def __init__(self, congestion_mbps, congestion_samples, clear_mbps, clear_samples):
        self.hi, self.hi_n = congestion_mbps, congestion_samples
        self.lo, self.lo_n = clear_mbps, clear_samples
        self.last = {}                 # port key -> (time, tx_bytes)
        self.rates = {}                # port key -> latest Mbit/s
        self.congested = False
        self._over = 0
        self._quiet = 0

    def update(self, key, tx_bytes, now):
        """Record one counter reading. Returns the port's rate in Mbit/s (0.0 for the first reading)."""
        prev = self.last.get(key)
        self.last[key] = (now, tx_bytes)
        if prev is None or now <= prev[0] or tx_bytes < prev[1]:     # first reading, or the counter was reset
            self.rates[key] = 0.0
            return 0.0
        mbps = (tx_bytes - prev[1]) * 8 / (now - prev[0]) / 1e6
        self.rates[key] = mbps
        return mbps

    def evaluate(self):
        """Call once after all ports of one polling round were updated.
        Returns "start" or "end" when the state just changed, otherwise None."""
        busiest = max(self.rates.values(), default=0.0)
        self._over = self._over + 1 if busiest > self.hi else 0
        self._quiet = self._quiet + 1 if busiest < self.lo else 0
        if not self.congested and self._over >= self.hi_n:
            self.congested, self._quiet = True, 0
            return "start"
        if self.congested and self._quiet >= self.lo_n:
            self.congested, self._over = False, 0
            return "end"
        return None

    def busiest(self):
        """(port key, Mbit/s) of the busiest port in the latest round."""
        if not self.rates:
            return None, 0.0
        key = max(self.rates, key=self.rates.get)
        return key, self.rates[key]
