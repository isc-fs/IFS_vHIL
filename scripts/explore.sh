#!/usr/bin/env bash
# Exploratory headless run: boot a one-board system for N virtual seconds,
# logging every CAN frame it queues, and dump the log.
#   VHIL_CAN_BOOTLOADER_ELF=<CAN_BL.elf> scripts/explore.sh <systems/x.yaml> <elf> [seconds]
# The board boots through its CAN bootloader (2 s auto-jump window) first.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
bl=$(realpath "${VHIL_CAN_BOOTLOADER_ELF:?the CAN bootloader ELF every MainLite carries}")
board=$(python3 -c "from vhil.system import System; print(*System('$1').boards)")
script=$(mktemp --suffix=.resc)
python3 -m vhil.system render "$1" --firmware "$board=$(realpath "$2")" \
    --firmware "$board.bootloader=$bl" -o "$script"
# The monitor is this console: -P -1 keeps Renode from also opening its
# monitor port, which listens on every interface (vhil/renode.py).
"$renode" --disable-gui --plain --console -P -1 -e "
include @$script
logLevel 0 sysbus.fdcan1_h7
logLevel 0 sysbus.fdcan2_h7
logLevel 0 sysbus.fdcan3_h7
emulation RunFor \"${3:-3}\"
cpu PC
quit
"
