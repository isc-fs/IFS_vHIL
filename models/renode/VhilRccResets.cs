//
// The STM32H733's peripheral resets: RCC_AHB3RSTR .. RCC_APB4RSTR. Compiled by
// Renode at load time and created by the generated script for every stm32h733
// board:
//     emulation CreateVhilRccResets "vhil_rcc_ecu" "ecu"
//
// Writing 1 to a reset bit holds its peripheral in reset; writing 0 releases
// it with every register at its reset value (RM0468 Rev 3 §8.7.27-8.7.35, "1:
// resets the <X> block"). The CAN bootloader's HAL_DeInit pulses every bit
// before it jumps to the application (stm32h7xx_hal.c HAL_DeInit), so the
// application starts with its peripherals as the chip resets them.
//
// Renode's STM32H7_RCC (sealed) models these registers with the STM32H743's
// layout: it resets only the peripherals registered with it in stm32h7.repl,
// none of the ones stm32h733.repl adds or replaces (fdcan*_h7, timer23,
// usart10, spi1, sdmmc1, adc3_h73x), and it treats the H72x/H73x-only bits
// (TIM23/24, USART10, UART9, I2C5, DTS, OCTOSPI2, FMAC, CORDIC, OTFDEC,
// IOMNGR; DFSDM1 at bit 30) as reserved. This owns the nine registers through
// the RCC's bus hooks (VhilRccHooks.cs): it holds what was written, reads it
// back, resets the mapped peripheral models on each edge of their bit (so
// writes made while the bit is held are lost, as on silicon), and passes 0 to
// the RCC model underneath, so its own resets never fire.
//
// Bits: RM0468 Rev 3 register descriptions (pages below) and ST's
// stm32h733xx.h RCC_<reg>_<X>RST_Pos (ams@main Drivers/CMSIS/.../
// stm32h733xx.h:16140-16450). A defined bit with no model here (MDMA, CRYP,
// SPI2, I2C5, ...) is accepted and held: there is nothing to reset. A write to
// a reserved bit, or to AHB3RSTR.CPURST (a CPU reset, not modelled), is logged
// as an unhandled RCC write, so the peripheral guard flags it.
//
// GPIO: a port reset puts its pins back in their reset mode but cannot change
// a level something outside the chip drives onto them. Renode's
// STM32_GPIOPort.Reset() clears every pin's state, inputs included, so for
// the pins in input or analog mode the level is put back after the reset.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.GPIOPort;

