#!/bin/bash
# Checks goose run --debug's backtraces (patch 0004): an abort prints the
# Goose frames that led to it after its usual message, innermost first, as
# `  at name (file.goose:LINE)`, with C names shown as Goose names; a crash
# in code TinyCC compiled prints a message and the same frames, and exits
# 255. Without --debug every program behaves as before: the same first
# line and exit code, and no frames.
#
# GOOSE=/path/to/patched/goose tests/backtrace.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
goose=${GOOSE:?set GOOSE to the patched compiler}
work=${TMPDIR:-/tmp}/goose-bt-test.$$
mkdir -p "$work" && trap 'rm -rf "$work"' EXIT
cp "$here"/backtrace/* "$work/"
cd "$work"

fails=0
same() {    # same <what> <expected> <actual>
    if [[ "$2" == "$3" ]]; then echo "ok    $1"
    else
        echo "FAIL  $1"
        diff <(printf '%s\n' "$2") <(printf '%s\n' "$3") | sed 's/^/      /'
        fails=$((fails + 1))
    fi
}

# check <name> <code without --debug> <code with> <expected stderr with
# --debug> [goose options]: the first line under --debug is the one the
# plain run prints, unless the check exists only under --debug.
check() {
    local name=$1 plain_code=$2 debug_code=$3 want=$4
    shift 4
    local plain debug pc dc
    plain=$("$goose" run "$@" "$name.goose" 2>&1 >/dev/null); pc=$?
    debug=$("$goose" run --debug "$@" "$name.goose" 2>&1 >/dev/null); dc=$?
    same "$name: exit code without --debug" "$plain_code" "$pc"
    same "$name: no frames without --debug" "" "$(grep '^  at ' <<<"$plain")"
    if ((plain_code == 1)); then
        same "$name: first line as without --debug" "$(head -1 <<<"$plain")" \
            "$(head -1 <<<"$debug")"
    fi
    same "$name: exit code with --debug" "$debug_code" "$dc"
    same "$name: backtrace" "$want" "$debug"
}

check deep 1 1 "goose runtime error: index 3 out of bounds (length 3) (deep.goose:5)
  at leaf (deep.goose:5)
  at middle (deep.goose:9)
  at outer (deep.goose:16)
  at main (deep.goose:23)"

check assert 1 1 "goose runtime error: assert failed (geo.goose:5)
  at geo::area (geo.goose:5)
  at total (assert.goose:9)
  at main (assert.goose:15)"

check over 1 1 "goose runtime error: assert failed (over.goose:4)
  at f (over.goose:4)
  at f (over.goose:9)
  at main (over.goose:14)"

# The overflow check exists only under --debug: the plain run wraps.
check rec 0 1 "goose runtime error: integer overflow (debug): 3037000500 * 3037000500 at i64 (rec.goose:6)
  at down (rec.goose:6)
  at down (rec.goose:8)
  at down (rec.goose:8)
  at down (rec.goose:8)
  at main (rec.goose:12)"

# Without --debug the null store kills the process by SIGSEGV (128 + 11);
# the innermost frame is the user's C, which has a line but no .goose file.
check crash 139 255 "goose runtime error: invalid memory access
  at crash_store (crash.h:3)
  at poke (crash.goose:8)
  at main (crash.goose:13)" --include crash.h

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
