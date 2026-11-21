# goose_gdb: GDB pretty-printers for Goose programs

The gdb counterpart of [`lldb/goose_lldb.py`](../lldb/README.md): a Python
script that shows the C that Goose generates as Goose values in gdb, adds
the commands `goose-locals`, `goose-bt`, `goose-globals` and `goose-as`,
and installs a frame filter so that `bt` shows Goose function names and
parameters. It ports the LLDB module's logic (C type recognition, the
debug map decoder, scopes and names), so the two show the same text for the
same value.

Build programs with `goose build -g`: that gives `#line` directives (stops
and steps on `.goose` lines) and the debug map `<stem>.debug.json` beside
the generated C, from which most of what is shown comes.

## Loading it

```
(gdb) source /path/to/goose-debugger.git/gdb/goose_gdb.py
```

For every session, put that line in `~/.gdbinit`. Sourcing it again (after
editing it, say) replaces the printers, filter and commands rather than
adding a second set.

gdb's auto-loading needs the script next to the program as
`<program>-gdb.py` (and the directory in `add-auto-load-safe-path`), so for
one program:

```sh
ln -s /path/to/goose-debugger.git/gdb/goose_gdb.py ./prog-gdb.py
gdb -iex 'add-auto-load-safe-path .' ./prog
```

In VS Code with the C/C++ extension (cpptools, `"MIMode": "gdb"`), the
printers need MI's pretty-printing turned on:

```json
{
    "type": "cppdbg",
    "request": "launch",
    "name": "Debug Goose program",
    "program": "${workspaceFolder}/prog",
    "MIMode": "gdb",
    "setupCommands": [
        { "text": "-enable-pretty-printing" },
        { "text": "-enable-frame-filters" },
        { "text": "source /path/to/goose-debugger.git/gdb/goose_gdb.py" }
    ]
}
```

`disable pretty-printer global goose` and `disable frame-filter global
goose` turn the parts off.

## gdb versions

- Written for, and tested with, gdb 9.2 (Ubuntu 20.04, Python 3.8). It
  uses only Python API that 9.2 has.
- **gdb older than 10 cannot read clang's DWARF 5**, which clang 14 and
  later emit by default: gdb 9.2 says `Dwarf Error: DW_FORM_strx1 found in
  non-DWO CU` and loads no symbols. Use gcc (gcc 9.4 emits DWARF 4; it is
  what `goose build` picks first on Ubuntu 20.04), or LLDB, or make clang
  emit DWARF 4. `goose build` has no C flags option, but `--cc` takes a
  path, so a wrapper named `clang` does it:

  ```sh
  mkdir -p ~/cc4 && printf '#!/bin/sh\nexec clang-17 "$@" -gdwarf-4\n' > ~/cc4/clang
  chmod +x ~/cc4/clang
  goose build -g --cc ~/cc4/clang -o prog prog.goose
  ```

  Checked: gdb 9.2 then shows the same values as for a gcc build.

## What it shows

Values print as one line of Goose, the same text the LLDB module gives.
`info locals` in `lldb/test/structs.goose`, without the script:

```
p_g = {x_g = 3, y_g = -4}
u_g = {kind_g = 1 '\001', hp_g = 99, pos_g = {x_g = 3, y_g = -4}, tag_g = {len = 4 '\004', e = "hero\000\000\000"}}
c_g = {tag = 1 '\001'}
circle_g = {r_g = 1.5}
rect_g = {w_g = 2, h_g = 3}
dot_g = {tag = 2 '\002', u = {v_Circle_g = {r_g = 7.5430319131968664e-315}, v_Rect_g = {w_g = 7.5430319131968664e-315, h_g = -2.7160275508461652e-312}}}
label_g = 0x7fff77b4d000 ""
shapes_g = {base = 0x7ffef7a4d000 "", len = 2}
t7 = {tag = 0 '\000', u = {v_Circle_g = {r_g = 1.5}, v_Rect_g = {w_g = 1.5, h_g = 0}}}
...                                  (forty more temporaries)
```

With it:

