//
// isoSPI: an LTC6820 bridge on the host's SPI bus, and LTC6811-1 battery
// monitors daisy-chained behind it. Compiled by Renode at load time
// (`include @models/renode/IsoSpi.cs`). A chain in a platform description:
//
//     isospi: SPI.Ltc6820 @ spi1
//     gpioPortB:
//         9 -> isospi@0                 // chip select, active low
//     cells0: SPI.Ltc6811 @ isospi 0    // nearest the bridge
//     cells1: SPI.Ltc6811 @ isospi 1
//     ...
//
// Each Ltc6811 sees only its isoSPI port A and talks to the next one through
// port B, as on a real chain: it forwards every byte downstream, decodes the
// broadcast command itself, and on a read answers its own 8-byte segment
// before relaying what came back from downstream. Breaking the link after a
// chip (BreakDownstream) silences everything beyond it, like a cut cable.
//
// Protocol facts (framing, PEC15, NTC table, mux decode) are ported from
// IFS_HIL's Pico emulator, minus its known quirks (IFS_HIL#135).
// Conversions complete instantly: firmware only waits fixed delays.
//
// Cell source: what each chip converts (SetCell / SetAllCells, SetTemperature
// / SetAllTemperatures, SetAuxRaw) is the battery's state, not the chip's.
// Today tests set it through the monitor; the Simscape battery stacks (M9)
// will drive the same setters every co-simulation step, as a plant on the
// system's port (docs/cosim.md "Cell source"). Nothing else writes it, except
// Reset() restoring the defaults on the AMS's reset: a plant re-applies its
// values on its next step.
//
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;

namespace Antmicro.Renode.Peripherals.SPI
{
    // One device on an isoSPI link, seen from its upstream port.
    public interface IIsoSpiNode
    {
        void Wake();
        void BeginTransaction();
        // One byte in from upstream; the byte this node drives back upstream.
        byte Exchange(byte mosi);
        void EndTransaction();
        IIsoSpiNode Downstream { get; set; }
    }

    // LTC6820: SPI slave on the host side, isoSPI master on the chain side.
    public class Ltc6820 : SimpleContainer<Ltc6811>, ISPIPeripheral, IGPIOReceiver
    {
        public Ltc6820(IMachine machine) : base(machine)
        {
        }

        public override void Register(Ltc6811 peripheral, NumberRegistrationPoint<int> registrationPoint)
        {
            base.Register(peripheral, registrationPoint);
            Relink();
        }

        public override void Unregister(Ltc6811 peripheral)
        {
            base.Unregister(peripheral);
            Relink();
        }

        public override void Reset()
        {
            frameOpen = false;
            bytesInFrame = 0;
            wakes = 0;
        }

        public byte Transmit(byte data)
        {
            if(!frameOpen)
            {
                // No chip select wired: treat the byte as its own frame.
                OpenFrame();
            }
            bytesInFrame++;
            return First?.Exchange(data) ?? (byte)0xFF;
        }

        public void FinishTransmission()
        {
            // Not a frame boundary: STM32H7_SPI calls this at the end of every
            // HAL transfer. The frame is the chip-select window (OnGPIO).
        }

        // Chip select, active low.
        public void OnGPIO(int number, bool value)
        {
            if(number != 0)
            {
                return;
            }
            if(!value)
            {
                OpenFrame();
            }
            else if(frameOpen)
            {
                frameOpen = false;
                if(bytesInFrame == 0)
                {
                    // CS pulse with no clocks: wake the isoSPI port and chain.
                    wakes++;
                    First?.Wake();
                }
                else
                {
                    First?.EndTransaction();
                }
            }
        }

        // -- chain-wide test API (per-chip API lives on each Ltc6811) ----------

        public void SetAllCells(int millivolts)
        {
            foreach(var ic in Chain)
            {
                ic.SetAllCells(millivolts);
            }
        }

        public void SetAllTemperatures(int deciCelsius)
        {
            foreach(var ic in Chain)
            {
                ic.SetAllTemperatures(deciCelsius);
            }
        }

        // Bit i set -> chip i answers 0xFF (relaying downstream data intact),
        // as IFS_HIL's Pico STOP_REPLY.
        public void StopReply(uint mask)
        {
            var i = 0;
            foreach(var ic in Chain)
            {
                ic.Respond = ((mask >> i++) & 1) == 0;
            }
        }

