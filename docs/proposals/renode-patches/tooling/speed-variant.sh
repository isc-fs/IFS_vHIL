#!/usr/bin/env bash
# Run the vHIL's scripts/speed.py (from the docs worktree vhil/, dev) against
# one Renode variant: <name> = release (the image's /opt/renode) or a package
# in renode-fork-pkgs. 4 cores (cpuset 4-7), as VHIL_DOCKER_CPUS=4.
#   scripts/speed-variant.sh <name> <tag> [speed.py args]
set -euo pipefail
S=$(cd "$(dirname "$0")/.." && pwd)
name=${1:?name}; tag=${2:?tag}; shift 2
renode=/opt/renode-variants/$name/renode; [ "$name" = release ] && renode=/opt/renode/renode
mkdir -p "$S/results"
n=$(docker ps --format '{{.Image}} {{.Command}}' | grep -v -c -e 'vhil.server' -e 'vhil.work' -e deploy -e editor -e caddy || true)
[ "$n" = 0 ] || echo "warning: $n other containers running" >&2
docker run --rm --cpus 4 --cpuset-cpus 4-7 ${PRIV:+--privileged} \
    -v "$S/vhil:/work" -v "$S/fw:/fw" -v renode-fork-pkgs:/opt/renode-variants:ro -v "$S/results:/results" \
    ${EXTRA_ENV:-} renode-fork-runtime bash -c '
cd /work
python scripts/speed.py ecu=/fw/ECU08.elf ams=/fw/AMS.elf bootloader=/fw/CAN_BL.elf \
    --renode "$1" --json "/results/$2.json" "${@:3}"' bash "$renode" "$tag" "$@"
