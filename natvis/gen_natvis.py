#!/usr/bin/env python3
"""Natvis for Goose programs: the Visual Studio, VS Code (cppvsdbg) and
WinDbg/cdb counterpart of ../lldb/goose_lldb.py.

Natvis is static XML matched on C type names, with no wildcard but in a
C++ template's arguments, so it cannot match every sl_T or a4_T by prefix,
and cannot read the debug map while the debugger runs. This script writes
the entries instead:

  gen_natvis.py --generic > goose.natvis
      what any program has under the same names: gs_rhdr, gs_rref, and the
      slices of the scalars (sl_u8, sl_i64, ...).

  gen_natvis.py prog.debug.json > prog.natvis
      one program's entries, from the map that `goose -g` writes beside the
      C (<stem>.debug.json) and from that C (<stem>.c beside the map, or
      --c FILE): its structs with their Goose field names, its enums with
      their variant names, the variants' payload types, and its fixed and
      limited arrays and slices of non-scalar elements. The names that
      goose.natvis has are left out, so load both.

  gen_natvis.py --all prog.debug.json > prog.natvis
      both in one file, for the linker's /NATVIS: option, which embeds it in
      the PDB.

What natvis cannot show (the README has the list): the elements of a
resizable local (every T[>..] is the same gs_rhdr, so the element type is
not in the C type; a resizable field is decoded, since its struct says), a
variable-size value (a bare uint8_t *), a bool or a reference local (a
uint8_t, or a pointer whose r_/o_ typedef the PDB does not keep), a
variable-mode enum (no C type). goose.js, for WinDbg and cdb, shows those
from the map.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = "http://schemas.microsoft.com/vstudio/debugger/natvis/2010"

# Elements shown inline in a summary, and the length past which a header is
# garbage (an uninitialized local), as in goose_lldb.py.
MAX_INLINE = 8
MAX_LEN = 1 << 40
# Summaries per struct: one per combination of its nullable fields' states
# and its resizable fields' short lengths. Past this the struct's summary
# shows those fields compactly (an address, len=N).
MAX_COMBOS = 48

# Goose scalars: the C type the evaluator knows them by, and the format.
SCALAR_C = {
    "i8": "signed char", "u8": "unsigned char", "bool": "unsigned char",
    "i16": "short", "u16": "unsigned short", "i32": "int", "u32": "unsigned int",
    "i64": "__int64", "u64": "unsigned __int64", "f32": "float", "f64": "double",
}
SLICE_SCALARS = ("u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64", "f32", "f64")
REL_SIGNED = {"u8": "signed char", "u16": "short", "u32": "int", "u64": "__int64"}
BYTE_C = ("uint8_t", "int8_t", "gs_u8", "gs_i8", "unsigned char", "signed char", "char")


# --------------------------------------------------------------------------
# Goose type text, as the map writes it (the same grammar as goose_lldb.py):
#   ("scalar", "i64"), ("named", "Pair<i64>"), ("varmode", "Shape"),
#   ("variant", "Shape", "Circle"), ("array", elem, form, n),
#   ("ref", t), ("opt", t), ("rel", t, width, pool, optional)

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


def parse_type(s):
    s = s.strip()
    if s.startswith("const "):
        return parse_type(s[6:])
    if s.endswith("?"):
        inner = parse_type(s[:-1])
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
            return ("rel", parse_type(s[:i - 1]), args[0].strip(), pool, False)
    if s.endswith("&"):
        return ("ref", parse_type(s[:-1]))
    if s.endswith("]"):
        i = _open_of(s, len(s) - 1, "[", "]")
        inner = s[i + 1:-1].strip()
        elem = parse_type(s[:i])
        forms = {"": ("var", "u32"), ":": ("slice", None), ">..": ("grow", None),
                 ">..<": ("shrink", None), "..": ("cap", None)}
        if inner in forms:
            return ("array", elem) + forms[inner]
        if inner.startswith(".."):
            return ("array", elem, "limited", int(inner[2:]))
        if inner in ("u8", "u16", "u32", "u64", "varint"):
            return ("array", elem, "var", inner)
        return ("array", elem, "fixed", int(inner))
    if s.endswith(".."):
        return ("varmode", s[:-2])
    if s in SCALAR_C or s == "varint":
        return ("scalar", s)
    i = _top_level_dot(s)
    if i > 0:
        return ("variant", s[:i], s[i + 1:])
    return ("named", s)


# --------------------------------------------------------------------------
# The program: the map's types, and the array types its C declares.

_TYPEDEF_RE = re.compile(r"typedef struct \{([^{}]*)\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;")
_STRUCT_RE = re.compile(r"^struct ([A-Za-z_][A-Za-z0-9_]*) \{", re.M)


class Program:
    def __init__(self, docs, ctext):
        self.types = {}      # Goose name -> map type
        self.by_c = {}       # C name -> map type
        self.locals = []     # every local's parsed type
        self.globals = []    # the map's globals: {c, name, type, ...}
        self.thread = None   # "gs_gl" when threads have their own block
        for d in docs:
            g = d.get("globals") or {}
            for v in g.get("vars", []):
                self.globals.append(v)
                self.locals.append(parse_type(v["type"]))
            if g.get("thread"):
                self.thread = g["thread"]
            for t in d.get("types", []):
                self.types.setdefault(t["name"], t)
                if t.get("c"):
                    self.by_c.setdefault(t["c"], t)
            for f in d.get("functions", []):
                for l in f.get("locals", []):
                    self.locals.append(parse_type(l["type"]))
        # C array types: name -> (kind, element C type, n, element Goose
        # spelling in the name). From the C where there is one.
        self.arrays = {}
        self.cstructs = set()
        if ctext:
            for m in _TYPEDEF_RE.finditer(ctext):
                self._array_typedef(m.group(2), " ".join(m.group(1).split()))
            self.cstructs = set(_STRUCT_RE.findall(ctext))
        self.ctext_names = set(m.group(2) for m in _TYPEDEF_RE.finditer(ctext or ""))
        self.ctext_names |= set(re.findall(r"\}\s*([A-Za-z_]\w*)\s*;", ctext or ""))
        self.have_c = bool(ctext)
        # Without the C, the array types the map's types imply.
        if not ctext:
            for t in self.locals:
                self._implied(t)
            for td in self.types.values():
                for f in self._all_fields(td):
                    self._implied(parse_type(f["type"]))

    def _array_typedef(self, name, body):
        m = re.fullmatch(r"sl_(\w+)", name)
        if m:
            b = re.fullmatch(r"(.+?)\s*\*\s*data;\s*int64_t len;", body)
            if b:
                self.arrays[name] = ("slice", b.group(1), None, m.group(1))
            return
        m = re.fullmatch(r"a(\d+)_(\w+)", name)
        if m:
            b = re.fullmatch(r"(.+?)\s+e\[(\d+)\];", body)
            if b:
                self.arrays[name] = ("fixed", b.group(1), int(b.group(2)), m.group(2))
            return
        m = re.fullmatch(r"l(\d+)_(\w+)", name)
        if m:
            b = re.fullmatch(r"\w+\s+len;\s*(.+?)\s+e\[(\d+)\];", body)
            if b:
                self.arrays[name] = ("limited", b.group(1), int(b.group(2)), m.group(2))

    @staticmethod
    def _all_fields(td):
        out = list(td.get("fields", []))
        for v in td.get("variants", []):
            out += v.get("fields", [])
        return out

    def _implied(self, t):
        k = t[0]
        if k in ("ref", "opt", "rel"):
            return self._implied(t[1])
        if k != "array":
            return
        self._implied(t[1])
        en = self.mangle(t[1])
        if en is None:
            return
        ec = self.c_type(t[1])
        form = t[2]
        if form == "slice":
            self.arrays.setdefault("sl_" + en, ("slice", ec, None, en))
        elif form == "fixed":
            self.arrays.setdefault("a%d_%s" % (t[3], en), ("fixed", ec, t[3], en))
        elif form == "limited":
            self.arrays.setdefault("l%d_%s" % (t[3], en), ("limited", ec, t[3], en))

    def mangle(self, t):
        """The spelling of a Goose type inside a C array type's name."""
        if t[0] == "scalar" and t[1] != "varint":
            return t[1]
        if t[0] == "named":
            td = self.types.get(t[1])
            return td["c"] if td and td.get("c") else None
        if t[0] == "array" and t[2] in ("slice", "fixed", "limited"):
            inner = self.mangle(t[1])
            if inner is None:
                return None
            return {"slice": "sl_%s" % inner, "fixed": "a%s_%s" % (t[3], inner),
                    "limited": "l%s_%s" % (t[3], inner)}[t[2]]
        return None

    def c_type(self, t):
        """The C type a value of Goose type t has in place, or None where it
        has none (variable size, varint)."""
        k = t[0]
        if k == "scalar":
            return SCALAR_C.get(t[1])
        if k == "named":
            td = self.types.get(t[1])
            return td["c"] if td and td.get("c") else None
        if k == "variant":
            td = self.types.get(t[1])
            if td and td.get("c"):
                return "%s_%s_p" % (td["c"], t[2])
            return None
        if k == "array":
            if t[2] in ("grow", "shrink"):
                return "gs_rhdr"
            n = self.mangle(t)
            if n and (n in self.arrays or n in GENERIC_ARRAYS):
                return n
        return None

    def struct_exists(self, cname):
        return not self.have_c or cname in self.cstructs


