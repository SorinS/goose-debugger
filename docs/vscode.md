# Debugging Goose in VS Code and VSCodium

VS Code and VSCodium debug Goose programs the same way: the **LLDB DAP**
extension starts `goose-dap` from a snapshot in place of its own
`lldb-dap`, and `goose-dap` builds the program with `-g`, starts the
system's `lldb-dap` and makes what it shows Goose-level (Goose names,
types and values, stepping by Goose line, stops on runtime errors).

The two editors differ only in where things live (see
[VS Code or VSCodium](#vs-code-or-vscodium)). Everything else on this page
applies to both.

## What you need

1. **A snapshot** (a release archive or `scripts/make_snapshot.sh`),
   unpacked anywhere and kept as it is:

   ```
   goose-debug-<version>-<commit>-<platform>/
     bin/goose-debug     the compiler, with -g
     bin/goose-dap       the adapter
     lldb/goose_lldb.py
     stdlib/, bin/tcclib/
   ```

   `goose-dap` finds the compiler (`../bin/goose-debug`) and
   `goose_lldb.py` (`../lldb/`) beside itself, so nothing needs to be on
   `PATH` and no environment variable is needed.

2. **lldb-dap**, which `goose-dap` finds by itself:
   - macOS: Xcode or the Command Line Tools (`xcrun -f lldb-dap`).
   - Linux: LLVM 18 or later, e.g. `apt install lldb-18`
     (`/usr/bin/lldb-dap-18`). `lldb-vscode` 16 is the oldest that works.
   - Windows: LLVM's installer (`lldb-dap.exe`), and a Python LLVM's lldb
     can load, for `goose_lldb.py`. Without that Python, values show as
     their C.

   If it is somewhere `goose-dap` does not look, set `"lldbDap"` in the
   launch configuration or `LLDB_DAP` in the environment.

3. **The LLDB DAP extension** (`llvm-vs-code-extensions.lldb-dap`), 0.4.1
   or later for `debugAdapterExecutable`.

4. Optionally **the Goose extension** (`aardappel.goose-language`) for
   syntax, the language server and Run with JIT. Its 0.4.0 does not debug;
   debugging goes through LLDB DAP.

## VS Code or VSCodium

| | VS Code | VSCodium |
|---|---|---|
| Extensions from | Visual Studio Marketplace | Open VSX (or **Install from VSIX...**) |
| Install from the command line | `code --install-extension llvm-vs-code-extensions.lldb-dap` | `codium --install-extension llvm-vs-code-extensions.lldb-dap` |
| Installed extensions | `~/.vscode/extensions` | `~/.vscode-oss/extensions` |
| User settings, macOS | `~/Library/Application Support/Code/User/settings.json` | `~/Library/Application Support/VSCodium/User/settings.json` |
| User settings, Linux | `~/.config/Code/User/settings.json` | `~/.config/VSCodium/User/settings.json` |
| User settings, Windows | `%APPDATA%\Code\User\settings.json` | `%APPDATA%\VSCodium\User\settings.json` |

If an extension is not on Open VSX, download its `.vsix` from the
Marketplace page and install it with **Extensions: Install from VSIX...**
or `codium --install-extension file.vsix`.

The launch configuration is not in any of these directories: it is
`.vscode/launch.json` in the folder you open (the same name in both
editors), or the `"launch"` setting in the user settings for every folder.

## Setting up

**1. Allow breakpoints in `.goose` files.** No extension declares
breakpoints for Goose yet, so add to the user settings:

```json
"debug.allowBreakpointsEverywhere": true
```

**2. Add a launch configuration** to `.vscode/launch.json` in your project
(create the directory and file if they are not there):

```jsonc
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "lldb-dap",
      "request": "launch",
      "name": "Debug Goose program",
      "debugAdapterExecutable": "/path/to/goose-debug-.../bin/goose-dap",
      "source": "${file}",
      "program": "${fileDirname}/${fileBasenameNoExtension}",
      "build": true,
      "cwd": "${workspaceFolder}",
      "args": []
    }
  ]
}
```

On Windows, the adapter is `bin\\goose-dap.exe` and the program ends in
`.exe`: `"${fileDirname}\\${fileBasenameNoExtension}.exe"`.

To have it in every folder instead, put the same configuration in the user
settings:

```jsonc
"launch": {
  "version": "0.2.0",
  "configurations": [ { "type": "lldb-dap", "...": "as above" } ]
}
```

**3. Debug.** Open a `.goose` file, click in the gutter to set a
breakpoint, pick **Debug Goose program** in **Run and Debug**, and press
**F5**.

With `build: true`, `goose-dap` runs `goose-debug build -g -o <program>
<source>` before each launch. Compiler output goes to the Debug Console,
and a failed build fails the launch with the compiler's last line. Without
`build`, build the program yourself with `goose-debug build -g` and give
only `program`.

A JIT run (`goose run`) cannot be debugged: it is always a built
executable.

## Launch keys

`goose-dap` passes on everything `lldb-dap` takes (`program`, `args`,
`cwd`, `env`, `stopOnEntry`, `initCommands`, ...) and adds:

| Key | Meaning |
|---|---|
| `build` | `true`: build `source` with `-g` before launching. |
| `source` | The `.goose` file to build. Without `program`, the program is the source's stem, in `cwd` or else beside the source. |
| `goose` | The compiler. Default: `../bin/goose-debug` beside `goose-dap`. |
| `buildArgs` | More compiler arguments, as a list. |
| `lldbDap` | The `lldb-dap` to run. |
| `stopOnRuntimeError` | Default `true`: stop where a Goose runtime error is raised. The **Goose runtime errors** box under Breakpoints turns it on and off. |
| `showRuntimeFrames` | Default `false`. `true` keeps the runtime's `gs_*` frames in the call stack. |

The extension's schema does not know `source`, `build`, `goose`,
`buildArgs`, `lldbDap`, `stopOnRuntimeError` and `showRuntimeFrames`, so
the editor underlines them with "Property ... is not allowed". It passes
them on all the same; the warning can be ignored.

## Older LLDB DAP extensions

Before `debugAdapterExecutable`, the extension had only the setting
`lldb-dap.executable-path`. Set it to `goose-dap`:

```json
"lldb-dap.executable-path": "/path/to/goose-debug-.../bin/goose-dap"
```

and leave `debugAdapterExecutable` out of the configuration. The setting
applies to every `lldb-dap` session, C and C++ ones too; `goose-dap`
debugs those as `lldb-dap` would, but without `source` there is nothing
for it to build.

## When it does not work

- **Log.** Add `"debugAdapterArgs": ["--log", "/tmp/goose-dap.log"]` (or
  set `GOOSE_DAP_LOG`): every message both ways, and `lldb-dap`'s stderr.
- **"cannot start lldb-dap at ..."** or **"lldb-dap is not running"**:
  install it (see above) or set `lldbDap`.
- **Breakpoints stay grey or are refused**: the program was not built with
  `-g` (use `build: true` or `goose-debug build -g`), or
  `debug.allowBreakpointsEverywhere` is not set.
- **Values show as C structs** (Windows): LLVM's lldb found no Python to
  load `goose_lldb.py` with.
- **The build fails on std**: the compiler and the standard library are
  from different commits. Use the snapshot's own `bin/goose-debug`, which
  reads the `stdlib/` beside it, and no `--stdlib`.
