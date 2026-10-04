#!/usr/bin/env bash
# Run the vHIL in Docker: every CI job, locally, with nothing installed on
# the host but Docker. On macOS it runs in a Colima VM (profile `vhil`)
# because Docker Desktop's kernel has no vcan; on Linux, set
# VHIL_DOCKER_CONTEXT=default and load vcan on the host.
#
#   scripts/vhil-docker.sh vm                    create/start the VM, with vcan
#   scripts/vhil-docker.sh image                 (re)build the images (docker/)
#   scripts/vhil-docker.sh fw [ecu ams ...]      build firmware from systems/<s>.yaml
#   scripts/vhil-docker.sh unit                  tests/unit + validate every system
#   scripts/vhil-docker.sh smoke <ecu|ams>       tests/<s>_smoke.robot
#   scripts/vhil-docker.sh sim [pytest args]     tests/sim in virtual time
#   scripts/vhil-docker.sh coverage [glob] [pytest args]
#                                                firmware coverage of tests/sim/<glob>.py
#                                                (default test_*) into results/coverage
#   scripts/vhil-docker.sh speed [mips ...]      scripts/speed.py on the ECU
#   scripts/vhil-docker.sh ifs-hil [suite] [pytest args]
#                                                IFS_HIL's ECU suite over vcan
#                                                (VHIL_SYSTEM=systems/ecu-ams.yaml: with the AMS)
#   scripts/vhil-docker.sh editor                system editor on http://localhost:5050
#   scripts/vhil-docker.sh server                web app API + shell on http://localhost:8080
#                                                (M5; full stack: docker/compose.yaml)
#   scripts/vhil-docker.sh worker [--once ...]   run worker for the server's queue (vhil.worker)
#   scripts/vhil-docker.sh editor-check [systems...]
#                                                Pipeline Manager loads every system's graph
#   scripts/vhil-docker.sh shell                 a shell in the container
#   scripts/vhil-docker.sh run <cmd...>          any command in the container
#
# Firmware, the IFS_HIL checkout and their build trees live in the Docker
# volume `vhil-data` (/vhil in the container). IFS_HIL_REF picks IFS_HIL's
# ref (default dev); FW_REFS passes --ref pairs to the firmware build
# (e.g. FW_REFS="ecu=feat/x").
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
profile=${VHIL_COLIMA_PROFILE:-vhil}
context=${VHIL_DOCKER_CONTEXT:-colima-$profile}
base_image=${VHIL_IMAGE:-ifs-vhil}
editor_image=$base_image-editor
image=$base_image
docker=(docker --context "$context")
run_args=(--privileged --network host)

vm() {
    if ! colima status "$profile" >/dev/null 2>&1; then
        colima start "$profile" --cpu "${VHIL_CPUS:-8}" --memory "${VHIL_MEMORY:-8}" \
            --disk 40 --vm-type vz --arch aarch64
    fi
    # Colima's Ubuntu image ships without linux-modules-extra (vcan), and the
    # archive only carries it for the current kernel: move to that kernel
    # once, then restart into it.
    if ! colima ssh -p "$profile" -- sudo modprobe vcan 2>/dev/null; then
        echo "installing a kernel with vcan in the $profile VM (once)" >&2
        colima ssh -p "$profile" -- bash -c '
            set -e
            sudo apt-get update -qq
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq linux-generic
            v=$(ls /lib/modules | sort -V | tail -1)
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "linux-modules-extra-$v"
            echo vcan | sudo tee /etc/modules-load.d/vcan.conf >/dev/null'
        colima restart "$profile"
        colima ssh -p "$profile" -- sudo modprobe vcan
    fi
}

build_images() {
    "${docker[@]}" build -t "$base_image" "$repo/docker"
    "${docker[@]}" build -t "$editor_image" --build-arg BASE="$base_image" \
        -f "$repo/docker/editor.Dockerfile" "$repo/docker"
}

