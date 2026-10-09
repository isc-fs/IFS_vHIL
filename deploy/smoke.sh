#!/usr/bin/env bash
# Bring deploy/compose.prod.yaml up in local mode and check it end to end
# (docs/deploy.md, "Local test"):
#
#   deploy/smoke.sh [workspace-ref]       default: dev
#
#   1. up: workspace clone, api, 1 worker, editor, proxy (plain HTTP on
#      127.0.0.1 only, non-default ports), backup; dev auth; empty secret files
#   2. /api/health through the proxy; the editor and its socket.io under /editor/
#      on the same origin, through the proxy's auth gate, and no second port;
#      the hardening holds: uid 10001, read-only root, no secrets in the
#      worker, the worker reaches neither the API, the editor nor the metadata
#      address, and only GitHub through the egress proxy; security headers
#   3. save: an edit of systems/ecu.yaml committed through the API to a
#      branch of the workspace clone; it survives the workspace service
#      running again (fetch + checkout never touch local branches)
#   4. a 3 s virtual-time run of systems/ecu.yaml (past the bootloader's 2 s
#      auto-jump window, so the app's frames are in it), queued through the API
#      and executed by the worker: passes, with CAN frames in its trace
#   5. a snapshot (backup.py once: the DB and the saved branch), one more run,
#      then a restore of the snapshot: the later run is gone, the earlier one
#      is back
#   6. down -v (SMOKE_KEEP=1 leaves it running)
#
# Runs against the Docker context VHIL_DOCKER_CONTEXT (default colima-vhil,
# like scripts/vhil-docker.sh; on a Linux host: default). Firmware: ELFs listed
# in the vhil-data volume's /vhil/fw/built.txt (scripts/vhil-docker.sh fw) are
# copied into the stack's fw volume, so nothing is built; SMOKE_FW_VOLUME=""
# lets the worker build instead. Images: SMOKE_IMAGE (default ifs-vhil, local).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ref=${1:-dev}
context=${VHIL_DOCKER_CONTEXT:-colima-vhil}
project=${SMOKE_PROJECT:-ifs-vhil-smoke}
http_port=${SMOKE_HTTP_PORT:-18080}
fw_volume=${SMOKE_FW_VOLUME-vhil-data}
base="http://localhost:$http_port"

env_file=$(mktemp)
# Secret files are bind-mounted by the Docker daemon, so they live under this
# checkout (inside $HOME: Colima's VM sees it), not in the Mac's $TMPDIR.
secrets=$(mktemp -d "$here/.smoke-secrets.XXXXXX")
for f in session_secret github_client_secret github-app.pem github_token; do
    : > "$secrets/$f"
