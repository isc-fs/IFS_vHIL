//
// Execute-never regions of the ARMv7-M default memory map, per board.
// Compiled by Renode at load time and created by the generated script for
// every stm32h733 board:
//     emulation CreateVhilExecuteNever "vhil_xn_ecu" "ecu"
//
// On the chip (Cortex-M7, ARMv7-M), the default memory map marks the
// Peripheral (0x40000000-0x5FFFFFFF), Device (0xA0000000-0xDFFFFFFF) and
// System (0xE0000000-0xFFFFFFFF) regions Execute Never (ARMv7-M ARM DDI0403E
// B3.1, Table B3-1), and it is the map in force while the MPU is off
// (B3.5.1: MPU_CTRL.ENABLE = 0, "the default memory map"). An instruction
// fetch from an XN region is a MemManage fault with CFSR.IACCVIOL, MMFAR not
// valid (B3.2.15, MMFSR), taken on the faulting instruction; with
// SHCSR.MEMFAULTENA clear it escalates to HardFault with HFSR.FORCED
// (B1.5.8). A corrupted function pointer or return address into peripheral
// space ends there.
//
// Renode 1.17 aborts the machine instead (#239): tlib grants every address
// read, write and execute while the MPU is off (arch/arm/helper.c,
// get_phys_addr: `(env->cp15.c1_sys & 1) == 0`), so the fetch reaches
// get_page_addr_code (include/exec-all.h), which finds an I/O page and calls
// cpu_abort ("Trying to execute code outside RAM or ROM"). With the MPU on,
// tlib does apply the default map's XN to privileged background accesses
// (cortexm_check_default_mapping) and raises EXCP_PREFETCH_ABORT, which its
// v7-M exception entry turns into IACCVIOL + MemManage.
//
// While the MPU is off, this puts the default map's XN in front of tlib's
// translation with Renode's external MMU (tlib exec.c tlb_fill,
// EMMU_POS_BEFORE): four windows cover the 4 GB, the XN ones read-write only.
// A fetch from one is refused, and tlib calls the CPU's MMU-fault hook
// (get_external_mmu_phys_addr) before any page is mapped: on its second call
// this raises EXCP_PREFETCH_ABORT (tlib arch/arm/cpu.h) and lets the refusal
// stand, so tlb_fill unwinds to the CPU loop (arch_raise_mmu_fault_exception,
// cpu_loop_exit_restore; a code fetch carries no host return address) and
// the loop takes the exception as tlib's own MPU path would: IACCVIOL, then
// MemManage or its escalation by the NVIC. Data accesses pass, and the PC
// stacked is the fetch's.
//
// With the MPU on, tlib's own check applies, and the external MMU must be out
// of the way: tlb_fill maps a page with the window's permissions over the
// MPU's, so a read-only MPU region would turn writable. MPU_CTRL writes
// (0xE000ED94, through the NVIC's bus hooks, VhilBusHooks.cs) switch it, and
// every reset turns the MPU off (tlib cpu_reset clears c1_sys; the windows,
// past RESET_OFFSET in CPUState, survive).
//
// Limits: a fetch from a reserved range of an executable region (the
// platform's VhilBusError range at 0x24050000, say) is a BusFault (IBUSERR)
// on the chip, and still aborts in Renode: tlib has no instruction-side bus
// fault to raise (EXCP_BUS_FAULT is the precise data one). So does an XN
// fetch with the MPU on through a region that grants execution. vhil/sim.py
// fails a run at once on a machine abort (VhilMonitor.cs).
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.CPU;
using Privilege = Antmicro.Renode.Peripherals.Miscellaneous.ExternalMmuBase.Privilege;

namespace Antmicro.Renode.Testing
{
    public static class VhilExecuteNeverExtensions
    {
        public static void CreateVhilExecuteNever(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilExecuteNever(machine), name);
        }
    }

    public class VhilExecuteNever : IExternal
    {
        public VhilExecuteNever(IMachine machine)
        {
            if(!machine.TryGetByName<TranslationCPU>("sysbus.cpu", out cpu))
            {
                throw new ArgumentException("needs sysbus.cpu");
            }
            cpu.EnableExternalWindowMmu(ExternalMmuPosition.BeforeInternal);
            foreach(var (start, end, executable) in DefaultMap)
            {
                var id = cpu.AcquireExternalMmuWindow(Privilege.All);
                cpu.SetMmuWindowStart(id, start);
                cpu.SetMmuWindowEnd(id, end);
                cpu.SetMmuWindowPrivileges(id, executable ? Privilege.All : Privilege.ReadAndWrite);
            }
            cpu.AddHookOnMmuFault(OnFault);
            cpu.FlushTlb();
            VhilBusHooks.For(machine, "sysbus.nvic").OnWrite(MpuCtrl, value =>
            {
                SetMpuEnabled((value & MpuCtrlEnable) != 0);
                return value;
            });
            machine.MachineReset += _ => SetMpuEnabled(false);
        }

        public void Reset()
        {
        }

        // How many fetches from an XN region raised MemManage (monitor).
        public int Faults => faults;

        private void SetMpuEnabled(bool enabled)
        {
            if(enabled == mpuEnabled)
            {
                return;
            }
            mpuEnabled = enabled;
            cpu.EnableExternalWindowMmu(enabled ? ExternalMmuPosition.None : ExternalMmuPosition.BeforeInternal);
            cpu.FlushTlb();
        }

        private ExternalMmuResult OnFault(ulong address, AccessType type, ulong? window, bool firstTry)
        {
            if(type != AccessType.Execute || window == null)
            {
                // The windows cover the 4 GB and allow every data access.
                cpu.Log(LogLevel.Error, "unexpected external MMU fault: {0} at 0x{1:X8}", type, address);
                return ExternalMmuResult.Fault;
            }
            if(firstTry)
            {
                // No retry: the window stays read-write.
                return ExternalMmuResult.Fault;
            }
            faults++;
            cpu.Log(LogLevel.Info, "instruction fetch from XN 0x{0:X8}: MemManage (IACCVIOL)", address);
            cpu.RaiseException(ExcpPrefetchAbort);
            return ExternalMmuResult.NoFault;
        }

        // ARMv7-M ARM B3.1, Table B3-1: (first, past-the-end, executable).
        private static readonly (ulong, ulong, bool)[] DefaultMap =
        {
            (0x00000000, 0x40000000, true),     // Code, SRAM
            (0x40000000, 0x60000000, false),    // Peripheral: XN
            (0x60000000, 0xA0000000, true),     // RAM (WBWA, WT)
            (0xA0000000, 0x100000000, false),   // Device (shareable, non-shareable), System: XN
        };

        private const long MpuCtrl = 0xD94;          // MPU_CTRL 0xE000ED94, NVIC at 0xE000E000
        private const uint MpuCtrlEnable = 1u << 0;
        private const uint ExcpPrefetchAbort = 3;    // tlib arch/arm/cpu.h EXCP_PREFETCH_ABORT

        private readonly TranslationCPU cpu;
        private bool mpuEnabled;
        private int faults;
    }
}
