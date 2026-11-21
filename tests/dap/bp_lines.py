# Which lines lldb-dap accepts: setBreakpoints on every line, plus breakpointLocations.
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dap_session import Dap
prog, src, n = sys.argv[1], os.path.abspath(sys.argv[2]), int(sys.argv[3])
log = []
d = Dap(log)
d.request("initialize", {"clientID": "proto", "adapterID": "lldb-dap", "linesStartAt1": True, "columnsStartAt1": True, "pathFormat": "path"})
d.send("launch", {"program": os.path.abspath(prog), "stopOnEntry": True})
d.event("initialized")
r = d.request("setBreakpoints", {"source": {"path": src}, "breakpoints": [{"line": i} for i in range(1, n + 1)]})
for b in r["body"]["breakpoints"]:
    print("setBreakpoints", {k: b.get(k) for k in ("id", "verified", "line", "column", "message")})
r = d.request("breakpointLocations", {"source": {"path": src}, "line": 1, "endLine": n})
print("breakpointLocations", json.dumps(r.get("body")))
r = d.request("setFunctionBreakpoints", {"breakpoints": [{"name": "area"}, {"name": "area_g"}]})
print("setFunctionBreakpoints", json.dumps([{k: b.get(k) for k in ("verified", "line", "message")} for b in r["body"]["breakpoints"]]))
d.request("configurationDone")
try: d.request("disconnect", {"terminateDebuggee": True}, timeout=5)
except Exception as e: print("disconnect", e)
d.p.wait(timeout=5)
