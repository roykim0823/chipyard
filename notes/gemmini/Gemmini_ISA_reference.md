# Gemmini ISA reference

The normative reference for the Gemmini accelerator's instruction set, built
from the bottom: what RISC-V reserves, what Berkeley's RoCC interface layers on
top of that reservation, what Gemmini puts inside its slice of the encoding
space, and what the C toolchain and the RoCC hardware contract underneath it
guarantee.

This is document 2 of 2. Read [`Gemmini_ISA_tutorial.md`](Gemmini_ISA_tutorial.md)
first for one matmul walked from C to the encoded instruction word; this
document assumes it and does not repeat it. For the hardware underneath, read
the upstream [`README.md`](../../generators/gemmini/README.md).

**Provenance.** rocket-chip `tile/LazyRoCC.scala`, `rocket/IDecode.scala`,
`rocket/CustomInstructions.scala`, `rocket/RocketCore.scala`;
`ucb-bar/gemmini` `GemminiISA.scala`, `Configs.scala`, `Controller.scala` and
README; `ucb-bar/gemmini-rocc-tests` `include/gemmini.h` (**dev** branch);
`ibm/rocc-software` `src/xcustom.h`. §17 gives in-tree paths for the three
headers. Assembly listings in §18 are from GCC 13.2.0 in the Chipyard
`riscv-tools` environment.

> **Version warning.** RoCC is not a ratified RISC-V standard and carries no
> version number; Gemmini's ISA is defined by its own source tree. Both have
> changed across releases. Treat this document as a map, and read
> `LazyRoCC.scala` / `GemminiISA.scala` at your pinned commit before encoding
> anything by hand. Record that commit hash in any paper or internal spec.

> **Branch warning.** The `master` head of `gemmini-rocc-tests` carries a
> superseded CISC scheme (`k_CONFIG_CISC_EX`, `k_ADDR_AB`, `k_SIZE0`,
> `k_COMPUTE_CISC`) with no `mvin2`/`mvin3`, no conv loop, and no `CONFIG_BERT`.
> The `dev` head matches the README. Everything below is `dev`.

**Reference configuration assumed throughout:** `DIM = 16`, `ADDR_LEN = 32`,
`BANK_NUM = 4`, `BANK_ROWS = 4096`, `ACC_ROWS = 1024`, `elem_t = int8`,
`acc_t = int32`, `acc_scale_t = float32`.

**How to read this.** The document is top-down within a bottom-up frame.
**Part I** is the whole idea, in order: the opcodes RISC-V reserves, what RoCC
does with them, the shape of one instruction, what its operands really are, and
the four rules that govern all Gemmini code — read it start to finish. **Part
II** finishes the encoding and **Part III** is the instruction-by-instruction
reference, closing with a complete matmul (§14) that puts every one of them to
work; together they are what you come back to. **Part IV** is the C
toolchain that emits the instructions and **Part V** the RoCC hardware contract
underneath them — needed to integrate or modify an accelerator, not to program
one. **Part VI** is practice: elaboration-time options, accumulated traps,
sources, and two **appendices** hold the exhaustive tables — the complete
`funct7` map, and what Rocket's decoder does with a custom opcode. Sections are
numbered `§1`–`§24` continuously; the parts are grouping only.

---

## Part I — The big picture

### 1. What RISC-V actually reserves

All 32-bit RISC-V instructions have `inst[1:0] = 11`; the major opcode is
`inst[6:2]`. Four slots in the base opcode map are set aside for non-standard
extensions, and rocket-chip names them in `OpcodeSet`:

| Slot | `inst[6:2]` | 7-bit opcode | Hex | `OpcodeSet` |
|---|---|---|---|---|
| custom-0 | `00010` | `0001011` | `0x0B` | `custom0` |
| custom-1 | `01010` | `0101011` | `0x2B` | `custom1` |
| custom-2 / rv128 | `10110` | `1011011` | `0x5B` | `custom2` |
| custom-3 / rv128 | `11110` | `1111011` | `0x7B` | `custom3` |

That reservation is the **entire** extent of what the RISC-V specification says.
It guarantees no ratified extension will ever claim those opcodes, and says
nothing whatsoever about the remaining 25 bits.

The `/rv128` annotation on the last two means they are available as custom
opcodes on RV32 and RV64, and only earmarked for reclamation if RV128 ever
materializes. RV128 is unratified and unimplemented; Rocket is RV64. The
annotation is a portability footnote, not a restriction.

### 2. What Gemmini is, and what RoCC is

Gemmini is **not** a RISC-V extension. It is a *RoCC accelerator* — a separate
module bolted to the side of a Rocket tile and reached through one of the four
custom opcodes above. RoCC is one particular way of filling the 25 bits that
reservation leaves undefined, so **everything from here on is Berkeley
convention, not RISC-V.**

It is Berkeley's interface for bolting a *tightly-coupled* accelerator onto a
Rocket (or BOOM) core. The core keeps ownership of instruction fetch and decode:
an instruction carrying one of those four custom opcodes is decoded and
issued like any other, but instead of executing in the core's ALU it is handed
to the attached accelerator over a request/response port — the `funct7` field,
the `rs1`/`rs2` register *values*, and a tag for the eventual `rd` writeback.
The accelerator is a block of logic sitting inside the tile beside the pipeline,
running decoupled from it, optionally with its own port into the memory system
(L1 D$ or L2, plus a TLB/page-table walker so it can use virtual addresses), and
returns a result the core writes back to the register file.

The practical consequences: no driver, no MMIO doorbell, no interrupt — an
accelerator call is a single instruction with ~few-cycle issue latency, and it
shares the calling thread's address space and privilege level. The cost is that
the accelerator is welded to a specific core, and the whole thing is a Berkeley
convention that no RISC-V standard describes. It is defined in
`generators/rocket-chip/src/main/scala/tile/LazyRoCC.scala`.

#### The contract, stated exactly

RoCC gives an accelerator exactly two things:

1. **A slice of the RISC-V opcode space** — one of the four reserved `custom`
   opcodes.
2. **A decoupled bundle interface** — Rocket decodes the instruction, reads the
   register file, and hands over a `RoCCCommand` carrying *values*. Optionally
   the accelerator returns a `RoCCResponse` (§19 has the bundles).

That is the whole contract. RoCC deliberately does not supply architectural
registers, an accelerator-visible exception model, or any state the OS can name
— §21 collects what that costs.

The division of labour explains nearly every awkwardness in Gemmini's ISA:

> **Rocket owns fetch, decode, register read, and retirement. The accelerator
> owns everything after.** The accelerator never sees a PC, never fetches, and
> cannot raise a precise trap.

So an accelerator ISA designed on RoCC has only two degrees of freedom: which
bits of the 32-bit word it gets to use (§3), and what it packs into the two
64-bit operand values (§7).

#### The opcode is a routing key

A tile may host several accelerators. `RoccCommandRouter` broadcasts each
command to all of them and lets the one whose `OpcodeSet` matches accept it:

```scala
val cmd = Queue(io.in)
val cmdReadys = io.out.zip(opcodes).map { case (out, opcode) =>
  val me = opcode.matches(cmd.bits.inst.opcode)
  out.valid := cmd.valid && me
  out.bits  := cmd.bits
  out.ready && me
}
assert(PopCount(cmdReadys) <= 1.U,
  "Custom opcode matched for more than one accelerator")
```

Hence **at most one accelerator per custom opcode**, and at most four RoCC
accelerators per tile.

### 3. The RoCC instruction format

RoCC adopts the standard R-type layout rather than inventing one. This is what
lets a stock assembler emit RoCC instructions and lets Rocket's existing decoder
extract register numbers without special cases.

RoCC keeps `rd`, `rs1`, `rs2` with their normal meanings, takes `funct7` as the
accelerator's own opcode space (128 instructions per custom opcode), and splits
`funct3` into three single-bit flags:

```
 31        25 24   20 19   15 14      12 11    7 6        0
+------------+-------+-------+----------+-------+----------+
|   funct7   |  rs2  |  rs1  |  funct3  |  rd   |  opcode  |   RISC-V R-type
+------------+-------+-------+----------+-------+----------+
       7          5       5       3         5        7

============================================================

 31        25 24   20 19   15 14  13  12 11    7 6        0
+------------+-------+-------+--+---+---+-------+----------+
|   funct7   |  rs2# |  rs1# |xd|xs1|xs2|  rd#  |  opcode  |   RoCC
+------------+-------+-------+--+---+---+-------+----------+
   accel op     5-bit register            5-bit    custom-0..3
                             \_________/
                               funct3
```

```scala
class RoCCInstruction extends Bundle {
  val funct  = Bits(7.W)   // [31:25]  passed through, NOT decoded by Rocket
  val rs2    = Bits(5.W)   // [24:20]
  val rs1    = Bits(5.W)   // [19:15]
  val xd     = Bool()      // [14]
  val xs1    = Bool()      // [13]
  val xs2    = Bool()      // [12]
  val rd     = Bits(5.W)   // [11:7]
  val opcode = Bits(7.W)   // [6:0]
}
```

**`funct3` is not a function selector.** Those three bits are addressed to the
**core**, not to the accelerator — they are register-port enables:

| Bit | Position | Meaning |
|---|---|---|
| `xd` | `inst[14]` | A writeback to `rd` is coming. The core reserves `rd` in its scoreboard and interlocks any dependent instruction until the response arrives. |
| `xs1` | `inst[13]` | Read `rs1` and deliver the `xLen`-bit value with the command. |
| `xs2` | `inst[12]` | Same for `rs2`. |

`xd` is the most consequential bit in the interface:

- `xd = 0` — fire and forget. The core retires the instruction immediately and
  runs ahead; the accelerator executes decoupled.
- `xd = 1` — a blocking call. The core stalls any consumer of `rd`.

Gemmini issues essentially everything with `xd = 0`. That is precisely why
Gemmini work overlaps with CPU execution.

### 4. One instruction, four levels

Three things, three places:

1. **The instruction** is a 32-bit word in `.text`, fetched through the I-cache
   like any RISC-V instruction.
2. **The operands** are 64-bit values in the CPU's integer register file. The
   instruction carries only 5-bit register *numbers* — RoCC delivers the
   *values*.
3. **The Gemmini-local address is a bit field inside one of those values.** Not
   part of the instruction, and not a register of its own. It cannot be
   otherwise: RoCC gives Gemmini no architectural registers to name (§5).

Point 3 is where most confusion starts — the instruction is 32 bits, the local
address is *also* 32 bits, and they appear not to fit. They sit at different
depths, which the diagram below draws.

#### One `mvin`, level by level

Follow one `mvin` down through the levels. Each box is a field of the box above
it:

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

The instruction (top) contains `rs2 = x13` — a *register number*. `x13` (second
row) holds a 64-bit *value*. That value decomposes into rows, cols, and a 32-bit
local address (third row). And that local address itself decomposes into three
flag bits plus a row index (bottom row).

So the two 32-bit things never coexist: one is a word in memory, the other is a
bit field inside a register. Note also `x12` — it needed no packing at all,
because it is already a pointer. §7 covers why some operands are free and
others are not.

### 5. How the core and the accelerator share the work

Four properties of the RoCC contract shape every line of Gemmini code — they
are why the worked example in §14 looks the way it does. This is the level you
need in order to write and read that code; Part V has the RTL behind each.

**Commands arrive at writeback, in order, never early.** Rocket hands over the
command when the instruction reaches writeback, so it is non-speculative and in
program order. The accelerator cannot see a command early, and cannot see one
out of order (§20).

**Almost everything is fire-and-forget.** `xd = 0` says no writeback to `rd` is
coming, so the core retires the instruction immediately and runs on while the
accelerator works. Every Gemmini instruction but `counter_access` is issued this
way — which is precisely why Gemmini work overlaps with CPU execution, and why
the host can run far ahead queueing operations (§3 for the bit itself, §12 for
the one exception).

**`fence` is the entire synchronization mechanism.** The accelerator raises
`busy` while any issued work is outstanding, and a plain RISC-V `fence` stalls
in decode until it drops. There is no completion interrupt, no status register
to poll, no custom sync instruction: `gemmini_fence()` is literally
`asm volatile("fence")`. Anything that reads a Gemmini result must be fenced
against it (§12, §20).

**The accelerator runs in the calling thread's address space** — the subsection
below.

And two things the contract does *not* provide, which shape the ISA more than
anything it does:

- **No architectural registers.** Nothing inside the accelerator can be named by
  a register number. That is why a scratchpad row is addressed by a *value*
  packed into a 64-bit operand (§4) instead of by a register field, and why
  configuration is write-only latched state rather than something you can read
  back.
- **No precise traps, and no save/restore.** The accelerator has no PC and
  cannot fault an instruction; its configuration cannot be read out, so it
  cannot be context-switched (§21).

#### Why the operands hold *virtual* addresses

Gemmini's DMAs operate on virtual addresses. Each has a private TLB
(`FrontendTLB`), and on a miss it falls back to a page-table walker **shared
with the host CPU** through the RoCC `ptw` port. The `status` field of
`RoCCCommand` carries the `MStatus` captured at issue, giving that TLB the
privilege context it needs for permission checks.

This is what makes Gemmini pleasant to program: userspace hands it plain
`malloc`'d pointers. No kernel driver, no `dma-buf`, no pinning, no software
address translation. And because rocket-chip's TLB passes VA through unchanged
in Bare mode (`satp.MODE = 0`), the identical instruction stream runs bare-metal
and under Linux with no mode bit.

