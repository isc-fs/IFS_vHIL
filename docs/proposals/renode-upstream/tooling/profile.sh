#!/usr/bin/env bash
# perf-profile a Renode package while speed.py runs one vHIL system and path
# (steady state: sampling starts after <delay> s, for <dur> s), JIT frames
# named from .NET's perf map, and write `perf script` stacks for stacks.py
# (renode-patches/tooling/stacks.py): $WORK/results/stacks-<tag>.txt.
#   tooling/profile.sh <package> <tag> <system> <path> [delay_s] [dur_s]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vhil=$(cd "$here/../../../.." && pwd)
work=${WORK:-$vhil/.claude/worktrees/renode-bench}
p=$1; tag=$2; sys=$3; path=$4; delay=${5:-25}; dur=${6:-15}
docker run --rm --cpus 4 --cpuset-cpus "${BENCH_CPUSET:-4-7}" --privileged -v "$vhil:/work:ro" -v "$work/fw:/fw" \
    -v renode-up-pkgs:/opt/up:ro -v "$work/results:/results" -e HOME=/tmp \
    -e DOTNET_PerfMapEnabled=1 -e DOTNET_EnableWriteXorExecute=0 renode-up-runtime bash -c '
cd /work
python scripts/speed.py ecu=/fw/ECU08.elf ams=/fw/AMS.elf bootloader=/fw/CAN_BL.elf \
    --renode "/opt/up/$1/renode" --systems "$3" --paths "$4" --boot 3 --steady 60 > /tmp/speed.out 2>&1 &
sleep "$5"
pid=$(pgrep -n -f "^/opt/up/$1/renode")
echo "renode pid $pid" >&2
perf record -F 1999 -g -p "$pid" -o /tmp/perf.data -- sleep "$6" 2>/dev/null
perf script -i /tmp/perf.data -F comm,ip,sym 2>/dev/null > "/results/stacks-$2.txt"
pkill -f speed.py || true; pkill -f "^/opt/up/" || true
wc -l "/results/stacks-$2.txt"' bash "$p" "$tag" "$sys" "$path" "$delay" "$dur"
