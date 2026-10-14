#!/usr/bin/env bash
# Build Renode from source with the vHIL's patch queue into <dest> (a
# portable headless package, the layout of the release's portable tarball:
# <dest>/renode, <dest>/renode-test, ...), plus <dest>/VHIL_RENODE_BUILD, one
# line naming the release, its commit and the patch set.
#
#   docker/renode/build.sh <dest>
#
# Runs in docker/Dockerfile's renode-build stage (the .NET 8 SDK image, by
# digest), on a case-sensitive file system: the portable packaging renames
# Renode to renode, which fails on APFS. Every input is checked against
# pins.env and submodules.txt; any mismatch, and any patch that doesn't
# apply, stops the build. README.md has the why and the rebase recipe.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
dest=${1:?usage: build.sh <dest>}
# shellcheck source=pins.env
. "$here/pins.env"
fail() { echo "docker/renode/build.sh: FATAL: $*" >&2; exit 1; }

src=${RENODE_SRC:-/tmp/renode-src}
rm -rf "$src"
git init -q "$src"
cd "$src"
git config --global advice.detachedHead false

# The release tag, at the commit pinned.
git fetch -q --depth 1 https://github.com/renode/renode.git "refs/tags/v$RENODE_VERSION"
got=$(git rev-parse 'FETCH_HEAD^{commit}')
[ "$got" = "$RENODE_COMMIT" ] || fail "v$RENODE_VERSION is $got, pins.env says $RENODE_COMMIT"
git checkout -q FETCH_HEAD

# Its submodules, at the commits the tag records, which must be submodules.txt's.
git submodule update -q --init --recursive --depth 1 --jobs 8
git submodule foreach --recursive --quiet 'echo "$(git rev-parse HEAD) $displaypath"' >/tmp/submodules.txt
diff -u "$here/submodules.txt" /tmp/submodules.txt \
    || fail "the submodules are not submodules.txt's (above: - pinned, + fetched)"

# renode-resources at its pin (build.sh's own fetch would take master).
git init -q lib/resources
git -C lib/resources fetch -q --depth 1 https://github.com/renode/renode-resources.git "$RENODE_RESOURCES_COMMIT"
git -C lib/resources checkout -q FETCH_HEAD
[ "$(git -C lib/resources rev-parse HEAD)" = "$RENODE_RESOURCES_COMMIT" ] || fail "renode-resources is not at its pin"
touch .renode_libs_fetched

# The patch queue, in order. Each must apply cleanly; the result must be the
# tree pinned, so a patch edited without updating pins.env fails here too.
# patches/*.patch go to renode-infrastructure, patches/tlib/*.patch to tlib
# (its submodule, a repository of its own).
patches=("$here"/patches/*.patch)
[ -e "${patches[0]}" ] || fail "no patches in $here/patches"
for p in "${patches[@]}"; do
    git -C src/Infrastructure apply --check "$p" || fail "$(basename "$p") does not apply to renode-infrastructure"
    git -C src/Infrastructure apply "$p"
    echo "applied $(basename "$p")"
done
git -C src/Infrastructure add -A
tree=$(git -C src/Infrastructure write-tree)
[ "$tree" = "$RENODE_INFRA_PATCHED_TREE" ] \
    || fail "patched renode-infrastructure is tree $tree, pins.env says $RENODE_INFRA_PATCHED_TREE"
tlib=src/Infrastructure/src/Emulator/Cores/tlib
tlib_patches=()
for p in "$here"/patches/tlib/*.patch; do
    [ -e "$p" ] || continue
    tlib_patches+=("$p")
    git -C "$tlib" apply --check "$p" || fail "tlib/$(basename "$p") does not apply to tlib"
    git -C "$tlib" apply "$p"
    echo "applied tlib/$(basename "$p")"
done
git -C "$tlib" add -A
tree=$(git -C "$tlib" write-tree)
[ "$tree" = "$RENODE_TLIB_PATCHED_TREE" ] \
    || fail "patched tlib is tree $tree, pins.env says $RENODE_TLIB_PATCHED_TREE"

# Portable headless package (no GUI: the vHIL runs Renode --disable-gui),
# its assemblies compiled ReadyToRun (crossgen2): native code in the package,
# so a start JITs only what tiering later promotes, not every method it runs
# (docs/performance.md). Same IL and the same tiered re-JIT of hot methods:
# what a run computes is unchanged.
./build.sh -t --no-gui --skip-fetch -- p:PublishReadyToRun=true
pkg=$(ls output/packages/renode-*-portable-headless.tar.gz)
[ "$(echo "$pkg" | wc -l)" = 1 ] || fail "expected one package, got: $pkg"

rm -rf "$dest"
mkdir -p "$dest"
tar xzf "$pkg" -C "$dest" --strip-components=1
[ -x "$dest/renode" ] && [ -x "$dest/renode-test" ] || fail "the package has no renode / renode-test"
queue=$(cat "${patches[@]}" ${tlib_patches[@]+"${tlib_patches[@]}"} | sha256sum | cut -c1-12)
names=$( (for p in "${patches[@]}"; do basename "$p" .patch | cut -c1-4; done
          for p in ${tlib_patches[@]+"${tlib_patches[@]}"}; do echo "tlib/$(basename "$p" .patch | cut -c1-4)"; done) | paste -sd, -)
echo "renode $RENODE_VERSION (${RENODE_COMMIT:0:12}) + vhil patches $VHIL_PATCH_SET [$names] queue $queue" \
    >"$dest/VHIL_RENODE_BUILD"
cat "$dest/VHIL_RENODE_BUILD"
"$dest/renode" --version
rm -rf "$src"