        public void ResumeAll()
        {
            foreach(var ic in Chain)
            {
                ic.Respond = true;
                ic.BreakDownstream = false;
            }
        }

        public string Stats()
        {
            var sb = new StringBuilder();
            sb.AppendLine($"chain={ChainLength} wakes={wakes}");
            var i = 0;
            foreach(var ic in Chain)
            {
                sb.AppendLine($"[{i++}] {ic.Summary()}");
            }
            var totals = new SortedDictionary<ushort, int>();
            foreach(var ic in Chain.Take(1))
            {
                foreach(var kv in ic.CommandCounts)
                {
                    totals[kv.Key] = kv.Value;
                }
            }
            foreach(var kv in totals)
            {
                sb.AppendLine($"0x{kv.Key:X3} {Ltc6811.CommandName(kv.Key)} x{kv.Value}");
            }
            return sb.ToString();
        }

        public int ChainLength => ChildCollection.Count;

        private IEnumerable<Ltc6811> Chain => ChildCollection.OrderBy(kv => kv.Key).Select(kv => kv.Value);

        private Ltc6811 First => Chain.FirstOrDefault();

        private void Relink()
        {
            var chain = Chain.ToList();
            for(var i = 0; i < chain.Count; i++)
            {
                chain[i].Position = i;
                chain[i].Downstream = i + 1 < chain.Count ? chain[i + 1] : null;
            }
        }

        private void OpenFrame()
        {
            frameOpen = true;
            bytesInFrame = 0;
            First?.BeginTransaction();
        }

        private bool frameOpen;
        private int bytesInFrame;
        private int wakes;
    }

    // LTC6811-1 on an isoSPI daisy chain.
    public class Ltc6811 : IPeripheral, IIsoSpiNode
    {
        public Ltc6811(bool writeFarthestFirst = false)
        {
            // The AMS firmware writes segment i to the IC i places from the
            // bridge. Real LTC6811-1 silicon shifts write data the other way,
            // so segment 0 lands on the farthest IC. Off = what the firmware
            // intends (AMS docs/BMS_LTC6811.md); on = silicon.
            this.writeFarthestFirst = writeFarthestFirst;
            Reset();
        }

        public void Reset()
        {
            Array.Copy(new byte[] { 0xF8, 0, 0, 0, 0, 0 }, config, 6);   // CFGR power-on
            Array.Clear(comm, 0, 6);
            for(var c = 0; c < CellsPerIc; c++)
            {
                cells[c] = ToLtc(DefaultCellMillivolts);
                cellResults[c] = 0xFFFF;   // cleared result registers read 0xFFFF
            }
            for(var ch = 0; ch < MuxChannels; ch++)
            {
                aux[ch] = NtcAux(DefaultDeciCelsius);
            }
            gpio1Result = 0xFFFF;
            muxChannel = 0;
            Respond = true;
            BreakDownstream = false;
            CorruptPec = false;
            corruptNext = 0;
            corrupting = false;
            commandCounts.Clear();
            badCommandPec = badWritePec = wakes = adcvWhileDischarging = 0;
            rx.Clear();
            relayed.Clear();
            reply = null;
            command = null;
        }

        public IIsoSpiNode Downstream { get; set; }

        // Hops from the bridge (0 = nearest); set when the chain is linked.
        public int Position { get; set; }

        // -- isoSPI port A ----------------------------------------------------

        public void Wake()
        {
            wakes++;
            if(!BreakDownstream)
            {
                Downstream?.Wake();
            }
        }

        public void BeginTransaction()
        {
            rx.Clear();
            relayed.Clear();
            reply = null;
            command = null;
            corrupting = false;
            if(!BreakDownstream)
            {
                Downstream?.BeginTransaction();
            }
        }

