# goose-debugger

Debugging Goose programs with the debuggers that already exist (LLDB, and
through it any editor that speaks DAP), rather than a debugger inside the
compiler. The reasoning and the plan are in
[docs/goose_debugger.md](docs/goose_debugger.md).

Nothing here changes goose.git. What the compiler needs is kept as draft
patches for its author, against the goose.git commit each one names.

## Layout

- `patches/`: draft compiler patches, made with `git format-patch`, in the
  order they were written (and applied on goose.git's `debug1`).
  - `0001`: `#line` directives under `--debug`, so a debugger shows, steps
    and stops on the Goose source.
  - `0002`: `--debug` implies Goose `-O0` (no inlining) unless an `-O` is
    given, so every function keeps its own frame.
  - `0003`: one breakpoint location per Goose line, and a breakpoint in a
    loop's body stops each time round.
  - `0004`: `goose run --debug` prints Goose frames (`at fn (file.goose:N)`)
    after a runtime abort or a crash. macOS arm64 tested; Linux untested.
  - `0005`: `--debug` writes `<stem>.debug.json` beside the C: the Goose
    names, types and scopes of functions and locals, and the fields and
    variants of their types.
  - `0006`: all of the above moves from `--debug` to a flag of its own,
    `-g` (or `--debug-info`), which also turns on `--debug`'s checks.
    `--debug` alone is again what it is upstream: the checks and C debug
    information, with the same C as without the patches.
  - `0007`: under `-g`, the arms of a `match` and the tails of functions and
    blocks get their own lines; no resizable's count is cached, so a
    debugger sees the array as it is while a loop grows it; the map gains
    globals and what each function-value parameter is bound to.
- `patches/upstream/`: the same final change as three patches for the
  Goose author, each building and passing on its own: `-g` itself, the
  debug map, and `goose run -g`'s backtraces.
- `dap/`: `goose-dap`, a Debug Adapter Protocol proxy in front of LLDB's
  `lldb-dap` that makes an editor's debugger Goose-level: frame names,
  only the Goose locals in scope, Goose watch expressions and function
  breakpoints, and a "Goose runtime errors" exception filter.
- `gdb/`: `goose_gdb.py`, gdb pretty-printers, `goose-locals`, `goose-bt`,
  `goose-globals`, `goose-as` and a frame filter for Goose names in `bt`,
  and its tests.
- `lldb/`: `goose_lldb.py`, LLDB summaries and synthetic children for Goose
  values, and its tests.
- `tests/`: checks of the patched compiler: `line_directives.sh` through
  LLDB in batch mode, `backtrace.sh` for `goose run -g`.
- `docs/`: the plan.

## Trying the patches

```sh
git clone ~/Dev/Goose/goose.git goose-dbg && cd goose-dbg
git checkout 9412b80f
git am ../goose-debugger.git/patches/*.patch
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j8
GOOSE=$PWD/build/goose ../goose-debugger.git/tests/line_directives.sh
GOOSE=$PWD/build/goose ../goose-debugger.git/tests/backtrace.sh
```

Then, for any program:

```sh
goose build -g -o prog prog.goose
lldb -o 'b prog.goose:12' -o run ./prog
```

## Platforms

- **macOS arm64** (clang, Apple lldb-2100): everything here is developed and
  tested on it.
- **Linux x86_64** (Ubuntu 20.04; gcc 9.4 and clang 17; LLDB 16, gdb 9.2):
  patches 0001-0004 pass `line_directives.sh` (15/15, with either C
  compiler), `backtrace.sh` (22/22) and the LLDB module tests (9/9). gdb
  also stops on Goose lines, once per line and each time round a loop.
  gdb older than 10 cannot read clang's DWARF 5: use gcc, `-gdwarf-4`, or
  LLDB. The abort backtraces of 0004 need the compiler's
  `jit_runtime.c` built with frame pointers (0004 does that); crash
  backtraces work without. With patches 0005-0007 the gdb printers pass
  their tests (16/16 with gcc; clang needs `-gdwarf-4` for gdb before 10).
- **Windows** (Server 2022, MSVC 19.44; cdb 10.0.26100, LLDB 23.1 on PDB):
  patches 0001-0004 work. `goose build --debug` writes a PDB beside the
  exe; cdb and LLDB bind breakpoints to Goose lines (one location per
  line, each time round a loop), step by Goose line and show `.goose`
  frames, `area_g` keeping its own frame. The compiler suite passes, and
  714 test programs built with `--debug` print what `goose run --debug`
  prints. cdb needs `sxd av`: the runtime commits its stacks from a
  vectored exception handler, which cdb otherwise stops on. 0004's
  backtraces are off on Windows by design.

## Status

1. `#line` under `--debug`: done as draft patches 0001 to 0003, applied
   on goose.git's `debug1` branch.
2. LLDB module: done (`lldb/`). Slices, arrays, text, structs, enums and
   variants, references and optionals show as Goose values; resizables
   show their length only. `lldb/README.md` lists what DWARF cannot tell.
3. Debug map and DAP adapter: the map is patch 0005 (also on `debug1`) and
   `goose_lldb.py` reads it: Goose names and scopes (`goose-locals`,
   `goose-bt`, `goose-frame-format`), resizables' elements, variable-size
   values decoded, tag names, `T&`/`T?`/relative references and `bool`.
   With 0007 also globals (`goose-globals`), live lengths while a resizable
   grows, and function-value instances (`twice<F=inc>`). The `goose dap`
   proxy is `dap/goose-dap` (18 DAP tests on macOS);
   `docs/idea_integration.md` lists what an IDE needs from it.
4. Backtraces in `goose run`: done as draft patch 0004 (also on
   `debug1`). Tested on macOS arm64 only; `tests/backtrace.sh` should be
   run on Linux.
