# A debugger for Goose

Thoughts on how to debug Goose programs, written against Goose's own
design note (`goose.git/docs/design/debugging.md`) and the compiler as it
is at goose.git `9412b80f`. Nothing here has been changed in goose.git:
anything that needs the compiler is meant as a draft patch for its author.

## Where things stand

- Goose compiles to C, then either runs it under TinyCC (`goose run`) or
  hands it to clang, gcc or MSVC (`goose build`).
- `--debug` only adds `-O0 -g` (or `/Od /Zi`) to the C compiler
  (`src/build.h:52-56`). Goose's own inliner and constant folding still run.
- The generated C has no `#line` directives and there is no debug map. A
  native debugger therefore steps through generated C and shows mangled
  names like `area_g_2`.
- Source positions already reach the C, but only at checks and aborts:
  `gs_abort(err, file, line)`, `GS_UNREACHABLE(gs_file0, 19)`, and
  `CodeGen::FileRef`. Every statement in the AST carries its file and line
  (`src/ast.h`). So emitting `#line` would extend something the compiler
  already does, not add a new mechanism.
- `lldb-dap` comes with LLDB (on macOS it is in the Command Line Tools).
- TinyCC has a backtrace hook (`tcc_set_backtrace_func`) that Goose does not
  use yet.

## The options in the design note

- **A.** `#line` plus visualizers inside existing debuggers: natvis for
  Visual Studio, a Python module for LLDB.
- **B.** `goose dap`: a debugger inside the compiler, driving the JIT
  through stop points that the code generator inserts.
- **C.** `goose dap` in front of `lldb-dap`, translating between Goose and C
  with the help of a `<prog>.debug.json` map.

The note recommends C. I agree with it. B means writing a debugger, with
stepping, breakpoints, threads and signals, inside the compiler and keeping
it in step with the code generator; only the JIT would ever get it. C reuses
LLDB for everything that is hard and keeps the Goose-specific part as a
translation layer. It works the same for JIT and native builds wherever
LLDB can attach.

I would build C in a different order from the note's, so that each step is
useful on its own and the compiler changes stay small.

## A plan in four steps

### 1. `#line` under `--debug` (a draft patch; 2–3 days)

- Emit `#line N "file.goose"` before each statement whose Goose line differs
  from the previous one, only when `--debug` is given.
- Never emit `#line 0`. The prototype writes it, but C requires a line
  number from 1 to 2147483647 (C17 §6.10.4), and compilers treat it
  differently.
- Make `--debug` imply Goose's own `-O0`: no inlining and no constant
  propagation, and `let` variables kept as real C locals. Otherwise frames
  and values disappear exactly where people look for them.
- Test it with lldb in batch mode: set a breakpoint at a Goose line, run,
  and check the backtrace shows `.goose` files and lines.

This step alone gives LLDB, GDB, Visual Studio and the VS Code C/C++
extensions breakpoints, stepping and call stacks in Goose source. It is the
most useful single change and the least controversial.

A related question for the author is whether `#line` should also appear in
release builds. Profilers such as perf and Instruments would then report
Goose lines too. It costs nothing at run time, only debug-info size.

### 2. An LLDB Python module, outside goose.git (about a week)

A `goose_lldb.py` with type summaries and synthetic children, matched by
regex on the generated C type names:

- slices (`sl_*`): length plus the elements;
- ADTs: the tag, and the active variant's payload;
- text: the string, not bytes.

It works with today's compiler, without any patch, and can be loaded with
`command script import`. It will also show quickly where C-level information
runs out:

- resizable arrays are an untyped `gs_rhdr` in C, so the element type is
  not knowable from DWARF;
- values in variable mode are raw bytes;
- relative references are plain integers.

These are the cases that need step 3.

### 3. A debug map and a DAP adapter (2–4 weeks)

**The map** (a draft patch) is written by the compiler next to the program,
or into a section of it. It holds what DWARF cannot:

- for each C function, the Goose name, including generic instances and
  function values;
- for each local, its Goose name, Goose type and the line range it is in
  scope;
- element types of resizables, and variant names of ADTs;
- layouts, taken from the C compiler with a table of `offsetof` and `sizeof`
  compiled into the program. Goose packs some structures, so working out
  layout a second time in the adapter would be fragile.

**The adapter** is a separate process that speaks DAP to the editor and DAP
to `lldb-dap`:

- it passes most requests through unchanged;
- it renames frames from the map;
- it rewrites `variables` and `evaluate` responses into Goose values, reading
  memory through LLDB;
- it hides runtime frames (`gs_*`) by default.

It can live outside goose.git, for instance with gslib's tooling, until the
author wants it in-tree. That keeps the compiler change down to the map.

### 4. Backtraces in `goose run` (small, independent)

Once `#line` exists, TinyCC's `tcc_set_backtrace_func` can print Goose
files and lines when a JIT-run program crashes, on ELF and Mach-O. This
helps everyone who uses `goose run` and never opens a debugger.

Done as patch 0004. Under `goose run --debug` a runtime abort (bounds,
overflow, assert) or a crash prints the Goose frames after the usual
message, innermost first, with Goose names (`at geo::area (geo.goose:5)`).
TinyCC compiles with line information only (`-bt -g1dwarf`); three small
TinyCC fixes were needed, recorded in its README in goose.git. Tested on
macOS arm64; Linux uses TinyCC's ordinary ELF path but has not been run.
Windows is left as it was, since TinyCC's PE target would link its
backtrace object into every in-memory run.

## The hard parts

- **Data stacks.** Many locals do not live on the C stack. They live on
  per-thread data stacks, reached through the hidden `gs_sp` index into
  `gs_stks` (`src/runtime/runtime.h:903` and `:1009`). DWARF only sees an
  index. Showing those values needs the runtime's stack table, which is a
  job for the map and the adapter.
- **Resizables, variable-mode values and packed ADTs** each need a decoder,
  driven by the map's types and layouts.
- **Optimization.** Under `--debug` the Goose optimizer must be off.
  Otherwise inlined functions lose their frames and folded `let`s have no
  storage.
- **Scopes.** `const for` copies its body, and locals' scopes are
  approximate when they are given as line ranges (the note's §6.5). This
  matters for showing the right variables, not for correctness.
- **Threads.** They work under LLDB in built programs. The JIT refuses
  threaded programs, so threads can only be debugged in built executables.
- **Coroutines** are designed but not implemented. Leave them out until they
  exist.
- **Windows.** LLDB's PDB support is weaker than its DWARF support. The
  alternative there is the VS Code `cppvsdbg` adapter, with natvis files in
  place of the Python module. It needs the same map.

## Questions for the Goose author

1. Will `#line` under `--debug`, with `--debug` implying `-O0`, be accepted?
   Step 1 depends on it, and everything after it builds on step 1.
2. Should `#line` also go into release builds, for profilers?
3. Should the map be a separate file or a section in the executable?
4. Can locals be declared in nested C blocks, given `defer` and
   `return … from` jumps? That would make scopes exact.
5. Is LLDB on Windows good enough, or should Windows go through `cppvsdbg`
   and natvis?
6. Which `lldb-dap` versions should be supported?

## Summary

Start with `#line`. It is small and needs no new design, and it gives native
debuggers Goose source lines straight away. Then write an LLDB module
against today's C, to learn what is missing. Then add a debug map to the
compiler and write an adapter in front of `lldb-dap` that turns C into
Goose. Don't write a debugger inside the compiler: LLDB already does
everything hard, and reusing it means the JIT and native builds share one
debugger.