ensure_image() {
    "${docker[@]}" image inspect "$image" >/dev/null 2>&1 || build_images
}

# Run a script in the container. Host networking puts can0..can2 in the
# VM's namespace, where they outlive the container; --privileged lets the
# container create them.
in_container() {
    ensure_image
    local tty=()
    [ -t 0 ] && [ -t 1 ] && tty=(-it)
    "${docker[@]}" run --rm ${tty[@]+"${tty[@]}"} "${run_args[@]}" \
        -v "$repo:/work" -v vhil-data:/vhil \
        -e IFS_HIL_REF="${IFS_HIL_REF:-dev}" -e FW_REFS="${FW_REFS:-}" \
        -e VHIL_SYSTEM="${VHIL_SYSTEM:-}" \
        -e EDITOR_URL="http://localhost:${VHIL_EDITOR_PORT:-5050}" \
        "$image" bash -c "$1" vhil "${@:2}"
}

# ELF paths of the last firmware build, as board=elf lines.
prelude='set -euo pipefail
elf() { sed -n "s/^$1=//p" /vhil/fw/built.txt 2>/dev/null | tail -1; }
need_elf() { [ -n "$(elf "$1")" ] || { echo "no $1 firmware: run \`scripts/vhil-docker.sh fw $1\` first" >&2; exit 1; }; }
'

