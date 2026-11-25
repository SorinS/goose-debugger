#!/usr/bin/env python3
"""Tests of goose-dap: each one builds a sample with `goose build -g`, runs
it under goose-dap through a DAP client over pipes (as an IDE does), and
checks what the client sees.

usage: GOOSE=/path/to/goose dap/test/run.py [-k NAME] [-v] [--keep]

GOOSE is a compiler with -g (patches 0001-0007). LLDB_DAP (or
GOOSE_DAP_LLDB_DAP) picks the lldb-dap goose-dap runs. GOOSE_DAP_WORK keeps
the binaries and logs in a directory of your choice. GOOSE_DAP_ADAPTER
tests another copy of goose-dap (a snapshot's bin/goose-dap).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DAP = os.path.dirname(HERE)
REPO = os.path.dirname(DAP)
sys.path.insert(0, HERE)
from dapclient import Dap, AdapterExited  # noqa: E402

# GOOSE_DAP_ADAPTER: another copy of goose-dap to test (a snapshot's).
GOOSE_DAP = os.environ.get("GOOSE_DAP_ADAPTER") or os.path.join(DAP, "goose-dap")
SAMPLES = {
    "area": os.path.join(REPO, "tests", "area.goose"),
    "structs": os.path.join(REPO, "lldb", "test", "structs.goose"),
    "resizable": os.path.join(REPO, "lldb", "test", "resizable.goose"),
    "layouts": os.path.join(REPO, "lldb", "test", "layouts.goose"),
    "assert": os.path.join(REPO, "tests", "backtrace", "assert.goose"),
    "bounds": os.path.join(HERE, "progs", "bounds.goose"),
    "twice": os.path.join(HERE, "progs", "twice.goose"),
}
VERBOSE = False


def compiler():
    g = os.environ.get("GOOSE")
    if g:
        return g
    d = os.path.expanduser("~/Dev/Goose/goose.git/build-debug/goose-debug")
    return d if os.path.exists(d) else (shutil.which("goose") or "goose")


class Fail(Exception):
    pass


def check(cond, what):
    if not cond:
        raise Fail(what)


def eq(got, want, what):
    if got != want:
        raise Fail("%s: got %r, want %r" % (what, got, want))


# ---- Windows: the adapter run through Python (goose-dap is a sh script),
# executables named .exe, and processes listed, probed and killed through
# the Win32 API (os.kill(pid, 0) there sends a Ctrl-C to a console group).
# A Windows process keeps its parent's pid after the parent exits, and pids
# are soon reused: csrss.exe and wininit.exe name a long-gone smss.exe, whose
# pid the adapter may have now. So a child counts only if it started after
# its parent, and only the adapter's, LLDB's and the samples' images are
# ever killed (killing csrss.exe stops the machine).
WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""
ADAPTER = [sys.executable, "-u", os.path.join(os.path.dirname(os.path.realpath(GOOSE_DAP)), "goose_dap.py")] \
    if WINDOWS else [GOOSE_DAP]
KILLABLE = {"python.exe", "lldb.exe", "lldb-dap.exe", "lldb-server.exe", "conhost.exe"}


def _win_processes():
    """{pid: (parent pid, creation time or None, image name)}, from a
    Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    out = {}
    e = PE()
    e.dwSize = ctypes.sizeof(PE)
    ok = k.Process32First(snap, ctypes.byref(e))
    while ok:
        ctime = None
        h = k.OpenProcess(0x1000, False, e.th32ProcessID)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            t = [ctypes.c_ulonglong() for _ in range(4)]
            if k.GetProcessTimes(wintypes.HANDLE(h), *[ctypes.byref(x) for x in t]):
                ctime = t[0].value
            k.CloseHandle(wintypes.HANDLE(h))
        out[e.th32ProcessID] = (e.th32ParentProcessID, ctime,
                                e.szExeFile.decode("mbcs", "replace").lower())
        ok = k.Process32Next(snap, ctypes.byref(e))
    k.CloseHandle(wintypes.HANDLE(snap))
    return out


