# Saturn — Build & Test on Verilator

Building and testing the **Saturn** RVV 1.0 vector unit in Chipyard.

> The simulation machinery (run modes, artifact layout, Makefile variables, waveforms,
> troubleshooting) is **identical to Rocket** and documented once in
> [`Rocket_Build_Test.md`](Rocket_Build_Test.md). This note covers only what is
> Saturn-specific. Read the Rocket note first.

**Verified against** `/home/vscode/chipyard`, Saturn submodule `dfe75de` (`master`), 2026-09-08.

> **Legend (verification status)** — ✅ command actually run here · ❌ does not work in this
> environment · ⏳ not run · ⚠️ caution. Capability tables spell out "supported / unsupported"
> in words rather than reusing these glyphs.

---

## Contents

- [1. What Saturn is](#1-what-saturn-is)
- [2. Config naming scheme](#2-config-naming-scheme)
- [3. Parameter flavors](#3-parameter-flavors)
- [4. Full config list](#4-full-config-list)
- [5. Build the simulator](#5-build-the-simulator)
- [6. Build the test software](#6-build-the-test-software)
- [7. Running tests](#7-running-tests)
- [8. Cosimulation configs](#8-cosimulation-configs)
- [9. Verification log](#9-verification-log)

---

## 1. What Saturn is

A parameterized **RVV 1.0 application-profile** vector unit, integrated into either a Rocket
or a Shuttle tile (not a RoCC accelerator — it attaches as the core's vector unit). From
`generators/saturn/README.md`, it supports:

- `V` — full application-profile V extension
- `Zve64d` — FP64, `ELEN`=64 · `Zvfh` — FP16 · `Zvbb` — basic vector bit manipulation
- `Zvl64/128/256/512/1024` — configurable `VLEN`
- indexed / strided / segmented loads and stores
- virtual memory with precise traps
- full chaining with zero dead-time
- configurable SIMD datapath width (64/128/256/512+)

Microarchitecture manual: <https://saturn-vectors.org/> (also `generators/saturn/docs/*.adoc`).

### The three key parameters

From `docs/system.adoc:52`:

| Parameter | Meaning |
|---|---|
| **`VLEN`** | architectural vector register length, in bits |
| **`DLEN`** | datapath width of each SIMD pipe — the pipes deliver `DLEN` bits/cycle *regardless of element width* |
| **`MLEN`** | bandwidth of each load and store pipeline, in bits (defaults to `DLEN`) |

The **chime length** — occupancy of one vector instruction — is `VLEN/DLEN` cycles, extended
to `LMUL × VLEN/DLEN` with `LMUL` (`docs/programming.adoc:10`). So a `V512D128` config takes
4 cycles per vector instruction; `V128D128` takes 1.

## 2. Config naming scheme

```
<FLAVOR> V<VLEN> D<DLEN> [M<MLEN>] <Core> Config
   │        │       │        │        │
   │        │       │        │        └── Rocket | Shuttle
   │        │       │        └─────────── optional: MLEN when ≠ DLEN
   │        │       └──────────────────── datapath width in bits
   │        └──────────────────────────── architectural VLEN in bits
   └───────────────────────────────────── parameter flavor (§3)
```

Example: `REFV512D256M128ShuttleConfig` = vREF flavor, VLEN 512, DLEN 256, MLEN 128, on Shuttle.

Defined in `generators/saturn/chipyard/SaturnConfigs.scala`, `package chipyard` — so only
`CONFIG=` changes. Auto-discovered because `generators/saturn/.git` exists (`build.sbt:245`).

Config fragment signature (`generators/saturn/src/main/scala/rocket/Configs.scala:12`):

```scala
WithRocketVectorUnit(vLen, dLen, params, cores, useL1DCache, mLen)
WithShuttleVectorUnit(vLen, dLen, params, cores, location, mLen)
```

## 3. Parameter flavors

Five preset groups (`docs/design-space.adoc:6`, values in
`src/main/scala/common/Parameters.scala:13`):

| Flavor | Intent | Key settings |
|---|---|---|
| **`vMIN`** `minParams` | minimal but still V-profile compliant; performance de-emphasized. Reuses the scalar FPU where possible | bare `VectorParams()` defaults — iterative, element-wise functional units |
| **`vREF`** `refParams` | modest design with SIMD execution units; the reference point | `vlrobEntries=4`, `vl/vs/vxissqEntries=3`, `vatSz=5`, `useSegmentedIMul`, `useSegmentedFPFMA`, `vrfBanking=4`, `issStructure=Shared` |
| **`vDSP`** `dspParams` | splits FP and integer into two execution units — higher throughput on tightly optimized kernels | `refParams` + `issStructure=Split` |
| **`vGEN`** `genParams` | separate issue queues per sequencer — better on *suboptimally scheduled* code | `dspParams` + `vlifqEntries=16`, `vlrobEntries=16`, `vliqEntries=4`, `vsiqEntries=6` |
| **`vDMA`** `dmaParams` | memory-to-memory transfers only; compute de-emphasized, tolerates high memory latency | `vlifqEntries=32`, `vsifqEntries=32`, `vrfBanking=1`, `useIterativeIMul` |

Also defined but with no Chipyard config class: `multiFMAParams`, `multiALUParams`,
`multiMACParams` (second sequencer + functional units), `hwaParams` (Hwacha-like limited
sequencer slots).

## 4. Full config list

### Rocket-attached (10)

| Config | Flavor | VLEN | DLEN | MLEN | Extra |
|---|---|---:|---:|---:|---|
| `MINV64D64RocketConfig` | vMIN | 64 | 64 | — | |
| `MINV128D64RocketConfig` | vMIN | 128 | 64 | — | |
| `MINV256D64RocketConfig` | vMIN | 256 | 64 | — | |
| `REFV128D128RocketConfig` | vREF | 128 | 128 | — | SBus 128 |
| `REFV256D64RocketConfig` | vREF | 256 | 64 | — | |
| `REFV256D128RocketConfig` | vREF | 256 | 128 | — | SBus 128 |
| `REFV256D128M64RocketConfig` | vREF | 256 | 128 | 64 | |
| `REFV512D128RocketConfig` | vREF | 512 | 128 | — | SBus 128 |
| `REFV512D256RocketConfig` | vREF | 512 | 256 | — | SBus 256 |
| `DMAV256D256RocketConfig` | vDMA | 256 | 256 | — | SBus 256 |

All use `WithNHugeCores(1)`.

### Shuttle-attached (14)

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

All use `WithNShuttleCores(1)`.

### Cosimulation (2) — see §8

`MINV128D64RocketCosimConfig`, `GENV256D128ShuttleCosimConfig`

## 5. Build the simulator

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator

make -j$(nproc) CONFIG=MINV128D64RocketConfig
```

✅ Verified — produces `simulator-chipyard.harness-MINV128D64RocketConfig` (12.9 MB).

Start with a **`MIN`** or low-`DLEN` config. Elaboration and simulation cost scale steeply
with `DLEN` (datapath width) and, to a lesser extent, `VLEN`. `REFV512D512ShuttleConfig` is
at the far end of the design space and will be slow to build and slow to run.

> There is **no** `generators/saturn/chipyard.mk`, so Saturn contributes **no** custom make
> targets. Everything runs through the standard `run-binary` surface with `BINARY=`.

## 6. Build the test software

### 6.1 Saturn benchmarks — the practical option ✅

```bash
cd /home/vscode/chipyard/generators/saturn/benchmarks
make                          # all benchmarks
make vec-daxpy.riscv          # ✅ verified — one benchmark
```

Compiles with `-march=rv64gcv_zfh_zvfh -mabi=lp64d`. ✅ The conda toolchain
(`riscv64-unknown-elf-gcc 13.2.0`) supports that `-march` string.

**33 benchmarks** — 31 C plus 2 C++ (`vec-tasks`, `vec-daxpy`):

| Group | Benchmarks |
|---|---|
| **BLAS-like** | `vec-sgemm`, `vec-sgemm-v2`, `vec-sgemm-v3`, `vec-sgemv`, `vec-igemm`, `vec-daxpy`, `vec-dotprod`, `vec-fdotprod`, `vec-spmv` |
| **Convolution** | `vec-conv-3`, `vec-sep-conv-3`, `vec-slide-conv`, `vec-iconv2d`, `vec-fconv2d`, `vec-fconv3d` |
| **Transcendental / math** | `vec-exp`, `vec-log`, `vec-cos`, `vec-softmax`, `vec-div-approx`, `vec-square-root-approx` |
| **Stencil / solver** | `vec-jacobi2d`, `vec-conjugate-gradient`, `vec-pathfinder`, `vec-fft` |
| **Data movement / control** | `vec-transpose-load`, `vec-transpose-store`, `vec-conditional`, `vec-mixed_width_mask`, `vec-strlen`, `vec-dropout`, `vec-roi-align`, `vec-tasks` |

> ⚠️ These require **Zvfh** (FP16 vector). Saturn advertises `Zvfh` support, but confirm the
> flavor you picked enables it before blaming a kernel for a trap.

Two harmless build-time notes: `ld: warning: ... LOAD segment with RWX permissions`, and the
Makefile's `run` target invokes `spike --isa=rv64gcv_zfh_zvfh` rather than the RTL simulator.

### 6.2 `riscv-vector-tests` — ❌ blocked here

`generators/saturn/build-tests.sh` drives the `riscv-vector-tests` submodule for exhaustive
RVV conformance coverage. **It will not run in this environment:**

- it needs **Go**, which is not on `PATH`
- it is hardcoded to `-j72` across four VLEN/MODE sweeps (256/128 × machine/virtual) — written
  for a large CI machine
- it generates tens of thousands of tests and then deletes the crypto/reduction groups
  (`vaes*`, `vsha*`, `vsm3*`, `vsm4*`, `vclmul*`, `vghsh*`, `vgmul*`, `vfredusum*`,
  `vfwredusum*`) before building

Use it only if you specifically need conformance coverage, on a machine with Go and many cores.

### 6.3 The `vec-*` binaries in riscv-tests

`$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/` also ships `vec-daxpy.riscv`,
`vec-memcpy.riscv`, `vec-sgemm.riscv`, `vec-strcmp.riscv`. These are **not** in the default
`run-bmark-tests` list for a scalar config because they need RVV — a Saturn config is exactly
where they belong.

## 7. Running tests

> ⚠️ **Always pass `LOADMEM=1`.** ✅ Measured here on this exact config with `rv64ui-p-add`:
>
> | | Cycles | Wall |
> |---|---:|---|
> | default (serial TSI load) | 99 666 | did not finish in 110 s |
> | `LOADMEM=1` | **7 436** | **23 s** |
>
> The binary load is **~92% of all simulated cycles**. Without `LOADMEM=1` the run sits at the
> bootrom `wfi` and looks like a hang.

```bash
cd /home/vscode/chipyard/sims/verilator
SB=../../generators/saturn/benchmarks
BM=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks
ISA=$RISCV/riscv64-unknown-elf/share/riscv-tests/isa

make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 BINARY=$ISA/rv64ui-p-add   # ✅ scalar sanity
make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 BINARY=$SB/vec-daxpy.riscv
make run-binary CONFIG=MINV128D64RocketConfig LOADMEM=1 BINARY=$BM/vec-memcpy.riscv

# vector ISA tests from riscv-tests (scalar suites still apply too)
make -j$(nproc) CONFIG=MINV128D64RocketConfig run-asm-tests-fast
```

All the usual modes apply — `run-binary-fast`, `run-binary-debug`,
`EXTRA_SIM_FLAGS="+max-cycles=..."`. Artifacts land in
`output/chipyard.harness.TestHarness.MINV128D64RocketConfig/` exactly as in the Rocket note.

Start with the scalar sanity test above: it proves the simulator, the boot path and the
artifact plumbing all work before you introduce vector kernels as a variable.

Because Saturn changes the *core's* ISA rather than adding a RoCC unit, `run-asm-tests`
regenerates its list for the config — vector tests appear automatically if the generated `.d`
includes them.

## 8. Cosimulation configs

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
make run-binary CONFIG=MINV128D64RocketCosimConfig BINARY=<vector binary>
```

Cosim catches architectural divergence the self-checking benchmarks would miss, at the cost of
running Spike in lockstep. Use it when a kernel produces wrong results but no trap.

## 9. Verification log

| # | Command | Result |
|---|---|---|
| 1 | `riscv64-unknown-elf-gcc -march=rv64gcv -mabi=lp64d` | ✅ compiles — toolchain has RVV |
| 2 | `riscv64-unknown-elf-gcc -march=rv64gcv_zfh_zvfh -mabi=lp64d` | ✅ compiles |
| 3 | `make vec-daxpy.riscv` in `generators/saturn/benchmarks` | ✅ built 46904-byte RISC-V ELF |
| 4 | `which go` | ❌ absent — `build-tests.sh` cannot run |
| 5 | config/parameter tables | ✅ read from `SaturnConfigs.scala`, `Parameters.scala:13`, `docs/design-space.adoc`, `docs/system.adoc:52` |
| 6 | `make -j6 CONFIG=MINV128D64RocketConfig` | ✅ exit 0 — `simulator-chipyard.harness-MINV128D64RocketConfig`, 12.9 MB |
| 7 | `run-binary ... rv64ui-p-add` (no `LOADMEM`) | ⚠️ stalled at bootrom `wfi`, no finish in 110 s |
| 8 | `run-binary ... rv64ui-p-add LOADMEM=1` | ✅ `*** PASSED ***`, **7436 cycles, 23 s** — 13× fewer cycles than the unloaded run |
