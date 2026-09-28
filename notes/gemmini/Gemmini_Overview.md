# Gemmini — what it is and how it is built

The hardware, in one pass: what Gemmini is, what is inside it, and which
elaboration-time parameters shape it. No ISA detail — that is
[`Gemmini_ISA_tutorial.md`](Gemmini_ISA_tutorial.md) (how to program it) and
[`Gemmini_ISA_reference.md`](Gemmini_ISA_reference.md) (the encoding). No build
or run instructions — those are [`../Gemmini_Build_Test.md`](../Gemmini_Build_Test.md).

**Provenance.** `ucb-bar/gemmini` README (Architecture, Generator Parameters,
Major Components sections), checked against the in-tree RTL at
`generators/gemmini/src/main/scala/gemmini/`. Default-config values are from
`Configs.scala`, `defaultConfig`.

> **The README has drifted from this tree.** The upstream README still describes
> an `ROB` and an `rob_entries` parameter; this tree has
> `ReservationStation.scala` and three separate `reservation_station_entries_*`
> parameters. The README's type list is also short by three. §6 gives the real
> names. Read `GemminiConfigs.scala` at your pinned commit before trusting any
> parameter name, including the ones below.

---

## 1. The shape of the thing

Gemmini is a **RoCC accelerator**: a matrix-multiply engine bolted to the side
of a Rocket or BOOM tile, reached through custom RISC-V instructions rather than
through MMIO or a driver. It is not a RISC-V extension, and the RoCC interface
it hangs off is a Berkeley convention that no RISC-V standard describes — the
consequences of that are the whole subject of the reference document.

Three things define it:

- **A systolic array** at the centre, doing `C = A · B + D` on `DIM × DIM` tiles.
- **Private scratchpad and accumulator SRAMs**, explicitly managed. Nothing is
  cached, nothing is coherent, nothing is implicit. You move data in, compute,
  move it out.
- **Decoupled access/execute**: loads, stores, and computation run concurrently
  in three independent controllers, reordered against each other by a
  reservation station.

By default it connects to memory through the System Bus, straight to L2, and
not through the host's L1 D$.

```
            ┌─────────────────────────── Rocket / BOOM tile ───────────┐
            │                                                          │
            │   core ──RoCC cmd──► Gemmini                             │
            │                        │                                 │
            │        ┌───────────────┼───────────────┐                 │
            │        ▼               ▼               ▼                 │
            │   LoadCtrl        ExecuteCtrl      StoreCtrl             │
            │        │               │               │                 │
            │        │          MeshWithDelays       │                 │
            │        │         (Mesh + Transposer)   │                 │
            │        │               │               │                 │
            │        └──────► scratchpad / accumulator ◄────┘          │
            │                        │                                 │
            │                    DMA + TLB                             │
            └────────────────────────┼─────────────────────────────────┘
                                     ▼
                              System Bus → L2 → DRAM
```

---

## 2. The systolic array

`Mesh.scala` implements the array as a **two-level hierarchy**:

| Level | Module | Composition |
|---|---|---|
| Mesh | `Mesh.scala` | a grid of Tiles, **pipeline registers between tiles** |
| Tile | `Tile.scala` | a **combinational** set of PEs |
| PE | `PE.scala` | one multiply-accumulate, WS or OS |

The split exists to trade area against timing: `tileRows`/`tileColumns` set how
much logic is combinational, `meshRows`/`meshColumns` how many such blocks are
pipelined together. The default config is `tileRows = tileColumns = 1` and
`meshRows = meshColumns = 16` — every PE separated by a register, a 16×16 array.

`DIM` throughout the software is `meshRows × tileRows` = 16 in the default
config, and it is the *only* matmul size the array knows. Everything larger is
tiled, either by you or by the loop unrollers of §5.

### MeshWithDelays

The array is wrapped in `MeshWithDelays.scala`, which supplies what a bare mesh
cannot:

