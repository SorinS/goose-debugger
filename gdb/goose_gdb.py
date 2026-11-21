"""GDB pretty-printers, commands and a frame filter for programs built by the
Goose compiler: the gdb counterpart of lldb/goose_lldb.py.

Goose compiles to C, so gdb sees the generated C types. This module
recognizes them by name and shape and shows them as Goose values:

  sl_T                  T[:] slice           {T *data; int64_t len;}
  aN_T                  T[N] fixed array     {T e[N];}
  lN_T                  T[..N] limited array {<uint> len; T e[N];}
  gs_rhdr               T[>..], T[>..<]      {uint8_t *base; int64_t len;}
  gs_rref, gs_pref      a reference to one   {gs_rhdr *hdr; gs_stack *stk; ...}
  r_T, o_T              T& and T?            typedefs of T *
  Name_g, Name_g_Args   struct
  Name_g {tag; u}       enum (fixed mode)    {uint8_t tag; union {X_p v_X_g; ...} u;}
  Name_g {tag}          enum, no payloads    {uint8_t tag;}
  Name_g_Variant_p      a variant's payload, the type of a Shape.Circle value

Load it with `source /path/to/goose_gdb.py` (or from ~/.gdbinit).

A compiler with the debug map (goose -g writes <stem>.debug.json beside
the generated <stem>.c) says what the C cannot: each function's and local's
Goose name and type, the line range a local is in scope over, and the
fields and variants of the types they name. Where the map is found (the
compile unit's .c with .debug.json in place of .c, no older than the .c),
values are decoded from memory by their Goose type: the elements of a
resizable, a variable-size value behind a bare uint8_t *, an enum's variant,
a bool, a reference against an optional, a relative reference. Set
GOOSE_GDB_MAP=0 to ignore the map.

Without a map, tag names come from the DWARF (gdb on Linux keeps the
anonymous enum of tag constants) and else from the generated C file named
by the compile unit, when it still exists. GOOSE_GDB_CSOURCE=0 turns the
C file off.

Commands: goose-locals, goose-bt [N], goose-as VAR TYPE. A frame filter
gives `bt` Goose function names and parameters. `set goose-children on`
makes the printers list children (for MI front ends such as VS Code's
cpptools, which expand values); the CLI then prints them after the summary.

Nothing here may raise into gdb: every entry point catches its errors and
falls back to showing less. Written for gdb 9.2 and Python 3.8 up.
"""

import json
import os
import re
import struct

import gdb

try:
    from gdb.FrameDecorator import FrameDecorator
except ImportError:  # gdb built without the frame filter API
    FrameDecorator = None

NAME = "goose"

# Caps, so that a garbage header (an uninitialized local) cannot make gdb
# read gigabytes or list millions of children.
MAX_CHILDREN = 256
MAX_TEXT = 512
MAX_INLINE = 16
MAX_SUMMARY = 400
MAX_LEN = 1 << 40
# References followed inside one summary; deeper ones show as addresses.
MAX_DEPTH = 2
# How deep the per-stop index follows a local's fields.
MAX_INDEX_DEPTH = 4

_FIXED_RE = re.compile(r"^a[0-9]+_")
_LIMITED_RE = re.compile(r"^l[0-9]+_")
# A Goose field, local or function name: x_g, it_g_2, bump_g_1, x_g2.
_NAME_RE = re.compile(r"^(.+?)_g(_?[0-9]+)?$")
# 1-byte integers by their typedef names: u8 and i8 are numbers in Goose.
_BYTE_INTS = ("uint8_t", "int8_t", "gs_u8", "gs_i8")


def _env_on(name):
    return os.environ.get(name, "1") not in ("0", "off", "no", "false")


# --------------------------------------------------------------------------
# State that lives for one stop: caches cleared when the inferior stops or
# the program's object files change.

_gen = [0]
_registry = {}
_index_cache = {}
_type_cache = {}
_cu_cache = {}
_tag_cache = {}


def _on_stop(*_):
    _gen[0] += 1
    _registry.clear()
    _index_cache.clear()


def _on_objfiles(*_):
    _on_stop()
    _type_cache.clear()
    _cu_cache.clear()
    _tag_cache.clear()
    _map_cache.clear()


# --------------------------------------------------------------------------
# Names


def goose_name(cname):
    """x_g -> x, it_g_2 -> it, bump_g_1 -> bump; anything else unchanged."""
    m = _NAME_RE.match(cname or "")
    return m.group(1) if m else cname


def goose_type_name(cname):
    """Shape_g -> Shape, Pair_g_i64 -> Pair<i64>. Best effort: type arguments
    are shown as they are mangled."""
    if not cname:
        return cname
    if cname.endswith("_g"):
        return cname[:-2]
    i = cname.find("_g_")
    if i > 0:
        return "%s<%s>" % (cname[:i], cname[i + 3:])
    return cname


# --------------------------------------------------------------------------
# Types


def type_name(t):
    """The name a C type is written with, typedef kept, qualifiers and the
    struct keyword dropped."""
    try:
        n = str(t.unqualified())
    except Exception:
        n = str(t)
    for q in ("const ", "volatile ", "struct "):
        while n.startswith(q):
            n = n[len(q):]
    return n


def _code(t):
    return t.strip_typedefs().code


def _is_ptr(t):
    return _code(t) == gdb.TYPE_CODE_PTR


def _fields(t):
    s = t.strip_typedefs()
    if s.code != gdb.TYPE_CODE_STRUCT:
        return None
    return s.fields()


def _member(t, name):
    s = t.strip_typedefs()
    if s.code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
        return None
    for f in s.fields():
        if f.name == name:
            return f.type
    return None


def _is_goose_name(name):
    return name.endswith("_g") or "_g_" in name


def classify(t):
    """The kind of Goose value a C type holds, or None."""
    try:
        name = type_name(t)
        if t.code == gdb.TYPE_CODE_TYPEDEF and _is_ptr(t):
            if name.startswith("r_"):
                return "ref"
            if name.startswith("o_"):
                return "opt"
        if name == "uint8_t *":
            return "bytes"
        if name in _BYTE_INTS:
            return "byte"
        fs = _fields(t)
        if fs is None:
            return None
        names = [f.name for f in fs]
        if name == "gs_rhdr" and names == ["base", "len"]:
            return "rhdr"
        if name in ("gs_rref", "gs_pref") and names[:2] == ["hdr", "stk"]:
            return "rref"
        if name.startswith("sl_") and names == ["data", "len"]:
            return "slice"
        if _FIXED_RE.match(name) and names == ["e"]:
            return "fixed"
        if _LIMITED_RE.match(name) and names == ["len", "e"]:
            return "limited"
        if not _is_goose_name(name) and not name.endswith("_p"):
            return None
        if names == ["tag", "u"] and _code(fs[1].type) == gdb.TYPE_CODE_UNION:
            return "adt"
        if names == ["tag"]:
            return "cenum"
        if name.endswith("_p") and "_g_" in name:
            return "payload"
        if _is_goose_name(name):
            return "struct"
    except Exception:
        pass
    return None


def lookup_type(name):
    """A C type of the program by name, or None."""
    if name in _type_cache:
        return _type_cache[name]
    t = None
    for n in (name, "struct " + name):
        try:
            t = gdb.lookup_type(n)
            break
        except Exception:
            pass
    _type_cache[name] = t
    return t


def _u64():
    return lookup_type("unsigned long long")


def _ptr(v):
    """A pointer value as an int."""
    return int(v.cast(_u64())) & 0xFFFFFFFFFFFFFFFF


