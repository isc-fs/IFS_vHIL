#!/usr/bin/env bash
# scripts/fingerprint-variant.sh <variant> [ms]  -> results/fp/<variant>.json
set -euo pipefail
S=$(cd "$(dirname "$0")/.." && pwd); v=$1; ms=${2:-3500}
renode=/opt/renode-variants/$v/renode; [ "$v" = release ] && renode=/opt/renode/renode
mkdir -p "$S/results/fp"
docker run --rm --cpus 4 --cpuset-cpus 4-7 -v "$S/vhil:/work" -v "$S/fw:/fw" -v "$S/scripts:/scripts:ro" \
    -v renode-fork-pkgs:/opt/renode-variants:ro -v "$S/results:/results" -e RENODE="$renode" renode-fork-runtime \
    bash -c "cd /work && python /scripts/fingerprint.py /results/fp/$v.json $ms"
