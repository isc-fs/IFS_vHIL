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
# remote put a token in the file VHIL_GITHUB_TOKEN_FILE names (a compose
# secret; or VHIL_GITHUB_TOKEN itself). It reaches git as an HTTP header
# through git's environment (GIT_CONFIG_COUNT), for this run only: never on a
# command line (readable by every process on the host) and never written into
# the clone's config. Pushes of saved branches are the API's (GitHub App
# token, vhil/server/githost.py), not this script's.
set -euo pipefail

dir=${VHIL_WORKSPACE:-/workspace}
remote=${VHIL_WORKSPACE_REMOTE:-https://github.com/isc-fs/IFS_vHIL.git}
ref=${VHIL_WORKSPACE_REF:?set VHIL_WORKSPACE_REF to the tag or commit to deploy}

token=${VHIL_GITHUB_TOKEN:-}
if [ -n "${VHIL_GITHUB_TOKEN_FILE:-}" ]; then
    token=$(tr -d '[:space:]' < "$VHIL_GITHUB_TOKEN_FILE")
fi
g=(git -c advice.detachedHead=false)
if [ -n "$token" ]; then
    n=${GIT_CONFIG_COUNT:-0}
    export "GIT_CONFIG_KEY_$n=http.https://github.com/.extraHeader"
    export "GIT_CONFIG_VALUE_$n=Authorization: Basic $(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"
    export GIT_CONFIG_COUNT=$((n + 1))
fi
unset token VHIL_GITHUB_TOKEN
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