- **Delay registers** on the inputs. Each matrix element has to arrive at its PE
  on exactly the cycle its partner does, so inputs are staggered through shift
  registers. This is the hardware equivalent of the skew you would draw by hand
  in a systolic-array diagram.
- **A transposer** (`Transposer.scala`).
- **Counters and configuration registers.** Every matmul is exactly `DIM × DIM`;
  the counters count to `DIM` and then latch the next operation's configuration
  — which of `A`/`B` to transpose, and whether the preloaded values survive into
  the next matmul.

It takes `A`, `B` and `D` one row per cycle and emits `C = A · B + D` one row
per cycle.

### The transposer is not optional in OS mode

The transposer is used for **every output-stationary matmul**, even when you did
not ask for a transposition. The reason is a layout mismatch: in OS mode the
array wants all elements of one row of `A` to enter the *same* PE on consecutive
cycles, but a scratchpad row stores those elements side by side, which would
feed them to *adjacent* PEs. The transposer turns one into the other. It is
itself a small systolic array — left-to-right for `DIM` cycles, then
bottom-to-top for another `DIM`.

---

## 3. Scratchpad and accumulator

Both live in `Scratchpad.scala`; banks are `ScratchpadBank` and
`AccumulatorMem.scala`. Each row of either is `DIM` **elements** wide.

| | Element type | Default | Row width (default) | Extras |
|---|---|---|---|---|
| Scratchpad | `inputType` | `SInt(8.W)` | 16 × 8 = **128 bits** | little more than SRAM + queue |
| Accumulator | `accType` | `SInt(32.W)` | 16 × 32 = **512 bits** | adders, scalers, activation units |

The asymmetry is the point. The scratchpad is dumb storage for operands. The
accumulator is where the quantization pipeline lives:

- **Adders** for in-place accumulation, which is what makes `K`-dimension tiling
  work without a round trip to DRAM.
- **Scalers.** Reading `accType` out as `inputType` applies a scaling function —
  by default a multiply by a `float32` that casts `int32` down to `int8`. The
  function is configurable at elaboration (`acc_scale_args`).
- **Activation units** — ReLU and friends, applied on the way out.

Scratchpad inputs and outputs are always `inputType`. Accumulator inputs and
outputs may be either: `inputType` in is cast **up** to `accType`, `inputType`
out is scaled **down** from it. That down-conversion — scale, then activate — is
exactly the step that turns one layer's partial sums into the next layer's
quantized inputs, which is why it is in hardware at all.

Conceptually the accumulator is an extension of the scratchpad address space:
the array can write results to any accumulator address and read operands from
any accumulator address, and the DMA can move data directly between the
accumulator and DRAM, which is how biases get loaded.

---

## 4. Decoupled access/execute

Three controllers consume ISA commands independently and share the SRAMs:

| Controller | File | Owns |
|---|---|---|
| `ExecuteController` | `ExecuteController.scala` | matmuls; holds `MeshWithDelays` and the transposer |
| `LoadController` | `LoadController.scala` | DRAM → scratchpad/accumulator |
| `StoreController` | `StoreController.scala` | scratchpad/accumulator → DRAM, **and max-pooling** |

Pooling lives in the store path because Gemmini pools on the way out, as data
leaves the private SRAMs — so the `DMAWriter` carries a set of comparators.

Instructions in *different* controllers run concurrently and out of order.
Instructions in the *same* controller are issued strictly in order, and each
controller handles its own internal hazards on that assumption.

Cross-controller hazards are the job of **`ReservationStation.scala`** — what
the README still calls the ROB. An instruction is released to its controller
only once it has no outstanding dependency on an instruction in another
controller. It does *not* check hazards within a controller.

This is the structural reason Gemmini has no fence of its own and why the host's
`fence` works: the core stalls on Gemmini's `busy` signal, which is the OR of
the reservation station, the scratchpad, the loop unrollers, and pending
commands.

### DMA