It is also the deepest reason the interface passes *values*: a register number
would be useless to a DMA engine, but a translated 64-bit pointer plus the
issuing `MStatus` is exactly enough to walk the host's page tables on the
accelerator's behalf.

---

## Part II — The encoding

### 6. Gemmini's slice of the encoding

Gemmini claims **one** opcode, `custom-3` by default:

```scala
case class GemminiArrayConfig[T <: Data, U <: Data, V <: Data](
  opcodes: OpcodeSet = OpcodeSet.custom3,   // -> #define XCUSTOM_ACC 3
  ...
)
```

It is a config field, so it can be moved. Multi-accelerator setups — two Gemmini
instances, or Gemmini alongside another RoCC unit — assign a different opcode
per instance. The hardware's opcode and the C header's `XCUSTOM_ACC` cannot
disagree, because both are emitted from this one field (§17).

`custom-3` avoids collision with the tutorial accelerators and rocket-chip
examples, which habitually grab `custom0`. One opcode suffices because `funct7`
provides 128 slots and Gemmini uses roughly 30; it economizes further by making
`funct7 = 0` a *family* that dispatches on `rs1[1:0]`.

#### The encoding invariant

Nearly every Gemmini instruction is `ROCC_INSTRUCTION_RS1_RS2` → `xd=0, xs1=1,
xs2=1` → **funct3 = `0b011`**, with `rd = x0`. The 32-bit word reduces to:

```
word = (funct7 << 25) | (rs2 << 20) | (rs1 << 15) | (0b011 << 12) | 0x7B
```

With operands in `a0` (x10) and `a1` (x11), the base word is `0x00B5307B`, and
each increment of `funct7` adds `0x02000000`. The sole exception is
`counter_access` (funct3 = `0b111`, writes `rd`). Even `flush`, whose `rs2` is
unused, is emitted as `ROCC_INSTRUCTION_RS1_RS2` with a constant-zero `rs2`
rather than through the `xs2 = 0` encoding the hardware would also accept.

The instruction word therefore carries almost no information. **All the
semantics live in the two 64-bit registers**, which is what §7 is about.

#### The instructions you will actually use

Gemmini defines about 30 `funct7` values, but a working program touches fewer
than ten:

| funct7 | Name | What it does |
|---|---|---|
| 0 | `config_*` | Latch dataflow, strides, scales, activation. Four variants, selected by `rs1[1:0]` (§8) |
| 2 | `mvin` | DRAM → scratchpad or accumulator (§9) |
| 1, 14 | `mvin2`, `mvin3` | The same move on two more ports, each with its own stride and scale (§9) |
| 3 | `mvout` | Local → DRAM, requantizing on the way (§9) |
| 6 | `preload` | Stage the stationary operand and name the output tile (§10) |
| 4 | `compute.preloaded` | Multiply against the operand just staged (§10) |
| 5 | `compute.accumulated` | Multiply reusing the operand already resident (§10) |
| 7 | `flush` | Skip or retry a TLB request stuck on a page fault (§12) |
| 8 | `loop_ws` | Run an entire tiled matmul in hardware (§11) |

Everything else is supporting cast: the operand latches for the two loop
instructions, the convolution unroller, `mvout_spad`, clock gating, and the
performance counter. **Appendix A** has the complete map with encoded words.

Rocket itself never examines `funct7` — it forwards whatever it finds — so an
unimplemented value is not an illegal instruction, merely an undefined one
(**Appendix B**).

### 7. Operands: addresses, shapes, and packing

Every Gemmini instruction is two 64-bit values (§4). This section is their
format: the local address that both of them may contain, the four shapes `rs1`
takes, and the packed triple `rs2` almost always is.

#### Local address format (32 bits)

Shared by every instruction that names Gemmini-local memory. Internalize this
first.

```
 31   30   29   28                                        0
+----+----+----+------------------------------------------+
| SP |ACC |ACC |              row address                  |
| /AC| ovr| rd |                                           |
+----+----+----+------------------------------------------+
```

| Bit | Condition | Meaning |
|---|---|---|
| 31 | always | `0` = scratchpad, `1` = accumulator |
| 30 | accumulator **write** | `0` = overwrite, `1` = accumulate onto existing |
| 30 | scratchpad, or accumulator read | ignored |
| 29 | accumulator **read** | `0` = scaled-down `inputType`, `1` = raw `accType` |
| 29 | scratchpad, or accumulator write | ignored |
| 28:0 | always | Row address |

Reading with bit 29 = 1 bypasses **both** activation and scaling — the raw
partial-sum path, used when accumulating across outer tiles in software.

Common values:

| Value | Meaning |
|---|---|
| `0x0000_0000` | scratchpad row 0 |
| `0x8000_0000` | accumulator row 0, **overwrite** on write |
| `0xC000_0000` | accumulator row 0, **accumulate** on write |
| `0xA000_0000` | accumulator row 0, read raw `accType` |
| `0xFFFF_FFFF` | `GARBAGE_ADDR` |

`GARBAGE_ADDR` is overloaded by position:

