# goose-dap: Goose-level debugging over DAP

`goose-dap` is a Debug Adapter Protocol proxy. It sits between an editor and
LLDB's `lldb-dap` and turns lldb-dap's view of the generated C into a Goose
view: Goose function names in the call stack, Goose locals in scope, Goose
expressions in watches, and a stop on Goose runtime errors. This is step 3 of
[the debugger plan](../docs/goose_debugger.md). It meets the list in
[docs/idea_integration.md](../docs/idea_integration.md) ("What `goose dap`
must do for the IDE"), except where **Limits** below says otherwise.

```
editor  <--DAP over stdio-->  goose-dap  <--DAP over pipes-->  lldb-dap  -->  program
```

It needs Python 3 (standard library only), an `lldb-dap` (on macOS the
Command Line Tools have one), and a program built with `goose build -g`
(patches 0001-0007). That build writes `<stem>.debug.json` beside the
generated C, and goose-dap reads it.

The oldest adapter it is tested with is `lldb-vscode` 16 (LLVM 16; LLVM
18 renamed it `lldb-dap`). On Linux, LLVM 18 or later is the one to use:
Ubuntu 20.04's focal-updates has `lldb-18` (`/usr/bin/lldb-dap-18`), and
apt.llvm.org and the LLVM releases have newer ones. See
[Adapter versions](#adapter-versions) for what differs.

## Files

- `goose-dap`: the command an editor runs. A shell script that runs
  `goose_dap.py` with `python3 -u` and resolves symlinks, so it can be linked
  into `PATH`.
- `goose_dap.py`: the proxy.
- `goose_dap_lldb.py`: a few functions that run inside lldb-dap (where the
  maps are, the line table of a file, the message of a runtime error).
  goose-dap loads this module, and `../lldb/goose_lldb.py`, into lldb-dap
  through `initCommands`. You do not load either one yourself.
- `test/`: the tests (`run.py`), their DAP client (`dapclient.py`) and two
  samples (`progs/`).

## Running it

goose-dap speaks DAP on stdin and stdout, with `Content-Length` framing. It
assumes no TTY and writes nothing else to stdout. Options:

```
goose-dap [--lldb-dap PATH] [--log FILE]
```

- **lldb-dap.** It uses the first of: the launch configuration's `lldbDap`,
  `--lldb-dap`, `$GOOSE_DAP_LLDB_DAP`, `$LLDB_DAP`, `xcrun -f lldb-dap`,
  `/Library/Developer/CommandLineTools/usr/bin/lldb-dap`, then on `PATH`
  `lldb-dap`, `lldb-dap-NN` (newest first), `lldb-vscode`, `lldb-vscode-NN`,
  and last `/usr/lib/llvm-NN/bin/lldb-dap` or `lldb-vscode` (Debian and
  Ubuntu's LLVM packages).
- **Logging.** `--log FILE` or `GOOSE_DAP_LOG=FILE` logs every message in
  both directions, and lldb-dap's stderr.

### Launch configuration

goose-dap accepts what lldb-dap accepts (`program`, `args`, `cwd`, `env`,
`stopOnEntry`, `initCommands`, `commandEscapePrefix`, …) and passes it on.
It adds these keys:

| Key | Meaning |
|---|---|
| `build` | `true`: run `goose build -g -o <program> <source>` before launching. Compiler output goes to the debug console; if the build fails, the launch fails with the compiler's last line. |
| `source` | The `.goose` file to build. If `program` is not given, it defaults to the source's stem, in `cwd` or else beside the source. |
| `goose` | The compiler (default `$GOOSE`, then `../bin/goose-debug` beside goose-dap, as in a snapshot, then `goose` on `PATH`). |
| `buildArgs` | Extra compiler arguments, as a list (`["--include", "x.h"]`). |
| `lldbDap` | The lldb-dap to run, for this session only. |
| `gooseLldb` | Another `goose_lldb.py` to load (default `../lldb/goose_lldb.py`, or `$GOOSE_DAP_GOOSE_LLDB`). |
| `stopOnRuntimeError` | Default `true`. Used only when the client sends no `setExceptionBreakpoints` (see below). |
| `showRuntimeFrames` | Default `false`. `true` keeps `gs_*` frames in the stack, marked subtle. |

goose-dap puts its own `initCommands` before yours: it imports
`goose_lldb.py` and `goose_dap_lldb.py`, and sets
`target.process.thread.step-avoid-regexp` to `^(std::|gs_)`, so that step in
goes over the runtime's helpers (checked arithmetic, pushes) and does not
enter `goose_runtime.c`.

Debugging always uses a built executable. A JIT run (`goose run`) cannot be
debugged.

## In VS Code

**With the LLDB DAP extension** (`llvm-vs-code-extensions.lldb-dap`): its
launch configurations take `debugAdapterExecutable`, which replaces the
lldb-dap that the extension would otherwise start. Point it at goose-dap:

```jsonc
{
    "type": "lldb-dap",
    "request": "launch",
    "name": "Debug Goose program",
    "debugAdapterExecutable": "/path/to/goose-debugger.git/dap/goose-dap",
    "program": "${workspaceFolder}/${fileBasenameNoExtension}",
    "source": "${file}",
    "build": true,
    "goose": "/path/to/goose",
    "cwd": "${workspaceFolder}"
}
```

Older versions of the extension use the setting `lldb-dap.executable-path`
instead. That setting applies to every lldb-dap session, so a per-launch
`debugAdapterExecutable` is better where the extension has it. Breakpoints
in `.goose` files need `"debug.allowBreakpointsEverywhere": true` unless an
extension contributes a `breakpoints` entry for the language.

**With the Goose extension** (goose.git `vscode/`, patch 0008): the
extension's `goose` debug type debugs through goose-dap. Put this
directory's `goose-dap` on PATH or set `goose.debugAdapterPath` to it, and
set `goose.lldbDapPath` if goose-dap's own search for lldb-dap is not
enough. `goose.compilerPath` must be a compiler with `-g`: the extension
checks that `goose help build` lists `--debug-info`, and says so if not.
Start a session with **Goose: Debug** (the editor's debug button or the
Command Palette), or F5 on the "Goose: Debug" configuration. In a `goose`
launch configuration, `program` is the Goose entry file, as for Run with
JIT (default: `goose.entryFile` or the active file); the extension sends
goose-dap `source` (that file), `program` (an executable beside it),
`build: true`, `goose`, `cwd`, `args` (default `goose.runArguments`),
`env`, and `--stdlib` in `buildArgs`; other keys (`stopOnEntry`,
`showRuntimeFrames`, `initCommands`, ...) pass through. `noDebug: true`
(Run Without Debugging) runs the program with the JIT instead. The
extension contributes `breakpoints` for the goose language, so
`debug.allowBreakpointsEverywhere` is not needed. With
`goose.useRunKeybindings` on (the default), F5 in a Goose editor still runs
with the JIT.

## In Neovim (nvim-dap)

```lua
local dap = require('dap')
dap.adapters.goose = {
  type = 'executable',
  command = '/path/to/goose-debugger.git/dap/goose-dap',
  name = 'goose',
}
dap.configurations.goose = {
  {
    type = 'goose',
    request = 'launch',
    name = 'Build and debug this file',
    source = '${file}',
    program = '${fileDirname}/${fileBasenameNoExtension}',
    build = true,
    goose = 'goose',
    cwd = '${workspaceFolder}',
  },
}
```

(`.goose` files need `vim.filetype.add({ extension = { goose = 'goose' } })`
for `dap.configurations.goose` to apply.) The "Goose runtime errors"
exception filter is on by default. `:lua require('dap').set_exception_breakpoints({})`
turns it off.

## In IntelliJ IDEA and the other JetBrains IDEs

The platform DAP client (`intellij.platform.dap`, see
`docs/idea_integration.md`) runs an adapter as a command line over stdio.
The plugin's `DebugAdapterDescriptor.launchDebugAdapter` returns:

```java
new CommandLineDebugAdapterHandle(
    new GeneralCommandLine(gooseDapPath)            // .../dap/goose-dap
        .withEnvironment("GOOSE_DAP_LOG", logFile)) // optional
```

and its `DapLaunchArgumentsProvider` sends `program`, `args`, `cwd`, `env`,
and either `build: true` with `source` and `goose`, or a program the plugin
built itself. The plugin does not add `initCommands` for `goose_lldb.py`:
goose-dap loads it. Notes for that client:

- No TTY, `Content-Length` framing, nothing but DAP on stdout: tested by
  `test/run.py`, which drives goose-dap over plain pipes as the IDE does.
- If the client registers no exception breakpoint type, it never sends
  `setExceptionBreakpoints`. goose-dap then turns the Goose filter on at
  `configurationDone` (`stopOnRuntimeError: false` turns that off). With an
  exception breakpoint type, the filter id is `goose_runtime`.
- `breakpointLocations` is answered, so `canPutAt` can later ask which lines
  take a breakpoint.
- Frames that are not Goose (the C `main`, dyld) are `subtle` and their
  sources `deemphasize`. A presentation factory may hide them.

## What it rewrites

Messages not listed here pass through unchanged. Frame ids and
`variablesReference`s are lldb-dap's own and are never renumbered, so they
stay consistent. Request and response `seq`s are renumbered in both
directions, which lets goose-dap send requests of its own (`stackTrace`,
`variables`, and repl `script` calls into `goose_dap_lldb.py`) while it
handles the client's.

**initialize.** lldb-dap's C++, Objective-C and Swift exception filters are
replaced by one, `goose_runtime` ("Goose runtime errors", on by default).
`supportsCompletionsRequest` and `supportsCancelRequest` are removed: neither
would be translated.

**initialized** (event). goose-dap holds it back until it has asked lldb-dap
where the maps are (`goose_dap_lldb.info()`, with the same rules as
goose_lldb.py: beside each compile unit's `.c`, and not older than it).
Every breakpoint request from the client comes after this point, so the map
is already loaded when they arrive. If LLDB cannot tell, the fallback is the
newest `~/.goose/build/prog/<stem>-*/<stem>.debug.json`.

**stackTrace.** goose-dap gets all of the thread's frames and filters them
once per stop. It then pages the filtered list by the client's
`startFrame`/`levels` and sets `totalFrames` to its length.

```
lldb-dap                                            goose-dap
main_g      tests/area.goose:9                      main       tests/area.goose:9
main        ~/.goose/build/prog/area-…/area.c:1773  main (C)   area.c:1773   subtle, source deemphasize
start       /usr/lib/dyld`start   (deemphasize)     start      …             subtle, source deemphasize
```

- Functions in the map get their Goose name, with generic and function-value
  arguments: `geo_area_g3` → `geo::area`, `pick_g_4` → `pick<T=i64>`,
  `twice_g_3` → `twice<F={fn line 18}>`.
- `gs_*` frames, and frames in `goose_runtime.c`, are dropped.
- Any other frame is `subtle`, and its source `deemphasize`. A generated
  `.c` file is therefore never shown as a primary source.
- If filtering left nothing, the frames are returned unchanged.

**scopes / variables.** For a Goose frame, Locals lists the map's locals in
scope at the frame's line (the map's `line`..`end`), under their Goose
names, in the C's order (parameters first). Of two locals with one name, the
one declared later is shown. Temporaries (`tN`), `gs_sp` and compiler locals
are not listed. `type` is the Goose type, and `evaluateName` is the Goose
path (`u`, `u.pos`, `shapes[1]`). Values are goose_lldb.py's summaries,
without the address that lldb-dap puts in front of a pointer's summary.
`bool` is `true`/`false`. Children get Goose names (`x_g` → `x`) and Goose
paths, typed from the map's fields. `(Return Value)` after a step out is
kept. At `structs.goose:24`:

```
lldb-dap (Locals: 49 entries)                       goose-dap (Locals: 8)
gs_sp = 0                                           p = Point { x: 3, y: -4 }          Point
t0 = Point { x: 3, y: -4 }                          u = Unit { kind: 1, hp: 99, … }    Unit
p_g = Point { x: 3, y: -4 }          Point_g          kind = 1  u8    evaluateName u.kind
u_g = Unit { … }                     Unit_g           pos = Point { … } Point  evaluateName u.pos
  pos = …   evaluateName "pos->pos"                 c = Color.Green                     Color
label_g = 0x0000000300000000 Mark.Label { … }       label = Mark.Label { text: "hi" }   Mark..
t21 = Shape #112                                    shapes = [Shape.Circle { r: 1.5 }, …] Shape[>..]
…                                                     [0] = Shape.Circle { r: 1.5 }  evaluateName shapes[0]
```

At line 14 of the same file only `p` and `u` are listed.

Globals lists the map's `globals.vars` (Goose names and types), with values
from goose_lldb.py's `goose-globals`. Those are decoded by Goose type and
come from the selected thread's instance. Children come from the main
program's globals block. Runtime statics (`gs_stks`, `gs_file0`, `gs_str*`,
…) are not listed. With a map that has no globals, the block's Goose-named
fields are listed. At `layouts.goose:25` it shows
`pool = [PNode { v: 1, next: null }, PNode { v: 2, … }]` (`PNode[>..]`) and
`log = "x"` (`u8[>..]`).

**evaluate** (watch, hover, clipboard, variables). Identifiers and field
paths are rewritten to C; anything else is passed through:

- A root identifier becomes the C name of the frame's local of that name
  (the one in scope, so shadowed names resolve), or the path of a global.
- `.field` becomes the field's C name, from the map's types, with `->`
  through a reference.
- `[i]` stays. After an index, and in a value that is a bare `uint8_t *` in
  C (a variable-size struct or a variable-mode enum), fields keep their Goose
  names. That way LLDB's variable path goes through goose_lldb.py's children.
- If the C form fails, the all-Goose-names form is tried.
- Calls, `::` names, keywords and literals are left alone.

The result's `type` is the Goose type when the expression is a path, and a
repl result's `(Point_g) $2 = …` becomes `(Point) $2 = …`.

```
total        -> total_g                      "0"        i64
u.pos.x      -> u_g.pos_g.x_g                "3"        i32
second.v     -> second_g->v_g                "2"        i64    (second: PNode&)
r.tag        -> r_g->tag                     "\"ab\""   u8[..4] (Rec is variable-size)
pool[1].v    -> gs_globals_main.pool_g[1].v  needs the goose_lldb.py change below
total + i    -> total_g + i_g                "1"
```

In the `repl` context a line is rewritten only if it starts with a Goose
local or global of the frame and is not `word word` (a command and its
argument). So `goose-locals`, `bt` and `` `anything`` stay LLDB commands.

**setVariable.** The Goose name is turned back into the C name of that
scope's variable.

**setFunctionBreakpoints.** A Goose name becomes every C instance of it:
`area` → `area_g`; `twice` → `twice_g_1`, `twice_g_3`; `area` also matches
`geo::area`. A name the map does not know is passed on as it is, so C names
still work. lldb-dap replaces all function breakpoints on each request, so
goose-dap always sends the whole set (the client's, expanded, plus the
runtime-error ones). It returns one breakpoint per client entry: verified
if any instance is, with the message "N instances". `breakpoint` events and
`hitBreakpointIds` for the extra instances are mapped to the client's id.

**breakpointLocations.** goose-dap always offers it. lldb-dap 21 returns an
empty list; lldb-dap 18 and lldb-vscode 16 do not have the request (and exit
on a request they do not know), so goose-dap does not send it to them. Either
way goose-dap answers from LLDB's line table for that file (`goose_dap_lldb.lines`), the
lines a breakpoint binds to: for `area.goose` 1–12, the lines 1, 2, 3, 6, 7,
8, 9 and 11.

**setExceptionBreakpoints / runtime errors.** Every Goose runtime error (a
failed assert, bounds or overflow check, division by zero, `abort(msg)`,
and the runtime's own fatal errors) ends in `gs_fail(msg, len, file, line)`
or `gs_panic(msg)`. The `goose_runtime` filter is a function breakpoint on
both. At a stop there:

```
lldb-dap: stopped {reason: "breakpoint", description: "breakpoint 1.1", hitBreakpointIds: [1]}
          frames gs_fail, gs_failf, gs_idxfail, get_g (disassembly, line 29), main_g, main, start
goose-dap: stopped {reason: "exception",
                    description: "Goose runtime error: index 3 out of bounds (length 3) (bounds.goose:4)",
                    text: "index 3 out of bounds (length 3) (bounds.goose:4)"}
          frames get  bounds.goose:4, main  bounds.goose:11, main (C) (subtle), start (subtle)
```

`exceptionInfo` answers with the same message. The runtime frames are
dropped, so the Goose frame that failed is on top. A failing call to a
noreturn runtime function is the last instruction of its function, so the
frame's return address can fall outside the line table and lldb-dap shows
disassembly. goose-dap then uses the file and line that the runtime was
given. `continue` lets the runtime print the error and exit with status 1.
A contained worker's abort also stops here, before the runtime records it
for `thread_wait`.

**stopped / continued.** Per-stop state (frames, references, runtime-error
messages) is cleared.

## Tests

```
GOOSE=/path/to/goose-debug dap/test/run.py        # all
GOOSE=… dap/test/run.py -k runtime_error -v       # some
GOOSE_DAP_WORK=dir GOOSE=… dap/test/run.py        # keep binaries and logs in dir
```

Each test builds its sample with `goose build -g`, runs goose-dap over pipes
through `test/dapclient.py`, and checks what a client sees:

| Test | Checks |
|---|---|
| `capabilities` | the Goose filter, no completions; a run to exit |
| `line_breakpoint_and_steps` | breakpoint verified at area.goose:9; frames Goose-named, C frames subtle and deemphasized; locals exactly `total, i`; `total`, hover, `total + i + 1`, repl `goose-locals`; step in to `area:2`, next to 3, step out to `main:9`; exit 0 with output `6` |
| `breakpoint_in_loop` | a body line stops each time round, with the right values |
| `function_breakpoint` | `area` binds and stops in `area`, called from `main` |
| `function_instances` | `twice` and `pick` bind every instance; stops show `twice<F=inc>`, `twice<F={fn line 18}>`, `pick<T=i64>`, `pick<T=f64>` |
| `struct_values` | only `p, u` in scope at line 14; the eight locals at 24, typed; children and their evaluateNames; `u.pos.x`, `p.y`, `shapes[1]`, `circle.r`, `label.text`, a C path |
| `resizables` | `nums = [0, 3, 6, 9]` with four children, `text`, `s.items`, `s.items[1]` |
| `globals` | Globals is exactly `pool, log`, decoded; `cur.flag`, `second.v`, `r.tag` |
| `breakpoint_locations` | lines with code, and none without |
| `runtime_error_assert` | a failed assert in `geo::area` stops as an exception at geo.goose:5, called from `total`; exceptionInfo; exit 1 |
| `runtime_error_bounds` | an index out of bounds stops at `get` bounds.goose:4 with the runtime's message; `i = 3` |
| `runtime_error_filter_off` | with the filter off the program runs to exit 1 |
| `runtime_error_default` | with no setExceptionBreakpoints at all (IntelliJ), it still stops |
| `build_first` | `build: true` builds and debugs |
| `build_failure` | a compile error fails the launch with a message |
| `set_variable` | `total = 41` through the Goose name changes the output |
| `stop_on_entry` | the stop at entry is reported as `entry`, its dyld frames subtle |
| `client_vanishes` | stdin closed mid-session: goose-dap, lldb-dap, debugserver and the program all end |

Every test checks at the end that goose-dap, lldb-dap and the debuggee have
exited. Any that are left are killed by PID, and the test fails.

Tested (18/18 each) on macOS arm64 with Apple lldb-dap
`lldb-2100.0.17.203` (LLVM 21) and the goose-debug compiler of goose.git
`debug1` (patches 0001-0007); and on Linux x86_64 (Ubuntu 20.04, gcc 9.4,
DWARF 4, `debug1` at 0007) with `lldb-vscode-16` (16.0.6) and `lldb-dap`
18.1.8 (focal-updates' `lldb-18`).

## Adapter versions

What goose-dap does about the older adapters. None of it changes what a
client sees from lldb-dap 21.

| | lldb-vscode 16 | lldb-dap 18 | goose-dap |
|---|---|---|---|
| A request it has no handler for (`breakpointLocations`, ...) | exits, killing the program | exits, killing the program | answers `breakpointLocations` itself; fails a request whose capability lldb-dap did not announce instead of sending it |
| `hitBreakpointIds` in `stopped` | missing; only `"description": "breakpoint 2.1"` | present | takes the ids from the description |
| A value with a summary | `" Point { ... }"` (a leading space) | as 21 | strips it |
| A repl line that is a command (`goose-locals`, `bt`) | evaluated as a C expression, unless it starts with `` ` `` | auto-detected | a repl line that fails to parse as an expression is run again as a command |
| A repl expression's result | the value alone (`3`) | `(int32_t) $0 = 3` | passed on |
| `hover` result | the value | `(gs_i64) total_g = 0`, as the repl | keeps the value |
| `setVariable` response | `value` | `result` instead of `value` | moves it to `value` |
| A field's `variablesReference` | from `MightHaveChildren()`, kept for the life of the frame's variable | from `MightHaveChildren()` | see below |
| disconnect | exits | crashes ("double free or corruption") and hangs | ends it after 3 s |

`MightHaveChildren()` asks goose_lldb.py's synthetic provider
(`GooseSynth.has_children`) before the provider's `update()` has run, and it
answers from its still-empty child list. So `u.pos` and a resizable listed
before its first push cannot be expanded. `goose_dap_lldb.py` patches
`has_children` when it loads; the change goose_lldb.py needs is below.

## Limits

- **Scopes are the map's.** A loop variable is in scope to the end of the
  function (`i` in `resizable.goose` at line 12). A block's own locals show
  in the instance it is compiled into (`it` in `twice<F={fn line 18}>`).
  A local is listed from its declaration line, before its initializer has
  run.
- **Split parameters.** A slice parameter (`xs: i64[:]`) is rebuilt in the
  body, so it is listed after the other parameters and holds garbage at the
  function's first line.
- **Globals' children, and `pool[1].v`**, need the goose_lldb.py change
  below. Without it, a global resizable's children are its C header
  (`base`, `len`), and its value in a watch is
  `len=2, element type not in debug info`. The Globals scope's value is
  right either way, because it comes from `goose-globals`.
- **Expressions are C.** Only names and paths are translated. Goose
  operators that C lacks, method calls, `as` and so on are not. Relative
  references evaluate to their stored offset (`second.next` is `1`). The
  scope's children show their targets.
- **Completions** are not offered. **lldb-dap's own exception filters**
  (C++, Objective-C, Swift) are hidden.
- **Runtime-error stops** also catch a contained worker's abort, which the
  program may expect, and `gs_panic`.
- **Internal requests block.** goose-dap handles one client request at a
  time and waits (up to 15 s) for the requests it makes of its own. A
  `breakpointLocations` for a new file while the program runs asks LLDB's
  line table through a repl `script`; if lldb-dap defers that until the
  next stop, the client waits until the timeout.
- **stopOnEntry.** lldb-dap 21 reports the stop at entry as `exception`;
  goose-dap reports it as `entry`. The stack is then dyld's alone, shown
  subtle.
- **Not tried:** threaded programs (each thread's stack is rewritten, and a
  global in a watch is read through `gs_gl` when the map names it, but no
  threaded sample was run), lldb-dap 19 and 20, Windows, attach, and
  `runInTerminal`. Reverse requests are forwarded. The real VS Code,
  Neovim and IntelliJ clients were not run here; the configurations above
  follow their documentation.

## Changes goose_lldb.py needed

Three changes this proxy needed in goose_lldb.py are made there: a global's
fields are typed along the expression path from `gs_globals_main`
(`pool[1].v`); a C child name (`s_g.items_g`, `r_g->tag_g`) finds the child
the map named in Goose; and `GooseSynth.has_children` runs `update()`
first, so lldb-dap 18 and lldb-vscode 16 offer to expand a value whose
children are only known then. `goose_dap_lldb.py`'s `_patch_has_children`
stays as a fallback for an older goose_lldb.py given with `gooseLldb`.