Two engines in `DMA.scala`, one per direction, both operating on **virtual
addresses** and sharing a TLB (`FrontendTLB.scala`). On a TLB miss it falls
transparently back to a page-table walker **shared with the host CPU**. This is
why you hand Gemmini plain `malloc`'d pointers and why the same instruction
stream runs bare-metal and under Linux.

Once physical addresses are in hand, the DMAs break large requests into TileLink
reads and writes. TileLink requires each request to be aligned to its own size
and that size to be a power of two, so the engines deliberately **over-read**
when it lets them issue fewer requests — empirically, request count limits
throughput more than a little wasted bandwidth does.

---

## 5. Loop unrollers

The array only does `DIM × DIM`. Tiling anything bigger by hand in C costs loop
overhead, and unrolling and pipelining those loops well is hard. So Gemmini
provides CISC-type instructions that tile and unroll in hardware:
`LoopMatmul.scala` and `LoopConv.scala`.

They are FSMs, and they do two things software finds awkward:

- **Double-buffer** tiles, so the next load overlaps the current matmul.
- **Watch the reservation station's occupancy** and throttle themselves. If the
  station fills with matmul instructions leaving no room for loads, the FSM
  pauses issuing matmuls so loads can get in.

They add no expressive power — anything they do can be written by hand with
`mvin`/`preload`/`compute`/`mvout`. They exist because they schedule better than
most hand-written tiling, and because they eliminate the per-instruction operand
packing the host would otherwise pay for.

---

## 6. Generator parameters

From `GemminiArrayConfig` in `GemminiConfigs.scala`. Default-config values from
`Configs.scala`.

### Array geometry

| Parameter | Default | Meaning |
|---|---|---|
| `tileRows`, `tileColumns` | 1, 1 | PEs per combinational tile |
| `meshRows`, `meshColumns` | 16, 16 | tiles per pipelined mesh |
| `dataflow` | `Dataflow.BOTH` | `OS`, `WS`, or both selectable at runtime |

### Types

The README lists three. This tree has **six**:

| Parameter | Default | Meaning |
|---|---|---|
| `inputType` | `SInt(8.W)` | scratchpad element |
| `weightType` | `SInt(8.W)` | weight element |
| `accType` | `SInt(32.W)` | accumulator element, partial sums |
| `spatialArrayInputType` | `SInt(8.W)` | what enters the array |
| `spatialArrayWeightType` | `SInt(8.W)` | weights inside the array |
| `spatialArrayOutputType` | `SInt(20.W)` | **PE-to-PE** width, not the result width |

`spatialArrayOutputType` is the one that surprises people: it is the width of
the value passed *between* PEs, not of anything you can observe. An 8×8 multiply
needs more than 8 bits to hand on, and the default gives it 20.

For a floating-point `inputType`, also raise `pe_latency` — the number of shift
registers inside each PE — if a multiply-accumulate cannot close in one cycle.

### Memories

| Parameter | Default | Meaning |
|---|---|---|
| `sp_capacity` | 256 KiB | total scratchpad |
| `acc_capacity` | 64 KiB | total accumulator |
| `sp_banks` | 4 | scratchpad banks |
| `acc_banks` | 2 | accumulator banks |
| `sp_singleported` | `true` | |
| `acc_singleported` | `false` | |

### Decoupling

| Parameter | Default | Meaning |
|---|---|---|
| `ld_queue_length` | 8 | load instruction queue |
| `st_queue_length` | 2 | store instruction queue |
| `ex_queue_length` | 8 | execute instruction queue |
| `reservation_station_entries_ld` | 8 | **replaces the README's `rob_entries`** |
| `reservation_station_entries_st` | 4 | |
| `reservation_station_entries_ex` | 16 | |

Relative queue sizes set how far the three paths may slide apart; the
reservation-station entry counts bound how many cross-controller dependencies
can be tracked at once.

### DMA

