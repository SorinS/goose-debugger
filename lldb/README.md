# goose_lldb: LLDB formatters for Goose programs

Step 2 of [the debugger plan](../docs/goose_debugger.md): a Python module
that shows the C that Goose generates as Goose values, in LLDB and in
editors that drive LLDB. It needs no compiler change. It is most useful
with a compiler that emits `#line` under `--debug` (step 1), so that
breakpoints and stops are on `.goose` lines.

It also records, below, exactly what cannot be shown from DWARF. That list
is the specification for step 3's debug map.

## Loading it

In an LLDB session:

```
(lldb) command script import /path/to/goose-debugger.git/lldb/goose_lldb.py
```

For every session, add that line to `~/.lldbinit`.

In VS Code, with either the CodeLLDB extension or `lldb-dap` (the LLVM
"LLDB DAP" extension), put it in the launch configuration:

```json
{
    "type": "lldb",                     // "lldb-dap" for the LLDB DAP extension
    "request": "launch",
    "name": "Debug Goose program",
    "program": "${workspaceFolder}/prog",
    "initCommands": [
        "command script import /path/to/goose-debugger.git/lldb/goose_lldb.py"
    ]
}
```

Build the program with `goose build --debug -o prog prog.goose`. The
formatters are in the type category `goose`; `type category disable goose`
turns them off.

## What it shows

Types are recognized by name and by shape (an LLDB recognizer function, so
the matching is exact rather than a set of overlapping regexes).

| Goose | Generated C | Shown as |
|---|---|---|
| `T[:]`, `const T[:]` | `sl_T { T *data; int64_t len; }` | `[20, 30, 40]`; children `[0]`..`[n-1]` |
| `u8[:]` | `sl_u8` | `"hello, goose"`, bytes outside ASCII as `\xNN` |
| `T[k]` | `ak_T { T e[k]; }` | `[1, 2, 3, 4]`, or text for `u8`; children are the elements |
| `T[..k]` | `lk_T { uintN len; T e[k]; }` | the first `len` elements, or text for `u8` |
| `T[>..]`, `T[>..<]` | `gs_rhdr { uint8_t *base; int64_t len; }` | `len=5, element type not in debug info` |
| `T[>..]&` parameter | `gs_rref { gs_rhdr *hdr; gs_stack *stk; }` | the same, through `hdr` |
| struct `S`, `S<T>` | `struct S_g`, `struct S_g_T` | `Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" }` |
| enum, fixed mode | `struct E_g { uint8_t tag; union { E_g_V_p v_V_g; ... } u; }` | `Shape.Circle { r: 1.5 }`, `Shape.Dot`; children: `tag` and the active payload |
| enum, no payloads | `struct E_g { uint8_t tag; }` | `Color.Green` |
| a variant value | `struct E_g_V_p` | `Shape.Circle { r: 1.5 }`, `Opt<i64>.Some { v: 9 }` |
| `T&` local | `typedef T *r_T` | the target's value; children: the target's |
| `T?` local | `typedef T *o_T` | the target's value, or `NULL` |
| a pointer field (`Node?`, `T&`) | `Node_g *next_g` | the target's summary, or `null` |
| `u8`, `i8`, `bool` | `uint8_t`, `int8_t` | decimal numbers, not characters |
| any `uint8_t *` | | `<untyped bytes>` instead of LLDB's C-string guess |

Names in summaries drop the compiler's suffixes: `x_g` is `x`, `Shape_g` is
`Shape`, and `Pair_g_i64` is `Pair<i64>` (type arguments stay as they are
mangled). Child names stay the C names, so paths such as
`frame variable u_g.pos_g.x_g` and `p view_g.len` keep working; `[i]`
indexes slices and arrays (`frame variable view_g[1]`).

Summaries are capped: 16 elements inline, 512 bytes of text, 400
characters in all, and 256 children. A length that is negative or above
2^40 is shown as `<invalid len=…>` with no children, which is what an
uninitialized local usually looks like.

Two commands:

- `goose-locals` lists the frame's Goose locals and arguments under their
  Goose names, without the compiler's temporaries (`t0`, `t1`, ...) and the
  hidden `gs_sp`.
- `goose-as VAR TYPE` shows what the debug info cannot: the elements of a
  resizable as `TYPE` (`goose-as nums_g u32`, `goose-as shapes_g Shape_g`),
  or a `T[]` held in a `uint8_t *` (`goose-as frozen_g i64[]`). `TYPE` is a
  Goose scalar or a C type name. It reads the header as it is in memory, so
  it shares the staleness described below.