namespace Antmicro.Renode.Testing
{
    public static class VhilRccResetsExtensions
    {
        public static void CreateVhilRccResets(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilRccResets(machine), name);
        }
    }

    public class VhilRccResets : IExternal
    {
        public VhilRccResets(IMachine machine)
        {
            hooks = VhilRccHooks.For(machine);
            foreach(var register in Registers)
            {
                var offset = register.Key;
                held[offset] = 0;
                var bits = new Dictionary<int, IPeripheral[]>();
                foreach(var bit in register.Value.Bits)
                {
                    bits[bit.Key] = bit.Value
                        .Select(name => machine.TryGetByName<IPeripheral>("sysbus." + name, out var p) ? p : null)
                        .Where(p => p != null).ToArray();
                    foreach(var name in bit.Value)
                    {
                        if(machine.TryGetByName<IPeripheral>("sysbus." + name, out var p))
                        {
                            names[p] = name;
                            resets[name] = 0;
                        }
                    }
                }
                peripherals[offset] = bits;
                hooks.OnRead(offset, _ => held[offset]);
                hooks.OnWrite(offset, value =>
                {
                    Write(offset, value);
                    return 0;
                });
            }
            // A system reset clears the registers (reset value 0) and resets
            // every peripheral itself.
            machine.MachineReset += _ =>
            {
                foreach(var offset in Registers.Keys)
                {
                    held[offset] = 0;
                }
            };
        }

        public void Reset()
        {
        }

        // -- test API (monitor) ------------------------------------------------

        // Times a reset bit has reset this peripheral (each edge counts once);
        // -1 for a name this board doesn't have.
        public int ResetsOf(string name)
        {
            return resets.TryGetValue(name, out var n) ? n : -1;
        }

        public uint Held(long offset)
        {
            return held.TryGetValue(offset, out var v) ? v : 0;
        }

        private void Write(long offset, uint value)
        {
            var register = Registers[offset];
            var unhandled = value & ~register.Defined;
            if(unhandled != 0)
            {
                // The peripheral guard's pattern (vhil/peripheral_guard.py).
                hooks.Rcc.Log(LogLevel.Warning,
                    "Unhandled write to offset 0x{0:X}. Unhandled bits: 0x{1:X8} ({2}) when writing value 0x{3:X8}",
                    offset, unhandled, register.Name, value);
            }
            value &= register.Defined;
            var changed = value ^ held[offset];
            held[offset] = value;
            for(var bit = 0; bit < 32; bit++)
            {
                if((changed & (1u << bit)) != 0 && peripherals[offset].TryGetValue(bit, out var targets))
                {
                    foreach(var p in targets)
                    {
                        ResetPeripheral(p);
                    }
                }
            }
        }

        private void ResetPeripheral(IPeripheral peripheral)
        {
            resets[names[peripheral]]++;
            hooks.Rcc.Log(LogLevel.Debug, "{0} reset from its RSTR bit", names[peripheral]);
            if(!(peripheral is STM32_GPIOPort gpio))
            {
                peripheral.Reset();
                return;
            }
            var moder = gpio.ReadDoubleWord(GpioModer);
            var idr = gpio.ReadDoubleWord(GpioIdr);
            gpio.Reset();
            for(var pin = 0; pin < 16; pin++)
            {
                var mode = (moder >> (2 * pin)) & 0x3;
                if((mode == GpioModeInput || mode == GpioModeAnalog) && (idr & (1u << pin)) != 0)
                {
                    gpio.OnGPIO(pin, true);
                }
            }
        }

        private readonly VhilRccHooks hooks;
        private readonly Dictionary<long, uint> held = new Dictionary<long, uint>();
        private readonly Dictionary<long, Dictionary<int, IPeripheral[]>> peripherals =
            new Dictionary<long, Dictionary<int, IPeripheral[]>>();
        private readonly Dictionary<IPeripheral, string> names = new Dictionary<IPeripheral, string>();
        private readonly Dictionary<string, int> resets = new Dictionary<string, int>();

        private const long GpioModer = 0x00, GpioIdr = 0x10;
        private const uint GpioModeInput = 0, GpioModeAnalog = 3;

        private class Register
        {
            public string Name;
            public uint Defined;                                   // RM0468: every rw bit
            public Dictionary<int, string[]> Bits;                 // bit -> repl peripheral names
        }

        // Defined = the bits RM0468 documents (CPURST aside), which are also
        // what the HAL's __HAL_RCC_<bus>_FORCE_RESET writes on the H72x/H73x
        // (stm32h7xx_hal_rcc.h, STM32H7_DEV_ID 0x483). Peripherals by their
        // stm32h733.repl / stm32h7.repl name; a bit without one only holds.
        private static readonly Dictionary<long, Register> Registers = new Dictionary<long, Register>
        {
            // §8.7.27 p. 409-410: MDMA 0, DMA2D 4, FMC 12, OCTOSPI1 14,
            // SDMMC1 16, OCTOSPI2 19, IOMNGR 21, OTFD1 22, OTFD2 23 (CPURST 31).
            [0x7C] = new Register { Name = "AHB3RSTR", Defined = 0x00E95011, Bits = new Dictionary<int, string[]>
            {
                [4] = new[] { "dma2d" }, [12] = new[] { "fmc" },
                [14] = new[] { "qspi" },             // OCTOSPI1_R_BASE 0x52005000
                [16] = new[] { "sdmmc1" },
            }},
            // §8.7.28 p. 411: DMA1 0, DMA2 1, ADC12 5, ETH1MAC 15, USB1OTG 25.
            [0x80] = new Register { Name = "AHB1RSTR", Defined = 0x02008023, Bits = new Dictionary<int, string[]>
            {
                [0] = new[] { "dma1" }, [1] = new[] { "dma2" }, [5] = new[] { "adcM1S2" },
                [15] = new[] { "ethernet" }, [25] = new[] { "usb1" },
            }},
            // §8.7.29 p. 412-413: DCMI_PSSI 0, CRYP 4, HASH 5, RNG 6, SDMMC2 9,
            // FMAC 16, CORDIC 17.
            [0x84] = new Register { Name = "AHB2RSTR", Defined = 0x00030271, Bits = new Dictionary<int, string[]>
            {
                [6] = new[] { "rng" },
            }},
            // §8.7.30 p. 414-415: GPIOA..H 0-7, GPIOJ 9, GPIOK 10 (no GPIOI on
            // the H733: bit 8 reserved), CRC 19, BDMA 21, ADC3 24, HSEM 25.
            [0x88] = new Register { Name = "AHB4RSTR", Defined = 0x032806FF, Bits = new Dictionary<int, string[]>
            {
                [0] = new[] { "gpioPortA" }, [1] = new[] { "gpioPortB" }, [2] = new[] { "gpioPortC" },
                [3] = new[] { "gpioPortD" }, [4] = new[] { "gpioPortE" }, [5] = new[] { "gpioPortF" },
                [6] = new[] { "gpioPortG" }, [7] = new[] { "gpioPortH" }, [9] = new[] { "gpioPortJ" },
                [10] = new[] { "gpioPortK" }, [19] = new[] { "crc" }, [21] = new[] { "bdma" },
                [24] = new[] { "adc3_h73x" }, [25] = new[] { "hsem" },
            }},
            // §8.7.31 p. 416: LTDC 3.
            [0x8C] = new Register { Name = "APB3RSTR", Defined = 0x00000008, Bits = new Dictionary<int, string[]>
            {
                [3] = new[] { "ltdc" },
            }},
            // §8.7.32 p. 417-419: TIM2..7 0-5, TIM12..14 6-8, LPTIM1 9, SPI2 14,
            // SPI3 15, SPDIFRX 16, USART2 17, USART3 18, UART4 19, UART5 20,
            // I2C1..3 21-23, I2C5 25, CEC 27, DAC12 29, UART7 30, UART8 31.
            [0x90] = new Register { Name = "APB1LRSTR", Defined = 0xEAFFC3FF, Bits = new Dictionary<int, string[]>
            {
                [0] = new[] { "timer2" }, [1] = new[] { "timer3" }, [2] = new[] { "timer4" },
                [3] = new[] { "timer5" }, [4] = new[] { "timer6" }, [5] = new[] { "timer7" },
                [6] = new[] { "timer12" }, [7] = new[] { "timer13" }, [8] = new[] { "timer14" },
                [9] = new[] { "lptimer1" }, [17] = new[] { "usart2" }, [18] = new[] { "usart3" },
                [19] = new[] { "uart4" }, [20] = new[] { "uart5" }, [21] = new[] { "i2c1" },
                [22] = new[] { "i2c2_h7" }, [23] = new[] { "i2c3" }, [30] = new[] { "uart7" },
                [31] = new[] { "uart8" },
            }},
            // §8.7.33 p. 420-421: CRS 1, SWPMI 2, OPAMP 4, MDIOS 5, FDCAN 8 (the
            // whole FDCAN block: all three instances), TIM23 24, TIM24 25.
            [0x94] = new Register { Name = "APB1HRSTR", Defined = 0x03000136, Bits = new Dictionary<int, string[]>
            {
                [8] = new[] { "fdcan1_h7", "fdcan2_h7", "fdcan3_h7" },
                [24] = new[] { "timer23" },
            }},
            // §8.7.34 p. 422-423: TIM1 0, TIM8 1, USART1 4, USART6 5, UART9 6,
            // USART10 7, SPI1 12, SPI4 13, TIM15..17 16-18, SPI5 20, SAI1 22,
            // DFSDM1 30.
            [0x98] = new Register { Name = "APB2RSTR", Defined = 0x405730F3, Bits = new Dictionary<int, string[]>
            {
                [0] = new[] { "timer1" }, [1] = new[] { "timer8" }, [4] = new[] { "usart1" },
                [5] = new[] { "usart6" }, [7] = new[] { "usart10" }, [12] = new[] { "spi1" },
                [13] = new[] { "spi4" }, [16] = new[] { "timer15" }, [17] = new[] { "timer16" },
                [18] = new[] { "timer17" },
            }},
            // §8.7.35 p. 424-425: SYSCFG 1, LPUART1 3, SPI6 5, I2C4 7,
            // LPTIM2..5 9-12, COMP12 14, VREF 15, SAI4 21, DTS 26.
            [0x9C] = new Register { Name = "APB4RSTR", Defined = 0x0420DEAA, Bits = new Dictionary<int, string[]>
            {
                [1] = new[] { "syscfg" }, [3] = new[] { "lpuart1" }, [7] = new[] { "i2c4" },
                [9] = new[] { "lptimer2" }, [10] = new[] { "lptimer3" }, [11] = new[] { "lptimer4" },
                [12] = new[] { "lptimer5" },
            }},
        };
    }
}
