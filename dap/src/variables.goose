// scopes and variables: a Goose frame's Locals are the map's locals in
// scope at its line, under their Goose names, in the order they are
// declared, with Goose types and Goose paths as evaluateNames; children
// get Goose names and paths; Globals are the program's Goose globals.
// setVariable turns a Goose name back into the C one; exceptionInfo
// answers a runtime-error stop.
//
// What a variablesReference lists is kept in refs as a JSON object:
// {"kind": "locals" or "globals", "frame": info} or {"kind": "child",
// "prefix": evaluateName, "gtype": Goose type}, and, once listed,
// "names": {Goose name: C name}.

import std;
import json;
import .jsonw;
import .names;
import .store;
import .gmap;
import .proxy;
import .lldb;

fn c_scopes(req: json::Node&, text: const u8[:]) {
    let fid = get_int(json::member(req, "arguments"), "frameId", 0);
    var info: u8[>..] = [];
    var ctx: u8[>..] = [];
    ctx.append("{\"kind\":\"scopes\",\"frame\":");
    if is_int(json::member(json::member(req, "arguments"), "frameId")) && store_get(frames, fid, info) { ctx.append(info); }
    else { ctx.append("null"); }
    ctx.push('}');
    forward(req, text, "", ctx);
}

// The context a variablesReference was given: {"kind": kind, "frame": info}.
fn frame_ctx(out: u8[>..]&, kind: const u8[:], info: json::Node&) {
    out.push('{');
    kv_str(out, "kind", kind);
    kv_node(out, "frame", info);
    out.push('}');
}

fn rewrite_scopes(info: json::Node?, body: json::Node?, out: u8[>..]&) {
    if !truthy(info) || !truthy(json::member(info, "goose")) || !is_obj(body) {
        emit_or_null(out, body);
        return;
    }
    guard info else { return; }
    out.push('{');
    copy_except(out, body, ["scopes"]);
    let scopes = json::member(body, "scopes");
    if scopes {
        key(out, "scopes");
        if !is_arr(scopes) { emit(out, scopes); }
        else {
            out.push('[');
            var s = scopes.first;
            while s {
                sep(out);
                let name = str_or(s, "name", "");
                let ref = json::member(s, "variablesReference");
                var kind: const u8[:] = "";
                if truthy(ref) && is_int(ref) {
                    if str_or(s, "presentationHint", "") == "locals" || name == "Locals" { kind = "locals"; }
                    else if name == "Globals" { kind = "globals"; }
                }
                if kind.len > 0 {
                    var c: u8[>..] = [];
                    frame_ctx(c, kind, info);
                    store_put(refs, get_int(s, "variablesReference", 0), c);
                    out.push('{');
                    copy_except(out, s, ["namedVariables", "indexedVariables"]);
                    out.push('}');
                } else {
                    emit(out, s);
                }
                s .= s.next;
            }
            out.push(']');
        }
    }
    out.push('}');
}

fn c_variables(req: json::Node&, text: const u8[:]) {
    let ref = get_int(json::member(req, "arguments"), "variablesReference", 0);
    var ctx_t: u8[>..] = [];
    let known = is_int(json::member(json::member(req, "arguments"), "variablesReference")) && store_get(refs, ref, ctx_t);
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let ctx, err = json::parse(ctx_t, doc, dec);
    if known && str_or(ctx, "kind", "") == "globals" {
        if ctx { goose_globals(req, ref, ctx); }
        return;
    }
    var fwd: u8[>..] = [];
    fwd.append("{\"kind\":\"variables\",\"ref\":");
    if known { fwd.append(ctx_t); } else { fwd.append("null"); }
    fwd.push('}');
    forward(req, text, "", fwd);
}

// A ref's context with the names it lists added.
fn put_ref_with_names(ref: i64, ctx: json::Node&, names: const u8[:]) {
    var c: u8[>..] = [];
    c.push('{');
    copy_except(c, ctx, ["names"]);
    kv_raw(c, "names", names);
    c.push('}');
    store_put(refs, ref, c);
}

// The context of a value's children.
fn child_ctx(out: u8[>..]&, prefix: const u8[:], gtype: const u8[:]) {
    out.append("{\"kind\":\"child\"");
    if prefix.len > 0 { kv_str(out, "prefix", prefix); } else { kv_null(out, "prefix"); }
    if gtype.len > 0 { kv_str(out, "gtype", gtype); } else { kv_null(out, "gtype"); }
    out.push('}');
}

