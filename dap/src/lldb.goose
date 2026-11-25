// What the proxy asks LLDB, through lldb-dap. The Python adapter loaded a
// module of its own into lldb-dap's Python for this (goose_dap_lldb.py);
// here it is plain LLDB commands, sent as repl evaluations (with the
// command escape prefix) whose text is read, and DAP requests:
//
//   lldb_info       the compile units, from `image lookup -v -n main`: the
//                   program's generated C is the one with the C main, and
//                   its map is beside it
//   source_lines    the lines of a source file that have code, from
//                   `image dump line-table` of each such compile unit
//   runtime_error   the message and place of the Goose runtime error a
//                   thread is stopped in: its gs_fail or gs_panic frame from
//                   stackTrace, that frame's arguments from `frame variable`
//                   and gs_fail's message bytes from `memory read`
//
// and the debug maps, loaded once LLDB has the target (ensure_map).

import std;
import os;
import json;
import gslib.sys;
import .jsonw;
import .names;
import .store;
import .gmap;
import .proxy;
import .launch;

// An LLDB command's output (the repl result), appended to out; false if it
// fails.
fn lldb_command(cmd: const u8[:], out: u8[>..]&) -> bool { lldb_command_in(cmd, -1, out) }

// The lines of text, one at a time: F(line), its end of line left off.
fn each_line<F>(text: const u8[:]) {
    var from = 0;
    for c, i in text {
        if c == '\n' {
            var e = i;
            if e > from && text[e - 1] == '\r' { e--; }
            F(text[from..e]);
            from = i + 1;
        }
    }
    if from < text.len { F(text[from..]); }
}

// The program's compile units in C (cu_paths) and the debug maps beside
// them, NUL-ended onto maps: as goose_lldb.py finds a map, the .c's name
// with .debug.json for .c, and no older than the .c (a compiler without the
// map has rebuilt the program since).
fn lldb_info(maps: u8[>..]&) {
    var v: u8[>..] = [];
    if lldb_command("version", v) { log("lldb:", strip(v)); }
    var text: u8[>..] = [];
    guard lldb_command("image lookup -v -n main", text) else { return; }
    cu_paths.clear();
    each_line(text) { line =>
        let k = find(line, "CompileUnit:");
        if k >= 0 {
            let rest = line[k..];
            let f = find(rest, "file = \"");
            if f >= 0 {
                let p = rest[f + 8..];
                let q = find(p, "\"");
                let path = if q >= 0 { p[..q] } else { p };
                if ends_with(path, ".c") || ends_with(path, ".C") {
                    cu_paths.append(path);
                    cu_paths.push(0);
                }
            }
        }
    };
    var pos: i64 = 0;
    while pos < cu_paths.len {
        let c = next_cstring(cu_paths, pos);
        let j = str(c[..c.len - 2], ".debug.json");
        var cs = sys::Stat { .. };
        var js = sys::Stat { .. };
        if sys::stat(c, cs) != 0 || sys::stat(j, js) != 0 { continue; }
        if js.mtime_ns + 1000000 < cs.mtime_ns { continue; }
        maps.append(j);
        maps.push(0);
    }
}

// The NUL-ended strings of a list, one by one: the next from pos.
fn next_cstring(list: const u8[:], pos: i64&) -> const u8[:] {
    let start = pos;
    while pos < list.len && list[pos] != 0 { pos += 1; }
    let s = list[start..pos];
    if pos < list.len { pos += 1; }
    return s;
}