cmd=${1:-help}; shift || true
case "$cmd" in
vm) vm ;;
image) build_images ;;
fw)
    [ $# -gt 0 ] || set -- ecu ams ecu-bl
    in_container "$prelude"'
        refs=(); for r in $FW_REFS; do refs+=(--ref "$r"); done
        mkdir -p /vhil/fw
        for s in "$@"; do
            python -m vhil.system build "systems/$s.yaml" --workdir /vhil/fw "${refs[@]}" \
                | tee -a /vhil/fw/built.txt
        done' "$@" ;;
unit)
    in_container "$prelude"'
        python -m pytest tests/unit -q
        for s in systems/*.yaml; do python -m vhil.system validate "$s"; done' ;;
smoke)
    sys=${1:?usage: smoke <ecu|ams>}
    in_container "$prelude"'
        need_elf "$1"
        python -m vhil.system render "systems/$1.yaml" -o /tmp/system.resc
        renode-test "tests/$1_smoke.robot" --variable ELF:"$(elf "$1")" \
            --variable RESC:/tmp/system.resc -r "results/smoke-$1"' "$sys" ;;
sim)
    in_container "$prelude"'
        need_elf ecu; need_elf ams
        VHIL_ECU_ELF=$(elf ecu) VHIL_AMS_ELF=$(elf ams) VHIL_CAN_BOOTLOADER_ELF=$(elf ecu.bootloader) \
            python -m pytest tests/sim -v --sim-log-dir results/sim-logs "$@"' "$@" ;;
coverage)
    in_container "$prelude"'
        need_elf ecu; need_elf ams
        glob=${1:-test_*}; glob=${glob%.py}; shift || true
        files=$(ls tests/sim/$glob.py)
        VHIL_ECU_ELF=$(elf ecu) VHIL_AMS_ELF=$(elf ams) VHIL_CAN_BOOTLOADER_ELF=$(elf ecu.bootloader) \
            python -m pytest $files -v --sim-log-dir results/sim-logs \
                --vhil-coverage results/coverage "$@"' "$@" ;;
speed)
    [ $# -gt 0 ] || set -- 100 528
    in_container "$prelude"'
        need_elf ecu; nproc
        python scripts/speed.py systems/ecu.yaml "ecu=$(elf ecu)" --mips "$@"' "$@" ;;
ifs-hil)
    in_container "$prelude"'
        need_elf ecu
        # The other boards of $VHIL_SYSTEM run their last-built images.
        export VHIL_SYSTEM=${VHIL_SYSTEM:-systems/ecu.yaml}
        VHIL_FIRMWARE=
        for b in $(python -c "import sys; from vhil.system import System; print(*System(sys.argv[1]).images())" "$VHIL_SYSTEM"); do
            [ "$b" = ecu ] && continue
            need_elf "$b"; VHIL_FIRMWARE+="$b=$(elf "$b") "
        done
        export VHIL_FIRMWARE
        for i in 0 1 2; do
            ip link show "can$i" >/dev/null 2>&1 || ip link add "can$i" type vcan
            ip link set "can$i" txqueuelen 1000 up
        done
        if [ -d /vhil/IFS_HIL/.git ]; then
            git -C /vhil/IFS_HIL fetch -q origin "$IFS_HIL_REF"
            git -C /vhil/IFS_HIL checkout -q --detach FETCH_HEAD
        else
            git clone -q -b "$IFS_HIL_REF" https://github.com/isc-fs/IFS_HIL /vhil/IFS_HIL
        fi
        mkdir -p results/ifs-hil
        candump -L -t a can0 can1 can2 > results/ifs-hil/candump.log 2>&1 &
        set +e
        scripts/run-ifs-hil.sh /vhil/IFS_HIL "$(elf ecu)" "${1:-smoke}" \
            --vhil-log /work/results/ifs-hil/vhil-renode.log \
            --junitxml=/work/results/ifs-hil/junit.xml "${@:2}" | tee results/ifs-hil/pytest.txt
        rc=${PIPESTATUS[0]}
        if grep -q "SocketCAN interface .* not present" results/ifs-hil/pytest.txt; then
            echo "error: tests skipped for a missing SocketCAN interface" >&2; exit 1
        fi
        exit $rc' "$@" ;;
editor)
    # Its own network: the UI port is published (Colima forwards it to the
    # Mac); Run needs no vcan. Images come from the last `fw`. Not 5000 on
    # the host: macOS's AirPlay Receiver holds it.
    image=$editor_image run_args=(-p "${VHIL_EDITOR_PORT:-5050}:5000")
    in_container "$prelude"'
        export VHIL_ECU_ELF=$(elf ecu) VHIL_AMS_ELF=$(elf ams) PM_HOST=0.0.0.0
        exec scripts/editor.sh' ;;
editor-check)
    # Pipeline Manager's ./validate (its frontend's load) on each system's
    # dataflow, in the editor image where the patched checkout lives.
    image=$editor_image
    in_container 'exec python -m vhil.editor check "$@"' "$@" ;;
server)
    # A local checkout: dev mode (no login) unless VHIL_AUTH says otherwise.
    # The server refuses dev mode off loopback, and in the container it
    # listens on 0.0.0.0, so the port is published on the host's 127.0.0.1
    # only and VHIL_ALLOW_DEV_ON_NETWORK=1 says so (vhil/server/config.py).
    export VHIL_AUTH=${VHIL_AUTH:-dev}
    run_args=(-p "127.0.0.1:${VHIL_WEB_PORT:-8080}:8080")
    [ "$VHIL_AUTH" = dev ] && run_args+=(-e VHIL_ALLOW_DEV_ON_NETWORK=1)
    # Auth settings pass through when set (docs/development/web-app.md).
    for v in VHIL_AUTH VHIL_GITHUB_ORG VHIL_GITHUB_CLIENT_ID VHIL_GITHUB_CLIENT_SECRET \
             VHIL_SESSION_SECRET VHIL_PUBLIC_URL VHIL_GITHUB_APP_ID VHIL_GITHUB_APP_KEY \
             VHIL_ADMINS; do
        run_args+=(-e "$v")
    done
    in_container 'export VHIL_DATA=/vhil/server; exec python -m vhil.server --host 0.0.0.0 --port 8080' ;;
worker)
    in_container 'export VHIL_DATA=/vhil/server VHIL_FW_DIR=/vhil/fw; exec python -m vhil.worker "$@"' "$@" ;;
shell) in_container 'exec bash' ;;
run) in_container 'exec "$@"' "$@" ;;
*) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; [ "$cmd" = help ] ;;
esac