### Before and after

`frame variable` in `test/arrays.goose`, without the module:

```
(a4_i32) fixed_g = (e = (1, 2, 3, 4))
(gs_rhdr) grow_g = (base = "\n", len = 5)
(sl_i64) view_g = {
  data = 0x0000000300000008
  len = 3
}
(sl_u8) bytes_g = (data = "goose", len = 8)
(sl_u8) lit_g = (data = "hello, goosearrays.goose", len = 12)
(l16_u8) name_g = (len = '\x03', e = "ada")
(uint8_t *) frozen_g = 0x0000000380100000 "\U00000003"
```

With it:

```
(a4_i32) fixed_g = [1, 2, 3, 4]
(gs_rhdr) grow_g = len=5, element type not in debug info
(sl_i64) view_g = [20, 30, 40]
(sl_u8) bytes_g = "goose\x00\x01\x02"
(sl_u8) lit_g = "hello, goose"
(l16_u8) name_g = "ada"
(uint8_t *) frozen_g = 0x0000000380100000 <untyped bytes>
```

`test/structs.goose`, without and with:

```
(Color_g) c_g = (tag = '\x01')
(Shape_g) dot_g = {
  tag = '\x02'
  u = {
    v_Circle_g = (r_g = 3.0493301310381583E-314)
    v_Rect_g = (w_g = 3.0493301310381583E-314, h_g = 2.1220015833909042E-314)
  }
}
```

```
(Color_g) c_g = Color.Green
(Shape_g) dot_g = Shape.Dot
```

## Tests

```
GOOSE=/path/to/goose test/run.py            # check
GOOSE=/path/to/goose test/run.py --update   # rewrite the .expected files
GOOSE=/path/to/goose test/run.py arrays     # one sample
```

Each `test/*.goose` is built with `goose build --debug` and run under
`lldb --batch` with a breakpoint on every line marked `// BREAK`. At the
first stop at each function and line the test records `frame variable`
(Goose-named locals only), `goose-locals`, and the extra commands after
`lldb:` on that line (`test-children VAR` lists a value's children as an
editor would get them). `// BREAK when COND` stops only where the C
condition holds. Addresses are masked. The record must equal
`test/<sample>.expected`. `structs` and `generics` run a second time with
`GOOSE_LLDB_CSOURCE=0` (`*.nocsource.expected`), which is what DWARF alone
gives. `GOOSE_LLDB_WORK` keeps the binaries in a directory of your choice.

The samples cover fixed arrays, `u8[:]`, `i64[:]` and `const u8[:]` slices,
string literals, `u8[..16]`, `i64[]`, `u8[>..]`, `u32[>..]`, `i64[>..<]`,
a struct with a resizable tail, generic structs and enums, C-like enums,
fixed-mode and variable-mode enums, variant values, `T&` and `T?`
parameters, optional and self-relative fields, function values (a named
function and a block), slices in struct fields, a resizable passed by
reference and one being grown in a loop, and (in `layouts.goose`) globals,
pool-relative links, `u8[varint]`, `i64[..]`, a struct with a `varint`
field, a fixed array of enums, and `T&` and `bool` fields. Every C shape
quoted in the next section comes from these samples.

Checked with Apple LLDB `lldb-2100.0.17.203` on arm64 macOS. The
`lldb-dap` path uses the same SB API calls (`GetNumChildren`,
`GetChildAtIndex`, `GetSummary`) that `test-children` exercises, but it
has not been run in an editor here.

## What DWARF cannot tell

Observed with the `#line` compiler (goose.git `9412b80f` plus the step-1
patch) and Apple clang 21 at `-O0 -g`, on the samples in `test/` (the file
is named with each entry; the `.expected` files hold the output quoted).
Each entry gives the Goose declaration, the C the compiler emits for it,
and what is missing. Step 3's
map has to supply each missing fact.

### 1. The element type of a resizable

(`resizable.goose`, `stacks.goose`, `structs.goose`, `layouts.goose`)

```goose
var nums: u32[>..] = [];
var text: u8[>..] = [];
var shapes: Shape[>..] = [];
struct Stack { name: u8[..8], depth: i32, items: i64[>..<] }
```

