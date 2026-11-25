// Breakpoints: a function breakpoint on a Goose name is one on every C
// instance of it; the "Goose runtime errors" filter is a function
// breakpoint on gs_fail and gs_panic; breakpointLocations is answered from
// LLDB's line table when lldb-dap has none to give.

import std;
import json;
import gslib.sys.process;
import .jsonw;
import .names;
import .store;
import .gmap;
import .proxy;
import .lldb;
import .launch;

fn c_set_function_breakpoints(req: json::Node&) {
    let bps = json::member(json::member(req, "arguments"), "breakpoints");
    client_fbps.clear();
    client_fbps.push('[');
    if bps && is_arr(bps) {
        var b = bps.first;
        while b {
            if b && is_obj(b) && is_str(json::member(b, "name")) { sep(client_fbps); emit(client_fbps, b); }
            b .= b.next;
        }
    }
    client_fbps.push(']');
    var result: u8[>..] = [];
    if !sync_function_breakpoints(result) {
        respond_fail(req, "lldb-dap did not answer setFunctionBreakpoints");
        return;
    }
    respond_ok(req, str("{\"breakpoints\":", result, "}"));
}

// Where one of the editor's breakpoints went in what lldb-dap was sent:
// entries first..first + n.
struct Group { first: i64, n: i64 }

