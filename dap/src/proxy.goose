// The proxy's state and its plumbing: the log, writing to the editor and
// to lldb-dap, lldb-dap itself (started, stopped, respawned), requests of
// the proxy's own to lldb-dap, and the main loop that hands each message
// to its handler.
//
//     editor  <--DAP/stdio-->  goose-dap  <--DAP/pipes-->  lldb-dap
//
// Main does everything but reading. A request of the proxy's own is sent,
// and main then takes messages off the queue until its answer comes; what
// comes meanwhile is held, in order, for the main loop (except reverse
// requests and the editor's answers to them, which go on at once, as the
// Python adapter's reader threads sent them).

import std;
import os;
import json;
import gslib.sys;
import gslib.sys.process;
import .framing;
import .jsonw;
import .store;

let INTERNAL_TIMEOUT_MS: i64 = 15000;
let LOG_CLIP: i64 = 4000;

// --- State ------------------------------------------------------------------

var log_fd: i64 = -1;
var in_fd: i64 = 0;
var out_fd: i64 = 1;

var child = sys::Child { pid: 0, handle: 0, stdin_fd: -1, stdout_fd: -1, stderr_fd: -1, pty: 0 };
var child_alive = false;
var child_gen: i64 = 0;
var child_path: u8[>..];
var cli_lldb_dap: u8[>..];

var aseq: i64 = 0;             // seqs of messages sent to lldb-dap
var cseq: i64 = 0;             // seqs of messages sent to the editor

// The editor's requests that lldb-dap has yet to answer, by the seq they
// went to lldb-dap with: the request's text and the context its answer is
// rewritten with, end to end in pending_text. aseq < 0: answered.
struct Pending { aseq: i64, at: i64, len: i64, cat: i64, clen: i64 }
var pending: Pending[>..];
var pending_text: u8[>..];
var pending_live: i64 = 0;

// Reverse requests (runInTerminal, startDebugging): the seq the editor got
// one with, and lldb-dap's. a < 0: answered.
struct SeqPair { a: i64, b: i64 }
var reverse: SeqPair[>..];

// Messages that came while main waited for an answer of its own.
struct Held { src: i32, gen: i64, eof: bool, at: i64, len: i64 }
var held: Held[>..];
var held_bytes: u8[>..];
var hhead: i64 = 0;

// The debug maps: a JSON array of the documents.
var map_text: u8[>..];
var map_loaded = false;
// The compile units LLDB has, for their line tables (lldb_info).
var cu_paths: u8[>..];         // NUL-ended

var escape: u8[>..];
var init_args: u8[>..];        // JSON; empty until initialize
var adapter_caps: u8[>..];     // lldb-dap's own capabilities, JSON
var launch_args: u8[>..];      // JSON
var show_runtime_frames = false;
var stop_on_runtime_error = true;
var saw_exception_bps = false;
var client_fbps: u8[>..];      // the editor's function breakpoints, a JSON array
var goose_filter = false;
var goose_filter_condition: u8[>..];
var has_filter_condition = false;
var fbp_secondary: SeqPair[>..];   // lldb-dap breakpoint id -> the editor's
var runtime_bp_ids: i64[>..];
var terminated_sent = false;
var disconnecting = false;
var client_gone = false;
var seen_stop = false;

var line_cache = Store { .. };     // path -> lines, a JSON array
// Per stop: frame id -> info; thread id -> its rewritten frames;
// variablesReference -> what it lists; thread id -> its runtime error.
var frames = Store { .. };
var thread_frames = Store { .. };
var refs = Store { .. };
var exc = Store { .. };

// Where things are: the directory of goose_lldb.py, the goose_lldb.py to
// load, and the compiler beside the adapter.
var goose_lldb_path: u8[>..];
var beside_goose: u8[>..];
var exe_suffix: const u8[:] = "";

fn reset_stop_state() {
    store_clear(frames);
    store_clear(thread_frames);
    store_clear(refs);
}

// --- Logging ----------------------------------------------------------------

fn log(what: const u8[:], text: const u8[:]) {
    guard log_fd >= 0 else { return; }
    var line: u8[>..] = [];
    let ns = time_ns();
    format(line, ns / 1000000000, ".");
    format_int(line, (ns / 1000000) % 1000, 10, 3, '0');
    line.push(' ');
    line.append(what);
    if text.len > 0 {
        line.push(' ');
        line.append(text[..min(text.len, LOG_CLIP)]);
    }
    line.push('\n');
    sys::write_all(log_fd, line);
}