// A variable as the editor gets it, onto out: its Goose name, its
// evaluateName (none when evaluate_name is ""), its value cleaned, its
// Goose type when known; what it lists remembered.
fn goose_var(out: u8[>..]&, v: json::Node&, gname: const u8[:], evaluate_name: const u8[:], gtype: const u8[:]) {
    out.push('{');
    copy_except(out, v, ["name", "evaluateName", "value", "type", "declarationLocationReference"]);
    kv_str(out, "name", gname);
    if evaluate_name.len > 0 { kv_str(out, "evaluateName", evaluate_name); }
    put_value(out, json::member(v, "value"), str_or(v, "type", ""), gtype);
    if gtype.len > 0 { kv_str(out, "type", gtype); }
    else {
        let t = json::member(v, "type");
        if t { kv_node(out, "type", t); }
    }
    out.push('}');
    let ref = json::member(v, "variablesReference");
    if truthy(ref) && is_int(ref) {
        var c: u8[>..] = [];
        child_ctx(c, evaluate_name, gtype);
        store_put(refs, get_int(v, "variablesReference", 0), c);
    }
}

// "value": the value cleaned, if it is text.
fn put_value(out: u8[>..]&, value: json::Node?, ctype: const u8[:], gtype: const u8[:]) {
    guard value else { return; }
    key(out, "value");
    let s, ok = json::as_string(value);
    if ok {
        var c: u8[>..] = [];
        clean_value(c, s, ctype, gtype);
        json::format_string(out, c);
    } else { emit(out, value); }
}

// One listed variable: its text in an arena, the line its local was
// declared on, and its place in lldb-dap's order.
struct Listed { decl: i64, i: i64, at: i64, len: i64 }

fn rewrite_variables(req: json::Node?, ctx: json::Node?, body: json::Node?, out: u8[>..]&) {
    if !is_obj(ctx) || !is_obj(body) { emit_or_null(out, body); return; }
    guard ctx else { return; }
    let kind = str_or(ctx, "kind", "");
    if kind != "locals" && kind != "child" { emit_or_null(out, body); return; }
    var mdoc: json::Node[>..] = [];
    var mdec: u8[>..] = [];
    let m, merr = json::parse(map_text, mdoc, mdec);
    let vs = json::member(body, "variables");
    let ref = get_int(json::member(req, "arguments"), "variablesReference", 0);
    var names: u8[>..] = [];
    names.push('{');
    var arena: u8[>..] = [];
    var vals: Listed[>..] = [];
    var parens: Listed[>..] = [];
    var any_declared = false;
    if kind == "locals" {
        let info = json::member(ctx, "frame");
        let cname = str_or(info, "cname", "");
        let has_map = map_has_functions(m);
        let fnode = function_by_c(m, cname);
        let lnode = json::member(info, "line");
        let has_line = is_int(lnode);
        let line = int_value(lnode, 0);
        if vs && is_arr(vs) {
            var v = vs.first;
            var i = 0;
            while v {
                let c = str_or(v, "name", "");
                let at = arena.len;
                if starts_with(c, "(") {
                    // (Return Value) after a step out.
                    emit(arena, v);
                    parens.push(Listed { decl: 0, i: i, at: at, len: arena.len - at });
                } else if fnode {
                    let l = scope_local_by_c(fnode, c, line, has_line);
                    if l {
                        let gname = str_or(l, "name", "");
                        goose_var(arena, v, gname, gname, str_or(l, "type", ""));
                        vals.push(Listed { decl: get_int(l, "line", 0), i: i, at: at, len: arena.len - at });
                        any_declared = true;
                        kv_str(names, gname, c);
                    }
                } else if !starts_with(c, "gs_") && is_goose_cname(c) {
                    let gname = goose_name(c);
                    goose_var(arena, v, gname, gname, "");
                    vals.push(Listed { decl: 0, i: i, at: at, len: arena.len - at });
                    kv_str(names, gname, c);
                }
                i++;
                v .= v.next;
            }
        }
        // In the order of the Goose declarations, as the map gives them
        // (parameters, on the function's line, first): lldb-dap gives the
        // C's, which a PDB need not keep (MSVC listed a later struct local
        // first). Locals of one line keep lldb-dap's order; without a map,
        // all do. (Return Value) goes last.
        if any_declared {
            sort(vals) { a, b => a.decl < b.decl || (a.decl == b.decl && a.i < b.i) };
        }
    } else {
        let prefix = str_or(ctx, "prefix", "");
        let gtype = str_or(ctx, "gtype", "");
        let et, is_array = element_type(gtype);
        if vs && is_arr(vs) {
            var v = vs.first;
            var i = 0;
            while v {
                let c = str_or(v, "name", "");
                let at = arena.len;
                if is_index_name(c) {
                    let ev = if prefix.len > 0 { str(prefix, c) } else { str("") };
                    goose_var(arena, v, c, ev, if is_array { et } else { "" });
                    kv_str(names, c, c);
                    vals.push(Listed { decl: 0, i: i, at: at, len: arena.len - at });
                } else if starts_with(c, "*") {
                    emit(arena, v);
                    vals.push(Listed { decl: 0, i: i, at: at, len: arena.len - at });
                } else {
                    var gname: u8[>..] = [];
                    gname.append(goose_name(c));
                    var ct: u8[>..] = [];
                    var matched = false;
                    if gtype.len > 0 {
                        each_field_of(m, gtype) { f =>
                            if !matched && is_obj(f) && (str_or(f, "c", "\x00") == c || str_or(f, "name", "\x00") == c) {
                                matched = true;
                                let fname = str_or(f, "name", "");
                                if fname.len > 0 { gname.clear(); gname.append(fname); }
                                ct.append(str_or(f, "type", ""));
                            }
                        };
                    }
                    let ev = if prefix.len > 0 { str(prefix, ".", gname) } else { str("") };
                    goose_var(arena, v, gname, ev, ct);
                    kv_str(names, gname, c);
                    vals.push(Listed { decl: 0, i: i, at: at, len: arena.len - at });
                }
                i++;
                v .= v.next;
            }
        }
    }
    names.push('}');
    put_ref_with_names(ref, ctx, names);
    out.push('{');
    copy_except(out, body, ["variables"]);
    key(out, "variables");
    out.push('[');
    for x in vals { sep(out); out.append(arena[x.at..x.at + x.len]); }
    for x in parens { sep(out); out.append(arena[x.at..x.at + x.len]); }
    out.append("]}");
}

