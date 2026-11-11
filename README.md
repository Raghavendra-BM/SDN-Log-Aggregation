# SDN-Assisted Distributed Logging over UDP: Prioritising Critical Logs under Congestion

Several hosts send their application logs over **UDP** to one central log server. When the network is congested,
an **SDN controller (Ryu / OpenFlow 1.3)** makes sure the **critical** logs still get through, and the project
measures how much that helps in packet loss and delivery time.

## The problem
Develop a distributed logging system where multiple hosts send application logs using UDP. The SDN controller
should prioritise critical logs during congestion.

| Expectation | Where it is done |
|---|---|
| UDP log clients | `app/udp_client.py` (one per source host h1, h2, h3) |
| Centralised log server | `app/log_server.py` on host `srv` |
| Timestamps on logs | every datagram carries its send time; the server adds the receive time |
| Categorise logs by severity | DEBUG, INFO, WARNING, ERROR, CRITICAL; the severity decides the UDP port (`common/udp_protocol.py`) |
| Collect network-flow statistics | `sdn/controller.py`: port counters of all core links and flow counters per log class every 0.5 s |
| Prioritise critical logs | controller puts critical and important logs into higher-priority queues during congestion |
| Generate controlled congestion | `app/flooder.py` on host h4: UDP at a fixed rate (Mbit/s) towards the server |
| Measure packet loss and delivery time | `experiments/run_experiments.py`, `experiments/udp_analysis.py` |

## Architecture

```
  h1 h2 h3  (UDP log clients)      h4 (congestion generator)
     \  |  /                      /
        s1 ======= s2 =========== s4 --- srv (log server)     primary path   (5 ms links)
        s1 ======= s3 =========== s4                          backup path    (20 ms links)
                            |
                   Ryu controller (outside Mininet, 127.0.0.1:6653)
```
All core links are 10 Mbit/s, host links 20 Mbit/s. The core link s1 to s2 is the **bottleneck**: the logs of h1..h3 and
the flood of h4 meet there.

### Log format and severity classes
One log = one UDP datagram with a small JSON object: host, service, severity, sequence number, send time, message.
No handshake, no acknowledgement, no retry. The severity decides the destination port, because a switch can match a UDP
port but cannot read JSON:

| Severity | UDP port | Class | Queue on the switch ports |
|---|---|---|---|
| CRITICAL | 5001 | critical | 2 (highest priority) |
| ERROR, WARNING | 5002 | important | 1 |
| INFO, DEBUG | 5003 | normal | 0 (default) |
| flood from h4 | 6000 | background | 0 (default) |

### What the controller does
* **Static (baseline):** shortest-path routes only. Every packet leaves through the same first-in-first-out queue.
* **Dynamic:**
  1. *Statistics.* Every 0.5 s it reads the transmit counters of all core ports (Mbit/s) and the flow counters of the edge switch (packets and Mbit/s per log class).
  2. *Congestion detection.* A port above 8 Mbit/s in two polls in a row means congestion. It is over when every port stayed below 5 Mbit/s for four polls (hysteresis, so it does not flap).
  3. *Prioritisation.* The flow rules for ports 5001 to 5003 get a `set_queue` action: critical logs go into queue 2, important logs into queue 1. The queues are served by priority, so a full queue 0 cannot block critical logs. The rules are removed again after the congestion.
  4. *Link failure (bonus).* Port-status events trigger a new shortest-path computation; the queue rules follow the new route.

  Every decision is written to `controller_events.jsonl` (one JSON line per event, plus one `stats` line per poll).

The queues themselves are created by `sdn/topo.py` (Open vSwitch QoS, linux-htb) in **both** modes, so the static baseline runs
on exactly the same network; only the dynamic controller decides to use them.

## Install (Ubuntu 22.04 or 24.04)
```
bash scripts/install_ubuntu.sh
source .venv/bin/activate
python3 tests/test_udp_local.py            # UDP client, log server and analysis (no Mininet needed)
python3 tests/test_model.py                # routing and congestion-detector logic
python3 tests/test_controller_logic.py     # controller logic against a fake Ryu
python3 tests/test_query_api.py            # dashboard API
```
The priority queues need the **kernel** datapath of Open vSwitch (do not use `--datapath user`).

## Run
Open four terminals in the project folder (activate the environment in each; terminals 3 and 4 are for the dashboard).

