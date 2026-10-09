"""A run's live logs (vhil/runlog.py): Renode's log filtered, UART lines,
the rate limit, the firmware build streamed, and a worker's run trace
carrying build, Renode and UART records as they happen."""
import json
import sys

import pytest

pytest.importorskip("fastapi")

from vhil import runlog  # noqa: E402
from vhil.server.runs import read_trace  # noqa: E402
from vhil.system import System  # noqa: E402
from vhil.worker import FirmwareResolver, Worker, run_build  # noqa: E402
from tests.unit.test_runs import ECU, FakeBuild, FakeSim, settings, store  # noqa: E402,F401

RULES = [{"peripheral": "rcc", "offsets": [0x2C]}]

RENODE = """\
[14:26:02.8723] [INFO] Loaded monitor commands from: /opt/renode/scripts/monitor.py
[14:26:07.4513] [WARNING] Translation cache size 536870912 is larger than maximum allowed 134217728. It will be clamped to maximum
[14:26:07.9659] [INFO] ams/sysbus: Loading block of 109388 bytes length at 0x8020000.
[14:26:08.9078] [INFO] ecu: Machine started.
[14:26:08.9082] [INFO] ams/cpu: Setting initial values: PC = 0x8021E31, SP = 0x20020000.
[14:26:08.9760] [WARNING] ams/rcc: Unhandled write to offset 0x2C. Unhandled bits: [2-3] when writing value 0x1FF000C. Tags: PLL1GRE (0x3).
[14:26:08.9907] [WARNING] ecu/fdcan1_h7: Unhandled read from offset 0x108.
[14:26:08.9909] [WARNING] ecu/fdcan1_h7: Unhandled write to offset 0x108, value 0x0.
[14:26:09.0000] [WARNING] ecu/fdcan1_h7: FrameSent is not initialized. Is the controller connected to medium?
[14:26:09.0100] [WARNING] ecu/fdcan1_h7: FrameSent is not initialized. Is the controller connected to medium? (2)
[14:26:09.0200] [WARNING] ecu/fdcan1_h7: FrameSent is not initialized. Is the controller connected to medium? (3)
[14:26:09.1000] [INFO] ams: Machine paused.
[14:26:10.0000] [WARNING] ams/watchdog: Watchdog reset triggered!
[14:26:12.0000] [WARNING] ams/watchdog: Watchdog reset triggered!
[14:26:13.0000] [ERROR] ams/cpu: CPU abort [PC=0x40000000]: Trying to execute code outside RAM or ROM
   at Antmicro.Renode.Peripherals.CPU.TranslationCPU.OnCpuAbort
   at Antmicro.Renode.Peripherals.CPU.TranslationCPU.Run
[14:26:14.0000] [INFO] ams: Disposed.
"""


def shown(text, rules=RULES):
    f = runlog.RenodeFilter(rules)
    return f, [s for s in map(f.feed, text.splitlines()) if s is not None]


def test_renode_filter_keeps_what_a_user_wants_live():
    f, out = shown(RENODE)
    texts = [t for _, t, _ in out]
    # Machine and CPU starts; info noise (loading, monitor, pause, dispose) not.
    assert "ecu: Machine started." in texts
    assert ("info", "cpu: Setting initial values: PC = 0x8021E31, SP = 0x20020000.", "ams") in out
    assert not any("Loading block" in t or "paused" in t or "Disposed" in t or "monitor" in t
                   for t in texts)
    # The host's translation cache is not the firmware's business.
    assert not any("Translation cache" in t for t in texts)
    # An access the guard's rules explain is dropped; one they don't, said
    # once per register, whatever the access.
    assert not any("rcc" in t for t in texts)
    unmodelled = [t for t in texts if t.startswith("unmodelled hardware")]
    assert unmodelled == ["unmodelled hardware: ecu: fdcan1_h7 register offset 0x108 (not "
                          "explained in configs/peripherals.yaml; invariant 7)"]
    # Any other warning the first time it is said, numbers aside; repeats counted.
    assert sum("FrameSent" in t for t in texts) == 1
    assert "2 repeat(s) of 1 Renode warning(s)" in f.summary()
    # The watchdog every time; an error with its stack's next lines.
    assert [b for lvl, t, b in out if "Watchdog reset" in t] == ["ams", "ams"]
    abort = texts.index("cpu: CPU abort [PC=0x40000000]: Trying to execute code outside RAM or ROM")
    assert out[abort][0] == "error"
    assert texts[abort + 1].strip().startswith("at Antmicro") and out[abort + 1][0] == "error"