def _win_alive(pid):
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))
    k.CloseHandle(wintypes.HANDLE(h))
    return code.value == 259   # STILL_ACTIVE


def kill_pid(pid):
    """Kill one process of the test's; on Windows only one whose image is
    the adapter's, LLDB's or a sample's."""
    if WINDOWS:
        info = _win_processes().get(pid)
        if info is None or not (info[2] in KILLABLE or
                                os.path.splitext(info[2])[0] in SAMPLES or info[2] == "area-built.exe"):
            return
    os.kill(pid, 9)


def descendants(pid):
    """Every live process below pid (ps, no /proc on macOS)."""
    if WINDOWS:
        procs = _win_processes()
        kids = {}
        for p, (pp, ctime, _) in procs.items():
            parent = procs.get(pp)
            # Started after the parent: not an orphan whose parent's pid was reused.
            if parent and parent[1] is not None and ctime is not None and ctime >= parent[1]:
                kids.setdefault(pp, []).append(p)
        found, todo = [], [pid]
        while todo:
            for c in kids.get(todo.pop(), []):
                if c not in found and c != pid:
                    found.append(c)
                    todo.append(c)
        return found
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    except OSError:
        return []
    kids = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2:
            kids.setdefault(int(p[1]), []).append(int(p[0]))
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            found.append(c)
            todo.append(c)
    return found


def alive(pid):
    if WINDOWS:
        return _win_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is not running.
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


class Session:
    """One goose-dap session on one program."""

    def __init__(self, t, program, **launch):
        self.t = t
        self.log = []
        self.logfile = os.path.join(t.work, "%s.goose-dap.log" % t.name)
        env = dict(os.environ, GOOSE_DAP_LOG=self.logfile)
        self.d = Dap(ADAPTER, env=env, log=self.log)
        self.program = program
        self.launch_args = dict({"program": program, "cwd": t.work}, **launch)
        self.tree = []
        self.tid = None

    def start(self, lines=None, functions=None, exceptions=("goose_runtime",)):
        d = self.d
        r = d.request("initialize", {"clientID": "goose-dap-test", "adapterID": "goose",
                                     "linesStartAt1": True, "columnsStartAt1": True,
                                     "pathFormat": "path", "supportsVariableType": True})
        check(r["success"], "initialize failed: %s" % r.get("message"))
        self.caps = r["body"]
        d.send("launch", self.launch_args)
        d.event("initialized", timeout=120)
        self.tree = descendants(d.p.pid)
        self.bps = {}
        for src, ls in (lines or {}).items():
            r = d.request("setBreakpoints", {"source": {"path": src},
                                             "breakpoints": [{"line": n} for n in ls]})
            self.bps[src] = r["body"]["breakpoints"]
        self.fbps = None
        if functions is not None:
            r = d.request("setFunctionBreakpoints", {"breakpoints": [{"name": n} for n in functions]})
            check(r["success"], "setFunctionBreakpoints failed")
            self.fbps = r["body"]["breakpoints"]
        if exceptions is not None:
            r = d.request("setExceptionBreakpoints", {"filters": list(exceptions)})
            check(r["success"], "setExceptionBreakpoints failed")
        r = d.request("configurationDone")
        check(r["success"], "configurationDone failed")
        r = d.wait(lambda m: m.get("type") == "response" and m.get("command") == "launch", timeout=60)
        check(r["success"], "launch failed: %s" % r.get("message"))
        return self

    def stopped(self, timeout=30):
        st = self.d.event("stopped", timeout)
        self.tid = st["body"]["threadId"]
        self.stop = st["body"]
        return st["body"]

    def frames(self):
        r = self.d.request("stackTrace", {"threadId": self.tid, "startFrame": 0, "levels": 50})
        check(r["success"], "stackTrace failed")
        return r["body"]["stackFrames"]

    def top(self):
        return self.frames()[0]

    def scopes(self, frame=None):
        f = frame or self.top()
        r = self.d.request("scopes", {"frameId": f["id"]})
        return {s["name"]: s for s in r["body"]["scopes"]}

    def variables(self, ref):
        r = self.d.request("variables", {"variablesReference": ref})
        check(r["success"], "variables failed")
        return r["body"]["variables"]

    def locals(self, frame=None):
        return self.variables(self.scopes(frame)["Locals"]["variablesReference"])

    def evaluate(self, expr, context="watch", frame=None):
        f = frame or self.top()
        r = self.d.request("evaluate", {"expression": expr, "frameId": f["id"], "context": context})
        return r

    def value(self, expr, context="watch"):
        r = self.evaluate(expr, context)
        check(r["success"], "evaluate %r failed: %s" % (expr, r.get("message")))
        return r["body"]["result"]

    def step(self, cmd):
        r = self.d.request(cmd, {"threadId": self.tid})
        check(r["success"], "%s failed" % cmd)
        return self.stopped()

    def cont(self):
        self.d.request("continue", {"threadId": self.tid})

    def run_to_exit(self, code=None):
        e = self.d.event("exited", 60)
        if code is not None:
            eq(e["body"].get("exitCode"), code, "exit code")
        self.d.event("terminated", 30)
        return e

    def output(self):
        return "".join(m["body"].get("output", "") for m in self.d.events("output")
                       if m["body"].get("category") in ("stdout", "stderr"))

    def finish(self):
        """Disconnect, close stdin, and check that goose-dap, lldb-dap and
        the debuggee are all gone."""
        d = self.d
        self.tree = sorted(set(self.tree + descendants(d.p.pid)))
        try:
            r = d.request("disconnect", {"terminateDebuggee": True}, timeout=15)
            check(r["success"], "disconnect failed")
        except AdapterExited:
            pass
        rc = d.close(timeout=15)
        left = []
        end = time.time() + 5
        while time.time() < end:
            left = [p for p in self.tree if alive(p)]
            if not left:
                break
            time.sleep(0.1)
        for p in left:
            try:
                kill_pid(p)
            except OSError:
                pass
        check(not left, "processes left running: %s" % left)
        eq(rc, 0, "goose-dap exit status")

    def kill(self):
        for p in [self.d.p.pid] + descendants(self.d.p.pid) + self.tree:
            try:
                if alive(p):
                    kill_pid(p)
            except OSError:
                pass


