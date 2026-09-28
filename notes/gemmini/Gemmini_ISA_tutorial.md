# Gemmini ISA — a guided tour

One 16 × 16 matrix multiply, four ways: as the scalar C loop everybody writes
first, as the same tile written with the `gemmini.h` macros, as what those
macros expand to, and as the assembly the compiler finally emits. By the end you
should be able to read any line of Gemmini assembly and know what it does, even
though no disassembler will tell you.

§2 first builds the picture the rest depends on — how a weight-stationary array
actually computes `A · B`, and why that forces two instructions per matmul.
§8 closes by scaling the one tile up to a real matrix.

This is document 1 of 2. [`Gemmini_ISA_reference.md`](Gemmini_ISA_reference.md)
is the instruction-by-instruction reference, and every cross-reference below
points into it. Read this one start to finish; reach for the reference
afterwards. For the hardware underneath, read the upstream
[`README.md`](../../generators/gemmini/README.md) —
its Architecture and Major Components sections, with the figures.

**Provenance.** `ucb-bar/gemmini-rocc-tests` `include/gemmini.h` (**dev**
branch) and `ibm/rocc-software` `src/xcustom.h`. Assembly is `clang -O2` for
RV64GC from the Chipyard `riscv-tools` environment.

**Reference configuration assumed throughout:** `DIM = 16`, `ADDR_LEN = 32`,
`elem_t = int8`, `acc_t = int32`, `acc_scale_t = float32`.

---

## 1. Why a matrix accelerator

Start with the most ordinary loop in linear algebra, a matrix multiply with a bias,
`C = A · B + D`, in the types Gemmini's reference configuration uses: `int8` inputs, `int32`
accumulation, `int8` output.

```c
/* C = A * B + D.  elem_t = int8, acc_t = int32.  Row-major, N x N. */
void matmul_scalar(size_t N, const elem_t *A, const elem_t *B, const acc_t *D, elem_t *C) {
    for (size_t i = 0; i < N; i++)
        for (size_t j = 0; j < N; j++) {
            acc_t acc = D[i*N + j];
            for (size_t k = 0; k < N; k++)
                acc += A[i*N + k] * B[k*N + j];
            C[i*N + j] = acc > 127 ? 127 : (acc < -128 ? -128 : acc);   // saturate to int8
        }
}
```

A scalar core runs the inner body as roughly seven instructions: load `A[i][k]`, load `B[k][j]`,
multiply, add, advance two pointers, branch. For N = 16 that is 4,096 multiply-accumulates and
about 33,000 dynamic instructions once the `i` and `j` loops are counted, and 8,192 of them are
loads that each move a single byte.

A **systolic array** lets you describe the same work once. Gemmini's array is `DIM × DIM` = 16 × 16
multiply-accumulate units, so one `DIM × DIM × DIM` tile, the whole N = 16 problem above, is *one*
instruction pair: "preload these weights", "stream these activations through". Around that pair
sit a handful of moves between DRAM and Gemmini's private memory, and a few configuration
instructions that are set once. Eleven Gemmini instructions replace 33,000, and the systolic array
retires 256 multiply-accumulates per cycle without a single instruction being fetched for any of
them.

> **Insight.** The reason to care about a matrix accelerator is the same as the reason to care
> about vectors, taken one step further. A vector instruction removes the per-*element*
> instruction traffic; a matmul instruction removes the per-*row* traffic too, because the
> reduction over `k` happens inside the array as data flows between PEs. The host issues one
> instruction per *tile*, and the rest of this document is about what that instruction costs
> to issue.

Two questions arise immediately, and they are the same two questions one asks of a vector unit:

1. *How much work* does one instruction do, and who decides: the programmer, the compiler, or the
   chip?
2. What happens when the matrix dimensions are not a multiple of that amount?

Gemmini's answers are the fixed-width ones, not the RVV ones. `DIM` is fixed when the accelerator
is elaborated, and software must *know* it: `gemmini_params.h` is generated alongside the hardware
and `#define DIM 16` is the first thing in it. Every tiling loop, every scratchpad address and
every stride in the library is written in terms of `DIM`. There is no `vsetvli`, no run-time query,
and a binary tiled for `DIM = 16` is not the right binary for a `DIM = 32` Gemmini. What Gemmini
*does* borrow from the vector world is the answer to question 2: every instruction that names a
tile carries an explicit `rows` and `cols` count in its operand ([reference §7](Gemmini_ISA_reference.md)), so a 13 × 16 edge tile is
the *same* `mvin` and the *same* `compute` with smaller fields, not a separate scalar loop. That
count is Gemmini's `vl`, and it travels inside each instruction rather than in a control register.

## 2. How a weight-stationary array multiplies

Before the code, the picture the code is written against. Everything in §4 —
why there are two instructions per matmul, why `B` is named by one and `A` by
the other, why the bias arrives through a third path — follows from this.

A systolic array is a grid of multiply-accumulate units with no instruction
fetch and no register file. Data flows between neighbours on wires. In
**weight-stationary** mode, one element of `B` is loaded into each PE and
*stays there*, while `A` streams through:

```
           B is resident: PE at (row k, col j) holds b[k][j]
           A enters from the left, one row of A per cycle
           partial sums flow DOWN and accumulate

                        j=0      j=1      j=2
                     +--------+--------+--------+
  a[i][0] ─────────► │  b00   │  b01   │  b02   │   k=0
                     +--------+--------+--------+
  a[i][1] ─────────► │  b10   │  b11   │  b12   │   k=1
                     +--------+--------+--------+
  a[i][2] ─────────► │  b20   │  b21   │  b22   │   k=2
                     +--------+--------+--------+
                          │        │        │
                          ▼        ▼        ▼
                       c[i][0]  c[i][1]  c[i][2]
```

Follow one column. PE `(0,j)` computes `a[i][0] · b[0][j]` and passes it down.
PE `(1,j)` computes `a[i][1] · b[1][j]`, **adds what came from above**, and
passes that down. By the time the value leaves the bottom of column `j` it is

```
    c[i][j] = a[i][0]·b[0][j] + a[i][1]·b[1][j] + a[i][2]·b[2][j] + ...
```

which is exactly the `k` loop of the scalar version — except it is a column of
wires and adders, not a loop. **That is where the 33,000 instructions went.**
No instruction is fetched for any of those multiplies or adds; feed one row of
`A` per cycle and one row of `C` falls out the bottom per cycle.

