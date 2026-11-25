#!/bin/bash
# A point-in-time goose-debug: the compiler built from the debug1 branch with
# the standard library and TinyCC files it was built with, and this repo's
# DAP adapter and debugger modules, in one directory that needs no --stdlib,
# no GOOSE_STDLIB and no build tree:
#
#   dist/goose-debug-<date>-<debug1 sha>-<platform>/
#     bin/goose-debug     the compiler
#     bin/goose-dap       -> ../dap/goose-dap
#     bin/tcclib/         TinyCC's headers and libtcc1 (goose run --jit)
#     stdlib/             found as bin/../stdlib
#     dap/                goose-dap (Python 3; runs the system's lldb-dap)
#     lldb/, gdb/         goose_lldb.py, goose_gdb.py
#     VERSION
#
# goose looks for stdlib/ above its executable and for tcclib/ beside it
# before the paths CMake baked in (StdlibDirs, JitLibPath), so the directory
# can be moved anywhere. goose-dap finds goose_lldb.py at ../lldb and builds
# with ../bin/goose-debug. Point the IDEA plugin's compiler at
# bin/goose-debug, its adapter at bin/goose-dap, and leave its standard
# library setting empty. lldb-dap itself is the system's (Xcode's or LLVM's).
#
#   scripts/make_snapshot.sh [goose-worktree]     (default ~/Dev/Goose/goose-dbg1)
#
# The worktree must be clean: the snapshot is named after its commit. macOS
# and Linux; Windows builds are made by hand for now.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
src=$(cd "${1:-$HOME/Dev/Goose/goose-dbg1}" && pwd)

cd "$src"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "make_snapshot: $src has uncommitted changes" >&2
    exit 1
fi
sha=$(git rev-parse --short HEAD)
branch=$(git branch --show-current)
base=$(git merge-base HEAD origin/master 2>/dev/null || git merge-base HEAD master)
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  platform=macos-arm64 ;;
    Darwin-x86_64) platform=macos-x86_64 ;;
    Linux-x86_64)  platform=linux-x86_64 ;;
    Linux-aarch64) platform=linux-arm64 ;;
    *)             platform=$(uname -s)-$(uname -m) ;;
esac
name=goose-debug-$(date +%Y%m%d)-$sha-$platform
out=$here/dist/$name

# Its own build directory, so the one used day to day is left alone.
build=$src/build-snapshot
mkdir -p "$build"
if ! { cmake -S "$src" -B "$build" -DCMAKE_BUILD_TYPE=Release &&
       cmake --build "$build" --target goose -j "$(getconf _NPROCESSORS_ONLN)"; } > "$build/snapshot.log" 2>&1; then
    tail -n 30 "$build/snapshot.log" >&2
    echo "make_snapshot: the build failed (all of it in $build/snapshot.log)" >&2
    exit 1
fi

rm -rf "$out"
mkdir -p "$out/bin"
cp "$build/goose" "$out/bin/goose-debug"
cp -R "$build/tcclib" "$out/bin/tcclib"
# The committed standard library only, not whatever else lies in the tree.
git -C "$src" archive HEAD stdlib | tar -x -C "$out"
# The debugger side, from this repo as it is (VERSION says if it has changes).
mkdir -p "$out/dap" "$out/lldb" "$out/gdb"
cp "$here"/dap/{goose-dap,goose_dap.py,goose_dap_lldb.py,README.md} "$out/dap/"
cp "$here"/lldb/{goose_lldb.py,README.md} "$out/lldb/"
cp "$here"/gdb/{goose_gdb.py,README.md} "$out/gdb/"
ln -s ../dap/goose-dap "$out/bin/goose-dap"
dbg=$(git -C "$here" rev-parse --short HEAD)
if [ -n "$(git -C "$here" status --porcelain --untracked-files=no -- dap lldb gdb)" ]; then
    dbg="$dbg + local changes"
fi
cat > "$out/VERSION" <<EOF
$("$out/bin/goose-debug" version)
branch:   $branch $(git rev-parse HEAD)
debugger: goose-debugger $dbg
on goose: $(git log -1 --format='%H %s' "$base")
platform: $platform
built:    $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

# Check that it uses its own files: run from elsewhere with no hints (and a
# GOOSE_HOME of its own for the build's intermediates), and see that a JIT
# run and a -g build work and that the -g C's #line directives name the
# snapshot's std.goose.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
printf 'import std;\nfn main() { print(isqrt(1000000)); }\n' > "$tmp/ok.goose"
goose() { (cd "$tmp" && env -u GOOSE_STDLIB -u GOOSE_TCCLIB GOOSE_HOME="$tmp/home" "$out/bin/goose-debug" "$@"); }
goose run --jit ok.goose | grep -qx 1000 || { echo "make_snapshot: the JIT run failed" >&2; exit 1; }
cfile=$(goose build -g --stats -o ok ok.goose 2>&1 | sed -n 's/^wrote \(.*\.c\) (.*/\1/p')
"$tmp/ok" | grep -qx 1000 || { echo "make_snapshot: the -g build failed" >&2; exit 1; }
if ! grep -q "#line [0-9]* \"$out/stdlib/std.goose\"" "$cfile"; then
    echo "make_snapshot: the compiler did not read $out/stdlib" >&2
    exit 1
fi

# And goose-dap's tests, through the snapshot's own adapter and compiler,
# where there is an lldb-dap to run them on.
have_lldb_dap() {
    [ -n "${LLDB_DAP:-}" ] || [ -x /Library/Developer/CommandLineTools/usr/bin/lldb-dap ] ||
        xcrun -f lldb-dap > /dev/null 2>&1 ||
        compgen -c | grep -qE '^lldb-(dap|vscode)(-[0-9]+)?$' ||
        ls /usr/lib/llvm-*/bin/lldb-dap /usr/lib/llvm-*/bin/lldb-vscode > /dev/null 2>&1
}
if have_lldb_dap; then
    if ! env GOOSE="$out/bin/goose-debug" GOOSE_DAP_ADAPTER="$out/bin/goose-dap" \
         python3 "$here/dap/test/run.py" > "$tmp/dap.log" 2>&1 || ! grep -q " 0 failed" "$tmp/dap.log"; then
        cat "$tmp/dap.log" >&2
        echo "make_snapshot: goose-dap's tests failed" >&2
        exit 1
    fi
    dapcheck=$(tail -n 1 "$tmp/dap.log")
else
    dapcheck="skipped (no lldb-dap)"
fi

(cd "$(dirname "$out")" && tar -czf "$name.tar.gz" "$name")

echo "$out"
cat "$out/VERSION"
echo "goose-dap tests: $dapcheck"
echo "archive: $out.tar.gz"
