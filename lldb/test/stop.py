"""test-stop: the breakpoint command run.py uses. At the first stop in each
function and line it prints "@@ function file:line", then the output of
`frame variable`, `goose-locals` and any further commands given to it,
separated by ";". A Goose line that compiles to several
C statements can stop more than once; later stops at the same place print
nothing, so the record does not depend on how the C compiler splits lines."""

import lldb

_seen = set()


def stop(debugger, command, exe_ctx, result, internal_dict):
    f = exe_ctx.GetFrame()
    le = f.GetLineEntry()
    key = (f.GetFunctionName(), le.GetFileSpec().GetFilename(), le.GetLine())
    if key in _seen:
        return
    _seen.add(key)
    lines = ["@@ %s %s:%d" % key]
    ci = debugger.GetCommandInterpreter()
    extra = [c.strip() for c in command.split(";") if c.strip()]
    for cmd in ["frame variable", "goose-locals"] + extra:
        r = lldb.SBCommandReturnObject()
        ci.HandleCommand(cmd, exe_ctx, r)
        lines.append("-- " + cmd)
        lines.append((r.GetOutput() or "") + (r.GetError() or ""))
    result.AppendMessage("\n".join(lines))


def children(debugger, command, exe_ctx, result, internal_dict):
    """test-children VAR: VAR's children as a debugger UI (lldb-dap) lists
    them: synthetic ones where a provider gives them."""
    v = exe_ctx.GetFrame().GetValueForVariablePath(command.strip())
    if not v.IsValid():
        result.AppendMessage("%s: no such variable" % command.strip())
        return
    n = v.GetNumChildren()
    result.AppendMessage("%s: %d children" % (command.strip(), n))
    for i in range(min(n, 8)):
        c = v.GetChildAtIndex(i)
        s = c.GetSummary() or c.GetValue() or ""
        result.AppendMessage("  %s (%s) = %s" % (c.GetName(), c.GetTypeName(), s))


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("command script add -o -f %s.stop test-stop" % __name__)
    debugger.HandleCommand("command script add -o -f %s.children test-children" % __name__)
