//
// nRF24L01+ 2.4 GHz transceiver, transmit side, on GPIO: the ECU bit-bangs
// its SPI over plain GPIOs (IFS08-CE-ECU dev 544b651 nrf24.c:49-79, :367-401;
// SPI1's hardware MISO reads stuck high on that board). Compiled by Renode at
// load time (`include @models/renode/Nrf24l01p.cs`). In a platform
// description (vhil/system.py generates it from catalog/models/nrf24l01p.yaml):
//
//     radio: Wireless.Nrf24l01p @ sysbus
//         Miso -> gpioPortA@6
//         Irq -> gpioPortC@4
//     gpioPortB:
//         0 -> radio@0          // CSN, active low
//     gpioPortA:
//         5 -> radio@1          // SCK
//         7 -> radio@2          // MOSI
//     gpioPortC:
//         5 -> radio@3          // CE
//
// Behaviour from the Nordic nRF24L01+ Product Specification v1.0 (2008),
// "PS" below:
//
//   - SPI (PS 8.3, 8.3.1-8.3.2): mode 0, MSB first, every command starts at a
//     CSN falling edge. MOSI is sampled on SCK's rising edge and MISO changes
//     on its falling edge; STATUS is shifted out while the command byte is
//     shifted in. Multi-byte registers go LSByte first. Commands: R_REGISTER,
//     W_REGISTER, R_RX_PAYLOAD (no RX: reads 0), W_TX_PAYLOAD (and _NO_ACK),
//     FLUSH_TX, FLUSH_RX, R_RX_PL_WID (0), NOP. A byte cut short by CSN rising
//     is dropped.
//   - Registers (PS 9.1, Table 28) with their reset values and writable bits;
//     STATUS RX_DR/TX_DS/MAX_RT are write-1-to-clear, FIFO_STATUS, OBSERVE_TX
//     and RPD read-only. RX_ADDR_P0/P1 and TX_ADDR are 5 bytes.
//   - TX (PS 6.1.5, 6.1.7 Table 16, Figure 4): with PWR_UP = 1 and PRIM_RX = 0,
//     a CE high of at least Thce = 10 us with a payload in the TX FIFO sends
//     it. The packet leaves after Tstby2a = 130 us of PLL settling from CE's
//     rising edge plus its time on air, then TX_DS is set and, unless
//     MASK_TX_DS, IRQ goes low (active low, PS 6.1.7, 8.4). A CE pulse under
//     10 us sends nothing and is logged. With CE still high and the FIFO not
//     empty the next payload follows straight away (PS 6.1.5); a payload
//     written with CE high and the FIFO empty (Standby-II) goes after 130 us.
//     A CE high within Tpd2stby = 1.5 ms of PWR_UP (Table 16, crystal start)
//     finds the chip not yet in Standby-I and sends nothing, logged.
//   - Time on air (PS 7.3, 7.10): preamble 1 byte, address SETUP_AW bytes,
//     the 9-bit packet control field (Enhanced ShockBurst only: absent with
//     EN_AA = 0 and ARC = 0, the ShockBurst-compatible format), the payload
//     and 0/1/2 CRC bytes (EN_CRC, CRCO), at RF_DR_LOW/RF_DR_HIGH's 1 Mbps,
//     2 Mbps or 250 kbps. The ECU (EN_AA 0, ARC 0, 1 Mbps, 1-byte CRC, 5-byte
//     address, 32-byte payloads; nrf24.c:204, :216, :222, :234, :288) takes
//     130 + 312 us per packet.
//
// Not modelled, on purpose: the RF channel and any receiver (no ACK, so with
// Enhanced ShockBurst on, a packet still ends in TX_DS, not MAX_RT; logged
// once), RX mode (PRIM_RX = 1 with CE high: logged once, nothing received),
// REUSE_TX_PL and W_ACK_PAYLOAD (logged), the W_REGISTER-only-in-standby
// rule, MISO's high impedance with CSN high (it reads low), and the IRQ
// latency after the packet (PS Appendix A, Tirq; under 10 us).
//
// Power: Reset() is a power-on reset (PS 6.1.2). The radio is powered from
// the ECU's backplane, so the board's power cycle resets it; in Renode a
// watchdog or software reset of the MCU resets every peripheral too, where
// the chip on the car would keep its registers. The ECU re-applies its whole
// configuration at boot (nrf24.c:195-289), so that can't change what a test
// sees.
//
// MISO's timing: the chip drives the next bit Tcd = 60 ns after SCK falls
// (PS 8.3.2, Table 13), and the model does the same, 0.1 us later on a
// timer. It can't drive it from inside the SCK write: Renode 1.17's STM32
// GPIO port rewrites every pin of the port from the levels it read before a
// BSRR write, so a level driven on PA6 while the port is still applying the
// write to PA5 is put back to the old one (STM32_GPIOPort.WriteState).
// Driving on a CSN (port B) edge is immediate. The pins are driven again
// after a machine reset, which clears the ports' inputs (VhilProbe.cs), and
// at every CSN edge.
//
// Test API (monitor, e.g. `sysbus.radio Payloads 0`):
//   Payloads sinceUs   "t_us hex" per transmitted payload, t_us when TX_DS
//                      was set (the packet's end)
//   TxDsCleared sinceUs "set_us cleared_us" per TX_DS the firmware cleared
//                      (write 1 to STATUS): how long it took to see it
//   Transmitted        payloads sent in the run
//   ShortCePulses      CE pulses under 10 us in the run (sent nothing)
//   Status             the STATUS byte
//   Register reg       one register's bytes, hex (LSByte first)
//   TxTimeUs           settle + air time of the next packet, us
// The records and counts survive a machine reset: a power cycle is when a
// test wants to see them.
//
using System;
using System.Collections.Generic;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Wireless
{
    public class Nrf24l01p : IGPIOReceiver
    {
        public Nrf24l01p(IMachine machine)
        {
            this.machine = machine;
            Miso = new GPIO();
            Irq = new GPIO();
            ceTimer = NewTimer("ce");
            ceTimer.LimitReached += OnCeHeld;
            txTimer = NewTimer("tx");
            txTimer.LimitReached += OnPacketSent;
            misoTimer = NewTimer("miso");
            misoTimer.LimitReached += OnMisoValid;
            // The GPIO ports clear their inputs on a machine reset, after the
            // peripherals' Reset (VhilProbe.cs): drive them again then.
            machine.MachineReset += _ => DrivePins();
            Reset();
        }

        public GPIO Miso { get; }
        public GPIO Irq { get; }

        public void Reset()
        {
            lock(sync)
            {
                foreach(var r in ResetValues)
                {
                    regs[r.Key] = (byte[])r.Value.Clone();
                }
                fifo.Clear();
                pending.Clear();
                level = new bool[4];
                csnLow = false;
                bitCount = 0;
                byteIndex = 0;
                current = next = 0;
                state = State.PowerDown;
                ceHigh = false;
                powerUpUs = 0;
                ceTimer.Enabled = false;
                txTimer.Enabled = false;
                misoTimer.Enabled = false;
                sending = null;
                misoLevel = false;
            }
            DrivePins();
        }

        // 0 CSN, 1 SCK, 2 MOSI, 3 CE (catalog/models/nrf24l01p.yaml gpio_in).
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number > 3)
            {
                return;
            }
            lock(sync)
            {
                if(level[number] == value)
                {
                    return;   // not an edge
                }
                level[number] = value;
                switch(number)
                {
                case Csn:
                    if(!value)
                    {
                        BeginCommand();
                    }
                    else
                    {
                        EndCommand();
                    }
                    break;
                case Sck:
                    if(!csnLow)
                    {
                        break;
                    }
                    if(value)
                    {
                        ShiftIn(level[Mosi]);
                    }
                    else
                    {
                        if(bitCount == 0)
                        {
                            current = next;
                        }
                        ScheduleMiso(((current >> (7 - bitCount)) & 1) != 0);
                    }
                    break;
                case Ce:
                    OnCe(value);
                    break;
                }
            }
        }

        // -- test API ----------------------------------------------------------

        public string Payloads(ulong sinceUs = 0)
        {
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var p in payloads)
                {
                    if(p.Item1 >= sinceUs)
                    {
                        sb.Append(p.Item1).Append(' ').Append(Hex(p.Item2)).Append('\n');
                    }
                }
            }
            return sb.ToString();
        }

        public string TxDsCleared(ulong sinceUs = 0)
        {
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var c in cleared)
                {
                    if(c.Item1 >= sinceUs)
                    {
                        sb.Append(c.Item1).Append(' ').Append(c.Item2).Append('\n');
                    }
                }
            }
            return sb.ToString();
        }

        public ulong Transmitted => transmitted;

        public ulong ShortCePulses => shortCe;

        public byte Status
        {
            get
            {
                lock(sync)
                {
                    return StatusByte();
                }
            }
        }

        public string Register(int reg)
        {
            lock(sync)
            {
                return Hex(ReadRegister(reg & 0x1F));
            }
        }

        public double TxTimeUs
        {
            get
            {
                lock(sync)
                {
                    return SettleUs + AirTimeUs(fifo.Count > 0 ? fifo.Peek().Length : MaxPayload);
                }
            }
        }

        // -- SPI -----------------------------------------------------------------

        private void BeginCommand()
        {
            csnLow = true;
            bitCount = 0;
            byteIndex = 0;
            shift = 0;
            command = -1;
            pending.Clear();
            // STATUS goes out while the command byte comes in (PS 8.3.1).
            current = next = StatusByte();
            DriveMiso((current & 0x80) != 0);
            DriveIrq();
        }

        private void EndCommand()
        {
            csnLow = false;
            misoTimer.Enabled = false;
            if(command == WTxPayload || command == WTxPayloadNoAck)
            {
                PushPayload();
            }
            command = -1;
            DriveMiso(false);
            DriveIrq();
        }

        private void ShiftIn(bool bit)
        {
            shift = (byte)((shift << 1) | (bit ? 1 : 0));
            if(++bitCount < 8)
            {
                return;
            }
            bitCount = 0;
            OnByte(shift);
            byteIndex++;
        }

        // A whole byte in: the command, or the data byte at byteIndex - 1.
        private void OnByte(byte b)
        {
            if(byteIndex == 0)
            {
                command = b;
                next = 0;
                if(b < 0x20)                       // R_REGISTER
                {
                    next = ReadByte(b & 0x1F, 0);
                }
                else if(b < 0x40)                  // W_REGISTER
                {
                }
                else if(b == FlushTx)
                {
                    fifo.Clear();
                }
                else if(b == FlushRx || b == Nop || b == WTxPayload || b == WTxPayloadNoAck)
                {
                }
                else if(b == RRxPayload || b == RRxPlWid)
                {
                    LogOnce("RX", "R_RX_PAYLOAD/R_RX_PL_WID: no receiver is modelled, reads 0");
                }
                else
                {
                    this.Log(LogLevel.Warning, "nRF24: command 0x{0:X2} not modelled, ignored", b);
                }
                return;
            }
            var k = byteIndex - 1;
            if(command < 0x20)
            {
                next = ReadByte(command & 0x1F, k + 1);
            }
            else if(command < 0x40)
            {
                WriteByte(command & 0x1F, k, b);
            }
            else if((command == WTxPayload || command == WTxPayloadNoAck) && pending.Count < MaxPayload)
            {
                pending.Add(b);
            }
        }

        private byte ReadByte(int reg, int k)
        {
            var bytes = ReadRegister(reg);
            return k < bytes.Length ? bytes[k] : (byte)0;
        }

        private byte[] ReadRegister(int reg)
        {
            switch(reg)
            {
            case RegStatus:
                return new[] { StatusByte() };
            case RegFifoStatus:
                // TX_FULL (5), TX_EMPTY (4), RX_EMPTY (0): no RX FIFO content.
                return new[] { (byte)((fifo.Count >= FifoDepth ? 0x20 : 0) | (fifo.Count == 0 ? 0x10 : 0) | 0x01) };
            }
            return regs.TryGetValue(reg, out var v) ? (byte[])v.Clone() : new byte[] { 0 };
        }

        private void WriteByte(int reg, int k, byte b)
        {
            if(reg == RegStatus)
            {
                if(k == 0)
                {
                    if((b & regs[RegStatus][0] & TxDs) != 0)
                    {
                        cleared.Add(Tuple.Create((ulong)Math.Round(txDsSetUs), (ulong)Math.Round(NowUs())));
                        if(cleared.Count > MaxRecords)
                        {
                            cleared.RemoveRange(0, cleared.Count - MaxRecords);
                        }
                    }
                    regs[RegStatus][0] &= (byte)~(b & IrqFlags);   // write 1 to clear
                    DriveIrq();
                }
                return;
            }
            if(!WriteMask.TryGetValue(reg, out var mask) || !regs.TryGetValue(reg, out var bytes) || k >= bytes.Length)
            {
                return;   // read-only, reserved, or past the register
            }
            var old = bytes[0];
            bytes[k] = (byte)(b & mask);
            if(reg == RegConfig)
            {
                OnConfig(old, bytes[0]);
            }
        }

        private void PushPayload()
        {
            if(pending.Count == 0)
            {
                return;
            }
            if(fifo.Count >= FifoDepth)
            {
                this.Log(LogLevel.Warning, "nRF24: W_TX_PAYLOAD with the TX FIFO full, dropped");
                return;
            }
            fifo.Enqueue(pending.ToArray());
            pending.Clear();
            if(state == State.StandbyII && ceHigh && sending == null)
            {
                var now = NowUs();
                StartPacket(now + SettleUs, now);   // Standby-II -> TX (PS 6.1.5)
            }
        }

        // -- radio states ---------------------------------------------------------

        private void OnConfig(byte old, byte now)
        {
            var wasUp = (old & PwrUp) != 0;
            var isUp = (now & PwrUp) != 0;
            if(!wasUp && isUp)
            {
                state = State.Starting;
                powerUpUs = NowUs();
            }
            else if(wasUp && !isUp)
            {
                if(sending != null)
                {
                    this.Log(LogLevel.Warning, "nRF24: PWR_UP cleared during a transmission, packet lost");
                }
                state = State.PowerDown;
                ceTimer.Enabled = false;
                txTimer.Enabled = false;
                sending = null;
            }
            DriveIrq();   // MASK_* may have changed
        }

        private void OnCe(bool high)
        {
            var now = NowUs();
            ceHigh = high;
            if(high)
            {
                ceRiseUs = now;
                if(sending == null && (regs[RegConfig][0] & PwrUp) != 0)
                {
                    Arm(ceTimer, ThceUs);   // after NowUs: from this instruction
                }
                return;
            }
            if(ceTimer.Enabled)
            {
                ceTimer.Enabled = false;
                shortCe++;
                this.Log(LogLevel.Warning, "nRF24: CE high for {0:F1} us, under the 10 us Thce minimum " +
                         "(PS Table 16): nothing sent", now - ceRiseUs);
            }
            if(sending == null && state == State.StandbyII)
            {
                state = State.StandbyI;
            }
        }

        // CE has been high for Thce.
        private void OnCeHeld()
        {
            lock(sync)
            {
                ceTimer.Enabled = false;
                if(!ceHigh || sending != null)
                {
                    return;
                }
                var config = regs[RegConfig][0];
                if((config & PwrUp) == 0)
                {
                    return;
                }
                if((config & PrimRx) != 0)
                {
                    LogOnce("PRIM_RX", "RX mode (PRIM_RX = 1, CE high): no receiver is modelled");
                    return;
                }
                if(state == State.Starting && ceRiseUs - powerUpUs < PowerUpUs)
                {
                    this.Log(LogLevel.Warning, "nRF24: CE high {0:F0} us after PWR_UP, before the " +
                             "1.5 ms Tpd2stby (PS Table 16): not in Standby-I, nothing sent", ceRiseUs - powerUpUs);
                    return;
                }
                if(fifo.Count == 0)
                {
                    state = State.StandbyII;   // TX mode with an empty FIFO
                    return;
                }
                StartPacket(ceRiseUs + SettleUs, ceRiseUs + ThceUs);
            }
        }

        // The head payload goes on air at startUs (after the PLL settled).
        // nowUs: the time now. A timer's callback passes its own due time:
        // it runs inside the CPU's execution, where SyncTime is not called.
        private void StartPacket(double startUs, double nowUs)
        {
            sending = fifo.Peek();
            if((regs[RegEnAa][0] & 0x3F) != 0 || (regs[RegSetupRetr][0] & 0x0F) != 0)
            {
                LogOnce("ESB", "Enhanced ShockBurst on (EN_AA/ARC): no receiver acknowledges, " +
                        "the packet still ends in TX_DS (no ACK or MAX_RT modelled)");
            }
            state = State.Tx;
            sendEndUs = startUs + AirTimeUs(sending.Length);
            Arm(txTimer, Math.Max(sendEndUs - nowUs, 0.1));
        }

        private void OnPacketSent()
        {
            lock(sync)
            {
                txTimer.Enabled = false;
                if(sending == null)
                {
                    return;
                }
                if(fifo.Count > 0)
                {
                    fifo.Dequeue();   // sent without auto-ack: off the FIFO
                }
                var now = sendEndUs;
                payloads.Add(Tuple.Create((ulong)Math.Round(now), sending));
                if(payloads.Count > MaxRecords)
                {
                    payloads.RemoveRange(0, payloads.Count - MaxRecords);
                }
                transmitted++;
                sending = null;
                regs[RegStatus][0] |= TxDs;
                txDsSetUs = now;
                DriveIrq();
                if(ceHigh && fifo.Count > 0)
                {
                    StartPacket(now, now);   // still in TX mode: the next one follows
                }
                else
                {
                    state = ceHigh ? State.StandbyII : State.StandbyI;
                }
            }
        }

        // PS 7.3 / 7.10: preamble + address + [PCF] + payload + CRC, at the rate.
        private double AirTimeUs(int payloadBytes)
        {
            var aw = regs[RegSetupAw][0] & 0x03;
            var addressBytes = aw == 0 ? 3 : aw + 2;   // 00 is illegal; 01 3 B, 10 4 B, 11 5 B
            var config = regs[RegConfig][0];
            var crcBytes = (config & EnCrc) == 0 ? 0 : (config & Crco) != 0 ? 2 : 1;
            var esb = (regs[RegEnAa][0] & 0x3F) != 0 || (regs[RegSetupRetr][0] & 0x0F) != 0;
            var bits = 8 * (1 + addressBytes + payloadBytes + crcBytes) + (esb ? 9 : 0);
            var setup = regs[RegRfSetup][0];
            double bps = (setup & RfDrLow) != 0 ? 250e3 : (setup & RfDrHigh) != 0 ? 2e6 : 1e6;
            return bits * 1e6 / bps;
        }

        private byte StatusByte()
        {
            // RX_P_NO = 111 (RX FIFO empty), TX_FULL.
            return (byte)((regs[RegStatus][0] & IrqFlags) | 0x0E | (fifo.Count >= FifoDepth ? 1 : 0));
        }

        // -- pins and time ------------------------------------------------------------

        // The next bit, valid Tcd after SCK's falling edge.
        private void ScheduleMiso(bool value)
        {
            misoPending = value;
            NowUs();
            Arm(misoTimer, TcdUs);
        }

        private void OnMisoValid()
        {
            lock(sync)
            {
                misoTimer.Enabled = false;
                if(csnLow)
                {
                    DriveMiso(misoPending);
                }
            }
        }

        private void DriveMiso(bool value)
        {
            misoLevel = value;
            Drive(Miso, value);
        }

        private void DriveIrq()
        {
            var config = regs[RegConfig][0];
            var active = (regs[RegStatus][0] & IrqFlags & ~config) != 0;   // MASK_* bits 6:4
            Drive(Irq, !active);   // active low
        }

        private void DrivePins()
        {
            lock(sync)
            {
                Drive(Miso, misoLevel);
                DriveIrq();
            }
        }

        // Straight to the port, so a level is put back even when the GPIO
        // object thinks it unchanged (the port may have dropped it).
        private static void Drive(GPIO gpio, bool value)
        {
            gpio.Set(value);
            foreach(var e in gpio.Endpoints)
            {
                e.Receiver.OnGPIO(e.Number, value);
            }
        }

        private double NowUs()
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            return machine.ElapsedVirtualTime.TimeElapsed.TotalMicroseconds;
        }

        private LimitTimer NewTimer(string name)
        {
            return new LimitTimer(machine.ClockSource, TimerHz, this, name, limit: 1,
                                  direction: Direction.Ascending, enabled: false,
                                  workMode: WorkMode.OneShot, eventEnabled: true);
        }

        // Callers on the CPU's MMIO path have called NowUs, which syncs the
        // clock source to the current instruction first.
        private void Arm(LimitTimer timer, double us)
        {
            timer.Enabled = false;
            timer.Value = 0;
            timer.Limit = (ulong)Math.Max(1, Math.Round(us * TimerHz / 1e6));
            timer.Enabled = true;
        }

        private void LogOnce(string key, string text)
        {
            if(logged.Add(key))
            {
                this.Log(LogLevel.Info, "nRF24: " + text);
            }
        }

        private static string Hex(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach(var b in bytes)
            {
                sb.Append(b.ToString("x2"));
            }
            return sb.ToString();
        }

        private enum State { PowerDown, Starting, StandbyI, StandbyII, Tx }

        private readonly IMachine machine;
        private readonly object sync = new object();
        private readonly LimitTimer ceTimer, txTimer, misoTimer;
        private readonly Dictionary<int, byte[]> regs = new Dictionary<int, byte[]>();
        private readonly Queue<byte[]> fifo = new Queue<byte[]>();
        private readonly List<byte> pending = new List<byte>();
        private readonly List<Tuple<ulong, byte[]>> payloads = new List<Tuple<ulong, byte[]>>();
        private readonly List<Tuple<ulong, ulong>> cleared = new List<Tuple<ulong, ulong>>();
        private readonly HashSet<string> logged = new HashSet<string>();
        private bool[] level = new bool[4];
        private bool csnLow, ceHigh, misoLevel, misoPending;
        private int bitCount, byteIndex, command = -1;
        private byte shift, current, next;
        private State state;
        private double powerUpUs, ceRiseUs, sendEndUs, txDsSetUs;
        private byte[] sending;
        private ulong transmitted, shortCe;

        private const int Csn = 0, Sck = 1, Mosi = 2, Ce = 3;
        private const int MaxPayload = 32, FifoDepth = 3;
        // A run's payloads (and TX_DS clears) kept for Payloads and
        // TxDsCleared (25 a second from the ECU: about an hour of it); the
        // oldest go first.
        private const int MaxRecords = 100000;
        private const long TimerHz = 10000000;   // 0.1 us resolution
        private const double ThceUs = 10, SettleUs = 130, PowerUpUs = 1500;   // PS Table 16
        private const double TcdUs = 0.1;   // Tcd 60 ns (PS Table 13), at the timer's resolution

        // PS 8.3.1 Table 20 (commands).
        private const int RRxPayload = 0x61, WTxPayload = 0xA0, WTxPayloadNoAck = 0xB0,
            FlushTx = 0xE1, FlushRx = 0xE2, RRxPlWid = 0x60, Nop = 0xFF;

        // PS 9.1 Table 28 (register map).
        private const int RegConfig = 0x00, RegEnAa = 0x01, RegSetupAw = 0x03, RegSetupRetr = 0x04,
            RegRfSetup = 0x06, RegStatus = 0x07, RegFifoStatus = 0x17;
        private const byte PwrUp = 0x02, PrimRx = 0x01, EnCrc = 0x08, Crco = 0x04;
        private const byte RfDrLow = 0x20, RfDrHigh = 0x08;
        private const byte TxDs = 0x20, IrqFlags = 0x70;

        private static readonly Dictionary<int, byte[]> ResetValues = new Dictionary<int, byte[]>
        {
            { 0x00, new byte[] { 0x08 } },                            // CONFIG
            { 0x01, new byte[] { 0x3F } },                            // EN_AA
            { 0x02, new byte[] { 0x03 } },                            // EN_RXADDR
            { 0x03, new byte[] { 0x03 } },                            // SETUP_AW
            { 0x04, new byte[] { 0x03 } },                            // SETUP_RETR
            { 0x05, new byte[] { 0x02 } },                            // RF_CH
            { 0x06, new byte[] { 0x0E } },                            // RF_SETUP
            { 0x07, new byte[] { 0x0E } },                            // STATUS (flags here)
            { 0x08, new byte[] { 0x00 } },                            // OBSERVE_TX
            { 0x09, new byte[] { 0x00 } },                            // RPD
            { 0x0A, new byte[] { 0xE7, 0xE7, 0xE7, 0xE7, 0xE7 } },    // RX_ADDR_P0
            { 0x0B, new byte[] { 0xC2, 0xC2, 0xC2, 0xC2, 0xC2 } },    // RX_ADDR_P1
            { 0x0C, new byte[] { 0xC3 } },                            // RX_ADDR_P2
            { 0x0D, new byte[] { 0xC4 } },
            { 0x0E, new byte[] { 0xC5 } },
            { 0x0F, new byte[] { 0xC6 } },
            { 0x10, new byte[] { 0xE7, 0xE7, 0xE7, 0xE7, 0xE7 } },    // TX_ADDR
            { 0x11, new byte[] { 0x00 } },                            // RX_PW_P0..P5
            { 0x12, new byte[] { 0x00 } },
            { 0x13, new byte[] { 0x00 } },
            { 0x14, new byte[] { 0x00 } },
            { 0x15, new byte[] { 0x00 } },
            { 0x16, new byte[] { 0x00 } },
            { 0x1C, new byte[] { 0x00 } },                            // DYNPD
            { 0x1D, new byte[] { 0x00 } },                            // FEATURE
        };

        // Writable bits (reserved bits read 0). STATUS is write-1-to-clear
        // (WriteByte); FIFO_STATUS, OBSERVE_TX and RPD are read-only.
        private static readonly Dictionary<int, byte> WriteMask = new Dictionary<int, byte>
        {
            { 0x00, 0x7F }, { 0x01, 0x3F }, { 0x02, 0x3F }, { 0x03, 0x03 }, { 0x04, 0xFF },
            { 0x05, 0x7F }, { 0x06, 0xBF }, { 0x0A, 0xFF }, { 0x0B, 0xFF }, { 0x0C, 0xFF },
            { 0x0D, 0xFF }, { 0x0E, 0xFF }, { 0x0F, 0xFF }, { 0x10, 0xFF }, { 0x11, 0x3F },
            { 0x12, 0x3F }, { 0x13, 0x3F }, { 0x14, 0x3F }, { 0x15, 0x3F }, { 0x16, 0x3F },
            { 0x1C, 0x3F }, { 0x1D, 0x07 },
        };
    }
}
