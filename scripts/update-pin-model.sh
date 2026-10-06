#!/usr/bin/env bash
# Vendor the MainLite's published pin model (isc-fs/IFS08-ES-MainLite,
# docs/pin-model.md) into catalog/pin-models/mainlite.pins.yaml.
#
#   scripts/update-pin-model.sh <tag>      download release <tag> (pin-model-vX.Y),
#                                          check it against its SHA256SUMS and
#                                          replace the vendored file
#   scripts/update-pin-model.sh --check    warn (exit 0) if a newer pin-model-v*
#                                          release than the vendored one exists
#
# The vendored file is the release's mainlite.pins.yaml, byte for byte, after a
# comment header that records where it came from (repo, tag, sha256).
# tests/unit/test_pin_model.py checks the body still hashes to that sha256.
# Only schema_version 1 is supported (vhil/pin_model.py).
set -euo pipefail
repo=isc-fs/IFS08-ES-MainLite
asset=mainlite.pins.yaml
root=$(cd "$(dirname "$0")/.." && pwd)
dest=$root/catalog/pin-models/$asset
end_marker="# --- end of header: the release asset follows, unchanged ---"

sha256() {
    if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi |
        cut -d' ' -f1
}

vendored_tag() {
    sed -n 's/^# tag: //p' "$dest" | head -1
}

# The newest pin-model-vX.Y tag among the repo's releases (by X, then Y).
latest_tag() {
    curl -fsSL ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} \
        "https://api.github.com/repos/$repo/releases?per_page=100" |
        python3 -c '
import json, re, sys
tags = [r["tag_name"] for r in json.load(sys.stdin) if not r.get("draft")]
found = [(tuple(map(int, m.groups())), t) for t in tags
         if (m := re.fullmatch(r"pin-model-v(\d+)\.(\d+)", t))]
print(max(found)[1] if found else "")'
}

if [ "${1:-}" = --check ]; then
    have=$(vendored_tag)
    if ! latest=$(latest_tag) || [ -z "$latest" ]; then
        echo "::warning::could not list $repo releases to check the pin model"
        exit 0
    fi
    newest=$(printf '%s\n%s\n' "$have" "$latest" | sed 's/^pin-model-v//' | sort -t. -k1,1n -k2,2n | tail -1)
    if [ "pin-model-v$newest" != "$have" ]; then
        echo "::warning::the MainLite pin model $latest is out; catalog/pin-models/$asset is $have. Run scripts/update-pin-model.sh $latest"
    else
        echo "pin model up to date ($have)"
    fi
    exit 0
fi

tag=${1:?usage: $0 <pin-model-vX.Y> | --check}
[[ $tag =~ ^pin-model-v[0-9]+\.[0-9]+$ ]] || { echo "not a pin-model tag: $tag" >&2; exit 1; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
base=https://github.com/$repo/releases/download/$tag
curl -fsSL -o "$tmp/$asset" "$base/$asset"
curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS"
want=$(awk -v f="$asset" '$2 == f || $2 == "*" f {print $1}' "$tmp/SHA256SUMS")
got=$(sha256 "$tmp/$asset")
[ -n "$want" ] || { echo "$asset is not in $tag's SHA256SUMS" >&2; exit 1; }
[ "$want" = "$got" ] || { echo "$asset: sha256 $got, SHA256SUMS says $want" >&2; exit 1; }
grep -qx 'schema_version: 1' "$tmp/$asset" ||
    { echo "$tag: schema_version is not 1; vhil/pin_model.py needs updating first" >&2; exit 1; }
mkdir -p "$(dirname "$dest")"
{
    echo "# Vendored, do not edit: scripts/update-pin-model.sh <tag> replaces it."
    echo "# The MainLite's pin model, the source of truth for which of its pins exist"
    echo "# and leave the module (catalog/boards/mainlite.yaml, vhil/pin_model.py)."
    echo "# repo: $repo"
    echo "# tag: $tag"
    echo "# sha256: $got"
    echo "# url: $base/$asset"
    echo "$end_marker"
    cat "$tmp/$asset"
} > "$dest"
echo "vendored $repo $tag ($got) -> ${dest#"$root"/}"
