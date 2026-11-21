"""LLDB formatters for programs built by the Goose compiler.

Goose compiles to C, so LLDB sees the generated C types. This module
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

Load it with `command script import /path/to/goose_lldb.py`.

Tag names are not in the debug info: the generated C declares them in an
anonymous enum that the C compiler drops as unused. They are read from the
generated C file named by the compile unit, when that file still exists.
Set GOOSE_LLDB_CSOURCE=0 to turn that off and see only what DWARF gives.

Nothing here may raise into LLDB: every entry point catches its errors and
falls back to showing less.
"""

import os
import re

import lldb

CATEGORY = "goose"

# Caps, so that a garbage header (an uninitialized local) cannot make LLDB
# read gigabytes or list millions of children.
MAX_CHILDREN = 256
MAX_TEXT = 512
MAX_INLINE = 16
MAX_SUMMARY = 400
MAX_LEN = 1 << 40

_FIXED_RE = re.compile(r"^a[0-9]+_")
_LIMITED_RE = re.compile(r"^l[0-9]+_")
# A Goose field, local or function name: x_g, it_g_2, bump_g_1, x_g2.
_NAME_RE = re.compile(r"^(.+?)_g(_?[0-9]+)?$")


def _csource_enabled():
    return os.environ.get("GOOSE_LLDB_CSOURCE", "1") not in ("0", "off", "no", "false")


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
# Classification


def _fields(t):
    t = t.GetCanonicalType()
    if t.GetTypeClass() not in (lldb.eTypeClassStruct, lldb.eTypeClassClass):
        return None
    return [t.GetFieldAtIndex(i) for i in range(t.GetNumberOfFields())]


def type_name(t):
    """The name a C type is written with, typedef kept, qualifiers and the
    struct keyword dropped. (SBType.GetUnqualifiedType strips typedefs too.)"""
    n = t.GetName() or ""
    for q in ("const ", "volatile ", "struct "):
        while n.startswith(q):
            n = n[len(q):]
    return n


def _is_goose_name(name):
    return name.endswith("_g") or "_g_" in name


def classify(t):
    """The kind of Goose value a C type holds, or None."""
    try:
        name = type_name(t)
        canon = t.GetCanonicalType()
        if t.IsTypedefType() and canon.IsPointerType():
            if name.startswith("r_"):
                return "ref"
            if name.startswith("o_"):
                return "opt"
            return None
        fs = _fields(t)
        if fs is None:
            return None
        names = [f.GetName() for f in fs]
        if name in ("gs_rhdr",) and names == ["base", "len"]:
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
        if names == ["tag", "u"] and fs[1].GetType().GetCanonicalType().GetTypeClass() == lldb.eTypeClassUnion:
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


def recognize(sbtype, internal_dict):
    return classify(sbtype) is not None


# --------------------------------------------------------------------------
# Tag names from the generated C


_enum_cache = {}
_ENUM_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)_k\s*=\s*([0-9]+)\s*,")


def _cu_paths(target):
    paths = []
    try:
        for m in target.module_iter():
            for i in range(m.GetNumCompileUnits()):
                fs = m.GetCompileUnitAtIndex(i).GetFileSpec()
                p = fs.fullpath
                if p and p.endswith(".c"):
                    paths.append(p)
    except Exception:
        pass
    return paths


