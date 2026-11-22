#!/usr/bin/env python3
"""goose-dap: a Debug Adapter Protocol proxy that turns lldb-dap's C-level
view of a Goose program into a Goose-level one.

    editor  <--DAP/stdio-->  goose_dap.py  <--DAP/stdio-->  lldb-dap

It speaks DAP on its own stdin/stdout (Content-Length framing, binary, no
TTY assumed) and runs lldb-dap as a child over pipes. Most messages pass
through unchanged; the ones below are rewritten with the program's debug
map (<stem>.debug.json, written by `goose build -g` beside the generated C):

  initialize      a "Goose runtime errors" exception filter in place of
                  lldb-dap's C++/ObjC/Swift ones; completions and cancel
                  are not offered (they would need translating)
  launch/attach   goose_lldb.py and goose_dap_lldb.py loaded through
                  initCommands, step-in avoiding gs_* runtime functions,
                  and optionally `goose build -g` first
  stackTrace      Goose function names; gs_* runtime frames dropped; the C
                  main subtle, its generated .c source deemphasized
  scopes          Locals and Globals of a Goose frame are rewritten
  variables       Goose names, only the locals in scope at the frame's line,
                  no temporaries; Goose types; evaluateName in Goose form
  evaluate        Goose identifiers and field paths to C (u.pos.x ->
                  u_g.pos_g.x_g); repl commands pass through
  setVariable     a Goose name back to the C one
  setFunctionBreakpoints   a Goose name to every C instance of it
  breakpointLocations      answered from LLDB's line table when lldb-dap
                  returns nothing
  setExceptionBreakpoints  the Goose filter is a function breakpoint on
                  gs_fail and gs_panic; a stop there is reported as an
                  exception with the runtime's message (exceptionInfo too)

Python 3 standard library only. Set GOOSE_DAP_LOG=FILE to log every message.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.realpath(__file__))
GOOSE_LLDB = os.path.normpath(os.path.join(HERE, "..", "lldb", "goose_lldb.py"))
HELPER = os.path.join(HERE, "goose_dap_lldb.py")
DEFAULT_LLDB_DAP = "/Library/Developer/CommandLineTools/usr/bin/lldb-dap"

# ---- Platform: where the compiler keeps its builds, and how an executable
# and a path are spelled (Windows: .exe, backslashes, a drive letter, a
# case-blind file system).
WINDOWS = os.name == "nt"
EXE_SUFFIX = ".exe" if WINDOWS else ""


def goose_home():
    """GOOSE_HOME, else ~/.goose, as the compiler has it (toolchain.h)."""
    return os.environ.get("GOOSE_HOME") or os.path.join(os.path.expanduser("~"), ".goose")


def _slashed(path):
    return path.replace("\\", "/").lower() if WINDOWS else path


def in_runtime_build(path):
    """Whether path is in the compiler's runtime builds: GOOSE_HOME/build/runtime/."""
    p = _slashed(path)
    root = _slashed(goose_home()).rstrip("/") + "/build/runtime/"
    return p.startswith(root) or "/.goose/build/runtime/" in p

GOOSE_FILTER = "goose_runtime"
RUNTIME_ERROR_FUNCS = ("gs_fail", "gs_panic")
INTERNAL_TIMEOUT = 15.0

# Requests that need a capability; one lldb-dap did not announce is
# answered here rather than forwarded (see on_client).
GATED_REQUESTS = {
    "stepInTargets": "supportsStepInTargetsRequest",
    "gotoTargets": "supportsGotoTargetsRequest",
    "goto": "supportsGotoTargetsRequest",
    "restart": "supportsRestartRequest",
    "restartFrame": "supportsRestartFrame",
    "loadedSources": "supportsLoadedSourcesRequest",
    "readMemory": "supportsReadMemoryRequest",
    "writeMemory": "supportsWriteMemoryRequest",
    "disassemble": "supportsDisassembleRequest",
    "setExpression": "supportsSetExpression",
    "dataBreakpointInfo": "supportsDataBreakpoints",
    "setDataBreakpoints": "supportsDataBreakpoints",
    "setInstructionBreakpoints": "supportsInstructionBreakpoints",
    "modules": "supportsModulesRequest",
    "terminateThreads": "supportsTerminateThreadsRequest",
    "stepBack": "supportsStepBack",
    "reverseContinue": "supportsStepBack",
}

