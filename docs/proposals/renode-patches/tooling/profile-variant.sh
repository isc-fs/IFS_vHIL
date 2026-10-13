#!/usr/bin/env bash
# perf-profile Renode (one variant) while speed.py runs one system/path:
# samples the steady phase (after a delay) for N seconds, JIT symbols from
# .NET's perf map. Output: results/profile-<tag>.txt (+ perf.data).
#   scripts/profile-variant.sh <name> <tag> <system> <path> [delay_s] [dur_s]
set -euo pipefail
S=$(cd "$(dirname "$0")/.." && pwd)
name=$1; tag=$2; sys=$3; path=$4; delay=${5:-25}; dur=${6:-15}
renode=/opt/renode-variants/$name/renode; [ "$name" = release ] && renode=/opt/renode/renode
mkdir -p "$S/results"
docker run --rm --cpus 4 --cpuset-cpus 4-7 --privileged \
    -v "$S/vhil:/work" -v "$S/fw:/fw" -v renode-fork-pkgs:/opt/renode-variants:ro -v "$S/results:/results" \
    -e DOTNET_PerfMapEnabled=1 -e DOTNET_EnableWriteXorExecute=0 renode-fork-runtime bash -c '
cd /work
d=/usr/lib/debug$(dirname "$1"); mkdir -p "$d"; cp /opt/renode-variants/syms/libcoreclr.so.dbg "$d/libcoreclr.so.debug"; cp /opt/renode-variants/syms/libclrjit.so.dbg "$d/libclrjit.so.debug"
python scripts/speed.py ecu=/fw/ECU08.elf ams=/fw/AMS.elf bootloader=/fw/CAN_BL.elf \
    --renode "$1" --systems "$3" --paths "$4" --boot 3 --steady 60 > /tmp/speed.out 2>&1 &
sleep "$5"
pid=$(pgrep -n -x renode)
echo "renode pid $pid" >&2; echo $pid > /results/pid-$2
perf record -F 1999 -g -p "$pid" -o /tmp/perf.data -- sleep "$6" 2>/dev/null
perf report -i /tmp/perf.data --no-children --sort comm,sym --stdio -g none --percent-limit 0.3 2>/dev/null > /results/profile-$2.txt
perf report -i /tmp/perf.data --no-children --sort sym --stdio -g none --percent-limit 0.2 2>/dev/null > /results/profile-$2-sym.txt
perf report -i /tmp/perf.data --children --sort sym --stdio -g none --percent-limit 1 2>/dev/null > /results/profile-$2-children.txt
perf report -i /tmp/perf.data --no-children --sort comm --stdio -g none 2>/dev/null > /results/profile-$2-threads.txt
cp /tmp/perf.data /results/perf-$2.data; cp /tmp/perf-$pid.map /results/perf-$2.map 2>/dev/null || true
pkill -f speed.py || true; pkill -x renode || true' bash "$renode" "$tag" "$sys" "$path" "$delay" "$dur"
