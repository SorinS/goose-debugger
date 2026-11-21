# Debugging Goose in IntelliJ IDEA

How the debugger work in this repository (patches 0001–0004, `lldb/goose_lldb.py`,
the planned debug map and `goose dap`) can reach the IntelliJ plugin in
`~/Dev/Goose/goose-idea.git`. This is a design and an estimate, not an
implementation: nothing in goose-idea.git has been changed. The claims below
come from the IDEs installed on this machine (IntelliJ IDEA, CLion, GoLand and
RustRover 2026.2, inspected with `javap` and by reading plugin descriptors),
from a DAP client script driven against `lldb-dap`, and from vendor docs.

## Summary

Use the DAP client that is built into the 2026.2 platform
(`intellij.platform.dap`). Point it at `lldb-dap` now, and at `goose dap` once
the proxy exists. The plugin needs a line-breakpoint type for `.goose` files,
three small extensions and a Debug path in its run configuration. Stepping,
the call stack, variables, watches, conditional breakpoints and logpoints then
come from the platform. Goose formatting comes from `goose_lldb.py` through
DAP `variables`, so the plugin decodes nothing.

The platform DAP API is marked `@ApiStatus.Experimental`, and nothing bundled
in the four IDEs registers against it yet. If it changes or goes away, the
fallback is an `XDebugProcess` of the plugin's own over the bundled
`lsp4j.debug` library. That costs two to three more weeks; the proxy is
unaffected.

Estimate: about 1.5 weeks for a plugin that debugs through raw `lldb-dap`, and
2–3 weeks for the proxy features the IDE needs (shared with VS Code), on top
of the debug map. Plugin polish after that is 3–5 days. Windows is 1–2 more
weeks and depends on how well LLDB reads PDB.

## The plugin as it is

- Gradle with `org.jetbrains.intellij.platform`, built against the local
  `/Applications/IntelliJ IDEA.app` (IU 262.8665.258). `pluginSinceBuild=262`,
  no until-build. It is pure Java (release 21), with no Kotlin.
- `plugin.xml` depends on `com.intellij.modules.platform`,
  `com.intellij.modules.lsp` and `org.jetbrains.plugins.textmate`. It uses the
  platform's own LSP client (`platform.lsp.integrationProvider`), not LSP4IJ.
- `.goose` files have no file type of their own: the TextMate bundle claims
  the extension, and `GooseFiles.isGooseFile` tests the file name.
- There is one run configuration type, `GooseRunConfiguration`
  (`LocatableConfigurationBase`), with the actions RUN (JIT), CHECK, BUILD and
  GENERATE_C. Its `CommandLineState` runs the compiler, which
  `GooseSettings.compiler(project)` finds through `GooseCompiler` (the
  settings path, then the project's `build/*/goose`, then `PATH`, then the
  usual install directories). There is no Debug support.
- README and USAGE name IntelliJ IDEA, GoLand and CLion 2026.2 as targets.
  `pluginVerification` checks against IDEA, GoLand and CLion.

## Which IDEs, and what each one offers

| | IDEA 2026.2 (IU) | CLion 2026.2 | GoLand 2026.2 | RustRover 2026.2 |
|---|---|---|---|---|
| XDebugger API (`intellij.platform.debugger*.jar`) | yes | yes | yes | yes |
| Platform DAP client (`lib/intellij.platform.dap.jar`) | yes | yes | yes | yes |
| `eclipse.lsp4j.debug.jar` (DAP model and JSON-RPC) | yes | yes | yes | yes |
| CIDR native debugger (`plugins/nativeDebug-plugin`) | Marketplace only | bundled | Marketplace only | bundled |
| LSP4IJ installed | no | no | no | — |

- **IDEA Community.** There is no separate Community install here. Since
  2025.3, JetBrains ships one unified IntelliJ IDEA download, with a free
  core and paid Ultimate features in the same binary. An open-source build
  from GitHub remains possible. Two things keep
  the plugin out of a pure open-source build anyway. It already requires
  `com.intellij.modules.lsp`, and the DAP module is not in the
  intellij-community repository: `platform/` there has `xdebugger-api` and
  `xdebugger-impl`, but no `dap`. In practice the debugger reaches every IDE
  the plugin runs in today.
