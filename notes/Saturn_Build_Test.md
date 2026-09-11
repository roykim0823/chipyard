# Saturn — Build & Test on Verilator

Build and test the **Saturn** RVV 1.0 vector unit in Chipyard. Saturn is a parameterized
application-profile vector unit that attaches as the *core's* vector unit — not a RoCC
accelerator — inside either a Rocket or a Shuttle tile.

**[§1](#1--build--run) is the whole tutorial** — pick a config, build it, get vector binaries, run
them. Everything else is reference and lives in the appendices.

> The simulation machinery — run modes, artifact layout, Makefile variables, waveforms, pass/fail
> rules — is **identical to Rocket** and documented once in
> [`Rocket_Build_Test.md`](Rocket_Build_Test.md). Read that first; this note covers only what is
> Saturn-specific.

**Verified against** `/home/vscode/chipyard`, Saturn submodule `dfe75de` (`master`) — first pass
2026-09-08, re-verified 2026-09-11.

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
- [Appendix D — Cosimulation configs](#appendix-d--cosimulation-configs)
- [Appendix E — Verification log](#appendix-e--verification-log)

---

# 1 — Build & run

## 1.1 Set up the shell

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator
```

## 1.2 Pick a config

Saturn ships **26 config classes** in `generators/saturn/chipyard/SaturnConfigs.scala` ✅ —
10 Rocket-attached, 14 Shuttle-attached, 2 cosimulation. They live in `package chipyard` and are
auto-discovered (`build.sbt:245`), so **only `CONFIG=` changes**; there are no Saturn-specific
make targets.

Names decode as `<FLAVOR>V<VLEN>D<DLEN>[M<MLEN>]<Core>Config`, e.g. `MINV128D64RocketConfig` =
minimal flavor, VLEN 128, DLEN 64, on Rocket. Full list and the parameter meanings: Appendix B.

**Start with `MINV128D64RocketConfig`.** Elaboration and simulation cost scale steeply with
`DLEN`, and to a lesser extent `VLEN` — `REFV512D512ShuttleConfig` sits at the far end of the
design space and is slow to build *and* slow to run.

## 1.3 Build the simulator

```bash
make -j$(nproc) CONFIG=MINV128D64RocketConfig      # ✅ → simulator-chipyard.harness-MINV128D64RocketConfig (12.9 MB)
```

## 1.4 Get vector binaries

Two sources, in increasing order of effort.

**(a) Already installed — zero build.** `riscv-tests` ships four RVV benchmarks that a scalar
config cannot run, so a Saturn config is exactly where they belong ✅:

```bash
BM=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks
ls $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-*.riscv        # vec-daxpy vec-memcpy vec-sgemm vec-strcmp
```

These are the **smoke tests**: hand-written RVV assembly kernels, built `-march=rv64gcv` (base V
only), each ending in `return verify…()` so a wrong result becomes `*** FAILED ***` ✅. They run on
*any* RVV config.

**(b) Saturn's own benchmarks — 33 kernels**, the practical option for real coverage ✅:

```bash
cd /home/vscode/chipyard/generators/saturn/benchmarks
make vec-daxpy.riscv      # ✅ one benchmark
make                      # all 33
cd /home/vscode/chipyard/sims/verilator
```

These are the **workload suite**: RVV *C intrinsics* (`#include <riscv_vector.h>`) ported from the
Barcelona Supercomputing Center / Ara vector benchmarks (`benchmarks/common/ara/`), each printing
its own `mcycle`/`minstret` deltas for the region of interest. The catalogue is in Appendix C.1.

They compile `-march=rv64gcv_zfh_zvfh -mabi=lp64d` (`Makefile:64`); the conda toolchain
(`riscv64-unknown-elf-gcc 13.2.0`) handles that ✅.

> ⚠️ Those flags request **Zfh + Zvfh** (FP16 vector) — unlike the riscv-tests four, which need
> only base V. Confirm the flavor you picked enables Zvfh before blaming a kernel for a trap.

### Which set to use

| | `riscv-tests` `vec-*` (BM) | Saturn benchmarks (SB) |
|---|---|---|
| Count | 4 | 33 |
| Kernel written as | hand-written RVV assembly | RVV C intrinsics |
| `-march` | `rv64gcv` — base V only | `rv64gcv_zfh_zvfh` — needs Zfh + Zvfh |
| Reports | pass/fail only | pass/fail **plus** cycle and instruction counts |
| Self-checks the result | all 4 ✅ | **27 of 33** ✅ — see the ⚠️ below |
| Use for | "is the vector unit alive and correct?" | real kernel coverage and performance |

> ⚠️ **6 of the 33 Saturn benchmarks never fail.** ✅ `vec-daxpy`, `vec-div-approx`,
> `vec-dotprod`, `vec-fdotprod`, `vec-square-root-approx` and `vec-tasks` end in a bare
> `return 0` — they time the kernel but never compare against a golden result. On those a
> `*** PASSED ***` means only *"ran to completion without trapping"*, **not** *"computed the right
> answer"*. The other 27 return `verify…()` or an error index, so their verdict is real.

## 1.5 Run a test

> ⚠️ **Always pass `LOADMEM=1`** on a Saturn config. ✅ Measured on `MINV128D64RocketConfig` with
> `rv64ui-p-add`:
>
> | | Cycles | Wall |
> |---|---:|---|
> | default (serial TSI load) | 99 666 | did not finish in 110 s |
> | `LOADMEM=1` | **7 436** | **23 s** |
>
> The binary load is **~92% of all simulated cycles**. Without it the run sits at the bootrom
> `wfi` and looks like a hang (Appendix A).

Two ways, exactly as in Rocket §1.3.

### Option 1 — run the simulator directly

The simulator is a plain executable (Rocket Appendix B.2), so the simplest possible run is the
binary and nothing else — no plusargs at all:

```bash
./simulator-chipyard.harness-MINV128D64RocketConfig $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-strcmp.riscv
# ✅ passes (rc=0) · 392 186 simulation cycles · 46 s
```

✅ That works, and it is the fastest thing to *type*. It is also **23× slower to run** than it
needs to be, because without `+loadmem=` the binary arrives over the serial TSI link one word at a
time — the very stall the warning above describes.

**Full form** — `+loadmem=` to preload, plus the two plusargs `make` always passes. Note the ELF
path now appears **twice**: once as `+loadmem=` (that is what `LOADMEM=1` expands to) and once as
the positional argument.

```bash
./simulator-chipyard.harness-MINV128D64RocketConfig +permissive \
  +dramsim +dramsim_ini_dir=../../generators/testchipip/src/main/resources/dramsim2_ini \
  +max-cycles=100000000 \
  +loadmem=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-strcmp.riscv \
  +permissive-off \
  $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-strcmp.riscv
# ✅ 1 135 cyc · 2 s
```

Swap **both** paths to run a different binary. The `+permissive` / `+permissive-off` brackets are
mandatory once you pass any plusarg, and their order is not negotiable — Rocket Appendix E.1
explains why. (In the bare form above there are no plusargs, so no brackets are needed.)

### What the preload actually buys — same binary, both forms ✅

| | bare `./simulator-… <elf>` | `+loadmem` + `+dramsim` |
|---|---:|---:|
| Harness total cycles | **392 186** | **23 396** |
| `mcycle` reported by the benchmark | 960 | 1 135 |
| `minstret` | 84 | 84 |
| Wall | **46 s** | **2 s** |

**94% of the bare run's cycles are the serial load, not the test** — the same ~92% the warning at
the top of §1.5 quotes for `rv64ui-p-add`, measured here on a second binary. `minstret` is
identical across both (84), which confirms the two runs execute the same work; only the delivery
of the binary and the memory model differ.

⚠️ **Two different cycle numbers appear in this section — do not mix them up.**

- **`mcycle = …`** on stdout is the *target's own* counter, printed by the benchmark over UART
  after boot. It measures the kernel, not the load.
- **`Completed after N simulation cycles`** on stderr (needs `+verbose`) is the *harness* total,
  including the TSI boot.

They differ by 345× in the bare run above. The per-command comments in this section quote
`mcycle`, since that is what you see without `+verbose`.

⚠️ `mcycle` also shifts between the two forms — **960 vs 1 135** — because `+dramsim` swaps in the
DRAMSim2 timing model. Quote numbers from the `+dramsim` form if you want them comparable with
`make` (Rocket Appendix E.2).

✅ Verified verbatim — that exact command prints the DRAMSim2 banners, then:

```
mcycle = 1135
minstret = 84
- .../gen-collateral/TestDriver.v:158: Verilog $finish
```

⚠️ **There is no `*** PASSED ***` line here**, because without `+verbose` the harness never prints
one (Rocket §2.1). The pass signal is the **exit status** — `0` on `$finish`, non-zero on `$fatal`.
Add `+verbose` if you want the verdict and the commit trace, at the cost of speed.

> ✅ **Every timing in this section was measured this way**, without `+verbose`. Cycle counts are
> exact and mode-independent, but adding `+verbose` (or using plain `make run-binary`) writes a
> trace and runs slower.

### Option 2 — through `make`

Shorter to type, and it files the artifacts under
`output/chipyard.harness.TestHarness.MINV128D64RocketConfig/` for you. `LOADMEM=1` reuses
`BINARY=` as the preload, so the path is written once.

**Scalar sanity first** — it proves the simulator, the boot path and the artifact plumbing all
work before vector kernels become a variable:

```bash
make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=$RISCV/riscv64-unknown-elf/share/riscv-tests/isa/rv64ui-p-add                     # ✅ 7 436 cyc · 23 s
```

### All four `riscv-tests` vector benchmarks ✅

Every one passed on `MINV128D64RocketConfig`, ordered cheapest first. Copy and run:

```bash
make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-strcmp.riscv   # ✅ 1 135 cyc ·  2 s

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-daxpy.riscv    # ✅ 3 907 cyc ·  9 s

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-memcpy.riscv   # ✅ 5 084 cyc · 14 s

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/vec-sgemm.riscv    # ✅ 73 111 cyc · 43 s
```

The whole set costs about **70 seconds** — cheap enough to use as the standard "is this Saturn
config alive and correct?" gate after every rebuild. All four self-check, so a pass is a real
verdict (§1.4).

### Saturn benchmark examples ✅

Same surface, different price tag — these are full kernels, so they cost **minutes, not seconds**:

```bash
make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=../../generators/saturn/benchmarks/vec-exp.riscv       # ✅ 111 s

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=../../generators/saturn/benchmarks/vec-daxpy.riscv     # ✅ 186 336 ROI cyc · 259 s — ⚠️ timing only

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 \
  BINARY=../../generators/saturn/benchmarks/vec-sgemm.riscv     # ✅ 536 530 cyc · 307 s
```

All three passed. Two things to take from the numbers:

- **They are 10–150× the riscv-tests kernels.** Saturn's `vec-sgemm` burns 536 530 cycles against
  the riscv-tests `vec-sgemm`'s 73 111 ✅ — same name, different program (§1.4), with a far larger
  working set. Budget ~2–5 minutes per kernel on `MINV128D64RocketConfig`, so **all 33 is a
  multi-hour sweep**, not a gate you run after every rebuild. Use the riscv-tests four for that.
- ⚠️ **`vec-daxpy` is one of the six that never fail** (§1.4). It reports 186 336 cycles for its
  region of interest and returns 0 regardless of the result — a "does the vector unit execute?"
  probe, not a correctness check. `vec-exp` and `vec-sgemm` above do self-check.

Saturn kernels print their own region-of-interest counters over UART into `.log`:

```
-CSR   NUMBER OF EXEC CYCLES :186336
-CSR   NUMBER OF INSTRUCTIONS EXECUTED :…
```

Every mode and variable from the Rocket note applies unchanged — `run-binary-fast`,
`run-binary-debug`, `EXTRA_SIM_FLAGS="+max-cycles=…"` — and artifacts land in
`output/chipyard.harness.TestHarness.MINV128D64RocketConfig/` with the same names and the same
pass/fail rules (Rocket §2.1, §2.2).

## 1.6 Run the scalar suites

```bash
make -j$(nproc) CONFIG=MINV128D64RocketConfig run-asm-tests-fast
```

> ⚠️ **This gives you no vector coverage.** ✅ Verified twice over: the generated `.d` for
> `MINV128D64RocketConfig` contains **22 test groups — exactly the same 22 as plain
> `RocketConfig`** — and the `riscv-tests` source tree has **no vector ISA test directory at all**.
> Its `isa/` holds 28 groups (`rv32`/`rv64` × `mi si ua uc ud uf ui um uzba uzbb uzbc uzbs uzfh`,
> plus `rv64mzicbo`, `rv64ssvnapot`, `hypervisor`) and its `isa/Makefile` has no vector target.
> This is not a build flag you are missing: upstream `riscv-tests` (pinned at `0494f95`) simply
> does not carry RVV assembly tests — RVV conformance is delegated to the separate
> `riscv-vector-tests` generator (Appendix C.3).
> Saturn changes the core's ISA rather than adding a RoCC unit, so the suite list *does* regenerate
> per config — there is simply nothing vector-flavoured in `riscv-tests` for it to pick up.
>
> Run `run-asm-tests` to confirm the vector unit did not break the scalar core. For actual RVV
> coverage use the Saturn benchmarks (§1.4b), or `riscv-vector-tests` for conformance
> (Appendix C.3).

---

# Appendix A — Troubleshooting

## Run sits at the bootrom `wfi` and never finishes

Not a hang — the serial TSI load is ~92% of simulated cycles on a Saturn config and produces no
commits. Add `LOADMEM=1` (§1.5). This is the single most common Saturn-specific symptom.

## Illegal instruction / trap in a vector kernel

The config you built does not implement an extension the binary uses. Most often **Zvfh** (FP16),
which the Saturn benchmarks request via `-march=rv64gcv_zfh_zvfh`. Check the flavor's parameters
in Appendix B.4 before assuming a kernel bug. The same applies to running `vec-*` binaries on a
*scalar* config: they trap by design, which is expected and not a core bug.

## Kernel produces wrong results but no trap

This is what cosimulation is for — run the RTL against Spike in lockstep and catch the first
diverging instruction. See Appendix D.

## `%Warning: System has stack size 8192 kb which may be too small`

Verilator prints this on startup when you invoke the simulator directly. ✅ Harmless here — runs
complete normally with it. Raise the limit (`ulimit -s`) only if a large config actually crashes.

## `make run` in `generators/saturn/benchmarks` doesn't use your simulator

That target invokes `spike --isa=rv64gcv_zfh_zvfh`, not the RTL simulator. Build the `.riscv`
binaries there, then run them through `make run-binary` from `sims/verilator` (§1.5).

## `ld: warning: … LOAD segment with RWX permissions`

Harmless; emitted while building the Saturn benchmarks.

---

# Appendix B — Config reference

## B.1 What Saturn is

A parameterized **RVV 1.0 application-profile** vector unit integrated into a Rocket or Shuttle
tile. From `generators/saturn/README.md`:

- `V` — full application-profile V extension
- `Zve64d` — FP64, `ELEN`=64 · `Zvfh` — FP16 · `Zvbb` — basic vector bit manipulation
- `Zvl64/128/256/512/1024` — configurable `VLEN`
- indexed / strided / segmented loads and stores
- virtual memory with precise traps
- full chaining with zero dead-time
- configurable SIMD datapath width (64/128/256/512+)

Microarchitecture manual: <https://saturn-vectors.org/> (also `generators/saturn/docs/*.adoc`).

## B.2 The three key parameters

From `docs/system.adoc:52`:

| Parameter | Meaning |
|---|---|
| **`VLEN`** | architectural vector register length, in bits |
| **`DLEN`** | datapath width of each SIMD pipe — the pipes deliver `DLEN` bits/cycle *regardless of element width* |
| **`MLEN`** | bandwidth of each load and store pipeline, in bits (defaults to `DLEN`) |

The **chime length** — occupancy of one vector instruction — is `VLEN/DLEN` cycles, extended to
`LMUL × VLEN/DLEN` with `LMUL` (`docs/programming.adoc:10`). So `V512D128` takes 4 cycles per
vector instruction; `V128D128` takes 1.

## B.3 Naming scheme

```
<FLAVOR> V<VLEN> D<DLEN> [M<MLEN>] <Core> Config
   │        │       │        │        │
   │        │       │        │        └── Rocket | Shuttle
   │        │       │        └─────────── optional: MLEN when ≠ DLEN
   │        │       └──────────────────── datapath width in bits
   │        └──────────────────────────── architectural VLEN in bits
   └───────────────────────────────────── parameter flavor (B.4)
```

Example: `REFV512D256M128ShuttleConfig` = vREF flavor, VLEN 512, DLEN 256, MLEN 128, on Shuttle.

To roll your own, the config fragments are
(`generators/saturn/src/main/scala/rocket/Configs.scala:12`):

```scala
WithRocketVectorUnit(vLen, dLen, params, cores, useL1DCache, mLen)
WithShuttleVectorUnit(vLen, dLen, params, cores, location, mLen)
```

## B.4 Parameter flavors

Five preset groups (`docs/design-space.adoc:6`, values in
`src/main/scala/common/Parameters.scala:13`):

| Flavor | Intent | Key settings |
|---|---|---|
| **`vMIN`** `minParams` | minimal but still V-profile compliant; performance de-emphasized. Reuses the scalar FPU where possible | bare `VectorParams()` defaults — iterative, element-wise functional units |
| **`vREF`** `refParams` | modest design with SIMD execution units; the reference point | `vlrobEntries=4`, `vl/vs/vxissqEntries=3`, `vatSz=5`, `useSegmentedIMul`, `useSegmentedFPFMA`, `vrfBanking=4`, `issStructure=Shared` |
| **`vDSP`** `dspParams` | splits FP and integer into two execution units — higher throughput on tightly optimized kernels | `refParams` + `issStructure=Split` |
| **`vGEN`** `genParams` | separate issue queues per sequencer — better on *suboptimally scheduled* code | `dspParams` + `vlifqEntries=16`, `vlrobEntries=16`, `vliqEntries=4`, `vsiqEntries=6` |
| **`vDMA`** `dmaParams` | memory-to-memory transfers only; compute de-emphasized, tolerates high memory latency | `vlifqEntries=32`, `vsifqEntries=32`, `vrfBanking=1`, `useIterativeIMul` |

Also defined but with **no** Chipyard config class, so not buildable via `CONFIG=`:
`multiFMAParams`, `multiALUParams`, `multiMACParams` (second sequencer + functional units),
`hwaParams` (Hwacha-like limited sequencer slots).

## B.5 Full config list

### Rocket-attached (10) — all `WithNHugeCores(1)`

| Config | Flavor | VLEN | DLEN | MLEN | Extra |
|---|---|---:|---:|---:|---|
| `MINV64D64RocketConfig` | vMIN | 64 | 64 | — | |
| `MINV128D64RocketConfig` | vMIN | 128 | 64 | — | ← start here |
| `MINV256D64RocketConfig` | vMIN | 256 | 64 | — | |
| `REFV128D128RocketConfig` | vREF | 128 | 128 | — | SBus 128 |
| `REFV256D64RocketConfig` | vREF | 256 | 64 | — | |
| `REFV256D128RocketConfig` | vREF | 256 | 128 | — | SBus 128 |
| `REFV256D128M64RocketConfig` | vREF | 256 | 128 | 64 | |
| `REFV512D128RocketConfig` | vREF | 512 | 128 | — | SBus 128 |
| `REFV512D256RocketConfig` | vREF | 512 | 256 | — | SBus 256 |
| `DMAV256D256RocketConfig` | vDMA | 256 | 256 | — | SBus 256 |

### Shuttle-attached (14) — all `WithNShuttleCores(1)`

| Config | Flavor | VLEN | DLEN | MLEN | Extra |
|---|---|---:|---:|---:|---|
| `GENV128D128ShuttleConfig` | vGEN | 128 | 128 | — | SBus 128, beat 16 |
| `REFV256D64ShuttleConfig` | vREF | 256 | 64 | — | beat 16 |
| `REFV256D128ShuttleConfig` | vREF | 256 | 128 | — | SBus 128, beat 16 |
| `GENV256D128ShuttleConfig` | vGEN | 256 | 128 | — | SBus 128, beat 16 |
| `DSPV256D128ShuttleConfig` | vDSP | 256 | 128 | — | SBus 128, beat 16, **+SGTCM @0x78000000 (8 KB, 16 banks) +TCM** |
| `REFV256D256ShuttleConfig` | vREF | 256 | 256 | — | SBus 256, beat 32 |
| `REFV512D128ShuttleConfig` | vREF | 512 | 128 | — | SBus 128, beat 16 |
| `DSPV512D128ShuttleConfig` | vDSP | 512 | 128 | — | SBus 128, beat 16 |
| `GENV512D128ShuttleConfig` | vGEN | 512 | 128 | — | SBus 128, beat 16 |
| `GENV512D256ShuttleConfig` | vGEN | 512 | 256 | — | SBus 256, beat 32 |
| `REFV512D256ShuttleConfig` | vREF | 512 | 256 | — | SBus 256, beat 32 |
| `REFV512D256M128ShuttleConfig` | vREF | 512 | 256 | 128 | SBus 128, beat 16 |
| `REFV512D512ShuttleConfig` | vREF | 512 | 512 | — | SBus 256, beat 64 |
| `GENV1024D128ShuttleConfig` | vGEN | 1024 | 128 | — | SBus 128, beat 16 |

### Cosimulation (2)

`MINV128D64RocketCosimConfig`, `GENV256D128ShuttleCosimConfig` — see Appendix D.

---

# Appendix C — Test software reference

## C.1 Saturn benchmarks — 33 kernels ✅

`generators/saturn/benchmarks/`, one subdirectory per benchmark. 31 built as C, 2 as C++
(`vec-tasks`, `vec-daxpy`). Kernels use RVV C intrinsics and derive from the Barcelona
Supercomputing Center / Ara vector benchmarks (`benchmarks/common/ara/`); each times its region of
interest with `mcycle`/`minstret` CSR reads.

⚠️ **27 self-check, 6 do not** ✅. These six end in a bare `return 0` and can never report a wrong
answer — only a trap or a hang will fail them: `vec-daxpy`, `vec-div-approx`, `vec-dotprod`,
`vec-fdotprod`, `vec-square-root-approx`, `vec-tasks`. The other 27 return `verify…()` or an error
index.

| Group | Benchmarks |
|---|---|
| **BLAS-like** | `vec-sgemm`, `vec-sgemm-v2`, `vec-sgemm-v3`, `vec-sgemv`, `vec-igemm`, `vec-daxpy`, `vec-dotprod`, `vec-fdotprod`, `vec-spmv` |
| **Convolution** | `vec-conv-3`, `vec-sep-conv-3`, `vec-slide-conv`, `vec-iconv2d`, `vec-fconv2d`, `vec-fconv3d` |
| **Transcendental / math** | `vec-exp`, `vec-log`, `vec-cos`, `vec-softmax`, `vec-div-approx`, `vec-square-root-approx` |
| **Stencil / solver** | `vec-jacobi2d`, `vec-conjugate-gradient`, `vec-pathfinder`, `vec-fft` |
| **Data movement / control** | `vec-transpose-load`, `vec-transpose-store`, `vec-conditional`, `vec-mixed_width_mask`, `vec-strlen`, `vec-dropout`, `vec-roi-align`, `vec-tasks` |

## C.2 The `vec-*` binaries in `riscv-tests`

`$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/` ships four ✅: `vec-daxpy.riscv`,
`vec-memcpy.riscv`, `vec-sgemm.riscv`, `vec-strcmp.riscv`. They are excluded from
`run-bmark-tests` on a scalar config because they need RVV — a Saturn config is where they run.

Unlike the Saturn suite these are **hand-written RVV assembly** (e.g.
`benchmarks/vec-daxpy/vec-daxpy.S`: `vsetvli`, `vle64.v`, `vfmacc.vf`) driven by a C main that ends
in `return verifyDouble(...)`, and they are built `-march=rv64gcv` — **base V only, no Zvfh** ✅.
That makes them the safest first vector test on any config: fewer extension requirements, and all
four genuinely verify their results. Same name, different program — the Saturn `vec-daxpy` is an
intrinsics port and is *not* self-checking.

## C.3 `riscv-vector-tests` — exhaustive conformance ⚠️

`generators/saturn/build-tests.sh` drives the `riscv-vector-tests` submodule (✅ checked out) for
exhaustive RVV conformance coverage.

> ⚠️ **Status changed since the 2026-09-08 pass.** Go is now installed (`go1.22.2`, ✅
> 2026-09-11), so the script is **no longer blocked** — but it is still impractical here:
>
> - hardcoded `-j72` across four VLEN/MODE sweeps (256/128 × machine/virtual) — written for a
>   large CI machine; this box has 16 cores
> - generates tens of thousands of tests, then deletes the crypto/reduction groups (`vaes*`,
>   `vsha*`, `vsm3*`, `vsm4*`, `vclmul*`, `vghsh*`, `vgmul*`, `vfredusum*`, `vfwredusum*`) before
>   building
> - starts with `rm -rf out/`, discarding any previous generation

Use it when you specifically need conformance coverage, and expect a long run.

---

# Appendix D — Cosimulation configs

Two configs pair the RTL against Spike as a golden model, committing instruction-by-instruction:

```scala
class MINV128D64RocketCosimConfig extends Config(
  new chipyard.harness.WithCospike ++          // Spike cosimulation harness
  new chipyard.config.WithTraceIO ++           // commit trace port
  new saturn.rocket.WithRocketVectorUnit(128, 64, VectorParams.minParams) ++
  new freechips.rocketchip.rocket.WithCease(false) ++
  new freechips.rocketchip.rocket.WithDebugROB ++
  new freechips.rocketchip.rocket.WithNHugeCores(1) ++
  new chipyard.config.AbstractConfig)
```

`GENV256D128ShuttleCosimConfig` is the Shuttle equivalent (`WithShuttleDebugROB`).

```bash
make -j$(nproc) CONFIG=MINV128D64RocketCosimConfig
make run-binary CONFIG=MINV128D64RocketCosimConfig LOADMEM=1 BINARY=<vector binary>
```

Cosim catches architectural divergence the self-checking benchmarks would miss, at the cost of
running Spike in lockstep. **Use it when a kernel produces wrong results but no trap.**

---

# Appendix E — Verification log

Executed 2026-09-08:

| # | Command | Result |
|---|---|---|
| 1 | `riscv64-unknown-elf-gcc -march=rv64gcv -mabi=lp64d` | ✅ compiles — toolchain has RVV |
| 2 | `riscv64-unknown-elf-gcc -march=rv64gcv_zfh_zvfh -mabi=lp64d` | ✅ compiles |
| 3 | `make vec-daxpy.riscv` in `generators/saturn/benchmarks` | ✅ built 46904-byte RISC-V ELF |
| 4 | `which go` | ❌ absent — `build-tests.sh` cannot run **(superseded by #9)** |
| 5 | config/parameter tables | ✅ read from `SaturnConfigs.scala`, `Parameters.scala:13`, `docs/design-space.adoc`, `docs/system.adoc:52` |
| 6 | `make -j6 CONFIG=MINV128D64RocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-MINV128D64RocketConfig`, 12.9 MB |
| 7 | `run-binary ... rv64ui-p-add` (no `LOADMEM`) | ⚠️ stalled at bootrom `wfi`, no finish in 110 s |
| 8 | `run-binary ... rv64ui-p-add LOADMEM=1` | ✅ `*** PASSED ***`, **7436 cycles, 23 s** — 13× fewer cycles than the unloaded run |

Added 2026-09-11:

| # | Command | Result |
|---|---|---|
| 9 | `go version` | ✅ **`go1.22.2` now installed** — reverses #4; `build-tests.sh` is no longer Go-blocked (C.3) |
| 10 | vector groups in `MINV128D64RocketConfig`'s generated `.d` | ✅ **none.** 22 asm-test groups, byte-identical set to `RocketConfig`'s 22 — `run-asm-tests` adds **zero** vector coverage on a Saturn config (§1.6) |
| 11 | `ls $RISCV/.../riscv-tests/isa/rv64uv*` | ✅ **0 files** — `riscv-tests` ships no RVV assembly tests at all, which is *why* #10 holds |
| 12 | `ls -d generators/saturn/benchmarks/vec-*/` | ✅ **33** benchmark directories, matching C.1 |
| 13 | `grep march generators/saturn/benchmarks/Makefile` | ✅ `-march=rv64gcv_zfh_zvfh -mabi=lp64d` at `Makefile:64` |
| 14 | `grep -c '^class .*Config' SaturnConfigs.scala` | ✅ **26** classes = 10 Rocket + 14 Shuttle + 2 cosim, matching B.5 |
| 15 | `riscv-tests` source tree `isa/` | ✅ **28 groups, none vector** (`rv32`/`rv64` × mi si ua uc ud uf ui um uzba uzbb uzbc uzbs uzfh, + `rv64mzicbo`, `rv64ssvnapot`, `hypervisor`); `isa/Makefile` has no vector target. Explains #10/#11 — upstream simply ships no RVV assembly tests |
| 16 | `objdump -d` on the four `riscv-tests` `vec-*.riscv` | ✅ all contain RVV instructions (5–52 each); kernels are hand-written `.S`, built `-march=rv64gcv` (base V, no Zvfh) per `benchmarks/Makefile:53` |
| 17 | return paths of all 33 Saturn benchmark mains | ✅ **27 self-check** (`return verify…()` / error index); **6 always `return 0`** — `vec-daxpy`, `vec-div-approx`, `vec-dotprod`, `vec-fdotprod`, `vec-square-root-approx`, `vec-tasks` (§1.4, C.1) |
| 18 | all four `riscv-tests` `vec-*.riscv` on `MINV128D64RocketConfig`, `LOADMEM=1` | ✅ **4/4 pass** — `vec-strcmp` 1 135 cyc/2 s, `vec-daxpy` 3 907/9 s, `vec-memcpy` 5 084/14 s, `vec-sgemm` 73 111/43 s. Whole set ≈70 s (§1.5) |
| 19 | three Saturn benchmarks, same config | ✅ **3/3 pass** — `vec-exp` 111 s, `vec-daxpy` 186 336 ROI cyc/259 s, `vec-sgemm` 536 530 cyc/307 s. Saturn's `vec-sgemm` is **7.3×** the cycles of the riscv-tests kernel of the same name, confirming they are different programs |
| 20 | bare `./simulator-…-MINV128D64RocketConfig <elf>` on `vec-strcmp.riscv`, no plusargs | ✅ passes, rc=0 — but **392 186** harness cycles / **46 s** vs **23 396** / **2 s** with `+loadmem +dramsim`. 94% of the bare run is the serial TSI load (§1.5) |
| 21 | `minstret` across both forms | ✅ **84 in both** — same work executed; only binary delivery and the memory model differ |
| 22 | `mcycle` across both forms | ⚠️ **960** bare vs **1 135** with `+dramsim` — the DRAMSim2 model shifts the target's own counter too, so only `+dramsim` numbers compare with `make` |
| 23 | `ls generators/saturn/riscv-vector-tests/` | ✅ submodule checked out; `build-tests.sh` still hardcodes `-j72` across 4 sweeps and opens with `rm -rf out/` |
