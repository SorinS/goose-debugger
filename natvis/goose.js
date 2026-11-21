// goose.js: Goose values in WinDbg and cdb from the debug map, for what
// natvis cannot express. A port of the decoder in ../lldb/goose_lldb.py.
//
//   .scriptload C:\path\to\goose-debugger\natvis\goose.js
//   !goose_locals                    the frame's Goose locals in scope: name: type = value
//   !goose_globals                   the globals of the main program
//   !goose_as "grow_g", "i64[>..]"   a C expression's value read as a Goose type
//   !goose_map "C:\...\prog.debug.json"   use this map (else it is found, below)
//   dx @$scriptContents.goose_as("grow_g", "i64[>..]")   the same, as a dx value
//
// The map is found the way goose_lldb.py finds it: the program's C file is
// named in the PDB (the frames of the C `main`, below the Goose ones, are
// in it), and <stem>.debug.json lies beside it. Values are decoded from
// memory by their Goose type: the elements of a resizable, a variable-size
// value behind a bare uint8_t *, an enum's variant, a bool, a reference, a
// relative reference. It shows text only (no children to expand), and it
// works in WinDbg and cdb, not in Visual Studio, which has no script host.
"use strict";

const MAX_INLINE = 16;
const MAX_TEXT = 512;
const MAX_LEN = Math.pow(2, 40);
const MAX_DEPTH = 2;

const SCALAR_SIZES = { i8: 1, u8: 1, bool: 1, i16: 2, u16: 2, i32: 4, u32: 4, f32: 4,
                       i64: 8, u64: 8, f64: 8 };
const COUNT_TYPES = ["u8", "u16", "u32", "u64", "varint"];
const WIDTH = { u8: 1, u16: 2, u32: 4, u64: 8 };

function initializeScript() {
    return [new host.apiVersionSupport(1, 7),
            new host.functionAlias(goose_locals, "goose_locals"),
            new host.functionAlias(goose_globals, "goose_globals"),
            new host.functionAlias(goose_as_cmd, "goose_as"),
            new host.functionAlias(goose_map, "goose_map")];
}

function log(s) { host.diagnostics.debugLog(s + "\n"); }

function num(x) {
    if (typeof x === "number") return x;
    if (x !== null && x !== undefined && typeof x.asNumber === "function") return x.asNumber();
    return Number(x);
}

// ---------------------------------------------------------------------------
// The map

let mapPath = null;   // set by !goose_map
const mapCache = {};

function readFile(path) {
    const fs = host.namespace.Debugger.Utility.FileSystem;
    if (!fs.FileExists(path)) return null;
    const f = fs.OpenFile(path);
    try {
        const r = fs.CreateTextReader(f, "Utf8");
        const lines = [];
        for (const l of r.ReadLineContents()) lines.push(l);
        return lines.join("\n");
    } finally {
        f.Close();
    }
}

function loadMap(path) {
    if (mapCache[path] !== undefined) return mapCache[path];
    let m = null;
    const text = readFile(path);
    if (text !== null) {
        const d = JSON.parse(text);
        if (d.version === 1) {
            m = { path: path, funcs: {}, types: {}, typesByC: {}, globals: d.globals || null };
            for (const f of d.functions || []) if (!(f.c in m.funcs)) m.funcs[f.c] = f;
            for (const t of d.types || []) {
                if (!(t.name in m.types)) m.types[t.name] = t;
                if (t.c && !(t.c in m.typesByC)) m.typesByC[t.c] = t;
            }
        }
    }
    mapCache[path] = m;
    return m;
}

// The C file a frame of the program's own C is in: a Goose frame's source
// is a .goose file, the C `main` below them is in <stem>.c.
function findMapPath() {
    if (mapPath) return mapPath;
    for (const fr of host.currentThread.Stack.Frames) {
        let src = null;
        try { src = fr.Attributes.SourceInformation.SourceFile; } catch (e) { continue; }
        if (src && /\.c$/i.test(src)) {
            const p = src.slice(0, -2) + ".debug.json";
            if (host.namespace.Debugger.Utility.FileSystem.FileExists(p)) return p;
        }
    }
    return null;
}

function getMap() {
    const p = findMapPath();
    if (!p) throw new Error("no debug map: build with goose build -g, or give it with !goose_map \"<stem>.debug.json\"");
    const m = loadMap(p);
    if (!m) throw new Error("unreadable debug map " + p);
    return m;
}

// ---------------------------------------------------------------------------
// Goose type text, parsed (the same tuples as goose_lldb.py, as arrays)

