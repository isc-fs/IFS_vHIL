//
// The last blocks a CPU ran, kept in memory for failure snapshots
// (vhil/snapshot.py, `pytest tests/sim --vhil-trace`). Compiled by Renode at
// load time (`include @models/renode/VhilTrace.cs`).
//
//     emulation CreateVhilTrace "vhil_trace_ecu" "ecu" 4096
//     vhil_trace_ecu Ring 0        the ring, oldest first: "0xPC count" lines
//     vhil_trace_ecu Blocks        blocks run since the trace was created
//
// A block-begin hook on the board's CPU writes each translation block's start
// PC and instruction count into a fixed ring. Renode 1.17's ExecutionTracer
// has no bounded mode: it writes every instruction to a file (about 2.6 GB per
// virtual second of the ECU in PC format, and 12x slower), when a snapshot
// only needs the tail. The hook still costs a managed call per block, 5-8x
// on the ECU, which is why the trace is opt-in.
//
using System;
using System.Linq;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.CPU;

namespace Antmicro.Renode.Testing
{
    public static class VhilTraceExtensions
    {
        public static void CreateVhilTrace(this Emulation emulation, string name, string machineName, int ring = 4096)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            var cpu = machine.SystemBus.GetCPUs().OfType<TranslationCPU>().FirstOrDefault();
            if(cpu == null)
            {
                throw new ArgumentException($"machine '{machineName}' has no translation CPU");
            }
            emulation.ExternalsManager.AddExternal(new VhilTrace(cpu, ring), name);
        }
    }

    public class VhilTrace : IExternal
    {
        public VhilTrace(TranslationCPU cpu, int ring)
        {
            var size = 1;
            while(size < Math.Max(ring, 1))
            {
                size <<= 1;
            }
            pcs = new ulong[size];
            counts = new uint[size];
            mask = (ulong)(size - 1);
            cpu.SetHookAtBlockBegin(OnBlock);
        }

        // Runs on the CPU thread; the monitor reads the ring only while the
        // emulation is paused between RunFor calls.
        private void OnBlock(ulong pc, uint count)
        {
            var i = next & mask;
            pcs[i] = pc;
            counts[i] = count;
            next++;
        }

        // The last `last` blocks (0: the whole ring), oldest first.
        public string Ring(int last = 0)
        {
            var have = Math.Min(next, (ulong)pcs.Length);
            var take = last <= 0 ? have : Math.Min(have, (ulong)last);
            var sb = new StringBuilder();
            for(var k = next - take; k < next; k++)
            {
                var i = k & mask;
                sb.Append("0x").Append(pcs[i].ToString("X")).Append(' ').Append(counts[i]).Append('\n');
            }
            return sb.ToString();
        }

        public ulong Blocks()
        {
            return next;
        }

        private readonly ulong[] pcs;
        private readonly uint[] counts;
        private readonly ulong mask;
        private ulong next;
    }
}
