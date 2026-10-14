//
// Busy-wait fast-forward, per board (#154): an emulation optimisation that
// never changes what the firmware observes. Compiled by Renode at load time
// and created by the generated script for every stm32h733 board:
//
//     emulation CreateVhilFastForward "vhil_ff_ecu" "ecu"
//     vhil_ff_ecu Enabled false        (every busy-wait executed again)
//     vhil_ff_ecu Stats                (what it skipped)
//
// Virtual time is instructions / cpu.mips (docs/cpu-timing.md), so a busy-wait
// costs the host in proportion to the virtual time it waits: every MainLite's
// CAN bootloader polls three FDCANs and the IWDG for its whole 2 s auto-jump
// window (invariant 5), half a billion instructions on the ECU and a billion
// on the AMS, and the ECU's bit-banged nRF24 waits a 400-iteration counter
// loop per half bit (a third of its instructions in steady state). This
// recognises these busy-waits and, instead of executing their
// iterations, charges the exact instructions they would have executed, ending
// in the exact state they would have ended in (docs/performance.md):
//
//   Counter loops. A delay loop on a volatile counter, as GCC emits it at -O0
//   (ECU nrf24.c NRF24_BitBangDelay, `for (volatile uint32_t i = 0U; i < 400U;
//   ++i) __NOP();`):
//         L:  nop (0 or more)
//             ldr  rT, [rB, #o]
//             adds rT, #1
//             str  rT, [rB, #o]
//         C:  ldr  rT, [rB, #o]
//             cmp.w rT, #N
//             bcc|bls|bne L
//   A pre-opcode hook on CMP.W (immediate) checks, the first time it runs at
//   a PC, that the code around it is exactly this (decoded, with the same rT,
//   rB and o throughout; the bytes are compared again on every use). At the
//   CMP the loop's state is the counter i in rT and in memory; m iterations
//   later it is i + m in both, with the flags the last ADDS set, and nothing
//   else has changed. So m iterations are skipped by writing that state and
//   charging m * (6 + nops) instructions. A counter loop has no other effect:
//   no peripheral access, no other register or memory written.
//
//   Fixed-point polling loops. A loop polling peripheral registers whose value
//   can change only at an event (a clock entry, a sync point), such as the
//   bootloader's main loop. Armed once a quantum, Renode's memory-access hook
//   sees every MMIO access in Thread mode, and one iteration's accesses are
//   recorded between two passes through the same access (same PC, address,
//   width and direction). When two consecutive iterations:
//     - made the same accesses with the same values, in the same order, every
//       one of them to a register in the whitelist below (event-driven reads;
//       writes whose repetition changes nothing),
//     - executed the same number of instructions, with no interrupt or WFI,
//     - and left every CPU register and every RAM byte as the iteration
//       before them found them,
//   the machine is at a fixed point: each further iteration repeats the last
//   until an event changes a peripheral. Iterations are then skipped by
//   charging their instructions alone, never past the next event. The pass
//   that closes the second iteration must also read what the first two did:
//   it starts the next iteration, which repeats only if it does.
//
//   Time-bounded polls (#260). A peripheral that computes a register from the
//   time when it is read, but knows until when the value stands, says so with
//   `bool VhilStableUntil(long offset, out ulong ticks)` (ADC3: up to its
//   next result, Stm32H7Adc3.cs). Such a read joins the whitelist, and a skip
//   in a loop that makes one ends before that time: every skipped read comes
//   before it, by a margin for Renode's instruction-to-time rounding
//   (BoundedIterations). Its `VhilOnRead` cue arms the detector as soon as a
//   poll on it starts (HAL_ADC_PollForConversion), not at the next quantum.
//
// Exactness. Renode runs a CPU in chunks that end at the nearest clock entry
// (a timer's limit) or the end of the quantum (BaseCPU.CpuThreadBodyInner):
// events fire, interrupts are raised and sync-point actions (CAN frames from
// the bus model) run between chunks, and tlib stops exactly at the chunk's
// instruction count. A skip never crosses the end of the current chunk, so it
// lands before every event, which then fires at the same instruction count as
// without it. The instructions are charged inside tlib, by adding them to its
// executed-instruction counters, so Renode reports them as executed: virtual
// time, `cpu ExecutedInstructions`, CYCCNT, the chunk's end and everything
// timed by them are what they would have been. A polling skip leaves at least
// one real iteration before the chunk ends, so a time-dependent write the loop
// repeats (the IWDG reload) happens last at the same instruction as without
// the skip. tests/sim/test_fast_forward.py runs systems with and without this
// and compares the bus timeline, GPIO edges with instruction counts, the
// radio's payloads and the final registers and RAM.
//
// On the wall-clock bench (vhil/bench.py) a monitor command lands while the
// CPU runs, at whatever instruction the host's timing gives it (a broker's DAC
// or TCA write, say). Inside a skip it lands at the skip's end instead, at
// most one chunk later in virtual time: as host-timed as before. Lock-step
// runs (vhil.sim) take monitor commands only while paused.
//
// tlib keeps those counters in its CPUState (tlib include/cpu-defs.h,
// CPU_COMMON: instructions_count_limit, _value, _declaration, _total_value) and
// the memory-access flag (tlib_is_on_memory_access_enabled) beside them. Their
// offsets are found at run time, by value, and confirmed before the first skip
// (Calibrate): a Renode whose layout differs disables the fast-forward and
// logs why, it never guesses. Renode 1.17.0 (tlib 167decf).
//
// Not accelerated: anything else, including a loop reading a time-dependent
// register that cannot say until when it stands (DWT CYCCNT, SysTick VAL, a
// timer's CNT: not in the whitelist) or a GPIO input a device model drives on
// its own clock.
//
using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Logging.Profiling;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Memory;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Testing
{
    public static class VhilFastForwardExtensions
    {
        public static void CreateVhilFastForward(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            var ff = new VhilFastForward(machine);
            emulation.ExternalsManager.AddExternal(ff, name);
            emulation.MasterTimeSource.SyncHook += ff.OnSync;
        }
    }

    public class VhilFastForward : IExternal
    {
        public VhilFastForward(IMachine machine)
        {
            this.machine = machine;
            if(!machine.TryGetByName<TranslationCPU>("sysbus.cpu", out cpu))
            {
                throw new ArgumentException("needs sysbus.cpu");
            }
            tlib = new TlibCounters(cpu);
            if(Environment.GetEnvironmentVariable("VHIL_FAST_FORWARD") == "0")
            {
                // Off for the whole run, without its hooks: Renode as it is.
                disabledWhy = "VHIL_FAST_FORWARD=0";
                enabled = false;
                return;
            }
            if(!tlib.FindMemoryAccessFlag(out var why))
            {
                Disable(why);
                return;
            }
            accessHook = OnAccess;
            InstallAccessHook();
            // Exception entries and WFI: a polling iteration that saw one is
            // not a repetition.
            cpu.AddHookAtInterruptBegin(_ => interrupts++);
            cpu.AddHookAtWfiStateChange(_ => interrupts++);
            // CMP.W Rn, #const (T2): 11110 i 0 1101 1 Rn; tlib matches a Thumb
            // opcode's first halfword (SUBS.W Rd, Rn, #const shares it).
            compareHook = OnCompare;
            cpu.AddPreOpcodeExecutionHook(0xFBF0, 0xF1B0, compareHook);
            // A CMP.W that is not a delay loop's is left alone for good (its
            // decode is remembered as none until a reset): a Renode that can
            // stop calling the hook there (docker/renode's tlib patch) is told
            // so, and the hook then reaches C# from the delay loops only.
            var exclude = typeof(TranslationCPU).GetMethod("ExcludePreOpcodeExecutionHookAt", new[] { typeof(Action<ulong, ulong>), typeof(ulong) });
            var clear = typeof(TranslationCPU).GetMethod("ClearPreOpcodeExecutionHookExclusions", Type.EmptyTypes);
            if(exclude != null && clear != null)
            {
                excludeCompare = (Func<Action<ulong, ulong>, ulong, bool>)exclude.CreateDelegate(typeof(Func<Action<ulong, ulong>, ulong, bool>), cpu);
                clearExclusions = (Action)clear.CreateDelegate(typeof(Action), cpu);
            }
            machine.MachineReset += _ => OnReset();
            // Peripherals whose register reads are stable up to a time they
            // know (VhilStableUntil, ADC3's lazily computed conversions): a
            // read of one in Thread mode arms the detector at once, so a
            // HAL poll on it is caught where it starts (#260).
            foreach(var peripheral in machine.GetPeripheralsOfType<IBusPeripheral>())
            {
                var cue = peripheral.GetType().GetProperty("VhilOnRead", typeof(Action<long>));
                var stable = cue == null ? null : StableUntilOf(peripheral);
                if(stable != null)
                {
                    cue.SetValue(peripheral, (Action<long>)(offset => OnTimeBoundedRead(stable, offset)));
                }
            }
        }

        public void Reset()
        {
        }

        // Every fast-forward on (default) or off. Off, every busy-wait is
        // executed instruction by instruction, as Renode does on its own, and
        // the CMP.W hook is out of the translated code.
        public bool Enabled
        {
            get => enabled;
            set { enabled = value; ApplyCompareHook(); }
        }

        // The two kinds, separately (for tests and measurement).
        public bool Counters
        {
            get => counters;
            set { counters = value; ApplyCompareHook(); }
        }

        public bool Polls { get; set; } = true;

        // Log every polling-loop decision (Info): what was skipped, why a
        // candidate was refused.
        public bool Trace { get; set; }

        public ulong SkippedInstructions => counterInstructions + pollInstructions;

        // SHA-256 of every CPU register Renode lists and every RAM byte: two
        // runs that agree on it ended in the same state
        // (tests/sim/test_fast_forward.py). Not tlib's raw CPUState, which
        // holds a few host pointers that differ from one process to the next.
        public string StateDigest()
        {
            return Snapshot.Digest(cpu, Snapshot.Ram(machine));
        }

        public string Stats()
        {
            double Ms(long ticks) => Math.Round(ticks * 1000.0 / System.Diagnostics.Stopwatch.Frequency, 1);
            return $"enabled={Enabled && disabledWhy == null}"
                + (disabledWhy != null ? $" ({disabledWhy})" : "")
                + $"; counter loops: {counterSkips} skips, {counterInstructions} instructions"
                + $" ({compareCalls} CMP.W hooks, {Ms(compareTicks)} ms, {excludedCompares.Count} CMP.W excluded)"
                + $"; polling loops: {pollSkips} skips, {pollInstructions} instructions"
                + $" ({pollAttempts} armed, {pollRefusals} refused, {accessCalls} accesses, {Ms(accessTicks)} ms)"
                + $"; time-bounded polls: {boundedSkips} skips, {boundedInstructions} instructions ({cues} cues)"
                + $"; core registers via {tlib.RegisterAccess}";
        }

        public void OnSync(TimeInterval _)
        {
            cueRefused.Clear();
            if(pendingInvalidation)
            {
                pendingInvalidation = false;
                tlib.InvokeOnCpuThread(() => cpu.InvalidateTranslationBlocks());
            }
            // Master thread, between quanta: arm the polling detector for the
            // next quantum, on the CPU's thread (tlib is not thread-safe). A
            // detector armed for a whole quantum without finding an iteration
            // gives up: while armed, a host whose tlib calls its softmmu
            // helpers for RAM too (aarch64) brings every access here.
            if(armed)
            {
                if(++armedQuanta > MaxArmedQuanta && !pendingRefusal)
                {
                    pendingRefusal = true;
                    tlib.InvokeOnCpuThread(() =>
                    {
                        pendingRefusal = false;
                        if(armed)
                        {
                            Refuse("no iteration within a quantum");
                        }
                    });
                }
                return;
            }
            if(!enabled || !Polls || disabledWhy != null || pendingArm)
            {
                return;
            }
            if(backoff > 0)
            {
                backoff--;
                return;
            }
            RequestArm();
        }

        // -- counter loops ------------------------------------------------------

        private void OnCompare(ulong pc, ulong opcode)
        {
            var t0 = System.Diagnostics.Stopwatch.GetTimestamp();
            compareCalls++;
            if(enabled && counters && disabledWhy == null)
            {
                SkipCounterLoop(pc);
            }
            compareTicks += System.Diagnostics.Stopwatch.GetTimestamp() - t0;
        }

        private void SkipCounterLoop(ulong pc)
        {
            if(!loops.TryGetValue(pc, out var loop))
            {
                loop = CounterLoop.Decode(machine.SystemBus, cpu, pc);
                loops[pc] = loop;
                if(loop == null && excludeCompare != null && excludeCompare(compareHook, pc))
                {
                    // The blocks holding it are invalidated at the next sync.
                    excludedCompares.Add(pc);
                    pendingInvalidation = true;
                }
            }
            if(loop == null || !loop.StillThere())
            {
                return;
            }
            if(!Calibrated())
            {
                return;
            }
            var i = tlib.CoreRegister(loop.Rt);
            var address = (ulong)unchecked(tlib.CoreRegister(loop.Rb) + loop.Offset);
            if(!RamAt(address, out var ram, out var ramOffset) || ReadRam(ram, ramOffset) != i)
            {
                return;   // not the loop's state as decoded: leave it alone
            }
            var m = Math.Min(loop.StepsTaken(i), tlib.Budget() / loop.InstructionsPerStep);
            if(m < 2)
            {
                return;
            }
            var v = unchecked(i + (uint)m);
            WriteRam(ram, ramOffset, address, v);
            tlib.SetCoreRegister(loop.Rt, v);
            // The flags the last ADDS rT, #1 set (ARMv7-M ARM A7.7.3, AddWithCarry).
            var before = unchecked(v - 1);
            var nzcv = (v >> 31) << 3 | (v == 0 ? 1u : 0u) << 2
                | (before == 0xFFFFFFFFu ? 1u : 0u) << 1 | (before == 0x7FFFFFFFu ? 1u : 0u);
            var xpsr = (uint)cpu.GetRegister(XpsrIndex).RawValue;
            cpu.SetRegister(XpsrIndex, (xpsr & 0x0FFFFFFFu) | nzcv << 28);
            var charged = m * loop.InstructionsPerStep;
            tlib.Charge(charged);
            counterSkips++;
            counterInstructions += charged;
        }

        // The RAM holding a counter, remembered from the last lookup: a delay
        // loop's counter is on a task's stack, the same memory every time.
        private bool RamAt(ulong address, out MappedMemory memory, out long offset)
        {
            if(lastRam == null || address < lastRamStart || address + 4 > lastRamEnd)
            {
                var at = machine.SystemBus.WhatIsAt(address, cpu);
                if(!(at?.Peripheral is MappedMemory found))
                {
                    memory = null;
                    offset = 0;
                    return false;
                }
                lastRam = found;
                lastRamStart = at.RegistrationPoint.Range.StartAddress;
                lastRamEnd = at.RegistrationPoint.Range.EndAddress + 1;
                // Where MappedMemory invalidates a write (GetRegistrationPoints:
                // StartAddress + Offset of each); null: write through it.
                lastRamPoints = DelayedInvalidationField == null ? null
                    : machine.SystemBus.GetRegistrationPoints(found)
                        .Select(p => (long)(p.Range.StartAddress + p.Offset)).ToList();
            }
            memory = lastRam;
            offset = (long)(address - lastRamStart);
            return true;
        }

        // MappedMemory's ReadDoubleWord and WriteDoubleWord, without their
        // per-access segment lock and the write's dynamic dispatch (#260):
        // the same bytes, and the same translation-block invalidation over
        // the word at each of the memory's registration points
        // (MappedMemory.InvalidateMemoryFragment).
        private unsafe uint ReadRam(MappedMemory memory, long offset)
        {
            var segment = memory.GetSegment((int)(offset / memory.SegmentSize));
            var local = offset % memory.SegmentSize;
            if(segment == IntPtr.Zero || local > memory.SegmentSize - 4)
            {
                return memory.ReadDoubleWord(offset);
            }
            return *(uint*)((byte*)segment + local);
        }

        private unsafe void WriteRam(MappedMemory memory, long offset, ulong address, uint value)
        {
            var segment = memory.GetSegment((int)(offset / memory.SegmentSize));
            var local = offset % memory.SegmentSize;
            if(segment == IntPtr.Zero || local > memory.SegmentSize - 4 || lastRamPoints == null)
            {
                memory.WriteDoubleWord(offset, value);
                return;
            }
            *(uint*)((byte*)segment + local) = value;
            var delayed = (bool)DelayedInvalidationField.GetValue(memory);
            foreach(var point in lastRamPoints)
            {
                cpu.OrderTranslationBlocksInvalidation(new IntPtr(point + offset), new IntPtr(point + offset + 4), delayed);
            }
        }

        private static readonly FieldInfo DelayedInvalidationField =
            typeof(MappedMemory).GetField("delayedInvalidation", BindingFlags.Instance | BindingFlags.NonPublic);

        private void ApplyCompareHook()
        {
            if(disabledWhy != null)
            {
                return;
            }
            // tlib puts the hook into blocks as it translates them: drop the
            // blocks translated so far, on the CPU's thread.
            var on = enabled && counters;
            tlib.InvokeOnCpuThread(() =>
            {
                cpu.EnablePreOpcodeExecutionHooks(on);
                cpu.ClearTranslationCache();
            });
        }

        // -- fixed-point polling loops -----------------------------------------------

        private void RequestArm()
        {
            pendingArm = true;
            tlib.InvokeOnCpuThread(Arm);
        }

        // On the CPU's thread, between chunks.
        private void Arm()
        {
            pendingArm = false;
            if(!enabled || !Polls || disabledWhy != null || armed)
            {
                return;
            }
            if(!InstallAccessHook())
            {
                return;   // another user of the hook (flash programming): later
            }
            armed = true;
            cuePc = null;
            armedQuanta = 0;
            pollAttempts++;
            state = PollState.Seek;
            accessesThisAttempt = 0;
            retries = 0;
            tlib.SetMemoryAccessFlag(true);
        }

        private void Disarm()
        {
            // The flag is ours only while our hook is the CPU's: the flash
            // controller turns it on for its own while it programs.
            if(armed && tlib.MemoryAccessHook == accessHook)
            {
                tlib.SetMemoryAccessFlag(false);
            }
            armed = false;
            state = PollState.Off;
        }

        private void Refuse(string why)
        {
            Disarm();
            if(cuePc != null)
            {
                cueRefused.Add(cuePc.Value);   // not from this read again this quantum
            }
            pollRefusals++;
            backoff = Math.Min(MaxBackoff, Math.Max(1, lastBackoff * 2));
            lastBackoff = backoff;
            if(Trace)
            {
                cpu.Log(LogLevel.Info, "fast-forward: no fixed point: {0}", why);
            }
        }

        private void OnAccess(ulong pc, MemoryOperation operation, ulong virtualAddress, ulong physicalAddress,
                              uint width, ulong value)
        {
            // Every access reaches here while armed on a host whose tlib
            // backend calls its softmmu helpers for RAM too (aarch64); only
            // the MMIO ones count, and only in Thread mode (IPSR 0): a
            // handler's are not the loop's.
            if(!armed || (operation != MemoryOperation.MemoryIORead && operation != MemoryOperation.MemoryIOWrite)
               || ((uint)cpu.GetRegister(XpsrIndex).RawValue & 0x1FF) != 0)
            {
                return;
            }
            var t0 = System.Diagnostics.Stopwatch.GetTimestamp();
            accessCalls++;
            OnPeripheralAccess(operation == MemoryOperation.MemoryIOWrite, physicalAddress, width, value);
            accessTicks += System.Diagnostics.Stopwatch.GetTimestamp() - t0;
        }

        private void OnPeripheralAccess(bool write, ulong address, uint width, ulong value)
        {
            if(!enabled || !Polls)
            {
                Disarm();
                return;
            }
            if(++accessesThisAttempt > MaxAccessesPerAttempt)
            {
                Refuse("no repeating iteration");
                return;
            }
            if(!Whitelisted(address, write, value, out var why))
            {
                Refuse(why);
                return;
            }
            if(!Calibrated())
            {
                Disarm();
                return;
            }
            // The exact position: the PC tlib keeps per instruction (Renode's
            // default, SyncPCEveryInstructionDisabled false), and the executed
            // count of the blocks before this one plus this instruction's index
            // in its block.
            var index = tlib.RestoreContext();
            if(index < 0)
            {
                Refuse("no block for this access");
                return;
            }
            var position = cpu.GetCurrentInstructionsCount() + (ulong)index;
            var access = new Access(cpu.PC.RawValue, address, width, write, value);
            if(state == PollState.Seek)
            {
                Begin(PollState.First, access, position);
                return;
            }
            if(!access.SamePlace(boundary))
            {
                trace.Add(access);
                if(trace.Count > MaxTrace)
                {
                    Refuse("iteration too long");
                }
                return;
            }
            OnBoundary(access, position);
        }

        private void Begin(PollState next, Access access, ulong position)
        {
            boundary = access;
            trace.Clear();
            trace.Add(access);
            boundaryPosition = position;
            boundaryInterrupts = interrupts;
            state = next;
        }

        // The same access again: one iteration done.
        private void OnBoundary(Access access, ulong position)
        {
            var k = position - boundaryPosition;
            if(interrupts != boundaryInterrupts || k == 0 || !access.Equals(boundary))
            {
                // An interrupt in between, or the boundary read itself
                // returned something new (a time-bounded register changes
                // within a chunk): the next iteration need not repeat the
                // last. Start again from here.
                Begin(PollState.First, access, position);
                return;
            }
            if(state == PollState.First || !SameIteration(k))
            {
                // The first iteration seen (or one unlike the last): keep it
                // and the state at its end, to compare the next one with.
                if(state == PollState.Second && ++retries > MaxRetries)
                {
                    Refuse($"iterations of {k} instructions at 0x{access.Pc:X8} differ");
                    return;
                }
                firstTrace.Clear();
                firstTrace.AddRange(trace);
                firstK = k;
                snapshot.Take(tlib, memories);
                Begin(PollState.Second, access, position);
                return;
            }
            if(!snapshot.Matches(tlib, memories, out var where))
            {
                if(++retries > MaxRetries)
                {
                    Refuse($"an iteration of {k} instructions at 0x{access.Pc:X8} changes {where}");
                    return;
                }
                snapshot.Take(tlib, memories);
                Begin(PollState.Second, access, position);
                return;
            }
            // A fixed point. Leave one real iteration, and the rest of the
            // current block (at most one iteration: every iteration has a
            // branch, and a block ends at one), before the chunk ends.
            var budget = tlib.Budget();
            var n = budget > 2 * k ? (budget - 2 * k) / k : 0;
            var bounded = false;
            var hasBound = BoundOfTrace(out var until);
            if(n > 0 && hasBound)
            {
                bounded = true;
                n = Math.Min(n, BoundedIterations(until, k, position));
            }
            if(n > 0)
            {
                tlib.Charge(n * k);
                pollSkips++;
                pollInstructions += n * k;
                if(bounded)
                {
                    boundedSkips++;
                    boundedInstructions += n * k;
                }
                if(Trace)
                {
                    cpu.Log(LogLevel.Info, "fast-forward: {0} iterations of {1} instructions at 0x{2:X8} ({3} accesses)",
                            n, k, access.Pc, trace.Count);
                }
            }
            // Nothing more in this chunk; look again in the next, after the
            // event that ends this one.
            lastBackoff = 0;
            Disarm();
            RequestArm();
        }

        // The earliest time (ticks, exclusive) up to which every time-bounded
        // read of the iteration returns what it returned; false if it has none.
        private bool BoundOfTrace(out ulong until)
        {
            until = ulong.MaxValue;
            var any = false;
            foreach(var access in trace)
            {
                if(access.Write || !kinds.TryGetValue(access.Address, out var kind) || kind != RegisterKind.TimeBounded)
                {
                    continue;
                }
                any = true;
                var r = timeBounded[access.Address];
                if(!r.Stable(r.Offset, out var t))
                {
                    until = 0;
                    return true;
                }
                until = Math.Min(until, t);
            }
            return any;
        }

        // Iterations of k instructions, from the iteration starting at
        // `position`, whose reads all come before `until`. The time a read
        // sees is Renode's: tlib's executed instructions reported to the time
        // source (cpu.SyncTime) at the CPU's rate, less a residuum of fewer
        // than mips / gcd(mips, ticks per us) instructions (BaseCPU
        // ReportProgress, TimeInterval.FromCPUCycles). Synced here, the time T
        // covers at least the instructions before this block (E), so a read
        // at instruction X >= E sees at most T + (X - E + residuum) / rate.
        // Every skipped read is at X < position + n * k; a margin of one more
        // iteration and a few instructions keeps that bound strict whatever
        // tlib counts of the current block.
        private ulong BoundedIterations(ulong until, ulong k, ulong position)
        {
            if(until == ulong.MaxValue)
            {
                return ulong.MaxValue;
            }
            cpu.SyncTime();
            var now = machine.ElapsedVirtualTime.TimeElapsed.Ticks;
            var e = cpu.GetCurrentInstructionsCount();
            if(until <= now || position < e)
            {
                return 0;
            }
            var mips = (ulong)cpu.PerformanceInMips;
            var ticksPerUs = TimeInterval.FromMicroseconds(1).Ticks;
            var residuum = mips / Gcd(mips, ticksPerUs);
            var room = (until - now - 1) * mips / ticksPerUs;   // instructions after E, all before `until`
            var margin = (position - e) + residuum + k + 16;
            if(Trace)
            {
                cpu.Log(LogLevel.Info, "fast-forward: bounded until {0} now {1} room {2} margin {3}", until, now, room, margin);
            }
            return room > margin ? (room - margin) / k : 0;
        }

        private static ulong Gcd(ulong a, ulong b)
        {
            while(b != 0)
            {
                var t = a % b;
                a = b;
                b = t;
            }
            return a;
        }

        // A read of a time-bounded register (VhilOnRead): arm the detector now,
        // on the CPU's thread, in Thread mode, when the register will not
        // change for at least MinCueTicks, no attempt is armed, and none armed
        // from this read was refused this quantum.
        private void OnTimeBoundedRead(StableUntil stable, long offset)
        {
            if(armed || !enabled || !Polls || disabledWhy != null)
            {
                return;
            }
            if(!machine.SystemBus.TryGetCurrentCPU(out var current) || current != cpu
               || ((uint)cpu.GetRegister(XpsrIndex).RawValue & 0x1FF) != 0)
            {
                return;
            }
            var pc = cpu.PC.RawValue;
            if(cueRefused.Contains(pc) || !stable(offset, out var until) || until == ulong.MaxValue
               || until < machine.ElapsedVirtualTime.TimeElapsed.Ticks + MinCueTicks)
            {
                return;   // refused here this quantum, or no wait worth skipping ahead
            }
            cues++;
            Arm();
            cuePc = pc;
        }

        private static StableUntil StableUntilOf(IPeripheral peripheral)
        {
            var method = peripheral.GetType().GetMethod("VhilStableUntil", new[] { typeof(long), typeof(ulong).MakeByRefType() });
            if(method == null || method.ReturnType != typeof(bool))
            {
                return null;
            }
            return (StableUntil)Delegate.CreateDelegate(typeof(StableUntil), peripheral, method);
        }

        private bool SameIteration(ulong k)
        {
            if(k != firstK || trace.Count != firstTrace.Count)
            {
                return false;
            }
            for(var n = 0; n < trace.Count; n++)
            {
                if(!trace[n].Equals(firstTrace[n]))
                {
                    return false;
                }
            }
            return true;
        }

        private bool Calibrated()
        {
            if(tlib.Calibrated)
            {
                return true;
            }
            if(!tlib.Calibrate(out var why) && why != null)
            {
                Disable(why);
            }
            return tlib.Calibrated;
        }

        // Peripheral registers whose reads return a value that changes only at
        // an event (a clock entry, a sync point) or by the loop's own accesses,
        // and writes whose repetition changes nothing. A read with a side
        // effect (FDCAN ECR.CEL and PSR.LEC clear on read) passes because two
        // iterations must read the same values: once read, the side effect is
        // a fixed point.
        private bool Whitelisted(ulong address, bool write, ulong value, out string why)
        {
            if(!kinds.TryGetValue(address, out var kind))
            {
                kind = Classify(address);
                kinds[address] = kind;
            }
            why = null;
            switch(kind)
            {
            case RegisterKind.EventDrivenRead:
                if(!write)
                {
                    return true;
                }
                break;
            case RegisterKind.TimeBounded:
                if(!write)
                {
                    var r = timeBounded[address];
                    if(r.Stable(r.Offset, out var _))
                    {
                        return true;
                    }
                }
                break;
            case RegisterKind.WatchdogKey:
                // KR = 0xAAAA reloads the counter (RM0468 IWDG_KR): a restart
                // at the time of the last write, which a skip keeps (it leaves
                // a real iteration before the chunk's end). Only with the
                // window off: Renode's reload compares the counter with the
                // window, a time-dependent decision.
                if(write && (value & 0xFFFF) == 0xAAAA && !WatchdogWindowEnabled(address))
                {
                    return true;
                }
                break;
            }
            why = $"{(write ? "write" : "read")} of 0x{address:X8} ({Describe(address)})";
            return false;
        }

        private RegisterKind Classify(ulong address)
        {
            var at = machine.SystemBus.WhatIsAt(address, cpu);
            if(at == null)
            {
                return RegisterKind.Other;
            }
            var offset = address - at.RegistrationPoint.Range.StartAddress;
            for(var t = at.Peripheral.GetType(); t != null; t = t.BaseType)
            {
                if(t.FullName == "Antmicro.Renode.Peripherals.CAN.MCAN")
                {
                    // FDCAN (RM0468 59.5; Stm32H7Fdcan.cs ReadDoubleWord): CCCR
                    // 0x018, ECR 0x040, PSR 0x044, IR 0x050, RXF0S 0x0A4, RXF1S
                    // 0x0B4, TXFQS 0x0C4, TXBRP 0x0CC. They change when the
                    // controller does: a frame from the bus model (at a sync
                    // point or a scheduled action), a transmission's end (a
                    // clock entry), a firmware write. Not TSCV 0x024 or TOCV
                    // 0x02C, the timestamp and timeout counters, which count
                    // time.
                    switch(offset)
                    {
                    case 0x018: case 0x040: case 0x044: case 0x050:
                    case 0x0A4: case 0x0B4: case 0x0C4: case 0x0CC:
                        return RegisterKind.EventDrivenRead;
                    }
                    return RegisterKind.Other;
                }
                var stable = StableUntilOf(at.Peripheral);
                if(stable != null)
                {
                    timeBounded[address] = new TimeBoundedRegister { Stable = stable, Offset = (long)offset };
                    return RegisterKind.TimeBounded;
                }
                if(t.FullName == "Antmicro.Renode.Peripherals.Timers.STM32_IndependentWatchdog")
                {
                    return offset == 0x00 ? RegisterKind.WatchdogKey : RegisterKind.Other;
                }
            }
            return RegisterKind.Other;
        }

        private bool WatchdogWindowEnabled(ulong address)
        {
            var at = machine.SystemBus.WhatIsAt(address, cpu);
            var field = at?.Peripheral.GetType().GetField("windowEnabled", BindingFlags.Instance | BindingFlags.NonPublic);
            return field == null || (bool)field.GetValue(at.Peripheral);
        }

        private string Describe(ulong address)
        {
            var at = machine.SystemBus.WhatIsAt(address, cpu);
            return at == null ? "unmapped"
                : $"{at.Peripheral.GetType().Name} +0x{address - at.RegistrationPoint.Range.StartAddress:X}";
        }

        // Renode keeps one memory-access hook per CPU; the flash controller
        // takes it while it programs (Stm32H7Flash.cs). Ours is put back when
        // the hook is free again; false while someone else holds it.
        private bool InstallAccessHook()
        {
            var current = tlib.MemoryAccessHook;
            if(current == accessHook)
            {
                return true;
            }
            if(current != null)
            {
                return false;
            }
            cpu.SetHookAtMemoryAccess(accessHook);
            // SetHookAtMemoryAccess also sends every block translated from now
            // on through tlib's slow path; turn that off, keep the hook, and
            // switch the flag alone while armed.
            tlib.MemoryAccessEventEnabled(false);
            return true;
        }

        private void OnReset()
        {
            loops.Clear();
            if(clearExclusions != null && excludedCompares.Count > 0)
            {
                // Every CMP.W is looked at again after a reset, as the
                // remembered decodes are gone: the hook back everywhere, in
                // the blocks holding an excluded one invalidated, on the CPU's
                // thread before it next executes (not ClearTranslationCache,
                // which pauses the machine: from the CPU's thread it waits on
                // itself).
                var excluded = excludedCompares.ToArray();
                excludedCompares.Clear();
                tlib.InvokeOnCpuThread(() =>
                {
                    clearExclusions();
                    foreach(var pc in excluded)
                    {
                        cpu.OrderTranslationBlocksInvalidation(new IntPtr(unchecked((long)pc)), new IntPtr(unchecked((long)pc + 4)), true);
                    }
                    cpu.InvalidateTranslationBlocks();
                });
            }
            kinds.Clear();
            timeBounded.Clear();
            Disarm();
        }

        private void Disable(string why)
        {
            disabledWhy = why;
            cpu.Log(LogLevel.Warning, "fast-forward disabled: {0}", why);
        }

        private const int XpsrIndex = 25;              // Renode's Cortex-M CPSR: the xPSR
        private const int MaxAccessesPerAttempt = 256;
        private const int MaxTrace = 64;
        private const int MaxRetries = 2;
        private const int MaxBackoff = 64;             // quanta
        private const int MaxArmedQuanta = 2;
        // A time-bounded read arms the detector only for a wait at least this
        // long (ADC3's single conversions on the ECU take 156 ns).
        private static readonly ulong MinCueTicks = TimeInterval.FromMicroseconds(5).Ticks;

        private readonly IMachine machine;
        private readonly TranslationCPU cpu;
        private readonly TlibCounters tlib;
        private readonly MemoryAccessHook accessHook;
        private readonly Dictionary<ulong, CounterLoop> loops = new Dictionary<ulong, CounterLoop>();
        private readonly Action<ulong, ulong> compareHook;
        private readonly Func<Action<ulong, ulong>, ulong, bool> excludeCompare;
        private readonly Action clearExclusions;
        private readonly List<ulong> excludedCompares = new List<ulong>();
        private bool pendingInvalidation;
        private readonly Dictionary<ulong, RegisterKind> kinds = new Dictionary<ulong, RegisterKind>();
        private readonly Dictionary<ulong, TimeBoundedRegister> timeBounded = new Dictionary<ulong, TimeBoundedRegister>();
        private ulong? cuePc;                         // the read that armed this attempt, if one did
        private readonly HashSet<ulong> cueRefused = new HashSet<ulong>();
        private ulong cues;
        private ulong boundedSkips;
        private ulong boundedInstructions;
        private readonly List<Access> trace = new List<Access>();
        private readonly List<Access> firstTrace = new List<Access>();
        private readonly Snapshot snapshot = new Snapshot();
        private List<MappedMemory> memories => memoriesCache ?? (memoriesCache = Snapshot.Ram(machine));
        private List<MappedMemory> memoriesCache;

        private MappedMemory lastRam;
        private List<long> lastRamPoints;
        private ulong lastRamStart;
        private ulong lastRamEnd;
        private bool enabled = true;
        private bool counters = true;
        private string disabledWhy;
        private volatile bool pendingArm;
        private bool armed;
        private int backoff;
        private int lastBackoff;
        private int retries;
        private PollState state;
        private Access boundary;
        private ulong boundaryPosition;
        private ulong boundaryInterrupts;
        private ulong firstK;
        private int accessesThisAttempt;
        private ulong interrupts;
        private int armedQuanta;
        private volatile bool pendingRefusal;

        private ulong counterSkips;
        private ulong counterInstructions;
        private ulong pollSkips;
        private ulong pollInstructions;
        private ulong pollAttempts;
        private ulong pollRefusals;
        private ulong compareCalls;
        private ulong accessCalls;
        private long compareTicks;
        private long accessTicks;

        private enum PollState { Off, Seek, First, Second }

        private enum RegisterKind { Other, EventDrivenRead, WatchdogKey, TimeBounded }

        private delegate bool StableUntil(long offset, out ulong ticks);

        private struct TimeBoundedRegister
        {
            public StableUntil Stable;
            public long Offset;
        }

        private struct Access : IEquatable<Access>
        {
            public Access(ulong pc, ulong address, uint width, bool write, ulong value)
            {
                Pc = pc;
                Address = address;
                Width = width;
                Write = write;
                Value = value;
            }

            public bool SamePlace(Access other)
            {
                return Pc == other.Pc && Address == other.Address && Width == other.Width && Write == other.Write;
            }

            public bool Equals(Access other)
            {
                return SamePlace(other) && Value == other.Value;
            }

            public readonly ulong Pc;
            public readonly ulong Address;
            public readonly uint Width;
            public readonly bool Write;
            public readonly ulong Value;
        }
    }

    // A volatile counter loop at its CMP.W (see VhilFastForward).
    public class CounterLoop
    {
        public static CounterLoop Decode(IBusController bus, ICPU cpu, ulong cmp)
        {
            ushort H(ulong a) => (ushort)bus.ReadWord(a, cpu);
            var first = H(cmp);
            var second = H(cmp + 2);
            // CMP.W Rn, #const (ARMv7-M ARM A7.7.27 T2): Rd field 1111.
            if((first & 0xFBF0) != 0xF1B0 || (second & 0x8F00) != 0x0F00)
            {
                return null;
            }
            var rt = first & 0xF;
            if(!ThumbExpandImm((uint)((first >> 10) & 1) << 11 | (uint)((second >> 12) & 7) << 8 | (uint)(second & 0xFF),
                               out var limit))
            {
                return null;
            }
            // B<c> back to L (A7.7.12 T1).
            var branch = H(cmp + 4);
            if((branch & 0xF000) != 0xD000)
            {
                return null;
            }
            var cond = (branch >> 8) & 0xF;
            if(cond != CondCc && cond != CondLs && cond != CondNe)
            {
                return null;
            }
            var target = (ulong)((long)cmp + 4 + 4 + (sbyte)(branch & 0xFF) * 2);
            // C: LDR rT, [rB, #o] right before the CMP.
            if(!DecodeLoadStore(H(cmp - 2), load: true, out var cRt, out var rb, out var offset) || cRt != rt)
            {
                return null;
            }
            // L: nops, LDR, ADDS #1, STR, then C.
            var nops = 0;
            var at = target;
            while(at < cmp - 8 && H(at) == 0xBF00)
            {
                nops++;
                at += 2;
            }
            if(at + 6 != cmp - 2)
            {
                return null;
            }
            if(!DecodeLoadStore(H(at), load: true, out var lRt, out var lRb, out var lOff)
               || lRt != rt || lRb != rb || lOff != offset)
            {
                return null;
            }
            var add = H(at + 2);
            // ADDS rT, #1 (A7.7.3 T2: 00110 Rdn imm8) or ADDS rT, rT, #1 (T1).
            if(add != (0x3000 | rt << 8 | 1) && add != (0x1C40 | rt << 3 | rt))
            {
                return null;
            }
            if(!DecodeLoadStore(H(at + 4), load: false, out var sRt, out var sRb, out var sOff)
               || sRt != rt || sRb != rb || sOff != offset)
            {
                return null;
            }
            if(rb == rt)
            {
                return null;
            }
            var bytes = new byte[cmp + 6 - target];
            for(var n = 0; n < bytes.Length; n += 2)
            {
                var h = H(target + (ulong)n);
                bytes[n] = (byte)h;
                bytes[n + 1] = (byte)(h >> 8);
            }
            // The code's memory, to check it is unchanged on every use without
            // a bus access per halfword.
            var at2 = bus.WhatIsAt(target, cpu);
            if(!(at2?.Peripheral is MappedMemory memory))
            {
                return null;
            }
            var offset2 = (long)(target - at2.RegistrationPoint.Range.StartAddress);
            if(offset2 / memory.SegmentSize != (offset2 + bytes.Length - 1) / memory.SegmentSize)
            {
                return null;   // across two segments: not worth the bookkeeping
            }
            return new CounterLoop
            {
                Start = target, Rt = rt, Rb = rb, Offset = offset, Limit = limit, Condition = cond,
                InstructionsPerStep = (ulong)(nops + 6), code = bytes, codeMemory = memory,
                codeSegment = (int)(offset2 / memory.SegmentSize), codeOffset = (int)(offset2 % memory.SegmentSize),
            };
        }

        // Steps the loop still takes from the CMP with the counter at i: each
        // one sees the branch taken.
        public ulong StepsTaken(uint i)
        {
            switch(Condition)
            {
            case CondCc:   // unsigned <
                return i < Limit ? Limit - i : 0;
            case CondLs:   // unsigned <=
                return Limit == 0xFFFFFFFFu || i > Limit ? 0 : (ulong)Limit - i + 1;
            default:       // !=
                return unchecked(Limit - i);
            }
        }

        // The decoded bytes are still the code there (a flash write changes
        // it without a reset only through the flash controller, but check).
        public unsafe bool StillThere()
        {
            var segment = codeMemory.GetSegment(codeSegment);
            return segment != IntPtr.Zero
                && new ReadOnlySpan<byte>((byte*)segment + codeOffset, code.Length).SequenceEqual(code);
        }

        public ulong Start;
        public int Rt;
        public int Rb;
        public uint Offset;
        public uint Limit;
        public int Condition;
        public ulong InstructionsPerStep;

        // LDR/STR (immediate) T1 (Rn-relative, imm5 * 4) or T2 (SP-relative, imm8 * 4).
        private static bool DecodeLoadStore(ushort h, bool load, out int rt, out int rb, out uint offset)
        {
            var l = load ? 0x0800 : 0;
            if((h & 0xF800) == (0x6000 | l))
            {
                rt = h & 7;
                rb = (h >> 3) & 7;
                offset = (uint)((h >> 6) & 0x1F) * 4;
                return true;
            }
            if((h & 0xF800) == (0x9000 | l))
            {
                rt = (h >> 8) & 7;
                rb = 13;
                offset = (uint)(h & 0xFF) * 4;
                return true;
            }
            rt = rb = -1;
            offset = 0;
            return false;
        }

        // ARMv7-M ARM A5.3.2, without the carry out (CMP sets C from the
        // subtraction, not from the expansion).
        private static bool ThumbExpandImm(uint imm12, out uint value)
        {
            var imm8 = imm12 & 0xFF;
            if((imm12 >> 10) == 0)
            {
                switch((imm12 >> 8) & 3)
                {
                case 0: value = imm8; return true;
                case 1: value = imm8 << 16 | imm8; return imm8 != 0;
                case 2: value = imm8 << 24 | imm8 << 8; return imm8 != 0;
                default: value = imm8 << 24 | imm8 << 16 | imm8 << 8 | imm8; return imm8 != 0;
                }
            }
            var unrotated = 0x80u | (imm12 & 0x7F);
            var rotation = (int)(imm12 >> 7);
            value = unrotated >> rotation | unrotated << (32 - rotation);
            return true;
        }

        private const int CondNe = 0x1;
        private const int CondCc = 0x3;
        private const int CondLs = 0x9;

        private byte[] code;
        private MappedMemory codeMemory;
        private int codeSegment;
        private int codeOffset;
    }

    // The CPU's architectural state and every RAM byte, at one point of a
    // loop. The CPU's is tlib's CPUState up to CPU_COMMON (tlib arch/arm/cpu.h:
    // the core and banked registers, the flags, IT state, the v7-M system
    // registers, the FPU, the exclusive monitor), compared as bytes: a
    // superset of what the firmware can observe.
    public class Snapshot
    {
        // RAM: the machine's memories outside the ARMv7-M Code region's flash
        // (0x08000000-0x1FFFFFFF), which only the flash controller writes.
        public static List<MappedMemory> Ram(IMachine machine)
        {
            var list = new List<MappedMemory>();
            foreach(var mem in machine.GetPeripheralsOfType<MappedMemory>())
            {
                var points = machine.SystemBus.GetRegistrationPoints(mem);
                if(points.Any(p => p.Range.StartAddress >= 0x08000000 && p.Range.StartAddress < 0x20000000))
                {
                    continue;
                }
                list.Add(mem);
            }
            return list;
        }

        // Memory is compared segment by segment, in place: only the segments
        // the emulation has written (MappedMemory allocates a segment when it
        // is first touched).
        public unsafe void Take(TlibCounters tlib, List<MappedMemory> memories)
        {
            var arch = tlib.Architectural;
            if(registers.Length != arch.Length)
            {
                registers = new byte[arch.Length];
            }
            arch.CopyTo(registers);
            if(saved == null)
            {
                saved = memories.Select(m => new byte[Segments(m)][]).ToArray();
            }
            for(var n = 0; n < memories.Count; n++)
            {
                var mem = memories[n];
                for(var s = 0; s < saved[n].Length; s++)
                {
                    var segment = mem.GetSegment(s);
                    if(segment == IntPtr.Zero)
                    {
                        saved[n][s] = null;
                        continue;
                    }
                    var count = Length(mem, s);
                    if(saved[n][s] == null)
                    {
                        saved[n][s] = new byte[count];
                    }
                    new ReadOnlySpan<byte>((void*)segment, count).CopyTo(saved[n][s]);
                }
            }
        }

        public unsafe bool Matches(TlibCounters tlib, List<MappedMemory> memories, out string where)
        {
            if(!tlib.Architectural.SequenceEqual(registers))
            {
                where = "the CPU's registers";
                return false;
            }
            for(var n = 0; n < memories.Count; n++)
            {
                var mem = memories[n];
                for(var s = 0; s < saved[n].Length; s++)
                {
                    var segment = mem.GetSegment(s);
                    if((segment != IntPtr.Zero) != (saved[n][s] != null))
                    {
                        where = $"memory {n} segment {s}";
                        return false;
                    }
                    if(segment == IntPtr.Zero)
                    {
                        continue;
                    }
                    if(!new ReadOnlySpan<byte>((void*)segment, saved[n][s].Length).SequenceEqual(saved[n][s]))
                    {
                        where = $"RAM in segment {s} of memory {n}";
                        return false;
                    }
                }
            }
            where = null;
            return true;
        }

        public static unsafe string Digest(TranslationCPU cpu, List<MappedMemory> memories)
        {
            using(var sha = System.Security.Cryptography.SHA256.Create())
            {
                foreach(var register in cpu.GetRegisters().OrderBy(r => r.Index))
                {
                    var value = BitConverter.GetBytes(cpu.GetRegister(register.Index).RawValue);
                    sha.TransformBlock(value, 0, value.Length, null, 0);
                }
                foreach(var mem in memories)
                {
                    for(var s = 0; s < Segments(mem); s++)
                    {
                        var segment = mem.GetSegment(s);
                        if(segment == IntPtr.Zero)
                        {
                            continue;
                        }
                        var bytes = new ReadOnlySpan<byte>((void*)segment, Length(mem, s)).ToArray();
                        var tag = BitConverter.GetBytes(((long)memories.IndexOf(mem) << 32) | (long)s);
                        sha.TransformBlock(tag, 0, tag.Length, null, 0);
                        sha.TransformBlock(bytes, 0, bytes.Length, null, 0);
                    }
                }
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return BitConverter.ToString(sha.Hash).Replace("-", "");
            }
        }

        private static int Segments(MappedMemory m)
        {
            return (int)((m.Size + m.SegmentSize - 1) / m.SegmentSize);
        }

        private static int Length(MappedMemory m, int segment)
        {
            return (int)Math.Min(m.SegmentSize, m.Size - (long)segment * m.SegmentSize);
        }

        private byte[] registers = new byte[0];
        private byte[][][] saved;
    }

    // tlib's executed-instruction counters and memory-access flag, in its
    // CPUState (tlib include/cpu-defs.h CPU_COMMON).
    public class TlibCounters
    {
        public TlibCounters(TranslationCPU cpu)
        {
            this.cpu = cpu;
            exportState = Delegate<Func<IntPtr>>("TlibExportState");
            stateSize = Delegate<Func<int>>("TlibGetStateSize");
            restoreContext = Delegate<Func<int>>("TlibRestoreContext");
            memoryAccessEventEnabled = Delegate<Action<int>>("TlibOnMemoryAccessEventEnabled");
            hookField = Field("memoryAccessHook");
            invoke = Method("InvokeInCpuThreadSafely");
        }

        public bool Calibrated => totalOffset >= 0;

        // The CPUState pointer, which tlib allocates once per CPU.
        private IntPtr State => state != IntPtr.Zero ? state : (state = exportState());

        // R0-R12 and SP: tlib keeps them in CPUState.regs[16], its first field
        // (tlib arch/arm/cpu.h), which GetRegister and SetRegister read and
        // write for these. Read from there once the layout is confirmed
        // against GetRegister (RegistersConfirmed), else through it.
        public uint CoreRegister(int n)
        {
            if(registersInState == 0)
            {
                ConfirmRegisters();
            }
            return registersInState > 0 ? (uint)Marshal.ReadInt32(State, 4 * n) : (uint)cpu.GetRegister(n).RawValue;
        }

        public string RegisterAccess => registersInState > 0 ? "CPUState" : registersInState < 0 ? "GetRegister" : "not used yet";

        public void SetCoreRegister(int n, uint value)
        {
            if(registersInState > 0)
            {
                Marshal.WriteInt32(State, 4 * n, unchecked((int)value));
            }
            else
            {
                cpu.SetRegister(n, value);
            }
        }

        // Confirmed, once, when R0-R12 and SP all equal GetRegister and a
        // value written to R12 with SetRegister (then put back) shows up at
        // its place; refuted otherwise.
        private void ConfirmRegisters()
        {
            for(var n = 0; n <= 13; n++)
            {
                if((uint)Marshal.ReadInt32(State, 4 * n) != (uint)cpu.GetRegister(n).RawValue)
                {
                    registersInState = -1;
                    return;
                }
            }
            var r12 = (uint)cpu.GetRegister(12).RawValue;
            cpu.SetRegister(12, r12 ^ 0x5A5AA5A5u);
            var seen = (uint)Marshal.ReadInt32(State, 4 * 12);
            cpu.SetRegister(12, r12);
            registersInState = seen == (r12 ^ 0x5A5AA5A5u) && (uint)Marshal.ReadInt32(State, 4 * 12) == r12 ? 1 : -1;
        }

        // The CPUState before CPU_COMMON (its first field is the limit).
        public unsafe ReadOnlySpan<byte> Architectural => new ReadOnlySpan<byte>((void*)State, limitOffset);

        public MemoryAccessHook MemoryAccessHook => (MemoryAccessHook)hookField.GetValue(cpu);

        public void InvokeOnCpuThread(Action action)
        {
            invoke.Invoke(cpu, new object[] { action });
        }

        public int RestoreContext() => restoreContext();

        public void MemoryAccessEventEnabled(bool on) => memoryAccessEventEnabled(on ? 1 : 0);

        // tlib_is_on_memory_access_enabled, found by turning it on and off on a
        // CPU that isn't running (at creation): the one int that changes.
        public bool FindMemoryAccessFlag(out string why)
        {
            var size = stateSize();
            var state = State;
            var before = new byte[size];
            var after = new byte[size];
            Marshal.Copy(state, before, 0, size);
            memoryAccessEventEnabled(1);
            Marshal.Copy(state, after, 0, size);
            memoryAccessEventEnabled(0);
            var changed = Enumerable.Range(0, size).Where(i => before[i] != after[i]).ToList();
            if(changed.Count != 1 || changed[0] % 4 != 0 || after[changed[0]] != 1)
            {
                why = $"tlib's memory-access flag not found ({changed.Count} bytes changed)";
                return false;
            }
            flagOffset = changed[0];
            why = null;
            return true;
        }

        public void SetMemoryAccessFlag(bool on)
        {
            Marshal.WriteInt32(State, flagOffset, on ? 1 : 0);
        }

        // The counters, found on the CPU thread inside a block (where the
        // declaration holds the block's length): the 8-byte total equal to
        // the executed-instruction count, its three 32-bit neighbours
        // plausible, and confirmed by SyncTime, which reads and zeroes the
        // count of this chunk and takes it off the limit (tlib
        // tlib_get_executed_instructions). Null why: not yet, try later.
        public bool Calibrate(out string why)
        {
            why = null;
            var total = cpu.GetCurrentInstructionsCount();
            if(total < 100000)
            {
                return false;   // too common a value to search for yet
            }
            var size = stateSize();
            var state = State;
            var bytes = new byte[size];
            Marshal.Copy(state, bytes, 0, size);
            var found = new List<(int limit, int total)>();
            for(var t = 16; t + 8 <= size; t += 8)
            {
                if(BitConverter.ToUInt64(bytes, t) != total)
                {
                    continue;
                }
                foreach(var l in new[] { t - 16, t - 12 })
                {
                    var limit = BitConverter.ToUInt32(bytes, l);
                    var value = BitConverter.ToUInt32(bytes, l + 4);
                    var declaration = BitConverter.ToUInt32(bytes, l + 8);
                    if(declaration >= 1 && declaration <= (uint)cpu.MaximumBlockSize
                       && (ulong)limit >= (ulong)value + declaration)
                    {
                        found.Add((l, t));
                    }
                }
            }
            if(found.Count != 1)
            {
                why = $"tlib's instruction counters not found ({found.Count} candidates)";
                return false;
            }
            var (lim, tot) = found[0];
            var limitBefore = (uint)Marshal.ReadInt32(state, lim);
            var valueBefore = (uint)Marshal.ReadInt32(state, lim + 4);
            cpu.SyncTime();
            var limitAfter = (uint)Marshal.ReadInt32(state, lim);
            var valueAfter = (uint)Marshal.ReadInt32(state, lim + 4);
            if(valueAfter != 0 || limitAfter != limitBefore - valueBefore)
            {
                why = "tlib's instruction counters did not behave as expected";
                return false;
            }
            limitOffset = lim;
            totalOffset = tot;
            return true;
        }

        // Instructions this chunk can still take after the current block.
        public ulong Budget()
        {
            var state = State;
            var limit = (uint)Marshal.ReadInt32(state, limitOffset);
            var value = (uint)Marshal.ReadInt32(state, limitOffset + 4);
            var declaration = (uint)Marshal.ReadInt32(state, limitOffset + 8);
            return (ulong)limit > (ulong)value + declaration ? limit - value - declaration : 0;
        }

        // Count `instructions` as executed now; never more than Budget().
        public void Charge(ulong instructions)
        {
            var state = State;
            var value = (uint)Marshal.ReadInt32(state, limitOffset + 4);
            Marshal.WriteInt32(state, limitOffset + 4, (int)(value + (uint)instructions));
            var total = (ulong)Marshal.ReadInt64(state, totalOffset);
            Marshal.WriteInt64(state, totalOffset, (long)(total + instructions));
        }

        private T Delegate<T>(string name) where T : class
        {
            return (T)Field(name).GetValue(cpu);
        }

        private FieldInfo Field(string name)
        {
            for(var t = cpu.GetType(); t != null; t = t.BaseType)
            {
                var f = t.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if(f != null)
                {
                    return f;
                }
            }
            throw new ArgumentException($"TranslationCPU.{name} not found (Renode version?)");
        }

        private MethodInfo Method(string name)
        {
            for(var t = cpu.GetType(); t != null; t = t.BaseType)
            {
                var m = t.GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if(m != null)
                {
                    return m;
                }
            }
            throw new ArgumentException($"TranslationCPU.{name} not found (Renode version?)");
        }

        private readonly TranslationCPU cpu;
        private readonly Func<IntPtr> exportState;
        private readonly Func<int> stateSize;
        private readonly Func<int> restoreContext;
        private readonly Action<int> memoryAccessEventEnabled;
        private readonly FieldInfo hookField;
        private readonly MethodInfo invoke;
        private IntPtr state;
        private int registersInState;
        private int flagOffset = -1;
        private int limitOffset = -1;
        private int totalOffset = -1;
    }
}