| Terminal | Command |
|---|---|
| 1. Controller | `mkdir -p results/demo` then `SDN_MODE=dynamic EVENT_LOG=results/demo/controller_events.jsonl ryu-manager sdn/controller.py` |
| 2. Network + applications | `sudo $(which python3) sdn/topo.py --demo` (gives a `mininet>` prompt) |
| 3. Dashboard API | `python3 app/query_api.py --db results/demo/server.db --events results/demo/controller_events.jsonl` |
| 4. Dashboard | `cd ui && npm install && npm run dev`, open the address it prints and click **Live API** |

The dashboard shows every log with its severity (DEBUG to CRITICAL), its delivery time in ms (the **Delay** column), the number of
critical logs in the last minute and the number of lost logs (estimated from gaps in the sequence numbers). It is optional for the
experiments but makes the live demo easy to follow: start a `flood 20 15` at the `mininet>` prompt and watch the Delay column and the
lost-log counter, then compare with the static controller.

Smoke test (controller in dynamic mode must be running in terminal 1, about 1 minute):
```
sudo $(which python3) tests/smoke_mininet.py
```

## Live demo script
At the `mininet>` prompt:
1. `report`: loss and delay per severity so far (should be 0% loss).
2. `flows s1`: the log rules at priority 20 forward to port 5 with **no** `set_queue`.
3. `flood 20 15`: h4 sends 15 Mbit/s for 20 s into a 10 Mbit/s link.
4. After about 1 s: `flows s1` again. The rules now contain `set_queue:2`, `set_queue:1`, `set_queue:0`. Check the controller terminal for the `congestion_start` event.
5. `queues`: packets and drops per queue; queue 0 (flood and normal logs) drops, the critical queue does not.
6. After the flood: `report` (critical loss about 0%, normal logs lost), and after about 2 s `flows s1` shows the queue actions removed again.

## Experiments: static vs dynamic
```
sudo $(which python3) experiments/run_experiments.py              # 4 congestion levels x 2 modes, about 8 minutes
python3 experiments/plot_results.py                               # graphs in results/plots/
```
Each run: 40 s of logs from three hosts (100 logs/s each), h4 floods from second 10 for 20 s at 0, 8, 15 or 20 Mbit/s.
The result table (`results/summary.csv`) covers the congested 20 s only: per severity the logs sent, received, loss %, and delivery time
(mean, median, 95th percentile, maximum), plus the controller's reaction time. Delivery time = arrival time at the server minus the send
time written by the client; all Mininet hosts share one clock, so this is the true one-way delay.

Expected pattern: below 10 Mbit/s nothing is lost in either mode. Above it, the static network loses and delays **all** severities alike
(the queue is shared), while the dynamic controller keeps CRITICAL loss near 0% with small delay and pushes the loss onto the normal logs and the
background traffic. In the first second after the flood starts, before the controller reacts, critical logs are affected too (reaction time is in the table).

## UDP and the timing difference to TCP
UDP sends a log immediately: no handshake (TCP needs one extra round trip before the first data), no acknowledgements, no retransmission and no
waiting behind a lost packet. So its delay is only propagation plus queuing. The price is that nothing repairs a lost log, and UDP does not slow
down under congestion. That is why the network itself (the controller) has to protect the important logs. TCP would deliver every log, but late: a
loss costs at least one more round trip (often 200 ms or more after a timeout) and TCP slows its sending when it sees congestion.

## Project layout
```
app/udp_client.py     UDP log client (fake service)        app/log_server.py   central UDP log server (SQLite)
app/flooder.py        congestion generator                 common/udp_protocol.py   datagram format, severity classes, ports
sdn/controller.py     Ryu controller (static / dynamic)    sdn/model.py     routing + congestion detector (no Ryu needed)
sdn/topo.py           Mininet topology, queues, apps       config/topology.json   topology, queues, thresholds
experiments/          run_experiments.py, udp_analysis.py, plot_results.py
app/query_api.py      dashboard API (reads server.db)       ui/              web dashboard
tests/                test_udp_local, test_query_api, test_model, test_controller_logic, smoke_mininet
```
`app/query_api.py` serves the log server's database to the web dashboard in `ui/` (React, shows severity and delay).

## Limitations
* One log server, one bottleneck; the 10 Mbit/s links are an emulation, not real hardware.
* Prioritisation works per UDP port, so a client that uses the wrong port is classified wrongly; nothing authenticates the sender.
* The controller reacts after about 1 s (two polls of 0.5 s), so a very short burst can pass before the rules change.
* Statistics come from polling, not from per-packet telemetry.
* No retransmission: UDP logs that are dropped are gone. This is the intended trade-off of the assignment.