def test_a_line_with_no_kept_message_before_it_is_not_shown():
    _, out = shown("   at Somewhere\n[14:00:00.0] [INFO] ams/sysbus: Loading block\n   at Else\n")
    assert out == []


def test_rate_limit_caps_a_source_a_second_and_counts_the_rest():
    limit = runlog.RateLimit(per_s=3)
    recs = [runlog.record(t, f"line {i}", "ecu.USART10", board="ecu")
            for i, t in enumerate([0, 1000, 2000, 3000, 4000, 5000])]
    recs.append(runlog.record(6000, "boom", "ecu.USART10", "error"))
    recs.append(runlog.record(7000, "other", "renode"))
    out = limit(recs)
    assert [r["text"] for r in out] == ["line 0", "line 1", "line 2", "boom", "other"]
    # The next second's first line says how many the last one dropped.
    out = limit([runlog.record(1_000_000, "next", "ecu.USART10")])
    assert out[0]["dropped"] == 3 and out[0]["source"] == "ecu.USART10" \
        and out[0]["level"] == "warning" and out[0]["t_us"] == 1_000_000
    assert out[1]["text"] == "next"
    assert limit.dropped["ecu.USART10"] == 3
    # flush: what is still uncounted, now.
    limit([runlog.record(1_000_000 + i, "x", "ecu.USART10") for i in range(5)])
    notes = limit.flush(runlog.record(2_000_000, "", ""))
    assert [(n["dropped"], n["t_us"]) for n in notes] == [(3, 2_000_000)]
    assert limit.flush() == []


def test_wall_time_records_are_limited_in_wall_seconds():
    limit = runlog.RateLimit(per_s=1)
    a = runlog.record(0, "a", "build", wall_s=100.0)
    b = runlog.record(0, "b", "build", wall_s=100.5)
    c = runlog.record(0, "c", "build", wall_s=101.2)
    out = limit([a, b, c])
    assert [r["text"] for r in out] == ["a", next(r for r in out if r.get("dropped"))["text"], "c"]
    assert out[1]["wall_s"] == 101.2


def test_uart_tail_cuts_lines_and_escapes_control_bytes(tmp_path):
    path = tmp_path / "ecu.USART10.txt"
    tail = runlog.UartTail(path)
    assert tail.lines() == []                       # no file yet
    path.write_bytes(b"$PMTK220,100*2F\r\n$PMTK3")
    assert tail.lines() == ["$PMTK220,100*2F"]
    with open(path, "ab") as f:
        f.write(b"14,0*29\r\n\x01\x7fbin" + b"A" * runlog.UART_LINE_BYTES)
    assert tail.lines() == ["$PMTK314,0*29", "\\x01\\x7fbin" + "A" * (runlog.UART_LINE_BYTES - 5)]
    assert tail.rest() == ["AAAAA"]
    assert tail.rest() == []


def test_run_build_streams_stderr_and_returns_stdout(tmp_path):
    script = ("import sys,time\n"
              "print('ecu=/fw/ecu.elf')\n"
              "for i in range(3):\n"
              "    print(f'[ecu] step {i}', file=sys.stderr, flush=True)\n")
    lines = []
    assert run_build([sys.executable, "-c", script], lines.append) == "ecu=/fw/ecu.elf\n"
    assert lines == ["[ecu] step 0", "[ecu] step 1", "[ecu] step 2"]
    fail = "import sys\nprint('cmake: error: no compiler', file=sys.stderr)\nsys.exit(2)\n"
    with pytest.raises(Exception) as e:
        run_build([sys.executable, "-c", fail])
    assert e.value.returncode == 2 and "no compiler" in e.value.stderr


def test_level_of():
    assert runlog.level_of("main.c:3: error: x") == "error"
    assert runlog.level_of("warning: unused") == "warning"
    assert runlog.level_of("[ 50%] Building C object") == "info"