- **XDebugger** (`XDebugProcess`, `XLineBreakpointType`, `XStackFrame`,
  `XValue`, `XDebuggerEvaluator`) is open-source platform API in every IDE.
  Every route below ends in it.

### a. Writing our own XDebugProcess

This route has the most stable API and the most code. The plugin would need a
DAP transport (the bundled `lsp4j.debug` provides `IDebugProtocolServer` and
the model classes), a session state machine (initialize, launch,
configurationDone, stopped and continued events, threads), a breakpoint
handler, frames, values with lazy children and paging, an evaluator, a
console for output events, and stop and disconnect handling. That is about
2–3 weeks of work before it reaches what route b gives for free, plus the
upkeep. It is the fallback, not the plan.

### b. The platform's DAP client (recommended)

`lib/intellij.platform.dap.jar` is a core platform module, declared
`visibility="public"` and marked `@ApiStatus.Experimental` on every class. It
is the same jar in all four IDEs. It brings:

- the extension points `com.intellij.platform.dap.debugAdapterSupportProvider`
  (`DebugAdapterSupportProvider`) and
  `com.intellij.platform.dap.launchArgumentsProvider`
  (`DapLaunchArgumentsProvider`);
- `DapProgramRunner`, a program runner registered by the platform. Its
  `canRun(executorId, profile)` asks every `DapLaunchArgumentsProvider`
  whether it `isApplicable`. For the Debug executor it starts a
  `DapProcessStarter`. For the Run executor it starts one too, with
  `noDebug: true` added to the launch arguments;
- `DebugAdapterDescriptor`, an abstract class. The plugin implements `getId`,
  `launchDebugAdapter(env, executionResult, sessionId)` (a Kotlin suspend
  function that returns a `DebugAdapterHandle`; the bundled
  `CommandLineDebugAdapterHandle(GeneralCommandLine)` runs an adapter over
  stdio) and `getBreakpointsDescription()`. It can override
  `createInitializeParams`, `configureProfileState`, `beforeSessionStart`,
  `createVirtualFileResolver` and `createXDebugProcess`;
- `DapBreakpointsDescription(sourceBreakpointType, exceptionBreakpointType)`,
  which binds an `XLineBreakpointType` of the plugin's own to DAP
  `setBreakpoints`, conditions and log messages included
  (`DapSourceBreakpointHandler`);
- `DapXDebugProcess` with a `DapXDebuggerPresentationFactory`
  (`createStackFrame`, `createScope`, `createValue`). A plugin can rename,
  hide or restyle frames and values here without touching the protocol;
- registry keys for timeouts, and `dap.message.trace.dir`, which writes a
  trace of every DAP message (useful when debugging the adapter).

Bundled use: none. Every `debugAdapterSupportProvider` match in the four
IDEs' jars is the extension-point declaration itself. JavaScript's
`intellij.javascript.debugger.dap.launcher` depends on the module but
registers nothing. The API is new and has no in-house user to keep it stable.

CLion also has a user-facing DAP debugger (Settings | Build, Execution,
Deployment | Debugger | Debug Profiles, since 2025.3, in
`nativeDebug-plugin/lib/modules/intellij.cidr.debugger.dap.jar`). It is CIDR's
own DAP driver, it is internal (`visibility="internal"`), and it is CLion-only,
so a plugin cannot build on it.

LSP4IJ (Red Hat) has a mature generic DAP client: line, conditional and
hit-count breakpoints, logpoints, evaluate, a REPL console, a disassembly
view, and file-to-adapter mappings by language, file type or file-name
pattern. The plugin does not use LSP4IJ. Moving to it would add a third-party
plugin for users to install, and would mean replacing or duplicating the
platform LSP integration. It is not worth that when the platform now has its
own DAP client.

### c. CLion's native debugger (CIDR)