fn log_open(path: const u8[:]) {
    guard path.len > 0 else { return; }
    let fd = sys::open(path, sys::O_WRONLY | sys::O_CREATE | sys::O_APPEND, 420);
    if fd >= 0 { log_fd = fd; }
}

// --- Writing ----------------------------------------------------------------

// obj, an object without a seq, with seq put first.
fn with_seq(out: u8[>..]&, obj: const u8[:], seq: i64) {
    out.append("{\"seq\":");
    format(out, seq);
    if obj.len > 2 { out.push(','); out.append(obj[1..]); } else { out.push('}'); }
}

fn send_client(obj: const u8[:]) {
    cseq += 1;
    var m: u8[>..] = [];
    with_seq(m, obj, cseq);
    log("<-C", m);
    if !write_fd(out_fd, frame(m)) { client_gone = true; }
}

// A message to lldb-dap under a new seq: the seq, or -1 if it cannot be
// written (no lldb-dap, or it has gone).
fn send_adapter(obj: const u8[:], tag: const u8[:]) -> i64 {
    aseq += 1;
    let seq = aseq;
    var m: u8[>..] = [];
    with_seq(m, obj, seq);
    log(tag, m);
    guard child_alive else { return -1; }
    if !write_fd(child.stdin_fd, frame(m)) { return -1; }
    return seq;
}

// A response to the editor's request req.
fn respond(req: json::Node?, body: const u8[:], success: bool, message: const u8[:]) {
    var r: u8[>..] = [];
    r.append("{\"type\":\"response\"");
    key(r, "request_seq");
    emit_or_null(r, json::member(req, "seq"));
    key(r, "command");
    emit_or_null(r, json::member(req, "command"));
    kv_bool(r, "success", success);
    if body.len > 0 { kv_raw(r, "body", body); }
    if message.len > 0 { kv_str(r, "message", message); }
    r.push('}');
    send_client(r);
}

fn respond_ok(req: json::Node?, body: const u8[:]) { respond(req, body, true, ""); }
fn respond_fail(req: json::Node?, message: const u8[:]) { respond(req, "", false, message); }

fn event(name: const u8[:], body: const u8[:]) {
    var e: u8[>..] = [];
    e.append("{\"type\":\"event\"");
    kv_str(e, "event", name);
    if body.len > 0 { kv_raw(e, "body", body); }
    e.push('}');
    send_client(e);
}

fn console(text: const u8[:]) {
    var b: u8[>..] = [];
    b.append("{\"category\":\"console\"");
    kv_str(b, "output", text);
    b.push('}');
    event("output", b);
}

// Sends the editor's request on to lldb-dap; its answer comes back through
// on_adapter_response with ctx. args: the arguments to send instead of the
// request's own ("" for those).
fn forward(req: json::Node&, req_text: const u8[:], args: const u8[:], ctx: const u8[:]) {
    var m: u8[>..] = [];
    m.append("{\"type\":\"request\"");
    key(m, "command");
    emit_or_null(m, json::member(req, "command"));
    if args.len > 0 { kv_raw(m, "arguments", args); }
    else {
        let a = json::member(req, "arguments");
        if a { kv_node(m, "arguments", a); }
    }
    m.push('}');
    let seq = send_adapter(m, "->A");
    if seq < 0 {
        respond_fail(req, "lldb-dap is not running");
        return;
    }
    let at = pending_text.len;
    pending_text.append(req_text);
    let cat = pending_text.len;
    pending_text.append(ctx);
    pending.push(Pending { aseq: seq, at: at, len: req_text.len, cat: cat, clen: ctx.len });
    pending_live += 1;
}

// The request and context lldb-dap's answer to seq belongs to, appended to
// req and ctx; false if none is pending.
fn pending_pop(seq: i64, req: u8[>..]&, ctx: u8[>..]&) -> bool {
    for &p in pending {
        if p.aseq == seq {
            req.append(pending_text[p.at..p.at + p.len]);
            ctx.append(pending_text[p.cat..p.cat + p.clen]);
            p.aseq = -1;
            pending_live -= 1;
            return true;
        }
    }
    return false;
}

// Forgets answered requests once none is left.
fn pending_compact() {
    if pending_live == 0 && pending.len > 0 {
        pending.clear();
        pending_text.clear();
    }
}

// --- Requests of the proxy's own ----------------------------------------------------

