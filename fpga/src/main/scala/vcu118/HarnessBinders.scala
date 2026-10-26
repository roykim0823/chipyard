package chipyard.fpga.vcu118

import chisel3._
import chisel3.experimental.{BaseModule}

import org.chipsalliance.diplomacy.nodes.{HeterogeneousBag}
import freechips.rocketchip.tilelink.{TLBundle}

import sifive.blocks.devices.uart.{UARTPortIO}
import sifive.blocks.devices.spi.{HasPeripherySPI, SPIPortIO}
import sifive.fpgashells.shell.{IOPin}

import chipyard._
import chipyard.harness._
import chipyard.iobinders._

/*** UART ***/
class WithUART extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: UARTPort, chipId: Int) => {
    th.vcu118Outer.io_uart_bb.bundle <> port.io
  }
})

/*** TSI over the on-board USB-UART ***/
// The UARTOverlay owns the on-board UART pins (AW25/BB21), so this drives the
// overlay's BundleBridgeSource rather than hand-placing pins.  Both bundles are
// two-wire -- UARTParams.includeFourWire defaults to false on the peripheral
// UART and on the TSI UART alike -- so the connection is structurally exact.
class WithVCU118UARTTSI extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: UARTTSIPort, chipId: Int) => {
    th.vcu118Outer.io_uart_bb.bundle <> port.io.uart
  }
})

/*** Console UART on PMOD J52 pins 9/10 ***/
// PMOD0_6_LS (AT15) and PMOD0_7_LS (AT16) are the only J52 signals that neither
// the SDIO nor the JTAG overlay claims, so the console can sit on the same
// header as JTAG.  On the 12-pin Pmod footprint those are header pins 9 and 10
// (bottom row, positions 3 and 4) -- NOT 7 and 8, which carry PMOD0_4/PMOD0_5,
// i.e. the SDIO DAT1/DAT2 pins.  J52 is level-shifted to 3.3V by U41 (UG1224 p.89), so a
// stock 3.3V Pmod USB-UART attaches directly.
class WithVCU118PMODUART(rxdPin: String = "AT15", txdPin: String = "AT16") extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: UARTPort, chipId: Int) => {
    val harnessIO = IO(chiselTypeOf(port.io)).suggestName("uart_console")
    harnessIO <> port.io
    Seq((rxdPin, IOPin(harnessIO.rxd)), (txdPin, IOPin(harnessIO.txd))) foreach { case (pin, io) => {
      th.vcu118Outer.xdc.addPackagePin(io, pin)
      th.vcu118Outer.xdc.addIOStandard(io, "LVCMOS18")
      th.vcu118Outer.xdc.addIOB(io)
    } }
  }
})

/*** SPI ***/
class WithSPISDCard extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: SPIPort, chipId: Int) => {
    th.vcu118Outer.io_spi_bb.bundle <> port.io
  }
})

/*** Experimental DDR ***/
class WithDDRMem extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: TLMemPort, chipId: Int) => {
    val bundles = th.vcu118Outer.ddrClient.out.map(_._1)
    val ddrClientBundle = Wire(new HeterogeneousBag(bundles.map(_.cloneType)))
    bundles.zip(ddrClientBundle).foreach { case (bundle, io) => bundle <> io }
    ddrClientBundle <> port.io
  }
})

class WithJTAG extends HarnessBinder({
  case (th: VCU118FPGATestHarnessImp, port: JTAGPort, chipId: Int) => {
    val jtag_io = th.vcu118Outer.jtagPlacedOverlay.overlayOutput.jtag.getWrappedValue
    port.io.TCK := jtag_io.TCK
    port.io.TMS := jtag_io.TMS
    port.io.TDI := jtag_io.TDI
    port.io.reset.foreach(_ := th.referenceReset)
    jtag_io.TDO.data := port.io.TDO
    jtag_io.TDO.driven := true.B
    // ignore srst_n
    jtag_io.srst_n := DontCare

  }
})
