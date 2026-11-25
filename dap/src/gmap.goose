// The debug map: the functions, locals, types and globals of a program, from
// the <stem>.debug.json files `goose build -g` writes beside the generated
// C. The proxy keeps the maps as one JSON text, an array of the documents,
// and parses it where it is needed; these read the parsed tree as the
// Python adapter's GooseMap read its dicts. Unknown keys are ignored, and
// anything malformed is passed over.
//
// m is the array of documents (null: no map).

import std;
import json;
import .jsonw;
import .names;

// F(f) for each function that has a C name, in the maps' order.
fn each_function<F>(m: json::Node?) {
    guard m else { return; }
    var d = m.first;
    while d {
        let fs = json::member(d, "functions");
        if fs && is_arr(fs) {
            var f = fs.first;
            while f {
                if f && is_obj(f) && is_str(json::member(f, "c")) { F(f); }
                f .= f.next;
            }
        }
        d .= d.next;
    }
}

// Whether the map describes any function (Python's bool(gmap)).
fn map_has_functions(m: json::Node?) -> bool {
    each_function(m) { f => return true; };
    return false;
}

fn map_function_count(m: json::Node?) -> i64 {
    var n = 0;
    each_function(m) { f => n++; };
    return n;
}

// The function whose C name is cname (the first, of several maps).
fn function_by_c(m: json::Node?, cname: const u8[:]) -> json::Node? {
    each_function(m) { f =>
        let c, ok = get_str(f, "c");
        if c == cname { return f; }
    };
    return null;
}

// The last part of a qualified name: geo::area -> area.
fn last_part(name: const u8[:]) -> const u8[:] {
    let i = rfind(name, "::");
    return if i >= 0 { name[i + 2..] } else { name };
}

// The C names of every instance of a Goose function, NUL-ended onto out:
// `area`, a qualified `geo::area`, or the last part of one. Python's
// by_name lists the functions of each name in the order the names first
// appear.
fn functions_named(m: json::Node?, name: const u8[:], out: u8[>..]&) {
    var found = false;
    each_function(m) { f =>
        let n, ok = get_str(f, "name");
        if ok && n == name {
            out.append(str_or(f, "c", ""));
            out.push(0);
            found = true;
        }
    };
    if found { return; }
    each_function(m) { f =>
        let n, ok = get_str(f, "name");
        if ok && last_part(n) == name && first_named(m, n) .== f {
            each_function(m) { g =>
                let gn, gok = get_str(g, "name");
                if gok && gn == n {
                    out.append(str_or(g, "c", ""));
                    out.push(0);
                }
            };
        }
    };
}

fn first_named(m: json::Node?, name: const u8[:]) -> json::Node? {
    each_function(m) { f =>
        let n, ok = get_str(f, "name");
        if ok && n == name { return f; }
    };
    return null;
}

// F(l) for each local of function f that has a C name and a Goose name.
fn each_local<F>(f: json::Node?) {
    let ls = json::member(f, "locals");
    guard ls else { return; }
    guard is_arr(ls) else { return; }
    var l = ls.first;
    while l {
        if l && is_obj(l) && is_str(json::member(l, "c")) && is_str(json::member(l, "name")) { F(l); }
        l .= l.next;
    }
}

fn local_line(l: json::Node?) -> i64 { get_int(l, "line", 0) }

// Whether local l is in scope at line (any line when has_line is false).
fn in_scope(l: json::Node?, line: i64, has_line: bool) -> bool {
    if !has_line { return true; }
    let lo = get_int(l, "line", 0);
    let hi = get_int(l, "end", 1 << 30);
    return lo <= line && line <= hi;
}

// The local of f in scope at line that a name means there: of two with
// one name (shadowing), the later-declared one. Python's locals_at keeps,
// per name, the last of those declared on the greatest line.
fn scope_winner(f: json::Node?, name: const u8[:], line: i64, has_line: bool) -> json::Node? {
    var best: json::Node? = null;
    var best_line = 0;
    each_local(f) { l =>
        let n = str_or(l, "name", "");
        if n == name && in_scope(l, line, has_line) {
            let ll = local_line(l);
            if !best || best_line <= ll { best .= l; best_line = ll; }
        }
    };
    return best;
}

// The local of f whose C name is cname, if it is the one its Goose name
// means at line (Python's {l.c: l for l in locals_at(...).values()}).
fn scope_local_by_c(f: json::Node?, cname: const u8[:], line: i64, has_line: bool) -> json::Node? {
    var hit: json::Node? = null;
    each_local(f) { l =>
        if !hit && str_or(l, "c", "") == cname && in_scope(l, line, has_line) {
            let w = scope_winner(f, str_or(l, "name", ""), line, has_line);
            if w && w .== l { hit .= l; }
        }
    };
    return hit;
}

// The local a name means in f at line; else the one of that name declared
// last.
fn local_named(f: json::Node?, name: const u8[:], line: i64, has_line: bool) -> json::Node? {
    let w = scope_winner(f, name, line, has_line);
    if w { return w; }
    var best: json::Node? = null;
    var best_line = 0;
    each_local(f) { l =>
        if str_or(l, "name", "") == name {
            let ll = local_line(l);
            if !best || ll > best_line { best .= l; best_line = ll; }
        }
    };
    return best;
}

// --- Types ------------------------------------------------------------------

// The type named name (the first, of several maps).
fn type_named(m: json::Node?, name: const u8[:]) -> json::Node? {
    guard m else { return null; }
    var d = m.first;
    while d {
        let ts = json::member(d, "types");
        if ts && is_arr(ts) {
            var t = ts.first;
            while t {
                if t && is_obj(t) {
                    let n, ok = get_str(t, "name");
                    if ok && n == name { return t; }
                }
                t .= t.next;
            }
        }
        d .= d.next;
    }
    return null;
}

