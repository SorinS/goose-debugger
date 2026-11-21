#!/bin/bash
# Checks a goose built with patches 0001 to 0003 through LLDB in batch
# mode: a breakpoint on a Goose line binds and stops there, the call stack
# names .goose files and lines, and a function Goose would have inlined
# keeps a frame of its own under --debug. Also checks that the C is handed
# back to the .c file after each function, and that goose run --debug
# (TinyCC) still compiles the directives, and that a line is one breakpoint
# location however many C blocks its code goes through.
#
# GOOSE=/path/to/patched/goose tests/line_directives.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
goose=${GOOSE:?set GOOSE to the patched compiler}
work=${TMPDIR:-/tmp}/goose-line-test.$$
mkdir -p "$work" && trap 'rm -rf "$work"' EXIT
cp "$here/area.goose" "$work/"
cd "$work"

fails=0
check() {   # check <what> <pattern> <text>
    if grep -qE -- "$2" <<<"$3"; then echo "ok    $1"
    else echo "FAIL  $1 (no match for: $2)"; fails=$((fails + 1)); fi
}

out=$("$goose" run --debug area.goose 2>&1)
check "goose run --debug runs" '^6$' "$out"

"$goose" --debug -o area.c area.goose || exit 1
c=$(cat area.c)
check "#line names the .goose file" '^#line 9 ".*/area\.goose"$' "$c"
check "#line hands back to the .c file" '^#line [0-9]+ ".*/area\.c"$' "$c"
check "no #line 0" '^#line [1-9]' "$(grep '^#line' area.c | head -1)"
if grep -q '^#line 0' area.c; then echo "FAIL  a #line 0"; fails=$((fails + 1)); fi

"$goose" -o plain.c area.goose || exit 1
if grep -q '^#line' plain.c; then echo "FAIL  #line without --debug"; fails=$((fails + 1))
else echo "ok    no #line without --debug"; fi

"$goose" build --debug -o area area.goose >/dev/null || exit 1
lldb_out=$(lldb --batch -o 'b area.goose:9' -o 'b area.goose:2' -o run -o bt \
    -o 'frame variable' -o continue -o bt -o quit ./area 2>&1)
check "breakpoint binds to a Goose line" 'Breakpoint 1: where = .* at area\.goose:9' "$lldb_out"
check "stops in main at area.goose:9" 'frame #0: .*main_g.* at area\.goose:9' "$lldb_out"
check "area keeps its frame (needs 0002)" 'frame #0: .*area_g\(w_g=0, h_g=2\) at area\.goose:2' "$lldb_out"
check "caller line in the stack (needs 0002)" 'frame #1: .*main_g.* at area\.goose:9' "$lldb_out"
check "Goose locals visible" '\(gs_i64\) total_g = 0' "$lldb_out"

# stops.goose:24 prints values whose formatting goes through C switch
# cases; one Goose line must still be one breakpoint location, hit once.
cp "$here/stops.goose" .
"$goose" build --debug -o stops stops.goose >/dev/null || exit 1
lldb_out=$(lldb --batch -o 'b stops.goose:24' -o run -o continue -o quit ./stops 2>&1)
check "one location per line (needs 0003)" 'Breakpoint 1: where = .* at stops\.goose:24' "$lldb_out"
hits=$(grep -c 'stop reason = breakpoint 1\.' <<<"$lldb_out")
check "one stop per line (needs 0003)" '^1$' "$hits"

# area.goose:9 is the body of a loop of three: three stops, none before
# the loop runs.
lldb_out=$(lldb --batch -o 'b area.goose:9' -o run -o 'frame variable i_g' -o continue \
    -o 'frame variable i_g' -o continue -o 'frame variable i_g' -o continue -o quit ./area 2>&1)
hits=$(grep -c 'stop reason = breakpoint 1\.' <<<"$lldb_out")
check "a loop line stops each time round" '^3$' "$hits"
check "first stop is in the loop" '\(gs_i64\) i_g = 0' "$lldb_out"
check "last stop is the third time round" '\(gs_i64\) i_g = 2' "$lldb_out"

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
