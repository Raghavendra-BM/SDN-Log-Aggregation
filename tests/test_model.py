"""
Unit tests for sdn/model.py (no network needed).     Run:  python3 tests/test_model.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdn"))
from model import BackendPool, RateMonitor, Topology, load_config  # noqa: E402

cfg = load_config(ROOT / "config" / "topology.json")
S1, S2, S3, S4 = 1, 2, 3, 4
H1, C1 = cfg["hosts"]["h1"]["ip"], cfg["hosts"]["c1"]["ip"]


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


def test_pool_balances_and_fails_over():
    pool = BackendPool([("c1", "10.0.0.11", "m1"), ("c2", "10.0.0.12", "m2")], timeout=1.5, now=100.0)
    picks = [pool.choose(100.0) for _ in range(4)]
    assert picks.count("c1") == 2 and picks.count("c2") == 2, picks      # spread evenly
    pool.heartbeat("c1", {"pending": 0, "conns": 2}, 101.0)
    pool.heartbeat("c2", {"pending": 0, "conns": 2}, 101.0)
    assert pool.expire(101.5) == []
    pool.heartbeat("c2", {"pending": 0}, 102.0)                          # c1 stops sending heartbeats
    assert pool.expire(102.8) == ["c1"]
    assert all(pool.choose(103.0) == "c2" for _ in range(3))             # only c2 is chosen now
    assert pool.heartbeat("c1", {"pending": 0}, 104.0) is True           # c1 is back
    assert pool.choose(104.0) == "c1"                                    # least loaded again


def test_pool_avoids_a_backlogged_collector():
    pool = BackendPool([("c1", "a", "m"), ("c2", "b", "m")], timeout=5, now=0.0)
    pool.heartbeat("c1", {"pending": 5000}, 1.0)
    pool.heartbeat("c2", {"pending": 0}, 1.0)
    assert pool.choose(1.0) == "c2"


def test_no_live_backend():
    pool = BackendPool([("c1", "a", "m")], timeout=1.0, now=0.0)
    assert pool.choose(5.0) is None


def test_flood_detection():
    m = RateMonitor(threshold_mbps=3.0, samples_needed=2)
    assert m.update("p", 0, 0.0) == (0.0, False)
    mbps, block = m.update("p", 125_000, 1.0)                # 1 Mbit/s: fine
    assert round(mbps, 1) == 1.0 and not block
    mbps, block = m.update("p", 125_000 + 1_875_000, 2.0)    # 15 Mbit/s: first sample over
    assert round(mbps) == 15 and not block
    mbps, block = m.update("p", 125_000 + 2 * 1_875_000, 3.0)
    assert block                                             # second sample over: block
    assert not m.update("p", 125_000 + 2 * 1_875_000 + 1000, 4.0)[1]   # quiet again


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"\n{len(tests)} tests passed")
