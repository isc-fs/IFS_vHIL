#!/usr/bin/env bash
# Bring deploy/compose.prod.yaml up in local mode and check it end to end
# (docs/deploy.md, "Local test"):
#
#   deploy/smoke.sh [workspace-ref]       default: dev
#
#   1. up: workspace clone, api, 1 worker, editor, proxy (plain HTTP on
#      localhost, non-default ports), backup; dev auth
#   2. /api/health through the proxy; the editor through the proxy's auth gate
#   3. save: an edit of systems/ecu.yaml committed through the API to a
#      branch of the workspace clone; it survives the workspace service
#      running again (fetch + checkout never touch local branches)
#   4. a 500 ms virtual-time run of systems/ecu.yaml, queued through the API
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
editor_port=${SMOKE_EDITOR_PORT:-15443}
fw_volume=${SMOKE_FW_VOLUME-vhil-data}
base="http://localhost:$http_port"

env_file=$(mktemp)
trap 'rm -f "$env_file"' EXIT
cat > "$env_file" <<EOF
VHIL_IMAGE=${SMOKE_IMAGE:-ifs-vhil}
VHIL_TAG=${SMOKE_TAG:-latest}
VHIL_WORKSPACE_REF=$ref
VHIL_AUTH=dev
VHIL_SITE=http://localhost
VHIL_EDITOR_SITE=http://localhost:5443
VHIL_PUBLIC_URL=$base
VHIL_EDITOR_URL=http://localhost:$editor_port
VHIL_HTTP_PORT=$http_port
VHIL_HTTPS_PORT=${SMOKE_HTTPS_PORT:-18443}
VHIL_EDITOR_PORT=$editor_port
VHIL_WORKERS=1
VHIL_WORKER_CPUS=${SMOKE_WORKER_CPUS:-4}
VHIL_BACKUP_DIR=backups
EOF

dc() { docker --context "$context" compose -p "$project" -f "$here/compose.prod.yaml" \
           --env-file "$env_file" "$@"; }
say() { printf '\n== %s\n' "$*"; }
fail() { echo "SMOKE FAILED: $*" >&2; dc ps -a >&2 || true; dc logs --tail 40 >&2 || true; exit 1; }
api() { curl -fsS "$@"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

if [ "${SMOKE_KEEP:-0}" != 1 ]; then
    # down needs the env file (its required variables): remove it last.
    trap 'say "down"; dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$env_file"' EXIT
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
        done
        cp /seed/fw/built.txt /vhil/fw/built.txt' || fail "firmware copy"
fi

say "health through the proxy"
health=$(api "$base/api/health") || fail "no /api/health at $base"
echo "  $health"
[ "$(echo "$health" | json 'd["status"]')" = ok ] || fail "health: $health"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$editor_port/")
[ "$code" = 200 ] || fail "editor through the proxy: HTTP $code"
echo "  editor: HTTP $code"

say "save an edit of systems/ecu.yaml to branch smoke/deploy"
api "$base/api/systems/ecu" | python3 -c '
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
         -d '{"system": "ecu", "scenario": {"kind": "run", "virtual_ms": 500}}' \
         | json 'd["run_id"]') || fail "POST /api/runs"
    for _ in $(seq 1 300); do
        state=$(api "$base/api/runs/$id" | json 'd["state"]')
        case "$state" in queued|running) sleep 2 ;; *) break ;; esac
    done
    echo "  run $id: $state" >&2
    [ "$state" = passed ] || { api "$base/api/runs/$id" >&2; fail "run $id ended $state"; }
    echo "$id"
}

say "run systems/ecu.yaml (500 ms virtual) through the API"
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