`com.intellij.nativeDebug` ("Native Debugging Support") is bundled in CLion
and RustRover. It requires `com.intellij.modules.nativeDebug-plugin-capable`
and runs a commercial-license check (`NativeDebugVerifier`). In IDEA and
GoLand it is a Marketplace install. Its line breakpoints
(`CidrLineBreakpointType.canPutAt`) accept a file only when its *file type*
is listed by a `cidr.debugger.lineBreakpointFileTypesProvider`. Goose files
are of the shared TextMate file type, so enabling them would enable
breakpoints in every TextMate file, unless the plugin registers a real Goose
file type, which goes against the plugin's design. Values would come through
CIDR's own LLDB driver, which loads Python formatters from an `.lldbinit`.
Expression editors need a `cidr.debugger.languageSupport`. Most of this API is
CLion-internal.

This is how the old intellij-rust plugin worked, but it ties Goose debugging
to CLion and RustRover and to internal API. It is not recommended. As a
stopgap, a CLion user can already debug a `goose build -g` executable
with a Custom Build Application configuration. Stepping and stops should
land on `.goose` lines. Judging from `canPutAt`'s bytecode (this was not
tried), breakpoints cannot be set in `.goose` files.

## What lldb-dap gives with no proxy

`~/Dev/Goose/goose.git/build-debug/goose-debug` (branch `debug1`, at
`d5a3cf7e`) built `tests/area.goose` and three samples with `--debug`. The
`goose_lldb.py` loaded was the working-tree copy as it was at run time, with
uncommitted changes that were in progress. A
Python DAP client (`tests/dap/dap_session.py`) drove Apple's
`lldb-dap` (LLVM 21, `lldb-2100.0.17.203`) the way the IDE would: initialize,
launch with `initCommands: ["command script import …/goose_lldb.py"]`,
setBreakpoints, configurationDone, then stackTrace, scopes, variables and
evaluate at each stop. This is what the IDE would show with no proxy.

**Breakpoints.** `setBreakpoints` on `area.goose:9` is verified at column 14.
Setting all 13 lines at once shows how lines move:

- a line without code moves to the next line with code: 1 → 2, 5 → 6,
  10 → 11. The IDE draws the breakpoint where the adapter says it is;
- the lines after the last code (4, 12, 13) come back unverified;
- `breakpointLocations` for lines 1–13 returns an empty list, so the IDE
  cannot learn ahead of time which lines take a breakpoint. It was asked
  after `setBreakpoints` had verified (so the line table was loaded) and
  before `configurationDone`;
- function breakpoints resolve only C names: `area` is unverified, `area_g`
  is verified.

**Stops and the call stack.** At the line-9 breakpoint (second hit, `i = 1`):

