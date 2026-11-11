"""
SDN controller (Ryu, OpenFlow 1.3) that protects CRITICAL logs when the network is congested.

Start it with:   SDN_MODE=dynamic ryu-manager sdn/controller.py
                 SDN_MODE=static  ryu-manager sdn/controller.py       (the baseline for the comparison)

The application sends every log as one UDP datagram. The destination port tells the network how important the
log is (5001 critical, 5002 important, 5003 normal, see common/udp_protocol.py). Every core switch port has
three output queues (created by sdn/topo.py): queue 2 for critical, 1 for important, 0 for everything else.
The queues are served by priority, so a full "normal" queue can never block a critical log.

STATIC  baseline  shortest-path routes, installed once. Logs are forwarded but never put into a queue, so
                  critical logs share one first-in-first-out line with all the other traffic.
DYNAMIC           1. Statistics    every 0.5 s the controller reads the port counters of all core links and the
                                   flow counters of the edge switch (Mbit/s per port, packets per log class)
                  2. Congestion    a port above 8 Mbit/s twice in a row = congestion (10 Mbit/s links)
                  3. Prioritising  the flow rules for the log ports are rewritten to send critical and
                                   important logs through their own queues; they are removed again after the
                                   traffic has been calm for 2 s
                  4. Link failure  port-status events -> recompute shortest paths -> update the routes
                                   (the queue rules follow the new route)

Every decision is written to the file named in EVENT_LOG as one JSON line, which the experiments use.
Flow table (one table): priority 20 = log-class rules (UDP port -> queue -> output), priority 10 = routes.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from ryu.lib import hub
    from ryu.ofproto import ofproto_v1_3
except ImportError:                     # os-ken is the maintained fork of Ryu, same API
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from os_ken.lib import hub
    from os_ken.ofproto import ofproto_v1_3

from common.udp_protocol import PORT_OF_CLASS  # noqa: E402
from model import CongestionDetector, Topology, load_config  # noqa: E402

TABLE = 0
PRIO_ROUTE, PRIO_CLASS = 10, 20
CLASS_COOKIE = {"critical": 0xC100, "important": 0xC101, "normal": 0xC102}
CLASS_COOKIE_MASK = 0xFFFFFFFFFFFFFFF0          # matches 0xC100 ... 0xC10F
ETH_IP, IP_UDP = 0x0800, 17


class PriorityController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = os.environ.get("SDN_MODE", "dynamic").lower()
        cfg = load_config(os.environ.get("TOPO_FILE", os.path.join(ROOT, "config", "topology.json")))
        self.cfg = cfg
        self.event_path = os.environ.get("EVENT_LOG")
        pol = cfg["policy"]
        self.stats_interval = pol["stats_interval_s"]

        self.topo = Topology(cfg)
        self.server_ip = next(h["ip"] for h in cfg["hosts"].values() if h["role"] == "server")
        self.queue_of = {c: cfg["qos"]["queues"][c]["id"] for c in PORT_OF_CLASS}
        self.edge_switches = {self.topo.dpid_of[h["switch"]] for h in cfg["hosts"].values()
                              if h["role"] in ("source", "generator")}
        # every core port: its transmit counter tells us how busy that link is
        self.core_ports = {}
        for l in cfg["links"]:
            self.core_ports[(self.topo.dpid_of[l["a"]], l["a_port"])] = f"{l['a']}:{l['a_port']}"
            self.core_ports[(self.topo.dpid_of[l["b"]], l["b_port"])] = f"{l['b']}:{l['b_port']}"
        self.detector = CongestionDetector(pol["congestion_mbps"], pol["congestion_samples"],
                                           pol["clear_mbps"], pol["clear_samples"])

        self.datapaths = {}       # dpid -> datapath
        self.installed = {}       # dpid -> {host_ip: out_port}  routes currently in the switch
        self.prio_on = False      # are the critical/important logs currently using their own queues?
        self.flow_last = {}       # (dpid, cookie) -> (time, bytes, packets)
        self.flow_rate = {}       # (dpid, cookie) -> (Mbit/s, packets/s)

        self.log_event("controller_started", policy=pol)
        hub.spawn(self._tick_loop)

    # ================================================================ helpers
    def log_event(self, kind, quiet=False, **fields):
        rec = {"t": time.time(), "mode": self.mode, "event": kind}
        rec.update(fields)
        if not quiet:
            self.logger.info("EVENT %s", json.dumps(rec))
        if self.event_path:
            with open(self.event_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def add_flow(self, dp, priority, match, actions, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, list(actions))]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, cookie=cookie, table_id=TABLE, command=ofp.OFPFC_ADD, priority=priority,
            buffer_id=ofp.OFP_NO_BUFFER, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=match,
            instructions=inst))

    def delete_flows(self, dp, match=None, cookie=0, cookie_mask=0, strict_priority=None):
        """Delete flows. No arguments = wipe the switch. cookie+cookie_mask = every flow with that cookie.
        match + strict_priority = exactly one flow."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        kw = dict(datapath=dp, cookie=cookie, cookie_mask=cookie_mask, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                  match=match or parser.OFPMatch())
        if strict_priority is None:
            dp.send_msg(parser.OFPFlowMod(command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL, **kw))
        else:
            dp.send_msg(parser.OFPFlowMod(command=ofp.OFPFC_DELETE_STRICT, table_id=TABLE,
                                          priority=strict_priority, **kw))

    # ================================================================ switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.delete_flows(dp)                                   # start from a clean switch
        self.installed[dp.id] = {}
        self.install_routes(dp, self.topo.compute_routes().get(dp.id, {}))
        self.log_event("switch_connected", switch=self.topo.name_of.get(dp.id, dp.id))

    # ================================================================ routes
    def install_routes(self, dp, routes):
        """Make the flow table match `routes` ({host_ip: out_port}); only changes are sent."""
        parser = dp.ofproto_parser
        current = self.installed.setdefault(dp.id, {})
        for ip, port in routes.items():
            if current.get(ip) != port:
                self.add_flow(dp, PRIO_ROUTE, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=ip),
                              [parser.OFPActionOutput(port)])
                current[ip] = port
        for ip in [i for i in current if i not in routes]:           # destination no longer reachable
            self.delete_flows(dp, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=ip), strict_priority=PRIO_ROUTE)
            del current[ip]
        self.apply_class_rules(dp)                                   # the log rules follow the route

    def apply_class_rules(self, dp):
        """One rule per log class for traffic to the log server (UDP destination port = severity class).
        prio_on False: just forward (queue 0, the default).  prio_on True: send the class into its own queue."""
        parser = dp.ofproto_parser
        port = self.installed.get(dp.id, {}).get(self.server_ip)
        if port is None:                                             # the server is unreachable from here
            self.delete_flows(dp, cookie=CLASS_COOKIE["critical"], cookie_mask=CLASS_COOKIE_MASK)
            return
        for cls, udp_port in PORT_OF_CLASS.items():
            actions = []
            if self.prio_on:
                actions.append(parser.OFPActionSetQueue(self.queue_of[cls]))
            actions.append(parser.OFPActionOutput(port))
            match = parser.OFPMatch(eth_type=ETH_IP, ip_proto=IP_UDP, ipv4_dst=self.server_ip, udp_dst=udp_port)
            self.add_flow(dp, PRIO_CLASS, match, actions, cookie=CLASS_COOKIE[cls])

    def set_priority(self, on):
        self.prio_on = on
        for dp in self.datapaths.values():
            self.apply_class_rules(dp)

    # ================================================================ link failure
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def on_port_status(self, ev):
        if self.mode != "dynamic":
            return
        msg, dp = ev.msg, ev.msg.datapath
        ofp, desc = dp.ofproto, ev.msg.desc
        down = (bool(desc.state & ofp.OFPPS_LINK_DOWN) or bool(desc.config & ofp.OFPPC_PORT_DOWN)
                or msg.reason == ofp.OFPPR_DELETE)
        if self.topo.set_port_state(dp.id, desc.port_no, up=not down):
            self.log_event("link_state", switch=self.topo.name_of.get(dp.id), port=desc.port_no,
                           state="down" if down else "up")
            t0 = time.time()
            routes = self.topo.compute_routes()
            for dpid, d in self.datapaths.items():
                self.install_routes(d, routes.get(dpid, {}))
            self.log_event("routes_updated", reaction_ms=round((time.time() - t0) * 1000, 2),
                           path=self.topo.path("s1", "s4"))

    # ================================================================ statistics, congestion, priority
    def _tick_loop(self):
        while True:
            hub.sleep(self.stats_interval)
            self._evaluate_round()
            for dpid, dp in list(self.datapaths.items()):
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                if dpid in self.edge_switches:
                    dp.send_msg(parser.OFPFlowStatsRequest(dp, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY, ofp.OFPG_ANY,
                                                           0, 0, parser.OFPMatch()))

    def _evaluate_round(self):
        """Look at the newest port rates, decide, and write one statistics record."""
        if not self.detector.rates:
            return
        change = self.detector.evaluate()
        port, mbps = self.detector.busiest()
        if change == "start":
            t0 = time.time()
            reacted = self.mode == "dynamic"
            if reacted:
                self.set_priority(True)
            self.log_event("congestion_start", port=self.core_ports.get(port), mbps=round(mbps, 2),
                           reacted=reacted, reaction_ms=round((time.time() - t0) * 1000, 2))
        elif change == "end":
            if self.mode == "dynamic":
                self.set_priority(False)
            self.log_event("congestion_end", port=self.core_ports.get(port), mbps=round(mbps, 2))
        flows = {}
        for cls, cookie in CLASS_COOKIE.items():
            rates = [r for (d, c), r in self.flow_rate.items() if c == cookie]
            flows[cls] = {"mbps": round(sum(r[0] for r in rates), 3), "pps": round(sum(r[1] for r in rates), 1)}
        self.log_event("stats", quiet=True, busiest_port=self.core_ports.get(port), busiest_mbps=round(mbps, 2),
                       ports={self.core_ports[k]: round(v, 2) for k, v in self.detector.rates.items()},
                       flows=flows, congested=self.detector.congested, priority=self.prio_on)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats(self, ev):
        dp, now = ev.msg.datapath, time.time()
        for st in ev.msg.body:
            key = (dp.id, st.port_no)
            if key in self.core_ports:
                self.detector.update(key, st.tx_bytes, now)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats(self, ev):
        """Per log class: how many packets and Mbit/s did the edge switch forward? (flow statistics)"""
        dp, now = ev.msg.datapath, time.time()
        for st in ev.msg.body:
            if st.cookie not in CLASS_COOKIE.values():
                continue
            key = (dp.id, st.cookie)
            prev = self.flow_last.get(key)
            self.flow_last[key] = (now, st.byte_count, st.packet_count)
            if prev and now > prev[0] and st.byte_count >= prev[1]:
                dt = now - prev[0]
                self.flow_rate[key] = ((st.byte_count - prev[1]) * 8 / dt / 1e6, (st.packet_count - prev[2]) / dt)
