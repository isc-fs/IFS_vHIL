//
// A memory access hook on a Cortex-M that counts the accesses it is called
// for, standing for one that only watches peripherals (a trace of MMIO, a
// detector of polling loops): mode "none" sets no hook, "all" the hook for
// every access (SetHookAtMemoryAccess(hook)), "io" for IO accesses only
// (SetHookAtMemoryAccess(hook, true), PR 17, looked up by reflection so this
// also loads on a Renode without it). Also the peripheral the benchmark's
// loop reads: its accesses are the IO ones.
//
using System;
using System.Reflection;

using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Logging.Profiling;

namespace Antmicro.Renode.Peripherals.Mocks
{
    public class MemoryHookBench : IDoubleWordPeripheral, IKnownSize
    {
        public MemoryHookBench(ICPU cpu, string mode)
        {
            var translationCpu = (TranslationCPU)cpu;
            MemoryAccessHook hook = (pc, operation, virtualAddress, physicalAddress, width, value) =>
            {
                Calls++;
                if(operation == MemoryOperation.MemoryIORead || operation == MemoryOperation.MemoryIOWrite)
                {
                    IoCalls++;
                }
            };
            switch(mode)
            {
            case "none":
                break;
            case "all":
                translationCpu.SetHookAtMemoryAccess(hook);
                break;
            case "io":
                var method = typeof(TranslationCPU).GetMethod("SetHookAtMemoryAccess", new[] { typeof(MemoryAccessHook), typeof(bool) });
                if(method == null)
                {
                    throw new ArgumentException("This Renode cannot limit a memory access hook to IO accesses");
                }
                method.Invoke(translationCpu, new object[] { hook, true });
                break;
            default:
                throw new ArgumentException($"Unknown mode: {mode}");
            }
        }

        public void Reset()
        {
        }

        public uint ReadDoubleWord(long offset)
        {
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
        }

        public ulong Calls { get; private set; }

        public ulong IoCalls { get; private set; }

        public long Size => 0x1000;
    }
}