// Without an answer from LLDB: the newest
// GOOSE_HOME/build/prog/<stem>-*/<stem>.debug.json for the program.
fn guess_map_paths(maps: u8[>..]&) {
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let la, err = json::parse(launch_args, doc, dec);
    let prog, ok = get_str(la, "program");
    guard ok else { return; }
    let st = stem(basename(prog));
    var home: u8[>..] = [];
    goose_home(home);
    var root: u8[>..] = [];
    join_path(root, home, "build");
    var prog_dir: u8[>..] = [];
    join_path(prog_dir, root, "prog");
    var e: i64 = 0;
    let entries = sys::read_dir(prog_dir, e);
    guard e == 0 else { return; }
    var best: u8[>..] = [];
    var best_m: i64 = 0;
    for d in entries {
        if !starts_with(d.name, str(st, "-")) { continue; }
        var dir: u8[>..] = [];
        join_path(dir, prog_dir, d.name);
        var j: u8[>..] = [];
        join_path(j, dir, str(st, ".debug.json"));
        var s = sys::Stat { .. };
        if sys::stat(j, s) != 0 { continue; }
        if best.len == 0 || s.mtime_ns > best_m {
            best.clear();
            best.append(j);
            best_m = s.mtime_ns;
        }
    }
    if best.len > 0 { maps.append(best); maps.push(0); }
}

// Loads the debug maps (again, with force), once lldb-dap has the target.
fn ensure_map(force: bool) {
    if map_loaded && !force { return; }
    map_loaded = true;
    var paths: u8[>..] = [];
    lldb_info(paths);
    if paths.len == 0 { guess_map_paths(paths); }
    map_text.clear();
    map_text.push('[');
    var names_logged: u8[>..] = [];
    names_logged.push('[');
    var pos: i64 = 0;
    while pos < paths.len {
        let p = next_cstring(paths, pos);
        sep(names_logged);
        json::format_string(names_logged, p);
        var text: u8[>..] = [];
        if !read_file(p, text) { log("cannot read map", p); continue; }
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let root, err = json::parse(text, doc, dec);
        if !root { log("cannot read map", str(p, ": ", err)); continue; }
        sep(map_text);
        map_text.append(text);
    }
    map_text.push(']');
    names_logged.push(']');
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let m, err = json::parse(map_text, doc, dec);
    log("map:", str(names_logged, " ", map_function_count(m), " functions"));
}

// --- Line tables ------------------------------------------------------------

// Whether two paths name one file: the same text, the same with Windows'
// case and separators ignored, or the same real path.
fn same_file(a: const u8[:], b: const u8[:]) -> bool {
    if a.len == 0 || b.len == 0 { return false; }
    if a == b { return true; }
    if windows_host && a.len == b.len {
        var same = true;
        for i in a.len {
            var x = a[i];
            var y = b[i];
            if x == '\\' { x = '/'; }
            if y == '\\' { y = '/'; }
            if x >= 'A' && x <= 'Z' { x += 32; }
            if y >= 'A' && y <= 'Z' { y += 32; }
            if x != y { same = false; break; }
        }
        if same { return true; }
    }
    let ra = sys::realpath(a);
    let rb = sys::realpath(b);
    return ra.len > 0 && ra == rb;
}

// A line table entry, "0x0000000100000ab4: /path/file.goose:3:30": its
// file and line (0 if it gives none).
fn line_entry(line: const u8[:]) -> const u8[:], i64 {
    let t = strip(line);
    if !starts_with(t, "0x") { return "", 0; }
    let colon = find(t, ": ");
    if colon < 0 { return "", 0; }
    let spec = t[colon + 2..];
    let p1, n1, ok1 = cut_number(spec);
    if !ok1 { return spec, 0; }
    let p2, n2, ok2 = cut_number(p1);
    if ok2 { return p2, n2; }
    return p1, n1;
}

// "text:123" -> "text", 123.
fn cut_number(s: const u8[:]) -> const u8[:], i64, bool {
    var i = s.len;
    while i > 0 && ascii::is_digit(s[i - 1]) { i--; }
    if i == s.len || i == 0 || s[i - 1] != ':' { return s, 0, false; }
    let n, ok = parse_int(s[i..]);
    return s[..i - 1], n, ok;
}