def _addr(v):
    a = v.address
    return None if a is None else _ptr(a)


# --------------------------------------------------------------------------
# Compile units and tag names


def _cu_paths():
    """The generated .c files of the program: the compile unit of `main`,
    else every .c that `info sources` names."""
    if "paths" in _cu_cache:
        return _cu_cache["paths"]
    paths = []
    try:
        sym = gdb.lookup_global_symbol("main")
        if sym is not None and sym.symtab is not None:
            p = sym.symtab.fullname()
            if p.endswith(".c"):
                paths.append(p)
    except Exception:
        pass
    if not paths:
        try:
            out = gdb.execute("info sources", to_string=True)
            for p in re.findall(r"(/[^\s,]+\.c)(?=[,\s]|$)", out):
                if p not in paths:
                    paths.append(p)
        except Exception:
            pass
    _cu_cache["paths"] = paths
    return paths


_ENUM_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)_k\s*=\s*([0-9]+)\s*,")


def _tag_constants():
    """{"Shape_g_Circle_g": 0, ...}: the enumerators X_k of the tag enums,
    from the DWARF (gdb keeps the anonymous enum) and from the generated C."""
    if "consts" in _tag_cache:
        return _tag_cache["consts"]
    consts = {}
    try:
        sym = gdb.lookup_global_symbol("main")
        blk = sym.symtab.static_block() if sym is not None and sym.symtab else None
        if blk is not None:
            for s in blk:
                if s.addr_class == gdb.SYMBOL_LOC_CONST and s.name.endswith("_k"):
                    try:
                        consts[s.name[:-2]] = int(s.value())
                    except Exception:
                        pass
    except Exception:
        pass
    if _env_on("GOOSE_GDB_CSOURCE"):
        for p in _cu_paths():
            try:
                if os.path.getsize(p) > 64 << 20:
                    continue
                with open(p, "r", errors="replace") as f:
                    text = f.read()
            except Exception:
                continue
            for block in re.finditer(r"enum\s*\{([^}]*)\}\s*;", text):
                for m in _ENUM_RE.finditer(block.group(1)):
                    consts.setdefault(m.group(1), int(m.group(2)))
    _tag_cache["consts"] = consts
    return consts


def tag_name(enum_cname, tag):
    """The variant name for a tag value, or None when it cannot be known:
    from the debug map, else from the DWARF or the generated C."""
    try:
        m = get_map()
        if m is not None:
            td = m.types_by_c.get(enum_cname)
            if td is not None:
                for v in td.get("variants", []):
                    if v.get("tag") == tag:
                        return v.get("name")
        prefix = enum_cname + "_"
        for cname, v in _tag_constants().items():
            if v == tag and cname.startswith(prefix) and cname.endswith("_g"):
                return cname[len(prefix):-2]
    except Exception:
        pass
    return None


def payload_owner(payload_cname):
    """For Shape_g_Circle_p, ("Shape_g", "Circle"): the enum whose union has a
    member v_Circle_g of this type."""
    try:
        stem = payload_cname[:-2]
        i = stem.find("_g_")
        if i <= 0:
            return None, None
        j = stem.find("_", i + 2)
        while j > 0:
            ename, variant = stem[:j], stem[j + 1:]
            et = lookup_type(ename)
            if et is not None:
                u = _member(et, "u")
                if u is not None and _member(u, "v_%s_g" % variant) is not None:
                    return ename, variant
            j = stem.find("_", j + 1)
        return stem[:i + 2], stem[i + 3:]
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------
# Memory and text


def _clip(s, n=MAX_SUMMARY):
    return s if len(s) <= n else s[:n - 3] + "..."


def read_mem(addr, n):
    if addr == 0:
        raise ValueError("null")
    data = bytes(gdb.selected_inferior().read_memory(addr, n))
    if len(data) != n:
        raise ValueError("unreadable at 0x%x" % addr)
    return data


def quote_bytes(data, total):
    out = ['"']
    for b in data:
        if b == 0x22:
            out.append('\\"')
        elif b == 0x5C:
            out.append("\\\\")
        elif b == 0x0A:
            out.append("\\n")
        elif b == 0x09:
            out.append("\\t")
        elif b == 0x0D:
            out.append("\\r")
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append("\\x%02x" % b)
    out.append('"')
    s = "".join(out)
    if total > len(data):
        s += "... (%d bytes)" % total
    return s


def read_text(addr, n):
    if n == 0:
        return '""'
    if addr == 0:
        return "<null data, len=%d>" % n
    try:
        return quote_bytes(bytearray(read_mem(addr, min(n, MAX_TEXT))), n)
    except Exception:
        return "<unreadable at 0x%x, len=%d>" % (addr, n)


def _fmt_float(x, single):
    if x != x:
        return "nan"
    if x in (float("inf"), float("-inf")):
        return "inf" if x > 0 else "-inf"
    s = repr(x)
    if single:
        for p in range(1, 10):
            c = "%.*g" % (p, x)
            if struct.unpack("<f", struct.pack("<f", float(c)))[0] == x:
                s = c
                break
    if not any(ch in s for ch in ".en"):
        s += ".0"
    return s


def _is_u8(t):
    s = t.strip_typedefs()
    return s.sizeof == 1 and s.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_CHAR)


def _scalar_text(v):
    """A scalar as text: integers in decimal (u8 is a number in Goose, not a
    character), floats as Goose prints them; None for anything else."""
    s = v.type.strip_typedefs()
    c = s.code
    if c == gdb.TYPE_CODE_FLT:
        return _fmt_float(float(v), s.sizeof == 4)
    if c == gdb.TYPE_CODE_BOOL:
        return "true" if int(v) else "false"
    if c in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_CHAR, gdb.TYPE_CODE_ENUM):
        return str(int(v))
    return None


def value_text(v, depth=0):
    """A value as one line of text, for inside a summary."""
    try:
        s = _scalar_text(v)
        if s is not None:
            return s
        if classify(v.type) not in (None, "byte"):
            s = summary_text(v, depth)
            if s is not None:
                return s
        t = v.type.strip_typedefs()
        if t.code == gdb.TYPE_CODE_PTR:
            # A reference is transparent in Goose: show what a field such as
            # `line: i64&` points at, when it is a readable scalar, and what a
            # Node_g *next_g points at, when it is a Goose value.
            a = _ptr(v)
            if a == 0:
                return "null"
            try:
                d = v.dereference()
                d.fetch_lazy()
                s = _scalar_text(d)
                if s is not None:
                    return s
                if depth < MAX_DEPTH and classify(d.type) not in (None, "byte", "bytes"):
                    s = summary_text(d, depth + 1)
                    if s is not None:
                        return s
            except Exception:
                pass
            return "0x%x" % a
        if t.code == gdb.TYPE_CODE_STRUCT:
            return _fields_text(v, depth)
        if t.code == gdb.TYPE_CODE_ARRAY:
            lo, hi = t.range()
            n = hi - lo + 1
            return _list_text([v[i] for i in range(lo, lo + min(n, MAX_INLINE))], n, depth)
        return str(v)
    except Exception:
        return "?"


def _list_text(children, total, depth=0):
    parts = [value_text(c, depth) for c in children[:MAX_INLINE]]
    if total > MAX_INLINE:
        parts.append("... (%d elements)" % total)
    return "[" + ", ".join(parts) + "]"


