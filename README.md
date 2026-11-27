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
  - `0008`: the VS Code extension (goose.git `vscode/`) debugs through
    `dap/goose-dap`: a `goose` launch without `noDebug` builds with
    `goose build -g` and starts goose-dap. Adds the `goose.debugAdapterPath`
    and `goose.lldbDapPath` settings, a **Goose: Debug** command, and
    breakpoints in `.goose` files. Its `goose-language.vsix` still has to be
    rebuilt (`python vscode/build_vsix.py`, which needs the network).
  - `0009`: under `-g` the compiler also writes `<stem>.natvis` beside the
    map, and an MSVC-style link embeds it in the PDB (`/NATVIS`), so Visual
    Studio and cdb show Goose values with nothing to load.
- `patches/upstream/`: the same final change as five patches for the
  Goose author, each building and passing on its own: `-g` itself, the
  debug map, `goose run -g`'s backtraces, the natvis embedded in the PDB,
  and the VS Code extension's debugging.
- `dap/`: `goose-dap`, a Debug Adapter Protocol proxy in front of LLDB's
  `lldb-dap` that makes an editor's debugger Goose-level: frame names,
  only the Goose locals in scope, Goose watch expressions and function
  breakpoints, and a "Goose runtime errors" exception filter. Written in
  Goose (`dap/src/`, built by `dap/scripts/build.sh`) and needing only
  lldb-dap; up to v0.2.0 it was Python. The VS Code extension uses it as of
  patch 0008.
- `gdb/`: `goose_gdb.py`, gdb pretty-printers, `goose-locals`, `goose-bt`,
  `goose-globals`, `goose-as` and a frame filter for Goose names in `bt`,
  and its tests.
- `natvis/`: Windows: `goose.natvis` for the generic names,
  `gen_natvis.py` for a program's own types (from its debug map and C),
  and `goose.js`, a WinDbg/cdb script that decodes what natvis cannot
  (`!goose_locals`, `!goose_globals`). With patch 0009, `goose build -g`
  with MSVC embeds the program's natvis in its PDB itself (`/NATVIS`).
- `lldb/`: `goose_lldb.py`, LLDB summaries and synthetic children for Goose
  values, and its tests.
- Neovim: [goose-nvim-dap](https://github.com/SorinS/goose-nvim-dap), an
  nvim-dap plugin that runs `dap/goose-dap` for the `goose` filetype.
- `tests/`: checks of the patched compiler: `line_directives.sh` through
  LLDB in batch mode, `backtrace.sh` for `goose run -g`.
- `docs/`: the plan.
- `scripts/make_snapshot.sh`: a point-in-time `goose-debug` in `dist/` (not
  committed); see *Snapshots*.

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

## Snapshots

An editor (the IDEA plugin, VS Code, Neovim) is best pointed at a snapshot
(for VS Code and VSCodium, see [docs/vscode.md](docs/vscode.md)):
the compiler built from `debug1` together with the standard library and
TinyCC files of the same commit, this repo's `goose-dap` built by that
compiler, and the debugger modules, in one directory that can live
anywhere.

```sh
scripts/make_snapshot.sh [goose-worktree]     # default ~/Dev/Goose/goose-dbg1
```

builds the worktree (which must be clean) in its own `build-snapshot`
directory and writes

```
dist/goose-debug-<goose-debugger version>-<goose master commit>-<platform>/
  bin/goose-debug     the compiler
  bin/goose-dap       the DAP adapter, a native program
  bin/tcclib/         TinyCC's headers and libtcc1, for goose run --jit
  stdlib/             the standard library of that commit
  lldb/, gdb/         goose_lldb.py, goose_gdb.py
  docs/vscode.md      setting up VS Code and VSCodium
  VERSION             goose version, the debug1, goose-debugger and master
                      commits, platform
```

goose-dap in a snapshot loads `../lldb/goose_lldb.py` and builds with
`../bin/goose-debug`. The one thing that comes from the system is
`lldb-dap` (Xcode's Command Line Tools on macOS, LLVM's package on Linux
and Windows), found as goose-dap always finds it; goose_lldb.py runs in
lldb's own Python (on Windows, LLVM's lldb wants a Python installed for
that, and without one values show as their C). Building the snapshot
fetches gslib for goose-dap the first time.

goose finds `stdlib/` above its executable and `tcclib/` beside it before
the paths baked in when it was built, so the snapshot needs no `--stdlib`
and no `GOOSE_STDLIB`. In the plugin, set the compiler to
`.../bin/goose-debug`, the debug adapter to `.../bin/goose-dap`, and leave
the standard library empty; the language server should use the same
snapshot, so that it reads the same std as the debug build. A `--stdlib` pointing at a goose checkout breaks as soon as
that checkout moves past the compiler (a std using syntax the compiler does
not know). The script checks the result before printing its path: a JIT
run, a `-g` build, the `#line` directives of that build naming the
snapshot's own `std.goose`, and, where there is an `lldb-dap`, goose-dap's
tests (`dap/test/run.py`) through the snapshot's own adapter and compiler,
and packs `dist/<name>.tar.gz`. On Linux, build with clang and libc++
linked in so the binary needs only glibc:

```sh
CC=clang-17 CXX=clang++-17 CXXFLAGS=-stdlib=libc++ \
LDFLAGS="-pthread -stdlib=libc++ -static-libstdc++" scripts/make_snapshot.sh
```

Windows (MSVC) is staged by hand for now, in the same layout but with
`bin\goose-debug.exe`, `bin\goose-dap.exe` (`goose-debug build` of
`dap\src\main.goose`) and `natvis\` in place of `gdb/`, zipped with
`tar -a` (PowerShell 5's `Compress-Archive` writes `\` into the zip's
paths).

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
  goose-dap passes its tests (18/18) with `lldb-vscode-16` (LLVM 16.0.6)
  and with `lldb-dap` 18.1.8 (Ubuntu's `lldb-18`); it works around the
  older adapters' protocol differences (dap/README.md, "Adapter
  versions"), but LLVM 18 or later is the one to use.
- **Windows** (Server 2022, MSVC 19.44; cdb 10.0.26100, LLDB 23.1 on PDB):
  patches 0001-0004 work. `goose build --debug` writes a PDB beside the
  exe; cdb and LLDB bind breakpoints to Goose lines (one location per
  line, each time round a loop), step by Goose line and show `.goose`
  frames, `area_g` keeping its own frame. The compiler suite passes, and
  714 test programs built with `--debug` print what `goose run --debug`
  prints. cdb needs `sxd av`: the runtime commits its stacks from a
  vectored exception handler, which cdb otherwise stops on. 0004's
  backtraces are off on Windows by design. With 0005-0007, natvis and
  `goose.js` pass their cdb tests (14/14). LLDB 23 on PDB runs
  `goose_lldb.py` and `goose-locals` decodes as on macOS, but PDB's
  canonical type names (`unsigned char *`, `long long`) are handled: the
  LLDB module tests pass 16/16 there with LLDB 23, `goose-catch` 7/7, and
  goose-dap 18/18 with lldb-dap 23. With 0009 the natvis is embedded and
  cdb shows the same values with no `.nvload`.

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
   proxy is `dap/` (18 DAP tests, on macOS, Linux and Windows);
   `docs/idea_integration.md` lists what an IDE needs from it.
4. Backtraces in `goose run`: done as draft patch 0004 (also on
   `debug1`). Tested on macOS arm64 only; `tests/backtrace.sh` should be
   run on Linux.