// The Globals scope of a Goose frame: the program's Goose globals from the
// map (globals.vars), not the runtime's statics. Values come from
// goose_lldb.py's goose-globals (decoded by Goose type, the selected
// thread's instance); children from the globals block's fields.
fn goose_globals(req: json::Node&, ref: i64, ctx: json::Node&) {
    var mdoc: json::Node[>..] = [];
    var mdec: u8[>..] = [];
    let m, merr = json::parse(map_text, mdoc, mdec);
    let info = json::member(ctx, "frame");
    // The block's fields, by C name.
    var r: u8[>..] = [];
    adapter_request("variables", str("{\"variablesReference\":", ref, "}"), INTERNAL_TIMEOUT_MS, r);
    var tdoc: json::Node[>..] = [];
    var tdec: u8[>..] = [];
    let top, terr = json::parse(r, tdoc, tdec);
    let block_name = globals_block(m);
    var block_ref: i64 = 0;
    let tv = json::member(json::member(top, "body"), "variables");
    if tv && is_arr(tv) {
        var v = tv.first;
        while v {
            if block_ref == 0 && str_or(v, "name", "") == block_name && truthy(json::member(v, "variablesReference")) {
                block_ref = get_int(v, "variablesReference", 0);
            }
            v .= v.next;
        }
    }
    var k: u8[>..] = [];
    if block_ref != 0 {
        adapter_request("variables", str("{\"variablesReference\":", block_ref, "}"), INTERNAL_TIMEOUT_MS, k);
    }
    var kdoc: json::Node[>..] = [];
    var kdec: u8[>..] = [];
    let kroot, kerr = json::parse(k, kdoc, kdec);
    let kids = json::member(json::member(kroot, "body"), "variables");
    var names: u8[>..] = [];
    names.push('{');
    var out: u8[>..] = [];
    out.append("{\"variables\":[");
    if has_globals(m) {
        var texts: u8[>..] = [];
        goose_globals_text(m, info, texts);
        each_global(m) { g, blk =>
            let gname = str_or(g, "name", "");
            let gc = str_or(g, "c", "");
            let gtype = str_or(g, "type", "");
            let v = last_named(kids, gc);
            sep(out);
            out.push('{');
            if v { copy_except(out, v, ["name", "evaluateName", "type", "value", "declarationLocationReference"]); }
            else { out.append("\"variablesReference\":0"); }
            kv_str(out, "name", gname);
            kv_str(out, "evaluateName", gname);
            if is_str(json::member(g, "type")) { kv_str(out, "type", gtype); }
            else if v {
                let vt = json::member(v, "type");
                if vt { kv_node(out, "type", vt); }
            }
            let t, found = text_of(texts, gname);
            if found { kv_str(out, "value", t); }
            else if v { put_value(out, json::member(v, "value"), str_or(v, "type", ""), gtype); }
            else { kv_str(out, "value", "<not in the debug info>"); }
            out.push('}');
            kv_str(names, gname, gc);
            if v && truthy(json::member(v, "variablesReference")) {
                var c: u8[>..] = [];
                child_ctx(c, gname, gtype);
                store_put(refs, get_int(v, "variablesReference", 0), c);
            }
        };
    } else if kids && is_arr(kids) {
        // A map without globals (an older compiler): the block's fields
        // that are named like Goose variables.
        var v = kids.first;
        while v {
            let c = str_or(v, "name", "");
            if !starts_with(c, "gs_") && is_goose_cname(c) && last_named(kids, c) .== v {
                sep(out);
                goose_var(out, v, goose_name(c), goose_name(c), "");
                kv_str(names, goose_name(c), c);
            }
            v .= v.next;
        }
    }
    out.append("]}");
    names.push('}');
    put_ref_with_names(ref, ctx, names);
    respond_ok(req, out);
}