def _fields_text(v, depth=0):
    parts = []
    fs = v.type.strip_typedefs().fields()
    for f in fs[:MAX_INLINE]:
        parts.append("%s: %s" % (goose_name(f.name), value_text(v[f.name], depth)))
    if len(fs) > MAX_INLINE:
        parts.append("...")
    return "{ " + ", ".join(parts) + " }" if parts else "{}"


# --------------------------------------------------------------------------
# The debug map


class GooseMap:
    """One program's <stem>.debug.json files, indexed: functions by C name,
    types by Goose name and by C name. Unknown keys are ignored."""

    def __init__(self, docs):
        self.funcs = {}
        self.types = {}
        self.types_by_c = {}
        self.globals = []            # The map's globals.vars, every compile unit's.
        self.globals_main = "gs_globals_main"
        self.globals_thread = None   # "gs_gl" when the program starts threads.
        for d in docs:
            g = d.get("globals")
            if isinstance(g, dict):
                self.globals += g.get("vars", [])
                self.globals_main = g.get("main") or self.globals_main
                self.globals_thread = g.get("thread") or self.globals_thread
            for f in d.get("functions", []):
                self.funcs.setdefault(f.get("c"), f)
            for t in d.get("types", []):
                self.types.setdefault(t.get("name"), t)
                if t.get("c"):
                    self.types_by_c.setdefault(t["c"], t)

    def local(self, fname, cname):
        f = self.funcs.get(fname)
        if f is None:
            return None
        for l in f.get("locals", []):
            if l.get("c") == cname:
                return l
        return None


_map_cache = {}


def map_path(cpath):
    return cpath[:-2] + ".debug.json" if cpath.endswith(".c") else None


def get_map():
    """The program's debug map, or None: none written, older than its .c (a
    compiler without the map rebuilt the program since), unreadable, or
    turned off with GOOSE_GDB_MAP=0. Checked once per stop."""
    if not _env_on("GOOSE_GDB_MAP"):
        return None
    g = _map_cache.get("gen")
    if g is not None and g[0] == _gen[0]:
        return g[1]
    m = None
    try:
        key = []
        for c in _cu_paths():
            j = map_path(c)
            try:
                cm, jm = os.path.getmtime(c), os.path.getmtime(j)
            except OSError:
                continue
            if jm + 1e-3 < cm:
                continue
            key.append((j, jm))
        key = tuple(key)
        if key:
            if key in _map_cache:
                m = _map_cache[key]
            else:
                docs = []
                for j, _ in key:
                    try:
                        with open(j) as f:
                            d = json.load(f)
                        # The decoder builds in version 1's layouts.
                        if d.get("version") == 1:
                            docs.append(d)
                    except Exception:
                        pass
                m = GooseMap(docs) if docs else None
                for k in [k for k in _map_cache if k != "gen"]:
                    del _map_cache[k]
                _map_cache[key] = m
    except Exception:
        m = None
    _map_cache["gen"] = (_gen[0], m)
    return m


# Goose type text, as the map writes it, parsed into tuples:
#   ("scalar", "i64")            i8 ... u64, f32, f64, bool, varint
#   ("named", "Pair<i64>")       a struct or a fixed-mode enum
#   ("varmode", "Shape")         Shape..
#   ("variant", "Shape", "Circle")
#   ("array", elem, form, n)     form: fixed (n = k), var (n = count type),
#                                limited (n = k), cap, grow, shrink, slice
#   ("ref", t), ("opt", t)       T&, T?
#   ("rel", t, width, pool, optional)   T&<u32>, T&<u32 in pool>?

_SCALAR_SIZES = {"i8": 1, "u8": 1, "bool": 1, "i16": 2, "u16": 2, "i32": 4, "u32": 4,
                 "f32": 4, "i64": 8, "u64": 8, "f64": 8, "varint": None}
_COUNT_TYPES = ("u8", "u16", "u32", "u64", "varint")
_parse_cache = {}


def _open_of(s, close_idx, op, cl):
    depth = 0
    for i in range(close_idx, -1, -1):
        if s[i] == cl:
            depth += 1
        elif s[i] == op:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _top_level_dot(s):
    """The index of the last '.' outside <...> that is not part of '..', or -1."""
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        c = s[i]
        if c == ">":
            depth += 1
        elif c == "<":
            depth -= 1
        elif c == "." and depth == 0:
            if (i > 0 and s[i - 1] == ".") or (i + 1 < len(s) and s[i + 1] == "."):
                return -1
            return i
    return -1


def parse_type(text):
    t = _parse_cache.get(text)
    if t is None:
        t = _parse(text.strip())
        _parse_cache[text] = t
    return t


def _parse(s):
    if s.startswith("const "):
        return _parse(s[6:].strip())
    if s.endswith("?"):
        inner = _parse(s[:-1])
        if inner[0] == "ref":
            return ("opt", inner[1])
        if inner[0] == "rel":
            return inner[:4] + (True,)
        return ("opt", inner)
    if s.endswith(">"):
        i = _open_of(s, len(s) - 1, "<", ">")
        if i > 0 and s[i - 1] == "&":
            args = s[i + 1:-1].split(" in ")
            pool = args[1].strip() if len(args) > 1 else None
            return ("rel", _parse(s[:i - 1]), args[0].strip(), pool, False)
    if s.endswith("&"):
        return ("ref", _parse(s[:-1]))
    if s.endswith("]"):
        i = _open_of(s, len(s) - 1, "[", "]")
        inner = s[i + 1:-1].strip()
        elem = _parse(s[:i])
        if inner == "":
            return ("array", elem, "var", "u32")
        if inner == ":":
            return ("array", elem, "slice", None)
        if inner == ">..":
            return ("array", elem, "grow", None)
        if inner == ">..<":
            return ("array", elem, "shrink", None)
        if inner == "..":
            return ("array", elem, "cap", None)
        if inner.startswith(".."):
            return ("array", elem, "limited", int(inner[2:]))
        if inner in _COUNT_TYPES:
            return ("array", elem, "var", inner)
        return ("array", elem, "fixed", int(inner))
    if s.endswith(".."):
        return ("varmode", s[:-2])
    if s in _SCALAR_SIZES:
        return ("scalar", s)
    i = _top_level_dot(s)
    if i > 0:
        return ("variant", s[:i], s[i + 1:])
    return ("named", s)


def _len_width(k):
    return 1 if k < 1 << 8 else 2 if k < 1 << 16 else 4 if k < 1 << 32 else 8


def _width_bytes(w):
    return {"u8": 1, "u16": 2, "u32": 4, "u64": 8}.get(w)


