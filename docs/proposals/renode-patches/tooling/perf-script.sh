#!/usr/bin/env bash
# perf script of results/perf-<tag>.data (with its map) into results/stacks-<tag>.txt
set -euo pipefail
S=$(cd "$(dirname "$0")/.." && pwd); tag=$1; variant=${2:-stock}
docker run --rm -v "$S/results:/r" -v renode-fork-pkgs:/opt/renode-variants:ro renode-fork-runtime bash -c '
d=/usr/lib/debug/opt/renode-variants/$2; mkdir -p $d; cp /opt/renode-variants/syms/libcoreclr.so.dbg $d/libcoreclr.so.debug
pid=$(cat /r/pid-$1 2>/dev/null || echo 13)
cp /r/perf-$1.map /tmp/perf-$pid.map
perf script -i /r/perf-$1.data -F comm,ip,sym 2>/dev/null > /r/stacks-$1.txt' bash "$tag" "$variant"
