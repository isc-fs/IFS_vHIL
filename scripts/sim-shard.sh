#!/usr/bin/env bash
# Run test files of tests/sim as separate pytest processes, N at a time
# (#260): one emulated system keeps one to two of a runner's cores busy (its
# CPU threads and Renode's time source), so a 4-core runner runs two at once
# at nearly twice the rate. Each file gets its own log directory; its output
# is printed in full when it ends, one file at a time. Exits non-zero if any
# file failed.
#
#   scripts/sim-shard.sh <N> <log-dir> <file>... [-- <pytest args>]
set -uo pipefail
n=${1:?usage: sim-shard.sh <N> <log-dir> <file>... [-- <pytest args>]}
logs=${2:?log dir}
shift 2
files=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do files+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
mkdir -p "$logs"
out=$(mktemp -d)

run_one() {
    local file=$1; shift
    local name
    name=$(basename "$file" .py)
    local t0=$SECONDS
    python -m pytest "$file" -v --sim-log-dir "$logs/$name" "$@" > "$out/$name.txt" 2>&1
    local rc=$?
    echo "$rc $((SECONDS - t0))" > "$out/$name.rc"
    # Exclusive: whole files, not interleaved lines.
    {
        flock 9
        echo "::group::$file (exit $rc, $((SECONDS - t0)) s)"
        cat "$out/$name.txt"
        echo "::endgroup::"
    } 9> "$out/.lock"
}
export -f run_one
export logs out

printf '%s\n' "${files[@]}" | xargs -P "$n" -I{} bash -c 'run_one "$@"' _ {} "$@"

# Per file: its exit code and wall seconds (also into a GitHub job summary).
failed=0
{
    echo "| file | exit | s |"
    echo "|---|---|---|"
    for f in "${files[@]}"; do
        name=$(basename "$f" .py)
        read -r rc secs < "$out/$name.rc" || { rc=1; secs=-; }
        echo "| $f | $rc | $secs |"
    done
} | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
for f in "${files[@]}"; do
    read -r rc _ < "$out/$(basename "$f" .py).rc" || rc=1
    [ "$rc" = 0 ] || failed=1
done
exit $failed