class Decoder:
    """Reads Goose values out of the inferior by their Goose type. Generated
    types are packed (#pragma pack 1), so a field's offset is the sum of the
    sizes before it. Every method may raise; callers catch."""

    def __init__(self, gmap):
        self.map = gmap

    # -- memory

    def uint(self, addr, n):
        return int.from_bytes(read_mem(addr, n), "little")

    def sint(self, addr, n):
        return int.from_bytes(read_mem(addr, n), "little", signed=True)

    def uleb(self, addr):
        v, shift, i = 0, 0, 0
        while i < 10:
            b = self.uint(addr + i, 1)
            v |= (b & 0x7F) << shift
            shift += 7
            i += 1
            if not b & 0x80:
                return v, i
        raise ValueError("bad LEB128 at 0x%x" % addr)

    def varint(self, addr):
        u, n = self.uleb(addr)
        return (u >> 1) ^ -(u & 1), n

    # -- types

    def tdef(self, name):
        return self.map.types.get(name) if self.map else None

    def size(self, t):
        """The byte size of a fixed-size type, None for a variable one."""
        k = t[0]
        if k == "scalar":
            return _SCALAR_SIZES[t[1]]
        if k in ("ref", "opt"):
            return 8
        if k == "rel":
            return _width_bytes(t[2])
        if k == "array":
            form = t[2]
            if form in ("slice", "grow", "shrink"):
                return 16
            es = self.size(t[1])
            if es is None:
                return None
            if form == "fixed":
                return t[3] * es
            if form == "limited":
                return _len_width(t[3]) + t[3] * es
            return None
        if k == "variant":
            v = self._variant(self.tdef(t[1]), t[2])
            return self._fields_size(v.get("fields", [])) if v is not None else None
        if k == "named":
            td = self.tdef(t[1])
            if td is None or td.get("variable"):
                return None
            if td.get("kind") == "enum":
                if not td.get("fixed"):
                    return None
                sizes = [self._fields_size(v.get("fields", [])) for v in td.get("variants", [])]
                if any(x is None for x in sizes):
                    return None
                return 1 + max(sizes + [0])
            return self._fields_size(td.get("fields", []))
        return None

    def _fields_size(self, fields):
        n = 0
        for f in fields:
            s = self.size(parse_type(f["type"]))
            if s is None:
                return None
            n += s
        return n

    @staticmethod
    def _variant(td, name):
        if td is None:
            return None
        for v in td.get("variants", []):
            if v.get("name") == name:
                return v
        return None

    @staticmethod
    def _variant_by_tag(td, tag):
        for v in td.get("variants", []):
            if v.get("tag") == tag:
                return v
        return None

    # -- values as text: (text, size); size is None where it is not known

    def text(self, addr, t, depth=0):
        k = t[0]
        if k == "scalar":
            return self._scalar(addr, t[1])
        if k in ("ref", "opt"):
            p = self.uint(addr, 8)
            if p == 0:
                return ("null", 8)
            return (self._target_text(p, t[1], depth), 8)
        if k == "rel":
            w = _width_bytes(t[2])
            if w is None:
                return ("<%s offset>" % t[2], None)
            target = self.rel_target(addr, t)
            if target is None:
                return ("null", w)
            return (self._target_text(target, t[1], depth), w)
        if k == "array":
            return self._array(addr, t, depth)
        if k == "named":
            td = self.tdef(t[1])
            if td is None:
                return ("<%s: not in the debug map>" % t[1], self.size(t))
            if td.get("kind") == "enum":
                return self._enum(addr, td, td.get("fixed", False), depth)
            body, n = self._fields(addr, td.get("fields", []), depth)
            return ("%s %s" % (td["name"], body), n)
        if k == "varmode":
            td = self.tdef(t[1])
            if td is None:
                return ("<%s..: not in the debug map>" % t[1], None)
            return self._enum(addr, td, False, depth)
        if k == "variant":
            td = self.tdef(t[1])
            v = self._variant(td, t[2])
            if v is None:
                return ("<%s.%s: not in the debug map>" % (t[1], t[2]), None)
            body, n = self._fields(addr, v.get("fields", []), depth)
            if v.get("fields"):
                return ("%s.%s %s" % (td["name"], t[2], body), n)
            return ("%s.%s" % (td["name"], t[2]), n)
        return ("?", None)

    def _target_text(self, addr, t, depth):
        # Depth counts the references followed, which is what can cycle.
        if depth >= MAX_DEPTH:
            return "0x%x" % addr
        return self.text(addr, t, depth + 1)[0]

    def rel_target(self, addr, t):
        """The address a relative reference at addr points to, or None for
        null. Self-relative: the field's own address plus the signed offset.
        Pool-relative: the pool's base, minus one, plus the offset."""
        w = _width_bytes(t[2])
        off = self.uint(addr, w)
        if off == 0 and t[4]:
            return None
        if t[3] is None:
            if off >= 1 << (8 * w - 1):
                off -= 1 << (8 * w)
            return addr + off
        base = self.pool_base(t[3])
        if base is None:
            raise ValueError("pool %s not found" % t[3])
        return base - 1 + off

    def pool_base(self, pool):
        try:
            v = globals_block(self.map)
            for part in pool.split("."):
                v = v[part + "_g"]
            return _ptr(v["base"])
        except Exception:
            return None

    def _scalar(self, addr, name):
        if name == "varint":
            v, n = self.varint(addr)
            return (str(v), n)
        n = _SCALAR_SIZES[name]
        if name == "bool":
            return ("true" if self.uint(addr, 1) else "false", 1)
        if name == "f64":
            return (_fmt_float(struct.unpack("<d", read_mem(addr, 8))[0], False), 8)
        if name == "f32":
            return (_fmt_float(struct.unpack("<f", read_mem(addr, 4))[0], True), 4)
        if name.startswith("i"):
            return (str(self.sint(addr, n)), n)
        return (str(self.uint(addr, n)), n)

    def array_layout(self, addr, t):
        """(address of the first element, element count, byte size of the
        array where it is stored, or None)."""
        form, n = t[2], t[3]
        if form == "fixed":
            es = self.size(t[1])
            return addr, n, (n * es if es is not None else None)
        if form == "limited":
            w = _len_width(n)
            es = self.size(t[1])
            return addr + w, self.uint(addr, w), (w + n * es if es is not None else None)
        if form in ("slice", "grow", "shrink"):
            return self.uint(addr, 8), self.sint(addr + 8, 8), 16
        if form == "cap":
            cap, ln = self.uint(addr, 4), self.uint(addr + 4, 4)
            es = self.size(t[1])
            return addr + 8, ln, (8 + cap * es if es is not None else None)
        # var: a count, then the elements; the size is known after a walk.
        if n == "varint":
            cnt, w = self.uleb(addr)
        else:
            w = _width_bytes(n)
            cnt = self.uint(addr, w)
        return addr + w, cnt, None

    def elements(self, addr, t, limit):
        """[(address, element type)] for at most limit elements, the total
        count, and the array's stored size (None where unknown)."""
        first, cnt, stored = self.array_layout(addr, t)
        if cnt < 0 or cnt > MAX_LEN:
            raise ValueError("invalid len=%d" % cnt)
        et = t[1]
        es = self.size(et)
        out = []
        a = first
        for _ in range(min(cnt, limit)):
            out.append((a, et))
            if es is None:
                _, sz = self.text(a, et, MAX_DEPTH)
                if sz is None:
                    break
                a += sz
            else:
                a += es
        if stored is None and t[2] == "var":
            if es is not None:
                stored = (first - addr) + cnt * es
            elif len(out) == cnt:
                stored = a - addr
        return out, cnt, stored

    def _array(self, addr, t, depth):
        et = t[1]
        if et == ("scalar", "u8"):
            first, cnt, stored = self.array_layout(addr, t)
            if cnt < 0 or cnt > MAX_LEN:
                return ("<invalid len=%d>" % cnt, stored)
            data = read_mem(first, min(cnt, MAX_TEXT)) if cnt else b""
            if stored is None and t[2] == "var":
                stored = (first - addr) + cnt
            return (quote_bytes(bytearray(data), cnt), stored)
        els, cnt, stored = self.elements(addr, t, MAX_INLINE)
        parts = [self.text(a, et, depth)[0] for a, et in els]
        if cnt > len(els):
            parts.append("... (%d elements)" % cnt)
        return ("[" + ", ".join(parts) + "]", stored)

    def _fields(self, addr, fields, depth):
        """'{ x: 3, y: -4 }' and the fields' total size."""
        parts = []
        a = addr
        for f in fields:
            ft = parse_type(f["type"])
            txt, sz = self.text(a, ft, depth)
            parts.append("%s: %s" % (f["name"], txt))
            if sz is None:
                if len(parts) < len(fields):
                    parts.append("...")
                return ("{ " + ", ".join(parts) + " }", None)
            a += sz
        return ("{ " + ", ".join(parts) + " }" if parts else "{}", a - addr)

    def field_addrs(self, addr, fields):
        """[(name, address, type)] for the fields that can be located."""
        out = []
        a = addr
        for f in fields:
            ft = parse_type(f["type"])
            out.append((f["name"], a, ft))
            sz = self.size(ft)
            if sz is None:
                sz = self.text(a, ft, MAX_DEPTH)[1]
                if sz is None:
                    break
            a += sz
        return out

    def _enum(self, addr, td, fixed, depth):
        tag = self.uint(addr, 1)
        v = self._variant_by_tag(td, tag)
        size = self.size(("named", td["name"])) if fixed else None
        if v is None:
            return ("%s #%d" % (td["name"], tag), size)
        name = "%s.%s" % (td["name"], v["name"])
        if not v.get("fields"):
            return (name, size if fixed else 1)
        body, n = self._fields(addr + 1, v["fields"], depth)
        return ("%s %s" % (name, body), size if fixed else (1 + n if n is not None else None))

    # -- children: [(name, address, type)]

    def children(self, addr, t):
        k = t[0]
        if k in ("ref", "opt"):
            p = self.uint(addr, 8)
            return self.children(p, t[1]) if p else []
        if k == "rel":
            target = self.rel_target(addr, t)
            return self.children(target, t[1]) if target is not None else []
        if k == "array":
            els, _, _ = self.elements(addr, t, MAX_CHILDREN)
            return [("[%d]" % i, a, et) for i, (a, et) in enumerate(els)]
        if k == "named":
            td = self.tdef(t[1])
            if td is None:
                return []
            if td.get("kind") == "enum":
                return self._enum_children(addr, td)
            return self.field_addrs(addr, td.get("fields", []))
        if k == "varmode":
            td = self.tdef(t[1])
            return self._enum_children(addr, td) if td else []
        if k == "variant":
            v = self._variant(self.tdef(t[1]), t[2])
            return self.field_addrs(addr, v.get("fields", [])) if v else []
        return []

    def _enum_children(self, addr, td):
        v = self._variant_by_tag(td, self.uint(addr, 1))
        if v is None:
            return []
        return self.field_addrs(addr + 1, v.get("fields", []))