        public byte Exchange(byte mosi)
        {
            var index = rx.Count;
            rx.Add(mosi);
            // Port B: everything goes on downstream; collect what comes back.
            var fromDownstream = (byte)0xFF;
            if(!BreakDownstream && Downstream != null)
            {
                fromDownstream = Downstream.Exchange(mosi);
            }
            if(index == 3)
            {
                DecodeCommand();
            }
            if(index < 4)
            {
                return 0xFF;
            }
            var p = index - 4;
            relayed.Add(fromDownstream);
            if(reply == null)
            {
                return 0xFF;
            }
            // Own segment first, then downstream's stream delayed by one segment.
            if(p < 8)
            {
                if(!Respond)
                {
                    return 0xFF;
                }
                if(p == 7 && (CorruptPec || corruptNext > 0))
                {
                    corrupting = true;
                    return (byte)(reply[p] ^ 0x01);   // one PEC bit flipped
                }
                return reply[p];
            }
            return relayed[p - 8];
        }

        public void EndTransaction()
        {
            if(!BreakDownstream)
            {
                Downstream?.EndTransaction();
            }
            if(corrupting && corruptNext > 0)
            {
                corruptNext--;
            }
            if(command == null)
            {
                return;
            }
            switch(command.Value)
            {
                case WRCFGA:
                    TakeSegment(config);
                    break;
                case WRCOMM:
                    TakeSegment(comm);
                    break;
                case STCOMM:
                    LatchMuxFromComm();
                    break;
            }
        }

        // -- per-chip test API (monitor: `sysbus.spi1.isospi.cells3 SetCell 2 3600`)

        public bool Respond { get; set; }

        // Cut the isoSPI link to the next chip: nothing beyond it hears
        // commands or answers.
        public bool BreakDownstream { get; set; }

        // Flip a bit of this chip's reply PEC, so the host rejects its segment
        // while every other chip's data stays clean: every read while set, or
        // just the next n reads (EMI that clears on a re-read).
        public bool CorruptPec { get; set; }

        public void CorruptNextReplies(int n)
        {
            corruptNext = Math.Max(0, n);
        }

        public void SetCell(int cell, int millivolts)
        {
            CheckRange(cell, CellsPerIc, "cell");
            cells[cell] = ToLtc(millivolts);
        }

        public void SetAllCells(int millivolts)
        {
            for(var c = 0; c < CellsPerIc; c++)
            {
                cells[c] = ToLtc(millivolts);
            }
        }

        // Open (or reconnect) cell-sense conductor C<conductor>, 0..12: C0 is
        // the bottom of cell 1, C<n> the top of cell n. A broken wire is on
        // the battery module, so it survives the AMS's resets.
        public void OpenWire(int conductor, bool open)
        {
            CheckRange(conductor, CellsPerIc + 1, "conductor");
            openWires[conductor] = open;
        }

        public int CellMillivolts(int cell)
        {
            CheckRange(cell, CellsPerIc, "cell");
            return cells[cell] / 10;
        }

        // Temperature behind one ADG731 mux channel, tenths of a degree C.
        public void SetTemperature(int muxChannel, int deciCelsius)
        {
            CheckRange(muxChannel, MuxChannels, "muxChannel");
            aux[muxChannel] = NtcAux(deciCelsius);
        }

        public void SetAllTemperatures(int deciCelsius)
        {
            var v = NtcAux(deciCelsius);
            for(var ch = 0; ch < MuxChannels; ch++)
            {
                aux[ch] = v;
            }
        }

        // Raw GPIO1 voltage behind one mux channel, 100 uV units (0 = shorted
        // sensor; >= 28000 reads as open to the AMS).
        public void SetAuxRaw(int muxChannel, int raw100uV)
        {
            CheckRange(muxChannel, MuxChannels, "muxChannel");
            aux[muxChannel] = (ushort)Math.Max(0, Math.Min(0xFFFF, raw100uV));
        }

        public int MuxChannel => muxChannel;

        // DCC (discharge) bits last written, cells 1..12.
        public int DischargeBits => config[4] | ((config[5] & 0x0F) << 8);

        // Cell conversions started with a discharge switch on: the host is
        // meant to quiesce balancing first, or the reading includes the
        // balance current's IR drop.
        public int AdcvWhileDischarging => adcvWhileDischarging;

        public int CommandCount(int opcode)
        {
            return commandCounts.TryGetValue((ushort)opcode, out var n) ? n : 0;
        }

        public IReadOnlyDictionary<ushort, int> CommandCounts => commandCounts;

