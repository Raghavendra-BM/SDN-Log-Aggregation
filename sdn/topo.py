"""
Mininet topology for the SDN log aggregation system, built from config/topology.json.

         h1 h2 h3 h4                                 s2 (fast path, 5 ms)
          \\ | | /                                  /           \\
            s1 ------------------------------------              s4 --- c1
              \\                                                 /    \\-- c2
                ------------------------- s3 (backup path, 20 ms)

  h1..h3  log sources (service + agent)     h4  untrusted host / attacker
  c1, c2  collectors                        the controller (Ryu) runs outside Mininet on 127.0.0.1:6653

Start the controller first (in another terminal), then:

    sudo python3 sdn/topo.py --demo            # network + all applications + a Mininet prompt
    sudo python3 sdn/topo.py                   # only the network and the prompt

Use --datapath user if your machine has no Open vSwitch kernel module.
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
from model import load_config  # noqa: E402

from mininet.cli import CLI                     # noqa: E402
from mininet.link import Link, TCLink          # noqa: E402
from mininet.log import setLogLevel            # noqa: E402
from mininet.net import Mininet                # noqa: E402
from mininet.node import OVSSwitch, RemoteController  # noqa: E402

PY = sys.executable
SERVICE_OF = {"h1": "order", "h2": "payment", "h3": "inventory", "h4": "rogue"}


# ====================================================================== the network
def quick_cleanup():
    """Remove leftovers of an earlier run. We do NOT call 'mn -c' here: on many systems it also kills a
    running ryu-manager, which is exactly the controller we need."""
    out = subprocess.run(["ovs-vsctl", "list-br"], capture_output=True, text=True).stdout
    for bridge in out.split():
        subprocess.run(["ovs-vsctl", "--if-exists", "del-br", bridge], capture_output=True)
    links = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True).stdout
    for line in links.splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if re.match(r"^[shc]\d+-eth\d+$", name):
            subprocess.run(["ip", "link", "del", name], capture_output=True)


def build_network(cfg, datapath="kernel", shaping=True):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink if shaping else Link,
                  autoSetMacs=False, build=False)

    def shape(entry):
        return {"bw": entry["bw"], "delay": entry["delay"]} if shaping else {}

    for name, dpid in cfg["switches"].items():
        opts = {"dpid": f"{int(dpid):016x}", "protocols": "OpenFlow13"}
        if datapath == "user":
            opts["datapath"] = "user"
        net.addSwitch(name, **opts)
    for name, h in cfg["hosts"].items():
        net.addHost(name, ip=f"{h['ip']}/24", mac=h["mac"])
    for name, h in cfg["hosts"].items():                       # host links: explicit switch port numbers
        net.addLink(net.get(name), net.get(h["switch"]), port2=h["port"], **shape(cfg["host_link"]))
    for l in cfg["links"]:                                     # core links
        net.addLink(net.get(l["a"]), net.get(l["b"]), port1=l["a_port"], port2=l["b_port"], **shape(l))
    net.addController("c0", controller=RemoteController, ip=cfg["controller"]["ip"],
                      port=cfg["controller"]["port"])
    return net


def tune_hosts(net, cfg):
    """Turn NIC offloading off. The switches rewrite IP addresses (NAT for the virtual IP) and the receiving
    host must see a TCP checksum that matches the rewritten packet. This is the usual Mininet fix."""
    for name in cfg["hosts"]:
        host = net.get(name)
        host.cmd(f"ethtool -K {host.defaultIntf().name} rx off tx off sg off tso off gso off gro off 2>/dev/null")


def install_neighbors(net, cfg):
    """Static ARP entries: no ARP traffic, no flooding, no loops in a topology that has loops."""
    entries = [(h["ip"], h["mac"]) for h in cfg["hosts"].values()]
    entries += [(cfg["vip"]["ip"], cfg["vip"]["mac"]), (cfg["ctl_vip"]["ip"], cfg["ctl_vip"]["mac"])]
    for name in cfg["hosts"]:
        host = net.get(name)
        dev = host.defaultIntf().name
        for ip, mac in entries:
            if ip != cfg["hosts"][name]["ip"]:
                host.cmd(f"ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent")


def start_network(cfg, datapath="kernel", shaping=True):
    net = build_network(cfg, datapath, shaping)
    net.start()
    if hasattr(net, "waitConnected"):
        if not net.waitConnected(timeout=20):
            raise RuntimeError("the switches did not connect to the controller on "
                               f"{cfg['controller']['ip']}:{cfg['controller']['port']}. Is the controller running?")
    else:
        time.sleep(5)
    tune_hosts(net, cfg)
    install_neighbors(net, cfg)
    time.sleep(1.5)                      # let the controller install its flows
    return net


# ====================================================================== the applications
class Apps:
    """Starts and stops collectors, services, agents and the flooder inside the Mininet hosts."""

    def __init__(self, net, cfg, run_dir, rate=40):
        self.net, self.cfg, self.rate = net, cfg, rate
        self.run = Path(run_dir)
        self.run.mkdir(parents=True, exist_ok=True)
        self.procs, self.files = {}, []

    def _start(self, key, host, script, *args):
        out = open(self.run / f"{key}.out", "w")
        self.files.append(out)
        cmd = [PY, str(ROOT / "app" / script)] + [str(a) for a in args]
        self.procs[key] = self.net.get(host).popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))

    def start_collectors(self):
        hb = f"{self.cfg['ctl_vip']['ip']}:{self.cfg['ctl_vip']['port']}"
        for name, h in self.cfg["hosts"].items():
            if h["role"] == "collector":
                self._start(f"collector_{name}", name, "collector.py", "--id", name,
                            "--db", self.run / f"{name}.db", "--heartbeat", hb)

    def start_source(self, host):
        vip = f"{self.cfg['vip']['ip']}:{self.cfg['vip']['port']}"
        logdir = self.run / "logs" / host
        self._start(f"service_{host}", host, "service.py", SERVICE_OF[host], "--logdir", logdir,
                    "--host-name", host, "--rate", self.rate)
        self._start(f"agent_{host}", host, "agent.py", "--collector", vip, "--logdir", logdir,
                    "--checkpoint", self.run / f"agent_{host}.json", "--metrics", self.run / f"metrics_{host}.jsonl",
                    "--rotate-batches", 10, "--timeout", 3, "--max-backoff", 4)

    def start_sources(self):
        for host, h in self.cfg["hosts"].items():
            if h["role"] == "source":
                self.start_source(host)

    def start_rogue(self):
        self.start_source("h4")

    def start_flood(self, seconds=20, mbps=15, target="c1"):
        self._start("flooder_h4", "h4", "flooder.py", "--target", self.cfg["hosts"][target]["ip"],
                    "--mbps", mbps, "--seconds", seconds)

    def kill(self, key, sig=signal.SIGKILL):
        p = self.procs.get(key)
        if p and p.poll() is None:
            p.send_signal(sig)

    def stop_services(self):
        for key in [k for k in self.procs if k.startswith("service_")]:
            self.kill(key, signal.SIGTERM)

    def stop_all(self):
        for p in self.procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.time() + 4
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

    def do_kill_collector(self, line):
        "kill_collector [c1|c2]   hard-kill a collector process"
        self.apps.kill(f"collector_{line.strip() or 'c1'}")

    def do_start_collector(self, line):
        "start_collector [c1|c2]  start a collector again"
        name = line.strip() or "c1"
        hb = f"{self.apps.cfg['ctl_vip']['ip']}:{self.apps.cfg['ctl_vip']['port']}"
        self.apps._start(f"collector_{name}", name, "collector.py", "--id", name,
                         "--db", self.apps.run / f"{name}.db", "--heartbeat", hb)

    def do_flood(self, line):
        "flood [seconds] [mbps]   h4 floods c1 with UDP traffic"
        parts = line.split()
        self.apps.start_flood(seconds=float(parts[0]) if parts else 20, mbps=float(parts[1]) if len(parts) > 1 else 15)

    def do_rogue(self, line):
        "rogue   h4 tries to send logs through the virtual IP (it is not an authorised source)"
        self.apps.start_rogue()

    def do_flows(self, line):
        "flows [s1|s2|s3|s4]   show the OpenFlow rules of a switch"
        sw = line.strip() or "s1"
        print(self.mn.get(sw).cmd(f"ovs-ofctl -O OpenFlow13 dump-flows {sw}"))


def main():
    parser = argparse.ArgumentParser(description="Mininet topology for the SDN log aggregation system")
    parser.add_argument("--config", default=str(ROOT / "config" / "topology.json"))
    parser.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    parser.add_argument("--no-shaping", action="store_true", help="plain links without bandwidth and delay")
    parser.add_argument("--demo", action="store_true", help="also start collectors, services and agents")
    parser.add_argument("--run-dir", default=str(ROOT / "results" / "demo"))
    parser.add_argument("--rate", type=float, default=2, help="logs per second per source (2 keeps the dashboard readable)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run  sudo python3 sdn/topo.py ...")
    cfg = load_config(args.config)
    setLogLevel("info")
    quick_cleanup()                                              # leftovers of earlier runs (keeps the controller alive)
    net = start_network(cfg, args.datapath, not args.no_shaping)
    apps = None
    try:
        if args.demo:
            apps = Apps(net, cfg, args.run_dir, args.rate)
            apps.start_collectors()
            time.sleep(2)
            apps.start_sources()
            print("\nApplications started. Try:  flows s1 | link s1 s2 down | kill_collector c1 | flood 20 | rogue\n")
            DemoCLI(net, apps)
        else:
            CLI(net)
    finally:
        if apps:
            apps.stop_all()
        net.stop()


if __name__ == "__main__":
    main()
