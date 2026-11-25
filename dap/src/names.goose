// Goose names and types as C and LLDB spell them: the C name of a local,
// field or function (x_g, it_g_2, bump_g_1), Goose type text from the
// debug map (Pair<i64>&, PNode&<u32 in pool>?, u8[][>..]), the tokens of a
// watch expression, and lldb-dap's value text cleaned for a Goose value.
// The Python adapter's regular expressions are gslib's regex patterns here,
// each compiled once, as a global.

import std;
import gslib.regex;

// A Goose local, field or function as C spells it: x_g, it_g_2, bump_g_1.
// The lazy (.+?) makes the Goose name end at the first _g that has only
// an optional _?digits after it.
let NAME_RE = regex::compile("^(.+?)_g(?:_?[0-9]+)?$");

// An element's name: [3].
let INDEX_RE = regex::compile("""^\[[0-9]+\]$""");

// The Goose name in a C name: x_g -> x, twice_g_3 -> twice, pick_g4 ->
// pick; anything else as it is.
fn goose_name(c: const u8[:]) -> const u8[:] {
    regex::each_captures(NAME_RE, c) { m => return c[..m.slots[3]]; };
    return c;
}

// Whether c is a Goose name as C spells it.
fn is_goose_cname(c: const u8[:]) -> bool { regex::matches(NAME_RE, c) }

fn is_index_name(s: const u8[:]) -> bool { regex::matches(INDEX_RE, s) }

fn is_space_byte(c: u8) -> bool { c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == 11 || c == 12 }

fn strip(s: const u8[:]) -> const u8[:] {
    var a = 0;
    var b = s.len;
    while a < b && is_space_byte(s[a]) { a++; }
    while b > a && is_space_byte(s[b - 1]) { b--; }
    return s[a..b];
}

fn lstrip(s: const u8[:]) -> const u8[:] {
    var a = 0;
    while a < s.len && is_space_byte(s[a]) { a++; }
    return s[a..];
}

// The index of the bracket op that opens the one at s[close], counting
// nesting back from it; -1 if none does.
fn open_index(s: const u8[:], close: i64, op: u8, cl: u8) -> i64 {
    var depth = 0;
    var i = close;
    while i >= 0 {
        if s[i] == cl { depth++; }
        else if s[i] == op {
            depth--;
            if depth == 0 { return i; }
        }
        i--;
    }
    return -1;
}

// Pair<i64>& -> Pair<i64>; Node? -> Node; PNode&<u32 in pool>? -> PNode.
fn strip_ref(text: const u8[:]) -> const u8[:] {
    var t = strip(text);
    if starts_with(t, "const ") { t = t[6..]; }
    var changed = true;
    while changed {
        changed = false;
        if t.len > 0 && (t[t.len - 1] == '?' || t[t.len - 1] == '&') {
            t = t[..t.len - 1];
            changed = true;
        } else if t.len > 0 && t[t.len - 1] == '>' {
            let i = open_index(t, t.len - 1, '<', '>');
            if i > 0 && t[i - 1] == '&' {
                t = t[..i - 1];
                changed = true;
            }
        }
    }
    return t;
}

fn is_pointer_type(text: const u8[:]) -> bool {
    let t = strip(text);
    if t.len == 0 { return false; }
    let last = t[t.len - 1];
    return last == '?' || last == '&' || (last == '>' && find(t, "&<") >= 0);
}

// i64[>..] -> i64; u8[][>..] -> u8[]; false if not an array.
fn element_type(text: const u8[:]) -> const u8[:], bool {
    let t = strip_ref(text);
    if t.len > 0 && t[t.len - 1] == ']' {
        let i = open_index(t, t.len - 1, '[', ']');
        if i > 0 { return t[..i], true; }
    }
    return "", false;
}

// --- Tokens of an expression ------------------------------------------------------

let TOK_WS: i32 = 0;
let TOK_STR: i32 = 1;
let TOK_NUM: i32 = 2;
let TOK_ID: i32 = 3;
let TOK_OP: i32 = 4;

// A token: its kind and where it is in the text.
struct Tok { kind: i32, at: i64, end: i64 }

fn is_hex(c: u8) -> bool { ascii::hex_value(c) >= 0 }

// The Python adapter's _TOKEN_RE, a group per kind in TOK_ order: white
// space, a quoted string, a number (with a suffix), an identifier, or an
// operator (->, ::, .. or any one byte). Every byte is in some token, so
// the matches tile the text.
let TOKEN_RE = regex::compile(str(
    """(?s)(\s+)""",
    """|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""",
    """|((?:0[xX][0-9a-fA-F_]+|[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9]+)?)[A-Za-z0-9_]*)""",
    """|([A-Za-z_][A-Za-z0-9_]*)""",
    """|(->|::|\.\.|.)"""));

