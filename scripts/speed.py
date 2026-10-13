"""How fast this host emulates the systems: virtual seconds per wall second
(the real-time factor), per system, per path, for the boot and steady state.

    python scripts/speed.py ecu=ECU08.elf ams=AMS.elf bootloader=CAN_BL.elf \
        [--systems ams ecu ecu-ams] [--paths bench sim] [--boot 3] [--steady 5] \
        [--json results/speed.json] [--profile results/profile] [--mips N]

Paths:
  bench  the wall-clock bench's emulation (vhil/bench.py): Renode's shared
         time source (`mach create`), every core at WALL_CLOCK_MIPS, the bench
         clock (VhilClock.cs) and GPIO probes, run flat out, without the
         pacer, so the factor is the host's headroom (the bench itself paces
         to 1x at most);
  sim    lock-step vhil.sim as native tests run it: the catalogue's cpu.mips,
         a time source per board in a multi-board system (invariant 11), a
         CAN probe on every bus and a GPIO probe per board.

Each system boots from power-on: `boot` is its first --boot seconds of virtual
time (the bootloaders' 2 s auto-jump window, then the apps' start), `steady`
the --steady seconds after. Above 1.0x the wall-clock bench keeps real time;
IFS_HIL's tests run on the bench's time either way (vhil/benchclock.py), so
below it a suite only takes longer, as native tests do.

--baseline measures each configuration a second time as the bench ran
before #154's speed-ups, on the same host: without the busy-wait
fast-forward (VHIL_FAST_FORWARD=0, models/renode/VhilFastForward.cs) and with
.NET's default tiering delay (vhil/renode.py RENODE_ENV).

--profile DIR samples each board's PC every millisecond of host time
(models/renode/VhilPcSampler.cs) and writes, per run and phase, the guest
functions the host spent its time in.

Firmware: board=ELF for each board a system names; bootloader=ELF stands for
every <board>.bootloader. Defaults from VHIL_ECU_ELF, VHIL_AMS_ELF and
VHIL_CAN_BOOTLOADER_ELF.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vhil import elf as elfsyms  # noqa: E402
from vhil import renode as rn  # noqa: E402
from vhil.bench import CLOCK_SOURCE, DEFAULT_RENODE, WALL_CLOCK_MIPS  # noqa: E402
from vhil.broker import probe_commands  # noqa: E402
from vhil.sim import PROBE_SOURCE, _free_port  # noqa: E402
from vhil.system import System  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SAMPLER_SOURCE = REPO / "models" / "renode" / "VhilPcSampler.cs"


def firmware_for(system: System, given: dict[str, str]) -> dict[str, Path]:
    out = {}
    for image in system.images():
        key = "bootloader" if image.endswith(".bootloader") else image
        path = given.get(image) or given.get(key)
        if not path:
            raise SystemExit(f"{system.id}: no firmware for '{image}' (give {key}=ELF)")
        out[image] = Path(path).resolve()
    return out


class Run:
    """One Renode process emulating one system on one path."""

    def __init__(self, system_path: Path, firmware: dict[str, str], path: str,
                 renode: str, mips: int | None, profile: Path | None, log: Path | None,
                 setup: list[str] = ()):
        self.system = System(system_path)
        self.firmware = firmware_for(self.system, firmware)
        self.path, self.renode, self.mips, self.profile = path, renode, mips, profile
        self.log = log
        self.setup = list(setup)

    def __enter__(self):
        lockstep = self.path == "sim"
        script = Path(tempfile.mkdtemp(prefix="vhil-speed-")) / f"{self.system.id}.resc"
        script.write_text(self.system.render_renode(self.firmware, lockstep=lockstep,
                                                    sources=(PROBE_SOURCE,) if lockstep else ()))
        port = _free_port()
        out = open(self.log, "w") if self.log else None
        self.proc = rn.launch(self.renode, port, **({"stdout": out} if out else {}))
        self.m = rn.RenodeMonitor(port, timeout_s=3600)
        m = self.m
        if lockstep:
            # As vhil.sim.Sim.start does.
            m.execute("emulation SetSeed 1")
            m.execute(f"include {rn.file_arg(script)}")
            for bus in self.system.buses:
                probe = f"vhil_probe_{rn.ident(bus)}"
                m.execute(f"emulation CreateVhilCanProbe {rn.quote(probe)}")
                m.execute(f"connector Connect {probe} {bus}")
            for board in self.system.boards:
                m.execute(f"emulation CreateVhilGpioProbe {rn.quote('vhil_gpio_' + rn.ident(board))} "
                          f"{rn.quote(board)}")
        else:
            # As vhil.bench.VirtualBench.start does, less the pacer and the
            # broker: the bench clock on every sync point, every core at the
            # bench's rate, the GPIO probes its routes need.
            m.execute(f"include {rn.file_arg(script)}")
            m.execute(f"include {rn.file_arg(CLOCK_SOURCE)}")
            m.execute(f"emulation EnableVhilClock {rn.quote((script.parent / 'bench.clock').as_posix())}")
            for board in self.system.boards:
                m.execute(f"mach set {rn.quote(rn.ident(board))}")
                m.execute(f"cpu PerformanceInMips {WALL_CLOCK_MIPS}")
            for command in probe_commands(self.system.bench_config()):
                m.execute(command)
        m.execute("emulation SetGlobalAdvanceImmediately true")
        if self.mips is not None:
            for board in self.system.boards:
                m.execute(f"mach set {rn.quote(rn.ident(board))}")
                m.execute(f"cpu PerformanceInMips {int(self.mips)}")
        for command in self.setup:
            # Experiments: run on every board, with {board} its machine name.
            for board in self.system.boards:
                m.execute(f"mach set {rn.quote(rn.ident(board))}")
                print(f"[speed] {board}: {command} -> "
                      f"{m.execute(command.format(board=board)).strip()}", file=sys.stderr)
        if self.profile:
            m.execute(f"include {rn.file_arg(SAMPLER_SOURCE)}")
            for board in self.system.boards:
                m.execute(f"emulation CreateVhilPcSampler {rn.quote('vhil_pcs_' + rn.ident(board))} "
                          f"{rn.quote(board)} 1")
        return self

    def run_for(self, seconds: float) -> float:
        """Advance every board by `seconds` of virtual time; the wall time it took.
        In whole quanta, as a lock-step run of boards on their own time
        sources needs (vhil.sim.Sim.run_for)."""
        q = self.system.sync_quantum_us()
        us = round(seconds * 1e6)
        us -= us % q
        t0 = time.monotonic()
        self.m.execute(f'emulation RunFor "{us / 1e6:.6f}"')
        return time.monotonic() - t0

    def fast_forward_stats(self) -> dict:
        """Each board's busy-wait fast-forward counters (VhilFastForward.cs)."""
        out = {}
        for board in self.system.boards:
            reply = self.m.execute(f"vhil_ff_{rn.ident(board)} Stats").strip()
            out[board] = reply
            print(f"[speed] {board}: {reply}", file=sys.stderr, flush=True)
        return out

    def dump_profile(self, phase: str) -> dict:
        """Hottest guest functions per board since the last dump, by share of
        host-time samples; resets the samplers."""
        out = {}
        for board in self.system.boards:
            name = "vhil_pcs_" + rn.ident(board)
            raw = Path(tempfile.mkdtemp(prefix="vhil-pcs-")) / f"{board}.pcs"
            self.m.execute(f"{name} Dump {rn.file_arg(raw)}")
            self.m.execute(f"{name} Clear")
            counts: Counter = Counter()
            images = [p for p in (self.firmware.get(board), self.firmware.get(f"{board}.bootloader")) if p]
            for line in raw.read_text().splitlines():
                pc, n = line.split()
                pc = int(pc, 16)
                where = "?"
                for img in images:
                    hit = elfsyms.symbol_at(img, pc)
                    if hit:
                        where = f"{hit[0]} ({img.stem})"
                        break
                counts[where] += int(n)
            total = sum(counts.values()) or 1
            out[board] = [(f, round(100 * n / total, 1)) for f, n in counts.most_common(15)]
            dest = self.profile / f"{self.system.id}-{self.path}-{phase}-{board}.txt"
            dest.write_text("\n".join(f"{pct:5.1f}%  {f}" for f, pct in out[board]) + "\n")
        return out

    def __exit__(self, *exc):
        try:
            self.m.execute("quit")
        except Exception:
            pass
        self.m.close()
        try:
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


