# Rocket + Gemmini on VCU118 with Chipyard: Hands-on Tutorial (Draft)

- **Author:** Min Jung
- **Paths:** All paths are relative to the Chipyard root (`chipyard/`).
- **Shared files:** Everything needed for the hands-on part is in `notes/vcu118_port/`. On the Windows host, copy this whole folder and use it as the **working folder**; relative paths in Windows commands (`.\uart_tsi.py`, etc.) are relative to it.

  ```text
  notes/vcu118_port/
  ├── README.md                                     # this document
  ├── patches/
  │   ├── firemarshal-prototype-buildroot-config.patch   # 7.2 Step 0
  │   ├── opensbi-sbi_pmu-keep-cy-ir.patch               # 7.2 Step 0
  │   └── sdboot-48MiB-300kHz.patch                      # only when reproducing the bitstream (4.1)
  └── uart_tsi.py                                   # UART-TSI host tool (6.3)
  ```
- **Bitstreams:** Not in the repository; distributed as a GitHub Release (`vcu118-port-v1`). Download them into `bitstream\` inside the Windows working folder (5.2 Step 3).

> **Conventions**
> - Hands-on steps are written in the order **Goal / Commands / Expected output / ✅ Checkpoint / ⚠️ Caution**.

---

## Contents

- [0. Document Info](#0-document-info)
- [1. Overview](#1-overview)
- [2. Prerequisites](#2-prerequisites)
- [3. Hardware Configuration (Configs)](#3-hardware-configuration-configs)
- [4. Bitstream Generation](#4-bitstream-generation)
- [5. Board Setup and Programming](#5-board-setup-and-programming)
- [6. Bare-metal Hands-on](#6-bare-metal-hands-on)
- [7. Linux Hands-on](#7-linux-hands-on)
- [8. Results](#8-results)
- [9. Troubleshooting / FAQ](#9-troubleshooting--faq)

---

## 0. Document Info

### 0.1 Purpose
Build a Rocket + Gemmini SoC with Chipyard, put it on a Xilinx VCU118, and run Gemmini tests and DNN workloads (MobileNet, ResNet50)
through two paths: **bare-metal (UART-TSI)** and **Linux (SD boot)**.
The document also records the problems hit during bring-up (SD payload limit, login failure, cycle counters, etc.) and their fixes.

> **Scope**
> - **Chapters 3–4 (background)**: how the SoC configs and bitstreams were built, and the results. The bitstreams are provided pre-built, so you do not need to follow these chapters. Use them only if you need to reproduce the bitstreams.
> - **Chapters 5–7 (hands-on)**: step-by-step, from programming the provided bitstream to bare-metal / Linux board tests.

### 0.2 Tested Environment and Versions

| Item | Version / Value | Notes |
|---|---|---|
| Chipyard | `dev_dn` branch (origin fork, after the PR that adds this document is merged) | |
| Gemmini | `8c3f992` (v0.1a-515) | Used unmodified (`software/gemmini-rocc-tests` @ `7c540b3`) |
| testchipip | `26f821b` | |
| FireMarshal | `21119e5` (v1.12.0-274) | With the changes in 7.2 Step 0 |
| Linux / OpenSBI | 6.6 (`v6.6-4-g67bc4513761`) / v1.2 (`v1.2-78-g5ccebf0`, `PLATFORM=generic`) | FireMarshal `boards/prototype` |
| Vivado (bitstream generation) | 2023.1 | External build environment (chapters 3–4, background) |
| Vivado (programming) | 2023.1 | Windows, Hardware Manager (5.2) |
| RISC-V toolchain | `riscv64-unknown-linux-gnu-gcc` 13.2.0 (`.conda-env/riscv-tools`) | |
| Build Host OS | devcontainer Ubuntu 24.04.4 (Linux Docker host) | |
| Test Host OS | Windows (PowerShell) + WSL2 | uart_tsi.py, UART console, SD writing (WSL2 + usbipd-win, 7.3) |
| Python / pyserial | 3.13.15 / 3.5 | Runtime for uart_tsi.py |

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
         ├─ Periphery Bus : UART (0x6400_0000), SPI-SD (0x6400_1000)
         └─ Control Bus   : BootROM(sdboot, 0x1_0000), PLIC(0xC00_0000), CLINT(0x200_0000), Debug(0x0)
```

- **RISC-V Tile**: one Rocket core (RV64GC + B/Zfh extensions, Sv39 MMU). Gemmini shares the core's PTW and uses virtual addresses, so it also works from Linux user programs.
- **RoCC**: Rocket's custom-instruction interface. Gemmini commands are delivered as RoCC custom instructions.
- **L2**: 1024 sets × 8 ways × 64 B = 512 KB (DTS `cache-controller@2010000`).
- **TL**: TileLink (chip-scale interconnect), **PLIC**: Platform-Level Interrupt Controller, **CLINT**: Core-Local Interruptor.

### 1.3 Overall Workflow

```
[Background · chapters 3–4, bitstreams provided]
Chipyard Configuration → RTL Elaboration → Vivado Implementation → Bitstream Generation

[Hands-on · chapters 5–7]
  → VCU118 Programming → Basic Board Bring-up
      ├─ Bare-metal : Rocket Test → Gemmini Test → DNN Test        (UART-TSI, no SD needed)
      └─ Linux      : SD Boot → OpenSBI → Linux → Gemmini Test     (SD card needed)
```

### 1.4 Bare-metal vs Linux

| | Bare-metal | Linux |
|---|---|---|
| Program loading | UART-TSI (host → DDR) | SD card (BootROM sdboot → DDR) |
| SD card | Not needed | Needed (PMOD) |
| Output path | HTIF (tohost/fromhost) → host console | UART console (SiFive UART, 115200) |
| Execution mode | M-mode | U-mode (Linux user space) |
| Binaries | `*-baremetal` | `*-linux` (dynamically linked, glibc) |
| Switching tests | Re-send every ELF (resnet50: 125.5 MB) | Write the SD once, pick tests from the shell |
| Use | HW functional checks, performance baseline (no virtual-address/TLB overhead) | Real-environment checks, regression over many tests |
| Bitstream config | `GemminiVCU118Config50` (`WithVCU118Tweaks`) | `GemminiVCU118SDConfig50` (`WithVCU118SDTweaks`) |

Because of the SD card, the two experiments use **different bitstreams** (3.2). The SPI-SD and BootROM (sdboot) in 1.2 apply only to the SD-card version (`WithVCU118SDTweaks`).

---

## 2. Prerequisites