// The fields of a Goose type, given as type text: a struct's, or a
// variant's (Shape.Circle); false when it is neither. A struct without a
// fields list has none (true, null).
fn fields_of(m: json::Node?, gtype: const u8[:]) -> json::Node?, bool {
    if gtype.len == 0 { return null, false; }
    let t = strip_ref(gtype);
    let td = type_named(m, t);
    if td {
        if str_or(td, "kind", "") == "struct" { return list_member(td, "fields"), true; }
        return null, false;
    }
    if find(t, ".") >= 0 && !ends_with(t, "..") {
        let dot = rfind(t, ".");
        let base = type_named(m, t[..dot]);
        if base {
            let vs = json::member(base, "variants");
            if vs && is_arr(vs) {
                var v = vs.first;
                while v {
                    if str_or(v, "name", "") == t[dot + 1..] { return list_member(v, "fields"), true; }
                    v .= v.next;
                }
            }
        }
    }
    return null, false;
}

// The variants of an enum type (Shape, Shape..); false when it is not one.
fn enum_variants(m: json::Node?, gtype: const u8[:]) -> json::Node?, bool {
    var t = strip_ref(gtype);
    if ends_with(t, "..") { t = t[..t.len - 2]; }
    let td = type_named(m, t);
    if td && str_or(td, "kind", "") == "enum" { return list_member(td, "variants"), true; }
    return null, false;
}

// An array member, or null for a missing or empty one (Python's `or []`).
fn list_member(n: json::Node?, k: const u8[:]) -> json::Node? {
    let v = json::member(n, k);
    if v && is_arr(v) { return v; }
    return null;
}

// The field named name among the fields of a type (fields, or every
// variant's when the type is an enum): F(field).
fn each_field_of<F>(m: json::Node?, gtype: const u8[:]) -> bool {
    let fl, found = fields_of(m, gtype);
    if found {
        if fl {
            var f = fl.first;
            while f { F(f); f .= f.next; }
        }
        return true;
    }
    let vs, isenum = enum_variants(m, gtype);
    if !isenum { return false; }
    if vs {
        var v = vs.first;
        while v {
            let vf = list_member(v, "fields");
            if vf {
                var f = vf.first;
                while f { F(f); f .= f.next; }
            }
            v .= v.next;
        }
    }
    return true;
}

// --- Globals ----------------------------------------------------------------

// The globals block of one document: globals.main, gs_globals_main for a
// map whose globals are a list (or name no block), "" for none.
fn doc_block(d: json::Node?) -> const u8[:] {
    let g = json::member(d, "globals");
    if g && is_obj(g) { return str_or(g, "main", "gs_globals_main"); }
    if g && is_arr(g) { return "gs_globals_main"; }
    return "";
}

fn doc_vars(d: json::Node?) -> json::Node? {
    let g = json::member(d, "globals");
    if g && is_obj(g) { return list_member(g, "vars"); }
    if g && is_arr(g) { return g; }
    return null;
}

// F(v, block) for each global, the first of each Goose name.
fn each_global<F>(m: json::Node?) {
    guard m else { return; }
    var d = m.first;
    while d {
        let vs = doc_vars(d);
        if vs {
            var v = vs.first;
            while v {
                if v && is_obj(v) && is_str(json::member(v, "c")) && is_str(json::member(v, "name")) {
                    let g = global_named(m, str_or(v, "name", ""));
                    if g && g .== v { F(v, doc_block(d)); }
                }
                v .= v.next;
            }
        }
        d .= d.next;
    }
}

fn global_named(m: json::Node?, name: const u8[:]) -> json::Node? {
    guard m else { return null; }
    var d = m.first;
    while d {
        let vs = doc_vars(d);
        if vs {
            var v = vs.first;
            while v {
                if v && is_obj(v) && is_str(json::member(v, "c")) && str_or(v, "name", "") == name { return v; }
                v .= v.next;
            }
        }
        d .= d.next;
    }
    return null;
}

// The block of the document a global comes from.
fn global_block(m: json::Node?, g: json::Node&) -> const u8[:] {
    guard m else { return ""; }
    var d = m.first;
    while d {
        let vs = doc_vars(d);
        if vs {
            var v = vs.first;
            while v {
                if v .== g { return doc_block(d); }
                v .= v.next;
            }
        }
        d .= d.next;
    }
    return "";
}

// A global's C path: <block>.<c>, or its C name alone with no block.
fn global_path(out: u8[>..]&, m: json::Node?, g: json::Node&) {
    let blk = global_block(m, g);
    if blk.len > 0 { out.append(blk); out.push('.'); }
    out.append(str_or(g, "c", ""));
}

fn has_globals(m: json::Node?) -> bool {
    var any = false;
    each_global(m) { v, b => any = true; };
    return any;
}

// The program's globals block (the last map's), and, when it starts
// threads, the thread-local pointer to the running thread's ("gs_gl").
fn globals_block(m: json::Node?) -> const u8[:] {
    guard m else { return "gs_globals_main"; }
    var last: json::Node? = null;
    var d = m.first;
    while d {
        let g = json::member(d, "globals");
        if g && is_obj(g) { last .= g; }
        d .= d.next;
    }
    guard last else { return "gs_globals_main"; }
    return str_or(last, "main", "gs_globals_main");
}

fn globals_thread(m: json::Node?) -> const u8[:] {
    guard m else { return ""; }
    var last: json::Node? = null;
    var d = m.first;
    while d {
        let g = json::member(d, "globals");
        if g && is_obj(g) && str_or(g, "thread", "").len > 0 { last .= g; }
        d .= d.next;
    }
    guard last else { return ""; }
    return str_or(last, "thread", "");
}
