#!/bin/sh
# Builds goose-dap into dap/build/, laid out as a snapshot has it:
#
#   build/bin/goose-dap         the adapter
#   build/lldb/goose_lldb.py    the value formatters it loads into lldb-dap
#
# so that the built tree runs (and dap/test/run.py tests it) with no
# environment variable set.
#
#   dap/scripts/build.sh [compiler]
#
# The compiler is the first argument, else $GOOSE, else this repo's
# bin/goose-debug, else goose-debug or goose on PATH (building the adapter
# needs no -g; what it debugs does). OUT is the directory to build into
# (default dap/build/); scripts/make_snapshot.sh points it at a snapshot.
# gslib is fetched on the first build.
set -eu

DAP=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(cd "$DAP/.." && pwd)
GOOSE=${1:-${GOOSE:-}}
if [ -z "$GOOSE" ]; then
    if [ -x "$REPO/bin/goose-debug" ]; then GOOSE=$REPO/bin/goose-debug
    elif command -v goose-debug > /dev/null 2>&1; then GOOSE=goose-debug
    else GOOSE=goose
    fi
fi
OUT=${OUT:-$DAP/build}

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) EXE=.exe ;;
    *) EXE= ;;
esac

mkdir -p "$OUT/bin" "$OUT/lldb"
"$GOOSE" build -o "$OUT/bin/goose-dap$EXE" "$DAP/src/main.goose"
cp "$REPO/lldb/goose_lldb.py" "$OUT/lldb/goose_lldb.py"
echo "built $OUT/bin/goose-dap$EXE"