# --------------------------------------------------------------------------
# XML

def esc_braces(s):
    return s.replace("{", "{{").replace("}", "}}")


class Out:
    def __init__(self):
        ET.register_namespace("", NS)
        self.root = ET.Element("{%s}AutoVisualizer" % NS)
        self.names = set()

    def type(self, name, comment=None):
        if name in self.names:
            return None
        self.names.add(name)
        if comment:
            self.root.append(ET.Comment(" %s " % comment))
        return ET.SubElement(self.root, "{%s}Type" % NS, {"Name": name})

    def text(self):
        ET.indent(self.root, space="  ")
        return ('<?xml version="1.0" encoding="utf-8"?>\n'
                "<!-- Written by goose-debugger natvis/gen_natvis.py. -->\n"
                + ET.tostring(self.root, encoding="unicode") + "\n")


def sub(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, "{%s}%s" % (NS, tag), {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        e.text = text
    return e


def display(t, text, cond=None, view=None):
    attrs = {}
    if cond:
        attrs["Condition"] = cond
    if view == "shallow":
        attrs["IncludeView"] = "shallow"
    elif view == "deep":
        attrs["ExcludeView"] = "shallow"
    sub(t, "DisplayString", text, **attrs)


def invalid_len(len_expr):
    return "%s < 0 || %s > %d" % (len_expr, len_expr, MAX_LEN)


def and_(*conds):
    cs = [c for c in conds if c]
    if not cs:
        return None
    if len(cs) == 1:
        return cs[0]
    return " && ".join("(%s)" % c for c in cs)


# --------------------------------------------------------------------------
# Arrays: [a, b, c] summaries and their elements as children.

def elem_fmt(ec):
    return ",d" if ec in BYTE_C else ""


def list_summaries(ptr, len_expr, ec, fixed_n=None):
    """[(condition, text)] showing the first MAX_INLINE elements of ptr[0..len)."""
    f = elem_fmt(ec)

    def items(k):
        return ", ".join("{%s[%d]%s}" % (ptr, i, f) for i in range(k))

    if fixed_n is not None:
        k = min(fixed_n, MAX_INLINE)
        more = ", ...] (%d elements)" % fixed_n if fixed_n > k else "]"
        return [(None, "[" + items(k) + more)]
    out = [(invalid_len(len_expr), "<invalid len={%s}>" % len_expr)]
    for k in range(MAX_INLINE + 1):
        out.append(("%s == %d" % (len_expr, k), "[" + items(k) + "]"))
    out.append((None, "[" + items(MAX_INLINE) + ", ...] ({%s} elements)" % len_expr))
    return out


def text_summaries(ptr, len_expr, fixed_n=None):
    if fixed_n is not None:
        return [(None, "{%s,[%d]s8}" % (ptr, fixed_n))]
    return [(invalid_len(len_expr), "<invalid len={%s}>" % len_expr),
            (None, "{%s,[%s]s8}" % (ptr, len_expr))]


def list_children(parent, ptr, len_expr, ec, guard=True):
    cond = "%s >= 0 && %s <= %d" % (len_expr, len_expr, MAX_LEN) if guard else None
    if ec in BYTE_C:
        il = sub(parent, "IndexListItems", Condition=cond)
        sub(il, "Size", len_expr)
        # dx ignores a format specifier here: a cast shows the byte in decimal.
        sub(il, "ValueNode", "(int)%s[$i]" % ptr)
    else:
        ai = sub(parent, "ArrayItems", Condition=cond)
        sub(ai, "Size", len_expr)
        sub(ai, "ValuePointer", ptr)


def emit_array(out, name, kind, ec, n, en):
    is_text = en == "u8"
    t = out.type(name)
    if t is None:
        return
    if kind == "slice":
        sums = text_summaries("data", "len") if is_text else list_summaries("data", "len", ec)
        ptr, ln = "data", "len"
    elif kind == "fixed":
        sums = text_summaries("e", None, n) if is_text else list_summaries("e", None, ec, n)
        ptr, ln = "e", str(n)
    else:
        sums = (text_summaries("e", "len") if is_text else list_summaries("e", "len", ec))
        ptr, ln = "e", "len"
    for cond, text in sums:
        display(t, text, cond)
    exp = sub(t, "Expand")
    if kind == "slice":
        sub(exp, "Item", "len", Name="[len]")
    list_children(exp, ptr, ln, ec, guard=kind != "fixed")


def emit_generic(out):
    t = out.type("gs_rhdr", "T[>..] and T[>..<]: the element type is not in the C type. "
                            "goose.js decodes a local from the debug map; the [as ...] "
                            "views are guesses.")
    display(t, "<invalid len={len}>", invalid_len("len"))
    display(t, "len={len}")
    exp = sub(t, "Expand")
    sub(exp, "Item", "len", Name="[len]")
    sub(exp, "Item", "base", Name="[base]")
    for label, ctype in (("[as u8 text]", None), ("[as i64]", "__int64"), ("[as i32]", "int"),
                         ("[as f64]", "double")):
        s = sub(exp, "Synthetic", Name=label, Condition="len > 0 && len <= %d" % MAX_LEN)
        if ctype is None:
            sub(s, "DisplayString", "{base,[len]s8}")
            continue
        for cond, text in list_summaries("((%s*)base)" % ctype, "len", ctype):
            display(s, text, cond)
        e2 = sub(s, "Expand")
        list_children(e2, "(%s*)base" % ctype, "len", ctype, guard=False)
    for name in ("gs_rref", "gs_pref"):
        t = out.type(name, "a resizable passed by reference: its header" if name == "gs_rref" else None)
        display(t, "null", "hdr == 0")
        display(t, "{*hdr}")
        exp = sub(t, "Expand")
        sub(exp, "ExpandedItem", "*hdr", Condition="hdr != 0")
    for s in SLICE_SCALARS:
        ec = SCALAR_C[s]
        emit_array(out, "sl_" + s, "slice", ec, None, s)


def generic_names():
    o = Out()
    emit_generic(o)
    return set(o.names)


GENERIC_ARRAYS = set("sl_" + s for s in SLICE_SCALARS)


# --------------------------------------------------------------------------
# Fields: each gives summary alternatives [(condition, text)] and children.

class Fields:
    """The summary and children of a run of fields at `prefix` (a C
    member path such as "" or "u.v_Circle_g.")."""

    def __init__(self, prog, warn):
        self.prog = prog
        self.warn = warn

    def field(self, prefix, f, view):
        """(alternatives, children). Alternatives are [(cond, text)];
        children are [(name, expr, cond, synthetic-or-None)]."""
        c = prefix + f["c"]
        name = f["name"]
        t = parse_type(f["type"])
        return self._value(name, c, t, view)

    def _value(self, name, c, t, view):
        k = t[0]
        prog = self.prog
        if k == "scalar":
            if t[1] == "bool":
                return [(None, "{(bool)%s}" % c)], [(name, "(bool)%s" % c, None, None)]
            if t[1] in ("u8", "i8"):
                return [(None, "{%s,d}" % c)], [(name, "%s,d" % c, None, None)]
            return [(None, "{%s}" % c)], [(name, c, None, None)]
        if k in ("ref", "opt"):
            target = t[1]
            deref = "*%s" % c
            tc = prog.c_type(target)
            fmt = ",d" if target[0] == "scalar" and target[1] in ("u8", "i8") else ""
            if target[0] == "scalar" and target[1] == "bool":
                deref, fmt = "(bool)*%s" % c, ""
            shown = self._shown(deref, target, tc, fmt, view)
            if k == "ref":
                return [(None, shown)], [(name, deref + fmt, None, None)]
            if view == "compact":
                return [(None, "{%s}" % c)], [(name, c, None, None)]
            return ([("%s == 0" % c, "null"), (None, shown)],
                    [(name, c, "%s == 0" % c, None), (name, deref + fmt, "%s != 0" % c, None)])
        if k == "rel":
            target, width, pool, optional = t[1], t[2], t[3], t[4]
            tc = prog.c_type(target)
            if tc is None or width not in REL_SIGNED:
                self.warn("%s: %s: target type has no C type; shown as its offset" % (name, f_text(t)))
                return [(None, "{%s}" % c)], [(name, c, None, None)]
            if pool is None:
                addr = "(unsigned char*)&%s + (%s)%s" % (c, REL_SIGNED[width], c)
            else:
                g = ".".join(p + "_g" for p in pool.split("."))
                addr = "gs_globals_main.%s.base - 1 + %s" % (g, c)
            deref = "*(%s*)(%s)" % (tc, addr)
            shown = self._shown(deref, target, tc, "", view)
            if optional and view == "compact":
                return [(None, "{%s}" % c)], [(name, c, None, None)]
            if optional:
                return ([("%s == 0" % c, "null"), (None, shown)],
                        [(name, c, "%s == 0" % c, None), (name, deref, "%s != 0" % c, None)])
            return [(None, shown)], [(name, deref, None, None)]
        if k == "array" and t[2] in ("grow", "shrink"):
            ec = prog.c_type(t[1])
            if ec is None:
                self.warn("%s: %s: elements without a C type (variable size); shown as gs_rhdr"
                          % (name, f_text(t)))
                return [(None, "{%s}" % c)], [(name, c, None, None)]
            ptr = "((%s*)%s.base)" % (ec, c)
            ln = "%s.len" % c
            if t[1] == ("scalar", "u8"):
                sums = text_summaries(ptr, ln)
            else:
                sums = list_summaries(ptr, ln, ec)
            syn = ("synthetic", sums, ptr, ln, ec)
            if view in ("shallow", "compact"):
                return [(None, "len={%s}" % ln)], [(name, None, None, syn)]
            return [(cnd, txt) for cnd, txt in sums], [(name, None, None, syn)]
        # A struct, an enum, a fixed or limited array, a slice: its own type
        # has an entry.
        if view == "shallow" and k in ("named", "variant"):
            return [(None, "{%s,view(shallow)}" % c)], [(name, c, None, None)]
        return [(None, "{%s}" % c)], [(name, c, None, None)]

    @staticmethod
    def _shown(deref, target, tc, fmt, view):
        if view == "shallow" and target[0] not in ("scalar",):
            return "{&%s}" % deref
        if target[0] in ("named", "variant") and tc:
            return "{%s,view(shallow)}" % deref
        return "{%s%s}" % (deref, fmt)


def f_text(t):
    return repr(t)


def combine(parts, limit=MAX_COMBOS):
    """Summary alternatives of fields in a row -> [(cond, [texts])]: the
    product of the alternatives, each with the conjunction of conditions.
    A field's last alternative is its unconditional default."""
    combos = [(None, [])]
    for alts in parts:
        if len(alts) == 1:
            combos = [(c, ts + [alts[0][1]]) for c, ts in combos]
            continue
        nxt = []
        for c, ts in combos:
            for cond, text in alts:
                if cond is None:
                    # The default: every earlier alternative's condition fails.
                    neg = [a for a, _ in alts if a is not None]
                    cond = " && ".join("!(%s)" % a for a in neg) if neg else None
                nxt.append((and_(c, cond), ts + [text]))
        combos = nxt
        if len(combos) > limit:
            return None
    return combos


def emit_fields_body(t, fields_obj, prefix, fields, head, cond=None, view="deep"):
    """DisplayStrings '<head> { f: v, ... }' for the fields at prefix, under cond."""
    parts, labels = [], []
    for f in fields:
        alts, _ = fields_obj.field(prefix, f, view)
        parts.append(alts)
        labels.append(f["name"])
    if not fields:
        display(t, head, cond, view)
        return
    combos = combine(parts)
    if combos is None:
        # Too many nullable fields: the defaults only (addresses for them).
        parts = [fields_obj.field(prefix, f, "compact")[0] for f in fields]
        combos = combine(parts, 1 << 30)
    for c, texts in combos:
        body = ", ".join("%s: %s" % (l, x) for l, x in zip(labels, texts))
        display(t, "%s {{ %s }}" % (head, body), and_(cond, c), view)


def emit_children(exp, fields_obj, prefix, fields, cond=None):
    for f in fields:
        _, kids = fields_obj.field(prefix, f, "deep")
        for name, expr, kc, syn in kids:
            if syn is not None:
                _, sums, ptr, ln, ec = syn
                s = sub(exp, "Synthetic", Name=name, Condition=cond)
                for c2, txt in sums:
                    display(s, txt, c2)
                e2 = sub(s, "Expand")
                sub(e2, "Item", ln, Name="[len]")
                list_children(e2, ptr, ln, ec)
                continue
            sub(exp, "Item", expr, Name=name, Condition=and_(cond, kc))


def emit_struct(out, prog, fo, td):
    c = td["c"]
    if not prog.struct_exists(c):
        return
    t = out.type(c, "struct %s" % td["name"])
    if t is None:
        return
    fields = td.get("fields", [])
    head = esc_braces(td["name"])
    emit_fields_body(t, fo, "", fields, head, view="deep")
    emit_fields_body(t, fo, "", fields, head, view="shallow")
    if fields:
        emit_children(sub(t, "Expand"), fo, "", fields)


def emit_enum(out, prog, fo, td):
    c = td["c"]
    if not prog.struct_exists(c):
        return
    t = out.type(c, "enum %s" % td["name"])
    if t is None:
        return
    name = esc_braces(td["name"])
    variants = td.get("variants", [])
    for view in ("deep", "shallow"):
        for v in variants:
            cond = "tag == %d" % v["tag"]
            head = "%s.%s" % (name, v["name"])
            emit_fields_body(t, fo, "u.v_%s_g." % v["name"], v.get("fields", []), head, cond, view)
        display(t, "%s #{tag,d}" % name, None, view)
    exp = sub(t, "Expand") if any(v.get("fields") for v in variants) else None
    for v in variants:
        if v.get("fields"):
            emit_children(exp, fo, "u.v_%s_g." % v["name"], v["fields"], "tag == %d" % v["tag"])
    # The payload types: the C type of a Shape.Circle value.
    for v in variants:
        if not v.get("fields"):
            continue
        pc = "%s_%s_p" % (c, v["name"])
        if not prog.struct_exists(pc):
            continue
        pt = out.type(pc, "%s.%s" % (td["name"], v["name"]))
        if pt is None:
            continue
        head = "%s.%s" % (name, v["name"])
        emit_fields_body(pt, fo, "", v["fields"], head, view="deep")
        emit_fields_body(pt, fo, "", v["fields"], head, view="shallow")
        emit_children(sub(pt, "Expand"), fo, "", v["fields"])


def emit_globals(out, prog, fo):
    """gs_globals_t: the globals under their Goose names, a resizable one
    with its elements. The main program's block is gs_globals_main (`dx
    gs_globals_main`); a thread's own copy is behind gs_gl."""
    if not prog.globals:
        return
    if prog.have_c and "gs_globals_t" not in prog.ctext_names:
        return
    t = out.type("gs_globals_t", "the globals")
    if t is None:
        return
    fields = [{"c": v["c"], "name": v["name"], "type": v["type"]} for v in prog.globals]
    emit_fields_body(t, fo, "", fields, "globals", view="deep")
    emit_fields_body(t, fo, "", fields, "globals", view="shallow")
    emit_children(sub(t, "Expand"), fo, "", fields)


def generate(map_paths, cpath=None, generic=False, include_generic=False):
    out = Out()
    notes = []
    if generic or include_generic:
        emit_generic(out)
    if generic and not map_paths:
        return out.text(), notes
    skip = set() if include_generic else generic_names()
    docs, ctexts = [], []
    for mp in map_paths:
        with open(mp, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") != 1:
            notes.append("%s: map version %r, not 1: skipped" % (mp, d.get("version")))
            continue
        docs.append(d)
        cp = cpath or (mp[:-len(".debug.json")] + ".c" if mp.endswith(".debug.json") else None)
        if cp and os.path.exists(cp):
            with open(cp, encoding="utf-8", errors="replace") as f:
                ctexts.append(f.read())
        elif cpath:
            raise SystemExit("cannot read %s" % cpath)
        else:
            notes.append("%s: no C beside it: array types are taken from the map's types" % mp)
    prog = Program(docs, "\n".join(ctexts))
    fo = Fields(prog, notes.append)
    out.names |= skip
    for name in sorted(prog.arrays):
        kind, ec, n, en = prog.arrays[name]
        emit_array(out, name, kind, ec, n, en)
    for gname in sorted(prog.types):
        td = prog.types[gname]
        if not td.get("c"):
            notes.append("%s: no C type (variable size): not in natvis; goose.js decodes it" % gname)
            continue
        if td.get("kind") == "struct":
            emit_struct(out, prog, fo, td)
        else:
            emit_enum(out, prog, fo, td)
    emit_globals(out, prog, fo)
    return out.text(), notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("maps", nargs="*", help="<stem>.debug.json files of one program")
    ap.add_argument("--generic", action="store_true",
                    help="write goose.natvis: the entries every program shares")
    ap.add_argument("--all", action="store_true",
                    help="the generic entries and the program's in one file (for /NATVIS)")
    ap.add_argument("--c", dest="cpath", help="the generated C (default: beside the map)")
    ap.add_argument("-o", dest="out", help="write here instead of stdout")
    ap.add_argument("-q", action="store_true", help="no notes on stderr")
    a = ap.parse_args(argv)
    if not a.maps and not a.generic:
        ap.error("a debug map, or --generic")
    text, notes = generate(a.maps, a.cpath, generic=a.generic and not a.maps,
                           include_generic=a.all or (a.generic and bool(a.maps)))
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    if not a.q:
        for n in notes:
            sys.stderr.write("gen_natvis: %s\n" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
