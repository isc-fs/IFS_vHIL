#!/usr/bin/env bash
# PR 09's integer path against master's Fraction path, outside Renode (Docker):
#   tooling/clock-fuzz/run.sh [cases]       (default 20000000; ~5 minutes on 4 cores)
# Needs the host clone (RENODE_UPSTREAM) with branches up/master and
# up/09-clock-entry-integer-arithmetic in src/Infrastructure. Exit status 0
# when every case and sequence agrees.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vhil=$(cd "$here/../../../../.." && pwd)
host=${RENODE_UPSTREAM:-$vhil/.claude/worktrees/renode-upstream}/src/Infrastructure
work=$(mktemp -d "${WORK:-$vhil/.claude/worktrees/renode-bench}/clock-fuzz.XXXXXX")
trap 'rm -rf "$work"' EXIT
cs=src/Emulator/Main/Time/BaseClockSource.cs
git -C "$host" show "up/master:$cs" >"$work/orig.cs"
git -C "$host" show "up/09-clock-entry-integer-arithmetic:$cs" >"$work/new.cs"
git -C "$host" show "up/master:src/Emulator/Main/Utilities/Fraction.cs" >"$work/Fraction.cs"
python3 "$here/extract.py" "$work/orig.cs" "$work/new.cs" "$work"
cp "$here/Program.cs" "$here/clock-fuzz.csproj" "$work/"
rm "$work/orig.cs" "$work/new.cs"
docker run --rm --cpus 4 -v "$work:/fuzz" -v renode-up-nuget:/root/.nuget -w /fuzz renode-up-builder \
    dotnet run -c Release -- "${1:-20000000}"