```
main_g   …/tests/area.goose:9
main     ~/.goose/build/prog/area-94627d85/area.c:1773
start    /usr/lib/dyld`start   (presentationHint: deemphasize)
```

Frames in Goose code carry `.goose` paths and lines. Names are the C names
(`main_g`, `area_g`, `geo_area_g3` for `geo::area`). The C `main` would open
the generated C in the editor.

**Stepping** goes one Goose line at a time:

- step in goes from line 9 to `area_g` at line 2 (`a_g` not yet set);
- next goes to line 3 (`a_g = 2`);
- step out returns to line 9 and shows a local `(Return Value) = 2`.

**Variables.** There are three scopes: Locals, Globals and Registers. In
`main_g` the Locals are:

```
gs_sp = 0             total_g = 0   t0 = 0   t1 = 3   i_g = 1   t4 = 0   t5 = 0
t6 = len=1, element type not in debug info      (gs_rhdr, from goose_lldb.py)
t7 = -16              t8 = 0x0000000300000000 <untyped bytes>
```

Globals are the runtime's (`gs_gl`, `gs_stks`, `gs_nstks`, `gs_globals_main`,
`gs_file0` as 60 `char` children, `gs_str0`). In `lldb/test/structs.goose`
at line 24 the formatters come through DAP unchanged, as `value` strings and
expandable children:

```
p_g      = Point { x: 3, y: -4 }                                      children x_g, y_g
u_g      = Unit { kind: 1, hp: 99, pos: Point { x: 3, y: -4 }, tag: "hero" }
c_g      = Color.Green
circle_g = Shape.Circle { r: 1.5 }
dot_g    = Shape.Dot
shapes_g = len=2, element type not in debug info
label_g  = 0x0000000300000000 <untyped bytes>                         (a Mark.. value)
t21      = Shape #112 (tag names not in debug info)                   (an unset temporary)
```

Without `goose_lldb.py` the same values read `Point_g @ 0x16fdfebc0` and
`kind_g = '\x01'`. structs has 40 temporaries (`t0`–`t39`) in its Locals,
most of them holding garbage at this line.

**Evaluate.** lldb-dap reports `supportsEvaluateForHovers`,
`supportsCompletionsRequest`, `supportsSetVariable`, `supportsLogPoints`,
`supportsConditionalBreakpoints`, `supportsHitConditionalBreakpoints`,
`supportsDataBreakpoints`, `supportsDisassembleRequest` and
`supportsReadMemoryRequest`, among others.

- In the `watch` context (also used for hovers), `total` fails with "use of
  undeclared identifier" and `total_g` returns `0`. `i_g + 1` works, because
  the expressions are C, run as C++ (each error begins "Expression evaluation
  in pure C not supported. Ran expression as 'ISO C++'").
- In the `repl` context, LLDB commands run. `goose-locals` returns
  `total = 0\ni = 1\n` (in `area_g`: `w = 1\nh = 2\na = 2`).

**Output and exit.** The program's stdout arrives as `output` events with
category `stdout` (`'6\r\n'`, a pty), then `exited {exitCode: 0}` and
`terminated`.

**Crashes and runtime errors.**

- A null write in C (`tests/backtrace/crash.goose`, built with
  `--include crash.h`) stops with `reason: exception`, `EXC_BAD_ACCESS
  (code=1, address=0x0)`. The frames are `crash_store` (crash.h:3), `poke_g`
  (crash.goose:8), `main_g` (crash.goose:13) and `main`.
- A failed Goose `assert` (`tests/backtrace/assert.goose`) does not stop at
  all. The runtime prints `goose runtime error: assert failed (geo.goose:5)`
  and the process exits with status 1.
- With a function breakpoint on `gs_abort`, the same run stops in `gs_abort`
  (goose_runtime.c:1371) above `geo_area_g3` (geo.goose:5), `total_g` and
  `main_g`. Goose runtime errors therefore need a breakpoint the adapter sets
  by itself.

The client is `tests/dap/`: `dap_session.py` (one session: a breakpoint,
then stack, scopes, variables, steps and evaluations at each stop),
`bp_lines.py` (which lines take a breakpoint) and `run_to_end.py` (a run
with no breakpoints, every event printed). They also make a ready-made
test for `goose dap`.

## The recommended design

### In the plugin

New classes, in a `debug` package:

- **`GooseLineBreakpointType extends XLineBreakpointType<…>`**. Its
  `canPutAt(file, line, project)` is true for `GooseFiles.isGooseFile(file)`
  and a line that is not blank or a comment. It cannot do better until the
  proxy answers `breakpointLocations`; lldb-dap moves a breakpoint to the next
  line with code, and the IDE shows where it went. It is registered as
  `<xdebugger.breakpointType>`. It replaces the need for a Goose file type.
- **`GooseDebugAdapterId extends DebugAdapterId`** (`"goose"`, "Goose (LLDB)")
  and **`GooseDebugAdapterSupportProvider`**, registered under
  `<extensions defaultExtensionNs="com.intellij.platform.dap">
  <debugAdapterSupportProvider …/>`.
- **`GooseDebugAdapterDescriptor extends DebugAdapterDescriptor`**:
  - `launchDebugAdapter` returns `new CommandLineDebugAdapterHandle(cmd)`,
    where `cmd` is `lldb-dap` (phase 1) or `goose dap` (phase 2). It is a
    Kotlin suspend function, but a Java override may return the handle
    directly without suspending, so the plugin can stay Java;
  - `getBreakpointsDescription()` returns
    `new DapBreakpointsDescription(GooseLineBreakpointType.class, null)`.
    Later it can add an exception breakpoint type for "stop on Goose runtime
    errors";
  - later, `createXDebugProcess` can supply a Goose
    `DapXDebuggerPresentationFactory` as a stopgap for renaming and hiding
    (below), and an editors provider for the Goose file type of the evaluate
    field.
- **`GooseDapLaunchArgumentsProvider implements DapLaunchArgumentsProvider`**.
  `isApplicable` is true only for the Debug executor and a
  `GooseRunConfiguration`. It must return false for Run, or `DapProgramRunner`
  would take over plain Run as a `noDebug` DAP launch.
  `getLaunchArguments` returns `Launch` with:
  - `program`, `args`, `cwd` and `env`;
  - `initCommands: ["command script import <goose_lldb.py>"]` (phase 1 only;
    `goose dap` would load it itself);
  - `stopOnEntry: false`.
- **Building.** Debug always runs `goose build -g -o <out> <file>` first.
  `<out>` is a per-configuration path under the IDE's system directory or
  `build/`. The JIT cannot be debugged: `goose run` is TinyCC in memory, and
  LLDB cannot debug it as it can a built executable. There are two places to
  run the build:
  - a `BeforeRunTask` that the configuration adds by default for Debug. The
    build output then goes to the Build/Run console, and a failed build stops
    the launch;
  - `DebugAdapterDescriptor.configureProfileState`, together with the
    configuration's `getState(…)` for the Debug executor.
    `DapProcessStarter.executeState` calls `state.execute(executor, runner)`
    before it launches the adapter, and passes the `ExecutionResult` on to
    `launchDebugAdapter`. So the state can be the build itself (its console
    shows compiler errors, filtered by `GooseConsoleFilter`), with the program
    started by the adapter afterwards.

  The second keeps everything in one console tab. Which of the two the
  platform handles more cleanly (for instance, whether it waits for the
  state's process to end) has to be tried in a `runIde` sandbox; that is
  phase 0.
- **Settings** (`GooseSettings` and `GooseConfigurable`): the adapter
  (`lldb-dap` path; empty searches
  `/Library/Developer/CommandLineTools/usr/bin`, `xcrun -f lldb-dap`, `PATH`,
  `lldb-dap-NN` names on Linux, and the LLVM install on Windows), and
  later a choice between `lldb-dap` and `goose dap`.
- **`goose_lldb.py`** ships with the plugin, beside `textmate/` in the plugin
  directory (the `prepareSandbox` copy already does this for the TextMate
  bundle), until `goose dap` carries it.

`plugin.xml` gets:

```xml
<dependencies> <module name="intellij.platform.dap"/> </dependencies>
<extensions defaultExtensionNs="com.intellij">
  <xdebugger.breakpointType implementation="io.github.sorins.goose.debug.GooseLineBreakpointType"/>
