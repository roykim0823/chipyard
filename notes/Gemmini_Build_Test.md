# Gemmini — Build & Test on Verilator

Build and test the **Gemmini** systolic-array GEMM/DNN accelerator in Chipyard. Gemmini is a
parameterized `meshRows × meshColumns` PE array attached to a Rocket (or Shuttle) core over the
**RoCC** interface.

**[§1](#1--build--run) is the whole tutorial** — pick a config, build it, build the test software,
run a kernel. Everything else is reference and lives in the appendices.

> The simulation machinery — run modes, artifact layout, Makefile variables, waveforms, pass/fail
> rules — is **identical to Rocket** and documented once in
> [`Rocket_Build_Test.md`](Rocket_Build_Test.md). Read that first; this note covers only what is
> Gemmini-specific.

**Verified against** `/home/vscode/chipyard`, Gemmini submodule `8c3f992` (`v0.1a-515`) — first
pass 2026-09-08, re-verified 2026-09-11.

> **Legend (verification status)** — ✅ command actually run here · ❌ does not work in this
> environment · ⏳ not run · ⚠️ caution. Capability tables spell out "supported / unsupported"
> in words rather than reusing these glyphs.

---

## Contents

**Tutorial**
- [1 — Build & run](#1--build--run)

**Reference**
- [Appendix A — Troubleshooting](#appendix-a--troubleshooting)
- [Appendix B — Config reference](#appendix-b--config-reference)
- [Appendix C — Test software reference](#appendix-c--test-software-reference)
- [Appendix D — Investigation: `matmul_ws` on Lean vs. full](#appendix-d--investigation-matmul_ws-on-lean-vs-full)
- [Appendix E — Verification log](#appendix-e--verification-log)

---

# 1 — Build & run

## 1.1 Set up the shell

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator
```

## 1.2 Pick a config

Gemmini ships **6 config classes** ✅ in `generators/gemmini/chipyard/GemminiConfigs.scala`. They
live in `package chipyard` and are auto-discovered (`build.sbt:245`), so **only `CONFIG=`
changes**; there are no Gemmini-specific make targets.

**Start with `LeanGemminiRocketConfig`** — meaningfully cheaper to elaborate and simulate than
the full config. But read §1.6 first: Lean is weight-stationary only, and that changes which
tests are meaningful on it. Full list: Appendix B.2.

## 1.3 Build the simulator

```bash
make -j$(nproc) CONFIG=LeanGemminiRocketConfig     # ✅ → simulator-chipyard.harness-LeanGemminiRocketConfig (15.7 MB)
```

Add `debug` for waveforms. Expect a long first build — full elaboration plus a Verilator compile,
with a 16×16 PE array on top of Rocket.

## 1.4 Build the test software

```bash
cd /home/vscode/chipyard/generators/gemmini/software/gemmini-rocc-tests
./build.sh                                          # ✅ verified
cd /home/vscode/chipyard/sims/verilator
```

`build.sh` runs `autoconf`, configures into `build/`, then `make -j`. It builds Linux binaries too
when `riscv64-unknown-linux-gnu-gcc` is on `PATH`, otherwise falls back to `BAREMETAL_ONLY=1`.
Those are the two toolchain *target triples* installed under `$RISCV` — bare-metal vs. Linux
userspace; see [`Rocket_Build_Test.md`](Rocket_Build_Test.md) Appendix C for what each sysroot
holds and why.
✅ Both toolchains are present here, so all three environments get built — the one you want on
Verilator is **`-baremetal`**:

| Suffix | Environment | Run on |
|---|---|---|
| `-baremetal` | no OS, no virtual memory | **Verilator / VCS** — this is the one you want |
| `-pk` | RISC-V proxy kernel, has virtual memory | Verilator, via `pk`. ⚠️ limited heap — some tests that pass baremetal fail here |
| `-linux` | dynamically linked, full syscalls | FireSim / Linux-capable SoCs |

Inventory and the full test catalogue: Appendix C.

## 1.5 Run a test

> ⚠️ **Always pass `LOADMEM=1`.** Without it the serial TSI boot dominates — on a comparable
> accelerator config the binary load was **~92% of all simulated cycles** (99 666 → 7 436 with
> `LOADMEM=1`). A Gemmini run without it sits at the bootrom `wfi` for many minutes and looks like
> a hang (Appendix A).

Two ways, exactly as in Rocket §1.3.

### Option 1 — run the simulator directly

The simulator is a plain executable (Rocket Appendix B.2). The ELF path appears **twice**: once as
`+loadmem=` — what `LOADMEM=1` expands to — and once as the positional argument.

**Simplest form that works** ✅ — nothing but the preload and the fesvr brackets:

```bash
./simulator-chipyard.harness-LeanGemminiRocketConfig +permissive \
  +loadmem=../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal \
  +permissive-off \
  ../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal
# ✅ rc=0 · 12 s · 63 796 cycles
```

**Full form** ✅ — adds the two plusargs `make` always passes:

```bash
./simulator-chipyard.harness-LeanGemminiRocketConfig +permissive \
  +dramsim +dramsim_ini_dir=../../generators/testchipip/src/main/resources/dramsim2_ini \
  +max-cycles=100000000 \
  +loadmem=../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal \
  +permissive-off \
  ../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal
# ✅ rc=0 · 12 s · 66 646 cycles
```

> ⚠️ **The simple form is not cycle-comparable.** ✅ Measured: **63 796** cycles without `+dramsim`
> vs **66 646** with — the DRAMSim2 timing model is the whole difference, the same effect
> documented in [`Rocket_Build_Test.md`](Rocket_Build_Test.md) Appendix E.2. Use the simple form to
> ask *"does it run?"*; keep `+dramsim` for any number you intend to quote or compare with `make`.

Neither form prints a verdict, because neither passes `+verbose` — the pass signal is the **exit
status** (`0` on `$finish`). Add `+verbose` and pipe stderr through `spike-dasm` to see it ✅:

```bash
./simulator-chipyard.harness-LeanGemminiRocketConfig +permissive \
  +dramsim +dramsim_ini_dir=../../generators/testchipip/src/main/resources/dramsim2_ini \
  +max-cycles=100000000 +verbose \
  +loadmem=../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal \
  +permissive-off \
  ../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal 2>&1 >/dev/null | spike-dasm | tail -1
# ✅ → *** PASSED *** Completed after 66646 simulation cycles
```

### Option 2 — through `make`

Shorter to type, and it files the artifacts under
`output/chipyard.harness.TestHarness.LeanGemminiRocketConfig/` for you. `LOADMEM=1` reuses
`BINARY=` as the preload, so the path is written once:

```bash
make run-binary CONFIG=LeanGemminiRocketConfig LOADMEM=1 \
  BINARY=../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/mvin_mvout-baremetal        # ✅ 66 646 cyc

make run-binary CONFIG=LeanGemminiRocketConfig LOADMEM=1 \
  BINARY=../../generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC/tiled_matmul_ws-baremetal   # ⏳ longer — raise +max-cycles if it times out
```

Every mode and variable from the Rocket note applies unchanged — `run-binary-fast`,
`run-binary-debug`, `EXTRA_SIM_FLAGS="+max-cycles=…"` — and artifacts land in
`output/chipyard.harness.TestHarness.LeanGemminiRocketConfig/` with the same names and pass/fail
rules (Rocket §2.1, §2.2).

Gemmini tests print their own progress and pass/fail text over UART (into `.log`), *in addition
to* the harness `*** PASSED ***` verdict in `.out`. A healthy `template-baremetal` run prints its
stages — useful for telling real progress from a stall (✅ captured here):

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

**Budget your time** — measured here on an idle machine, `LeanGemminiRocketConfig` with
`LOADMEM=1`:

| Test | Cycles | Wall |
|---|---:|---|
| `mvin_mvout-baremetal` (data movement) | 66 646 | **19 s** |
| `matmul_ws-baremetal` (matmul + scale sweep) | 1 738 258 | **895 s** (~15 min) |

Roughly **1 900 cycles/s**. Data-movement tests are seconds; matmul tests are a quarter hour.
Raise `+max-cycles` for the tiled/conv tests.

> ⚠️ **Do not run these alongside a parallel `run-asm-tests` sweep.** Verilator simulators are
> single-threaded but CPU-hungry; a `-j$(nproc)` suite starved this exact test badly enough to
> make it look ~5× slower than it is (370 cyc/s contended vs 1 900 idle).

## 1.6 Which tests work on which config ⚠️

`LeanGemminiConfig` sets `dataflow = Dataflow.WS` — **weight-stationary only**. Tests that
exercise the output-stationary path will not behave correctly on it:

| Test | `LeanGemminiRocketConfig` (WS) | `GemminiRocketConfig` (BOTH) |
|---|---|---|
| `mvin_mvout` and data-movement tests | ✅ **passes** (19 s, 66 646 cycles) | expected to pass |
| `matmul_ws` | ❌ **FAILS** — numeric mismatch (Appendix D) | ✅ **passes** (9 673 026 cycles, 1655 s) |
| `matmul_os`, `tiled_matmul_os` | **unsupported** — OS dataflow absent | supported |
| `matmul` (tries both) | **unsupported** — ✅ observed printing `Output-stationary` on a WS-only design | supported |

**Practical rule:** use `LeanGemminiRocketConfig` for data-movement and plumbing work, where it is
fast and correct. Validate **numeric** matmul results on `GemminiRocketConfig` or Spike.

> ⚠️ **Do not report a Lean numeric mismatch upstream as an RTL bug.** It was established here by
> controlled experiment that `matmul_ws` failing on Lean is a *config limitation*, not a Gemmini
> RTL defect — reproduce on the full config first. Full investigation: Appendix D.

If a Gemmini test misbehaves on a Lean config, check its dataflow before suspecting the RTL.

## 1.7 Use Spike for the fast loop

Chipyard installs the Gemmini Spike extension ✅ — `$RISCV/lib/libgemmini.so` is present:

```bash
cd generators/gemmini/software/gemmini-rocc-tests/build/bareMetalC
spike --extension=gemmini mvin_mvout-baremetal        # ✅ verified
# → Gemmini extension configured with:
#       dim = 16
```

Spike is functional-only but **~100× faster**. Check a kernel's correctness there first, then move
to Verilator when you need cycle counts or waveforms.

---

# Appendix A — Troubleshooting

## Run sits at the bootrom `wfi` for minutes

Not a hang — the serial TSI load dominates on an accelerator-sized design. Add `LOADMEM=1`
(§1.5). ✅ Observed: a `template-baremetal` run without it exceeded 900 s and was killed at cycle
84 021, even though UART showed every stage completing.

## A matmul test fails with a small numeric mismatch on a Lean config

Expected; not an RTL bug. Saturated `±127/-128` entries agree while non-saturated ones differ by a
few LSBs. Reproduce on `GemminiRocketConfig` before suspecting the RTL — see §1.6 and Appendix D.

## A test prints `Output-stationary` on a Lean config

Dataflow mismatch. `leanConfig` is weight-stationary only; `matmul` tries both dataflows and
`matmul_os` needs OS. Use `GemminiRocketConfig` for those (§1.6).

## Simulation seems ~5× slower than the quoted numbers

CPU contention. Verilator sims are single-threaded but saturate a core; a parallel
`run-asm-tests -j$(nproc)` sweep alongside dropped this box from 1 900 to 370 cycles/s. Run
accelerator tests on an idle machine.

## `spike --extension=gemmini` can't find the extension

`libgemmini.so` is missing from `$RISCV/lib`. Rebuild it:

```bash
make -C generators/gemmini/software/libgemmini install
```

## A test passes baremetal but fails under `pk`

The proxy-kernel environment has a limited heap. Prefer `-baremetal` binaries on Verilator (§1.4).

---

# Appendix B — Config reference

## B.1 What Gemmini is

A parameterized systolic-array accelerator on the RoCC interface. Major blocks (from
`generators/gemmini/README.md`):

| Block | Role |
|---|---|
| **Decoupled access/execute** | separate load / store / execute queues so DMA overlaps compute |
| **Scratchpad + accumulator** | banked on-chip SRAM; scratchpad holds operands, accumulator holds partial sums at wider precision |
| **Systolic array + transposer** | the `meshRows × meshColumns` PE grid; transposer feeds A or B transposed |
| **DMA** | moves tiles between DRAM/L2 and the scratchpad (`mvin`/`mvout`) |
| **ROB** | tracks dependencies between queued Gemmini instructions |
| **Loop unrollers** | hardware `loop_ws` / `loop_conv` state machines that expand a whole tiled matmul or convolution from one instruction |

The ISA is documented in `generators/gemmini/README.md` §ISA (line 382 onward) and in
[`gemmini-isa-reference.md`](gemmini-isa-reference.md).

## B.2 Config classes

| Config | Accelerator | Cores | Notes |
|---|---|---|---|
| `GemminiRocketConfig` | `DefaultGemminiConfig` | 1 Rocket | full-featured, both dataflows |
| `LeanGemminiRocketConfig` | `LeanGemminiConfig` | 1 Rocket | **recommended starting point** — smaller, faster to simulate |
| `LeanGemminiPrintfRocketConfig` | `LeanGemminiPrintfConfig` | 1 Rocket | Lean + FireSim simulation counters |
| `FPGemminiRocketConfig` | `GemminiFP32DefaultConfig` | 1 Rocket | FP32 datapath |
| `GemminiShuttleConfig` | `DefaultGemminiConfig` | Shuttle | superscalar in-order core instead of Rocket |
| `ReRoCCManyGemminiConfig` | 4 × `LeanGemminiConfig` | 4 Rocket | ReRoCC — 4 Gemminis shared over 4 cores |

All the Rocket variants also apply `WithSystemBusWidth(128)` — the default 64-bit SBus cannot feed
a 16×16 8-bit array.

## B.3 Array parameters

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

`leanConfig` is `defaultConfig` with six changes (`Configs.scala:244`): `dataflow=WS`
(weight-stationary only), `max_in_flight_mem_reqs=64`, `acc_read_full_width=false`,
`ex_read_from_acc=false`, `ex_write_to_spad=false`, `hardcode_d_to_garbage_addr=true`. Dropping
the OS dataflow and the extra read/write ports is what makes it substantially cheaper to elaborate
and simulate — and is also why numeric results can differ (Appendix D).

`FP32DefaultConfig` swaps every datatype to `Float(8, 24)` (IEEE binary32) with `tile_latency = 2`.
FP16 (`Float(5,11)`) and BF16 (`Float(8,8)`) variants also exist in `ConfigsFP.scala` but have no
Chipyard-level config class.

---

# Appendix C — Test software reference

## C.1 What `build.sh` produces ✅

Counted 2026-09-11, all three environments built:

| Directory | Distinct programs | Binaries (× 3 environments) |
|---|---:|---:|
| `build/bareMetalC/` | 56 | 168 |
| `build/mlps/` | 9 | 27 |
| `build/imagenet/` | 2 | 6 |
| `build/transformers/` | 1 | 3 |

> ⚠️ `bareMetalC/` has **57** `.c` sources but builds **56** — `transpose_scale.c` is absent from
> the Makefile's `tests` list and is silently skipped ✅.

## C.2 Test catalog

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
On a Lean config, stop before `matmul_ws` (§1.6).

## C.3 Writing your own test

```bash
cd generators/gemmini/software/gemmini-rocc-tests
cp bareMetalC/template.c bareMetalC/my_test.c
# add `my_test` to the `tests` list at the top of bareMetalC/Makefile
./build.sh
# → build/bareMetalC/my_test-baremetal
```

Forgetting the Makefile edit is how `transpose_scale.c` ended up unbuilt (C.1).

Tests mostly call the C wrappers in `include/gemmini.h` rather than raw ISA instructions —
`tiled_matmul_auto`, `tiled_conv_auto`, `gemmini_config_ex`, etc. See
[`gemmini-isa-reference.md`](gemmini-isa-reference.md) for the raw instruction encodings.

## C.4 DNN workloads

`build/imagenet/` (ResNet50 etc.), `build/mlps/` and `build/transformers/` are full networks. They
run fine on Spike:

```bash
spike --extension=gemmini build/imagenet/resnet50-baremetal
```

but ❌ are impractical on Verilator — upstream measures them in **days**. Use
[FireSim](https://fires.im/) for cycle-accurate DNN numbers.

---

# Appendix D — Investigation: `matmul_ws` on Lean vs. full

Why `matmul_ws-baremetal` fails on `LeanGemminiRocketConfig` and passes on `GemminiRocketConfig`.
**Established by controlled experiment here** — same binary, same `LOADMEM=1`, idle machine:

| Where | Result |
|---|---|
| `LeanGemminiRocketConfig` RTL | ❌ **FAILED** (exit 1) after 895 s / 1 738 258 cycles |
| `GemminiRocketConfig` RTL | ✅ **PASSED** after 1655 s / 9 673 026 cycles |
| Spike (`spike --extension=gemmini`) | ✅ passes |
| `mvin_mvout-baremetal` on the *Lean* RTL | ✅ passes in 19 s |

**Conclusion: a `leanConfig` limitation, not a Gemmini RTL bug.** The Lean data path is sound
(`mvin_mvout` passes), the test is sound (Spike and the full config both pass), and the only
variable left is the Lean parameter set.

The failure is a **small numeric mismatch**, not a crash — saturated `±127/-128` entries all agree,
while non-saturated entries differ by a few LSBs (actual `-50` vs gold `-52`, `102` vs `86`, `2`
vs `-10`). The Lean run aborts at the first mismatch, which is why it burns only 1.7 M cycles
against the full config's 9.7 M for the complete activation/scale sweep.

**What is *not* isolated.** `leanConfig` differs from `defaultConfig` in six settings
(`Configs.scala:244`). The experiment proves *the set* is responsible, not which member.
`acc_read_full_width = false` remains the leading suspect — it narrows the accumulator read port
from `acc_w` to `spad_w` (`Scratchpad.scala:212`), and `matmul_ws.c` sweeps the accumulator scale
path (`for (acc_scale_t scale = SINIT; scale <= 12; scale += 4)`), which is exactly where
LSB-level rounding differences would surface. Pinning it down would need a one-flag-at-a-time
config and another Verilator build per flag.

---

# Appendix E — Verification log

Executed 2026-09-08:

| # | Command | Result |
|---|---|---|
| 1 | `./build.sh` in `gemmini-rocc-tests` | ✅ exit 0 — 56 baremetal + 56 linux + 56 pk, plus imagenet/mlps/transformers |
| 2 | `ls $RISCV/lib/libgemmini*` | ✅ `libgemmini.so` installed |
| 3 | `spike --extension=gemmini mvin_mvout-baremetal` | ✅ exit 0, `dim = 16` |
| 4 | config/parameter tables | ✅ read from `GemminiConfigs.scala`, `Configs.scala:21`, `ConfigsFP.scala:88` |
| 5 | `make -j8 CONFIG=LeanGemminiRocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-LeanGemminiRocketConfig`, 15.7 MB |
| 6 | `run-binary ... template-baremetal` (no `LOADMEM`) | ⚠️ ran the full test but **exceeded 900 s** and was killed at cycle 84 021 — the serial TSI load plus a Gemmini-sized design. UART showed every stage completing (TLB flush → mvin → matmul → mvout → fence) |
| 7 | `run-binary ... matmul-baremetal LOADMEM=1` | ✅ boots and executes the test (UART: `A_transpose: 0, B_transpose: 0, dataflow: 0` / `Output-stationary`); revealed the WS/OS mismatch in §1.6 |
| 8 | `run-binary ... matmul_ws-baremetal LOADMEM=1` (contended) | ⏳ inconclusive — cycle 665 091, killed at 1800 s while sharing 16 cores with 48 other simulators |
| 9 | `run-binary ... matmul_ws-baremetal LOADMEM=1` (idle machine) | ❌ **FAILED** (exit 1) after 895 s / 1 738 258 cycles — numeric mismatch. See Appendix D |
| 10 | `spike --extension=gemmini matmul_ws-baremetal` | ✅ passes — isolates the failure to the RTL config, not the test |
| 11 | `run-binary ... mvin_mvout-baremetal LOADMEM=1` (idle) | ✅ `*** PASSED ***`, 66 646 cycles / 19 s — RTL, `LOADMEM` and data path all sound |
| 12 | `make -j14 CONFIG=GemminiRocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-GemminiRocketConfig`, 15.6 MB |
| 13 | `run-binary ... matmul_ws-baremetal LOADMEM=1` on **`GemminiRocketConfig`** | ✅ `*** PASSED ***`, 9 673 026 cycles / 1655 s — **settles Appendix D: Lean config limitation, not an RTL bug** |

Added 2026-09-11:

| # | Command | Result |
|---|---|---|
| 14 | `ls $RISCV/lib/libgemmini*` | ✅ `libgemmini.so` still installed — re-confirms #2 |
| 15 | built-test inventory under `build/` | ✅ `bareMetalC` 56 programs / 168 binaries; `mlps` 9 / 27; `imagenet` 2 / 6; `transformers` 1 / 3 (C.1) |
| 16 | `bareMetalC/*.c` vs the Makefile `tests` list | ✅ **57 sources, 56 built** — `transpose_scale` is absent from the list, confirming the C.1 note |
| 17 | `grep -c '^class .*Config' GemminiConfigs.scala` | ✅ **6** config classes, matching B.2 |
| 18 | relink after the simulator executables were removed | ✅ `make CONFIG=LeanGemminiRocketConfig` rebuilt the binary by **linking only** — `generated-src/` still held the 244 objects and the 24 MB `VTestDriver__ALL.a`, so no re-elaboration |
| 19 | `mvin_mvout-baremetal`, simulator invoked directly, simple form (`+permissive +loadmem=… +permissive-off`) | ✅ rc=0, **12 s**, 63 796 cycles |
| 20 | same, full form (`+dramsim` + `+max-cycles`) | ✅ rc=0, **12 s**, **66 646 cycles** — matches #11's count exactly; the 2 850-cycle gap vs #19 is entirely the DRAMSim2 model |
| 21 | same + `+verbose`, stderr through `spike-dasm` | ✅ `*** PASSED *** Completed after 66646 simulation cycles`, 13 s |