| Used as | Effect |
|---|---|
| B or D operand of `compute` | inject a zero matrix |
| A operand | inject undefined data (don't care) |
| C operand of `preload` | suppress writeback |
| B operand of `preload` (WS) | **do not reload weights** — reuse what's in the array |

That last row is the mechanism the entire WS inner loop is built on (§10).

Memory is **row-addressed**: one address = one row of `DIM` elements
(`inputType` in the scratchpad, `accType` in the accumulator).

#### The four rs1 shapes

`rs2` is almost always the packed triple. `rs1` varies by instruction:

```
 Shape A — bare 64-bit pointer, nothing packed
     +--------------------------------------------------------------------------+
     |                     virtual DRAM byte address [63:0]                      |
     +--------------------------------------------------------------------------+
     mvin, mvin2, mvin3, mvout, LOOP_*_CONFIG_ADDRS_*  cost: 0

 Shape B — packed triple, identical in layout to rs2
     +------------------+------------------+------------------------------------+
     |   rows [63:48]   |   cols [47:32]   |        local address [31:0]        |
     +------------------+------------------+------------------------------------+
     preload, compute_preloaded, compute_accumulated   cost: ~4

 Shape C — local address plus stride
     +------------------------------------+-------------------------------------+
     |         dst stride [63:32]         |      dst local address [31:0]       |
     +------------------------------------+-------------------------------------+
     mvout_spad                                        cost: ~2

 Shape D — dense configuration bitfield
     +------------------------------------+------------------+------------------+
     |      scale / constant [63:32]      |  stride [31:16]  | flags+sel [15:0] |
     +------------------------------------+------------------+------------------+
     config_ex/ld/st/norm, loop triggers               cost: ~2-6
```

Shape A costs **zero** packing instructions — a pointer is already a 64-bit
value in a register. Shapes B, C, and D all require compiler-emitted shifts and
ORs; §18 has the per-instruction cost.

Note `rs1[1:0]` always holds the config selector. That position is forced: all
four config instructions share `funct7 = 0`, so the decoder needs the selector
at a fixed offset in a fixed register.

#### The common rs2 packing

With `ADDR_LEN = 32`:

```
 63           48 47           32 31                        0
+---------------+---------------+---------------------------+
|     rows      |     cols      |      local address        |
+---------------+---------------+---------------------------+
```

| Field | Bits | Notes |
|---|---|---|
| local address | `[31:0]` | format above |
| cols | `[47:32]` | may exceed `DIM` for `mvin` (block move) |
| rows | `[63:48]` | must be ≤ `DIM` |

#### Complete map

| Instruction | rs1 | rs2 |
|---|---|---|
| `mvin` / `mvin2` / `mvin3` | virtual DRAM address (A) | packed triple (B) |
| `mvout` | virtual DRAM address (A) | packed triple (B) |
| `mvout_spad` | dst stride + dst local addr (C) | packed triple (B) |
| `preload` | packed: B/D (B) | packed: C (B) |
| `compute_preloaded` | packed: A (B) | packed: B/D (B) |
| `compute_accumulated` | packed: A (B) | packed: B/D (B) |
| `config_ex` | selector, dataflow, act, transposes, A_stride, acc_scale (D) | C_stride `[63:48]`, sys_shift `[31:0]` |
| `config_ld` | selector, shrunk, id, pixel_repeats, block_stride, scale (D) | **bare DRAM stride in bytes** |
| `config_st` | selector, act, pooling block (D) | stride `[31:0]`, acc_scale `[63:32]` |
| `config_norm` | selector, stat_id, flags, q_const (D) | igelu_qb `[31:0]`, igelu_qc `[63:32]` |
| `flush` | `[0]` = skip/retry | unused (0) |
| `LOOP_*_CONFIG_ADDRS_*` | bare DRAM pointer (A) | bare DRAM pointer (A) |
| `LOOP_*` trigger | dense flags (D) | dense flags (D) |
| `counter_access` | config_reg | unused — writes `rd` |

Two patterns:

- **For DMA, rs1 is external and rs2 is local**, regardless of transfer
  direction. The register role tracks *which memory space*, not which way data
  flows.
- **For config, rs1 always holds the selector** in `[1:0]`, which is why
  `config_ld` looks backwards with its plain stride in rs2.

---

## Part III — Instruction reference

### 8. Configuration instructions (funct7 = 0)

All four share `funct7 = 0` and dispatch on `rs1[1:0]`.

#### `config_ex` — selector `00`

**rs1**

```
 63                    32 31          16 15    10  9  8  7  6   3  2  1 0
+------------------------+--------------+---------+--+--+--+-----+--+---+
|  acc_scale (float32)   |  A_stride    |    —    |Bt|At|so| act |df| 00|
+------------------------+--------------+---------+--+--+--+-----+--+---+
```

| Bits | Field | Meaning |
|---|---|---|
| `[1:0]` | selector | must be `00` |
| `[2]` | `dataflow` | 0 = OS, 1 = WS |
| `[6:3]` | `sys_act` | activation enum |
| `[7]` | `set_only_strides` | update strides without disturbing other state |
| `[8]` | `A_transpose` | route A through the transposer |
| `[9]` | `B_transpose` | route B through the transposer |
| `[31:16]` | `A_stride` | scratchpad row stride feeding A into the array |
| `[63:32]` | `sys_acc_scale` | accumulator → `inputType` scale (float32) |

**rs2**

```
 63        48 47                                             0
+------------+-----------------------------------------------+
|  C_stride  |                  sys_shift                    |
+------------+-----------------------------------------------+
```

`sys_shift` (`[31:0]`) is the post-array right-shift — **OS only**, silently
meaningless in WS.

**Example** — WS, no activation, shift 0, `acc_scale = 1.0f`, `C_stride = 1`,
`A_stride = 1`, no transposes:

```
funct7 = 0
rs1    = 0x3F80_0000_0001_0004
         └─ 0x3F800000 = 1.0f ─┘└A_stride=1┘└ df=1(WS)<<2 | sel=00 ┘
rs2    = 0x0001_0000_0000_0000
         └ C_stride=1 <<48 ┘   └ sys_shift = 0 ┘
```

**Transpose × dataflow legality** (from README — not orthogonal):

| Dataflow | Transpose A | Transpose B | Permitted |
|---|---|---|---|
| OS | no | no | yes |
| OS | no | yes | **no** |
| OS | yes | no | yes |
| OS | yes | yes | yes |
| WS | no | no | yes |
| WS | no | yes | yes |
| WS | yes | no | yes |
| WS | yes | yes | **no** |

The single hardware transposer sits on the A path, which is why `OS + Bᵀ only`
is unrepresentable.

#### `config_ld` (`config_mvin`) — selector `01`

**rs1**

```
 63                    32 31          16 15        8  7  5 4 3  2  1 0
+------------------------+--------------+-----------+-----+---+--+---+
|      mvin scale        | block_stride | pix_repeat|  —  |id |sh| 01|
+------------------------+--------------+-----------+-----+---+--+---+
```

| Bits | Field | Meaning |
|---|---|---|
| `[1:0]` | selector | must be `01` |
| `[2]` | `shrunk` | 0 = accumulator mvins are `accType`, 1 = `inputType` |
| `[4:3]` | `id` | **which mvin**: 0 = `mvin`, 1 = `mvin2`, 2 = `mvin3` |
| `[15:8]` | `pixel_repeats` | experimental, likely to be removed |
| `[31:16]` | `block_mvin_stride` | private-memory stride between submatrices when cols > `DIM` |
| `[63:32]` | `scale` | multiply data during the move |

**rs2** = DRAM stride **in bytes**, full 64 bits.

Each of the three mvin ports has its own scale and stride register, which is
what lets A, B and D be in flight at different scales simultaneously. §22 covers
the optional mvin-scaling hardware behind the `scale` field.

**Example** — configure `mvin` (id=0) and `mvin2` (id=1), DRAM stride 64 B,
scale 1.0f, `block_mvin_stride = DIM = 16`, `pixel_repeats = 1`:

```
funct7 = 0
id=0:  rs1 = 0x3F80_0000_0010_0101    rs2 = 0x0000_0000_0000_0040
id=1:  rs1 = 0x3F80_0000_0010_0109    rs2 = 0x0000_0000_0000_0040
                                ^^
                        id field at [4:3]: 0 vs 1
```

#### `config_st` (`config_mvout`) — selector `10`

**rs1**

```
 63    56 55    48 47    40 39    32 31      24 23  16 15  10 9  8 7 6 5 4 3 2 1 0
+--------+--------+--------+--------+----------+------+------+----+---+---+---+--+
| ocols  | orows  | pocols | porows | pool_out |  —   | lpad |upad|psz|pst|act|10|
+--------+--------+--------+--------+----------+------+------+----+---+---+---+--+
```

| Bits | Field |
|---|---|
| `[1:0]` | selector — must be `10` |
| `[3:2]` | `acc_act` |
| `[5:4]` | `pool_stride` — **0 disables pooling** |
| `[7:6]` | `pool_size` |
| `[9:8]` | `upad` |
| `[11:10]` | `lpad` |
| `[31:24]` | `pool_out_dim` |
| `[39:32]` | `porows` |
| `[47:40]` | `pocols` |
| `[55:48]` | `orows` |
| `[63:56]` | `ocols` |

**rs2**: `[31:0]` = DRAM stride in bytes, `[63:32]` = `acc_scale`.

Pooling assumes NHWC layout and is experimental.

**Example** — stride 64 B, no activation, `acc_scale = 1.0f`, pooling disabled:

```
funct7 = 0
rs1    = 0x0000_0000_0000_0002     (only the selector is set)
rs2    = 0x3F80_0000_0000_0040
         └ 1.0f ─┘└ stride=64 ┘
```

#### `config_norm` — selector `11`

**rs1**: `[1:0]`=`11`, `[15:8]`=`stat_id`, `[16]`=`act_msb`,
`[17]`=`set_stats_id_only`, `[18]`=`q_const_type`, `[63:32]`=`q_const`
**rs2**: `[31:0]`=`igelu_qb`, `[63:32]`=`igelu_qc`

Supports integer-only GELU, layernorm, and softmax (I-BERT). Experimental.

**Example** — all constants zero:

```
funct7 = 0
rs1    = 0x0000_0000_0000_0003
rs2    = 0x0000_0000_0000_0000
```

### 9. Data movement

| Macro | funct7 | Direction |
|---|---|---|
| `gemmini_extended_mvin(dram, spad, cols, rows)` | 2 | DRAM → local |
| `gemmini_extended_mvin2(...)` | 1 | DRAM → local |
| `gemmini_extended_mvin3(...)` | 14 | DRAM → local |
| `gemmini_extended_mvout(dram, spad, cols, rows)` | 3 | local → DRAM |
| `gemmini_extended_mvout_spad(dst, dst_stride, src, cols, rows)` | 23 | scratchpad → scratchpad |

`rs1` = DRAM address (shape A), `rs2` = packed rows/cols/local address (shape
B). Requantization from `acc_t` to `elem_t` happens on the way out of a `mvout`
unless address bit 29 is set (§7).

The three `mvin`s are **functionally identical hardware**. They exist so A, B,
and D can each hold their own DRAM stride and scale simultaneously. Library
convention: `mvin` = A, `mvin2` = B, `mvin3` = D. Configure once outside the
loop; never touch `config_ld` in the inner loop.

Full encoding of one `mvin`, for reference:

```
 31        25 24   20 19   15 14 13 12 11    7 6        0
+------------+-------+-------+--+--+--+-------+----------+
|  0000010   | 01011 | 01010 | 0| 1| 1| 00000 | 1111011  |
+------------+-------+-------+--+--+--+-------+----------+
   funct7=2    rs2=a1  rs1=a0 xd xs1 xs2 rd=x0  custom-3
```

**Examples** — 16×16 tiles, `A` at `0x8FF32100`, `B` at `0x8FF33000`, `D` at
`0x8FF35000`, `C` at `0x8FF34000`. Scratchpad: A at row 0, B at row `0x3F00`
(`BANK_NUM*BANK_ROWS − K*J*DIM = 16384 − 256 = 16128`).

```
mvin  A → spad 0x0000
  funct7 = 2    rs1 = 0x0000_0000_8FF3_2100    rs2 = 0x0010_0010_0000_0000
                      └ bare pointer ────────┘        └r16┘└c16┘└ addr=0x0 ┘

mvin2 B → spad 0x3F00
  funct7 = 1    rs1 = 0x0000_0000_8FF3_3000    rs2 = 0x0010_0010_0000_3F00

mvin3 D → accumulator row 0, overwrite
  funct7 = 14   rs1 = 0x0000_0000_8FF3_5000    rs2 = 0x0010_0010_8000_0000
                                                            └ bit31 = acc ┘

mvin  A → accumulator row 0, ACCUMULATING onto existing data
  funct7 = 2    rs1 = 0x0000_0000_8FF3_2100    rs2 = 0x0010_0010_C000_0000
                                                            └ bit31|bit30 ┘

mvout C ← accumulator row 0 (scaled inputType read)
  funct7 = 3    rs1 = 0x0000_0000_8FF3_4000    rs2 = 0x0010_0010_8000_0000

mvout C ← accumulator row 0, raw accType instead of requantizing
  funct7 = 3    rs1 = 0x0000_0000_8FF3_4000    rs2 = 0x0010_0010_A000_0000
                                                            └ bit29 = full width ┘

mvout_spad  spad 0x200 → spad 0x100, dst stride 1
  funct7 = 23   rs1 = 0x0000_0001_0000_0100    rs2 = 0x0010_0010_0000_0200
                      └stride=1┘└dst=0x100┘           └r16┘└c16┘└src=0x200┘
```

### 10. Execute

One RISC-V instruction cannot hold four addresses, so every matmul is a
**pair**:

```
preload   rs1 = stationary operand,  rs2 = C
compute   rs1 = A,                   rs2 = the other operand
```

Operand roles **swap by dataflow** — getting this backwards yields plausible
wrong numbers:

| | `preload` rs1 | `compute` rs2 |
|---|---|---|
| **WS** | B (weights) | D (bias) |
| **OS** | D (bias) | B |

| Macro | funct7 | Behavior |
|---|---|---|
| `gemmini_extended_preload(BD, C, BD_cols, BD_rows, C_cols, C_rows)` | 6 | Load stationary operand. Commits the cycle after the array receives it; array idles until a compute arrives. |
| `gemmini_extended_compute_preloaded(A, BD, ...)` | 4 | Compute against what was just preloaded, and flip the double buffer |
| `gemmini_extended_compute_accumulated(A, BD, ...)` | 5 | WS: reuse loaded weights. OS: accumulate onto C already in the array. |

`preload` and `compute` differ **only** in bits 31:25. Identical funct3,
identical operand packing — one decoder, one operand path. That is the payoff of
reusing the R-type overlay:

```
 31        25 24   20 19   15 14 13 12 11    7 6        0
+------------+-------+-------+--+--+--+-------+----------+
|  0000110   | 01011 | 01010 | 0| 1| 1| 00000 | 1111011  |   preload
+------------+-------+-------+--+--+--+-------+----------+
|  0000100   | 01011 | 01010 | 0| 1| 1| 00000 | 1111011  |   compute.preloaded
+------------+-------+-------+--+--+--+-------+----------+
```

#### Real bit-field examples (WS)

```
preload, first i of a tile-column: load weights from spad 0x3F00,
                                   C → accumulator row 0, OVERWRITE
  funct7 = 6    rs1 = 0x0010_0010_0000_3F00    rs2 = 0x0010_0010_8000_0000
                      └r16┘└c16┘└ B = 0x3F00 ┘      └r16┘└c16┘└ bit31, bit30=0 ┘

preload, subsequent i: DO NOT reload weights
  funct7 = 6    rs1 = 0x0010_0010_FFFF_FFFF    rs2 = 0x0010_0010_8000_0000
                                  └ GARBAGE ┘

preload, k > 0: accumulate instead of overwrite
  funct7 = 6    rs1 = 0x0010_0010_FFFF_FFFF    rs2 = 0x0010_0010_C000_0000
                                                                 └ bit31|bit30 ┘

compute_preloaded: A from spad 0x0000, no D
  funct7 = 4    rs1 = 0x0010_0010_0000_0000    rs2 = 0x0010_0010_FFFF_FFFF

compute_accumulated: identical operands, different funct7
  funct7 = 5    rs1 = 0x0010_0010_0000_0000    rs2 = 0x0010_0010_FFFF_FFFF
```

The only difference between the three `preload` forms above is **the low 32 bits
of two operands**. No opcode changes. Weight reuse and reduction accumulation
are both expressed purely as address bits.

#### The real WS inner loop

Structure from `sp_tiled_matmul_ws` in `gemmini.h`:

```c
for (size_t k = 0; k < K; k++) {
  for (size_t j = 0; j < J; j++) {
    for (size_t i = 0; i < I; i++) {              // i INNERMOST

      const uint32_t A_sp_addr = a_transpose ? (A_sp_addr_start + (k*I + i)*DIM)
                                             : (A_sp_addr_start + (i*K + k)*DIM);
      const uint32_t B_sp_addr = b_transpose ? (B_sp_addr_start + (j*K + k)*DIM)
                                             : (B_sp_addr_start + (k*J + j)*DIM);

      // ... conditional mvin of A (via mvin) and B (via mvin2) ...

      uint32_t pre_sp_addr = i == 0 ? B_sp_addr : GARBAGE_ADDR;   // (1)
      uint32_t out_sp_addr = C_sp_addr;

      if (k != 0)
        out_sp_addr &= ~(1 << (ADDR_LEN-2));                      // (2)

      gemmini_extended_preload(pre_sp_addr, out_sp_addr,
                               B_cols, B_rows, C_cols, C_rows);

      if (i == 0)
        gemmini_extended_compute_preloaded (A_sp_addr, GARBAGE_ADDR, A_cols, A_rows, DIM, DIM);
      else
        gemmini_extended_compute_accumulated(A_sp_addr, GARBAGE_ADDR, A_cols, A_rows, DIM, DIM);
    }
  }
}
```

**(1)** Weights load on the *first* `i` only; `GARBAGE_ADDR` thereafter means
*don't reload*. With `i` innermost, one weight tile amortizes across `I`
A-tiles. That is weight-stationary, expressed entirely through the address
sentinel.

**(2)** `ADDR_LEN-2 = 30` — the accumulate/overwrite bit. Set on the first `k`
(overwrite), cleared on later `k` so the accumulator reduces. Note the library
initializes `C_sp_addr_start` with bits 31 *and* 30 set (`0xC000_0000`) and
clears bit 30 for `k == 0`.

**(3)** `compute`'s second operand is always `GARBAGE_ADDR` in WS — D does not
enter via `compute`. Bias is preloaded into the accumulator separately with
`mvin3`.

### 11. CISC loop instructions

#### The model

These are **not** single instructions. Each is a sequence where the first N
*latch operands into controller registers* and the last one *triggers
execution*. The split exists purely because the operand set is far too wide for
two 64-bit GPRs.

```
   ┌─ k_LOOP_WS_CONFIG_BOUNDS      ─┐
   ├─ k_LOOP_WS_CONFIG_ADDRS_AB     │  latch into controller registers
   ├─ k_LOOP_WS_CONFIG_ADDRS_DC     │  (each commits immediately,
   ├─ k_LOOP_WS_CONFIG_STRIDES_AB   │   no computation happens)
   ├─ k_LOOP_WS_CONFIG_STRIDES_DC  ─┘
   └─ k_LOOP_WS                       TRIGGER — hardware generates the full
                                      mvin / preload / compute / mvout stream
```

- **Must be issued consecutively.** There is no tag or handle; the latched state
  is a single register set. Interleaving two setups corrupts both.
- **They add no functionality.** Anything they do can be done by hand with §9
  and §10. They exist because the hardware unroller generates a better-scheduled
  stream than most hand-written tiling — and because it eliminates
  per-operation host packing cost (§18).
- **Half-scratchpad constraint.** A single call's operands must fit in **half**
  the private memory, because the unroller double-buffers. Larger problems need
  an outer software tile loop.
- **`ex_accumulate`** is the hook for that outer loop.
- **WS only.** There is no OS variant of either loop instruction.
- **Experimental** per the README, and subject to change.

#### `gemmini_loop_ws` — matmul

Sizes in units of `DIM` tiles:

```
scratchpad  rows of A = I * K * DIM
scratchpad  rows of B = K * J * DIM
accumulator rows of D = I * J * DIM
accumulator rows of C = I * J * DIM
```

| # | funct7 | rs1 | rs2 |
|---|---|---|---|
| 1 | 9 `CONFIG_BOUNDS` | `[15:0]` pad_I, `[31:16]` pad_J, `[63:32]` pad_K | `[15:0]` I, `[31:16]` J, `[63:32]` K |
| 2 | 10 `CONFIG_ADDRS_AB` | A (DRAM ptr) | B (DRAM ptr) |
| 3 | 11 `CONFIG_ADDRS_DC` | D (DRAM ptr) | C (DRAM ptr) |
| 4 | 12 `CONFIG_STRIDES_AB` | A_stride | B_stride |
| 5 | 13 `CONFIG_STRIDES_DC` | D_stride | C_stride |
| 6 | 8 `LOOP_WS` **trigger** | flags, below | flags, below |

**Trigger rs1**

```
 63          20 19  18 17  16 15        8 7   3  2  1  0
+--------------+------+------+-----------+-----+--+--+--+
|      —       | a_id | b_id |    act    |  —  |lD|fC|ex|
+--------------+------+------+-----------+-----+--+--+--+
```

| Bits | Field |
|---|---|
| `[0]` | `ex_accumulate` — accumulate onto existing accumulator contents |
| `[1]` | `full_C` — C is `accType` rather than `inputType` |
| `[2]` | `low_D` — D is `inputType` rather than `accType` |
| `[15:8]` | `act` |
| `[17:16]` | `b_spad_id` |
| `[19:18]` | `a_spad_id` |

**Trigger rs2**: `[0]` = `A_transpose`, `[1]` = `B_transpose`, `[2]` =
`is_resadd`

**Full example** — `C[64×64] = A[64×64] · B[64×64]`, int8, no bias, no
activation. `I = J = K = 4` tiles of 16. Row strides 64 B.

```
 #  funct7  instruction            rs1                        rs2
 1    9     CONFIG_BOUNDS          0x0000_0000_0000_0000      0x0000_0004_0004_0004
                                   └ pad_I/J/K = 0 ┘          └ K=4 ┘└ J=4 ┘└ I=4 ┘
 2   10     CONFIG_ADDRS_AB        0x0000_0000_8FF3_2100      0x0000_0000_8FF3_3000
 3   11     CONFIG_ADDRS_DC        0x0000_0000_0000_0000      0x0000_0000_8FF3_4000
 4   12     CONFIG_STRIDES_AB      0x0000_0000_0000_0040      0x0000_0000_0000_0040
 5   13     CONFIG_STRIDES_DC      0x0000_0000_0000_0040      0x0000_0000_0000_0040
 6    8     LOOP_WS   (trigger)    0x0000_0000_0000_0000      0x0000_0000_0000_0000
```

Six instructions replace roughly `4·4·4 = 64` preload/compute pairs plus their
mvins — several hundred host instructions saved.

The `_spad` variant (`gemmini_loop_ws_spad`) replaces steps 2–5 with
`k_LOOP_WS_CONFIG_SPAD_AB` (24) and `k_LOOP_WS_CONFIG_SPAD_C` (25), for operands
already resident in the scratchpad. It also carries a `skips` field.

#### `gemmini_loop_conv_ws` — convolution

Seven instructions: six latch, one triggers.

| # | funct7 | rs1 | rs2 |
|---|---|---|---|
| 1 | 16 | `[15:0]` batch_size, `[31:16]` in_row_dim, `[47:32]` in_channels, `[63:48]` out_channels | `[15:0]` out_row_dim, `[31:16]` pool_out_row_dim, `[47:32]` out_col_dim, `[55:48]` stride, `[63:56]` padding |
| 2 | 17 | `[7:0]` pool_padding, `[15:8]` pool_stride, `[31:16]` pool_size, `[47:32]` pool_out_col_dim, `[63:48]` kernel_dim | `[15:0]` pochs, `[31:16]` pocols, `[47:32]` porows, `[63:48]` batches |
| 3 | 18 | `[15:0]` lpad, `[31:16]` kchs, `[47:32]` kcols, `[63:48]` krows | `[15:0]` in_col_dim, `[23:16]` plpad, `[31:24]` dpad, `[47:32]` upad, `[63:48]` rpad |
| 4 | 19 | `[9:0]` kernel_dilation, `[20:10]` pdpad, `[31:21]` pupad, `[47:32]` prpad, `[63:48]` orows | `[15:0]` ocols, `[31:16]` out_stride, `[47:32]` weight_stride, `[63:48]` in_stride |
| 5 | 20 | `weights` (DRAM ptr) | `output` (DRAM ptr) |
| 6 | 21 | `bias` (DRAM ptr) | `input` (DRAM ptr) |
| 7 | 15 | trigger flags, below | trigger flags, below |

**Trigger rs1**

| Bits | Field |
|---|---|
| `[0]` | `no_bias` |
| `[1]` | `wrot180` — rotate kernel 180° (transposed convolution) |
| `[2]` | `trans_output_1203` |
| `[3]` | `trans_weight_1203` |
| `[4]` | `trans_weight_0132` |
| `[5]` | `trans_input_3120` |
| `[6]` | `dw` — depthwise |
| `[15:8]` | `max_pixels_per_row` |
| `[17:16]` | `b_spad_id` |
| `[19:18]` | `a_spad_id` |

**Trigger rs2**

| Bits | Field |
|---|---|
| `[0]` | `no_pool` |
| `[1]` | `downsample` |
| `[2]` | `input_dilated` |
| `[10:3]` | `activation` |

The four `trans_*` bits are NHWC layout permutations — this is how im2col-free
convolution feeds a GEMM engine. It is a hardware/compiler contract, and the
most directly reusable idea in the ISA.

**Full example** — 3×3 convolution, stride 1, pad 1, no pooling. Input
`1×32×32×64` (NHWC), output `1×32×32×64`. Tile:
`porows = pocols = pochs = 16`, `krows = kcols = 3`, `kchs = 16`,
`orows = ocols = 16`. All row strides 64 B. Weights at `0x8FF40000`, input at
`0x8FF32100`, bias at `0x8FF35000`, output at `0x8FF34000`.

```
 #  funct7  instruction     rs1                        rs2
 1   16     CONFIG_1        0x0040_0040_0020_0001      0x0101_0020_0020_0020
                            └oc┘└ic┘└ird┘└bs┘          └p┘└s┘└ocd ┘└pord┘└ord┘
 2   17     CONFIG_2        0x0003_0020_0000_0000      0x0001_0010_0010_0010
                            └kd┘└pocd┘└ps ┘└pst,ppad┘  └bat┘└por┘└poc┘└poch┘
 3   18     CONFIG_3        0x0003_0003_0010_0001      0x0001_0001_0100_0020
                            └kr┘└kc┘└kch┘└lpad┘        └rp┘└up┘└dp┘└plp┘└icd┘
 4   19     CONFIG_4        0x0010_0000_0000_0001      0x0040_0040_0040_0010
                            └orows┘  …  └ kdil = 1 ┘   └ins┘└ws ┘└outs┘└ocols┘
 5   20     CONFIG_5        0x0000_0000_8FF4_0000      0x0000_0000_8FF3_4000
                            └ weights ────────────┘    └ output ───────────┘
 6   21     CONFIG_6        0x0000_0000_8FF3_5000      0x0000_0000_8FF3_2100
                            └ bias ───────────────┘    └ input ────────────┘
 7   15     TRIGGER         0x0000_0000_0000_0100      0x0000_0000_0000_0001
                            └ max_pixels_per_row=1 ┘   └ no_pool = 1 ┘
```

Same half-scratchpad and outer-tiling rules as `loop_ws`.

### 12. Control

#### `flush` — funct7 = 7, word `0x0E05307B`

```c
#define gemmini_flush(skip) \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, skip, 0, k_FLUSH)
```

```
 31        25 24   20 19   15 14 13 12 11    7 6        0
+------------+-------+-------+--+--+--+-------+----------+
|  0000111   | 00000 | 01010 | 0| 1| 1| 00000 | 1111011  |
+------------+-------+-------+--+--+--+-------+----------+
   funct7=7   rs2 = 0  rs1=a0 xd xs1 xs2 rd=x0  custom-3
```

```
retry:  rs1 = 0x0000_0000_0000_0000    rs2 = 0
skip:   rs1 = 0x0000_0000_0000_0001    rs2 = 0
```

`rs1[0]`: 1 = skip a TLB request stuck on a page fault, 0 = retry it. `rs2` is
ignored by the hardware, but the library still supplies it as a constant `0` —
GCC normally allocates `x0` for that, so the emitted word is `0x0E05307B`. The
`xs2 = 0` encoding (funct3 = `0b010`, word `0x0E05207B`) is equally legal to
Rocket and saves a register read; `gemmini.h` simply does not use it, because
`xcustom.h` exposes no macro for that shape (§16).

Executes **immediately on receipt**, bypassing the instruction queue — the
programmer must fence around it. This is the visible surface of Gemmini's
incomplete page-fault handling: there is no precise resumable trap, only
skip-or-retry (§21).

#### `counter_access` — funct7 = 126

```c
gemmini_counter_access(rd, config_reg);   // funct3 = 0x7, the only instruction with xd = 1
```

The only Gemmini instruction that returns a value, so the only one using
`RoCCResponse` and the long-latency writeback path — scoreboarded on `rd` like a
`div` (§20). Its `rs2` is the uninitialized placeholder of §16.

#### `gemmini_fence()`

```c
#define gemmini_fence() asm volatile("fence")
```

Not a custom instruction — a plain RISC-V `fence`. It works because Rocket's
decode stage stalls a fence while the accelerator asserts `busy` or any RoCC
instruction is still in flight (§20). Note it carries no `"memory"` clobber, so
it is a hardware fence only (§16).

### 13. C API layering

Consistent widening convention: base macro, then `extended`, `extended2`, … as
fields were exposed over successive revisions. `config_ld` has reached
`extended5`.

```
gemmini_config_ld(stride)
  └─ gemmini_extended_config_ld(stride, scale)
       └─ gemmini_extended2_config_ld(stride, scale, shrunk)
            └─ gemmini_extended3_config_ld(..., id)
                 └─ gemmini_extended4_config_ld(..., block_mvin_stride, id)
                      └─ gemmini_extended5_config_ld(..., pixel_repeats, id)   ← emits
```

Narrow forms pass defaults (`MVIN_SCALE_IDENTITY`, `ACC_SCALE_IDENTITY`, `DIM`,
`false`). **For anything performance-relevant, call the widest variant** — the
defaults silently reset fields you may have set earlier. Concretely: a
`gemmini_config_ex` call issued after a `gemmini_extended3_config_ex` quietly
puts `acc_scale` back to `1.0` and `C_stride` back to `1`, because the narrow
macro supplies those defaults on its way down the ladder. Nothing warns; the
next `mvout` is simply scaled wrong.

Above these sit ordinary C tiling functions: `sp_tiled_matmul_ws`,
`sp_tiled_matmul_os`, `tiled_matmul_auto`, `tiled_conv_auto`.

### 14. A complete matmul, end to end

Everything in Part III, assembled into one program: `C = A · B + D` for a single
16×16 tile, int8, weight-stationary, with all four matrices at fixed DRAM
addresses. Ten Gemmini instructions and a `fence` — four `config_*` (§8) to
latch state, three `mvin`s (§9), a `preload`/`compute` pair (§10), one `mvout`,
and the fence that is the only synchronization in the whole sequence (§5).

Every operand value below decodes with §7: a DRAM pointer in one register, a
packed `rows/cols/local-address` triple in the other.

#### As an instruction trace

```
 #   funct7  instruction            rs1                        rs2
 --  ------  ---------------------  -------------------------  -------------------------
  1     0    config_ex (WS)         0x3F80_0000_0001_0004      0x0001_0000_0000_0000
  2     0    config_ld  id=0 (A)    0x3F80_0000_0010_0101      0x0000_0000_0000_0040
  3     0    config_ld  id=1 (B)    0x3F80_0000_0010_0109      0x0000_0000_0000_0040
  4     0    config_st              0x0000_0000_0000_0002      0x3F80_0000_0000_0040
  5     2    mvin   A → spad 0x0    0x0000_0000_8FF3_2100      0x0010_0010_0000_0000
  6     1    mvin2  B → spad 0x3F00 0x0000_0000_8FF3_3000      0x0010_0010_0000_3F00
  7    14    mvin3  D → acc  0x0    0x0000_0000_8FF3_5000      0x0010_0010_8000_0000
  8     6    preload B, C=acc(acc)  0x0010_0010_0000_3F00      0x0010_0010_C000_0000
  9     4    compute_preloaded A    0x0010_0010_0000_0000      0x0010_0010_FFFF_FFFF
 10     3    mvout  C ← acc 0x0     0x0000_0000_8FF3_4000      0x0010_0010_8000_0000
 --           fence
```

Line 8 uses `0xC000_0000` (accumulate) because D was already moved into the
accumulator at line 7. With no bias, line 7 is dropped and line 8 uses
`0x8000_0000` (overwrite).

Line 10 reads with bit 29 = 0, so the accumulator applies `acc_scale` and the
activation function and delivers `inputType`. Using `0xA000_0000` instead would
return raw `accType` with neither applied.

#### The same thing as assembly

```asm
  # 1. config_ex: WS dataflow, no activation, acc_scale 1.0f, A_stride 1, C_stride 1
  li   a0, 0x3F80000000010004
  li   a1, 0x0001000000000000
  .insn r 0x7B, 3, 0,  x0, a0, a1     # 0x00B5307B

  # 2-3. config_ld for mvin (id=0) and mvin2 (id=1): 64-byte DRAM rows, scale 1.0f
  li   a0, 0x3F80000000100101
  li   a1, 64
  .insn r 0x7B, 3, 0,  x0, a0, a1     # 0x00B5307B
  li   a0, 0x3F80000000100109
  .insn r 0x7B, 3, 0,  x0, a0, a1     # 0x00B5307B

  # 4. config_st: 64-byte DRAM rows, acc_scale 1.0f, no pooling
  li   a0, 2
  li   a1, 0x3F80000000000040
  .insn r 0x7B, 3, 0,  x0, a0, a1     # 0x00B5307B

  # 5. mvin A -> scratchpad row 0
  ld   a0, activations
  li   a1, 0x0010001000000000
  .insn r 0x7B, 3, 2,  x0, a0, a1     # 0x04B5307B

  # 6. mvin2 B -> scratchpad row 0x3F00
  ld   a0, weights
  li   a1, 0x0010001000003F00
  .insn r 0x7B, 3, 1,  x0, a0, a1     # 0x02B5307B

  # 7. mvin3 D -> accumulator row 0
  ld   a0, bias
  li   a1, 0x0010001080000000
  .insn r 0x7B, 3, 14, x0, a0, a1     # 0x1CB5307B

  # 8. preload B from spad 0x3F00, C -> accumulator row 0, accumulate onto D
  li   a0, 0x0010001000003F00
  li   a1, 0x00100010C0000000
  .insn r 0x7B, 3, 6,  x0, a0, a1     # 0x0CB5307B

  # 9. compute.preloaded: A from spad 0, D not supplied here (WS)
  li   a0, 0x0010001000000000
  li   a1, 0x00100010FFFFFFFF        # sentinel in the address field
  .insn r 0x7B, 3, 4,  x0, a0, a1     # 0x08B5307B

  # 10. mvout accumulator row 0 -> DRAM
  ld   a0, output
  li   a1, 0x0010001080000000
  .insn r 0x7B, 3, 3,  x0, a0, a1     # 0x06B5307B

  fence
```

Every instruction has `xd = 0`. The core fires all ten and keeps going; the only
synchronization point is the `fence`, which the core resolves against Gemmini's
`busy` signal (§5).

Contrast this with §11: the same computation scaled to 64×64×64 is 64
preload/compute pairs by hand, or six CISC instructions.

---

## Part IV — The software path

### 15. From C macro to instruction

§4 showed what the levels are. Part IV is the toolchain that produces them: two
layers of macro, three headers, and whatever RV64I the compiler needs to build
the operand values.

```
 C source  ->  compiler  ->  .text  ->  CPU regfile  ->  RoCC port  ->  Gemmini
 (a macro)     (packs        (one      (two 64-bit      (delivers       (slices
               operands)     custom3    values)          values)         fields)
                             word)
```

Each stage is walked with a concrete 16 × 16 tile — C source, preprocessor
output, emitted assembly, encoded word — in
[`Gemmini_ISA_tutorial.md`](Gemmini_ISA_tutorial.md) §5–§6. What follows here is
the general rule rather than the example: §16 the macro layers, §17 the three
headers and their in-tree paths, §18 what the packing costs.

### 16. The macro layers

Two layers of macro, one job each:

| Layer | File | Job |
|---|---|---|
| Gemmini | `gemmini.h` | name a `funct7`, and pack the arguments into two 64-bit C **expressions** |
| RoCC | `xcustom.h` | wrap those expressions in inline asm that emits one `.insn` |

Neither layer emits an instruction by itself. `gemmini.h` produces expressions;
`xcustom.h` produces an asm template with holes; GCC fills the holes with
register names and emits whatever RV64I is needed to make those registers hold
the right values (§18).

#### The inline-asm mechanism

Every Gemmini instruction but one bottoms out here, in
`ibm/rocc-software/src/xcustom.h`:

```c
#define ROCC_INSTRUCTION_0_R_R(x, rs1, rs2, func7)                                   \
  {                                                                                  \
    asm volatile(                                                                    \
        ".insn r " STR(CAT(CUSTOM_, x)) ", " STR(0x3) ", " STR(func7) ", x0, %0, %1" \
        :                                                                            \
        : "r"(rs1), "r"(rs2));                                                       \
  }
```

The three colon-separated sections are GCC's extended-asm form — `asm
volatile(template : outputs : inputs : clobbers)` — with the outputs empty and
the clobber list absent entirely.

`"r"(expr)` is an **input constraint**: *evaluate this C expression, place the
result in some general-purpose register, and substitute that register's name
here.* That one line is the whole mechanism. The macro's job is to turn two C
expressions into two register values.

`gemmini_extended_mvin(dram_addr, spad_addr, cols, rows)` therefore expands to:

```c
asm volatile(".insn r CUSTOM_3, 0x3, 2, x0, %0, %1"
             :                                   /* no outputs */
             : "r"(dram_addr),                                                   /* -> %0 */
               "r"(((uint64_t)rows << 48) | ((uint64_t)cols << 32) | spad_addr)); /* -> %1 */
```

The `.insn r` directive takes its fields in a fixed order:

```
.insn r  <opcode>, <funct3>, <funct7>, <rd>, <rs1>, <rs2>
         CUSTOM_3    0x3        2       x0    %0     %1
            |         |         |       |     |      |
          0x7B     011 =     k_MVIN   unused |      +-- second input operand
                   xd=0,                     +---------- first input operand
                   xs1=1,
                   xs2=1
```

**`%0` lands in the rs1 field and `%1` in the rs2 field, strictly by declaration
order of the input constraints.** Nothing deeper is going on; the header author
chose which argument goes where. The one place position is genuinely forced is
the config selector in `rs1[1:0]` (§7), because all four config instructions
share `funct7 = 0` and the decoder needs it at a fixed offset.

Two properties of this asm statement are easy to miss and both matter:

- **`volatile` is load-bearing for the `rd` form.** An `asm` with no outputs is
  implicitly volatile, so the keyword is redundant in `_0_R_R` — but `_R_R_R`
  *has* an output, and without `volatile` GCC would be free to delete it when
  `rd` is unused, or to common two identical reads.
- **There is no `"memory"` clobber.** GCC therefore does not know that `mvin`
  reads DRAM or that `mvout` writes it. `volatile` stops the instruction from
  being deleted and from being reordered against other `volatile` asm; it does
  *not* stop GCC from keeping a value in a register across a `mvout` that has
  just overwritten that address, or from sinking a store past a `mvin` that was
  supposed to read it. Note that `gemmini_fence()` has no clobber either:

  ```c
  #define gemmini_fence() asm volatile("fence")
  ```

  so it is a *hardware* fence with no compiler barrier attached. Anywhere the C
  code and Gemmini share a buffer, that pairing is the programmer's
  responsibility.

The arithmetic inside `"r"(...)` is **ordinary RV64I scalar code emitted by the
compiler**, not part of the custom instruction. §18 walks through exactly what
it costs.

#### The four macros of `xcustom.h`

```
                     writes rd?          no rd
                  (funct3 = 0x7)    (funct3 = 0x3)
                  ---------------   ---------------
  .S files        RAW_R_R_R          RAW_0_R_R      -> bare `.insn` directive
  C/C++           R_R_R              0_R_R          -> asm volatile + "r" constraints
```

The vertical axis is *delivery*, the horizontal axis is *exactly the funct3
table in Appendix B* — `xd` is never a parameter, it is chosen by picking a macro.
`ROCC_INSTRUCTION(x, rd, rs1, rs2, func7)` is a plain alias for
`ROCC_INSTRUCTION_R_R_R`.

All four, expanded for `CUSTOM_3`:

```c
/* 1. no result — the workhorse */
ROCC_INSTRUCTION_0_R_R(3, a, b, 2)
  ->  { asm volatile(".insn r CUSTOM_3, 0x3, 2, x0, %0, %1"
                     :: "r"(a), "r"(b)); }

/* 2. result in a C variable */
ROCC_INSTRUCTION_R_R_R(3, r, a, b, 126)
  ->  { asm volatile(".insn r CUSTOM_3, 0x7, 126, %0, %1, %2"
                     : "=r"(r)
                     : "r"(a), "r"(b)); }
```

Note the renumbering: operands are numbered across *all* sections, outputs
first, so adding an output shifts the sources from `%0`/`%1` to `%1`/`%2`.
`"=r"` is a write-only output constraint.

The `RAW_*` pair is not inline asm at all — it expands to a naked directive for
use inside a `.S` file, and takes **architectural register names**, not C
expressions:

```asm
#include "rocc-software/src/xcustom.h"
    ROCC_INSTRUCTION_RAW_0_R_R(3, a0, a1, 2)      # -> .insn r CUSTOM_3, 3, 2, x0, a0, a1
    ROCC_INSTRUCTION_RAW_R_R_R(3, a0, a1, a2, 42) # -> .insn r CUSTOM_3, 7, 42, a0, a1, a2
```

Register allocation is yours in that form. Gemmini never uses it; it exists for
hand-written test harnesses.

Finally, two immediate-operand variants (`R_R_I`, `R_I_I`) sit commented out at
the bottom of the file and cannot be revived as written: RoCC's R-type layout
spends `[31:25]` on `funct7`, leaving no immediate field. An "immediate" RoCC
operand can only be a compiler-materialized constant in a register — which is
what `"r"(constant)` already gives you.

#### Only two of the six funct3 encodings are exposed

Appendix B lists six encodings Rocket decodes. `xcustom.h` offers C macros for exactly
two of them, `0x3` and `0x7`. There is no macro for `0x6` — *writes `rd`, reads
`rs1`, no `rs2`* — even though the hardware accepts it.

`counter_access` wants precisely that shape: a selector in, a value out, no
second source. Lacking the macro, `gemmini.h` fabricates an operand:

```c
#define gemmini_counter_access(rd, config_reg) \
  { \
    uint32_t _placeholder; \
    ROCC_INSTRUCTION(XCUSTOM_ACC, rd, config_reg, _placeholder, k_COUNTER) \
  }
```

`_placeholder` is declared and never assigned. GCC says so, on every build that
reads a counter:

```
xcustom.h:117:5: warning: '_placeholder' is used uninitialized [-Wuninitialized]
gemmini.h:314:14: note: '_placeholder' was declared here
```

and then settles on zero:

```asm
g:
    li      a5,0
    .insn r CUSTOM_3, 0x7, 126, a0, a0, a5    # rd = rs1 = a0; rs2 = the placeholder
    sext.w  a0,a0                             # because rd is declared uint32_t
```

Harmless — Gemmini ignores `rs2` for `k_COUNTER` — but it is noise in every
build, and it exists purely because the `0x6` macro was never written. Note also
that nothing forbids `rd` and `rs1` naming the same register.

#### The anatomy of a `gemmini.h` macro

`gemmini.h` does not call `xcustom.h` directly. It interposes one shim of its
own, a pure rename whose only contribution is documentation — *two source
operands, no result*, the shape of every Gemmini instruction but
`counter_access`:

```c
#define ROCC_INSTRUCTION_RS1_RS2(x, rs1, rs2, funct) \
  ROCC_INSTRUCTION_0_R_R(x, rs1, rs2, funct)
```

Below that, every instruction macro is the same three things: a `funct7` name,
two packing expressions, and a position in the `extended` ladder. Annotated:

```c
#define gemmini_extended_mvin(dram_addr, spad_addr, cols, rows)              \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC,           /* opcode  -> custom3   */ \
      dram_addr,                                  /* rs1: shape A, no packing */ \
      ((uint64_t)(rows) << (ADDR_LEN + 16))       /* rs2[63:48]           */ \
    | ((uint64_t)(cols) << ADDR_LEN)              /* rs2[47:32]           */ \
    | (spad_addr),                                /* rs2[31:0]            */ \
      k_MVIN)                                     /* funct7 = 2           */