</extensions>
<extensions defaultExtensionNs="com.intellij.platform.dap">
  <debugAdapterSupportProvider implementation="io.github.sorins.goose.debug.GooseDebugAdapterSupportProvider"/>
  <launchArgumentsProvider implementation="io.github.sorins.goose.debug.GooseDapLaunchArgumentsProvider"/>
</extensions>
```

The module declares no `<module value=…>` alias, so `<depends>` cannot name
it. The bundled `intellij.javascript.debugger.dap.launcher` uses the same
`<dependencies>` form. Phase 0 should check how that form combines with the
plugin's existing `<depends>` lines.
`build.gradle` gets `bundledModule('intellij.platform.dap')` in the
`intellijPlatform` dependencies, and the plugin verifier will report the
experimental API usage. Kotlin is not needed. lsp4j.debug types
(`org.eclipse.lsp4j.debug.*`) appear in a few signatures; they come with the
platform.

### What the platform then provides

- Breakpoints, with conditions, hit counts and log messages, sent as DAP
  `setBreakpoints` per file.
- Step over, into and out, run to cursor, pause, resume and threads.
- The frames list and Variables view, with lazy children. Frame chunk size is
  the registry key `dap.lazy.load.stack.trace.chunk.size`.
- Watches, hover and Evaluate through DAP `evaluate`.
- The program's output in the debug console, from `output` events.

Its limits, as far as the jar shows:

- it has no `runInTerminal` handling, so a program that reads stdin cannot be
  fed from the IDE;
- `startDebugging` (child sessions) is not handled; Goose does not need it.

### What `goose dap` must do for the IDE

All of this belongs in the proxy, so that VS Code and IDEA share it. The
plugin's presentation factory is only a stopgap until it exists.

1. **Frames.** Rename from the map (`area_g` → `area`, `geo_area_g3` →
   `geo::area`, generic instances). Mark or drop the C `main`, `gs_*` runtime
   frames and system frames with `presentationHint: "subtle"` or
   `"deemphasize"`, so that the IDE does not open `area.c` or
   `goose_runtime.c`. The stack should start at the Goose frame that called
   the runtime.
2. **Locals.** Hide `gs_sp` and the `tN` temporaries. Name variables by their
   Goose names (`total`, not `total_g`), and children too (`x`, not `x_g`).
   Show only the locals in scope at the stop line, using the map's line
   ranges, which also hides values that are not set yet (`a_g =
   4295120080`).
3. **Values the formatters cannot show**, from the map: elements of
   resizables, variable-mode values (`label_g`), pool-relative references,
   and data-stack locals.
4. **Globals.** Show the program's Goose globals and drop the runtime ones
   (`gs_file0`, `gs_stks` and the rest). Mark the scope `expensive` if it is
   large.
5. **Evaluate.** In the `watch` and `hover` contexts, rewrite Goose
   identifiers and field paths to their C forms (`total` → `total_g`,
   `u.pos.x` → `u_g.pos_g.x_g`) and pass the rest through. Later it can
   evaluate Goose expressions properly. Keep `repl` as LLDB commands (with
   `goose-locals` and `goose-as`), or offer a Goose prompt with a `:` escape
   to LLDB.
6. **Breakpoints.**
   - Answer `breakpointLocations` from the map's line table, so that lines
     with no code can be refused.
   - Map function breakpoints from Goose names to C names.
   - Offer an exception filter "Goose runtime errors", which sets a breakpoint
     on `gs_abort` and reports the stop as `reason: exception` with the
     runtime's message (bounds, overflow, assert). Without it the IDE never
     stops on a failed assert.
7. **Set variable** through Goose names, if `supportsSetVariable` is passed
   through.
8. **Capabilities.** Pass lldb-dap's capabilities through, minus whatever the
   proxy cannot translate (`supportsCompletionsRequest` until completions are
   rewritten).

Everything else (stepping, threads, memory, disassembly, data breakpoints)
passes through unchanged.

### Windows

- **vsdbg / cppvsdbg is out.** Its license allows use only with Visual
  Studio, Visual Studio Code and other Microsoft products, and Microsoft has
  enforced this against VS Code forks since April 2025. The same holds for
  the .NET `vsdbg`. An IntelliJ plugin cannot ship or drive it.
- **lldb-dap with PDB.** LLVM made its native PDB reader the default on
  Windows in October 2025 (llvm-project PR #165363, so LLVM 22 and later),
  and plans to remove the DIA reader. `goose build -g` with MSVC gives
  `/Zi` PDBs, and with clang it can give PDB or DWARF. Whether `#line`
  locations, locals and struct types come through LLDB's PDB reader well
  enough has to be tested. The `.expected` files in `lldb/test/` are the
  yardstick.