class T:
    def __init__(self, name, work, bins):
        self.name = name
        self.work = work
        self.bins = bins
        self.sessions = []

    def session(self, sample, **launch):
        s = Session(self, self.bins[sample], **launch)
        self.sessions.append(s)
        return s


def names(vs):
    return [v["name"] for v in vs]


def assert_goose_stack(frames):
    for f in frames:
        src = f.get("source") or {}
        p = src.get("path") or ""
        if p.endswith(".c"):
            eq(src.get("presentationHint"), "deemphasize", "hint of the C source of %s" % f["name"])
            eq(f.get("presentationHint"), "subtle", "hint of C frame %s" % f["name"])
        check(not f["name"].startswith("gs_"), "a runtime frame is shown: %s" % f["name"])
        check(not re.match(r".*_g(_?[0-9]+)?$", f["name"]), "a C name is shown: %s" % f["name"])


# --------------------------------------------------------------------------
# The tests


def test_capabilities(t):
    s = t.session("area").start()
    c = s.caps
    eq([f["filter"] for f in c.get("exceptionBreakpointFilters", [])], ["goose_runtime"], "filters")
    check(not c.get("supportsCompletionsRequest"), "completions offered")
    check(c.get("supportsFunctionBreakpoints"), "no function breakpoints")
    s.run_to_exit(0)
    s.finish()