function openOf(s, close, op, cl) {
    let depth = 0;
    for (let i = close; i >= 0; i--) {
        if (s[i] === cl) depth++;
        else if (s[i] === op && --depth === 0) return i;
    }
    return -1;
}

function topLevelDot(s) {
    let depth = 0;
    for (let i = s.length - 1; i >= 0; i--) {
        const c = s[i];
        if (c === ">") depth++;
        else if (c === "<") depth--;
        else if (c === "." && depth === 0) {
            if ((i > 0 && s[i - 1] === ".") || s[i + 1] === ".") return -1;
            return i;
        }
    }
    return -1;
}

const parseCache = {};
function parseType(text) {
    let t = parseCache[text];
    if (!t) { t = parse(text.trim()); parseCache[text] = t; }
    return t;
}

function parse(s) {
    if (s.startsWith("const ")) return parse(s.slice(6).trim());
    if (s.endsWith("?")) {
        const inner = parse(s.slice(0, -1));
        if (inner[0] === "ref") return ["opt", inner[1]];
        if (inner[0] === "rel") return ["rel", inner[1], inner[2], inner[3], true];
        return ["opt", inner];
    }
    if (s.endsWith(">")) {
        const i = openOf(s, s.length - 1, "<", ">");
        if (i > 0 && s[i - 1] === "&") {
            const args = s.slice(i + 1, -1).split(" in ");
            return ["rel", parse(s.slice(0, i - 1)), args[0].trim(), args.length > 1 ? args[1].trim() : null, false];
        }
    }
    if (s.endsWith("&")) return ["ref", parse(s.slice(0, -1))];
    if (s.endsWith("]")) {
        const i = openOf(s, s.length - 1, "[", "]");
        const inner = s.slice(i + 1, -1).trim();
        const elem = parse(s.slice(0, i));
        if (inner === "") return ["array", elem, "var", "u32"];
        if (inner === ":") return ["array", elem, "slice", null];
        if (inner === ">..") return ["array", elem, "grow", null];
        if (inner === ">..<") return ["array", elem, "shrink", null];
        if (inner === "..") return ["array", elem, "cap", null];
        if (inner.startsWith("..")) return ["array", elem, "limited", parseInt(inner.slice(2), 10)];
        if (COUNT_TYPES.indexOf(inner) >= 0) return ["array", elem, "var", inner];
        return ["array", elem, "fixed", parseInt(inner, 10)];
    }
    if (s.endsWith("..")) return ["varmode", s.slice(0, -2)];
    if (s in SCALAR_SIZES || s === "varint") return ["scalar", s];
    const i = topLevelDot(s);
    if (i > 0) return ["variant", s.slice(0, i), s.slice(i + 1)];
    return ["named", s];
}

function typeText(t) {
    switch (t[0]) {
        case "scalar": case "named": return t[1];
        case "varmode": return t[1] + "..";
        case "variant": return t[1] + "." + t[2];
        case "ref": return typeText(t[1]) + "&";
        case "opt": return typeText(t[1]) + "?";
        case "rel": return typeText(t[1]) + "&<" + t[2] + (t[3] ? " in " + t[3] : "") + ">" + (t[4] ? "?" : "");
    }
    const e = typeText(t[1]);
    const suffix = { slice: "[:]", grow: "[>..]", shrink: "[>..<]", cap: "[..]" }[t[2]];
    if (suffix) return e + suffix;
    if (t[2] === "limited") return e + "[.." + t[3] + "]";
    if (t[2] === "fixed") return e + "[" + t[3] + "]";
    return e + (t[3] === "u32" ? "[]" : "[" + t[3] + "]");
}

function lenWidth(k) { return k < 256 ? 1 : k < 65536 ? 2 : k < 4294967296 ? 4 : 8; }

// ---------------------------------------------------------------------------
// Memory

function bytes(addr, n) {
    if (!addr) throw new Error("null");
    const v = host.memory.readMemoryValues(addr, n, 1);
    const out = new Array(n);
    for (let i = 0; i < n; i++) out[i] = num(v[i]);
    return out;
}

function u32(addr) { return num(host.memory.readMemoryValues(addr, 1, 4)[0]) >>> 0; }

function uint(addr, n) {
    if (n === 8) {
        const lo = u32(addr), hi = u32(addr + 4);
        return hi * 4294967296 + lo;   // exact below 2^53: lengths and addresses
    }
    const b = bytes(addr, n);
    let v = 0;
    for (let i = n - 1; i >= 0; i--) v = v * 256 + b[i];
    return v;
}

function sint(addr, n) {
    const v = uint(addr, n);
    const top = Math.pow(2, 8 * n - 1);
    return v >= top ? v - 2 * top : v;
}

