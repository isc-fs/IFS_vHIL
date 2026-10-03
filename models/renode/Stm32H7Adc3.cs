//
// ADC3 of the STM32H72x/H73x, compiled by Renode at load time and placed by
// platforms/cpus/stm32h733.repl in place of the H743 ADC that Renode's
// stm32h7.repl puts at the same address.
//
// On the H72x/H73x ADC3 is a different 12-bit block (RM0468 "ADC3"): its own
// CFGR layout (RES at [4:3], ALIGN at 15), DIFSEL and CALFACT at 0xB0/0xB4,
// no PCSEL, and an LDO-ready flag. Register offsets and bits are from ST's
// stm32h733xx.h (ADC_TypeDef, ADC3_CFGR_*, ADC_CR_*, ADC_ISR_*).
//
// Modelled: enable/ready, offset calibration (single and differential),
// software-started regular sequences, single and continuous (the AMS runs
// continuous, main.c:411), EOSMP/EOC/EOS, resolution and alignment,
// differential inputs, offsets, the common CSR/CCR, and the IRQ. A conversion
// takes no virtual time: the next one runs when DR is read.
// Not modelled: injected conversions, hardware triggers, DMA, oversampling
// and the analog watchdogs; using one logs a warning once.
//
// Inputs are pin voltages, set per INP channel number:
//     sysbus.adc3 SetVoltage 1650000 3        (uV, channel)
// A differential channel reads INP<n> minus the pin that is its INN<n>,
// given by negativeInputs ("n:m" = INN<n> shares the pin of INP<m>).
//
// Fault injection: while ConversionFault is true a started conversion never
// ends (no EOC, DR unchanged), as when the ADC kernel clock is lost; the
// HAL's poll for EOC then times out.
//     sysbus.adc3 ConversionFault true
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Analog
{
    public class STM32H7_ADC3 : IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_ADC3(IMachine machine, double referenceVoltage = 3.3, string negativeInputs = "")
        {
            vref = referenceVoltage;
            foreach(var pair in (negativeInputs ?? "").Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var p = pair.Split(':');
                innOf[int.Parse(p[0].Trim())] = int.Parse(p[1].Trim());
            }
            IRQ = new GPIO();
            Reset();
        }

        public long Size => 0x400;
        public GPIO IRQ { get; }

        // Conversions done since reset: lets a test see the firmware sample.
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
            UpdateIrq();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset < 0 || offset >= Size)
            {
                return 0;
            }
            if(offset == DR)
            {
                var data = regs[DR / 4];
                regs[ISR / 4] &= ~ISR_EOC;   // reading DR clears EOC
                if((regs[CR / 4] & CR_ADSTART) != 0)
                {
                    ConvertNext();           // sequence or continuous mode goes on
                }
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
            switch(offset)
            {
            case ISR:
                regs[ISR / 4] &= ~value;   // write 1 to clear
                UpdateIrq();
                return;
            case CR:
                WriteCr(value);
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
                if((value & 1) != 0)
                {
                    WarnOnce("oversampling");
                }
                break;
            }
            regs[offset / 4] = value;
            if(offset == IER)
            {
                UpdateIrq();
            }
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
            }
            else if((value & CR_ADEN) != 0 && !enabled)
            {
                regs[ISR / 4] |= ISR_ADRDY;
                enabled = true;
            }
            if((value & CR_ADSTP) != 0)
            {
                value &= ~(CR_ADSTP | CR_ADSTART);
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
            // ADEN, ADDIS, ADSTART and friends are set-only from software.
            regs[CR / 4] = value | (cr & (enabled ? CR_ADEN : 0u));

            if((value & CR_ADSTART) != 0 && (cr & CR_ADSTART) == 0 && enabled)
            {
                rank = 1;
                ConvertNext();
            }
            UpdateIrq();
        }

        // One conversion of the regular sequence, at `rank`. After the last
        // rank: EOS, then wrap (continuous) or stop (single, ADSTART clears).
        private void ConvertNext()
        {
            if(ConversionFault)
            {
                return;
            }
            var length = (int)(regs[SQR1 / 4] & 0xF) + 1;
            regs[DR / 4] = Convert(SequenceChannel(rank));
            Conversions++;
            regs[ISR / 4] |= ISR_EOSMP | ISR_EOC;
            if(rank < length)
            {
                rank++;
                return;
            }
            regs[ISR / 4] |= ISR_EOS;
            rank = 1;
            if((regs[CFGR / 4] & CFGR_CONT) == 0)
            {
                regs[CR / 4] &= ~CR_ADSTART;
            }
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
        private const long SQR1 = 0x30;
        private const long SQR2 = 0x34;
        private const long SQR3 = 0x38;
        private const long SQR4 = 0x3C;
        private const long DR = 0x40;
        private const long OFR1 = 0x60;
        private const long DIFSEL = 0xB0;     // LTR2_DIFSEL on ADC3
        private const long CALFACT = 0xB4;    // HTR2_CALFACT on ADC3
        private const long COMMON_CSR = 0x300;

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
        private const uint ISR_LDORDY = 1u << 12;

        private const uint CFGR_EXTEN = 3u << 10;
        private const uint CFGR_CONT = 1u << 13;
        private const uint CFGR_ALIGN = 1u << 15;

        private int rank = 1;
        private readonly double vref;
        private readonly uint[] regs = new uint[0x400 / 4];
        private readonly double[] volts = new double[Channels];
        private readonly Dictionary<int, int> innOf = new Dictionary<int, int>();
        private readonly HashSet<string> warned = new HashSet<string>();
    }
}