def test_line_breakpoint_and_steps(t):
    src = SAMPLES["area"]
    s = t.session("area").start(lines={src: [9]})
    b = s.bps[src][0]
    check(b["verified"], "breakpoint at area.goose:9 not verified")
    eq(b["line"], 9, "breakpoint line")
    st = s.stopped()
    eq(st["reason"], "breakpoint", "stop reason")
    fs = s.frames()
    assert_goose_stack(fs)
    eq(fs[0]["name"], "main", "top frame")
    eq(fs[0]["line"], 9, "top line")
    check(fs[0]["source"]["path"].endswith("area.goose"), "top source")
    check(fs[0].get("presentationHint") != "subtle", "the Goose frame is subtle")
    eq(names(s.locals()), ["total", "i"], "locals of main at 9")
    eq(s.value("total"), "0", "total")
    eq(s.value("i"), "0", "i")
    eq(s.value("total", "hover"), "0", "hover total")
    eq(s.value("total + i + 1"), "1", "total + i + 1")
    r = s.evaluate("goose-locals", "repl")
    check(r["success"] and "total: i64 = 0" in r["body"]["result"], "repl goose-locals: %r" % r)
    lv = {v["name"]: v for v in s.locals()}
    eq(lv["total"]["type"], "i64", "total's type")
    eq(lv["total"]["evaluateName"], "total", "total's evaluateName")

    s.step("stepIn")
    f = s.top()
    eq((f["name"], f["line"]), ("area", 2), "after step in")
    ls = names(s.locals())
    check(set(ls) <= {"w", "h", "a"} and {"w", "h"} <= set(ls), "locals of area: %s" % ls)
    eq(s.value("w"), "0", "w")
    eq(s.value("h"), "2", "h")
    s.step("next")
    f = s.top()
    eq((f["name"], f["line"]), ("area", 3), "after next")
    eq(s.value("a"), "0", "a")
    s.step("stepOut")
    f = s.top()
    eq((f["name"], f["line"]), ("main", 9), "after step out")
    s.d.request("setBreakpoints", {"source": {"path": src}, "breakpoints": []})
    s.cont()
    s.run_to_exit(0)
    check("6" in s.output(), "program output: %r" % s.output())
    s.finish()


def test_breakpoint_in_loop(t):
    src = SAMPLES["area"]
    s = t.session("area").start(lines={src: [3]})
    for i in range(3):
        s.stopped()
        eq((s.top()["name"], s.top()["line"]), ("area", 3), "stop %d" % i)
        eq(s.value("w"), str(i), "w at stop %d" % i)
        eq(s.value("a"), str(2 * i), "a at stop %d" % i)
        s.cont()
    s.run_to_exit(0)
    s.finish()


def test_function_breakpoint(t):
    s = t.session("area").start(functions=["area"])
    eq(len(s.fbps), 1, "function breakpoints")
    check(s.fbps[0]["verified"], "function breakpoint area not verified: %r" % s.fbps)
    s.stopped()
    f = s.top()
    eq(f["name"], "area", "top frame")
    check(f["line"] in (1, 2), "line %s" % f["line"])
    check(f["source"]["path"].endswith("area.goose"), "source")
    eq(s.frames()[1]["name"], "main", "caller")
    s.finish()


def test_function_instances(t):
    src = SAMPLES["twice"]
    s = t.session("twice").start(functions=["twice", "pick"])
    check(all(b["verified"] for b in s.fbps), "not verified: %r" % s.fbps)
    seen = []
    for _ in range(4):
        s.stopped()
        f = s.top()
        check(f["source"]["path"] == src, "source %s" % f["source"].get("path"))
        seen.append(f["name"])
        if f["name"].startswith("twice"):
            # The block's `it` is compiled into its instance of twice.
            check(names(s.locals())[0] == "x", "locals of twice: %s" % names(s.locals()))
        s.cont()
    eq(seen, ["twice<F=inc>", "twice<F={fn line 18}>", "pick<T=i64>", "pick<T=f64>"], "instances")
    s.run_to_exit(0)
    s.finish()


