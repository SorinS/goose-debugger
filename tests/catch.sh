#!/bin/bash
# Checks goose_lldb.py's goose-catch: a program built with -g, run under
# LLDB with goose-catch, stops on a Goose runtime error instead of exiting,
# prints the error as the runtime would, and leaves the Goose frame that
# failed selected, with its locals.
#
# GOOSE=/path/to/patched/goose tests/catch.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
goose=${GOOSE:?set GOOSE to the patched compiler}
work=${TMPDIR:-/tmp}/goose-catch-test.$$
mkdir -p "$work" && trap 'rm -rf "$work"' EXIT
cp "$here"/backtrace/{deep,assert,geo}.goose "$work/"
cd "$work"

fails=0
check() {   # check <what> <pattern> <text>
    if grep -qE -- "$2" <<<"$3"; then echo "ok    $1"
    else echo "FAIL  $1 (no match for: $2)"; fails=$((fails + 1)); fi
}
run() {     # run <program>: lldb's output with goose-catch
    lldb --batch -o "command script import $here/../lldb/goose_lldb.py" -o goose-catch \
        -o run -o 'frame info' -o goose-locals -o quit "./$1" 2>&1
}

for p in deep assert; do "$goose" build -g -o "$p" "$p.goose" >/dev/null || exit 1; done

out=$(run deep)
check "deep: stops on the bounds error" 'goose runtime error: index 3 out of bounds \(length 3\) \(deep\.goose:5\)' "$out"
check "deep: the failing frame is selected" '^frame #[0-9]+: .*leaf_g.* at deep\.goose:5' "$out"
check "deep: its locals" '^xs: i64\[:\] = \[1, 2, 3\]$' "$out"
if grep -q 'exited with status' <<<"$out"; then echo "FAIL  deep: the program exited"; fails=$((fails + 1)); fi

out=$(run assert)
check "assert: stops on the failed assert" 'goose runtime error: assert failed \(geo\.goose:5\)' "$out"
check "assert: the namespaced frame is selected" '^frame #[0-9]+: .*geo_area_g3.* at geo\.goose:5' "$out"
check "assert: its locals" '^w: i64 = -1$' "$out"

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