// A request to lldb-dap that main waits for: its response (the whole
// message) appended to out, or false when lldb-dap does not answer within
// timeout_ms, cannot be written to, or exits.
fn adapter_request(command: const u8[:], args: const u8[:], timeout_ms: i64, out: u8[>..]&) -> bool {
    var m: u8[>..] = [];
    m.append("{\"type\":\"request\"");
    kv_str(m, "command", command);
    if args.len > 0 { kv_raw(m, "arguments", args); }
    m.push('}');
    let seq = send_adapter(m, "->A*");
    guard seq >= 0 else { return false; }
    let gen = child_gen;
    let deadline = clock_ns() + timeout_ms * 1000000;
    loop {
        let left = (deadline - clock_ns()) / 1000000;
        guard left > 0 else { log("timed out waiting for", command); return false; }
        let r, got = qget<Msg>(left);
        guard got else { log("timed out waiting for", command); return false; }
        if r.src == SRC_CHILD && r.gen != gen { continue; }
        if r.src == SRC_CHILD_ERR { log_stderr(r.gen, r.body); continue; }
        if r.src == SRC_CHILD && r.eof {
            hold(r.src, r.gen, r.eof, r.body);
            return false;
        }
        if r.src == SRC_CHILD {
            log("<-A", r.body);
            var doc: json::Node[>..] = [];
            var dec: u8[>..] = [];
            let root, err = json::parse(r.body, doc, dec);
            let t = str_or(root, "type", "");
            if t == "response" && get_int(root, "request_seq", -1) == seq && is_int(json::member(root, "request_seq")) {
                out.append(r.body);
                return true;
            }
            if t == "request" {
                if root { forward_reverse(root); }
                continue;
            }
            hold(r.src, r.gen, r.eof, r.body);
            continue;
        }
        if r.src == SRC_CLIENT && !r.eof {
            log("C->", r.body);
            var doc: json::Node[>..] = [];
            var dec: u8[>..] = [];
            let root, err = json::parse(r.body, doc, dec);
            if root && str_or(root, "type", "") == "response" {
                reverse_response(root);
                continue;
            }
            hold(r.src, r.gen, r.eof, r.body);
            continue;
        }
        hold(r.src, r.gen, r.eof, r.body);
    }
}

// The response of an adapter_request, parsed: whether it succeeded.
fn request_ok(root: json::Node?) -> bool { truthy(json::member(root, "success")) }

fn hold(src: i32, gen: i64, eof: bool, body: const u8[:]) {
    held.push(Held { src: src, gen: gen, eof: eof, at: held_bytes.len, len: body.len });
    held_bytes.append(body);
}

// A reverse request from lldb-dap, to the editor under a seq of the
// editor's; its answer is matched back in reverse_response.
fn forward_reverse(msg: json::Node&) {
    cseq += 1;
    let seq = cseq;
    reverse.push(SeqPair { a: seq, b: get_int(msg, "seq", 0) });
    var m: u8[>..] = [];
    m.append("{\"seq\":");
    format(m, seq);
    copy_except(m, msg, ["seq"]);
    m.push('}');
    log("<-C", m);
    if !write_fd(out_fd, frame(m)) { client_gone = true; }
}

// The editor's answer to a reverse request, to lldb-dap with lldb-dap's seq.
fn reverse_response(msg: json::Node&) {
    let rs = json::member(msg, "request_seq");
    var m: u8[>..] = [];
    m.push('{');
    copy_except(m, msg, ["seq", "request_seq"]);
    key(m, "request_seq");
    var mapped = false;
    if is_int(rs) {
        let s = get_int(msg, "request_seq", 0);
        for &p in reverse {
            if p.a == s && !mapped {
                format(m, p.b);
                p.a = -1;
                mapped = true;
            }
        }
    }
    if !mapped { emit_or_null(m, rs); }
    m.push('}');
    send_adapter(m, "->A");
}

fn log_stderr(gen: i64, line: const u8[:]) {
    var t: u8[>..] = [];
    t.append(line);
    log("lldb-dap stderr:", t);
}

// --- lldb-dap -------------------------------------------------------------------

