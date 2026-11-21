"""The gdb side of run.py, sourced into gdb after goose_gdb.py.

goose_test_drive(stops, progout) runs the program (its output to the file
progout) and, at the first stop at each function and line, prints
"@@ function file:line", then `info args`, `info locals`, `goose-locals` and
the extra commands for that line, each under "-- command". A stop whose
commands begin with "!" records only those. Later stops at the same place
print nothing, so the record does not depend on how the C compiler splits
lines. At the end it prints "exited with status = N".

goose-test-children VAR lists VAR's children as an MI front end would get
them (with `goose-children` on).
"""

import os

import gdb

_events = []


def _on_stop(ev):
    _events.append(ev)


def _run(cmd):
    try:
        return gdb.execute(cmd, to_string=True) or ""
    except gdb.error as e:
        return "error: %s\n" % e


def goose_test_drive(stops, progout):
    gdb.events.stop.connect(_on_stop)
    out = []
    seen = set()
    _run("run > %s 2>&1" % progout)
    for _ in range(10000):
        inf = gdb.selected_inferior()
        if inf.pid == 0:
            break
        ev = _events[-1] if _events else None
        if ev is not None and isinstance(ev, gdb.SignalEvent):
            out.append("!! stopped by %s" % ev.stop_signal)
            out.append(_run("bt 8").rstrip("\n"))
            _run("kill")
            break
        f = gdb.selected_frame()
        sal = f.find_sal()
        fname = os.path.basename(sal.symtab.filename) if sal.symtab else "?"
        key = (f.name(), fname, sal.line)
        if key not in seen:
            seen.add(key)
            out.append("@@ %s %s:%d" % key)
            cmds = stops.get(sal.line, "")
            only = cmds.startswith("!")
            extra = [c.strip() for c in cmds.lstrip("!").split(";") if c.strip()]
            for c in ([] if only else ["info args", "info locals", "goose-locals"]) + extra:
                out.append("-- " + c)
                out.append(_run(c).rstrip("\n"))
        _run("continue")
    try:
        with open(progout, errors="replace") as f:
            prog = f.read().rstrip("\n")
        if prog:
            out.append(prog)
    except OSError:
        pass
    try:
        code = int(gdb.parse_and_eval("$_exitcode"))
        out.append("exited with status = %d" % code)
    except Exception:
        out.append("exited: no exit code")
    gdb.write("\n".join(out) + "\n")


class TestChildren(gdb.Command):
    """goose-test-children VAR: VAR's children, as a front end lists them."""

    def __init__(self):
        super(TestChildren, self).__init__("goose-test-children", gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        name = arg.strip()
        try:
            v = gdb.parse_and_eval(name)
        except gdb.error:
            gdb.write("%s: no such variable\n" % name)
            return
        gdb.execute("set goose-children on")
        try:
            vis = gdb.default_visualizer(v)
            kids = list(vis.children()) if vis is not None and hasattr(vis, "children") else []
        finally:
            gdb.execute("set goose-children off")
        gdb.write("%s: %d children\n" % (name, len(kids)))
        for n, c in kids[:8]:
            try:
                s = str(c)
            except gdb.error as e:
                s = "<%s>" % e
            gdb.write("  %s (%s) = %s\n" % (n, c.type, s))


TestChildren()
