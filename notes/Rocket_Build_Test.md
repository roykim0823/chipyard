# Rocket — Build & Test on Verilator

Build a baseline Rocket SoC in Chipyard and run the RISC-V test suites against it.

**[§1](#1--build--run) is the whole tutorial** — set up, build, run one test, run a suite, read
the verdict. §2 covers what the results mean, plus waveforms and simulation speed. The reference
material — every target, every variable, every plusarg — lives in the appendices.

**Companion documents**
- [`Gemmini_Build_Test.md`](Gemmini_Build_Test.md) — Gemmini systolic-array accelerator
- [`Saturn_Build_Test.md`](Saturn_Build_Test.md) — Saturn RVV vector unit
- [`tutorial_riscv_tests_isa.md`](tutorial_riscv_tests_isa.md),
  [`tutorial_riscv_tests_bm.md`](tutorial_riscv_tests_bm.md) — the original short tutorials this
  document grew out of. Still on disk, but superseded here; they carry 10 factual errors listed
  in [Appendix G](#appendix-g--corrections-applied).

This document is the canonical reference for the simulation machinery (run modes, artifacts,
Makefile variables, troubleshooting); the accelerator notes build on it.

**Verified against** `/home/vscode/chipyard` (branch `dev_dn`), Verilator 5.022 — first pass
2026-09-08, re-verified 2026-09-10.

> **Legend (verification status)** — ✅ command actually run here · ❌ does not work in this
> environment · ⏳ not run · ⚠️ caution, including corrections to the source tutorials
> ([Appendix G](#appendix-g--corrections-applied))
>
> **Feature matrices** (§2.2, Appendix D.1) use a different pair — **●** = produced / applicable,
> **–** = not produced. Those say nothing about whether anything is broken.

---

## Contents

**Tutorial**
- [1 — Build & run](#1--build--run)
- [2 — Results, waveforms & performance](#2--results-waveforms--performance)

**Reference**
- [Appendix A — Troubleshooting](#appendix-a--troubleshooting)
- [Appendix B — Build reference](#appendix-b--build-reference)
- [Appendix C — Test binaries reference](#appendix-c--test-binaries-reference)
- [Appendix D — Run reference: targets & variables](#appendix-d--run-reference-targets--variables)
- [Appendix E — Driving the simulator binary directly](#appendix-e--driving-the-simulator-binary-directly)
- [Appendix F — Verification log](#appendix-f--verification-log)
- [Appendix G — Corrections applied](#appendix-g--corrections-applied)
- [Appendix H — Cheat sheet](#appendix-h--cheat-sheet)

---

# 1 — Build & run

Minimum viable path for `RocketConfig`. Every command runs from `sims/verilator/`.
Substitute any other `CONFIG=` and the flow is identical.

## 1.1 Set up the shell

Once per shell:

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator
```

That sets `RISCV` to the toolchain prefix, which is where the prebuilt test binaries live:

```bash
ISA=$RISCV/riscv64-unknown-elf/share/riscv-tests/isa           # ISA assembly tests
BM=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks     # benchmarks
OUT=output/chipyard.harness.TestHarness.RocketConfig           # where results land
```

The tests are already compiled — they were built by `build-setup.sh`, there is no separate
build step (Appendix C).

## 1.2 Build the simulator

```bash
make CONFIG=RocketConfig          # ✅ ~11 MB executable; `CONFIG` defaults to RocketConfig anyway
```

This elaborates the Chisel design, emits Verilog, and Verilates it into **one standalone
executable** in this directory:

```
simulator-chipyard.harness-RocketConfig
```

Add `make CONFIG=RocketConfig debug` **only when you need waveforms** — a separate 15 MB
executable with a `-debug` suffix, built into its own tree, and far slower to run.

> ⚠️ Elaboration needs **~6.5 GB RAM**. Short of that the build dies with
> `make: *** [firrtl_temp] Error 137` — that is an OOM kill, not a code error.

## 1.3 Run one test

Two ways, both useful.

**(a) Through `make`** — the normal way. It rebuilds the simulator if stale, adds the standard
plusargs, and files the output under `$OUT/`:

```bash
make run-binary CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add      # ✅
```

**(b) By running the simulator executable yourself** — it is an ordinary program that takes an
ELF as its argument:

```bash
./simulator-chipyard.harness-RocketConfig $ISA/rv64ui-p-simple    # ✅
```

which prints:

```
[UART] UART0 is here (stdin/stdout).
- /home/vscode/chipyard/sims/verilator/generated-src/chipyard.harness.TestHarness.RocketConfig/gen-collateral/TestDriver.v:158: Verilog $finish
```

⚠️ **`Verilog $finish` is not a pass signal** — it prints on failure too, and this bare
invocation prints no verdict at all. Add `+verbose` to get one (it goes to **stderr**):

```bash
./simulator-chipyard.harness-RocketConfig +permissive +verbose +permissive-off \
  $ISA/rv64ui-p-simple 2>&1 >/dev/null | spike-dasm | tail -1
# ✅ → *** PASSED *** Completed after                30696 simulation cycles
```

Use **(a)** by default. Reach for **(b)** when you want a debugger or profiler around a single
run — Appendix D.1 compares them, Appendix E is the full manual.

## 1.4 The same test, three modes

`run-binary` has two siblings. They trade logging detail against speed:

| Mode | Command | Simulator | Trace | Waveform |
|---|---|---|:---:|:---:|
| **default** | `make run-binary` | regular | ● `.out` commit trace | – |
| **fast** | `make run-binary-fast` | regular | – | – |
| **debug** | `make run-binary-debug` | `-debug` | ● `.out` | ● `.vcd`/`.fst` |

```bash
make run-binary       CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add     # ✅ default
make run-binary-fast  CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add     # ✅ fast
make run-binary-debug CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add     # ✅ needs `make debug` first

make run-binary       CONFIG=RocketConfig BINARY=$BM/dhrystone.riscv   # benchmarks: same surface
make run-binary       CONFIG=RocketConfig BINARY=../../tests/hello.riscv   # custom test (Appendix C.4)
```

Use **fast** for bulk and regression runs, **default** when you want to read the trace, **debug**
only when you need to see signals — it is slow and the waveforms are enormous (§2.2).

The same three modes exist when you drive the simulator yourself; there they are just *which*
executable you invoke and whether you pass `+verbose` and `+vcdfile=` (Appendix E.3). Other
knobs — `LOADMEM`, `EXTRA_SIM_FLAGS`, `USE_FST` — are in Appendix D.2.

## 1.5 Run a whole suite

Suite targets come from a makefrag generated at elaboration time, so **plain `make` must have
succeeded first**. They run serially by default; `-j` is a large and safe win:

```bash
make -j$(nproc) CONFIG=RocketConfig run-asm-tests-fast      # ✅ all ISA tests   (~1.5 h, 335 tests)
make -j$(nproc) CONFIG=RocketConfig run-bmark-tests-fast    # ✅ all benchmarks  (12 tests)
make -j$(nproc) CONFIG=RocketConfig run-fast                # ✅ both of the above
```

In a hurry, the 25-test smoke subset is the "did I break the core?" check:

```bash
make -j$(nproc) CONFIG=RocketConfig run-regression-tests-fast   # ✅
```

The main targets:

| Target | Runs | Artifacts |
|---|---|---|
| `run-asm-tests-fast` | every supported ISA test | `.log` + `.run` marker |
| `run-asm-tests` | same, with commit traces | `.out` + `.log` |
| `run-bmark-tests-fast` | every supported benchmark | `.log` + `.run` |
| `run-bmark-tests` | same, with commit traces | `.out` + `.log` |
| `run-regression-tests-fast` | 25-test smoke subset | `.log` + `.run` |
| `run-fast` | `run-asm-tests-fast` + `run-bmark-tests-fast` | both |

Subsets (`-p-` only, `-v-` only, one extension group), waveform suites and the targets that
don't work here are in Appendix D.3.

**Two things that make sweeps bearable:**

- The list is generated **per config** — only extensions the target actually implements are
  included, so unsupported tests never run and never produce false failures (Appendix C.5).
- Passed tests leave a `.run` marker and are **skipped on re-run**, so an interrupted sweep
  resumes where it stopped. To force a full re-run: `rm -f $OUT/*.run`.

✅ Measured here on 16 cores: the full ISA suite is **335 tests, 96 min 47 s wall, ~8.3×
speedup** from `-j16`, zero failures. The 157 `-v-` (virtual memory) tests are the slow tail, and
the sweep saturates the CPU — don't run anything you care about timing alongside it.

> ⚠️ There is **no `run-tests` target**, despite what the older tutorials say. Use `run-fast`.

## 1.6 Did it pass?

```bash
ls $OUT/*.run | wc -l              # tests passed so far (-fast runs leave a marker per pass)
grep -l 'FAILED' $OUT/*.out        # which tests failed — empty output means none
tail -1 $OUT/rv64ui-p-add.out      # the verdict for one test
```

The `.run` marker is the most reliable signal: the rule only `touch`es it on a zero exit.
`make` itself also returns non-zero if any test failed. Full detail in §2.1.

---

# 2 — Results, waveforms & performance

## 2.1 Determining pass/fail

Three signals, in order of reliability:

1. **`.run` marker exists** — the fast suite rule is `(... | tee $<.log) && touch $@`
   (`common.mk:458`), so the marker appears *only* on a zero exit. Definitive for `*-fast` runs.
2. **`*** PASSED ***` in `.out`** ⚠️ — **not** in `.log`. The harness writes the verdict to
   **stderr**, piped through `spike-dasm` into `.out`. Failures read `*** FAILED *** (tohost = N)`.
   Only modes that pass `+verbose` (default and debug `run-binary*`) print the PASSED line at all —
   see the ⚠️ under §2.2. For everything else, **absence of `FAILED`** is the pass signal.
3. **Exit status** — `make` returns 0 on pass, 2 on failure.

`Verilog $finish` appears in `.log` on **both** pass and fail — it means the harness terminated,
not that the test passed. Never use it as a pass signal.

## 2.2 Artifact matrix

```
sims/verilator/output/chipyard.harness.TestHarness.<CONFIG>/
```

**Single-binary targets** (`run-binary*` with an absolute `BINARY=`) ●:

| File | default | fast | debug | Contents |
|---|:---:|:---:|:---:|---|
| `<name>.log` | ● | ● | ● | stdout — UART output, `Verilog $finish` |
| `<name>.out` | ● | – | ● | stderr — commit trace + `*** PASSED/FAILED ***` |
| `<name>.dump` | – | – | ● | `objdump -D -S` disassembly |
| `<name>.vcd` | – | – | ● | waveform (`.fst` if `USE_FST=1`) |
| `<name>` symlink | – | – | – | ⚠️ **not** created |
| `<name>.run` | – | – | – | ⚠️ **not** created |

**Suite targets** (`run-asm-tests*`, `run-bmark-tests*`) ●:

| File | default | `-fast` | Contents |
|---|:---:|:---:|---|
| `<name>` | ● | ● | symlink into `$RISCV/.../isa/` or `.../benchmarks/` |
| `<name>.log` | ● | ● | stdout |
| `<name>.out` | ● | – | stderr — see the ⚠️ below |
| `<name>.run` | – | ● | empty pass marker |

> ⚠️ **Suite `.out` files are not the same as `run-binary` `.out` files.** ✅ Confirmed with
> `make -n`: the suite rule `$(output_dir)/%.out` (`common.mk:461`) does **not** pass
> `$(VERBOSE_FLAGS)`, while `run-binary` does. Since `TestDriver.v:152` prints `*** PASSED ***`
> only under `+verbose`, a suite `.out` carries **no commit trace and no PASSED line**. Only
> `*** FAILED ***` appears, printed unconditionally (`TestDriver.v:145`) — which is why the suite
> summary one-liner works by grepping for `FAILED` and passing when it finds none. For a trace,
> re-run the one test with `make run-binary BINARY=…`.

### What `<name>` is ⚠️

`run-binary` names artifacts after the binary with its **last extension stripped** — `get_out_name`
at `variables.mk:304` runs the path through GNU make's `$(basename)`. Suite targets instead use the
test name verbatim. For extensionless ISA tests the two agree; for benchmarks they do not ✅:

| Run | Artifacts |
|---|---|
| `make run-binary BINARY=$BM/memcpy.riscv` | `memcpy.log`, `memcpy.out` |
| `make run-bmark-tests` (suite) | `median.riscv`, `median.riscv.log`, `median.riscv.run` |

So after a single-binary benchmark run `tail -1 $OUT/memcpy.riscv.out` finds nothing — the file is
`memcpy.out`.

### What one run leaves behind ✅

Measured with `run-binary` on a benchmark no suite had touched, so the directory started empty:

```bash
make run-binary CONFIG=RocketConfig BINARY=$BM/memcpy.riscv
```

```
output/chipyard.harness.TestHarness.RocketConfig/
├── memcpy.log      212 B     # stdout — UART banner, mcycle/minstret, Verilog $finish
└── memcpy.out      4.9 MB    # stderr — commit trace + *** PASSED *** (1332136 cycles)
```

Two files, nothing else: no symlink, no `.run` marker, no `.dump`, no `.vcd` — exactly what the
first table predicts. `memcpy.log` in full:

```
[UART] UART0 is here (stdin/stdout).
mcycle = 22309
minstret = 5525
- .../gen-collateral/TestDriver.v:158: Verilog $finish
```

The `mcycle`/`minstret` pair is the benchmark reporting its own cycle and retired-instruction
counts over UART; ISA tests print neither.

The split exists because the paths use different rules: suite targets go through
`$(output_dir)/%.out` and `$(output_dir)/%.run` (`common.mk:458-462`), which create the symlink
and marker; `run-binary` goes through the generic `%.run`/`%.run.fast`/`%.run.debug` rules
(`common.mk:378-419`), which do neither.

> **Size warning:** a single `rv64ui-p-add` debug run produced a **220 MB `.vcd`** — and that is
> one of the smallest tests. Confirm a pass with `fast` first, then trace only what you need;
> `+dump-start=<cycle>` (supported — `TestDriver.v:31`) limits the waveform to a window.
>
> **Benchmarks are far worse** — they run for millions of cycles, so a default-mode `.out` trace
> reaches hundreds of MB and a debug `.vcd` runs into the GB range. ✅ `memcpy.riscv`, one of the
> *shorter* benchmarks at 1.33 M cycles, already produces a 4.9 MB `.out`.

## 2.3 Reading the commit trace

```
C0:  <cycle> [<valid>] pc=[...] W[<rd>=<val>][<wen>] R[<rs1>=<val>] R[<rs2>=<val>] inst=[<hex>] <disassembly>
```

Real excerpt ✅ from an actual `rv64ui-p-add` run here:

```
C0:  19 [1] pc=[0000000000010000] W[r10=0000000000010000][1] R[r 0=...] R[r 0=...] inst=[00000517] auipc   a0, 0x0
C0:  20 [1] pc=[0000000000010004] W[r10=0000000000010040][1] R[r10=0000000000010000] R[r 0=...] inst=[04050513] addi    a0, a0, 64
C0:  21 [1] pc=[0000000000010008] W[r 0=0000000000000000][1] R[r10=0000000000010040] R[r 0=...] inst=[30551073] csrw    mtvec, a0
```

- `C0:` — hart that committed. `C0` = core 0.
- `19` — cycle when the line was emitted. Gaps (19→20→21→26) are stalls.
- `[1]` — **valid** bit. `[1]` = an instruction retired; `[0]` = bubble, rest of line is stale.
- `pc=[...]` — ⚠️ execution begins at **`0x10000`** (the bootrom), which then jumps to `0x80000000`.
- `W[r10=...][1]` — write-back: destination, value, write-enable. Writes to `r 0` carry `[0]`.
- `R[...] R[...]` — the two source-register reads with values.
- `inst=[...]` — raw 32-bit encoding; then the disassembly, expanded by `spike-dasm` from the
  `DASM(...)` token the RTL emits.

The trace begins after DRAMSim2 model-loading banners (also stderr), so filter it:

```bash
grep -a '^C0:' $OUT/rv64ui-p-add.out | less
```

## 2.4 Waveforms

```bash
make CONFIG=RocketConfig debug                                  # build once
make run-binary-debug CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add
make run-binary-debug CONFIG=RocketConfig BINARY=... USE_FST=1  # .fst instead of .vcd
make run-binary-debug CONFIG=RocketConfig BINARY=... EXTRA_SIM_FLAGS="+dump-start=100000"

gtkwave $OUT/rv64ui-p-add.vcd                                   # or: surfer
```

| Simulator | Format | Viewer |
|---|---|---|
| Verilator | VCD (or FST with `USE_FST=1`) | Surfer, GTKWave |
| VCS | FSDB | Verdi (Synopsys license) |

For whole-suite waveforms under Verilator use the **`-fst`** targets — the `-debug` suite
targets emit `.vpd` via `vcd2vpd`, a VCS/Verdi utility **not installed here** (Appendix D.3).

## 2.5 Speeding up simulation

```bash
make run-binary BINARY=test.riscv LOADMEM=1    # skip the slow serial TSI boot
make VERILATOR_THREADS=8                       # parallel simulation threads
make NUMACTL=1                                 # pin to one socket
```

Upstream recommends using the last two one at a time.

### `LOADMEM=1` matters far more than the docs suggest ✅

The serial TSI boot dominates short tests. Measured here on `MINV128D64RocketConfig` with
the *same* binary (`rv64ui-p-add`):

| | Cycles | Wall |
|---|---:|---|
| default (serial TSI load) | 99 666 | did not finish in 110 s |
| `LOADMEM=1` | **7 436** | **23 s** |

**~92% of the cycles were the binary load, not the test.** On a plain `RocketConfig` the
simulator is fast enough to hide this, but on any larger config (accelerators especially) it
is the difference between a test finishing and appearing to hang. Treat `LOADMEM=1` as the
default for single-binary runs unless you are deliberately exercising the TSI bringup path.

**~2× by dropping TileLink monitors** — pure verification logic:

```scala
class FastRTLSimRocketConfig extends Config(
  new freechips.rocketchip.subsystem.WithoutTLMonitors ++
  new chipyard.RocketConfig)
```

`WithoutTLMonitors` is at `generators/rocket-chip/src/main/scala/subsystem/Configs.scala:239`.

---

# Appendix A — Troubleshooting

## `make: *** [firrtl_temp] Error 137`
OOM during elaboration. RocketConfig needs ~6.5 GB. Free memory or raise the container limit.

## `No rule to make target 'run-asm-tests'`
Plain `make` has not succeeded yet. Suite targets live in the generated
`generated-src/<long_name>/<long_name>.d`, which only exists after elaboration.

## `Binary output/.../<test> not found`
You passed a **relative** path to an `$(output_dir)/%.run` style target. `output_dir` is
absolute, so a relative path falls through to the generic `%.run` rule which demands an existing
file. Use an absolute path, or the `run-binary`/suite targets.

## Simulation appears to hang at `wfi` (pc `0x10034`)

The last trace line is `wfi` in the bootrom and nothing follows. Usually **not** a hang — the
core is idle while fesvr loads the binary over serial TSI, which produces no commits, and
`spike-dasm`'s block-buffered stdout has not flushed to `.out` yet.

Confirm the simulator is alive (`ps aux | grep simulator-`; look for non-zero CPU), then
re-run with `LOADMEM=1` to skip the load entirely — see §2.5.

## `undefined reference to __isoc23_sscanf` / `__isoc23_strtol` at link time

Stale objects compiled against **system** glibc ≥ 2.38 headers, linked against the conda
sysroot's older libc.

- glibc ≥ 2.38 redirects `sscanf`/`strtol`/`strtoull` to `__isoc23_*`.
- This env's conda sysroot is **glibc 2.34** → defines none of those; the Ubuntu system libc
  (2.39) defines all 32.
- Objects built before `sysroot_linux-64` was installed picked up `/usr/include`.

```bash
cd generated-src/<long_name>/<long_name>
for f in *.o; do nm -u "$f" 2>/dev/null | grep -q isoc23 && echo "$f"; done
rm -f verilated.o verilated_vpi.o verilated_dpi.o verilated_threads.o \
      verilated_timing.o SimJTAG.o SimUART.o uart.o remote_bitbang.o \
      verilated*.d SimJTAG.d SimUART.d uart.d remote_bitbang.d
cd ../../.. && make
```

The Verilator **model** archive (`VTestDriver__ALL.a`) is normally clean, so full
re-elaboration is usually unnecessary. `make clean` is the sledgehammer.

## `build-setup.sh` step 6: "untracked working tree files would be overwritten"

A half-finished submodule checkout under
`sims/firesim/platforms/f2/aws-fpga-firesim-f2/hdk/common/shell_stable/hlx`. Confirm the
stranded files match the target commit, then complete the checkout:

```bash
cd sims/firesim/platforms/f2/aws-fpga-firesim-f2/hdk/common/shell_stable/hlx
git checkout -f <target-sha>
```

If `git-lfs` is absent, that repo's `.gitattributes` also breaks plain `git status`. Neutralize
it repo-locally (safe only when the target commit has no LFS-tracked files):

```bash
git config --local filter.lfs.smudge "cat"
git config --local filter.lfs.clean  "cat"
git config --local filter.lfs.process ""
git config --local filter.lfs.required false
```

---

# Appendix B — Build reference

## B.1 Choice of simulator

| Simulator | License | Directory | Executable |
|---|---|---|---|
| **Verilator** | open source (LGPL) | `sims/verilator` | `simulator-chipyard.harness-<CONFIG>` |
| **Synopsys VCS** | commercial | `sims/vcs` | `simv-chipyard.harness-<CONFIG>` |

VCS compiles faster; `vcs` must be on `PATH`. This document covers Verilator.

## B.2 What the build produces

Each build emits **one standalone executable** into `sims/verilator/` — a Verilated C++ model of
the whole SoC, linked against fesvr (`-lfesvr`) and DRAMSim2. It is an ordinary program: `make`
is not needed to run it.

| Executable | Built by | Size | `+define+DEBUG` | Can emit |
|---|---|---|:---:|---|
| `simulator-chipyard.harness-RocketConfig` | `make` | 11 MB | – | UART output, commit trace |
| `simulator-chipyard.harness-RocketConfig-debug` | `make debug` | 15 MB | ● | the above **plus** `.vcd`/`.fst` waveforms |

Name pattern: `simulator-$(MODEL_PACKAGE)-$(CONFIG)[-debug]` (`sims/verilator/Makefile:25-26`).
Its command-line interface is a positional ELF argument plus Verilog plusargs:

```
./simulator-chipyard.harness-<CONFIG>[-debug] [+plusargs …] <program.riscv> [target args]
```

It writes to the two standard streams — **nothing lands in `output/` unless you redirect it there
yourself**; that filing is the Makefile's doing (§1.3, Appendix E):

| Stream | Contents |
|---|---|
| **stdout** | target UART output, DRAMSim2 banners, `Verilog $finish` |
| **stderr** | commit trace (only with `+verbose`) and the `*** PASSED/FAILED ***` verdict, with instructions still as raw `DASM(…)` tokens — pipe through `spike-dasm` to disassemble |
| **exit status** | 0 when the harness reaches `$finish` (pass); non-zero on `$fatal` (failure or timeout) |

`CONFIG` defaults to `RocketConfig`. Each `CONFIG` gets an independent build tree and its own
binaries, so configs never clobber each other.

## B.3 What `RocketConfig` actually is

```scala
class RocketConfig extends Config(
  new freechips.rocketchip.rocket.WithNHugeCores(1) ++
  new chipyard.config.AbstractConfig)
```

One "huge" Rocket core (`rv64gc`, FPU, MMU, branch predictor, L1 I/D + L2) on Chipyard's
`AbstractConfig` platform — UART, DRAM via DRAMSim2, TSI/HTIF bringup, JTAG.

## B.4 Design-selection variables

For non-standard targets, `SUB_PROJECT` presets a bundle (`variables.mk:89-101`):

| Variable | Meaning | Default (`SUB_PROJECT=chipyard`) |
|---|---|---|
| `SUB_PROJECT` | preset bundle for all of the below | `chipyard` |
| `SBT_PROJECT` | `build.sbt` project holding the sources | `chipyard` |
| `MODEL` | top-level harness class | `TestHarness` |
| `VLOG_MODEL` | Verilog name of `MODEL` | `$(MODEL)` |
| `MODEL_PACKAGE` | Scala package containing `MODEL` | `chipyard.harness` |
| `CONFIG` | parameter configuration class | `RocketConfig` |
| `CONFIG_PACKAGE` | package containing `CONFIG` | `chipyard` |
| `GENERATOR_PACKAGE` | package containing the elaboration Generator | `chipyard` |
| `TB` | Verilog wrapper tying harness to simulator | `TestDriver` |
| `TOP` | true design top (vs. the TestHarness) | `ChipTop` |

```bash
make help              # ✅ full variable reference
make find-configs      # lists all Config classes (chipyard.ChipyardConfigFinder)
```

## B.5 Generated file layout

```
sims/verilator/
├── simulator-chipyard.harness-RocketConfig
├── simulator-chipyard.harness-RocketConfig-debug
├── generated-src/chipyard.harness.TestHarness.RocketConfig/
│   ├── gen-collateral/                              # emitted Verilog + C++ shims
│   ├── chipyard.harness.TestHarness.RocketConfig.d  # ← generated test-suite makefrag
│   ├── ...chiptop0.graphml                          # diplomacy graph (open in yEd)
│   └── chipyard.harness.TestHarness.RocketConfig/   # Verilator obj dir
└── output/chipyard.harness.TestHarness.RocketConfig/  # ← all run artifacts
```

The `.d` file defines `run-asm-tests`, `run-bmark-tests` and friends. It is generated during
elaboration and included only when the make goal matches `run% %.run %.out %.vpd %.vcd %.fsdb`
(`common.mk:468`) — **so plain `make` must succeed before any suite target exists.**

## B.6 Visualizing the SoC

Elaboration emits `generated-src/<long_name>/<long_name>.chiptop0.graphml`. Open in **yEd**,
switch layout to *hierarchical*.

---

# Appendix C — Test binaries reference

## C.1 Where `riscv-tests` comes from

`$RISCV/riscv64-unknown-elf/share/riscv-tests/` is **not** shipped by conda — it is compiled
locally during `build-setup.sh` **step 3** ("Building toolchain collateral") by
`scripts/build-toolchain-extra.sh:89`:

```bash
module_all riscv-tests --prefix="${RISCV}/riscv64-unknown-elf" --with-xlen=64
```

Source: the `toolchains/riscv-tools/riscv-tests` submodule
(`github.com/riscv-software-src/riscv-tests`, pinned at `0494f95`). No separate build step is
needed — the binaries exist after `build-setup.sh`. The build tree survives at
`toolchains/riscv-tools/riscv-tests/build/` (gitignored).

> 📌 **Caveat:** this prefix mixes conda-owned files with locally built ones — 6265 files conda
> tracks vs 12812 on disk, i.e. **6547 orphans**. Deleting or recreating `.conda-env` silently
> destroys all locally built collateral (spike, pk, riscv-tests, libgloss, espresso).

## C.2 ISA tests — `isa/`

```
$RISCV/riscv64-unknown-elf/share/riscv-tests/isa/
```

1232 entries, half of them `.dump` objdump listings. ELF counts by group:

```
104 rv64ui    48 rv64uzbb   26 rv64um    22 rv64uf     16 rv64mi    6 rv64uzbc
 80 rv32ui    38 rv64ua     24 rv64ud    22 rv64uzfh    7 rv64si    2 rv64uc  ...
```

**Naming:** `rv<XLEN><exts>-<p|v>-<test>`, e.g. `rv64ui-p-add`, `rv64um-v-mul`.

| Variant | Environment | Exercises |
|---|---|---|
| `-p-` | **physical** — no virtual memory, machine mode | instruction behaviour only; fast |
| `-v-` | **virtual** — paging enabled, user mode | instruction behaviour **plus** MMU and trap paths; slower |

Most tests are built both ways, so you can pick what to exercise by changing one letter:

```bash
make run-binary CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add   # physical
make run-binary CONFIG=RocketConfig BINARY=$ISA/rv64ui-v-add   # virtual — same test, plus MMU/traps
```

`-v-` requires an MMU on the target. Privileged tests (`rv64mi-*`, `rv64si-*`) exist only as `-p-`.

## C.3 Benchmarks — `benchmarks/`

```
$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/
```

Naming: `<name>.riscv`. No `-p-`/`-v-` variants — each is a single bare-metal ELF run in
machine mode. Benchmarks print `mcycle`/`minstret` over UART on completion.

The exact set is generated per-config into the `.d` file. For `RocketConfig` ✅:

**Integer — `rvi-bmark-tests` (9)**

| Benchmark | What it does |
|---|---|
| `median.riscv` | 3-tap median filter (1D) |
| `multiply.riscv` | software integer multiply routine |
| `qsort.riscv` | quicksort |
| `rsort.riscv` | radix sort |
| `pmp.riscv` | Physical Memory Protection exercise |
| `towers.riscv` | Towers of Hanoi (recursion) |
| `vvadd.riscv` | vector-vector add (simple loop) |
| `dhrystone.riscv` | Dhrystone integer benchmark |
| `mt-matmul.riscv` | multi-threaded matrix multiply |

**Double-precision FP — `rvd-bmark-tests` (3, present only because RocketConfig has an FPU)**

| Benchmark | What it does |
|---|---|
| `mm.riscv` | matrix multiply (double) |
| `spmv.riscv` | sparse matrix-vector multiply |
| `mt-vvadd.riscv` | multi-threaded vector-vector add (double) |

**Installed but not in the default list** — run individually with `BINARY=`:

| Benchmark | What it does | On RocketConfig |
|---|---|---|
| `memcpy.riscv` | `memcpy` throughput | ✅ runs — 1.33 M cycles |
| `mt-memcpy.riscv` | multi-threaded `memcpy` | ✅ runs |
| `vec-daxpy.riscv` | DAXPY (`y = a*x + y`) | ❌ needs RVV |
| `vec-memcpy.riscv` | vectorized `memcpy` | ❌ needs RVV |
| `vec-sgemm.riscv` | single-precision GEMM | ❌ needs RVV |
| `vec-strcmp.riscv` | vectorized `strcmp` | ❌ needs RVV |

The `vec-*` four issue RVV vector instructions; RocketConfig has no vector unit, so forcing one
through `BINARY=` traps as an illegal instruction — expected, not a core bug (C.5). Run them on a
vector target instead: see [`Saturn_Build_Test.md`](Saturn_Build_Test.md).

List everything actually installed:

```bash
ls $RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks/*.riscv
```

## C.4 Custom tests — `tests/`

Put sources in `tests/`, add the name to `PROGRAMS` in `tests/Makefile`. Binaries link against
**libgloss-htif**.

```bash
cd ../../tests && make && cd ../sims/verilator
make run-binary BINARY=../../tests/hello.riscv
```

Existing examples: `hello.c`, `gcd.c`, `fft.c`, `accum.c`, `charcount.c`, `mt-hello.c`,
`nic-loopback.c`, `blkdev.c`, `nvdla.c`, `cpp-hello.cpp`, …

> **Multi-core:** only hart 0 runs `main()`. Other harts enter the secondary `__main()`, which
> defaults to a busy loop.

## C.5 Unsupported ISA extensions

Suite targets never include unsupported extensions — the generated `.d` lists only what the
config implements. Forcing one through `BINARY=` traps as an illegal instruction, which is
expected behaviour and not a core bug:

```bash
make run-binary CONFIG=RocketConfig BINARY=$ISA/rv64uzbc-p-clmul
# ✅ → *** FAILED *** (tohost = 668)      [RocketConfig has no Zbc]
```

This is why a hand-rolled glob over `isa/` is a bad substitute for the suite targets
(Appendix E.4).

---

# Appendix D — Run reference: targets & variables

## D.1 `make` vs. the simulator binary

**Way A — through `make`.** Rebuilds the simulator if stale, assembles the plusargs, runs the
binary, splits stdout/stderr into `.log`/`.out`, pipes stderr through `spike-dasm`, and — for
suite targets — already knows which tests *this config* supports.

**Way B — the simulator executable directly.** You supply the plusargs and the ELF, and do your
own redirection. Full treatment in Appendix E.

| | Way A — `make` | Way B — simulator directly |
|---|:---:|:---:|
| Rebuilds the simulator when stale | ● | – |
| Per-config test list (from the generated `.d`) | ● | – |
| `.log` / `.out` split + `spike-dasm` | ● | you write the redirection |
| Symlinks, `.run` markers, interrupted-sweep resume | ● (suite targets) | – |
| Whole suite in one command, with summary + exit code | ● | – (loop it yourself) |
| `-j` parallel scheduling | ● | – |
| Wrap in `gdb` / `perf`, or pass a flag `make` doesn't expose | awkward | ● |

**Use Way A by default.** Way B is for a debugger, a profiler, or a custom harness around a
*single* run.

## D.2 Single-binary targets and runtime variables

```bash
make run-binary       CONFIG=RocketConfig BINARY=<elf>    # default mode
make run-binary-fast  CONFIG=RocketConfig BINARY=<elf>    # no trace
make run-binary-debug CONFIG=RocketConfig BINARY=<elf>    # trace + waveform
```

Related: `run-binaries[-fast|-debug]` take `BINARIES=<glob or list>`; `run-binary-debug-bg` runs
the debug sim in the background and prints its PID.

| Variable | Default | Meaning |
|---|---|---|
| `BINARY=<path>` | — | ELF to run (required by `run-binary*`) |
| `BINARY_ARGS=...` | empty | arguments passed to the binary (mainly for `pk`) |
| `LOADMEM=<elf>` / `LOADMEM=1` | empty | load the ELF straight into simulated DRAM instead of the slow serial TSI boot; `1` reuses `BINARY` |
| `EXTRA_SIM_FLAGS="+max-cycles=N"` | empty | extra runtime plusargs; raise the cycle cap for long benchmarks |
| `VERBOSE_FLAGS` | `+verbose` | what enables the commit trace in default/debug modes |
| `DUMP_BINARY` | **`1`** ⚠️ | emit `objdump -D -S` `.dump`. Only consulted by the **debug** rules |
| `USE_FST` | `0` | emit `.fst` instead of `.vcd` (Verilator only) |
| `NUMACTL` | `0` | set `1` to wrap the sim in `numactl` via `scripts/numa_prefix` |

Default cycle cap is `+max-cycles=10000000`. These variables are a front end for the simulator's
own plusargs — Appendix E.2 maps each to the flag it becomes. ⚠️ `VERBOSE_FLAGS` reaches the
`run-binary` and `run-binary-debug` rules only; the suite `.out` rule never passes it (§2.2).

## D.3 All suite targets

Generated into `generated-src/<long_name>/<long_name>.d` at elaboration time. Each expands to one
simulator invocation per test plus a perl one-liner that prints the summary and sets the exit
code. **They exist only under `make`** — Appendix E.4 shows what reproducing one by hand costs.

### ISA assembly tests

| Target | Artifacts | Status |
|---|---|---|
| `run-asm-tests` | `.out` + `.log` | ✅ |
| `run-asm-tests-fast` | `.log` + `.run` marker | ✅ |
| `run-asm-tests-fst` | `.fst` waveform | ✅ Verilator waveform suite |
| `run-asm-tests-debug` | `.vpd` | ❌ **needs `vcd2vpd`** (VCS/Verdi tool, absent here) |
| `run-asm-p-tests[-fast/-fst]` | physical subset | ✅ |
| `run-asm-v-tests[-fast/-fst]` | virtual subset | ✅ |
| `run-rv64ui-p-asm-tests[-…]` | one extension group | ✅ |

### Benchmarks

| Target | Artifacts | Status |
|---|---|---|
| `run-bmark-tests` | `.out` + `.log` | ✅ |
| `run-bmark-tests-fast` | `.log` + `.run` | ✅ |
| `run-bmark-tests-fst` | `.fst` | ✅ |
| `run-bmark-tests-debug` | `.vpd` | ❌ needs `vcd2vpd` |
| `run-rvi-bmark-tests` | integer group only | ✅ |
| `run-rvd-bmark-tests` | double-FP group only | ✅ |
| `run-rvi-bmark-tests-fast` / `run-rvd-bmark-tests-fast` | — | ❌ **do not exist** |

### Combined and subset

| Target | Runs |
|---|---|
| `run-fast` | `run-asm-tests-fast` + `run-bmark-tests-fast` |
| `run-regression-tests[-fast/-fst/-debug]` | fixed 25-test cross-section of the ISA suite (`...RocketConfig.d:841-874`) — `fdiv`, `lrsc`, `fence_i`, `breakpoint`, `wfi`, `rvc`, … ✅ `make -n` plans exactly 25 runs |
| `run-tests` | ❌ **does not exist**, despite the older tutorials |

---

# Appendix E — Driving the simulator binary directly

Way B from Appendix D.1, in full. The command lines below are transcribed from the Makefiles
(line numbers cited) and spot-checked against `make -n` and live runs on 2026-09-10.

```bash
cd /home/vscode/chipyard/sims/verilator
SIM=./simulator-chipyard.harness-RocketConfig
DBG=./simulator-chipyard.harness-RocketConfig-debug
DRAMSIM="+dramsim +dramsim_ini_dir=../../generators/testchipip/src/main/resources/dramsim2_ini"
ISA=$RISCV/riscv64-unknown-elf/share/riscv-tests/isa
OUT=output/chipyard.harness.TestHarness.RocketConfig
```

## E.1 Argument order is not optional

```
$SIM  +permissive  <RTL/harness plusargs>  +permissive-off  <ELF>  [target args]
```

fesvr parses the whole `argv` and throws
`Unknown argument (did you mean to enable +permissive parsing?)` on anything it does not
recognise. `+permissive` suppresses that check and `+permissive-off` restores it
(`fesvr/htif.cc:365-368,411-419`), so every simulator-side plusarg must sit **between** the two
and the ELF path must come **after** `+permissive-off`. Get it wrong and the failure mode is a
confusing "could not open …" from fesvr, not a usage message.

With **no** extra plusargs the brackets are unnecessary — `$SIM <ELF>` is a legal, complete
command line ✅ (§1.3).

## E.2 Plusargs

| Plusarg | Read by | Effect |
|---|---|---|
| `+verbose` | `TestDriver.v:32` | commit trace on stderr — **and** the `*** PASSED ***` line (`TestDriver.v:152`), which is printed *only* under verbose |
| `+max-cycles=N` | `TestDriver.v:30` | timeout: `*** FAILED *** (timeout)` past N cycles. `N=0` (the default when the arg is absent) means **no timeout** — a hang runs forever |
| `+dump-start=N` | `TestDriver.v:31` | begin waveform dumping at cycle N |
| `+vcdfile=<path>` | `TestDriver.v:74` | waveform file — **debug build only**; the regular build `$fatal`s with "compile did not have +define+DEBUG enabled" (`TestDriver.v:95-99`). Use a `.fst` path only if the sim was built `USE_FST=1` |
| `+dramsim` `+dramsim_ini_dir=<dir>` | DRAM model | use DRAMSim2 with those ini files; always passed by `make` (`variables.mk:301`) |
| `+loadmem=<elf>` | testchipip TSI | preload DRAM directly, skipping the slow serial TSI boot (§2.5) |
| `+verilator+seed+N` | Verilator | RNG seed (`RANDOM_SEED=N` under `make`) |
| `+permissive` / `+permissive-off` | fesvr | bracket all of the above |

> ⚠️ **`+dramsim` changes your cycle counts**, so leave it in if you want numbers comparable to
> `make`. ✅ Measured on `rv64ui-p-simple`: **30 696** cycles without it, **31 136** with — the
> latter matching what `make run-binary` reports. Everything else about the run is identical.

`</dev/null` on the invocations below is deliberate: the simulated UART reads the terminal's
stdin. Drop it only if you mean to type at the target.

## E.3 The three modes, by hand

**= `make run-binary`** (`common.mk:378-386`; ✅ expansion confirmed with `make -n`)

```bash
mkdir -p $OUT
$SIM +permissive $DRAMSIM +max-cycles=10000000 +verbose +permissive-off \
  $ISA/rv64ui-p-add </dev/null \
  2> >(spike-dasm > $OUT/rv64ui-p-add.out) | tee $OUT/rv64ui-p-add.log
```

**= `make run-binary-fast`** (`common.mk:392-399`) — drop `+verbose` and the stderr pipe

```bash
$SIM +permissive $DRAMSIM +max-cycles=10000000 +permissive-off \
  $ISA/rv64ui-p-add </dev/null | tee $OUT/rv64ui-p-add.log
```

**= `make run-binary-debug`** (`common.mk:407-419`) — debug executable, `+vcdfile`, plus the
disassembly step the Makefile does for you ⏳

```bash
riscv64-unknown-elf-objdump -D -S $ISA/rv64ui-p-add > $OUT/rv64ui-p-add.dump
$DBG +permissive $DRAMSIM +max-cycles=10000000 +verbose \
  +vcdfile=$OUT/rv64ui-p-add.vcd +permissive-off \
  $ISA/rv64ui-p-add </dev/null \
  2> >(spike-dasm > $OUT/rv64ui-p-add.out) | tee $OUT/rv64ui-p-add.log
```

**Just the verdict**, nothing written to disk ✅

```bash
$SIM +permissive $DRAMSIM +verbose +permissive-off $ISA/rv64ui-p-simple \
  2>&1 >/dev/null | spike-dasm | tail -1
# → *** PASSED *** Completed after                31136 simulation cycles
```

And the reason Way B exists at all — a debugger or profiler around the run ⏳

```bash
gdb --args $SIM +permissive $DRAMSIM +max-cycles=10000000 +permissive-off $ISA/rv64ui-p-add
perf stat -- $SIM +permissive $DRAMSIM +max-cycles=10000000 +permissive-off $ISA/rv64ui-p-add
```

## E.4 Can `run-asm-tests` be done with the simulator directly?

**Not as one command.** The simulator runs exactly one ELF per invocation; `run-asm-tests` is not
a mode of it. It is a generated make target that expands to one `$(output_dir)/<test>.out`
prerequisite per test (`...RocketConfig.d:773`), each satisfied by one simulator run
(`common.mk:461`), followed by a perl one-liner over the resulting `.out` files that prints the
per-test verdicts and sets the exit code (`...RocketConfig.d:774`).

You can reproduce the shape of it in shell ⏳:

```bash
mkdir -p $OUT
for t in $ISA/rv64ui-p-*; do
  [ "${t%.dump}" = "$t" ] || continue          # skip the objdump listings
  n=$(basename "$t")
  $SIM +permissive $DRAMSIM +max-cycles=10000000 +verbose +permissive-off "$t" \
    </dev/null 2> >(spike-dasm > "$OUT/$n.out") > "$OUT/$n.log"
done
grep -l 'FAILED' $OUT/*.out          # non-empty = something broke
```

What that loop gives up:

- **the per-config test list.** The `.d` lists only extensions this config implements. A
  hand-rolled glob happily runs `rv64uzbc-*` on a core with no Zbc and reports false failures
  (Appendix C.5).
- **`.run` markers**, so an interrupted sweep restarts from zero (§1.5).
- **`-j` scheduling** — the ~8× win in §1.5.
- **the summary line and the aggregate exit code.**

Two middle grounds keep the Makefile but let you pick the binaries ⏳:

```bash
make run-binaries      CONFIG=RocketConfig BINARIES="$(ls $ISA/rv64ui-p-* | grep -v '\.dump$' | tr '\n' ' ')"
make run-binaries-fast CONFIG=RocketConfig BINARIES="$(ls $ISA/rv64ui-p-* | grep -v '\.dump$' | tr '\n' ' ')"
```

`BINARIES` is `$(wildcard)`-expanded (`common.mk:376`), so a literal list or a glob both work —
but a bare `rv64ui-p-*` glob would also match the `.dump` listings and feed them to fesvr. These
go through the generic `%.run` rules, so still no symlinks, no markers and no incremental skip
(§2.2).

---

# Appendix F — Verification log

Executed here on 2026-09-08, `CONFIG=RocketConfig`:

| # | Command | Result |
|---|---|---|
| 1 | `make run-binary BINARY=$ISA/rv64ui-p-simple` | ✅ `*** PASSED ***`, 31136 cycles |
| 2 | `make run-binary BINARY=$ISA/rv64ui-p-add` | ✅ PASSED, 99666 cycles; `.log` + `.out` only |
| 3 | `make run-binary-fast BINARY=$ISA/rv64ui-p-add` | ✅ `.log` only |
| 4 | `make CONFIG=RocketConfig debug` | ✅ built `simulator-...-RocketConfig-debug` (15 MB) |
| 5 | `make run-binary-debug BINARY=$ISA/rv64ui-p-add` | ✅ `.dump` + `.log` + `.out` + 220 MB `.vcd` |
| 6 | `make $OUT/rv64ui-p-add.run` (abs path) | ✅ symlink + `.log` + empty `.run` marker |
| 7 | `make $OUT/median.riscv.run` (abs path) | ✅ symlink into `benchmarks/`, `mcycle = 6117`, `minstret = 4659` |
| 8 | `make run-binary BINARY=$ISA/rv64uzbc-p-clmul` | ✅ `*** FAILED *** (tohost = 668)`, exit 2 |
| 9 | `make -n run-tests` | ❌ `No rule to make target 'run-tests'` |
| 10 | `make -n run-asm-tests-debug` | ❌ recipe shells out to `vcd2vpd` — not installed |
| 11 | `make -n run-asm-tests-fst` | ✅ emits `-v<test>.fst` |
| 12 | `make -n run-bmark-tests-fast` | ✅ 11 of 12 planned — incremental skip confirmed |
| 13 | `make -n USE_FST=1 run-binary-debug` | ✅ flag becomes `+vcdfile=<test>.fst` |
| 14 | `make -n run-rvi-bmark-tests-fast` | ❌ does not exist |
| 15 | `run-binary BINARY=$ISA/rv64ui-p-add LOADMEM=1` on a Saturn config | ✅ 7436 cycles / 23 s vs 99666 cycles unloaded — TSI load is ~92% of cycles |
| 16 | `make -j16 run-asm-tests-fast` — **the full ISA suite** | ✅ exit 0, **335/335 asm tests passed**, 336 markers incl. one benchmark. 96 min 47 s wall / 799 min 41 s CPU → **~8.3× parallel speedup** on 16 cores. Split: 178 `-p-`, 157 `-v-`. Zero failures |

Added 2026-09-10, all `CONFIG=RocketConfig`:

| # | Command | Result |
|---|---|---|
| 17 | `./simulator-…-RocketConfig $ISA/rv64ui-p-simple` (no plusargs at all) | ✅ runs; prints only the UART banner and `TestDriver.v:158: Verilog $finish`; exit 0. **No verdict without `+verbose`** |
| 18 | same + `+permissive +verbose +permissive-off`, stderr through `spike-dasm` | ✅ full commit trace then `*** PASSED *** Completed after 30696 simulation cycles` |
| 19 | #18 again, this time **with** `+dramsim` | ✅ `*** PASSED ***` at **31136** cycles — matches #1, so the cycle delta vs #18 is entirely the DRAM model |
| 20 | `make -n $OUT/rv64ui-p-simple.out` vs `make -n run-binary` | ✅ the suite `%.out` rule carries **no `+verbose`**; `run-binary` does. Confirms the §2.2 ⚠️ |
| 21 | `make -n run-regression-tests-fast` | ✅ target exists, plans exactly **25** simulator runs |
| 22 | `make run-binary BINARY=$BM/memcpy.riscv` into an empty output dir | ✅ PASSED, 1332136 cycles. Left **exactly two** files — `memcpy.log` (212 B) + `memcpy.out` (4.9 MB). No symlink, no `.run`, no `.dump`, no `.vcd` — confirms the §2.2 single-binary table |
| 23 | artifact naming, same run | ⚠️ artifacts are `memcpy.*`, **not** `memcpy.riscv.*` — `run-binary` strips the last extension via `get_out_name` (`variables.mk:304`). Suite targets do not: `median.riscv.log` / `.run` sit in the same directory |

Config-specific lists in Appendix C.3 were read from the generated
`chipyard.harness.TestHarness.RocketConfig.d`.

---

# Appendix G — Corrections applied

Errors found in `tutorial_riscv_tests_isa.md` / `tutorial_riscv_tests_bm.md` while verifying:

| # | Source claim | Reality |
|---|---|---|
| 1 | `run-tests` runs "asm + bmark together" | **No such target.** Use `run-fast` (`common.mk:434`) |
| 2 | `.log` tail shows `*** PASSED *** Completed after 512 cycles` | `*** PASSED ***` is in **`.out`**, never `.log`. Real count for `rv64ui-p-add` is **99666** |
| 3 | A `run-binary` run leaves a `<name>` symlink | Symlinks come only from **suite** targets |
| 4 | Artifact table marks `.run` as produced by default mode | `run-binary` creates **no** `.run`; only `$(output_dir)/%.run` does |
| 5 | "`Verilog $finish` means the test passed" | Printed on pass **and** fail |
| 6 | `run-asm-tests-debug` / `run-bmark-tests-debug` usable | Target `.vpd`, require **`vcd2vpd`** (absent). Use `-fst` |
| 7 | `run-rvi/rvd-bmark-tests` listed as fast-capable | No `-fast` variant exists |
| 8 | "`0x80000000` is the reset vector" | Execution begins in the **bootrom at `0x10000`** |
| 9 | `DUMP_BINARY=1` presented as opt-in | **Defaults to 1**; only consulted by debug rules |
| 10 | Waveform size warning attached to benchmarks only | Even `rv64ui-p-add` yields **220 MB** |
| 11 | Artifact tables write `<name>.log` / `<name>.out` uniformly, and the benchmark tutorial reuses them for `*.riscv` files | `run-binary` **strips the last extension**: `BINARY=…/memcpy.riscv` produces `memcpy.out`, not `memcpy.riscv.out`. Only suite targets keep the full name (§2.2, log #23) |

One correction to an earlier revision of *this* document: the suite `.out` files were described as
carrying a commit trace and a `PASSED` line. They carry neither — see the ⚠️ in §2.2 (verified,
log entry #20).

---

# Appendix H — Cheat sheet

```bash
source /home/vscode/chipyard/env.sh
cd /home/vscode/chipyard/sims/verilator
ISA=$RISCV/riscv64-unknown-elf/share/riscv-tests/isa
BM=$RISCV/riscv64-unknown-elf/share/riscv-tests/benchmarks
OUT=output/chipyard.harness.TestHarness.RocketConfig

# --- build ---
make CONFIG=RocketConfig
make CONFIG=RocketConfig debug
make help / make find-configs

# --- way A: single test via make ---
make run-binary       CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add
make run-binary-fast  CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add
make run-binary-debug CONFIG=RocketConfig BINARY=$ISA/rv64ui-p-add
make run-binary       CONFIG=RocketConfig BINARY=$BM/dhrystone.riscv EXTRA_SIM_FLAGS="+max-cycles=100000000"
make run-binary       CONFIG=RocketConfig BINARY=../../tests/hello.riscv
make run-binary       CONFIG=RocketConfig BINARY=test.riscv LOADMEM=1

# --- way A: suites, make only (run plain `make` first) ---
make -j$(nproc) CONFIG=RocketConfig run-asm-tests-fast
make -j$(nproc) CONFIG=RocketConfig run-bmark-tests-fast
make -j$(nproc) CONFIG=RocketConfig run-fast
make            CONFIG=RocketConfig run-rvi-bmark-tests
make -j$(nproc) CONFIG=RocketConfig run-regression-tests-fast   # 25-test smoke subset
make -j$(nproc) CONFIG=RocketConfig run-asm-tests-fst

# --- way B: the simulator binary directly (Appendix E) ---
SIM=./simulator-chipyard.harness-RocketConfig
DRAMSIM="+dramsim +dramsim_ini_dir=../../generators/testchipip/src/main/resources/dramsim2_ini"
$SIM $ISA/rv64ui-p-simple                                   # simplest possible run
#   order: +permissive <sim plusargs> +permissive-off <ELF>
$SIM +permissive $DRAMSIM +verbose +permissive-off $ISA/rv64ui-p-simple \
  2>&1 >/dev/null | spike-dasm | tail -1                    # just the verdict
$SIM +permissive $DRAMSIM +max-cycles=10000000 +verbose +permissive-off $ISA/rv64ui-p-add \
  </dev/null 2> >(spike-dasm > $OUT/rv64ui-p-add.out) | tee $OUT/rv64ui-p-add.log
./simulator-chipyard.harness-RocketConfig-debug +permissive $DRAMSIM +max-cycles=10000000 \
  +verbose +vcdfile=$OUT/rv64ui-p-add.vcd +permissive-off $ISA/rv64ui-p-add

# --- results ---
ls $OUT/*.run | wc -l
grep -l 'FAILED' $OUT/*.out
tail -1 $OUT/rv64ui-p-add.out
grep -a '^C0:' $OUT/rv64ui-p-add.out | less
rm -f $OUT/*.run
```