def test_struct_values(t):
    src = SAMPLES["structs"]
    s = t.session("structs").start(lines={src: [14, 24]})
    s.stopped()
    eq(s.top()["line"], 14, "first stop")
    eq(names(s.locals()), ["p", "u"], "locals in scope at line 14")
    s.cont()
    s.stopped()
    eq(s.top()["line"], 24, "second stop")
    ls = s.locals()
    eq(names(ls), ["p", "u", "c", "circle", "rect", "dot", "label", "shapes"], "locals at line 24")
    lv = {v["name"]: v for v in ls}
    eq(lv["p"]["value"], "Point { x: 3, y: -4 }", "p")
    eq(lv["p"]["type"], "Point", "p's type")
    eq(lv["c"]["value"], "Color.Green", "c")
    eq(lv["label"]["value"], 'Mark.Label { text: "hi" }', "label (no address)")
    kids = s.variables(lv["u"]["variablesReference"])
    eq(names(kids), ["kind", "hp", "pos", "tag"], "u's children")
    pos = [k for k in kids if k["name"] == "pos"][0]
    eq(pos["evaluateName"], "u.pos", "u.pos evaluateName")
    eq(s.value(pos["evaluateName"]), "Point { x: 3, y: -4 }", "evaluate u.pos")
    xs = s.variables(pos["variablesReference"])
    eq([(k["name"], k["value"], k["evaluateName"]) for k in xs],
       [("x", "3", "u.pos.x"), ("y", "-4", "u.pos.y")], "u.pos's children")
    eq(s.value("u.pos.x"), "3", "u.pos.x")
    eq(s.value("p.y"), "-4", "p.y")
    eq(s.value("u.hp"), "99", "u.hp")
    # lldb-dap: "(int32_t) $0 = 3"; lldb-vscode 16: the value alone.
    r = s.value("u.pos.x", "repl")
    check(r == "3" or r.endswith("= 3"), "repl u.pos.x: %r" % r)
    eq(s.value("shapes[1]"), "Shape.Rect { w: 2.0, h: 3.0 }", "shapes[1]")
    eq(s.value("circle.r"), "1.5", "circle.r")
    eq(s.value("label.text"), '"hi"', "label.text")
    eq(s.value("u_g.pos_g.x_g"), "3", "a C path still works")
    sh = s.variables(lv["shapes"]["variablesReference"])
    eq([(k["name"], k["evaluateName"]) for k in sh], [("[0]", "shapes[0]"), ("[1]", "shapes[1]")],
       "shapes' children")
    s.finish()


def test_resizables(t):
    src = SAMPLES["resizable"]
    s = t.session("resizable").start(lines={src: [12]})
    s.stopped()
    lv = {v["name"]: v for v in s.locals()}
    # The map gives `for i` the rest of the function as its scope.
    eq(sorted(lv), ["i", "nums", "s", "text"], "locals")
    eq(lv["nums"]["value"], "[0, 3, 6, 9]", "nums")
    eq(lv["nums"]["type"], "u32[>..]", "nums' type")
    eq(lv["text"]["value"], '"grow 42"', "text")
    el = s.variables(lv["nums"]["variablesReference"])
    eq([(k["name"], k["value"]) for k in el], [("[0]", "0"), ("[1]", "3"), ("[2]", "6"), ("[3]", "9")],
       "nums' elements")
    eq(s.value("nums[2]"), "6", "nums[2]")
    eq(s.value("s.items"), "[7, 9]", "s.items")
    eq(s.value("s.items[1]"), "9", "s.items[1]")
    eq(s.value("s.name"), '"main"', "s.name")
    s.finish()


def test_globals(t):
    src = SAMPLES["layouts"]
    s = t.session("layouts").start(lines={src: [25]})
    s.stopped()
    sc = s.scopes()
    check("Globals" in sc, "no Globals scope")
    gv = s.variables(sc["Globals"]["variablesReference"])
    lv = {v["name"]: v for v in gv}
    eq(sorted(lv), ["log", "pool"], "globals")
    check(lv["pool"]["value"].startswith("[PNode { v: 1"), "pool: %r" % lv["pool"]["value"])
    eq(lv["log"]["value"], '"x"', "log")
    eq(lv["pool"]["type"], "PNode[>..]", "pool's type")
    ls = {v["name"]: v for v in s.locals()}
    eq(ls["cur"]["value"], "Cursor { line: 1, flag: true }", "cur")
    eq(s.value("cur.flag"), "true", "cur.flag")
    eq(s.value("second.v"), "2", "second.v (through a reference)")
    eq(s.value("r.tag"), '"ab"', "r.tag (a variable-size struct)")
    check(s.value("pool").startswith("len=2") or s.value("pool").startswith("[PNode"), "pool evaluates")
    s.finish()


