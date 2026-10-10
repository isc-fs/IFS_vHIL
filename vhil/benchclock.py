"""The wall-clock bench's time: IFS_HIL's tests on the emulation's clock.

IFS_HIL's suites time the bench with the host's clock (`time.sleep`,
`time.monotonic`, CAN frame timestamps). On the physical bench that is the
firmware's own time. On the virtual bench it is not when the host cannot
emulate the system at real time (#154): two boards on a CI runner run at
0.1-0.9x, so a 6 s wait for the ECU's first heartbeat covered 0.6-2 s of its
boot, less than the bootloader's 2 s window (#243), and a 60 s soak counted
the 0x4A0 frames of 55 s.

Renode publishes its virtual time against the host's real-time clock at every
sync point (models/renode/VhilClock.cs). BenchClock puts the test process on
that time: `time.time`, `time.monotonic`, `time.perf_counter` (and their _ns
forms) return it, `time.sleep` waits for it, and python-can's `recv` waits
for it and stamps each frame with the bench time it arrived at. A host at
real time sees no change (the pacer caps the emulation at 1x, VhilPacer.cs);
a slower host takes longer, and the tests see the firmware's time.

Waits elsewhere stay on the host's clock: other processes (can-flasher), C
level timeouts (sockets, `threading` waits) and vHIL's own code, which uses
`host` below.

If the emulation stops (a deadlock in the emulator, #243's freeze), its time
stops: the first wait or clock read after `stall_s` of host time with no
progress raises BenchStalled, naming the virtual time it stopped at.
"""
from __future__ import annotations

import bisect
import mmap
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

# The host's own clock, taken before anything patches `time`. vHIL's own waits
# (the monitor connection, the broker's pacing watch) use it.
host = SimpleNamespace(time=time.time, time_ns=time.time_ns, monotonic=time.monotonic,
                       monotonic_ns=time.monotonic_ns, perf_counter=time.perf_counter,
                       perf_counter_ns=time.perf_counter_ns, sleep=time.sleep)

MAGIC = b"VHILCLK1"
HEADER = 64
PAIR = struct.Struct("<dd")
# Pairs this close to being overwritten are not read (the writer may be there).
GUARD = 256
# The longest slice a wait sleeps on the host clock: how soon a stall is seen.
SLICE_S = 0.05


class BenchStalled(RuntimeError):
    """The emulation's time stopped while the host's ran on."""


class ClockFile:
    """The ring of (virtual s, host Unix s) pairs VhilClock.cs writes."""

    def __init__(self, path: Path | str):
        with open(path, "rb") as f:
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        if self._mm[:8] != MAGIC:
            raise ValueError(f"{path}: not a vHIL bench clock")
        self.capacity = struct.unpack_from("<Q", self._mm, 16)[0]

    def close(self) -> None:
        self._mm.close()

    def count(self) -> int:
        return struct.unpack_from("<Q", self._mm, 8)[0]

    def _pair(self, i: int) -> tuple[float, float]:
        return PAIR.unpack_from(self._mm, HEADER + (i % self.capacity) * PAIR.size)

    def latest(self) -> tuple[float, float] | None:
        """The newest (virtual, host) pair, or None before the first sync point."""
        c = self.count()
        return self._pair(c - 1) if c else None

    def virtual_at(self, host_s: float) -> float | None:
        """The virtual time the emulation was at when the host's real-time
        clock read host_s, interpolated between the sync points around it.
        After the newest sync point: its virtual time (the emulation is at
        most one quantum on). Before the history kept: the oldest pair's,
        less the host time between, as at real time."""
        c = self.count()
        if not c:
            return None
        lo = max(0, c - self.capacity + GUARD)
        hosts = _Hosts(self, lo, c)
        j = bisect.bisect_right(hosts, host_s) - 1
        if j < 0:
            v, h = self._pair(lo)
            return v - (h - host_s)
        if lo + j >= c - 1:
            return self._pair(c - 1)[0]
        (v0, h0), (v1, h1) = self._pair(lo + j), self._pair(lo + j + 1)
        if h1 <= h0:
            return v1
        return v0 + (v1 - v0) * (host_s - h0) / (h1 - h0)


class _Hosts:
    """The host times of pairs lo..count-1, as a sequence bisect can search."""

    def __init__(self, clock: ClockFile, lo: int, count: int):
        self._clock, self._lo, self._n = clock, lo, count - lo

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, k: int) -> float:
        return self._clock._pair(self._lo + k)[1]


