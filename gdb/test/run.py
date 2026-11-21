#!/usr/bin/env python3
"""Regression test for goose_gdb.py.

For each sample lldb/test/*.goose: build it with `goose build -g`, run it
under gdb in batch mode with a breakpoint on every line marked `// BREAK`,
and at the first stop at each function and line record `info args`, `info
locals` (Goose-named variables only: temporaries tN and the hidden gs_sp are
dropped), `goose-locals`, and the commands after `lldb:` on that line,
translated to gdb (below). The record, with addresses, C line numbers and
directories masked, must equal gdb/test/<name>.expected (or
<name>.<variant>.expected).

Each sample runs up to three ways:
  <name>.expected            with the compiler's debug map (<stem>.debug.json)
  <name>.nomap.expected      GOOSE_GDB_MAP=0: DWARF and the generated C
  <name>.nocsource.expected  (structs, generics) GOOSE_GDB_MAP=0
                             GOOSE_GDB_CSOURCE=0: DWARF alone
A compiler that writes no map is checked against <name>.nomap.expected only.

  run.py              check every sample
  run.py --update     rewrite the .expected files from this run
  run.py arrays refs  only these samples

Environment:
  GOOSE           the goose compiler (default: goose on PATH)
  GDB             gdb (default: gdb on PATH); gdb < 10 needs DWARF 4 (gcc,
                  or clang with -gdwarf-4)
  GOOSE_GDB_WORK  where binaries go (default: a fresh temporary directory)

`// BREAK` lines are those of the LLDB tests; their `lldb:` commands are
translated: `frame variable X`, `target variable X` and `p X` become
`print X`, `memory read -s1 -cN -fu X` becomes `x/Nub X`, `test-children X`
becomes `goose-test-children X`, `goose-frame-format` is dropped, and
goose-locals, goose-bt, goose-as, goose-globals and bt are the same.
"""

import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GDBDIR = os.path.dirname(HERE)
SAMPLES = os.path.join(os.path.dirname(GDBDIR), "lldb", "test")
MODULE = os.path.join(GDBDIR, "goose_gdb.py")
STOP = os.path.join(HERE, "stop.py")

NOMAP = {"GOOSE_GDB_MAP": "0"}
NOCSOURCE = {"GOOSE_GDB_MAP": "0", "GOOSE_GDB_CSOURCE": "0"}
DWARF_ONLY = ("structs", "generics")

GOOSE_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*?_g(_?[0-9]+)? = ")
ADDR = re.compile(r"0x[0-9a-fA-F]+")
HISTORY = re.compile(r"^\$[0-9]+ = ")
C_LINE = re.compile(r"(\.c):[0-9]+")
DIRS = re.compile(r"(?<![\w.])/[^\s:,()]*/(?=[^/\s:,()]+\.(goose|c)\b)")