        public string Summary()
        {
            return $"respond={Respond} breakDownstream={BreakDownstream} wakes={wakes} " +
                   $"badCommandPec={badCommandPec} badWritePec={badWritePec} " +
                   $"mux={muxChannel} dcc=0x{DischargeBits:X3}";
        }

        public static string CommandName(ushort op)
        {
            switch(op)
            {
                case WRCFGA: return "WRCFGA";
                case RDCFGA: return "RDCFGA";
                case RDCVA: return "RDCVA";
                case RDCVB: return "RDCVB";
                case RDCVC: return "RDCVC";
                case RDCVD: return "RDCVD";
                case RDAUXA: return "RDAUXA";
                case RDAUXB: return "RDAUXB";
                case WRCOMM: return "WRCOMM";
                case RDCOMM: return "RDCOMM";
                case STCOMM: return "STCOMM";
            }
            if(IsAdcv(op)) return "ADCV";
            if(IsAdow(op)) return "ADOW";
            if(IsAdax(op)) return "ADAX";
            return "?";
        }

        // -- command handling -------------------------------------------------

        private void DecodeCommand()
        {
            var opcode = (ushort)(((rx[0] & 0x07) << 8) | rx[1]);
            var pec = (ushort)((rx[2] << 8) | rx[3]);
            if(Pec15(rx, 0, 2) != pec)
            {
                badCommandPec++;
                return;   // a real LTC6811 ignores a command with a bad PEC
            }
            command = opcode;
            commandCounts[opcode] = CommandCount(opcode) + 1;

            if(IsAdcv(opcode))
            {
                if(DischargeBits != 0)
                {
                    adcvWhileDischarging++;   // the conversion sees the balance load
                }
                Array.Copy(cells, cellResults, CellsPerIc);
                return;
            }
            if(IsAdow(opcode))
            {
                ConvertOpenWire(pullUp: (opcode & AdowPup) != 0);
                return;
            }
            if(IsAdax(opcode))
            {
                gpio1Result = aux[muxChannel];
                return;
            }
            switch(opcode)
            {
                case RDCFGA: reply = Segment(config); break;
                case RDCVA: reply = Segment(Words(cellResults, 0)); break;
                case RDCVB: reply = Segment(Words(cellResults, 3)); break;
                case RDCVC: reply = Segment(Words(cellResults, 6)); break;
                case RDCVD: reply = Segment(Words(cellResults, 9)); break;
                case RDAUXA: reply = Segment(Words(new[] { gpio1Result, (ushort)0, (ushort)0 }, 0)); break;
                case RDAUXB: reply = Segment(Words(new[] { (ushort)0, (ushort)0, Ref2 }, 0)); break;
                case RDCOMM: reply = Segment(comm); break;
            }
        }

        // This chip's write segment. Every chip sees the whole stream; it keeps
        // segment Position (nearest-first, as the AMS firmware intends) or
        // segment <chips downstream> (farthest-first, as silicon shifts it).
        private void TakeSegment(byte[] register)
        {
            var segment = writeFarthestFirst ? DownstreamDepth() : Position;
            var start = 4 + segment * 8;
            if(rx.Count < start + 8)
            {
                return;
            }
            var data = rx.GetRange(start, 6);
            var pec = (ushort)((rx[start + 6] << 8) | rx[start + 7]);
            if(Pec15(data, 0, 6) != pec)
            {
                badWritePec++;
                return;
            }
            data.CopyTo(register);
        }

        private int DownstreamDepth()
        {
            var depth = 0;
            for(var node = Downstream; node != null; node = node.Downstream)
            {
                depth++;
            }
            return depth;
        }

        // The ADG731 address shifted out through COMM: a START (ICOM 0b1000)
        // then data, D7..D4 in COMM0 and D3..D0 in COMM1. The ADG731 takes the
        // low 5 bits (EN = CS = 0). A no-op COMM (ICOM 0xF) leaves it alone.
        private void LatchMuxFromComm()
        {
            if((comm[0] >> 4) != 0x8)
            {
                return;
            }
            muxChannel = (((comm[0] & 0x0F) << 4) | (comm[1] >> 4)) & 0x1F;
        }

        private static byte[] Segment(byte[] data6)
        {
            var pec = Pec15(data6, 0, 6);
            return data6.Concat(new[] { (byte)(pec >> 8), (byte)pec }).ToArray();
        }

