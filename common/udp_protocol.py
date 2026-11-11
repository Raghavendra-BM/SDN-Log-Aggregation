"""
Wire format and severity classes for the UDP version of the log system.

Every log is ONE UDP datagram holding a small JSON object (no framing is needed: UDP keeps message
boundaries, unlike TCP). There is no acknowledgement, so a lost datagram is simply lost.

    {"h":"h1", "svc":"payment", "sev":"CRITICAL", "seq":17, "t":1762300000.123, "msg":"Card payment failed"}

    h    host that sent it            svc  service that wrote it         sev  severity
    seq  counter per (host, service, severity): the server finds lost logs by looking for gaps
    t    send time, seconds since 1970 (Mininet hosts share one clock, so recv_time - t is the real delay)
    msg  the log text

Severity decides the UDP destination PORT. The SDN controller cannot read JSON, but it can match on a UDP
port, so the port is how the network learns how important a log is.
"""
import json

SEVERITIES = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SEVERITY_WEIGHTS = [20, 55, 15, 7, 3]             # percent of logs that a service produces

CLASS_OF = {"CRITICAL": "critical", "ERROR": "important", "WARNING": "important",
            "INFO": "normal", "DEBUG": "normal"}
PORT_OF_CLASS = {"critical": 5001, "important": 5002, "normal": 5003}
CLASS_OF_PORT = {p: c for c, p in PORT_OF_CLASS.items()}
LOG_PORTS = sorted(PORT_OF_CLASS.values())
FLOOD_PORT = 6000                                  # background traffic that creates the congestion
MAX_DATAGRAM = 1200                                # stay below the Ethernet MTU


def port_for(severity):
    return PORT_OF_CLASS[CLASS_OF[severity]]


def encode(host, service, severity, seq, sent_at, message):
    data = json.dumps({"h": host, "svc": service, "sev": severity, "seq": seq,
                       "t": round(sent_at, 6), "msg": message}, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_DATAGRAM:
        raise ValueError(f"log too large for one datagram ({len(data)} bytes)")
    return data


def decode(data):
    """Bytes -> dict with keys h, svc, sev, seq, t, msg. Raises ValueError if the datagram is not a valid log."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("not valid JSON")
    if not isinstance(d, dict):
        raise ValueError("not a JSON object")
    if d.get("sev") not in SEVERITIES:
        raise ValueError(f"unknown severity {d.get('sev')!r}")
    for key, kind in (("h", str), ("svc", str), ("msg", str)):
        if not isinstance(d.get(key), kind):
            raise ValueError(f"missing field '{key}'")
    if not isinstance(d.get("seq"), int) or not isinstance(d.get("t"), (int, float)):
        raise ValueError("seq must be an integer and t a number")
    return d
