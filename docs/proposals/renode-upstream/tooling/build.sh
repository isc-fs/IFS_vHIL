#!/usr/bin/env bash
# Build renode with renode-infrastructure at <infra-ref> (a branch of the host
# clone's src/Infrastructure) and tlib at the commit that branch records,
# Docker only, and optionally run Renode's own tests from that build.
#
#   [RENODE_REF=<renode-ref>] tooling/build.sh <name> <infra-ref> [renode-test args...]
#
# The host clone (RENODE_UPSTREAM, default .claude/worktrees/renode-upstream:
# renode master with its submodules) is copied once into the volume
# renode-up-src (ext4: the portable packaging renames Renode to renode, which
# fails on APFS); later calls only fetch the branches of renode (RENODE_REF,
# default master: the tests and scripts), src/Infrastructure and its tlib
# from it and check them out, so git rewrites exactly the files that differ
# and the incremental build is never stale. A tlib change is a branch of the
# host's tlib plus a commit on <infra-ref> moving the submodule to it. The
# portable headless package goes to the volume renode-up-pkgs:/<name> (what
# docker/renode/build.sh builds, on master). Extra arguments go to ./renode-test, run from the build tree after
# the build; its results land in renode-up-pkgs:/<name>.tests/. RENODE_TEST_SLAVES, if set, reaches the
# tests' Renode (tests-slaves.txt: a test-only commit on PR 13 reads it).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vhil=$(cd "$here/../../../.." && pwd)
host=${RENODE_UPSTREAM:-$vhil/.claude/worktrees/renode-upstream}
name=${1:?usage: build.sh <name> <infra-ref> [renode-test args]}
ref=${2:?infra-ref}
shift 2
docker run --rm --cpus "${BUILD_CPUS:-6}" -v "$host:/host:ro" -v renode-up-src:/src -v renode-up-pkgs:/pkgs \
    -v renode-up-nuget:/root/.nuget -v renode-up-cache:/root/.cache -e RENODE_REF="${RENODE_REF:-master}" \
    -e RENODE_TEST_SLAVES renode-up-builder bash -c '
set -euo pipefail
name=$1; ref=$2; shift 2
git config --global --add safe.directory "*"
git config --global advice.detachedHead false
if [ ! -e /src/.renode-root ]; then
    rsync -a --exclude=/output/ /host/ /src/
fi
cd /src
tlib=src/Infrastructure/src/Emulator/Cores/tlib
git fetch -q --no-recurse-submodules /host "+refs/heads/*:refs/remotes/host/*"
git checkout -q -f --detach "host/$RENODE_REF"
git -C src/Infrastructure fetch -q --no-recurse-submodules /host/src/Infrastructure "+refs/heads/*:refs/remotes/host/*"
git -C src/Infrastructure checkout -q -f --detach "host/$ref"
git -C $tlib fetch -q /host/$tlib "+refs/heads/*:refs/remotes/host/*"
git -C $tlib checkout -q -f --detach "$(git -C src/Infrastructure ls-tree HEAD src/Emulator/Cores/tlib | cut -d" " -f3 | cut -f1)"
echo "== $name: renode $(git rev-parse --short HEAD) ($RENODE_REF), renode-infrastructure $(git -C src/Infrastructure rev-parse --short HEAD) ($ref), tlib $(git -C $tlib rev-parse --short HEAD)"
git -C src/Infrastructure log --oneline -20 | sed "s/^/   /"
fetch=--skip-fetch; [ -f .renode_libs_fetched ] || fetch=
rm -rf output/packages
./build.sh -t --no-gui $fetch
pkg=$(ls output/packages/renode-*-portable-headless.tar.gz)
rm -rf "/pkgs/$name"; mkdir -p "/pkgs/$name"
tar xzf "$pkg" -C "/pkgs/$name" --strip-components=1
echo "renode $(git rev-parse HEAD) ($RENODE_REF) + renode-infrastructure $(git -C src/Infrastructure rev-parse HEAD) ($ref) + tlib $(git -C $tlib rev-parse HEAD)" > "/pkgs/$name/UPSTREAM_BUILD"
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