```c
typedef struct { uint8_t *base; int64_t len; } gs_rhdr;
gs_rhdr nums_g;
gs_rhdr text_g;
gs_rhdr shapes_g;
struct Stack_g { l8_u8 name_g; int32_t depth_g; gs_rhdr items_g; };
```

Every grow-only and grow-shrink array, of any element type, is the same
`gs_rhdr`: `len` is an element count, `base` is untyped. The same holds for
a resizable passed by reference:

```c
static void fill_g(gs_rhdr *out_g_hdr, gs_stack *out_g_stk, gs_i64 n_g, int64_t gs_sp) {
    gs_rref out_g; out_g.hdr = out_g_hdr; out_g.stk = out_g_stk;
```

and for a global, which is a field of the globals struct beside its own
data stack:

```c
typedef struct {
    gs_rhdr pool_g;
    gs_stack gs_gstk_pool_g;
    gs_rhdr log_g;
    gs_stack gs_gstk_log_g;
} gs_globals_t;
static gs_globals_t gs_globals_main;
```

An array of variable-size elements (`u8[][>..]`, `Order[>..]`) is also a
`gs_rhdr`; its `len` counts elements, but they are back to back, each with
its own length prefix (`gs_size_vu32_u8` walks a `u8[]` element: a `u32`
count, then the bytes), so even its byte size is not in the header.

**The map needs:** the Goose type of every resizable local, parameter,
struct field and global, and for a sequential array, how to step from one
element to the next.

### 2. The live length of a resizable, while code is growing it

(`stacks.goose`, `layouts.goose`, `arrays.goose`)

The header is not always current. A loop that pushes caches the length and
the data-stack top in temporaries and writes them back after the loop:

```goose
fn fill(out: i64[>..]&, n: i64) {
    for i in n {
        out.push(i * i);
    }
```

```c
    uint8_t *t6 = out_g.stk->top;
    int64_t t7 = out_g.hdr->len;
    for (gs_i64 i_g = 0; i_g < t0; i_g++) {
        t3 = gs_mul_i64((gs_i64)(i_g), i_g, gs_file0, 6);
        *(gs_i64 *)t6 = t3;
        t6 += 8;
        t7++;
    }
    out_g.stk->top = t6;
    out_g.hdr->len = t7;
```

Stopped in that loop after three pushes (`t7 == 3`), `out_g` shows
`len=0`, while `p *(gs_i64(*)[3])out_g.hdr->base` gives `0, 1, 4`: the
elements are written, only the header lags. The same happens to globals
over a whole function: `main_g` begins with
`int64_t t48 = GS_GL->pool_g.len;` (and caches the base in a local named
like a Goose one, `uint8_t *gs_pb_pool_g`), and after two `pool.push`
calls `target variable gs_globals_main` still shows `pool_g = len=0`. For a fresh
literal it is the other way round: `grow_g.len = 5;` is stored before the
five elements.

**The map needs:** for each such caching range, which C local holds the
live length (and the live top) of which Goose variable, over which lines.
The alternative is for `--debug` to write the header back at every push.

### 3. Variable-size values: a bare `uint8_t *`

(`arrays.goose`, `stacks.goose`, `structs.goose`, `layouts.goose`)

Every value of variable class is a pointer into a data stack, typed
`uint8_t *`, with the layout only in the code that reads it:

| Goose | C | Layout at the pointer |
|---|---|---|
| `let frozen: i64[] = [7, 8, 9];` | `uint8_t *frozen_g` | `u32` count, then the elements (`*(gs_u32 *)(frozen_g)`, `(frozen_g) + 4`) |
| `let word: u8[] = "frozen";` | `uint8_t *word_g` | `u32` count, then the bytes |
| `let short: u8[varint] = "hi";` | `uint8_t *short_g` | ULEB128 count (`gs_uleb_write`), then the bytes |
| `var sized: i64[..] = [..8];` | `uint8_t *sized_g` | `u32` capacity, `u32` length at `+4`, the elements at `+8` |
| `let r = Rec { id: 5, tag: "ab" };` with `struct Rec { id: varint, tag: u8[..4] }` | `uint8_t *r_g` | zigzag LEB128 (`gs_zig_write`), then the `l4_u8` at a computed offset |
| `let rec = Rec { id: 300, name: "rec" };` with `struct Rec { id: varint, name: u8[varint] }` | `uint8_t *rec_g` | zigzag LEB128 (300 is `d8 04`), then a ULEB128 count (`03`) and the bytes `rec` |
| `let label: Mark.. = Mark.Label { text: "hi" };` | `uint8_t *label_g` | a 1-byte tag, then that variant's payload, packed |
| `let c: Shape.. = Shape.Circle { r: 2.0 };` | `uint8_t *c_g` | the tag `0`, then the `f64` 2.0: `memory read -s1 -c9 -fu c_g` gives `0 0 0 0 0 0 0 0 64` |