        // ADOW: the cells converted with a test current on every C pin, up
        // (PUP = 1) or down. A connected conductor holds its pin; an open one
        // follows the current to its neighbour, C<n+1> pulled up or C<n-1>
        // pulled down, so the cell below it and the one above see the sum or
        // nothing, the ADC saturating. That is the datasheet's open-wire
        // signature ("Open Wire Check (ADOW Command)"): an interior C<n> gives
        // CELL<n+1>(PU) - CELL<n+1>(PD) < -400 mV, C0 gives CELL1(PU) = 0 and
        // the top conductor CELL<N>(PD) = 0. A simplification of the physics,
        // exact at the endpoints where silicon may read a few mV.
        private void ConvertOpenWire(bool pullUp)
        {
            var pin = new int[CellsPerIc + 1];        // C0..C12 potentials, 100 uV
            for(var n = 1; n <= CellsPerIc; n++)
            {
                pin[n] = pin[n - 1] + cells[n - 1];
            }
            var level = (int[])pin.Clone();
            for(var n = 0; n <= CellsPerIc; n++)
            {
                if(!openWires[n])
                {
                    continue;
                }
                if(pullUp && n < CellsPerIc)
                {
                    level[n] = pin[n + 1];
                }
                else if(!pullUp && n > 0)
                {
                    level[n] = pin[n - 1];
                }
            }
            for(var c = 0; c < CellsPerIc; c++)
            {
                cellResults[c] = (ushort)Math.Max(0, Math.Min(0xFFFF, level[c + 1] - level[c]));
            }
        }

        // Fixed bits per the LTC6811 command table; MD, DCP/PUP and CH/CHG
        // vary (AMS: ADCV 0x360, ADOW 0x368/0x328, ADAX 0x561).
        private static bool IsAdcv(ushort op) => (op & 0x668) == 0x260;
        private static bool IsAdow(ushort op) => (op & 0x628) == 0x228;
        private const ushort AdowPup = 0x40;          // ADOW bit 6 (AMS 0x368 up, 0x328 down)
        private static bool IsAdax(ushort op) => (op & 0x678) == 0x460;

        private static byte[] Words(ushort[] values, int first)
        {
            var b = new byte[6];
            for(var k = 0; k < 3; k++)
            {
                var v = first + k < values.Length ? values[first + k] : (ushort)0;
                b[2 * k] = (byte)v;              // little-endian, 100 uV LSB
                b[2 * k + 1] = (byte)(v >> 8);
            }
            return b;
        }

        private static ushort ToLtc(int millivolts)
        {
            return (ushort)Math.Max(0, Math.Min(0xFFFF, millivolts * 10));
        }

        // GPIO1 voltage of a 10k B3950 NTC behind a 6.8k pull-up to VREF2,
        // linearly interpolated, 100 uV units: the table the AMS decodes with.
        private static ushort NtcAux(int deciCelsius)
        {
            var t = Math.Max(NtcMinC * 10, Math.Min(NtcMaxC * 10, deciCelsius)) - NtcMinC * 10;
            var index = t / 10;
            if(index >= NtcAux100uV.Length - 1)
            {
                return NtcAux100uV[NtcAux100uV.Length - 1];
            }
            var a = NtcAux100uV[index];
            var b = NtcAux100uV[index + 1];
            return (ushort)(a + (b - a) * (t % 10) / 10);
        }

        // PEC15: polynomial 0x4599, seed 16, result shifted left by one.
        private static ushort Pec15(IList<byte> data, int offset, int count)
        {
            var rem = 16;
            for(var i = 0; i < count; i++)
            {
                var address = ((rem >> 7) ^ data[offset + i]) & 0xFF;
                rem = ((rem << 8) ^ PecTable[address]) & 0xFFFF;
            }
            return (ushort)((rem << 1) & 0xFFFF);
        }

        private static int[] BuildPecTable()
        {
            var table = new int[256];
            for(var i = 0; i < 256; i++)
            {
                var rem = i << 7;
                for(var bit = 8; bit > 0; bit--)
                {
                    rem = (rem & 0x4000) != 0 ? ((rem << 1) ^ 0x4599) : (rem << 1);
                }
                table[i] = rem & 0xFFFF;
            }
            return table;
        }