# C types for Goose types, for children: a child with a C type is an
# ordinary value of that type; one without (a bool, a varint, a reference to
# a scalar, a variable-size value) is a uint8_t * whose printer decodes it.
_C_SCALARS = {"i8": "int8_t", "u8": "uint8_t", "i16": "int16_t", "u16": "uint16_t",
              "i32": "int32_t", "u32": "uint32_t", "i64": "int64_t", "u64": "uint64_t",
              "f32": "float", "f64": "double"}


def c_type_for(gmap, t):
    k = t[0]
    name = None
    if k == "scalar":
        name = _C_SCALARS.get(t[1])
    elif k == "named":
        td = gmap.types.get(t[1]) if gmap else None
        name = td.get("c") if td and not td.get("variable") else None
    elif k == "array" and t[2] in ("grow", "shrink"):
        name = "gs_rhdr"
    elif k == "array" and t[2] in ("slice", "fixed", "limited"):
        e = t[1]
        en = e[1] if e[0] == "scalar" else None
        if e[0] == "named" and gmap and gmap.types.get(e[1]):
            en = gmap.types[e[1]].get("c") or None
        if en:
            name = ("sl_%s" % en if t[2] == "slice" else
                    "a%d_%s" % (t[3], en) if t[2] == "fixed" else "l%d_%s" % (t[3], en))
    elif k in ("ref", "opt") and t[1][0] == "named":
        target_ct = c_type_for(gmap, t[1])
        return target_ct.pointer() if target_ct is not None else None
    if not name:
        return None
    return lookup_type(name)


def _u8p():
    t = lookup_type("uint8_t")
    return t.pointer() if t is not None else None


# --------------------------------------------------------------------------
# Which Goose type a gdb.Value holds


def _register(key, t):
    if len(_registry) > 20000:
        _registry.clear()
    _registry[key] = t


def _frame_key(frame):
    try:
        sp = int(frame.read_register("sp"))
    except Exception:
        sp = 0
    return (_gen[0], frame.name(), frame.pc(), sp)


def frame_index(frame, gmap):
    """{(address, C type name): Goose type} for the frame's locals that the
    map names, and their fields, walked through the map without following
    pointers. Built once per stop and frame."""
    key = _frame_key(frame)
    idx = _index_cache.get(key)
    if idx is not None:
        return idx
    idx = {}
    _index_cache[key] = idx
    if gmap.globals:
        try:
            block = globals_block(gmap)
            for g in gmap.globals:
                try:
                    _walk(idx, block[g["c"]], parse_type(g["type"]), gmap, 0)
                except Exception:
                    pass
        except Exception:
            pass
    f = gmap.funcs.get(frame.name()) if frame.name() else None
    if f is None:
        return idx
    for l in f.get("locals", []):
        try:
            v = frame.read_var(l["c"])
        except Exception:
            continue
        try:
            _walk(idx, v, parse_type(l["type"]), gmap, 0)
        except Exception:
            pass
    return idx


def globals_block(gmap):
    """The globals of the selected thread: its own block through the map's
    thread pointer (gs_gl) when the program starts threads and it is set,
    else the main program's (gs_globals_main). Raises when neither is
    there."""
    name = gmap.globals_main if gmap else "gs_globals_main"
    th = gmap.globals_thread if gmap else None
    if th:
        try:
            p = gdb.parse_and_eval(th)
            if _ptr(p):
                t = lookup_type("gs_globals_t")
                return (p.cast(t.pointer()) if t is not None else p).dereference()
        except Exception:
            pass
    return gdb.parse_and_eval(name)


def _walk(idx, v, t, gmap, depth):
    a = _addr(v)
    if a is None:
        return
    idx.setdefault((a, type_name(v.type)), t)
    if depth >= MAX_INDEX_DEPTH or _code(v.type) != gdb.TYPE_CODE_STRUCT:
        return
    if t[0] == "named":
        td = gmap.types.get(t[1])
        if td is None or td.get("variable"):
            return
        if td.get("kind") == "enum":
            if not td.get("fixed"):
                return
            var = Decoder._variant_by_tag(td, int(v["tag"]))
            if var is None or not var.get("fields"):
                return
            try:
                p = v["u"]["v_%s_g" % var["name"]]
            except Exception:
                return
            _walk(idx, p, ("variant", t[1], var["name"]), gmap, depth + 1)
            return
        fields = td.get("fields", [])
    elif t[0] == "variant":
        var = Decoder._variant(gmap.types.get(t[1]), t[2])
        fields = var.get("fields", []) if var else []
    else:
        return
    for fd in fields:
        try:
            c = v[fd["c"]]
        except Exception:
            continue
        _walk(idx, c, parse_type(fd["type"]), gmap, depth + 1)


