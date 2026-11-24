# Rocket + Gemmini on VCU118 with Chipyard: Hands-on Tutorial

- **Author:** Min Jung
- **Paths:** All paths are relative to the Chipyard root (`chipyard/`).
- **Shared files:** Everything needed for the hands-on part is in `notes/vcu118_port/`.

  ```text
  notes/vcu118_port/
  ├── README.md                                     # this document
  ├── patches/
  │   ├── firemarshal-prototype-buildroot-config.patch   # 7.2 Step 0
  │   └── opensbi-sbi_pmu-keep-cy-ir.patch               # 7.2 Step 0
  └── uart_tsi.py                                   # board host tool: bare-metal over UART-TSI, Linux boot request (6.3)
  ```
- **Bitstream:** One bitstream, **`GemminiVCU118DualBootConfig`**, runs both the bare-metal and the Linux tests. It is not distributed; build it yourself (chapter 4). The test board's configuration flash holds this bitstream (5.2).

> **Conventions**
> - Hands-on steps are written in the order **Goal / Commands / Expected output / ✅ Checkpoint / ⚠️ Caution**.

---

## Contents

- [0. Document Info](#0-document-info)
- [1. Overview](#1-overview)
- [2. Prerequisites](#2-prerequisites)
- [3. Hardware Configuration (Configs)](#3-hardware-configuration-configs)
- [4. Bitstream Generation](#4-bitstream-generation)
- [5. Board Setup](#5-board-setup)
- [6. Bare-metal Hands-on](#6-bare-metal-hands-on)
- [7. Linux Hands-on](#7-linux-hands-on)
- [8. Results](#8-results)
- [9. Troubleshooting / FAQ](#9-troubleshooting--faq)

---

## 0. Document Info

### 0.1 Purpose
Build a Rocket + Gemmini SoC with Chipyard, put it on a Xilinx VCU118, and run Gemmini tests and DNN workloads (MobileNet, ResNet50)
through two paths: **bare-metal (UART-TSI)** and **Linux (SD boot)**. Both paths run on **one dual-boot bitstream** (`GemminiVCU118DualBootConfig`); the host picks the path after each reset over the same USB-UART cable.
The document also records the problems hit during bring-up (SD payload limit, SD SPI clock, login failure, cycle counters, etc.) and their fixes.

> **Scope**
> - **Chapters 3–4**: the SoC config and how the bitstream is built, with the results. The bitstream is not distributed, so build it with these chapters if your board does not already have it.
> - **Chapters 5–7 (hands-on)**: step-by-step, from the board with the bitstream in flash to bare-metal / Linux board tests.
>
> **How this document was tested:** the bitstream was generated in the devcontainer on the build server (Vivado 2025.2), written to the VCU118's configuration flash, and the board tests were run with `uart_tsi.py` from the same server, which is connected to the board's USB-UART.

### 0.2 Tested Environment and Versions

| Item | Version / Value | Notes |
|---|---|---|
| Chipyard | `dev_dn` branch (origin fork, after the PR that adds this document is merged) | |
| Gemmini | `8c3f992` (v0.1a-515) | Used unmodified (`software/gemmini-rocc-tests` @ `7c540b3`) |
| testchipip | `26f821b` | |
| FireMarshal | `21119e5` (v1.12.0-274) | With the changes in 7.2 Step 0 |
| Linux / OpenSBI | 6.6 (`v6.6-4-g67bc4513761`) / v1.2 (`v1.2-78-g5ccebf0`, `PLATFORM=generic`) | FireMarshal `boards/prototype` |
| Bitstream config | `GemminiVCU118DualBootConfig` | Bare-metal and Linux on one bitstream (3.2) |
| Vivado (bitstream generation) | 2025.2 | Server install, bind-mounted into the devcontainer at `/opt/Xilinx` (2.2 Step 3) |
| Bitstream on the board | VCU118 configuration flash | Loaded at power-up (5.2). Flash programming is not covered here |
| RISC-V toolchain | `riscv64-unknown-linux-gnu-gcc` 13.2.0 (`.conda-env/riscv-tools`) | |
| Build / test host | devcontainer Ubuntu 24.04.5 on a Linux server, the board's USB cables plugged into that server | Builds, bitstream generation, `uart_tsi.py`, UART console |
| Python / pyserial | 3.10.14 / 3.5 (chipyard conda env) | Runtime for `uart_tsi.py` |
| Serial terminal | picocom 3.1 | Linux console (7.5) |

---

## 1. Overview

### 1.1 Goals
- Port a Chipyard-based **Rocket + Gemmini** SoC to the **Xilinx VCU118**
- Verify functionality with board tests in both **bare-metal** and **Linux** environments
- Workloads: `chipyard-tests`, `riscv-tests`, `gemmini-rocc-tests`, `imagenet/mobilenet`, `imagenet/resnet50`

### 1.2 Target System Architecture

```
ChipTop = VCU118 IO & Harness + DigitalTop
 └─ DigitalTop
     ├─ RISC-V Tile 0 : Rocket core, PTW, L1 I$, L1 D$, TileBus
     │    └─ RoCC cmd / RoCC PTW ── Gemmini (RoCC Matrix Multiplication Accelerator)
     └─ System Bus
         ├─ Memory Bus    : L2 (InclusiveCache, 512 KB) → TL to AXI → DDR4 (MIG, 2 GB)
         ├─ Periphery Bus : UART (0x6400_0000), SPI-SD (0x6400_1000), GPIO (0x6400_2000, 1 pin = TSI gate)
         ├─ Front Bus     : UART-TSI (TSIToTileLink, host → memory)
         └─ Control Bus   : BootROM(sdboot -DDUALBOOT, 0x1_0000), PLIC(0xC00_0000), CLINT(0x200_0000), Debug(0x0)

 Harness: console UART + UART-TSI share the on-board USB-UART (CP2105 Standard port); SD card on PMOD J52
```

- **RISC-V Tile**: one Rocket core (RV64GC, Sv39 MMU). Gemmini shares the core's PTW and uses virtual addresses, so it also works from Linux user programs.
- **RoCC**: Rocket's custom-instruction interface. Gemmini commands are delivered as RoCC custom instructions.
- **L2**: 1024 sets × 8 ways × 64 B = 512 KB (DTS `cache-controller@2010000`).
- **TL**: TileLink (chip-scale interconnect), **PLIC**: Platform-Level Interrupt Controller, **CLINT**: Core-Local Interruptor.

### 1.3 Overall Workflow

```
[Chapters 3–4 · build server]
Chipyard Configuration → RTL Elaboration → Vivado Implementation → Bitstream Generation → configuration flash

[Hands-on · chapters 5–7]
  → Power-up (bitstream loads from flash) → CPU_RESET → host picks the path with uart_tsi.py
      ├─ Bare-metal : uart_tsi.py <ELF>        → Rocket Test → Gemmini Test → DNN Test
      └─ Linux      : uart_tsi.py --boot-linux → SD Boot → OpenSBI → Linux → Gemmini Test
```

### 1.4 Bare-metal vs Linux

| | Bare-metal | Linux |
|---|---|---|
| Bitstream | `GemminiVCU118DualBootConfig` | ← same bitstream |
| How the path is chosen | `uart_tsi.py <ELF>` (sets MSIP after loading) | `uart_tsi.py --boot-linux` (mailbox request) |
| Program loading | UART-TSI (host → DDR) | SD card (boot ROM → DDR, 48 MiB from sector 34) |
| SD card | Not needed | Needed (PMOD J52) |
| Output path | HTIF (tohost/fromhost) → host terminal | UART console on the same USB-UART (115200 after the kernel console starts, 7.5) |
| Execution mode | M-mode | U-mode (Linux user space) |
| Binaries | `*-baremetal` | `*-linux` (dynamically linked, glibc) |
| Switching tests | Re-send every ELF (resnet50: 125.5 MB), CPU_RESET before each | Write the SD once, pick tests from the shell |
| Use | HW functional checks, performance baseline (no virtual-address/TLB overhead) | Real-environment checks, regression over many tests |

The boot ROM decides **once per reset**: press CPU_RESET before every run, whichever path you take (5.3).

---

## 2. Prerequisites

### 2.1 Hardware
- [ ] Xilinx VCU118 board + power supply, with the `GemminiVCU118DualBootConfig` bitstream in its configuration flash (5.2)
- [ ] UART USB cable (on-board CP2105 USB-UART, see 5.1) — the only cable the tests need: UART-TSI and the Linux console share it
- [ ] PMOD MicroSD adapter (Digilent PmodMicroSD, for Linux) on PMOD J52
- [ ] MicroSD card (partition 1 needs at least 48 MiB; 512 MiB recommended)
- [ ] USB SD card reader (for writing the image, 7.3)
- [ ] Host — a Linux server running the devcontainer, with the board's USB-UART plugged into it (builds, bitstream generation, `uart_tsi.py`, console)

### 2.2 Software Setup
Everything runs on the build server inside the devcontainer: the bitstream (chapter 4), the bare-metal test ELFs (6.4), the FireMarshal Linux image (7.2), and the board tests themselves (`uart_tsi.py`, console).

#### Step 1. Clone Chipyard and run build-setup
```bash
git clone https://github.com/roykim0823/chipyard.git
cd chipyard
git checkout dev_dn
./build-setup.sh riscv-tools
```
- The build environment is this repository's devcontainer. See `dockerfiles/README.md` for how to use it.

⚠️ **Caution**: FireMarshal builds need `/usr/bin/file` and `cpio`. Install them if missing:

```bash
sudo apt-get update && sudo apt-get install -y file cpio
```

#### Step 2. Check the RISC-V toolchain
```bash
source env.sh
riscv64-unknown-linux-gnu-gcc --version     # 13.2.0
riscv64-unknown-elf-gcc --version
```
⚠️ **Caution**: Run the toolchain and all build commands inside the conda environment (`source env.sh`).

#### Step 3. Vivado (bitstream generation)
Vivado 2025.2 is installed on the server and bind-mounted read-only into the devcontainer at `/opt/Xilinx` (see `.devcontainer/devcontainer.json`). Needed only for chapter 4.
```bash
source env.sh
export PATH="$PATH:/opt/Xilinx/2025.2/Vivado/bin"   # append; do not source settings64.sh after env.sh
which dtc vivado    # dtc must be .conda-env/bin/dtc
```
⚠️ **Caution**: `settings64.sh` puts Vivado's own `dtc` first in `PATH`. That `dtc` cannot find `libyaml-0.so.2` in the container, and elaboration fails with `Failed to run dtc; is it in your path?` (9.2).

#### Step 4. Serial port access (USB-UART)
The on-board CP2105 shows up as two ports. The FPGA is wired to the **Standard** port (usually `/dev/ttyUSB1`); the Enhanced port (`/dev/ttyUSB0`) goes to the system controller.
```bash
for t in /sys/class/tty/ttyUSB*; do echo "$(basename $t): $(cat $t/device/../interface)"; done
sudo chmod 666 /dev/ttyUSB1        # or add the user to the dialout group
```
✅ **Checkpoint**: one port prints `Standard Com Port`; use that one everywhere below.

⚠️ **Caution**: the privileged devcontainer only sees the `/dev` nodes that existed when it started. If the USB cable is re-plugged or the board's USB re-enumerates, reopen the container (9.2).

#### Step 5. Python (pyserial) and a serial terminal
`uart_tsi.py` needs pyserial; it is installed in the chipyard conda environment (Python 3.10.14, pyserial 3.5). The Linux console uses picocom.
```bash
source env.sh
python3 -c "import serial; print(serial.__version__)"   # 3.5 (pip install pyserial==3.5 if missing)
sudo apt-get install -y picocom
```

#### Step 6. Initialize FireMarshal (board = prototype)
```bash
cd software/firemarshal
grep -q '^board-dir' marshal-config.yaml 2>/dev/null || echo "board-dir : 'boards/prototype'" >> marshal-config.yaml
cat marshal-config.yaml
```
- If a `board-dir` line already exists, nothing is added (running this repeatedly leaves exactly one line).

✅ **Checkpoint**: The line `board-dir : 'boards/prototype'` appears exactly once.

#### Step 7. SD card writing tools
The image is written to the SD card with Linux `dd` (7.3), on any Linux machine with a USB SD reader. The SD device is not visible inside the devcontainer, so write from the server host itself, another Linux PC, or Windows + WSL2 (usbipd-win). Install the partitioning tool there (needed only to partition a new card, 7.3 Step 3):
```bash
sudo apt-get update && sudo apt-get install -y gdisk
```

### 2.3 Directory Layout

Shows where files come from during the tutorial and where they go.
**[provided]** files you receive · **[authored]** files you create yourself · **[built]** files produced by a build

```text
chipyard/
├── notes/vcu118_port/                         [provided] README.md (this document), patches/, uart_tsi.py
├── fpga/generated-src/chipyard.fpga.vcu118.VCU118FPGATestHarness.GemminiVCU118DualBootConfig/
│   └── obj/VCU118FPGATestHarness.bit          [built] chapter 4 → configuration flash (5.2)
├── tests/build/*.riscv                        [built] 6.4 Step 1
├── .conda-env/riscv-tools/                    ($RISCV)
│   └── riscv64-unknown-elf/share/riscv-tests/
│       └── benchmarks/*.riscv                 [built] build-setup (6.4 Step 2)
├── generators/gemmini/software/gemmini-rocc-tests/
│   └── build/{bareMetalC,imagenet}/           [built] 6.4 Step 3
│       ├── *-baremetal                        → uart_tsi.py (6.5)
│       └── *-linux                            → overlay (7.2 Step 2)
└── software/
    ├── gemmini-all/                           [authored] 7.2 Steps 2–3
    │   ├── gemmini-all.json, gemmini-all.kfrag
    │   └── overlay/
    │       ├── root/{gemmini,imagenet}/       ← copies of *-linux from build/
    │       └── etc/sysctl.d/90-perf-user-access.conf
    └── firemarshal/images/prototype/gemmini-all/
        └── gemmini-all-bin-nodisk-flat        [built] 7.2 Step 4 → SD card (7.3)
```

The bare-metal ELFs are run in place from the chipyard root; there is nothing to copy to another machine.

There are two kinds of changes relative to upstream.

**(1) Changes used to build the bitstream (already in the repository)**

| File | Change |
|---|---|
| `fpga/src/main/scala/vcu118/Configs.scala` | Adds `WithVCU118DualBootTweaks` and `GemminiVCU118DualBootConfig` (3.1) |
| `fpga/src/main/scala/vcu118/HarnessBinders.scala` | Adds `WithVCU118SharedUART`: console UART + UART-TSI on the on-board USB-UART (3.1) |
| `generators/chipyard/src/main/scala/config/DualBootConfigs.scala` | `WithDualBootROM` (builds sdboot `-DDUALBOOT` during elaboration), `WithVCU118PeripheralMap` (3.1) |
| `generators/chipyard/src/main/scala/harness/DualBootHarnessBinders.scala` | `SharedUART` / `WithSharedUART`: one line for the console and TSI, with the GPIO gate (3.1) |
| `generators/chipyard/src/main/scala/config/RoCCAcceleratorConfigs.scala` | `LeanGemminiDefaultParamsRocketConfig` (3.2) |
| `fpga/src/main/resources/vcu118/sdboot/{sd.c, Makefile}` | Dual-boot wait/branch (`-DDUALBOOT`), separate init / copy SPI clocks, build-time overrides (7.4) |

**(2) Changes you apply yourself during the hands-on part**

| File | Change |
|---|---|
| devcontainer packages | Install `file` and `cpio` in the container (2.2 Step 1) |
| `software/firemarshal/boards/prototype/base-workloads/br-base/buildroot-config` (tracked in the firemarshal submodule) | Root password set as an MD5 hash; adds `BR2_TOOLCHAIN_EXTERNAL_FORTRAN=y` / `OPENMP=y` (+12/−1) — 7.2 Step 0. Patch: `notes/vcu118_port/patches/firemarshal-prototype-buildroot-config.patch` |
| `software/firemarshal/boards/default/firmware/opensbi/lib/sbi/sbi_pmu.c` (opensbi submodule inside firemarshal) | `pmu_ctr_stop_hw()` no longer stops the CY(0)·IR(2) counters (+16) — 7.2 Step 0. Patch: `notes/vcu118_port/patches/opensbi-sbi_pmu-keep-cy-ir.patch` |

---

## 3. Hardware Configuration (Configs)

### 3.1 VCU118 Fragments

| Fragment | Role | Defined in |
|---|---|---|
| `WithFPGAFrequency(50)` | sbus/pbus/cbus/fbus/mbus and harness clocks at 50 MHz | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithVCU118DualBootTweaks` | **Dual-boot harness**: bare-metal over UART-TSI or Linux from the SD card, picked by the host after each reset (table below) | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithVCU118Tweaks` | Base VCU118 harness that the dual-boot Tweaks build on: on-board USB-UART, SPI-SD on PMOD J52 (`VCU118ShellPMOD = "SDIO"`), 2 GB DDR, JTAG on FMC_J2, sdboot BootROM | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithSystemBusWidth(128)` | 128-bit system bus | `generators/chipyard/src/main/scala/config/fragments/SubsystemFragments.scala:17` |
| `WithNBigCores(1)` | One Rocket core (BigCore: 16 KB L1 I$ / D$) | `generators/rocket-chip/src/main/scala/rocket/Configs.scala:62` |

#### What `WithVCU118DualBootTweaks` adds on top of `WithVCU118Tweaks`

```scala
class WithVCU118DualBootTweaks(payloadSizeB: BigInt = 30 << 20, spiClkKHz: Int = 1000,
                               spiInitClkKHz: Int = 300) extends Config(
  new WithVCU118SharedUART ++
  new chipyard.iobinders.WithGPIOPunchthrough ++
  new testchipip.tsi.WithUARTTSIClient(initBaudRate = BigInt(921600)) ++
  new chipyard.WithDualBootROM(payloadSizeB, spiClkKHz, spiInitClkKHz) ++
  new chipyard.WithVCU118PeripheralMap(consoleBaud = 921600) ++
  new WithVCU118Tweaks
)
```

| Fragment | Effect | Defined in |
|---|---|---|
| `WithVCU118SharedUART` | Console UART and UART-TSI share the on-board USB-UART (CP2105 Standard port): RX feeds both, TX is the AND of both (each idles high). GPIO[0] driven high holds the TSI receiver idle (the **TSI gate**) | `fpga/src/main/scala/vcu118/HarnessBinders.scala` → `harness/DualBootHarnessBinders.scala` (`WithSharedUART`) |
| `WithGPIOPunchthrough` | Brings the GPIO port out to the harness, so the gate pin can reach the shared UART | chipyard iobinders |
| `WithUARTTSIClient(921600)` | UART-TSI client (`TSIToTileLink` on the front bus) at 921600 baud | testchipip |
| `WithDualBootROM(payloadSizeB, spiClkKHz, spiInitClkKHz)` | BootROM = sdboot built with `-DDUALBOOT` during elaboration, into its own directory `sdboot/build-dualboot-<pbus>MHz-<size>B-init<k>kHz-<k>kHz/` (7.1, 7.4) | `generators/chipyard/src/main/scala/config/DualBootConfigs.scala` |
| `WithVCU118PeripheralMap(921600)` | UART `0x6400_0000` (921600 baud), SPI `0x6400_1000`, GPIO `0x6400_2000` (1 pin) — the addresses hard-coded in sdboot's `include/platform.h` | `generators/chipyard/src/main/scala/config/DualBootConfigs.scala` |

Defaults: SD init clock 300 kHz (the SD spec allows 100–400 kHz during identification), SD copy clock 1 MHz (measured limit about 3 MHz on this board, 7.4), payload 30 MiB (the config below sets 48 MiB).

Also inherited from `WithVCU118Tweaks`: 2 GB DDR (`ExtMem` = `VCU118DDRSize`), JTAG on FMC_J2, TL backing memory, TL monitors removed, one memory channel, default 100 MHz clocking (overridden by `WithFPGAFrequency(50)`).

> `Configs.scala` also keeps two single-path configs from the earlier bring-up, `GemminiVCU118UARTTSIConfig` (bare-metal only, no SD) and `GemminiVCU118Config` (Linux only). They are not used in this tutorial.

### 3.2 Config Used in the Experiments

| Item | Value |
|---|---|
| VCU118 config | `GemminiVCU118DualBootConfig` |
| Harness | `WithVCU118DualBootTweaks(payloadSizeB = 48 << 20)` |
| CPU | 1 Rocket core (BigCore) |
| Gemmini | `LeanGemminiConfig(defaultConfig)` — 16×16, Dataflow **WS** (3.3) |
| Clock | 50 MHz (all buses) |
| System bus | 128-bit |
| SD payload / SPI clocks | 48 MiB / init 300 kHz, copy 1 MHz |
| UART-TSI / console UART | 921600 baud, both on the on-board USB-UART |

```scala
// fpga/src/main/scala/vcu118/Configs.scala
class GemminiVCU118DualBootConfig extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118DualBootTweaks(payloadSizeB = 48 << 20) ++
  new chipyard.LeanGemminiDefaultParamsRocketConfig
)

// generators/chipyard/src/main/scala/config/RoCCAcceleratorConfigs.scala
class LeanGemminiDefaultParamsRocketConfig extends Config(
  new gemmini.LeanGemminiConfig(gemmini.GemminiConfigs.defaultConfig) ++
  new chipyard.config.WithSystemBusWidth(128) ++
  new freechips.rocketchip.rocket.WithNBigCores(1) ++
  new chipyard.config.AbstractConfig
)
```

- 48 MiB: the `gemmini-all` nodisk flat image is 42.8 MB (7.2).
- `WithFPGAFrequency(50)` must be to the left of the Tweaks. The leftmost fragment wins, so it overrides the 100 MHz setting inside the Tweaks.

### 3.3 Default vs Lean

`leanConfig` is `defaultConfig.copy(...)` with only the items below changed (`generators/gemmini/src/main/scala/gemmini/Configs.scala:244`).

| Parameter | Default | Lean | Effect |
|---|---|---|---|
| dataflow | BOTH (WS, OS) | WS | **Whether OS is available — the only functional difference.** Tests using the `os` backend (`tiled_matmul_os`, `mobilenet os matmul`) only work on default |
| max_in_flight_mem_reqs | 16 | 64 | Number of concurrent DMA requests |
| acc_read_full_width | true | false | Accumulator read width. `ACC_READ_FULL_WIDTH` / `ACC_READ_SMALL_WIDTH` in `gemmini_params.h` |
| ex_read_from_acc | true | false | Extra datapath |
| ex_write_to_spad | true | false | Extra datapath |
| hardcode_d_to_garbage_addr | false | true | — |
| Rocket core | BigCore | HugeCore | 16 KB vs 32 KB L1 |

- Size parameters (DIM=16, BANK_NUM=4, BANK_ROWS=4096, ACC_ROWS=1024): identical
- `gemmini_params.h` difference: one `ACC_READ_*_WIDTH` macro line, referenced by no code → binaries built with the original header are used as-is (4.3)

### 3.4 Final Hardware Spec

| Block | Item | Value |
|---|---|---|
| Rocket | ISA | RV64GC (DTS: `rv64imafdczicsr_zifencei_zihpm_xrocket`) |
| | Cores | 1 |
| | L1 I$ / D$ | 16 KB / 16 KB |
| | MMU | Sv39 |
| L2 | | 512 KB (1024 sets, 64 B lines) |
| DDR | | 2 GB (from `0x8000_0000`) |
| Clock | | 50 MHz (all buses) |
| Gemmini | Systolic array | 16 × 16 |
| | Input / Accumulator | Int8 / Int32 |
| | Dataflow | WS (Lean) |
| | Scratchpad | 256 KB (4 banks) |
| | Accumulator | 64 KB (2 banks) |
| I/O | Console UART | `0x6400_0000`, 921600 baud at reset |
| | SPI (SD card) | `0x6400_1000`, PMOD J52 |
| | GPIO | `0x6400_2000`, 1 pin (TSI gate) |
| | UART-TSI | 921600 baud, shares the on-board USB-UART with the console |

### 3.5 Elaboration

Elaborate the config into RTL and DTS (the `bitstream` target in 4.1 also starts from elaboration). Set up the shell as in 2.2 Step 3 first.

```bash
source env.sh
export PATH="$PATH:/opt/Xilinx/2025.2/Vivado/bin"
cd fpga
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118DualBootConfig verilog
```
- vcu118 defaults in `fpga/Makefile`: `CONFIG_PACKAGE=chipyard.fpga.vcu118`, `MODEL=VCU118FPGATestHarness`.
- Elaboration also builds the dual-boot BootROM: `fpga/src/main/resources/vcu118/sdboot/build-dualboot-50MHz-50331648B-init300kHz-1000kHz/sdboot.bin` (its `boot_defs` file shows `-DDUALBOOT -DPAYLOAD_SIZE_B=50331648 -DSPI_CLK=1000 -DSPI_INIT_CLK=300 PBUS_CLK=50`).
- Check the result in `*.dts` (4.3).

---

## 4. Bitstream Generation

This chapter describes how the bitstream on the test board was built and what the results were. It was built in the devcontainer on the build server.

### 4.1 Generating the Bitstream

```bash
source env.sh
export PATH="$PATH:/opt/Xilinx/2025.2/Vivado/bin"
cd fpga
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118DualBootConfig bitstream
```
- The `bitstream` target starts from elaboration (3.5) if it has not run yet.
- If timing is not met (slack < 0), the Vivado flow prints `Failed to meet timing` and fails.
- Run it in `tmux` or with `nohup`. Closing the VS Code window can stop the devcontainer and kill the build (9.2).

⚠️ **Caution — changing only sdboot does not update the BootROM.** The `make` dependencies only track Scala sources (`common.mk:136`).
sdboot is built during elaboration and baked into the Verilog as a BootROM constant, so after changing `sd.c` you must
delete the `<build>` directory (4.2) or touch a Scala file. Otherwise **you get a bitstream with the old BootROM**.

### 4.2 Build Outputs

`<build>` = `fpga/generated-src/chipyard.fpga.vcu118.VCU118FPGATestHarness.GemminiVCU118DualBootConfig/`

| Category | Files | Location |
|---|---|---|
| Elaboration | `*.dts`, `*.mems.conf`, `*.top.f`, `*.fir`, `*.memmap.json` | `<build>` |
| Gemmini SW header | `gemmini_params.h` | `generators/gemmini/software/gemmini-rocc-tests/include/` (rewritten during elaboration; same content as the committed header, 4.3) |
| Vivado log / checkpoints | `vivado.log`, `obj/post_{synth,opt,place,route}.dcp` | `<build>` |
| Implementation reports | `utilization.txt`, `timing.txt`, `drc.txt`, `clocks.txt`, etc. | `<build>/obj/report/` |
| Constraints | `*.shell.xdc`, `*.shell.sdc` | `<build>` |
| Bitstream | `VCU118FPGATestHarness.bit` | `<build>/obj/` |
| BootROM | `sdboot.bin`, `boot_defs` | `fpga/src/main/resources/vcu118/sdboot/build-dualboot-50MHz-50331648B-init300kHz-1000kHz/` |

### 4.3 Checking the Elaboration Results
#### Device Tree (`*.dts`)

| Item | Expected | Observed |
|---|---|---|
| DRAM base / size | 0x80000000 / 2 GB | `memory@80000000`, `reg = <0x80000000 0x80000000>` ✅ |
| BootROM | 0x10000 | `rom@10000`, size 0x10000 ✅ |
| CLINT / PLIC | 0x2000000 / 0xC000000 | `clint@2000000` / `interrupt-controller@c000000` ✅ |
| UART / SPI (SD) / GPIO | 0x64000000 / 0x64001000 / 0x64002000 | `serial@64000000` (sifive,uart0) / `spi@64001000` + `mmc@0` (mmc-spi-slot) / `gpio@64002000` (sifive,gpio0) ✅ |
| riscv,isa | rv64imafdc | `rv64imafdczicsr_zifencei_zihpm_xrocket` ✅ |
| L1 I$ / D$ | 16 KB / 16 KB (BigCore) | `i-cache-size` / `d-cache-size = <16384>` ✅ |
| L2 | 512 KB | `cache-size = <524288>`, 1024 sets ✅ |
| clock-frequency | 50 MHz | `sbus/pbus/cbus/mbus/fbus_clock = 50000000` ✅ |
| timebase | 50 kHz | `timebase-frequency = <50000>` ✅ |

The UART node has no `current-speed` or `clock-frequency` property; this decides the console baud rates during a Linux boot (7.5).

#### Gemmini Parameters (`gemmini_params.h`)

Excerpt (`gemmini-rocc-tests` @ `7c540b3`; elaboration of this config writes the same file):

```c
#define DIM 16
#define BANK_NUM 4
#define BANK_ROWS 4096        // 4 banks × 4096 rows × 16 B = 256 KB scratchpad
#define ACC_ROWS 1024         // 1024 rows × 16 × 4 B = 64 KB accumulator
typedef int8_t elem_t;
typedef int32_t acc_t;
...
#define ACC_READ_SMALL_WIDTH
#define ACC_READ_FULL_WIDTH
```

⚠️ **Caution**: The software (gemmini-rocc-tests) must be compiled with **a header generated from the same config as the hardware**. For `GemminiVCU118DualBootConfig` the header committed in the submodule already matches, so the tests can be built before or after elaboration. If you change the Gemmini config, rebuild the tests after elaboration.

### 4.4 Synthesis Results
- Vivado version: 2025.2
- Target part: `xcvu9p-flga2104-2L-e` (`part_fpga` in `fpga/fpga-shells/xilinx/vcu118/tcl/board.tcl`) — confirmed by `Device : xcvu9p-flga2104-2L-e` in `<build>/obj/report/utilization.txt` ✅

### 4.5 Implementation Results

#### Timing

| Item | Value | Verdict |
|---|---|---|
| WNS (setup) | 0.106 ns | ✅ ≥ 0 |
| TNS | 0.000 ns (0 failing / 304,010 endpoints) | ✅ = 0 |
| WHS (hold) | 0.009 ns | ✅ ≥ 0 |
| THS | 0.000 ns (0 failing / 303,308 endpoints) | ✅ = 0 |
| Target clock | 50 MHz (`clk_out1_harnessSysPLL`, 20 ns) | |

On the board, `Total cycles ÷ user time` also measures about 50 MHz, confirming the core runs at 50 MHz (7.6.2).

#### Resource Utilization (post-route)

From `<build>/obj/report/utilization.txt` (`report_utilization -hierarchical`). "Rocket" is the Rocket tile without Gemmini (core, FPU, L1 caches, PTW).

| Resource | Used | Available | Utilization | Rocket | Gemmini |
|---|---|---|---|---|---|
| LUT | 240,342 | 1,182,240 | 20.33% | 30,816 | 172,759 |
| FF | 106,249 | 2,364,480 | 4.49% | 12,784 | 60,563 |
| BRAM (36Kb equiv.) | 301.5 (RAMB36 225 + RAMB18 153) | 2,160 | 13.96% | 8 (RAMB18 16) | 128 (RAMB36 64 + RAMB18 128) |
| URAM | 0 | 960 | 0.00% | 0 | 0 |
| DSP | 230 | 6,840 | 3.36% | 24 | 203 |

Breakdown of the main blocks:

| Instance | LUTs | (LUTRAM) | FFs | RAMB36 | RAMB18 | DSP |
|---|---:|---:|---:|---:|---:|---:|
| SoC (`chiptop0`) | 222,807 | 7,962 | 81,620 | 200 | 152 | 227 |
| └ Gemmini | 172,759 | 2,878 | 60,563 | 64 | 128 | 203 |
| └ Rocket core | 5,708 | 80 | 2,108 | 0 | 0 | 13 |
| └ FPU | 14,070 | 0 | 3,764 | 0 | 0 | 11 |
| └ L1 D-cache | 6,182 | 2,664 | 2,417 | 0 | 0 | 0 |
| └ Frontend (L1 I-cache) | 4,182 | 112 | 3,924 | 0 | 16 | 0 |
| └ L2 (`InclusiveCache`) | 9,089 | 634 | 3,829 | 120 | 8 | 0 |
| DDR4 controller (`mig`) | 17,102 | 1,696 | 23,652 | 25 | 1 | 3 |

Gemmini accounts for about 72% of the design's LUTs.

#### DRC

| Item | Value |
|---|---|
| DRC errors | 0 (676 warnings — 655 DSP pipelining recommendations, etc., all non-critical) |

✅ **Checkpoint**: WNS ≥ 0, 0 DRC errors

---

## 5. Board Setup

### 5.1 Connection and Power Checklist
- [ ] 0. Power and the UART USB cable connected; the UART cable goes to the server that runs the devcontainer
- [ ] 1. (Linux) PMOD MicroSD adapter on **PMOD J52**, top row (pin 1 = CS), SD card inserted
- [ ] 2. Power switch up — the bitstream loads from the configuration flash and the **DS34 DONE LED** lights (5.2)
- [ ] 3. Find the CP2105 **Standard** port (`/dev/ttyUSB1` here) and make it accessible (2.2 Step 4). It carries both UART-TSI and the console

⚠️ **Caution**: Do not use the PROG button (left); it clears the FPGA configuration. Reset with **SW5 CPU_RESET**.

### 5.2 Bitstream in the Configuration Flash

The test board's configuration flash holds the `GemminiVCU118DualBootConfig` bitstream built in chapter 4. The FPGA loads it automatically at every power-up, so no programming step is needed before the tests. How to program the flash is not covered in this document.

| Item | Value |
|---|---|
| Config | `GemminiVCU118DualBootConfig` |
| File | `<build>/obj/VCU118FPGATestHarness.bit`, 39,595,808 B |
| sha256 | `9095d8aa08dbdf640824c8fa8587f76c00d1a7bfa14cde936741a6eb8e02db28` |

✅ **Checkpoint**: after power-up the DONE LED is lit and the link check in 6.5 Lab 0 answers.

### 5.3 Choosing the Boot Path

```
Power-up or CPU_RESET
  └─ boot ROM (sdboot -DDUALBOOT) clears the mailbox, prints nothing, waits with no timeout
       ├─ MSIP set by the host                 → bare-metal: jump to 0x8000_0000          (chapter 6)
       └─ mailbox == "LINUXSD!" (from the host) → Linux:                                   (chapter 7)
            write ACK → close TSI gate → print INIT → SD init → copy payload → BOOT → jump to 0x8000_0000
```

- **The choice is made once per reset.** Right after power-up the ROM is already waiting; after that, press CPU_RESET before every run.
- While the ROM waits it prints nothing, because the TX line is shared with TSI responses.
- `uart_tsi.py --read` only reads memory, so it does not use up the choice.
- In both paths the ROM jumps to `0x8000_0000` with `a0 = hartid` and `a1 = DTB` (the DTB is stored in the BootROM).

| Boot ROM parameter | Value |
|---|---|
| Mailbox | `0x8FFF_F800`, 64-bit |
| Linux request magic | `0x4C494E5558534421` (`"LINUXSD!"`) |
| ROM ack | `0x41434B4544545349` (`"ACKEDTSI"`), written back to the mailbox |

---

## 6. Bare-metal Hands-on

### 6.1 Concepts: UART-TSI and HTIF
- **FESVR** (Front-End Server): C++ library that handles host ↔ RISC-V DUT communication: reset, program load/run, peripheral emulation.
- **HTIF** (Host Target InterFace): Berkeley's non-standard protocol FESVR uses to talk to the DUT. Requests and responses go through the `tohost`/`fromhost` memory words (proxying syscalls such as printf and exit).
- **TSI** (Tethered Serial Interface): one implementation of HTIF. Simple read/write commands into the DUT's memory space. Chipyard's default host–DUT link.
- **UART-TSI**: TSI commands carried over UART. On the board, the host writes the ELF directly into DDR without an SD card.

```
       Host (uart_tsi.py, pyserial)                 VCU118
        │  TSI  ─────────────────────────────▶  Memory R/W, ELF loading, MSIP write
        │  HTIF ◀────────────────────────────   tohost   (printf, exit requests)
        │       ─────────────────────────────▶  fromhost (responses)
        ▼
   console output / PASS·FAIL / exit code
```

### 6.2 Execution Flow

```
① ELF Parsing             PT_LOAD segments + symbol table (tohost/fromhost)
② ELF → DDR Load          Write segments to p_paddr, zero-fill .bss
③ Hart0 Start             Write 1 to CLINT MSIP (0x2000000)
④ Rocket starts execution hart0, waiting in the dual-boot ROM (5.3), jumps to 0x80000000
⑤ Gemmini RoCC commands   Test code issues Gemmini commands
⑥ HTIF                    printf → tohost, exit → tohost (odd payload)
⑦ PASS / FAIL             Exit code 0 / 1
```

- ② `crt.S` does not clear .bss, so the loader zero-fills it. Without this, tests fail only on the board because of garbage in DDR.
- ④ hart0 always jumps to `0x80000000`. All binaries in 6.4 start at this address.

### 6.3 Host Tool

The original `generators/testchipip/uart_tsi/` is a C++ program tied to fesvr and termios, so it is **Linux-only** and knows nothing about the dual-boot ROM.
We use `uart_tsi.py` (`notes/vcu118_port/uart_tsi.py`), a Python reimplementation of the host side that also sends the Linux boot request (7.5). Run it from the chipyard root inside the devcontainer, after `source env.sh` (2.2 Step 5).

| | Original (C++) | `uart_tsi.py` |
|---|---|---|
| Dependencies | fesvr, pthread, termios | pyserial |
| Platforms | Linux | Linux / Windows |
| HTIF coverage | Full syscall proxy | Console putchar, `write` (fd 1/2), `exit` |
| Dual-boot Linux request | — | `--boot-linux` |
| Timeout / stall diagnosis | None | 10 s response timeout; warns with the likely cause after 30 s without progress |
| TSI bytes on the wire | — | **Identical to the original** |

| Mode | Command | What it does |
|---|---|---|
| Link check | `--read ADDR` | Reads one 32-bit word over TSI and exits. Starts nothing, so the boot choice is still open |
| Bare-metal | `<ELF>` | Loads the ELF's `PT_LOAD` segments, zero-fills `.bss`, sets MSIP, then serves HTIF until the program exits |
| Linux | `--boot-linux` | Writes the magic to the boot mailbox once per second until the console prints `INIT`, then exits. The port then belongs to the console (7.5) |

```bash
T="python3 notes/vcu118_port/uart_tsi.py --tty /dev/ttyUSB1 --baud 921600"
$T --read 0x80000000          # 1. link check: read a single word (no load, no start)
$T <ELF>                      # 2. run a bare-metal program
$T --boot-linux               # 3. boot Linux from the SD card (chapter 7)
```

| Option | Default | Notes |
|---|---|---|
| `--tty` | (required) | `/dev/ttyUSB1` here (`COM3` etc. on Windows) |
| `--baud` | 115200 | **Always pass `--baud 921600`** — the board's UART-TSI baud rate |
| `--timeout` | 10 s | Timeout for each TSI response |
| `--boot-timeout` | 30 s | How long `--boot-linux` keeps asking |
| `--boot-retry` | 1 s | Interval between mailbox writes |
| `--boot-mailbox` | `0x8FFFF800` | Must match `sd.c` |

⚠️ **Caution**: Close any terminal program (picocom, etc.) on the port before you run the script. Two programs reading the same port steal each other's bytes.

Exit codes of `uart_tsi.py`:

| Code | Meaning |
|---|---|
| `0` | The program exited with 0, `--read` succeeded, or `--boot-linux` saw `INIT` |
| `1` | The program exited with a non-zero value (printed as `exit code N`) |
| `2` | Bad arguments, `cannot open port: …` (wrong port name, or another program such as a terminal holds it), `no response from target (…)`, or no `INIT` within `--boot-timeout` |
| `130` | Interrupted with Ctrl+C |

### 6.4 Building the Test Binaries

Build all three in the devcontainer. The ELFs are run in place from the chipyard root (6.5).

#### Step 1. chipyard-tests
```bash
source env.sh
cd tests
cmake -S ./ -B ./build/ -D CMAKE_BUILD_TYPE=Debug
cmake --build ./build/ --target hello cpp-hello mt-hello
```
- Output: `tests/build/*.riscv` (linked with `htif_nano.specs`, start address `0x80000000`)
- `--target all` builds everything, but the tests below use devices this SoC does not have (MMIO accelerators, RoCC examples, NIC, block device, etc.), so they are excluded:
  `accum`, `charcount`, `gcd`, `fft`, `pwm`, `blkdev`, `big-blkdev`, `nic-loopback`, `pingd`, `nvdla`, `spiflashread`, `spiflashwrite`, `streaming-fir`, `streaming-passthrough`, `ctc-test`, `multi-ctc-test`, `triple-chiplet`

#### Step 2. riscv-tests/benchmarks
Already built and installed by `./build-setup.sh riscv-tools`.
```bash
ls $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/*.riscv
```
To rebuild:
```bash
source env.sh
cd toolchains/riscv-tools/riscv-tests
mkdir -p build && cd build
../configure --prefix=$RISCV/riscv64-unknown-elf --with-xlen=64
make benchmarks
ls benchmarks/*.riscv
```
- Outputs: `dhrystone`, `median`, `memcpy`, `mm`, `multiply`, `qsort`, `rsort`, `spmv`, `towers`, `vvadd`, `mt-*`, `vec-*`, etc. (`*.riscv`)
- `vec-*` needs the Vector extension, so it is not run on this SoC (RV64GC).

#### Step 3. gemmini-rocc-tests (bare-metal)
```bash
source env.sh
cd generators/gemmini/software/gemmini-rocc-tests
./build.sh                    # or ./build.sh BAREMETAL_ONLY=1 for the bare-metal tests only
```
- If `riscv64-unknown-linux-gnu-gcc` is on PATH, it builds all three variants: `-baremetal`, `-pk`, and `-linux` (the `-linux` ones go into the Linux image, 7.2).
- Outputs: `build/bareMetalC/*-baremetal`, `build/imagenet/{mobilenet,resnet50}-baremetal`
- `include/gemmini_params.h` already matches this bitstream's config (4.3); use it as checked out.
- If linking fails with `cannot open linker script file .../riscv-tests/benchmarks/common/test.ld`, the `riscv-tests` submodule inside gemmini-rocc-tests is empty (9.2).

⚠️ **Caution**: Pass the ELF itself to `uart_tsi.py` — not the `.dump` file, and do not `strip` it. uart_tsi.py looks up `tohost`/`fromhost` in the ELF symbol table; without them it only prints `no tohost/fromhost symbols; exiting without HTIF.` and exits with no test output.

### 6.5 Labs

> ⚠️ **One workload per reset.** After a program exits, hart0 does not go back to the boot ROM's wait loop. Press **SW5 CPU_RESET** before every run (right after power-up the ROM is already waiting). Without a reset the script fails with `no response from target (…)`.
>
> All commands below run from the chipyard root in the devcontainer, with `source env.sh` and
> `T="python3 notes/vcu118_port/uart_tsi.py --tty /dev/ttyUSB1 --baud 921600"` (6.3).
> General form: press CPU_RESET, then `$T <workload path>`.
>
> All labs below pass on the `GemminiVCU118DualBootConfig` bitstream, with the outputs shown.

#### Lab 0. Link check
**Goal**: Confirm the UART-TSI link and DDR access work.
```bash
$T --read 0x80000000
```
**Expected output**:
```text
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  [0x80000000] = 0x00050433
```
The value itself is meaningless. It changes with the DDR contents after power-up or the last program loaded.

✅ **Checkpoint**: A value is printed. If the target does not answer, `uart_tsi.py` prints `no response from target (…)` and exits with code 2.

#### Lab 1. Rocket only
**Goal**: Confirm the Rocket core and HTIF output work, without Gemmini.
```bash
$T tests/build/hello.riscv
#   [CPU_RESET]
$T $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/dhrystone.riscv
```
**Expected output**:
```text
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  hello.riscv  entry 0x80000000
  segment 0x80000000-0x80001ed8  7.7 KB
  loading  90.1%  (0 s)
  .bss    0x80001ed8-0x80002240  0.9 KB zero-filled
  loading 100.0%  (0 s)
  loaded 8.6 KB in 0.1 s
  waking hart0
  HTIF attached  tohost=0x80001e00 fromhost=0x80001e08

Hello world from core 0, a rocket

  exit code 0
```
```text
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  dhrystone.riscv  entry 0x80000000
  segment 0x80000000-0x800001cc  0.4 KB
  loading   2.2%  (0 s)
  segment 0x80001000-0x80003824  10.0 KB
  loading  51.0%  (0 s)
  .bss    0x80003824-0x80006070  10.1 KB zero-filled
  loading 100.0%  (0 s)
  loaded 20.6 KB in 0.3 s
  waking hart0
  HTIF attached  tohost=0x80001000 fromhost=0x80001040

Microseconds for one run through Dhrystone: 449
Dhrystones per Second:                      2222
mcycle = 225007
minstret = 186031

  exit code 0
```

✅ **Checkpoint**: `exit code 0`

#### Lab 2. Basic Gemmini operations
**Goal**: Confirm a minimal Gemmini round trip and matmul work.
```bash
G=generators/gemmini/software/gemmini-rocc-tests/build
$T $G/bareMetalC/template-baremetal
#   [CPU_RESET]
$T $G/bareMetalC/mvin_mvout-baremetal
#   [CPU_RESET]
$T $G/bareMetalC/tiled_matmul_ws-baremetal
```
**Expected output** (`tiled_matmul_ws`):
```text
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  tiled_matmul_ws-baremetal  entry 0x80000000
  segment 0x80000000-0x800001c8  0.4 KB
  loading   0.6%  (0 s)
  segment 0x80001000-0x80003450  9.1 KB
  loading  12.9%  (0 s)
  .bss    0x80003450-0x800134a0  64.1 KB zero-filled
  loading 100.0%  (1 s)
  loaded 73.6 KB in 1.0 s
  waking hart0
  HTIF attached  tohost=0x80001000 fromhost=0x80001040

MAT_DIM_I: 64
MAT_DIM_J: 64
MAT_DIM_K: 64
Starting gemmini matmul
Cycles taken: 2412
Starting slow CPU matmul
Cycles taken: 3227178

  exit code 0
```

✅ **Checkpoint**: `exit code 0`

#### Lab 3. DNN workloads
```bash
$T $G/imagenet/mobilenet-baremetal
#   [CPU_RESET]
$T $G/imagenet/resnet50-baremetal
```
See 6.6 for how to read the output.

### 6.6 Reading the Output

`resnet50-baremetal` run log:

```text
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  resnet50-baremetal  entry 0x80000000
  segment 0x80000000-0x800001c8  0.4 KB
  loading   0.0%  (0 s)
  segment 0x80001000-0x8190e504  25.1 MB
  loading  20.0%  (… s)
  .bss    0x8190e504-0x87d820d0  100.5 MB zero-filled
  loading 100.0%  (… s)
  loaded 125.5 MB in … s
  waking hart0
  HTIF attached  tohost=0x80001000 fromhost=0x80001040

conv_1 cycles: ...
matmul_2 cycles: ...
...
Total cycles: 100181838 (100%)
Matmul cycles: 35113272 (35%)
Im2col cycles: 0 (0%)
Conv cycles: 53238468 (53%)
Pooling cycles: 0 (0%)
Depthwise convolution cycles: 0 (0%)
Res add cycles: 11688823 (11%)
Other cycles: 141275 (0%)
PASS

  exit code 0
```
The `loading` line updates in place while loading; the log above shows it as it looks at the end of each stage (`… s` varies per run).

| Part | Meaning |
|---|---|
| Segment 1 (0x80000000~) | First `PT_LOAD` (R E). `.text.init` — boot code, where hart0 starts executing |
| Segment 2 | The part of the second `PT_LOAD` with actual file contents (filesz). `.tohost`, `.text`, `.rodata` (weights), etc. |
| .bss | The memsz − filesz range. Not in the file, so it is zero-filled (activation buffers, etc.) |
| waking hart0 | CLINT MSIP write → Rocket starts executing |
| HTIF attached | Starts polling the `tohost` / `fromhost` addresses found in the ELF symbol table |
| Cycle breakdown | Sum of 7 instrumented regions. Not the whole program's runtime |
| PASS / exit code | Classification of 4 images compared with the expected answers / HTIF exit value |

- Check the section → segment mapping with "Section to Segment mapping" from `riscv64-unknown-elf-readelf -lW <elf>`.
- Judge by the **exit code**, not by the printed strings.

### 6.7 Limitations
- `gemmini-rocc-tests` workloads with funct7 = 23 (using the external scratchpad, `*_spad`) are not supported

---

## 7. Linux Hands-on

Linux experiments use the same `GemminiVCU118DualBootConfig` bitstream as chapter 6; only the boot request differs (5.3). The SD card goes into PMOD J52.

### 7.0 Hands-on Flow

```
[Build server (devcontainer)]
Build the FireMarshal image → *-bin-nodisk-flat                         (7.2)
    ↓ (copy to the machine with the SD reader, if it is not the server)
[Linux host with a USB SD reader, or Windows + WSL2]
Check partition 1 starts at sector 34 → raw write to partition 1 → verify   (7.3)
    → SD card → PMOD microSD adapter → VCU118 PMOD J52
[VCU118, bitstream already in flash]
CPU_RESET → uart_tsi.py --boot-linux → picocom 115200                    (7.5)
    → sdboot copies the SD payload → OpenSBI → Linux → Buildroot
    → log in as root / fpga                                             (7.5 Step 4)
    → run Gemmini tests                                                 (7.6)
```

### 7.1 Boot Flow

```
CPU_RESET → boot ROM waits → host: --boot-linux → ACK, TSI gate closed → SD Initialization → Payload Loading
          → OpenSBI → Linux / Buildroot → Gemmini Application
```

- **BootROM (sdboot -DDUALBOOT)**: waits for the host (5.3). On the Linux request it closes the TSI gate, initializes the SD card in SPI mode at 300 kHz, then — without reading the partition table — copies **`PAYLOAD_SIZE_B` = 48 MiB starting at absolute sector 34** to `0x80000000` at 1 MHz and jumps there. It does not look at the payload size.
- **payload**: the flat binary built by FireMarshal = OpenSBI `fw_payload` + Linux `Image` + embedded initramfs (nodisk). Byte 0 = physical address `0x80000000`.
- **Console**: `CONFIG_CMDLINE="console=ttyS0 console=ttySIF0 earlycon"` + `CMDLINE_FORCE` → SiFive UART at `0x64000000`, on the same USB-UART as UART-TSI. Its baud rate changes during the boot (7.5).

### 7.2 Building the Linux Image (FireMarshal)

Put the 56 gemmini-rocc-tests, mobilenet, and resnet50 into one image (`gemmini-all`).
The workload name and contents below are an example. You may change which binaries go into the overlay (within the size limit in 7.4).

#### Step 0. Apply the FireMarshal / OpenSBI patches (once)
Apply the patches in `notes/vcu118_port/patches/` to the two submodules. From the Chipyard root:
```bash
git -C software/firemarshal apply "$PWD/notes/vcu118_port/patches/firemarshal-prototype-buildroot-config.patch"
git -C software/firemarshal/boards/default/firmware/opensbi apply "$PWD/notes/vcu118_port/patches/opensbi-sbi_pmu-keep-cy-ir.patch"
```
Check that they applied:
```bash
git -C software/firemarshal diff --stat -- boards/prototype/base-workloads/br-base/buildroot-config
git -C software/firemarshal/boards/default/firmware/opensbi diff --stat
```
**Expected output**:
```text
 boards/prototype/base-workloads/br-base/buildroot-config | 13 ++++++++++++-
 1 file changed, 12 insertions(+), 1 deletion(-)
 lib/sbi/sbi_pmu.c | 16 ++++++++++++++++
 1 file changed, 16 insertions(+)
```

| Patch | Change | Reason |
|---|---|---|
| `firemarshal-prototype-buildroot-config.patch` | **Replaces** the root password `"fpga"` with an MD5 hash (`$1$`); adds `BR2_TOOLCHAIN_EXTERNAL_FORTRAN=y` / `OPENMP=y` | Login failure; buildroot toolchain check failure (9.1) |
| `opensbi-sbi_pmu-keep-cy-ir.patch` | `pmu_ctr_stop_hw()` no longer stops the CY(0)·IR(2) counters | User-mode `rdcycle` returns 0 (9.1) |

⚠️ **Caution**: The patches are against FireMarshal `21119e5` / OpenSBI `5ccebf0` from 0.2. If `apply` fails, check the submodule commits (`git submodule status software/firemarshal`). Running it again after the patches are already applied is expected to fail.

#### Step 1. Get the test binaries
```bash
source env.sh
(cd generators/gemmini/software/gemmini-rocc-tests && ./build.sh)
ls generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/*-linux | wc -l   # 56
ls generators/gemmini/software/gemmini-rocc-tests/build/imagenet/*-linux
```

#### Step 2. Workload directory + overlay
```bash
W=software/gemmini-all
mkdir -p $W/overlay/root/gemmini $W/overlay/root/imagenet $W/overlay/etc/sysctl.d
cp generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/*-linux $W/overlay/root/gemmini/
cp generators/gemmini/software/gemmini-rocc-tests/build/imagenet/{mobilenet,resnet50}-linux $W/overlay/root/imagenet/
chmod 755 $W/overlay/root/imagenet/*

# Allow user-mode rdcycle (applied by S02sysctl at boot)
cat > $W/overlay/etc/sysctl.d/90-perf-user-access.conf <<'EOF'
# Gemmini tests read cycle/instret from user mode (rdcycle/rdinstret).
kernel.perf_user_access = 2
EOF
```

#### Step 3. initramfs compression kfrag and workload JSON
Create these two new files with the contents below (in `software/gemmini-all/`, created in Step 2).

`software/gemmini-all/gemmini-all.kfrag`:
```
CONFIG_RD_GZIP=y
CONFIG_INITRAMFS_COMPRESSION_GZIP=y
# CONFIG_INITRAMFS_COMPRESSION_NONE is not set
```

`software/gemmini-all/gemmini-all.json`:
```json
{
  "name" : "gemmini-all",
  "workdir" : ".",
  "base" : "br-base.json",
  "overlay" : "overlay",
  "linux" : { "config" : "gemmini-all.kfrag", "modules" : {} }
}
```
⚠️ **Caution**:
- `"workdir" : "."` is required — without it, workdir becomes `<JSON directory>/<name>` and the overlay is not found.
- `"modules" : {}` is required — with `linux.config`, a FireMarshal `inheritLinuxOpts` bug raises `KeyError: 'modules'`.
- Without gzip, the flat image is ~60 MB and exceeds the 48 MiB limit.

#### Step 4. Build the image (inside the devcontainer)
```bash
source env.sh
cd software/firemarshal
./marshal -d build   ../gemmini-all/gemmini-all.json
./marshal -d install -t prototype ../gemmini-all/gemmini-all.json
```
- `-d` (nodisk): the rootfs is embedded in the kernel as an initramfs. The prototype board supports only nodisk.
- `install -t prototype` builds the flat image with `objcopy -O binary --change-addresses -0x80000000`.
- The warning `opensbi has uncommited changes ... will be rebuilt` is expected: the OpenSBI change from Step 0 is not committed.

#### Step 5. Verify
```bash
cd software/firemarshal/images/prototype/gemmini-all
grep INITRAMFS_COMPRESSION linux_config            # GZIP=y  ← check this before the size
stat -c %s gemmini-all-bin-nodisk-flat             # ≤ 50331648 (48 MiB)
cpio -it < initramfs.cpio | grep -c '^root/gemmini/.*-linux$'   # 56
stat -c '%y %s' gemmini-all-bin-nodisk-flat        # modification time = the build you just ran
sha256sum gemmini-all-bin-nodisk-flat              # compare after copying (7.3 Step 1)
```

**Output**: `software/firemarshal/images/prototype/gemmini-all/gemmini-all-bin-nodisk-flat`
(For reference: 42,813,448 B in our run, 85.0% of the 48 MiB limit)

⚠️ **Caution**: The file written to the SD card must be **`-bin-nodisk-flat`**. The `-bin-nodisk` (ELF) and `-bin-dwarf-nodisk` files in the same directory cannot be read by sdboot.

✅ **Checkpoint**: `GZIP=y`, size ≤ 50,331,648 B, 56 tests included, modification time is current

### 7.3 Preparing the SD Card and Writing the Image

**Goal**: Write the flat image from 7.2 raw to **partition 1** of the SD card (starting at sector 34).

SD card layout:
```
LBA 0–33   GPT (protective MBR + header + partition entries)
LBA 34~    Partition 1  ← flat image (sdboot reads PAYLOAD_SIZE_B from here, 7.1)
           (Partition 2 — optional, regular filesystem)
```

⚠️ **Caution — the most important distinction in this whole section**
- Write to **partition 1 (`/dev/sdX1`)**. Writing to the whole disk (`/dev/sdX`) destroys the GPT.
- **Always identify the device name by its size.** Guessing can overwrite a system disk.
- Do not use Rufus / balenaEtcher / Win32 Disk Imager — they write from sector 0, destroying the GPT and shifting the payload.
- The SD device is not visible inside the devcontainer. Writing to `/dev/sdX1` there only creates a **regular file** inside the container. Run this section on the server host (outside the container) or on another Linux machine with the SD reader.

#### Step 1. Get the image onto the machine with the SD reader
On the server host itself the image is already at `<chipyard>/software/firemarshal/images/prototype/gemmini-all/gemmini-all-bin-nodisk-flat` (the devcontainer bind-mounts the repository). From another machine:
```bash
scp <user>@<server>:<absolute chipyard path on the host>/software/firemarshal/images/prototype/gemmini-all/gemmini-all-bin-nodisk-flat .
sha256sum gemmini-all-bin-nodisk-flat
```
- Windows + WSL2 also works: copy the file to Windows, then attach the USB SD reader to WSL with `usbipd attach --wsl --busid <BUSID>` (administrator PowerShell, usbipd-win) and run Steps 2–5 in WSL (the file is under `/mnt/c/...`).

✅ **Checkpoint**: Size and SHA256 match the `stat` / `sha256sum` values from 7.2 Step 5.

#### Step 2. Identify the SD card
```bash
lsblk -o NAME,SIZE,TYPE,TRAN,MODEL,MOUNTPOINT   # identify the SD card by size (replace /dev/sdX below)
```
Expected output (e.g. when the card shows up as `sdc`):
```text
sdc        29.7G disk usb
├─sdc1      512M part
└─sdc2     ...   part
```

#### Step 3. Check partition 1's start sector (partition a new card)
```bash
sudo fdisk -l /dev/sdX
```
```text
Device      Start     End  Sectors  Size Type
/dev/sdX1      34 1048609  1048576  512M ...
```
- **If `sdX1` starts at 34, skip partitioning.** A card that has already booted to `buildroot login:` has a verified layout, so just replace the image without reformatting.
- If the start is not 34, or the card is new, partition it as below (**erases the whole card**). GPT, alignment 1, 512 MiB starting at sector 34:
  ```bash
  sudo sgdisk --zap-all /dev/sdX                            # delete the existing partition table
  sudo sgdisk -o -a 1 -n 1:34:+1048576 -t 1:af0a /dev/sdX   # 1,048,576 sectors (512 MiB) from sector 34
  sudo sgdisk -p /dev/sdX                                   # confirm partition 1 starts at sector 34
  ```

✅ **Checkpoint**: Partition 1 Start = 34

#### Step 4. Write the image to partition 1
Unmount first if mounted:
```bash
mount | grep sdX
sudo umount /dev/sdX1 /dev/sdX2 2>/dev/null
```
```bash
IMAGE=<path>/gemmini-all-bin-nodisk-flat
ls -lh "$IMAGE"
sudo dd if="$IMAGE" of=/dev/sdX1 bs=4M status=progress conv=fsync
sync
```

#### Step 5. Verify the write
```bash
sudo blockdev --getsize64 /dev/sdX1        # partition size > image size
SIZE=$(stat -c %s "$IMAGE")
sudo cmp -n "$SIZE" "$IMAGE" /dev/sdX1; echo rc=$?
```
✅ **Checkpoint**: `cmp` prints nothing and `rc=0` — the first `SIZE` bytes of the SD card match the image.

#### Step 6. Insert into the board
Run `sync`, eject the card, put the microSD into the PMOD microSD adapter, and connect it to **PMOD J52** on the VCU118, top row (J52 is the SPI SD card in this config, 3.1 `VCU118ShellPMOD = "SDIO"`).

### 7.4 SD Bootloader Settings

The dual-boot BootROM takes its SD settings from the config (`WithDualBootROM`, 3.1); they are passed to sdboot as `-D` flags during elaboration. No patch is needed.

| Item | sdboot default (`sd.c`) | Dual-boot config | Reason |
|---|---|---|---|
| `PAYLOAD_SIZE_B` | 48 MiB | **48 MiB** (`payloadSizeB = 48 << 20`) | The image with 56 tests + mobilenet + resnet50 (42.8 MB) exceeds 30 MiB |
| `SPI_INIT_CLK` (CMD0–CMD16) | 300 kHz | **300 kHz** (`spiInitClkKHz`) | Within the 100–400 kHz range the SD spec requires during initialization. Our card does not initialize at 25 MHz |
| `SPI_CLK` (CMD18 copy) | 300 kHz | **1 MHz** (`spiClkKHz`) | Measured limit about 3 MHz (below); 1 MHz keeps a ~3.5× margin |
| `BBL_PARTITION_START_SECTOR` | 34 | 34 | Partition 1 start (7.3) |

```c
// fpga/src/main/resources/vcu118/sdboot/sd.c (excerpt)
#ifndef SPI_CLK
#define SPI_CLK 	300
#endif
#ifndef SPI_INIT_CLK
#define SPI_INIT_CLK	300
#endif
...
#define SPI_DIV 	(((F_CLK * 1000) / SPI_CLK) / 2 - 1)
#define SPI_INIT_DIV	(((F_CLK * 1000) / SPI_INIT_CLK) / 2 - 1)
```

- **Init and copy clocks are separate.** `sd_poweron()` (initialization, CMD0–CMD16) uses `SPI_INIT_DIV` from `SPI_INIT_CLK`, and `copy()` (payload copy, CMD18) uses `SPI_DIV` from `SPI_CLK`.
- `F_CLK` is the pbus clock (MHz). At 50 MHz: `SPI_INIT_DIV` = 50000 / 300 / 2 − 1 = **82** (SCK = 50 MHz / (2 × 83) ≈ **301 kHz**), `SPI_DIV` = 50000 / 1000 / 2 − 1 = **24** (exactly **1 MHz**). Copying 48 MiB takes about 7 minutes.
- **Expected limit of `SPI_CLK`: about 3 MHz.** Measured with our card and the Pmod microSD adapter on J52 (level-shifted by U41), with the host driving the SPI controller over TSI while the ROM waited; CMD18 reads at sector 34 were checked against CRC16 and a 300 kHz reference:

  | SPI clock | Result |
  |---|---|
  | 301 kHz – 3.125 MHz | Pass |
  | 3.57 MHz | Fail: CMD18 returns R1 `0x08` (CRC error) |
  | 5 MHz and above | Fail: no response to CMD18 (`sd_cmd: timeout`); 12.5 MHz always fails |

  The limit depends on the card and adapter. Keep `SPI_CLK` at 1 MHz or below for margin.
- Loading an image of 48 MiB or less is fine. **A larger image leaves the tail unloaded and boot fails silently** (9.1).

⚠️ **Caution** (when changing): These values are part of the BootROM, so **the bitstream must be regenerated** after changing them. Changing only `sd.c` does not trigger re-elaboration (see the caution in 4.1).

### 7.5 Booting

#### Step 1. Console baud rates
The console runs at two baud rates during one Linux boot:

| Stage | Baud | Why |
|---|---|---|
| Boot ROM (`INIT` … `BOOT`) | 921600 | UART reset value (`WithVCU118PeripheralMap`) |
| OpenSBI and the early kernel log (earlycon) | 921600 | The DTS UART node has no `clock-frequency`, so OpenSBI keeps the existing divisor |
| From `printk: console [ttySIF0] enabled` onward, including login | **115200** | The Linux SiFive UART driver defaults to 115200 when `console=ttySIF0` gives no rate |

So a terminal at 115200 shows `�` characters until the kernel console switches, and a terminal at 921600 shows garbage after it switches.

#### Step 2. CPU_RESET → request the Linux path → open the console
With the SD card in J52 (7.3 Step 6):
```bash
# 1. Press SW5 CPU_RESET
# 2. Request the Linux path; the script exits as soon as the ROM prints INIT
python3 notes/vcu118_port/uart_tsi.py --tty /dev/ttyUSB1 --baud 921600 --boot-linux
# 3. Open the console at 115200 (login runs at this rate)
picocom -b 115200 /dev/ttyUSB1
```
**Expected output**:
```text
$ python3 notes/vcu118_port/uart_tsi.py --tty /dev/ttyUSB1 --baud 921600 --boot-linux
uart_tsi.py  port /dev/ttyUSB1  921600 baud
  boot ROM took the SD path after ~0 s (1 request)
INIT
CMD0
CMD8
ACMD41

  the line now belongs to the console; open a terminal on the same port

$ picocom -b 115200 /dev/ttyUSB1
picocom v3.1

port is        : /dev/ttyUSB1
flowcontrol    : none
baudrate is    : 115200
...
Type [C-a] [C-h] to see available commands
Terminal ready
�����������������������������������        ← ROM copy spinner + OpenSBI/early kernel log at 921600 (about 7 min)
```
- The script exits as soon as it sees `INIT`. The ROM lines after `INIT` that the script did not read are lost, which is why the output stops at `ACMD41` here.
- The `�` characters are normal (Step 1). To read the ROM and OpenSBI log instead, open picocom at 921600; when the text turns to garbage at the kernel console switch, press **Ctrl-A Ctrl-D** three times to drop to 115200 (Ctrl-A Ctrl-U raises it again).
- **Exit picocom** with **Ctrl-A** then **Ctrl-X** (Ctrl-C is sent to the board). Exiting does not reset the board; Linux keeps running and the same command reopens the console. Exit picocom before running `uart_tsi.py` again.

The ROM log at 921600, for reference:
```text
INIT
CMD0
CMD8
ACMD41
CMD58
CMD16
CMD18
LOADING 0x03000000 B PAYLOAD      ← 48 MiB
LOADING  -/|\                     ← about 7 min at 1 MHz
BOOT
```

#### Step 3. Linux → Buildroot
Once the kernel console switches to 115200, the boot log becomes readable and ends at:
```text
...
Running sysctl: OK
...
Welcome to Buildroot
buildroot login:
```
If no prompt appears after the boot finishes, press Enter.

#### Step 4. Log in
- ID: `root` / PW: `fpga`

✅ **Checkpoint**: A shell prompt (`#`) after login. `Login incorrect` means the password change from 7.2 Step 0 is not in the new image (9.1).

✅ **Checkpoint**: `cat /proc/version` shows `Linux version ... #N SMP <build time>` of the image you just built — the easiest way to confirm the new image made it onto the SD card (the same line in the boot log is printed at 921600, so it is not readable at 115200).

#### Diagnosing by the boot log
How far the boot gets tells you which parts are working. The ROM lines are printed at 921600.

| Last output seen | Confirmed working | Suspect next |
|---|---|---|
| `no INIT within 30 s` from the script | — | No CPU_RESET since the last run, port / baud (5.1), bitstream not loaded (DONE LED, 5.2) |
| `INIT` | Rocket runs the BootROM, UART OK | SD card / PMOD J52 seating (9.1) |
| `CMD0` … `CMD16` | SPI SD card initializes at 300 kHz | — |
| `CMD18` → `LOADING  sd_cmd: timeout` → `ERROR` | SD init | SPI copy clock too fast for this card (7.4, 9.1) |
| `CRC mismatch` during `LOADING` | SD read path | Bad card or bad write (7.3) |
| `LOADING 0x… B PAYLOAD` → `BOOT` | SD → DDR copy | Payload size / position (7.4, 7.3 Step 3) |
| Readable kernel log at 115200 | Base SoC: Rocket + DDR + interrupts / timers, OpenSBI | initramfs, kernel config (7.2) |
| `buildroot login:` → successful login | FireMarshal rootfs | — |
| Gemmini test `rc=0` (7.6) | RoCC / Gemmini functionality | — |

### 7.6 Running Gemmini Applications

#### 7.6.0 Check the test binaries
After logging in, check that the binaries added through the overlay (7.2 Step 2) are in the image.
```sh
ls /root/gemmini | wc -l          # 56
ls -l /root/imagenet              # mobilenet-linux, resnet50-linux, check the execute bit (x)
```
If a file is not executable, run `chmod +x <file>` (the real fix is to check the `chmod 755` in 7.2 Step 2 and rebuild the image).

✅ **Checkpoint**: 56 files in `/root/gemmini`, 2 executable files in `/root/imagenet`

#### 7.6.1 gemmini-rocc-tests (`/root/gemmini/*-linux`)
These are the `-linux` variants of the `bareMetalC/` sources built for Linux. The bare-metal runs in chapter 6 use the `-baremetal` variants of the same sources.

```sh
cd /root/gemmini
./tiled_matmul_ws-linux; echo rc=$?
```

- Judge **only by the exit code** (`rc=0` → pass). Output strings differ per test, and some tests such as `matmul*` print nothing.
- busybox `ash` has no `PIPESTATUS`, so do not use `./t | grep ...; echo $?` — there `$?` is `grep`'s exit code.

#### 7.6.2 imagenet (`/root/imagenet/*-linux`)
```
./<model>-linux [ws|os|cpu] [conv|matmul] [check]
```
- All three arguments are **positional** — none can be skipped. To use `check`, fill in the first two.
- ⚠️ The usage string from `-h` is **wrong** (it shows two arguments). `./mobilenet-linux ws check` gives `Unknown command-line argument`. The correct call is `ws conv check`.
- `os conv` is rejected by the software (`Gemmini convs do not currently support OS`).
- With `check`, the CPU recomputation falls inside the measured cycle regions, so the cycle counts are meaningless. Run performance measurements and correctness checks separately.

```bash
cd /root/imagenet
time ./mobilenet-linux ws conv
```
**Expected output**:
```text
conv_1: ...
conv_dw_2: ...
matmul_3: ...
...
matmul_53: ...
Prediction: 75 (score: 127)
Prediction: 900 (score: 127)
Prediction: 125 (score: 103)
Prediction: 897 (score: 98)

Total cycles: 120340273 (100%)
Matmul cycles: 9905159 (8%)
Im2col cycles: 0 (0%)
Conv cycles: 2222171 (1%)
Pooling cycles: 0 (0%)
Depthwise convolution cycles: 105415237 (87%)
Res add cycles: 596428 (0%)
Other cycles: 2201278 (1%)
PASS
real    0m 5.24s
user    0m 2.41s
sys     0m 2.79s
```
Cycle counts may vary slightly between runs.

Interpretation:
- **Prediction**: argmax class and score for each of the 4 images in the batch. `PASS` if they match the expected `{75, 900, 125, 897}`.
- **Total cycles**: sum of 7 instrumented regions (not the whole program's runtime).
- **Clock check**: `Total cycles ÷ user` ≈ 50 MHz (120,340,273 ÷ 2.41 s ≈ 49.9 MHz). Dividing by `real` mixes in `sys` time (`mlockall`, etc.) and looks like 23 MHz, which is misleading.
- **sys is more than half**: likely because `mlockall(MCL_CURRENT|MCL_FUTURE)` faults in and locks ~32 MB, including 28 MB of bss.
- **Depthwise 87%**: Amdahl's law. On the CPU (`cpu conv`) it was 22%, but depthwise gets much less speedup (31×) than matmul (1033×), so its share flips.

---

## 8. Results

### 8.1 Test Results (Rocket + Gemmini SoC)

| Test | Bare-metal | Linux |
|---|---|---|
| gemmini-rocc-tests | PASS* | PASS** |
| ImageNet/mobilenet | PASS | PASS |
| ImageNet/ResNet50 | PASS | PASS |

\* Excluding `*_spad` workloads (6.7)
\*\* 54 of 56 pass. `gemmini_counter` and `global_average` fail.

All results above were confirmed on the `GemminiVCU118DualBootConfig` bitstream: bare-metal through `uart_tsi.py <ELF>` and Linux through `uart_tsi.py --boot-linux`, on the same board without reprogramming.

### 8.2 Open Items
- [ ] Find the cause of the `gemmini_counter` and `global_average` failures
- [ ] Per-layer depthwise analysis (17 `conv_dw_N:` lines) and review of better mappings

---

## 9. Troubleshooting / FAQ

> Format: **Symptom → Cause → Fix**

### 9.1 Linux
#### SD initialization stops at the CMD stage (`INIT` → `sd_cmd: timeout` → `ERROR`)
- Cause: the card does not answer the initialization commands. Upstream sdboot ran them at 25 MHz, which our card does not support; this config initializes at 300 kHz (7.4), so a failure here usually means no electrical contact.
- Fix:
  1. Check the SD card and PMOD adapter seating: J52, top row, pin 1 = CS
  2. Re-insert the card and press CPU_RESET (the card keeps its state across an FPGA reset)
  3. Try another card (a microSDHC 4–32 GB card is the safest; SDSC ≤ 2 GB cards fail CMD8)

#### Copy fails right after `CMD18` (`LOADING  sd_cmd: timeout` → `ERROR`)
- Cause: the SPI copy clock is too fast for the card and the J52 level shifter. On our board copies fail from 3.57 MHz; an earlier version of this config copied at 12.5 MHz and always failed here (7.4).
- Fix: keep `spiClkKHz` at 1 MHz (the `WithVCU118DualBootTweaks` default) or lower, and regenerate the bitstream (4.1).

#### Boot fails after payload loading (hangs with no message)
- Cause: the payload is larger than `PAYLOAD_SIZE_B` — sdboot ignores the size and reads a fixed amount
- Fix:
  1. Check the BootROM — the ROM log at 921600 should say `LOADING 0x3000000` (48 MiB). `0x1e00000` (30 MiB) means the config's `payloadSizeB` was not set (7.5 Step 2)
  2. Check and shrink the image — `stat -c %s ...-flat` ≤ 50,331,648 B, confirm gzip initramfs compression (7.2 Step 5). You can also drop binaries from the overlay
  3. If 48 MiB is still not enough, increase `payloadSizeB` in `GemminiVCU118DualBootConfig` and regenerate the bitstream (4.1, 7.4)

#### Only `�` at 115200, even after 10 minutes
- Cause: either the boot stopped in the ROM (its messages are at 921600), or the prompt has scrolled by.
- Fix: press Enter. If nothing appears, switch picocom to 921600 (Ctrl-A Ctrl-U three times) and look for `ERROR`; or press CPU_RESET and boot again with the console at 921600 (7.5 Step 2).

#### `root` / `fpga` login fails (`Login incorrect`)
- Cause: in a nodisk initramfs, FireMarshal's static busybox overwrites buildroot's busybox. This busybox has no SHA crypt, so it cannot verify buildroot's default SHA-256 (`$5$`) hash.
- Fix: set the root password as a precomputed MD5 (`$1$`) hash (7.2 Step 0).

#### Gemmini tests die with `Illegal instruction` (SIGILL, `rc=132`)
- Cause: the faulting instruction is `rdcycle`, not a Gemmini instruction. Linux 6.6 blocks user-mode cycle/instret reads by default (`scounteren = 0x2`). Bare-metal runs in M-mode, so it is unaffected.
- Fix: `kernel.perf_user_access = 2` — temporarily with `echo 2 > /proc/sys/kernel/perf_user_access`, permanently with `/etc/sysctl.d/90-perf-user-access.conf` in the overlay (7.2 Step 2).

#### `Cycles taken: 0`
- Cause: at boot, Linux sends SBI `COUNTER_STOP` to every counter, and OpenSBI sets CY·IR in `mcountinhibit`. `perf_user_access=2` only grants read permission.
- Fix: change OpenSBI's `pmu_ctr_stop_hw()` so it does not stop CY(0)·IR(2) (7.2 Step 0). PASS/FAIL results are valid even without the fix.
- Note: QEMU does not model `scounteren`/`mcountinhibit`, so neither problem reproduces there. They can only be seen on the board.

#### `marshal build` fails immediately with `KeyError: 'modules'`
- Cause: a FireMarshal `inheritLinuxOpts` bug — triggered by `linux.config` in the workload JSON.
- Fix: add `"modules" : {}` to the JSON. To check without building: `./marshal -d clean <json>`.

#### buildroot build fails (`You must install '/usr/bin/file'`, `cpio`, `Fortran support is not selected`)
- Cause: missing container packages; missing toolchain declarations in the prototype board's buildroot-config
- Fix: `apt-get install file cpio` (2.2 Step 1); `FORTRAN=y`, `OPENMP=y` in buildroot-config (7.2 Step 0)

#### `./mobilenet-linux ws check` → `Unknown command-line argument`
- Cause: the `-h` usage string is wrong. `check` is the third positional argument.
- Fix: `./mobilenet-linux ws conv check`

#### `matmul*` shows `rc=1` in a test loop
- Cause: in `./t | grep ...; echo $?`, `$?` is `grep`'s exit code. `matmul*` prints nothing, so grep returns 1.
- Fix: run the test on its own and check its exit code directly (7.6.1).

### 9.2 Build, host and `uart_tsi.py`
- **`Failed to run dtc; is it in your path?` during elaboration** (`dtc: error while loading shared libraries: libyaml-0.so.2`) → Vivado's `settings64.sh` put Vivado's `dtc` first in `PATH`. Do not source it; append `/opt/Xilinx/2025.2/Vivado/bin` to `PATH` instead (2.2 Step 3). If the conda env was already active, re-running `source env.sh` does not move it back to the front.
- **The Vivado run stopped without an error message** → the devcontainer was stopped (e.g. the VS Code window closed). Keep the window open, or run the build on a host that stays up. `obj/post_*.dcp` checkpoints show how far it got.
- **Linking gemmini-rocc-tests fails with `cannot open linker script file .../riscv-tests/benchmarks/common/test.ld`** → the `riscv-tests` submodule inside gemmini-rocc-tests is empty. Restore it: `git -C generators/gemmini/software/gemmini-rocc-tests/riscv-tests reset --hard HEAD && git -C generators/gemmini/software/gemmini-rocc-tests/riscv-tests submodule update --init`.
- **`no response from target (…)`** → no CPU_RESET since the last run, or the TSI gate is still closed from an earlier `--boot-linux`. Press CPU_RESET. Also check `--baud 921600` and the port (5.1).
- **`no INIT within 30 s`** → the board was not reset, or the ROM already took a path. Press CPU_RESET and retry.
- **`cannot open port`** → another program (picocom) holds the port, or no permission (2.2 Step 4).
- **`/dev/ttyUSB1` is gone inside the devcontainer** → the USB-UART re-enumerated (cable re-plugged, board power-cycled). A privileged container does not see hot-plugged devices; reopen the container, or run `uart_tsi.py` on the host.

### 9.3 Other
- **Not sure the new image made it onto the card** → compare `Linux version ... #N SMP <build time>` in the boot log, and `sha256sum` on the host.
- **The shell says a command does not exist** → with nodisk, only FireMarshal's busybox applets are available (e.g. no `timeout`). Check `images/prototype/<workload>/busybox_config`.
