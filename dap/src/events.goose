// lldb-dap's responses and events on their way to the editor: a response is
// matched to the editor's request and rewritten by the context it was sent
// with; events are rewritten (stopped at a runtime error is an exception),
// passed, or dropped.

import std;
import json;
import .jsonw;
import .names;
import .store;
import .proxy;
import .launch;
import .lldb;
import .breakpoints;
import .variables;
import .evaluate;

fn on_adapter_response(m: json::Node&) {
    let rs = json::member(m, "request_seq");
    var req_t: u8[>..] = [];
    var ctx_t: u8[>..] = [];
    if !is_int(rs) || !pending_pop(get_int(m, "request_seq", 0), req_t, ctx_t) {
        var s: u8[>..] = [];
        emit_or_null(s, rs);
        log("unmatched response", s);
        return;
    }
    var rdoc: json::Node[>..] = [];
    var rdec: u8[>..] = [];
    let req, rerr = json::parse(req_t, rdoc, rdec);
    var cdoc: json::Node[>..] = [];
    var cdec: u8[>..] = [];
    let ctx, cerr = json::parse(ctx_t, cdoc, cdec);
    var out: u8[>..] = [];
    rewrite_response(req, ctx, m, out);
    send_client(out);
}

// Whether a failed evaluation ran nothing: it did not parse.
fn parse_failure(message: const u8[:]) -> bool {
    return find(message, "failed to parse") >= 0 || find(message, "undeclared identifier") >= 0;
}