class BenchClock:
    """The test process's clock on the emulation's virtual time. install()
    patches `time` and python-can; uninstall() puts them back."""

    def __init__(self, path: Path | str, stall_s: float = 30.0, wait_s: float = 60.0):
        self.path, self.stall_s = Path(path), stall_s
        self._file: ClockFile | None = None
        self._saved: list[tuple[object, str, object]] = []
        self._lock = threading.Lock()
        self._last_v: float | None = None
        self._moved_at = 0.0
        self._wait_s = wait_s
        self.start_v = self.start_host = 0.0

    # -- time -------------------------------------------------------------

    def virtual(self) -> float:
        """The emulation's virtual time (s): the newest sync point's. Raises
        BenchStalled when it has not moved for stall_s of host time."""
        pair = self._file.latest()
        v = pair[0] if pair else 0.0
        now = host.monotonic()
        with self._lock:
            if v != self._last_v:
                self._last_v, self._moved_at = v, now
            elif now - self._moved_at > self.stall_s:
                raise BenchStalled(
                    f"virtual bench stalled: its virtual time has stood at {v:.6f} s for "
                    f"{now - self._moved_at:.0f} s of host time. The emulation stopped (see "
                    "the Renode log); a wall-clock wait would have timed out instead (#243).")
        return v

    def sleep(self, seconds: float) -> None:
        """Wait for `seconds` of the emulation's time. The pacer keeps the
        emulation at or below real time, so a host-clock sleep of what is left
        never overshoots it by more than a sync quantum."""
        if seconds <= 0:
            host.sleep(0)
            return
        target = self.virtual() + seconds
        while True:
            left = target - self.virtual()
            if left <= 0:
                return
            host.sleep(min(left, SLICE_S))

    def stamp(self, host_s: float) -> float:
        """A host real-time timestamp (a CAN frame's) as bench time.time()."""
        v = self._file.virtual_at(host_s)
        return self._base_time + (v if v is not None else 0.0)

    def rtf(self) -> float | None:
        """Virtual over host seconds since install()."""
        host_s = host.monotonic() - self.start_host
        return (self.virtual() - self.start_v) / host_s if host_s > 0 else None

    # -- install ----------------------------------------------------------

    def install(self) -> "BenchClock":
        global _active
        if _active is not None:
            raise RuntimeError("a bench clock is already installed")
        self._file = ClockFile(self.path)
        deadline = host.monotonic() + self._wait_s
        while self._file.latest() is None:          # the first sync point
            if host.monotonic() > deadline:
                self._file.close()
                self._file = None
                raise BenchStalled(f"virtual bench clock {self.path}: no sync point in "
                                   f"{self._wait_s:.0f} s; the emulation never ran")
            host.sleep(0.01)
        v0 = self.virtual()
        self.start_v, self.start_host = v0, host.monotonic()
        self._base_time = host.time() - v0
        self._base_mono = host.monotonic() - v0
        self._base_perf = host.perf_counter() - v0
        # python-can first: its bus module takes `time` at import.
        try:
            import can
        except ImportError:
            can = None
        _active = self
        for name, fn in _PATCHES.items():
            self._patch(time, name, fn)
        if can is not None:
            self._patch(can.BusABC, "recv", _recv(can.BusABC.recv))
        return self

    def uninstall(self) -> None:
        global _active
        if _active is self:
            _active = None
        for owner, name, original in reversed(self._saved):
            setattr(owner, name, original)
        self._saved.clear()
        # The file stays mapped: a thread that took this clock just before
        # may still read it.

    def _patch(self, owner, name, fn) -> None:
        self._saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, fn)



# The functions `time` gets while a clock is installed. A module imported
# meanwhile may keep one (`from time import time`); with no clock installed
# they are the host's again.
_active: BenchClock | None = None


def _bench(fallback, of):
    def fn():
        clock = _active
        return fallback() if clock is None else of(clock)
    fn.__name__ = fallback.__name__
    return fn


def _sleep(seconds):
    clock = _active
    return host.sleep(seconds) if clock is None else clock.sleep(seconds)


_PATCHES = {
    "time": _bench(host.time, lambda c: c._base_time + c.virtual()),
    "time_ns": _bench(host.time_ns, lambda c: int((c._base_time + c.virtual()) * 1e9)),
    "monotonic": _bench(host.monotonic, lambda c: c._base_mono + c.virtual()),
    "monotonic_ns": _bench(host.monotonic_ns, lambda c: int((c._base_mono + c.virtual()) * 1e9)),
    "perf_counter": _bench(host.perf_counter, lambda c: c._base_perf + c.virtual()),
    "perf_counter_ns": _bench(host.perf_counter_ns,
                              lambda c: int((c._base_perf + c.virtual()) * 1e9)),
    "sleep": _sleep,
}


def _recv(original):
    """python-can's recv: the timeout in bench time, the frame's timestamp
    (the kernel's, on the host's real-time clock) as bench time."""

    def recv(bus, timeout=None):
        clock = _active
        if clock is None:
            return original(bus, timeout)
        if timeout is None:
            msg = original(bus, None)
        else:
            deadline = clock.virtual() + timeout
            while True:
                left = deadline - clock.virtual()
                msg = original(bus, max(0.0, min(left, SLICE_S)))
                if msg is not None or left <= 0:
                    break
        if msg is not None and msg.timestamp:
            msg.timestamp = clock.stamp(msg.timestamp)
        return msg

    recv.__wrapped__ = original
    return recv
