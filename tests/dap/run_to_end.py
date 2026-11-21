# Run a program under lldb-dap with no breakpoints; print every event until terminated.
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dap_session import Dap, GOOSE_LLDB
prog = os.path.abspath(sys.argv[1])
log = []
d = Dap(log)
d.request("initialize", {"clientID": "proto", "adapterID": "lldb-dap", "linesStartAt1": True, "columnsStartAt1": True, "pathFormat": "path", "supportsRunInTerminalRequest": False})
d.send("launch", {"program": prog, "initCommands": ["command script import " + GOOSE_LLDB]})
d.event("initialized")
if len(sys.argv) > 2:
    r = d.request("setFunctionBreakpoints", {"breakpoints": [{"name": n} for n in sys.argv[2:]]})
    print("setFunctionBreakpoints", [b.get("verified") for b in r["body"]["breakpoints"]])
d.request("configurationDone")
while True:
    m = d.wait(lambda m: m.get("type") == "event", timeout=20)
    b = m.get("body", {})
    if m["event"] == "output":
        print("output", b.get("category"), repr(b.get("output")))
    elif m["event"] == "stopped":
        print("stopped", json.dumps(b))
        r = d.request("exceptionInfo", {"threadId": b["threadId"]})
        print("exceptionInfo", json.dumps(r.get("body")), r.get("message"))
        st = d.request("stackTrace", {"threadId": b["threadId"], "levels": 12})
        for f in st["body"]["stackFrames"]:
            print("  frame", f["name"], (f.get("source") or {}).get("path") or (f.get("source") or {}).get("name"), f.get("line"), (f.get("source") or {}).get("presentationHint"))
        d.request("disconnect", {"terminateDebuggee": True}, timeout=5)
        break
    elif m["event"] in ("exited", "terminated"):
        print(m["event"], json.dumps(b))
        if m["event"] == "terminated":
            break
    elif m["event"] not in ("module", "process", "thread", "breakpoint", "capabilities", "loadedSource"):
        print("event", m["event"], json.dumps(b)[:200])
try:
    d.request("disconnect", {}, timeout=3)
except Exception:
    pass
d.p.wait(timeout=5)
