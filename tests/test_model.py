"""
Unit tests for sdn/model.py (no network needed).     Run:  python3 tests/test_model.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
from model import CongestionDetector, Topology, load_config  # noqa: E402

cfg = load_config(ROOT / "config" / "topology.json")
S1, S2, S3, S4 = 1, 2, 3, 4
H1, C1 = cfg["hosts"]["h1"]["ip"], cfg["hosts"]["srv"]["ip"]


def test_primary_path_is_the_fast_one():
    t = Topology(cfg)
    assert t.path("s1", "s4") == ["s1", "s2", "s4"], t.path("s1", "s4")
    r = t.compute_routes()
    assert r[S1][C1] == 5        # s1 sends toward c1 through port 5 (link to s2)
    assert r[S4][C1] == 1        # s4 delivers to c1 on its host port
    assert r[S4][H1] == 3        # replies go back through s2 (port 3)


def test_link_failure_switches_to_backup_path():
    t = Topology(cfg)
    assert t.set_port_state(S1, 5, up=False) is True        # s1-s2 link goes down
    assert t.path("s1", "s4") == ["s1", "s3", "s4"], t.path("s1", "s4")
    r = t.compute_routes()
    assert r[S1][C1] == 6        # now through port 6 (link to s3)
    assert r[S4][H1] == 4        # replies come back through s3 as well


def test_other_end_of_the_failed_link_changes_nothing_new():
    t = Topology(cfg)
    t.set_port_state(S1, 5, up=False)
    assert t.set_port_state(S2, 1, up=False) is False        # same link, second report


def test_link_recovery_returns_to_primary():
    t = Topology(cfg)
    t.set_port_state(S1, 5, up=False)
    t.set_port_state(S2, 1, up=False)
    assert t.set_port_state(S1, 5, up=True) is False         # the other end is still down
    assert t.set_port_state(S2, 1, up=True) is True
    assert t.path("s1", "s4") == ["s1", "s2", "s4"]


def test_both_paths_down_means_no_route():
    t = Topology(cfg)
    t.set_port_state(S1, 5, up=False)
    t.set_port_state(S1, 6, up=False)
    assert t.path("s1", "s4") is None
    assert C1 not in t.compute_routes()[S1]


def feed(det, t, mbps_by_port, last_bytes):
    """One polling round: every port sends `mbps` for 0.5 s more than at the previous round."""
    for port, mbps in mbps_by_port.items():
        last_bytes[port] = last_bytes.get(port, 0) + int(mbps * 1e6 / 8 * 0.5)
        det.update(port, last_bytes[port], t)
    return det.evaluate()


def test_congestion_starts_after_two_busy_rounds_and_ends_after_four_calm_ones():
    det, b = CongestionDetector(8.0, 2, 5.0, 4), {}
    t = 0.0
    assert feed(det, t, {"p": 0.3}, b) is None                       # first reading: no rate yet
    for _ in range(3):
        t += 0.5
        assert feed(det, t, {"p": 0.3}, b) is None                   # quiet
    t += 0.5
    assert feed(det, t, {"p": 9.5}, b) is None                       # one busy round is not enough
    t += 0.5
    assert feed(det, t, {"p": 9.6}, b) == "start" and det.congested  # two in a row
    for _ in range(3):
        t += 0.5
        assert feed(det, t, {"p": 0.2}, b) is None and det.congested # calm, but not long enough yet
    t += 0.5
    assert feed(det, t, {"p": 0.2}, b) == "end" and not det.congested


def test_hysteresis_between_the_two_thresholds_changes_nothing():
    det, b = CongestionDetector(8.0, 2, 5.0, 4), {}
    t = 0.0
    feed(det, t, {"p": 0}, b)
    for _ in range(2):
        t += 0.5
        r = feed(det, t, {"p": 9.0}, b)
    assert r == "start"
    for _ in range(10):                                              # 6 Mbit/s: below 8 but above 5
        t += 0.5
        assert feed(det, t, {"p": 6.0}, b) is None and det.congested


def test_busiest_port_decides_and_is_reported():
    det, b = CongestionDetector(8.0, 1, 5.0, 1), {}
    feed(det, 0.0, {"a": 0, "b": 0}, b)
    assert feed(det, 0.5, {"a": 1.0, "b": 9.9}, b) == "start"
    key, mbps = det.busiest()
    assert key == "b" and round(mbps, 1) == 9.9


def test_counter_reset_is_not_a_negative_rate():
    det = CongestionDetector(8.0, 2, 5.0, 4)
    det.update("p", 1_000_000, 0.0)
    assert det.update("p", 500, 1.0) == 0.0                          # the switch restarted: counter went down


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"\n{len(tests)} tests passed")
