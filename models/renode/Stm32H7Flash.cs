//
// The STM32H733's embedded flash controller with its option bytes and sector
// write protection, compiled by Renode at load time and placed by
// platforms/cpus/stm32h733.repl in place of the STM32H7_FlashController that
// Renode's stm32h7.repl puts at the same address (FLASH_R_BASE =
// D1_AHB1PERIPH_BASE + 0x2000 = 0x52002000, stm32h733xx.h:2215).
//
// Renode's model keeps its program/erase path (KEYR/CR/SR/CCR, the write
// buffer, PGSERR/INCERR) and adds the option bytes as RM0468 Rev 3 §4.4 and
// §4.9 describe them:
//
//   non-volatile   The option bytes live in the controller object, not in its
//                  registers, so they survive machine Reset and a virtual
//                  power cycle (Sim.power_cycle, the broker's relay), as flash
//                  does. Factory values from RM0468 Table 18 (§4.4.4 p. 173):
//                  OPTSR 0x179EAAF0 (RDP level 0 = 0xAA), OPTSR2 0, BOOT
//                  0x1FF00800, PRAR 0x000000FF and SCAR 0x800000FF (PCROP and
//                  secure area disabled: start > end), WPSN 0xFF (no sector
//                  protected).
//   _CUR / _PRG    OPTSR_CUR 0x1C, PRAR_CUR1 0x28, SCAR_CUR1 0x30, WPSN_CUR1
//                  0x38, BOOT_CUR 0x40, OPTSR2_CUR 0x70 read the option bytes;
//                  their _PRG twins (0x20, 0x2C, 0x34, 0x3C, 0x44, 0x74) take
//                  writes only while OPTCR.OPTLOCK is clear (§4.4.3, §4.9.7)
//                  and reload from the option bytes at reset ("Values after
//                  reset reflects the current values", §4.9.9), so a staged
//                  change that was never launched is lost.
//   unlock         OPTKEYR 0x08: 0x08192A3B then 0x4C5D6E7F clears OPTLOCK; a
//                  wrong sequence keeps it set until reset (§4.5.1, §4.9.7).
//                  Renode's lock register, unchanged.
//   OPTSTART       OPTCR bit 1 with OPTLOCK clear programs every _PRG value
//                  into the option bytes and the _CUR registers follow
//                  (§4.4.3 "Option byte modification sequence"). No reset:
//                  the flash reloads the option registers itself (§4.4.2 item
//                  3). The change takes no virtual time, so OPT_BUSY (OPTSR
//                  bit 0) always reads 0. With RDP at level 2 (0xCC) any
//                  change is refused with OPTCHANGEERR (OPTSR bit 30), which
//                  OPTCCR.CLR_OPTCHANGEERR (0x24 bit 30) clears.
//   write          A sector whose WRPSn bit is 0 in WPSN_CUR1 can be neither
//   protection     erased nor programmed (§4.5.2). A sector erase (CR1.SER +
//                  START) of a protected sector, or a bank erase (BER + START)
//                  with any sector protected, is rejected: WRPERR (SR1 bit 17)
//                  is set and nothing is erased. A program write into a
//                  protected sector is ignored and flags WRPERR, the flash
//                  word left as it was (§4.7.2). CCR1.CLR_WRPERR clears it.
//
// Not modelled: the PCROP and secure-only areas' own erase/program and read
// protection (factory-disabled, and nothing the firmwares run sets them), the
// RDP level 1 -> 0 regression's mass erase, the option change error rules
// other than RDP level 2, the WRPERRIE interrupt, ECC (ECC_FA1R 0x60 reads 0:
// no ECC error is ever recorded), and bank 2 (the H733 has one 1 MB bank).
// OPTCR.MER (bit 4) is the H743's; on the H733 bits 29:2 are reserved
// (§4.9.7), so it is ignored rather than erasing the bank. CCR1's
// CLR_STRBERR, CLR_RDPERR, CLR_RDSERR and CLR_CRCRDERR clear flags this model
// never raises, so they are accepted and do nothing.
//
// Program writes: the CPU stores into the flash memory directly, and Renode's
// model only watches them (a CPU memory hook while CR1.PG is set) and finds
// out after the store. So the protected sectors are snapshotted when PG goes
// up, and a store into one is undone from the snapshot. Writes from the
// monitor (sysbus WriteDoubleWord, LoadBinary) are not CPU stores and are not
// checked: they stand for the debugger and the provisioning burn.
//
// Test API (monitor): WriteProtectedSectors, the option bytes' sector mask
// (bit N = sector N protected): reading it reads the option bytes, setting it
// burns them as an SWD option-byte write does, _CUR and _PRG at once (the
// system file's write_protect on a board does this before it boots).
// WriteProtectionErrors counts the WRPERRs raised since the board was created.
//
// Offsets and bits from ST's stm32h733xx.h (FLASH_TypeDef :960-987; FLASH_CR_*
// :11106-11171, FLASH_SR_WRPERR :11191, FLASH_CCR_* :11226-11260, FLASH_OPTCR_*
// :11264-11270, FLASH_OPTSR_* :11275-11318, FLASH_OPTCCR :11323, FLASH_PRAR_*,
// FLASH_SCAR_*, FLASH_WPSN_*, FLASH_BOOT_* :11328-11358, FLASH_OPTSR2_*
// :11412-11417; FLASH_BANK1_BASE :2180, FLASH_SECTOR_SIZE :11073).
//
using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Logging.Profiling;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.MTD
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public class STM32H733_FlashController : STM32H7_FlashController
    {
        public STM32H733_FlashController(IMachine machine, MappedMemory flash1, MappedMemory flash2)
            : base(machine, flash1, flash2)
        {
            this.flash = flash1;
            sysbus = machine.GetSystemBus(this);
            // Renode's program-write watcher, which ours runs in front of.
            var watcher = typeof(STM32H7_FlashController).GetMethod(
                "OnMemoryProgramWrite", BindingFlags.Instance | BindingFlags.NonPublic);
            baseProgramWrite = (MemoryAccessHook)Delegate.CreateDelegate(typeof(MemoryAccessHook), this, watcher);
        }

        // Called by the base constructor too, before this one's body: only
        // fields with initialisers are safe here.
        public override void Reset()
        {
            base.Reset();
            optsrPrg = optsr;
            optsr2Prg = optsr2;
            prarPrg = prar;
            scarPrg = scar;
            wpsnPrg = wpsn;
            bootPrg = boot;
            wrpErr = false;
            optChangeErr = false;
            shadow.Clear();
        }

        // -- test API (monitor) ------------------------------------------------

        public uint WriteProtectedSectors
        {
            get => ~wpsn & WpsnMask;
            set
            {
                wpsn = ~value & WpsnMask;
                wpsnPrg = wpsn;
                shadow.Clear();
                this.Log(LogLevel.Info, "Option bytes burned: WPSN 0x{0:X2} (protected sectors 0x{1:X2})",
                         wpsn, WriteProtectedSectors);
            }
        }

        public uint WriteProtectionErrors { get; private set; }

        // -- registers ---------------------------------------------------------

        public override uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case Sr1:
                return base.ReadDoubleWord(offset) | (wrpErr ? SrWrperr : 0u);
            case OptsrCur:
                return optsr | (optChangeErr ? OptsrOptChangeErr : 0u);
            case OptsrPrg:
                return optsrPrg;
            case Optccr:
                return 0;                               // write-only (§4.9.10)
            case PrarCur1:
                return prar;
            case PrarPrg1:
                return prarPrg;
            case ScarCur1:
                return scar;
            case ScarPrg1:
                return scarPrg;
            case WpsnCur1:
                return wpsn;
            case WpsnPrg1:
                return wpsnPrg;
            case BootCur:
                return boot;
            case BootPrg:
                return bootPrg;
            case EccFa1:
                return 0;                               // no ECC model
            case Optsr2Cur:
                return optsr2;
            case Optsr2Prg:
                return optsr2Prg;
            default:
                return base.ReadDoubleWord(offset);
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case Cr1:
                WriteCr1(value);
                return;
            case Ccr1:
                if((value & CcrClrWrperr) != 0)
                {
                    wrpErr = false;
                }
                base.WriteDoubleWord(offset, value & ~CcrNeverRaised);
                return;
            case Optcr:
                var unlocked = (base.ReadDoubleWord(Optcr) & OptcrOptlock) == 0;
                base.WriteDoubleWord(offset, value & ~OptcrMer);
                if(unlocked && (value & OptcrOptstart) != 0)
                {
                    Launch();
                }
                return;
            case Optccr:
                if((value & OptccrClrOptChangeErr) != 0)
                {
                    optChangeErr = false;
                }
                return;
            case OptsrPrg:
            case PrarPrg1:
            case ScarPrg1:
            case WpsnPrg1:
            case BootPrg:
            case Optsr2Prg:
                WriteProgramRegister(offset, value);
                return;
            case OptsrCur:
            case PrarCur1:
            case ScarCur1:
            case WpsnCur1:
            case BootCur:
            case Optsr2Cur:
            case EccFa1:
                return;                                 // read-only
            default:
                base.WriteDoubleWord(offset, value);
                return;
            }
        }

        private void WriteCr1(uint value)
        {
            var before = base.ReadDoubleWord(Cr1);
            if((value & CrStart) != 0)
            {
                var sector = (int)((value >> CrSnbPos) & CrSnbMask);
                if((value & CrBer) != 0 && WriteProtectedSectors != 0)
                {
                    RaiseWrpErr($"bank erase with sectors 0x{WriteProtectedSectors:X2} write-protected");
                    value &= ~CrStart;
                }
                else if((value & CrBer) == 0 && (value & CrSer) != 0 && IsProtected(sector))
                {
                    RaiseWrpErr($"erase of write-protected sector {sector}");
                    value &= ~CrStart;
                }
            }
            base.WriteDoubleWord(Cr1, value);
            // Renode hooks the CPU's stores on PG's rising edge; put ours in
            // front of its watcher.
            var pg = (base.ReadDoubleWord(Cr1) & CrPg) != 0;
            if(pg && (before & CrPg) == 0)
            {
                Snapshot();
                foreach(var cpu in sysbus.GetCPUs().OfType<ICPUWithMemoryAccessHooks>())
                {
                    cpu.SetHookAtMemoryAccess(OnProgramWrite);
                }
            }
        }

        private void OnProgramWrite(ulong pc, MemoryOperation operation, ulong virtualAddress,
                                    ulong physicalAddress, uint width, ulong value)
        {
            if(operation == MemoryOperation.MemoryWrite
               && physicalAddress >= FlashBase && physicalAddress < FlashBase + (ulong)flash.Size)
            {
                var offset = (long)(physicalAddress - FlashBase);
                var sector = (int)(offset / SectorSize);
                if(IsProtected(sector) && shadow.TryGetValue(sector, out var saved))
                {
                    var at = (int)(offset % SectorSize);
                    var count = (int)Math.Min(width, (uint)(SectorSize - at));
                    flash.WriteBytes(offset, saved, at, count);
                    RaiseWrpErr($"program at 0x{physicalAddress:X8} in write-protected sector {sector}");
                    return;
                }
            }
            baseProgramWrite(pc, operation, virtualAddress, physicalAddress, width, value);
        }

        private void WriteProgramRegister(long offset, uint value)
        {
            if((base.ReadDoubleWord(Optcr) & OptcrOptlock) != 0)
            {
                this.Log(LogLevel.Warning, "Write to option register 0x{0:X2} while OPTLOCK is set; ignored", offset);
                return;
            }
            switch(offset)
            {
            case OptsrPrg:
                optsrPrg = (optsrPrg & ~OptsrWritable) | (value & OptsrWritable);
                break;
            case PrarPrg1:
                prarPrg = value & AreaMask;
                break;
            case ScarPrg1:
                scarPrg = value & AreaMask;
                break;
            case WpsnPrg1:
                wpsnPrg = value & WpsnMask;
                break;
            case BootPrg:
                bootPrg = value;
                break;
            case Optsr2Prg:
                optsr2Prg = value & Optsr2Writable;
                break;
            }
        }

        private void Launch()
        {
            var changed = optsrPrg != optsr || optsr2Prg != optsr2 || prarPrg != prar
                          || scarPrg != scar || wpsnPrg != wpsn || bootPrg != boot;
            if(changed && ((optsr >> OptsrRdpPos) & 0xFF) == RdpLevel2)
            {
                optChangeErr = true;
                this.Log(LogLevel.Warning, "Option byte change refused: RDP is at level 2");
                return;
            }
            optsr = optsrPrg;
            optsr2 = optsr2Prg;
            prar = prarPrg;
            scar = scarPrg;
            wpsn = wpsnPrg;
            boot = bootPrg;
            shadow.Clear();
            this.Log(LogLevel.Info, "Option bytes programmed: OPTSR 0x{0:X8} WPSN 0x{1:X2} BOOT 0x{2:X8}",
                     optsr, wpsn, boot);
        }

        private bool IsProtected(int sector) => sector >= 0 && sector < Sectors && (wpsn & (1u << sector)) == 0;

        // Keep a copy of every protected sector to undo a store with; taken
        // when PG goes up, so it is what the flash holds at the time.
        private void Snapshot()
        {
            shadow.Clear();
            for(var sector = 0; sector < Sectors; sector++)
            {
                if(IsProtected(sector))
                {
                    shadow[sector] = flash.ReadBytes((long)sector * SectorSize, SectorSize);
                }
            }
        }

        private void RaiseWrpErr(string what)
        {
            wrpErr = true;
            WriteProtectionErrors++;
            this.Log(LogLevel.Info, "WRPERR: {0} rejected", what);
        }

        private readonly MappedMemory flash;
        private readonly IBusController sysbus;
        private readonly MemoryAccessHook baseProgramWrite;
        private readonly Dictionary<int, byte[]> shadow = new Dictionary<int, byte[]>();

        // The option bytes (non-volatile): RM0468 Table 18 factory values.
        private uint optsr = 0x179EAAF0;
        private uint optsr2 = 0x00000000;
        private uint prar = 0x000000FF;
        private uint scar = 0x800000FF;
        private uint wpsn = 0x000000FF;
        private uint boot = 0x1FF00800;

        // _PRG registers and flags (volatile; reloaded at Reset).
        private uint optsrPrg = 0x179EAAF0;
        private uint optsr2Prg = 0x00000000;
        private uint prarPrg = 0x000000FF;
        private uint scarPrg = 0x800000FF;
        private uint wpsnPrg = 0x000000FF;
        private uint bootPrg = 0x1FF00800;
        private bool wrpErr;
        private bool optChangeErr;

        private const ulong FlashBase = 0x08000000;     // FLASH_BANK1_BASE
        private const int SectorSize = 0x20000;         // FLASH_SECTOR_SIZE
        private const int Sectors = 8;

        private const long Cr1 = 0x0C, Sr1 = 0x10, Ccr1 = 0x14, Optcr = 0x18;
        private const long OptsrCur = 0x1C, OptsrPrg = 0x20, Optccr = 0x24;
        private const long PrarCur1 = 0x28, PrarPrg1 = 0x2C, ScarCur1 = 0x30, ScarPrg1 = 0x34;
        private const long WpsnCur1 = 0x38, WpsnPrg1 = 0x3C, BootCur = 0x40, BootPrg = 0x44;
        private const long EccFa1 = 0x60, Optsr2Cur = 0x70, Optsr2Prg = 0x74;

        private const uint CrPg = 1u << 1, CrSer = 1u << 2, CrBer = 1u << 3, CrStart = 1u << 7;
        private const int CrSnbPos = 8;
        private const uint CrSnbMask = 0x7;
        private const uint SrWrperr = 1u << 17;
        private const uint CcrClrWrperr = 1u << 17;
        // CLR_WRPERR (ours), CLR_STRBERR, CLR_RDPERR, CLR_RDSERR, CLR_CRCRDERR
        // (never raised here): kept from Renode's model, which tags them.
        private const uint CcrNeverRaised = (1u << 17) | (1u << 19) | (1u << 23) | (1u << 24) | (1u << 28);
        private const uint OptcrOptlock = 1u << 0, OptcrOptstart = 1u << 1, OptcrMer = 1u << 4;
        private const uint OptccrClrOptChangeErr = 1u << 30;
        private const uint OptsrOptChangeErr = 1u << 30;
        private const int OptsrRdpPos = 8;
        private const uint RdpLevel2 = 0xCC;
        // OPTSR_PRG rw bits (§4.9.9): BOR_LEV 3:2, IWDG1_SW 4, NRST_STOP_D1 6,
        // NRST_STBY_D1 7, RDP 15:8, IWDG_FZ_STOP 17, IWDG_FZ_SDBY 18,
        // ST_RAM_SIZE 20:19, SECURITY 21, IO_HSLV 29.
        private const uint OptsrWritable = 0x203EFFDC;
        private const uint Optsr2Writable = 0x7;        // TCM_AXI_SHARED 1:0, CPUFREQ_BOOST 2
        private const uint AreaMask = 0x8FFF0FFF;       // DMEP/DMES 31, END 27:16, START 11:0
        private const uint WpsnMask = 0xFF;             // WRPSn 7:0
    }
}
