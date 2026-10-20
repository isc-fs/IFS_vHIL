//
// A pre-opcode execution hook on every `nop` of a Cortex-M, standing for a hook
// that only needs the first execution at each address (coverage-style). With
// exclude: true it excludes itself at each address it ran at
// (TranslationCPU.ExcludePreOpcodeExecutionHookAt, PR 11, looked up by
// reflection so this also loads on a Renode without it); `Calls` counts the
// calls that reached it. Registered on the bus only to be created from the
// platform description.
//
using System;
using System.Reflection;

using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;

namespace Antmicro.Renode.Peripherals.Mocks
{
    public class HookBench : IDoubleWordPeripheral, IKnownSize
    {
        public HookBench(ICPU cpu, bool exclude)
        {
            var translationCpu = (TranslationCPU)cpu;
            var method = typeof(TranslationCPU).GetMethod("ExcludePreOpcodeExecutionHookAt", new[] { typeof(Action<ulong, ulong>), typeof(ulong) });
            if(exclude && method == null)
            {
                throw new ArgumentException("This Renode cannot exclude a pre-opcode execution hook at an address");
            }
            Action<ulong, ulong> hook = null;
            hook = (pc, opcode) =>
            {
                Calls++;
                if(exclude)
                {
                    method.Invoke(translationCpu, new object[] { hook, pc });
                }
            };
            translationCpu.AddPreOpcodeExecutionHook(0xFFFFFFFF, 0xBF00, hook);
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

        public long Size => 0x1000;
    }
}
