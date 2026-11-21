#!/usr/bin/env python3
"""Regression test for goose_lldb.py.

For each test/*.goose: build it with `goose build --debug`, run it under
lldb in batch mode with a breakpoint on every line marked `// BREAK`, and at
each stop record `frame variable` (Goose-named locals only, temporaries tN
and hidden arguments dropped) and `goose-locals`. The record, with addresses
masked, must equal test/<name>.expected (or <name>.<variant>.expected).

  run.py              check every sample
  run.py --update     rewrite the .expected files from this run
  run.py arrays refs  only these samples

Environment:
  GOOSE            the goose compiler (default: goose on PATH); it must emit
                   #line under --debug
  LLDB             lldb (default: lldb on PATH)
  GOOSE_LLDB_WORK  where binaries go (default: a fresh temporary directory)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(os.path.dirname(HERE), "goose_lldb.py")
STOP = os.path.join(HERE, "stop.py")

# Extra runs of a sample, under a name and an environment.
VARIANTS = {
    "structs": [("nocsource", {"GOOSE_LLDB_CSOURCE": "0"})],
    "generics": [("nocsource", {"GOOSE_LLDB_CSOURCE": "0"})],
}

GOOSE_LOCAL = re.compile(r"^\([^)]*\) [A-Za-z_][A-Za-z0-9_]*?_g(_?[0-9]+)? = ")
ADDR = re.compile(r"0x[0-9a-fA-F]+")


def build(goose, src, out):
    try:
        r = subprocess.run([goose, "build", "--debug", "-o", out, src],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        raise SystemExit("cannot run the goose compiler %r (set GOOSE): %s" % (goose, e))
    if r.returncode != 0:
        raise RuntimeError("build failed: %s\n%s" % (src, r.stdout))


def break_lines(src):
    """(line, condition, commands) for each `// BREAK` line.
    `// BREAK when COND` stops only where the C expression COND holds;
    `lldb: a; b` adds the lldb commands a and b to that stop's record."""
    out = []
    with open(src) as f:
        for i, line in enumerate(f):
            j = line.find("// BREAK")
            if j < 0:
                continue
            rest = line[j + len("// BREAK"):].lstrip("0123456789")
            k = rest.find("lldb:")
            cmds = rest[k + 5:].strip() if k >= 0 else ""
            rest = rest[:k] if k >= 0 else rest
            cond = rest.strip()[5:].strip() if rest.strip().startswith("when ") else ""
            out.append((i + 1, cond, cmds))
    return out


def record(lldb, exe, src, env):
    name = os.path.basename(src)
    args = [lldb, "--batch", "-o", "command script import " + MODULE,
            "-o", "command script import " + STOP]
    for line, cond, cmds in break_lines(src):
        c = " -c '%s'" % cond if cond else ""
        args += ["-o", "breakpoint set -f %s -l %d%s -G true -C 'test-stop %s'" % (name, line, c, cmds)]
    args += ["-o", "run", "-o", "quit", exe]
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env=dict(os.environ, **env), cwd=os.path.dirname(exe), timeout=120)
    out = []
    section = None
    for line in r.stdout.splitlines():
        if line.startswith("@@ "):
            out.append(line)
            section = None
        elif line.startswith("-- "):
            section = line[3:]
            out.append(line)
        elif line.startswith("(lldb) ") or line.startswith("Process "):
            section = None
            if "exited with status" in line:
                out.append(re.sub(r"Process [0-9]+", "Process", line))
        elif section == "frame variable" and GOOSE_LOCAL.match(line):
            out.append(ADDR.sub("0x_", line))
        elif section == "goose-locals" and " = " in line:
            out.append(ADDR.sub("0x_", line))
        elif section not in (None, "frame variable", "goose-locals") and line.strip():
            out.append(ADDR.sub("0x_", line))
    if not any(l.startswith("@@ ") for l in out):
        out.append("!! no stop recorded; lldb said:")
        out += r.stdout.splitlines()[-20:]
    return "\n".join(out) + "\n"


def main(argv):
    update = "--update" in argv
    names = [a for a in argv if not a.startswith("--")]
    goose = os.environ.get("GOOSE", "goose")
    lldb = os.environ.get("LLDB", "lldb")
    work = os.environ.get("GOOSE_LLDB_WORK") or tempfile.mkdtemp(prefix="goose-lldb-")
    os.makedirs(work, exist_ok=True)
    samples = sorted(f[:-6] for f in os.listdir(HERE) if f.endswith(".goose"))
    if names:
        samples = [s for s in samples if s in names]
    failed = []
    for s in samples:
        src = os.path.join(HERE, s + ".goose")
        exe = os.path.join(work, s)
        build(goose, src, exe)
        for variant, env in [("", {})] + VARIANTS.get(s, []):
            label = s + ("." + variant if variant else "")
            got = record(lldb, exe, src, env)
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
            else:
                failed.append(label)
                print("FAIL", label)
                with open(os.path.join(work, label + ".got"), "w") as f:
                    f.write(got)
                if want is not None:
                    import difflib
                    sys.stdout.writelines(difflib.unified_diff(
                        want.splitlines(True), got.splitlines(True),
                        label + ".expected", label + ".got"))
                else:
                    print("  no %s; run with --update to create it" % os.path.basename(exp_path))
    if not os.environ.get("GOOSE_LLDB_WORK"):
        shutil.rmtree(work, ignore_errors=True)
    if failed:
        print("%d failed: %s" % (len(failed), " ".join(failed)))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
