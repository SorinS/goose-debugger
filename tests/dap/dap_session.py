#!/usr/bin/env python3
"""A minimal DAP client: drives lldb-dap over stdio the way an IDE would,
and records every request and response.

usage: dap_session.py PROGRAM SOURCE LINE [--no-formatters] [--expand N] [--log FILE]
                      [--step SEQ]   SEQ is e.g. "in,next,out"
                      [--eval EXPR]... (evaluated in the top frame, context watch and repl)
"""
import argparse, json, os, subprocess, sys, threading, queue, time

LLDB_DAP = os.environ.get("LLDB_DAP", "/Library/Developer/CommandLineTools/usr/bin/lldb-dap")
GOOSE_LLDB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lldb", "goose_lldb.py")


class Dap:
    def __init__(self, log):
        self.p = subprocess.Popen([LLDB_DAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.seq = 0
        self.q = queue.Queue()
        self.log = log
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        f = self.p.stdout
        while True:
            hdr = b""
            while not hdr.endswith(b"\r\n\r\n"):
                c = f.read(1)
                if not c:
                    self.q.put(None)
                    return
                hdr += c
            n = int([l for l in hdr.decode().split("\r\n") if l.lower().startswith("content-length")][0].split(":")[1])
            msg = json.loads(f.read(n))
            self.q.put(msg)

    def send(self, command, arguments=None):
        self.seq += 1
        m = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            m["arguments"] = arguments
        b = json.dumps(m).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
        self.p.stdin.flush()
        self.log.append({"->": m})
        return self.seq

    def wait(self, pred, timeout=20):
        # Messages read while waiting for something else are kept, so a
        # response or event that arrives early is not lost.
        if not hasattr(self, "pending"):
            self.pending = []
        for i, m in enumerate(self.pending):
            if pred(m):
                del self.pending[i]
                return m
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if m is None:
                raise RuntimeError("adapter exited")
            self.log.append({"<-": m})
            if pred(m):
                return m
            if m.get("type") == "event" and m.get("event") in ("stopped", "initialized", "exited", "terminated") \
                    or m.get("type") == "response":
                self.pending.append(m)
        raise TimeoutError("timed out")

    def request(self, command, arguments=None, timeout=20):
        s = self.send(command, arguments)
        return self.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == s, timeout)

    def event(self, name, timeout=20):
        return self.wait(lambda m: m.get("type") == "event" and m.get("event") == name, timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("program")
    ap.add_argument("source")
    ap.add_argument("line", type=int)
    ap.add_argument("--no-formatters", action="store_true")
    ap.add_argument("--expand", type=int, default=2, help="levels of variables to expand")
    ap.add_argument("--log", default=None)
    ap.add_argument("--step", default="")
    ap.add_argument("--eval", action="append", default=[])
    ap.add_argument("--hits", type=int, default=1, help="stops at the breakpoint to report")
    a = ap.parse_args()

    log = []
    d = Dap(log)
    out = []
    def say(s=""):
        out.append(s)
        print(s)

    r = d.request("initialize", {"clientID": "proto", "adapterID": "lldb-dap", "linesStartAt1": True,
                                 "columnsStartAt1": True, "pathFormat": "path",
                                 "supportsVariableType": True, "supportsRunInTerminalRequest": False})
    caps = r.get("body", {})
    say("capabilities: " + ", ".join(sorted(k for k, v in caps.items() if v is True)))
    init = [] if a.no_formatters else ["command script import " + GOOSE_LLDB]
    d.send("launch", {"program": os.path.abspath(a.program), "initCommands": init, "stopOnEntry": False,
                      "cwd": os.path.dirname(os.path.abspath(a.program))})
    d.event("initialized")
    r = d.request("setBreakpoints", {"source": {"path": os.path.abspath(a.source)},
                                     "breakpoints": [{"line": a.line}], "lines": [a.line]})
    say("setBreakpoints -> " + json.dumps(r["body"]["breakpoints"]))
    d.request("configurationDone")
    d.wait(lambda m: m.get("type") == "response" and m.get("command") == "launch")

    def show_stop(label):
        st = d.event("stopped")
        say(f"\n=== {label}: stopped reason={st['body'].get('reason')} thread={st['body'].get('threadId')}"
            + (f" description={st['body'].get('description')!r}" if st['body'].get('description') else ""))
        tid = st["body"]["threadId"]
        th = d.request("threads")
        say("threads: " + json.dumps([(t["id"], t["name"]) for t in th["body"]["threads"]]))
        stk = d.request("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 20})
        frames = stk["body"]["stackFrames"]
        for f in frames:
            src = f.get("source", {})
            say(f"  frame {f['id']}: name={f['name']!r} source={src.get('path') or src.get('name')!r} "
                f"line={f.get('line')} col={f.get('column')} hint={f.get('presentationHint')!r} "
                f"srcHint={src.get('presentationHint')!r} ref={src.get('sourceReference')}")
        top = frames[0]["id"]
        sc = d.request("scopes", {"frameId": top})
        for s in sc["body"]["scopes"]:
            say(f"  scope {s['name']!r} ref={s['variablesReference']} expensive={s.get('expensive')}")
            if s["name"].lower().startswith("register"):
                continue
            dump(s["variablesReference"], 2, a.expand)
        for e in a.eval:
            for ctx in ("watch", "repl"):
                r = d.request("evaluate", {"expression": e, "frameId": top, "context": ctx})
                body = r.get("body", {})
                say(f"  evaluate[{ctx}] {e!r} -> success={r['success']} result={body.get('result')!r} "
                    f"type={body.get('type')!r} ref={body.get('variablesReference')} msg={r.get('message')!r}")
        return tid

    def dump(ref, indent, depth):
        v = d.request("variables", {"variablesReference": ref})
        for x in v["body"].get("variables", []):
            say(" " * indent + f"{x['name']} = {x.get('value')!r}  type={x.get('type')!r} "
                f"evaluateName={x.get('evaluateName')!r} ref={x.get('variablesReference')}")
            if depth > 1 and x.get("variablesReference"):
                dump(x["variablesReference"], indent + 4, depth - 1)

    tid = show_stop("breakpoint")
    for i in range(1, a.hits):
        d.request("continue", {"threadId": tid})
        tid = show_stop(f"breakpoint hit {i + 1}")
    for s in [x for x in a.step.split(",") if x]:
        cmd = {"in": "stepIn", "next": "next", "out": "stepOut"}[s]
        d.request(cmd, {"threadId": tid})
        tid = show_stop(cmd)

    try:
        d.request("disconnect", {"terminateDebuggee": True}, timeout=5)
    except Exception as e:
        say(f"disconnect: {e}")
    try:
        d.p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        d.p.kill()
    if a.log:
        with open(a.log, "w") as f:
            json.dump(log, f, indent=1)
            f.write("\n")


if __name__ == "__main__":
    main()
