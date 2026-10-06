#!/usr/bin/env bash
# Run an IFS_HIL suite against the virtual bench: what CI does, locally.
#
#   scripts/run-ifs-hil.sh <ifs_hil-checkout> <dut.elf> [suite|path] [pytest args...]
#
# VHIL_DUT names the device under test, `ecu` (default) or `ams`: the .elf is
# its image, and the suite is IFS_HIL's for that DUT. suite defaults to
# `smoke` and is expanded by IFS_HIL's own `tools.bench suite`, exactly as
# /hil-test does. SocketCAN is used when can0..can2 exist (CI, or WSL2 after
# scripts/wsl-vcan.sh --load). RENODE overrides the launcher path,
# VHIL_SYSTEM the system file (default systems/<dut>.yaml).
# <DUT>_FIRMWARE_BIN (ECU_FIRMWARE_BIN, AMS_FIRMWARE_BIN) defaults to the .bin
# next to the .elf; for the AMS, AMS_SOURCE_DIR defaults to the source tree
# the .elf was built in (its VERSION file, A-013). VHIL_FIRMWARE names every
# other image of the system: the CAN bootloader each MainLite carries
# (VHIL_FIRMWARE="ecu.bootloader=CAN_BL.elf"), and with
# VHIL_SYSTEM=systems/ecu-ams.yaml the AMS's ("... ams=build/AMS.elf
# ams.bootloader=CAN_BL.elf").
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
ifs_hil=$(realpath "$1"); elf=$(realpath "$2"); suite=${3:-smoke}
shift $(( $# < 3 ? $# : 3 ))
dut=${VHIL_DUT:-ecu}
case "$dut" in
ecu|ams) ;;
*) echo "VHIL_DUT must be ecu or ams, got '$dut'" >&2; exit 2 ;;
esac

bin=${elf%.elf}.bin
if [ ! -f "$bin" ] && command -v arm-none-eabi-objcopy >/dev/null; then
    arm-none-eabi-objcopy -O binary "$elf" "$bin"
fi
if [ "$dut" = ams ]; then
    export AMS_FIRMWARE_BIN=${AMS_FIRMWARE_BIN:-$bin}
    export AMS_SOURCE_DIR=${AMS_SOURCE_DIR:-$(dirname "$(dirname "$elf")")}
else
    export ECU_FIRMWARE_BIN=${ECU_FIRMWARE_BIN:-$bin}
fi
# Resolved before the cd below, like every other path argument.
system=$(realpath "${VHIL_SYSTEM:-$here/systems/$dut.yaml}")

socketcan=()
if ip link show can0 >/dev/null 2>&1 && ip link show can1 >/dev/null 2>&1 \
        && ip link show can2 >/dev/null 2>&1; then
    socketcan=(--vhil-socketcan)
else
    echo "warning: can0..can2 not present; CAN-side tests will skip" >&2
fi

# vcan has no bit timing: IFS_HIL's bus-retiming sudo calls become no-ops
# on vcan links (scripts/shims/sudo).
export PATH="$here/scripts/shims:$PATH"

cd "$ifs_hil"
targets=$(python3 -m tools.bench suite --dut "$dut" --suite "$suite")
# Every option that takes a path is passed as one --opt=value word. pytest
# picks its config file from the common ancestor of the arguments that are
# existing paths, and a separate value word (this repo's system file) moves
# that ancestor out of IFS_HIL: its pyproject.toml, and with it its
# addopts (`-m 'not soak'`), would be ignored.
firmware=(--vhil-firmware="$dut=$elf")
for pair in ${VHIL_FIRMWARE:-}; do
    firmware+=(--vhil-firmware="${pair%%=*}=$(realpath "${pair#*=}")")
done
renode_opt=()
[ -n "${RENODE:-}" ] && renode_opt=(--vhil-renode="$RENODE")
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m pytest -p vhil.pytest_plugin \
    --vhil-system="$system" \
    "${firmware[@]}" "${socketcan[@]}" "${renode_opt[@]}" \
    --rootdir "$ifs_hil" -p no:cacheprovider -rA --log-level=INFO $targets "$@"