def test_breakpoint_locations(t):
    src = SAMPLES["area"]
    s = t.session("area").start()
    r = s.d.request("breakpointLocations", {"source": {"path": src}, "line": 1, "endLine": 12})
    lines = sorted(b["line"] for b in r["body"]["breakpoints"])
    # 12 is main's `}`, where falling off the end returns; area's (4) is not
    # reached past its `return`.
    check({2, 3, 7, 8, 9, 11, 12} <= set(lines), "lines: %s" % lines)
    check(not {4, 5, 10} & set(lines), "lines without code: %s" % lines)
    s.run_to_exit(0)
    s.finish()


def runtime_error_stop(s, want_text, fn, line, path_end):
    st = s.stopped()
    eq(st["reason"], "exception", "stop reason")
    check(want_text in (st.get("text") or ""), "stop text %r" % st.get("text"))
    check(want_text in (st.get("description") or ""), "description %r" % st.get("description"))
    fs = s.frames()
    assert_goose_stack(fs)
    eq((fs[0]["name"], fs[0]["line"]), (fn, line), "top frame")
    check(fs[0]["source"]["path"].endswith(path_end), "top source %s" % fs[0]["source"]["path"])
    r = s.d.request("exceptionInfo", {"threadId": s.tid})
    check(r["success"] and want_text in r["body"]["description"], "exceptionInfo %r" % r)
    return fs


def test_runtime_error_assert(t):
    s = t.session("assert").start()
    fs = runtime_error_stop(s, "assert failed", "geo::area", 5, "geo.goose")
    eq(fs[1]["name"], "total", "caller")
    eq(s.value("w"), "-1", "w where the assert failed")
    s.cont()
    s.run_to_exit(1)
    s.finish()


def test_runtime_error_bounds(t):
    s = t.session("bounds").start()
    runtime_error_stop(s, "index 3 out of bounds (length 3)", "get", 4, "bounds.goose")
    # A slice parameter arrives split and is rebuilt as a C local, so it
    # comes after i in the C's order.
    eq(sorted(names(s.locals())), ["i", "xs"], "locals")
    eq(s.value("i"), "3", "i")
    eq(s.value("xs"), "[1, 2, 3]", "xs")
    s.cont()
    s.run_to_exit(1)
    check("out of bounds" in s.output(), "the runtime's message: %r" % s.output())
    s.finish()


def test_runtime_error_filter_off(t):
    s = t.session("bounds").start(exceptions=())
    s.run_to_exit(1)
    s.finish()


def test_runtime_error_default(t):
    # A client that sends no setExceptionBreakpoints (IntelliJ's DAP client
    # without an exception breakpoint type) still stops.
    s = t.session("bounds").start(exceptions=None)
    runtime_error_stop(s, "out of bounds", "get", 4, "bounds.goose")
    s.finish()


def test_build_first(t):
    src = SAMPLES["area"]
    prog = os.path.join(t.work, "area-built" + EXE)
    if os.path.exists(prog):
        os.unlink(prog)
    s = Session(t, prog, build=True, goose=compiler(), source=src)
    t.sessions.append(s)
    s.start(lines={src: [9]})
    s.stopped()
    eq(s.top()["name"], "main", "top frame")
    out = "".join(m["body"].get("output", "") for m in s.d.events("output"))
    check("built" in out, "build output: %r" % out)
    s.finish()


def test_build_failure(t):
    bad = os.path.join(t.work, "bad.goose")
    with open(bad, "w") as f:
        f.write("fn main() {\n    let x = ;\n}\n")
    s = Session(t, os.path.join(t.work, "bad" + EXE), build=True, goose=compiler(), source=bad)
    t.sessions.append(s)
    d = s.d
    d.request("initialize", {"clientID": "t", "adapterID": "goose", "pathFormat": "path"})
    r = d.request("launch", s.launch_args, timeout=120)
    check(not r["success"] and "goose build failed" in r.get("message", ""), "launch: %r" % r)
    s.finish()


