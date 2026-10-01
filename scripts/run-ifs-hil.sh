#!/usr/bin/env bash
# Run an IFS_HIL suite against the virtual bench: what CI does, locally.
#
#   scripts/run-ifs-hil.sh <ifs_hil-checkout> <ECU08.elf> [suite|path] [pytest args...]
#
# suite defaults to `smoke` and is expanded by IFS_HIL's own `tools.bench
# suite`, exactly as /hil-test does. SocketCAN is used when can0..can2 exist
# (CI, or WSL2 after scripts/wsl-vcan.sh --load). RENODE overrides the
# launcher path. ECU_FIRMWARE_BIN defaults to the .bin next to the .elf.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
ifs_hil=$(realpath "$1"); elf=$(realpath "$2"); suite=${3:-smoke}
shift $(( $# < 3 ? $# : 3 ))

bin=${elf%.elf}.bin
if [ ! -f "$bin" ] && command -v arm-none-eabi-objcopy >/dev/null; then
    arm-none-eabi-objcopy -O binary "$elf" "$bin"
fi
export ECU_FIRMWARE_BIN=${ECU_FIRMWARE_BIN:-$bin}

socketcan=()
if ip link show can0 >/dev/null 2>&1 && ip link show can1 >/dev/null 2>&1 \
        && ip link show can2 >/dev/null 2>&1; then
    socketcan=(--vhil-socketcan)
else
    echo "warning: can0..can2 not present; CAN-side tests will skip" >&2
fi

cd "$ifs_hil"
targets=$(python3 -m tools.bench suite --dut ecu --suite "$suite")
renode_opt=()
[ -n "${RENODE:-}" ] && renode_opt=(--vhil-renode "$RENODE")
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m pytest -p vhil.pytest_plugin \
    --vhil-elf "$elf" "${socketcan[@]}" "${renode_opt[@]}" \
    -p no:cacheprovider -rA --log-level=INFO $targets "$@"