```

Three things to take from it:

1. **Field positions are written against `ADDR_LEN`, never a literal 32.**
   `gemmini_params.h` supplies it, so a Gemmini elaborated with a different
   local-address width repacks correctly with no change to `gemmini.h` (§17).
2. **The `(uint64_t)` casts are load-bearing.** `rows << 48` on an `int` is
   undefined; the cast promotes before shifting. The same requirement in reverse
   is the sign-extension trap of §18 — `spad_addr` must be *unsigned* 32-bit or
   it destroys the fields above it.
3. **These are expressions, textually substituted.** No function, no prototype,
   no type checking. Passing a `float` where a `uint32_t` is expected converts
   silently inside the shift.

#### Six shapes, six real macros

| Shape (§7) | Macro | rs1 | rs2 |
|---|---|---|---|
| A + B — pointer, packed triple | `gemmini_extended_mvin` | `dram_addr` | rows, cols, spad |
| B + B — two packed triples | `gemmini_extended_preload` | BD triple | C triple |
| D — dense bitfield + selector | `gemmini_extended3_config_ex` | fields + `CONFIG_EX` | C_stride, sys_shift |
| A + A — two bare operands | `k_LOOP_WS_CONFIG_ADDRS_AB` | `A` | `B` |
| rd-returning | `gemmini_counter_access` | `config_reg` | `_placeholder` |
| not an instruction at all | `gemmini_fence` | — | — |

Two of these are worth reading in full.

**Floats are passed as bits, not values.** `config_ld`'s scale is a `float` in C
and an integer field in the instruction word:

```c
#define gemmini_extended5_config_ld(stride, scale, shrunk, block_mvin_stride, pixel_repeats, id) \
  ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, \
    ((uint64_t)(scale_t_to_scale_t_bits(scale)) << 32) | ((uint64_t)(block_mvin_stride) << 16) \
    | ((uint64_t)(pixel_repeats) << 8) | ((id) << 3) | ((shrunk) << 2) | CONFIG_LD, \
    stride, k_CONFIG)
