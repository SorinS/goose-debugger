// The editor's requests: which ones the proxy handles and which pass on,
// initialize, launch and attach (with `build: true`, `goose build -g`
// first), and finding and respawning lldb-dap.

import std;
import os;
import json;
import gslib.sys;
import gslib.sys.process;
import .framing;
import .jsonw;
import .names;
import .store;
import .proxy;

let DEFAULT_LLDB_DAP = "/Library/Developer/CommandLineTools/usr/bin/lldb-dap";

// Requests that need a capability; one lldb-dap did not announce is
// answered here rather than forwarded (see on_client): lldb-vscode and
// lldb-dap 18 exit on a request they have no handler for, taking the
// debuggee with them.
fn gating_capability(cmd: const u8[:]) -> const u8[:] {
    if cmd == "stepInTargets" { return "supportsStepInTargetsRequest"; }
    if cmd == "gotoTargets" || cmd == "goto" { return "supportsGotoTargetsRequest"; }
    if cmd == "restart" { return "supportsRestartRequest"; }
    if cmd == "restartFrame" { return "supportsRestartFrame"; }
    if cmd == "loadedSources" { return "supportsLoadedSourcesRequest"; }
    if cmd == "readMemory" { return "supportsReadMemoryRequest"; }
    if cmd == "writeMemory" { return "supportsWriteMemoryRequest"; }
    if cmd == "disassemble" { return "supportsDisassembleRequest"; }
    if cmd == "setExpression" { return "supportsSetExpression"; }
    if cmd == "dataBreakpointInfo" || cmd == "setDataBreakpoints" { return "supportsDataBreakpoints"; }
    if cmd == "setInstructionBreakpoints" { return "supportsInstructionBreakpoints"; }
    if cmd == "modules" { return "supportsModulesRequest"; }
    if cmd == "terminateThreads" { return "supportsTerminateThreadsRequest"; }
    if cmd == "stepBack" || cmd == "reverseContinue" { return "supportsStepBack"; }
    return "";
}

let HANDLED = ["initialize", "launch", "attach", "setFunctionBreakpoints", "setExceptionBreakpoints",
               "configurationDone", "breakpointLocations", "stackTrace", "scopes", "variables",
               "evaluate", "setVariable", "exceptionInfo", "disconnect", "terminate",
               "completions", "cancel"];

fn is_handled(cmd: const u8[:]) -> bool {
    for h in HANDLED { if h == cmd { return true; } }
    return false;
}

// Whether lldb-dap announced capability name.
fn adapter_has(name: const u8[:]) -> bool {
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let caps, err = json::parse(adapter_caps, doc, dec);
    return truthy(json::member(caps, name));
}

fn on_client(req: json::Node&, text: const u8[:]) {
    let cmd, is_cmd = get_str(req, "command");
    let handled = is_cmd && is_handled(cmd);
    if cmd != "initialize" && !child_alive {
        if cmd == "disconnect" { respond_ok(req, ""); return; }
        respond_fail(req, "lldb-dap is not running");
        return;
    }
    let cap = gating_capability(cmd);
    if !handled && cap.len > 0 && !adapter_has(cap) {
        respond_fail(req, str(cmd, " is not supported by this lldb-dap"));
        return;
    }
    if !handled {
        if cmd == "continue" || cmd == "next" || cmd == "stepIn" || cmd == "stepOut" || cmd == "stepBack"
           || cmd == "reverseContinue" || cmd == "goto" || cmd == "restartFrame" {
            reset_stop_state();
            store_clear(exc);
        }
        forward(req, text, "", "");
        return;
    }
    if cmd == "initialize" { c_initialize(req, text); }
    else if cmd == "launch" || cmd == "attach" { c_launch_or_attach(req, text); }
    else if cmd == "setFunctionBreakpoints" { c_set_function_breakpoints(req); }
    else if cmd == "setExceptionBreakpoints" { c_set_exception_breakpoints(req); }
    else if cmd == "configurationDone" { c_configuration_done(req, text); }
    else if cmd == "breakpointLocations" { c_breakpoint_locations(req, text); }
    else if cmd == "stackTrace" { c_stack_trace(req, text); }
    else if cmd == "scopes" { c_scopes(req, text); }
    else if cmd == "variables" { c_variables(req, text); }
    else if cmd == "evaluate" { c_evaluate(req, text); }
    else if cmd == "setVariable" { c_set_variable(req, text); }
    else if cmd == "exceptionInfo" { c_exception_info(req, text); }
    else if cmd == "disconnect" { disconnecting = true; forward(req, text, "", ""); }
    else if cmd == "terminate" { forward(req, text, "", ""); }
    else if cmd == "completions" { respond_ok(req, "{\"targets\":[]}"); }
    else if cmd == "cancel" { respond_ok(req, ""); }
}

