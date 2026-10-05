#!/usr/bin/env bash
# Boot a one-board system for N virtual seconds, then run extra monitor commands.
#   VHIL_CAN_BOOTLOADER_ELF=<CAN_BL.elf> scripts/probe.sh <systems/x.yaml> <elf> <seconds> "<cmd1>" ...
# The board boots through its CAN bootloader (2 s auto-jump window) first.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
bl=$(realpath "${VHIL_CAN_BOOTLOADER_ELF:?the CAN bootloader ELF every MainLite carries}")
system=$1; elf=$(realpath "$2"); secs=$3; shift 3
board=$(python3 -c "from vhil.system import System; print(*System('$system').boards)")
script=$(mktemp --suffix=.resc)
python3 -m vhil.system render "$system" --firmware "$board=$elf" \
    --firmware "$board.bootloader=$bl" -o "$script"
cmds=$(printf '%s\n' "$@")
# The monitor is this console: -P -1 keeps Renode from also opening its
# monitor port, which listens on every interface (vhil/renode.py).
"$renode" --disable-gui --plain --console -P -1 -e "
include @$script
emulation RunFor \"$secs\"
$cmds
quit
" 2>&1 | grep -v -E 'usb1:|CAN_CCU|not preselected|will be transmitted'
