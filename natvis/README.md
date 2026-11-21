# natvis: Goose values in Visual Studio, VS Code (cppvsdbg) and WinDbg/cdb

The Windows counterpart of [`../lldb/goose_lldb.py`](../lldb/README.md):
type visualizers that show the C that Goose generates as Goose values, in
the debuggers that read PDBs.

| File | What it is |
|---|---|
| `goose.natvis` | What every program has under the same C names: `gs_rhdr`, `gs_rref`, and the slices of the scalars (`sl_u8` as text, `sl_i64`, ...). Written by `gen_natvis.py --generic`; do not edit by hand. |
| `gen_natvis.py` | Writes one program's entries from the debug map that `goose -g` writes beside the C (`<stem>.debug.json`, patches 0005-0007) and from that C: its structs under their Goose field names, its enums with their variant names, the variants' payload types, its fixed and limited arrays, and its globals. |
| `goose.js` | For WinDbg and cdb only: a script that reads the map and decodes what natvis cannot express (below). |
| `test/` | `run.py`, which builds `../lldb/test/*.goose` on Windows and checks cdb's `dx` against `*.expected`. |

Natvis is static XML matched on C type names. Two things follow, checked
with cdb 10.0.26100 and MSVC 19.44:

- **No prefix wildcards.** `*` matches only a C++ template's arguments:
  `<Type Name="sl_*">` loads and never matches, `<Type Name="l*_*">` is
  rejected ("Failed to register NatVis for type"). So `sl_T`, `aN_T` and
  `lN_T` need one entry per name, and only the scalar slices can be in a
  generic file; the generator writes the rest from the program's C, whose
  typedefs name them. The PDB keeps a typedef'd anonymous struct under its
  typedef name (`sl_u8`, `a4_i32`), so these do match.
- **No debug map at run time.** Tag names, variant names and Goose field
  names come from the map, so the generator writes them into the natvis.

## Generating a program's natvis

```
goose build -g -o prog.exe prog.goose
python gen_natvis.py %GOOSE_HOME%\build\prog\prog-<hash>\prog.debug.json > prog.natvis
```

The map and `prog.c` are written beside each other in the program's build
directory (`goose -g -o dir\prog.c prog.goose` puts them where you like).
`gen_natvis.py` leaves out the names `goose.natvis` defines, so load both;
`--all` writes one self-contained file instead (what the linker's
`/NATVIS:` wants, below). Notes about what it skipped (a type with no C
type) go to stderr; `-q` silences them.

## Loading it

**WinDbg / cdb**: `.nvload` the program's file first (when two files
define one name, the first loaded wins), then the generic one, then the
script:

```
.nvload C:\path\to\prog.natvis
.nvload C:\path\to\goose-debugger\natvis\goose.natvis
.scriptload C:\path\to\goose-debugger\natvis\goose.js
```

`.nvlist` lists what is loaded. Natvis changes `dx` (and WinDbg's Locals
and Watch windows); `dv` and `dt` stay raw. The runtime commits its stacks
from a vectored exception handler, so start with `sxd av` or cdb stops on
each first-chance access violation.

**Visual Studio**: put both `.natvis` files in
`%USERPROFILE%\Documents\Visual Studio 2022\Visualizers\`, or add them to
the project. They are re-read when they change while debugging.

**VS Code with the C/C++ extension's `cppvsdbg`**: in `launch.json`,
`"visualizerFile": ["${workspaceFolder}/prog.natvis", "C:/path/to/goose.natvis"]`
(older versions take one file: use `gen_natvis.py --all`). The `cppvsdbg`
debugger is licensed for use with Microsoft products only (Visual Studio,
VS Code); it is not available to other editors. LLDB's `lldb-dap` and
CodeLLDB use `goose_lldb.py` instead (see "LLDB on Windows" below).

**Embedded in the PDB**: `link /NATVIS:prog.natvis` (from `cl`: `... /link
/NATVIS:prog.natvis`, last on the line) stores the file in the PDB, and
Visual Studio and cdb load it from there with nothing to configure: `.nvlist`
then shows `prog.exe (embedded NatVis "...\prog.natvis")`. Checked with cdb
on a goose `-g` build relinked by hand. `goose build -g` does not do it yet;
the proposal is a compiler change that writes `<stem>.natvis` beside the map
and passes `/NATVIS:` to the MSVC link.

## What it shows

`dx -r1` at a stop in `../lldb/test/structs.goose`, without natvis:

```
p_g              [Type: Point_g]
    [+0x000] x_g              : 3 [Type: int]
    [+0x004] y_g              : -4 [Type: int]
