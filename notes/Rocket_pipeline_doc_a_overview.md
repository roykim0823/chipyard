# Rocket Core Pipeline Overview (Doc A)

> **Where this document sits**
> This document defines the **common pipeline foundation** of the Rocket scalar core and serves as the reference base for the series.
> The follow-up document, "Per-Instruction-Type Execution Trace (Doc B)" — ALU / Load·Store / Mul·Div / Branch / FP — assumes the
> **shared vocabulary and skeleton** defined here, and describes only the **differences (diffs)** of each type.
> Reference source: [`RocketCore.scala`](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala)
> (under `generators/rocket-chip/src/main/scala/rocket/` in Chipyard)

---

## 0. How to read this document (conventions used by the whole series)

- Signal names are the identifiers used in the source, verbatim. For example: `ex_reg_valid`, `id_ctrl.mem_cmd`.
- Stage prefix convention: `id_*` (Decode) → `ex_*` (Execute) → `mem_*` (Memory) → `wb_*` (Writeback).
- **Only names containing `*_reg_*` are actual pipeline registers** (see §6). Most `id_*` names are combinational wires.
- Code locations are given as `file:line` links. Links are relative paths from the workspace root (`chipyard/`).
- **Scope:** the scalar **integer pipeline (ID~WB)**. The vector unit, RoCC, and trace/debug are covered only as boundaries and overviews (§3~§4).
- **Reference configuration:** RV64GC with virtual memory (`useVM`), `pipelinedMul`, and other defaults are assumed (§5).
- **Reference source revision:** rocket-chip `55bcad0f5` (`v1.6-819-g55bcad0f5`), the revision pinned by the Chipyard superproject at `db852f0a`. Line numbers are valid for that snapshot and may shift in other versions.

---

## 1. The big picture: core boundary and the five stages

### 1.1 What the 5-stage pipeline is

The classic RISC 5-stage organization splits the processing of one instruction into five steps — `IF` (fetch) → `ID` (decode and register read) → `EX` (execute) → `MEM` (memory access) → `WB` (register write). Pipelining raises throughput by letting different instructions **occupy the stages simultaneously**, one per cycle. Rocket is an **in-order, single-issue (one instruction issued per cycle), scalar** implementation of that structure.

### 1.2 The core boundary (IF lives outside the core)

Of the familiar `IF → ID → EX → MEM → WB` sequence, **IF (Instruction Fetch) is not present in this file (RocketCore.scala).** Fetch is handled by a separate module, the **Frontend**, and the core picks up the pipeline at the instruction buffer (**IBuf**) boundary.

```
        ┌─────────────────────────── Frontend (separate module) ────────────────────────┐
        │   PC gen → I$ (ICache) → BTB/BHT/RAS prediction → IBuf                        │
        └───────────────────────────────┬───────────────────────────────────────────────┘
                                         │  io.imem.resp    (RocketCore.scala:321)
                                         ▼
   ┌── the pipeline in RocketCore.scala ──────────────────────────────────────────────┐
   │                                                                                  │
   │   ID (Decode)          EX (Execute)        MEM (Memory)        WB (Writeback)    │
   │   ─ comb. wires ─       ─ ex_reg_* ─         ─ mem_reg_* ─       ─ wb_reg_* ─       │
   │                                                                                  │
   │   fetch from IBuf      ALU / Mul·Div        branch resolution    RegFile write    │
   │   decode(id_ctrl)      address calc (adder) take_pc_mem          CSR read/write   │
   │   RegFile read         dmem.req (s0)        dmem.s1_data (s1)    dmem.s2_* (s2)   │
   │   hazard/stall logic   cmp_out              exception gathering  exception commit │
   │                                                                                  │
   └──────────┬───────────────────┬──────────────────┬────────────────┬──────────────┘
              │ io.dmem (HellaCache: s0=EX, s1=MEM, s2=WB)             │
              │ io.fpu   io.ptw   io.rocc   io.vector                  │
              ▼                                                        ▼
          external hardware blocks (§3)                       redirect PC → io.imem.req
```

**A key asymmetry, which recurs throughout Doc B:**

| Stage | Character | Evidence |
|---|---|---|
| ID | **A combinational stage.** The instruction coming out of the IBuf is decoded, its registers read, and its hazards resolved within the same cycle. Most `id_*` names are wires. | `id_ctrl` = `Wire(...)` [RocketCore.scala:327](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L327) |
| EX | **The first pipeline latch.** Values are captured in registers for the first time at the `ID→EX` boundary (`ex_reg_*`, `ex_ctrl`). | [RocketCore.scala:248-267](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L248-L267) |
| MEM | Pipeline latch (`mem_reg_*`). Branch and exception resolution is finalized here. | [RocketCore.scala:269-291](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L269-L291) |
| WB | Pipeline latch (`wb_reg_*`). RegFile write and exception commit. | [RocketCore.scala:293-311](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L293-L311) |

> ⚠️ Consequently there is **no symmetric `id_reg_*` register group.** The only actual registers in the ID stage are a handful of state bits such as
> `id_reg_fence` and `id_reg_pause` ([:166](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L166), [:339](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L339)).

---

## 2. Background

This section collects the minimum background needed for the rest of the document as **concept summaries plus code links**. Detailed implementations are covered by their own files (the predictor internals and the D$ internals are out of scope here).

### 2.1 Pipelining and hazard basics

- **in-order, single-issue, scalar**: up to five instructions coexist in different stages in a given cycle.
- Values cross stage boundaries through **pipeline registers** (§6). Data hazards are covered by **forwarding/bypass** (§9); what forwarding cannot cover becomes a **stall** (§10). Control hazards (branches) are handled by **prediction plus mispredict flush** (§11).

### 2.2 Branch prediction (BTB / BHT / RAS)