fn c_initialize(req: json::Node&, text: const u8[:]) {
    init_args.clear();
    let a = json::member(req, "arguments");
    if a && is_obj(a) { emit(init_args, a); } else { init_args.append("{}"); }
    if !child_alive && !start_child(find_lldb_dap("")) {
        respond_fail(req, "lldb-dap is not running");
        return;
    }
    forward(req, text, "", "{\"kind\":\"initialize\"}");
}

// lldb-dap's capabilities as the editor gets them: a "Goose runtime errors"
// exception filter in place of lldb-dap's C++/ObjC/Swift ones; completions
// and cancel not offered (they would need translating).
fn rewrite_capabilities(out: u8[>..]&, caps: json::Node?) {
    out.push('{');
    copy_except(out, caps, ["supportsCompletionsRequest", "completionTriggerCharacters", "supportsCancelRequest",
                            "exceptionBreakpointFilters", "supportsExceptionInfoRequest",
                            "supportsExceptionFilterOptions", "supportsBreakpointLocationsRequest"]);
    kv_raw(out, "exceptionBreakpointFilters", """[{"filter":"goose_runtime","label":"Goose runtime errors","description":"Stop where a Goose check or abort fails: assert, bounds, overflow, division by zero, abort(msg)","default":true,"supportsCondition":true}]""");
    kv_bool(out, "supportsExceptionInfoRequest", true);
    kv_bool(out, "supportsExceptionFilterOptions", true);
    kv_bool(out, "supportsBreakpointLocationsRequest", true);
    out.push('}');
}

let LAUNCH_KEYS = ["showRuntimeFrames", "stopOnRuntimeError", "lldbDap", "goose", "source", "build",
                   "buildArgs", "gooseLldb", "program", "initCommands"];

fn c_launch_or_attach(req: json::Node&, text: const u8[:]) {
    let args = json::member(req, "arguments");
    show_runtime_frames = truthy(json::member(args, "showRuntimeFrames"));
    let sore = json::member(args, "stopOnRuntimeError");
    stop_on_runtime_error = if sore { truthy(sore) } else { true };
    let prefix, has_prefix = get_str(args, "commandEscapePrefix");
    if has_prefix { escape.clear(); escape.append(prefix); }
    let override = json::member(args, "lldbDap");
    let source = str_or(args, "source", "");
    let build_on = truthy(json::member(args, "build"));
    var goose_lldb: u8[>..] = [];
    let gl = json::member(args, "gooseLldb");
    if truthy(gl) && is_str(gl) { goose_lldb.append(str_or(args, "gooseLldb", "")); }
    else {
        var e: u8[>..] = [];
        if env("GOOSE_DAP_GOOSE_LLDB", e) && e.len > 0 { goose_lldb.append(e); }
        else { goose_lldb.append(goose_lldb_path); }
    }
    if truthy(override) && is_str(override) {
        let path = str_or(args, "lldbDap", "");
        if path != child_path && !respawn(path) {
            respond_fail(req, str("cannot start lldb-dap at ", path));
            return;
        }
    }
    // program: the source's stem, in cwd or else beside the source.
    var program: u8[>..] = [];
    let prog = json::member(args, "program");
    let given = truthy(prog);
    if given && is_str(prog) { program.append(str_or(args, "program", "")); }
    if source.len > 0 && !given {
        let cwd = str_or(args, "cwd", "");
        var dir: u8[>..] = [];
        if cwd.len > 0 { dir.append(cwd); } else { abs_dir_of(dir, source); }
        join_path(program, dir, str(stem(basename(source)), exe_suffix));
    }
    if build_on {
        var message: u8[>..] = [];
        if !build(str_or(args, "goose", ""), source, program, json::member(args, "buildArgs"),
                  str_or(args, "cwd", ""), message) {
            respond_fail(req, message);
            return;
        }
    }
    var a: u8[>..] = [];
    a.push('{');
    copy_except(a, args, LAUNCH_KEYS);
    if source.len > 0 && !given { kv_str(a, "program", program); }
    else if prog { kv_node(a, "program", prog); }
    key(a, "initCommands");
    a.append("[\"command script import ");
    var quoted: u8[>..] = [];
    json::format_string(quoted, goose_lldb);
    json::format_escaped(a, quoted);
    a.push('"');
    // Step in goes over the runtime's helpers (checked arithmetic,
    // pushes) rather than into goose_runtime.c.
    a.append(",\"settings set target.process.thread.step-avoid-regexp ^(std::|gs_)\"");
    let user = json::member(args, "initCommands");
    if user && is_arr(user) {
        var c = user.first;
        while c { sep(a); emit(a, c); c .= c.next; }
    }
    a.append("]}");
    launch_args.clear();
    launch_args.append(a);
    forward(req, text, a, "{\"kind\":\"launch\"}");
}

