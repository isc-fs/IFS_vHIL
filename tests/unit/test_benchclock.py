"""The wall-clock bench's clock (vhil/benchclock.py, #243): the test process
on the emulation's virtual time, from the ring VhilClock.cs writes."""
from __future__ import annotations

import re
import struct
import threading
import time
from pathlib import Path

import pytest

from vhil import benchclock
from vhil.benchclock import BenchClock, BenchStalled, ClockFile, host

REPO = Path(__file__).resolve().parents[2]


class Writer:
    """VhilClock.cs's side of the file, in Python."""

    def __init__(self, path: Path, capacity: int = 64):
        self.path, self.capacity, self.count = path, capacity, 0
        size = benchclock.HEADER + capacity * benchclock.PAIR.size
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        self.f = open(path, "r+b")
        self._at(0, b"VHILCLK1")
        self._at(16, struct.pack("<Q", capacity))

    def _at(self, offset: int, data: bytes) -> None:
        self.f.seek(offset)
        self.f.write(data)
        self.f.flush()

    def sync(self, virtual_s: float, host_s: float | None = None) -> None:
        host_s = host.time() if host_s is None else host_s
        at = benchclock.HEADER + (self.count % self.capacity) * benchclock.PAIR.size
        self._at(at, benchclock.PAIR.pack(virtual_s, host_s))
        self.count += 1
        self._at(8, struct.pack("<Q", self.count))


class Emulation(threading.Thread):
    """Virtual time advancing at `rate` x the host's, in 1 ms sync points."""

    def __init__(self, writer: Writer, rate: float):
        super().__init__(daemon=True)
        self.writer, self.rate, self.stop = writer, rate, threading.Event()
        self.v = 10.0

    def run(self):
        last = host.monotonic()
        while not self.stop.is_set():
            host.sleep(0.001)
            now = host.monotonic()
            self.v += (now - last) * self.rate
            last = now
            self.writer.sync(self.v)


@pytest.fixture
def ring(tmp_path):
    w = Writer(tmp_path / "bench.clock", capacity=4096)
    yield w
    w.f.close()


def test_the_layout_is_the_one_vhilclock_writes():
    """Magic, offsets and the pair format agree with models/renode/VhilClock.cs."""
    src = (REPO / "models" / "renode" / "VhilClock.cs").read_text()
    assert '"VHILCLK1"' in src
    assert re.search(r"Header = 64;", src) and benchclock.HEADER == 64
    assert re.search(r"Pair = 16;", src) and benchclock.PAIR.size == 16
    assert "view.Write(8, count)" in src and "view.Write(16, (ulong)Capacity)" in src


def test_a_host_timestamp_maps_to_the_virtual_time_around_it(ring):
    ring.sync(1.000, 100.0)
    ring.sync(1.001, 100.010)              # 1 ms of virtual time in 10 ms of host
    ring.sync(1.002, 100.020)
    clock = ClockFile(ring.path)
    assert clock.latest() == (1.002, 100.020)
    assert clock.virtual_at(100.005) == pytest.approx(1.0005)
    assert clock.virtual_at(100.030) == 1.002      # after the newest sync point
    assert clock.virtual_at(99.0) == pytest.approx(0.0)   # before the history: at 1x
    clock.close()


def test_only_the_ring_last_pairs_are_read(tmp_path):
    w = Writer(tmp_path / "c", capacity=benchclock.GUARD + 8)
    for i in range(1000):
        w.sync(i * 0.001, 1000.0 + i)
    clock = ClockFile(w.path)
    assert clock.virtual_at(1000.0 + 995.5) == pytest.approx(0.9955)
    clock.close()
    w.f.close()


def test_the_test_process_runs_on_bench_time(ring):
    """At half real time, a 0.2 s sleep takes 0.4 s of host time, and
    monotonic/time/perf_counter advance by bench time; uninstall restores
    the host's clock."""
    emu = Emulation(ring, rate=0.5)
    emu.start()
    clock = BenchClock(ring.path).install()
    try:
        assert time.sleep is not host.sleep
        m0, t0, h0 = time.monotonic(), time.time(), host.monotonic()
        assert abs(t0 - host.time()) < 0.1         # continuous with the host at install
        time.sleep(0.2)
        assert time.monotonic() - m0 == pytest.approx(0.2, abs=0.01)
        assert time.time() - t0 == pytest.approx(0.2, abs=0.01)
        assert host.monotonic() - h0 == pytest.approx(0.4, abs=0.1)
        assert clock.rtf() == pytest.approx(0.5, abs=0.15)
    finally:
        clock.uninstall()
        emu.stop.set()
        emu.join()
    assert time.sleep is host.sleep and time.monotonic is host.monotonic


def test_a_stalled_emulation_fails_loudly(ring):
    """Virtual time that stops raises BenchStalled from the next wait instead
    of a deadline that never comes."""
    ring.sync(5.0)
    clock = BenchClock(ring.path, stall_s=0.3).install()
    try:
        with pytest.raises(BenchStalled, match=r"virtual bench stalled: its virtual time has "
                                               r"stood at 5\.000000 s"):
            time.sleep(1.0)
    finally:
        clock.uninstall()


def test_install_waits_for_the_first_sync_point(ring):
    with pytest.raises(BenchStalled, match="no sync point"):
        BenchClock(ring.path, wait_s=0.1).install()
    assert time.sleep is host.sleep


def test_can_recv_waits_and_stamps_frames_in_bench_time(ring):
    """python-can's recv timeout is bench time, and a frame's kernel (host)
    timestamp becomes the bench time it arrived at."""
    can = pytest.importorskip("can")

    class Bus(can.BusABC):
        def __init__(self):
            self.frames = []
            super().__init__(channel="test")

        def _recv_internal(self, timeout):
            if self.frames:
                return self.frames.pop(0), True
            host.sleep(timeout or 0)
            return None, True

        def send(self, msg, timeout=None):
            pass

    emu = Emulation(ring, rate=0.5)
    emu.start()
    clock = BenchClock(ring.path).install()
    bus = Bus()
    try:
        h0 = host.monotonic()
        assert bus.recv(timeout=0.2) is None
        assert host.monotonic() - h0 == pytest.approx(0.4, abs=0.1)
        sent = host.time()
        bus.frames.append(can.Message(arbitration_id=0x100, timestamp=sent))
        msg = bus.recv(timeout=0.1)
        assert msg.timestamp == pytest.approx(time.time(), abs=0.01)
    finally:
        bus.shutdown()
        clock.uninstall()
        emu.stop.set()
        emu.join()
    assert can.BusABC.recv.__name__ == "recv" and not hasattr(can.BusABC.recv, "__wrapped__")