```

with the conversion a union type-pun, not an arithmetic cast:

```c
static scale_t_bits scale_t_to_scale_t_bits(scale_t x) {
    union { scale_t_bits b; scale_t f; } un;
    un.f = x;
    return un.b;
}
```

The hardware holds a float in that field, so the float's *bit pattern* is what
must arrive. And if the accelerator was elaborated without mvin scaling,
`gemmini_params.h` leaves `HAS_MVIN_SCALE` undefined and the helper degenerates
to `#define scale_t_to_scale_t_bits(x) 0` — the same source compiles either way.
This is `gemmini_params.h` steering `gemmini.h`, not just supplying constants.

**One macro, six instructions.** The CISC latch protocol of §11 is a single
macro that emits five `LOOP_WS_CONFIG_*` instructions and then the trigger:

```c
#define gemmini_loop_ws(I, J, K, pad_I, pad_J, pad_K, A, B, D, C, ...) \
  { \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, (pad_K<<32)|(pad_J<<16)|pad_I, \
                                          (K<<32)|(J<<16)|I, k_LOOP_WS_CONFIG_BOUNDS) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, A, B,               k_LOOP_WS_CONFIG_ADDRS_AB) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, D, C,               k_LOOP_WS_CONFIG_ADDRS_DC) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, A_stride, B_stride, k_LOOP_WS_CONFIG_STRIDES_AB) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, D_stride, C_stride, k_LOOP_WS_CONFIG_STRIDES_DC) \
    ROCC_INSTRUCTION_RS1_RS2(XCUSTOM_ACC, /* flags */, /* flags */, k_LOOP_WS)             \
  }
```

