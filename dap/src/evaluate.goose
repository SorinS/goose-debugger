// evaluate (watch, hover, clipboard, variables, repl): Goose identifiers and
// field paths are rewritten to C (u.pos.x -> u_g.pos_g.x_g, second.v ->
// second_g->v_g); anything else passes as it is. The result gets the Goose
// type of the path. In the repl, a line is a Goose expression only if it
// starts with a Goose local or global of the frame; otherwise it is an
// LLDB command.

import std;
import json;
import .jsonw;
import .names;
import .store;
import .gmap;
import .proxy;
import .variables;
import .launch;

// The frame a request names, its info onto out; false for none.
fn frame_info(args: json::Node?, out: u8[>..]&) -> bool {
    let fid = json::member(args, "frameId");
    guard is_int(fid) else { return false; }
    return store_get(frames, get_int(args, "frameId", 0), out);
}

// a with its expression replaced, onto out.
fn with_expression(out: u8[>..]&, a: json::Node?, expr: const u8[:]) {
    out.push('{');
    copy_except(out, a, ["expression"]);
    kv_str(out, "expression", expr);
    out.push('}');
}

fn c_evaluate(req: json::Node&, text: const u8[:]) {
    let a = json::member(req, "arguments");
    let expr, is_text = get_str(a, "expression");
    let ctxname = json::member(a, "context");
    let is_repl = str_or(a, "context", "") == "repl";
    var info_t: u8[>..] = [];
    let has_info = frame_info(a, info_t);
    var idoc: json::Node[>..] = [];
    var idec: u8[>..] = [];
    let info, ierr = json::parse(info_t, idoc, idec);
    // lldb-vscode 16 has no repl auto mode: a command needs the escape
    // prefix there. A plain repl line it cannot parse as an expression is
    // tried again as a command (see rewrite_response).
    var plain_ctx: u8[>..] = [];
    plain_ctx.append("{\"kind\":\"evaluate\",\"prefix\":null");
    key(plain_ctx, "context");
    emit_or_null(plain_ctx, ctxname);
    key(plain_ctx, "retry");
    if is_repl && is_text && escape.len > 0 && !starts_with(expr, escape) {
        with_expression(plain_ctx, a, str(escape, expr));
    } else { plain_ctx.append("null"); }
    plain_ctx.push('}');
    if !is_text || !has_info || !truthy(json::member(info, "goose")) {
        forward(req, text, "", plain_ctx);
        return;
    }
    var mdoc: json::Node[>..] = [];
    var mdec: u8[>..] = [];
    let m, merr = json::parse(map_text, mdoc, mdec);
    if is_repl {
        if escape.len > 0 && starts_with(expr, escape) {
            forward(req, text, "", "");
            return;
        }
        if !repl_is_goose(m, expr, info) {
            forward(req, text, "", plain_ctx);
            return;
        }
    }
    var cexpr: u8[>..] = [];
    var gtype: u8[>..] = [];
    rewrite_expr(m, info, expr, false, cexpr, gtype);
    var alt: u8[>..] = [];
    var ignored: u8[>..] = [];
    rewrite_expr(m, info, expr, true, alt, ignored);
    var args: u8[>..] = [];
    with_expression(args, a, cexpr);
    var ctx: u8[>..] = [];
    ctx.append("{\"kind\":\"evaluate\"");
    kv_str(ctx, "prefix", strip(expr));
    if gtype.len > 0 { kv_str(ctx, "gtype", gtype); } else { kv_null(ctx, "gtype"); }
    kv_bool(ctx, "changed", cexpr != expr);
    key(ctx, "context");
    emit_or_null(ctx, ctxname);
    key(ctx, "alt");
    if alt != cexpr { with_expression(ctx, a, alt); } else { ctx.append("null"); }
    ctx.push('}');
    forward(req, text, args, ctx);
}

// The frame's function in the map, if it has one.
fn frame_function(m: json::Node?, info: json::Node?) -> json::Node? {
    guard map_has_functions(m) else { return null; }
    return function_by_c(m, str_or(info, "cname", ""));
}

// A repl line is a Goose expression, rather than an LLDB command, if it
// starts with a Goose local or global of the frame and is not `word word`
// (a command and its argument).
fn repl_is_goose(m: json::Node?, expr: const u8[:], info: json::Node?) -> bool {
    let all = tokenize(expr);
    var toks: Tok[>..] = [];
    for t in all { if t.kind != TOK_WS { toks.push(t); } }
    if toks.len == 0 || toks[0].kind != TOK_ID { return false; }
    if toks.len > 1 && (toks[1].kind == TOK_ID || toks[1].kind == TOK_NUM || toks[1].kind == TOK_STR) { return false; }
    if toks.len > 2 && expr[toks[1].at..toks[1].end] == "-" && toks[2].kind == TOK_ID && command_with_option(expr) {
        return false;
    }
    let name = expr[toks[0].at..toks[0].end];
    let f = frame_function(m, info);
    let lnode = json::member(info, "line");
    if f && local_named(f, name, int_value(lnode, 0), is_int(lnode)) { return true; }
    return global_named(m, name) != null;
}