A variable-mode enum has no C struct of its own: `Mark_g` is never
declared. Only payload structs that some code reads by type are
(`Mark_g_Pin_p`); a payload with a variable-size field (`Label { text:
u8[varint] }`) has none.

**The map needs:** the Goose type of each such local, and a layout
description for each variable-class type: length encodings (`u32`,
`varint`, zigzag), capacity fields, tag widths, and the order of fields
whose offsets are computed.

### 4. Enum tag values and variant names

(`structs.goose`, `generics.goose`; compare `*.expected` with
`*.nocsource.expected`)

```goose
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }
enum Color { Red, Green, Blue }
```

```c
enum {
    Shape_g_Circle_g_k = 0,
    Shape_g_Rect_g_k = 1,
    Shape_g_Dot_g_k = 2,
};
struct Shape_g { uint8_t tag; union { Shape_g_Circle_p v_Circle_g; Shape_g_Rect_p v_Rect_g; } u; };
struct Color_g { uint8_t tag; };
```

The constants are in an anonymous enum that no declaration uses as a
type. Clang does emit it into the object file's DWARF (compiling the kept
`structs.c` with `clang -g -O0 -c` gives four `DW_TAG_enumeration_type`s,
`Shape_g_Circle_g_k` among the enumerators), but on macOS `dsymutil`, which
keeps only DIEs something refers to, drops it: the program's `.dSYM` has
none, `SBModule.GetTypes(eTypeClassEnumeration)` finds none, and
`p Shape_g_Rect_g_k` is an undeclared identifier. (On Linux, with no
dsymutil, the enumerators would probably stay; not checked.) From the
DWARF LLDB gets on macOS:

- the union's member names give the names of the variants **with** a
  payload, and map a payload type back to its enum
  (`Shape_g_Circle_p` → `Shape.Circle`, `Opt_g_i64_Some_p` →
  `Opt<i64>.Some`);
- the tag **values** are not known, so the active variant is not known;
- payload-less variants (`Dot`, `None`) have no union member at all;
- a C-like enum (`Color_g`) has no name for any value.

So `dot_g` can only be shown as `Shape #2` and `c_g` as `Color #1`. The
module gets the names by reading the generated C file named in the compile
unit (`~/.goose/build/prog/<prog>-<hash>/<prog>.c`), which works only while
that file exists and matches the binary; `GOOSE_LLDB_CSOURCE=0` turns it
off.

**The map needs:** for each enum type, its Goose name and each variant's
name, tag value and payload type. A cheaper compiler-side fix gets the
names into DWARF: under `--debug`, name the enum and declare one variable
of its type, e.g. `enum Shape_g_tag { ... };
static enum Shape_g_tag gs_dbg_Shape_g __attribute__((used));`. Checked on
a small C file: the named enum with such a variable survives dsymutil (one
`DW_TAG_enumeration_type` with both enumerators), the anonymous one beside
it does not. The tag field itself can stay `uint8_t`; the module would then
match tag values to enumerators by name prefix. Not tried with MSVC/PDB.

### 5. Relative references

(`refs.goose`, `layouts.goose`)

```goose
struct Tree { key: i32, left: Tree&<u32>? }
struct PNode { v: i64, next: PNode&<u32 in pool>? }
```

```c
struct Tree_g  { int32_t key_g; uint32_t left_g; };
struct PNode_g { int64_t v_g;   uint32_t next_g; };
```

A relative reference is a plain unsigned integer, the same C type as a
`u32` field (`uint32_t` for `&<u32>` as observed; the other widths were not
built here). A self-relative one is the offset from the field's own
address to the target, 0 meaning null, wrapping when the target is
earlier: in `refs.goose`, `pool[1].left` points at `pool[0]`, 12 bytes
before the field (8-byte elements, the field at offset 4), and
`goose-as pool_g Tree_g` shows `Tree { key: 2, left: 4294967284 }`, which
is -12 as a `u32`. A pool-relative one is `target - (pool base - 1)`:
`&pool[0]` stores 1 (`p *(PNode_g(*)[2])gs_globals_main.pool_g.base` in
`layouts.goose` shows `PNode { v: 2, next: 1 }`), and the code encodes it
as `(uint8_t *)target - (gs_pb_pool_g - 1)`.