// 64-bit integers as decimal text, exact: four 16-bit limbs.
function int64Text(addr, signed) {
    const b = bytes(addr, 8);
    let limbs = [b[0] | b[1] << 8, b[2] | b[3] << 8, b[4] | b[5] << 8, b[6] | b[7] << 8];
    let neg = false;
    if (signed && b[7] & 0x80) {
        neg = true;
        let carry = 1;
        limbs = limbs.map(x => { const y = (~x & 0xffff) + carry; carry = y >> 16; return y & 0xffff; });
    }
    let digits = "";
    while (limbs.some(x => x !== 0)) {
        let rem = 0;
        for (let i = 3; i >= 0; i--) {
            const cur = rem * 65536 + limbs[i];
            limbs[i] = Math.floor(cur / 10);
            rem = cur % 10;
        }
        digits = rem + digits;
    }
    return (neg ? "-" : "") + (digits || "0");
}

function uleb(addr) {
    let v = 0, mul = 1;
    for (let i = 0; i < 10; i++) {
        const b = bytes(addr + i, 1)[0];
        v += (b & 0x7f) * mul;
        mul *= 128;
        if (!(b & 0x80)) return [v, i + 1];
    }
    throw new Error("bad LEB128");
}

function varint(addr) {
    const [u, n] = uleb(addr);
    const half = Math.floor(u / 2);
    return [u % 2 ? -half - 1 : half, n];
}

function floatText(addr, single) {
    const b = bytes(addr, single ? 4 : 8);
    const dv = new DataView(new ArrayBuffer(8));
    b.forEach((x, i) => dv.setUint8(i, x));
    const x = single ? dv.getFloat32(0, true) : dv.getFloat64(0, true);
    if (x !== x) return "nan";
    if (!isFinite(x)) return x > 0 ? "inf" : "-inf";
    let s = String(x);
    if (single) {
        for (let p = 1; p < 10; p++) {
            const c = x.toPrecision(p);
            if (Math.fround(parseFloat(c)) === x) { s = String(parseFloat(c)); break; }
        }
    }
    if (!/[.en]/.test(s)) s += ".0";
    return s;
}

function quoteBytes(data, total) {
    let s = '"';
    for (const b of data) {
        if (b === 0x22) s += '\\"';
        else if (b === 0x5c) s += "\\\\";
        else if (b === 0x0a) s += "\\n";
        else if (b === 0x09) s += "\\t";
        else if (b === 0x0d) s += "\\r";
        else if (b >= 0x20 && b < 0x7f) s += String.fromCharCode(b);
        else s += "\\x" + (b < 16 ? "0" : "") + b.toString(16);
    }
    s += '"';
    if (total > data.length) s += "... (" + total + " bytes)";
    return s;
}

function hex(a) { return "0x" + a.toString(16); }

// ---------------------------------------------------------------------------
// The decoder: text(addr, t) -> [text, size or null]

function Decoder(map) { this.map = map; }

Decoder.prototype.tdef = function (name) { return this.map.types[name] || null; };

Decoder.prototype.variant = function (td, name) {
    if (!td) return null;
    for (const v of td.variants || []) if (v.name === name) return v;
    return null;
};

Decoder.prototype.variantByTag = function (td, tag) {
    for (const v of td.variants || []) if (v.tag === tag) return v;
    return null;
};

Decoder.prototype.size = function (t) {
    switch (t[0]) {
        case "scalar": return t[1] === "varint" ? null : SCALAR_SIZES[t[1]];
        case "ref": case "opt": return 8;
        case "rel": return WIDTH[t[2]] || null;
        case "array": {
            const form = t[2];
            if (form === "slice" || form === "grow" || form === "shrink") return 16;
            const es = this.size(t[1]);
            if (es === null) return null;
            if (form === "fixed") return t[3] * es;
            if (form === "limited") return lenWidth(t[3]) + t[3] * es;
            return null;
        }
        case "variant": {
            const v = this.variant(this.tdef(t[1]), t[2]);
            return v ? this.fieldsSize(v.fields || []) : null;
        }
        case "named": {
            const td = this.tdef(t[1]);
            if (!td) return null;
            if (td.kind === "enum") {
                if (!td.fixed) return null;
                let mx = 0;
                for (const v of td.variants || []) {
                    const s = this.fieldsSize(v.fields || []);
                    if (s === null) return null;
                    mx = Math.max(mx, s);
                }
                return 1 + mx;
            }
            if (!td.c) return null;
            return this.fieldsSize(td.fields || []);
        }
    }
    return null;
};

Decoder.prototype.fieldsSize = function (fields) {
    let n = 0;
    for (const f of fields) {
        const s = this.size(parseType(f.type));
        if (s === null) return null;
        n += s;
    }
    return n;
};

