#!/usr/bin/env bash
# Build the host Renode clone (renode/, at whatever branch is checked out) as a
# portable headless package into the Docker volume renode-fork-pkgs:/<name>.
# The build tree lives in the volume renode-fork-src (case-sensitive ext4:
# the portable packaging's Renode/renode mv fails on APFS).
#   scripts/build-variant.sh <name> [extra build.sh args]
set -euo pipefail
S=$(cd "$(dirname "$0")/.." && pwd)
name=${1:?name}; shift
docker run --rm --cpuset-cpus 0-3 -v "$S/renode:/host:ro" -v renode-fork-src:/src -v renode-fork-pkgs:/pkgs \
    -v renode-fork-nuget:/root/.nuget renode-fork-builder bash -c '
set -euo pipefail
git config --global --add safe.directory "*"
rsync -a --exclude=bin/ --exclude=obj/ --exclude=/output/ --exclude="/tools/packaging/renode_*" /host/ /src/
cd /src
fetch=--skip-fetch; [ -f .renode_libs_fetched ] || fetch=
rm -rf tools/packaging/renode_*-portable* output/packages
./build.sh -t --no-gui $fetch "${@:2}"
pkg=$(ls -t output/packages/renode-*-portable-headless.tar.gz | head -1)
rm -rf "/pkgs/$1"; mkdir -p "/pkgs/$1"; tar xzf "$pkg" -C "/pkgs/$1" --strip-components=1; cp "$pkg" "/pkgs/$1.tar.gz"
echo "built /pkgs/$1"; ls "/pkgs/$1" | head -5; du -sh "/pkgs/$1"' bash "$name" "$@"
