package chipyard.fpga.vcu118

import sys.process._

import org.chipsalliance.cde.config.{Config, Parameters}
import freechips.rocketchip.subsystem.{SystemBusKey, PeripheryBusKey, ControlBusKey, ExtMem}
import freechips.rocketchip.devices.debug.{DebugModuleKey, ExportDebug, JTAG}
import freechips.rocketchip.devices.tilelink.{DevNullParams, BootROMLocated}
import freechips.rocketchip.diplomacy.{RegionType, AddressSet}
import freechips.rocketchip.resources.{DTSModel, DTSTimebase}
import freechips.rocketchip.util.{SystemFileName}

import sifive.blocks.devices.spi.{PeripherySPIKey, SPIParams}
import sifive.blocks.devices.uart.{PeripheryUARTKey, UARTParams}

import sifive.fpgashells.shell.{DesignKey}
import sifive.fpgashells.shell.xilinx.{VCU118ShellPMOD, VCU118DDRSize}

import testchipip.serdes.{SerialTLKey}

import chipyard._
import chipyard.harness._

class WithDefaultPeripherals extends Config((site, here, up) => {
  case PeripheryUARTKey => List(UARTParams(address = BigInt(0x64000000L)))
  case PeripherySPIKey => List(SPIParams(rAddress = BigInt(0x64001000L)))
  case VCU118ShellPMOD => "SDIO"
})

class WithSystemModifications extends Config((site, here, up) => {
  case DTSTimebase => BigInt((1e6).toLong)
  case BootROMLocated(x) => up(BootROMLocated(x), site).map { p =>
    // invoke makefile for sdboot
    val freqMHz = (site(SystemBusKey).dtsFrequency.get / (1000 * 1000)).toLong
    val make = s"make -C fpga/src/main/resources/vcu118/sdboot PBUS_CLK=${freqMHz} bin"
    require (make.! == 0, "Failed to build bootrom")
    p.copy(hang = 0x10000, contentFileName = SystemFileName(s"./fpga/src/main/resources/vcu118/sdboot/build/sdboot.bin"))
  }
  case ExtMem => up(ExtMem, site).map(x => x.copy(master = x.master.copy(size = site(VCU118DDRSize)))) // set extmem to DDR size
  case SerialTLKey => Nil // remove serialized tl port
})

/** No PMOD microSD on hand.  Drop the SPI controller entirely and hand PMOD J52
  * to JTAG -- which also rescues JTAG from FMC_J2, where nothing on this bench
  * can reach it (see jtag_location in TestHarness.scala).
  */
class WithUARTTSIPeripherals extends Config((site, here, up) => {
  case PeripheryUARTKey => List(UARTParams(address = BigInt(0x64000000L)))
  case PeripherySPIKey => Nil
  case VCU118ShellPMOD => "JTAG"
})

/** Everything WithSystemModifications does except pinning the boot ROM to
  * sdboot.  With no card present sdboot spins forever in sd_acmd41, which has
  * no timeout and prints nothing.  The chipyard default ROM instead parks hart0
  * in WFI on MSIP -- exactly what uart_tsi releases once it has finished
  * loading.
  */
class WithNoSDBootSystemModifications extends Config((site, here, up) => {
  case DTSTimebase => BigInt((1e6).toLong)
  case ExtMem => up(ExtMem, site).map(x => x.copy(master = x.master.copy(size = site(VCU118DDRSize))))
  case SerialTLKey => Nil // remove serialized tl port
})

// DOC include start: AbstractVCU118 and Rocket
class WithVCU118Tweaks extends Config(
  // clocking
  new chipyard.harness.WithAllClocksFromHarnessClockInstantiator ++
  new chipyard.clocking.WithPassthroughClockGenerator ++
  new chipyard.config.WithUniformBusFrequencies(100) ++
  new WithFPGAFrequency(100) ++ // default 100MHz freq
  // harness binders
  new WithUART ++
  new WithSPISDCard ++
  new WithDDRMem ++
  new WithJTAG ++
  // other configuration
  new WithDefaultPeripherals ++
  new chipyard.config.WithTLBackingMemory ++ // use TL backing memory
  new WithSystemModifications ++ // setup busses, use sdboot bootrom, setup ext. mem. size
  new freechips.rocketchip.subsystem.WithoutTLMonitors ++
  new freechips.rocketchip.subsystem.WithNMemoryChannels(1)
)

class RocketVCU118Config extends Config(
  new WithVCU118Tweaks ++
  new chipyard.RocketConfig
)
// DOC include end: AbstractVCU118 and Rocket

/** VCU118 target that loads over UART-TSI, no SD card required.
  *
  * The pin budget is the whole story.  Exactly one USB-UART reaches the fabric
  * -- UG1224 routes the CP2105's standard COM port to the FPGA and its enhanced
  * port to the system controller -- so the two UARTs this design needs cannot
  * both live on it:
  *
  *   TSI     -> on-board USB-UART (the UARTOverlay pins, AW25/BB21)
  *   console -> PMOD J52 pins 9/10 (AT15/AT16 = PMOD0_6_LS/PMOD0_7_LS)
  *
  * That ordering is deliberate.  With TSI on the cable that is already plugged
  * in, bare-metal bring-up needs no extra hardware at all: uart_tsi is an HTIF
  * frontend server, so program output returns over the same link.  Only a Linux
  * console requires a 3.3V USB-UART to appear on J52 -- and J52 is level-shifted
  * by U41, so a stock Pmod drops straight on.
  */