Decoder.prototype.text = function (addr, t, depth) {
    depth = depth || 0;
    switch (t[0]) {
        case "scalar": return this.scalar(addr, t[1]);
        case "ref": case "opt": {
            const p = uint(addr, 8);
            if (p === 0) return ["null", 8];
            return [this.target(p, t[1], depth), 8];
        }
        case "rel": {
            const w = WIDTH[t[2]];
            if (!w) return ["<" + t[2] + " offset>", null];
            const a = this.relTarget(addr, t);
            return [a === null ? "null" : this.target(a, t[1], depth), w];
        }
        case "array": return this.array(addr, t, depth);
        case "named": {
            const td = this.tdef(t[1]);
            if (!td) return ["<" + t[1] + ": not in the debug map>", null];
            if (td.kind === "enum") return this.enumText(addr, td, !!td.fixed, depth);
            const [body, n] = this.fields(addr, td.fields || [], depth);
            return [td.name + " " + body, n];
        }
        case "varmode": {
            const td = this.tdef(t[1]);
            if (!td) return ["<" + t[1] + "..: not in the debug map>", null];
            return this.enumText(addr, td, false, depth);
        }
        case "variant": {
            const td = this.tdef(t[1]);
            const v = this.variant(td, t[2]);
            if (!v) return ["<" + t[1] + "." + t[2] + ": not in the debug map>", null];
            const [body, n] = this.fields(addr, v.fields || [], depth);
            return [(v.fields || []).length ? td.name + "." + t[2] + " " + body : td.name + "." + t[2], n];
        }
    }
    return ["?", null];
};

Decoder.prototype.target = function (addr, t, depth) {
    if (depth >= MAX_DEPTH) return hex(addr);
    return this.text(addr, t, depth + 1)[0];
};

Decoder.prototype.relTarget = function (addr, t) {
    const w = WIDTH[t[2]];
    let off = uint(addr, w);
    if (off === 0 && t[4]) return null;
    if (t[3] === null) {
        if (off >= Math.pow(2, 8 * w - 1)) off -= Math.pow(2, 8 * w);
        return addr + off;
    }
    return poolBase(this.map, t[3]) - 1 + off;
};

Decoder.prototype.scalar = function (addr, name) {
    if (name === "varint") { const [v, n] = varint(addr); return [String(v), n]; }
    const n = SCALAR_SIZES[name];
    if (name === "bool") return [uint(addr, 1) ? "true" : "false", 1];
    if (name === "f64") return [floatText(addr, false), 8];
    if (name === "f32") return [floatText(addr, true), 4];
    if (n === 8) return [int64Text(addr, name[0] === "i"), 8];
    return [String(name[0] === "i" ? sint(addr, n) : uint(addr, n)), n];
};

// [first element's address, count, stored size or null]
Decoder.prototype.layout = function (addr, t) {
    const form = t[2], n = t[3];
    if (form === "fixed") {
        const es = this.size(t[1]);
        return [addr, n, es === null ? null : n * es];
    }
    if (form === "limited") {
        const w = lenWidth(n), es = this.size(t[1]);
        return [addr + w, uint(addr, w), es === null ? null : w + n * es];
    }
    if (form === "slice" || form === "grow" || form === "shrink")
        return [uint(addr, 8), sint(addr + 8, 8), 16];
    if (form === "cap") {
        const cap = uint(addr, 4), ln = uint(addr + 4, 4), es = this.size(t[1]);
        return [addr + 8, ln, es === null ? null : 8 + cap * es];
    }
    if (n === "varint") { const [cnt, w] = uleb(addr); return [addr + w, cnt, null]; }
    const w = WIDTH[n];
    return [addr + w, uint(addr, w), null];
};

Decoder.prototype.array = function (addr, t, depth) {
    let [first, cnt, stored] = this.layout(addr, t);
    if (cnt < 0 || cnt > MAX_LEN) return ["<invalid len=" + cnt + ">", stored];
    const et = t[1];
    if (et[0] === "scalar" && et[1] === "u8") {
        const data = cnt ? bytes(first, Math.min(cnt, MAX_TEXT)) : [];
        if (stored === null && t[2] === "var") stored = first - addr + cnt;
        return [quoteBytes(data, cnt), stored];
    }
    const es = this.size(et);
    const parts = [];
    let a = first;
    for (let i = 0; i < cnt; i++) {
        let txt, sz;
        if (i < MAX_INLINE) [txt, sz] = this.text(a, et, depth);
        else if (es === null) [txt, sz] = this.text(a, et, MAX_DEPTH);
        else break;
        if (i < MAX_INLINE) parts.push(txt);
        if (es !== null) a += es;
        else if (sz === null) break;
        else a += sz;
    }
    if (cnt > MAX_INLINE) parts.push("... (" + cnt + " elements)");
    if (stored === null && t[2] === "var") {
        if (es !== null) stored = first - addr + cnt * es;
        else if (cnt <= MAX_INLINE || es === null) stored = a - addr;
    }
    return ["[" + parts.join(", ") + "]", stored];
};