# The bench before #154's speed-ups, for --baseline.
BASELINE_ENV = {"VHIL_FAST_FORWARD": "0", "DOTNET_TC_CallCountingDelayMs": "100"}


def measure(system: str, firmware: dict[str, str], path: str, args, variant: str = "current") -> dict:
    sys_path = Path(system) if system.endswith(".yaml") else REPO / "systems" / f"{system}.yaml"
    log = (Path(args.log_dir) / f"{Path(sys_path).stem}-{path}-{variant}.log"
           if args.log_dir else None)
    saved = {k: os.environ.get(k) for k in BASELINE_ENV}
    if variant == "baseline":
        os.environ.update(BASELINE_ENV)
    try:
        return _measure(sys_path, firmware, path, args, variant, log)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _measure(sys_path: Path, firmware: dict[str, str], path: str, args, variant: str,
             log: Path | None) -> dict:
    with Run(sys_path, firmware, path, args.renode, args.mips, args.profile, log,
             args.setup) as run:
        result = {"system": run.system.id, "path": path, "variant": variant,
                  "mips": args.mips or ("bench" if path == "bench" else "catalogue")}
        for phase, seconds in (("boot", args.boot), ("steady", args.steady)):
            print(f"[speed] {run.system.id} {path} {variant} {phase} start", file=sys.stderr, flush=True)
            wall = run.run_for(seconds)
            result[phase] = round(seconds / wall, 3)
            result[f"{phase}_wall_s"] = round(wall, 2)
            if args.profile:
                result[f"{phase}_profile"] = run.dump_profile(phase)
            result[f"{phase}_fast_forward"] = run.fast_forward_stats()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("firmware", nargs="*", metavar="BOARD=ELF")
    ap.add_argument("--systems", nargs="+", default=["ams", "ecu", "ecu-ams"],
                    help="system ids (systems/<id>.yaml) or files")
    ap.add_argument("--paths", nargs="+", default=["bench", "sim"], choices=["bench", "sim"])
    ap.add_argument("--boot", type=float, default=3.0, help="virtual s from power-on")
    ap.add_argument("--steady", type=float, default=5.0, help="virtual s after the boot")
    ap.add_argument("--mips", type=int, default=None, help="every core at this rate instead")
    ap.add_argument("--renode", default=DEFAULT_RENODE)
    ap.add_argument("--json", type=Path, help="write the results here")
    ap.add_argument("--baseline", action="store_true",
                    help="also measure each configuration as before #154's speed-ups")
    ap.add_argument("--repeat", type=int, default=1, help="runs per configuration (median)")
    ap.add_argument("--profile", type=Path, help="sample guest PCs into this directory")
    ap.add_argument("--log-dir", help="Renode's output per run")
    ap.add_argument("--setup", action="append", default=[],
                    help="monitor command run on every board after setup ({board}: its name); "
                         "for experiments")
    args = ap.parse_args()
    given = {k: v for k, v in (f.split("=", 1) for f in args.firmware)}
    for key, env in (("ecu", "VHIL_ECU_ELF"), ("ams", "VHIL_AMS_ELF"),
                     ("bootloader", "VHIL_CAN_BOOTLOADER_ELF")):
        if key not in given and os.environ.get(env):
            given[key] = os.environ[env]
    if args.profile:
        args.profile.mkdir(parents=True, exist_ok=True)
    if args.log_dir:
        Path(args.log_dir).mkdir(parents=True, exist_ok=True)

    host = {"cpus": os.cpu_count(), "machine": platform.machine(),
            "affinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "renode": rn.build_id(args.renode)}
    results = []
    print(f"host: {host}")
    variants = ["baseline", "current"] if args.baseline else ["current"]
    print(f"| system | path | variant | boot 0-{args.boot:g} s | steady +{args.steady:g} s |")
    print("|---|---|---|---|---|")
    for system in args.systems:
        for path in args.paths:
            for variant in variants:
                runs = [measure(system, given, path, args, variant) for _ in range(args.repeat)]
                r = dict(runs[0])
                for phase in ("boot", "steady"):
                    r[phase] = statistics.median(x[phase] for x in runs)
                    r[f"{phase}_runs"] = [x[phase] for x in runs]
                results.append(r)
                print(f"| {r['system']} | {path} | {variant} | {r['boot']:.2f}x | {r['steady']:.2f}x |",
                      flush=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"host": host, "boot_s": args.boot,
                                         "steady_s": args.steady, "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