class WithVCU118UARTTSITweaks extends Config(
  // clocking
  new chipyard.harness.WithAllClocksFromHarnessClockInstantiator ++
  new chipyard.clocking.WithPassthroughClockGenerator ++
  new chipyard.config.WithUniformBusFrequencies(100) ++
  new WithFPGAFrequency(100) ++ // default 100MHz freq
  // harness binders
  new WithUARTTSI ++   // TSI on the on-board USB-UART
  new WithPMODUART ++  // console on PMOD J52 pins 9/10
  new WithDDRMem ++
  new WithJTAG ++
  // TSI transport
  new testchipip.tsi.WithUARTTSIClient(initBaudRate = BigInt(921600)) ++
  new chipyard.harness.WithSerialTLTiedOff ++
  // other configuration
  new WithUARTTSIPeripherals ++
  new chipyard.config.WithTLBackingMemory ++ // use TL backing memory
  new WithNoSDBootSystemModifications ++ // busses, default (WFI-on-MSIP) bootrom, ext. mem. size
  new freechips.rocketchip.subsystem.WithoutTLMonitors ++
  new freechips.rocketchip.subsystem.WithNMemoryChannels(1)
)

// Rocket + Gemmini (defaultConfig), Linux from SD card via sdboot
class GemminiVCU118Config extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118Tweaks ++
  new chipyard.LeanGemminiDefaultParamsRocketConfig
)

// Rocket + Gemmini (defaultConfig), bare-metal over UART-TSI (no SD card)
class GemminiVCU118UARTTSIConfig extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118UARTTSITweaks ++
  new chipyard.LeanGemminiDefaultParamsRocketConfig
)

/** Dual boot: one bitstream, the remote host picks bare-metal or Linux.
  *
  * WithVCU118Tweaks (SD on PMOD J52, DDR, JTAG on FMC_J2) plus:
  *   - console UART and UART-TSI share the on-board USB-UART (one cable).
  *     RX goes to both, TX is the AND of both; GPIO[0] high pins the TSI RX
  *     idle, i.e. closes the TSI gate. Both run at 921600 baud.
  *   - boot ROM = sdboot -DDUALBOOT: waits for MSIP (bare-metal, loaded by
  *     uart_tsi) or the mailbox magic (Linux: closes the gate, copies the SD
  *     payload, boots it). No timeout.
  *   - peripheral map = the addresses in sdboot's platform.h (UART, SPI, GPIO).
  *
  * Host: notes/vcu118_port/uart_tsi.py [--boot-linux].
  *
  * SD SPI clocks: init (CMD0 ... CMD16) at 300 kHz, within the spec's
  * 100-400 kHz identification range (our card failed init at 25 MHz). Copy
  * (CMD18) at 1 MHz: measured on the board through the J52 level shifter,
  * reads pass up to 3.125 MHz and fail from 3.57 MHz (12.5 MHz: no response),
  * so 1 MHz keeps a ~3.5x margin. 48 MiB then takes ~7 min.
  *
  * @param payloadSizeB SD payload copied by the ROM (30 MiB default; 48 << 20 for gemmini-all)
  * @param spiClkKHz     SD SPI clock for the payload copy (1 MHz default)
  * @param spiInitClkKHz SD SPI clock for card init (300 kHz default)
  */
class WithVCU118DualBootTweaks(payloadSizeB: BigInt = 30 << 20, spiClkKHz: Int = 1000,
                               spiInitClkKHz: Int = 300) extends Config(
  new WithVCU118SharedUART ++
  new chipyard.iobinders.WithGPIOPunchthrough ++
  new testchipip.tsi.WithUARTTSIClient(initBaudRate = BigInt(921600)) ++
  new chipyard.WithDualBootROM(payloadSizeB, spiClkKHz, spiInitClkKHz) ++
  new chipyard.WithVCU118PeripheralMap(consoleBaud = 921600) ++
  new WithVCU118Tweaks
)

// Rocket + Gemmini (defaultConfig), bare-metal over UART-TSI or Linux from SD.
// 48 MiB: the gemmini-all nodisk image is 42.8 MB. SD SPI clocks are the
// WithVCU118DualBootTweaks defaults; at 50 MHz pbus: SPI_INIT_DIV = 82
// (~301 kHz), SPI_DIV = 24 (exactly 1 MHz).
class GemminiVCU118DualBootConfig extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118DualBootTweaks(payloadSizeB = 48 << 20) ++
  new chipyard.LeanGemminiDefaultParamsRocketConfig
)

class BoomVCU118Config extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118Tweaks ++
  new chipyard.MegaBoomV3Config
)

class WithFPGAFrequency(fMHz: Double) extends Config(
  new chipyard.harness.WithHarnessBinderClockFreqMHz(fMHz) ++
  new chipyard.config.WithSystemBusFrequency(fMHz) ++
  new chipyard.config.WithPeripheryBusFrequency(fMHz) ++
  new chipyard.config.WithControlBusFrequency(fMHz) ++
  new chipyard.config.WithFrontBusFrequency(fMHz) ++
  new chipyard.config.WithMemoryBusFrequency(fMHz)
)

class WithFPGAFreq25MHz extends WithFPGAFrequency(25)
class WithFPGAFreq50MHz extends WithFPGAFrequency(50)
class WithFPGAFreq75MHz extends WithFPGAFrequency(75)
class WithFPGAFreq100MHz extends WithFPGAFrequency(100)