(Abridged — the real macro takes 23 arguments; §11 has the field layouts.) The
outer braces make the six one statement, and because `volatile` asm statements
are not reordered with respect to one another, they reach Gemmini in written
order. That ordering is what the latch protocol depends on.

#### Macro-hygiene hazards

Both of these are real, and both are `xcustom.h`/`gemmini.h` behaviour rather
than anything about RoCC.

**1. The macros are braced blocks, so they break `if`/`else`.**
`ROCC_INSTRUCTION_0_R_R` expands to `{ ... }`, and the semicolon you write at
the call site becomes an empty statement after it:

```c
if (c) gemmini_mvin(d, s);
else   gemmini_mvin(d, s);      // error: 'else' without a previous 'if'
```

The conventional fix — `do { ... } while (0)` — was not used, so brace your call
sites.

**2. Not every macro argument is parenthesized.** In `config_ex`, `A_transpose`
and `B_transpose` appear bare inside their shifts:

```c
| (B_transpose << 9) | (A_transpose << 8) | ((set_only_strides) << 7) | ((sys_act) << 3) | ...
```

so an argument with lower precedence than `<<` reassociates.
`gemmini_extended_config_ex(..., p|q)` preprocesses to `(p|q << 9)`, which is
`p | (q << 9)`:

```
$ riscv64-unknown-elf-gcc -E ...
... | (p|q << 9) | ...
```

`config_norm` has the same exposure on `q_const_type`, `set_stats_id_only`,
`act_msb` and `stat_id`. Pass plain identifiers or literals to these, or
parenthesize at the call site.

### 17. The header stack

Three files, three provenances — and the split is deliberate: one file per thing
that can change independently. `xcustom.h` is accelerator-agnostic, `gemmini.h`
is the ISA, `gemmini_params.h` is this particular elaboration.

| File | Path from Chipyard root | Origin |
|---|---|---|
| `xcustom.h` | `generators/gemmini/software/gemmini-rocc-tests/rocc-software/src/xcustom.h` | `ibm/rocc-software`, pinned at `fddb795` (2020-02-19) |
| `gemmini.h` | `generators/gemmini/software/gemmini-rocc-tests/include/gemmini.h` | `ucb-bar/gemmini-rocc-tests` |
| `gemmini_params.h` | `generators/gemmini/software/gemmini-rocc-tests/include/gemmini_params.h` | **generated** at Chisel elaboration by `GemminiConfigs.scala` |

`rocc-software` is a submodule of `gemmini-rocc-tests`, which is itself a
submodule of `gemmini` — so a depth-limited `init-submodules` does not fetch it.
A missing `xcustom.h` is almost always that, not a broken checkout.

Its pin has not moved since February 2020, and the whole repository is two
headers. That inertia is the point: `xcustom.h` encodes the **RoCC calling
convention**, which has not changed, and knows nothing about Gemmini.

#### Which macro each file uses

| Macro | Used by |
|---|---|
| `ROCC_INSTRUCTION_0_R_R` | `gemmini.h`, via its `ROCC_INSTRUCTION_RS1_RS2` shim — every instruction in §6 but one |
| `ROCC_INSTRUCTION_R_R_R` (alias `ROCC_INSTRUCTION`) | `gemmini.h`, for `counter_access` only — the sole `xd = 1` case |
| `ROCC_INSTRUCTION_RAW_*` | nothing in Gemmini; present for hand-written `.S` harnesses |

#### Who resolves `CUSTOM_3`?

Not these headers — nothing in `gemmini.h` or `xcustom.h` defines `CUSTOM_3`. It
is a **built-in opcode name in GNU as**, from the table in
`gas/config/tc-riscv.c`:

```c
{"CUSTOM_0",  0x0b},   {"CUSTOM_1",  0x2b},
{"CUSTOM_2",  0x5b},   {"CUSTOM_3",  0x7b},
```

So the `0x7B` in §3 is the assembler's, and `CUSTOM_3` reaches it as a *string*
in the asm template. The token is built by the two-level paste at the top of
`xcustom.h`:

```c
#define STR1(x) #x
#define STR(x)  STR1(x)      // expand x, then stringify
#define CAT_(A, B) A##B
#define CAT(A, B)  CAT_(A, B)  // expand A and B, then paste
```

The indirection is load-bearing. `gemmini.h` passes the *macro* `XCUSTOM_ACC`,
not the literal `3`; `##` suppresses expansion of its operands, so a
single-level `CAT_(CUSTOM_, XCUSTOM_ACC)` would paste the undefined token
`CUSTOM_XCUSTOM_ACC`. Routing through `CAT` expands the argument first:

```
CAT(CUSTOM_, XCUSTOM_ACC)  ->  CAT_(CUSTOM_, 3)  ->  CUSTOM_3  ->  STR(...)  ->  "CUSTOM_3"
```

`STR`/`STR1` is the same trick for `#`.

#### What `gemmini.h` contributes

Mechanically `gemmini.h` adds almost nothing to `xcustom.h` — one renaming shim.
What it contributes is the three things `xcustom.h` deliberately does not know:

1. **`funct7` names** — the `k_*` constants (`k_MVIN 2`, `k_PRELOAD 6`,
   `k_LOOP_WS 8`, `k_COUNTER 126`, …), i.e. the §6 map as C.
2. **Operand packing** — the shift/OR expressions that build the §7 field
   layouts, written against `ADDR_LEN` rather than a hard-coded 32.
3. **The `extended` ladder** — the widening variants of §13.

#### The whole chain, one call

`gemmini_extended_mvin(dram, sp, 16, 16)`:

```
gemmini.h    gemmini_extended_mvin(dram_addr, spad_addr, cols, rows)
                 |  substitutes k_MVIN (=2), XCUSTOM_ACC (=3), and the rs2 packing expression
                 v
gemmini.h    ROCC_INSTRUCTION_RS1_RS2(3, dram_addr, (rows<<48)|(cols<<32)|spad_addr, 2)
                 |  rename only
                 v
xcustom.h    ROCC_INSTRUCTION_0_R_R(3, ..., ..., 2)
                 |  CAT/STR -> "CUSTOM_3"; funct3 fixed at 0x3; rd fixed at x0
                 v
C source     asm volatile(".insn r CUSTOM_3, 0x3, 2, x0, %0, %1"
                          :: "r"(dram_addr), "r"((rows<<48)|(cols<<32)|spad_addr));
                 |  GCC allocates registers, emits RV64I for the operand expressions  (§18)
                 v
.s           .insn r CUSTOM_3, 0x3, 2, x0, a0, a1
                 |  GNU as: CUSTOM_3 -> opcode 0x7B, fields packed R-type  (§3)
                 v
.text        0x04b5307b
```

The three layers map cleanly onto the three things an accelerator ISA needs:
`gemmini_params.h` carries what the *hardware* was elaborated with, `gemmini.h`
carries what the *ISA* means, and `xcustom.h` carries how *RoCC* is encoded.
Only the first two change when you fork Gemmini.

#### `gemmini_params.h` is build output

`gemmini.h` is hand-written and permanent. `gemmini_params.h` is a **build
artifact** emitted by Chisel elaboration that happens to live in the `include/`
directory. The dependency runs hardware → header → software: the header carries
`XCUSTOM_ACC`, `DIM`, `ADDR_LEN`, the `elem_t` / `acc_t` typedefs, `BANK_NUM`,
`BANK_ROWS`, `ACC_ROWS`, `MAX_BYTES`, feature flags, and the scale function —
all emitted from the elaborated `GemminiArrayConfig` — and `gemmini.h` uses them
to pick loop tiling.

A copy matching the default config is checked into git so `./build.sh` works
before you have ever run the generator — the same pattern as autoconf's
`config.h`.

**Consequence:** after changing `Configs.scala`, re-elaborate *before* rebuilding
software. The `scripts/build-verilator.sh` and `scripts/build-spike.sh` wrappers
enforce this ordering. Skip it and you get software compiled for `DIM = 16`
running on a `DIM = 32` array — it compiles cleanly and produces silently wrong
results, because nothing at the ISA level checks that the two agree. Same
failure class as gotcha 10 in §23, reached from the software side.

### 18. Operand packing and its cost

#### The generated assembly, line by line

Verified output — GCC 13.2.0, `-O2`,
`void f(void *dram, uint32_t sp) { gemmini_extended_mvin(dram, sp, 16, 16); }`,
so `a0 = dram_addr` and `a1 = spad_addr` on entry:

```asm
    # dram_addr needs NO packing — it is already a 64-bit pointer in a0
    lui     a5, 0x10001                   # a5 = 0x0000_0000_1000_1000   = ((rows<<16)|cols) << 12
    slli    a1, a1, 32                    # \  zero-extend spad_addr:
    slli    a5, a5, 24                    #  > a5 = 0x0010_0010_0000_0000  fields at [63:48],[47:32]
    srli    a1, a1, 32                    # /  clear a1[63:32]
    or      a1, a1, a5                    # a1 = 0x0010_0010_xxxx_xxxx   | spad_addr
    .insn r CUSTOM_3, 0x3, 2, x0, a0, a1  # rs1 = a0, rs2 = a1
```

Every one of the first five is **plain RV64I** — no relationship to Gemmini or
RoCC. The `slli`/`srli` pair is interleaved with the constant build by the
scheduler; the two chains are independent.

| Instruction | Meaning | Effect here |
|---|---|---|
| `lui rd, imm` | load upper immediate — 20-bit immediate at bits `[31:12]`, zeros below | `0x10001 << 12`, materializing `(rows<<16)\|cols` pre-shifted |
| `slli rd, rs, sh` | shift left logical immediate | `<< 24` lifts both fields to `[63:48]` and `[47:32]` |
| `slli` + `srli` by 32 | the canonical RV64 zero-extend-32 idiom | clears `spad_addr[63:32]` |
| `or rd, rs1, rs2` | bitwise OR | merges the runtime `spad_addr` into `[31:0]` |

Two things about the compiler's factoring are worth noting. First, it never
builds `rows<<48 | cols<<32` as two shifts: it folds both fields into the single
constant `0x10001000` — which is exactly a `lui` immediate, so no `addi` is
needed — and applies one `slli 24`. Two instructions for a constant that looks
like it needs four. Second, both fields are compile-time constants whenever
`DIM` is used, so only the `or` is data-dependent.

The `slli`/`srli` pair is the sign-extension trap below, made visible:
`spad_addr` is `uint32_t`, and GCC emits two instructions purely to guarantee
the upper half is clean. Where GCC can already prove the value is zero-extended,
that pair disappears and the sequence is four instructions.

Assembled, the whole thing is 16 bytes — `lui` and `.insn` are 4 bytes each, the
three middle instructions compress to 2 (`c.slli`, `c.srli`, `c.or`):

```
   0:  100017b7    lui     a5,0x10001
   4:  1582        slli    a1,a1,0x20
   6:  07e2        slli    a5,a5,0x18
   8:  9181        srli    a1,a1,0x20
   a:  8ddd        or      a1,a1,a5
   c:  04b5307b    .insn   r CUSTOM_3, 0x3, 2, x0, a0, a1
```

That final word decodes exactly as §3 predicts: `funct7 = 0000010`
(2 = `k_MVIN`), `rs2 = 01011` (`a1`), `rs1 = 01010` (`a0`), `funct3 = 011`,
`rd = 00000` (`x0`), `opcode = 1111011` (`0x7B`).

#### Which operand is being built

**All five instructions build `rs2`.** `rs1` costs nothing.

```
    lui   a5, 0x10001    +
    slli  a1, a1, 32     |   five instructions, all to build rs2
    slli  a5, a5, 24     |
    srli  a1, a1, 32     |
    or    a1, a1, a5     +
    .insn r CUSTOM_3, 0x3, 2, x0, a0, a1
                                   |   +-- rs2 = a1 = 0x0010_0010_8000_0040   packed, cost 4–5
                                   +------ rs1 = a0 = 0x0000_0000_8FF3_2100   pointer, cost 0
```

Which side pays depends on the instruction's operand shapes (§7):

| Instruction | rs1 shape | rs1 cost | rs2 shape | rs2 cost |
|---|---|---|---|---|
| `mvin` / `mvin2` / `mvin3` / `mvout` | A — bare pointer | 0 | B — packed triple | 4–5 |
| `mvout_spad` | C — addr + stride | ~2 | B — packed triple | ~4 |
| `preload` | B — packed triple | ~4 | B — packed triple | ~4 |
| `compute_preloaded` / `compute_accumulated` | B — packed triple | ~4 | B — packed triple | ~4 |
| `config_*` | D — dense bitfield | ~2–6 | varies | ~1–2 |
| `LOOP_*_CONFIG_ADDRS_*` | A — bare pointer | 0 | A — bare pointer | 0 |