// Starts lldb-dap at path, and the workers that read it: false if it
// cannot be started.
fn start_child(path: const u8[:]) -> bool {
    log("spawn", path);
    child_path.clear();
    child_path.append(path);
    var c = process::Cmd { .. };
    process::arg(c, path);
    c.stdin = process::PIPE;
    c.stdout = process::PIPE;
    c.stderr = process::PIPE;
    // As Python's subprocess: SIGPIPE, which this process ignores, is the
    // default again in lldb-dap.
    c.default_signals = true;
    var e: i64 = 0;
    let ch = process::start(c, e);
    if e != 0 {
        log("cannot start lldb-dap:", sys::error_text(e));
        return false;
    }
    child = ch;
    child_gen += 1;
    child_alive = true;
    thread_spawn(reader, child.stdout_fd, SRC_CHILD, child_gen, true);
    thread_spawn(line_reader, child.stderr_fd, SRC_CHILD_ERR, child_gen);
    // The readers own those two now, and close them at their end.
    child.stdout_fd = -1;
    child.stderr_fd = -1;
    return true;
}

// Ends lldb-dap: its stdin closed, grace_ms for it to go, then killed.
fn stop_child(grace_ms: i64) {
    guard child_alive else { return; }
    child_alive = false;
    if child.stdin_fd >= 0 {
        sys::close(child.stdin_fd);
        child.stdin_fd = -1;
    }
    var st = process::Status { .. };
    if process::wait(child, st, grace_ms) == 0 {
        process::kill(child, sys::SIGTERM);
        if process::wait(child, st, 2000) == 0 {
            process::kill(child, sys::SIGKILL);
            process::wait(child, st, 2000);
        }
    }
    process::release(child);
}

// Whether lldb-dap still runs.
fn child_running() -> bool {
    guard child_alive else { return false; }
    var st = process::Status { .. };
    return process::wait(child, st, 0) == 0;
}

// --- The main loop --------------------------------------------------------------

fn run() {
    var body: u8[>..] = [];
    loop {
        if hhead == held.len && hhead > 0 { held.clear(); held_bytes.clear(); hhead = 0; }
        pending_compact();
        var src: i32 = SRC_CLIENT;
        var gen: i64 = 0;
        var eof = false;
        var fresh = true;
        body.clear();
        if hhead < held.len {
            let h = held[hhead];
            hhead++;
            src = h.src;
            gen = h.gen;
            eof = h.eof;
            fresh = false;
            body.append(held_bytes[h.at..h.at + h.len]);
        } else {
            let m = qget<Msg>();
            src = m.src;
            gen = m.gen;
            eof = m.eof;
            body.append(m.body);
        }
        if src == SRC_SIGNAL {
            // How nvim-dap ends every session (after closing stdin): a
            // normal end.
            shutdown();
            return;
        }
        if src == SRC_CHILD_ERR { log_stderr(gen, body); continue; }
        if src == SRC_CLIENT {
            if eof { shutdown(); return; }
            if fresh { log("C->", body); }
            on_client_message(body);
            continue;
        }
        // lldb-dap: an earlier one's are dropped.
        if gen != child_gen { continue; }
        if eof { adapter_eof(); continue; }
        if fresh { log("<-A", body); }
        on_adapter_message(body);
    }
}

fn on_client_message(body: const u8[:]) {
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(body, doc, dec);
    guard root else { log("client message is not JSON:", err); return; }
    let t = str_or(root, "type", "");
    if t == "response" {
        // The editor's answer to a reverse request.
        reverse_response(root);
        return;
    }
    if t != "request" { return; }
    on_client(root, body);
}

fn on_adapter_message(body: const u8[:]) {
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(body, doc, dec);
    guard root else { log("lldb-dap message is not JSON:", err); return; }
    let t = str_or(root, "type", "");
    if t == "response" { on_adapter_response(root); return; }
    if t == "request" {
        // A reverse request: straight to the editor.
        forward_reverse(root);
        return;
    }
    if t == "event" {
        var out: u8[>..] = [];
        if rewrite_event(root, out) { send_client(out); }
    }
}

// lldb-dap has exited.
fn adapter_eof() {
    log("lldb-dap exited", "");
    if !terminated_sent && !client_gone {
        terminated_sent = true;
        event("terminated", "");
    }
    // Answer whatever lldb-dap left unanswered.
    for p, i in pending {
        if p.aseq < 0 { continue; }
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let req, err = json::parse(pending_text[p.at..p.at + p.len], doc, dec);
        if str_or(req, "command", "") == "disconnect" { respond_ok(req, ""); }
        else { respond_fail(req, "lldb-dap exited"); }
    }
    for &p in pending { p.aseq = -1; }
    pending_live = 0;
    // Reaped last, so that the editor hears at once.
    stop_child(3000);
}

fn shutdown() {
    if child_running() && !disconnecting {
        var r: u8[>..] = [];
        adapter_request("disconnect", "{\"terminateDebuggee\":true}", 5000, r);
    }
    stop_child(3000);
}