def goose_type_of(v, gmap):
    """A value's Goose type, parsed, or None: a child or argument this module
    made, a local of the selected frame (or a field of one), or a value of a
    C type the map names."""
    try:
        tn = type_name(v.type)
        a = _addr(v)
        t = None
        if tn == "uint8_t *":
            t = _registry.get(("p", _ptr(v)))
        if t is None and a is not None:
            t = _registry.get((a, tn))
        if t is None and a is not None:
            try:
                frame = gdb.selected_frame()
            except Exception:
                frame = None
            if frame is not None:
                t = frame_index(frame, gmap).get((a, tn))
        if t is None:
            td = gmap.types_by_c.get(tn)
            if td is not None:
                t = ("named", td["name"])
        return t
    except Exception:
        return None


def value_location(v, t):
    """Where a value of Goose type t that gdb holds as v starts in memory, or
    None. A variable-size value is a uint8_t * to its bytes; a reference or
    optional, and gs_rref, hold the pointer in place."""
    if type_name(v.type) == "uint8_t *":
        return _ptr(v)
    if _is_ptr(v.type) and t[0] not in ("ref", "opt"):
        return _ptr(v)
    return _addr(v)


def map_text(v, gmap=None):
    """The value decoded through the debug map, or None to fall back."""
    gmap = gmap or get_map()
    if gmap is None:
        return None
    t = goose_type_of(v, gmap)
    if t is None:
        return None
    addr = value_location(v, t)
    if addr is None:
        return None
    try:
        return Decoder(gmap).text(addr, t)[0]
    except Exception as e:
        return "<%s>" % e


def map_children(v):
    """[(name, gdb.Value)] decoded through the debug map, or None."""
    gmap = get_map()
    if gmap is None:
        return None
    t = goose_type_of(v, gmap)
    if t is None:
        return None
    addr = value_location(v, t)
    if addr is None:
        return None
    try:
        kids = Decoder(gmap).children(addr, t)
    except Exception:
        return []
    out = []
    u8p = _u8p()
    for name, a, gt in kids[:MAX_CHILDREN]:
        ct = c_type_for(gmap, gt)
        if ct is not None:
            c = gdb.Value(a).cast(ct.pointer()).dereference()
            _register((a, type_name(ct)), gt)
        elif u8p is not None:
            c = gdb.Value(a).cast(u8p)
            _register(("p", a), gt)
        else:
            continue
        out.append((name, c))
    return out


# --------------------------------------------------------------------------
# Summaries from DWARF and the generated C


def summary_text(v, depth=0):
    """A Goose value's text without the address a pointer printer adds: from
    the map, else from the C types. None where it is not a Goose value."""
    s = map_text(v)
    if s is not None:
        return s
    return dwarf_text(v, depth)


def dwarf_text(v, depth=0):
    k = classify(v.type)
    if k == "byte":
        return str(int(v))
    if k == "bytes":
        # In a Goose program a uint8_t * is a variable-size value or a
        # data-stack top, never a NUL-terminated string.
        return "<untyped bytes>"
    if k is None:
        if _is_ptr(v.type):
            # A pointer field Node_g *next_g (a Node& or a Node?, DWARF does
            # not say which), or a gs_rhdr *.
            if _ptr(v) == 0:
                return "null"
            if depth >= MAX_DEPTH:
                return "0x%x" % _ptr(v)
            d = v.dereference()
            if classify(d.type) in (None, "byte", "bytes"):
                return None
            return dwarf_text(d, depth + 1)
        return None

    if k == "slice":
        data = v["data"]
        n = int(v["len"])
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        et = data.type.strip_typedefs().target()
        if _is_u8(et):
            return read_text(_ptr(data), n)
        if _ptr(data) == 0 and n != 0:
            return "<null data, len=%d>" % n
        return _list_text([data[i] for i in range(min(n, MAX_INLINE))], n, depth)

    if k in ("fixed", "limited"):
        e = v["e"]
        lo, hi = e.type.strip_typedefs().range()
        cap = hi - lo + 1
        n = cap if k == "fixed" else int(v["len"])
        if n > cap or n < 0:
            return "<invalid len=%d, cap=%d>" % (n, cap)
        if _is_u8(e.type.strip_typedefs().target()):
            a = _addr(e)
            if a is not None:
                return read_text(a, n)
            return quote_bytes(bytearray(int(e[i]) & 0xFF for i in range(min(n, MAX_TEXT))), n)
        return _list_text([e[i] for i in range(min(n, MAX_INLINE))], n, depth)

    if k == "rhdr":
        n = int(v["len"])
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        return "len=%d, element type not in debug info" % n

    if k == "rref":
        h = v["hdr"]
        if _ptr(h) == 0:
            return "null"
        n = int(h.dereference()["len"])
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        return "len=%d, element type not in debug info" % n

    if k in ("ref", "opt"):
        if _ptr(v) == 0:
            return "null"
        if depth >= MAX_DEPTH:
            return "0x%x" % _ptr(v)
        try:
            d = v.dereference()
            d.fetch_lazy()
        except Exception:
            return "<unreadable 0x%x>" % _ptr(v)
        return value_text(d, depth + 1)

    if k == "cenum":
        cn = type_name(v.type)
        tag = int(v["tag"])
        name = tag_name(cn, tag)
        if name is None:
            return "%s #%d" % (goose_type_name(cn), tag)
        return "%s.%s" % (goose_type_name(cn), name)

    if k == "adt":
        cn = type_name(v.type)
        tag = int(v["tag"])
        name = tag_name(cn, tag)
        tn = goose_type_name(cn)
        if name is None:
            return "%s #%d (tag names not in debug info)" % (tn, tag)
        try:
            p = v["u"]["v_%s_g" % name]
        except Exception:
            return "%s.%s" % (tn, name)
        return "%s.%s %s" % (tn, name, _fields_text(p, depth))

    if k == "payload":
        cn = type_name(v.type)
        ename, variant = payload_owner(cn)
        prefix = "%s.%s" % (goose_type_name(ename), variant) if ename else cn
        return "%s %s" % (prefix, _fields_text(v, depth))

    if k == "struct":
        return "%s %s" % (goose_type_name(type_name(v.type)), _fields_text(v, depth))

    return None


def dwarf_children(v):
    """[(name, gdb.Value)]: the elements of an array or slice, the target's
    children for a reference, the tag and active payload of an enum, the
    fields of a struct."""
    k = classify(v.type)
    if k == "slice":
        data = v["data"]
        n = int(v["len"])
        if n <= 0 or n > MAX_LEN or _ptr(data) == 0:
            return []
        return [("[%d]" % i, data[i]) for i in range(min(n, MAX_CHILDREN))]
    if k in ("fixed", "limited"):
        e = v["e"]
        lo, hi = e.type.strip_typedefs().range()
        n = hi - lo + 1 if k == "fixed" else min(int(v["len"]), hi - lo + 1)
        return [("[%d]" % i, e[i]) for i in range(min(n, MAX_CHILDREN))]
    if k in ("ref", "opt"):
        if _ptr(v) == 0:
            return []
        d = v.dereference()
        if _code(d.type) != gdb.TYPE_CODE_STRUCT:
            return [("*", d)]
        return [(f.name, d[f.name]) for f in d.type.strip_typedefs().fields()[:MAX_CHILDREN]]
    if k == "adt":
        tag = v["tag"]
        out = [("tag", tag)]
        name = tag_name(type_name(v.type), int(tag))
        if name is not None:
            try:
                out.append(("v_%s_g" % name, v["u"]["v_%s_g" % name]))
            except Exception:
                pass
        else:
            out.append(("u", v["u"]))
        return out
    if k in ("struct", "payload", "cenum", "rhdr", "rref"):
        return [(f.name, v[f.name]) for f in v.type.strip_typedefs().fields()[:MAX_CHILDREN]]
    return []


