"""LLDB-side helpers for goose_dap.py, the Goose DAP proxy.

goose-dap loads this module into lldb-dap with `command script import`
(after goose_lldb.py) and calls its functions through `script` commands
sent as repl evaluations. Each function prints one line,
`GOOSE_DAP:<json>`, that the proxy parses. Nothing here may raise into
LLDB: every entry point prints an {"error": ...} object instead.

  info()               the program's compile units, the debug maps beside
                       them (as goose_lldb.py finds them) and the fields of
                       the globals block
  lines(path)          the lines of a source file that have code, from the
                       line tables (what a breakpoint can bind to)
  runtime_error(tid)   the message of the Goose runtime error a thread is
                       stopped in (gs_fail or gs_panic), and its location
"""

import json
import os

import lldb

MARK = "GOOSE_DAP:"


def _out(obj):
    print(MARK + json.dumps(obj))


def _target():
    t = getattr(lldb, "target", None)
    if t is not None and t.IsValid():
        return t
    return lldb.debugger.GetSelectedTarget()


def _cu_paths(target):
    """The compile units' source files that are C files. goose_lldb.py has
    the same walk (_cu_paths); this copy keeps the proxy working with a
    goose_lldb.py that renames it."""
    try:
        import goose_lldb
        return list(goose_lldb._cu_paths(target))
    except Exception:
        pass
    paths = []
    for m in target.module_iter():
        for i in range(m.GetNumCompileUnits()):
            p = m.GetCompileUnitAtIndex(i).GetFileSpec().fullpath
            if p and p.endswith(".c"):
                paths.append(p)
    return paths


def info():
    try:
        target = _target()
        cus = _cu_paths(target)
        maps = []
        for c in cus:
            j = c[:-2] + ".debug.json"
            try:
                cm, jm = os.path.getmtime(c), os.path.getmtime(j)
            except OSError:
                continue
            # As goose_lldb.get_map: a map older than its .c is stale (a
            # compiler without the map has rebuilt the program since).
            if jm + 1e-3 < cm:
                continue
            maps.append(j)
        block = []
        gt = target.FindFirstType("gs_globals_t")
        if gt.IsValid():
            gt = gt.GetCanonicalType()
            block = [gt.GetFieldAtIndex(i).GetName() for i in range(gt.GetNumberOfFields())]
        exe = target.GetExecutable()
        _out({"cus": cus, "maps": maps, "globals_block": block,
              "program": exe.fullpath if exe and exe.IsValid() else None,
              "lldb": lldb.SBDebugger.GetVersionString()})
    except Exception as e:
        _out({"error": "%s: %s" % (type(e).__name__, e)})


def _same_file(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return False


def lines(path):
    try:
        target = _target()
        found = set()
        base = os.path.basename(path)
        for m in target.module_iter():
            for i in range(m.GetNumCompileUnits()):
                cu = m.GetCompileUnitAtIndex(i)
                for k in range(cu.GetNumLineEntries()):
                    le = cu.GetLineEntryAtIndex(k)
                    fs = le.GetFileSpec()
                    if fs.GetFilename() != base:
                        continue
                    if le.GetLine() > 0 and _same_file(fs.fullpath, path):
                        found.add(le.GetLine())
        _out({"lines": sorted(found)})
    except Exception as e:
        _out({"error": "%s: %s" % (type(e).__name__, e)})


def _cstring(process, addr, limit=4096):
    if not addr:
        return None
    err = lldb.SBError()
    s = process.ReadCStringFromMemory(addr, limit, err)
    return s if err.Success() else None


def runtime_error(tid):
    """The runtime error thread `tid` is stopped in: gs_fail(msg, len, file,
    line) is where every failed check and abort ends (gs_abort, gs_idxfail,
    gs_ovf, ... call it); gs_panic(msg) is the runtime's own fatal error."""
    try:
        target = _target()
        process = target.GetProcess()
        thread = process.GetThreadByID(int(tid))
        if not thread.IsValid():
            _out({"error": "no thread %s" % tid})
            return
        for i in range(min(thread.GetNumFrames(), 16)):
            fr = thread.GetFrameAtIndex(i)
            fn = fr.GetFunctionName() or ""
            if fn == "gs_fail":
                msg = fr.FindVariable("msg").GetValueAsUnsigned()
                n = fr.FindVariable("len").GetValueAsSigned()
                text = ""
                if msg and 0 < n < 1 << 16:
                    err = lldb.SBError()
                    data = process.ReadMemory(msg, n, err)
                    if err.Success() and data is not None:
                        text = bytes(data).decode("utf-8", "replace")
                elif n == 0:
                    text = ""
                f = _cstring(process, fr.FindVariable("file").GetValueAsUnsigned())
                line = fr.FindVariable("line").GetValueAsSigned()
                _out({"function": fn, "frame": i, "message": text, "file": f,
                      "line": line if f else None})
                return
            if fn == "gs_panic":
                text = _cstring(process, fr.FindVariable("msg").GetValueAsUnsigned()) or ""
                _out({"function": fn, "frame": i, "message": text, "file": None, "line": None})
                return
        _out({"error": "not stopped in gs_fail or gs_panic"})
    except Exception as e:
        _out({"error": "%s: %s" % (type(e).__name__, e)})


def _patch_has_children():
    """lldb-vscode 16 and lldb-dap 18 give a variable a variablesReference
    only if MightHaveChildren(), which asks the synthetic provider's
    has_children(), possibly before update() has run; goose_lldb.GooseSynth
    then answers from its still-empty child list, so a struct field (u.pos)
    cannot be expanded. Until goose_lldb.py does this itself, patch it."""
    try:
        import goose_lldb
        cls = goose_lldb.GooseSynth
    except Exception:
        return
    if getattr(cls, "_goose_dap_has_children", False):
        return
    orig = cls.has_children

    def has_children(self):
        if not self.kids:
            try:
                self.update()
            except Exception:
                pass
        if orig(self):
            return True
        # LLDB keeps the first answer for as long as the frame's variable
        # lives (lldb-vscode 16: a resizable seen before it was filled in
        # then never expands), so answer "might", not "has now".
        return getattr(self, "kind", None) not in (None, "cenum")

    cls.has_children = has_children
    cls._goose_dap_has_children = True


def __lldb_init_module(debugger, internal_dict):
    _patch_has_children()