// The absolute directory of a path (Python's dirname(abspath(path))).
fn abs_dir_of(out: u8[>..]&, path: const u8[:]) {
    var full: u8[>..] = [];
    if is_abs(path) { full.append(path); } else { join_path(full, sys::getcwd(), path); }
    out.append(dirname(full));
}

fn respawn(path: const u8[:]) -> bool {
    stop_child(1000);
    if !start_child(path) {
        log("respawn failed", path);
        return false;
    }
    var r: u8[>..] = [];
    let args = if init_args.len > 0 { init_args[..] } else { "{}" };
    guard adapter_request("initialize", args, INTERNAL_TIMEOUT_MS, r) else { return false; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    guard request_ok(root) else { return false; }
    let body = json::member(root, "body");
    adapter_caps.clear();
    if body && is_obj(body) { emit(adapter_caps, body); } else { adapter_caps.append("{}"); }
    var caps: u8[>..] = [];
    rewrite_capabilities(caps, body);
    event("capabilities", str("{\"capabilities\":", caps, "}"));
    return true;
}

// --- lldb-dap and the compiler ------------------------------------------------------

// The lldb-dap to run: the launch's lldbDap, --lldb-dap, $GOOSE_DAP_LLDB_DAP,
// $LLDB_DAP, xcrun's, the Command Line Tools', then on PATH.
fn find_lldb_dap(override: const u8[:]) -> u8[>..] {
    if override.len > 0 { return copy_text(override); }
    if cli_lldb_dap.len > 0 { return copy(cli_lldb_dap); }
    for name in ["GOOSE_DAP_LLDB_DAP", "LLDB_DAP"] {
        var e: u8[>..] = [];
        if env(name, e) && e.len > 0 { return copy_text(e); }
    }
    let x = xcrun_lldb_dap();
    if x.len > 0 { return copy_text(x); }
    if file_exists(DEFAULT_LLDB_DAP) { return copy_text(DEFAULT_LLDB_DAP); }
    // LLVM 18 renamed lldb-vscode to lldb-dap; Debian and Ubuntu install
    // versioned names (lldb-dap-18, lldb-vscode-16) on PATH and the
    // plain ones in /usr/lib/llvm-NN/bin.
    var names: u8[>..] = [];
    names.append("lldb-dap"); names.push(0);
    var v = 30;
    while v >= 18 { format(names, "lldb-dap-", v); names.push(0); v--; }
    names.append("lldb-vscode"); names.push(0);
    v = 17;
    while v >= 16 { format(names, "lldb-vscode-", v); names.push(0); v--; }
    var pos: i64 = 0;
    while pos < names.len {
        let n = process::next_string(names, pos);
        var err: i64 = 0;
        let p = process::look_path(n, err);
        if err == 0 && p.len > 0 { return p; }
    }
    v = 30;
    while v >= 16 {
        for n in ["lldb-dap", "lldb-vscode"] {
            let p = str("/usr/lib/llvm-", v, "/bin/", n);
            if process::is_program(p) { return p; }
        }
        v--;
    }
    return copy_text("lldb-dap");
}

fn copy_text(s: const u8[:]) -> u8[>..] {
    var out: u8[>..] = [];
    out.append(s);
    return out;
}

// `xcrun -f lldb-dap`, given 10 s, if it names a file; "" otherwise.
fn xcrun_lldb_dap() -> u8[>..] {
    var out: u8[>..] = [];
    var c = process::Cmd { .. };
    process::args(c, ["xcrun", "-f", "lldb-dap"]);
    c.stdin = process::NULL;
    c.stdout = process::PIPE;
    c.stderr = process::NULL;
    var e: i64 = 0;
    var ch = process::start(c, e);
    if e != 0 { return out; }
    var text: u8[>..] = [];
    var st = process::Status { .. };
    let code = run_capture(ch, text, 10000, st);
    if code == 0 {
        let p = strip(text);
        if p.len > 0 && file_exists(p) { out.append(p); }
    }
    return out;
}

// Reads a started child's stdout to its end while waiting for it, up to
// timeout_ms (killed then): its exit code, or -1. As process::collect,
// with a deadline: Windows' pipes are asked how much is there rather than
// polled.
fn run_capture(ch: sys::Child&, out: u8[>..]&, timeout_ms: i64, st: process::Status&) -> i64 {
    var buf: u8[16384] = [0; 16384];
    let deadline = clock_ns() + timeout_ms * 1000000;
    var ended = ch.stdout_fd < 0;
    var timed_out = false;
    loop {
        if clock_ns() > deadline { timed_out = true; break; }
        if !ended && sys::is_windows() {
            loop {
                let a = sys::available(ch.stdout_fd);
                if a <= 0 { break; }
                let n = sys::read(ch.stdout_fd, buf[..min(a, buf.len)]);
                if n <= 0 { break; }
                out.append(buf[..n]);
            }
        } else if !ended {
            var fds: i64[1] = [ch.stdout_fd];
            var ev: i64[1] = [sys::POLL_READ];
            let p = sys::poll(fds[..], ev[..], 50);
            if p > 0 {
                let n = sys::read(ch.stdout_fd, buf[..]);
                if n > 0 { out.append(buf[..n]); } else { ended = true; }
            } else if p < 0 { ended = true; }
        }
        let r = process::wait(ch, st, if ended { 50 } else { if sys::is_windows() { 20 } else { 0 } });
        if r == 1 { break; }
        if r < 0 { break; }
    }
    if timed_out {
        process::kill(ch, sys::SIGKILL);
        process::wait(ch, st, 2000);
        process::release(ch);
        return -1;
    }
    if ch.stdout_fd >= 0 { sys::read_all(ch.stdout_fd, out); }
    process::release(ch);
    return if st.exited { st.code } else { -1 };
}

fn goose_home(out: u8[>..]&) {
    if env("GOOSE_HOME", out) && out.len > 0 { return; }
    out.clear();
    join_path(out, sys::home_dir(), ".goose");
}

// `goose build -g` of source into program: false, with the message the
// launch fails with, if it fails.
fn build(goose_arg: const u8[:], source: const u8[:], program: const u8[:], extra: json::Node?,
         cwd: const u8[:], message: u8[>..]&) -> bool {
    var goose: u8[>..] = [];
    if goose_arg.len > 0 { goose.append(goose_arg); }
    else {
        var e: u8[>..] = [];
        if env("GOOSE", e) && e.len > 0 { goose.append(e); }
        else if process::is_program(beside_goose) { goose.append(beside_goose); }
        else {
            var err: i64 = 0;
            let p = process::look_path("goose", err);
            if err == 0 && p.len > 0 { goose.append(p); } else { goose.append("goose"); }
        }
    }
    if source.len == 0 { message.append("build: true needs a \"source\" .goose file"); return false; }
    if program.len == 0 { message.append("build: true needs a \"program\" path"); return false; }
    var c = process::Cmd { .. };
    var line: u8[>..] = [];
    fn word(w: const u8[:]) {
        process::arg(c, w);
        if line.len > 0 { line.push(' '); }
        line.append(w);
    }
    word(goose);
    word("build");
    word("-g");
    if extra && is_arr(extra) {
        var x = extra.first;
        while x {
            let s, ok = json::as_string(x);
            if ok { word(s); }
            x .= x.next;
        }
    }
    word("-o");
    word(program);
    word(source);
    console(str("goose-dap: ", line, "\n"));
    if cwd.len > 0 { c.dir.append(cwd); }
    c.stdin = process::NULL;
    c.stdout = process::PIPE;
    c.stderr = process::STDOUT;
    c.default_signals = true;
    var e: i64 = 0;
    var ch = process::start(c, e);
    if e != 0 { message.append(str("goose build failed: ", sys::error_text(e))); return false; }
    var out: u8[>..] = [];
    var st = process::Status { .. };
    let code = run_capture(ch, out, 600000, st);
    if out.len > 0 {
        if out[out.len - 1] == '\n' { console(out); } else { console(str(out, "\n")); }
    }
    if code < 0 && !st.exited {
        message.append("goose build failed: timed out");
        return false;
    }
    if code != 0 {
        format(message, "goose build failed (exit ", code, ")");
        let t = strip(out);
        if t.len > 0 {
            var i = t.len;
            while i > 0 && t[i - 1] != '\n' { i--; }
            message.append(": ");
            message.append(strip(t[i..]));
        }
        return false;
    }
    return true;
}
