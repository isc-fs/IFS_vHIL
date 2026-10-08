//
// ADC3 of the STM32H72x/H73x, compiled by Renode at load time and placed by
// platforms/cpus/stm32h733.repl in place of the H743 ADC that Renode's
// stm32h7.repl puts at the same address.
//
// On the H72x/H73x ADC3 is a different 12-bit block (RM0468 "ADC3"): its own
// CFGR layout (DMAEN 0, DMACFG 1, RES at [4:3], ALIGN at 15), its own CFGR2
// oversampling ratio (OVSR at [4:2]), DIFSEL and CALFACT at 0xB0/0xB4, no
// PCSEL, and an LDO-ready flag. Register offsets and bits are from ST's
// stm32h733xx.h (ADC_TypeDef, ADC3_CFGR_*, ADC3_CFGR2_*, ADC_CR_*, ADC_ISR_*,
// ADC_CCR_*).
//
// Modelled: enable/ready, offset calibration (single and differential),
// software-started regular sequences, single and continuous, EOSMP/EOC/EOS
// and OVR, resolution and alignment, differential inputs, offsets, regular
// oversampling, DMA requests, the common CSR/CCR, and the IRQ.
//
// Conversions take virtual time (RM0468 ADC3, "Conversion timing" and
// "Oversampler"): one result every N x (sampling time + SAR time) ADC clock
// cycles, where the sampling time is the channel's SMPR field (2.5, 6.5,
// 12.5, 24.5, 47.5, 92.5, 247.5 or 640.5 cycles), the SAR time is 12.5, 10.5,
// 8.5 or 6.5 cycles for 12, 10, 8 or 6 bits, and N is the oversampling ratio
// (1 without it). The ADC clock is the kernel clock (the repl's
// kernelClockFrequency: PLL2P, 96 MHz on both MainLite firmwares) divided by
// CCR.PRESC, or HCLK (busClockFrequency) divided by CCR.CKMODE when that is
// not 0. The AMS on dev (main.c:416-436, current_task.cpp:229) runs 47.5
// cycles at 96 MHz / 2 with 64x oversampling: a result every 80 us, the
// 12.5 kHz its ams_config.hpp:789-809 sizes the capture for.
// A result sets EOC (and EOS at the end of the sequence); one landing while
// EOC is still set also sets OVR (DR is overwritten, OVRMOD aside: nothing
// here keeps the old value). In single mode ADSTART clears after the
// sequence; in continuous mode it runs until ADSTP.
//
// Results are computed when the firmware next looks (a register access) or,
// with DMAEN set, at their own time, raising a DMA request each (DmaRequest,
// for DMAMUX1 request 115, DMA_REQUEST_ADC3 in stm32h7xx_hal_dma.h:377). A
// rate above 100 kHz is delivered in 10 us batches.
//
// Oversampling: each of the N conversions of a result reads the same input
// (the emulated inputs are noise-free), the N codes are summed, shifted right
// by OVSS and kept to the 16 bits of DR, right-aligned whatever ALIGN says
// (RM0468 ADC3 "Oversampler"). The truncating shift is exact for the shifts
// the firmwares use (OVSS <= log2 N).
// Not modelled: injected conversions, hardware triggers, triggered
// oversampling (TROVS), the DMA one-shot stop after the DMA's last transfer
// (the DMA stream stops instead; the ADC's further requests are ignored), and
// the analog watchdogs; using one logs a warning once.
//
// Inputs are pin voltages, set per INP channel number:
//     sysbus.adc3 SetVoltage 1650000 3        (uV, channel)
// A differential channel reads INP<n> minus the pin that is its INN<n>,
// given by negativeInputs ("n:m" = INN<n> shares the pin of INP<m>).
//
// Fault injection: while ConversionFault is true a started conversion never
// ends (no EOC, no DMA request, DR unchanged), as when the ADC kernel clock
// is lost; the HAL's poll for EOC then times out.
//     sysbus.adc3 ConversionFault true
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Analog
{
    // The DMA reads DR as a half-word (PSIZE half-word, the AMS's
    // stm32h7xx_hal_msp.c:136 on dev).
    [AllowedTranslations(AllowedTranslation.WordToDoubleWord | AllowedTranslation.ByteToDoubleWord)]
    public class STM32H7_ADC3 : IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_ADC3(IMachine machine, double referenceVoltage = 3.3, string negativeInputs = "",
                            long kernelClockFrequency = 96000000, long busClockFrequency = 264000000)
        {
            this.machine = machine;
            vref = referenceVoltage;
            kernelHz = kernelClockFrequency;
            hclkHz = busClockFrequency;
            foreach(var pair in (negativeInputs ?? "").Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var p = pair.Split(':');
                innOf[int.Parse(p[0].Trim())] = int.Parse(p[1].Trim());
            }
            IRQ = new GPIO();
            DmaRequest = new GPIO();
            Reset();
        }

        public long Size => 0x400;
        public GPIO IRQ { get; }
        public GPIO DmaRequest { get; }

        // Results (oversampled ones count once) since reset: lets a test see
        // the firmware sample.
        public ulong Conversions { get; private set; }

        // Fault injection (see the header): started conversions never end.
        public bool ConversionFault { get; set; }

        public void SetVoltage(ulong microvolts, int channel)
        {
            CheckChannel(channel);
            volts[channel] = microvolts / 1e6;
        }

        public double GetVoltage(int channel)
        {
            CheckChannel(channel);
            return volts[channel];
        }

        public void Reset()
        {
            Array.Clear(regs, 0, regs.Length);
            regs[CR / 4] = CR_DEEPPWD;   // reset value 0x2000_0000
            warned.Clear();
            Conversions = 0;
            rank = 1;
            running = false;
            generation++;                // drop a pending result
            pendingAt = -1;
            IRQ.Unset();
            DmaRequest.Unset();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset < 0 || offset >= Size)
            {
                return 0;
            }
            CatchUp(Now());
            if(offset == DR)
            {
                var data = regs[DR / 4];
                regs[ISR / 4] &= ~ISR_EOC;   // reading DR clears EOC
                UpdateIrq();
                return data;
            }
            if(offset == COMMON_CSR)
            {
                return regs[ISR / 4] & 0x7FF;   // master flags mirror this ADC's ISR
            }
            return regs[offset / 4];
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset < 0 || offset >= Size)
            {
                return;
            }
            // Results due before this write land under the old settings.
            CatchUp(Now());
            switch(offset)
            {
            case ISR:
                regs[ISR / 4] &= ~value;   // write 1 to clear
                UpdateIrq();
                return;
            case CR:
                WriteCr(value);
                Schedule();
                return;
            case DR:
            case COMMON_CSR:
                return;   // read-only
            case CFGR:
                if((value & CFGR_EXTEN) != 0)
                {
                    WarnOnce($"hardware-triggered conversion (CFGR 0x{value:X8})");
                }
                break;
            case CFGR2:
                if((value & CFGR2_TROVS) != 0)
                {
                    WarnOnce("triggered oversampling (CFGR2.TROVS)");
                }
                if((value & CFGR2_JOVSE) != 0)
                {
                    WarnOnce("injected oversampling (CFGR2.JOVSE)");
                }
                break;
            }
            regs[offset / 4] = value;
            if(offset == IER)
            {
                UpdateIrq();
            }
            Schedule();
        }

        private void WriteCr(uint value)
        {
            var cr = regs[CR / 4];
            var enabled = (cr & CR_ADEN) != 0;

            if((value & CR_ADCAL) != 0 && !enabled)
            {
                // Calibration finishes at once: ADCAL reads back 0. Store a
                // plausible factor in the half of CALFACT the mode selects.
                var shift = (value & CR_ADCALDIF) != 0 ? 16 : 0;
                regs[CALFACT / 4] = (regs[CALFACT / 4] & ~(0x7FFu << shift)) | (0x40u << shift);
                value &= ~CR_ADCAL;
            }
            if((value & CR_ADVREGEN) != 0)
            {
                regs[ISR / 4] |= ISR_LDORDY;
            }
            if((value & CR_ADDIS) != 0 && enabled)
            {
                value &= ~(CR_ADEN | CR_ADDIS | CR_ADSTART | CR_JADSTART);
                enabled = false;
                running = false;
            }
            else if((value & CR_ADEN) != 0 && !enabled)
            {
                regs[ISR / 4] |= ISR_ADRDY;
                enabled = true;
            }
            if((value & CR_ADSTP) != 0)
            {
                value &= ~(CR_ADSTP | CR_ADSTART);
                running = false;
            }
            if((value & CR_JADSTP) != 0)
            {
                value &= ~(CR_JADSTP | CR_JADSTART);
            }
            if((value & CR_JADSTART) != 0)
            {
                WarnOnce("injected conversion");
                value &= ~CR_JADSTART;
            }
            // ADSTART stays set from a running sequence; ADEN, ADDIS and
            // ADSTART are set-only from software.
            var keep = (enabled ? CR_ADEN : 0u) | (running ? CR_ADSTART : 0u);
            regs[CR / 4] = value | (cr & keep);

            if((value & CR_ADSTART) != 0 && !running && enabled)
            {
                rank = 1;
                running = true;
                nextAt = Now() + PeriodTicks(rank);
            }
            UpdateIrq();
        }

        // Deliver every result due by `now` (see the header).
        private void CatchUp(double now)
        {
            if(catchingUp || !running)
            {
                return;
            }
            catchingUp = true;
            try
            {
                while(running && nextAt <= now)
                {
                    if(ConversionFault)
                    {
                        nextAt = now + PeriodTicks(rank);   // never ends
                        break;
                    }
                    if(!DmaEnabled && rank == 1)
                    {
                        // Nobody takes results one by one: skip whole
                        // sequences that ended unread, keeping the last.
                        var sequence = SequenceTicks();
                        var whole = Math.Floor((now - nextAt) / sequence);
                        if(whole >= 2)
                        {
                            nextAt += (whole - 1) * sequence;
                            Conversions += (ulong)(whole - 1) * (ulong)SequenceLength;
                            regs[ISR / 4] |= ISR_OVR;
                        }
                    }
                    Deliver();
                    if(running)
                    {
                        nextAt += PeriodTicks(rank);
                    }
                }
            }
            finally
            {
                catchingUp = false;
            }
            UpdateIrq();
        }

        // One result of the regular sequence, at `rank`. After the last
        // rank: EOS, then wrap (continuous) or stop (single, ADSTART clears).
        private void Deliver()
        {
            var length = SequenceLength;
            if((regs[ISR / 4] & ISR_EOC) != 0)
            {
                regs[ISR / 4] |= ISR_OVR;   // the previous result was never read
            }
            regs[DR / 4] = Convert(SequenceChannel(rank));
            Conversions++;
            regs[ISR / 4] |= ISR_EOSMP | ISR_EOC;
            if(rank < length)
            {
                rank++;
            }
            else
            {
                regs[ISR / 4] |= ISR_EOS;
                rank = 1;
                if((regs[CFGR / 4] & CFGR_CONT) == 0)
                {
                    regs[CR / 4] &= ~CR_ADSTART;
                    running = false;
                }
            }
            if(DmaEnabled)
            {
                // The DMA reads DR inside the request, which clears EOC.
                DmaRequest.Set(true);
                DmaRequest.Set(false);
            }
        }

        // With DMAEN, results land at their own time: one pending action at
        // the next result (or 10 us on, for faster rates).
        private void Schedule()
        {
            if(!running || !DmaEnabled)
            {
                return;
            }
            var now = Now();
            var at = Math.Max(nextAt, now + MinStepTicks);
            if(pendingAt >= 0 && pendingAt <= at)
            {
                return;   // an earlier action reschedules itself
            }
            var g = ++generation;
            pendingAt = at;
            var delay = (ulong)Math.Max(1, Math.Ceiling(at - now));
            machine.ScheduleAction(TimeInterval.FromTicks(delay), _ => OnResultDue(g), "adc3");
        }

        private void OnResultDue(ulong g)
        {
            if(g != generation)
            {
                return;   // superseded, or the ADC was reset
            }
            pendingAt = -1;
            CatchUp(Now());
            Schedule();
        }

        private double Now()
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            return machine.ElapsedVirtualTime.TimeElapsed.Ticks;
        }

        private bool DmaEnabled => (regs[CFGR / 4] & CFGR_DMAEN) != 0;

        private bool Oversampling => (regs[CFGR2 / 4] & CFGR2_ROVSE) != 0;

        private int OversamplingRatio => Oversampling ? 2 << (int)((regs[CFGR2 / 4] >> 2) & 0x7) : 1;

        private int SequenceLength => (int)(regs[SQR1 / 4] & 0xF) + 1;

        // Virtual-time ticks from one result to the next at `r`.
        private double PeriodTicks(int r)
        {
            var channel = SequenceChannel(r);
            var smpr = channel < 10 ? regs[SMPR1 / 4] : regs[SMPR2 / 4];
            var smp = SamplingCycles[(smpr >> (3 * (channel % 10))) & 0x7];
            var sar = 12.5 - 2 * ((regs[CFGR / 4] >> 3) & 0x3);   // RES: 12/10/8/6 bits
            return OversamplingRatio * (smp + sar) / AdcClockHz() * TicksPerSecond;
        }

        private double SequenceTicks()
        {
            double total = 0;
            for(var r = 1; r <= SequenceLength; r++)
            {
                total += PeriodTicks(r);
            }
            return total;
        }

        private double AdcClockHz()
        {
            var ccr = regs[COMMON_CCR / 4];
            var ckmode = (ccr >> 16) & 0x3;
            if(ckmode != 0)
            {
                return hclkHz / (double)(ckmode == 1 ? 1 : ckmode == 2 ? 2 : 4);   // HCLK/1, /2, /4
            }
            var presc = (ccr >> 18) & 0xF;
            return kernelHz / (double)(presc < PrescDivider.Length ? PrescDivider[presc] : 256);
        }

        // SQ1..SQ16: 5-bit fields, four in SQR1 (from bit 6), five in SQR2
        // and SQR3, two in SQR4, 6 bits apart.
        private int SequenceChannel(int rank)
        {
            uint reg;
            int index;
            if(rank <= 4)
            {
                reg = regs[SQR1 / 4];
                index = rank;
            }
            else if(rank <= 9)
            {
                reg = regs[SQR2 / 4];
                index = rank - 5;
            }
            else if(rank <= 14)
            {
                reg = regs[SQR3 / 4];
                index = rank - 10;
            }
            else
            {
                reg = regs[SQR4 / 4];
                index = rank - 15;
            }
            return (int)((reg >> (6 * index)) & 0x1F);
        }

        private uint Convert(int channel)
        {
            if(channel >= Channels)
            {
                return 0;
            }
            var bits = 12 - 2 * (int)((regs[CFGR / 4] >> 3) & 0x3);   // RES: 12/10/8/6
            var full = (1 << bits) - 1;
            double code;
            if((regs[DIFSEL / 4] & (1u << channel)) != 0)
            {
                var vn = innOf.TryGetValue(channel, out var m) ? volts[m] : 0.0;
                if(!innOf.ContainsKey(channel))
                {
                    WarnOnce($"differential channel {channel} has no negativeInputs entry; INN taken as 0 V");
                }
                code = (1 + (volts[channel] - vn) / vref) * (1 << bits) / 2;
            }
            else
            {
                code = volts[channel] / vref * full;
            }
            var value = (int)Math.Round(code);
            value = ApplyOffsets(channel, value, full);
            value = Math.Max(0, Math.Min(full, value));
            if(Oversampling)
            {
                // N identical conversions summed, shifted by OVSS, 16 bits.
                var shift = (int)((regs[CFGR2 / 4] >> 5) & 0xF);
                var sum = (long)value * OversamplingRatio;
                return (uint)((sum >> Math.Min(shift, 8)) & 0xFFFF);
            }
            if((regs[CFGR / 4] & CFGR_ALIGN) != 0)
            {
                value <<= 16 - bits;   // left-aligned in the 16-bit DR
            }
            return (uint)value;
        }

        // ADC3_OFRy: OFFSET [11:0], OFFSETPOS 24, SATEN 25, OFFSET_CH [30:26], EN 31.
        private int ApplyOffsets(int channel, int value, int full)
        {
            for(var i = 0; i < 4; i++)
            {
                var ofr = regs[(OFR1 + 4 * i) / 4];
                if((ofr & (1u << 31)) == 0 || ((ofr >> 26) & 0x1F) != channel)
                {
                    continue;
                }
                var offset = (int)(ofr & 0xFFF);
                value = (ofr & (1u << 24)) != 0 ? value + offset : value - offset;
            }
            return value;
        }

        private void UpdateIrq()
        {
            IRQ.Set((regs[ISR / 4] & regs[IER / 4] & 0x7FF) != 0);
        }

        private void CheckChannel(int channel)
        {
            if(channel < 0 || channel >= Channels)
            {
                throw new ArgumentException($"ADC3 has channels 0..{Channels - 1}");
            }
        }

        private void WarnOnce(string what)
        {
            if(warned.Add(what))
            {
                this.Log(LogLevel.Warning, "not modelled: {0}", what);
            }
        }

        private const int Channels = 20;

        private const long ISR = 0x00;
        private const long IER = 0x04;
        private const long CR = 0x08;
        private const long CFGR = 0x0C;
        private const long CFGR2 = 0x10;
        private const long SMPR1 = 0x14;
        private const long SMPR2 = 0x18;
        private const long SQR1 = 0x30;
        private const long SQR2 = 0x34;
        private const long SQR3 = 0x38;
        private const long SQR4 = 0x3C;
        private const long DR = 0x40;
        private const long OFR1 = 0x60;
        private const long DIFSEL = 0xB0;     // LTR2_DIFSEL on ADC3
        private const long CALFACT = 0xB4;    // HTR2_CALFACT on ADC3
        private const long COMMON_CSR = 0x300;
        private const long COMMON_CCR = 0x308;

        private const uint CR_ADEN = 1u << 0;
        private const uint CR_ADDIS = 1u << 1;
        private const uint CR_ADSTART = 1u << 2;
        private const uint CR_JADSTART = 1u << 3;
        private const uint CR_ADSTP = 1u << 4;
        private const uint CR_JADSTP = 1u << 5;
        private const uint CR_ADVREGEN = 1u << 28;
        private const uint CR_DEEPPWD = 1u << 29;
        private const uint CR_ADCALDIF = 1u << 30;
        private const uint CR_ADCAL = 1u << 31;

        private const uint ISR_ADRDY = 1u << 0;
        private const uint ISR_EOSMP = 1u << 1;
        private const uint ISR_EOC = 1u << 2;
        private const uint ISR_EOS = 1u << 3;
        private const uint ISR_OVR = 1u << 4;
        private const uint ISR_LDORDY = 1u << 12;

        private const uint CFGR_DMAEN = 1u << 0;
        private const uint CFGR_EXTEN = 3u << 10;
        private const uint CFGR_CONT = 1u << 13;
        private const uint CFGR_ALIGN = 1u << 15;

        private const uint CFGR2_ROVSE = 1u << 0;
        private const uint CFGR2_JOVSE = 1u << 1;
        private const uint CFGR2_TROVS = 1u << 9;

        // ADC3 SMPx codes 000..111 (RM0468 ADC3_SMPR1/2; the HAL's
        // ADC3_SAMPLETIME_* in stm32h7xx_hal_adc.h:673-680).
        private static readonly double[] SamplingCycles = { 2.5, 6.5, 12.5, 24.5, 47.5, 92.5, 247.5, 640.5 };
        // CCR.PRESC 0000..1011 (RM0468 ADC common CCR).
        private static readonly int[] PrescDivider = { 1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256 };

        private static readonly double TicksPerSecond = TimeInterval.FromSeconds(1).Ticks;
        private static readonly double MinStepTicks = TicksPerSecond / 100000;   // 10 us

        private int rank = 1;
        private bool running;
        private bool catchingUp;
        private double nextAt;
        private double pendingAt = -1;
        private ulong generation;
        private readonly IMachine machine;
        private readonly double vref;
        private readonly long kernelHz;
        private readonly long hclkHz;
        private readonly uint[] regs = new uint[0x400 / 4];
        private readonly double[] volts = new double[Channels];
        private readonly Dictionary<int, int> innOf = new Dictionary<int, int>();
        private readonly HashSet<string> warned = new HashSet<string>();
    }
}
