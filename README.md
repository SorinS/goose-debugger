# goose-debugger

Debugging Goose programs with the debuggers that already exist (LLDB, and
through it any editor that speaks DAP), rather than a debugger inside the
compiler. The reasoning and the plan are in
[docs/goose_debugger.md](docs/goose_debugger.md).

Nothing here changes goose.git. What the compiler needs is kept as draft
patches for its author, against the goose.git commit each one names.

## Layout

- `patches/`: draft compiler patches, made with `git format-patch`.
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
- `lldb/`: `goose_lldb.py`, LLDB summaries and synthetic children for Goose
  values, and its tests.
- `tests/`: checks of the patched compiler: `line_directives.sh` through
  LLDB in batch mode, `backtrace.sh` for `goose run --debug`.
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
goose build --debug -o prog prog.goose
lldb -o 'b prog.goose:12' -o run ./prog
```

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
   Not yet: globals, live lengths while a resizable grows, function-value
   instances. The `goose dap` proxy is not started;
   `docs/idea_integration.md` lists what it must do.
4. Backtraces in `goose run`: done as draft patch 0004 (also on
   `debug1`). Tested on macOS arm64 only; `tests/backtrace.sh` should be
   run on Linux.