- **BTB** (Branch Target Buffer): caches PC → predicted target and CFI type. **BHT** (Branch History Table): predicts the direction (taken/not-taken) of conditional branches. **RAS** (Return Address Stack): pushes the return address on a `call` and pops it on a `ret`.
- In Rocket the **BHT and RAS are contained inside the `BTB` module**, and that module **lives in the Frontend**. Update kinds are distinguished by `CFIType` (call/ret/jump/branch).
- **Boundary with the core**: the core only consumes prediction results (`*_reg_btb_resp`) and sends updates (`btb_update`/`bht_update`); it **ties off the RAS** (§11.3).
- Code: [`BTB.scala`](generators/rocket-chip/src/main/scala/rocket/BTB.scala) — `BTB` ([:185](generators/rocket-chip/src/main/scala/rocket/BTB.scala#L185)), `BHT` ([:75](generators/rocket-chip/src/main/scala/rocket/BTB.scala#L75)), `RAS` ([:39](generators/rocket-chip/src/main/scala/rocket/BTB.scala#L39)).

### 2.3 The D-cache layer: HellaCache (DCache vs NonBlockingCache)

- `HellaCache` is an **abstract interface** (`HellaCacheIO`) with two implementations:
  - **`DCache`** ([DCache.scala](generators/rocket-chip/src/main/scala/rocket/DCache.scala)): pipelined, with ECC support. Selected when `nMSHRs == 0`.
  - **`NonBlockingCache`** ([NBDcache.scala](generators/rocket-chip/src/main/scala/rocket/NBDcache.scala)): the traditional multiple-**MSHR** (Miss Status Handling Register) organization. Selected when `nMSHRs ≥ 1`.
  - The choice is made by `HellaCacheFactory` from the parameters ([HellaCache.scala:263-266](generators/rocket-chip/src/main/scala/rocket/HellaCache.scala#L263-L266)); a scratchpad mode also exists.
- **Both expose the same `HellaCacheIO` (s0/s1/s2)** → the core (this document) never distinguishes between the implementations.
- What **non-blocking** means here: the pipeline keeps going even on a miss, late completion arrives through `resp.replay`, and a rejection arrives as `s2_nack` (§12, and Doc B Load/Store).

### 2.4 Virtual memory (TLB / PTW / PMP)

- **TLB**: caches virtual→physical translations. The **D-TLB sits inside the D$**, and the **I-TLB sits inside the Frontend**.
- **PTW** (Page Table Walker, `io.ptw`): walks the page table on a TLB miss. **PMP** (Physical Memory Protection): checks access permissions on physical addresses.
- The associated faults (page/access/misaligned) are committed in WB (§13).

---

## 3. Map of the external interfaces (interaction with hardware outside the core)

The core IO is defined at [RocketCore.scala:138-154](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L138-L154).
When a per-type document in Doc B covers "interaction with hardware outside the core", it **links here** and describes only the type-specific differences.

| IO port | Peer block | In which stage | Main use | Related types |
|---|---|---|---|---|
| `io.imem` | **Frontend** (I$, BTB/BHT/RAS, IBuf) | ID (receive) / MEM·WB (send redirect and updates) | instruction fetch, predictor updates, redirect | Branch, Jump |
| `io.dmem` | **HellaCache** (D$, DTLB) | **EX(s0)→MEM(s1)→WB(s2)** | load/store request, response, nack, exception | Load/Store, AMO |
| `io.fpu` | **FPU** | ID through WB | floating-point operations, decoupled writeback | FP |
| `io.ptw` | **PTW** (Page Table Walker) | (translation goes via the TLB) | page table walk, PMP/status | Load/Store (VM) |
| `io.rocc` | **RoCC** accelerator | WB (command issue) | custom instruction offload | (out of scope) |
| `io.vector` | **Vector unit** | EX~WB | vector instructions | (out of scope) |

> **Scope decision for the predictors (BTB/BHT/RAS):** Doc B covers them **only up to the core interface** (the concepts are in §2.2).
> That is, it describes the `io.imem.btb_update` / `io.imem.bht_update` the core sends and the `*_reg_btb_resp` it consumes, and nothing more;
> the internal structure of the prediction hardware and the **RAS** are the responsibility of the Frontend and [`BTB.scala`](generators/rocket-chip/src/main/scala/rocket/BTB.scala), which are linked rather than described.
> The reason: the core never touches the RAS directly and ties it off —
> `io.imem.ras_update := DontCare` [RocketCore.scala:1121-1122](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1121-L1122).

---

## 4. Modules the core instantiates (module inventory)

These are the hardware blocks that `Rocket` (more precisely the inner `RocketImpl`) instantiates directly. Whereas the **external blocks (§3)** are not created by the core but connected by the tile, the modules below are created inside the core.

| Module | Instantiation site | Role | Related section |
|---|---|---|---|
| `IBuf` | [:317](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L317) | Frontend↔core instruction buffer (RVC expansion and alignment) | §7.1 |
| `CSRFile` (`csr`) | [:347](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L347) | CSRs, exceptions, interrupts, privilege state | §13, §7.4 |
| `BreakpointUnit` (`bpu`) | [:421](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L421) | hardware breakpoints/watchpoints | §13 |
| `ALU` (`alu`) | [:511](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L511) | arithmetic, logic, shift, compare, address addition | §7.2 |
| `MulDiv` (`div`) | [:518](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L518) | division (plus non-pipelined multiplication) | §7.2, §12 |
| `PipelinedMultiplier` (`mul`, optional) | [:526](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L526) | fixed-latency multiplication (`pipelinedMul`) | §7.2 |
| `Arbiter` (`ll_arb`) | [:784](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L784) | long-latency writeback arbitration (div/rocc/vec) | §12 |
| `RegFile` (`rf`, a plain class) | [:342](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L342) | integer register file | §7.1, §7.4 |
| `Scoreboard` ×2 (plain classes) | [:1007](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1007), [:1043](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1043) | tracks integer/FP long-latency destinations | §12 |
| (optional) vector decoder / `TraceCoreIngress` / `DebugROB` | [:357](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L357) / [:835](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L835) / [:962](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L962) | vector decode / trace / debug ROB | (out of scope) |

- **`Module` vs plain class**: `RegFile` and `Scoreboard` are not Chisel `Module`s but helper classes that create their `Mem`/`Reg` state directly inside the core module.
- **Clock gating**: most of the logic above lives in the inner class `RocketImpl`, and the whole of it is wrapped in `withClock(gated_clock)` so that it falls into the clock-gating domain ([:169-173](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L169-L173), [:1306](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1306)). The intent is to stop the clock while the core is idle to save power.
  - **The gating is off by default.** It is controlled by `clockGate` (`RocketCoreParams`), whose default is `false` ([:52](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L52)), and `gated_clock` is then simply `clock` — no `ClockGate` cell is emitted at all ([:169-171](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L169-L171)). The logic that computes the enable (`clock_en`, `long_latency_stall`, `imem_might_request_reg`) is likewise generated only under `if (rocketParams.clockGate)` ([:1198](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1198)). Turning it on takes the `WithCoreClockGatingEnabled` fragment.
  - So in the reference configuration of §5 the "gated-clock domain" is a **structural boundary only**, and every stage is clocked normally. The CSR file takes a separate `ungated_clock` in either case, since its time counter must keep running ([:857](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L857)).

---

## 5. Configuration parameters (RocketCoreParams)

The line numbers and the descriptions here assume a particular configuration. The core's configuration knobs live in `RocketCoreParams` ([:16-62](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L16-L62)).

| Parameter | Meaning | Assumed here |
|---|---|---|
| `xLen` | register/data width | 64 (RV64) (default) |
| `useCompressed` | RVC (16-bit compressed instructions) | on (default) |
| `fpu` | FPU presence and configuration | on, `fLen = 64` → F and D (default) |
| `mulDiv` | multiply/divide unit | on (default) |
| `pipelinedMul` | fixed-latency multiplier (derived, not a knob) | on — **not** a default; requires `mulUnroll == xLen` |
| `useVM` | virtual memory (TLB/PTW) | on, Sv39 (`pgLevels = 3`) (default) |
| `useAtomics` | A extension (AMO/LR-SC) | on (default) |
| `useVector` | vector extension | off (default) — out of scope |
| `nBreakpoints` / `nPMPs` | number of hardware breakpoints / PMPs | 1 / 8 (default) |
| `fastLoadWord` / `fastLoadByte` | load bypass latency | on / off (default) |
| `clockGate` | core clock gating | off (default) — see §4 |

> In other words, this document assumes roughly an **RV64GC + VM + pipelined-mul** configuration. Other configurations can change some paths (for example non-pipelined multiplication, or the address path when VM is disabled).

**Relation to the stock configurations:** the closest fragment is `WithNBigCores(n)`, which overrides only `mulDiv` and leaves every other row above at its default. Chipyard's own `RocketConfig` (= `WithNHugeCores(1)` + `AbstractConfig`) sits one step further away: it additionally enables Zba/Zbb/Zbs and sets `fpu = FPUParams(minFLen = 16)` — the two knobs that surface in the ALU result mux (`Zbb`) and in the FP load-response path (`minFLen`). Neither fragment yields `pipelinedMul`; that takes an explicit `WithCustomFastMulDiv(mUnroll = 64)` ([Configs.scala](generators/rocket-chip/src/main/scala/rocket/Configs.scala)).

---

## 6. Pipeline register naming convention

At each stage boundary the values are **copied** into the next register group. What follows is the "shared register dictionary" that all of Doc B refers to.

### 6.1 Control signal registers (the decoded `IntCtrlSigs` flowing down)

```
id_ctrl ──(ID→EX)──▶ ex_ctrl ──(EX→MEM)──▶ mem_ctrl ──(MEM→WB)──▶ wb_ctrl
```
- Definition: [RocketCore.scala:248-250](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L248-L250)
- Copies: `ex_ctrl := id_ctrl` ([:538](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L538)), `mem_ctrl := ex_ctrl` ([:648](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L648)), `wb_ctrl := mem_ctrl` ([:717](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L717))
- The meaning of each field (`alu_fn`, `mem_cmd`, `wxd`, …) is in §8.

### 6.2 Data/state registers (only the frequently referenced ones)

| ID (comb.) | EX | MEM | WB | Meaning |
|---|---|---|---|---|
| `ibuf.io.pc` | `ex_reg_pc` | `mem_reg_pc` | `wb_reg_pc` | instruction PC |
| `id_inst(0)` | `ex_reg_inst` | `mem_reg_inst` | `wb_reg_inst` | instruction bits |
| `id_rs` (RegFile read) | `ex_rs(0/1)` | `mem_reg_rs2` | `wb_reg_rs2` | source operands |
| — | `alu.io.out` | `mem_reg_wdata` | `wb_reg_wdata` | operation result (writeback candidate) |
| — | `ex_reg_valid` | `mem_reg_valid` | `wb_reg_valid` | stage valid |
| — | `ex_reg_cause` | `mem_reg_cause` | `wb_reg_cause` | exception cause |
| — | `ex_reg_btb_resp` | `mem_reg_btb_resp` | — | prediction result (Branch/Jump) |
| — | — | `mem_br_taken` | `wb_reg_br_taken` | actual branch direction |

- `valid` updates: `ex_reg_valid := !ctrl_killd` ([:532](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L532)), `mem_reg_valid := !ctrl_killx` ([:638](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L638)), `wb_reg_valid := !ctrl_killm` ([:712](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L712)). → **A kill signal clearing the next `valid`** is the basic mechanism of pipeline invalidation.
- The data registers are copied inside conditional `when` blocks (ID→EX: [:537-599](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L537-L599), EX→MEM: [:645-682](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L645-L682), MEM→WB: [:716-734](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L716-L734)).

---

## 7. The per-stage skeleton

Each per-type document follows this order (ID→EX→MEM→WB) and links back to the subsections below for everything it shares.
The code excerpts below are taken from [`RocketCore.scala`](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala), with the original line number prefixed to each line. Where line numbers skip, intervening lines have been elided (`/* … */`) for readability.

### 7.1 ID (Decode) — the combinational stage

**① Fetch (the IBuf boundary ← Frontend):** the IBuf takes the Frontend response (`io.imem.resp`) and provides an aligned instruction. When `take_pc` asserts, the IBuf is flushed along with the pipeline.

```scala
317  val ibuf = Module(new IBuf)
318  val id_expanded_inst = ibuf.io.inst.map(_.bits.inst)
319  val id_raw_inst = ibuf.io.inst.map(_.bits.raw)
320  val id_inst = id_expanded_inst.map(_.bits)
321  ibuf.io.imem <> io.imem.resp        // Frontend → core
322  ibuf.io.kill := take_pc             // flush the IBuf on a redirect
```

**② Decode:** the instruction bits pass through the decode table, yielding the combinational wire `id_ctrl` (an `IntCtrlSigs`). → §8.

```scala
327  val id_ctrl = Wire(new IntCtrlSigs).decode(id_inst(0), decode_table)
```

**③ Register read:** the RegFile is read (combinationally) at the source addresses selected by `id_ctrl.rxs1/rxs2`.

```scala
340  val id_ren   = IndexedSeq(id_ctrl.rxs1, id_ctrl.rxs2)
341  val id_raddr = IndexedSeq(id_raddr1, id_raddr2)
342  val rf       = new RegFile(regAddrMask, xLen)
343  val id_rs    = id_raddr.map(rf.read _)   // the two source operands
```

**④ Hazard/stall/kill resolution:** computes the conditions that stall ID (`ctrl_stalld`) and the conditions that invalidate it (`ctrl_killd`). → §10.

```scala
1061  val ctrl_stalld =
1062    id_ex_hazard || id_mem_hazard || id_wb_hazard || id_sboard_hazard ||
1063    id_vconfig_hazard ||
        /* … single-step / fp·vec busy / D$·RoCC blocked / div busy … */
1072    id_do_fence ||
1073    csr.io.csr_stall ||
1074    id_reg_pause ||
1075    io.traceStall
1076  ctrl_killd := !ibuf.io.inst(0).valid || ibuf.io.inst(0).bits.replay ||
                   take_pc_mem_wb || ctrl_stalld || csr.io.interrupt
```

**⑤ Exception detection (first pass):** interrupts, breakpoints, fetch faults, illegal instructions, and so on are gathered in priority order. → §13.

```scala
431  val (id_xcpt, id_cause) = checkExceptions(List(
432    (csr.io.interrupt, csr.io.interrupt_cause),
434    (bpu.io.xcpt_if,   Causes.breakpoint.U),
435    (id_xcpt0.pf.inst, Causes.fetch_page_fault.U),
       /* … fetch guest/access fault, virtual insn … */
442    (id_illegal_insn,  Causes.illegal_instruction.U)))
```

### 7.2 EX (Execute)

**① Operand selection:** `ex_rs` picks the forwarding-mux value when a bypass is needed, and the latched register value otherwise (→ §9). The ALU inputs are then chosen according to `sel_alu1/2/imm`.

```scala
475  val ex_rs = for (i <- 0 until id_raddr.size)
476    yield Mux(ex_reg_rs_bypass(i), bypass_mux(ex_reg_rs_lsb(i)),
                 Cat(ex_reg_rs_msb(i), ex_reg_rs_lsb(i)))
477  val ex_imm = ImmGen(ex_ctrl.sel_imm, ex_reg_inst)
479  val ex_op1 = MuxLookup(ex_ctrl.sel_alu1, 0.S)(Seq(
480    A1_RS1 -> ex_rs(0).asSInt,
481    A1_PC  -> ex_reg_pc.asSInt,
482    A1_RS1SHL -> /* Zba */ ))
485  val ex_op2 = MuxLookup(ex_ctrl.sel_alu2, 0.S)(Seq(
486    A2_RS2  -> ex_rs(1).asSInt,
487    A2_IMM  -> ex_imm,
488    A2_SIZE -> Mux(ex_reg_rvc, 2.S, 4.S)))
```

**ImmGen — immediate generation:** when `sel_alu2 = A2_IMM`, the `ex_imm` fed into `ex_op2` is produced by `ImmGen`. Because the immediate field is laid out differently in each instruction format (six of them: I/S/SB/UJ/U/Z), `ImmGen` rearranges the instruction bits according to `sel_imm` and **sign-extends** them into a single `xLen` value. In other words, EX needs no separate immediate unit — one pure combinational function covers every format.

```scala
1370  object ImmGen {
1371    def apply(sel: UInt, inst: UInt) = {
1372      val sign   = Mux(sel === IMM_Z, 0.S, inst(31).asSInt)          // top sign bit
1373      val b30_20 = Mux(sel === IMM_U, inst(30,20).asSInt, sign)      // real upper bits for U-type only
1374      val b19_12 = Mux(sel =/= IMM_U && sel =/= IMM_UJ, sign, inst(19,12).asSInt)
1375      val b11    = Mux(sel === IMM_U || sel === IMM_Z, 0.S,
1376                   Mux(sel === IMM_UJ, inst(20).asSInt,
1377                   Mux(sel === IMM_SB, inst(7).asSInt, sign)))        // bit 11 sits elsewhere per format
        /* … b10_5 / b4_1 / b0 are likewise extracted from format-specific inst fields … */
1386      Cat(sign, b30_20, b19_12, b11, b10_5, b4_1, b0).asSInt
      }
   }
```

- Format mapping in brief: `IMM_I` = I-type (12b), `IMM_S` = store offset, `IMM_SB` = branch offset (§11), `IMM_UJ` = JAL offset, `IMM_U` = upper 20b (LUI/AUIPC), `IMM_Z` = CSR zimm (no sign extension).
- The same `ImmGen` is reused for the branch/jump target computation in the MEM stage (§7.3 ②, `ImmGen(IMM_SB, …)` / `ImmGen(IMM_UJ, …)`).

**② ALU:** the selected `ex_op1/op2` enter the ALU, and `alu_fn`/`alu_dw` determine the operation. The output is consumed in three ways — the arithmetic result `out`, the branch comparison `cmp_out`, and the memory address `adder_out`.

```scala
511  val alu = Module(new ALU)
512  alu.io.dw  := ex_ctrl.alu_dw
513  alu.io.fn  := ex_ctrl.alu_fn
514  alu.io.in2 := ex_op2.asUInt
515  alu.io.in1 := ex_op1.asUInt
```

**③ Mul/Div issue:** multiplication and division are issued to the long-latency units here. Division always goes to `MulDiv` (variable latency); multiplication takes one of two paths depending on the configuration. → Doc B (Mul/Div).

```scala
519  div.io.req.valid    := ex_reg_valid && ex_ctrl.div
520  div.io.req.bits.dw  := ex_ctrl.alu_dw
521  div.io.req.bits.fn  := ex_ctrl.alu_fn
522  div.io.req.bits.in1 := ex_rs(0)
523  div.io.req.bits.in2 := ex_rs(1)
524  div.io.req.bits.tag := ex_waddr         // rd carried as a tag (for out-of-order completion)
```

**Pipelined multiplier (optional):** with `pipelinedMul`, multiplication is handled not by `MulDiv` but by a separate **fixed-latency pipelined multiplier**, and `MulDiv` becomes division-only (`mulUnroll = 0`). At issue the request payload simply reuses `div.io.req.bits`.

```scala
222  val pipelinedMul = usingMulDiv && mulDivParams.mulUnroll == xLen
518  val div = Module(new MulDiv(if (pipelinedMul) mulDivParams.copy(mulUnroll = 0) else mulDivParams, width = xLen))
525  val mul = pipelinedMul.option {
526    val m = Module(new PipelinedMultiplier(xLen, 2))
527    m.io.req.valid := ex_reg_valid && ex_ctrl.mul
528    m.io.req.bits  := div.io.req.bits    // shares the div request payload
529    m
530  }
```

- **Difference in the multiply result path**: the pipelined multiplier has a fixed latency, so its result enters the `rf_wdata` mux in WB directly through the `wb_ctrl.mul` path (§7.4 ③, [:830](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L830)), whereas division (and non-pipelined multiplication) has variable latency and writes back out of order through the `ll_arb` path (§12).

**④ Memory request issue (s0):** for a `mem` instruction, the request is sent to the D$ during the EX cycle. There is **no dedicated address adder — the ALU is reused** for address computation: loads and stores are decoded with `sel_alu1=A1_RS1`, `sel_alu2=A2_IMM`, `alu_fn=FN_ADD`, so `alu.io.adder_out = base(rs1) + offset(imm)` is the effective address. That value passes through `encodeVirtualAddress` (an internal `Mux` that compresses the VA to `vaddrBits+1`, [:1320-1327](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1320-L1327)) on its way to `io.dmem.req.bits.addr`. → Doc B (Load/Store).

```scala
1160  io.dmem.req.valid     := ex_reg_valid && ex_ctrl.mem
1161  val ex_dcache_tag = Cat(ex_waddr, ex_ctrl.fp)      // rd + fp flag → response routing tag
1163  io.dmem.req.bits.tag  := ex_dcache_tag
1164  io.dmem.req.bits.cmd  := ex_ctrl.mem_cmd            // M_XRD / M_XWR / AMO …
1165  io.dmem.req.bits.size := ex_reg_mem_size            // LB/LH/LW/LD
1166  io.dmem.req.bits.signed := !Mux(ex_reg_hls, ex_reg_inst(20), ex_reg_inst(14))
1168  io.dmem.req.bits.addr := encodeVirtualAddress(ex_rs(0), alu.io.adder_out)  // ← the ALU adder result
1172  io.dmem.req.bits.no_resp := !isRead(ex_ctrl.mem_cmd) || (!ex_ctrl.fp && ex_waddr === 0.U)
```

- The same sum **fans out to two places**: `alu.io.adder_out` becomes the request address above, while `alu.io.out` (for a `mem` instruction `alu_fn` is `FN_ADD`, so `out == adder_out`) is latched into `mem_reg_wdata` (§7.3 ①) and reused downstream as the **effective address** (breakpoint comparison `bpu.io.ea`, `tval` on an exception, and so on).
- `no_resp`: when there is no value to return to a register — a store, or a load with `rd=x0` — the response is suppressed.

**⑤ Replay/kill:** a structural hazard (D$ or div not ready) or a load-use miss replays EX; a redirect, a replay, or an invalid stage clears the next `valid`.

```scala
604  val replay_ex_structural = ex_ctrl.mem && !io.dmem.req.ready ||
605                             ex_ctrl.div && !div.io.req.ready || /* vec */
607  val replay_ex_load_use   = wb_dcache_miss && ex_reg_load_use
608  val replay_ex  = ex_reg_replay || (ex_reg_valid && (replay_ex_structural || replay_ex_load_use))
609  val ctrl_killx = take_pc_mem_wb || replay_ex || !ex_reg_valid
```

### 7.3 MEM (Memory)

> **It is called the "Memory" stage, but it is not reserved for memory instructions.** Every instruction passes through it.
> - **Memory instructions**: interact with D$ s1 (supplying store data, or killing the access); the response arrives in the next stage, WB (s2) (③).
> - **Non-memory instructions (ALU, mul/div, CSR, …)**: the s1 signals are meaningless (`mem_ctrl.mem=0` → no `io.dmem` request), and the stage is a **pass-through that carries the EX result `mem_reg_wdata` down to WB**.
> - **Branches and jumps**: instead of arithmetic, this is where the direction is resolved and the redirect is finalized (②, §11).

The integer result handed to WB is finally selected at the end of the stage as `mem_int_wdata` — normally `mem_reg_wdata`, except for a path that swaps in `mem_br_target` to handle a jump's link value (detailed in Doc B (JAL/JALR)):

```scala
631  val mem_int_wdata = Mux(!mem_reg_xcpt && (mem_ctrl.jalr ^ mem_npc_misaligned),
                           mem_br_target, mem_reg_wdata.asSInt).asUInt
```

**① Latching the result and the branch direction (EX→MEM boundary):** the ALU's arithmetic result and comparison result are captured in registers here. `mem_br_taken` is the actual branch direction. For a non-memory instruction `mem_reg_wdata` is already the final result; for a memory instruction it is the effective address (§7.2 ④).

```scala
666  mem_reg_wdata := Mux(ex_reg_set_vconfig, ex_new_vl.getOrElse(alu.io.out), alu.io.out)
667  mem_br_taken  := alu.io.cmp_out
```

**② Branch/jump target computation and resolution (finalized here):** the next PC is computed, and if it disagrees with the prediction, the redirect signal `take_pc_mem` is raised. → §11.

```scala
622  val mem_br_target = mem_reg_pc.asSInt +
623    Mux(mem_ctrl.branch && mem_br_taken, ImmGen(IMM_SB, mem_reg_inst),
624    Mux(mem_ctrl.jal,                    ImmGen(IMM_UJ, mem_reg_inst),
625    Mux(mem_reg_rvc, 2.S, 4.S)))
626  val mem_npc = (Mux(mem_ctrl.jalr || mem_reg_sfence,
                       encodeVirtualAddress(mem_reg_wdata, mem_reg_wdata).asSInt,
                       mem_br_target) & (-2).S).asUInt
635  val mem_misprediction = if (usingBTB) mem_wrong_npc else mem_cfi_taken
636  take_pc_mem := mem_reg_valid && !mem_reg_xcpt && (mem_misprediction || mem_reg_sfence)
```

**③ Memory s1:** hands the store data to the D$ and, if necessary, cancels the access with `s1_kill`.

```scala
1178  io.dmem.s1_data.data := /* store_data when fp, else */ mem_reg_rs2
1179  io.dmem.s1_data.mask := DontCare
1181  io.dmem.s1_kill := killm_common || mem_ldst_xcpt || fpu_kill_mem || vec_kill_mem
```

**④ Sending predictor updates and gathering exceptions (second pass):** BTB/BHT updates are sent to the Frontend ([:1101-1119](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1101-L1119)), and misaligned-fetch and load/store breakpoint exceptions are gathered.

```scala
690  val (mem_xcpt, mem_cause) = checkExceptions(List(
691    (mem_reg_xcpt_interrupt || mem_reg_xcpt, mem_reg_cause),
692    (mem_reg_valid && mem_npc_misaligned,    Causes.misaligned_fetch.U),
693    (mem_reg_valid && mem_ldst_xcpt,         mem_ldst_cause)))
```

**⑤ Kill/replay:** MEM is killed by a D$ writeback-port conflict, a redirect, an exception, and so on.

```scala
706  val replay_mem   = dcache_kill_mem || mem_reg_replay || fpu_kill_mem || vec_kill_mem || vec_kill_all
707  val killm_common = dcache_kill_mem || take_pc_wb || mem_reg_xcpt || !mem_reg_valid
709  val ctrl_killm   = killm_common || mem_xcpt || fpu_kill_mem || vec_kill_mem
```

### 7.4 WB (Writeback)

**① Finalizing the result (MEM→WB boundary):** the value to be written back is settled. An FP→integer move takes the FPU value; everything else takes the integer result (`mem_int_wdata`).

```scala
712  wb_reg_valid := !ctrl_killm
716  when (mem_pc_valid) {
717    wb_ctrl := mem_ctrl
719    wb_reg_wdata := Mux(!mem_reg_xcpt && mem_ctrl.fp && mem_ctrl.wxd,
                          io.fpu.toint_data, mem_int_wdata)
       /* … copies of wb_reg_inst / pc / cause / mem_size, etc. … */
     }
```

**② Memory s2 (load result, nack, exception):** the D$ response arrives here. `s2_nack` causes a replay; `s2_xcpt` commits a load/store fault.

```scala
736  val (wb_xcpt, wb_cause) = checkExceptions(List(
737    (wb_reg_xcpt, wb_reg_cause),
739    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.pf.ld, Causes.load_page_fault.U),
743    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.ae.ld, Causes.load_access.U),
       /* … the store-side pf/gf/ae, misaligned ld/st … */ ))
765  val replay_wb_common = io.dmem.s2_nack || wb_reg_replay
```

**③ RegFile write and writeback arbitration:** the normal result (`wb_wen`) and the long-latency results (`ll_wen`: load replay, div, rocc, vec) are arbitrated onto a single write port. → §12.

```scala
823  val wb_valid  = wb_reg_valid && !replay_wb && !wb_xcpt
824  val wb_wen    = wb_valid && wb_ctrl.wxd
825  val rf_wen    = wb_wen || ll_wen
826  val rf_waddr  = Mux(ll_wen, ll_waddr, wb_waddr)
827  val rf_wdata  = Mux(dmem_resp_valid && dmem_resp_xpu, io.dmem.resp.bits.data(xLen-1, 0),
828                 Mux(ll_wen, ll_wdata,
829                 Mux(wb_ctrl.csr =/= CSR.N, csr.io.rw.rdata,
830                 Mux(wb_ctrl.mul, mul.map(_.io.resp.bits.data).getOrElse(wb_reg_wdata),
831                 wb_reg_wdata))))
832  when (rf_wen) { rf.write(rf_waddr, rf_wdata) }
```

**What WB does per instruction family:** the priority order of the `rf_wdata` mux above (top to bottom) *is* the per-family writeback path. Each family behaves as follows in WB:

| Instruction family | `rf_wdata` path (priority) | Write enable | Notes |
|---|---|---|---|
| **Load (hit)** | `io.dmem.resp.bits.data` (highest) | `wb_wen` | the D$ response arrives at s2 |
| **Load (miss) / Div / RoCC** | `ll_wdata` (the `ll_wen` path) | `ll_wen` | out-of-order completion, scoreboard (§12) |
| **CSR** | `csr.io.rw.rdata` | `wb_wen` | read-modify-write (④) |
| **Mul (pipelined)** | `mul.io.resp.bits.data` (`wb_ctrl.mul`) | `wb_wen` | fixed latency (§7.2 ③) |
| **ALU / JAL / JALR** | `wb_reg_wdata` (fallthrough) | `wb_wen` | for jumps this is the link value (PC+4) |
| **Store / Branch** | — | none (`wxd=0`) | no RegFile write |
| **FP (fp destination)** | — (not the integer RF) | the `wfd` path | the FPU completes separately; only fp→int moves use `wb_reg_wdata` (①) |

- Since `wb_wen = wb_valid && wb_ctrl.wxd`, **stores and branches, which have `wxd=0`, never write the integer RegFile.** A destination of `x0` (rd=0) is likewise ignored by `RegFile.write` ([:1360-1366](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1360-L1366)).
- FP instructions write the FP register file through `wfd` rather than `wxd` (inside the FPU); the only case that writes the integer RF is an fp→int move (FCVT.W/FMV.X/comparisons), through `io.fpu.toint_data` in ①.

**④ CSR read/write:** a CSR instruction accesses the CSRFile in WB through `csr.io.rw`. The address and write data come from `wb_reg_inst`/`wb_reg_wdata`, and the read result `csr.io.rw.rdata` returns through the `rf_wdata` mux of ③ (the `wb_ctrl.csr =/= CSR.N` path) to be written to the integer register.

```scala
939  csr.io.rw.addr  := wb_reg_inst(31,20)
940  csr.io.rw.cmd   := CSR.maskCmd(wb_reg_valid, wb_ctrl.csr)
941  csr.io.rw.wdata := wb_reg_wdata
```

**⑤ Exception commit and redirect:** WB makes the final decision on exception/replay/eret/flush and redirects the fetch PC (priority in §11.2).

```scala
769  val replay_wb = replay_wb_common || replay_wb_rocc || replay_wb_csr || replay_wb_vec
770  take_pc_wb := replay_wb || wb_xcpt || csr.io.eret || wb_reg_flush_pipe
```

---

## 8. Shared vocabulary: the decode table and `IntCtrlSigs`

An instruction's "character" is summarized at decode time into the **control signal bundle `IntCtrlSigs`**. All of Doc B distinguishes instruction types by these fields.

- Bundle definition: [`IDecode.scala:19-48`](generators/rocket-chip/src/main/scala/rocket/IDecode.scala#L19-L48)
  *(note that the file is **`IDecode.scala`**, not `control-signals.scala`.)*
- The opcode ↔ signal mapping for each instruction: the `table` in `IDecode.scala`, with the opcode constants in [`Instructions.scala`](generators/rocket-chip/src/main/scala/rocket/Instructions.scala).

### How the decode table is built (at elaboration time)

The instruction → `IntCtrlSigs` mapping table (`decode_table`) is built by **concatenating the `.table` of each `DecodeConstants` subclass for the enabled extensions**. It is **static data** fixed once at circuit elaboration, not a separate hardware module (see §4).

```scala
223  val decode_table = {
       // conditionally list the DecodeConstants subclasses for the enabled extensions
224    (if (usingMulDiv) new MDecode(pipelinedMul) +: … else Nil) ++:
225    (if (usingAtomics) new ADecode +: … else Nil) ++:
226    (if (fLen >= 32)   new FDecode +: … else Nil) ++:
       /* … D / H / RoCC / SVM / Supervisor / Debug / NMI / FenceI / Zba·Zbb·Zbs / Vector … */
230    (if (xLen == 32) new I32Decode else new I64Decode) +:
       /* … */
245    Seq(new IDecode)            // the base integer ISA
246  } flatMap(_.table)           // concatenate every class's .table into one
```

- Each `XDecode` (`IDecode`/`MDecode`/`FDecode`/`ADecode`/`ZbaDecode`, …) extends `DecodeConstants` and provides a `.table`: `Seq[(BitPat, List[BitPat])]` = (opcode pattern → list of signal values).
- Inclusion is **conditional on the configuration** — `usingMulDiv`, `usingAtomics`, `fLen`, `useZbb`, and so on. Rows for disabled extensions are simply absent from the table, so their opcodes fall through to the default row and become illegal.
- `flatMap(_.table)` concatenates **all rows of the enabled extensions into a single table**, with the base integer ISA (`IDecode`) appended last.

This table is consumed by `.decode(id_inst(0), decode_table)` in §7.1 ②. `decode()` builds **minimized combinational logic** (via `DecodeLogic`) that maps the opcode to the signal list — again, not a separate module — and when no row matches it emits the **default row** (`decode_default`, mostly `X`/`N` → `legal=N`), which leads to `id_illegal_insn` (§13).

```scala
// IDecode.scala:61-68 — the decode call mechanism
61  def decode(inst: UInt, table: Iterable[(BitPat, List[BitPat])]) = {
62    val decoder = DecodeLogic(inst, default, table)   // default = decode_default (on a match failure)
63    val sigs = Seq(legal, fp, rocc, branch, jal, jalr, rxs2, rxs1, sel_alu2,
64                   sel_alu1, sel_imm, alu_dw, alu_fn, mem, mem_cmd,
65                   rfs1, rfs2, rfs3, wfd, mul, div, wxd, csr, fence_i, fence, amo, dp)
66    sigs zip decoder map {case(s,d) => s := d}         // wire the fields in order
68  }
```

### The main fields (exact names — used verbatim in Doc B)

| Field | Meaning | Values/constants |
|---|---|---|
| `legal` | whether the instruction is legal | Bool |
| `branch` / `jal` / `jalr` | control-flow kind | Bool |
| `mem` | memory access instruction | Bool |
| `mem_cmd` | memory op | `M_XRD` (load) / `M_XWR` (store) / AMO / `M_XLR`/`M_XSC` … ([Consts.scala](generators/rocket-chip/src/main/scala/rocket/Consts.scala)) |
| `alu_fn` | **ALU operation select** (the field is named `alu_fn`, not `ex_cmd`) | `FN_ADD`, `FN_SUB`, … ([ALU.scala](generators/rocket-chip/src/main/scala/rocket/ALU.scala)) |
| `alu_dw` | ALU data width | `DW_32` / `DW_64` (= `DW_XPR`) |
| `sel_alu1` | op1 source | `A1_RS1` / `A1_PC` / `A1_RS1SHL` |
| `sel_alu2` | op2 source | `A2_RS2` / `A2_IMM` / `A2_SIZE` / `A2_ZERO` |
| `sel_imm` | immediate format | `IMM_I` / `IMM_S` / `IMM_SB` / `IMM_UJ` / `IMM_U` / `IMM_Z` |
| `wxd` | **integer register write enable** (the field is named `wxd`, not `wb_wen`) | Bool |
| `wfd` | floating-point register write enable | Bool |
| `rxs1` / `rxs2` | reads rs1/rs2 from the integer register file | Bool |
| `rfs1`/`rfs2`/`rfs3` | reads a source from the floating-point register file | Bool |
| `mul` / `div` | multiply/divide (long latency) | Bool |
| `fp` / `dp` | floating-point / double precision | Bool |
| `csr` | kind of CSR access | `CSR.N/R/W/S/C/I` |
| `fence` / `fence_i` / `amo` | synchronization/atomicity | Bool |

> **Frequently used derived expressions** (repeated throughout Doc B):
> - load = `mem && isRead(mem_cmd)`, store = `mem && isWrite(mem_cmd)` ([:650-651](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L650-L651))
> - pure arithmetic (ALU) = `wxd && !(jal||jalr||mem||fp||mul||div||csr=/=N)` ([:185](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L185))

---

## 9. Bypass / forwarding

When an instruction's source register is the result of an earlier instruction that has not reached the RegFile yet, the **latest value is bypassed directly to the EX input** as the instruction enters EX.

**The list of bypass sources** ([:463-468](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L463-L468)):

| Source | Condition | Value supplied |
|---|---|---|
| x0 | always | 0 |
| EX result | `ex_reg_valid && ex_ctrl.wxd` | `mem_reg_wdata` |
| MEM result (non-memory) | `mem_reg_valid && mem_ctrl.wxd && !mem_ctrl.mem` | `wb_reg_wdata` |
| MEM result (including memory) | `mem_reg_valid && mem_ctrl.wxd` | `dcache_bypass_data` (fast-load) |

- **Address match detection:** `id_bypass_src` ([:468](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L468)); which source to use is latched into `ex_reg_rs_bypass/_lsb/_msb` at the ID→EX boundary ([:574-583](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L574-L583)).
- **The actual mux in EX:** `ex_rs` ([:471-476](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L471-L476)).
- **fast-load bypass:** load data is bypassed straight out of the D$ response (`dcache_bypass_data`) ([:454-457](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L454-L457)) — detailed in Doc B (Load/Store).

Whatever forwarding cannot cover becomes a **stall** (§10).

---

## 10. Hazard / stall / kill

### 10.1 Data hazards (no bypass possible → interlock)

When an earlier instruction in EX/MEM/WB has not yet resolved the same destination register, and it is **of a kind that cannot be bypassed**, ID is stalled.

- Checked operands: `hazard_targets` = {rs1, rs2, rd} ([:999-1001](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L999-L1001)).
- One hazard term per stage:
  - `id_ex_hazard` — when `ex_cannot_bypass` (csr/jalr/mem/mul/div/fp/rocc/vec) ([:1018-1021](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1018-L1021))
  - `id_mem_hazard` — when `mem_cannot_bypass` (csr/slow load/mul/div/fp, …) ([:1024-1030](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1024-L1030))
  - `id_wb_hazard` — while waiting on a long-latency writeback (scoreboard) ([:1038-1040](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1038-L1040))
- **load-use hazard:** `id_load_use := mem_reg_valid && data_hazard_mem && mem_ctrl.mem` ([:1031](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1031)) → central to Doc B (Load/Store).

### 10.2 Stall (halting ID)

The OR terms of `ctrl_stalld` ([:1061-1075](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1061-L1075)): the hazards above, plus the scoreboard hazard, single-step, fp/vec busy, `dcache_blocked`, `rocc_blocked`, div busy, fence (`id_do_fence`), CSR stall, `id_reg_pause`, and `traceStall`.

### 10.3 Kill (pipeline invalidation)

Each stage's kill signal clears the next `valid`. A flush is precisely this **kill signal propagating upstream**.

| Signal | Definition | Triggers in brief |
|---|---|---|
| `ctrl_killd` | [:1076](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1076) | instruction invalid / replay / `take_pc_mem_wb` / `ctrl_stalld` / interrupt |
| `ctrl_killx` | [:609](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L609) | `take_pc_mem_wb` / `replay_ex` / `!ex_reg_valid` |
| `ctrl_killm` | [:707-709](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L707-L709) | `dcache_kill_mem` / `take_pc_wb` / `mem_xcpt` / `!mem_reg_valid` … |

---

## 11. Control flow: where branches are resolved, and redirect priority

> **Correcting the most common misconception:** branches are not "resolved" in EX.
> **The comparison is computed in EX** (`alu.io.cmp_out`), but its result is latched into `mem_br_taken` and **the actual resolution and redirect happen in MEM**.

### 11.1 Step by step

1. **EX:** the ALU performs the condition comparison → at the end of the cycle, `mem_br_taken := alu.io.cmp_out` ([:667](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L667)).
2. **MEM:** the real next PC is compared against the prediction.
   - `mem_br_target` / `mem_npc` computation ([:622-626](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L622-L626))
   - `mem_direction_misprediction`, `mem_misprediction` ([:634-635](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L634-L635))
   - **`take_pc_mem := mem_reg_valid && !mem_reg_xcpt && (mem_misprediction || mem_reg_sfence)`** ([:636](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L636))
3. **WB:** exception, CSR, and flush-type redirects.
   - **`take_pc_wb := replay_wb || wb_xcpt || csr.io.eret || wb_reg_flush_pipe`** ([:770](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L770))

### 11.2 Redirect priority

```
take_pc_mem_wb = take_pc_wb || take_pc_mem          (RocketCore.scala:313)
```
and the actual fetch PC selection favors WB:
```
io.imem.req.bits.pc =
    Mux(wb_xcpt || eret, csr.io.evec,   // 1st: exception/return vector
    Mux(replay_wb,       wb_reg_pc,      // 2nd: replay
                         mem_npc))       // 3rd: flush / branch mispredict
```
([:1078-1083](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1078-L1083))

So **WB (`take_pc_wb`) outranks MEM (`take_pc_mem`)**, and the mispredict redirect from MEM takes effect only when no later redirect arrives from WB. Both signals kill (§10.3) the upstream stages, which is the flush.

### 11.3 Interface to the predictors (core side only)

- Consumed: `ex_reg_btb_resp` → `mem_reg_btb_resp` (the prediction result) ([:255](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L255), [:272](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L272)).
- Sent: `io.imem.btb_update` (with a `CFIType.call/ret/jump/branch` hint) ([:1101-1112](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1101-L1112)), `io.imem.bht_update` ([:1114-1119](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1114-L1119)).
- RAS: the core is not involved (`ras_update := DontCare`, §3). It is implemented in the Frontend and `BTB.scala` (concepts in §2.2).

---

## 12. Scoreboard and long-latency writeback

Ordinary instructions write the RegFile in order at WB, but instructions **whose completion time varies** (load miss, `div`, RoCC, vector) write **out of order and later**. Two mechanisms support this:

### 12.1 The long-latency writeback path (`ll_arb`)

- A 3-input arbiter: div / rocc / vec ([:784-813](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L784-L813)).
- The D$ load replay also enters through this write port ([:817-821](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L817-L821)).
- The final RegFile write mux: `rf_wen = wb_wen || ll_wen`, with `rf_waddr`/`rf_wdata` ([:823-832](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L823-L832)).

### 12.2 The scoreboard (tracking long-latency destinations)

- `sboard = new Scoreboard(32, true)` ([:1007](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1007)); the class definition is at [:1329-1346](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1329-L1346).
- **set:** the destination bit is set as a long-latency instruction passes WB — `wb_set_sboard = wb_ctrl.div || wb_dcache_miss || wb_ctrl.rocc || wb_ctrl.vec` ([:764](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L764), set at [:1015](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1015)).
- **clear:** cleared on the actual late writeback (`ll_wen`) ([:1008](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1008)).
- **hazard:** while the destination is still outstanding, `id_sboard_hazard` stalls ID ([:1014](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1014)).

> This mechanism first appears in **Doc B (Load/Store)** with a load miss, and is completed formally in **Doc B (Mul/Div)**.
> FP uses the same pattern with its own `fp_sboard` ([:1042-1050](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1042-L1050)).

---

## 13. The common exception / interrupt path

Exceptions are gathered as candidates in every stage and **committed in WB**. Detection is unified through `checkExceptions` (a priority mux) ([:1308-1309](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1308-L1309)).

| Stage | Signals | Representative causes |
|---|---|---|
| ID | `id_xcpt`, `id_cause` [:431-442](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L431-L442) | interrupt, breakpoint, fetch page/access fault, illegal instruction |
| EX | `ex_xcpt`, `ex_cause` [:614-615](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L614-L615) | propagation of the cause passed down from ID |
| MEM | `mem_xcpt`, `mem_cause` [:690-693](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L690-L693) | misaligned fetch, load/store breakpoint |
| WB | `wb_xcpt`, `wb_cause` [:736-746](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L736-L746) | D$ s2 page/access/misaligned fault |

- **commit:** `csr.io.exception := wb_xcpt`, `csr.io.cause := wb_cause` ([:859-860](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L859-L860)); the redirect to the trap vector is the `csr.io.evec` path in §11.2.
- **interrupt:** injected at ID (`csr.io.interrupt` → `ex_reg_xcpt_interrupt`) and propagated down the later stages ([:535](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L535)).
- The ALU path is reused to compute badaddr on an exception (`sel_alu1/2` are overridden when `id_xcpt`) ([:543-557](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L543-L557)).

---

## 14. How Doc B refers back to this document (the convention)

Each per-type document follows the skeleton below, **linking to this document's section numbers for everything shared** and describing only the **differences**.

```
[type] execution trace
 ├─ summary: the combination of IntCtrlSigs fields that defines this type (§8)
 ├─ ID  : common (§7.1) + the decode peculiarities of this type
 ├─ EX  : common (§7.2) + operand-select and operation peculiarities
 ├─ MEM : common (§7.3) + (where applicable) external block interaction / branch resolution (§11)
 ├─ WB  : common (§7.4) + writeback path (§12: normal vs long latency)
 └─ external interaction: link to the relevant IO port in §3
```

**What each type adds on top (the learning order):**

| Doc B order | Type | What it introduces relative to this document |
|---|---|---|
| 1 | ALU | (baseline — fully covered by §7~§9) |
| 2 | Load/Store | §3 `io.dmem` s0/s1/s2, §10.1 load-use, the first appearance of §12 late writeback |
| 3 | Mul/Div | completes §12 scoreboard/`ll_arb` |
| 4 | Branch (+JAL/JALR) | §11 branch resolution (computed in EX, resolved in MEM), redirect, predictor interface |
| 5 | FP | §3 `io.fpu`, the `wfd` path, the FP scoreboard |

---

## Appendix A. Quick signal index (the key signals covered here)

| Signal | Meaning | Line |
|---|---|---|
| `id_ctrl` / `ex_ctrl` / `mem_ctrl` / `wb_ctrl` | per-stage control bundles | [248-250](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L248-L250) |
| `ctrl_killd/killx/killm` | per-stage kill | [1076](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1076)/[609](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L609)/[707](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L707) |
| `ctrl_stalld` | ID stall | [1061](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1061) |
| `take_pc_mem` / `take_pc_wb` | MEM/WB redirect | [636](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L636)/[770](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L770) |
| `ex_rs(0/1)` | EX operands after bypass | [475](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L475) |
| `id_load_use` | load-use hazard | [1031](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1031) |
| `sboard` / `wb_set_sboard` | long-latency scoreboard | [1007](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1007)/[764](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L764) |
| `ll_arb` / `ll_wen` | long-latency writeback arbitration | [784](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L784)/[789](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L789) |
| `rf_wen/waddr/wdata` | RegFile write | [823-826](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L823-L826) |

## Appendix B. Referenced files

| File | Role |
|---|---|
| [`RocketCore.scala`](generators/rocket-chip/src/main/scala/rocket/RocketCore.scala) | the basis of this document; the ID~WB pipeline |
| [`IDecode.scala`](generators/rocket-chip/src/main/scala/rocket/IDecode.scala) | the `IntCtrlSigs` definition and the decode table |
| [`Instructions.scala`](generators/rocket-chip/src/main/scala/rocket/Instructions.scala) | opcode constants |
| [`Consts.scala`](generators/rocket-chip/src/main/scala/rocket/Consts.scala) | the `M_*` (mem_cmd), `A1_*`/`A2_*`, and `IMM_*` constants |
| [`ALU.scala`](generators/rocket-chip/src/main/scala/rocket/ALU.scala) | the `FN_*` constants and the ALU implementation |
| [`Frontend.scala`](generators/rocket-chip/src/main/scala/rocket/Frontend.scala) / [`IBuf.scala`](generators/rocket-chip/src/main/scala/rocket/IBuf.scala) | the IF stage and the instruction buffer (outside the core boundary) |
| [`BTB.scala`](generators/rocket-chip/src/main/scala/rocket/BTB.scala) | the BTB/BHT/RAS predictors (outside the core boundary) |
| [`HellaCache.scala`](generators/rocket-chip/src/main/scala/rocket/HellaCache.scala) / [`DCache.scala`](generators/rocket-chip/src/main/scala/rocket/DCache.scala) / [`NBDcache.scala`](generators/rocket-chip/src/main/scala/rocket/NBDcache.scala) | the D$ interface, DCache, and NonBlockingCache (the peer block of `io.dmem`) |

## Appendix C. Glossary

| Abbreviation | Expansion | Related section |
|---|---|---|
| IF / ID / EX / MEM / WB | Fetch / Decode / Execute / Memory / Writeback stages | §1 |
| BTB / BHT / RAS | Branch Target Buffer / Branch History Table / Return Address Stack | §2.2, §11.3 |
| CFI | Control-Flow Instruction (call/ret/jump/branch) | §2.2 |
| MSHR | Miss Status Handling Register (tracks D$ misses) | §2.3 |
| TLB / PTW / PMP | Translation Lookaside Buffer / Page Table Walker / Physical Memory Protection | §2.4 |
| CSR | Control and Status Register | §7.4, §13 |
| AMO / LR-SC | Atomic Memory Operation / Load-Reserved·Store-Conditional | Doc B |
| RVC | RISC-V Compressed (16-bit) instructions | §5 |
| bypass / forwarding | supplying an unwritten result directly to an EX input | §9 |
| interlock / stall | halting ID because of a hazard | §10 |
| replay / nack | retry / rejection (D$, CSR, and so on) | §12, §13 |
| scoreboard | bitmap tracking outstanding long-latency destinations | §12 |
| hart | hardware thread | — |
| xLen | integer register/data width (64 for RV64) | §5 |

---

*End of Doc A. The next document, Doc B (starting with the ALU chapter), refers back to §7~§13 here as its reference base.*