# --------------------------------------------------------------------------
# The printers


def _children_on():
    try:
        return bool(gdb.parameter("goose-children"))
    except Exception:
        return False


class GoosePrinter(object):
    """A Goose value's summary, one line. A pointer (a T& or T? local, a
    variable-size value's uint8_t *, a pointer field) is shown as gdb shows
    a char *: its address, then the value."""

    def __init__(self, val):
        self.val = val

    def to_string(self):
        try:
            v = self.val
            s = summary_text(v)
            if s is None:
                s = value_text(v)
            if _is_ptr(v.type):
                a = _ptr(v)
                if a == 0:
                    return "null"
                if type_name(v.type) == "uint8_t *" and ("p", a) in _registry:
                    # A child made for a value with no C type of its own:
                    # the pointer only carries the value's address.
                    return _clip(s)
                return _clip("0x%x %s" % (a, s))
            return _clip(s)
        except Exception as e:
            return "<goose_gdb: %s>" % type(e).__name__


class GooseChildrenPrinter(GoosePrinter):
    """The same summary, and children for a front end to expand."""

    def children(self):
        try:
            kids = map_children(self.val)
            if kids is None:
                kids = dwarf_children(self.val)
        except Exception:
            kids = []
        return iter(kids)


class GooseScalarPrinter(object):
    """A 1-byte integer in decimal, or a map-typed bool as true/false."""

    def __init__(self, val, text):
        self.val = val
        self.text = text

    def to_string(self):
        return self.text


def lookup(val):
    try:
        k = classify(val.type)
        if k == "byte" or k is None:
            # A scalar the map types (a bool local, a varint field reached
            # by its path), else a 1-byte integer in decimal.
            gmap = get_map()
            if gmap is not None and val.address is not None:
                t = goose_type_of(val, gmap)
                if t is not None and t[0] != "named":
                    s = map_text(val, gmap)
                    if s is not None:
                        return GooseScalarPrinter(val, s)
            if k == "byte":
                return GooseScalarPrinter(val, str(int(val)))
            # A pointer to a Goose value (a struct field Node_g *next_g).
            if not _is_ptr(val.type):
                return None
            if classify(val.type.strip_typedefs().target()) in (None, "byte", "bytes"):
                return None
        return GooseChildrenPrinter(val) if _children_on() else GoosePrinter(val)
    except Exception:
        return None


lookup.__name__ = NAME
lookup.name = NAME
lookup.enabled = True


# --------------------------------------------------------------------------
# Commands


def _out(s):
    gdb.write(s + "\n")


def frame_name(frame):
    """A frame's function under its Goose name, with the type arguments of a
    generic instance: from the debug map, else the C name."""
    try:
        cname = frame.name() or ""
        gmap = get_map()
        f = gmap.funcs.get(cname) if gmap else None
        if f is None:
            return cname or "??"
        b = dict(f.get("bindings") or {})
        b.update(f.get("fnvals") or {})
        if b:
            return "%s<%s>" % (f["name"], ", ".join("%s=%s" % kv for kv in b.items()))
        return f["name"]
    except Exception:
        return frame.name() or "??"