```
p_g = Point { x: 3, y: -4 }
u_g = Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" }
c_g = Color.Green
circle_g = Shape.Circle { r: 1.5 }
rect_g = Shape.Rect { w: 2.0, h: 3.0 }
dot_g = Shape.Dot
label_g = 0x7fff77b4d000 Mark.Label { text: "hi" }
shapes_g = [Shape.Circle { r: 1.5 }, Shape.Rect { w: 2.0, h: 3.0 }]
```

and `goose-locals`, which lists only the Goose locals in scope, under their
Goose names and types:

```
p: Point = Point { x: 3, y: -4 }
u: Unit = Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" }
c: Color = Color.Green
circle: Shape.Circle = Shape.Circle { r: 1.5 }
...
label: Mark.. = Mark.Label { text: "hi" }
shapes: Shape[>..] = [Shape.Circle { r: 1.5 }, Shape.Rect { w: 2.0, h: 3.0 }]
```

### With the debug map

Everything in the LLDB README's table
([With the debug map](../lldb/README.md#with-the-debug-map)) holds here:
resizables show their elements (`u32[>..]`, `u8[>..]` as text, `u8[][>..]`
walked element by element, `T[>..]&` parameters, a struct's `T[>..<]`
field), variable-size values behind a `uint8_t *` are decoded (`i64[]`,
`u8[varint]`, `i64[..]`, structs with `varint` fields, variable-mode
enums), enum tags are named, `bool` fields and locals print `true`/`false`,
`T&` and `T?` fields are told apart, relative references (`Tree&<u32>?`,
`PNode&<u32 in pool>?`) are followed, two deep in one summary.

How gdb finds a value's Goose type: gdb hands a printer a value with no
name, so the script indexes, once per stop, the address and C type of each
local the map names for the selected frame, and of their fields, walked
through the map, and of the map's globals. `print s_g.items_g` (a
`gs_rhdr`) shows `[7, 9]`, `print cur_g.flag_g` shows `true`, `print
gs_globals_main.pool_g` shows the pool's elements, and after `frame 1`,
that frame's locals decode. Values of a C type the map names (`Point_g`,
`Shape_g`) decode wherever they are, `print *some_g` included.

Under patch 0007 a resizable's header is live in every frame, so a caller
sees what a callee has pushed so far: stopped in `fill`'s loop in
`stacks.goose`, `bt` shows `#0 fill (out=[0, 1], n=4)` and `frame 1` then
`print squares_g` gives `[0, 1]`.

Pointers (`T&`/`T?` locals, `uint8_t *` variable-size values) print as gdb
prints a `char *`: the address, then the value (`r_g = 0x7ffe… 6`); null
prints `null`.

`GOOSE_GDB_MAP=0` ignores the map. A map older than its `.c` is ignored.

### Without the map

As in the LLDB module: slices, fixed and limited arrays, text, structs,
generic structs (`Pair<i64>`), fixed-mode and C-like enums, variant values,
`T&`/`T?` locals and pointer fields; resizables show
`len=N, element type not in debug info` and variable-size values
`<untyped bytes>`; `goose-as VAR TYPE` shows a resizable's or a `T[]`'s
elements as a type you name.

Tag names (`Color.Green`, `Shape.Dot`) need no map on Linux: unlike macOS's
dsymutil, the Linux toolchains keep the anonymous enum of tag constants in
the DWARF (checked with gcc 9.4, and clang 17 at `-gdwarf-4`), and the
script reads its enumerators from the program's static block. The
generated C file is read as well, for what DWARF lacks;
`GOOSE_GDB_CSOURCE=0` turns that off. On the samples the result is the same
either way.

## Commands

- `goose-locals`: with the map, `name: type = value` for the Goose locals
  in scope at the frame's line, parameters first; without it, the frame's
  Goose-named arguments and locals as `name = value`.
