#!/usr/bin/env bash
# Build renode master with renode-infrastructure at <infra-ref> (a branch of
# the host clone's src/Infrastructure), Docker only, and optionally run
# Renode's own tests from that build.
#
#   tooling/build.sh <name> <infra-ref> [renode-test args...]
#
# The host clone (RENODE_UPSTREAM, default .claude/worktrees/renode-upstream:
# renode master with its submodules) is copied once into the volume
# renode-up-src (ext4: the portable packaging renames Renode to renode, which
# fails on APFS); later calls only fetch src/Infrastructure's branches from it
# and check <infra-ref> out, so git rewrites exactly the files that differ and
# the incremental build is never stale. The portable headless package goes to
# the volume renode-up-pkgs:/<name> (what docker/renode/build.sh builds, on
# master). Extra arguments go to ./renode-test, run from the build tree after
# the build; its results land in renode-up-pkgs:/<name>.tests/.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vhil=$(cd "$here/../../../.." && pwd)
host=${RENODE_UPSTREAM:-$vhil/.claude/worktrees/renode-upstream}
name=${1:?usage: build.sh <name> <infra-ref> [renode-test args]}
ref=${2:?infra-ref}
shift 2
docker run --rm --cpus "${BUILD_CPUS:-6}" -v "$host:/host:ro" -v renode-up-src:/src -v renode-up-pkgs:/pkgs \
    -v renode-up-nuget:/root/.nuget -v renode-up-cache:/root/.cache renode-up-builder bash -c '
set -euo pipefail
name=$1; ref=$2; shift 2
git config --global --add safe.directory "*"
git config --global advice.detachedHead false
if [ ! -e /src/.renode-root ]; then
    rsync -a --exclude=/output/ /host/ /src/
fi
cd /src
git -C src/Infrastructure fetch -q /host/src/Infrastructure "+refs/heads/*:refs/remotes/host/*"
git -C src/Infrastructure checkout -q -f --detach "host/$ref"
echo "== $name: renode $(git rev-parse --short HEAD), renode-infrastructure $(git -C src/Infrastructure rev-parse --short HEAD) ($ref)"
git -C src/Infrastructure log --oneline -12 | sed "s/^/   /"
fetch=--skip-fetch; [ -f .renode_libs_fetched ] || fetch=
rm -rf output/packages
./build.sh -t --no-gui $fetch
pkg=$(ls output/packages/renode-*-portable-headless.tar.gz)
rm -rf "/pkgs/$name"; mkdir -p "/pkgs/$name"
tar xzf "$pkg" -C "/pkgs/$name" --strip-components=1
echo "renode master $(git rev-parse HEAD) + renode-infrastructure $(git -C src/Infrastructure rev-parse HEAD) ($ref)" > "/pkgs/$name/UPSTREAM_BUILD"
echo "== built /pkgs/$name"
if [ $# -gt 0 ]; then
    rm -rf "/pkgs/$name.tests"
    set +e
    ./renode-test -r "/pkgs/$name.tests" "$@"
    rc=$?
    set -e
    echo "== renode-test exit $rc"
    exit $rc
fi' bash "$name" "$ref" "$@"
