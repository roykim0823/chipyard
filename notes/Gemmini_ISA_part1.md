# Gemmini ISA — from RoCC up

A single reference for the Gemmini accelerator's instruction set, built from the
bottom: what RISC-V reserves, what Berkeley's RoCC interface layers on top of
that reservation, how a line of C becomes a RoCC command, and what Gemmini puts
inside its slice of the encoding space.

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
numbered continuously, `§1`–`§24`, across both files; the parts are grouping
only.

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
   accel op     5-bit register NUMBERS    5-bit    custom-0..3
                        \_____________/
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