fn tokenize(s: const u8[:]) -> Tok[>..] {
    var out: Tok[>..] = [];
    regex::each_captures(TOKEN_RE, s) { m =>
        var kind: i32 = TOK_OP;
        for k in 1..6 { if m.slots[2 * k] >= 0 { kind = (k - 1) as i32; break; } }
        out.push(Tok { kind: kind, at: m.slots[0], end: m.slots[1] });
    };
    return out;
}

let KEYWORDS = ["true", "false", "null", "sizeof", "as", "and", "or", "not"];

fn is_keyword(s: const u8[:]) -> bool {
    for k in KEYWORDS { if k == s { return true; } }
    return false;
}

// A command, then an option: `\s*\S+\s+-` at the start.
let COMMAND_OPTION_RE = regex::compile("""^\s*\S+\s+-""");

fn command_with_option(s: const u8[:]) -> bool { regex::matches(COMMAND_OPTION_RE, s) }

// --- Values -----------------------------------------------------------------

// lldb-dap's value text for a Goose value, onto out: no address in front of
// a summary (a variable-size value or a reference is a pointer in C), and
// bool as true/false.
fn clean_value(out: u8[>..]&, text: const u8[:], ctype: const u8[:], gtype: const u8[:]) {
    // lldb-vscode 16 joins an empty value and the summary with a space.
    var value = lstrip(text);
    let rest = after_address(value);
    if rest.len > 0 && (find(ctype, "*") >= 0 || starts_with(ctype, "r_") || starts_with(ctype, "o_")) {
        value = rest;
    }
    if gtype == "bool" && value == "0" { out.append("false"); return; }
    if gtype == "bool" && value == "1" { out.append("true"); return; }
    out.append(value);
}

// A summary after an address: "0x0000000300000000 Mark.Label { ... }".
// The Python pattern's (?!<) (no lookaround here) is the check in
// after_address.
let ADDR_PREFIX_RE = regex::compile("(?s)^0x[0-9a-fA-F]+ (.+)$");

// What follows an address and a space, if it is there and does not start
// with '<'; "" otherwise.
fn after_address(v: const u8[:]) -> const u8[:] {
    regex::each_captures(ADDR_PREFIX_RE, v) { m =>
        let rest = regex::group(m, 1);
        if rest[0] == '<' { return v[..0]; }
        return v[m.slots[2]..m.slots[3]];
    };
    return v[..0];
}

// "breakpoint 2.1" (breakpoint 2, location 1).
let BREAKPOINT_ID_RE = regex::compile("""\b([0-9]+)\.[0-9]+\b""");

// The breakpoint ids in a description: "breakpoint 2.1" -> 2.
fn breakpoint_ids(s: const u8[:], out: i64[>..]&) {
    regex::each_captures(BREAKPOINT_ID_RE, s) { m =>
        let n, ok = parse_int(regex::group(m, 1));
        if ok { out.push(n); }
    };
}

// The last path element (either separator on Windows).
fn basename(path: const u8[:]) -> const u8[:] {
    var i = path.len;
    while i > 0 && !is_sep(path[i - 1]) { i--; }
    return path[i..];
}

// Everything before the last separator ("" if there is none).
fn dirname(path: const u8[:]) -> const u8[:] {
    var i = path.len;
    while i > 0 && !is_sep(path[i - 1]) { i--; }
    if i == 0 { return ""; }
    if i == 1 { return path[..1]; }
    return path[..i - 1];
}

var windows_host = false;

fn is_sep(c: u8) -> bool { c == '/' || (c == '\\' && windows_host) }

// A file name without its extension.
fn stem(name: const u8[:]) -> const u8[:] {
    var i = name.len;
    while i > 0 && name[i - 1] != '.' { i--; }
    if i <= 1 { return name; }
    return name[..i - 1];
}

// dir and name joined with the platform's separator.
fn join_path(out: u8[>..]&, dir: const u8[:], name: const u8[:]) {
    out.append(dir);
    if dir.len > 0 && !is_sep(dir[dir.len - 1]) { out.push(if windows_host { '\\' } else { '/' }); }
    out.append(name);
}

fn is_abs(p: const u8[:]) -> bool {
    if p.len > 0 && is_sep(p[0]) { return true; }
    return windows_host && p.len > 2 && p[1] == ':' && is_sep(p[2]);
}
