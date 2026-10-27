"""
SDN controller (Ryu, OpenFlow 1.3) for the distributed log aggregation system.

Start it with:   SDN_MODE=dynamic ryu-manager sdn/controller.py
                 SDN_MODE=static  ryu-manager sdn/controller.py       (the baseline for the comparison)

STATIC  baseline: routes on the primary path are installed once, the virtual IP is mapped to the first
        collector, and nothing ever changes. No reaction to failures or attacks.

DYNAMIC
  1. Link failure     port-status events -> recompute shortest paths -> update the routing table
  2. Load balancing   agents connect to a virtual IP. The first packet of every TCP connection goes to the
                      controller, which picks the least-loaded live collector and installs NAT rules
  3. Health checks    collectors send UDP heartbeats to a controller address (they arrive as packet-in).
                      A collector without heartbeat is declared dead and its flows are removed, so the
                      agents' reconnects are steered to the other collector
  4. Access control   only hosts with role "source" may use the virtual IP, everyone else is dropped
  5. Flood defence    port byte counters are polled; a host that sends too much is quarantined

Pipeline:  table 0 = classifier (NAT, ACL, quarantine, punt to controller)   table 1 = routing by destination IP
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from ryu.lib import hub
    from ryu.lib.packet import ipv4, packet, tcp, udp
    from ryu.ofproto import ofproto_v1_3
except ImportError:                     # os-ken is the maintained fork of Ryu, same API
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from os_ken.lib import hub
    from os_ken.lib.packet import ipv4, packet, tcp, udp
    from os_ken.ofproto import ofproto_v1_3

from model import BackendPool, RateMonitor, Topology, load_config  # noqa: E402

T_CLASSIFY, T_ROUTE = 0, 1
PRIO_ROUTE, PRIO_PUNT, PRIO_NAT, PRIO_ACL, PRIO_BLOCK = 10, 50, 100, 120, 200
BLOCK_COOKIE = 0xB10C
ALL_BITS = 0xFFFFFFFFFFFFFFFF
ETH_IP = 0x0800


class SdnLogController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mode = os.environ.get("SDN_MODE", "dynamic").lower()
        cfg = load_config(os.environ.get("TOPO_FILE", os.path.join(root, "config", "topology.json")))
        self.cfg = cfg
        self.event_path = os.environ.get("EVENT_LOG")
        pol = cfg["policy"]
        self.idle = pol["idle_timeout_s"]
        self.block_seconds = pol["block_seconds"]
        self.stats_interval = pol["stats_interval_s"]

        self.topo = Topology(cfg)
        self.vip_ip, self.vip_mac, self.vip_port = cfg["vip"]["ip"], cfg["vip"]["mac"], cfg["vip"]["port"]
        self.ctl_ip, self.ctl_port = cfg["ctl_vip"]["ip"], cfg["ctl_vip"]["port"]
        collectors = [(n, h["ip"], h["mac"]) for n, h in cfg["hosts"].items() if h["role"] == "collector"]
        self.pool = BackendPool(collectors, pol["heartbeat_timeout_s"])
        self.cookie_name = {b["cookie"]: n for n, b in self.pool.b.items()}
        self.authorized = {h["ip"] for h in cfg["hosts"].values() if h["role"] == "source"}
        self.edge_switches = {h["switch"] for h in cfg["hosts"].values() if h["role"] in ("source", "attacker")}
        # access ports we watch for floods: (dpid, port) -> host name
        self.watch = {(self.topo.dpid_of[h["switch"]], h["port"]): n
                      for n, h in cfg["hosts"].items() if h["role"] in ("source", "attacker")}
        self.monitor = RateMonitor(pol["flood_mbps"], pol["flood_samples"])

        self.datapaths = {}       # dpid -> datapath
        self.installed = {}       # dpid -> {host_ip: out_port}  routes currently in the switch
        self.conns = {}           # (client_ip, client_port) -> connection record
        self.blocked_until = {}   # (dpid, port) -> time

        self.log_event("controller_started", policy=pol)
        hub.spawn(self._tick_loop)

    # ================================================================ helpers
    def log_event(self, kind, **fields):
        rec = {"t": time.time(), "mode": self.mode, "event": kind}
        rec.update(fields)
        self.logger.info("EVENT %s", json.dumps(rec))
        if self.event_path:
            with open(self.event_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def add_flow(self, dp, table, priority, match, actions=(), goto=None, cookie=0, idle=0, hard=0, flags=0):
        """Install a flow. No actions and no goto means DROP."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = []
        if actions:
            inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, list(actions)))
        if goto is not None:
            inst.append(parser.OFPInstructionGotoTable(goto))
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, cookie=cookie, table_id=table, command=ofp.OFPFC_ADD, idle_timeout=idle,
            hard_timeout=hard, priority=priority, buffer_id=ofp.OFP_NO_BUFFER, out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY, flags=flags, match=match, instructions=inst))

    def delete_flows(self, dp, table, match=None, cookie=0, cookie_mask=0, strict_priority=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        kw = dict(datapath=dp, cookie=cookie, cookie_mask=cookie_mask, table_id=table,
                  out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=match or parser.OFPMatch())
        if strict_priority is None:
            dp.send_msg(parser.OFPFlowMod(command=ofp.OFPFC_DELETE, **kw))
        else:
            dp.send_msg(parser.OFPFlowMod(command=ofp.OFPFC_DELETE_STRICT, priority=strict_priority, **kw))

    # ================================================================ switch connects: base rules
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.install_base(dp)
        self.install_routes(dp, self.topo.compute_routes().get(dp.id, {}))
        self.log_event("switch_connected", switch=self.topo.name_of.get(dp.id, dp.id))

    def install_base(self, dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.delete_flows(dp, ofp.OFPTT_ALL)                          # start from a clean switch
        self.installed[dp.id] = {}
        self.add_flow(dp, T_CLASSIFY, 0, parser.OFPMatch(), goto=T_ROUTE)   # default: go and route

        is_edge = self.topo.name_of.get(dp.id) in self.edge_switches
        if self.mode == "dynamic":
            to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
            # first packet of every connection to the virtual IP -> controller picks a collector
            self.add_flow(dp, T_CLASSIFY, PRIO_PUNT, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=self.vip_ip),
                          actions=to_ctrl)
            # collector heartbeats -> controller
            self.add_flow(dp, T_CLASSIFY, PRIO_PUNT,
                          parser.OFPMatch(eth_type=ETH_IP, ip_proto=17, ipv4_dst=self.ctl_ip, udp_dst=self.ctl_port),
                          actions=to_ctrl)
        elif is_edge:
            # static baseline: the virtual IP always means the first collector
            name = sorted(self.pool.b)[0]
            b = self.pool.b[name]
            self.add_flow(dp, T_CLASSIFY, PRIO_NAT, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=self.vip_ip),
                          actions=[parser.OFPActionSetField(eth_dst=b["mac"]),
                                   parser.OFPActionSetField(ipv4_dst=b["ip"])], goto=T_ROUTE)
            self.add_flow(dp, T_CLASSIFY, PRIO_NAT,
                          parser.OFPMatch(eth_type=ETH_IP, ip_proto=6, ipv4_src=b["ip"], tcp_src=self.vip_port),
                          actions=[parser.OFPActionSetField(eth_src=self.vip_mac),
                                   parser.OFPActionSetField(ipv4_src=self.vip_ip)], goto=T_ROUTE)

    def install_routes(self, dp, routes):
        """Make table 1 of this switch match `routes` ({host_ip: out_port}); only changes are sent."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        current = self.installed.setdefault(dp.id, {})
        for ip, port in routes.items():
            if current.get(ip) != port:
                self.add_flow(dp, T_ROUTE, PRIO_ROUTE, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=ip),
                              actions=[parser.OFPActionOutput(port)])
                current[ip] = port
        for ip in [i for i in current if i not in routes]:           # destination no longer reachable
            self.delete_flows(dp, T_ROUTE, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=ip),
                              strict_priority=PRIO_ROUTE)
            del current[ip]

    # ================================================================ 1. link failure
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
            self.recompute_routes()

    def recompute_routes(self):
        t0 = time.time()
        routes = self.topo.compute_routes()
        for dpid, dp in self.datapaths.items():
            self.install_routes(dp, routes.get(dpid, {}))
        self.log_event("routes_updated", reaction_ms=round((time.time() - t0) * 1000, 2),
                       path=self.topo.path("s1", "s4"))

    # ================================================================ packet-in: heartbeats and new connections
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        if self.mode != "dynamic":
            return
        msg, dp = ev.msg, ev.msg.datapath
        pkt = packet.Packet(msg.data)
        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is None:
            return
        if ip.dst == self.ctl_ip:
            self.handle_heartbeat(pkt)
        elif ip.dst == self.vip_ip:
            self.handle_vip(dp, msg, pkt, ip)

    def handle_heartbeat(self, pkt):
        if pkt.get_protocol(udp.udp) is None or not isinstance(pkt.protocols[-1], (bytes, bytearray)):
            return
        try:
            beat = json.loads(bytes(pkt.protocols[-1]).decode())
            name = beat["id"]
        except (ValueError, KeyError, UnicodeDecodeError):
            return
        if self.pool.heartbeat(name, beat, time.time()):
            self.log_event("backend_up", backend=name)

    def handle_vip(self, dp, msg, pkt, ip):
        t = pkt.get_protocol(tcp.tcp)
        if t is None or t.dst_port != self.vip_port:
            return                                                    # not our service: drop
        parser = dp.ofproto_parser
        if ip.src not in self.authorized:                             # 4. access control
            self.add_flow(dp, T_CLASSIFY, PRIO_ACL,
                          parser.OFPMatch(eth_type=ETH_IP, ipv4_src=ip.src, ipv4_dst=self.vip_ip), idle=30)
            self.log_event("acl_block", src=ip.src)
            return

        now = time.time()
        key = (ip.src, t.src_port)
        is_syn = bool(t.bits & tcp.TCP_SYN) and not (t.bits & tcp.TCP_ACK)
        conn = self.conns.get(key)
        if conn and (is_syn or not self.pool.is_alive(conn["backend"], now)):
            self.drop_connection(key)
            conn = None
        if conn is None:                                              # 2. new connection: pick a collector
            name = self.pool.choose(now)
            if name is None:
                self.log_event("no_backend", client=ip.src)
                return
            conn = {"client_ip": ip.src, "client_port": t.src_port, "backend": name,
                    "cookie": self.pool.b[name]["cookie"]}
            self.conns[key] = conn
        self.install_connection(dp, conn)
        # send the packet that triggered all this through the pipeline, now that the rules exist
        ofp = dp.ofproto
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=msg.match["in_port"],
                                        actions=[parser.OFPActionOutput(ofp.OFPP_TABLE)], data=msg.data))

    def install_connection(self, dp, conn):
        """NAT rules for one TCP connection: client -> VIP becomes client -> collector, and back."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        b = self.pool.b[conn["backend"]]
        fwd = parser.OFPMatch(eth_type=ETH_IP, ip_proto=6, ipv4_src=conn["client_ip"], tcp_src=conn["client_port"],
                              ipv4_dst=self.vip_ip, tcp_dst=self.vip_port)
        self.add_flow(dp, T_CLASSIFY, PRIO_NAT, fwd,
                      actions=[parser.OFPActionSetField(eth_dst=b["mac"]), parser.OFPActionSetField(ipv4_dst=b["ip"])],
                      goto=T_ROUTE, cookie=b["cookie"], idle=self.idle, flags=ofp.OFPFF_SEND_FLOW_REM)
        rev = parser.OFPMatch(eth_type=ETH_IP, ip_proto=6, ipv4_src=b["ip"], tcp_src=self.vip_port,
                              ipv4_dst=conn["client_ip"], tcp_dst=conn["client_port"])
        self.add_flow(dp, T_CLASSIFY, PRIO_NAT, rev,
                      actions=[parser.OFPActionSetField(eth_src=self.vip_mac),
                               parser.OFPActionSetField(ipv4_src=self.vip_ip)],
                      goto=T_ROUTE, cookie=b["cookie"], idle=self.idle)

    def drop_connection(self, key):
        conn = self.conns.pop(key, None)
        if conn:
            self.pool.release(conn["backend"])

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def on_flow_removed(self, ev):
        msg = ev.msg
        if msg.table_id != T_CLASSIFY or msg.cookie not in self.cookie_name:
            return
        try:
            key = (msg.match["ipv4_src"], msg.match["tcp_src"])
        except KeyError:
            return
        conn = self.conns.get(key)
        if conn and conn["cookie"] == msg.cookie:
            self.drop_connection(key)

    # ================================================================ 3. health checks and 5. flood defence
    def _tick_loop(self):
        last_stats = 0.0
        while True:
            hub.sleep(0.25)
            if self.mode != "dynamic":
                continue
            now = time.time()
            for name in self.pool.expire(now):
                self.backend_down(name)
            if now - last_stats >= self.stats_interval:
                last_stats = now
                for dpid in {d for d, _ in self.watch}:
                    dp = self.datapaths.get(dpid)
                    if dp:
                        dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))

    def backend_down(self, name):
        b = self.pool.b[name]
        for dp in self.datapaths.values():                            # remove every NAT rule that points to it
            self.delete_flows(dp, T_CLASSIFY, cookie=b["cookie"], cookie_mask=ALL_BITS)
        for key in [k for k, c in self.conns.items() if c["backend"] == name]:
            self.conns.pop(key)
        b["assigned"] = 0
        self.log_event("backend_down", backend=name)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats(self, ev):
        dp, now = ev.msg.datapath, time.time()
        for st in ev.msg.body:
            key = (dp.id, st.port_no)
            if key not in self.watch:
                continue
            mbps, flood = self.monitor.update(key, st.rx_bytes, now)
            if flood and self.blocked_until.get(key, 0) <= now:
                self.block_port(dp, st.port_no, self.watch[key], mbps, now)

    def block_port(self, dp, port, host, mbps, now):
        """Quarantine a host: drop everything that enters the switch on its port."""
        self.add_flow(dp, T_CLASSIFY, PRIO_BLOCK, dp.ofproto_parser.OFPMatch(in_port=port),
                      cookie=BLOCK_COOKIE, hard=self.block_seconds)
        self.blocked_until[(dp.id, port)] = now + self.block_seconds
        self.log_event("flood_block", host=host, port=port, mbps=round(mbps, 2), seconds=self.block_seconds)