> **Sign-extension trap.** `spad_addr` must be zero-extended into the upper 32
> bits or it will corrupt the rows/cols fields. This is why `GARBAGE_ADDR` is
> defined as `((uint32_t)(-1))` — `0x0000_0000_FFFF_FFFF` after promotion. Had
> it been `(int32_t)(-1)`, promotion would sign-extend to all ones and destroy
> both fields.

#### Issue-cost consequence

A `DIM×DIM` compute occupies the array roughly `DIM` cycles (16). The execute
pair pays packing on **both** operands — and it is the pair sitting in the
innermost loop. With runtime `rows`/`cols` (partial edge tiles) and a computed
scratchpad address (`A_sp_addr_start + (i*K + k)*DIM`), a single `compute`
reaches 8–10 host instructions, and a `preload`+`compute` pair 12–20.

Rocket is single-issue and in-order, so the host is at rough parity with the
array just *issuing* work. Any extra address arithmetic starves the accelerator.
Add the replay behaviour of §20 — a full command queue costs a refetch, not a
stall — and the margin is thinner still.

This is the concrete motivation for both of Gemmini's throughput mechanisms:
**decoupled access/execute** (core runs ahead while the array works) and the
**CISC loop instructions** (one setup sequence generates thousands of operations
with zero further host cost).

---

## Part V — The hardware interface

### 19. The RoCC signals and bundles

```scala
class RoCCCoreIO extends CoreBundle {
  val cmd       = Flipped(Decoupled(new RoCCCommand))   // core  -> accel
  val resp      = Decoupled(new RoCCResponse)           // accel -> core
  val mem       = new HellaCacheIO                      // accel -> L1 D$
  val busy      = Output(Bool())                        // accel -> core: work in flight
  val interrupt = Output(Bool())                        // accel -> PLIC
  val exception = Input(Bool())                         // core  -> accel: core trapped
  val csrs      = Flipped(Vec(nRoCCCSRs, new CustomCSRIO))
}

class RoCCIO extends RoCCCoreIO {
  val ptw       = Vec(nPTWPorts, new TLBPTWIO)          // accel -> shared page-table walker
  val fpu_req   = Decoupled(new FPInput)                // accel -> core FPU
  val fpu_resp  = Flipped(Decoupled(new FPResult))
}
```

The two payloads. Note that `cmd` carries **values, not register numbers** —
`inst.rs1`/`inst.rs2` are along for the ride as metadata, while the operands
proper are the top-level `rs1`/`rs2`:

```scala
class RoCCCommand extends CoreBundle {
  val inst   = new RoCCInstruction   // the decoded 32-bit word
  val rs1    = Bits(xLen.W)          // 64-bit VALUE, already read from the regfile
  val rs2    = Bits(xLen.W)          // 64-bit VALUE
  val status = new MStatus           // privilege context at issue — needed by the accel's TLB
}

class RoCCResponse extends CoreBundle {
  val rd     = Bits(5.W)             // which register to write
  val data   = Bits(xLen.W)
}
```

Watch the shadowing: `cmd.bits.inst.rs1` is a register *number*,
`cmd.bits.rs1` is its *value*. Everyone trips on this once.

What each signal means:

| Signal | Direction | Meaning |
|---|---|---|
| `cmd` | core → accel | Issued **at writeback**, so non-speculative and in program order. Deasserting `ready` replays the instruction (§20). |
| `resp` | accel → core | Send **only** if `xd == 1`. Sending when `xd == 0` corrupts the register file; not sending when `xd == 1` hangs the core forever. |
| `busy` | accel → core | High while any issued work is outstanding. The core resolves `fence` against this — it is the entire CPU↔accelerator memory-ordering mechanism. Be conservative. |
| `interrupt` | accel → core | Raises a local interrupt line into the PLIC. |
| `exception` | core → accel | The core trapped; flush speculative state. Gemmini flushes its TLB here. |
| `mem` | accel ↔ L1 D$ | `HellaCacheIO`. One request per cycle. Tag with `io.mem.req.tag`; responses may return out of order. Max 6 tag bits (`dcacheReqTagBits`) — more causes hangs. |
| `ptw` | accel → PTW | Only needed if you instantiate your own TLB. |
| `fpu_req` / `fpu_resp` | accel ↔ FPU | Borrow the core's FPU; set `usesFPU = true`. |
| `status` | in `cmd.bits` | Privilege mode, `SUM`/`MXR`, `FS`/`XS`. A private TLB needs this for permission checks. |

Plus two diplomacy nodes on the `LazyRoCC` object itself, outside this bundle:

- `tlNode` — TileLink port straight to the L1–L2 crossbar. The high-bandwidth
  path; this is what Gemmini uses for its DMA.
- `atlNode` — TileLink port into a tile-local arbiter shared with the L1 I$
  backside.

#### What Gemmini actually connects

From `Controller.scala`:

| Port | Gemmini's use |
|---|---|
| `cmd` | every instruction |
| `resp` | `counter_access` only — `io.resp <> counters.io.out` |
| `mem` | **unused.** Gemmini's DMA does not go through the L1 D$; it takes its own TileLink node (`spad.id_node`, exposed as `tlNode` or `atlNode` per `use_dedicated_tl_port`) |
| `busy` | OR of the reservation station, scratchpad, loop unrollers and pending commands — this is what makes `fence` work (§20) |
| `interrupt` | wired to the TLB's exception output, but no handler exists — see §21 |
| `exception` | consumed to flush in-flight state when the core traps |
| `ptw` | `nPTWPorts = 2`, one per DMA TLB (`1` if `use_shared_tlb`) |
| `fpu_req` / `fpu_resp` | **unused.** Gemmini has its own arithmetic units |

The `mem` row matters for performance reasoning: a RoCC accelerator *may* borrow
the core's L1, but Gemmini deliberately does not, so scratchpad fills never
contend for D$ ports and never pollute it.

### 20. How Rocket issues and retires a RoCC instruction

Not at execute — **at writeback**. From `RocketCore.scala`:

```scala
io.rocc.cmd.valid      := wb_reg_valid && wb_ctrl.rocc && !replay_wb_common
io.rocc.cmd.bits.inst  := wb_reg_inst.asTypeOf(new RoCCInstruction())
io.rocc.cmd.bits.rs1   := wb_reg_wdata      // rs1, having passed through the ALU
io.rocc.cmd.bits.rs2   := wb_reg_rs2
io.rocc.cmd.bits.status := csr.io.status
io.rocc.exception      := wb_xcpt && csr.io.status.xs.orR
```

Four details worth knowing:

1. **`rs1` arrives via the ALU.** The `RoCCDecode` entry sets
   `A1_RS1, A2_ZERO, FN_ADD`, so the operand is computed as `rs1 + 0` and picked
   up as `wb_reg_wdata`. `rs2` has its own path. This costs nothing, but it is
   why `rs1` and `rs2` are not symmetric in the pipeline.
2. **`xs1`/`xs2` become `rxs1`/`rxs2`** in the decode table, gating the
   register-file read ports directly. The funct3 table in Appendix B is literally a
   register-port enable.
3. **Backpressure is a replay, not a stall.**
   ```scala
   val replay_wb_rocc = wb_reg_valid && wb_ctrl.rocc && !io.rocc.cmd.ready
   ```
   If the accelerator's command queue is full, the instruction is replayed from
   the top of the pipeline — a redirect and refetch, not a cheap in-place stall.
   Saturating Gemmini's queue is therefore more expensive than it looks, which
   reinforces §18.
4. **`xd = 1` uses the long-latency writeback port.** `wb_set_sboard` includes
   `wb_ctrl.rocc`, so `rd` is marked busy in the scoreboard and written later
   via `ll_arb`, arbitrated against the divider. Reading a Gemmini counter is a
   scoreboarded long-latency operation, like a `div`.

And the mechanism behind `gemmini_fence()` (§12):

```scala
val id_rocc_busy = usingRoCC.B &&
  (io.rocc.busy || ex_reg_valid && ex_ctrl.rocc ||
   mem_reg_valid && mem_ctrl.rocc || wb_reg_valid && wb_ctrl.rocc)
val id_do_fence = WireDefault(id_rocc_busy && (id_ctrl.fence || id_csr_rocc_write) || ...)
```

A plain RISC-V `fence` in decode stalls while the accelerator asserts `busy`
*or* any RoCC instruction is still in EX/MEM/WB. No custom instruction is needed
to synchronize with a RoCC accelerator — which is why `gemmini_fence()` is one
line of ordinary inline asm.

### 21. What RoCC does not give you

The gaps below are RoCC's, not Gemmini's, and any RoCC accelerator inherits
them.

**No architectural registers.** Nothing inside the accelerator can be named by a
register number. For Gemmini:

| Gemmini state | Addressable how? |
|---|---|
| Configuration (dataflow, strides, scales, activation) | Not addressable. Latched by `config_*`, write-only, no readback |
| Loop-instruction operands (bounds, addrs, strides) | Not addressable. Latched by `LOOP_*_CONFIG_*` |
| Scratchpad SRAM | By **32-bit local address** — a *value* in a CPU register, not a register number |
| Accumulator SRAM | Same |
| Reservation station, queues, systolic array PE registers | Not visible to software |

This is what resolves the puzzle the ISA tables create later: the instruction is
32 bits, the local address is *also* 32 bits, and they appear not to fit. They
never have to — one is a word in `.text`, the other is a bit field inside a
64-bit operand value (§4).

**No state save/restore.** Because configuration has no architectural name,
there is no way for an OS to read it out. A context switch with configuration
live — or a DMA in flight — is a correctness hazard the architecture simply does
not address.

**No precise traps.** The accelerator has no PC and retires asynchronously, so
it cannot fault an instruction. Gemmini's page-fault handling is the visible
consequence: `flush` (§12) offers only skip-or-retry, and while `io.interrupt`
is wired to the TLB, the source carries a telling commented-out assertion:

```scala
// assert(!io.interrupt, "Interrupt handlers have not been written yet")
```

**Asynchronous by construction.** `cmd` is `Decoupled`, so an accepted command
carries no completion guarantee. Gemmini queues commands through its reservation
station and executes them out of order (decoupled access/execute); ordering is
the programmer's job, via `busy`/`fence` or Gemmini's own dependency tracking.
The sole exception is `k_FLUSH`, which bypasses the queue.

#### Things that bite at the RoCC level

- **`busy` is load-bearing.** Deassert it while work is in flight and `fence`
  silently stops working — stale data, no error reported anywhere.
- **`xd` and `resp` must agree exactly.** A mismatch is a hang or silent
  corruption, never an exception.
- **In-order, non-speculative, at writeback.** You cannot receive a command out
  of order, but you also cannot receive one early.
- **No versioning.** `HellaCacheIO` in particular has gained signals across
  Rocket Chip releases.

#### Attaching an accelerator

```scala
class WithCustomAccelerator extends Config((site, here, up) => {
  case BuildRoCC => Seq((p: Parameters) => LazyModule(
    new CustomAccelerator(OpcodeSet.custom0 | OpcodeSet.custom1)(p)))
})
```

`OpcodeSet` is just a matcher over the four reserved values. `BuildRoCC` takes
one entry per accelerator, so four accelerators per core is the hard ceiling —
fewer if one claims several opcodes.

---

## Part VI — Practice

### 22. Hardware parameters the ISA exposes

Some ISA fields exist only if the hardware was elaborated with the matching
option. The `config_ld` scale field is the main one.

#### Optional mvin scaling

`mvin_scale_args` and `mvin_scale_acc_args` are elaboration-time options that
place `DIM` multipliers on the DMA path. They are programmed per-instruction
through `config_ld rs1[63:32]` (§8), and if they are absent the C helper
`scale_t_to_scale_t_bits` degenerates to a constant 0 (§16) — the same source
compiles either way, and the field is silently inert.

Distinguish them from `acc_scale_args`, the accumulator readout scaling, which
performs the standard int32 → int8 requantization and which essentially every
quantized-inference config keeps enabled.

mvin scaling earns its area when a tensor arrives at a different scale than the
one it was stored at:

- Bias / `D` loads into the accumulator at a per-layer scale.
- Residual and skip connections whose branches carry different quantization
  scales.
- Datatype conversion on load (fp32 in DRAM, int8 in the scratchpad).
- First-layer input normalization.
- Keeping one copy in DRAM instead of N pre-scaled copies.

`ScaleArguments` takes the scale *function*, not just "multiply" — a power-of-2
shift cuts the area cost sharply. `mvin_scale_shared` lets the scratchpad and
accumulator paths share multipliers when their scaling is identical.

Leave both as `None` for straight feed-forward int8 inference where every
rescale happens at accumulator readout. The DSE base config does exactly that
while keeping `acc_scale_args` enabled.

### 23. Gotchas

1. **Instructions are not in registers.** Instructions live in `.text`; only
   *operand values* live in the CPU register file. Gemmini has no architectural
   registers of its own (§21).
2. **Gemmini config state is not context-switchable.** No save/restore path
   exists — a RoCC gap, not a Gemmini oversight (§21).
3. **A bad `funct7` does not trap.** Rocket wildcards `[31:25]`, so an
   unimplemented opcode is forwarded to Gemmini rather than faulting (Appendix B).
4. **Operand roles swap between OS and WS** in `preload`/`compute` (§10).
5. **`GARBAGE_ADDR` is overloaded** — zero matrix, don't-care,
   suppress-writeback, and don't-reload-weights, depending on position. Read
   §7 before debugging.
6. **The accumulate bit is in the address, not the opcode.** Software toggles
   bit 30 of the C address.
7. **Loop instruction sequences must not be interleaved.** One latched register
   set (§11).