// The lines of path that have code (a breakpoint binds to), as a JSON
// array onto out; false if LLDB gives none.
fn source_lines(path: const u8[:], out: u8[>..]&) -> bool {
    if store_get_named(line_cache, path, out) { return true; }
    var found: i64[>..] = [];
    let base = basename(path);
    var pos: i64 = 0;
    while pos < cu_paths.len {
        let cu = next_cstring(cu_paths, pos);
        var text: u8[>..] = [];
        if !lldb_command(str("image dump line-table \"", cu, "\""), text) { continue; }
        // The last file asked about, and whether it is path: an entry's
        // file is most often the one before it.
        var last: u8[>..] = [];
        var last_same = false;
        each_line(text) { l =>
            let f, n = line_entry(l);
            if n > 0 && basename(f) == base {
                if f != last {
                    last.clear();
                    last.append(f);
                    last_same = same_file(f, path);
                }
                if last_same { found.push(n); }
            }
        };
    }
    guard found.len > 0 else { return false; }
    sort(found);
    var lines: u8[>..] = [];
    lines.push('[');
    for n, i in found {
        if i > 0 && found[i - 1] == n { continue; }
        sep(lines);
        format(lines, n);
    }
    lines.push(']');
    store_put_named(line_cache, path, lines);
    out.append(lines);
    return true;
}

// --- Runtime errors -----------------------------------------------------------

// The values of a frame's variables, from `frame variable` run in that
// frame: "(int64_t) len = 32" a line.
fn frame_variables(frame_id: i64, names: const u8[:], out: u8[>..]&) -> bool {
    return lldb_command_in(str("frame variable ", names), frame_id, out);
}

// lldb_command in a frame (frame_id >= 0).
fn lldb_command_in(cmd: const u8[:], frame_id: i64, out: u8[>..]&) -> bool {
    var args: u8[>..] = [];
    args.push('{');
    kv_str(args, "expression", str(escape, cmd));
    kv_str(args, "context", "repl");
    if frame_id >= 0 { kv_int(args, "frameId", frame_id); }
    args.push('}');
    var r: u8[>..] = [];
    if !adapter_request("evaluate", args, INTERNAL_TIMEOUT_MS, r) {
        log("command failed:", cmd);
        return false;
    }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    if !request_ok(root) {
        log("command failed:", str(cmd, ": ", str_or(root, "message", "")));
        return false;
    }
    out.append(str_or(json::member(root, "body"), "result", ""));
    return true;
}

// The value of the variable called name in `frame variable`'s text, or "".
fn value_of(text: const u8[:], name: const u8[:]) -> const u8[:] {
    var at: i64 = -1;
    var len: i64 = 0;
    var from = 0;
    let head = str(") ", name, " = ");
    while from <= text.len {
        var e = from;
        while e < text.len && text[e] != '\n' { e++; }
        let line = text[from..e];
        let k = find(line, head);
        if at < 0 && starts_with(lstrip(line), "(") && k >= 0 {
            at = from + k + head.len;
            len = e - at;
            if len > 0 && text[at + len - 1] == '\r' { len--; }
        }
        from = e + 1;
    }
    if at < 0 { return text[..0]; }
    return lstrip(text[at..at + len]);
}

// The address a pointer's value starts with ("0x0000000100003f20 ..."), or 0.
fn address_of(value: const u8[:]) -> u64 {
    if !starts_with(value, "0x") { return 0; }
    var i = 2;
    while i < value.len && is_hex(value[i]) { i++; }
    let n, ok = parse_uint(value[2..i], 16);
    return if ok { n } else { 0 };
}

fn int_of(value: const u8[:]) -> i64, bool {
    var i = 0;
    if i < value.len && value[i] == '-' { i++; }
    while i < value.len && ascii::is_digit(value[i]) { i++; }
    return parse_int(value[..i]);
}

