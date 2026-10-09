//
// A reserved range of the STM32H733's memory map: every access to it ends in
// a bus error, as on the chip. Compiled by Renode at load time and placed by
// platforms/cpus/stm32h733.repl.
//
// On the chip, an address no slave decodes is answered by the bus matrix with
// an error response (RM0468 Rev 3 §2.3.2: accesses to reserved areas of the
// memory map; AXI DECERR), which the Cortex-M7 takes as a BusFault: precise
// for a load (CFSR.PRECISERR, BFAR = the address), imprecise for a buffered
// store (ARMv7-M ARM B3.2.15). Renode, by default, returns 0 for an unmapped
// read and drops the write: firmware that dereferences a corrupted pointer
// into such a range runs on. This model throws Renode's BusAccessException,
// which the CortexM model turns into a precise BusFault at the faulting
// address (CortexM.HandleBusAccessError -> tlibRaisePreciseBusFault); NVIC
// then takes BusFault_Handler if SHCSR.BUSFAULTENA is set, or escalates it to
// HardFault with HFSR.FORCED if not (ARMv7-M ARM B1.5.8, "Priority
// escalation").
//
// No firmware touches these ranges in a passing run (the peripheral guard,
// vhil/peripheral_guard.py, would have failed it on an unmapped access); tests
// point a load at them to fault the CPU (vhil/sim.py, Sim.bus_fault_at).
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class VhilBusError : IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral, IKnownSize
    {
        public VhilBusError(long size)
        {
            Size = size;
        }

        public long Size { get; }

        public void Reset()
        {
        }

        public uint ReadDoubleWord(long offset) => throw Fault(offset, "read");
        public ushort ReadWord(long offset) => throw Fault(offset, "read");
        public byte ReadByte(long offset) => throw Fault(offset, "read");
        public void WriteDoubleWord(long offset, uint value) => throw Fault(offset, "write");
        public void WriteWord(long offset, ushort value) => throw Fault(offset, "write");
        public void WriteByte(long offset, byte value) => throw Fault(offset, "write");

        private BusAccessException Fault(long offset, string what)
        {
            this.Log(LogLevel.Info, "bus error: {0} at offset 0x{1:X}", what, offset);
            return new BusAccessException(BusAccessError.AddressError);
        }
    }
}