// Sends lldb-dap the whole set (it replaces all function breakpoints on
// each request): each of the editor's breakpoints as every C instance of
// its Goose name, plus gs_fail and gs_panic for the Goose filter. result
// gets one breakpoint per editor breakpoint, as a JSON array; false if
// lldb-dap does not answer.
fn sync_function_breakpoints(result: u8[>..]&) -> bool {
    ensure_map(false);
    var mdoc: json::Node[>..] = [];
    var mdec: u8[>..] = [];
    let m, merr = json::parse(map_text, mdoc, mdec);
    var cdoc: json::Node[>..] = [];
    var cdec: u8[>..] = [];
    let fbps, cerr = json::parse(client_fbps, cdoc, cdec);
    var out: u8[>..] = [];
    out.append("{\"breakpoints\":[");
    var count = 0;
    var groups: Group[>..] = [];
    let has_map = map_has_functions(m);
    if fbps {
        var b = fbps.first;
        while b {
            let name = strip(str_or(b, "name", ""));
            var cnames: u8[>..] = [];
            if has_map { functions_named(m, name, cnames); }
            if cnames.len == 0 { cnames.append(name); cnames.push(0); }
            let first = count;
            var pos: i64 = 0;
            while pos < cnames.len {
                let c = next_cstring(cnames, pos);
                sep(out);
                out.push('{');
                copy_except(out, b, ["name"]);
                kv_str(out, "name", c);
                out.push('}');
                count++;
            }
            groups.push(Group { first: first, n: count - first });
            b .= b.next;
        }
    }
    let rt = count;
    if goose_filter {
        for f in ["gs_fail", "gs_panic"] {
            sep(out);
            out.push('{');
            kv_str(out, "name", f);
            if has_filter_condition { kv_str(out, "condition", goose_filter_condition); }
            out.push('}');
            count++;
        }
    }
    out.append("]}");
    var r: u8[>..] = [];
    guard adapter_request("setFunctionBreakpoints", out, INTERNAL_TIMEOUT_MS, r) else { return false; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    guard request_ok(root) else { return false; }
    let got = json::member(json::member(root, "body"), "breakpoints");
    let ngot = if got && is_arr(got) { count_of(got) } else { 0 };
    runtime_bp_ids.clear();
    if goose_filter {
        for i in rt..rt + 2 {
            let g = element(got, i);
            if g && is_int(json::member(g, "id")) { runtime_bp_ids.push(get_int(g, "id", 0)); }
        }
    }
    fbp_secondary.clear();
    result.push('[');
    for g in groups {
        sep(result);
        let last = min(g.first + g.n, ngot);
        if last <= g.first {
            result.append("{\"verified\":false,\"message\":\"no answer from lldb-dap\"}");
            continue;
        }
        var prim = g.first;
        var found = false;
        for i in g.first..last {
            if !found && truthy(json::member(element(got, i), "verified")) { prim = i; found = true; }
        }
        let p = element(got, prim);
        let pid = json::member(p, "id");
        var verified = 0;
        for i in g.first..last {
            let x = element(got, i);
            if truthy(json::member(x, "verified")) { verified++; }
            let xid = json::member(x, "id");
            if i != prim && is_int(xid) && is_int(pid) {
                fbp_secondary.push(SeqPair { a: get_int(x, "id", 0), b: get_int(p, "id", 0) });
            }
        }
        result.push('{');
        if last - g.first > 1 && verified > 0 {
            copy_except(result, p, ["message"]);
            kv_str(result, "message", str(verified, " instances"));
        } else {
            copy_all(result, p);
        }
        result.push('}');
    }
    result.push(']');
    return true;
}

let GOOSE_FILTER = "goose_runtime";

fn c_set_exception_breakpoints(req: json::Node&) {
    let a = json::member(req, "arguments");
    saw_exception_bps = true;
    var n = 0;
    var on = false;
    let filters = json::member(a, "filters");
    if filters && is_arr(filters) {
        var f = filters.first;
        while f {
            let s, ok = json::as_string(f);
            if ok { n++; if s == GOOSE_FILTER { on = true; } }
            f .= f.next;
        }
    }
    var cond: u8[>..] = [];
    var has_cond = false;
    let opts = json::member(a, "filterOptions");
    if opts && is_arr(opts) {
        var o = opts.first;
        while o {
            if o && is_obj(o) {
                n++;
                if str_or(o, "filterId", "") == GOOSE_FILTER {
                    on = true;
                    let c = json::member(o, "condition");
                    cond.clear();
                    has_cond = false;
                    if truthy(c) && is_str(c) {
                        cond.append(str_or(o, "condition", ""));
                        has_cond = true;
                    }
                }
            }
            o .= o.next;
        }
    }
    goose_filter = on;
    goose_filter_condition.clear();
    goose_filter_condition.append(cond);
    has_filter_condition = has_cond;
    // lldb-dap's own (C++, ObjC, Swift) filters are not offered, so
    // nothing goes to it.
    var ignored: u8[>..] = [];
    sync_function_breakpoints(ignored);
    var body: u8[>..] = [];
    body.append("{\"breakpoints\":[");
    for i in n { sep(body); body.append("{\"verified\":true}"); }
    body.append("]}");
    respond_ok(req, body);
}

fn c_configuration_done(req: json::Node&, text: const u8[:]) {
    if !saw_exception_bps && stop_on_runtime_error && !goose_filter {
        // A client that sends no setExceptionBreakpoints (IntelliJ's DAP
        // client without an exception breakpoint type) still stops on
        // runtime errors, unless the launch says stopOnRuntimeError: false.
        goose_filter = true;
        var ignored: u8[>..] = [];
        sync_function_breakpoints(ignored);
    }
    forward(req, text, "", "");
}

fn c_breakpoint_locations(req: json::Node&, text: const u8[:]) {
    if adapter_has("supportsBreakpointLocationsRequest") {
        forward(req, text, "", "{\"kind\":\"bplocs\"}");
        return;
    }
    // lldb-vscode and lldb-dap 18 have no breakpointLocations, and a
    // request they do not know ends them: answer from the line table.
    var body: u8[>..] = [];
    body.append("{\"breakpoints\":");
    breakpoint_locations(req, body);
    body.push('}');
    respond_ok(req, body);
}

// The lines of the request's range that have code, as a JSON array of
// {line} onto out ([] when LLDB knows none).
fn breakpoint_locations(req: json::Node?, out: u8[>..]&) {
    let a = json::member(req, "arguments");
    let path = str_or(json::member(a, "source"), "path", "");
    var lines: u8[>..] = [];
    out.push('[');
    if path.len > 0 && source_lines(path, lines) {
        let lo = if truthy(json::member(a, "line")) { get_int(a, "line", 1) } else { 1 };
        let hi = if truthy(json::member(a, "endLine")) { get_int(a, "endLine", lo) } else { lo };
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let ls, err = json::parse(lines, doc, dec);
        if ls {
            var n = ls.first;
            while n {
                let v = int_value(n, 0);
                if lo <= v && v <= hi { sep(out); format(out, "{\"line\":", v, "}"); }
                n .= n.next;
            }
        }
    }
    out.push(']');
}