### Why this needs two instructions

Loading `B` into the array and streaming `A` through it are physically
different operations on different cycles, and a single RISC-V instruction
cannot name four matrices anyway. So every matmul is a **pair**:

```
    preload   rs1 = the stationary operand    rs2 = where C goes
    compute   rs1 = A                         rs2 = the other operand
```

`preload` pushes `B` into the PEs and names the destination for `C`. `compute`
then streams `A` through. The array sits idle between them — `preload` commits
immediately and waits.

This also explains the payoff that makes WS worth using. Loading weights is
expensive; streaming activations is cheap. If the next matmul uses the *same*
`B`, you skip the reload and issue `compute.accumulated` instead, or pass
`GARBAGE_ADDR` as `preload`'s `rs1` to mean "keep what you have". A tiling loop
is built around reusing one resident `B` across many `A` tiles.

### The operand roles swap between dataflows

Gemmini also supports **output-stationary**, where `D` is resident and both `A`
and `B` stream through. The instruction encoding is identical; only the meaning
of two operands changes:

| | `preload` rs1 holds | `compute` rs2 holds |
|---|---|---|
| **Weight-stationary** | `B` — the weights | `D` — the bias |
| **Output-stationary** | `D` — the bias | `B` |

Getting this backwards does not fault. It produces plausible, wrong numbers —
which is why [reference §23](Gemmini_ISA_reference.md) lists it as a
standing trap.

### Where the bias actually goes

One more consequence, and it is the detail that makes the code in §4 look
strange until you see it. In WS mode `D` would have to enter as `inputType` —
`int8` in the reference config — and `int8` is far too narrow to hold a partial
sum that has been accumulating over `k`.

So in practice **WS code does not send `D` through the array at all.** It moves
`D` into the *accumulator* SRAM, which is `int32` wide and has adders on its
write port, and then tells `preload` to write `C` to that same accumulator
address in *accumulate* mode. `A · B` lands on top of `D` inside the
accumulator. The `compute` instruction's `rs2` — the `D` slot — is filled with
`GARBAGE_ADDR` to say "nothing arrives this way".

That is why the next section's eleven instructions include a third `mvin`
targeting an accumulator address, and why `compute` is handed a constant that
looks like a bug.

## 3. The scalar version, and what it costs

Here is the inner loop of `matmul_scalar` as `clang -O2` compiles it for RV64GC (unrolling
disabled so the loop is readable). The RISC-V calling convention puts `N` in `a0` and the four
pointers in `a1` through `a4`:

```asm
# void matmul_scalar(size_t N, const elem_t *A, const elem_t *B, const acc_t *D, elem_t *C)
#   a0 = N    a1 = A    a2 = B    a3 = D    a4 = C
#   in the k loop:  a5 = &A[i][k]   t6 = &B[k][j]   t5 = acc   t4 = &A[i][N] (loop end)
.k_loop:
    lb      s0, 0(a5)              # s0 = A[i][k]                one byte
    lb      s1, 0(t6)              # s1 = B[k][j]                one byte
    addi    a5, a5, 1              # &A[i][k+1]: the next column is the next byte
    mul     s0, s1, s0             # A[i][k] * B[k][j]
    addw    t5, s0, t5             # acc += product              32-bit accumulate
    add     t6, t6, a0             # &B[k+1][j]: the next row is N bytes further on
    bne     a5, t4, .k_loop        # until k == N
                                   # ... then, per (i, j): clamp t5 to [-128, 127], sb to C[i][j],
                                   #     reload acc from D, and go round the j loop
```

Reading the assembly, three things stand out:

- **Every load moves one byte.** `lb` fetches a single `int8`, and the `B` access strides by `N`
  bytes, so consecutive iterations touch consecutive *rows* of `B`, not consecutive bytes. For
  any realistically sized N, each of the 16 loads of one column lands in a different cache line.
- **Seven instructions per multiply-accumulate,** two of which do arithmetic. The other five are
  bookkeeping: two loads, two pointer updates and a branch.
- **The reduction is the loop.** The `k` loop exists only to sum 16 products into `t5`. In a
  systolic array that sum is a wire between adjacent PEs and costs no instruction at all.

For N = 16, the per-`(i, j)` epilogue (clamp, store, reload the bias) and the two outer loops add
another four thousand or so instructions on top of the 28,672 in the `k` loop. Call it 33,000.

## 4. The same tile with the Gemmini macros

Here is the N = 16 case written against `gemmini.h`. The scratchpad and accumulator are
**row-addressed** ([reference §7](Gemmini_ISA_reference.md)): one address names one row
of `DIM` elements — not one element — so a 16 × 16 tile occupies 16 consecutive
rows. (The library puts `B` at the top of the scratchpad rather than at row 16;
any free rows work.)

Four addresses are in play, and the top bits of a local address carry meaning:

```
  DRAM, host-visible                      Gemmini private memory
  virtual addresses                       32-bit local addresses

  ┌──────────────────┐                    ┌───────────────────────────────────┐
  │ A   16×16 int8   │ ─ mvin  (f7 2) ──► │ 0x0000_0000  scratchpad rows 0-15 │
  │ B   16×16 int8   │ ─ mvin2 (f7 1) ──► │ 0x0000_0010  scratchpad rows16-31 │
  │ D   16×16 int32  │ ─ mvin3 (f7 14)──► │ 0x8000_0000  accumulator rows 0-15│
  │ C   16×16 int8   │ ◄ mvout (f7 3) ─── │ 0x8000_0000  same accumulator rows│
  └──────────────────┘                    └───────────────────────────────────┘
                                             │
                    bit 31 = 1 → accumulator ┘
                    bit 30 = 1 → accumulate on write, not overwrite
                                 (0x8000_0000 | 0x4000_0000 = 0xC000_0000)
```

`D` and `C` share one accumulator address. That is deliberate, and it is the §2
trick made concrete: `mvin3` writes `D` there with bit 30 **clear** (overwrite),
`preload` names the same rows with bit 30 **set** (accumulate) so `A · B` lands
on top, and `mvout` reads the sum back with scaling and saturation applied on
the way out.