done
chmod 0755 "$secrets"; chmod 0444 "$secrets"/*   # empty, and readable by uid 10001
# SMOKE_KEEP=1 leaves the stack up, and so the secret files it mounts.
cleanup() { rm -f "$env_file"; [ "${SMOKE_KEEP:-0}" = 1 ] || rm -rf "$secrets"; }
trap cleanup EXIT
cat > "$env_file" <<EOF
VHIL_IMAGE=${SMOKE_IMAGE:-ifs-vhil}
VHIL_TAG=${SMOKE_TAG:-latest}
VHIL_WORKSPACE_REF=$ref
VHIL_AUTH=dev
VHIL_ALLOW_DEV_ON_NETWORK=1
VHIL_SITE=http://localhost
VHIL_PUBLIC_URL=$base
VHIL_HTTP_PORT=$http_port
VHIL_HTTPS_PORT=${SMOKE_HTTPS_PORT:-18443}
VHIL_WORKERS=1
VHIL_WORKER_CPUS=${SMOKE_WORKER_CPUS:-4}
VHIL_BACKUP_DIR=backups
VHIL_SECRETS_DIR=$secrets
VHIL_BIND_ADDR=127.0.0.1
VHIL_HSTS_MAX_AGE=0
EOF

dc() { docker --context "$context" compose -p "$project" -f "$here/compose.prod.yaml" \
           --env-file "$env_file" "$@"; }
say() { printf '\n== %s\n' "$*"; }
fail() { echo "SMOKE FAILED: $*" >&2; dc ps -a >&2 || true; dc logs --tail 40 >&2 || true; exit 1; }
api() { curl -fsS "$@"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

if [ "${SMOKE_KEEP:-0}" != 1 ]; then
    # down needs the env file (its required variables): remove it last.
    trap 'say "down"; dc down -v --remove-orphans >/dev/null 2>&1 || true; cleanup' EXIT
fi

say "up ($project, workspace ref $ref)"
dc up -d --wait --wait-timeout 300 || fail "the stack did not come up healthy"
dc logs workspace | sed 's/^/  /'

if [ -n "$fw_volume" ]; then
    say "firmware: copy the built ELFs from volume $fw_volume"
    dc run --rm --no-deps -T -v "$fw_volume:/seed:ro" --entrypoint bash worker -c '
        set -e
        [ -f /seed/fw/built.txt ] || { echo "no /seed/fw/built.txt" >&2; exit 1; }
        sed -n "s/^[^=]*=//p" /seed/fw/built.txt | sort -u | while read -r elf; do
            [ -f "/seed${elf#/vhil}" ] || continue
            mkdir -p "$(dirname "$elf")"; cp "/seed${elf#/vhil}" "$elf"; echo "  $elf"
            # The CAN contract (.def files) next to it, so runs decode
            # (vhil/candef.py looks for Core/Inc/can/messages above the ELF).
            src=${elf%%/build/*}
            if [ -d "/seed${src#/vhil}/Core/Inc/can" ] && [ ! -d "$src/Core/Inc/can" ]; then
                mkdir -p "$src/Core/Inc"; cp -r "/seed${src#/vhil}/Core/Inc/can" "$src/Core/Inc/"
                echo "  $src/Core/Inc/can"
            fi
        done
        cp /seed/fw/built.txt /vhil/fw/built.txt' || fail "firmware copy"
fi

say "health through the proxy"
health=$(api "$base/api/health") || fail "no /api/health at $base"
echo "  $health"
[ "$(echo "$health" | json 'd["status"]')" = ok ] || fail "health: $health"
code=$(curl -s -o /dev/null -w '%{http_code}' "$base/editor/")
[ "$code" = 200 ] || fail "editor through the proxy (/editor/): HTTP $code"
echo "  editor at $base/editor/: HTTP $code"
# Its socket.io, next to it: the handshake answers through the same gate.
curl -fsS "$base/editor/socket.io/?EIO=4&transport=polling" | grep -q '"sid"' \
    || fail "the editor's socket.io does not answer at $base/editor/socket.io/"
echo "  editor socket.io at $base/editor/socket.io/: handshake ok"
# One origin: the proxy publishes no other port (the editor had 5443).
dc port proxy 5443 >/dev/null 2>&1 && fail "the proxy still publishes the editor's old port 5443"
echo "  no second port"

say "hardening"
in_svc() { dc exec -T "$1" "${@:2}"; }
for svc in api worker editor egress backup; do
    [ "$(in_svc "$svc" id -u)" = 10001 ] || fail "$svc does not run as uid 10001"
    in_svc "$svc" sh -c 'touch /probe 2>/dev/null' && fail "$svc has a writable root filesystem"
    [ "$(in_svc "$svc" sh -c 'grep CapEff /proc/self/status' | awk '{print $2}')" = 0000000000000000 ] \
        || fail "$svc has capabilities"
done
echo "  api worker editor egress backup: uid 10001, read-only root, no capabilities"
in_svc worker sh -c 'ls /run/secrets 2>/dev/null | grep -q .' && fail "the worker has secrets"
in_svc worker sh -c 'env | grep -qi -e secret -e token' && fail "the worker's environment holds a secret"
echo "  worker: no secrets mounted or in its environment"
reach() {   # service host port: exit 0 if a TCP connection to host:port opens
    in_svc "$1" python -c "import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 3)" \
        "$2" "$3" >/dev/null 2>&1
}
reach worker api 8080 && fail "the worker reaches the API"
reach worker editor 5000 && fail "the worker reaches the editor"
reach worker 169.254.169.254 80 && fail "the worker reaches the metadata address"
reach worker 1.1.1.1 443 && fail "the worker has a route off the host"
reach api editor 5000 && fail "the API reaches the editor directly"
echo "  worker: no API, no editor, no metadata, no direct route out; api: no editor"
in_svc worker git ls-remote -q https://github.com/isc-fs/IFS_vHIL.git HEAD >/dev/null \
    || fail "the worker can't reach GitHub through the egress proxy"
in_svc worker python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=10)" \
    >/dev/null 2>&1 && fail "the egress proxy let the worker reach example.com"
echo "  worker egress: github.com yes, example.com no"
headers=$(curl -sS -D - -o /dev/null "$base/")
for h in "X-Content-Type-Options: nosniff" "Referrer-Policy:" "frame-ancestors 'none'"; do
    echo "$headers" | grep -qiF "$h" || fail "the app's responses lack '$h'"
done
editor_headers=$(curl -sS -D - -o /dev/null "$base/editor/")
echo "$editor_headers" | grep -qiF "frame-ancestors 'self'" \
    || fail "the editor may be framed by other origins"
echo "$editor_headers" | grep -qiE "^content-security-policy(-report-only)?: default-src 'self'; script-src 'self'; style-src 'self'" \
    || fail "the editor sends no script-src/style-src 'self' policy"
echo "$editor_headers" | grep -qiF "unsafe-" && fail "the editor's policy allows an unsafe- source"
echo "  headers: nosniff, Referrer-Policy, frame-ancestors (app: none; editor: 'self'), editor CSP"

say "save an edit of systems/ecu.yaml to branch smoke/deploy"
# Edit the file as it is on the branch the save goes to (the editor's flow),
# not the checked-out tree's: with SMOKE_KEEP=1 the workspace volume keeps an
# earlier run's smoke/deploy, whose catalogue may predate a model the tree's
# system now names, and the save is checked against the branch's tip.
api "$base/api/systems/ecu/dataflow?branch=smoke/deploy" | python3 -c '
import json, sys
d = json.load(sys.stdin)
y = d["yaml"] + "# edited by deploy/smoke.sh\n"
json.dump({"yaml": y, "message": "smoke: edit the ECU description", "branch": "smoke/deploy"},
          sys.stdout)' > "$env_file.save"
saved=$(api -X PUT -H 'Content-Type: application/json' "$base/api/systems/ecu" \
        --data-binary @"$env_file.save" | json 'd["ref"]') || fail "PUT /api/systems/ecu"
rm -f "$env_file.save"
echo "  smoke/deploy = $saved"
dc run --rm -T workspace | sed 's/^/  /'
dc run --rm --no-deps -T --entrypoint git api -C /workspace rev-parse refs/heads/smoke/deploy \
    | grep -qx "$saved" || fail "the saved branch did not survive the workspace update"

run() {   # queue a run of systems/ecu.yaml; wait; print its id
    local id state
    id=$(api -X POST -H 'Content-Type: application/json' "$base/api/runs" \
         -d '{"system": "ecu", "scenario": {"kind": "run", "virtual_ms": 3000}}' \
         | json 'd["run_id"]') || fail "POST /api/runs"
    for _ in $(seq 1 300); do
        state=$(api "$base/api/runs/$id" | json 'd["state"]')
        case "$state" in queued|running) sleep 2 ;; *) break ;; esac
    done
    echo "  run $id: $state" >&2
    [ "$state" = passed ] || { api "$base/api/runs/$id" >&2; fail "run $id ended $state"; }
    echo "$id"
}

say "run systems/ecu.yaml (3 s virtual) through the API"
first=$(run)
frames=$(api "$base/api/runs/$first/trace?kinds=frame" | json 'len(d) if isinstance(d, list) else len(d.get("records", []))')
echo "  trace: $frames frames"
[ "$frames" -gt 0 ] || fail "run $first traced no CAN frames"
dc logs worker | grep -F "run $first" | sed 's/^/  /' || fail "the worker log does not show run $first"

count() { api "$base/api/runs?limit=1000" | json 'len(d)'; }

say "backup, another run, restore"
dc exec -T backup python /deploy/backup.py once | tee /dev/stderr | grep -q " 1 branches" \
    || fail "backup once (with the saved branch)"
snap=$(dc exec -T backup python /deploy/backup.py list | head -1)
modes=$(dc exec -T backup stat -c %a "/backups/$snap" "/backups/$snap/vhil.db" | tr '\n' ' ')
[ "$modes" = "700 600 " ] || fail "snapshot $snap is not private (modes $modes)"
echo "  snapshot $snap: directory 700, files 600"
before=$(count)
second=$(run)
[ "$(count)" -eq $((before + 1)) ] || fail "the second run is not in the history"
dc stop api worker >/dev/null
dc exec -T backup python /deploy/backup.py restore "$snap" || fail "restore"
dc start api worker >/dev/null
for _ in $(seq 1 60); do api "$base/api/health" >/dev/null 2>&1 && break; sleep 2; done
after=$(count)
echo "  runs: $before at the snapshot, $((before + 1)) after run $second, $after after restore"
[ "$after" -eq "$before" ] || fail "restore left $after runs, expected $before"
code=$(curl -s -o /dev/null -w '%{http_code}' "$base/api/runs/$second")
[ "$code" = 404 ] || fail "run $second still there after restore (HTTP $code)"
api "$base/api/runs/$first" >/dev/null || fail "run $first missing after restore"

say "restore the saved branches from the snapshot's bundle"
gitws() { dc run --rm --no-deps -T -v "${project}_backups:/backups:ro" --entrypoint git api \
              -C /workspace "$@"; }
gitws branch -q -D smoke/deploy
gitws fetch -q "/backups/$snap/branches.bundle" 'refs/heads/*:refs/heads/*'
gitws rev-parse refs/heads/smoke/deploy | grep -qx "$saved" || fail "branch not restored"
echo "  smoke/deploy = $saved again"

say "SMOKE PASSED"
