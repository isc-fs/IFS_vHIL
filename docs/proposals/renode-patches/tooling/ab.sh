#!/usr/bin/env bash
# Interleaved A/B: for each repeat, each variant once.
#   scripts/ab.sh <label> <repeats> "<variants>" [speed.py args]
set -uo pipefail
S=$(cd "$(dirname "$0")/.." && pwd)
label=$1; reps=$2; variants=$3; shift 3
for r in $(seq 1 "$reps"); do
  for v in $variants; do
    echo "== $label rep $r $v $(date +%T)"
    "$S/scripts/speed-variant.sh" "$v" "$label/$v-$r" "$@" 2>&1 | grep -E '^\| [a-z]' || echo "FAILED $v $r"
  done
done