```c
#include "include/gemmini.h"

/* One DIM x DIM tile: C = A * B + D, weight-stationary. */
void tile_matmul_ws(const elem_t *A, const elem_t *B, const acc_t *D, elem_t *C) {
    const uint32_t A_sp        = 0;                                   /* scratchpad rows 0..15   */
    const uint32_t B_sp        = DIM;                                 /* scratchpad rows 16..31  */
    const uint32_t C_acc       = 1u << (ADDR_LEN - 1);                /* 0x8000_0000: acc row 0, overwrite  */
    const uint32_t C_acc_accum = C_acc | (1u << (ADDR_LEN - 2));      /* 0xC000_0000: acc row 0, accumulate */

    /* 1. configure: set once, outside any loop */
    gemmini_config_ex(WEIGHT_STATIONARY, NO_ACTIVATION, 0);                         /* dataflow, act, shift    */
    gemmini_extended3_config_ld(DIM * sizeof(elem_t), MVIN_SCALE_IDENTITY, false, 0); /* mvin  (A): 16 B stride */
    gemmini_extended3_config_ld(DIM * sizeof(elem_t), MVIN_SCALE_IDENTITY, false, 1); /* mvin2 (B): 16 B stride */
    gemmini_extended3_config_ld(DIM * sizeof(acc_t),  MVIN_SCALE_IDENTITY, false, 2); /* mvin3 (D): 64 B stride */
    gemmini_config_st(DIM * sizeof(elem_t));                                          /* mvout (C): 16 B stride */

    /* 2. move operands in: DRAM -> scratchpad / accumulator */
    gemmini_extended_mvin (A, A_sp,  DIM, DIM);                    /* 16 rows x 16 cols of A -> spad rows 0..15  */
    gemmini_extended_mvin2(B, B_sp,  DIM, DIM);                    /* 16 x 16 of B            -> spad rows 16..31 */
    gemmini_extended_mvin3(D, C_acc, DIM, DIM);                    /* 16 x 16 of D (int32)    -> acc rows 0..15   */

    /* 3. the matmul: one preload / compute pair */
    gemmini_extended_preload(B_sp, C_acc_accum, DIM, DIM, DIM, DIM);          /* weights = B; result accumulates onto D */
    gemmini_extended_compute_preloaded(A_sp, GARBAGE_ADDR, DIM, DIM, DIM, DIM); /* stream A through; no D via this path  */

    /* 4. move the result out: accumulator -> DRAM, scaled and saturated to int8 */
    gemmini_extended_mvout(C, C_acc, DIM, DIM);
    gemmini_fence();                                               /* wait for Gemmini before anyone reads C */
}
```

Set it beside the scalar function. There is no `i`, `j` or `k` loop, no `acc`, no clamp: the
reduction happens in the array, the bias add happens in the accumulator, and the saturation
happens on the way out (`acc_scale = 1.0` then clamp to `inputType`, which is exactly the
scalar epilogue). What the program says instead is *where things are*: a DRAM pointer and a
local address for each move, and two local addresses for the compute. Four kinds of statement
appear, and [the reference](Gemmini_ISA_reference.md) has a section for each:

| Call | funct7 | What it does | Reference § |
|---|---|---|---|
| `gemmini_config_ex` | 0 | Dataflow (WS), activation, output shift, strides. Latched; write-only. | ref §8 |
| `gemmini_extended3_config_ld` ×3 | 0 | DRAM stride and scale for `mvin`, `mvin2`, `mvin3` respectively (the `id` argument) | ref §8 |
| `gemmini_config_st` | 0 | DRAM stride and `acc_scale` for `mvout` | ref §8 |
| `gemmini_extended_mvin` / `mvin2` / `mvin3` | 2 / 1 / 14 | DMA a `rows × cols` block from a virtual address into a local address | ref §9 |
| `gemmini_extended_preload` | 6 | Load the stationary operand (B in WS) into the array; name where C goes | ref §10 |
| `gemmini_extended_compute_preloaded` | 4 | Stream A through the array against the preloaded B | ref §10 |
| `gemmini_extended_mvout` | 3 | DMA a `rows × cols` block from a local address to a virtual address | ref §9 |
| `gemmini_fence` | — | A plain RISC-V `fence`; Rocket holds it until Gemmini is idle | ref §12 |

Laid out as a pipeline, the eleven instructions and their dependencies:

```
  config_ex     dataflow = WS, no activation           ┐
  config_ld ×3  DRAM stride + scale for each mvin port ├─ set ONCE,
  config_st     DRAM stride + acc_scale for mvout      ┘  outside any loop
        │
        │   latched, write-only state — no readback, no save/restore
        ▼
  mvin   A ──► spad 0x0000_0000   16 B stride  ┐
  mvin2  B ──► spad 0x0000_0010   16 B stride  ├─ three DMAs, independent
  mvin3  D ──► acc  0x8000_0000   64 B stride  ┘  ports, may overlap
        │
        ▼
  preload  rs1 = B @ spad 0x10     rs2 = C @ acc 0xC000_0000  (accumulate)
        │       push weights into the array         name where C lands
        ▼
  compute  rs1 = A @ spad 0x00     rs2 = GARBAGE_ADDR
        │       stream A through                    no D on this path
        ▼
  mvout    acc 0x8000_0000 ──► C in DRAM
        │       bit 29 = 0, so scale by acc_scale then saturate to int8
        ▼
  fence    plain RISC-V fence; Rocket stalls until Gemmini's busy drops
```

Only the last instruction is a synchronization point. Everything above it is
fire-and-forget: the host issues and moves on, and the reservation station
sorts out the ordering. Nothing in this program reads a result back into a
host register.

Three details of the C worth noticing before we look underneath it:

- **The bias enters through the accumulator, not through `compute`.** `mvin3` writes `D` into
  accumulator rows 0–15 with the *overwrite* address (`0x8000_0000`). The `preload` then names C
  as `0xC000_0000`, the *accumulate* address, so `A · B` lands on top of `D`. The second operand
  of `compute` is `GARBAGE_ADDR` because in weight-stationary mode nothing arrives that way ([reference §10](Gemmini_ISA_reference.md)).
- **`D` has its own load configuration.** It is `int32`, so its DRAM row stride is 64 bytes where
  `A` and `B` have 16, and the three `mvin`s exist precisely so that A, B and D can each carry a
  different stride and scale at the same time ([reference §9](Gemmini_ISA_reference.md)). Forgetting the `id` argument configures the
  wrong one ([reference §23, item 9](Gemmini_ISA_reference.md)).