// The C string of a char *'s value, `0x... "text"` (LLDB's summary, with
// C's escapes), onto out; false if it has none.
fn c_string_of(value: const u8[:], out: u8[>..]&) -> bool {
    let q = find(value, "\"");
    guard q >= 0 else { return false; }
    var i = q + 1;
    while i < value.len {
        let c = value[i];
        if c == '"' { return true; }
        if c == '\\' && i + 1 < value.len {
            let e = value[i + 1];
            i += 2;
            if e == 'n' { out.push('\n'); }
            else if e == 't' { out.push('\t'); }
            else if e == 'r' { out.push('\r'); }
            else if e == '0' { out.push(0); }
            else if e == 'a' { out.push(7); }
            else if e == 'b' { out.push(8); }
            else if e == 'f' { out.push(12); }
            else if e == 'v' { out.push(11); }
            else if e == 'e' { out.push(27); }
            else if e == 'x' {
                var n = 0;
                var k = 0;
                while k < 2 && i < value.len && is_hex(value[i]) {
                    n = n * 16 + ascii::hex_value(value[i]);
                    i++;
                    k++;
                }
                out.push(n as! u8);
            }
            else { out.push(e); }
            continue;
        }
        out.push(c);
        i++;
    }
    return true;
}

// n bytes at addr, read with `memory read`, onto out; false if LLDB
// cannot read them.
fn read_memory(addr: u64, n: i64, out: u8[>..]&) -> bool {
    var text: u8[>..] = [];
    var cmd: u8[>..] = [];
    format(cmd, "memory read --force -s 1 -f x -c ", n, " 0x");
    format_uint(cmd, addr, 16, 0, '0');
    guard lldb_command(cmd, text) else { return false; }
    let start = out.len;
    each_line(text) { line =>
        // "0x16fdfe938: 0x69 0x6e 0x64 ...".
        let colon = find(line, ": ");
        if colon > 0 && starts_with(line, "0x") {
            var rest = line[colon + 2..];
            loop {
                rest = lstrip(rest);
                guard starts_with(rest, "0x") else { break; }
                var j = 2;
                while j < rest.len && is_hex(rest[j]) { j++; }
                let b, ok = parse_uint(rest[2..j], 16);
                guard ok else { break; }
                out.push(b as! u8);
                rest = rest[j..];
            }
        }
    };
    if out.len - start != n { out.resize(start); return false; }
    return true;
}

// The runtime error thread tid is stopped in, as a JSON object onto out
// ({function, frame, message, file, line}): gs_fail(msg, len, file, line)
// is where every failed check and abort ends (gs_abort, gs_idxfail,
// gs_ovf, ... call it); gs_panic(msg) is the runtime's own fatal error.
// False if it is not stopped in either.
fn runtime_error(tid: i64, out: u8[>..]&) -> bool {
    var r: u8[>..] = [];
    guard adapter_request("stackTrace", str("{\"threadId\":", tid, ",\"startFrame\":0,\"levels\":16}"),
                          INTERNAL_TIMEOUT_MS, r) else { return false; }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let root, err = json::parse(r, doc, dec);
    guard request_ok(root) else { return false; }
    let fs = json::member(json::member(root, "body"), "stackFrames");
    guard fs && is_arr(fs) else { return false; }
    var i = 0;
    var f = fs.first;
    while f {
        let name = str_or(f, "name", "");
        if name == "gs_fail" || name == "gs_panic" {
            var vars: u8[>..] = [];
            let wanted = if name == "gs_fail" { "msg len file line" } else { "msg" };
            guard frame_variables(get_int(f, "id", 0), wanted, vars) else { return false; }
            var text: u8[>..] = [];
            var file: u8[>..] = [];
            var has_file = false;
            var line: i64 = 0;
            if name == "gs_fail" {
                let msg = address_of(value_of(vars, "msg"));
                let n, nok = int_of(value_of(vars, "len"));
                if msg != 0 && nok && n > 0 && n < (1 << 16) { read_memory(msg, n, text); }
                has_file = c_string_of(value_of(vars, "file"), file);
                let l, lok = int_of(value_of(vars, "line"));
                line = l;
            } else {
                c_string_of(value_of(vars, "msg"), text);
            }
            out.push('{');
            kv_str(out, "function", name);
            kv_int(out, "frame", i);
            kv_str(out, "message", text);
            if has_file { kv_str(out, "file", file); kv_int(out, "line", line); }
            else { kv_null(out, "file"); kv_null(out, "line"); }
            out.push('}');
            return true;
        }
        i++;
        f .= f.next;
    }
    return false;
}