// A type saved outside an open [: its text in an arena, and whether the
// expression was one path up to the [.
struct Saved { at: i64, len: i64, ok: bool }

// Goose identifiers and field paths to their C spelling, onto out; anything
// not known passes unchanged. gtype gets the Goose type of the whole
// expression if it is a plain path. goose_fields keeps every field's Goose
// name (LLDB's variable path through the formatters' children).
fn rewrite_expr(m: json::Node?, info: json::Node?, expr: const u8[:], goose_fields: bool,
                out: u8[>..]&, gtype: u8[>..]&) {
    let f = frame_function(m, info);
    let lnode = json::member(info, "line");
    let has_line = is_int(lnode);
    let line = int_value(lnode, 0);
    let thread = globals_thread(m);
    let toks = tokenize(expr);
    var nonspace: i64[>..] = [];
    for t, i in toks { if t.kind != TOK_WS { nonspace.push(i); } }
    var cur: u8[>..] = [];           // Goose type of the path being built ("" for none)
    var path_ok = true;              // the expression is one path so far
    var has_prev = false;            // the previous non-space token
    var prev_text: u8[>..] = [];
    var dot_at: i64 = -1;            // where out's last piece is a "." just written
    var saved: Saved[>..<] = [];     // the type and path_ok outside each open [
    var saved_text: u8[>..] = [];
    var synth = false;               // an index has been taken
    var k = 0;
    for t in toks {
        let text = expr[t.at..t.end];
        if t.kind == TOK_WS {
            out.append(text);
            dot_at = -1;
            continue;
        }
        let nxt = if k + 1 < nonspace.len { expr[toks[nonspace[k + 1]].at..toks[nonspace[k + 1]].end] } else { "" };
        let has_nxt = k + 1 < nonspace.len;
        k++;
        var piece_dot = false;
        if t.kind == TOK_ID {
            let after_dot = has_prev && (prev_text == "." || prev_text == "->");
            let after_ns = has_prev && prev_text == "::";
            if after_dot {
                var fname: u8[>..] = [];
                var ftype: u8[>..] = [];
                var arrow = false;
                let has_ftype = field_c(m, cur, text, fname, ftype, arrow);
                if goose_fields || (synth && has_ftype) || no_c_type(m, cur) {
                    // Past an index only LLDB's variable path (the
                    // formatters' children, Goose-named) can go on: C
                    // cannot index a resizable or a slice.
                    // A pointer in C (a reference, a variable-size
                    // value) takes ->, which LLDB's variable path also
                    // follows through the formatters' children.
                    fname.clear();
                    fname.append(text);
                    arrow = is_pointer_type(cur) || no_c_type(m, cur);
                }
                if arrow && dot_at >= 0 {
                    out.resize(dot_at);
                    out.append("->");
                }
                out.append(fname);
                cur.clear();
                cur.append(ftype);
            } else if after_ns || (has_nxt && (nxt == "(" || nxt == "::")) || is_keyword(text) {
                out.append(text);
                cur.clear();
                if has_prev { path_ok = false; }
            } else {
                if has_prev && prev_text != "[" && prev_text != "(" && prev_text != "," { path_ok = false; }
                let l = local_named(f, text, line, has_line);
                let g = global_named(m, text);
                if l {
                    out.append(str_or(l, "c", ""));
                    cur.clear();
                    cur.append(str_or(l, "type", ""));
                } else if g {
                    if thread.len > 0 {
                        // The selected thread's own instance.
                        format(out, "((gs_globals_t *)", thread, ")->", str_or(g, "c", ""));
                    } else {
                        global_path(out, m, g);
                    }
                    cur.clear();
                    cur.append(str_or(g, "type", ""));
                } else {
                    out.append(text);
                    cur.clear();
                }
            }
        } else if t.kind == TOK_OP && text == "[" {
            out.append(text);
            synth = true;
            saved.push(Saved { at: saved_text.len, len: cur.len, ok: path_ok });
            saved_text.append(cur);
            cur.clear();
            path_ok = true;
        } else if t.kind == TOK_OP && text == "]" {
            out.append(text);
            var base: u8[>..] = [];
            if saved.len > 0 {
                let s = saved.pop();
                base.append(saved_text[s.at..s.at + s.len]);
                saved_text.resize(s.at);
                path_ok = s.ok;
            } else {
                path_ok = false;
            }
            cur.clear();
            if base.len > 0 {
                let et, ok = element_type(base);
                if ok { cur.append(et); }
            }
        } else if t.kind == TOK_OP && (text == "." || text == "->") {
            if text == "." { dot_at = out.len; piece_dot = true; }
            out.append(text);
        } else {
            out.append(text);
            path_ok = false;
            cur.clear();
        }
        if !piece_dot { dot_at = -1; }
        has_prev = true;
        prev_text.clear();
        prev_text.append(text);
    }
    if path_ok && saved.len == 0 { gtype.append(cur); }
}