- **Every count is `DIM`.** `rows = cols = DIM` on every move and compute. For an edge tile you
  pass the true size, and the same eleven instructions handle it; that is the whole of Gemmini's
  tail handling.

## 5. What a macro actually is

None of the calls above is a function. Each is a preprocessor macro that bottoms out in one
inline-assembly statement. Here are the definitions the tile uses, verbatim from `gemmini.h`
(`dev`) and `rocc-software/src/xcustom.h`, with line breaks added:

```c
/* gemmini.h */
#define gemmini_extended_mvin(dram_addr, spad_addr, cols, rows)                                   \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, dram_addr,                                                \
      ((uint64_t)(rows) << (ADDR_LEN + 16)) | ((uint64_t)(cols) << ADDR_LEN) | (spad_addr), k_MVIN)

#define gemmini_extended_preload(BD, C, BD_cols, BD_rows, C_cols, C_rows)                         \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC,                                                           \
      ((uint64_t)(BD_rows) << (ADDR_LEN + 16)) | ((uint64_t)(BD_cols) << ADDR_LEN) | (uint64_t)(BD), \
      ((uint64_t)(C_rows)  << (ADDR_LEN + 16)) | ((uint64_t)(C_cols)  << ADDR_LEN) | (uint64_t)(C),  \
      k_PRELOAD)

#define gemmini_extended_compute_preloaded(A, BD, A_cols, A_rows, BD_cols, BD_rows)               \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC,                                                           \
      ((uint64_t)(A_rows)  << (ADDR_LEN + 16)) | ((uint64_t)(A_cols)  << ADDR_LEN) | (uint64_t)(A),  \
      ((uint64_t)(BD_rows) << (ADDR_LEN + 16)) | ((uint64_t)(BD_cols) << ADDR_LEN) | (uint64_t)(BD), \
      k_COMPUTE_PRELOADED)

#define gemmini_config_ex(dataflow, sys_act, sys_shift)                                           \
    gemmini_extended_config_ex(dataflow, sys_act, sys_shift, 1, 0, 0)         /* A_stride 1, no transposes */
/* ... -> gemmini_extended2_config_ex -> gemmini_extended3_config_ex(..., ACC_SCALE_IDENTITY, 1, ...) which emits: */
#define gemmini_extended3_config_ex(dataflow, sys_act, sys_shift, sys_acc_scale, C_stride, A_stride, A_transpose, B_transpose, set_only_strides) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC,                                                         \
      ((uint64_t)acc_scale_t_to_acc_scale_t_bits((acc_scale_t)sys_acc_scale) << 32) | ((uint64_t)(A_stride) << 16) \
        | (B_transpose << 9) | (A_transpose << 8) | ((set_only_strides) << 7) | ((sys_act) << 3) | ((dataflow) << 2) | CONFIG_EX, \
      ((uint64_t)(C_stride) << 48) | (sys_shift), k_CONFIG)

#define gemmini_fence() asm volatile("fence")

#define ROCC_INSTRUCTION_RS1_RS2(x, rs1, rs2, funct) \
  ROCC_INSTRUCTION_0_R_R(x, rs1, rs2, funct)

/* rocc-software/src/xcustom.h */
#define ROCC_INSTRUCTION_0_R_R(x, rs1, rs2, func7)                                   \
  {                                                                                  \
    asm volatile(                                                                    \
        ".insn r " STR(CAT(CUSTOM_, x)) ", " STR(0x3) ", " STR(func7) ", x0, %0, %1" \
        :                                                                            \
        : "r"(rs1), "r"(rs2));                                                       \
  }
```

Two things to see in these. First, the *shape* of every operand is decided here, in C, by the
header author: `rows` goes to bits `[63:48]`, `cols` to `[47:32]`, the local address to `[31:0]`
([reference §7](Gemmini_ISA_reference.md)), and `ADDR_LEN + 16` is just 48. Second, the only thing that reaches the assembler is one
`.insn r` directive with two `"r"` operands. `"r"(expr)` tells the compiler: evaluate this C
expression, put the result in some general-purpose register, and write that register's name here.
`%0` becomes the `rs1` field and `%1` the `rs2` field, strictly by the order the constraints are
declared. [Reference §16](Gemmini_ISA_reference.md) states the general rule.

Run the preprocessor (`clang -E`) over the `mvin` of `A` and the `preload`, and this is the whole
of what the compiler sees:

```c
{ asm volatile(".insn r " "CUSTOM_3" ", " "0x3" ", " "2" ", x0, %0, %1"
               : : "r"(A),
                   "r"(((uint64_t)(16) << (32 + 16)) | ((uint64_t)(16) << 32) | (A_sp))); };

{ asm volatile(".insn r " "CUSTOM_3" ", " "0x3" ", " "6" ", x0, %0, %1"
               : : "r"(((uint64_t)(16) << (32 + 16)) | ((uint64_t)(16) << 32) | (uint64_t)(B_sp)),
                   "r"(((uint64_t)(16) << (32 + 16)) | ((uint64_t)(16) << 32) | (uint64_t)(C_acc_accum))); };
```

`DIM` and `ADDR_LEN` have become the literals 16 and 32, `k_MVIN` and `k_PRELOAD` have become the
strings `"2"` and `"6"`, and the two operand expressions are plain 64-bit integer arithmetic. The
`mvin`'s first operand is `A` itself: a pointer is already a 64-bit value in a register and needs
no packing at all. Its second operand, and both operands of the `preload`, are shifts and ORs the
compiler must now turn into RV64I instructions. That asymmetry is the subject of [reference §18](Gemmini_ISA_reference.md).

## 6. The same function in assembly

The compiler turns `tile_matmul_ws` into 44 instructions (`clang -O2`, RV64GC): the eleven
`.insn r` lines that are the Gemmini instructions, a `fence`, a `ret`, and 31 lines of ordinary
RV64I building the 64-bit operand values that §5 left as C expressions. The listing below is
abridged to the parts discussed underneath it: the three `config_ld` and the `config_st` are
elided, and each operand build is placed next to the instruction that consumes it. The complete,
unreordered compiler output is the appendix.

Five 64-bit constants do almost all the work. Decode them once and the listing
reads straight through:

| Constant | rows | cols | local address | Meaning |
|---|---|---|---|---|
| `0x0010_0010_0000_0000` | 16 | 16 | `0x0000_0000` | scratchpad row 0 — `A` |
| `0x0010_0010_0000_0010` | 16 | 16 | `0x0000_0010` | scratchpad row 16 — `B` |
| `0x0010_0010_8000_0000` | 16 | 16 | `0x8000_0000` | accumulator row 0, **overwrite** |
| `0x0010_0010_C000_0000` | 16 | 16 | `0xC000_0000` | accumulator row 0, **accumulate** |
| `0x0010_0010_FFFF_FFFF` | 16 | 16 | `0xFFFF_FFFF` | `GARBAGE_ADDR` |

`0x0010_0010` in the top half is just `rows = 16` in `[63:48]` and `cols = 16`
in `[47:32]`, which is why every operand looks alike: **only the low 32 bits
carry the interesting information.** The two `config_ex` operands are the
exception, and they are built once:

| Constant | Field breakdown |
|---|---|
| `0x3F80_0000_0001_0004` | `acc_scale = 1.0f` `[63:32]`, `A_stride = 1` `[31:16]`, `dataflow = WS` `<<2`, selector `00` |
| `0x0001_0000_0000_0000` | `C_stride = 1` `[63:48]`, `sys_shift = 0` |

`0x3F80_0000` is the IEEE-754 single-precision encoding of `1.0f`, which is why
the compiler builds it as `127 << 23` — watch for `li a7, 127` followed by a
shift in the listing below.

```asm
# void tile_matmul_ws(const elem_t *A, const elem_t *B, const acc_t *D, elem_t *C)
#   a0 = A    a1 = B    a2 = D    a3 = C        virtual DRAM addresses, passed through untouched
tile_matmul_ws:
    # -- config_ex: WS, no activation, shift 0, acc_scale 1.0f, A_stride 1, C_stride 1
    li      a6, 1
    li      a7, 127                       # 0x7F: the top bits of 1.0f (0x3F80_0000 = 0x7F << 23)
    slli    a6, a6, 48                    # a6 = 0x0001_0000_0000_0000      rs2: C_stride = 1 [63:48], sys_shift = 0
    slli    a5, a7, 39                    # a5 = 0x0000_3F80_0000_0000
    addi    a5, a5, 1                     #      0x0000_3F80_0000_0001
    slli    a5, a5, 16                    #      0x3F80_0000_0001_0000      acc_scale = 1.0f [63:32], A_stride = 1 [31:16]
    addi    a5, a5, 4                     #      0x3F80_0000_0001_0004      | dataflow = WS << 2 | CONFIG_EX
    .insn r CUSTOM_3, 0x3, 0, x0, a5, a6  # funct7 = 0   k_CONFIG            rs1[1:0] = 00 -> config_ex
    ...                                   # config_ld x3 and config_st: 4 more .insn, 15 more RV64I  (appendix)
    # -- the packed rs2 triples, built once and shared between instructions
    lui     t0, 0x10001
    slli    t0, t0, 24                    # t0 = 0x0010_0010_0000_0000      rows = 16 [63:48], cols = 16 [47:32], spad row 0
    lui     t1, 0x200
    addiw   a4, t1, 0x21
    slli    a4, a4, 31                    # a4 = 0x0010_0010_8000_0000      16 x 16 @ acc row 0, bit 31 = accumulator, bit 30 = 0 overwrite
    lui     a6, 0x400
    addiw   a5, a6, 0x43
    slli    a5, a5, 30                    # a5 = 0x0010_0010_C000_0000      16 x 16 @ acc row 0, bit 31 | bit 30 = accumulate
    lui     a7, 0x100
    addi    a7, a7, 0x11
    slli    a7, a7, 32                    # a7 = 0x0010_0011_0000_0000
    addi    a7, a7, -1                    #      0x0010_0010_FFFF_FFFF      16 x 16 @ GARBAGE_ADDR   (0x11_0000_0000 - 1 borrows down)
    # -- move in, compute, move out
    .insn r CUSTOM_3, 0x3, 2, x0, a0, t0  # funct7 = 2   k_MVIN     rs1 = A (bare pointer), rs2 = 16 x 16 @ spad 0x0
    addi    a0, t0, 16                    # a0 = 0x0010_0010_0000_0010      16 x 16 @ spad row 16      (A's pointer is no longer needed)
    .insn r CUSTOM_3, 0x3, 1, x0, a1, a0  # funct7 = 1   k_MVIN2    rs1 = B, rs2 = 16 x 16 @ spad 0x10
    .insn r CUSTOM_3, 0x3, 14, x0, a2, a4 # funct7 = 14  k_MVIN3    rs1 = D, rs2 = 16 x 16 @ acc 0x8000_0000
    .insn r CUSTOM_3, 0x3, 6, x0, a0, a5  # funct7 = 6   k_PRELOAD  rs1 = B @ spad 0x10 (weights), rs2 = C @ acc 0xC000_0000 (accumulate onto D)
    .insn r CUSTOM_3, 0x3, 4, x0, t0, a7  # funct7 = 4   k_COMPUTE_PRELOADED   rs1 = A @ spad 0x0, rs2 = GARBAGE (no D on this path)
    .insn r CUSTOM_3, 0x3, 3, x0, a3, a4  # funct7 = 3   k_MVOUT    rs1 = C, rs2 = 16 x 16 @ acc 0x8000_0000 (bit 29 = 0: scaled inputType read)
    fence                                 # Rocket stalls here until Gemmini is idle; C is now safe to read
    ret
```

Reading the assembly, three things stand out:

- **Every Gemmini instruction has the same shape.** Eleven `.insn r CUSTOM_3, 0x3, F, x0, rs1, rs2`
  lines, and the only things that vary are `F`, the `funct7` that selects the operation
  ([reference appendix A](Gemmini_ISA_reference.md)), and two register *numbers*. There is no immediate, no address, no size in the
  instruction word. All of that is in the two registers it names, and Gemmini slices it out (§7.3).
- **The rest is constant-building.** In the full listing, thirty-one RV64I instructions serve
  eleven Gemmini instructions, and not one of them computes anything about the matrices: they are
  `lui`, `slli`, `addi` and `or` assembling 64-bit bit-fields. The compiler is clever about it.
  The triple `rows = 16, cols = 16, address 0` is built once in `t0` and reused for the `mvin` of
  A and the `compute`; `t0 + 16` serves both `mvin2` and `preload`; the accumulator address `a4`
  serves both `mvin3` and `mvout`; and `GARBAGE_ADDR` arrives as `0x0010_0011_0000_0000 - 1`. In
  straight-line code with compile-time `DIM` this sharing is free. Inside a real tiling loop,
  where every scratchpad address is `base + (i*K + k)*DIM` and edge tiles have run-time
  `rows`/`cols`, it disappears, and each `preload`/`compute` pair costs 12–20 host instructions
  ([reference §18](Gemmini_ISA_reference.md)).