Decoder.prototype.fields = function (addr, fields, depth) {
    const parts = [];
    let a = addr;
    for (let i = 0; i < fields.length; i++) {
        const f = fields[i];
        const [txt, sz] = this.text(a, parseType(f.type), depth);
        parts.push(f.name + ": " + txt);
        if (sz === null) {
            if (i + 1 < fields.length) parts.push("...");
            return ["{ " + parts.join(", ") + " }", null];
        }
        a += sz;
    }
    return [parts.length ? "{ " + parts.join(", ") + " }" : "{}", a - addr];
};

Decoder.prototype.enumText = function (addr, td, fixed, depth) {
    const tag = uint(addr, 1);
    const v = this.variantByTag(td, tag);
    const size = fixed ? this.size(["named", td.name]) : null;
    if (!v) return [td.name + " #" + tag, size];
    const name = td.name + "." + v.name;
    if (!(v.fields || []).length) return [name, fixed ? size : 1];
    const [body, n] = this.fields(addr + 1, v.fields, depth);
    return [name + " " + body, fixed ? size : (n === null ? null : 1 + n)];
};

// ---------------------------------------------------------------------------
// Where values are

function evalNum(expr) { return num(host.evaluateExpression("(unsigned __int64)(" + expr + ")")); }

// The address a Goose value of type t held in the C expression `expr`
// starts at: a variable-size value (and any pointer that is not a T& or T?)
// is a pointer to its bytes; everything else is in place.
function location(expr, t) {
    let tn = "";
    try { tn = String(host.evaluateExpression(expr).targetType.name); } catch (e) { }
    if (/\*\s*$/.test(tn) && t[0] !== "ref" && t[0] !== "opt") return evalNum(expr);
    return evalNum("&(" + expr + ")");
}

function globalsBlock(map) {
    const g = map.globals || {};
    return g.main || "gs_globals_main";
}

function poolBase(map, pool) {
    const path = pool.split(".").map(p => p + "_g").join(".");
    return evalNum(globalsBlock(map) + "." + path + ".base");
}

function show(map, expr, t) {
    try {
        return new Decoder(map).text(location(expr, t), t, 0)[0];
    } catch (e) {
        return "<" + (e && e.message ? e.message : e) + ">";
    }
}

// ---------------------------------------------------------------------------
// Commands

function currentFrame() {
    try {
        const f = host.namespace.Debugger.State.DebuggerVariables.curframe;
        if (f) return f;
    } catch (e) { }
    return host.currentThread.Stack.Frames[0];
}

function goose_locals() {
    let map;
    try { map = getMap(); } catch (e) { log(e.message); return; }
    const fr = currentFrame();
    const si = fr.Attributes.SourceInformation;
    const fname = String(si.FunctionName);
    const line = num(si.SourceLine);
    const f = map.funcs[fname];
    if (!f) { log("goose_locals: " + fname + " is not a Goose function in " + map.path); return; }
    const locals = (f.locals || []).filter(l => l.line <= line && line <= l.end);
    locals.sort((a, b) => (b.param ? 1 : 0) - (a.param ? 1 : 0));
    for (const l of locals) {
        const t = parseType(l.type);
        log(l.name + ": " + l.type + " = " + show(map, l.c, t));
    }
}

function goose_globals() {
    let map;
    try { map = getMap(); } catch (e) { log(e.message); return; }
    const g = map.globals;
    if (!g || !(g.vars || []).length) { log("goose_globals: no globals in " + map.path); return; }
    for (const v of g.vars) {
        const t = parseType(v.type);
        log(v.name + ": " + v.type + " = " + show(map, globalsBlock(map) + "." + v.c, t));
    }
}

function goose_as(expr, type) {
    const map = getMap();
    return show(map, String(expr), parseType(String(type)));
}

function goose_as_cmd(expr, type) { log(goose_as(expr, type)); }

function goose_map(path) {
    mapPath = path ? String(path) : null;
    if (mapPath) {
        delete mapCache[mapPath];
        log(loadMap(mapPath) ? "goose: map " + mapPath : "goose: cannot read a version 1 map at " + mapPath);
    }
}