u_g              [Type: Unit_g]
    [+0x000] kind_g           : 0x1 [Type: unsigned char]
    [+0x001] hp_g             : 99 [Type: int]
    [+0x005] pos_g            [Type: Point_g]
    [+0x00d] tag_g            [Type: l8_u8]
dot_g            [Type: Shape_g]
    [+0x000] tag              : 0x2 [Type: unsigned char]
    [+0x001] u                [Type: <unnamed-tag>]
shapes_g         [Type: gs_rhdr]
    [+0x000] base             : 0x... : 0x0 [Type: unsigned char *]
    [+0x008] len              : 2 [Type: __int64]
```

With `structs.natvis` and `goose.natvis`:

```
p_g              : Point { x: 3, y: -4 } [Type: Point_g]
    x                : 3 [Type: int]
    y                : -4 [Type: int]
u_g              : Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" } [Type: Unit_g]
    kind             : 1 [Type: unsigned char]
    hp               : 99 [Type: int]
    pos              : Point { x: 3, y: -4 } [Type: Point_g]
    tag              : "hero" [Type: l8_u8]
c_g              : Color.Green [Type: Color_g]
circle_g         : Shape.Circle { r: 1.500000 } [Type: Shape_g_Circle_p]
dot_g            : Shape.Dot [Type: Shape_g]
shapes_g         : len=2 [Type: gs_rhdr]
```

and goose.js's `!goose_locals`, from the map:

```
p: Point = Point { x: 3, y: -4 }
u: Unit = Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" }
c: Color = Color.Green
circle: Shape.Circle = Shape.Circle { r: 1.5 }
rect: Shape.Rect = Shape.Rect { w: 2.0, h: 3.0 }
dot: Shape = Shape.Dot
label: Mark.. = Mark.Label { text: "hi" }
shapes: Shape[>..] = [Shape.Circle { r: 1.5 }, Shape.Rect { w: 2.0, h: 3.0 }]
```

Entry by entry (`test/*.expected` hold every case):

| Goose | C | natvis |
|---|---|---|
| `T[:]` | `sl_T` | `[20, 30, 40]` (first 8 inline, then `... (N elements)`); children `[len]` and the elements |
| `u8[:]`, `u8[k]`, `u8[..k]` | `sl_u8`, `ak_u8`, `lk_u8` | `"hello, goose"`; children are the bytes as numbers |
| `T[k]`, `T[..k]` | `ak_T`, `lk_T` | `[1, 2, 3, 4]` |
| struct `S`, `S<T>` | `S_g`, `S_g_T` | `Pair<i64> { a: 1, b: 2 }`; children under the Goose names |
| `bool`, `u8` field | `uint8_t` | `true`, `1` (not `0x1`) |
| `T&` field | `T *` | the target (`Cursor { line: 1, ... }`) |
| `T?` field | `T *` | `null` or the target's summary (`Node { v: 2, next: Node { v: 3, next: null } }`); the target is shown through a `shallow` natvis view whose pointer fields are addresses, to stop a cycle (not checked past one level: no sample has a deeper non-null chain) |
| `T&<u32>?`, `T&<u32 in pool>?` field | `uint32_t` | the target, through the offset (self-relative, or from `gs_globals_main.pool_g.base`) |
| `T[>..]` field | `gs_rhdr` | `items: [7, 9]`, children the elements: the struct says the element type |
| enum, fixed mode | `E_g {tag; union u}` | `Shape.Circle { r: 1.5 }`; children the active variant's fields |
| C-like enum | `E_g {tag}` | `Color.Green` |
| variant value | `E_g_V_p` | `Shape.Circle { r: 1.500000 }` |
| `T[>..]` local, `T[>..]&` | `gs_rhdr`, `gs_rref` | `len=5`, with `[as u8 text]`, `[as i64]`, `[as i32]`, `[as f64]` children that are guesses |
| `T?`, `T&` local | `T *` | the debugger follows a pointer to a struct and shows its summary |
| globals | `gs_globals_t` | `dx gs_globals_main`: `globals { pool: [...], log: "x" }` under the Goose names, resizables decoded |

## What natvis cannot show, and goose.js

Natvis decides by the C type alone, so where one C type stands for many
Goose types it cannot tell them apart:

1. **A resizable local's elements.** Every `T[>..]` and `T[>..<]` local is
   the same `gs_rhdr`; the element type is per variable, not per type. The
   entry shows `len=N` and guess views. (A resizable *field* or *global* is
   decoded: its struct or the globals block names the element type.)
2. **Variable-size values.** `T[]`, `u8[varint]`, `T[..]`, a struct with a
   `varint`, a variable-mode enum (`Mark..`): all a bare `uint8_t *`, so
   `dx` shows a byte.
3. **`bool` and reference locals.** A `bool` local is a `uint8_t` like a
   `u8`; the `r_T`/`o_T` typedefs of `T&` and `T?` locals are gone in the
   PDB (the variable's type is `T *`), so a `T&` local to a scalar shows as
   a pointer.
4. **Function-value instances, scopes.** Nothing about which locals are in
   scope at a line; `dx` and the Locals window list every hoisted C local.
5. **Text with a zero byte.** `{data,[len]s8}` stops at the first NUL:
   `"goose\x00\x01\x02"` shows as `"goose"`. The children still list all
   bytes.

`goose.js` (WinDbg and cdb; Visual Studio has no script host) reads the map
and decodes all of these, as `goose_lldb.py` does:

```
!goose_locals                       the frame's Goose locals in scope: name: type = value
!goose_globals                      the main program's globals
!goose_as "grow_g", "i64[>..]"      a C expression read as a Goose type
!goose_map "C:\...\prog.debug.json" use this map
dx @$scriptContents.goose_as("frozen_g", "i64[]")
```

It finds the map from the C file the PDB names for the program's own C
code (the frames below `main_g` are in `<stem>.c`), and `<stem>.debug.json`
beside it; `!goose_map` names it where that fails (a stop in a thread
whose stack has no such frame). It prints text only: no children to
expand.

```
frozen: i64[] = [7, 8, 9]
word: u8[] = "frozen"
rec: Rec = Rec { id: 300, name: "rec" }
words: u8[][>..] = ["one", "two"]
second: PNode& = PNode { v: 2, next: PNode { v: 1, next: null } }
pool: Tree[>..] = [Tree { key: 1, left: null }, Tree { key: 2, left: Tree { key: 1, left: null } }]
```

## Tests

On Windows, with Python 3, a goose with patches 0001-0007 and the Windows
Kits cdb:

```
set GOOSE=C:\path\to\goose.exe
python test\run.py              check every sample
python test\run.py --update     rewrite the .expected files
python test\run.py structs      one sample
```

For each `../lldb/test/*.goose`: `goose build -g`, `goose -g -o` for the C
and map, `gen_natvis.py`, then cdb with a breakpoint on each `// BREAK`
line (`when COND` as `bp /w`), recording at the first stop at each
function and line `dx -r1` of every Goose local in scope, `dx -r1
gs_globals_main` where there are globals, `!goose_locals` and
`!goose_globals`. A line in a generic function has one location per
instance, which cdb calls ambiguous, so a first cdb run lists the
locations and the second breaks at each. `<sample>.expected` is with
natvis and goose.js, `<sample>.raw.expected` the same stops without
(the "before"). Addresses are masked. Checked on Windows Server 2022 with
MSVC 19.44 and cdb 10.0.26100: 7 samples, 14 records.

## LLDB on Windows

LLDB 23 reads the PDB, and `goose_lldb.py` loads in it (`command script
import`, with Python 3.12 on PATH; `lldb -o "script import sys;
print(sys.version)"` checks). What works on a `-g` build: the map is found
(the PDB's compile unit is the `.c` in the build directory), `goose-locals`
decodes everything as on macOS, `goose-bt` and `goose-frame-format` show
Goose frames, and the summaries of slices, arrays, structs, enums and
variants are as on macOS. What differs comes from PDB type names: a
variable's type is the canonical one (`unsigned char *` for `uint8_t *`,
`long long` for `gs_i64`, `Node_g *` for `o_Node_g`), so the module's
checks for `uint8_t *`, `r_`/`o_` do not fire (variable-size locals show as
C strings in `frame variable`), and the children it makes typed by name
(`int64_t`, `uint8_t`) are not found (`None` in `test-children`), nor are
`goose-as`'s scalar types. `../lldb/test/run.py` also names the binary
without `.exe`.