8. **`sys_shift` is OS-only** and silently meaningless in WS.
9. **`config_ld`'s `id` field** — forgetting it reconfigures `mvin` when you
   meant `mvin2`.
10. **Dataflow must match the elaborated hardware.** A WS-only build asked for
    OS at runtime produces wrong results, not an error. The same applies to a
    stale `gemmini_params.h` (§17).
11. **`flush` bypasses the queue** — fence yourself (§12).
12. **The instruction macros are braced blocks.** `if (c) gemmini_mvin(...);
    else ...` does not compile. Brace your call sites (§16).
13. **No `"memory"` clobber anywhere**, `gemmini_fence()` included. The compiler
    does not know Gemmini touches DRAM, so it may cache across a `mvout` or sink
    a store past a `mvin` (§16).
14. **Some `config_*` macro arguments are unparenthesized** — `A_transpose`,
    `B_transpose`, `q_const_type`, `stat_id` and friends. Pass identifiers or
    literals, not expressions (§16).
15. **`mvin2` is `funct7 = 1`, `mvin3` is `funct7 = 14`.** Not adjacent, and
    easy to transpose when reading old notes (§6).
16. **Narrow `config_*` variants reset what wider ones set.** `gemmini_config_ex`
    after `gemmini_extended3_config_ex` restores `acc_scale = 1.0` and
    `C_stride = 1` (§13). Call the widest variant, every time, once you have
    touched a wide field.

**Version stability.** `funct7` values 0–13 and the rows/cols/address packing
are stable across releases. Flag-bit positions inside `config_ex`, the `loop_*`
field layouts, and the `funct7` values above 14 have moved as features landed.
The example instruction words throughout assume operands in `a0`/`a1`; different
register allocation changes bits 24:15. Read `GemminiISA.scala` at your pinned
commit before encoding anything by hand.

### 24. Sources

| Topic | Where |
|---|---|
| Custom opcode reservation | RISC-V unprivileged ISA specification, opcode map |
| RoCC interface | `generators/rocket-chip/src/main/scala/tile/LazyRoCC.scala` |
| RoCC decode / issue | `rocket-chip` `rocket/IDecode.scala`, `rocket/CustomInstructions.scala`, `rocket/RocketCore.scala` |
| RoCC integration guide | Chipyard docs, *Adding a RoCC Accelerator* |
| RoCC prose (outdated) | UCSD RoCC documentation, linked from Chipyard |
| RoCC example accelerator | `ucb-bar/rocc-template` (SHA3) |
| Gemmini ISA | `generators/gemmini/src/main/scala/gemmini/GemminiISA.scala` |
| Gemmini port connections | `generators/gemmini/src/main/scala/gemmini/Controller.scala` |
| Gemmini config | `generators/gemmini/src/main/scala/gemmini/Configs.scala` |
| Gemmini ISA prose | Gemmini `README.md`, ISA section |
| C macros | `.../gemmini-rocc-tests/rocc-software/src/xcustom.h`, `.../include/gemmini.h` (**dev**) |
| Generated params | `.../gemmini-rocc-tests/include/gemmini_params.h` (build artifact) |
| Assembler opcode names | binutils `gas/config/tc-riscv.c` |
| Functional model | Spike Gemmini extension — an independent implementation of the same ISA, useful to diff against the RTL |

#### Where the RoCC specification lives

There is no official RoCC specification document.

| Source | What it gives you |
|---|---|
| `rocket-chip/src/main/scala/tile/LazyRoCC.scala` | The authoritative definition, plus `AccumulatorExample`, `CharacterCountExample`, `TranslatorExample`. |
| Chipyard docs, *Adding a RoCC Accelerator* | Practical integration guide: `BuildRoCC`, `OpcodeSet`, `HellaCacheIO`. |
| [UCSD RoCC documentation](https://docs.google.com/document/d/e/2PACX-1vRlO9BaoEVfrsn2fcefZrRLwDQjtf0fmbdhfYEAGCrac9XOjpyBnV8mZOcBSk9s5DjfXatPMDBppQGB/pub) | The only prose walkthrough. Chipyard itself flags it as outdated and inaccurate for many signals. |
| Rocket Chip tech report, UCB/EECS-2016-17 | The original written description. Dated. |
| `ucb-bar/rocc-template` (SHA3) | A minimal working accelerator to read end to end. |

---

## Appendices

### A. Complete funct7 map

Words assume operands in `a0`/`a1` and `rd = x0`; `funct7` alone is the
authoritative field. Values from `GemminiISA.scala` and `gemmini.h`.

| funct7 | Word (a0/a1) | Symbol | Group |
|---|---|---|---|
| 0 | `0x00B5307B` | `k_CONFIG` | Configuration (4-way sub-selected) |
| 1 | `0x02B5307B` | `k_MVIN2` | Data movement |
| 2 | `0x04B5307B` | `k_MVIN` | Data movement |
| 3 | `0x06B5307B` | `k_MVOUT` | Data movement |
| 4 | `0x08B5307B` | `k_COMPUTE_PRELOADED` | Execute |
| 5 | `0x0AB5307B` | `k_COMPUTE_ACCUMULATE` | Execute |
| 6 | `0x0CB5307B` | `k_PRELOAD` | Execute |
| 7 | `0x0E05307B` | `k_FLUSH` | Control — `rs2` is a constant 0, usually `x0` |
| 8 | `0x10B5307B` | `k_LOOP_WS` | CISC matmul — **trigger** |
| 9 | `0x12B5307B` | `k_LOOP_WS_CONFIG_BOUNDS` | CISC matmul — latch |
| 10 | `0x14B5307B` | `k_LOOP_WS_CONFIG_ADDRS_AB` | CISC matmul — latch |
| 11 | `0x16B5307B` | `k_LOOP_WS_CONFIG_ADDRS_DC` | CISC matmul — latch |
| 12 | `0x18B5307B` | `k_LOOP_WS_CONFIG_STRIDES_AB` | CISC matmul — latch |
| 13 | `0x1AB5307B` | `k_LOOP_WS_CONFIG_STRIDES_DC` | CISC matmul — latch |
| 14 | `0x1CB5307B` | `k_MVIN3` | Data movement |
| 15 | `0x1EB5307B` | `k_LOOP_CONV_WS` | CISC conv — **trigger** |
| 16–21 | `0x20`–`0x2A``B5307B` | `k_LOOP_CONV_WS_CONFIG_1…6` | CISC conv — latch |
| 22 | `0x2CB5307B` | `CLKGATE_EN` | clock-gating enable, recent versions only |
| 23 | `0x2EB5307B` | `k_MVOUT_SPAD` | Data movement |
| 24 | `0x30B5307B` | `k_LOOP_WS_CONFIG_SPAD_AB` | CISC matmul — latch (spad variant) |
| 25 | `0x32B5307B` | `k_LOOP_WS_CONFIG_SPAD_C` | CISC matmul — latch (spad variant) |
| 126 | funct3 = `0b111` | `k_COUNTER` | Control — the only instruction using `rd` |

> Note the numbering trap: `mvin2` is `funct7 = 1` and `mvin3` is `funct7 = 14`.
> They are not adjacent, and `mvin3` sits between the two loop-instruction
> blocks.

Configuration sub-selector, `rs1[1:0]`:

| Value | Symbol | README name | Target |
|---|---|---|---|
| 0 | `CONFIG_EX` | `config_ex` | Execute pipeline |
| 1 | `CONFIG_LOAD` / `CONFIG_LD` | `config_mvin` | Load pipeline |
| 2 | `CONFIG_STORE` / `CONFIG_ST` | `config_mvout` | Store pipeline |
| 3 | `CONFIG_NORM` / `CONFIG_BERT` | `config_norm` | I-BERT normalization |

Enums:

```c
OUTPUT_STATIONARY 0     NO_ACTIVATION 0     LAYERNORM 2     SOFTMAX 4
WEIGHT_STATIONARY 1     RELU          1     IGELU     3
GARBAGE_ADDR 0xFFFFFFFF
```

### B. What Rocket decodes, and what it doesn't

§3 gave the field layout. This section answers the question it raises: **does a
RoCC instruction mean something different when `funct3` changes?**

#### No — `funct3` retargets the core, not the accelerator

The accelerator's operation is selected by `funct7`, and by nothing else.
Gemmini's decoder switches on `cmd.bits.inst.funct`; the names `xd`, `xs1` and
`xs2` do not appear anywhere in its RTL. Re-encode the same `funct7` with a
different legal `funct3` and the accelerator performs exactly the same
operation — what changes is the work the *core* does around it.

So `funct3` is not an opcode modifier. It is three independent yes/no questions
that the instruction answers on the core's behalf:

| Bit | The question it answers | Consequence in Rocket |
|---|---|---|
| `xs1` | "Does this instruction depend on `rs1`?" | If 1, it interlocks against any pending write to `rs1`. If 0, it does not wait. |
| `xs2` | Same for `rs2`. | Same — plus, with `xs2 = 0`, the `rs2` value is never even captured. |
| `xd` | "Will a result come back into `rd`?" | If 0, **asynchronous call**: the instruction retires immediately and nothing waits on it. If 1, **synchronous call**: `rd` is marked busy in the scoreboard and any later reader of `rd` stalls until the accelerator's response lands. |

`xd` is the bit that changes the character of the call — a procedure that
returns a value versus one that does not, exactly as you framed it. One
refinement: the stall is *consumer-side*. Even with `xd = 1` the core does not
block at the RoCC instruction itself, only at the next instruction that reads
`rd` — the same scoreboarded long-latency path a `div` uses (§20).

#### What the core actually does with those bits

Three details, because they are easy to assume wrong:

- **Register reads are unconditional.** `rf.read` is issued for both source
  fields regardless of `xs1`/`xs2` — reads are free. What the bits gate is the
  *hazard* logic: `hazard_targets` includes `rs1` only when `rxs1` is set, `rs2`
  only when `rxs2`, and `rd` only when `wxd`.
- **`xs = 0` does not mean "the operand is zero".** With `xs2 = 0` the pipeline
  never captures `rs2` at all — `mem_reg_rs2` is written only
  `when (ex_ctrl.rxs2 && ... ex_ctrl.rocc)` — so `cmd.bits.rs2` carries a stale
  leftover. You cannot use `xs2 = 0` to pass a zero; it says "I will not look at
  this", nothing more.
- **`xd` and the accelerator's response must agree exactly.** Nothing checks it.
  `xd = 1` with no response hangs the core forever; `xd = 0` with a response
  corrupts whatever register that response names. `funct3` is a contract between
  core and accelerator, not a request the accelerator may interpret.

#### Why the decode table looks like six instructions

`funct3` is 3 bits, so 8 combinations are expressible. Rocket's decode table
admits **six of them per custom opcode** and omits two. They appear as six named
rows only because a Chisel decoder matches fixed bit patterns:
`rocket/CustomInstructions.scala` spells out all six for each of the four custom
opcodes — 24 rows in total, differing only in bits `[14:12]`:

```scala
def CUSTOM3            = BitPat("b?????????????????000?????1111011")
def CUSTOM3_RS1        = BitPat("b?????????????????010?????1111011")
def CUSTOM3_RS1_RS2    = BitPat("b?????????????????011?????1111011")
def CUSTOM3_RD         = BitPat("b?????????????????100?????1111011")
def CUSTOM3_RD_RS1     = BitPat("b?????????????????110?????1111011")
def CUSTOM3_RD_RS1_RS2 = BitPat("b?????????????????111?????1111011")
```

| funct3 | `xd xs1 xs2` | Decode-table row | Call shape |
|---|---|---|---|
| `0x0` | `0 0 0` | `CUSTOM3` | async, no operands |
| `0x2` | `0 1 0` | `CUSTOM3_RS1` | async, one operand — legal, but `gemmini.h` emits nothing with it |
| `0x3` | `0 1 1` | `CUSTOM3_RS1_RS2` | async, two operands — **every Gemmini instruction but `counter_access`** |
| `0x4` | `1 0 0` | `CUSTOM3_RD` | returns a value, no operands |
| `0x6` | `1 1 0` | `CUSTOM3_RD_RS1` | returns a value, one operand |
| `0x7` | `1 1 1` | `CUSTOM3_RD_RS1_RS2` | returns a value, two operands — **`counter_access` only** |

In `RoCCDecode` (`rocket/IDecode.scala`) those six control lists are identical
except in three signals — `rxs1`, `rxs2`, `wxd`, precisely the three questions
above. Everything else (`rocc = Y`, `A1_RS1`, `A2_ZERO`, `FN_ADD`) is the same
in all six, which is why §20 can say `rs1` always arrives through the ALU
regardless of encoding.

The two missing combinations are `0x1` and `0x5` — `xs2` without `xs1`. They are
absent from the table, so **you cannot read `rs2` without also reading `rs1`**;
those encodings trap as illegal instructions. And because a combination is
picked rather than parameterized, `xcustom.h` has one macro per usable shape
instead of an argument (§16).

#### And what Rocket never looks at

- **`funct7` is never checked.** The decode BitPat is
  `b?????????????????011?????1111011` — bits `[31:15]` and `[11:7]` are all
  wildcards. An unimplemented `funct7` is therefore **not** an illegal
  instruction; Rocket forwards it happily and the accelerator's behaviour is
  whatever its own decoder does with an unknown value. There is no
  architectural fault path for a bad Gemmini opcode.
- **7 bits of `funct7` = 128 operations** per custom opcode. Gemmini uses about
  30 (§6).