class GooseLocals(gdb.Command):
    """goose-locals: the selected frame's Goose locals and arguments under
    their Goose names, without compiler temporaries (tN) and hidden arguments
    (gs_sp). With the debug map: `name: type = value`, only the locals in
    scope at the frame's line, decoded by their Goose type."""

    def __init__(self):
        super(GooseLocals, self).__init__("goose-locals", gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        try:
            frame = gdb.selected_frame()
        except Exception:
            raise gdb.GdbError("no frame selected")
        try:
            gmap = get_map()
            f = gmap.funcs.get(frame.name()) if gmap and frame.name() else None
            if f is not None:
                return self._from_map(frame, gmap, f)
            for cn, v in _frame_vars(frame):
                k = classify(v.type)
                if k == "bytes" or (k is None and _is_ptr(v.type)):
                    s = "0x%x" % _ptr(v)
                elif k is not None:
                    s = summary_text(v) or value_text(v)
                else:
                    s = value_text(v)
                _out("%s = %s" % (goose_name(cn), _clip(s)))
        except gdb.GdbError:
            raise
        except Exception as e:
            raise gdb.GdbError("goose-locals: %s" % e)

    @staticmethod
    def _from_map(frame, gmap, f):
        line = frame.find_sal().line
        dec = Decoder(gmap)
        ls = [l for l in f.get("locals", []) if l.get("line", 0) <= line <= l.get("end", 0)]
        ls.sort(key=lambda l: (not l.get("param"), l.get("line", 0)))
        for l in ls:
            try:
                v = frame.read_var(l["c"])
            except Exception:
                _out("%s: %s = <not in the debug info>" % (l["name"], l["type"]))
                continue
            t = parse_type(l["type"])
            try:
                addr = value_location(v, t)
                txt = dec.text(addr, t)[0] if addr is not None else value_text(v)
            except Exception as e:
                txt = "<%s>" % e
            _out("%s: %s = %s" % (l["name"], l["type"], _clip(txt)))


class GooseGlobals(gdb.Command):
    """goose-globals: the program's Goose globals, `name: type = value`, from
    the debug map: the main program's instance, or under threads the
    selected thread's own (gs_gl)."""

    def __init__(self):
        super(GooseGlobals, self).__init__("goose-globals", gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        gmap = get_map()
        if gmap is None or not gmap.globals:
            _out("no globals in the debug map" if gmap else "no debug map")
            return
        try:
            block = globals_block(gmap)
        except Exception:
            raise gdb.GdbError("no %s in the debug info" % gmap.globals_main)
        dec = Decoder(gmap)
        for g in gmap.globals:
            try:
                v = block[g["c"]]
            except Exception:
                _out("%s: %s = <not in the debug info>" % (g["name"], g["type"]))
                continue
            t = parse_type(g["type"])
            try:
                addr = value_location(v, t)
                txt = dec.text(addr, t)[0] if addr is not None else value_text(v)
            except Exception as e:
                txt = "<%s>" % e
            _out("%s: %s = %s" % (g["name"], g["type"], _clip(txt)))


def _frame_vars(frame):
    """[(C name, value)]: the arguments, then the locals from the outermost
    block in, of Goose-named variables, the innermost of a shadowed name."""
    blocks = []
    b = frame.block()
    while b is not None:
        blocks.append(b)
        if b.function is not None:
            break
        b = b.superblock
    args, locs, seen = [], [], set()
    for b in blocks:
        for sym in b:
            if not (sym.is_argument or sym.is_variable) or sym.name in seen:
                continue
            seen.add(sym.name)
            if not _NAME_RE.match(sym.name):
                continue
            try:
                v = sym.value(frame)
            except Exception:
                continue
            (args if sym.is_argument else locs).append((sym.name, v))
    return args + locs


def _module_of(frame):
    try:
        fn = frame.function()
        if fn is not None and fn.symtab is not None:
            return os.path.basename(fn.symtab.objfile.filename)
        so = gdb.solib_name(frame.pc())
        if so:
            return os.path.basename(so)
        return os.path.basename(gdb.current_progspace().filename or "?")
    except Exception:
        return "?"


class GooseBt(gdb.Command):
    """goose-bt [N]: the selected thread's frames under their Goose names
    (from the debug map), with Goose files and lines; at most N frames."""

    def __init__(self):
        super(GooseBt, self).__init__("goose-bt", gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        try:
            n = int(arg.strip()) if arg.strip() else 1 << 30
            try:
                sel = gdb.selected_frame()
            except Exception:
                raise gdb.GdbError("no frame selected")
            fr = gdb.newest_frame()
            i = 0
            while fr is not None and i < n:
                sal = fr.find_sal()
                where = ""
                if sal.symtab is not None and sal.symtab.filename:
                    where = " at %s:%d" % (os.path.basename(sal.symtab.filename), sal.line)
                mark = "*" if fr == sel else " "
                _out("%s #%d: %s`%s%s" % (mark, i, _module_of(fr), frame_name(fr), where))
                fr = fr.older()
                i += 1
        except gdb.GdbError:
            raise
        except Exception as e:
            raise gdb.GdbError("goose-bt: %s" % e)


_SCALARS = {
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "f32": "float", "f64": "double", "bool": "uint8_t",
}


class GooseAs(gdb.Command):
    """goose-as VAR TYPE: shows VAR's elements as TYPE, which the debug info
    cannot say. VAR is a resizable (gs_rhdr, gs_rref: TYPE is the element
    type) or a uint8_t * holding a T[] (TYPE written as T[]: a u32 count,
    then the elements). TYPE is a Goose scalar (i64, u8, f64, ...) or a C
    type name (Point_g, Shape_g, a4_i32)."""

    def __init__(self):
        super(GooseAs, self).__init__("goose-as", gdb.COMMAND_DATA, gdb.COMPLETE_SYMBOL)

    def invoke(self, arg, from_tty):
        args = arg.split()
        if len(args) != 2:
            raise gdb.GdbError("usage: goose-as VAR TYPE  (TYPE: i64, Point_g, ... or T[] for a T[])")
        path, tname = args
        try:
            frozen = tname.endswith("[]")
            if frozen:
                tname = tname[:-2]
            try:
                v = gdb.parse_and_eval(path)
            except gdb.error:
                raise gdb.GdbError("no variable %s" % path)
            et = lookup_type(_SCALARS.get(tname, tname))
            if et is None or et.sizeof == 0:
                raise gdb.GdbError("unknown type %s" % tname)
            k = classify(v.type)
            if frozen:
                addr = _ptr(v)
                try:
                    n = int.from_bytes(read_mem(addr, 4), "little")
                except Exception:
                    raise gdb.GdbError("cannot read the count at 0x%x" % addr)
                addr += 4
            elif k in ("rhdr", "rref"):
                h = v if k == "rhdr" else v["hdr"].dereference()
                addr = _ptr(h["base"])
                n = int(h["len"])
            else:
                raise gdb.GdbError("%s is not a resizable; for a T[] write the type as %s[]" % (path, tname))
            if n < 0 or n > MAX_LEN:
                raise gdb.GdbError("invalid length %d" % n)
            if _is_u8(et):
                _out("len=%d %s" % (n, read_text(addr, n)))
                return
            _out("len=%d" % n)
            ep = et.pointer()
            for i in range(min(n, MAX_CHILDREN)):
                c = gdb.Value(addr + i * et.sizeof).cast(ep).dereference()
                _out("  [%d] = %s" % (i, value_text(c)))
            if n > MAX_CHILDREN:
                _out("  ... (%d more)" % (n - MAX_CHILDREN))
        except gdb.GdbError:
            raise
        except Exception as e:
            raise gdb.GdbError("goose-as: %s" % e)


class GooseChildren(gdb.Parameter):
    """Whether the Goose printers list children (for MI front ends)."""

    set_doc = "Set whether Goose values list their children."
    show_doc = "Show whether Goose values list their children."

    def __init__(self):
        super(GooseChildren, self).__init__("goose-children", gdb.COMMAND_DATA, gdb.PARAM_BOOLEAN)
        self.value = False

    def get_set_string(self):
        return ""

    def get_show_string(self, svalue):
        return "Goose values list their children: %s" % svalue


# --------------------------------------------------------------------------
# The frame filter: Goose names and parameters in `bt`


class _Arg(object):
    def __init__(self, name, val):
        self._name = name
        self._val = val

    def symbol(self):
        return self._name

    def value(self):
        return self._val


def _goose_args(frame, f):
    """The Goose parameters of a frame, in the C argument order: a split
    slice parameter (xs_g_data, xs_g_len) and a resizable passed by
    reference (out_g_hdr, out_g_stk) are their Goose parameter once; the
    hidden gs_sp is left out."""
    params = [l for l in f.get("locals", []) if l.get("param")]
    by_c = dict((l["c"], l) for l in params)
    order = []
    try:
        b = frame.block()
        while b is not None and b.function is None:
            b = b.superblock
        for sym in (b or []):
            if not sym.is_argument:
                continue
            l = by_c.get(sym.name)
            if l is None:
                l = next((p for p in params if sym.name.startswith(p["c"] + "_")), None)
            if l is not None and l not in order:
                order.append(l)
    except Exception:
        pass
    order += [l for l in params if l not in order]
    gmap = get_map()
    out = []
    for l in order:
        try:
            v = frame.read_var(l["c"])
        except Exception:
            continue
        a = _addr(v)
        t = parse_type(l["type"])
        if a is not None:
            _register((a, type_name(v.type)), t)
        if type_name(v.type) == "uint8_t *":
            _register(("p", _ptr(v)), t)
        out.append(_Arg(l["name"], v))
    return out


if FrameDecorator is not None:
    class GooseFrameDecorator(FrameDecorator):
        def function(self):
            try:
                fr = self.inferior_frame()
                gmap = get_map()
                if gmap is not None and fr.name() in gmap.funcs:
                    return frame_name(fr)
            except Exception:
                pass
            return super(GooseFrameDecorator, self).function()

        def frame_args(self):
            try:
                fr = self.inferior_frame()
                gmap = get_map()
                f = gmap.funcs.get(fr.name()) if gmap is not None else None
                if f is not None:
                    return _goose_args(fr, f)
            except Exception:
                pass
            return super(GooseFrameDecorator, self).frame_args()
else:
    GooseFrameDecorator = None


class GooseFrameFilter(object):
    def __init__(self):
        self.name = NAME
        self.priority = 100
        self.enabled = True

    def filter(self, frame_iter):
        return map(GooseFrameDecorator, frame_iter)


# --------------------------------------------------------------------------
# Registration (safe to source more than once)


def register():
    gdb.pretty_printers[:] = [p for p in gdb.pretty_printers
                              if getattr(p, "name", getattr(p, "__name__", None)) != NAME]
    gdb.pretty_printers.append(lookup)
    if GooseFrameDecorator is not None and hasattr(gdb, "frame_filters"):
        gdb.frame_filters[NAME] = GooseFrameFilter()
    old = getattr(gdb, "_goose_gdb_handlers", None)
    if old:
        for ev, fn in old:
            try:
                ev.disconnect(fn)
            except Exception:
                pass
    handlers = [(gdb.events.stop, _on_stop), (gdb.events.new_objfile, _on_objfiles)]
    if hasattr(gdb.events, "clear_objfiles"):
        handlers.append((gdb.events.clear_objfiles, _on_objfiles))
    if hasattr(gdb.events, "memory_changed"):
        handlers.append((gdb.events.memory_changed, _on_stop))
    for ev, fn in handlers:
        ev.connect(fn)
    gdb._goose_gdb_handlers = handlers
    GooseLocals()
    GooseBt()
    GooseAs()
    GooseGlobals()
    if not getattr(gdb, "_goose_gdb_param", None):
        try:
            gdb._goose_gdb_param = GooseChildren()
        except Exception:
            pass


register()
