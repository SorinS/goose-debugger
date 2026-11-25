// stackTrace: the proxy gets all of a thread's frames from lldb-dap once
// per stop, gives Goose frames their Goose names, drops the runtime's
// (gs_*) frames, marks the rest (the C main, libc, dyld) subtle, and pages
// the result by the editor's startFrame and levels.
//
// Each frame's info, kept by frame id for scopes, variables and evaluate,
// is a JSON object: {id, cname, line, thread, goose}. Its function in the
// map is found again by cname.

import std;
import json;
import .jsonw;
import .names;
import .store;
import .gmap;
import .proxy;
import .launch;

fn c_stack_trace(req: json::Node&, text: const u8[:]) {
    let a = json::member(req, "arguments");
    let tidn = json::member(a, "threadId");
    let tid = int_value(tidn, 0);
    var frames_text: u8[>..] = [];
    if !store_get(thread_frames, tid, frames_text) {
        var raw: u8[>..] = [];
        if !fetch_frames(tidn, raw) {
            forward(req, text, "", "");
            return;
        }
        rewrite_frames(tidn, raw, frames_text);
        store_put(thread_frames, tid, frames_text);
    }
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let fs, err = json::parse(frames_text, doc, dec);
    let start = if truthy(json::member(a, "startFrame")) { get_int(a, "startFrame", 0) } else { 0 };
    let levels = if truthy(json::member(a, "levels")) { get_int(a, "levels", 0) } else { 0 };
    let total = count_of(fs);
    var body: u8[>..] = [];
    body.append("{\"stackFrames\":[");
    if fs {
        var i = 0;
        var f = fs.first;
        while f {
            if i >= start && (levels <= 0 || i < start + levels) { sep(body); emit(body, f); }
            i++;
            f .= f.next;
        }
    }
    body.append("],\"totalFrames\":");
    format(body, total);
    body.push('}');
    respond_ok(req, body);
}

// All of a thread's frames, 200 at a time, as one JSON array onto out;
// false if lldb-dap gives none.
fn fetch_frames(tid: json::Node?, out: u8[>..]&) -> bool {
    out.push('[');
    var n = 0;
    var start = 0;
    while start < 4000 {
        var args: u8[>..] = [];
        args.append("{\"threadId\":");
        emit_or_null(args, tid);
        format(args, ",\"startFrame\":", start, ",\"levels\":200}");
        var r: u8[>..] = [];
        let ok = adapter_request("stackTrace", args, INTERNAL_TIMEOUT_MS, r);
        var doc: json::Node[>..] = [];
        var dec: u8[>..] = [];
        let root, err = json::parse(r, doc, dec);
        if !ok || !request_ok(root) {
            if n == 0 { return false; }
            break;
        }
        let body = json::member(root, "body");
        let fs = json::member(body, "stackFrames");
        var got = 0;
        if fs && is_arr(fs) {
            var f = fs.first;
            while f { sep(out); emit(out, f); got++; f .= f.next; }
        }
        n += got;
        let total = json::member(body, "totalFrames");
        if got < 200 || (is_int(total) && n >= get_int(body, "totalFrames", 0)) { break; }
        start += got;
    }
    out.push(']');
    return true;
}

fn is_runtime_frame(f: json::Node?) -> bool {
    let name = str_or(f, "name", "");
    let path = str_or(json::member(f, "source"), "path", "");
    if starts_with(name, "gs_") { return true; }
    return basename(path) == "goose_runtime.c" || in_runtime_build(path);
}

// A path as compared on this platform: Windows' with forward slashes and
// lower case.
fn slashed(out: u8[>..]&, path: const u8[:]) {
    for c in path {
        if windows_host && c == '\\' { out.push('/'); }
        else if windows_host && c >= 'A' && c <= 'Z' { out.push(c + 32); }
        else { out.push(c); }
    }
}

// Whether path is in the compiler's runtime builds: GOOSE_HOME/build/runtime/.
fn in_runtime_build(path: const u8[:]) -> bool {
    var p: u8[>..] = [];
    slashed(p, path);
    var home: u8[>..] = [];
    goose_home(home);
    var root: u8[>..] = [];
    slashed(root, home);
    while root.len > 0 && root[root.len - 1] == '/' { root.pop(); }
    root.append("/build/runtime/");
    return starts_with(p, root) || find(p, "/.goose/build/runtime/") >= 0;
}