### 2.1 Hardware
- [ ] Xilinx VCU118 board + power supply
- [ ] JTAG USB cable (on-board USB-JTAG, for programming the bitstream)
- [ ] UART USB cable (on-board CP2105 USB-UART, see 5.1)
- [ ] (Optional) 3.3 V Pmod USB-UART — to watch the console on PMOD J52 pins 9/10 with the no-SD version (3.1)
- [ ] PMOD MicroSD adapter (Digilent PmodMicroSD, for Linux)
- [ ] MicroSD card (partition 1 needs at least 48 MiB; 512 MiB recommended)
- [ ] USB SD card reader (to pass through from Windows to WSL2, 7.3; a laptop's built-in SD slot cannot be passed to WSL)
- [ ] Host PCs — build: Linux server (devcontainer) / test: Windows (Vivado, uart_tsi.py, UART console) + WSL2 (SD writing)

### 2.2 Software Setup
The Chipyard environment (Steps 1, 2, 6) is **for software builds only**. The bitstreams are provided, so there is no hardware build (Vivado synthesis). This environment produces the bare-metal test ELFs (6.4) and the FireMarshal Linux image (7.2).

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

#### Step 3. Install Vivado and the JTAG cable driver (Windows, for programming)
Used only for programming the bitstream (5.2). The Windows host just needs the **Vivado Hardware Manager**; **Vivado Lab Edition** is enough (no synthesis license required).
The build host (devcontainer) does not need Vivado.

**You must install the JTAG cable driver.** Windows needs it to recognize the board's USB-JTAG.
- Enable **Install Cable Drivers** in the Vivado installer.
- If Vivado is already installed without it, run the script below as administrator (for Lab Edition, use `Vivado_Lab\<version>`):
  ```powershell
  cd <Vivado install path>\Vivado\<version>\data\xicom\cable_drivers\nt64
  .\install_drivers_wrapper.bat
  ```
- Re-plug the USB cable after installing the driver.

✅ **Checkpoint**: `get_hw_targets` in 5.2 Step 2 does not return an empty list.

#### Step 4. Install the CP2105 USB-UART driver (Windows)
To use the on-board USB-UART (CP2105) as a COM port, install the Silicon Labs **CP210x VCP driver**.
- Download and install "CP210x Universal Windows Driver" from the Silicon Labs website.
- Re-plug the USB cable after installing.

✅ **Checkpoint**: Device Manager → Ports (COM & LPT) shows two ports: **`Silicon Labs Dual CP2105 USB to UART Bridge: Standard COM Port (COMx)`** and **`... Enhanced COM Port (COMy)`**. The tutorial uses the Standard port (5.1).

#### Step 5. Python environment (pyserial)
Tested with Python **3.13.15** and pyserial **3.5**.
```powershell
python --version        # Python 3.13.15
pip install pyserial==3.5
```

#### Step 6. Initialize FireMarshal (board = prototype)
```bash
cd software/firemarshal
grep -q '^board-dir' marshal-config.yaml 2>/dev/null || echo "board-dir : 'boards/prototype'" >> marshal-config.yaml
cat marshal-config.yaml
```
- If a `board-dir` line already exists, nothing is added (running this repeatedly leaves exactly one line).

✅ **Checkpoint**: The line `board-dir : 'boards/prototype'` appears exactly once.

#### Step 7. WSL2 + usbipd-win (Windows, for SD writing)
To write the image to the SD card (7.3), the USB SD reader is passed through to WSL2 and written with Linux `dd`.
```powershell
wsl --install                      # WSL2 + Ubuntu (skip if already installed)
winget install usbipd              # usbipd-win
```
Inside WSL, install the partitioning tool (needed only to partition a new card, 7.3 Step 3):
```bash
sudo apt-get update && sudo apt-get install -y gdisk
```
Copying the image from the server to Windows (7.3 Step 1) uses `scp` from the built-in Windows OpenSSH client.

### 2.3 Directory Layout

Shows where files come from during the tutorial and where they go.
**[provided]** files you receive · **[authored]** files you create yourself · **[built]** files produced by a build

```text
chipyard/
├── notes/vcu118_port/                         [provided] README.md (this document), patches/, uart_tsi.py
│                                              (bitstreams: GitHub Release, 5.2 Step 3)
├── tests/build/*.riscv                        [built] 6.4 Step 1 → Windows (6.4 Step 4)
├── .conda-env/riscv-tools/                    ($RISCV)
│   └── riscv64-unknown-elf/share/riscv-tests/
│       └── benchmarks/*.riscv                 [built] build-setup (6.4 Step 2) → Windows
├── generators/gemmini/software/gemmini-rocc-tests/
│   └── build/{bareMetalC,imagenet}/           [built] 6.4 Step 3
│       ├── *-baremetal                        → Windows (6.4 Step 4)
│       └── *-linux                            → overlay (7.2 Step 2)
└── software/
    ├── gemmini-all/                           [authored] 7.2 Steps 2–3
    │   ├── gemmini-all.json, gemmini-all.kfrag
    │   └── overlay/
    │       ├── root/{gemmini,imagenet}/       ← copies of *-linux from build/
    │       └── etc/sysctl.d/90-perf-user-access.conf
    └── firemarshal/images/prototype/gemmini-all/
        └── gemmini-all-bin-nodisk-flat        [built] 7.2 Step 4 → Windows → SD card (7.3)
```

For the Scala / sdboot files used to build the bitstreams, see table "(1) Reference-only changes" below.

There are two kinds of changes relative to upstream.

**(1) Changes used to build the bitstreams — reference only (not touched in the hands-on part)**

Already included in the provided bitstreams. Needed only if you reproduce the bitstreams yourself.

| File | Change |
|---|---|
| `fpga/src/main/scala/vcu118/Configs.scala` | Adds custom configs (3.1) |
| `fpga/src/main/scala/vcu118/HarnessBinders.scala` | Adds custom configs (3.1) |
| `generators/chipyard/src/main/scala/config/RoCCAcceleratorConfigs.scala` | Adds custom configs (3.1) |
| `fpga/src/main/resources/vcu118/sdboot/sd.c` | `PAYLOAD_SIZE_B` 30 → 48 MiB, `SPI_CLK` 25 MHz → 300 kHz (7.4). Patch: `notes/vcu118_port/patches/sdboot-48MiB-300kHz.patch` |

**(2) Changes you apply yourself during the hands-on part**

| File | Change |
|---|---|
| devcontainer packages | Install `file` and `cpio` in the container (2.2 Step 1) |
| `software/firemarshal/boards/prototype/base-workloads/br-base/buildroot-config` (tracked in the firemarshal submodule) | Root password set as an MD5 hash; adds `BR2_TOOLCHAIN_EXTERNAL_FORTRAN=y` / `OPENMP=y` (+12/−1) — 7.2 Step 0. Patch: `notes/vcu118_port/patches/firemarshal-prototype-buildroot-config.patch` |
| `software/firemarshal/boards/default/firmware/opensbi/lib/sbi/sbi_pmu.c` (opensbi submodule inside firemarshal) | `pmu_ctr_stop_hw()` no longer stops the CY(0)·IR(2) counters (+10) — 7.2 Step 0. Patch: `notes/vcu118_port/patches/opensbi-sbi_pmu-keep-cy-ir.patch` |

---

## 3. Hardware Configuration (Configs)

### 3.1 VCU118 Fragments

| Fragment | Role | Defined in |
|---|---|---|
| `WithFPGAFrequency(50)` | sbus/pbus/cbus/fbus/mbus and harness clocks at 50 MHz | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithVCU118Tweaks` | **No-SD-card version.** Programs loaded over UART-TSI; console on PMOD J52 | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithVCU118SDTweaks` | **SD-card version.** Programs loaded from the SD card by sdboot; console on the on-board USB-UART | `fpga/src/main/scala/vcu118/Configs.scala` |
| `WithSystemBusWidth(128)` | 128-bit system bus | `generators/chipyard/src/main/scala/config/fragments/SubsystemFragments.scala:17` |
| `WithNBigCores(1)` / `WithNHugeCores(1)` | One Rocket core (Big: 16 KB L1, Huge: 32 KB L1) | `generators/rocket-chip/src/main/scala/rocket/Configs.scala:62` |

#### Configs added for this tutorial

| File | Class | Description |
|---|---|---|
| `fpga/src/main/scala/vcu118/Configs.scala` | `WithVCU118Tweaks` | No-SD-card version (comparison table below) |
| | `WithVCU118SDTweaks` | SD-card version (comparison table below) |
| | `WithNoSDCardPeripherals` | Keeps only the UART at `0x64000000`, removes SPI, assigns PMOD J52 to JTAG (`VCU118ShellPMOD = "JTAG"`) |
| | `WithNoSDBootSystemModifications` | `WithSystemModifications` without the sdboot BootROM — uses the default Chipyard BootROM (WFI, then wait for MSIP) |
| | `GemminiVCU118Config50`, `GemminiVCU118SDConfig50` | VCU118 configs used in the experiments (3.2) |
| `fpga/src/main/scala/vcu118/HarnessBinders.scala` | `WithVCU118UARTTSI` | Connects UART-TSI to the on-board USB-UART (AW25/BB21) |
| | `WithVCU118PMODUART` | Connects the console UART to PMOD J52 pins 9/10 (AT15/AT16, LVCMOS18) |
| `generators/chipyard/src/main/scala/config/RoCCAcceleratorConfigs.scala` | `GemminiPaperRocketConfig` | Rocket (BigCore) + Gemmini `defaultConfig` SoC (3.2) |

#### Comparing the two Tweaks

| Item | `WithVCU118Tweaks` (no SD) | `WithVCU118SDTweaks` (with SD) |
|---|---|---|
| Used in | Bare-metal (chapter 6) | Linux (chapter 7) |
| Program loading | UART-TSI — the host writes directly into DDR | SD sector 34 → DDR (7.1) |
| BootROM | Default Chipyard BootROM (WFI, then wait for MSIP) — `WithNoSDBootSystemModifications` | sdboot — `WithSystemModifications` (builds `fpga/src/main/resources/vcu118/sdboot` during elaboration) |
| TSI | `WithUARTTSIClient` (921600 baud) + `WithVCU118UARTTSI` — on-board USB-UART | None (`SerialTLKey => Nil`) |
| Console UART | PMOD J52 pins 9/10 (`WithVCU118PMODUART`) | On-board USB-UART (`WithUART`) |
| SD card (SPI) | None (`WithNoSDCardPeripherals`) | `WithSPISDCard`, SPI at `0x64001000` (`WithDefaultPeripherals`) |
| PMOD J52 used for | JTAG + console UART (`VCU118ShellPMOD = "JTAG"`) | SD card (`VCU118ShellPMOD = "SDIO"`) |
| Debug JTAG location | PMOD J52 | FMC_J2 |
| Extra hardware | None — bare-metal output comes back over the TSI link via HTIF. A 3.3 V Pmod USB-UART if you want the PMOD J52 console | PMOD MicroSD adapter + SD card |

Common to both: clocking (default 100 MHz, overridden by `WithFPGAFrequency(50)` in the 3.2 configs), UART at `0x64000000`, 2 GB DDR (`ExtMem` = `VCU118DDRSize`), JTAG, TL backing memory, TL monitors removed, one memory channel.

Why the UARTs are placed this way: the only USB-UART wired to the FPGA fabric is the standard COM port of the on-board CP2105. The no-SD version uses that port for TSI and moves the console to PMOD J52.

### 3.2 Gemmini Default Config Used in the Experiments

The SoC (`GemminiPaperRocketConfig`) is the same; only the VCU118 harness (Tweaks) differs depending on the SD card. Bare-metal uses the no-SD version and Linux uses the SD version.

| Item | Bare-metal (chapter 6) | Linux (chapter 7) |
|---|---|---|
| VCU118 config | `GemminiVCU118Config50` | `GemminiVCU118SDConfig50` |
| Harness | `WithVCU118Tweaks` (no SD card) | `WithVCU118SDTweaks` (with SD card) |
| CPU | 1 Rocket core (BigCore) | ← |
| Gemmini | `defaultConfig` — 16×16, Dataflow BOTH (WS, OS) | ← |
| Clock | 50 MHz | ← |
| System bus | 128-bit | ← |

```scala
// Bare-metal (UART-TSI)
class GemminiVCU118Config50 extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118Tweaks ++
  new chipyard.GemminiPaperRocketConfig
)

// Linux (SD boot)
class GemminiVCU118SDConfig50 extends Config(
  new WithFPGAFrequency(50) ++
  new WithVCU118SDTweaks ++
  new chipyard.GemminiPaperRocketConfig
)

// Common SoC
class GemminiPaperRocketConfig extends Config(
  new gemmini.LeanGemminiConfig(gemmini.GemminiConfigs.defaultConfig) ++
  new chipyard.config.WithSystemBusWidth(128) ++
  new freechips.rocketchip.rocket.WithNBigCores(1) ++
  new chipyard.config.AbstractConfig
)
```

- These classes were added for this tutorial (3.1 "Configs added for this tutorial").

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
| Rocket | ISA | RV64GC (DTS: `rv64imafdcbzicsr_zifencei_zihpm_zfh_zba_zbb_zbs_xrocket`) |
| | Cores | 1 |
| | L1 I$ / D$ | 16 KB / 16 KB |
| | MMU | Sv39 |
| L2 | | 512 KB (1024 sets, 64 B lines) |
| DDR | | 2 GB (from `0x8000_0000`) |
| Clock | | 50 MHz (all buses) |
| Gemmini | Systolic array | 16 × 16 |
| | Input / Accumulator | Int8 / Int32 |
| | Dataflow | Default: BOTH (WS, OS) / Lean: WS |
| | Scratchpad | 256 KB (4 banks) |
| | Accumulator | 64 KB (2 banks) |

### 3.5 Reference: Reproducing Elaboration

> Skip this section if you use the provided bitstreams. Use it only if you change a config or reproduce the bitstreams yourself.

Commands to elaborate the configs into RTL and DTS (for the full bitstream, see 4.1).

```bash
source env.sh
cd fpga
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118Config50 verilog     # Bare-metal
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118SDConfig50 verilog   # Linux
```
- vcu118 defaults in `fpga/Makefile`: `CONFIG_PACKAGE=chipyard.fpga.vcu118`, `MODEL=VCU118FPGATestHarness`. If your config lives in another package, also pass `CONFIG_PACKAGE`.
- `WithFPGAFrequency(50)` must be to the left of the Tweaks. The leftmost fragment wins, so it overrides the 100 MHz setting inside the Tweaks with 50 MHz.
- Check the result in `*.dts` (4.3).

---

## 4. Bitstream Generation

This chapter describes how the provided bitstreams were built and what the results were. The hands-on part starts in chapter 5.

### 4.1 Reference: Reproducing the Bitstreams

> Skip this section if you use the provided bitstreams. It needs a build environment with Vivado (including a synthesis license).

To reproduce the Linux bitstream, first apply the sdboot patch (7.4):
```bash
git apply notes/vcu118_port/patches/sdboot-48MiB-300kHz.patch
```

```bash
source env.sh
cd fpga
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118Config50 bitstream     # for bare-metal
make SUB_PROJECT=vcu118 CONFIG=GemminiVCU118SDConfig50 bitstream   # for Linux
```
- Even if you skip the elaboration in 3.5, the `bitstream` target starts from elaboration.
- Each config produces its own bitstream (the `<build>` directories in 4.2 are named by config).
- If timing is not met (slack < 0), the Vivado flow prints `Failed to meet timing` and fails.

⚠️ **Caution — changing only sdboot does not update the BootROM.** The `make` dependencies only track Scala sources (`common.mk:136`).
sdboot is built during elaboration and baked into the Verilog as a BootROM constant, so after changing `sd.c` you must
delete the `<build>` directory (4.2) or touch a Scala file. Otherwise **you get a bitstream with the old BootROM**.

### 4.2 Build Outputs

The only outputs needed for the hands-on part are the **two bitstreams (`.bit`)**, distributed as a GitHub Release (5.2 Step 3). Everything else is reference material produced along the way.

`<build>` = `fpga/generated-src/chipyard.fpga.vcu118.VCU118FPGATestHarness.<CONFIG>/`

| Category | Files | Location |
|---|---|---|
| Elaboration | `*.dts`, `*.mems.conf`, `*.top.f`, `*.fir`, `*.memmap.json` | `<build>` |
| Gemmini SW header | `gemmini_params.h` | `generators/gemmini/software/gemmini-rocc-tests/include/` (overwritten during elaboration) |
| Implementation reports | `utilization.txt`, `timing.txt`, `drc.txt`, `clocks.txt`, etc. | `<build>/obj/report/` |
| Constraints | `*.shell.xdc`, `*.shell.sdc` | `<build>` |
| Bitstream | `VCU118FPGATestHarness.bit` | `<build>/obj/` |
| BootROM | `sdboot.bin` | `fpga/src/main/resources/vcu118/sdboot/build/` |

### 4.3 Checking the Elaboration Results
#### Device Tree (`*.dts`)

For default Gemmini with BigCore. The SPI (SD) entries apply only to the SD-card version (`WithVCU118SDTweaks`).

| Item | Expected | Observed |
|---|---|---|
| DRAM base / size | 0x80000000 / 2 GB | `memory@80000000`, `reg = <0x80000000 0x80000000>` ✅ |
| BootROM | 0x10000 | `rom@10000`, size 0x10000 ✅ |
| CLINT / PLIC | 0x2000000 / 0xC000000 | `clint@2000000` / `interrupt-controller@c000000` ✅ |
| UART / SPI (SD) | 0x64000000 / 0x64001000 | `serial@64000000` (sifive,uart0) / `spi@64001000` + `mmc@0` (mmc-spi-slot) ✅ |
| riscv,isa | rv64imafdc | `rv64imafdcbzicsr_zifencei_zihpm_zfh_zba_zbb_zbs_xrocket` ✅ |
| L1 I$ / D$ | 16 KB / 16 KB (BigCore) | `i-cache-size` / `d-cache-size = <16384>` ✅ |
| L2 | 512 KB | `cache-size = <524288>`, 1024 sets ✅ |
| clock-frequency | 50 MHz | `sbus/pbus/cbus/mbus/fbus_clock = 50000000` ✅ |
| timebase | 50 kHz | `timebase-frequency = <50000>` ✅ |

#### Gemmini Parameters (`gemmini_params.h`)

Excerpt from the original upstream header (`gemmini-rocc-tests` @ `7c540b3`):

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

⚠️ **Caution**: The software (gemmini-rocc-tests) must be compiled with **a header generated from the same config as the hardware**. Elaboration overwrites the header, so if you regenerate the bitstream with a different config, rebuild the tests too.
default ↔ lean is the exception: no code references the header difference (`ACC_READ_*_WIDTH`), so the original header gives the same result (3.3).

### 4.4 Synthesis Results
- Vivado version: 2023.1
- Target part: `xcvu9p-flga2104-2L-e` (`part_fpga` in `fpga/fpga-shells/xilinx/vcu118/tcl/board.tcl`) — confirmed by `Device : xcvu9p-flga2104-2L-e` in `<build>/obj/report/utilization.txt` ✅

### 4.5 Implementation Results
The results below are for the Linux version (`GemminiVCU118SDConfig50`).

#### Timing

| Item | Value | Verdict |
|---|---|---|
| WNS (setup) | 0.249 ns | ✅ ≥ 0 |
| TNS | 0.000 ns (0 failing / 300,917 endpoints) | ✅ = 0 |
| WHS (hold) | 0.007 ns | ✅ ≥ 0 |
| THS | 0.000 ns (0 failing / 300,215 endpoints) | ✅ = 0 |
| Target clock | 50 MHz (`clk_out1_harnessSysPLL`, 20 ns) | |

On the board, `Total cycles ÷ user time` also measures about 50 MHz, confirming the core runs at 50 MHz (7.6.2).

#### Resource Utilization (post-route)

| Resource | Used | Available | Utilization | Rocket | Gemmini |
|---|---|---|---|---|---|
| LUT | 240,040 | 1,182,240 | 20.30% | 30,783 | 173,633 |
| FF | 104,807 | 2,364,480 | 4.43% | 12,783 | 60,863 |
| BRAM (36Kb equiv.) | 301.5 (RAMB36 225 + RAMB18 153) | 2,160 | 13.96% | 8 (RAMB18 16) | 128 (RAMB36 64 + RAMB18 128) |
| URAM | 0 | 960 | 0.00% | 0 | 0 |
| DSP | 230 | 6,840 | 3.36% | 24 | 203 |

#### DRC / Power

| Item | Value |
|---|---|
| DRC errors | 0 (673 warnings — 655 DSP pipelining recommendations, etc., all non-critical) |
| Total on-chip power | 4.575 W (dynamic 2.074 W + static 2.501 W; low-confidence estimate) |

✅ **Checkpoint**: WNS ≥ 0, 0 DRC errors

---

## 5. Board Setup and Programming

### 5.1 Connection and Power Checklist
- [ ] 0. Power, JTAG, and UART cables connected
- [ ] 1. Power switch up
- [ ] 2. (Linux) PMOD adapter + SD card inserted
- [ ] 3. Check the UART COM port — the on-board CP2105 shows up as two COM ports (Standard / Enhanced); the one wired to the FPGA is the **Standard COM port** (Enhanced is for the system controller). This port is UART-TSI in the no-SD version (chapter 6) and the console in the SD version (chapter 7). If no COM port appears, install the CP2105 driver (2.2 Step 4)

⚠️ **Caution**: Do not use the PROG button (left).

### 5.2 Programming with Vivado Tcl
#### Step 1. Start the Vivado Tcl shell
Windows: Start menu → Vivado Tcl Shell

#### Step 2. Connect to the HW server and select the device
```tcl
open_hw_manager
connect_hw_server -url localhost:3121
current_hw_target [lindex [get_hw_targets] 0]
open_hw_target
current_hw_device [lindex [get_hw_devices] 0]
get_property PART [current_hw_device]
```
#### Step 3. Program the bitstream
The bitstreams are not in the repository; they are distributed as a **GitHub Release**.
- Release page: <https://github.com/roykim0823/chipyard/releases/tag/vcu118-port-v1>
- Download both `.bit` files from the page's Assets into `bitstream\` inside the Windows working folder (`vcu118_port\bitstream\`).

After downloading, compare the sha256 to make sure the files are correct (PowerShell: `Get-FileHash .\bitstream\<file> -Algorithm SHA256`). Pick the file for your experiment (3.2).

| Experiment | Config | File | sha256 |
|---|---|---|---|
| Bare-metal | `GemminiVCU118Config50` | `GemminiVCU118Config50.bit` | `e639974f9d6704e630a3a06c5fed3c7c32657cdee6f0f095071e3dd573630cc3` |
| Linux | `GemminiVCU118SDConfig50` | `GemminiVCU118SDConfig50-sd48-spi300k.bit` (`PAYLOAD_SIZE_B` 48 MiB, `SPI_CLK` 300 kHz — 7.4) | `57ecad2ce6e52da59956e88011337a6f458b50d5da9efd6d2eb1892fbb18c6a1` |

Use `/` in Tcl paths.
```tcl
set_property PROGRAM.FILE {C:/<working folder>/vcu118_port/bitstream/<bitstream>.bit} [current_hw_device]
get_property PROGRAM.FILE [current_hw_device]
program_hw_devices [current_hw_device]
refresh_hw_device [current_hw_device]
```
#### Step 4. Confirm completion
```tcl
get_property REGISTER.IR.BIT5_DONE [current_hw_device]
report_property [current_hw_device]
```

✅ **Checkpoint**: `program_hw_devices` prints `INFO: [Labtools 27-3164] End of startup status: HIGH`, `BIT5_DONE = 1`, and the board's **DS34 DONE LED** is lit

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
④ Rocket starts execution hart0, waiting in WFI in the BootROM, jumps to 0x80000000
⑤ Gemmini RoCC commands   Test code issues Gemmini commands
⑥ HTIF                    printf → tohost, exit → tohost (odd payload)
⑦ PASS / FAIL             Exit code 0 / 1
```

- ② `crt.S` does not clear .bss, so the loader zero-fills it. Without this, tests fail only on the board because of garbage in DDR.
- ④ hart0 always jumps to `0x80000000`. All binaries in 6.4 start at this address.

### 6.3 Host Tool

The original `generators/testchipip/uart_tsi/` is a C++ program tied to fesvr and termios, so it is **Linux-only**.
The host connected to the board is Windows, so we use `uart_tsi.py` (`notes/vcu118_port/uart_tsi.py`), a Python reimplementation of the host side. Run it from the Windows working folder (`vcu118_port\`).

| | Original (C++) | `uart_tsi.py` |
|---|---|---|
| Dependencies | fesvr, pthread, termios | pyserial |
| Platforms | Linux | Windows / Linux |
| HTIF coverage | Full syscall proxy | Console putchar, `write` (fd 1/2), `exit` |
| Timeout / stall diagnosis | None | 10 s response timeout; warns with the likely cause after 30 s without progress |
| TSI bytes on the wire | — | **Identical to the original** |

```powershell
# 1. Link check: read a single word (no load, no start)
python .\uart_tsi.py --tty <COM> --baud 921600 --read 0x80000000
# 2. Run a bare-metal program
python .\uart_tsi.py --tty <COM> --baud 921600 <ELF>
```

⚠️ **Caution**: `--baud` defaults to 115200. It must match the board's UART-TSI baud rate (921600), so always pass `--baud 921600`.

`uart_tsi.py` prints its status messages in Korean. The expected outputs in this document show them as-is; their meanings:

| Message | Meaning |
|---|---|
| `포트` | port |
| `세그먼트` | segment |
| `로딩 xx% (N초)` | loading xx% (N seconds) |
| `.bss ... 0으로` | .bss ... zero-filled |
| `로딩 완료` | load complete |
| `hart0 기동` | hart0 started |
| `HTIF 부착` | HTIF attached |
| `종료 코드` | exit code |
| `tohost/fromhost 심볼이 없습니다` | no tohost/fromhost symbols |

Bare-metal experiments use the `GemminiVCU118Config50` bitstream from 3.2 (`WithVCU118Tweaks`, no SD).

### 6.4 Building the Test Binaries

Build all three on the build host (devcontainer), then copy the output ELFs to the Windows host and run them with `uart_tsi.py`.

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
./build.sh
```
- If `riscv64-unknown-linux-gnu-gcc` is on PATH, it builds all three variants: `-baremetal`, `-pk`, and `-linux`.
- Outputs: `build/bareMetalC/*-baremetal`, `build/imagenet/{mobilenet,resnet50}-baremetal`
- Use the original `include/gemmini_params.h` from a fresh clone as-is. The provided bitstreams use the default config (3.2), which matches the original header. Elaboration (3.5) overwrites this header, so do not run it during the hands-on part.

#### Step 4. Copy to Windows
Copy each step's output ELFs to any folder on Windows and give that path when running them. The commands in 6.5 assume they are under `firmware\baremetal\{chipyard-tests,riscv-tests,gemmini}\` in the working folder.

| Output (build host) | Example location in 6.5 (relative to the Windows working folder) |
|---|---|
| `tests/build/{hello,cpp-hello,mt-hello}.riscv` | `firmware\baremetal\chipyard-tests\` |
| `$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/*.riscv` | `firmware\baremetal\riscv-tests\` |
| `generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/*-baremetal`, `build/imagenet/*-baremetal` | `firmware\baremetal\gemmini\` |

⚠️ **Caution**: Copy the ELF itself — not the `.dump` file, and do not `strip` it. uart_tsi.py looks up `tohost`/`fromhost` in the ELF symbol table; without them it only prints `tohost/fromhost 심볼이 없습니다` (no tohost/fromhost symbols) and exits with no test output.

### 6.5 Labs

> ⚠️ Press the **SW5 CPU_RESET** button before every run. (hart0 must be back in WFI in the BootROM so MSIP can wake it again.)

#### Lab 0. Link check
**Goal**: Confirm the UART-TSI link and DDR access work.
```powershell
python .\uart_tsi.py --tty <COM> --baud 921600 --read 0x80000000
```
**Expected output**:
```text
uart_tsi.py  포트 COM4  921600 baud
  [0x80000000] = 0x00050433
```
The value itself is meaningless. It changes with the DDR contents after power-up or the last program loaded.

✅ **Checkpoint**: A value is printed without `TimeoutError` (exit code 2).

#### Lab 1. Rocket only
**Goal**: Confirm the Rocket core and HTIF output work, without Gemmini.
```powershell
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\chipyard-tests\hello.riscv
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\riscv-tests\dhrystone.riscv
```
**Expected output**:
```text
uart_tsi.py  포트 COM4  921600 baud
  hello.riscv  entry 0x80000000
  세그먼트 0x80000000-0x80001ed8  7.7 KB
  로딩  90.1%  (0초)
  .bss    0x80001ed8-0x80002240  0.9 KB 0으로
  로딩 100.0%  (0초)
  로딩 완료 8.6 KB / 0.1초
  hart0 기동
  HTIF 부착  tohost=0x80001e00 fromhost=0x80001e08

Hello world from core 0, a rocket

  종료 코드 0
```
```text
uart_tsi.py  포트 COM4  921600 baud
  dhrystone.riscv  entry 0x80000000
  세그먼트 0x80000000-0x800001cc  0.4 KB
  로딩   2.2%  (0초)
  세그먼트 0x80001000-0x80003824  10.0 KB
  로딩  51.0%  (0초)
  .bss    0x80003824-0x80006070  10.1 KB 0으로
  로딩 100.0%  (0초)
  로딩 완료 20.6 KB / 0.3초
  hart0 기동
  HTIF 부착  tohost=0x80001000 fromhost=0x80001040

Microseconds for one run through Dhrystone: 449
Dhrystones per Second:                      2222
mcycle = 225007
minstret = 186031

  종료 코드 0
```

✅ **Checkpoint**: `종료 코드 0` (exit code 0)

#### Lab 2. Basic Gemmini operations
**Goal**: Confirm a minimal Gemmini round trip and matmul work.
```powershell
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\gemmini\template-baremetal
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\gemmini\mvin_mvout-baremetal
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\gemmini\tiled_matmul_ws-baremetal
```
**Expected output** (`tiled_matmul_ws`):
```text
uart_tsi.py  포트 COM4  921600 baud
  tiled_matmul_ws-baremetal  entry 0x80000000
  세그먼트 0x80000000-0x800001c8  0.4 KB
  로딩   0.6%  (0초)
  세그먼트 0x80001000-0x80003450  9.1 KB
  로딩  12.9%  (0초)
  .bss    0x80003450-0x800134a0  64.1 KB 0으로
  로딩 100.0%  (1초)
  로딩 완료 73.6 KB / 1.0초
  hart0 기동
  HTIF 부착  tohost=0x80001000 fromhost=0x80001040

MAT_DIM_I: 64
MAT_DIM_J: 64
MAT_DIM_K: 64
Starting gemmini matmul
Cycles taken: 2412
Starting slow CPU matmul
Cycles taken: 3227178

  종료 코드 0
```

✅ **Checkpoint**: `종료 코드 0` (exit code 0)

#### Lab 3. DNN workloads
```powershell
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\gemmini\mobilenet-baremetal
python .\uart_tsi.py --tty <COM> --baud 921600 .\firmware\baremetal\gemmini\resnet50-baremetal
```
See 6.6 for how to read the output.

### 6.6 Reading the Output

`resnet50-baremetal` run log:

```text
uart_tsi.py  포트 <COM>  921600 baud
  resnet50-baremetal  entry 0x80000000
  세그먼트 0x80000000-0x800001c8  0.4 KB
  로딩   0.0%  (0초)
  세그먼트 0x80001000-0x8190e504  25.1 MB
  로딩  20.0%  (…초)
  .bss    0x8190e504-0x87d820d0  100.5 MB 0으로
  로딩 100.0%  (…초)
  로딩 완료 125.5 MB / …초
  hart0 기동
  HTIF 부착  tohost=0x80001000 fromhost=0x80001040

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

  종료 코드 0
```
The `로딩` (loading) line updates in place while loading; the log above shows it as it looks at the end of each stage (`…초` varies per run).

| Part | Meaning |
|---|---|
| Segment 1 (0x80000000~) | First `PT_LOAD` (R E). `.text.init` — boot code, where hart0 starts executing |
| Segment 2 | The part of the second `PT_LOAD` with actual file contents (filesz). `.tohost`, `.text`, `.rodata` (weights), etc. |
| .bss | The memsz − filesz range. Not in the file, so it is zero-filled (activation buffers, etc.) |
| hart0 기동 (hart0 started) | CLINT MSIP write → Rocket starts executing |
| HTIF 부착 (HTIF attached) | Starts polling the `tohost` / `fromhost` addresses found in the ELF symbol table |
| Cycle breakdown | Sum of 7 instrumented regions. Not the whole program's runtime |
| PASS / exit code | Classification of 4 images compared with the expected answers / HTIF exit value |

- Check the section → segment mapping with "Section to Segment mapping" from `riscv64-unknown-elf-readelf -lW <elf>`.
- Judge by the **exit code**, not by the printed strings.

### 6.7 Limitations
- `gemmini-rocc-tests` workloads with funct7 = 23 (using the external scratchpad, `*_spad`) are not supported

---

## 7. Linux Hands-on

Linux experiments use the `GemminiVCU118SDConfig50` bitstream from 3.2 (`WithVCU118SDTweaks`, SD-card version). The chapter 6 bitstream is the no-SD version, so program the Linux bitstream again with 5.2.

### 7.0 Hands-on Flow

```
[Build host (devcontainer)]
Build the FireMarshal image → *-bin-nodisk-flat                    (7.2)
    ↓ scp
[Windows + WSL2]
Copy flat image, check hash → attach SD reader to WSL              (7.3 Steps 1–2)
    → confirm partition 1 starts at sector 34 → raw write to partition 1 → verify   (7.3 Steps 3–5)
    → SD card → PMOD microSD adapter → VCU118 PMOD J52              (7.3 Step 6)
[VCU118]
Program the Linux bitstream                                         (5.2)
    → connect UART at 115200 → SW5 CPU_RESET → sdboot → Linux boot  (7.5 Steps 1–3)
    → log in as root / fpga                                         (7.5 Step 4)
    → run Gemmini tests                                             (7.6)
```

You may program the bitstream with the SD card already inserted. sdboot runs right after programming, so open the console and then reset again with SW5 to see the log from the start (7.5).

### 7.1 Boot Flow

```
VCU118 Reset → SD Initialization → Payload Loading → OpenSBI → Linux / Buildroot → Gemmini Application
```

- **BootROM (sdboot)**: initializes the SD card in SPI mode, then — without reading the partition table — copies **`PAYLOAD_SIZE_B` bytes starting at absolute sector 34** to `0x80000000` and jumps there. It does not look at the payload size.
- **payload**: the flat binary built by FireMarshal = OpenSBI `fw_payload` + Linux `Image` + embedded initramfs (nodisk). Byte 0 = physical address `0x80000000`.
- **Console**: `CONFIG_CMDLINE="console=ttyS0 console=ttySIF0 earlycon"` + `CMDLINE_FORCE` → SiFive UART at `0x64000000`, 115200.

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
 lib/sbi/sbi_pmu.c | 10 ++++++++++
 1 file changed, 10 insertions(+)
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
sha256sum gemmini-all-bin-nodisk-flat              # compare with Windows in 7.3 Step 1
```

**Output**: `software/firemarshal/images/prototype/gemmini-all/gemmini-all-bin-nodisk-flat`
(For reference: 42,813,448 B in our run, 85.0% of the 48 MiB limit)

⚠️ **Caution**: The file written to the SD card must be **`-bin-nodisk-flat`**. The `-bin-nodisk` (ELF) and `-bin-dwarf-nodisk` files in the same directory cannot be read by sdboot.

✅ **Checkpoint**: `GZIP=y`, size ≤ 50,331,648 B, 56 tests included, modification time is current

### 7.3 Preparing the SD Card and Writing the Image (Windows + WSL2)

**Goal**: Write the flat image from 7.2 raw to **partition 1** of the SD card (starting at sector 34).

SD card layout:
```
LBA 0–33   GPT (protective MBR + header + partition entries)
LBA 34~    Partition 1  ← flat image (sdboot reads PAYLOAD_SIZE_B from here, 7.1)
           (Partition 2 — optional, regular filesystem)
```

⚠️ **Caution — the most important distinction in this whole section**
- Write to **partition 1 (`/dev/sdX1`)**. Writing to the whole disk (`/dev/sdX`) destroys the GPT.
- **Always identify the disk number / device name by its size.** Guessing can overwrite the Windows SSD or another disk.
- Do not use Rufus / balenaEtcher / Win32 Disk Imager — they write from sector 0, destroying the GPT and shifting the payload.
- The SD device is not visible inside the devcontainer. Writing to `/dev/sdX1` there only creates a **regular file** inside the container.

#### Step 1. Copy the image to Windows
Fetch the flat image from the build host into the Windows working folder (`vcu118_port\`).
```powershell
scp <user>@<server>:<absolute chipyard path>/software/firemarshal/images/prototype/gemmini-all/gemmini-all-bin-nodisk-flat .\
Get-Item .\gemmini-all-bin-nodisk-flat
Get-FileHash .\gemmini-all-bin-nodisk-flat -Algorithm SHA256
```
- `<absolute chipyard path>` is **the path on the server host**. The devcontainer bind-mounts the source, so it may differ from the path inside the container (e.g. `/workspaces/...`).
- scp is not required. Any method works (VS Code Explorer's Download, WinSCP, etc.); what matters is the SHA256 comparison below.

✅ **Checkpoint**: Size and SHA256 match the `stat` / `sha256sum` values from 7.2 Step 5.

#### Step 2. Attach the SD reader to WSL
(Optional) First check the SD card's disk number and partition position on Windows.
```powershell
Get-Disk                              # identify the SD card number (N) by its size
Get-Partition -DiskNumber N           # partition 1 Offset = 17408 (= 34 × 512) means it starts at sector 34
```

Pass the SD reader to WSL from an administrator PowerShell:
```powershell
usbipd list                           # find the SD reader's BUSID
usbipd bind --busid <BUSID>           # once
usbipd attach --wsl --busid <BUSID>
```

Check the device in WSL:
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
- **If `sdX1` starts at 34, skip partitioning.** A card that has already booted to `buildroot login:` has a verified layout and sdboot setup, so just replace the image without reformatting.
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
The Windows working folder appears under `/mnt/c/...` in WSL:
```bash
IMAGE="/mnt/c/<working folder>/vcu118_port/gemmini-all-bin-nodisk-flat"
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

#### Step 6. Detach and insert into the board
```bash
sync
```
```powershell
usbipd detach --busid <BUSID>
```
Safely remove the SD card in Windows, put the microSD into the PMOD microSD adapter, and connect it to **PMOD J52** on the VCU118 (in the Linux bitstream, J52 is assigned to the SPI SD card, 3.1 `VCU118ShellPMOD = "SDIO"`).

> **Note — if you cannot use WSL**: dd for Windows (as administrator) writing the whole disk with `seek=34` writes to the same location. `seek=34` preserves the GPT (LBA 0–33).
> ```
> dd if=gemmini-all-bin-nodisk-flat of=\\?\Device\HarddiskN\Partition0 bs=512 seek=34 --progress
> ```

### 7.4 SD Bootloader Settings

The provided Linux bitstream **already includes** the settings below (48 MiB / 300 kHz). In the hands-on part you only need to observe two things:
- Flat image size **≤ 48 MiB (50,331,648 B)** — checked in 7.2 Step 5
- **`LOADING 0x3000000 B PAYLOAD`** in the boot log — checked in 7.5 Step 2 (proof that the bitstream has the 48 MiB BootROM)

Below are the changes from the defaults and why. Apply `notes/vcu118_port/patches/sdboot-48MiB-300kHz.patch` only when regenerating the bitstream (4.1).

| Item | Default | Changed to | Reason |
|---|---|---|---|
| `SPI_CLK` | 25 MHz | **300 kHz** | At 25 MHz, SD initialization does not get past the CMD stage (9.1) |
| `PAYLOAD_SIZE_B` | 30 MiB | **48 MiB** | The image with 56 tests + mobilenet + resnet50 (42.8 MB) exceeds 30 MiB |

`fpga/src/main/resources/vcu118/sdboot/sd.c` (after applying `notes/vcu118_port/patches/sdboot-48MiB-300kHz.patch`):
```c
//#define PAYLOAD_SIZE_B (30 << 20) // default: 30MiB
#define PAYLOAD_SIZE_B (48 << 20)
...
// #define SPI_CLK 	25000 // default: 25 MHz
#define SPI_CLK 	300                // kHz
#define SPI_DIV 	(((F_CLK * 1000) / SPI_CLK) / 2 - 1)
```

- `F_CLK` is the pbus clock (MHz) passed in at elaboration. At 50 MHz, `SPI_DIV` = 50000 / 300 / 2 − 1 = **82**, so the actual SCK = 50 MHz / (2 × 83) ≈ **301 kHz**.
- `sd_poweron()` (initialization) and `sd_copy()` (payload copy) use the same `SPI_DIV`, so **the copy also runs at 300 kHz**.
- Loading an image of 30 MiB or less with the new BootROM is fine. Conversely, **loading a 48 MiB-class image with a 30 MiB BootROM leaves the tail unloaded and boot fails silently**.

⚠️ **Caution** (when regenerating): These values are part of the BootROM, so **the Linux bitstream must be regenerated** after changing them. Changing only `sd.c` does not trigger re-elaboration (see the caution in 4.1).

### 7.5 Booting
First program the Linux bitstream (`GemminiVCU118SDConfig50-sd48-spi300k.bit`) with 5.2.

#### Step 1. Connect the UART console
Use the CP2105 **Standard COM port** (5.1). Settings: **115200 8-N-1, no flow control**.
```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Name   # find the Standard COM port number
python -m serial.tools.miniterm <COM> 115200                       # terminal included with pyserial (2.2 Step 5)
```
- PuTTY / Tera Term also work.
- Linux host: `screen /dev/ttyUSB1 115200` (the port number depends on your setup)

#### Step 2. SW5 CPU_RESET → check the SD loading log
With the console **already open**, press **SW5 CPU_RESET** on the board. sdboot initializes the SD card and copies the payload into DDR.
```text
INIT
CMD0
CMD8
ACMD41
CMD58
CMD16
CMD18
LOADING 0x3000000 B PAYLOAD      ← 48 MiB. 0x1e00000 (30 MiB) means an old BootROM
LOADING
BOOT
```
✅ **Checkpoint**: `LOADING 0x3000000` — proof that the bitstream has the 48 MiB BootROM. A `CRC mismatch` during loading means an SD write / card problem.

#### Step 3. OpenSBI → Linux → Buildroot
```text
OpenSBI v1.2
   ____                    _____ ____ _____
  / __ \                  / ____|  _ \_   _|
 ...
Platform Name             : ucb-bar,chipyard
...
[    0.000000] Linux version 6.6.0-00004-g67bc4513761f ... #N SMP <build time>
...
Running sysctl: OK
...
Welcome to Buildroot
buildroot login:
```

✅ **Checkpoint**: Check that `Linux version ... #N SMP <build time>` matches the image you just built. This is the easiest way to confirm the new image made it onto the SD card.

#### Step 4. Log in
- ID: `root` / PW: `fpga`

✅ **Checkpoint**: A shell prompt (`#`) after login. `Login incorrect` means the password change from 7.2 Step 0 is not in the new image (9.1).

#### Diagnosing by the boot log
How far the log gets tells you which parts are working.

| Last output seen | Confirmed working | Suspect next |
|---|---|---|
| (no output) | — | Bitstream (5.2), COM port / baud (Step 1), SW5 reset |
| `INIT` | Rocket runs the BootROM, UART OK | SD card / PMOD J52 seating, SPI clock (9.1) |
| `CMD0` … `CMD18` | SPI SD card responds | Bad card, write (7.3) |
| `LOADING 0x… B PAYLOAD` → `BOOT` | SD → DDR copy | Payload size / position (7.4, 7.3 Step 3) |
| `OpenSBI` / `Linux version …` | Base SoC: Rocket + DDR + interrupts / timers | initramfs, kernel config (7.2) |
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

### 8.2 Open Items
- [ ] Find the cause of the `gemmini_counter` and `global_average` failures
- [ ] Per-layer depthwise analysis (17 `conv_dw_N:` lines) and review of better mappings

---

## 9. Troubleshooting / FAQ

> Format: **Symptom → Cause → Fix**

### 9.1 Linux
#### SD initialization stops at the CMD stage
- Cause: sdboot defaults to 25 MHz High Speed mode. The comment in `sd.c` says to drop to Default Speed mode (12.5 MHz) if the SD card does not support that speed. A card that does not support 25 MHz SPI does not respond to the initialization commands.
- Fix:
  1. Check that you programmed the provided Linux bitstream (`...-sd48-spi300k.bit`, 300 kHz) (5.2 Step 3, sha256 comparison)
  2. If it still stops, check the SD card and PMOD adapter seating; try another card
  3. If you regenerated the bitstream yourself, check that `notes/vcu118_port/patches/sdboot-48MiB-300kHz.patch` was applied (4.1, 7.4)

#### Boot fails after payload loading (hangs with no message)
- Cause: the payload is larger than `PAYLOAD_SIZE_B` — sdboot ignores the size and reads a fixed amount
- Fix:
  1. Check the bitstream — the log should say `LOADING 0x3000000` (48 MiB). `0x1e00000` (30 MiB) means an old BootROM (7.5 Step 2)
  2. Check and shrink the image — `stat -c %s ...-flat` ≤ 50,331,648 B, confirm gzip initramfs compression (7.2 Step 5). You can also drop binaries from the overlay
  3. If 48 MiB is still not enough, increase `PAYLOAD_SIZE_B` and regenerate the bitstream (4.1, 7.4)

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

### 9.2 Other
- **Not sure the new image made it onto the card** → compare `Linux version ... #N SMP <build time>` in the boot log, and `sha256sum` on the host.
- **The shell says a command does not exist** → with nodisk, only FireMarshal's busybox applets are available (e.g. no `timeout`). Check `images/prototype/<workload>/busybox_config`.
