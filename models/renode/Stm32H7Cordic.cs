//
// CORDIC co-processor of the STM32H72x/H73x, compiled by Renode at load time
// and placed by platforms/cpus/stm32h733.repl. Renode's stm32h7.repl (H743)
// has none, so CORDIC_BASE (D2_AHB2PERIPH_BASE + 0x4400 = 0x48024400,
// stm32h733xx.h:2275) was unmapped: the uDV's attitude filter
// (IFS09-DV-uDV attitude.c:19-56) read 0 for every atan2 and modulus.
//
// Registers (stm32h733xx.h CORDIC_TypeDef, CORDIC_CSR_*): CSR 0x00, WDATA
// 0x04, RDATA 0x08. Behaviour after RM0468 "CORDIC co-processor": a write to
// WDATA supplies the arguments (ARGSIZE 32-bit: one per write, NARGS+1 writes;
// 16-bit: both in one write), the last one starts the calculation, RRDY rises
// with the results, and RDATA returns them (RESSIZE 32-bit: NRES+1 reads;
// 16-bit: both in one read), clearing RRDY on the last read. ARG2 keeps its
// value between calculations and resets to +1 (0x7FFFFFFF). IEN raises the
// IRQ while RRDY is set.
//
// Modelled: all ten functions, in double precision rounded to q1.31 / q1.15
// and saturated, with SCALE as RM0468 defines it per function. A calculation
// takes no virtual time, so RDATA never stalls the bus. PRECISION (the
// iteration count) is held but does not degrade the result.
// Not modelled: DMA requests (DMAREN/DMAWEN); setting one logs a warning once.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class STM32H7_CORDIC : IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_CORDIC(IMachine machine)
        {
            IRQ = new GPIO();
            Reset();
        }

        public long Size => 0x400;
        public GPIO IRQ { get; }

        // Calculations done since reset: lets a test see the firmware use it.
        public ulong Calculations { get; private set; }

        public void Reset()
        {
            csr = CSR_RESET;
            arg1 = 0;
            arg2 = Q31_ONE;
            argsWritten = 0;
            results = new uint[0];
            resultsRead = 0;
            Calculations = 0;
            warned.Clear();
            UpdateIrq();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case CSR:
                return csr | (Ready ? CSR_RRDY : 0u);
            case RDATA:
                if(!Ready)
                {
                    // The chip inserts bus wait states until a result is
                    // ready; with no calculation pending there is none.
                    return 0;
                }
                var value = results[resultsRead++];
                UpdateIrq();
                return value;
            default:
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case CSR:
                csr = value & CSR_WRITABLE;
                argsWritten = 0;
                if((value & (CSR_DMAREN | CSR_DMAWEN)) != 0)
                {
                    WarnOnce("DMA requests (DMAREN/DMAWEN)");
                }
                UpdateIrq();
                return;
            case WDATA:
                WriteArgument(value);
                return;
            default:
                return;   // RDATA is read-only
            }
        }

        private bool Ready => resultsRead < results.Length;

        private void WriteArgument(uint value)
        {
            if((csr & CSR_ARGSIZE) != 0)
            {
                // Two q1.15 arguments packed in one word (NARGS must be 0).
                arg1Value = Q15ToDouble((ushort)value);
                arg2Value = Q15ToDouble((ushort)(value >> 16));
                Calculate();
                return;
            }
            if(argsWritten == 0)
            {
                arg1 = value;
            }
            else
            {
                arg2 = value;
            }
            argsWritten++;
            if(argsWritten < ((csr & CSR_NARGS) != 0 ? 2 : 1))
            {
                return;
            }
            argsWritten = 0;
            arg1Value = Q31ToDouble(arg1);
            arg2Value = Q31ToDouble(arg2);
            Calculate();
        }

        private void Calculate()
        {
            var func = (int)(csr & CSR_FUNC);
            var n = (int)((csr & CSR_SCALE) >> CSR_SCALE_POS);
            double x = arg1Value, y = arg2Value, r1, r2 = 0;
            switch(func)
            {
            case 0:   // cosine: ARG1 = angle / pi, ARG2 = modulus
                r1 = y * Math.Cos(x * Math.PI);
                r2 = y * Math.Sin(x * Math.PI);
                break;
            case 1:   // sine
                r1 = y * Math.Sin(x * Math.PI);
                r2 = y * Math.Cos(x * Math.PI);
                break;
            case 2:   // phase: ARG1 = x, ARG2 = y; RES1 = atan2(y, x) / pi, RES2 = modulus
                r1 = Math.Atan2(y, x) / Math.PI;
                r2 = Math.Sqrt(x * x + y * y);
                break;
            case 3:   // modulus
                r1 = Math.Sqrt(x * x + y * y);
                r2 = Math.Atan2(y, x) / Math.PI;
                break;
            case 4:   // arctangent: ARG1 = x * 2^-n; RES1 = atan(x) * 2^-n / pi
                r1 = Math.Atan(x * Math.Pow(2, n)) / Math.PI / Math.Pow(2, n);
                break;
            case 5:   // hyperbolic cosine: ARG1 = x * 2^-n; RES1 = cosh * 2^-n, RES2 = sinh * 2^-n
                r1 = Math.Cosh(x * Math.Pow(2, n)) / Math.Pow(2, n);
                r2 = Math.Sinh(x * Math.Pow(2, n)) / Math.Pow(2, n);
                break;
            case 6:   // hyperbolic sine
                r1 = Math.Sinh(x * Math.Pow(2, n)) / Math.Pow(2, n);
                r2 = Math.Cosh(x * Math.Pow(2, n)) / Math.Pow(2, n);
                break;
            case 7:   // hyperbolic arctangent: RES1 = atanh(x) * 2^-n
                r1 = Atanh(x * Math.Pow(2, n)) / Math.Pow(2, n);
                break;
            case 8:   // natural log: ARG1 = x * 2^-n; RES1 = ln(x) * 2^-(n+1)
                r1 = Math.Log(x * Math.Pow(2, n)) / Math.Pow(2, n + 1);
                break;
            case 9:   // square root: ARG1 = x * 2^-n; RES1 = sqrt(x) * 2^-n
                r1 = Math.Sqrt(x * Math.Pow(2, n)) / Math.Pow(2, n);
                break;
            default:
                WarnOnce($"reserved function {func}");
                r1 = 0;
                break;
            }
            if((csr & CSR_RESSIZE) != 0)
            {
                results = new[] { (uint)DoubleToQ15(r1) | ((uint)DoubleToQ15(r2) << 16) };
            }
            else
            {
                results = (csr & CSR_NRES) != 0
                    ? new[] { DoubleToQ31(r1), DoubleToQ31(r2) }
                    : new[] { DoubleToQ31(r1) };
            }
            resultsRead = 0;
            Calculations++;
            UpdateIrq();
        }

        private static double Atanh(double v) => 0.5 * Math.Log((1 + v) / (1 - v));

        private static double Q31ToDouble(uint q) => (int)q / 2147483648.0;
        private static double Q15ToDouble(ushort q) => (short)q / 32768.0;

        private static uint DoubleToQ31(double v)
        {
            if(double.IsNaN(v)) return 0;
            var s = Math.Round(v * 2147483648.0);
            if(s > int.MaxValue) s = int.MaxValue;
            if(s < int.MinValue) s = int.MinValue;
            return (uint)(int)s;
        }

        private static ushort DoubleToQ15(double v)
        {
            if(double.IsNaN(v)) return 0;
            var s = Math.Round(v * 32768.0);
            if(s > short.MaxValue) s = short.MaxValue;
            if(s < short.MinValue) s = short.MinValue;
            return (ushort)(short)s;
        }

        private void UpdateIrq()
        {
            IRQ.Set((csr & CSR_IEN) != 0 && Ready);
        }

        private void WarnOnce(string what)
        {
            if(warned.Add(what))
            {
                this.Log(LogLevel.Warning, "not modelled: {0}", what);
            }
        }

        private const long CSR = 0x00;
        private const long WDATA = 0x04;
        private const long RDATA = 0x08;

        private const uint CSR_RESET = 0x00000050;   // FUNC cosine, PRECISION 5
        private const uint CSR_FUNC = 0xFu;
        private const int CSR_SCALE_POS = 8;
        private const uint CSR_SCALE = 7u << CSR_SCALE_POS;
        private const uint CSR_IEN = 1u << 16;
        private const uint CSR_DMAREN = 1u << 17;
        private const uint CSR_DMAWEN = 1u << 18;
        private const uint CSR_NRES = 1u << 19;
        private const uint CSR_NARGS = 1u << 20;
        private const uint CSR_RESSIZE = 1u << 21;
        private const uint CSR_ARGSIZE = 1u << 22;
        private const uint CSR_RRDY = 1u << 31;
        private const uint CSR_WRITABLE = 0x007F07FF;
        private const uint Q31_ONE = 0x7FFFFFFF;

        private uint csr;
        private uint arg1;
        private uint arg2;
        private double arg1Value;
        private double arg2Value;
        private int argsWritten;
        private uint[] results;
        private int resultsRead;
        private readonly HashSet<string> warned = new HashSet<string>();
    }
}