// Another evaluate for a failed one: true, with its body (or null) onto
// body, if it succeeds.
fn evaluate_again(args: json::Node?, body: u8[>..]&) -> bool {
    var a: u8[>..] = [];
    emit_or_null(a, args);
    var r: u8[>..] = [];
    guard adapter_request("evaluate", a, INTERNAL_TIMEOUT_MS, r) else { return false; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    guard request_ok(root) else { return false; }
    body.clear();
    emit_or_null(body, json::member(root, "body"));
    return true;
}

fn rewrite_response(req: json::Node?, ctx: json::Node?, m: json::Node&, out: u8[>..]&) {
    var success = truthy(json::member(m, "success"));
    var drop_message = false;
    let mb = json::member(m, "body");
    var has_body = mb != null;
    var body: u8[>..] = [];
    if mb { emit(body, mb); }
    let kind = str_or(ctx, "kind", "");
    var done = false;
    if !success && kind == "evaluate" && truthy(json::member(ctx, "alt")) {
        // The C spelling failed; LLDB's variable path through the
        // formatters' Goose-named children may still get there.
        if evaluate_again(json::member(ctx, "alt"), body) {
            success = true;
            drop_message = true;
            has_body = true;
        }
    }
    if !success && kind == "evaluate" && truthy(json::member(ctx, "retry"))
       && parse_failure(str_or(m, "message", "")) {
        // A parse failure ran nothing, so the line can go again as a command.
        if evaluate_again(json::member(ctx, "retry"), body) {
            success = true;
            drop_message = true;
            has_body = true;
            done = true;
        }
    }
    var new_body: u8[>..] = [];
    if success && !done && has_body {
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let b, err = json::parse(body, doc, dec);
        if kind == "initialize" {
            adapter_caps.clear();
            if b && is_obj(b) { emit(adapter_caps, b); } else { adapter_caps.append("{}"); }
            rewrite_capabilities(new_body, b);
        } else if kind == "scopes" {
            rewrite_scopes(json::member(ctx, "frame"), b, new_body);
        } else if kind == "variables" {
            rewrite_variables(req, json::member(ctx, "ref"), b, new_body);
        } else if kind == "evaluate" {
            rewrite_evaluate(ctx, b, new_body);
        } else if kind == "setVariable" && is_obj(b) {
            // lldb-dap 18 answers with "result" where the protocol has "value".
            new_body.push('{');
            copy_except(new_body, b, ["value", "result"]);
            let v = if json::member(b, "value") { json::member(b, "value") } else { json::member(b, "result") };
            key(new_body, "value");
            let s, ok = json::as_string(v);
            if ok {
                var c: u8[>..] = [];
                clean_value(c, s, str_or(b, "type", ""), "");
                json::format_string(new_body, c);
            } else { emit_or_null(new_body, v); }
            new_body.push('}');
        } else if kind == "bplocs" && is_obj(b) && !truthy(json::member(b, "breakpoints")) {
            new_body.push('{');
            copy_except(new_body, b, ["breakpoints"]);
            key(new_body, "breakpoints");
            breakpoint_locations(req, new_body);
            new_body.push('}');
        }
    }
    out.push('{');
    copy_except(out, m, ["seq", "request_seq", "success", "message", "body"]);
    key(out, "request_seq");
    emit_or_null(out, json::member(req, "seq"));
    kv_bool(out, "success", success);
    let msg = json::member(m, "message");
    if msg && !drop_message { kv_node(out, "message", msg); }
    if new_body.len > 0 { kv_raw(out, "body", new_body); }
    else if has_body { kv_raw(out, "body", body); }
    out.push('}');
}

// An event as the editor gets it, onto out; false to drop it.
fn rewrite_event(e: json::Node&, out: u8[>..]&) -> bool {
    let name = str_or(e, "event", "");
    let body = obj_member(e, "body");
    var new_body: u8[>..] = [];
    if name == "initialized" {
        ensure_map(true);
    } else if name == "stopped" {
        reset_stop_state();
        let first = !seen_stop;
        seen_stop = true;
        if body { rewrite_stopped(body, first, new_body); }
    } else if name == "continued" {
        reset_stop_state();
        store_clear(exc);
    } else if name == "breakpoint" && body {
        let bid = json::member(json::member(body, "breakpoint"), "id");
        if is_int(bid) {
            let id = get_int(json::member(body, "breakpoint"), "id", 0);
            for r in runtime_bp_ids { if r == id { return false; } }
            for p in fbp_secondary { if p.a == id { return false; } }
        }
    } else if name == "terminated" {
        if terminated_sent { return false; }
        terminated_sent = true;
    } else if name == "capabilities" && body && is_obj(json::member(body, "capabilities")) {
        let caps = json::member(body, "capabilities");
        // lldb-dap's capabilities, updated.
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let old, err = json::parse(adapter_caps, doc, dec);
        var merged: u8[>..] = [];
        merged.push('{');
        if old {
            var c = old.first;
            while c {
                if json::member(caps, c.key) == null { sep(merged); json::format_key(merged, c.key); emit(merged, c); }
                c .= c.next;
            }
        }
        copy_all(merged, caps);
        merged.push('}');
        adapter_caps.clear();
        adapter_caps.append(merged);
        new_body.push('{');
        copy_except(new_body, body, ["capabilities"]);
        key(new_body, "capabilities");
        new_body.push('{');
        copy_except(new_body, caps, ["supportsCompletionsRequest", "supportsCancelRequest", "exceptionBreakpointFilters"]);
        new_body.push('}');
        new_body.push('}');
    }
    out.push('{');
    if new_body.len > 0 {
        copy_except(out, e, ["seq", "body"]);
        kv_raw(out, "body", new_body);
    } else {
        copy_except(out, e, ["seq"]);
    }
    out.push('}');
    return true;
}

fn launch_stop_on_entry() -> bool {
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let la, err = json::parse(launch_args, doc, dec);
    return truthy(json::member(la, "stopOnEntry"));
}

// A stopped event's body, onto out if it changes.
fn rewrite_stopped(body: json::Node&, first: bool, out: u8[>..]&) {
    let reason = str_or(body, "reason", "");
    if first && launch_stop_on_entry() && !truthy(json::member(body, "hitBreakpointIds")) && reason == "exception" {
        // lldb-dap 21 reports the stop at entry (a SIGSTOP) as an exception.
        out.push('{');
        copy_except(out, body, ["reason", "description"]);
        kv_str(out, "reason", "entry");
        out.push('}');
        return;
    }
    var hit: i64[>..] = [];
    let hb = json::member(body, "hitBreakpointIds");
    if hb && is_arr(hb) {
        var h = hb.first;
        while h { if is_int(h) { hit.push(int_value(h, 0)); } h .= h.next; }
    }
    if hb == null && (reason == "breakpoint" || reason == "function breakpoint") {
        // lldb-vscode 16 has no hitBreakpointIds, only its description,
        // "breakpoint 2.1" (breakpoint 2, location 1).
        breakpoint_ids(str_or(body, "description", ""), hit);
    }
    let tid = get_int(body, "threadId", 0);
    var runtime = false;
    for h in hit { for r in runtime_bp_ids { if h == r { runtime = true; } } }
    if runtime {
        var et: u8[>..] = [];
        if !runtime_error(tid, et) { et.clear(); et.append("{}"); }
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let err, perr = json::parse(et, doc, dec);
        var msg: u8[>..] = [];
        let m = str_or(err, "message", "");
        msg.append(if m.len > 0 { m } else { "Goose runtime error" });
        let file = str_or(err, "file", "");
        if file.len > 0 {
            format(msg, " (", basename(file), ":");
            let l = json::member(err, "line");
            if l { emit(msg, l); } else { msg.append("None"); }
            msg.push(')');
        }
        var x: u8[>..] = [];
        x.push('{');
        kv_str(x, "message", msg);
        kv_raw(x, "error", et);
        x.push('}');
        store_put(exc, tid, x);
        out.push('{');
        copy_except(out, body, ["reason", "description", "text", "hitBreakpointIds"]);
        kv_str(out, "reason", "exception");
        kv_str(out, "description", str("Goose runtime error: ", msg));
        kv_str(out, "text", msg);
        out.push('}');
        return;
    }
    if hit.len > 0 {
        var ids: i64[>..] = [];
        for h in hit {
            var id = h;
            for p in fbp_secondary { if p.a == h { id = p.b; } }
            ids.push(id);
        }
        sort(ids);
        out.push('{');
        copy_except(out, body, ["hitBreakpointIds"]);
        key(out, "hitBreakpointIds");
        out.push('[');
        for id, i in ids {
            if i > 0 && ids[i - 1] == id { continue; }
            sep(out);
            format(out, id);
        }
        out.push(']');
        out.push('}');
    }
}
