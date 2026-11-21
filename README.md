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
- `lldb/`: `goose_lldb.py`, LLDB summaries and synthetic children for Goose
  values, and its tests.
- `tests/`: checks of the patched compiler through LLDB in batch mode.
- `docs/`: the plan.

## Trying the patches

```sh
git clone ~/Dev/Goose/goose.git goose-dbg && cd goose-dbg
git checkout 9412b80f
git am ../goose-debugger.git/patches/*.patch
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j8
GOOSE=$PWD/build/goose ../goose-debugger.git/tests/line_directives.sh
```

Then, for any program:

```sh
goose build --debug -o prog prog.goose
lldb -o 'b prog.goose:12' -o run ./prog
```

## Status

1. `#line` under `--debug`: done as draft patches 0001 and 0002.
2. LLDB module: done (`lldb/`). Slices, arrays, text, structs, enums and
   variants, references and optionals show as Goose values; resizables
   show their length only. `lldb/README.md` lists what DWARF cannot tell.
3. Debug map and DAP adapter: not started; `lldb/README.md`'s "What DWARF
   cannot tell" is its specification.
4. Backtraces in `goose run`: not started.