// The last variable of a list with a name (a dict keeps the last).
fn last_named(vs: json::Node?, name: const u8[:]) -> json::Node? {
    var hit: json::Node? = null;
    if vs && is_arr(vs) {
        guard vs else { return null; }
        var v = vs.first;
        while v {
            if str_or(v, "name", "") == name { hit .= v; }
            v .= v.next;
        }
    }
    return hit;
}

// The globals' value texts from goose-globals, "name\0text\0..." onto out.
fn goose_globals_text(m: json::Node?, info: json::Node?, out: u8[>..]&) {
    var args: u8[>..] = [];
    args.push('{');
    kv_str(args, "expression", str(escape, "goose-globals"));
    kv_str(args, "context", "repl");
    let fid = json::member(info, "id");
    if fid && is_int(fid) { kv_node(args, "frameId", fid); }
    args.push('}');
    var r: u8[>..] = [];
    guard adapter_request("evaluate", args, INTERNAL_TIMEOUT_MS, r) else { return; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    guard request_ok(root) else { return; }
    let res = str_or(json::member(root, "body"), "result", "");
    each_line(res) { line =>
        each_global(m) { g, blk =>
            let gtype = json::member(g, "type");
            var head: u8[>..] = [];
            head.append(str_or(g, "name", ""));
            head.append(": ");
            let ts, ok = json::as_string(gtype);
            if ok { head.append(ts); } else if gtype { emit(head, gtype); } else { head.append("None"); }
            head.append(" = ");
            if starts_with(line, head) {
                out.append(str_or(g, "name", ""));
                out.push(0);
                out.append(line[head.len..]);
                out.push(0);
            }
        };
    };
}

// The text of name in a "name\0text\0" list (its last).
fn text_of(list: const u8[:], name: const u8[:]) -> const u8[:], bool {
    var pos: i64 = 0;
    var at: i64 = -1;
    var len: i64 = 0;
    while pos < list.len {
        let n = next_cstring(list, pos);
        let start = pos;
        let t = next_cstring(list, pos);
        if n == name { at = start; len = t.len; }
    }
    if at < 0 { return list[..0], false; }
    return list[at..at + len], true;
}

fn c_set_variable(req: json::Node&, text: const u8[:]) {
    let a = json::member(req, "arguments");
    let ref = get_int(a, "variablesReference", 0);
    var ctx_t: u8[>..] = [];
    let name, has_name = get_str(a, "name");
    let known = is_int(json::member(a, "variablesReference")) && store_get(refs, ref, ctx_t) && has_name;
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let ctx, err = json::parse(ctx_t, doc, dec);
    // The C name of the last of that name (a dict keeps the last).
    var hit: json::Node? = null;
    let names = json::member(ctx, "names");
    if known && names && is_obj(names) {
        var c = names.first;
        while c {
            if c.key == name && is_str(c) { hit .= c; }
            c .= c.next;
        }
    }
    guard hit else { forward(req, text, "", "{\"kind\":\"setVariable\"}"); return; }
    var args: u8[>..] = [];
    args.push('{');
    copy_except(args, a, ["name"]);
    let cname, ok = json::as_string(hit);
    kv_str(args, "name", cname);
    args.push('}');
    forward(req, text, args, "{\"kind\":\"setVariable\"}");
}

fn c_exception_info(req: json::Node&, text: const u8[:]) {
    let tid = get_int(json::member(req, "arguments"), "threadId", 0);
    var e: u8[>..] = [];
    if !store_get(exc, tid, e) { forward(req, text, "", ""); return; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let x, err = json::parse(e, doc, dec);
    let msg = str_or(x, "message", "");
    var body: u8[>..] = [];
    body.append("{\"exceptionId\":\"goose runtime error\"");
    kv_str(body, "description", msg);
    kv_str(body, "breakMode", "always");
    key(body, "details");
    body.push('{');
    kv_str(body, "message", msg);
    kv_str(body, "typeName", "Goose runtime error");
    body.append("}}");
    respond_ok(req, body);
}
