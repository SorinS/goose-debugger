// goose-dap: a Debug Adapter Protocol proxy that turns lldb-dap's C-level
// view of a Goose program into a Goose-level one.
//
//     editor  <--DAP/stdio-->  goose-dap  <--DAP/pipes-->  lldb-dap
//
// It speaks DAP on its own stdin/stdout (Content-Length framing, binary, no
// TTY assumed) and runs lldb-dap as a child over pipes. Most messages pass
// through unchanged; the ones below are rewritten with the program's debug
// map (<stem>.debug.json, written by `goose build -g` beside the generated
// C):
//
//   initialize      a "Goose runtime errors" exception filter in place of
//                   lldb-dap's C++/ObjC/Swift ones; completions and cancel
//                   are not offered (they would need translating)
//   launch/attach   goose_lldb.py loaded through initCommands, step-in
//                   avoiding gs_* runtime functions, and optionally
//                   `goose build -g` first
//   stackTrace      Goose function names; gs_* runtime frames dropped; the C
//                   main subtle, its generated .c source deemphasized
//   scopes          Locals and Globals of a Goose frame are rewritten
//   variables       Goose names, only the locals in scope at the frame's line,
//                   no temporaries; Goose types; evaluateName in Goose form
//   evaluate        Goose identifiers and field paths to C (u.pos.x ->
//                   u_g.pos_g.x_g); repl commands pass through
//   setVariable     a Goose name back to the C one
//   setFunctionBreakpoints   a Goose name to every C instance of it
//   breakpointLocations      answered from LLDB's line table when lldb-dap
//                   returns nothing
//   setExceptionBreakpoints  the Goose filter is a function breakpoint on
//                   gs_fail and gs_panic; a stop there is reported as an
//                   exception with the runtime's message (exceptionInfo too)
//
// Its only dependency is LLDB: lldb-dap, and goose_lldb.py (the value
// formatters) loaded into it, from $GOOSE_DAP_LLDB_DIR or ../lldb/ beside
// this executable. Set GOOSE_DAP_LOG=FILE to log every message.

import std;
import os;
import gslib from "SorinS/gslib@v0.9.2";
import gslib.sys;
import gslib.sys.unix;
import .framing;
import .names;
import .proxy;
import .launch;
import .lldb;
import .breakpoints;
import .frames;
import .variables;
import .evaluate;
import .events;

let VERSION = "goose-dap 0.1";

let USAGE = """
    usage: goose-dap [-h] [--lldb-dap LLDB_DAP] [--log LOG] [--version]

    goose-dap: a Debug Adapter Protocol proxy that turns lldb-dap's C-level
    view of a Goose program into a Goose-level one.

    options:
      -h, --help           show this help message and exit
      --lldb-dap LLDB_DAP  the lldb-dap to run (default: $GOOSE_DAP_LLDB_DAP,
                           xcrun -f lldb-dap, the Command Line Tools, PATH)
      --log LOG            log every DAP message to this file (or set
                           GOOSE_DAP_LOG)
      --version

    """;

fn usage_error(what: const u8[:]) {
    write_stderr("usage: goose-dap [-h] [--lldb-dap LLDB_DAP] [--log LOG] [--version]\n");
    write_stderr(str("goose-dap: error: ", what, "\n"));
    exit(2);
}

// Where goose_lldb.py is, and the compiler beside this program: a
// snapshot has bin/goose-dap, bin/goose-debug and lldb/goose_lldb.py.
fn find_files() {
    let exe = sys::realpath(sys::exe_path());
    let bin = dirname(exe);
    let top = dirname(bin);
    var dir: u8[>..] = [];
    if !env("GOOSE_DAP_LLDB_DIR", dir) || dir.len == 0 {
        dir.clear();
        join_path(dir, top, "lldb");
    }
    join_path(goose_lldb_path, dir, "goose_lldb.py");
    var b: u8[>..] = [];
    join_path(b, top, "bin");
    join_path(beside_goose, b, str("goose-debug", exe_suffix));
}

fn main() {
    windows_host = sys::is_windows();
    if windows_host { exe_suffix = ".exe"; }
    var log_path: u8[>..] = [];
    let n = arg_count();
    var i = 1;
    while i < n {
        var a: u8[>..] = [];
        arg(i, a);
        i++;
        if a == "-h" || a == "--help" { write_stdout(USAGE); return; }
        if a == "--version" { write_stdout(str(VERSION, "\n")); return; }
        if a == "--lldb-dap" || a == "--log" {
            if i >= n { usage_error(str("argument ", a, ": expected one argument")); }
            var v: u8[>..] = [];
            arg(i, v);
            i++;
            if a == "--log" { log_path = copy(v); } else { cli_lldb_dap = copy(v); }
            continue;
        }
        if starts_with(a, "--lldb-dap=") { cli_lldb_dap.append(a[11..]); continue; }
        if starts_with(a, "--log=") { log_path.clear(); log_path.append(a[6..]); continue; }
        usage_error(str("unrecognized arguments: ", a));
    }
    if log_path.len == 0 { env("GOOSE_DAP_LOG", log_path); }

    in_fd = sys::std_fd(0);
    out_fd = sys::std_fd(1);
    if sys::isatty(in_fd) > 0 && sys::isatty(out_fd) > 0 {
        write_stderr("goose-dap speaks the Debug Adapter Protocol on stdin/stdout; run it from an editor's debug configuration.\n");
    }
    // A write to an editor or an lldb-dap that has gone is an error to
    // handle, not the end of this process (Python ignores SIGPIPE too).
    // POSIX only; Windows has no SIGPIPE.
    unix::ignore(unix::SIGPIPE);

    escape.append("`");
    adapter_caps.append("{}");
    launch_args.append("{}");
    client_fbps.append("[]");
    find_files();
    log_open(log_path);

    thread_spawn(reader, in_fd, SRC_CLIENT, 0, false);
    // SIGINT is how nvim-dap ends every session (after closing stdin): a
    // normal end, as Python's KeyboardInterrupt was.
    if unix::notify(unix::SIGINT) == 0 { thread_spawn(signal_waiter); }
    run();
    exit(0);
}