| Parameter | Default | Coupled to |
|---|---|---|
| `dma_maxbytes` | 64 | Rocket's `cacheblockbytes` |
| `dma_buswidth` | 128 | `SystemBusKey.beatBytes` |
| `max_in_flight_mem_reqs` | 16 | |
| `tlb_size` | 4 | |
| `use_shared_tlb` | `true` | one TLB for both DMAs instead of two |
| `use_dedicated_tl_port` | `true` | own TileLink node vs. tile-local arbiter |

The first two are **not free choices** — they must track the SoC parameters
named beside them.

### Optional features, elaborated in or out

| Parameter | Default | Effect |
|---|---|---|
| `mvin_scale_args` | `None` | scale data during `mvin`; `None` removes the hardware |
| `mvin_scale_acc_args` | `None` | same, on the accumulator port |
| `mvin_scale_shared` | `false` | share multipliers between the two if both scale alike |
| `acc_scale_args` | — | the `accType` → `inputType` scaling function |
| `has_training_convs` | `true` | |
| `has_max_pool` | `true` | |
| `has_nonlinear_activations` | `true` | |
| `hardcode_d_to_garbage_addr` | `false` | |

Setting a scale parameter to `None` does not disable a feature at runtime — it
removes the multipliers and functional units from the elaborated design.

---

## 7. Where things are

All paths under `generators/gemmini/src/main/scala/gemmini/`.

| Topic | File |
|---|---|
| Top level, controller wiring | `Controller.scala` |
| Parameters | `GemminiConfigs.scala`, `Configs.scala` |
| ISA constants | `GemminiISA.scala` |
| Systolic array | `Mesh.scala`, `Tile.scala`, `PE.scala` |
| Array wrapper, delays, counters | `MeshWithDelays.scala` |
| Transposer | `Transposer.scala` |
| Private SRAMs | `Scratchpad.scala`, `AccumulatorMem.scala` |
| Accumulator scaling | `AccumulatorScale.scala`, `Activation.scala` |
| Controllers | `ExecuteController.scala`, `LoadController.scala`, `StoreController.scala` |
| Cross-controller hazards | `ReservationStation.scala` |
| DMA and TLB | `DMA.scala`, `FrontendTLB.scala`, `XactTracker.scala` |
| Loop unrollers | `LoopMatmul.scala`, `LoopConv.scala`, `LoopUnroller.scala` |
| Local address format | `LocalAddr.scala` |
| Performance counters | `CounterFile.scala` |

The RoCC interface itself is one level out, in
`generators/rocket-chip/src/main/scala/tile/LazyRoCC.scala`.

---

## 8. Software stack

| Layer | Where |
|---|---|
| C library and macros | `generators/gemmini/software/gemmini-rocc-tests/include/gemmini.h` |
| Generated parameters | `.../include/gemmini_params.h` — **build output**, emitted from the elaborated config |
| RoCC instruction macros | `.../rocc-software/src/xcustom.h` |
| Baremetal tests | `.../bareMetalC/`, template at `template.c` |
| ONNX Runtime port | `generators/gemmini/software/onnxruntime-riscv/` |
| Functional simulator | Spike with the Gemmini extension |

Gemmini instructions are **not** in GNU binutils, so every instruction reaches
the assembler as a `.insn r` directive built by a C macro. That mechanism is the
subject of the tutorial document.

`gemmini_params.h` being build output is a standing trap: a stale copy compiles
cleanly against a differently-elaborated accelerator and produces wrong results
with no diagnostic.

---

## 9. Reading on

| You want | Go to |
|---|---|
| Build it, run a test | [`../Gemmini_Build_Test.md`](../Gemmini_Build_Test.md) |
| Learn to program it | [`Gemmini_ISA_tutorial.md`](Gemmini_ISA_tutorial.md) |
| Look up an instruction or a bit field | [`Gemmini_ISA_reference.md`](Gemmini_ISA_reference.md) |
| Change the RTL | this document, then the files in §7 |