def _tag_constants(target):
    """{"Shape_g_Circle_g": 0, ...} from the generated C files, or {}."""
    if not _csource_enabled():
        return {}
    paths = tuple(_cu_paths(target))
    if paths in _enum_cache:
        return _enum_cache[paths]
    consts = {}
    for p in paths:
        try:
            if os.path.getsize(p) > 64 << 20:
                continue
            with open(p, "r", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        for block in re.finditer(r"enum\s*\{([^}]*)\}\s*;", text):
            for m in _ENUM_RE.finditer(block.group(1)):
                consts[m.group(1)] = int(m.group(2))
    _enum_cache[paths] = consts
    return consts


def tag_name(valobj, enum_cname, tag):
    """The variant name for a tag value, or None when it cannot be known."""
    try:
        target = valobj.GetTarget()
        prefix = enum_cname + "_"
        for cname, v in _tag_constants(target).items():
            if v == tag and cname.startswith(prefix) and cname.endswith("_g"):
                return cname[len(prefix):-2]
    except Exception:
        pass
    return None


def payload_owner(valobj, payload_cname):
    """For Shape_g_Circle_p, ("Shape_g", "Circle"): the enum whose union has a
    member v_Circle_g of this type. Found in DWARF, so it needs the enum to be
    used somewhere in the program."""
    try:
        target = valobj.GetTarget()
        stem = payload_cname[:-2]
        i = stem.find("_g_")
        if i <= 0:
            return None, None
        # Shape_g_Circle splits after Shape_g, Opt_g_i64_Some after
        # Opt_g_i64: try each underscore past the _g.
        j = stem.find("_", i + 2)
        while j > 0:
            ename, variant = stem[:j], stem[j + 1:]
            et = target.FindFirstType(ename)
            if et.IsValid():
                u = _member(et, "u")
                if u is not None and _member(u, "v_%s_g" % variant) is not None:
                    return ename, variant
            j = stem.find("_", j + 1)
        # No enum using it was found: split at the first _g_.
        if i > 0:
            return stem[:i + 2], stem[i + 3:]
    except Exception:
        pass
    return None, None


def _member(t, name):
    t = t.GetCanonicalType()
    for i in range(t.GetNumberOfFields()):
        f = t.GetFieldAtIndex(i)
        if f.GetName() == name:
            return f.GetType()
    return None


# --------------------------------------------------------------------------
# Values


def _clip(s, n=MAX_SUMMARY):
    return s if len(s) <= n else s[:n - 3] + "..."


def _int_of(v):
    """A scalar child's value as text: integers in decimal (u8 is a number in
    Goose, not a character), floats as LLDB prints them."""
    t = v.GetType().GetCanonicalType()
    basic = t.GetBasicType()
    if basic in (lldb.eBasicTypeFloat, lldb.eBasicTypeDouble, lldb.eBasicTypeLongDouble):
        return v.GetValue() or "?"
    if basic == lldb.eBasicTypeBool:
        return "true" if v.GetValueAsUnsigned() else "false"
    if t.GetTypeFlags() & lldb.eTypeIsInteger:
        if t.GetTypeFlags() & lldb.eTypeIsSigned:
            return str(v.GetValueAsSigned())
        return str(v.GetValueAsUnsigned())
    return None


def value_text(v):
    """A child's value as one line of text."""
    try:
        if not v.IsValid() or v.GetError().Fail():
            return "?"
        s = _int_of(v)
        if s is not None:
            return s
        s = v.GetSummary()
        if s:
            return s
        t = v.GetType().GetCanonicalType()
        if t.IsPointerType():
            # A reference is transparent in Goose: show what a field such
            # as `line: i64&` points at, when it is a readable scalar.
            a = v.GetValueAsUnsigned()
            if a == 0:
                return "null"
            d = v.Dereference()
            if d.IsValid() and d.GetError().Success():
                s = _int_of(d)
                if s is not None:
                    return s
            return "0x%x" % a
        s = v.GetValue()
        if s:
            return s
        n = v.GetNumChildren()
        parts = [value_text(v.GetChildAtIndex(i)) for i in range(min(n, MAX_INLINE))]
        if n > MAX_INLINE:
            parts.append("...")
        return "{" + ", ".join(parts) + "}"
    except Exception:
        return "?"


def _is_u8(t):
    t = t.GetCanonicalType()
    return t.GetByteSize() == 1 and t.GetBasicType() in (
        lldb.eBasicTypeUnsignedChar, lldb.eBasicTypeChar, lldb.eBasicTypeSignedChar)


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


def read_text(process, addr, n):
    if n == 0:
        return '""'
    if addr == 0:
        return "<null data, len=%d>" % n
    k = min(n, MAX_TEXT)
    err = lldb.SBError()
    data = process.ReadMemory(addr, k, err)
    if err.Fail() or data is None:
        return "<unreadable at 0x%x, len=%d>" % (addr, n)
    return quote_bytes(bytearray(data), n)


def _list_text(children, total):
    parts = [value_text(c) for c in children[:MAX_INLINE]]
    if total > MAX_INLINE:
        parts.append("... (%d elements)" % total)
    return "[" + ", ".join(parts) + "]"


# --------------------------------------------------------------------------
# Synthetic children


class GooseSynth:
    """Children for each kind: the elements of an array or slice, the target's
    children for a reference, the tag and active payload of an enum."""

    def __init__(self, valobj, internal_dict):
        self.valobj = valobj
        self.kind = None
        self.kids = []
        try:
            self.kind = classify(valobj.GetType())
        except Exception:
            pass

    def update(self):
        self.kids = []
        try:
            self.kids = self._children()
        except Exception:
            self.kids = []
        return False

    def _raw(self):
        v = self.valobj.GetNonSyntheticValue()
        return [v.GetChildAtIndex(i) for i in range(v.GetNumChildren())]

    def _children(self):
        v = self.valobj.GetNonSyntheticValue()
        k = self.kind
        if k == "slice":
            data = v.GetChildMemberWithName("data")
            n = v.GetChildMemberWithName("len").GetValueAsSigned()
            et = data.GetType().GetPointeeType()
            return self._elements(data.GetValueAsUnsigned(), et, n)
        if k == "fixed":
            e = v.GetChildMemberWithName("e")
            return [e.GetChildAtIndex(i) for i in range(min(e.GetNumChildren(), MAX_CHILDREN))]
        if k == "limited":
            e = v.GetChildMemberWithName("e")
            n = v.GetChildMemberWithName("len").GetValueAsUnsigned()
            n = min(n, e.GetNumChildren(), MAX_CHILDREN)
            return [e.GetChildAtIndex(i) for i in range(n)]
        if k in ("ref", "opt"):
            if v.GetValueAsUnsigned() == 0:
                return []
            d = v.Dereference()
            if d.GetNumChildren() == 0:
                return [d.Clone("*")]
            return [d.GetChildAtIndex(i) for i in range(min(d.GetNumChildren(), MAX_CHILDREN))]
        if k == "adt":
            tag = v.GetChildMemberWithName("tag")
            kids = [tag]
            name = tag_name(v, _cname(v), tag.GetValueAsUnsigned())
            u = v.GetChildMemberWithName("u")
            if name is not None:
                p = u.GetChildMemberWithName("v_%s_g" % name)
                if p.IsValid():
                    kids.append(p)
            else:
                kids.append(u)
            return kids
        return self._raw()

    def _elements(self, addr, et, n):
        if n <= 0 or n > MAX_LEN or addr == 0:
            return []
        size = et.GetByteSize()
        if size == 0:
            return []
        out = []
        for i in range(min(n, MAX_CHILDREN)):
            out.append(self.valobj.CreateValueFromAddress("[%d]" % i, addr + i * size, et))
        return out

    def num_children(self):
        return len(self.kids)

    def get_child_index(self, name):
        try:
            if name.startswith("[") and name.endswith("]"):
                return int(name[1:-1])
            for i, c in enumerate(self.kids):
                if c.GetName() == name:
                    return i
        except Exception:
            pass
        return -1

    def get_child_at_index(self, index):
        if 0 <= index < len(self.kids):
            return self.kids[index]
        return None

    def has_children(self):
        return len(self.kids) > 0


def _cname(v):
    return type_name(v.GetType())


# --------------------------------------------------------------------------
# Summaries


def summary(valobj, internal_dict, options=None):
    try:
        s = _summary(valobj)
        return _clip(s) if s is not None else ""
    except Exception as e:
        return "<goose_lldb: %s>" % type(e).__name__


def _summary(valobj):
    v = valobj.GetNonSyntheticValue()
    k = classify(v.GetType())
    proc = v.GetProcess()
    if k is None and v.GetType().GetCanonicalType().IsPointerType():
        # A cascade through a pointer: a struct field Node_g *next_g, which
        # is a Node& or a Node? (DWARF does not say which), or a gs_rhdr *.
        if v.GetValueAsUnsigned() == 0:
            return "null"
        d = v.Dereference()
        if d.GetError().Fail() or classify(d.GetType()) is None:
            return None
        return _summary(d)

    if k == "slice":
        data = v.GetChildMemberWithName("data")
        n = v.GetChildMemberWithName("len").GetValueAsSigned()
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        et = data.GetType().GetPointeeType()
        if _is_u8(et):
            return read_text(proc, data.GetValueAsUnsigned(), n)
        if data.GetValueAsUnsigned() == 0 and n != 0:
            return "<null data, len=%d>" % n
        kids = [valobj.CreateValueFromAddress("[%d]" % i, data.GetValueAsUnsigned() + i * et.GetByteSize(), et)
                for i in range(min(n, MAX_INLINE))]
        return _list_text(kids, n)

    if k == "fixed":
        e = v.GetChildMemberWithName("e")
        n = e.GetNumChildren()
        if _is_u8(e.GetType().GetArrayElementType()):
            return quote_bytes(bytearray(e.GetChildAtIndex(i).GetValueAsUnsigned() for i in range(min(n, MAX_TEXT))), n)
        return _list_text([e.GetChildAtIndex(i) for i in range(min(n, MAX_INLINE))], n)

    if k == "limited":
        e = v.GetChildMemberWithName("e")
        cap = e.GetNumChildren()
        n = v.GetChildMemberWithName("len").GetValueAsUnsigned()
        if n > cap:
            return "<invalid len=%d, cap=%d>" % (n, cap)
        if _is_u8(e.GetType().GetArrayElementType()):
            return quote_bytes(bytearray(e.GetChildAtIndex(i).GetValueAsUnsigned() for i in range(min(n, MAX_TEXT))), n)
        return _list_text([e.GetChildAtIndex(i) for i in range(min(n, MAX_INLINE))], n)

    if k == "rhdr":
        n = v.GetChildMemberWithName("len").GetValueAsSigned()
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        return "len=%d, element type not in debug info" % n

    if k == "rref":
        h = v.GetChildMemberWithName("hdr")
        if h.GetValueAsUnsigned() == 0:
            return "null"
        n = h.Dereference().GetChildMemberWithName("len").GetValueAsSigned()
        if n < 0 or n > MAX_LEN:
            return "<invalid len=%d>" % n
        return "len=%d, element type not in debug info" % n

    if k in ("ref", "opt"):
        if v.GetValueAsUnsigned() == 0:
            return "null"
        d = v.Dereference()
        if d.GetError().Fail():
            return "<unreadable 0x%x>" % v.GetValueAsUnsigned()
        return value_text(d)

    if k == "cenum":
        cn = _cname(v)
        tag = v.GetChildMemberWithName("tag").GetValueAsUnsigned()
        name = tag_name(v, cn, tag)
        if name is None:
            return "%s #%d" % (goose_type_name(cn), tag)
        return "%s.%s" % (goose_type_name(cn), name)

    if k == "adt":
        cn = _cname(v)
        tag = v.GetChildMemberWithName("tag").GetValueAsUnsigned()
        name = tag_name(v, cn, tag)
        tn = goose_type_name(cn)
        if name is None:
            return "%s #%d (tag names not in debug info)" % (tn, tag)
        p = v.GetChildMemberWithName("u").GetChildMemberWithName("v_%s_g" % name)
        if not p.IsValid():
            return "%s.%s" % (tn, name)
        return "%s.%s %s" % (tn, name, _fields_text(p))

    if k == "payload":
        cn = _cname(v)
        ename, variant = payload_owner(v, cn)
        prefix = "%s.%s" % (goose_type_name(ename), variant) if ename else cn
        return "%s %s" % (prefix, _fields_text(v))

    if k == "struct":
        return "%s %s" % (goose_type_name(_cname(v)), _fields_text(v))

    return None


def bytes_summary(valobj, internal_dict, options=None):
    """uint8_t *: in a Goose program this is a variable-size value (T[],
    a variable-mode enum, a struct with varint fields) or a data-stack top,
    never a NUL-terminated string, so LLDB's C-string summary misleads."""
    return "<untyped bytes>"


def _fields_text(v):
    parts = []
    for i in range(min(v.GetNumChildren(), MAX_INLINE)):
        c = v.GetChildAtIndex(i)
        parts.append("%s: %s" % (goose_name(c.GetName()), value_text(c)))
    if v.GetNumChildren() > MAX_INLINE:
        parts.append("...")
    return "{ " + ", ".join(parts) + " }" if parts else "{}"


# --------------------------------------------------------------------------
# Commands


def cmd_locals(debugger, command, exe_ctx, result, internal_dict):
    """goose-locals: the selected frame's Goose locals and arguments, without
    compiler temporaries (tN) and hidden arguments (gs_sp), under their Goose
    names."""
    try:
        frame = exe_ctx.GetFrame()
        if not frame.IsValid():
            result.SetError("no frame selected")
            return
        vs = frame.GetVariables(True, True, False, True)
        for i in range(vs.GetSize()):
            v = vs.GetValueAtIndex(i)
            cn = v.GetName() or ""
            if not _NAME_RE.match(cn):
                continue
            if classify(v.GetType()) is not None:
                s = v.GetSummary() or value_text(v)
            elif v.GetType().GetCanonicalType().IsPointerType():
                s = "0x%x" % v.GetValueAsUnsigned()
            else:
                s = value_text(v)
            result.AppendMessage("%s = %s" % (goose_name(cn), s))
    except Exception as e:
        result.SetError("goose-locals: %s" % e)


_SCALARS = {
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "f32": "float", "f64": "double", "bool": "uint8_t",
}


def cmd_as(debugger, command, exe_ctx, result, internal_dict):
    """goose-as VAR TYPE: shows VAR's elements as TYPE, which the debug info
    cannot say. VAR is a resizable (gs_rhdr, gs_rref: TYPE is the element
    type) or a uint8_t * holding a T[] (TYPE written as T[]: a u32 count,
    then the elements). TYPE is a Goose scalar (i64, u8, f64, ...) or a C
    type name (Point_g, Shape_g, a4_i32)."""
    try:
        args = command.split()
        if len(args) != 2:
            result.SetError("usage: goose-as VAR TYPE  (TYPE: i64, Point_g, ... or T[] for a T[])")
            return
        path, tname = args
        frozen = tname.endswith("[]")
        if frozen:
            tname = tname[:-2]
        frame = exe_ctx.GetFrame()
        v = frame.GetValueForVariablePath(path)
        if not v.IsValid():
            v = exe_ctx.GetTarget().EvaluateExpression(path)
        if not v.IsValid() or v.GetError().Fail():
            result.SetError("no variable %s" % path)
            return
        et = exe_ctx.GetTarget().FindFirstType(_SCALARS.get(tname, tname))
        if not et.IsValid() or et.GetByteSize() == 0:
            result.SetError("unknown type %s" % tname)
            return
        v = v.GetNonSyntheticValue()
        k = classify(v.GetType())
        if frozen:
            addr = v.GetValueAsUnsigned()
            err = lldb.SBError()
            n = exe_ctx.GetProcess().ReadUnsignedFromMemory(addr, 4, err)
            if err.Fail():
                result.SetError("cannot read the count at 0x%x" % addr)
                return
            addr += 4
        elif k in ("rhdr", "rref"):
            h = v if k == "rhdr" else v.GetChildMemberWithName("hdr").Dereference()
            addr = h.GetChildMemberWithName("base").GetValueAsUnsigned()
            n = h.GetChildMemberWithName("len").GetValueAsSigned()
        else:
            result.SetError("%s is not a resizable; for a T[] write the type as %s[]" % (path, tname))
            return
        if n < 0 or n > MAX_LEN:
            result.SetError("invalid length %d" % n)
            return
        size = et.GetByteSize()
        if _is_u8(et):
            result.AppendMessage("len=%d %s" % (n, read_text(exe_ctx.GetProcess(), addr, n)))
            return
        result.AppendMessage("len=%d" % n)
        for i in range(min(n, MAX_CHILDREN)):
            c = v.CreateValueFromAddress("[%d]" % i, addr + i * size, et)
            result.AppendMessage("  [%d] = %s" % (i, value_text(c)))
        if n > MAX_CHILDREN:
            result.AppendMessage("  ... (%d more)" % (n - MAX_CHILDREN))
    except Exception as e:
        result.SetError("goose-as: %s" % e)


# --------------------------------------------------------------------------
# Registration


def __lldb_init_module(debugger, internal_dict):
    mod = __name__
    if debugger.GetCategory(CATEGORY).IsValid():
        debugger.DeleteCategory(CATEGORY)
    cat = debugger.CreateCategory(CATEGORY)
    spec = lldb.SBTypeNameSpecifier("%s.recognize" % mod, lldb.eFormatterMatchCallback)
    opts = lldb.eTypeOptionCascade | lldb.eTypeOptionHideChildren
    cat.AddTypeSummary(spec, lldb.SBTypeSummary.CreateWithFunctionName("%s.summary" % mod, opts))
    cat.AddTypeSynthetic(spec, lldb.SBTypeSynthetic.CreateWithClassName(
        "%s.GooseSynth" % mod, lldb.eTypeOptionCascade))
    # u8 is a number in Goose; there is no char type.
    for t in ("uint8_t", "int8_t"):
        cat.AddTypeFormat(lldb.SBTypeNameSpecifier(t, False),
                          lldb.SBTypeFormat(lldb.eFormatDecimal,
                                            lldb.eTypeOptionCascade | lldb.eTypeOptionSkipPointers
                                            | lldb.eTypeOptionSkipReferences))
    cat.AddTypeSummary(lldb.SBTypeNameSpecifier("uint8_t *", False),
                       lldb.SBTypeSummary.CreateWithFunctionName("%s.bytes_summary" % mod, 0))
    cat.SetEnabled(True)
    debugger.HandleCommand("command script add -o -f %s.cmd_locals goose-locals" % mod)
    debugger.HandleCommand("command script add -o -f %s.cmd_as goose-as" % mod)