- Two Windows-specific risks:
  - LLVM's Windows `lldb-dap` loads Python formatters only if the matching
    Python DLL is on `PATH`, and LLVM's release builds link one particular
    Python version;
  - PDB type names differ from DWARF's, so `goose_lldb.py`'s recognizers may
    need a Windows case.
- CLion on Windows debugs MSVC programs with its own LLDB-based debugger,
  which shows that LLDB with PDB is workable, but that path is CIDR's and not
  available to the plugin.
- If LLDB with PDB falls short, the next choice is DWARF on Windows: build
  with clang and `-gdwarf` and debug with lldb-dap. That works for programs
  that do not need MSVC.

## Plan and estimate

The estimates are for one person who knows the code. Plugin work and proxy
work are listed separately; the proxy also serves VS Code.

0. **Spike (plugin, 1–2 days).** In a `runIde` sandbox, register the support
   provider, descriptor, launch-arguments provider and breakpoint type, and
   hard-code `lldb-dap` and `area`. This settles where the build step lives
   (state or before-run task), confirms that breakpoints and the Variables
   view behave as in the transcript above, and checks the experimental API on
   2026.2.
1. **Plugin on raw lldb-dap (about 1.5 weeks with tests).**
   - Debug for Goose run configurations: build first, then launch;
   - the adapter setting and its discovery;
   - `goose_lldb.py` shipped with the plugin;
   - a headless test like the existing LSP test, that runs a DAP session when
     a debug compiler and `lldb-dap` are present;
   - a README and USAGE section, and an example.

   This is usable on macOS and Linux at the quality shown above: Goose lines
   and stepping, C names, temporaries visible, C expressions.
