#!/usr/bin/env python3
"""Regression test for goose.natvis, gen_natvis.py and goose.js, on Windows
with cdb.

For each ../../lldb/test/*.goose: build it with `goose build -g`, write its C
and debug map with `goose -g -o <work>/<stem>.c`, generate <stem>.natvis
from the map, and run the program under cdb with a breakpoint on every line
marked `// BREAK`. At the first stop at each function and line it records
`dx -r1` of each Goose local in scope (from the map's scopes), `dx -r1
gs_globals_main` when the program has globals, and goose.js's
`!goose_locals` and `!goose_globals`. Addresses are masked. The record must
equal test/<stem>.expected; the same stops without natvis and goose.js
must equal test/<stem>.raw.expected (the "before" of the README).

  run.py               check every sample
  run.py --update      rewrite the .expected files from this run
  run.py arrays refs   only these samples

Environment:
  GOOSE        the goose compiler (default: goose on PATH), with patches
               0001-0007 (-g, the map)
  CDB          cdb.exe (default: the Windows Kits 10 x64 one)
  NATVIS_WORK  where binaries go (default: a fresh temporary directory,
               removed afterwards)

`// BREAK when COND` stops only where the C expression COND holds (cdb's
`bp /w`). A line in several function instances (a generic function) stops
in each: cdb reports the line as ambiguous, so a first cdb run lists the
locations (`Matched: mod!fn+0x..`) and the second sets one breakpoint at
each.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NATVIS_DIR = os.path.dirname(HERE)
SAMPLES = os.path.join(os.path.dirname(NATVIS_DIR), "lldb", "test")
GEN = os.path.join(NATVIS_DIR, "gen_natvis.py")
GENERIC = os.path.join(NATVIS_DIR, "goose.natvis")
SCRIPT = os.path.join(NATVIS_DIR, "goose.js")
DEFAULT_CDB = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"

ADDR = re.compile(r"0x[0-9a-fA-F]{5,}|\b[0-9a-f]{8}`[0-9a-f]{8}\b")
LOCATION = re.compile(r"^(?:Matched: )?([A-Za-z0-9_]+![A-Za-z0-9_]+\+0x[0-9a-f]+)")
BL_LOCATION = re.compile(r"\s([A-Za-z0-9_]+![A-Za-z0-9_]+\+0x[0-9a-f]+)\s*$")


def break_lines(src):
    """(line, condition) for each `// BREAK` line."""
    out = []
    with open(src, encoding="utf-8") as f:
        for i, line in enumerate(f):
            j = line.find("// BREAK")
            if j < 0:
                continue
            rest = line[j + len("// BREAK"):].lstrip("0123456789").strip()
            if rest.startswith("maponly"):
                rest = rest[len("maponly"):].strip()
            k = rest.find("lldb")
            if k >= 0:
                rest = rest[:k].strip()
            cond = rest[5:].strip() if rest.startswith("when ") else ""
            out.append((i + 1, cond))
    return out


def run(args, timeout=180, **kw):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           text=True, encoding="utf-8", errors="replace", **kw)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -1, out + "\n!! timed out\n"


def cdb(cdb_exe, exe, script, work):
    return run([cdb_exe, "-lines", "-srcpath", work, "-cf", script, exe], cwd=work)[1]


PROMPT = re.compile(r"^\d+:\d+> ")


def locations(cdb_exe, exe, name, lines, work):
    """{line: [mod!fn+0xNN]}: where cdb puts a breakpoint on each line."""
    script = os.path.join(work, name + ".where.cdb")
    with open(script, "w") as f:
        f.write(".lines -e\n")
        for i, (line, _) in enumerate(lines):
            f.write(".echo @@line %d\nbp%d `%s.goose:%d`\n" % (line, i, name, line))
        f.write(".echo @@bl\nbl\nq\n")
    out = cdb(cdb_exe, exe, script, work)
    where = {line: [] for line, _ in lines}
    cur, bl = None, False
    for l in out.splitlines():
        l = PROMPT.sub("", l)
        if l.startswith("@@line "):
            cur = int(l.split()[1])
            continue
        if l.startswith("@@bl"):
            bl = True
            continue
        if bl:
            m = BL_LOCATION.search(l)
            n = re.match(r"\s*(\d+) e", l)
            if m and n and int(n.group(1)) < len(lines):
                where[lines[int(n.group(1))][0]].append(m.group(1))
            continue
        if cur is not None and l.startswith("Matched: "):
            m = LOCATION.match(l)
            if m:
                where[cur].append(m.group(1))
    return where


def scope_locals(gmap, fname, line):
    for f in gmap.get("functions", []):
        if f.get("c") == fname:
            ls = [l for l in f.get("locals", []) if l["line"] <= line <= l["end"]]
            return sorted(ls, key=lambda l: not l.get("param"))
    return []


def record(cdb_exe, exe, name, gmap, where, lines, work, natvis):
    script = os.path.join(work, name + (".natvis" if natvis else ".raw") + ".cdb")
    globals_ = (gmap.get("globals") or {}).get("vars") or []
    with open(script, "w") as f:
        f.write("sxd av\n.lines -e\n")
        if natvis:
            # The program's file first: on a name both define, the first
            # loaded wins.
            f.write(".nvload %s\n.nvload %s\n" % (os.path.join(work, name + ".natvis"), GENERIC))
            f.write(".scriptload %s\n" % SCRIPT)
        n = 0
        for line, cond in lines:
            for loc in where.get(line, []):
                fname = loc.split("!")[1].split("+")[0]
                cmds = [".echo @@ %s.goose:%d %s" % (name, line, fname)]
                for l in scope_locals(gmap, fname, line):
                    cmds += [".echo -- dx -r1 %s" % l["c"], "dx -r1 %s" % l["c"]]
                if globals_:
                    cmds += [".echo -- dx -r1 gs_globals_main", "dx -r1 gs_globals_main"]
                if natvis:
                    cmds += [".echo -- !goose_locals", "!goose_locals"]
                    if globals_:
                        cmds += [".echo -- !goose_globals", "!goose_globals"]
                cmds += [".echo @@end", "bd %d" % n, "g"]
                w = '/w "%s" ' % cond if cond else ""
                f.write('bp%d %s%s "%s"\n' % (n, w, loc, "; ".join(cmds)))
                n += 1
        f.write("g\n.echo @@exit\nq\n")
    out = cdb(cdb_exe, exe, script, work)
    rec, inside = [], False
    for l in out.splitlines():
        l = PROMPT.sub("", l)
        if l.startswith("@@ "):
            inside = True
            rec.append(l)
            continue
        if l.startswith("@@end"):
            inside = False
            continue
        if l.startswith("@@exit"):
            break
        if inside and l.strip():
            if l.startswith("Breakpoint ") and l.endswith(" hit"):
                continue
            if l.startswith("@$goose_"):   # cdb echoes a function alias's (empty) result
                continue
            rec.append(ADDR.sub("0x_", l.rstrip()))
    if not any(l.startswith("@@ ") for l in rec):
        rec.append("!! no stop recorded; cdb said:")
        rec += out.splitlines()[-30:]
    return "\n".join(rec) + "\n"


def main(argv):
    update = "--update" in argv
    names = [a for a in argv if not a.startswith("--")]
    goose = os.environ.get("GOOSE", "goose")
    cdb_exe = os.environ.get("CDB", DEFAULT_CDB)
    work = os.environ.get("NATVIS_WORK") or tempfile.mkdtemp(prefix="goose-natvis-")
    os.makedirs(work, exist_ok=True)
    samples = sorted(f[:-6] for f in os.listdir(SAMPLES) if f.endswith(".goose"))
    if names:
        samples = [s for s in samples if s in names]
    failed = []
    for s in samples:
        src = os.path.join(SAMPLES, s + ".goose")
        local = os.path.join(work, s + ".goose")
        shutil.copyfile(src, local)
        exe = os.path.join(work, s + ".exe")
        rc, out = run([goose, "build", "-g", "-o", exe, local], cwd=work)
        if rc != 0:
            raise SystemExit("build failed: %s\n%s" % (s, out))
        cfile = os.path.join(work, s + ".c")
        rc, out = run([goose, "-g", "-o", cfile, local], cwd=work)
        mpath = cfile[:-2] + ".debug.json"
        if rc != 0 or not os.path.exists(mpath):
            raise SystemExit("no debug map for %s (a goose without patch 0005?)\n%s" % (s, out))
        with open(mpath, encoding="utf-8") as f:
            gmap = json.load(f)
        rc, out = run([sys.executable, GEN, "-q", mpath, "-o", os.path.join(work, s + ".natvis")])
        if rc != 0:
            raise SystemExit("gen_natvis failed for %s\n%s" % (s, out))
        lines = break_lines(src)
        where = locations(cdb_exe, exe, s, lines, work)
        for label, natvis in ((s, True), (s + ".raw", False)):
            got = record(cdb_exe, exe, s, gmap, where, lines, work, natvis)
            exp_path = os.path.join(HERE, label + ".expected")
            if update:
                with open(exp_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(got)
                print("wrote", os.path.basename(exp_path))
                continue
            try:
                with open(exp_path, encoding="utf-8") as f:
                    want = f.read()
            except OSError:
                want = None
            if got == want:
                print("ok  ", label)
                continue
            failed.append(label)
            print("FAIL", label)
            with open(os.path.join(work, label + ".got"), "w", encoding="utf-8", newline="\n") as f:
                f.write(got)
            if want is None:
                print("  no %s; run with --update to create it" % os.path.basename(exp_path))
            else:
                import difflib
                sys.stdout.writelines(difflib.unified_diff(
                    want.splitlines(True), got.splitlines(True), label + ".expected", label + ".got"))
    if not os.environ.get("NATVIS_WORK"):
        shutil.rmtree(work, ignore_errors=True)
    if failed:
        print("%d failed: %s" % (len(failed), " ".join(failed)))
        return 1
    print("all %d samples ok" % len(samples))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
