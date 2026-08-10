# Rocket Core Execution Trace by Instruction Type (Doc B)

> **Where this document sits**
> This document assumes the **common pipeline skeleton and vocabulary** defined in [Doc A — Pipeline Overview](Rocket_pipeline_doc_a_overview.md),
> and traces the **differences (diffs)** between instruction types. Shared behavior is not repeated; it is **linked by Doc A section number (§)**.
> Reference source: [`RocketCore.scala`](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala), with decoding in [`IDecode.scala`](../generators/rocket-chip/src/main/scala/rocket/IDecode.scala).
> Reference source revision: the same as Doc A §0 — rocket-chip `55bcad0f5` (`v1.6-819-g55bcad0f5`); line numbers are valid for that snapshot.
> Reference configuration: the same as [Doc A §5](Rocket_pipeline_doc_a_overview.md#5-configuration-parameters-rocketcoreparams) — **RV64GC with virtual memory (`useVM`), plus `pipelinedMul`**.
> Everything there but `pipelinedMul` is a plain `RocketCoreParams` default; **`pipelinedMul` is not a default** (it requires `mulUnroll == xLen`, which no stock config sets), which is why §3.4 describes both the pipelined and the non-pipelined multiplier.
> Chapter order: **ALU → Load/Store → Mul/Div → Branch (+JAL/JALR) → FP**, with a signal matrix in the appendix.

---

## 0. How to read this document

- Each chapter follows the same stage order as Doc A (**ID→EX→MEM→WB**).
- Shared behavior is linked as "→ Doc A §x", and **what is unique to that type** is set in bold.
- Code excerpts carry the original line number in front of each line (as in Doc A). Where the numbers skip, intervening lines have been elided.
- Legend for the **pipeline path** diagram at the head of each chapter: `─▶` flow · `↕` interaction with an external `io.*` · `⟲` redirect/replay feedback · `╌▶` late (out-of-order) completion.

### How to read a decode row (`IDecode.scala`)

Each row of the decode table fills an `IntCtrlSigs` with the fields in this order ([IDecode.scala:63-65](../generators/rocket-chip/src/main/scala/rocket/IDecode.scala#L63-L65)):

```
legal, fp, rocc, branch, jal, jalr, rxs2, rxs1, | sel_alu2, sel_alu1, sel_imm, alu_dw, alu_fn, | mem, mem_cmd, | rfs1,rfs2,rfs3, wfd, mul, div, wxd, csr, fence_i, fence, amo, dp
```

Each chapter opens by quoting a representative instruction in this notation.

---

## 1. ALU — the baseline (ADD / ADDI / AND / SLL, …)

The simplest type. It has **no interaction with external hardware at all**, and is fully described by Doc A §7~§9. It is the reference point for every later chapter.

### 1.1 Definition (decode)

```
// IDecode.scala:95, 101, 108
ADDI  legal=Y ... rxs1=Y | A2_IMM, A1_RS1, IMM_I, DW_XPR, FN_ADD | mem=N | wxd=Y
ADD   legal=Y ... rxs2=Y rxs1=Y | A2_RS2, A1_RS1, IMM_X, DW_XPR, FN_ADD | mem=N | wxd=Y
SLL   legal=Y ... rxs2=Y rxs1=Y | A2_RS2, A1_RS1, IMM_X, DW_XPR, FN_SL  | mem=N | wxd=Y  (shift)
```

- Distinguishing signals: `wxd=Y` with `mem/branch/jal/jalr/mul/div/fp=N` and `csr=N` → this matches the "pure arithmetic" derived expression in Doc A ([RocketCore.scala:185](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L185)).
- **The only difference between ADD and ADDI is `sel_alu2`**: `A2_RS2` (register) versus `A2_IMM` (immediate). Every other path is identical.
- **Shifts and logic/set operations (SLL/SRL/SRA, AND/OR/XOR, SLT, …) share the skeleton of ADD and differ only in `alu_fn`** (`FN_SL/FN_SR/FN_SRA/FN_AND/…`). Note, however, that the path *inside* the ALU that produces the result in EX is different (§1.2 EX — `shift_logic_cond` rather than `adder_out`).

**Pipeline path:**

```
ID ───────────▶ EX ─────────────────▶ MEM ────────────▶ WB
decode·rf.read  op1/op2 → ALU → out    (pass-through)     RF.write
                ├ FN_ADD/SUB→adder_out  mem_reg_wdata       ← wb_reg_wdata
                └ otherwise→shift_logic_cond                  (lowest-priority rf_wdata mux leg)

  out ╌ bypass ╌▶ a later EX (§1.3)          external interaction: none
```

### 1.2 Per-stage diff

**ID** — the split between R-type (ADD) and I-type (ADDI) is **settled at decode time**. `rxs2` says "is the second operand read from a register?", and `sel_alu2`/`sel_imm` say "does EX use rs2 or the immediate?"

- **R-type (ADD)**: `rxs2=Y` → rs2 is a real operand. In EX, `sel_alu2=A2_RS2`.
- **I-type (ADDI)**: `rxs2=N` → rs2 is unused. In EX, `sel_alu2=A2_IMM`, with the value coming from `ImmGen(IMM_I, inst)`.

```scala
// RocketCore.scala:340-343 (ID: which sources to read), 485-487 (EX: choosing the second operand)
340  val id_ren = IndexedSeq(id_ctrl.rxs1, id_ctrl.rxs2)   // ADD=(Y,Y), ADDI=(Y,N)
343  val id_rs  = id_raddr.map(rf.read _)   // combinational read of the rs1/rs2 addresses (id_ren says whether they are used)
485  val ex_op2 = MuxLookup(ex_ctrl.sel_alu2, 0.S)(Seq(
486    A2_RS2 -> ex_rs(1).asSInt,           // R-type: register rs2
487    A2_IMM -> ex_imm ))                  // I-type: the ImmGen(IMM_I) result
```

- ID reads **both** rs1 and rs2 addresses combinationally, but `id_ren` (= `rxs1`/`rxs2`) marks which ones are actually used and subject to hazard checking (ADDI's rs2 is read but never consumed).
- Generating the immediate `ex_imm` itself (the per-format bit rearrangement and sign extension) is **the job of `ImmGen` in EX** → Doc A §7.2 ①. ID only decides *which source* is used.

**EX** — the heart of this type. `sel_alu1/2/imm` fix the ALU inputs and `alu_fn` fixes the operation (→ Doc A §7.2 ①②). Of the three ALU outputs, only **`out`** is used, and `out` itself splits into **two paths** depending on `alu_fn` — ADD/SUB take the adder (`adder_out`), while **everything else (shift, logic, set) takes the mux's default value `shift_logic_cond`**:

```scala
// ALU.scala:163-180 (result select mux) — the default is shift_logic_cond
163  val out = MuxLookup(io.fn, shift_logic_cond)(Seq(   // ← default = shift_logic_cond
164    FN_ADD -> io.adder_out,
165    FN_SUB -> io.adder_out
        /* … with Zbb, FN_MAX/MIN/ROL/ROR/UNARY are added … */ ))
177  io.out := out
180  when (io.dw === DW_32) { io.out := Cat(Fill(32, out(31)), out(31,0)) }  // sign-extend for W instructions
```

**The shift family (SLL/SRL/SRA) and the logic/set operations produce their result through `shift_logic_cond`, not `adder_out`.** That signal is the **OR** of the shift, logic, and compare-set results:

```scala
// ALU.scala:111-129 (in outline)
111  val shout = Mux(io.fn === FN_SR || io.fn === FN_SRA || io.fn === FN_BEXT, shout_r, 0.U) |
112              Mux(io.fn === FN_SL,                                          shout_l, 0.U)   // SLL/SRL/SRA
121  val logic = Mux(fn∈{XOR,OR,ORN,XNOR}, in1_xor_in2, 0.U) |
122              Mux(fn∈{OR,AND,ORN,ANDN}, in1_and_in2, 0.U)                                   // AND/OR/XOR
125  val shift_logic = (isCmp(io.fn) && slt) | logic | (shout & bext_mask)                     // + SLT/SLTU
126  val shift_logic_cond = shift_logic (| cond_out)   // includes the CZEQZ/CZNEZ result with Zicond
```

- In summary, the mux picks the result path according to the value of `alu_fn`:
  - `FN_ADD/FN_SUB` → **`adder_out`** (the adder)
  - `FN_SL` (SLL) / `FN_SR` (SRL) / `FN_SRA` (SRA) → the shift result `shout` → `shift_logic_cond`
  - `FN_AND/FN_OR/FN_XOR` → `logic` → `shift_logic_cond`
  - `FN_SLT/FN_SLTU` → compare-set (`slt`) → `shift_logic_cond`
- `shamt` is taken from `in2(5,0)` (RV64), and SLL reverses its input so that it can share the right shifter (`shout_l = Reverse(shout_r)`, [ALU.scala:108-112](../generators/rocket-chip/src/main/scala/rocket/ALU.scala#L108-L112)).
- With `alu_dw=DW_32` (the W instructions such as ADDW/SLLW), the lower 32 bits of the result are sign-extended ([ALU.scala:180](../generators/rocket-chip/src/main/scala/rocket/ALU.scala#L180)).

**MEM** — **no memory access.** The EX result is simply passed down the pipeline (→ Doc A §7.3 ①). Because `mem_ctrl.mem=N`, `io.dmem.req.valid=0`.

**WB** — written through the **lowest-priority leg of the `rf_wdata` mux (`wb_reg_wdata`)** (→ Doc A §7.4 ③), since the instruction is not a CSR, load, mul, or long-latency operation:

```scala
// RocketCore.scala:827-832 excerpt — the ALU is the final fallthrough
831                 wb_reg_wdata))))   // ← the ALU result lands here
832  when (rf_wen) { rf.write(rf_waddr, rf_wdata) }
```

### 1.3 Bypass

The ALU result is **forwarded immediately** to the following instruction (one cycle). Among the bypass sources in Doc A §9 these are "the EX result (`mem_reg_wdata`)" and "the MEM result (`wb_reg_wdata`)"; because the ALU is not caught by `ex_cannot_bypass`, back-to-back execution proceeds without a stall.

### 1.4 External interaction

**None.** Only the (internal) RegFile is accessed. This chapter is the baseline against which the later chapters measure "what gets added".

---

## 2. Load / Store (LW / LD / SW / SD / LB / LH, …)

**Up through the address computation in EX this is identical to the ALU**, but EX→MEM→WB is **aligned with the s0→s1→s2 pipeline of the HellaCache (D$)**, so this is where external interaction first appears. → for the external block see Doc A §3 (`io.dmem`).

### 2.1 Definition (decode)

```
// IDecode.scala:87, 92
LW  legal=Y ... rxs1=Y | A2_IMM, A1_RS1, IMM_I, FN_ADD | mem=Y, M_XRD | wxd=Y   (load)
SW  legal=Y ... rxs2=Y rxs1=Y | A2_IMM, A1_RS1, IMM_S, FN_ADD | mem=Y, M_XWR | wxd=N  (store)
```

- **The address computation reuses the ALU**: `A1_RS1 + A2_IMM` with `FN_ADD` → `adder_out = base + offset`. This is common to loads and stores.
- The difference: a load is `M_XRD` with `wxd=Y` (the result is written to a register), a store is `M_XWR` with `wxd=N` (rs2 is read and written to memory, with `sel_imm=IMM_S`).

**Pipeline path (core stages ↔ D$ s0/s1/s2):**

```
ID ───────────▶ EX (s0) ──────────▶ MEM (s1) ────────▶ WB (s2)
load-use detect  req.addr=adder_out   s1_data (store)     resp / s2_nack / s2_xcpt
                io.dmem ↕──────────────────────────────────↕            hit → RF
                                          miss → scoreboard.set
                                          miss ╌▶ resp.replay ╌▶ ll_wen ─▶ RF  ⟲
```

### 2.2 Per-stage diff

**ID** — this stage is where the **load-use hazard is detected and consumed**. When a load is in MEM (`mem_ctrl.mem`) and the instruction in ID reads that load's destination rd as a source, `id_load_use` asserts and travels down with the instruction as `ex_reg_load_use`.

```scala
// RocketCore.scala:1028 (rd match check), 1031 (load-use detection), 559 (handoff to EX)
1028  val data_hazard_mem = mem_ctrl.wxd && checkHazards(hazard_targets, _ === mem_waddr)
1031  id_load_use := mem_reg_valid && data_hazard_mem && mem_ctrl.mem
 559  ex_reg_load_use := id_load_use   // attached to the dependent instruction like a tag
```

- Two consequences (detailed in §2.3):
  1. **Slow bypass (LB/LH/SC)**: the load value in MEM cannot be bypassed in that cycle, so `id_mem_hazard` **stalls ID directly** (Doc A §10.1, the `mem_ctrl.mem && mem_mem_cmd_bh` term of `mem_cannot_bypass`).
  2. **Load miss**: if the dependent instruction has already advanced to EX, `ex_reg_load_use` triggers `replay_ex_load_use` and **EX is replayed** (§2.3).
- An ordinary word load (fast bypass) needs no stall; the D$ response is bypassed through the fast-load path (§2.4).

**Operand mapping (ID→EX):** for the address computation, op1 = `rs1` (base) and op2 = `imm` (offset). A store also reads `rs2` (`rxs2=Y`), but that value branches off as **store data, not as an ALU operand**.

```scala
// RocketCore.scala:480,487 (ALU operand mux), 672 (store data branch)
480    A1_RS1 -> ex_rs(0).asSInt,   // op1 = base (rs1)
487    A2_IMM -> ex_imm,            // op2 = offset (load=IMM_I, store=IMM_S)
672      mem_reg_rs2 := new StoreGen(size, 0.U, ex_rs(1), coreDataBytes).data  // store: rs2 → data
```

**EX (= D$ s0): address computation and request issue.** There is **no separate adder for the effective address — the ALU is reused**: decoding sets `A1_RS1 + A2_IMM` with `FN_ADD`, so the ALU adder output `adder_out = rs1 + imm` *is* the address:

```scala
// ALU.scala:90 (the adder), RocketCore.scala:513-515 (mem instructions drive FN_ADD)
 90  io.adder_out := io.in1 + in2_inv + isSub(io.fn)   // FN_ADD → rs1 + imm
513  alu.io.fn  := ex_ctrl.alu_fn      // load/store = FN_ADD
514  alu.io.in2 := ex_op2.asUInt       // = imm  (sel_alu2 = A2_IMM)
515  alu.io.in1 := ex_op1.asUInt       // = rs1  (sel_alu1 = A1_RS1)
```

That `alu.io.adder_out` is used as the request address and sent to the D$. The destination register travels **in the tag** (in preparation for out-of-order completion):

```scala
// RocketCore.scala:1160-1168 excerpt
1160  io.dmem.req.valid    := ex_reg_valid && ex_ctrl.mem
1161  val ex_dcache_tag = Cat(ex_waddr, ex_ctrl.fp)     // rd address as the tag
1163  io.dmem.req.bits.tag  := ex_dcache_tag
1164  io.dmem.req.bits.cmd  := ex_ctrl.mem_cmd           // M_XRD / M_XWR
1165  io.dmem.req.bits.size := ex_reg_mem_size           // LB/LH/LW/LD
1168  io.dmem.req.bits.addr := encodeVirtualAddress(ex_rs(0), alu.io.adder_out)  // ← receives adder_out
```

- The same value `alu.io.out` (for a `mem` instruction `alu_fn` is `FN_ADD`, so `out == adder_out`) is also latched into `mem_reg_wdata` and reused downstream as the effective address (breakpoint comparison, `tval`, and so on — Doc A §7.2 ④).

- If the D$ does not accept the request (`!io.dmem.req.ready`), `replay_ex_structural` replays EX (→ Doc A §7.2 ⑤).
- **TLB and address translation** happen inside the D$ (a virtual address is handed over, `phys=false`). The PTW is Doc A §3 (`io.ptw`).

**MEM (= D$ s1): supplying store data.** A store's rs2 is aligned by `StoreGen` at the EX→MEM boundary, held in `mem_reg_rs2`, and handed to the D$ in s1:

```scala
// RocketCore.scala:670-673 (EX→MEM), 1178-1181 (s1)
670    when (ex_ctrl.rxs2 && (ex_ctrl.mem || ex_ctrl.rocc || ex_sfence)) {
672      mem_reg_rs2 := new StoreGen(size, 0.U, ex_rs(1), coreDataBytes).data
       }
1178  io.dmem.s1_data.data := /* store_data when fp, else */ mem_reg_rs2
1181  io.dmem.s1_kill := killm_common || mem_ldst_xcpt || fpu_kill_mem || vec_kill_mem
```

**WB (= D$ s2): response / nack / exception.**

```scala
// RocketCore.scala:738-745 (exceptions), 765 (nack)
738    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.pf.st, Causes.store_page_fault.U),
739    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.pf.ld, Causes.load_page_fault.U),
743    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.ae.ld, Causes.load_access.U),
744    (wb_reg_valid && wb_ctrl.mem && io.dmem.s2_xcpt.ma.st, Causes.misaligned_store.U),
765  val replay_wb_common = io.dmem.s2_nack || wb_reg_replay
```

- On `s2_nack`, `replay_wb` → `take_pc_wb` refetches that instruction's PC (Doc A §11.2).
- A store has `wxd=N`, so it never writes the RegFile. Only a load takes the writeback path below.

### 2.3 Load-use hazard (up to two cycles)

The load result arrives across MEM and WB, so when **the immediately following instruction consumes it**, no bypass is possible and the pipeline stalls for one or two cycles.

```scala
// RocketCore.scala:1031 (detection), 611 (LB/LH/SC take two cycles), 607-608 (replay)
1031  id_load_use := mem_reg_valid && data_hazard_mem && mem_ctrl.mem
 611  val ex_slow_bypass = ex_ctrl.mem_cmd === M_XSC || ex_reg_mem_size < 2.U  // byte/half
 607  val replay_ex_load_use = wb_dcache_miss && ex_reg_load_use
```

- An ordinary word load is bypassed in a single cycle thanks to `fastLoadWord` (on by default, §2.4). **LB/LH/SC take two cycles because of `ex_slow_bypass`.**

### 2.4 The load writeback path (where it departs from the ALU)

Load data reaches the RegFile **directly from the D$ response (`io.dmem.resp`)**, not through the ALU result path. The destination is recovered from the response tag:

```scala
// RocketCore.scala:773-777 (tag decode), 827 (highest-priority writeback mux leg), 454-457 (fast-load bypass)
773  val dmem_resp_xpu   = !io.dmem.resp.bits.tag(0).asBool
775  val dmem_resp_waddr =  io.dmem.resp.bits.tag(5, 1)
827  val rf_wdata = Mux(dmem_resp_valid && dmem_resp_xpu, io.dmem.resp.bits.data(xLen-1, 0), … )
454  val dcache_bypass_data = if (fastLoadWord) io.dmem.resp.bits.data_word_bypass(xLen-1,0) else wb_reg_wdata
```

- **hit**: the data arrives on `dmem.resp` at WB time and is written through the highest-priority `rf_wdata` leg.
- **miss (non-blocking)**: it comes back later on `dmem.resp.replay` and is written through the **long-latency writeback path (`ll_wen`, Doc A §12)**. The scoreboard is set at the moment of the miss:

```scala
// RocketCore.scala:603 (miss detection), 764 (scoreboard set), 817-821 (replay writeback)
603  val wb_dcache_miss = wb_ctrl.mem && !io.dmem.resp.valid
764  val wb_set_sboard = wb_ctrl.div || wb_dcache_miss || wb_ctrl.rocc || wb_ctrl.vec
817  when (dmem_resp_replay && dmem_resp_xpu) { ll_waddr := dmem_resp_waddr; ll_wen := true.B }
```

- While the miss is outstanding, `dcache_blocked` stalls later memory instructions to reduce D$ activity (Doc A §10.2).

### 2.5 Summary of external interaction

| Stage | `io.dmem` signals | Direction | Meaning |
|---|---|---|---|
| EX (s0) | `req.valid/addr/cmd/size/tag` | core→D$ | access request |
| MEM (s1) | `s1_data`, `s1_kill` | core→D$ | store data / cancellation |
| WB (s2) | `resp`, `s2_nack`, `s2_xcpt` | D$→core | data, replay, fault |
| late | `resp.replay` (miss completion) | D$→core | out-of-order writeback |

→ The D$ internals (TLB, MSHRs, replay queue) are implemented in [`HellaCache.scala`](../generators/rocket-chip/src/main/scala/rocket/HellaCache.scala) and [`DCache.scala`](../generators/rocket-chip/src/main/scala/rocket/DCache.scala).

---

## 3. Mul / Div (MUL / MULH / DIV / REM, …)

These are **issued to a long-latency unit in EX**, complete later than WB, and are the first type to use the **scoreboard plus `ll_arb` path** (Doc A §12) in earnest.

### 3.1 Definition (decode)

```
// IDecode.scala:249, 254
MUL  legal=Y ... rxs2=Y rxs1=Y | A2_RS2, A1_RS1, FN_MUL | mem=N | mul=M div=D wxd=Y
DIV  legal=Y ... rxs2=Y rxs1=Y | A2_RS2, A1_RS1, FN_DIV | mem=N | div=Y wxd=Y
```

- **`alu_fn` is reused** as the routing selector for the unit: `FN_MUL=FN_ADD`, `FN_MULH=FN_SL`, `FN_DIV=FN_XOR`, `FN_REM=FN_OR`, … ([ALU.scala:47-55](../generators/rocket-chip/src/main/scala/rocket/ALU.scala#L47-L55)).
- MUL sets both the `mul` and `div` flags — with `pipelinedMul` it is handled by the pipelined multiplier, otherwise by the `div` unit (MulDiv) (§3.4).

**Pipeline path:**

```
ID ───────────▶ EX ──────────────▶ ╌╌ late ╌╌▶ (completes after WB)
rf.read         div/mul.req          ┌ mul fixed latency ╌▶ wb_ctrl.mul ─▶ RF
                in1=rs1, in2=rs2      └ div variable latency ╌▶ ll_arb ╌▶ ll_wen ─▶ RF
                scoreboard.set ╌╌ (ID stalls while rd is outstanding) ╌╌ clear

  external interaction: core-internal units (MulDiv / PipelinedMultiplier)
```

### 3.2 EX — issue

**Operand mapping (ID→EX):** mul/div are R-type (`rxs1=rxs2=Y`) and read `rs1` and `rs2`, but those values go **straight to the multiply/divide unit inputs without passing through the ALU's `ex_op1/ex_op2` mux** (the operation kind reuses `alu_fn`).

```scala
// RocketCore.scala:519-527 excerpt
519  div.io.req.valid    := ex_reg_valid && ex_ctrl.div
521  div.io.req.bits.fn  := ex_ctrl.alu_fn   // FN_MUL/FN_DIV/… (encoding shared with the ALU)
522  div.io.req.bits.in1 := ex_rs(0)         // = rs1 (bypassing the ALU mux)
523  div.io.req.bits.in2 := ex_rs(1)         // = rs2
524  div.io.req.bits.tag := ex_waddr         // destination rd as the tag
527    m.io.req.valid := ex_reg_valid && ex_ctrl.mul   // the pipelinedMul path
```

- If `div` does not accept the request (`!div.io.req.ready`), EX is replayed and ID stalls (Doc A §7.2 ⑤, §10.2 — the `id_ctrl.div` term).

### 3.3 MEM/WB — long-latency writeback

- **Multiplication (pipelined)**: after a fixed latency the result enters the `rf_wdata` mux through the `wb_ctrl.mul` path:

```scala
// RocketCore.scala:830
830                 Mux(wb_ctrl.mul, mul.map(_.io.resp.bits.data).getOrElse(wb_reg_wdata),
```

- **Division (variable latency)**: once `div.io.resp` is ready, the result writes back out of order through `ll_arb` (input 0):

```scala
// RocketCore.scala:792-795
792  div.io.resp.ready       := ll_arb.io.in(0).ready
793  ll_arb.io.in(0).valid   := div.io.resp.valid
794  ll_arb.io.in(0).bits.data := div.io.resp.bits.data
795  ll_arb.io.in(0).bits.tag  := div.io.resp.bits.tag   // rd recovered from the tag
```

- Between issue and completion the destination is protected by the **scoreboard**, so a later instruction reading that rd stalls via `id_sboard_hazard` (Doc A §12.2). The bit is cleared when the div result arrives:

```scala
// RocketCore.scala:708 (killing a stale div), 1012 (sboard clear bypass)
708  div.io.kill := killm_common && RegNext(div.io.req.fire)
```

### 3.4 pipelinedMul versus non-pipelined

```scala
// RocketCore.scala:222, 518
222  val pipelinedMul = usingMulDiv && mulDivParams.mulUnroll == xLen
518  val div = Module(new MulDiv(if (pipelinedMul) mulDivParams.copy(mulUnroll = 0) else mulDivParams, width = xLen))
```

- With `pipelinedMul`, multiplication goes to a separate `PipelinedMultiplier` (fixed latency, the `mul` path) and only division uses `MulDiv`.
- Otherwise both multiplication and division use `MulDiv` (variable latency, the `div` + `ll_arb` path).

### 3.5 External interaction

These are **core-internal modules** ([`Multiplier.scala`](../generators/rocket-chip/src/main/scala/rocket/Multiplier.scala)) and never leave the core IO. The nature of the interaction is "variable latency plus out-of-order completion", which shares the **same scoreboard/`ll_arb` mechanism** as a load miss (hence the placement right after Load/Store).

---

## 4. Branch / Jump (BEQ·BNE·… / JAL / JALR)

**The comparison happens in EX; the resolution and redirect happen in MEM** (→ Doc A §11, and note the common misconception flagged there). Interaction with the predictors is covered **only up to the core interface** (→ Doc A §3, §11.3).

**Pipeline path (common to Branch / JAL / JALR):**

```
ID ───────────▶ EX ──────────────▶ MEM ───────────────▶ WB
rf.read         compare cmp_out (br)  mem_npc·take_pc_mem    JAL/JALR: rd = link (PC+4)
                / adder (jump)       ├ prediction correct → continue   Branch: no write (wxd=N)
                → mem_br_taken       └ mispredict ⟲ flush upstream + refetch mem_npc
                io.imem ↕ btb_update / bht_update ─▶ Frontend      (RAS: DontCare)
```

### 4.1 Branch (conditional) — definition

```
// IDecode.scala:74-77
BEQ  branch=Y rxs2=Y rxs1=Y | A2_RS2, A1_RS1, IMM_SB, FN_SEQ  | mem=N | wxd=N
BNE  ...FN_SNE   BLT ...FN_SLT   BLTU ...FN_SLTU  (SGE/SGEU use fn rather than swapping the sources)
```

- `wxd=N` (no register write). `sel_imm=IMM_SB` (the branch offset).

**ID — there is no destination (rd):** because a branch has `wxd=N`, there is **no rd to write.** The instruction's `inst(11,7)` field is not an rd either — it is reinterpreted as branch offset bits (`IMM_SB`). In other words a branch has no "destination register to hold the target", and its only result is the **PC direction** (taken/not-taken); the target PC goes to `mem_npc` in MEM, not to a register.

**Operand mapping (ID→EX):** a branch reads both `rs1` and `rs2` like an R-type (`rxs1=rxs2=Y`) and uses them as the **ALU comparison inputs**. `sel_imm=IMM_SB` is not an EX operand; it is used only for the target computation in MEM.

```scala
// RocketCore.scala:480,486 (ALU operand mux) — the values being compared
480    A1_RS1 -> ex_rs(0).asSInt,   // op1 = rs1
486    A2_RS2 -> ex_rs(1).asSInt,   // op2 = rs2
```

**EX — the condition comparison:** the ALU compares the `A1_RS1` and `A2_RS2` above using `FN_SEQ/SNE/SLT/…` to produce `cmp_out`, which is latched into `mem_br_taken`:

```scala
// ALU.scala:96 (comparison), RocketCore.scala:667 (latch)
 96  io.cmp_out := cmpInverted(io.fn) ^ Mux(cmpEq(io.fn), in1_xor_in2 === 0.U, slt)
667  mem_br_taken  := alu.io.cmp_out
```

**MEM — resolution and redirect:** the target is built from the actual direction and compared against the prediction; on a misprediction `take_pc_mem` is raised:

```scala
// RocketCore.scala:622-636 excerpt
622  val mem_br_target = mem_reg_pc.asSInt +
623    Mux(mem_ctrl.branch && mem_br_taken, ImmGen(IMM_SB, mem_reg_inst),  // taken → the offset
625    Mux(mem_reg_rvc, 2.S, 4.S)))                                        // not-taken → sequential
634  val mem_direction_misprediction = mem_ctrl.branch && mem_br_taken =/= (usingBTB.B && mem_reg_btb_resp.taken)
636  take_pc_mem := mem_reg_valid && !mem_reg_xcpt && (mem_misprediction || mem_reg_sfence)
```

- On a misprediction the upstream stages are killed (flushed) and `mem_npc` is refetched (Doc A §10.3, §11.2).

**Predictor updates (core→Frontend):**

```scala
// RocketCore.scala:1114-1119
1114  io.imem.bht_update.valid      := mem_reg_valid && !take_pc_wb
1116  io.imem.bht_update.bits.taken := mem_br_taken
1117  io.imem.bht_update.bits.mispredict := mem_wrong_npc
1118  io.imem.bht_update.bits.branch     := mem_ctrl.branch
```

→ The internal structure of the BHT/BTB is in [`BTB.scala`](../generators/rocket-chip/src/main/scala/rocket/BTB.scala) (outside the core).

### 4.2 JAL — diff (unconditional jump)

```
// IDecode.scala:81
JAL  jal=Y | A2_SIZE, A1_PC, IMM_UJ, FN_ADD | mem=N | wxd=Y
```

- Because it is **unconditional**, there is no condition comparison (`branch=N`). The target is computed in MEM from `IMM_UJ` ([RocketCore.scala:624](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L624)).
- **ID — the destination (rd) holds the link (PC+4), not the target:** JAL has `wxd=Y` and writes the **return address PC+4** to rd. Contrary to a common misconception, rd does not receive the *target* — it receives the **link**; the target goes to the next PC (`mem_npc`), not to rd. The rd address is decoded in ID as `id_waddr`, and extracted from `inst(11,7)` in the later stages. If that rd is x1/x5 (a link register), the predictor is hinted with **call** (see `CFIType` below).

```scala
// RocketCore.scala:336 (ID rd decode), 460 (EX waddr)
336  val (id_waddr_illegal, id_waddr) = decodeReg(id_expanded_inst(0).rd)  // rd = inst(11,7)
460  val ex_waddr = ex_reg_inst(11,7) & regAddrMask.U                      // downstream waddr
```

- **Operand mapping (ID→EX):** JAL **reads no integer source registers** (`rxs1=rxs2=N`). The ALU inputs are not `rs` values but the `PC` and a constant size → producing the link value (PC+4).

```scala
// RocketCore.scala:481,488 (ALU operand mux) — JAL: link value PC+size
481    A1_PC   -> ex_reg_pc.asSInt,          // op1 = PC
488    A2_SIZE -> Mux(ex_reg_rvc, 2.S, 4.S)  // op2 = 2 (rvc) / 4
```

- **The key diff — producing the link value (rd = PC+4):** `A1_PC + A2_SIZE` → `alu.io.out = PC + (rvc?2:4)` = the return address → `mem_reg_wdata`. With `wxd=Y` it is written to rd.
- `mem_misprediction` follows the same path as a branch (redirect on a wrong prediction). When updating the BTB, an rd of x1/x5 produces a `CFIType.call` hint:

```scala
// RocketCore.scala:1103-1106
1104    Mux((mem_ctrl.jal || mem_ctrl.jalr) && mem_waddr(0), CFIType.call,
1105    Mux(mem_ctrl.jalr && (mem_reg_inst(19,15) & regAddrMask.U) === BitPat("b00?01"), CFIType.ret,
1106    Mux(mem_ctrl.jal || mem_ctrl.jalr, CFIType.jump, CFIType.branch)))
```

### 4.3 JALR — diff (register-indirect jump + RAS)

```
// IDecode.scala:82
JALR  jalr=Y rxs1=Y | A2_IMM, A1_RS1, IMM_I, FN_ADD | mem=N | wxd=Y
```

- **Operand mapping (ID→EX):** JALR reads only `rs1` (`rxs1=Y, rxs2=N`). The ALU inputs are `rs1` (base) and `imm` (IMM_I), so the adder computes the **target `rs1+imm`**.

```scala
// RocketCore.scala:480,487 (ALU operand mux) — JALR: target rs1+imm
480    A1_RS1 -> ex_rs(0).asSInt,   // op1 = rs1 (base)
487    A2_IMM -> ex_imm,            // op2 = imm (IMM_I)
```

- **ID — the subtle relationship between the destination (rd) and the target:** JALR too ultimately writes the **link (PC+4)** to rd (`wxd=Y`). In the datapath, however, **the rd writeback slot `mem_reg_wdata` first holds the ALU result, i.e. the target (`rs1+imm`)** — because that value is exactly what becomes `mem_npc` (the next PC). So the target sits briefly "in the rd slot", and the link value actually written to rd is taken from `mem_br_target` (the sequential PC+4) by the `mem_int_wdata` mux just before WB, **swapping the two** (see below). If rd/rs1 is x1/x5, that is also used as a call/ret hint. → The intuition that "the target appears in rd" comes from this intermediate slot (`mem_reg_wdata`); the final rd value is the link.

- **The target is `rs1+imm`** (dynamic). Unlike JAL, the ALU adder computes the **target**, so `mem_reg_wdata = rs1+imm` becomes `mem_npc`:

```scala
// RocketCore.scala:626
626  val mem_npc = (Mux(mem_ctrl.jalr || mem_reg_sfence,
                       encodeVirtualAddress(mem_reg_wdata, mem_reg_wdata).asSInt,  // jalr: rs1+imm
                       mem_br_target) & (-2).S).asUInt
```

- **Swapping the link value (PC+4) and the target:** since `mem_reg_wdata` holds the target for JALR, the PC+4 to be written to rd is taken from `mem_br_target` (which in this case is the sequential PC+4) and **swapped in** by a mux:

```scala
// RocketCore.scala:631
631  val mem_int_wdata = Mux(!mem_reg_xcpt && (mem_ctrl.jalr ^ mem_npc_misaligned),
                            mem_br_target,        // jalr → rd = PC+4 (the link)
                            mem_reg_wdata.asSInt).asUInt
```

> **JAL versus JALR in brief**: for JAL the ALU computes the *link (PC+4)* and the target comes from `mem_br_target`; for JALR the ALU computes the *target (rs1+imm)* and the link comes from `mem_br_target`. The roles are swapped, and the `mem_int_wdata` mux sorts it out.

- **RAS**: when a JALR is a return (rs1 = x1/x5) the core only sends the `CFIType.ret` hint ([:1105](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1105)). The actual RAS push/pop is **not done by the core** — `io.imem.ras_update := DontCare` ([:1121-1122](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1121-L1122)); it is handled by the Frontend and `BTB.scala` (Doc A §3, §11.3).
- JALR cannot be bypassed (`ex_ctrl.jalr` is one of the `ex_cannot_bypass` terms, [:1018](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1018)) → the pipeline stalls where necessary to settle rs1.

### 4.4 Summary of external interaction

| Item | Signal | Direction | Notes |
|---|---|---|---|
| prediction consumption | `*_reg_btb_resp` | Frontend→core | flows into EX/MEM |
| BTB update | `io.imem.btb_update` (+ `CFIType`) | core→Frontend | call/ret/jump/branch hints |
| BHT update | `io.imem.bht_update` | core→Frontend | direction training |
| RAS | `io.imem.ras_update` = **DontCare** | — | the core is not involved |
| redirect | `io.imem.req.bits.pc` ← `mem_npc` | core→Frontend | refetch after a flush |

---

## 5. FP (FLW/FSW, FADD, FMADD, FCVT, …)

These are delegated to the **external FPU (`io.fpu`) running alongside** the integer pipeline, and their results usually return through a **decoupled long-latency writeback**. → for the external block see Doc A §3 (`io.fpu`).

### 5.1 Definition (decode)

```
// IDecode.scala:365 (FLW as an example)
FLW  fp=Y rxs1=Y | A2_IMM, A1_RS1, IMM_I, FN_ADD | mem=Y, M_XRD | wfd=Y wxd=N
```

- Distinguishing signal: **`fp=Y`**. The floating-point destination is `wfd` (write FP dest) and the sources are `rfs1/2/3`. An integer register write (`wxd`) occurs only for int↔fp move instructions.
- The FP side also decodes in parallel: `io.fpu.dec.*` (ldst/wen/fma/…) drives the finer distinctions ([RocketCore.scala:193-199](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L193-L199)).

**Pipeline path (parallel to the integer pipeline):**

```
ID ───────────▶ EX ──────────────▶ MEM ───────────────▶ WB
io.fpu.valid    fromint_data        fpu.nack? ⟲ replay     fp→int: toint_data ─▶ RF (int)
                io.fpu ↕──────────────────────────────────↕  fp result ╌ FPU-internal latency ╌▶ FP RF
FP load/store:  reuses the §2 path (tag fp=1) ╌▶ ll_resp           hazards: fp_sboard
```

### 5.2 Per-stage diff

**ID/EX — issuing to the FPU:** the instruction and the integer operand are handed to the FPU with the same timing as the integer pipeline:

```scala
// RocketCore.scala:1124-1128 excerpt
1124  io.fpu.valid := !ctrl_killd && id_ctrl.fp
1125  io.fpu.killx := ctrl_killx
1126  io.fpu.killm := killm_common || vec_kill_mem
1127  io.fpu.inst  := id_inst(0)
1128  io.fpu.fromint_data := ex_rs(0)      // the integer source for an int→fp move
```

**Operand mapping (ID→EX):** the floating-point sources (`rfs1/2/3`) are read from **the FP register file inside the FPU**, not from the integer pipeline's `ex_op1/ex_op2` mux or `ex_rs`. The only sources the integer pipeline hands over are `ex_rs(0)` for int↔fp moves (→ `fromint_data`) and the address base (`rs1`) of an FP load/store.

| FP instruction family | Integer source (`rxs`) | Floating-point source (`rfs`, inside the FPU) |
|---|---|---|
| FADD/FMUL/FMADD and other arithmetic | — | `rfs1/rfs2/(rfs3)` |
| FMV.W.X, FCVT.S.W (int→fp) | `rs1` → `fromint_data` | — |
| FMV.X.W, FCVT.W.S, comparisons (fp→int) | — (the result comes back as `toint_data`) | `rfs1/(rfs2)` |
| FLW/FSW (load/store) | `rs1` (address base) | store data is `rfs2` (FPU) → `io.fpu.store_data` |

**MEM — a nack is possible:** if the FPU cannot accept the instruction, MEM is killed and replayed:

```scala
// RocketCore.scala:703
703  val fpu_kill_mem = mem_reg_valid && mem_ctrl.fp && io.fpu.nack_mem
```

**WB — two kinds of result:**
- **fp→int moves** (FCVT.W, FMV.X, comparisons, …): `io.fpu.toint_data` goes to the integer writeback:

```scala
// RocketCore.scala:719
719  wb_reg_wdata := Mux(!mem_reg_xcpt && mem_ctrl.fp && mem_ctrl.wxd, io.fpu.toint_data, mem_int_wdata)
```

- **fp results (written to the FP register file)**: the FPU completes them separately after **its own latency**. Hazards are managed by a dedicated FP scoreboard:

```scala
// RocketCore.scala:1042-1050 excerpt (fp_sboard)
1043    val fp_sboard = new Scoreboard(32)
1044    fp_sboard.set(((wb_dcache_miss || wb_ctrl.vec) && wb_ctrl.wfd || io.fpu.sboard_set) && wb_valid, wb_waddr)
1047    fp_sboard.clear(io.fpu.sboard_clr, io.fpu.sboard_clra)
1049    checkHazards(fp_hazard_targets, fp_sboard.read _)   // → id_stall_fpu
```

### 5.3 FP load/store

FP loads and stores **reuse the integer Load/Store path (§2)**, with the destination in the FP register file:
- The D$ request tag carries `ex_ctrl.fp` to distinguish them ([:1161](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1161)).
- On the response, `dmem_resp_fpu` (tag bit 0) routes the data to the FPU's long-latency response port:

```scala
// RocketCore.scala:1129-1132 excerpt
1129  io.fpu.ll_resp_val  := dmem_resp_valid && dmem_resp_fpu
1130  io.fpu.ll_resp_data := (if (minFLen == 32) io.dmem.resp.bits.data_word_bypass else io.dmem.resp.bits.data)
1132  io.fpu.ll_resp_tag  := dmem_resp_waddr
```

- Store data enters s1_data from `io.fpu.store_data` ([:1178](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala#L1178)).

### 5.4 Summary of external interaction

| Direction | Signal | Meaning |
|---|---|---|
| core→FPU | `io.fpu.valid/inst/fromint_data`, `killx/killm` | issue and cancellation |
| FPU→core | `io.fpu.nack_mem` | MEM replay |
| FPU→core | `io.fpu.toint_data` | fp→int result (WB) |
| FPU↔core | `io.fpu.sboard_set/clr`, `fcsr_*` | long-latency completion and flags |
| D$→FPU | `io.fpu.ll_resp_*` | FP load result |

→ The FPU internals are implemented in the tile's `FPU` (rocket-chip `tile/FPU.scala`).

---

## Appendix A. Stage × instruction-type signal matrix

A summary of how the main control signals are set per type. (`Y` = 1, `-` = 0/don't care, otherwise the constant value.)

| Signal (stage) | ALU-R | ALU-I | Load | Store | Mul/Div | Branch | JAL | JALR | FP |
|---|---|---|---|---|---|---|---|---|---|
| `rxs1` (ID) | Y | Y | Y | Y | Y | Y | - | Y | Y* |
| `rxs2` (ID) | Y | - | - | Y | Y | Y | - | - | - |
| `sel_alu1` (EX) | RS1 | RS1 | RS1 | RS1 | RS1 | RS1 | **PC** | RS1 | RS1 |
| `sel_alu2` (EX) | RS2 | IMM | IMM | IMM | RS2 | RS2 | **SIZE** | IMM | IMM |
| `sel_imm` (EX) | - | I | I | **S** | - | **SB** | **UJ** | I | I |
| `alu_fn` (EX) | op | op | ADD | ADD | (MUL/DIV) | (compare) | ADD | ADD | ADD |
| `io.dmem.req` (EX) | - | - | **Y** | **Y** | - | - | - | - | Y (ld/st) |
| `div.io.req` (EX) | - | - | - | - | **Y** | - | - | - | - |
| `io.fpu.valid` (EX) | - | - | - | - | - | - | - | - | **Y** |
| uses `mem_br_taken` (MEM) | - | - | - | - | - | **Y** | - | - | - |
| `mem_npc` source (MEM) | - | - | - | - | - | target | target | **rs1+imm** | - |
| triggers `take_pc_mem` (MEM) | - | - | - | - | - | **mispred** | mispred | mispred | - |
| `wxd` (WB) | Y | Y | Y | - | Y | - | Y | Y | moves only |
| `wfd` (WB) | - | - | - | - | - | - | - | - | **Y** |
| writeback path | normal | normal | resp/**ll** (miss) | none | **ll**/mul | none | normal | normal | **FPU/ll** |
| scoreboard set | - | - | on a miss | - | **Y** | - | - | - | **fp_sboard** |

\* `rxs1` for FP applies to int↔fp moves and address computation. Pure FP arithmetic uses `rfs1/2/3`.

## Appendix B. "New concepts introduced" per type (a recap of the learning order)

| Chapter | New relative to Doc A | Key signals |
|---|---|---|
| ALU | (baseline) | `alu_fn`, `sel_alu*`, `wxd` |
| Load/Store | the external D$ (s0/s1/s2), load-use, out-of-order writeback | `io.dmem.*`, `id_load_use`, `dmem.resp.tag` |
| Mul/Div | the scoreboard/`ll_arb` path in full | `div.io.req/resp`, `wb_set_sboard`, `ll_wen` |
| Branch (+JAL/JALR) | branch resolution (EX→MEM), redirect, the predictor interface | `mem_br_taken`, `take_pc_mem`, `btb/bht_update` |
| FP | the external FPU, decoupled writeback, `wfd` | `io.fpu.*`, `fp_sboard`, `ll_resp_*` |

## Appendix C. References

- Shared skeleton and vocabulary: [Doc A — Pipeline Overview](Rocket_pipeline_doc_a_overview.md) (§1~§14)
- Code: [`RocketCore.scala`](../generators/rocket-chip/src/main/scala/rocket/RocketCore.scala), [`IDecode.scala`](../generators/rocket-chip/src/main/scala/rocket/IDecode.scala), [`ALU.scala`](../generators/rocket-chip/src/main/scala/rocket/ALU.scala), [`Multiplier.scala`](../generators/rocket-chip/src/main/scala/rocket/Multiplier.scala)
- External blocks: [`Frontend.scala`](../generators/rocket-chip/src/main/scala/rocket/Frontend.scala) and [`BTB.scala`](../generators/rocket-chip/src/main/scala/rocket/BTB.scala) (prediction), [`HellaCache.scala`](../generators/rocket-chip/src/main/scala/rocket/HellaCache.scala) and [`DCache.scala`](../generators/rocket-chip/src/main/scala/rocket/DCache.scala) (D$)

---

*End of Doc B.*
