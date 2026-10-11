//
// Where the host's time goes, by guest code: a profiling aid for the speed
// benchmark (scripts/speed.py --profile), never loaded by a test or the bench.
// Compiled by Renode at load time:
//
//     emulation CreateVhilPcSampler "vhil_pcs_ecu" "ecu" 1
//     vhil_pcs_ecu Dump "/tmp/ecu.pcs"
//
// A host thread reads the board's PC every `periodMs` of host time and counts
// it. A PC that is hot in host time is code that costs the host the most to
// emulate (a busy-wait polling a peripheral, say), which instruction counts
// alone don't show: an MMIO read costs far more host time than an ALU op.
// Reading the PC from another thread races the CPU thread; a sample may be a
// block's start rather than the exact instruction, which is fine for a
// histogram. Dump writes "pc count" lines, hottest first.
//
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.CPU;

namespace Antmicro.Renode.Testing
{
    public static class VhilPcSamplerExtensions
    {
        public static void CreateVhilPcSampler(this Emulation emulation, string name, string machineName, int periodMs = 1)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilPcSampler(machine, periodMs), name);
        }
    }

    public class VhilPcSampler : IExternal
    {
        public VhilPcSampler(IMachine machine, int periodMs)
        {
            if(!machine.TryGetByName<TranslationCPU>("sysbus.cpu", out cpu))
            {
                throw new ArgumentException("needs sysbus.cpu");
            }
            period = Math.Max(1, periodMs);
            thread = new Thread(Run) { IsBackground = true, Name = "vhil-pc-sampler" };
            thread.Start();
        }

        public void Reset()
        {
        }

        public void Clear()
        {
            lock(counts)
            {
                counts.Clear();
                Samples = 0;
            }
        }

        public void Dump(string path)
        {
            lock(counts)
            {
                File.WriteAllLines(path, counts.OrderByDescending(kv => kv.Value)
                    .Select(kv => $"0x{kv.Key:X8} {kv.Value}"));
            }
        }

        public long Samples { get; private set; }

        private void Run()
        {
            while(true)
            {
                Thread.Sleep(period);
                ulong pc;
                try
                {
                    pc = cpu.PC;
                }
                catch(Exception)
                {
                    continue;
                }
                lock(counts)
                {
                    counts.TryGetValue(pc, out var n);
                    counts[pc] = n + 1;
                    Samples++;
                }
            }
        }

        private readonly TranslationCPU cpu;
        private readonly int period;
        private readonly Thread thread;
        private readonly Dictionary<ulong, long> counts = new Dictionary<ulong, long>();
    }
}