// A value whose C is a bare uint8_t * (a variable-size struct, a
// variable-mode enum): only the formatters' Goose-named children reach
// its fields.
fn no_c_type(m: json::Node?, gtype: const u8[:]) -> bool {
    if gtype.len == 0 { return false; }
    let t = strip_ref(gtype);
    if ends_with(t, "..") { return true; }
    let td = type_named(m, t);
    return td != null && !truthy(json::member(td, "c"));
}

// The C member for field `name` of a value of Goose type gtype onto cname,
// its Goose type onto ftype (true if it has one), and whether the value
// is a pointer to go through with ->.
fn field_c(m: json::Node?, gtype: const u8[:], name: const u8[:], cname: u8[>..]&, ftype: u8[>..]&, arrow: bool&) -> bool {
    arrow = if gtype.len > 0 { is_pointer_type(gtype) } else { false };
    if gtype.len > 0 {
        let base = strip_ref(gtype);
        let et, is_array = element_type(base);
        if is_array || ends_with(base, ":]") {
            if name == "len" || name == "data" || name == "base" || name == "cap" {
                cname.append(name);
                if name == "len" || name == "cap" { ftype.append("i64"); return true; }
                return false;
            }
        }
        var hit = false;
        var has_type = false;
        each_field_of(m, base) { f =>
            if !hit && str_or(f, "name", "\x00") == name && is_str(json::member(f, "c")) {
                hit = true;
                cname.append(str_or(f, "c", ""));
                let t, ok = get_str(f, "type");
                if ok { ftype.append(t); has_type = true; }
            }
        };
        if hit { return has_type; }
    }
    if is_goose_cname(name) || name == "len" || name == "data" || name == "base" || name == "tag" || name == "u" || name == "e" {
        cname.append(name);
        return false;
    }
    cname.append(name);
    cname.append("_g");
    return false;
}

fn rewrite_evaluate(ctx: json::Node?, body: json::Node?, out: u8[>..]&) {
    if !is_obj(body) { emit_or_null(out, body); return; }
    let gtype = str_or(ctx, "gtype", "");
    let ctype = str_or(body, "type", "");
    let resn = json::member(body, "result");
    let r0, is_text = json::as_string(resn);
    var res: u8[>..] = [];
    res.append(r0);
    var changed = false;
    let context = json::member(ctx, "context");
    let plain_context = is_null(context) || str_or(ctx, "context", "") == "repl";
    var head: u8[>..] = [];
    format(head, "(", ctype, ") ");
    if !plain_context && is_text && ctype.len > 0 && starts_with(res, head) && find(res, " = ") >= 0 {
        // lldb-dap 18 answers a hover as it does the repl,
        // "(gs_i64) total_g = 0": keep the value.
        let i = find(res, " = ");
        let v = copy_text(res[i + 3..]);
        res.clear();
        res.append(v);
        changed = true;
    }
    if resn && !is_null(json::member(ctx, "changed")) {
        if is_text {
            var r: u8[>..] = [];
            if gtype.len > 0 && ctype.len > 0 && starts_with(res, head) {
                // A repl result, "(Point_g) $2 = Point { ... }": the Goose type.
                var t: u8[>..] = [];
                format(t, "(", gtype, ") ", res[ctype.len + 3..]);
                let i = find(t, " = ");
                if i >= 0 {
                    r.append(t[..i + 3]);
                    clean_value(r, t[i + 3..], ctype, gtype);
                } else {
                    r.append(t);
                }
            } else {
                clean_value(r, res, ctype, gtype);
            }
            res.clear();
            res.append(r);
            changed = true;
        }
    }
    let ref = json::member(body, "variablesReference");
    if truthy(ref) && is_int(ref) {
        var c: u8[>..] = [];
        child_ctx(c, str_or(ctx, "prefix", ""), gtype);
        store_put(refs, get_int(body, "variablesReference", 0), c);
    }
    out.push('{');
    copy_except(out, body, ["result", "type"]);
    if changed { kv_str(out, "result", res); } else if resn { kv_node(out, "result", resn); }
    if gtype.len > 0 { kv_str(out, "type", gtype); }
    else {
        let t = json::member(body, "type");
        if t { kv_node(out, "type", t); }
    }
    out.push('}');
}

