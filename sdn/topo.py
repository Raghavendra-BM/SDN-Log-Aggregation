"""
Mininet topology for the UDP log system with priority queues, built from config/topology.json.

         h1 h2 h3 h4                                 s2 (primary path)
          \\ | | /                                  /           \\
            s1 ------------------------------------              s4 --- srv (log server)
              \\                                                 /
                ------------------------- s3 (backup path)

  h1..h3  log sources (UDP log clients)       h4   congestion generator (UDP flood to the server's sink port)
  srv     central log server                  the controller (Ryu) runs outside Mininet on 127.0.0.1:6653

Every core link is 10 Mbit/s. The rate limit is made by the switch ports themselves (Open vSwitch QoS, an HTB
queue discipline), because that is where the queues for the three log classes live:

      queue 2 = critical (UDP port 5001)   queue 1 = important (5002)   queue 0 = everything else (5003, flood)

The queues exist in the static baseline too; only the dynamic controller decides to use them.

Start the controller first (in another terminal), then:

    sudo $(which python3) sdn/topo.py --demo            # network + log server + clients + a Mininet prompt
    sudo $(which python3) sdn/topo.py                   # only the network and the prompt
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
sys.path.insert(0, str(ROOT))
from common.udp_protocol import FLOOD_PORT  # noqa: E402
from model import load_config  # noqa: E402

from mininet.cli import CLI                     # noqa: E402
from mininet.link import Link, TCLink          # noqa: E402
from mininet.log import setLogLevel            # noqa: E402
from mininet.net import Mininet                # noqa: E402
from mininet.node import OVSSwitch, RemoteController  # noqa: E402

PY = sys.executable
SERVICE_OF = {"h1": "order", "h2": "payment", "h3": "inventory"}


# ====================================================================== the network
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def quick_cleanup():
    """Remove leftovers of an earlier run. We do NOT call 'mn -c' here: on many systems it also kills a
    running ryu-manager, which is exactly the controller we need."""
    for bridge in run(["ovs-vsctl", "list-br"]).stdout.split():
        run(["ovs-vsctl", "--if-exists", "del-br", bridge])
    run(["ovs-vsctl", "--all", "destroy", "QoS"])               # queue settings of earlier runs
    run(["ovs-vsctl", "--all", "destroy", "Queue"])
    for line in run(["ip", "-o", "link", "show"]).stdout.splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if re.match(r"^[shc]\d+-eth\d+$", name):
            run(["ip", "link", "del", name])


def build_network(cfg, datapath="kernel", shaping=True):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink if shaping else Link,
                  autoSetMacs=False, build=False)
    for name, dpid in cfg["switches"].items():
        opts = {"dpid": f"{int(dpid):016x}", "protocols": "OpenFlow13"}
        if datapath == "user":
            opts["datapath"] = "user"
        net.addSwitch(name, **opts)
    for name, h in cfg["hosts"].items():
        net.addHost(name, ip=f"{h['ip']}/24", mac=h["mac"])
    hl = {"bw": cfg["host_link"]["bw"], "delay": cfg["host_link"]["delay"]} if shaping else {}
    for name, h in cfg["hosts"].items():                       # host links: explicit switch port numbers
        net.addLink(net.get(name), net.get(h["switch"]), port2=h["port"], **hl)
    for l in cfg["links"]:                                     # core links: plain, the QoS on the ports limits them
        net.addLink(net.get(l["a"]), net.get(l["b"]), port1=l["a_port"], port2=l["b_port"], cls=Link)
    net.addController("c0", controller=RemoteController, ip=cfg["controller"]["ip"], port=cfg["controller"]["port"])
    return net


def setup_qos(cfg):
    """Give every core switch port a rate limit and three queues (Open vSwitch QoS, type linux-htb).

    Queues are served by priority: a smaller 'priority' number is served first, and each queue is guaranteed
    its min-rate. Unused bandwidth is shared, so with no congestion every class can use the whole link."""
    queues = cfg["qos"]["queues"]
    ports = []
    for l in cfg["links"]:
        ports.append((f"{l['a']}-eth{l['a_port']}", l["bw"]))
        ports.append((f"{l['b']}-eth{l['b_port']}", l["bw"]))
    for iface, bw in ports:
        rate = int(bw * 1e6)
        cmd = ["ovs-vsctl", "--", "set", "Port", iface, "qos=@qos", "--", "--id=@qos", "create", "QoS",
               "type=linux-htb", f"other-config:max-rate={rate}"]
        cmd += [f"queues:{q['id']}=@q{q['id']}" for q in queues.values()]
        for q in queues.values():
            cmd += ["--", f"--id=@q{q['id']}", "create", "Queue", f"other-config:min-rate={int(q['min_mbps'] * 1e6)}",
                    f"other-config:max-rate={rate}", f"other-config:priority={q['priority']}"]
        out = run(cmd)
        if out.returncode != 0:
            raise RuntimeError(f"could not create the QoS queues on {iface}: {out.stderr.strip()}")
    time.sleep(1.0)                                            # let ovs-vswitchd build the queues
    first = ports[0][0]
    shown = run(["tc", "class", "show", "dev", first]).stdout
    if "htb" not in shown:
        raise RuntimeError(f"the QoS queues do not show up on {first} (tc class show printed nothing useful). "
                           "They need the kernel datapath: do not use --datapath user.")


def install_neighbors(net, cfg):
    """Static ARP entries: no ARP traffic, no flooding, no loops in a topology that has loops."""
    entries = [(h["ip"], h["mac"]) for h in cfg["hosts"].values()]
    for name in cfg["hosts"]:
        host = net.get(name)
        dev = host.defaultIntf().name
        for ip, mac in entries:
            if ip != cfg["hosts"][name]["ip"]:
                host.cmd(f"ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent")


def start_network(cfg, datapath="kernel", shaping=True, qos=True):
    net = build_network(cfg, datapath, shaping)
    net.start()
    if hasattr(net, "waitConnected"):
        if not net.waitConnected(timeout=20):
            raise RuntimeError("the switches did not connect to the controller on "
                               f"{cfg['controller']['ip']}:{cfg['controller']['port']}. Is the controller running?")
    else:
        time.sleep(5)
    if qos:
        setup_qos(cfg)
    install_neighbors(net, cfg)
    time.sleep(1.5)                      # let the controller install its flows
    return net


# ====================================================================== the applications
class Apps:
    """Starts and stops the log server, the clients and the congestion generator inside the Mininet hosts."""

    def __init__(self, net, cfg, run_dir, rate=100):
        self.net, self.cfg, self.rate = net, cfg, rate
        self.run = Path(run_dir)
        self.run.mkdir(parents=True, exist_ok=True)
        self.procs, self.files = {}, []
        self.server = next(n for n, h in cfg["hosts"].items() if h["role"] == "server")
        self.server_ip = cfg["hosts"][self.server]["ip"]

    def _start(self, key, host, script, *args):
        out = open(self.run / f"{key}.out", "w")
        self.files.append(out)
        cmd = [PY, str(ROOT / "app" / script)] + [str(a) for a in args]
        self.procs[key] = self.net.get(host).popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))

    def start_server(self):
        self._start("server", self.server, "log_server.py", "--db", self.run / "server.db",
                    "--summary", self.run / "server_summary.json")

    def start_clients(self, duration=None):
        """One UDP client per source host. duration=None: they run until stop_all()."""
        for host, h in self.cfg["hosts"].items():
            if h["role"] == "source":
                args = ["--server", self.server_ip, "--service", SERVICE_OF[host], "--host-name", host,
                        "--rate", self.rate, "--summary", self.run / f"sent_{host}.json"]
                if duration:
                    args += ["--duration", duration]
                self._start(f"client_{host}", host, "udp_client.py", *args)

    def start_flood(self, seconds=20, mbps=15):
        """h4 sends UDP at a fixed rate to the server's sink port: controlled congestion."""
        gen = next(n for n, h in self.cfg["hosts"].items() if h["role"] == "generator")
        self._start("flooder", gen, "flooder.py", "--target", self.server_ip, "--port", FLOOD_PORT,
                    "--mbps", mbps, "--seconds", seconds)

    def wait_clients(self, timeout):
        deadline = time.time() + timeout
        for key, p in self.procs.items():
            if key.startswith("client_"):
                try:
                    p.wait(max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    p.terminate()

    def stop_all(self):
        # clients and flooder first, then the server: the server must outlive the last datagrams
        for key in [k for k in self.procs if k != "server"]:
            p = self.procs[key]
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        p = self.procs.get("server")
        if p and p.poll() is None:
            p.send_signal(signal.SIGTERM)
        deadline = time.time() + 5
        for p in self.procs.values():
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        for f in self.files:
            f.close()


# ====================================================================== demo prompt
class DemoCLI(CLI):
    """Mininet prompt with extra commands for the live demo."""

    def __init__(self, net, apps, *args, **kwargs):
        self.apps = apps
        super().__init__(net, *args, **kwargs)

    def do_flood(self, line):
        "flood [seconds] [mbps]   h4 floods the server's sink port (default 20 s at 15 Mbit/s)"
        parts = line.split()
        self.apps.start_flood(seconds=float(parts[0]) if parts else 20, mbps=float(parts[1]) if len(parts) > 1 else 15)

    def do_flows(self, line):
        "flows [s1|s2|s3|s4]   show the OpenFlow rules of a switch (look for set_queue)"
        sw = line.strip() or "s1"
        print(self.mn.get(sw).cmd(f"ovs-ofctl -O OpenFlow13 dump-flows {sw}"))

    def do_queues(self, line):
        "queues [iface]   packets and drops per queue on a core port (default s1-eth5)"
        iface = line.strip() or "s1-eth5"
        print(run(["tc", "-s", "class", "show", "dev", iface]).stdout)

    def do_report(self, line):
        "report   loss and delay per severity so far (reads the server database)"
        sys.path.insert(0, str(ROOT / "experiments"))
        from udp_analysis import analyse, print_table
        print_table(analyse(self.apps.run))


def main():
    parser = argparse.ArgumentParser(description="Mininet topology for the UDP log system")
    parser.add_argument("--config", default=str(ROOT / "config" / "topology.json"))
    parser.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    parser.add_argument("--no-shaping", action="store_true", help="host links without bandwidth and delay")
    parser.add_argument("--no-qos", action="store_true", help="do not create the priority queues")
    parser.add_argument("--demo", action="store_true", help="also start the log server and the clients")
    parser.add_argument("--run-dir", default=str(ROOT / "results" / "demo"))
    parser.add_argument("--rate", type=float, default=50, help="logs per second per source host")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run  sudo $(which python3) sdn/topo.py ...")
    cfg = load_config(args.config)
    setLogLevel("info")
    quick_cleanup()                                              # leftovers of earlier runs (keeps the controller alive)
    net = start_network(cfg, args.datapath, not args.no_shaping, not args.no_qos)
    apps = None
    try:
        if args.demo:
            apps = Apps(net, cfg, args.run_dir, args.rate)
            apps.start_server()
            time.sleep(1.5)
            apps.start_clients()
            print("\nApplications started. Try:  flows s1 | queues | flood 20 15 | report | link s1 s2 down\n")
            DemoCLI(net, apps)
        else:
            CLI(net)
    finally:
        if apps:
            apps.stop_all()
        net.stop()


if __name__ == "__main__":
    main()