- **The host never touches the data.** There is no `lb`, no `mul`, no `sb`. The pointers `a0`–`a3`
  pass into `rs1` fields untouched, Gemmini's own DMA and TLB fetch through them ([reference §5](Gemmini_ISA_reference.md)), and the
  `fence` is the only point where the host waits.

Here is one of those words as the assembler encodes it, the `mvin` of A, so that §7.3's picture has
a concrete instance. `a0` is `x10` and `t0` is `x5`:

```
.insn r CUSTOM_3, 0x3, 2, x0, a0, t0      ->   0x0455307B

 31        25 24    20 19    15 14  12 11    7 6      0
+------------+--------+--------+------+-------+--------+
| 0000010    | 00101  | 01010  | 011  | 00000 | 1111011|
| funct7 = 2 | rs2=x5 | rs1=x10| xs1, | rd=x0 |  0x7B  |
| k_MVIN     |  (t0)  |  (a0)  | xs2  |       | custom3|
+------------+--------+--------+------+-------+--------+
```

`llvm-objdump` prints this word as `<unknown>`. Nothing in the RISC-V toolchain knows what it
means; only Gemmini's decoder does, and what it decodes is `funct7` plus two register numbers
whose *contents* are a pointer (`0x0000_0000_8FF3_2100`, if A lives where the reference's examples put it) and `0x0010_0010_0000_0000` (rows, cols, local address). §7 follows those two
values from the register file into the accelerator.

Count what was executed for the 16 × 16 × 16 tile:

| | Scalar loop | Gemmini tile |
|---|---|---|
| Dynamic instructions | ~33,000 | 44 |
| of which move or compute data | 8,192 `lb` + 256 `lw` + 256 `sb` + 8,192 `mul`/`addw` | 4 DMAs + 1 preload/compute pair |
| of which are bookkeeping | ~16,000 (pointers, counters, branches, clamps) | 31 RV64I operand builds + 1 fence + 1 ret |
| Host instructions per multiply-accumulate | 7 | 0.01 |
| Bytes per load | 1 | 256 per `mvin` (16 rows × 16 B), 1,024 for D |

Thirty-one of the forty-four instructions are packing cost. [Reference §18](Gemmini_ISA_reference.md) returns to that cost in the
general case, and it is why the CISC loop instructions exist: for a 64 × 64 problem the tile
above becomes 64 `preload`/`compute` pairs plus their moves, on the order of a thousand host
instructions hand-tiled, or six instructions with `gemmini_loop_ws` ([reference §11](Gemmini_ISA_reference.md)). A
worked trace of the same tile with the library's addresses is [reference §14](Gemmini_ISA_reference.md).

> **Mental model so far.** A Gemmini program is a stream of ordinary RISC-V instructions, each a
> 32-bit `custom3` word that names an operation and two registers. The registers hold 64-bit
> values that the header's macros pack from C expressions: a DRAM pointer travels as itself, while
> a local operand is `rows | cols | local address`, and the local address itself carries the
> scratchpad-versus-accumulator and overwrite-versus-accumulate choices in its top bits ([reference §7](Gemmini_ISA_reference.md)).
> The matmul is one `preload`/`compute` pair per `DIM × DIM × DIM` tile; the reduction over `k`
> happens inside the array; bias, scaling and activation happen in the accumulator on the way in
> and out. `DIM` is fixed by the hardware and known to the program, but every instruction carries
> its own `rows`/`cols`, so edge tiles need no second code path. The host's only real cost is
> building operands, and Gemmini's two throughput mechanisms, decoupled execution and the CISC
> loops, both exist to hide it.

---

---

## 7. Where the instruction and its operands live

§1–§6 followed one 16 × 16 tile from C source to the assembler's output. This section
explains the mechanism behind each of those `.insn` lines: where the instruction lives, where its operands
live, and how they reach the accelerator.

### 7.1 The short version

Three things, three places. Everything else in this section elaborates these:

1. **The instruction** is a 32-bit word in `.text`, fetched through the I-cache like any RISC-V
   instruction.
2. **The operands** are 64-bit values in the CPU's integer register file. The instruction carries
   only 5-bit register *numbers*.
3. **Gemmini has no architectural registers.** The only Gemmini state software can name is SRAM,
   addressed by a 32-bit value that lives *inside* one of those 64-bit registers.

That third point resolves the puzzle the ISA tables create: the instruction is 32 bits, the local
address is *also* 32 bits, and they seem not to fit. They don't need to — they sit at different
depths.

### 7.2 The five stages

```
 C source  ->  compiler  ->  .text  ->  CPU regfile  ->  RoCC port  ->  Gemmini
 (a macro)     (packs        (one      (two 64-bit      (delivers       (slices
               operands)     custom3    values)          values)         fields)
                             word)
```

| Stage | What happens |
|---|---|
| **C source** | One macro call, e.g. `gemmini_extended_mvin(...)` |
| **Compiler** | Expands the macro to inline asm with two `"r"` operands; emits plain RV64I to compute them |
| **`.text`** | One 32-bit `custom3` instruction naming two registers |
| **CPU regfile** | Holds the two 64-bit operand values |
| **RoCC port** | Rocket reads the regfile and sends a `RoCCCommand` bundle of *values* |
| **Gemmini** | Decodes `funct7`, slices the 64-bit operands into fields |

### 7.3 Zooming in

Follow one `mvin` down through the levels. Each box is a field of the box above it:

```
 [1] custom3 instruction — 32 bits, lives in .text
     +---------------+-----------+-----------+-------+-----------+--------------+
     |  funct7 = 2   | rs2 = x13 | rs1 = x12 |  011  |  rd = x0  |   custom3    |
     +---------------+-----------+-----------+-------+-----------+--------------+
                      \_____________________/
                        5-bit register NUMBERS, not values
                                 |
                                 |  Rocket decodes, then reads the register file
                                 v
 [2] integer register file — the 64-bit VALUES, in the CPU
     x12 = 0x0000_0000_8FF3_2100   <- rs1   bare pointer, nothing packed
     x13 = 0x0010_0010_8000_0040   <- rs2   packed triple, expanded below
                                 |
                                 v
 [3] x13 contents — 64 bits, built by compiler-emitted RV64I
     +------------------+------------------+------------------------------------+
     |    rows = 16     |    cols = 16     |    local address = 0x8000_0040     |
     |     [63:48]      |     [47:32]      |               [31:0]               |
     +------------------+------------------+------------------------------------+
                                                              |
                                                              v
 [4] local address — 32 bits, a FIELD inside x13, not a register
     +------+------+------+-----------------------------------------------------+
     | acc  | ovr  |  rd  |                   row index 0x40                    |
     | [31] | [30] | [29] |                       [28:0]                        |
     +------+------+------+-----------------------------------------------------+
```

The instruction (top) contains `rs2 = x13` — a *register number*. `x13` (second row) holds a 64-bit
*value*. That value decomposes into rows, cols, and a 32-bit local address (third row). And that
local address itself decomposes into three flag bits plus a row index (bottom row).

So the two 32-bit things never coexist: one is a word in memory, the other is a bit field inside a
register. Note also `x12` on the right — it needed no packing at all, because it is already a
pointer. [Reference §18](Gemmini_ISA_reference.md) covers why some operands are free and others are not.

---

---

## 8. Past one tile

Everything so far was one `DIM × DIM × DIM` tile. Real matrices are bigger, and
the array still only does 16 × 16. So you tile: split `C = A · B + D` into
`I × J` output tiles, each summed over `K` steps of the `k` dimension.

For a 64 × 64 matmul with `DIM = 16`, that is `I = J = K = 4`, so **64
`preload`/`compute` pairs** plus their moves. Three things change, and each one
costs:

**1. Addresses stop being constants.** In §6 the compiler built
`0x0010_0010_0000_0000` once and reused it for both the `mvin` and the
`compute`. In a loop, the scratchpad address is `base + (i*K + k)*DIM` — a
multiply and an add per operand, per instruction, and the shift-and-OR to pack
it back into bits `[31:0]`. What was free becomes 12–20 host instructions per
pair ([reference §18](Gemmini_ISA_reference.md)).

**2. Edge tiles.** If the matrix is not a multiple of `DIM`, the last tile in
each direction is short. This costs nothing structurally — you pass the true
`rows`/`cols` and the same instructions handle it, which is the point made at
the end of §1 — but the counts become runtime values, so they too must be
packed rather than folded into a constant.

**3. Accumulation across `k`.** The `k = 0` tile writes `C` with bit 30 clear
(overwrite); every `k > 0` tile writes the same accumulator rows with bit 30 set
(accumulate). And since `B` is resident across the inner loop, subsequent
`preload`s pass `GARBAGE_ADDR` to keep the weights in place. Both are *address*
changes; no opcode changes. The library's real loop
(`sp_tiled_matmul_ws` in `gemmini.h`) is reproduced in
[reference §10](Gemmini_ISA_reference.md).

### The CISC escape hatch

Those 64 pairs plus their address arithmetic run to roughly a thousand host
instructions — for a matmul the array itself could retire in a few hundred
cycles. That imbalance is what `loop_ws` exists to fix:

```
  loop_ws_config_bounds    I, J, K and padding
  loop_ws_config_addrs_AB  &A, &B
  loop_ws_config_addrs_DC  &D, &C
  loop_ws_config_strides_AB
  loop_ws_config_strides_DC
  loop_ws                  ← trigger: run the whole tiled matmul
```

Six instructions, and hardware does the tiling — `LoopMatmul.scala` generates
the same `mvin`/`preload`/`compute`/`mvout` stream, double-buffers the tiles,
and throttles itself against the reservation station's occupancy so loads and
matmuls overlap. It adds no expressive power; it removes host instruction
traffic and schedules better than most hand-written loops.

The five `config` instructions latch into registers and the sixth fires. They
must be issued as an uninterrupted group — there is no tag, just one latched
register set ([reference §11](Gemmini_ISA_reference.md)).

### Where to go next

| You want | Go to |
|---|---|
| The encoding of any instruction here | [reference §8–§12](Gemmini_ISA_reference.md) |
| The full WS tiling loop as the library writes it | [reference §10](Gemmini_ISA_reference.md) |
| The loop instructions in detail | [reference §11](Gemmini_ISA_reference.md) |
| What the packing actually costs | [reference §18](Gemmini_ISA_reference.md) |
| Traps, in a list | [reference §23](Gemmini_ISA_reference.md) |
| The hardware underneath | upstream [`README.md`](../../generators/gemmini/README.md) |

## Appendix — `tile_matmul_ws`, complete assembly

The unabridged `clang -O2` output for the function of §4, in the compiler's own order, with
comments added. §6 discusses it.

```asm
# void tile_matmul_ws(const elem_t *A, const elem_t *B, const acc_t *D, elem_t *C)
#   a0 = A    a1 = B    a2 = D    a3 = C        virtual DRAM addresses, passed through untouched
tile_matmul_ws:
    # -- config_ex: WS, no activation, shift 0, acc_scale 1.0f, A_stride 1, C_stride 1
    li      a6, 1
    li      a7, 127                       # 0x7F: the top bits of 1.0f (0x3F80_0000 = 0x7F << 23); reused five times
    li      t0, 16                        # DIM; also the 16-byte DRAM row stride of A and B
    slli    a6, a6, 48                    # a6 = 0x0001_0000_0000_0000      rs2: C_stride = 1 in [63:48], sys_shift = 0
    slli    a5, a7, 39                    # a5 = 0x0000_3F80_0000_0000
    addi    a5, a5, 1                     #      0x0000_3F80_0000_0001
    slli    a5, a5, 16                    #      0x3F80_0000_0001_0000      acc_scale = 1.0f [63:32], A_stride = 1 [31:16]
    addi    a5, a5, 4                     #      0x3F80_0000_0001_0004      | dataflow = WS << 2 | CONFIG_EX
    .insn r CUSTOM_3, 0x3, 0, x0, a5, a6  # funct7 = 0   k_CONFIG            rs1[1:0] = 00 -> config_ex
    # -- config_ld x3: scale 1.0f, block_mvin_stride = DIM, pixel_repeats = 1, id = 0 / 1 / 2
    slli    a5, a7, 35                    # a5 = 0x0000_003F_8000_0000
    addi    a5, a5, 1                     #      0x0000_003F_8000_0001
    slli    a5, a5, 20                    #      0x3F80_0000_0010_0000      scale = 1.0f [63:32], block_mvin_stride = 16 [31:16]
    addi    a4, a5, 0x101                 # a4 = 0x3F80_0000_0010_0101      | pixel_repeats = 1 << 8 | id = 0 << 3 | CONFIG_LD
    .insn r CUSTOM_3, 0x3, 0, x0, a4, t0  # funct7 = 0   k_CONFIG   id 0 (mvin,  A)   rs2 = DRAM stride 16 B
    addi    a4, a5, 0x109                 # a4 = 0x3F80_0000_0010_0109      id = 1 << 3
    .insn r CUSTOM_3, 0x3, 0, x0, a4, t0  # funct7 = 0   k_CONFIG   id 1 (mvin2, B)   rs2 = DRAM stride 16 B
    li      a4, 64                        # DRAM row stride of D: 16 x int32 = 64 B
    li      a6, 2                         # rs1 of config_st: only the selector, CONFIG_ST = 2
    addi    a5, a5, 0x111                 # a5 = 0x3F80_0000_0010_0111      id = 2 << 3
    .insn r CUSTOM_3, 0x3, 0, x0, a5, a4  # funct7 = 0   k_CONFIG   id 2 (mvin3, D)   rs2 = DRAM stride 64 B
    # -- config_st: DRAM stride 16, acc_scale 1.0f, no activation, no pooling
    lui     t0, 0x10001                   # t0 = 0x0000_0000_1000_1000      (first half of the shared rows/cols constant)
    lui     t1, 0x200                     # t1 = 0x0000_0000_0020_0000      (first half of the accumulator address)
    slli    a7, a7, 55                    # a7 = 0x3F80_0000_0000_0000      acc_scale = 1.0f [63:32]
    addi    a7, a7, 16                    #      0x3F80_0000_0000_0010      | stride = 16 [31:0]
    .insn r CUSTOM_3, 0x3, 0, x0, a6, a7  # funct7 = 0   k_CONFIG            rs1[1:0] = 10 -> config_st
    # -- the packed rs2 triples, built once and shared between instructions
    lui     a6, 0x400                     # a6 = 0x0000_0000_0040_0000
    lui     a7, 0x100                     # a7 = 0x0000_0000_0010_0000
    slli    t0, t0, 24                    # t0 = 0x0010_0010_0000_0000      rows = 16 [63:48], cols = 16 [47:32], spad row 0
    addiw   a4, t1, 0x21                  # a4 = 0x0000_0000_0020_0021
    addiw   a5, a6, 0x43                  # a5 = 0x0000_0000_0040_0043
    addi    a7, a7, 0x11                  # a7 = 0x0000_0000_0010_0011
    # -- mvin A -> scratchpad rows 0..15
    .insn r CUSTOM_3, 0x3, 2, x0, a0, t0  # funct7 = 2   k_MVIN     rs1 = A (bare pointer), rs2 = 16 x 16 @ spad 0x0
    addi    a0, t0, 16                    # a0 = 0x0010_0010_0000_0010      16 x 16 @ spad row 16      (a0 is free now)
    slli    a4, a4, 31                    # a4 = 0x0010_0010_8000_0000      16 x 16 @ acc row 0, bit 31 = accumulator, bit 30 = 0 overwrite
    slli    a5, a5, 30                    # a5 = 0x0010_0010_C000_0000      16 x 16 @ acc row 0, bit 31 | bit 30 = accumulate
    slli    a7, a7, 32                    # a7 = 0x0010_0011_0000_0000
    addi    a7, a7, -1                    #      0x0010_0010_FFFF_FFFF      16 x 16 @ GARBAGE_ADDR   (0x11_0000_0000 - 1 borrows down)
    # -- mvin2 B -> scratchpad rows 16..31;  mvin3 D -> accumulator rows 0..15, overwrite
    .insn r CUSTOM_3, 0x3, 1, x0, a1, a0  # funct7 = 1   k_MVIN2    rs1 = B, rs2 = 16 x 16 @ spad 0x10
    .insn r CUSTOM_3, 0x3, 14, x0, a2, a4 # funct7 = 14  k_MVIN3    rs1 = D, rs2 = 16 x 16 @ acc 0x8000_0000
    # -- the matmul: one preload / compute pair
    .insn r CUSTOM_3, 0x3, 6, x0, a0, a5  # funct7 = 6   k_PRELOAD  rs1 = B @ spad 0x10 (weights), rs2 = C @ acc 0xC000_0000 (accumulate onto D)
    .insn r CUSTOM_3, 0x3, 4, x0, t0, a7  # funct7 = 4   k_COMPUTE_PRELOADED   rs1 = A @ spad 0x0, rs2 = GARBAGE (no D on this path)
    # -- mvout C <- accumulator rows 0..15, scaled by acc_scale and saturated to int8
    .insn r CUSTOM_3, 0x3, 3, x0, a3, a4  # funct7 = 3   k_MVOUT    rs1 = C, rs2 = 16 x 16 @ acc 0x8000_0000 (bit 29 = 0: scaled inputType read)
    fence                                 # Rocket stalls here until Gemmini is idle; C is now safe to read
    ret
```

The compiler actually prints `.insn r 123, 3, 0, zero, a5, a6`: `CUSTOM_3` is opcode 123 = `0x7B`
and `x0` is `zero`. The symbolic form is used above so the fields can be read. Compressed forms of
the RV64I instructions (`c.li`, `c.slli`, `c.addi`) are 16 bits each; the `.insn r` words are
always 32 bits.

The RV64I instructions doing the packing, for readers new to RISC-V:

| Instruction | Meaning |
|---|---|
| `li rd, imm` | load immediate (pseudo-instruction; expands to `lui` + `addi` or a single `addi`) |
| `lui rd, imm` | load upper immediate — 20-bit immediate at bits `[31:12]`, zeros below, sign-extended to 64 |
| `addi` / `addiw rd, rs, imm` | add 12-bit signed immediate (`addiw`: 32-bit result, sign-extended) |
| `slli rd, rs, sh` | shift left logical immediate |
| `or rd, rs1, rs2` | bitwise OR |