- `goose-bt [N]`: frames under their Goose names, with a generic instance's
  type arguments and its function-value arguments (`twice<F=inc>`,
  `twice<F={fn line 23}>`), from the map's `bindings` and `fnvals`:

  ```
  * #0: refs`bump at refs.goose:11
    #1: refs`main at refs.goose:23
    #2: refs`main at refs.c:2029
  ```

- `goose-globals`: the map's globals, `name: type = value`, from
  `gs_globals_main`, or for a thread of a program that starts threads its
  own block through `gs_gl`.
- `goose-as VAR TYPE`: as in the LLDB module.

### `bt`

The frame filter renames frames and lists the Goose parameters (split slice
parameters and `out_g_hdr`/`out_g_stk` once, as the Goose parameter;
`gs_sp` left out), decoded by their Goose types:

```
#0  bump (r=0x7ffe… 6, o=0x7ffe… Node { v: 2, next: Node { v: 3, next: null } }) at refs.goose:11
#1  0x… in main () at refs.goose:23
#2  0x… in main (argc=1, argv=0x…) at refs.c:2029
```

Frame filters apply to `bt` and MI's `-stack-list-frames` (after
`-enable-frame-filters`), not to the line gdb prints when it stops
(`Breakpoint 1, bump_g_1 (r_g=…, o_g=…) at refs.goose:11`) nor to `frame`;
those keep the C names. gdb has no hook for them.

### Children, and MI front ends

gdb prints a printer's children after its summary, and has no way to hide
them on the command line while giving them to a front end. So by default the
printers give one line and no children. `set goose-children on` adds them:
a resizable's or array's elements, a struct's fields under their Goose
names, the active variant's fields, a reference's target. On the command
line that repeats each value (`[…] = {[0] = …, [1] = …}`).

In MI (VS Code), gdb shows any value whose printer has children as `{...}`
and lets you expand it (gdb's varobj code does this, not the script). So:

| `goose-children` | Variables pane |
|---|---|
| off (default) | the Goose summary, not expandable |
| on | `{...}`, expandable into Goose-named children |

Put `{ "text": "set goose-children on" }` in `setupCommands` for the second.

## What gdb cannot do here

- **Indexing a slice or array**: `print view_g[1]` is an error
  (`Structure has no component named operator[]`). gdb's xmethods, which
  would allow it, are for C++ only. Use `print view_g.data[1]`,
  `print fixed_g.e[2]`, or the children.
- **The stop line and `frame`** show C names (above).
- **Function entry**: as with LLDB, a parameter Goose copies or rebuilds in
  the body is garbage at the function's first line (`twice_g_3
  (x_g=93824992493728)` at the breakpoint on `fn twice`).
- Everything the LLDB README lists under
  [What DWARF cannot tell](../lldb/README.md#what-dwarf-cannot-tell) and the
  map does not cover applies here too.

## Tests

```
GOOSE=/path/to/goose test/run.py            # check
GOOSE=/path/to/goose test/run.py --update   # rewrite the .expected files
GOOSE=/path/to/goose test/run.py arrays     # one sample
```

The samples are the LLDB tests' (`lldb/test/*.goose`). Each is built with
`goose build -g` and run under `gdb -batch` with a breakpoint on each
`// BREAK` line (`when COND` becomes `if COND`; `maponly` stops are left
out of the runs without the map). `test/stop.py` drives the run from
Python: at the first stop at each function and line it records `info args`
and `info locals` (Goose-named variables only), `goose-locals`, and the
line's `lldb:` commands translated to gdb (`frame variable X`, `target
variable X` and `p X` become `print X`, `memory read -s1 -cN -fu X` becomes
`x/Nub X`, `test-children X` becomes `goose-test-children X`, which lists
children as a front end gets them; `goose-frame-format` is dropped). The
program's output follows, then its exit status. Addresses, C line numbers
and directories are masked.

Each sample is recorded with the map (`<sample>.expected`), with
`GOOSE_GDB_MAP=0` (`.nomap`), and for `structs` and `generics` also with
`GOOSE_GDB_CSOURCE=0` (`.nocsource`). `GDB` names the gdb to use,
`GOOSE_GDB_WORK` keeps the binaries.

Checked with gdb 9.2 on Ubuntu 20.04 x86_64, gcc 9.4, the goose-debug
compiler at patch 0007: all 16 records pass.