        private static void CheckRange(int value, int limit, string what)
        {
            if(value < 0 || value >= limit)
            {
                throw new ArgumentException($"{what} must be 0..{limit - 1}");
            }
        }

        private const int CellsPerIc = 12;
        private const int MuxChannels = 32;
        private const int DefaultCellMillivolts = 3700;
        private const int DefaultDeciCelsius = 250;
        private const ushort Ref2 = 30000;            // 3.000 V
        private const int NtcMinC = -55;
        private const int NtcMaxC = 125;

        private const ushort WRCFGA = 0x001;
        private const ushort RDCFGA = 0x002;
        private const ushort RDCVA = 0x004;
        private const ushort RDCVB = 0x006;
        private const ushort RDCVC = 0x008;
        private const ushort RDCVD = 0x00A;
        private const ushort RDAUXA = 0x00C;
        private const ushort RDAUXB = 0x00E;
        private const ushort WRCOMM = 0x721;
        private const ushort RDCOMM = 0x722;
        private const ushort STCOMM = 0x723;

        private static readonly int[] PecTable = BuildPecTable();

        // index = temp_C + 55; value = V_aux in 100 uV units (VREF2 = 3000 mV,
        // 6.8k pull-up, Fenghua CMFB103F3950 R-T table).
        private static readonly ushort[] NtcAux100uV = {
            29654, 29637, 29618, 29598, 29577, 29554, 29531, 29506, 29479, 29451,
            29422, 29391, 29358, 29323, 29286, 29247, 29207, 29164, 29118, 29071,
            29021, 28968, 28912, 28854, 28793, 28728, 28661, 28590, 28515, 28438,
            28356, 28270, 28181, 28088, 27990, 27888, 27781, 27670, 27554, 27433,
            27307, 27177, 27040, 26899, 26752, 26600, 26442, 26278, 26109, 25934,
            25753, 25566, 25373, 25174, 24969, 24758, 24541, 24318, 24090, 23855,
            23615, 23369, 23118, 22861, 22599, 22326, 22060, 21783, 21501, 21216,
            20926, 20632, 20335, 20034, 19730, 19423, 19114, 18802, 18488, 18173,
            17857, 17539, 17221, 16902, 16583, 16264, 15947, 15630, 15313, 14998,
            14685, 14374, 14065, 13758, 13454, 13153, 12854, 12560, 12267, 11980,
            11696, 11416, 11139, 10868, 10601, 10362, 10078,  9824,  9575,  9329,
             9090,  8856,  8625,  8399,  8177,  7963,  7751,  7543,  7343,  7145,
             6952,  6765,  6581,  6403,  6229,  6059,  5895,  5732,  5575,  5422,
             5273,  5128,  4988,  4852,  4718,  4589,  4462,  4340,  4220,  4105,
             3993,  3883,  3776,  3671,  3572,  3475,  3379,  3288,  3197,  3112,
             3027,  2944,  2865,  2789,  2713,  2643,  2570,  2503,  2436,  2373,
             2309,  2249,  2188,  2131,  2074,  2020,  1966,  1916,  1866,  1819,
             1773,  1726,  1682,  1639,  1600,  1560,  1520,  1480,  1445,  1409,
             1372,
        };

        private readonly bool writeFarthestFirst;
        private readonly byte[] config = new byte[6];
        private readonly byte[] comm = new byte[6];
        private readonly ushort[] cells = new ushort[CellsPerIc];
        private readonly bool[] openWires = new bool[CellsPerIc + 1];
        private readonly ushort[] cellResults = new ushort[CellsPerIc];
        private readonly ushort[] aux = new ushort[MuxChannels];
        private readonly List<byte> rx = new List<byte>();
        private readonly List<byte> relayed = new List<byte>();
        private readonly SortedDictionary<ushort, int> commandCounts = new SortedDictionary<ushort, int>();
        private int corruptNext;
        private bool corrupting;
        private ushort gpio1Result;
        private int muxChannel;
        private byte[] reply;
        private ushort? command;
        private int badCommandPec;
        private int adcvWhileDischarging;
        private int badWritePec;
        private int wakes;
    }
}