class TalkingSim(FakeSim):
    """A FakeSim whose Renode log and UART file grow as virtual time goes:
    the machines start at once, the ECU's GPS commands go out at 150 ms, the
    watchdog fires at 250 ms."""

    def __init__(self, log_path):
        super().__init__()
        self.log_path = log_path
        self.uart_logs = {"ecu.USART10": log_path.parent / "uart" / "ecu.USART10.txt"}
        self.uart_logs["ecu.USART10"].parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("[14:00:00.0] [INFO] ecu: Machine started.\n")
        self.said = {}

    def run_for(self, ms=0, us=0):
        now = super().run_for(ms, us)
        if now >= 150_000 and not self.said.get("gps"):
            self.said["gps"] = True
            with open(self.uart_logs["ecu.USART10"], "ab") as f:
                f.write(b"$PMTK220,100*2F\r\n")
        if now >= 250_000 and not self.said.get("wdg"):
            self.said["wdg"] = True
            with open(self.log_path, "a") as f:
                f.write("[14:00:01.0] [WARNING] ecu/iwdg: Watchdog reset triggered!\n")
        return now


def test_a_run_trace_has_build_renode_and_uart_logs_as_they_happen(tmp_path, settings, store,
                                                                   monkeypatch):
    import vhil.worker as vw
    system = System(ECU)
    monkeypatch.setattr(vw, "run_build", FakeBuild(system, tmp_path / "fw"))
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 400, "slice_ms": 100},
                          firmware_commits={"ecu": {"ref": "dev", "commit": "e" * 40},
                                            "ecu.bootloader": {"ref": "v1.7.0", "commit": "1" * 40}})
    Worker(settings, sim_factory=lambda path, fw, log: TalkingSim(log),
           resolver=FirmwareResolver(tmp_path / "fw", log=lambda m: None)).run_once()
    run = store.get(run_id)
    assert run["state"] == "passed", run["summary"]
    run_dir = settings.results / str(run_id)
    logs = [r for r in read_trace(run_dir / "trace.jsonl") if r["kind"] == "log"]
    assert all(r["source"] and r["level"] in runlog.LEVELS for r in logs)

    # The build's lines, before power-on: t_us 0 and the wall time; build.log has them all.
    build = [r for r in logs if r["source"] == "build"]
    assert build[0]["text"].startswith("building ecu at dev (eeeeeeeeeeee)")
    assert any("compiling" in r["text"] for r in build)
    assert all(r["t_us"] == 0 and r["wall_s"] > 1e9 for r in build)
    assert "compiling" in (run_dir / "build.log").read_text()
    # The worker's notes before power-on carry the wall time; after, not.
    worker = [r for r in logs if r["source"] == "worker"]
    assert worker and all("wall_s" in r for r in worker if r["t_us"] == 0)

    # Renode's lines and the UART's at the end of the slice they were read in.
    renode = [(r["t_us"], r["text"]) for r in logs if r["source"] == "renode"]
    assert renode[0] == (0, "ecu: Machine started.")
    assert (300_000, "iwdg: Watchdog reset triggered!") in renode
    uart = [(r["t_us"], r["text"], r["board"]) for r in logs if r["source"] == "ecu.USART10"]
    assert uart == [(200_000, "$PMTK220,100*2F", "ecu")]
    assert not any("wall_s" in r for r in logs if r["source"] in ("renode", "ecu.USART10"))
    # The trace stays in virtual-time order.
    times = [json.loads(line)["t_us"] for line in (run_dir / "trace.jsonl").read_text().splitlines()]
    assert times == sorted(times)


def test_boot_transitions_are_logged_from_the_pc():
    class Booting(FakeSim):
        def in_app(self, board):
            return 2_000_000 <= self.now < 3_000_000 or self.now >= 3_500_000
    sim = Booting()
    logs = runlog.RunLogs()
    got = []
    for t in (0, 1_000_000, 2_000_000, 2_500_000, 3_000_000, 3_500_000):
        sim.now = t
        got += [(r["t_us"], r["text"], r["level"]) for r in logs.poll(t, sim)]
    assert got == [(2_000_000, "ecu: the bootloader jumped to the app", "info"),
                   (3_000_000, "ecu: back in the bootloader (a reset)", "warning"),
                   (3_500_000, "ecu: the bootloader jumped to the app", "info")]