2. **`goose dap` features for the IDE (proxy, 2–3 weeks once the debug map
   exists).** Items 1–6 above, in that order. Frames and locals first, since
   they are most of what users see. Then switch the plugin's default adapter
   to `goose dap` (plugin, 1–2 days).
3. **Plugin polish (3–5 days).**
   - an exception-breakpoint type for runtime errors;
   - a Goose editors provider for watches and the evaluate field;
   - "Debug" gutter icons beside "Run" from the producer;
   - attach to a running process (DAP `attach` with `pid`).
4. **Windows (1–2 weeks, more if the PDB route fails).** Covers LLDB with
   PDB, Python discovery for `lldb-dap`, `goose_lldb.py` on PDB type names,
   and the DWARF fallback.

Contingency: 2–3 weeks if the platform DAP API changes incompatibly and the
plugin has to move to its own `XDebugProcess` (route a). In that case the
transcript behavior and the proxy stay as they are.

## Risks

- **Experimental platform API.** Every class in `intellij.platform.dap` is
  `@ApiStatus.Experimental`, nothing bundled uses it, and it is
  closed-source. Expect breaking changes between 2026.x releases. Keep the
  plugin's use of it to the four classes above. Keep Goose logic in the proxy
  rather than in presentation-factory overrides. Run the plugin verifier
  against each new IDE build.
- **Editions.** The DAP module is not in the open-source intellij-community
  repository. That is no new restriction, since the plugin already needs
  `com.intellij.modules.lsp`. CIDR-based debugging would further restrict
  Goose debugging to CLion and RustRover, which is a reason not to use it.
- **lldb-dap versions.** Apple's `lldb-dap` (LLVM 21) and LLVM's releases
  differ in launch arguments and capabilities. The proxy should state a
  minimum version and test against both.
- **No stdin** for the debugged program, since there is no `runInTerminal`
  client.
- **Windows** depends on LLDB's PDB support, which was not tested here.
- **JIT programs** cannot be debugged; Debug always builds an executable.

## Sources

- [IntelliJ IDEA moves to the unified distribution](https://blog.jetbrains.com/idea/2025/07/intellij-idea-moves-to-the-unified-distribution/)
  (2025.3).
- The installed IDEs: `/Applications/{IntelliJ IDEA,CLion,GoLand,RustRover}.app`,
  `product-info.json`, `lib/intellij.platform.dap.jar` (classes and
  `intellij.platform.dap.xml`), and
  `CLion.app/Contents/plugins/nativeDebug-plugin` (`plugin.xml`,
  `intellij.cidr.debugger.dap.jar`, `CidrLineBreakpointType`).
- [CLion: Debug Adapter Protocol](https://www.jetbrains.com/help/clion/2026.2/dap-support.html)
  and the CLion blog's [DAP debuggers](https://blog.jetbrains.com/clion/2025/10/dap-debuggers/).
- [LSP4IJ DAP user guide](https://github.com/redhat-developer/lsp4ij/blob/main/docs/dap/UserGuide.md).
- vsdbg licensing: [dotnet/core#4788](https://github.com/dotnet/core/issues/4788),
  the [cpptools license discussion in Theia](https://github.com/eclipse-theia/theia/discussions/8564),
  and [the April 2025 enforcement](https://winbuzzer.com/2025/04/26/microsoft-blocks-popular-c-extension-in-visual-studio-code-forks-xcxwbn/).
- LLDB's PDB reader: [llvm-project PR #165363](https://lists.llvm.org/pipermail/lldb-commits/Week-of-Mon-20251027/153862.html)
  and the [RFC to remove the DIA plugin](https://discourse.llvm.org/t/rfc-removing-the-dia-pdb-plugin-from-lldb/87827).
