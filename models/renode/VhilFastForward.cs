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
//   until an event changes a peripheral.
//   The RAM comparison copies and compares every RAM byte the board has
//   touched (330 KB on the uDV), for every proof. An iteration whose code
//   writes no memory on any path it may have taken (StoreFreeLoop: every
//   path of exactly its instruction count from the boundary access back to
//   it, decoded from the code) leaves RAM as it was by construction, so only
//   the CPU's registers are compared: the uDV's ADC3 polls, six a
//   millisecond.
//   Self-yielding polls: an iteration may pend a PendSV itself (a FreeRTOS
//   taskYIELD: SCB ICSR = PENDSVSET, then the PendSV handler switches back to
//   the same task), as the uDV's micro-ROS transport read does on every pass
//   while no agent answers (usb_cdc_transport.c cubemx_transport_read). ICSR
//   is whitelisted for that: its reads (the NVIC's pending and active state,
//   which only an event or the loop's own PENDSVSET changes within a chunk)
//   and a write of PENDSVSET alone. Such an iteration must take exactly as
//   many PendSVs as it pends (the exception the NVIC acknowledges at each
//   entry is PendSV, any other is an interrupt as before), read the same
//   ICSR at both ends (the NVIC as it found it) and record the PendSV
//   handler's MMIO accesses in its trace; the CPU's state and RAM, the
//   handler's stack frames included, are compared as for any iteration. Iterations are then skipped by
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
//   Cycle-counter waits. A wait on DWT CYCCNT (AMS ltc6820.cpp delay_us, the
//   isoSPI wake: `while ((DWT->CYCCNT - start) < cycles) {}`), as GCC emits it:
//         L:  ldr  rT, [rB, #4]        (rB + 4 = DWT_CYCCNT, 0xE0001004)
//             subs rD, rT, rS
//             cmp  rD, rN
//             bcc  L
//   CYCCNT is Renode's DWT timer on the machine's clock (a LimitTimer at the
//   platform's 528 MHz), so the value each iteration's read returns is a
//   non-decreasing function of the time, itself the instructions executed:
//   every read before the time the count reaches start + cycles sees the loop
//   go on, whatever value it returns. A pre-opcode hook on BCC (excluded, as
//   the CMP.W one, at every BCC that is not such a loop's) checks the code
//   around it is exactly this (rS, rN and rB not written by the loop); at the
//   BCC, the iterations whose reads all come before that time
//   (BoundedIterations, as for a time-bounded poll, from the count at the time
//   synced there, at least what the last read saw) are charged, never past
//   the chunk's end less two iterations. rT, rD and the flags they leave are dead: the next
//   iteration's read and SUBS write them before anything reads them, and no
//   exception can come between (one comes only at a chunk's end).
//
//   Tick waits. HAL_Delay's `while ((HAL_GetTick() - tickstart) < wait) {}`
//   as GCC emits it at -Og/-O2 (the uDV's BMI088 and I2C bring-up, 140 ms of
//   its boot at full speed), decoded at its BCC (TickWait): BL to a
//   HAL_GetTick that loads the tick from RAM, SUBS, CMP, BCC. The loop writes
//   no memory and only registers it recomputes from ones it never writes
//   and the tick, which only an interrupt changes. A pre-opcode hook on the
//   BCC (the CYCCNT waits' hook) sees two arrivals one iteration apart with
//   those registers equal and the branch taken, and charges the iterations
//   up to the chunk's end less two. Never on a page a lazily delivering
//   peripheral owns (VhilLazy.IoPages).
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
// register that cannot say until when it stands (SysTick VAL, a timer's CNT,
// CYCCNT in any other loop: not in the whitelist) or a GPIO input a device model drives on
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
using Antmicro.Renode.Peripherals.IRQControllers;
using Antmicro.Renode.Peripherals.Memory;
using Antmicro.Renode.Peripherals.Timers;
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
            // A PendSV the loop pends itself (a FreeRTOS taskYIELD in the
            // loop: SelfYield) is one, as long as the iteration takes exactly
            // as many as it pends.
            nvic = machine.GetPeripheralsOfType<NVIC>().FirstOrDefault();
            cpu.AddHookAtInterruptBegin(_ => OnInterruptBegin());
            cpu.AddHookAtWfiStateChange(_ => interrupts++);
            // CMP.W Rn, #const (T2): 11110 i 0 1101 1 Rn; tlib matches a Thumb
            // opcode's first halfword (SUBS.W Rd, Rn, #const shares it).
            compareHook = OnCompare;
            cpu.AddPreOpcodeExecutionHook(0xFBF0, 0xF1B0, compareHook);
            // BCC (A7.7.12 T1, cond 0011): the end of a CYCCNT wait.
            branchHook = OnBranch;
            cpu.AddPreOpcodeExecutionHook(0xFF00, 0xD300, branchHook);
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

        public ulong SkippedInstructions => counterInstructions + pollInstructions + cycleInstructions;

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
                + $"; self-yielding polls: {yieldSkips} skips, {yieldInstructions} instructions"
                + $" ({storeFreeProofs} proven store-free)"
                + $"; tick waits: {tickSkips} skips, {tickInstructions} instructions"
                + $"; CYCCNT waits: {cycleSkips} skips, {cycleInstructions} instructions"
                + $" ({branchCalls} BCC hooks, {Ms(branchTicks)} ms, {excludedBranches.Count} BCC excluded)"
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
            yieldingAttempt = false;
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
            if(!armed || (operation != MemoryOperation.MemoryIORead && operation != MemoryOperation.MemoryIOWrite))
            {
                return;
            }
            var ipsr = (uint)cpu.GetRegister(XpsrIndex).RawValue & 0x1FF;
            // A handler's accesses are not the loop's, except those of a PendSV
            // the loop pended itself (SelfYield), which are part of its
            // iteration: they join its trace, never as its boundary.
            var inSelfYield = ipsr == PendSvException && selfYieldActive;
            if(ipsr != 0 && !inSelfYield)
            {
                return;
            }
            // Timed one call in AccessTimingStride (two clock reads per access
            // were 2 % of the uDV's CPU thread): Stats' figure is an estimate.
            var timed = (++accessCalls & (AccessTimingStride - 1)) == 0UL;
            var t0 = timed ? System.Diagnostics.Stopwatch.GetTimestamp() : 0;
            OnPeripheralAccess(operation == MemoryOperation.MemoryIOWrite, pc, physicalAddress, width, value, inSelfYield);
            if(timed)
            {
                accessTicks += (System.Diagnostics.Stopwatch.GetTimestamp() - t0) * AccessTimingStride;
            }
        }

        private const int AccessTimingStride = 32;

        // `pc`: the access's instruction, as tlib reports it (CPU_PC, the PC it
        // stores before every instruction: Renode's default,
        // SyncPCEveryInstructionDisabled false).
        private void OnPeripheralAccess(bool write, ulong pc, ulong address, uint width, ulong value, bool inSelfYield)
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
            var access = new Access(pc, address, width, write, value);
            var atBoundary = state == PollState.Seek ? !inSelfYield : !inSelfYield && access.SamePlace(boundary);
            if(!atBoundary)
            {
                if(state == PollState.Seek)
                {
                    return;
                }
                // Inside an iteration: only the access itself is recorded.
                trace.Add(access);
                if(trace.Count > MaxTrace)
                {
                    Refuse("iteration too long");
                }
                return;
            }
            // The exact position, at an iteration's boundary only: the
            // executed count of the blocks before this one (tlib's total, read
            // from its CPUState) plus this instruction's index in its block.
            var index = tlib.RestoreContext();
            if(index < 0)
            {
                Refuse("no block for this access");
                return;
            }
            var position = tlib.Total() + (ulong)index;
            if(state == PollState.Seek)
            {
                Begin(PollState.First, access, position);
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
            boundaryPendSvs = pendSvs;
            // ICSR as the boundary found it, for an iteration that yields
            // (OnBoundary); read only once one has (reading it computes the
            // NVIC's pending vector, a cost every other poll would pay).
            boundaryIcsr = yieldingAttempt ? Icsr() : NoIcsr;
            state = next;
        }

        // An exception entry: a PendSV that a polling iteration pended itself
        // (its PENDSVSET write, SelfYield) counts apart; any other is not a
        // repetition. The NVIC acknowledges the exception it finds pending,
        // which is the one FindPendingInterrupt names here (the same call,
        // on the same state: NVIC.AcknowledgeIRQ).
        private void OnInterruptBegin()
        {
            if(armed && (state == PollState.First || state == PollState.Second) && nvic != null
               && nvic.FindPendingInterrupt() == PendSvException)
            {
                pendSvs++;
                selfYieldActive = true;
                return;
            }
            selfYieldActive = false;
            interrupts++;
        }

        // ICSR as the firmware would read it (PendSV pending, the vector
        // pending, the one active): the same at both ends of an iteration
        // that takes every PendSV it pends leaves the NVIC as it found it,
        // since only an event or the loop's own PENDSVSET changes it within
        // a chunk. Read from here, it has no effect (NVIC ReadDoubleWord:
        // FindPendingInterrupt recomputes the CPU's IRQ line from the same
        // state).
        private uint Icsr()
        {
            return nvic == null ? 0xFFFFFFFFu : nvic.ReadDoubleWord((long)IcsrOffset, false);
        }

        private int SelfYields()
        {
            var n = 0;
            foreach(var access in trace)
            {
                if(access.Write && kinds.TryGetValue(access.Address, out var kind) && kind == RegisterKind.SelfYield)
                {
                    n++;
                }
            }
            return n;
        }

        // The same access again: one iteration done.
        private void OnBoundary(Access access, ulong position)
        {
            var k = position - boundaryPosition;
            var yields = SelfYields();
            if(yields > 0)
            {
                yieldingAttempt = true;   // from the next boundary on, ICSR is read there too
            }
            selfYieldActive = false;
            if(interrupts != boundaryInterrupts || pendSvs - boundaryPendSvs != (ulong)yields
               || (yields > 0 && (nvic == null || boundaryIcsr == NoIcsr || boundaryIcsr != Icsr()))
               || k == 0 || !access.Equals(boundary))
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
                // An iteration whose every possible path writes no memory
                // (StoreFreeLoop) leaves RAM as it found it by construction:
                // only the CPU's registers need comparing. Otherwise, every
                // RAM byte as well.
                storeFree = yields == 0 && storeFreeLoops.IsStoreFree(access.Pc, k);
                snapshot.Take(tlib, storeFree ? null : memories);
                Begin(PollState.Second, access, position);
                return;
            }
            if(!snapshot.Matches(tlib, storeFree ? null : memories, out var where))
            {
                if(++retries > MaxRetries)
                {
                    Refuse($"an iteration of {k} instructions at 0x{access.Pc:X8} changes {where}");
                    return;
                }
                snapshot.Take(tlib, storeFree ? null : memories);
                Begin(PollState.Second, access, position);
                return;
            }
            if(storeFree)
            {
                storeFreeProofs++;
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
                if(yields > 0)
                {
                    yieldSkips++;
                    yieldInstructions += n * k;
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

        // -- cycle-counter waits ---------------------------------------------------

        // At a BCC, before it executes (the pre-opcode hook): if it closes a
        // CYCCNT wait (CycleCounterWait), charge the iterations whose reads
        // all come before the count reaches the end of the wait.
        private void OnBranch(ulong pc, ulong opcode)
        {
            var t0 = System.Diagnostics.Stopwatch.GetTimestamp();
            branchCalls++;
            if(enabled && counters && disabledWhy == null)
            {
                SkipCycleCounterWait(pc);
            }
            branchTicks += System.Diagnostics.Stopwatch.GetTimestamp() - t0;
        }

        private void SkipCycleCounterWait(ulong bcc)
        {
            if(!waits.TryGetValue(bcc, out var wait))
            {
                wait = CycleCounterWait.Decode(machine.SystemBus, cpu, bcc - 6);
                waits[bcc] = wait;
                if(wait == null && !tickWaits.ContainsKey(bcc))
                {
                    tickWaits[bcc] = TickWait.Decode(machine.SystemBus, cpu, bcc);
                }
                if(wait == null && tickWaits[bcc] == null && excludeCompare != null && excludeCompare(branchHook, bcc))
                {
                    excludedBranches.Add(bcc);
                    pendingInvalidation = true;
                }
            }
            if(wait == null)
            {
                if(tickWaits.TryGetValue(bcc, out var tick) && tick != null)
                {
                    SkipTickWait(tick);
                }
                return;
            }
            if(!wait.StillThere(machine.SystemBus, cpu) || !Calibrated())
            {
                return;
            }
            var timer = CycleCounter();
            if(timer == null || !timer.Enabled)
            {
                return;
            }
            // The time synced to the instructions so far (at or after the last
            // read's, which DWT synced too), and the count at that time: at
            // least what that read returned.
            cpu.SyncTime();
            var now = machine.ElapsedVirtualTime.TimeElapsed.Ticks;
            var start = tlib.CoreRegister(wait.Rs);
            var cycles = tlib.CoreRegister(wait.Rn);
            var counted = unchecked((uint)timer.Value - start);
            if(counted >= cycles)
            {
                return;
            }
            // The count goes up by at most ticks * f / ticks-per-second + 1
            // over `ticks`: every read before `until` sees fewer than
            // `cycles` counted since `start`, and the loop go on.
            var remaining = (ulong)(cycles - counted);
            var frequency = timer.Frequency / timer.Divider;
            var until = now + (ulong)((UInt128)(remaining - 1) * TimeInterval.TicksPerSecond / frequency);
            var k = CycleCounterWait.Instructions;
            var budget = tlib.Budget();
            var n = budget > 2 * k ? (budget - 2 * k) / k : 0;
            // The BCC is at most the block's fourth instruction (a block
            // starts at the loop's LDR, the branch target).
            n = Math.Min(n, BoundedIterations(until, k, cpu.GetCurrentInstructionsCount() + k));
            if(n < 2)
            {
                return;
            }
            tlib.Charge(n * k);
            cycleSkips++;
            cycleInstructions += n * k;
            if(Trace)
            {
                cpu.Log(LogLevel.Info, "fast-forward: {0} iterations of a CYCCNT wait at 0x{1:X8}", n, bcc - 6);
            }
        }

        // At the BCC of a tick wait (TickWait), before it executes. The loop
        // writes no memory and only LR, the literal's register, R0, Rd and the
        // flags, each a function of registers it never writes and of the tick
        // word in RAM, which nothing but an interrupt (or a DMA's event) can
        // change within a chunk. Two arrivals here one iteration apart (seven
        // instructions, no exception, the same chunk) with those registers
        // equal, the branch taken, are a fixed point: every iteration up to
        // the chunk's end repeats it. Charge them, leaving two.
        private void SkipTickWait(TickWait tick)
        {
            if(!tick.StillThere(machine.SystemBus, cpu) || !Calibrated())
            {
                lastTick = null;
                return;
            }
            var xpsr = (uint)cpu.GetRegister(XpsrIndex).RawValue;
            if((xpsr & CarryFlag) != 0)
            {
                lastTick = null;   // BCC not taken: the wait ends here
                return;
            }
            var budget = tlib.Budget();
            var regs = new TickRegisters(tlib.CoreRegister(0), tlib.CoreRegister(tick.Ra), tlib.CoreRegister(tick.Rd),
                                         tlib.CoreRegister(14), xpsr & 0xF8000000u);
            var k = TickWait.Instructions;
            var proven = lastTick == tick && lastTickInterrupts == interrupts
                && lastTickBudget == budget + k && lastTickRegisters.Equals(regs);
            lastTick = tick;
            lastTickInterrupts = interrupts;
            lastTickBudget = budget;
            lastTickRegisters = regs;
            if(!proven || OnLazyIoPage(tick.TickAddress))
            {
                return;
            }
            var n = budget > 2 * k ? (budget - 2 * k) / k : 0;
            if(n < 2)
            {
                return;
            }
            tlib.Charge(n * k);
            lastTickBudget = budget - n * k;   // the next arrival is one iteration on
            tickSkips++;
            tickInstructions += n * k;
            if(Trace)
            {
                cpu.Log(LogLevel.Info, "fast-forward: {0} iterations of a tick wait at 0x{1:X8}", n, tick.Bcc);
            }
        }

        // A word a lazily delivering peripheral may still write (VhilLazy
        // IoPages: tlib sends those pages through the bus, so a load there is
        // an MMIO access that catches it up first).
        private bool OnLazyIoPage(ulong address)
        {
            var lazy = lazyState ?? (lazyState = VhilLazy.For(machine));
            return lazy.IoPages.Count > 0 && lazy.IoPages.Contains(address & ~(VhilLazy.PageBytes - 1));
        }

        // DWT's cycle counter (Renode Cores/Arm-M/DWT.cs: a private LimitTimer).
        private LimitTimer CycleCounter()
        {
            if(cycleCounter == null)
            {
                var dwt = machine.SystemBus.WhatIsAt(DwtCyccnt, cpu)?.Peripheral;
                cycleCounter = dwt?.GetType().GetField("cycleCounter", BindingFlags.Instance | BindingFlags.NonPublic)
                    ?.GetValue(dwt) as LimitTimer;
            }
            return cycleCounter;
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
            // The model's answer first, which needs no call into tlib: most
            // reads come while nothing is under way (the uDV configures,
            // starts and stops ADC3 around each conversion, ~25 reads of a
            // stable register each), and those return here.
            if(!stable(offset, out var until) || until == ulong.MaxValue)
            {
                return;
            }
            var now = machine.ElapsedVirtualTime.TimeElapsed.Ticks;
            if(until < now + MinCueTicks)
            {
                return;   // no wait worth skipping ahead
            }
            if(!machine.SystemBus.TryGetCurrentCPU(out var current) || current != cpu
               || ((uint)cpu.GetRegister(XpsrIndex).RawValue & 0x1FF) != 0)
            {
                return;
            }
            var pc = cpu.PC.RawValue;
            if(cueRefused.Contains(pc))
            {
                return;   // refused here this quantum
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
            case RegisterKind.SelfYield:
                // ICSR: a read returns the NVIC's pending and active state,
                // which changes at an event or by the loop's own PENDSVSET; a
                // write of PENDSVSET alone pends a PendSV, which the iteration
                // must take before it ends (OnBoundary: as many PendSV entries
                // as writes, nothing pending at either end). FreeRTOS's
                // taskYIELD (portYIELD) and vPortEnterCritical's check.
                if(!write || (value & 0xFFFFFFFF) == PendSvSet)
                {
                    return true;
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
            if(at.Peripheral == nvic && offset == IcsrOffset)
            {
                return RegisterKind.SelfYield;
            }
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
            waits.Clear();
            tickWaits.Clear();
            lastTick = null;
            storeFreeLoopsCache?.Clear();
            if(clearExclusions != null && excludedCompares.Count + excludedBranches.Count > 0)
            {
                // Every CMP.W and BCC is looked at again after a reset, as the
                // remembered decodes are gone: the hook back everywhere, in
                // the blocks holding an excluded one invalidated, on the CPU's
                // thread before it next executes (not ClearTranslationCache,
                // which pauses the machine: from the CPU's thread it waits on
                // itself).
                var excluded = excludedCompares.Concat(excludedBranches).ToArray();
                excludedCompares.Clear();
                excludedBranches.Clear();
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
        private const int PendSvException = 14;        // ARMv7-M B1.5.2 exception number
        private const ulong IcsrOffset = 0xD04;        // SCB ICSR, 0xE000ED04 (core_cm7.h), at the NVIC's 0xE000E000
        private const ulong PendSvSet = 1UL << 28;     // ICSR.PENDSVSET (SCB_ICSR_PENDSVSET_Msk)
        private const ulong DwtCyccnt = 0xE0001004;    // DWT_BASE 0xE0001000 + CYCCNT 0x004 (core_cm7.h)
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
        private readonly Action<ulong, ulong> branchHook;
        private readonly List<ulong> excludedBranches = new List<ulong>();
        private ulong branchCalls;
        private long branchTicks;
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
        private ulong yieldSkips;
        private ulong storeFreeProofs;
        private bool storeFree;
        private ulong yieldInstructions;
        private readonly List<Access> trace = new List<Access>();
        private readonly List<Access> firstTrace = new List<Access>();
        private readonly Snapshot snapshot = new Snapshot();
        private StoreFreeLoop storeFreeLoopsCache;
        private StoreFreeLoop storeFreeLoops => storeFreeLoopsCache ?? (storeFreeLoopsCache = new StoreFreeLoop(machine.SystemBus, cpu));
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
        private ulong boundaryPendSvs;
        private uint boundaryIcsr;
        private bool yieldingAttempt;                 // this attempt has seen an iteration that yields
        private const uint NoIcsr = 0xFFFFFFFFu;      // not read (ICSR's bit 10 is reserved, never 1)
        private ulong pendSvs;
        private bool selfYieldActive;
        private readonly NVIC nvic;
        private ulong firstK;
        private int accessesThisAttempt;
        private ulong interrupts;
        private int armedQuanta;
        private volatile bool pendingRefusal;

        private readonly Dictionary<ulong, CycleCounterWait> waits = new Dictionary<ulong, CycleCounterWait>();
        private readonly Dictionary<ulong, TickWait> tickWaits = new Dictionary<ulong, TickWait>();
        private TickWait lastTick;
        private VhilLazy lazyState;
        private ulong lastTickInterrupts;
        private ulong lastTickBudget;
        private TickRegisters lastTickRegisters;
        private ulong tickSkips;
        private ulong tickInstructions;
        private const uint CarryFlag = 1u << 29;     // xPSR.C
        private LimitTimer cycleCounter;
        private ulong cycleSkips;
        private ulong cycleInstructions;
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

        private enum RegisterKind { Other, EventDrivenRead, WatchdogKey, TimeBounded, SelfYield }

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
    // A wait on DWT CYCCNT, as GCC emits `while ((DWT->CYCCNT - start) <
    // cycles) {}` (AMS ltc6820.cpp delay_us), decoded at its load:
    //     L: LDR rT, [rB, #4]   (A7.7.42 T1)
    //        SUBS rD, rT, rS    (A7.7.174 T1)
    //        CMP rD, rN         (A7.7.28 T1)
    //        BCC L              (A7.7.12 T1, cond 0011)
    // with rB, rS and rN none of rT and rD, so the loop never writes them.
    public class CycleCounterWait
    {
        public const ulong Instructions = 4;

        public static CycleCounterWait Decode(IBusController bus, ICPU cpu, ulong pc)
        {
            var code = Read(bus, cpu, pc);
            var ldr = code[0];
            var subs = code[1];
            var cmp = code[2];
            var bcc = code[3];
            if((ldr & 0xF800) != 0x6800 || ((ldr >> 6) & 0x1F) != 1)
            {
                return null;
            }
            var rt = ldr & 7;
            var rb = (ldr >> 3) & 7;
            if((subs & 0xFE00) != 0x1A00 || ((subs >> 3) & 7) != rt)
            {
                return null;
            }
            var rd = subs & 7;
            var rs = (subs >> 6) & 7;
            if((cmp & 0xFFC0) != 0x4280 || (cmp & 7) != rd)
            {
                return null;
            }
            var rn = (cmp >> 3) & 7;
            if((bcc & 0xFF00) != 0xD300 || (ulong)((long)pc + 6 + 4 + (sbyte)(bcc & 0xFF) * 2) != pc)
            {
                return null;
            }
            foreach(var r in new[] { rb, rs, rn })
            {
                if(r == rt || r == rd)
                {
                    return null;
                }
            }
            return new CycleCounterWait { Pc = pc, Rs = rs, Rn = rn, code = code };
        }

        // The code is the same as when decoded (flash, but checked as RAM
        // would need).
        public bool StillThere(IBusController bus, ICPU cpu)
        {
            var now = Read(bus, cpu, Pc);
            for(var i = 0; i < now.Length; i++)
            {
                if(now[i] != code[i])
                {
                    return false;
                }
            }
            return true;
        }

        private static ushort[] Read(IBusController bus, ICPU cpu, ulong pc)
        {
            var code = new ushort[4];
            for(var i = 0; i < code.Length; i++)
            {
                code[i] = (ushort)bus.ReadWord(pc + (ulong)(2 * i), cpu);
            }
            return code;
        }

        public ulong Pc { get; private set; }
        public int Rs { get; private set; }
        public int Rn { get; private set; }

        private ushort[] code;
    }

    // Whether every path one polling iteration may have taken writes no
    // memory, from its code: the iteration starts at the instruction making
    // its boundary access (pc) and runs k instructions, with no exception
    // (the detector refuses any other), until that instruction again. Every
    // path of exactly k instructions from pc back to pc through the code,
    // following BL into the callee and BX LR back, is enumerated (pruned at
    // k, and wherever it reaches pc early: that would have been a boundary
    // access); the iteration took one of them. If none of them has a store
    // (STR*, STM, PUSH, STREX, STRD, a coprocessor store) and none reaches an
    // instruction whose successor is not known from the code (an indirect
    // branch other than BX LR back from a followed BL, a load into PC, TBB,
    // an IT block, SVC, BKPT, UDF, BLX), the iteration wrote no memory: its
    // loads read RAM or, through the bus, peripherals (which the detector
    // sees as accesses and checks), and RAM changes within a chunk only by
    // the CPU. Thumb-2 encodings from the ARMv7-M ARM, A5.2-A5.3.
    // Small iterations only (MaxSteps); a larger one is compared by
    // snapshot. Results are kept per (pc, k) with the code halfwords they
    // read, which are checked again on every use.
    public class StoreFreeLoop
    {
        public StoreFreeLoop(IBusController bus, ICPU cpu)
        {
            this.bus = bus;
            this.cpu = cpu;
        }

        public void Clear() => known.Clear();

        public bool IsStoreFree(ulong pc, ulong k)
        {
            if(k == 0 || k > MaxSteps)
            {
                return false;
            }
            var key = (pc, k);
            if(known.TryGetValue(key, out var verdict))
            {
                if(StillThere(verdict.Code))
                {
                    return verdict.StoreFree;
                }
                known.Remove(key);
            }
            var code = new Dictionary<ulong, ushort>();
            var free = Analyse(pc, (int)k, code);
            known[key] = new Verdict { StoreFree = free, Code = code };
            return free;
        }

        private bool Analyse(ulong boundary, int k, Dictionary<ulong, ushort> code)
        {
            var paths = 0;
            var stack = new Stack<(ulong pc, int steps, bool store, ImmutableReturns returns)>();
            stack.Push((boundary, 0, false, null));
            while(stack.Count > 0)
            {
                var (pc, steps, store, returns) = stack.Pop();
                if(steps == k)
                {
                    if(pc == boundary)
                    {
                        if(store)
                        {
                            return false;
                        }
                        paths++;
                    }
                    continue;
                }
                if(steps > 0 && pc == boundary)
                {
                    continue;   // a boundary access before k: not this iteration's path
                }
                if(++visited > MaxVisits)
                {
                    visited = 0;
                    return false;
                }
                if(!Read(pc, code, out var h1))
                {
                    return false;
                }
                var wide = (h1 >> 11) >= 0x1D;
                ushort h2 = 0;
                if(wide && !Read(pc + 2, code, out h2))
                {
                    return false;
                }
                var next = pc + (wide ? 4UL : 2UL);
                var kind = wide ? Classify32(h1, h2, pc, out var target) : Classify16(h1, pc, out target);
                switch(kind)
                {
                case Kind.Unknown:
                    return false;
                case Kind.Plain:
                    stack.Push((next, steps + 1, store, returns));
                    break;
                case Kind.Store:
                    stack.Push((next, steps + 1, true, returns));
                    break;
                case Kind.Jump:
                    stack.Push((target, steps + 1, store, returns));
                    break;
                case Kind.Conditional:
                    stack.Push((next, steps + 1, store, returns));
                    stack.Push((target, steps + 1, store, returns));
                    break;
                case Kind.Call:
                    stack.Push((target, steps + 1, store, new ImmutableReturns(next, returns)));
                    break;
                case Kind.Return:
                    if(returns == null)
                    {
                        return false;   // a return out of the code we started in
                    }
                    stack.Push((returns.Address, steps + 1, store, returns.Rest));
                    break;
                }
            }
            visited = 0;
            return paths > 0;
        }

        private enum Kind { Plain, Store, Jump, Conditional, Call, Return, Unknown }

        private static Kind Classify16(ushort h, ulong pc, out ulong target)
        {
            target = 0;
            if((h & 0xF000) == 0x5000)
            {
                return ((h >> 9) & 7) <= 2 ? Kind.Store : Kind.Plain;          // STR, STRH, STRB (register); loads
            }
            if((h & 0xF800) == 0x6000 || (h & 0xF800) == 0x7000 || (h & 0xF800) == 0x8000 || (h & 0xF800) == 0x9000
               || (h & 0xFE00) == 0xB400 || (h & 0xF800) == 0xC000)
            {
                return Kind.Store;                                             // STR/STRB/STRH imm, STR SP, PUSH, STM
            }
            if((h & 0xF000) == 0xD000)
            {
                var cond = (h >> 8) & 0xF;
                if(cond >= 0xE)
                {
                    return Kind.Unknown;                                       // UDF, SVC
                }
                target = (ulong)((long)pc + 4 + (sbyte)(h & 0xFF) * 2);
                return Kind.Conditional;                                       // B<c> T1
            }
            if((h & 0xF800) == 0xE000)
            {
                target = (ulong)((long)pc + 4 + (((h & 0x7FF) << 21) >> 20));  // B T2
                return Kind.Jump;
            }
            if((h & 0xF500) == 0xB100)
            {
                target = pc + 4 + (ulong)((((h >> 9) & 1) << 6) | (((h >> 3) & 0x1F) << 1));   // CBZ, CBNZ
                return Kind.Conditional;
            }
            if((h & 0xFF87) == 0x4700)
            {
                return ((h >> 3) & 0xF) == 14 ? Kind.Return : Kind.Unknown;   // BX LR; BX other
            }
            if((h & 0xFF80) == 0x4780)
            {
                return Kind.Unknown;                                           // BLX register
            }
            if(((h & 0xFF00) == 0x4400 || (h & 0xFF00) == 0x4600) && ((h & 7) | ((h >> 4) & 8)) == 15)
            {
                return Kind.Unknown;                                           // ADD/MOV to PC
            }
            if((h & 0xFF00) == 0xBD00)
            {
                return Kind.Unknown;                                           // POP {..., PC}
            }
            if((h & 0xFF00) == 0xBF00 && (h & 0xF) != 0)
            {
                return Kind.Unknown;                                           // IT
            }
            if((h & 0xFF00) == 0xBE00)
            {
                return Kind.Unknown;                                           // BKPT
            }
            return Kind.Plain;
        }

        private static Kind Classify32(ushort h1, ushort h2, ulong pc, out ulong target)
        {
            target = 0;
            if((h1 & 0xFE40) == 0xE800)
            {
                if((h1 & 0x10) == 0)
                {
                    return Kind.Store;                                         // STM, STMDB
                }
                return (h2 & 0x8000) != 0 ? Kind.Unknown : Kind.Plain;        // LDM with PC; LDM
            }
            if((h1 & 0xFE40) == 0xE840)
            {
                if((h1 & 0x10) == 0)
                {
                    return Kind.Store;                                         // STREX*, STRD
                }
                if((h1 & 0xFFF0) == 0xE8D0 && (h2 & 0xFFE0) == 0xF000)
                {
                    return Kind.Unknown;                                       // TBB, TBH
                }
                return ((h2 >> 12) & 0xF) == 15 || ((h2 >> 8) & 0xF) == 15 ? Kind.Unknown : Kind.Plain;   // LDRD, LDREX* (PC as a target, or LDREX's 1111: refused)
            }
            if((h1 & 0xFE00) == 0xF800)
            {
                if((h1 & 0xFF10) == 0xF800)
                {
                    return Kind.Store;                                         // STR, STRB, STRH (single)
                }
                return ((h2 >> 12) & 0xF) == 15 ? Kind.Unknown : Kind.Plain;  // a load into PC or a preload hint; loads
            }
            if((h1 & 0xEE00) == 0xEC00)
            {
                return Kind.Unknown;                                           // coprocessor loads, stores, transfers
            }
            if((h1 & 0xF800) == 0xF000 && (h2 & 0x8000) != 0)
            {
                var s = (h1 >> 10) & 1;
                var j1 = (h2 >> 13) & 1;
                var j2 = (h2 >> 11) & 1;
                if((h2 & 0xD000) == 0xD000 || (h2 & 0xD000) == 0x9000)
                {
                    var i1 = 1 ^ (j1 ^ s);
                    var i2 = 1 ^ (j2 ^ s);
                    var offset = (s << 24) | (i1 << 23) | (i2 << 22) | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1);
                    target = (ulong)((long)pc + 4 + ((offset << 7) >> 7));
                    return (h2 & 0xD000) == 0xD000 ? Kind.Call : Kind.Jump;   // BL; B.W T4
                }
                if((h2 & 0xD000) == 0xC000)
                {
                    return Kind.Unknown;                                       // BLX immediate
                }
                if(((h1 >> 7) & 7) == 7)
                {
                    // Misc control (MSR, MRS, hints, barriers); UDF.W.
                    return (h1 & 0xFFF0) == 0xF7F0 && (h2 & 0xF000) == 0xA000 ? Kind.Unknown : Kind.Plain;
                }
                var offset3 = (s << 20) | (j2 << 19) | (j1 << 18) | ((h1 & 0x3F) << 12) | ((h2 & 0x7FF) << 1);
                target = (ulong)((long)pc + 4 + ((offset3 << 11) >> 11));
                return Kind.Conditional;                                       // B<c>.W T3
            }
            return Kind.Plain;                                                 // data processing
        }

        private bool Read(ulong address, Dictionary<ulong, ushort> code, out ushort value)
        {
            if(code.TryGetValue(address, out value))
            {
                return true;
            }
            if(!TryReadCode(address, out value))
            {
                return false;   // code outside memory: nothing to decode
            }
            code[address] = value;
            return true;
        }

        // Checked on every use, so read straight from the memory's segment
        // (as MappedMemory.ReadWord does, without the bus's look-up).
        private bool StillThere(Dictionary<ulong, ushort> code)
        {
            foreach(var pair in code)
            {
                if(!TryReadCode(pair.Key, out var value) || value != pair.Value)
                {
                    return false;
                }
            }
            return true;
        }

        private unsafe bool TryReadCode(ulong address, out ushort value)
        {
            value = 0;
            if(codeMemory == null || address < codeStart || address + 2 > codeEnd)
            {
                var at = bus.WhatIsAt(address, cpu);
                if(!(at?.Peripheral is MappedMemory memory))
                {
                    return false;
                }
                codeMemory = memory;
                codeStart = at.RegistrationPoint.Range.StartAddress;
                codeEnd = at.RegistrationPoint.Range.EndAddress + 1;
            }
            var offset = (long)(address - codeStart);
            var segment = codeMemory.GetSegment((int)(offset / codeMemory.SegmentSize));
            var local = offset % codeMemory.SegmentSize;
            if(segment == IntPtr.Zero || local > codeMemory.SegmentSize - 2)
            {
                value = codeMemory.ReadWord(offset);
                return true;
            }
            value = *(ushort*)((byte*)segment + local);
            return true;
        }

        private MappedMemory codeMemory;
        private ulong codeStart;
        private ulong codeEnd;

        private class ImmutableReturns
        {
            public ImmutableReturns(ulong address, ImmutableReturns rest)
            {
                Address = address;
                Rest = rest;
            }

            public readonly ulong Address;
            public readonly ImmutableReturns Rest;
        }

        private struct Verdict
        {
            public bool StoreFree;
            public Dictionary<ulong, ushort> Code;
        }

        private const ulong MaxSteps = 64;
        private const int MaxVisits = 20000;

        private readonly IBusController bus;
        private readonly ICPU cpu;
        private readonly Dictionary<(ulong, ulong), Verdict> known = new Dictionary<(ulong, ulong), Verdict>();
        private int visited;
    }

    // HAL_Delay's wait for the HAL tick, as GCC emits it at -Og/-O2 (uDV,
    // AMS stm32h7xx_hal.c HAL_Delay: `while ((HAL_GetTick() - tickstart) <
    // wait) {}`), decoded at its BCC:
    //     L:  BL   G                (A7.7.18 T1)
    //         SUBS rD, r0, rS       (A7.7.174 T1)
    //         CMP  rD, rN           (A7.7.28 T2)
    //         BCC  L                (A7.7.12 T1, cond 0011)
    //     G:  LDR  rA, [PC, #imm8]  (A7.7.44 T1: the tick's address)
    //         LDR  r0, [rA, #imm5]  (A7.7.42 T1: the tick, in RAM)
    //         BX   LR
    // with rS and rN none of r0, rD, rA: the loop never writes them.
    public class TickWait
    {
        public const ulong Instructions = 7;

        public static TickWait Decode(IBusController bus, ICPU cpu, ulong bcc)
        {
            var l = bcc - 8;
            var code = Read(bus, cpu, l, 5);
            var h1 = code[0];
            var h2 = code[1];
            var subs = code[2];
            var cmp = code[3];
            var b = code[4];
            if((b & 0xFF00) != 0xD300 || (ulong)((long)bcc + 4 + (sbyte)(b & 0xFF) * 2) != l)
            {
                return null;
            }
            if((h1 & 0xF800) != 0xF000 || (h2 & 0xD000) != 0xD000)
            {
                return null;
            }
            var sign = (h1 >> 10) & 1;
            var i1 = 1 ^ (((h2 >> 13) & 1) ^ sign);
            var i2 = 1 ^ (((h2 >> 11) & 1) ^ sign);
            var offset = (sign << 24) | (i1 << 23) | (i2 << 22) | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1);
            var g = (ulong)((long)l + 4 + ((offset << 7) >> 7));   // sign-extend 25 bits
            if((subs & 0xFE00) != 0x1A00 || ((subs >> 3) & 7) != 0)
            {
                return null;
            }
            var rd = subs & 7;
            var rs = (subs >> 6) & 7;
            if((cmp & 0xFFC0) != 0x4280 || (cmp & 7) != rd)
            {
                return null;
            }
            var rn = (cmp >> 3) & 7;
            var callee = Read(bus, cpu, g, 3);
            if((callee[0] & 0xF800) != 0x4800 || (callee[1] & 0xF800) != 0x6800 || callee[2] != 0x4770)
            {
                return null;
            }
            var ra = (callee[0] >> 8) & 7;
            var literal = ((g + 4) & ~3UL) + (ulong)(callee[0] & 0xFF) * 4;
            if((callee[1] & 7) != 0 || ((callee[1] >> 3) & 7) != ra)
            {
                return null;
            }
            foreach(var r in new[] { rs, rn })
            {
                if(r == 0 || r == rd || r == ra)
                {
                    return null;
                }
            }
            var tick = (ulong)bus.ReadDoubleWord(literal, cpu) + (ulong)((callee[1] >> 6) & 0x1F) * 4;
            // The tick a plain RAM word (not a peripheral, not the flash).
            if(!(bus.WhatIsAt(tick, cpu)?.Peripheral is MappedMemory) || (tick >= 0x08000000 && tick < 0x20000000))
            {
                return null;
            }
            return new TickWait { Bcc = bcc, Rd = rd, Ra = ra, TickAddress = tick, g = g, literal = literal,
                                  code = code, calleeCode = callee, literalValue = bus.ReadDoubleWord(literal, cpu) };
        }

        // The code and the literal are as decoded.
        public bool StillThere(IBusController bus, ICPU cpu)
        {
            return Same(Read(bus, cpu, Bcc - 8, 5), code) && Same(Read(bus, cpu, g, 3), calleeCode)
                && bus.ReadDoubleWord(literal, cpu) == literalValue;
        }

        private static bool Same(ushort[] a, ushort[] b)
        {
            for(var i = 0; i < a.Length; i++)
            {
                if(a[i] != b[i])
                {
                    return false;
                }
            }
            return true;
        }

        private static ushort[] Read(IBusController bus, ICPU cpu, ulong pc, int n)
        {
            var code = new ushort[n];
            for(var i = 0; i < n; i++)
            {
                code[i] = (ushort)bus.ReadWord(pc + (ulong)(2 * i), cpu);
            }
            return code;
        }

        public ulong Bcc { get; private set; }
        public int Rd { get; private set; }
        public int Ra { get; private set; }
        public ulong TickAddress { get; private set; }

        private ulong g;
        private ulong literal;
        private uint literalValue;
        private ushort[] code;
        private ushort[] calleeCode;
    }

    // The registers a tick wait's iteration writes, at its BCC.
    public struct TickRegisters : IEquatable<TickRegisters>
    {
        public TickRegisters(uint r0, uint ra, uint rd, uint lr, uint flags)
        {
            R0 = r0; Ra = ra; Rd = rd; Lr = lr; Flags = flags;
        }

        public bool Equals(TickRegisters o) => R0 == o.R0 && Ra == o.Ra && Rd == o.Rd && Lr == o.Lr && Flags == o.Flags;

        public readonly uint R0, Ra, Rd, Lr, Flags;
    }

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
            if(memories == null)
            {
                return;   // the CPU's registers only (a store-free iteration)
            }
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
            if(memories == null)
            {
                where = null;
                return true;
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

        // On: 2, peripheral (IO) accesses only. docker/renode's tlib/0002
        // then reports no load or store that hits RAM, which the detector
        // ignores anyway (OnAccess) but which, on a host whose TCG backend
        // calls its softmmu helpers for every access (aarch64), reached C#
        // one by one while armed. A tlib without the patch reports every
        // access for any value but 0, as for 1.
        public void SetMemoryAccessFlag(bool on)
        {
            Marshal.WriteInt32(State, flagOffset, on ? IoAccessesOnly : 0);
        }

        private const int IoAccessesOnly = 2;

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

        // tlib's executed-instruction total (tlib_get_total_executed_instructions,
        // TranslationCPU.GetCurrentInstructionsCount), without the call.
        public ulong Total() => (ulong)Marshal.ReadInt64(State, totalOffset);

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
