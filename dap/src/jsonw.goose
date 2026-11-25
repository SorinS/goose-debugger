// JSON out of JSON: the proxy's every rewrite is a parsed message written
// again with some members dropped, replaced or added. std's json parses a
// document into a read-only tree; this writes such a tree, or an object of
// it member by member, onto a builder, and reads members the way the
// Python adapter's dict.get did.
//
// Objects and arrays are written open-ended: `sep` puts a comma before a
// member or an element unless the builder has just opened one, so a writer
// never tracks whether it is first.

import std;
import json;

// A comma unless out has just opened an object or an array.
fn sep(out: u8[>..]&) {
    if out.len == 0 { return; }
    let c = out[out.len - 1];
    if c != '{' && c != '[' { out.push(','); }
}

fn key(out: u8[>..]&, k: const u8[:]) {
    sep(out);
    json::format_key(out, k);
}

fn kv_str(out: u8[>..]&, k: const u8[:], v: const u8[:]) { key(out, k); json::format_string(out, v); }
fn kv_int(out: u8[>..]&, k: const u8[:], v: i64) { key(out, k); format(out, v); }
fn kv_bool(out: u8[>..]&, k: const u8[:], v: bool) { key(out, k); json::format_bool(out, v); }
fn kv_null(out: u8[>..]&, k: const u8[:]) { key(out, k); out.append("null"); }

// A member whose value is JSON text already written (an object built
// elsewhere, a stored context).
fn kv_raw(out: u8[>..]&, k: const u8[:], text: const u8[:]) { key(out, k); out.append(text); }

fn kv_node(out: u8[>..]&, k: const u8[:], n: json::Node&) { key(out, k); emit(out, n); }

// A node and everything below it. Strings were decoded by the parser and
// are escaped again here; integers stay integers.
recursive fn emit(out: u8[>..]&, n: json::Node&) {
    match n.val {
        Null => { out.append("null"); },
        Bool b => { json::format_bool(out, b.v); },
        Int i => { format(out, i.v); },
        Num x => { json::format_number(out, x.v); },
        Str s => { json::format_string(out, s.s); },
        Arr => {
            out.push('[');
            var c = n.first;
            while c {
                sep(out);
                emit(out, c);
                c .= c.next;
            }
            out.push(']');
        },
        Obj => {
            out.push('{');
            var c = n.first;
            while c {
                sep(out);
                json::format_key(out, c.key);
                emit(out, c);
                c .= c.next;
            }
            out.push('}');
        },
    }
}

// Whether k is one of keys.
fn one_of(k: const u8[:], keys: const u8[:][:]) -> bool {
    for x in keys { if x == k { return true; } }
    return false;
}

// The members of object n but those named in skip, each after a comma
// where one is due; the caller has opened the object and closes it.
fn copy_except(out: u8[>..]&, n: json::Node?, skip: const u8[:][:]) {
    guard n else { return; }
    guard is_obj(n) else { return; }
    var c = n.first;
    while c {
        if !one_of(c.key, skip) {
            sep(out);
            json::format_key(out, c.key);
            emit(out, c);
        }
        c .= c.next;
    }
}

// n written whole, or `null`.
fn emit_or_null(out: u8[>..]&, n: json::Node?) {
    if n { emit(out, n); } else { out.append("null"); }
}

// --- Reading -------------------------------------------------------------------

fn is_obj(n: json::Node?) -> bool {
    guard n else { return false; }
    match n.val { Obj => { return true; }, _ => { return false; } }
}

fn is_arr(n: json::Node?) -> bool {
    guard n else { return false; }
    match n.val { Arr => { return true; }, _ => { return false; } }
}

fn is_str(n: json::Node?) -> bool {
    guard n else { return false; }
    match n.val { Str => { return true; }, _ => { return false; } }
}

fn is_null(n: json::Node?) -> bool {
    guard n else { return true; }
    match n.val { Null => { return true; }, _ => { return false; } }
}

// Python's truth of a JSON value: null, false, 0, "", [] and {} are false.
fn truthy(n: json::Node?) -> bool {
    guard n else { return false; }
    match n.val {
        Null => { return false; },
        Bool b => { return b.v; },
        Int i => { return i.v != 0; },
        Num x => { return x.v != 0.0; },
        Str s => { return s.s.len > 0; },
        Arr a => { return a.count > 0; },
        Obj o => { return o.count > 0; },
    }
}

// A string member, or "" with false.
fn get_str(n: json::Node?, k: const u8[:]) -> const u8[:], bool {
    return json::as_string(json::member(n, k));
}

// A string member that is not empty: Python's `isinstance(x, str) and x`.
fn str_or(n: json::Node?, k: const u8[:], dflt: const u8[:]) -> const u8[:] {
    let s, ok = json::as_string(json::member(n, k));
    return if ok { s } else { dflt };
}

// An integer member (a number with a fraction is cut to its integer part,
// as Python's int() does), or dflt.
fn get_int(n: json::Node?, k: const u8[:], dflt: i64) -> i64 {
    return int_value(json::member(n, k), dflt);
}

fn int_value(n: json::Node?, dflt: i64) -> i64 {
    guard n else { return dflt; }
    match n.val {
        Int i => { return i.v; },
        Num x => {
            if x.v == x.v && x.v < 9.0e18 && x.v > -9.0e18 { return x.v as! i64; }
            return dflt;
        },
        _ => { return dflt; },
    }
}

// Whether n is a number (an int or not); bools are not.
fn is_int(n: json::Node?) -> bool {
    guard n else { return false; }
    match n.val { Int => { return true; }, _ => { return false; } }
}

// The i-th element of an array, or null.
fn element(n: json::Node?, i: i64) -> json::Node? {
    guard n else { return null; }
    guard is_arr(n) else { return null; }
    var k = 0;
    var c = n.first;
    while c {
        if k == i { return c; }
        k++;
        c .= c.next;
    }
    return null;
}

fn count_of(n: json::Node?) -> i64 {
    guard n else { return 0; }
    match n.val {
        Arr a => { return a.count; },
        Obj o => { return o.count; },
        _ => { return 0; },
    }
}

// Every member of object n (copy_except with nothing to skip).
fn copy_all(out: u8[>..]&, n: json::Node?) {
    guard n else { return; }
    guard is_obj(n) else { return; }
    var c = n.first;
    while c {
        sep(out);
        json::format_key(out, c.key);
        emit(out, c);
        c .= c.next;
    }
}

// A member that is an object, or null.
fn obj_member(n: json::Node?, k: const u8[:]) -> json::Node? {
    let v = json::member(n, k);
    guard v else { return null; }
    if is_obj(v) { return v; }
    return null;
}