# A Goose local, field or function as C spells it: x_g, it_g_2, bump_g_1.
_NAME_RE = re.compile(r"^(.+?)_g(_?[0-9]+)?$")
_INDEX_RE = re.compile(r"^\[[0-9]+\]$")
_ADDR_PREFIX_RE = re.compile(r"^0x[0-9a-fA-F]+ (?!<)(.+)$", re.S)
_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<num>(?:0[xX][0-9a-fA-F_]+|[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9]+)?)[A-Za-z0-9_]*)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>->|::|\.\.|.)
""", re.X | re.S)
_KEYWORDS = {"true", "false", "null", "sizeof", "as", "and", "or", "not"}


def goose_name(cname):
    m = _NAME_RE.match(cname or "")
    return m.group(1) if m else cname


# --------------------------------------------------------------------------
# Logging


class Log:
    def __init__(self, path):
        self.f = None
        self.lock = threading.Lock()
        if path:
            try:
                self.f = open(path, "a", buffering=1)
            except OSError:
                self.f = None

    def __call__(self, *parts):
        if not self.f:
            return
        with self.lock:
            try:
                self.f.write("%.3f %s\n" % (time.time(), " ".join(str(p) for p in parts)))
            except Exception:
                pass


# --------------------------------------------------------------------------
# DAP framing


def read_message(f):
    """One DAP message from a binary stream, or None at EOF."""
    length = None
    while True:
        line = f.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is None:
                continue
            break
        k, _, v = line.decode("ascii", "replace").partition(":")
        if k.strip().lower() == "content-length":
            try:
                length = int(v.strip())
            except ValueError:
                length = None
    data = b""
    while len(data) < length:
        chunk = f.read(length - len(data))
        if not chunk:
            return None
        data += chunk
    return json.loads(data.decode("utf-8"))


def frame_bytes(msg):
    b = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(b) + b


# --------------------------------------------------------------------------
# The debug map


class GooseMap:
    """The functions, locals, types and globals of one program's maps.
    Unknown keys are ignored; anything malformed is skipped."""

    def __init__(self, docs=()):
        self.funcs = {}          # C name -> function
        self.by_name = {}        # Goose name -> [function]
        self.types = {}          # Goose name -> type
        self.types_by_c = {}
        self.globals = {}        # Goose name -> {c, name, type, path}
        self.globals_by_c = {}
        self.globals_block = "gs_globals_main"
        self.globals_thread = None   # "gs_gl" when the program starts threads
        self.paths = []
        for d in docs:
            self._add(d)

    def _add(self, d):
        if not isinstance(d, dict):
            return
        for f in d.get("functions") or []:
            if not isinstance(f, dict) or not isinstance(f.get("c"), str):
                continue
            if not isinstance(f.get("locals"), list):
                f["locals"] = []
            f["locals"] = [l for l in f["locals"] if isinstance(l, dict)
                           and isinstance(l.get("c"), str) and isinstance(l.get("name"), str)]
            self.funcs.setdefault(f["c"], f)
            name = f.get("name")
            if isinstance(name, str):
                self.by_name.setdefault(name, []).append(f)
        for t in d.get("types") or []:
            if not isinstance(t, dict):
                continue
            if isinstance(t.get("name"), str):
                self.types.setdefault(t["name"], t)
            if isinstance(t.get("c"), str) and t["c"]:
                self.types_by_c.setdefault(t["c"], t)
        g = d.get("globals")
        if isinstance(g, dict):
            block = g.get("main") if isinstance(g.get("main"), str) else "gs_globals_main"
            self.globals_block = block
            if isinstance(g.get("thread"), str) and g["thread"]:
                self.globals_thread = g["thread"]
            vs = g.get("vars") or []
        elif isinstance(g, list):
            block, vs = "gs_globals_main", g
        else:
            block, vs = None, []
        for v in vs:
            if not isinstance(v, dict) or not isinstance(v.get("c"), str) or not isinstance(v.get("name"), str):
                continue
            e = dict(v)
            e["path"] = "%s.%s" % (block, v["c"]) if block else v["c"]
            self.globals.setdefault(v["name"], e)
            self.globals_by_c.setdefault(v["c"], e)

    def __bool__(self):
        return bool(self.funcs)

    def functions_named(self, name):
        """C names of every instance of a Goose function: `area`, a
        qualified `geo::area`, or the last part of one."""
        fs = self.by_name.get(name)
        if not fs:
            fs = [f for n, l in self.by_name.items() if n.split("::")[-1] == name for f in l]
        return [f["c"] for f in fs]

    def locals_at(self, f, line):
        """The locals of function f in scope at a line, by Goose name; of two
        with one name (shadowing), the later-declared one."""
        out = {}
        for l in f.get("locals", []):
            try:
                if line is not None and not (int(l.get("line", 0)) <= line <= int(l.get("end", 1 << 30))):
                    continue
            except (TypeError, ValueError):
                continue
            prev = out.get(l["name"])
            if prev is None or int(prev.get("line", 0)) <= int(l.get("line", 0)):
                out[l["name"]] = l
        return out

    def local_named(self, f, name, line):
        l = self.locals_at(f, line).get(name)
        if l is not None:
            return l
        best = None
        for x in f.get("locals", []):
            if x["name"] == name and (best is None or x.get("line", 0) > best.get("line", 0)):
                best = x
        return best

    def fields_of(self, gtype):
        """The fields of a Goose type, given as type text: a struct, a variant
        (Shape.Circle), or None."""
        if not gtype:
            return None
        t = strip_ref(gtype)
        td = self.types.get(t)
        if td is not None:
            if td.get("kind") == "struct":
                return td.get("fields") or []
            return None
        if "." in t and not t.endswith(".."):
            base, _, variant = t.rpartition(".")
            td = self.types.get(base)
            if td is not None:
                for v in td.get("variants") or []:
                    if v.get("name") == variant:
                        return v.get("fields") or []
        return None

    def enum_variants(self, gtype):
        t = strip_ref(gtype or "")
        if t.endswith(".."):
            t = t[:-2]
        td = self.types.get(t)
        if td is not None and td.get("kind") == "enum":
            return td.get("variants") or []
        return None


def strip_ref(t):
    """Pair<i64>& -> Pair<i64>; Node? -> Node; PNode&<u32 in pool>? -> PNode."""
    t = (t or "").strip()
    if t.startswith("const "):
        t = t[6:]
    changed = True
    while changed:
        changed = False
        if t.endswith("?") or t.endswith("&"):
            t = t[:-1]
            changed = True
        elif t.endswith(">"):
            i = _open_index(t, len(t) - 1, "<", ">")
            if i > 0 and t[i - 1] == "&":
                t = t[:i - 1]
                changed = True
    return t


def is_pointer_type(t):
    t = (t or "").strip()
    return t.endswith("?") or t.endswith("&") or (t.endswith(">") and "&<" in t)


def _open_index(s, close, op, cl):
    depth = 0
    for i in range(close, -1, -1):
        if s[i] == cl:
            depth += 1
        elif s[i] == op:
            depth -= 1
            if depth == 0:
                return i
    return -1


def element_type(t):
    """i64[>..] -> i64; u8[][>..] -> u8[]; None if not an array."""
    t = strip_ref(t)
    if t.endswith("]"):
        i = _open_index(t, len(t) - 1, "[", "]")
        if i > 0:
            return t[:i]
    return None


# --------------------------------------------------------------------------
# The proxy


class Proxy:
    def __init__(self, lldb_dap=None, log=None):
        self.log = log or Log(None)
        self.lldb_dap_path = lldb_dap
        self.out = sys.stdout.buffer
        self.inp = sys.stdin.buffer
        self.out_lock = threading.Lock()
        self.child = None
        self.child_lock = threading.Lock()
        self.q = queue.Queue()
        self.seq_lock = threading.Lock()
        self.aseq = 0                 # seqs of messages sent to lldb-dap
        self.cseq = 0                 # seqs of messages sent to the client
        self.pending = {}             # adapter seq -> (client request, context)
        self.waiters = {}             # adapter seq -> [Event, response]
        self.reverse = {}             # client-facing seq -> adapter seq (reverse requests)
        self.gmap = GooseMap()
        self.map_loaded = False
        self.escape = "`"
        self.init_args = None
        self.adapter_caps = {}        # lldb-dap's own capabilities
        self.launch_args = {}
        self.show_runtime_frames = False
        self.stop_on_runtime_error = True
        self.saw_exception_bps = False
        self.client_fbps = []         # the client's function breakpoints
        self.goose_filter = False
        self.goose_filter_condition = None
        self.fbp_secondary = {}       # lldb-dap breakpoint id -> client-facing id
        self.runtime_bp_ids = set()
        self.line_cache = {}
        self.globals_block = []
        self.terminated_sent = False
        self.disconnecting = False
        self.client_gone = False
        self.reset_stop_state()

    def reset_stop_state(self):
        self.frames = {}              # frame id -> info
        self.thread_frames = {}       # thread id -> filtered frames
        self.refs = {}                # variablesReference -> context
        self.exc = getattr(self, "exc", {})

    # ---- I/O

    def send_client(self, msg):
        with self.out_lock:
            self.cseq += 1
            msg["seq"] = self.cseq
            self.log("<-C", json.dumps(msg)[:4000])
            try:
                self.out.write(frame_bytes(msg))
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.client_gone = True

    def send_adapter(self, msg):
        with self.seq_lock:
            self.aseq += 1
            msg["seq"] = self.aseq
        self.log("->A", json.dumps(msg)[:4000])
        with self.child_lock:
            try:
                self.child.stdin.write(frame_bytes(msg))
                self.child.stdin.flush()
            except (BrokenPipeError, OSError, ValueError, AttributeError):
                return None
        return msg["seq"]

    def adapter_request(self, command, arguments=None, timeout=INTERNAL_TIMEOUT):
        """A request of the proxy's own to lldb-dap; its response, or None."""
        ev = threading.Event()
        box = [ev, None]
        msg = {"type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        # The waiter must exist before the response can arrive.
        with self.seq_lock:
            self.aseq += 1
            seq = self.aseq
            self.waiters[seq] = box
        msg["seq"] = seq
        self.log("->A*", json.dumps(msg)[:4000])
        with self.child_lock:
            try:
                self.child.stdin.write(frame_bytes(msg))
                self.child.stdin.flush()
            except (BrokenPipeError, OSError, ValueError, AttributeError):
                self.waiters.pop(seq, None)
                return None
        ev.wait(timeout)
        with self.seq_lock:
            self.waiters.pop(seq, None)
        return box[1]

    def respond(self, req, body=None, success=True, message=None):
        r = {"type": "response", "request_seq": req.get("seq"), "command": req.get("command"),
             "success": success}
        if body is not None:
            r["body"] = body
        if message is not None:
            r["message"] = message
        self.send_client(r)

    def event(self, name, body=None):
        e = {"type": "event", "event": name}
        if body is not None:
            e["body"] = body
        self.send_client(e)

    def console(self, text, category="console"):
        self.event("output", {"category": category, "output": text})

    def forward(self, req, args=None, ctx=None):
        """Send a client request on to lldb-dap; its response comes back
        through on_response with ctx."""
        msg = {"type": "request", "command": req["command"]}
        a = req.get("arguments") if args is None else args
        if a is not None:
            msg["arguments"] = a
        with self.seq_lock:
            self.aseq += 1
            seq = self.aseq
            self.pending[seq] = (req, ctx)
        msg["seq"] = seq
        self.log("->A", json.dumps(msg)[:4000])
        with self.child_lock:
            try:
                self.child.stdin.write(frame_bytes(msg))
                self.child.stdin.flush()
            except (BrokenPipeError, OSError, ValueError, AttributeError):
                self.pending.pop(seq, None)
                self.respond(req, success=False, message="lldb-dap is not running")

    # ---- the child

    def find_lldb_dap(self, override=None):
        for c in (override, self.lldb_dap_path, os.environ.get("GOOSE_DAP_LLDB_DAP"),
                  os.environ.get("LLDB_DAP")):
            if c:
                return c
        try:
            p = subprocess.run(["xcrun", "-f", "lldb-dap"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip() and os.path.exists(p.stdout.strip()):
                return p.stdout.strip()
        except Exception:
            pass
        if os.path.exists(DEFAULT_LLDB_DAP):
            return DEFAULT_LLDB_DAP
        # LLVM 18 renamed lldb-vscode to lldb-dap; Debian and Ubuntu install
        # versioned names (lldb-dap-18, lldb-vscode-16) on PATH and the
        # plain ones in /usr/lib/llvm-NN/bin.
        names = ["lldb-dap"] + ["lldb-dap-%d" % v for v in range(30, 17, -1)] + \
                ["lldb-vscode"] + ["lldb-vscode-%d" % v for v in range(17, 15, -1)]
        for n in names:
            p = shutil.which(n)
            if p:
                return p
        for v in range(30, 15, -1):
            for n in ("lldb-dap", "lldb-vscode"):
                p = "/usr/lib/llvm-%d/bin/%s" % (v, n)
                if os.access(p, os.X_OK):
                    return p
        return "lldb-dap"

    def start_child(self, path):
        self.log("spawn", path)
        self.child_path = path
        self.child = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, close_fds=True)
        child = self.child
        threading.Thread(target=self.adapter_reader, args=(child,), daemon=True).start()
        threading.Thread(target=self.adapter_stderr, args=(child,), daemon=True).start()

    def stop_child(self, child, grace=3.0):
        if child is None:
            return
        try:
            child.stdin.close()
        except Exception:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                child.terminate()
                child.wait(timeout=2)
            except Exception:
                try:
                    child.kill()
                    child.wait(timeout=2)
                except Exception:
                    pass

    def adapter_stderr(self, child):
        for line in iter(child.stderr.readline, b""):
            self.log("lldb-dap stderr:", line.decode("utf-8", "replace").rstrip())

    def adapter_reader(self, child):
        while True:
            try:
                msg = read_message(child.stdout)
            except Exception as e:
                self.log("adapter read error", e)
                msg = None
            if msg is None:
                if child is self.child:
                    self.q.put(("adapter_eof", None))
                return
            if child is not self.child:
                continue
            self.log("<-A", json.dumps(msg)[:4000])
            t = msg.get("type")
            if t == "response":
                with self.seq_lock:
                    box = self.waiters.get(msg.get("request_seq"))
                if box is not None:
                    box[1] = msg
                    box[0].set()
                    continue
            elif t == "request":
                # A reverse request (runInTerminal, startDebugging): straight
                # to the client, so a request of ours never waits behind it.
                self.forward_reverse(msg)
                continue
            self.q.put(("adapter", msg))

    def forward_reverse(self, msg):
        aseq = msg.get("seq")
        m = dict(msg)
        with self.out_lock:
            self.cseq += 1
            m["seq"] = self.cseq
            self.reverse[m["seq"]] = aseq
            try:
                self.out.write(frame_bytes(m))
                self.out.flush()
            except Exception:
                pass

    def client_reader(self):
        while True:
            try:
                msg = read_message(self.inp)
            except Exception as e:
                self.log("client read error", e)
                msg = None
            if msg is None:
                self.q.put(("client_eof", None))
                return
            self.log("C->", json.dumps(msg)[:4000])
            if msg.get("type") == "response":
                # The client's answer to a reverse request.
                aseq = self.reverse.pop(msg.get("request_seq"), msg.get("request_seq"))
                m = dict(msg)
                m["request_seq"] = aseq
                self.send_adapter(m)
                continue
            self.q.put(("client", msg))

    # ---- main loop

    def run(self):
        threading.Thread(target=self.client_reader, daemon=True).start()
        while True:
            kind, msg = self.q.get()
            if kind == "client_eof":
                self.shutdown()
                return 0
            if kind == "adapter_eof":
                self.log("lldb-dap exited")
                if not self.terminated_sent and not self.client_gone:
                    self.terminated_sent = True
                    self.event("terminated")
                # Answer whatever lldb-dap left unanswered.
                for seq, (req, ctx) in list(self.pending.items()):
                    self.pending.pop(seq, None)
                    if req.get("command") == "disconnect":
                        self.respond(req)
                    else:
                        self.respond(req, success=False, message="lldb-dap exited")
                self.child = None
                continue
            try:
                if kind == "client":
                    self.on_client(msg)
                else:
                    self.on_adapter(msg)
            except Exception:
                self.log("error:", traceback.format_exc())
                if kind == "client" and msg.get("type") == "request":
                    # Fall back to passing the request through untouched.
                    try:
                        self.forward(msg)
                    except Exception:
                        self.respond(msg, success=False, message="goose-dap internal error")
                elif kind == "adapter":
                    self.pass_adapter(msg)

    def shutdown(self):
        child = self.child
        if child is not None and child.poll() is None and not self.disconnecting:
            self.adapter_request("disconnect", {"terminateDebuggee": True}, timeout=5)
        self.stop_child(child)

    # ---- from the client

    def on_client(self, req):
        if req.get("type") != "request":
            return
        cmd = req.get("command")
        h = getattr(self, "c_" + cmd, None) if isinstance(cmd, str) else None
        if cmd != "initialize" and self.child is None:
            if cmd == "disconnect":
                self.respond(req)
                return
            self.respond(req, success=False, message="lldb-dap is not running")
            return
        cap = GATED_REQUESTS.get(cmd)
        if h is None and cap and not self.adapter_caps.get(cap):
            # lldb-vscode and lldb-dap 18 exit on a request they have no
            # handler for, taking the debuggee with them.
            self.respond(req, success=False, message="%s is not supported by this lldb-dap" % cmd)
            return
        if h is None:
            if cmd in ("continue", "next", "stepIn", "stepOut", "stepBack", "reverseContinue",
                       "goto", "restartFrame"):
                self.reset_stop_state()
                self.exc.clear()
            self.forward(req)
            return
        h(req)

    def c_initialize(self, req):
        self.init_args = req.get("arguments") or {}
        if self.child is None:
            self.start_child(self.find_lldb_dap())
        self.forward(req, ctx={"kind": "initialize"})

    def c_launch(self, req):
        self.c_launch_or_attach(req)

    def c_attach(self, req):
        self.c_launch_or_attach(req)

    def c_launch_or_attach(self, req):
        args = dict(req.get("arguments") or {})
        self.launch_args = args
        self.show_runtime_frames = bool(args.pop("showRuntimeFrames", False))
        self.stop_on_runtime_error = bool(args.pop("stopOnRuntimeError", True))
        if isinstance(args.get("commandEscapePrefix"), str):
            self.escape = args["commandEscapePrefix"]
        override = args.pop("lldbDap", None)
        goose = args.pop("goose", None)
        source = args.pop("source", None)
        build = args.pop("build", False)
        build_args = args.pop("buildArgs", None)
        goose_lldb = args.pop("gooseLldb", None) or os.environ.get("GOOSE_DAP_GOOSE_LLDB") or GOOSE_LLDB
        if override and override != getattr(self, "child_path", None):
            if not self.respawn(override):
                self.respond(req, success=False, message="cannot start lldb-dap at %s" % override)
                return
        if source and not args.get("program"):
            base = os.path.splitext(os.path.basename(source))[0]
            args["program"] = os.path.join(args.get("cwd") or os.path.dirname(os.path.abspath(source)),
                                           base + EXE_SUFFIX)
        if build:
            ok, message = self.build(goose, source, args.get("program"), build_args, args.get("cwd"))
            if not ok:
                self.respond(req, success=False, message=message)
                return
        init = ["command script import %s" % json.dumps(goose_lldb),
                "command script import %s" % json.dumps(HELPER),
                # Step in goes over the runtime's helpers (checked arithmetic,
                # pushes) rather than into goose_runtime.c.
                "settings set target.process.thread.step-avoid-regexp ^(std::|gs_)"]
        user = args.get("initCommands")
        if isinstance(user, list):
            init += user
        args["initCommands"] = init
        self.forward(req, args=args, ctx={"kind": "launch"})

    def respawn(self, path):
        old = self.child
        self.child = None
        self.stop_child(old, grace=1.0)
        try:
            self.start_child(path)
        except OSError as e:
            self.log("respawn failed", e)
            return False
        r = self.adapter_request("initialize", self.init_args or {})
        if r is None or not r.get("success"):
            return False
        self.adapter_caps = dict(r.get("body") or {})
        caps = self.rewrite_capabilities(dict(r.get("body") or {}))
        self.event("capabilities", {"capabilities": caps})
        return True

    def build(self, goose, source, program, extra, cwd):
        goose = goose or os.environ.get("GOOSE") or shutil.which("goose") or "goose"
        if not source:
            return False, "build: true needs a \"source\" .goose file"
        if not program:
            return False, "build: true needs a \"program\" path"
        cmd = [goose, "build", "-g"] + (list(extra) if isinstance(extra, list) else []) + \
              ["-o", program, source]
        self.console("goose-dap: %s\n" % " ".join(cmd))
        try:
            p = subprocess.run(cmd, cwd=cwd or None, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "goose build failed: %s" % e
        out = p.stdout.decode("utf-8", "replace")
        if out:
            self.console(out if out.endswith("\n") else out + "\n")
        if p.returncode != 0:
            return False, "goose build failed (exit %d)%s" % (p.returncode, (": " + out.strip().splitlines()[-1]) if out.strip() else "")
        return True, None

    def c_setFunctionBreakpoints(self, req):
        bps = (req.get("arguments") or {}).get("breakpoints") or []
        self.client_fbps = [b for b in bps if isinstance(b, dict) and isinstance(b.get("name"), str)]
        result = self.sync_function_breakpoints()
        if result is None:
            self.respond(req, success=False, message="lldb-dap did not answer setFunctionBreakpoints")
            return
        self.respond(req, {"breakpoints": result})

    def sync_function_breakpoints(self):
        """Sends lldb-dap the whole set: each client breakpoint as every C
        instance of its Goose name, plus gs_fail/gs_panic for the Goose
        filter. Returns one breakpoint per client breakpoint."""
        self.ensure_map()
        out, groups = [], []
        for b in self.client_fbps:
            name = b["name"].strip()
            cnames = self.gmap.functions_named(name) if self.gmap else []
            if not cnames:
                cnames = [name]
            idx = []
            for c in cnames:
                nb = dict(b)
                nb["name"] = c
                idx.append(len(out))
                out.append(nb)
            groups.append(idx)
        rt = []
        if self.goose_filter:
            for f in RUNTIME_ERROR_FUNCS:
                rt.append(len(out))
                e = {"name": f}
                if self.goose_filter_condition:
                    e["condition"] = self.goose_filter_condition
                out.append(e)
        r = self.adapter_request("setFunctionBreakpoints", {"breakpoints": out})
        if r is None or not r.get("success"):
            return None
        got = (r.get("body") or {}).get("breakpoints") or []
        self.runtime_bp_ids = set()
        for i in rt:
            if i < len(got) and got[i].get("id") is not None:
                self.runtime_bp_ids.add(got[i]["id"])
        self.fbp_secondary = {}
        result = []
        for b, idx in zip(self.client_fbps, groups):
            items = [got[i] for i in idx if i < len(got)]
            if not items:
                result.append({"verified": False, "message": "no answer from lldb-dap"})
                continue
            prim = next((x for x in items if x.get("verified")), items[0])
            m = dict(prim)
            for x in items:
                if x is not prim and x.get("id") is not None and prim.get("id") is not None:
                    self.fbp_secondary[x["id"]] = prim["id"]
            if len(items) > 1:
                n = sum(1 for x in items if x.get("verified"))
                if n:
                    m["message"] = "%d instances" % n
            result.append(m)
        return result

    def c_setExceptionBreakpoints(self, req):
        a = req.get("arguments") or {}
        self.saw_exception_bps = True
        filters = [f for f in (a.get("filters") or []) if isinstance(f, str)]
        opts = [o for o in (a.get("filterOptions") or []) if isinstance(o, dict)]
        on = GOOSE_FILTER in filters
        cond = None
        for o in opts:
            if o.get("filterId") == GOOSE_FILTER:
                on = True
                cond = o.get("condition") or None
        self.goose_filter, self.goose_filter_condition = on, cond
        # lldb-dap's own (C++, ObjC, Swift) filters are not offered, so
        # nothing goes to it.
        self.sync_function_breakpoints()
        n = len(filters) + len(opts)
        self.respond(req, {"breakpoints": [{"verified": True} for _ in range(n)]})

    def c_configurationDone(self, req):
        if not self.saw_exception_bps and self.stop_on_runtime_error and not self.goose_filter:
            # A client that sends no setExceptionBreakpoints (IntelliJ's DAP
            # client without an exception breakpoint type) still stops on
            # runtime errors, unless the launch says stopOnRuntimeError: false.
            self.goose_filter = True
            self.sync_function_breakpoints()
        self.forward(req)

    def c_breakpointLocations(self, req):
        if self.adapter_caps.get("supportsBreakpointLocationsRequest"):
            self.forward(req, ctx={"kind": "bplocs"})
            return
        # lldb-vscode and lldb-dap 18 have no breakpointLocations, and a
        # request they do not know ends them: answer from the line table.
        body = {"breakpoints": []}
        self.fill_breakpoint_locations(req, body)
        self.respond(req, body)

    def fill_breakpoint_locations(self, req, body):
        a = req.get("arguments") or {}
        path = (a.get("source") or {}).get("path")
        lines = self.source_lines(path) if path else None
        if lines:
            lo = int(a.get("line") or 1)
            hi = int(a.get("endLine") or lo)
            body["breakpoints"] = [{"line": n} for n in lines if lo <= n <= hi]

    def c_stackTrace(self, req):
        a = req.get("arguments") or {}
        tid = a.get("threadId")
        frames = self.thread_frames.get(tid)
        if frames is None:
            raw = self.fetch_frames(tid)
            if raw is None:
                self.forward(req)
                return
            frames = self.rewrite_frames(tid, raw)
            self.thread_frames[tid] = frames
        start = int(a.get("startFrame") or 0)
        levels = int(a.get("levels") or 0)
        sl = frames[start:start + levels] if levels > 0 else frames[start:]
        self.respond(req, {"stackFrames": sl, "totalFrames": len(frames)})

    def fetch_frames(self, tid):
        raw, start = [], 0
        while start < 4000:
            r = self.adapter_request("stackTrace", {"threadId": tid, "startFrame": start, "levels": 200})
            if r is None or not r.get("success"):
                return None if not raw else raw
            fs = (r.get("body") or {}).get("stackFrames") or []
            raw += fs
            total = (r.get("body") or {}).get("totalFrames")
            if len(fs) < 200 or (isinstance(total, int) and len(raw) >= total):
                break
            start += len(fs)
        return raw

    def is_runtime_frame(self, f):
        name = f.get("name") or ""
        path = (f.get("source") or {}).get("path") or ""
        if name.startswith("gs_"):
            return True
        if os.path.basename(path) == "goose_runtime.c" or in_runtime_build(path):
            return True
        return False

    def rewrite_frames(self, tid, raw):
        out = []
        for f in raw:
            cname = f.get("name") or ""
            fn = self.gmap.funcs.get(cname) if self.gmap else None
            info = {"cname": cname, "line": f.get("line"), "fn": fn, "thread": tid}
            self.frames[f.get("id")] = info
            g = dict(f)
            src = dict(f.get("source") or {}) if f.get("source") else None
            path = (src or {}).get("path") or ""
            if fn is not None:
                b = dict(fn.get("bindings") or {}) if isinstance(fn.get("bindings"), dict) else {}
                if isinstance(fn.get("fnvals"), dict):
                    b.update(fn["fnvals"])
                name = fn.get("name") or goose_name(cname)
                if b:
                    name = "%s<%s>" % (name, ", ".join("%s=%s" % (k, v if isinstance(v, str) else json.dumps(v))
                                                        for k, v in b.items()))
                g["name"] = name
                info["goose"] = True
                if path.endswith(".c") and src is not None:
                    src["presentationHint"] = "deemphasize"
                    g["presentationHint"] = "subtle"
            elif self.is_runtime_frame(f):
                if not self.show_runtime_frames:
                    continue
                g["presentationHint"] = "subtle"
                if src is not None:
                    src["presentationHint"] = "deemphasize"
            elif path.endswith(".goose") and _NAME_RE.match(cname):
                # A Goose frame the map does not describe (no map, or an old one).
                g["name"] = goose_name(cname)
                info["goose"] = True
            else:
                # The C main, libc, dyld: not Goose code.
                g["presentationHint"] = "subtle"
                if cname == "main" and path.endswith(".c"):
                    g["name"] = "main (C)"
                if src is not None:
                    src["presentationHint"] = "deemphasize"
            if src is not None:
                g["source"] = src
            out.append(g)
        if not out:
            return raw
        self.fix_failing_frame(tid, out)
        return out

    def fix_failing_frame(self, tid, frames):
        """At a runtime error, the Goose frame that failed called a noreturn
        runtime function as its last instruction, so its return address can
        fall outside its line table (lldb-dap then shows disassembly). The
        runtime was handed the Goose file and line: use them."""
        e = (self.exc.get(tid) or {}).get("error") or {}
        if not e.get("file") or not e.get("line"):
            return
        for g in frames:
            info = self.frames.get(g.get("id")) or {}
            if not info.get("goose"):
                continue
            fn = info.get("fn") or {}
            path = (g.get("source") or {}).get("path") or ""
            if path.endswith(".goose"):
                return
            gfile = fn.get("file") if isinstance(fn.get("file"), str) else None
            if not gfile or os.path.basename(gfile) != os.path.basename(e["file"]):
                return
            g["source"] = {"name": os.path.basename(gfile), "path": gfile}
            g["line"] = int(e["line"])
            g["column"] = 1
            g.pop("presentationHint", None)
            info["line"] = int(e["line"])
            return

    def c_scopes(self, req):
        fid = (req.get("arguments") or {}).get("frameId")
        info = self.frames.get(fid)
        self.forward(req, ctx={"kind": "scopes", "frame": info})

    def c_variables(self, req):
        a = req.get("arguments") or {}
        ref = a.get("variablesReference")
        ctx = self.refs.get(ref)
        if ctx is not None and ctx.get("kind") == "globals":
            self.goose_globals(req, ctx)
            return
        self.forward(req, ctx={"kind": "variables", "ref": ctx})

    def goose_globals(self, req, ctx):
        """The Globals scope of a Goose frame: the program's Goose globals from
        the map (globals.vars), not the runtime's statics. Values come from
        goose_lldb.py's goose-globals (decoded by Goose type, the selected
        thread's instance); children from the globals block's fields."""
        ref = req["arguments"]["variablesReference"]
        info = ctx.get("frame") or {}
        out, names = [], {}
        kids = {}
        r = self.adapter_request("variables", {"variablesReference": ref})
        top = ((r or {}).get("body") or {}).get("variables") or []
        block = next((v for v in top if v.get("name") == self.gmap.globals_block
                      and v.get("variablesReference")), None)
        if block is not None:
            r = self.adapter_request("variables", {"variablesReference": block["variablesReference"]})
            for v in ((r or {}).get("body") or {}).get("variables") or []:
                kids[v.get("name")] = v
        if self.gmap.globals:
            texts = self.goose_globals_text(info)
            for g in self.gmap.globals.values():
                v = kids.get(g["c"]) or {"variablesReference": 0, "value": "<not in the debug info>"}
                nv = dict(v)
                nv["name"] = g["name"]
                nv["evaluateName"] = g["name"]
                if isinstance(g.get("type"), str):
                    nv["type"] = g["type"]
                nv["value"] = texts.get(g["name"], clean_value(v.get("value"), v.get("type"), g.get("type")))
                nv.pop("declarationLocationReference", None)
                names[g["name"]] = g["c"]
                if v.get("variablesReference"):
                    self.refs[v["variablesReference"]] = {"kind": "child", "prefix": g["name"],
                                                          "gtype": g.get("type")}
                out.append(nv)
        else:
            # A map without globals (an older compiler): the block's fields
            # that are named like Goose variables.
            for c, v in kids.items():
                c = c or ""
                if c.startswith("gs_") or not _NAME_RE.match(c):
                    continue
                out.append(self.goose_var(v, goose_name(c), goose_name(c), None))
                names[goose_name(c)] = c
        self.refs[ref] = dict(ctx, names=names)
        self.respond(req, {"variables": out})

    def goose_globals_text(self, info):
        """{name: value text} from goose-globals, or {}."""
        args = {"expression": "%sgoose-globals" % self.escape, "context": "repl"}
        fid = next((k for k, v in self.frames.items() if v is info), None)
        if fid is not None:
            args["frameId"] = fid
        r = self.adapter_request("evaluate", args)
        if r is None or not r.get("success"):
            return {}
        res = (r.get("body") or {}).get("result") or ""
        out = {}
        for line in res.splitlines():
            for g in self.gmap.globals.values():
                head = "%s: %s = " % (g["name"], g.get("type"))
                if line.startswith(head):
                    out[g["name"]] = line[len(head):]
        return out

    def c_evaluate(self, req):
        a = dict(req.get("arguments") or {})
        expr = a.get("expression")
        ctxname = a.get("context")
        info = self.frames.get(a.get("frameId"))
        # lldb-vscode 16 has no repl auto mode: a command needs the escape
        # prefix there. A plain repl line it cannot parse as an expression
        # is tried again as a command (see rewrite_response).
        retry = None
        if ctxname == "repl" and isinstance(expr, str) and self.escape and \
                not expr.startswith(self.escape):
            retry = dict(a, expression=self.escape + expr)
        if not isinstance(expr, str) or info is None or not info.get("goose"):
            self.forward(req, ctx={"kind": "evaluate", "prefix": None, "context": ctxname,
                                   "retry": retry})
            return
        if ctxname == "repl":
            if self.escape and expr.startswith(self.escape):
                self.forward(req)
                return
            if not self.repl_is_goose(expr, info):
                self.forward(req, ctx={"kind": "evaluate", "prefix": None, "context": ctxname,
                                       "retry": retry})
                return
        cexpr, gtype = self.rewrite_expr(expr, info)
        alt, _ = self.rewrite_expr(expr, info, goose_fields=True)
        a["expression"] = cexpr
        self.forward(req, args=a, ctx={"kind": "evaluate", "prefix": expr.strip(), "gtype": gtype,
                                       "changed": cexpr != expr, "context": ctxname,
                                       "alt": dict(a, expression=alt) if alt != cexpr else None})

    def repl_is_goose(self, expr, info):
        """A repl line is a Goose expression, rather than an LLDB command, if
        it starts with a Goose local or global of the frame and is not
        `word word` (a command and its argument)."""
        toks = [t for t in tokenize(expr) if t[0] != "ws"]
        if not toks or toks[0][0] != "id":
            return False
        if len(toks) > 1 and toks[1][0] in ("id", "num", "str"):
            return False
        if len(toks) > 1 and toks[1][1] == "-" and len(toks) > 2 and toks[2][0] == "id" and \
                re.match(r"\s*\S+\s+-", expr):
            return False
        name = toks[0][1]
        fn = info.get("fn")
        if fn is not None and self.gmap.local_named(fn, name, info.get("line")) is not None:
            return True
        return name in self.gmap.globals

    def rewrite_expr(self, expr, info, goose_fields=False):
        """Goose identifiers and field paths to their C spelling; anything
        not known passes unchanged. Returns (C expression, Goose type of the
        whole expression if it is a plain path)."""
        fn = info.get("fn")
        line = info.get("line")
        toks = tokenize(expr)
        out = []
        cur = None          # Goose type of the path being built, or None
        path_ok = True      # the expression is one path so far
        prev = None         # previous non-space token
        nonspace = [t for t in toks if t[0] != "ws"]
        stack = []          # (type, path_ok) outside each open [
        synth = False       # an index has been taken
        k = 0
        for i, (kind, text) in enumerate(toks):
            if kind == "ws":
                out.append(text)
                continue
            nxt = nonspace[k + 1] if k + 1 < len(nonspace) else None
            k += 1
            if kind == "id":
                after_dot = prev is not None and prev[1] in (".", "->")
                after_ns = prev is not None and prev[1] == "::"
                if after_dot:
                    fname, ftype, arrow = self.field_c(cur, text)
                    if goose_fields or (synth and ftype is not None) or self.no_c_type(cur):
                        # Past an index only LLDB's variable path (the
                        # formatters' children, Goose-named) can go on: C
                        # cannot index a resizable or a slice.
                        # A pointer in C (a reference, a variable-size
                        # value) takes ->, which LLDB's variable path also
                        # follows through the formatters' children.
                        fname, arrow = text, is_pointer_type(cur) or self.no_c_type(cur)
                    if arrow and out and out[-1] == ".":
                        out[-1] = "->"
                    out.append(fname)
                    cur = ftype
                elif after_ns or (nxt is not None and nxt[1] in ("(", "::")) or text in _KEYWORDS:
                    out.append(text)
                    cur = None
                    if prev is not None:
                        path_ok = False
                else:
                    if prev is not None and prev[1] not in ("[", "(", ","):
                        path_ok = False
                    l = self.gmap.local_named(fn, text, line) if fn is not None else None
                    if l is not None:
                        out.append(l["c"])
                        cur = l.get("type")
                    elif text in self.gmap.globals:
                        g = self.gmap.globals[text]
                        if self.gmap.globals_thread:
                            # The selected thread's own instance.
                            out.append("((gs_globals_t *)%s)->%s" % (self.gmap.globals_thread, g["c"]))
                        else:
                            out.append(g["path"])
                        cur = g.get("type")
                    else:
                        out.append(text)
                        cur = None
            elif kind == "op" and text == "[":
                out.append(text)
                synth = True
                stack.append((cur, path_ok))
                cur, path_ok = None, True
            elif kind == "op" and text == "]":
                out.append(text)
                base, path_ok = stack.pop() if stack else (None, False)
                cur = element_type(base) if base else None
            elif kind == "op" and text in (".", "->"):
                out.append(text)
            else:
                out.append(text)
                path_ok = False
                cur = None
            prev = (kind, text)
        return "".join(out), (cur if path_ok and not stack else None)

    def no_c_type(self, gtype):
        """A value whose C is a bare uint8_t * (a variable-size struct, a
        variable-mode enum): only the formatters' Goose-named children reach
        its fields."""
        if not gtype:
            return False
        t = strip_ref(gtype)
        if t.endswith(".."):
            return True
        td = self.gmap.types.get(t)
        return td is not None and not td.get("c")

    def field_c(self, gtype, name):
        """The C member for field `name` of a value of Goose type gtype:
        (C name, its Goose type, whether the value is a pointer to go
        through with ->)."""
        arrow = is_pointer_type(gtype) if gtype else False
        if gtype:
            base = strip_ref(gtype)
            if element_type(base) is not None or base.endswith(":]"):
                if name in ("len", "data", "base", "cap"):
                    return name, "i64" if name in ("len", "cap") else None, arrow
            fields = self.gmap.fields_of(base)
            if fields is None:
                vs = self.gmap.enum_variants(base)
                if vs is not None:
                    fields = [f for v in vs for f in (v.get("fields") or [])]
            if fields is not None:
                for f in fields:
                    if f.get("name") == name and isinstance(f.get("c"), str):
                        return f["c"], f.get("type"), arrow
        if _NAME_RE.match(name) or name in ("len", "data", "base", "tag", "u", "e"):
            return name, None, arrow
        return name + "_g", None, arrow

    def c_setVariable(self, req):
        a = dict(req.get("arguments") or {})
        ctx = self.refs.get(a.get("variablesReference"))
        if ctx is not None and isinstance(ctx.get("names"), dict) and a.get("name") in ctx["names"]:
            a["name"] = ctx["names"][a["name"]]
        self.forward(req, args=a, ctx={"kind": "setVariable"})

    def c_exceptionInfo(self, req):
        tid = (req.get("arguments") or {}).get("threadId")
        e = self.exc.get(tid)
        if e is None:
            self.forward(req)
            return
        self.respond(req, {"exceptionId": "goose runtime error", "description": e["message"],
                           "breakMode": "always",
                           "details": {"message": e["message"], "typeName": "Goose runtime error"}})

    def c_disconnect(self, req):
        self.disconnecting = True
        self.forward(req)

    def c_terminate(self, req):
        self.forward(req)

    def c_completions(self, req):
        self.respond(req, {"targets": []})

    def c_cancel(self, req):
        self.respond(req)

    # ---- from lldb-dap

    def pass_adapter(self, msg):
        if msg.get("type") == "response":
            p = self.pending.pop(msg.get("request_seq"), None)
            if p is None:
                return
            m = dict(msg)
            m["request_seq"] = p[0].get("seq")
            self.send_client(m)
        else:
            self.send_client(dict(msg))

    def on_adapter(self, msg):
        t = msg.get("type")
        if t == "response":
            p = self.pending.pop(msg.get("request_seq"), None)
            if p is None:
                self.log("unmatched response", msg.get("request_seq"))
                return
            req, ctx = p
            m = dict(msg)
            m["request_seq"] = req.get("seq")
            try:
                m = self.rewrite_response(req, ctx, m)
            except Exception:
                self.log("rewrite error:", traceback.format_exc())
            if m is not None:
                self.send_client(m)
        elif t == "event":
            m = self.rewrite_event(dict(msg))
            if m is not None:
                self.send_client(m)

    def rewrite_capabilities(self, caps):
        caps.pop("supportsCompletionsRequest", None)
        caps.pop("completionTriggerCharacters", None)
        caps.pop("supportsCancelRequest", None)
        caps["exceptionBreakpointFilters"] = [{
            "filter": GOOSE_FILTER, "label": "Goose runtime errors",
            "description": "Stop where a Goose check or abort fails: assert, bounds, overflow, "
                           "division by zero, abort(msg)",
            "default": True, "supportsCondition": True}]
        caps["supportsExceptionInfoRequest"] = True
        caps["supportsExceptionFilterOptions"] = True
        caps["supportsBreakpointLocationsRequest"] = True
        return caps

    def rewrite_response(self, req, ctx, m):
        kind = (ctx or {}).get("kind")
        if not m.get("success") and kind == "evaluate" and ctx.get("alt"):
            # The C spelling failed; LLDB's variable path through the
            # formatters' Goose-named children may still get there.
            r = self.adapter_request("evaluate", ctx["alt"])
            if r is not None and r.get("success"):
                m["success"] = True
                m.pop("message", None)
                m["body"] = r.get("body")
        if not m.get("success") and kind == "evaluate" and ctx.get("retry") and \
                re.search(r"failed to parse|undeclared identifier", m.get("message") or ""):
            # A parse failure ran nothing, so the line can go again as a command.
            r = self.adapter_request("evaluate", ctx["retry"])
            if r is not None and r.get("success"):
                m["success"] = True
                m.pop("message", None)
                m["body"] = r.get("body")
                return m
        if not m.get("success"):
            return m
        body = m.get("body")
        if kind == "initialize":
            self.adapter_caps = dict(body or {})
            m["body"] = self.rewrite_capabilities(dict(body or {}))
        elif kind == "scopes":
            self.rewrite_scopes(ctx.get("frame"), body)
        elif kind == "variables":
            self.rewrite_variables(req, ctx.get("ref"), body)
        elif kind == "evaluate":
            self.rewrite_evaluate(ctx, body)
        elif kind == "setVariable" and isinstance(body, dict):
            # lldb-dap 18 answers with "result" where the protocol has "value".
            v = body.get("value", body.pop("result", None))
            body["value"] = clean_value(v, body.get("type"), None)
        elif kind == "bplocs":
            if isinstance(body, dict) and not body.get("breakpoints"):
                self.fill_breakpoint_locations(req, body)
        return m

    def rewrite_scopes(self, info, body):
        if not info or not info.get("goose") or not isinstance(body, dict):
            return
        for s in body.get("scopes") or []:
            name = s.get("name") or ""
            ref = s.get("variablesReference")
            if not ref:
                continue
            if s.get("presentationHint") == "locals" or name == "Locals":
                self.refs[ref] = {"kind": "locals", "frame": info}
                s.pop("namedVariables", None)
                s.pop("indexedVariables", None)
            elif name == "Globals":
                self.refs[ref] = {"kind": "globals", "frame": info}
                s.pop("namedVariables", None)
                s.pop("indexedVariables", None)

    def rewrite_variables(self, req, ctx, body):
        if ctx is None or not isinstance(body, dict):
            return
        vs = body.get("variables") or []
        ref = (req.get("arguments") or {}).get("variablesReference")
        out = []
        names = {}
        if ctx["kind"] == "locals":
            info = ctx["frame"]
            fn = info.get("fn")
            scope = self.gmap.locals_at(fn, info.get("line")) if fn is not None else None
            by_c = {l["c"]: l for l in scope.values()} if scope is not None else None
            for v in vs:
                c = v.get("name") or ""
                if c.startswith("("):
                    out.append(v)        # (Return Value) after a step out
                    continue
                if by_c is not None:
                    l = by_c.get(c)
                    if l is None:
                        continue
                    gname, gtype = l["name"], l.get("type")
                else:
                    if c.startswith("gs_") or not _NAME_RE.match(c):
                        continue
                    gname, gtype = goose_name(c), None
                out.append(self.goose_var(v, gname, gname, gtype))
                names[gname] = c
            # lldb-dap's order is the C's: parameters first, then locals as
            # declared. (Return Value) goes last.
            out = [v for v in out if not (v.get("name") or "").startswith("(")] + \
                  [v for v in out if (v.get("name") or "").startswith("(")]
        elif ctx["kind"] == "child":
            prefix, gtype = ctx.get("prefix"), ctx.get("gtype")
            fields = self.gmap.fields_of(gtype) if gtype else None
            variants = self.gmap.enum_variants(gtype) if gtype and fields is None else None
            et = element_type(gtype) if gtype else None
            for v in vs:
                c = v.get("name") or ""
                if _INDEX_RE.match(c):
                    gname = c
                    ev = prefix + c if prefix else None
                    ct = et
                elif c.startswith("*"):
                    out.append(v)
                    continue
                else:
                    gname = goose_name(c)
                    ev = "%s.%s" % (prefix, gname) if prefix else None
                    ct = None
                    fl = fields
                    if fl is None and variants is not None:
                        fl = [f for vv in variants for f in (vv.get("fields") or [])]
                    for f in fl or []:
                        if f.get("c") == c or f.get("name") == c:
                            gname = f.get("name") or gname
                            ev = "%s.%s" % (prefix, gname) if prefix else None
                            ct = f.get("type")
                            break
                out.append(self.goose_var(v, gname, ev, ct))
                names[gname] = c
        else:
            return
        self.refs[ref] = dict(ctx, names=names)
        body["variables"] = out

    def goose_var(self, v, gname, evaluate_name, gtype):
        nv = dict(v)
        nv["name"] = gname
        if evaluate_name:
            nv["evaluateName"] = evaluate_name
        else:
            nv.pop("evaluateName", None)
        nv["value"] = clean_value(v.get("value"), v.get("type"), gtype)
        if gtype:
            nv["type"] = gtype
        nv.pop("declarationLocationReference", None)
        if v.get("variablesReference"):
            self.refs[v["variablesReference"]] = {"kind": "child", "prefix": evaluate_name, "gtype": gtype}
        return nv

    def rewrite_evaluate(self, ctx, body):
        if not isinstance(body, dict):
            return
        gtype = ctx.get("gtype")
        res, ctype = body.get("result"), body.get("type")
        if ctx.get("context") not in (None, "repl") and isinstance(res, str) and ctype and \
                res.startswith("(%s) " % ctype) and " = " in res:
            # lldb-dap 18 answers a hover as it does the repl,
            # "(gs_i64) total_g = 0": keep the value.
            body["result"] = res.partition(" = ")[2]
        if "result" in body and ctx.get("changed") is not None:
            res = body.get("result")
            ctype = body.get("type")
            if isinstance(res, str) and gtype and ctype and res.startswith("(%s) " % ctype):
                # A repl result, "(Point_g) $2 = Point { ... }": the Goose type.
                res = "(%s) %s" % (gtype, res[len(ctype) + 3:])
                head, sep, val = res.partition(" = ")
                if sep:
                    res = head + sep + clean_value(val, ctype, gtype)
            else:
                res = clean_value(res, ctype, gtype)
            body["result"] = res
        if gtype:
            body["type"] = gtype
        ref = body.get("variablesReference")
        if ref:
            self.refs[ref] = {"kind": "child", "prefix": ctx.get("prefix"), "gtype": gtype}

    def rewrite_event(self, e):
        name = e.get("event")
        body = e.get("body") if isinstance(e.get("body"), dict) else None
        # goose_dap_lldb.py's answers to the proxy, which lldb-dap also
        # echoes to the console.
        if name == "output" and body is not None and "GOOSE_DAP:" in str(body.get("output") or ""):
            return None
        if name == "initialized":
            self.ensure_map(force=True)
            return e
        if name == "stopped":
            self.reset_stop_state()
            first = not getattr(self, "seen_stop", False)
            self.seen_stop = True
            if body is not None and first and self.launch_args.get("stopOnEntry") \
                    and not body.get("hitBreakpointIds") and body.get("reason") == "exception":
                # lldb-dap 21 reports the stop at entry (a SIGSTOP) as an exception.
                body["reason"] = "entry"
                body.pop("description", None)
                return e
            if body is not None:
                hit = body.get("hitBreakpointIds") or []
                if "hitBreakpointIds" not in body and body.get("reason") in ("breakpoint", "function breakpoint"):
                    # lldb-vscode 16 has no hitBreakpointIds, only its
                    # description, "breakpoint 2.1" (breakpoint 2, location 1).
                    hit = [int(n) for n in re.findall(r"\b(\d+)\.\d+\b", body.get("description") or "")]
                    if hit:
                        body["hitBreakpointIds"] = hit
                tid = body.get("threadId")
                if self.runtime_bp_ids and any(h in self.runtime_bp_ids for h in hit):
                    err = self.runtime_error(tid)
                    msg = err.get("message") or "Goose runtime error"
                    if err.get("file"):
                        msg += " (%s:%s)" % (os.path.basename(err["file"]), err.get("line"))
                    self.exc[tid] = {"message": msg, "error": err}
                    body["reason"] = "exception"
                    body["description"] = "Goose runtime error: " + msg
                    body["text"] = msg
                    body.pop("hitBreakpointIds", None)
                elif hit:
                    body["hitBreakpointIds"] = sorted(set(self.fbp_secondary.get(h, h) for h in hit))
            return e
        if name == "continued":
            self.reset_stop_state()
            self.exc.clear()
            return e
        if name == "breakpoint" and body is not None:
            bid = (body.get("breakpoint") or {}).get("id")
            if bid in self.runtime_bp_ids or bid in self.fbp_secondary:
                return None
            return e
        if name == "terminated":
            if self.terminated_sent:
                return None
            self.terminated_sent = True
            return e
        if name == "capabilities" and body is not None and isinstance(body.get("capabilities"), dict):
            self.adapter_caps.update(body["capabilities"])
            caps = dict(body["capabilities"])
            caps.pop("supportsCompletionsRequest", None)
            caps.pop("supportsCancelRequest", None)
            caps.pop("exceptionBreakpointFilters", None)
            body["capabilities"] = caps
            return e
        return e

    # ---- talking to goose_dap_lldb.py

    def lldb_script(self, call):
        cmd = "%sscript goose_dap_lldb.%s" % (self.escape, call)
        r = self.adapter_request("evaluate", {"expression": cmd, "context": "repl"})
        if r is None or not r.get("success"):
            self.log("script failed:", call, r and r.get("message"))
            return None
        res = (r.get("body") or {}).get("result") or ""
        for line in res.splitlines():
            i = line.find("GOOSE_DAP:")
            if i >= 0:
                try:
                    return json.loads(line[i + len("GOOSE_DAP:"):])
                except ValueError:
                    return None
        return None

    def ensure_map(self, force=False):
        if self.map_loaded and not force:
            return
        self.map_loaded = True
        paths = []
        info = self.lldb_script("info()")
        if isinstance(info, dict) and not info.get("error"):
            paths = [p for p in info.get("maps") or [] if isinstance(p, str)]
            self.globals_block = info.get("globals_block") or []
            self.log("lldb:", info.get("lldb"))
        if not paths:
            paths = self.guess_map_paths()
        docs = []
        for p in paths:
            try:
                with open(p) as f:
                    docs.append(json.load(f))
            except Exception as e:
                self.log("cannot read map", p, e)
        self.gmap = GooseMap(docs)
        self.gmap.paths = paths
        self.log("map:", paths, len(self.gmap.funcs), "functions")

    def guess_map_paths(self):
        """Without an answer from LLDB: the newest
        GOOSE_HOME/build/prog/<stem>-*/<stem>.debug.json for the program."""
        prog = self.launch_args.get("program")
        if not isinstance(prog, str):
            return []
        stem = os.path.splitext(os.path.basename(prog))[0]
        root = os.path.join(goose_home(), "build", "prog")
        best = None
        try:
            for d in os.listdir(root):
                if not d.startswith(stem + "-"):
                    continue
                j = os.path.join(root, d, stem + ".debug.json")
                if os.path.exists(j):
                    mt = os.path.getmtime(j)
                    if best is None or mt > best[0]:
                        best = (mt, j)
        except OSError:
            return []
        return [best[1]] if best else []

    def source_lines(self, path):
        if path in self.line_cache:
            return self.line_cache[path]
        r = self.lldb_script("lines(%s)" % json.dumps(path))
        lines = r.get("lines") if isinstance(r, dict) else None
        if lines:
            self.line_cache[path] = lines
        return lines

    def runtime_error(self, tid):
        r = self.lldb_script("runtime_error(%d)" % int(tid or 0))
        return r if isinstance(r, dict) and not r.get("error") else {}


def tokenize(s):
    return [(m.lastgroup, m.group()) for m in _TOKEN_RE.finditer(s)]


def clean_value(value, ctype, gtype):
    """lldb-dap's value text for a Goose value: no address in front of a
    summary (a variable-size value or a reference is a pointer in C), and
    bool as true/false."""
    if not isinstance(value, str):
        return value
    # lldb-vscode 16 joins an empty value and the summary with a space.
    value = value.lstrip()
    m = _ADDR_PREFIX_RE.match(value)
    if m and ("*" in (ctype or "") or re.match(r"^[ro]_", ctype or "")):
        value = m.group(1)
    if gtype == "bool" and value in ("0", "1"):
        value = "true" if value == "1" else "false"
    return value


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="goose-dap", description=__doc__.split("\n\n")[0])
    ap.add_argument("--lldb-dap", help="the lldb-dap to run (default: $GOOSE_DAP_LLDB_DAP, "
                                       "xcrun -f lldb-dap, the Command Line Tools, PATH)")
    ap.add_argument("--log", help="log every DAP message to this file (or set GOOSE_DAP_LOG)")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print("goose-dap 0.1")
        return 0
    if sys.stdin.isatty() and sys.stdout.isatty():
        sys.stderr.write("goose-dap speaks the Debug Adapter Protocol on stdin/stdout; "
                         "run it from an editor's debug configuration.\n")
    log = Log(a.log or os.environ.get("GOOSE_DAP_LOG"))
    p = Proxy(lldb_dap=a.lldb_dap, log=log)
    try:
        return p.run()
    except KeyboardInterrupt:
        # How nvim-dap ends every session (after closing stdin): a normal end.
        p.shutdown()
        return 0


if __name__ == "__main__":
    rc = main()
    # The client reader is a daemon thread blocked reading stdin; at
    # interpreter shutdown Python aborts on a buffered stream another thread
    # holds (_enter_buffered_busy). The session is over: flush and leave.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(rc or 0)
