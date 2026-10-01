#!/usr/bin/env bash
# Exploratory headless run: boot a one-board system for N virtual seconds,
# logging every CAN frame it queues, and dump the log.
#   scripts/explore.sh <systems/x.yaml> <elf> [seconds]
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
board=$(python3 -c "from vhil.system import System; print(*System('$1').boards)")
script=$(mktemp --suffix=.resc)
python3 -m vhil.system render "$1" --firmware "$board=$(realpath "$2")" -o "$script"
"$renode" --disable-gui --plain --console -e "
include @$script
logLevel 0 sysbus.fdcan1
logLevel 0 sysbus.fdcan2
logLevel 0 sysbus.fdcan3
emulation RunFor \"${3:-3}\"
cpu PC
quit
"