**The map needs:** for each such field: that it is a relative reference,
self- or pool-relative, the target type, whether it is optional, and for
pool-relative ones which global array is the pool.

### 6. References against optionals, and `bool` against `u8`, in fields

(`refs.goose`, `layouts.goose`)

Locals and parameters get typedefs that say which they are
(`typedef gs_i64 *r_i64`, `typedef Node_g *o_Node_g`), but fields do not:

```c
struct Node_g   { int64_t v_g; Node_g * next_g; };   // next: Node?
struct Cursor_g { gs_i64 * line_g; uint8_t flag_g; }; // line: i64&, flag: bool
```

A field that is `T&` (never null) and one that is `T?` are the same
pointer, and a `bool` is a `uint8_t` like a `u8`, so `cur_g` shows as
`Cursor { line: 1, flag: 1 }` (a pointer field to a scalar is shown as its
target's value, since references are transparent in Goose).
`const` is gone too: `u8[:]` and `const u8[:]` are both `sl_u8`.

**The map needs:** the Goose type of every field (which also covers 1, 3
and 5).

### 7. Values on the data stacks

Variable and resizable values live on per-thread data stacks:
`GS(i)` is `&gs_stks[i]`, with `gs_stks` a `static GS_TLS gs_stack *` and
`i` the hidden `gs_sp` argument plus a constant known only to the code
(`uint8_t *t30 = GS(gs_sp + 0)->top;`, `GS(gs_sp + 1)`, ...). LLDB on macOS
does read the TLS (`p gs_stks[gs_sp].top`, `p gs_nstks` work), and every
Goose local that owns such a value holds a direct pointer (`base`, or the
`uint8_t *` of 3), so showing a value does not need the stack index. What
DWARF lacks is the type and the live extent (1, 2, 3). For a debugger that
wants to show a whole data stack, the map would also need each function's
stack offsets.

### 8. Scopes and the moment a local becomes valid

Every C local is declared at the top of its function, so before its `let`
line it shows garbage (with `b sum_g` in `generics.goose`, at the
function's first line: `t_g = 2314885399295790128`). A slice parameter arrives split and is rebuilt in
the body:

```c
static gs_i64 sum_g(void *xs_g_data, int64_t xs_g_len, void *label_g_data, int64_t label_g_len) {
    sl_i64 xs_g; xs_g.data = xs_g_data; xs_g.len = xs_g_len;
```

so at the function's first stop `xs_g` is garbage and the arguments are a
`void *` and a length, with the element type lost.

**The map needs:** for each local, the line range in which it holds its
value; and for split parameters, which C arguments make up the Goose
parameter.

### 9. Names

- Locals and fields are `x_g`, shadowing ones `it_g_2`; temporaries are
  `t0`, `t1`, ...; a function that uses data stacks takes a hidden `gs_sp`
  argument.
- Function instances are numbered with no meaning in Goose: `bump_g_1` and
  `bump_g_2` are two instances of `bump`; `twice_g_3` is `twice` with `inc`
  and `twice_g_5` is `twice` with a block whose body is compiled into it
  (its `it` is `it_g`). A function value has no runtime object at all.
- Generic types are mangled into the C name, `Pair_g_i64`,
  `Opt_g_i64_Some_p`; the module turns those into `Pair<i64>`, but cannot
  undo a mangling with several or compound type arguments reliably.
- Frames show the C names (`main_g`, `bump_g_1`): the module renames
  locals (in `goose-locals`) but not frames.

**The map needs:** for each C function, the Goose name and instance (type
arguments, function-value arguments); for each C local, its Goose name, or
that it is a temporary.

### 10. Globals

Globals are fields of `gs_globals_t`: main's instance is the static
`gs_globals_main`, and a worker's is a copy reached through the TLS pointer
`gs_gl`. `frame variable` does not list them; `target variable
gs_globals_main` does, with these formatters applied to the fields.

**The map needs:** the globals' Goose names and types, and, for workers,
that they are reached through `gs_gl`.

## Notes for step 1

A Goose line can stop several times: a breakpoint on line 24 of
`test/structs.goose` (one `print` of several values) resolves to 6
locations, because the line table has 6 separate address ranges for it,
and the program stops at 5 of them. The test records only the first stop
at each function and line.