def build(goose, src, out):
    try:
        r = subprocess.run([goose, "build", "-g", "-o", out, src],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    except OSError as e:
        raise SystemExit("cannot run the goose compiler %r (set GOOSE): %s" % (goose, e))
    if r.returncode != 0:
        raise RuntimeError("build failed: %s\n%s" % (src, r.stdout))


def writes_map(goose, src, work):
    c = os.path.join(work, "probe-" + os.path.basename(src)[:-6] + ".c")
    subprocess.run([goose, "-g", "-o", c, src], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return os.path.exists(c[:-2] + ".debug.json")


def translate(cmd):
    """An lldb: command as gdb commands (a list, empty to drop it)."""
    w = cmd.split()
    if not w:
        return []
    if w[0] == "frame" and len(w) > 2 and w[1] == "variable":
        return ["print " + x for x in w[2:]]
    if w[0] == "target" and len(w) > 2 and w[1] == "variable":
        return ["print " + x for x in w[2:]]
    if w[0] == "p":
        return ["print " + cmd[1:].strip()]
    if w[0] == "memory" and len(w) > 2 and w[1] == "read":
        m = re.match(r"memory read -s1 -c([0-9]+) -fu (\S+)$", cmd.strip())
        return ["x/%sub %s" % (m.group(1), m.group(2))] if m else []
    if w[0] == "test-children":
        return ["goose-test-children " + " ".join(w[1:])]
    if w[0].startswith("goose-frame-format"):
        return []
    return [cmd.strip()]


def break_lines(src):
    """(line, condition, gdb commands, maponly) for each `// BREAK` line, as
    lldb/test/run.py reads them."""
    out = []
    with open(src) as f:
        for i, line in enumerate(f):
            j = line.find("// BREAK")
            if j < 0:
                continue
            rest = line[j + len("// BREAK"):].lstrip("0123456789")
            maponly = rest.strip().startswith("maponly")
            if maponly:
                rest = rest.strip()[len("maponly"):]
            only = False
            k = rest.find("lldb only:")
            if k >= 0:
                only = True
                cmds = rest[k + 10:]
                rest = rest[:k]
            else:
                k = rest.find("lldb:")
                cmds = rest[k + 5:] if k >= 0 else ""
                rest = rest[:k] if k >= 0 else rest
            gcmds = [g for c in cmds.split(";") for g in translate(c.strip())]
            cond = rest.strip()[5:].strip() if rest.strip().startswith("when ") else ""
            out.append((i + 1, cond, ("!" if only else "") + "; ".join(gcmds), maponly))
    return out


def record(gdb, exe, src, env, with_map):
    work = os.path.dirname(exe)
    name = os.path.basename(src)
    stops = {}
    lines = ["set pagination off", "set width 0", "set height 0", "set confirm off",
             "set print pretty off", "source " + MODULE, "source " + STOP]
    for line, cond, cmds, maponly in break_lines(src):
        if maponly and not with_map:
            continue
        lines.append("break %s:%d%s" % (name, line, " if " + cond if cond else ""))
        stops[line] = cmds
    progout = exe + ".out"
    lines.append("python goose_test_drive(%r, %r)" % (stops, progout))
    script = exe + ".gdb"
    with open(script, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        r = subprocess.run([gdb, "-nx", "-batch", "-x", script, exe],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, env=dict(os.environ, **env), cwd=work,
                           timeout=180)
        text = r.stdout
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or "") + "\n!! gdb timed out\n"
    out = []
    section = None
    started = False
    for line in text.splitlines():
        if line.startswith("@@ "):
            started = True
            out.append(line)
            section = None
            continue
        if not started:
            continue
        if line.startswith("-- "):
            section = line[3:]
            out.append(line)
            continue
        line = DIRS.sub("", C_LINE.sub(r"\1:N", ADDR.sub("0x_", line)))
        if section in ("info args", "info locals"):
            if GOOSE_VAR.match(line) or line.startswith("error"):
                out.append(line)
        elif line.strip():
            out.append(HISTORY.sub("$ = ", line))
    if not started:
        out.append("!! no stop recorded; gdb said:")
        out += text.splitlines()[-30:]
    return "\n".join(out) + "\n"


def main(argv):
    update = "--update" in argv
    names = [a for a in argv if not a.startswith("--")]
    goose = os.environ.get("GOOSE", "goose")
    gdb = os.environ.get("GDB", "gdb")
    work = os.environ.get("GOOSE_GDB_WORK") or tempfile.mkdtemp(prefix="goose-gdb-")
    work = os.path.abspath(work)
    os.makedirs(work, exist_ok=True)
    samples = sorted(f[:-6] for f in os.listdir(SAMPLES) if f.endswith(".goose"))
    if names:
        samples = [s for s in samples if s in names]
    failed = []
    for s in samples:
        src = os.path.join(SAMPLES, s + ".goose")
        exe = os.path.join(work, s)
        build(goose, src, exe)
        if writes_map(goose, src, work):
            runs = [({}, s, True), (NOMAP, s + ".nomap", False)]
        else:
            runs = [({}, s + ".nomap", False)]
        if s in DWARF_ONLY:
            runs.append((NOCSOURCE, s + ".nocsource", False))
        for env, label, with_map in runs:
            got = record(gdb, exe, src, env, with_map)
            exp_path = os.path.join(HERE, label + ".expected")
            if update:
                with open(exp_path, "w") as f:
                    f.write(got)
                print("wrote", os.path.basename(exp_path))
                continue
            try:
                with open(exp_path) as f:
                    want = f.read()
            except OSError:
                want = None
            if got == want:
                print("ok  ", label)
                continue
            failed.append(label)
            print("FAIL", label)
            with open(os.path.join(work, label + ".got"), "w") as f:
                f.write(got)
            if want is not None:
                sys.stdout.writelines(difflib.unified_diff(
                    want.splitlines(True), got.splitlines(True),
                    label + ".expected", label + ".got"))
            else:
                print("  no %s; run with --update to create it" % os.path.basename(exp_path))
    if not os.environ.get("GOOSE_GDB_WORK"):
        shutil.rmtree(work, ignore_errors=True)
    if failed:
        print("%d failed: %s" % (len(failed), " ".join(failed)))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