// The editor's frames from lldb-dap's raw ones (a JSON array), onto out.
fn rewrite_frames(tid: json::Node?, raw: const u8[:], out: u8[>..]&) {
    var mdoc: json::Node[>..] = [];
    var mdec: u8[>..] = [];
    let m, merr = json::parse(map_text, mdoc, mdec);
    let has_map = map_has_functions(m);
    var doc: json::Node[>..] = [];
    var dec: u8[>..] = [];
    let fs, err = json::parse(raw, doc, dec);
    // The runtime error the thread is stopped at, if any: the Goose frame
    // that failed is placed where the runtime was told it failed (see
    // below).
    var etext: u8[>..] = [];
    var edoc: json::Node[>..] = [];
    var edec: u8[>..] = [];
    store_get(exc, int_value(tid, 0), etext);
    let ex, eerr = json::parse(etext, edoc, edec);
    let e = json::member(ex, "error");
    var fixing = truthy(json::member(e, "file")) && truthy(json::member(e, "line"));
    var n = 0;
    out.push('[');
    if fs {
        var f = fs.first;
        while f {
            let cname = str_or(f, "name", "");
            let fnode = function_by_c(m, cname);
            let src = json::member(f, "source");
            let has_src = truthy(src);
            let path = str_or(src, "path", "");
            var goose = false;
            var line_fix: i64 = -1;            // the line the failing frame gets
            var name: u8[>..] = [];
            var hint: const u8[:] = "";        // the frame's presentationHint
            var src_hint: const u8[:] = "";    // its source's
            var keep = true;
            if fnode {
                let fname = str_or(fnode, "name", "");
                name.append(if fname.len > 0 { fname } else { goose_name(cname) });
                frame_bindings(fnode, name);
                goose = true;
                if ends_with(path, ".c") && has_src { src_hint = "deemphasize"; hint = "subtle"; }
            } else if is_runtime_frame(f) {
                keep = show_runtime_frames;
                hint = "subtle";
                src_hint = "deemphasize";
                name.append(cname);
            } else if ends_with(path, ".goose") && is_goose_cname(cname) {
                // A Goose frame the map does not describe (no map, or an old one).
                name.append(goose_name(cname));
                goose = true;
            } else {
                // The C main, libc, dyld: not Goose code.
                hint = "subtle";
                if cname == "main" && ends_with(path, ".c") { name.append("main (C)"); } else { name.append(cname); }
                src_hint = "deemphasize";
            }
            // At a runtime error, the Goose frame that failed called a
            // noreturn runtime function as its last instruction, so its
            // return address can fall outside its line table (lldb-dap then
            // shows disassembly). The runtime was handed the Goose file and
            // line: use them.
            if keep && goose && fixing {
                fixing = false;
                let gf, gok = get_str(fnode, "file");
                if !ends_with(path, ".goose") && gok && gf.len > 0
                   && basename(gf) == basename(str_or(e, "file", "")) {
                    line_fix = get_int(e, "line", 0);
                }
            }
            // The frame's info, kept even for a frame left out.
            var info: u8[>..] = [];
            info.push('{');
            key(info, "id");
            emit_or_null(info, json::member(f, "id"));
            kv_str(info, "cname", cname);
            key(info, "line");
            if line_fix >= 0 { format(info, line_fix); } else { emit_or_null(info, json::member(f, "line")); }
            key(info, "thread");
            emit_or_null(info, tid);
            kv_bool(info, "goose", goose);
            info.push('}');
            store_put(frames, get_int(f, "id", 0), info);
            if keep {
                sep(out);
                out.push('{');
                if line_fix >= 0 {
                    copy_except(out, f, ["name", "source", "line", "column", "presentationHint"]);
                    kv_str(out, "name", name);
                    key(out, "source");
                    out.push('{');
                    let gfile = str_or(fnode, "file", "");
                    kv_str(out, "name", basename(gfile));
                    kv_str(out, "path", gfile);
                    out.push('}');
                    kv_int(out, "line", line_fix);
                    kv_int(out, "column", 1);
                } else {
                    copy_except(out, f, ["name", "source", "presentationHint"]);
                    kv_str(out, "name", name);
                    let ph = json::member(f, "presentationHint");
                    if hint.len > 0 { kv_str(out, "presentationHint", hint); }
                    else if ph { kv_node(out, "presentationHint", ph); }
                    if has_src && src {
                        key(out, "source");
                        if src_hint.len > 0 {
                            out.push('{');
                            copy_except(out, src, ["presentationHint"]);
                            kv_str(out, "presentationHint", src_hint);
                            out.push('}');
                        } else {
                            emit(out, src);
                        }
                    } else if src {
                        kv_node(out, "source", src);
                    }
                }
                out.push('}');
                n++;
            }
            f .= f.next;
        }
    }
    out.push(']');
    // If filtering left nothing, the frames are returned unchanged.
    if n == 0 { out.clear(); out.append(raw); }
}

// A generic instance's type and function-value arguments after its name:
// pick<T=i64>, twice<F={fn line 18}>.
fn frame_bindings(f: json::Node?, name: u8[>..]&) {
    let b = json::member(f, "bindings");
    let v = json::member(f, "fnvals");
    let bs = obj_member(f, "bindings");
    let vs = obj_member(f, "fnvals");
    var parts: u8[>..] = [];
    fn part(k: const u8[:], val: json::Node&) {
        if parts.len > 0 { parts.append(", "); }
        parts.append(k);
        parts.push('=');
        let s, ok = json::as_string(val);
        if ok { parts.append(s); } else { emit(parts, val); }
    }
    if bs {
        var c = bs.first;
        while c {
            let over = json::member(vs, c.key);
            if over { part(c.key, over); } else { part(c.key, c); }
            c .= c.next;
        }
    }
    if vs {
        var c = vs.first;
        while c {
            if json::member(bs, c.key) == null { part(c.key, c); }
            c .= c.next;
        }
    }
    if parts.len > 0 { format(name, "<", parts, ">"); }
}
