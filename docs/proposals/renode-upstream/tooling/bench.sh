#!/usr/bin/env bash
# Interleaved A/B of Renode packages built by build.sh on generic workloads
# (bench/*.resc: upstream's bus-performance loop, the register framework's
# read and write, an STM32 timer's update path, a pre-opcode execution hook
# on a loop (none / every call / excluding itself: hook-exclude needs PR 11),
# three of Renode's own demos, two machines on the master time source, on
# slave time sources, or on slave time sources on the master's thread
# (multi-*-inline needs PR 13), an MCAN in loopback (PR 15), a memory
# access hook (none / every access / IO accesses only: memhook-io needs
# PR 17)). For each repeat, each
# benchmark runs once on each package, in turn, in its own container limited
# to 4 cores (--cpuset-cpus 4-7, as the vHIL study measured).
#
#   tooling/bench.sh <out.jsonl> <repeats> "<packages>" ["<benchmarks>"]
#
# Each run appends one JSON line: package, benchmark, repeat, the host wall
# seconds for the measured virtual time, the bytes allocated, gen-0
# collections and process CPU seconds during it, and whether another
# container was running when it ended (idle.sh: each run first waits until
# none is).
# tooling/summarize_bench.py makes the table.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/idle.sh"
out=${1:?out.jsonl}; reps=${2:?repeats}; pkgs=${3:?packages}
benches=${4:-"bus-trivial register-read register-write stm32-timer hook-none hook-always hook-exclude demo-stm32f4_discovery demo-nucleo_h753zi_hubris demo-nrf52840 multi-spin-shared multi-spin-slaves multi-spin-inline multi-ble-shared multi-ble-slaves multi-ble-inline mcan-loopback memhook-none memhook-all memhook-io"}
for r in $(seq 1 "$reps"); do
  for b in $benches; do
    for p in $pkgs; do
      wait_idle
      line=$(docker run --rm --cpus 4 --cpuset-cpus "${BENCH_CPUSET:-4-7}" -v "$here/bench:/bench:ro" \
          -v renode-up-pkgs:/pkgs:ro -v renode-up-cache:/root/.cache -w "/pkgs/$p" renode-up-builder \
          bash -c "timeout 900 ./renode --disable-gui --console --plain --hide-log /bench/$b.resc 2>&1" | tr -d '\r' | grep '^BENCH' | tail -1)
      if [ -z "$line" ]; then echo "FAILED $p $b $r" >&2; continue; fi
      wall=$(sed -E 's/.*wall=([0-9.]+).*/\1/' <<<"$line")
      alloc=$(sed -E 's/.*alloc=([0-9]+).*/\1/' <<<"$line")
      gen0=$(sed -E 's/.*gen0=([0-9]+).*/\1/' <<<"$line")
      cpu=$(sed -nE 's/.*cpu=([0-9.]+).*/\1/p' <<<"$line")
      printf '{"package":"%s","bench":"%s","rep":%d,"wall":%s,"alloc":%s,"gen0":%s,"cpu":%s,"contended":%s}\n' "$p" "$b" "$r" "$wall" "$alloc" "$gen0" "${cpu:-null}" "$(contended)" | tee -a "$out"
    done
  done
done
