#!/usr/bin/env bash
# The production API's own clone of IFS_vHIL (docs/deploy.md, "State").
#
# Run by the `workspace` one-shot service before api/worker/editor start:
#
#   - clone VHIL_WORKSPACE_REMOTE into VHIL_WORKSPACE if it holds no clone yet;
#   - `git fetch` origin (branches into refs/remotes/origin/*, and tags);
#   - check out VHIL_WORKSPACE_REF (a branch of origin, a tag or a commit)
#     *detached*, so the working tree (the code, catalogue and systems the
#     app serves and runs) is that ref.
#
# Local branches are never touched: they are the systems users saved from the
# browser (vhil/server/gitstore.py writes refs/heads/<branch> with plumbing),
# some possibly not pushed yet. Only HEAD and the working tree move, and the
# working tree belongs to the deployment: nothing edits it in place.
#
# Credentials: IFS_vHIL is public, so clone and fetch need none. For a private
# remote set VHIL_GITHUB_TOKEN; it is sent as an HTTP header for this run
# only, never written into the clone's config. Pushes of saved branches are
# the API's (GitHub App token, vhil/server/githost.py), not this script's.
set -euo pipefail

dir=${VHIL_WORKSPACE:-/workspace}
remote=${VHIL_WORKSPACE_REMOTE:-https://github.com/isc-fs/IFS_vHIL.git}
ref=${VHIL_WORKSPACE_REF:-dev}

g=(git -c advice.detachedHead=false)
if [ -n "${VHIL_GITHUB_TOKEN:-}" ]; then
    basic=$(printf 'x-access-token:%s' "$VHIL_GITHUB_TOKEN" | base64 | tr -d '\n')
    g+=(-c "http.https://github.com/.extraHeader=Authorization: Basic $basic")
fi
export GIT_TERMINAL_PROMPT=0

if [ ! -e "$dir/.git" ]; then
    if [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
        echo "workspace: $dir is not empty and holds no git clone; refusing to touch it" >&2
        exit 1
    fi
    echo "workspace: cloning $remote into $dir"
    "${g[@]}" clone -q --no-checkout "$remote" "$dir"
    fresh=1
fi

cd "$dir"
"${g[@]}" config remote.origin.url "$remote"
echo "workspace: fetching origin"
"${g[@]}" fetch -q --tags --force origin

# A branch of origin first, then any ref or commit the clone knows.
commit=$("${g[@]}" rev-parse -q --verify "refs/remotes/origin/$ref^{commit}" 2>/dev/null \
         || "${g[@]}" rev-parse -q --verify "$ref^{commit}" 2>/dev/null) || {
    echo "workspace: '$ref' is neither a branch of origin nor a ref or commit" >&2
    exit 1
}
"${g[@]}" checkout -q --detach --force "$commit"
if [ -n "${fresh:-}" ]; then
    # The clone made a local copy of origin's default branch; drop it so the
    # only local branches are the ones users save (it lives on as origin/*).
    "${g[@]}" for-each-ref --format='%(refname:short)' refs/heads/ | xargs -r "${g[@]}" branch -q -D
fi
# Saves write loose objects and nothing else runs gc on this clone.
"${g[@]}" gc --auto -q || true

echo "workspace: $dir at $ref = $("${g[@]}" log -1 --format='%h %s')"
echo "workspace: local branches (saved systems), untouched:"
"${g[@]}" for-each-ref --format='  %(refname:short) %(objectname:short)' refs/heads/
