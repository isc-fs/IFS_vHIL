#!/usr/bin/env bash
# The vHIL's own workloads on Renode packages built by build.sh: interleaved
# scripts/speed.py runs (every system, both paths, boot + steady) and the
# byte-for-byte fingerprint of the study (renode-patches/tooling/fingerprint.py),
# in the ifs-vhil image (its Python, arm-none-eabi, models), the repository
# mounted read-only, 4 cores (--cpuset-cpus 4-7) as the study measured.
#
#   tooling/vhil-ab.sh speed <label> <repeats> "<packages>" [speed.py args]
#   tooling/vhil-ab.sh fingerprint <package> [ms] [out-name]
#
# FP_SYSTEMS (fingerprint.py's systems; default all it knows: since dev's
# #298 ecu, ams, ecu-ams, udv, ecu-ams-udv and ecu-ams-ecu) and
# VHIL_INLINE_SLAVES (VhilMachine: 0 keeps every board's slave time source on
# its own thread on a build with PR 13) pass through.
#
# Firmware: $WORK/fw/{ECU08,AMS,uDV,CAN_BL}.elf; results in $WORK/results
# (WORK defaults to .claude/worktrees/renode-bench, a path Docker can mount).
# VHIL_REPO: the vHIL checkout whose models, systems, speed.py and
# fingerprint.py run (default this one; e.g. a worktree at dev for systems
# and models newer than this branch).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vhil=$(cd "$here/../../../.." && pwd)
. "$here/idle.sh"
work=${WORK:-$vhil/.claude/worktrees/renode-bench}
repo=${VHIL_REPO:-$vhil}
run() {
    wait_idle
    docker run --rm --cpus 4 --cpuset-cpus "${BENCH_CPUSET:-4-7}" -v "$repo:/work:ro" -v "$work/fw:/fw" \
        -v "$repo/docs/proposals/renode-patches/tooling:/scripts:ro" -v renode-up-pkgs:/opt/up:ro \
        -v "$work/results:/results" -e HOME=/tmp "$@"
}
case ${1:?speed|fingerprint} in
speed)
    label=${2:?label}; reps=${3:?repeats}; pkgs=${4:?packages}; shift 4
    mkdir -p "$work/results/$label"
    for r in $(seq 1 "$reps"); do
        for p in $pkgs; do
            echo "== $label rep $r $p $(date +%T)"
            run ifs-vhil:latest bash -c 'cd /work && udv=(); [ -f /fw/uDV.elf ] && udv=(udv=/fw/uDV.elf)
                python scripts/speed.py ecu=/fw/ECU08.elf ams=/fw/AMS.elf "${udv[@]}" \
                bootloader=/fw/CAN_BL.elf --renode "/opt/up/$1/renode" --json "/results/$2/$1-$3.json" "${@:4}"' \
                bash "$p" "$label" "$r" "$@" 2>&1 | grep -E '^\| [a-z]' || echo "FAILED $p $r"
            [ "$(contended)" = false ] || echo "CONTENDED $p $r (another container was running)"
        done
    done ;;
fingerprint)
    p=${2:?package}; ms=${3:-3500}; out=${4:-$p}
    mkdir -p "$work/results/fp"
    run -e RENODE="/opt/up/$p/renode" -e FP_SYSTEMS -e VHIL_INLINE_SLAVES ifs-vhil:latest \
        bash -c "cd /work && python /scripts/fingerprint.py /results/fp/$out.json $ms ${FP_SYSTEMS:-}" ;;
esac
