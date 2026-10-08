//
// DMA1/DMA2 of the STM32H7 (ST's 8-stream DMA controller), compiled by Renode
// at load time and placed by platforms/cpus/stm32h733.repl in place of the
// STM32DMA that Renode's stm32h7.repl puts at DMA1.
//
// Renode's STM32DMA moves the data but tags HTIE, TEIE, DMEIE, CIRC, DBM and
// the half-transfer, error and FIFO flags: the AMS's ADC3 capture (dev,
// current_task.cpp:242, HAL_ADC_Start_DMA on DMA1 Stream1) enables HTIE and
// TEIE, which it dropped, and it never cleared EN at the end of a normal-mode
// transfer. Its class is sealed, so this is a re-implementation, not a
// subclass.
//
// Behaviour per RM0468 (DMA controller chapter):
// - Requests: a peripheral-to-memory stream moves one data item per request
//   from its DMAMUX1 channel (the request line's rising edge, as Renode's
//   STM32_DMAMUX drives it). Memory-to-memory moves the whole block at EN, as
//   the hardware does with no request; memory-to-peripheral is not paced by
//   the peripheral either (no model here raises a TX request) and moves the
//   whole block at EN, as Renode's did.
// - NDTR counts data items of PSIZE ("Programmable data width, packing/
//   unpacking"), the peripheral pointer advances by PSIZE (or 4 with
//   PINCOS), the memory pointer by the same number of bytes, so FIFO packing
//   lands the same bytes in memory.
// - HTIF when half the items of a pass have moved, TCIF when NDTR reaches 0.
//   Normal mode then clears EN by hardware; circular mode ("Circular mode")
//   reloads NDTR and the pointers and keeps going; double-buffer mode
//   ("Double-buffer mode") also toggles CT and switches memory target.
// - EN cleared by software stops the stream and sets TCIF ("Stream
//   configuration procedure" / disabling a stream): HAL_DMA_Abort_IT relies
//   on it. The HAL's HAL_DMA_Abort masks TCIE first, so it sees no interrupt.
// - A transfer to or from an address DMA1/DMA2 cannot reach sets TEIF and
//   clears EN ("Error management"). On the H72x/H73x the DMAs sit in D2 and
//   reach neither the ITCM nor the DTCM (RM0468, "Memory and bus
//   architecture": the TCMs are reached only by the CPU and, through the
//   AHBS port, the MDMA), nor any address nothing is mapped at. The AMS keeps
//   its DMA buffers out of the DTCM for exactly that reason (dev,
//   current_task.cpp:90-95, imu_task.cpp:39-42).
// - The stream's interrupt line is the OR of each set flag whose enable is
//   set: TCIF/TCIE, HTIF/HTIE, TEIF/TEIE, DMEIF/DMEIE, FEIF/FEIE.
// - SxCR, SxNDTR, SxPAR, SxM0AR (outside double-buffer mode) and SxFCR are
//   write-protected while EN is set; SxCR's EN and interrupt enables stay
//   writable, which is what HAL_DMA_Abort relies on.
// Not modelled: FEIF and DMEIF (the emulated bus never stalls, so the FIFO
// never under/overruns and a direct-mode request is always served before the
// next), stream priorities and burst beats (a transfer takes no virtual time,
// so arbitration cannot reorder anything a test sees), and the peripheral as
// flow controller (PFCTRL: only the SDMMC uses it on the H7, through its own
// IDMA instead). Setting PFCTRL logs a warning once.
//
// Register offsets from ST's stm32h733xx.h (DMA_TypeDef: LISR 0x00, HISR
// 0x04, LIFCR 0x08, HIFCR 0x0C; DMA_Stream_TypeDef at DMA1_BASE + 0x10 +
// 0x18 * n: CR, NDTR, PAR, M0AR, M1AR, FCR) and its DMA_SxCR_* / DMA_SxFCR_*
// / DMA_LISR_* bit positions.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.DMA
{
    public class STM32H7_DMA : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public STM32H7_DMA(IMachine machine)
        {
            sysbus = machine.GetSystemBus(this);
            streams = Enumerable.Range(0, Streams).Select(i => new Stream(this, i)).ToArray();
            Connections = Enumerable.Range(0, Streams).ToDictionary(i => i, i => (IGPIO)streams[i].IRQ);
            Reset();
        }

        public long Size => 0x400;
        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        // Data items moved since reset, per stream: lets a test see a transfer.
        public ulong Transfers(int stream) => streams[stream].Moved;

        public void Reset()
        {
            foreach(var s in streams)
            {
                s.Reset();
            }
            warned.Clear();
        }

        // A DMAMUX1 request line (0..7 -> stream 0..7).
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= Streams)
            {
                this.Log(LogLevel.Warning, "request on stream {0}: there are {1}", number, Streams);
                return;
            }
            if(value)
            {
                streams[number].OnRequest();
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case LISR:
                return Flags(0);
            case HISR:
                return Flags(4);
            case LIFCR:
            case HIFCR:
                return 0;   // write-only
            }
            if(TryStream(offset, out var s, out var reg))
            {
                return s.Read(reg);
            }
            this.Log(LogLevel.Warning, "Unhandled read from offset 0x{0:X}.", offset);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case LISR:
            case HISR:
                return;   // read-only
            case LIFCR:
                ClearFlags(0, value);
                return;
            case HIFCR:
                ClearFlags(4, value);
                return;
            }
            if(TryStream(offset, out var s, out var reg))
            {
                s.Write(reg, value);
                return;
            }
            this.Log(LogLevel.Warning, "Unhandled write to offset 0x{0:X}, value 0x{1:X}.", offset, value);
        }

        // LISR/HISR: each stream's six flags at 0, 6, 16 and 22.
        private uint Flags(int first)
        {
            uint v = 0;
            for(var i = 0; i < 4; i++)
            {
                v |= (uint)streams[first + i].FlagBits << FlagShift[i];
            }
            return v;
        }

        private void ClearFlags(int first, uint value)
        {
            for(var i = 0; i < 4; i++)
            {
                streams[first + i].ClearFlags((value >> FlagShift[i]) & FlagMask);
            }
        }

        private bool TryStream(long offset, out Stream stream, out long reg)
        {
            stream = null;
            reg = 0;
            if(offset < StreamBase || offset >= StreamBase + Streams * StreamStep)
            {
                return false;
            }
            stream = streams[(offset - StreamBase) / StreamStep];
            reg = (offset - StreamBase) % StreamStep;
            return true;
        }

        private void WarnOnce(string what)
        {
            if(warned.Add(what))
            {
                this.Log(LogLevel.Warning, "not modelled: {0}", what);
            }
        }

        // Whether DMA1/DMA2 can reach `address` (see the header).
        private bool Reachable(ulong address, int size)
        {
            if(address + (ulong)size <= ItcmEnd || (address >= DtcmBase && address < DtcmEnd))
            {
                return false;
            }
            return sysbus.WhatIsAt(address) != null;
        }

        private ulong Read(ulong address, int size)
        {
            switch(size)
            {
            case 1:
                return sysbus.ReadByte(address);
            case 2:
                return sysbus.ReadWord(address);
            default:
                return sysbus.ReadDoubleWord(address);
            }
        }

        private void Write(ulong address, ulong value, int size)
        {
            switch(size)
            {
            case 1:
                sysbus.WriteByte(address, (byte)value);
                break;
            case 2:
                sysbus.WriteWord(address, (ushort)value);
                break;
            default:
                sysbus.WriteDoubleWord(address, (uint)value);
                break;
            }
        }

        private class Stream
        {
            public Stream(STM32H7_DMA parent, int id)
            {
                this.parent = parent;
                this.id = id;
            }

            public GPIO IRQ { get; } = new GPIO();
            public ulong Moved { get; private set; }
            public uint FlagBits => flags;

            public void Reset()
            {
                cr = 0;
                ndtr = 0;
                par = 0;
                m0ar = 0;
                m1ar = 0;
                fcr = FcrReset;
                flags = 0;
                pending = 0;
                busy = false;
                Moved = 0;
                IRQ.Unset();
            }

            public uint Read(long reg)
            {
                switch(reg)
                {
                case CR: return cr;
                case NDTR: return ndtr;
                case PAR: return par;
                case M0AR: return m0ar;
                case M1AR: return m1ar;
                case FCR: return fcr;
                }
                return 0;
            }

            public void Write(long reg, uint value)
            {
                var enabled = (cr & CR_EN) != 0;
                switch(reg)
                {
                case CR:
                    WriteCr(value);
                    return;
                case NDTR:
                    if(!enabled)
                    {
                        ndtr = value & 0xFFFF;
                    }
                    return;
                case PAR:
                    if(!enabled)
                    {
                        par = value;
                    }
                    return;
                case M0AR:
                    // In double-buffer mode the target not in use is writable.
                    if(!enabled || ((cr & CR_DBM) != 0 && (cr & CR_CT) != 0))
                    {
                        m0ar = value;
                    }
                    return;
                case M1AR:
                    if(!enabled || ((cr & CR_DBM) != 0 && (cr & CR_CT) == 0))
                    {
                        m1ar = value;
                    }
                    return;
                case FCR:
                    if(enabled)
                    {
                        value = (value & FCR_FEIE) | (fcr & ~FCR_FEIE);
                    }
                    // FS (FIFO status) is read-only; the FIFO is always empty
                    // between transfers here (FS = 100b, the reset value).
                    fcr = (value & (FCR_FTH | FCR_DMDIS | FCR_FEIE)) | FCR_FS_EMPTY;
                    UpdateIrq();
                    return;
                }
            }

            public void ClearFlags(uint mask)
            {
                flags &= ~mask;
                UpdateIrq();
            }

            public void OnRequest()
            {
                if((cr & CR_EN) == 0 || Direction != DirP2M)
                {
                    return;   // a request to a stopped stream is lost, as on the chip
                }
                // A transfer reads the peripheral, which may raise the next
                // request at once (the I2C's next received byte): serve it
                // after this one, not inside it.
                pending++;
                if(busy)
                {
                    return;
                }
                busy = true;
                try
                {
                    while(pending > 0 && (cr & CR_EN) != 0)
                    {
                        pending--;
                        MoveOne();
                    }
                }
                finally
                {
                    pending = 0;
                    busy = false;
                }
            }

            private void WriteCr(uint value)
            {
                var was = cr;
                var enabled = (was & CR_EN) != 0;
                if(enabled)
                {
                    // Only EN and the interrupt enables change while enabled.
                    value = (value & (CR_EN | CR_IE)) | (was & ~(CR_EN | CR_IE));
                }
                cr = value & CrMask;
                if((cr & CR_PFCTRL) != 0)
                {
                    parent.WarnOnce($"peripheral flow control on stream {id}");
                }
                if(!enabled && (cr & CR_EN) != 0)
                {
                    Start();
                }
                else if(enabled && (cr & CR_EN) == 0)
                {
                    // Disabled by software mid-transfer: TCIF marks the stop.
                    flags |= F_TCIF;
                }
                UpdateIrq();
            }

            private void Start()
            {
                reload = ndtr;
                done = 0;
                pOffset = 0;
                mOffset = 0;
                if(Direction == DirReserved)
                {
                    parent.Log(LogLevel.Warning, "stream {0} enabled with DIR = 11 (reserved)", id);
                    return;
                }
                if(Direction != DirP2M)
                {
                    // No request pacing: the whole block moves now.
                    while((cr & CR_EN) != 0 && ndtr > 0)
                    {
                        MoveOne();
                    }
                }
            }

            private void MoveOne()
            {
                if(ndtr == 0)
                {
                    return;
                }
                var size = 1 << (int)((cr >> 11) & 0x3);   // PSIZE
                if(size > 4)
                {
                    parent.Log(LogLevel.Warning, "stream {0}: PSIZE 11 is reserved", id);
                    Fail();
                    return;
                }
                var memory = (ulong)(((cr & CR_DBM) != 0 && (cr & CR_CT) != 0) ? m1ar : m0ar);
                var periph = (ulong)par;
                ulong src, dst;
                if(Direction == DirM2P)
                {
                    src = memory + mOffset;
                    dst = periph + pOffset;
                }
                else
                {
                    // P2M, and M2M, whose source is the peripheral port (PAR).
                    src = periph + pOffset;
                    dst = memory + mOffset;
                }
                if(!parent.Reachable(src, size) || !parent.Reachable(dst, size))
                {
                    parent.Log(LogLevel.Debug, "stream {0}: bus error at 0x{1:X8} -> 0x{2:X8}", id, src, dst);
                    Fail();
                    return;
                }
                // Advance before touching the peripheral (see OnRequest).
                if((cr & CR_PINC) != 0)
                {
                    pOffset += (cr & CR_PINCOS) != 0 ? 4u : (uint)size;
                }
                if((cr & CR_MINC) != 0)
                {
                    mOffset += (uint)size;
                }
                ndtr--;
                done++;
                var value = parent.Read(src, size);
                parent.Write(dst, value, size);
                Moved++;

                if(reload >= 2 && done == reload / 2)
                {
                    flags |= F_HTIF;
                }
                if(ndtr == 0)
                {
                    flags |= F_TCIF;
                    if((cr & (CR_CIRC | CR_DBM)) != 0)
                    {
                        ndtr = reload;
                        done = 0;
                        pOffset = 0;
                        mOffset = 0;
                        if((cr & CR_DBM) != 0)
                        {
                            cr ^= CR_CT;
                        }
                    }
                    else
                    {
                        cr &= ~CR_EN;   // normal mode: the stream stops itself
                    }
                }
                UpdateIrq();
            }

            private void Fail()
            {
                flags |= F_TEIF;
                cr &= ~CR_EN;
                UpdateIrq();
            }

            private void UpdateIrq()
            {
                var active = ((flags & F_TCIF) != 0 && (cr & CR_TCIE) != 0)
                    || ((flags & F_HTIF) != 0 && (cr & CR_HTIE) != 0)
                    || ((flags & F_TEIF) != 0 && (cr & CR_TEIE) != 0)
                    || ((flags & F_DMEIF) != 0 && (cr & CR_DMEIE) != 0)
                    || ((flags & F_FEIF) != 0 && (fcr & FCR_FEIE) != 0);
                IRQ.Set(active);
            }

            private uint Direction => (cr >> 6) & 0x3;

            private uint cr, ndtr, par, m0ar, m1ar, fcr, flags;
            private uint reload, done, pOffset, mOffset;
            private int pending;
            private bool busy;
            private readonly STM32H7_DMA parent;
            private readonly int id;
        }

        private const int Streams = 8;
        private const long LISR = 0x00;
        private const long HISR = 0x04;
        private const long LIFCR = 0x08;
        private const long HIFCR = 0x0C;
        private const long StreamBase = 0x10;
        private const long StreamStep = 0x18;
        private const long CR = 0x00;
        private const long NDTR = 0x04;
        private const long PAR = 0x08;
        private const long M0AR = 0x0C;
        private const long M1AR = 0x10;
        private const long FCR = 0x14;

        private static readonly int[] FlagShift = { 0, 6, 16, 22 };
        private const uint FlagMask = 0x3D;   // FEIF 0, DMEIF 2, TEIF 3, HTIF 4, TCIF 5
        private const uint F_FEIF = 1u << 0;
        private const uint F_DMEIF = 1u << 2;
        private const uint F_TEIF = 1u << 3;
        private const uint F_HTIF = 1u << 4;
        private const uint F_TCIF = 1u << 5;

        private const uint CR_EN = 1u << 0;
        private const uint CR_DMEIE = 1u << 1;
        private const uint CR_TEIE = 1u << 2;
        private const uint CR_HTIE = 1u << 3;
        private const uint CR_TCIE = 1u << 4;
        private const uint CR_IE = CR_DMEIE | CR_TEIE | CR_HTIE | CR_TCIE;
        private const uint CR_PFCTRL = 1u << 5;
        private const uint CR_CIRC = 1u << 8;
        private const uint CR_PINC = 1u << 9;
        private const uint CR_MINC = 1u << 10;
        private const uint CR_PINCOS = 1u << 15;
        private const uint CR_DBM = 1u << 18;
        private const uint CR_CT = 1u << 19;
        private const uint CrMask = 0x01FFFFFF;   // TRBUFF 20, PBURST 22:21, MBURST 24:23 kept as written

        private const uint DirP2M = 0;
        private const uint DirM2P = 1;
        private const uint DirReserved = 3;

        private const uint FCR_FTH = 0x3;
        private const uint FCR_DMDIS = 1u << 2;
        private const uint FCR_FS_EMPTY = 4u << 3;
        private const uint FCR_FEIE = 1u << 7;
        private const uint FcrReset = 0x21;   // FS = 100b (empty), FTH = 01b

        // ITCM 0x00000000-0x0000FFFF, DTCM 0x20000000-0x2001FFFF (RM0468,
        // "Memory organization"; ITCMRAM/DTCMRAM in the AMS's
        // STM32H733XG_FLASH.ld:58,62).
        private const ulong ItcmEnd = 0x00010000;
        private const ulong DtcmBase = 0x20000000;
        private const ulong DtcmEnd = 0x20020000;

        private readonly IBusController sysbus;
        private readonly Stream[] streams;
        private readonly HashSet<string> warned = new HashSet<string>();
    }
}