def test_set_variable(t):
    src = SAMPLES["area"]
    s = t.session("area").start(lines={src: [11]})
    s.stopped()
    ref = s.scopes()["Locals"]["variablesReference"]
    s.variables(ref)   # Not s.locals(): lldb-dap 23 retires ref at the next scopes.
    r = s.d.request("setVariable", {"variablesReference": ref, "name": "total", "value": "41"})
    check(r["success"], "setVariable: %r" % r)
    eq(r["body"]["value"], "41", "new value")
    eq(s.value("total"), "41", "total after setVariable")
    s.d.request("setBreakpoints", {"source": {"path": src}, "breakpoints": []})
    s.cont()
    s.run_to_exit(0)
    check("41" in s.output(), "output %r" % s.output())
    s.finish()


def test_stop_on_entry(t):
    s = t.session("area", stopOnEntry=True).start()
    st = s.stopped()
    eq(st["reason"], "entry", "stop reason")
    fs = s.frames()
    check(fs and all(f.get("presentationHint") == "subtle" for f in fs), "entry frames: %r" % fs)
    s.cont()
    s.run_to_exit(0)
    s.finish()


def test_client_vanishes(t):
    # An editor that dies mid-session: goose-dap sees EOF on stdin and must
    # take lldb-dap and the debuggee down with it.
    src = SAMPLES["area"]
    s = t.session("area").start(lines={src: [9]})
    s.stopped()
    tree = sorted(set(s.tree + descendants(s.d.p.pid)))
    check(len(tree) >= 1, "no lldb-dap below goose-dap")
    rc = s.d.close(timeout=20)
    end = time.time() + 10
    left = tree
    while time.time() < end and left:
        left = [p for p in tree if alive(p)]
        time.sleep(0.1)
    for p in left:
        kill_pid(p)
    check(not left, "processes left running: %s" % left)
    eq(rc, 0, "goose-dap exit status")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
ORDER = ["capabilities", "line_breakpoint_and_steps", "breakpoint_in_loop", "function_breakpoint",
         "function_instances", "struct_values", "resizables", "globals", "breakpoint_locations",
         "runtime_error_assert", "runtime_error_bounds", "runtime_error_filter_off",
         "runtime_error_default", "build_first", "build_failure", "set_variable", "stop_on_entry", "client_vanishes"]
TESTS.sort(key=lambda f: ORDER.index(f.__name__[5:]) if f.__name__[5:] in ORDER else 99)


def build_all(work):
    g = compiler()
    bins = {}
    for name, src in SAMPLES.items():
        out = os.path.join(work, name + EXE)
        p = subprocess.run([g, "build", "-g", "-o", out, src], capture_output=True, text=True,
                           cwd=os.path.dirname(src))
        if p.returncode != 0:
            raise SystemExit("cannot build %s with %s:\n%s%s" % (src, g, p.stdout, p.stderr))
        bins[name] = out
    return bins


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[], help="run tests whose name contains this")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    a = ap.parse_args()
    VERBOSE = a.v
    work = os.environ.get("GOOSE_DAP_WORK")
    if work:
        os.makedirs(work, exist_ok=True)
    else:
        work = tempfile.mkdtemp(prefix="goose-dap-test-")
    bins = build_all(work)
    tests = [f for f in TESTS if not a.k or any(k in f.__name__ for k in a.k)]
    failed = []
    for f in tests:
        name = f.__name__[5:]
        t = T(name, work, bins)
        t0 = time.time()
        try:
            f(t)
            print("ok    %-28s %.1fs" % (name, time.time() - t0))
        except Exception as e:
            failed.append(name)
            print("FAIL  %-28s %s" % (name, e))
            if a.v or not isinstance(e, Fail):
                traceback.print_exc()
            for s in t.sessions:
                print("      log: %s" % s.logfile)
                s.kill()
    print("%d passed, %d failed%s" % (len(tests) - len(failed), len(failed),
                                      (": " + ", ".join(failed)) if failed else ""))
    if not failed and not a.keep and not os.environ.get("GOOSE_DAP_WORK"):
        shutil.rmtree(work, ignore_errors=True)
    else:
        print("work directory: %s" % work)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
