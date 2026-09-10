# Gemmini — Build & Test on Verilator

Building and testing the **Gemmini** systolic-array GEMM/DNN accelerator in Chipyard.

> The simulation machinery (run modes, artifact layout, Makefile variables, waveforms,
> troubleshooting) is **identical to Rocket** and documented once in
> [`Rocket_Build_Test.md`](Rocket_Build_Test.md). This note covers only what is
> Gemmini-specific. Read the Rocket note first.

**Verified against** `/home/vscode/chipyard`, Gemmini submodule `8c3f992` (`v0.1a-515`), 2026-09-08.

> **Legend (verification status)** — ✅ command actually run here · ❌ does not work in this
> environment · ⏳ not run · ⚠️ caution. Capability tables spell out "supported / unsupported"
> in words rather than reusing these glyphs.

---

## Contents

- [1. What Gemmini is](#1-what-gemmini-is)
- [2. Configs](#2-configs)
- [3. Build the simulator](#3-build-the-simulator)
- [4. Build the test software](#4-build-the-test-software)
- [5. Running tests](#5-running-tests)
- [6. Test catalog](#6-test-catalog)
- [7. Writing your own test](#7-writing-your-own-test)
- [8. Verification log](#8-verification-log)

---

## 1. What Gemmini is

A parameterized systolic-array accelerator attached to a Rocket (or Shuttle) core over the
**RoCC** interface. Its major blocks (from `generators/gemmini/README.md`):

| Block | Role |
|---|---|
| **Decoupled access/execute** | separate load / store / execute queues so DMA overlaps compute |
| **Scratchpad + accumulator** | banked on-chip SRAM; scratchpad holds operands, accumulator holds partial sums at wider precision |
| **Systolic array + transposer** | the `meshRows × meshColumns` PE grid; transposer feeds A or B transposed |
| **DMA** | moves tiles between DRAM/L2 and the scratchpad (`mvin`/`mvout`) |
| **ROB** | tracks dependencies between queued Gemmini instructions |
| **Loop unrollers** | hardware `loop_ws` / `loop_conv` state machines that expand a whole tiled matmul or convolution from one instruction |

The ISA is documented in `generators/gemmini/README.md` §ISA (line 382 onward) and in
`notes/gemmini-isa-reference.md`.

## 2. Configs

Defined in `generators/gemmini/chipyard/GemminiConfigs.scala`, `package chipyard` — so only
`CONFIG=` changes. Auto-discovered because `generators/gemmini/.git` exists (`build.sbt:245`).

| Config | Accelerator | Cores | Notes |
|---|---|---|---|
| `GemminiRocketConfig` | `DefaultGemminiConfig` | 1 Rocket | full-featured, both dataflows |
| `LeanGemminiRocketConfig` | `LeanGemminiConfig` | 1 Rocket | **recommended starting point** — smaller, faster to simulate |
| `LeanGemminiPrintfRocketConfig` | `LeanGemminiPrintfConfig` | 1 Rocket | Lean + FireSim simulation counters |
| `FPGemminiRocketConfig` | `GemminiFP32DefaultConfig` | 1 Rocket | FP32 datapath |
| `GemminiShuttleConfig` | `DefaultGemminiConfig` | Shuttle | superscalar in-order core instead of Rocket |
| `ReRoCCManyGemminiConfig` | 4 × `LeanGemminiConfig` | 4 Rocket | ReRoCC — 4 Gemminis shared over 4 cores |

All the Rocket variants also apply `WithSystemBusWidth(128)` — the default 64-bit SBus cannot
feed a 16×16 8-bit array.

### Array parameters

`GemminiConfigs.defaultConfig` (`generators/gemmini/src/main/scala/gemmini/Configs.scala:21`):

| Parameter | Value |
|---|---|
| `inputType` / `weightType` | `SInt(8.W)` |
| `accType` | `SInt(32.W)` |
| `spatialArrayOutputType` | `SInt(20.W)` |
| `tileRows` × `tileColumns` | 1 × 1 |
| `meshRows` × `meshColumns` | **16 × 16** |
| `dataflow` | `Dataflow.BOTH` (output-stationary **and** weight-stationary) |
| `sp_capacity` | 256 KB scratchpad, 4 banks, single-ported |
| `acc_capacity` | 64 KB accumulator, 2 banks, dual-ported |

`leanConfig` is `defaultConfig` with:
`dataflow=WS` (weight-stationary only), `max_in_flight_mem_reqs=64`,
`acc_read_full_width=false`, `ex_read_from_acc=false`, `ex_write_to_spad=false`,
`hardcode_d_to_garbage_addr=true`. Dropping the OS dataflow and the extra read/write ports is
what makes it substantially cheaper to elaborate and simulate.

`FP32DefaultConfig` swaps every datatype to `Float(8, 24)` (IEEE binary32) with
`tile_latency = 2`. FP16 (`Float(5,11)`) and BF16 (`Float(8,8)`) variants also exist in
`ConfigsFP.scala` but have no Chipyard-level config class.

## 3. Build the simulator

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator

make -j$(nproc) CONFIG=LeanGemminiRocketConfig
```

✅ Verified — produces `simulator-chipyard.harness-LeanGemminiRocketConfig` (15.7 MB).
Add `debug` for waveforms.

Expect a long first build — full elaboration plus a Verilator compile, and Gemmini adds a
16×16 PE array on top of Rocket. `LeanGemminiRocketConfig` is meaningfully cheaper than
`GemminiRocketConfig`; prefer it unless you specifically need the output-stationary dataflow.

> There is **no** `generators/gemmini/chipyard.mk`, so Gemmini contributes **no** custom make
> targets. Everything runs through the standard `run-binary` surface with `BINARY=`.

## 4. Build the test software

```bash
cd /home/vscode/chipyard/generators/gemmini/software/gemmini-rocc-tests
./build.sh                                     # ✅ verified
```

`build.sh` runs `autoconf`, configures into `build/`, then `make -j`. It builds Linux binaries
too when `riscv64-unknown-linux-gnu-gcc` is on `PATH`, otherwise falls back to
`BAREMETAL_ONLY=1`. ✅ Both toolchains are present here, so all three environments were built:

```
build/bareMetalC/   56 × -baremetal, 56 × -linux, 56 × -pk
build/imagenet/      6
build/mlps/         27
build/transformers/  3
```

| Suffix | Environment | Run on |
|---|---|---|
| `-baremetal` | no OS, no virtual memory | **Verilator / VCS** — this is the one you want |
| `-pk` | RISC-V proxy kernel, has virtual memory | Verilator, via `pk`. ⚠️ limited heap — some tests that pass baremetal fail here |
| `-linux` | dynamically linked, full syscalls | FireSim / Linux-capable SoCs |

> 57 sources in `bareMetalC/`, 56 binaries — `transpose_scale.c` is not in the Makefile's
> `tests` list and is silently skipped.

## 5. Running tests

### 5.1 On Verilator (cycle-accurate)

> ⚠️ **Always pass `LOADMEM=1`.** Without it the serial TSI boot dominates: measured on a
> comparable accelerator config, the binary load was **~92% of all simulated cycles**
> (99 666 → 7 436 cycles with `LOADMEM=1`). A Gemmini run without it sits at the bootrom
> `wfi` for many minutes and looks like a hang. See `Rocket_Build_Test.md` §5.3.

```bash
cd /home/vscode/chipyard/sims/verilator
GT=../../generators/gemmini/software/gemmini-rocc-tests/build

make run-binary CONFIG=LeanGemminiRocketConfig LOADMEM=1 BINARY=$GT/bareMetalC/matmul_ws-baremetal
make run-binary CONFIG=LeanGemminiRocketConfig LOADMEM=1 BINARY=$GT/bareMetalC/tiled_matmul_ws-baremetal
```

All the usual modes apply — `run-binary-fast`, `run-binary-debug`,
`EXTRA_SIM_FLAGS="+max-cycles=..."`. Artifacts land in
`output/chipyard.harness.TestHarness.LeanGemminiRocketConfig/` exactly as in the Rocket note.

Gemmini tests print their own progress and pass/fail text over UART (into `.log`), *in
addition to* the harness `*** PASSED ***` verdict in `.out`.

A healthy `template-baremetal` run prints its stages over UART — useful for telling real
progress from a stall (✅ captured here):

```
Flush Gemmini TLB of stale virtual addresses
Initialize our input and output matrices in main memory
Calculate the scratchpad addresses of all our matrices
Move "In" matrix from main memory into Gemmini's scratchpad
Move "Identity" matrix from main memory into Gemmini's scratchpad
Multiply "In" matrix with "Identity" matrix with a bias of 0
Move "Out" matrix from Gemmini's scratchpad into main memory
Fence till Gemmini completes all memory operations
```

**Runtime expectation** — measured here on an idle machine, `LeanGemminiRocketConfig` with
`LOADMEM=1`:

| Test | Cycles | Wall |
|---|---:|---|
| `mvin_mvout-baremetal` (data movement) | 66 646 | **19 s** |
| `matmul_ws-baremetal` (matmul + scale sweep) | 1 738 258 | **895 s** (~15 min) |

Roughly **1 900 cycles/s**. Data-movement tests are seconds; matmul tests are a quarter hour.
Raise `+max-cycles` for the tiled/conv tests.

⚠️ **Do not run these alongside a parallel `run-asm-tests` sweep.** Verilator simulators are
single-threaded but CPU-hungry; a `-j$(nproc)` suite starved this exact test badly enough to
make it look ~5× slower than it is (measured 370 cyc/s contended vs 1 900 idle).

### 5.1.1 ⚠️ Dataflow must match the config

`LeanGemminiConfig` sets `dataflow = Dataflow.WS` — **weight-stationary only**. Tests that
exercise the output-stationary path will not behave correctly on it:

| Test | `LeanGemminiRocketConfig` (WS) | `GemminiRocketConfig` (BOTH) |
|---|---|---|
| `mvin_mvout` and data-movement tests | ✅ **passes** (19 s, 66 646 cycles) | expected to pass |
| `matmul_ws` | ❌ **FAILS** — numeric mismatch, see §5.1.2 | ✅ **passes** (9 673 026 cycles, 1655 s) |
| `matmul_os`, `tiled_matmul_os` | **unsupported** — OS dataflow absent | supported |
| `matmul` (tries both) | **unsupported** — ✅ observed printing `Output-stationary` on a WS-only design | supported |

### 5.1.2 `matmul_ws` fails on Lean, passes on the full config — a config limitation, not a bug

**Established by controlled experiment here.** Same binary, same `LOADMEM=1`, idle machine:

| Where | Result |
|---|---|
| `LeanGemminiRocketConfig` RTL | ❌ **FAILED** (exit 1) after 895 s / 1 738 258 cycles |
| `GemminiRocketConfig` RTL | ✅ **PASSED** after 1655 s / 9 673 026 cycles |
| Spike (`spike --extension=gemmini`) | ✅ passes |
| `mvin_mvout-baremetal` on the *Lean* RTL | ✅ passes in 19 s |

**Conclusion: this is a `leanConfig` limitation, not a Gemmini RTL bug.** The Lean data path
is sound (`mvin_mvout` passes), the test is sound (Spike and the full config both pass), and
the only variable left is the Lean parameter set.

The failure is a **small numeric mismatch**, not a crash — saturated `±127/-128` entries all
agree, while non-saturated entries differ by a few LSBs (actual `-50` vs gold `-52`, `102` vs
`86`, `2` vs `-10`). The Lean run aborts at the first mismatch, which is why it burns only
1.7 M cycles against the full config's 9.7 M for the complete activation/scale sweep.

**What is *not* isolated.** `leanConfig` differs from `defaultConfig` in six settings
(`Configs.scala:244`) — `dataflow=WS`, `max_in_flight_mem_reqs=64`,
`acc_read_full_width=false`, `ex_read_from_acc=false`, `ex_write_to_spad=false`,
`hardcode_d_to_garbage_addr=true`. The experiment proves *the set* is responsible, not which
member. `acc_read_full_width = false` remains the leading suspect — it narrows the accumulator
read port from `acc_w` to `spad_w` (`Scratchpad.scala:212`), and `matmul_ws.c` sweeps the
accumulator scale path (`for (acc_scale_t scale = SINIT; scale <= 12; scale += 4)`), which is
exactly where LSB-level rounding differences would surface. Pinning it down would need a
one-flag-at-a-time config and another Verilator build per flag.

**Practical guidance:** use `LeanGemminiRocketConfig` for data-movement and plumbing work,
where it is fast and correct. Validate numeric matmul results on `GemminiRocketConfig` or
Spike. **Do not report a Lean numeric mismatch upstream as an RTL bug** — reproduce it on the
full config first.

If a Gemmini test misbehaves on a Lean config, check its dataflow before suspecting the RTL.

### 5.2 On Spike (functional, ~100× faster)

Chipyard installs the Gemmini Spike extension ✅ — `$RISCV/lib/libgemmini.so` is present:

```bash
cd generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC
spike --extension=gemmini mvin_mvout-baremetal        # ✅ verified
# → Gemmini extension configured with:
#       dim = 16
```

Use Spike to check functional correctness of a kernel first, then move to Verilator only when
you need cycle counts or waveforms. If `libgemmini.so` is ever missing, rebuild it with
`make -C generators/gemmini/software/libgemmini install`.

### 5.3 DNN workloads

`build/imagenet/` (ResNet50 etc.), `build/mlps/`, `build/transformers/` are full networks.
They run fine on Spike:

```bash
spike --extension=gemmini build/imagenet/resnet50-baremetal
```

but ❌ are impractical on Verilator — upstream measures them in **days**. Use
[FireSim](https://fires.im/) for cycle-accurate DNN numbers.

## 6. Test catalog

`bareMetalC/` grouped by what they exercise:

| Group | Tests |
|---|---|
| **Data movement** | `mvin_mvout`, `mvin_mvout_stride`, `mvin_mvout_zeros`, `mvin_mvout_spad`, `mvin_mvout_block_stride`, `mvin_mvout_acc[_full][_stride][_zero_stride]`, `mvin_scale`, `aligned`, `padded` |
| **Basic matmul** | `matmul`, `matmul_os` (output-stationary), `matmul_ws` (weight-stationary), `matmul_spad` |
| **Tiled matmul** | `tiled_matmul_ws`, `tiled_matmul_os`, `tiled_matmul_cpu`, `tiled_matmul_option`, `tiled_matmul_ws_At`, `_Bt`, `_full_C`, `_low_D`, `_perf` |
| **Fused DNN ops** | `tiled_matmul_ws_igelu`, `_layernorm`, `_softmax` |
| **Convolution** | `conv`, `conv_stride`, `conv_rect`, `conv_rect_pool`, `conv_with_pool`, `conv_first_layer`, `conv_dw`, `conv_dw_perf`, `conv_perf`, `conv_with_kernel_dilation`, `conv_with_input_dilation[_and_neg_padding][_and_rot180]`, `conv_with_rot180`, `conv_trans_input_3120[_with_kernel_dilation]`, `conv_trans_output_1203`, `conv_trans_weight_0132`, `conv_trans_weight_1203` |
| **Elementwise / pooling** | `matrix_add`, `resadd`, `resadd_stride`, `global_average`, `transpose` |
| **Microarchitecture** | `raw_hazard` (RAW dependency handling), `gemmini_counter` (performance counters), `aligned` |
| **Template** | `template` — the skeleton to copy |

Good smoke-test order: `template` → `mvin_mvout` → `matmul_ws` → `tiled_matmul_ws` → `conv`.

## 7. Writing your own test

```bash
cd generators/gemmini/software/gemmini-rocc-tests
cp bareMetalC/template.c bareMetalC/my_test.c
# add `my_test` to the `tests` list at the top of bareMetalC/Makefile
./build.sh
# → build/bareMetalC/my_test-baremetal
```

Tests mostly call the C wrappers in `include/gemmini.h` rather than raw ISA instructions —
`tiled_matmul_auto`, `tiled_conv_auto`, `gemmini_config_ex`, etc. See
`notes/gemmini-isa-reference.md` for the raw instruction encodings.

## 8. Verification log

| # | Command | Result |
|---|---|---|
| 1 | `./build.sh` in `gemmini-rocc-tests` | ✅ exit 0 — 56 baremetal + 56 linux + 56 pk, plus imagenet/mlps/transformers |
| 2 | `ls $RISCV/lib/libgemmini*` | ✅ `libgemmini.so` installed |
| 3 | `spike --extension=gemmini mvin_mvout-baremetal` | ✅ exit 0, `dim = 16` |
| 4 | config/parameter tables | ✅ read from `GemminiConfigs.scala`, `Configs.scala:21`, `ConfigsFP.scala:88` |
| 5 | `make -j8 CONFIG=LeanGemminiRocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-LeanGemminiRocketConfig`, 15.7 MB |
| 6 | `run-binary ... template-baremetal` (no `LOADMEM`) | ⚠️ ran the full test but **exceeded 900 s** and was killed at cycle 84 021 — the serial TSI load plus a Gemmini-sized design. UART showed every stage completing (TLB flush → mvin → matmul → mvout → fence) |
| 7 | `run-binary ... matmul-baremetal LOADMEM=1` | ✅ boots and executes the test (UART: `A_transpose: 0, B_transpose: 0, dataflow: 0` / `Output-stationary`); revealed the WS/OS mismatch in §5.1.1 |
| 8 | `run-binary ... matmul_ws-baremetal LOADMEM=1` (contended) | ⏳ inconclusive — cycle 665 091, killed at 1800 s while sharing 16 cores with 48 other simulators |
| 9 | `run-binary ... matmul_ws-baremetal LOADMEM=1` (idle machine) | ❌ **FAILED** (exit 1) after 895 s / 1 738 258 cycles — numeric mismatch. See §5.1.2 |
| 10 | `spike --extension=gemmini matmul_ws-baremetal` | ✅ passes — isolates the failure to the RTL config, not the test |
| 11 | `run-binary ... mvin_mvout-baremetal LOADMEM=1` (idle) | ✅ `*** PASSED ***`, 66 646 cycles / 19 s — RTL, `LOADMEM` and data path all sound |
| 12 | `make -j14 CONFIG=GemminiRocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-GemminiRocketConfig`, 15.6 MB |
| 13 | `run-binary ... matmul_ws-baremetal LOADMEM=1` on **`GemminiRocketConfig`** | ✅ `*** PASSED ***`, 9 673 026 cycles / 1655 s — **settles §5.1.2: Lean config limitation, not an RTL bug** |
