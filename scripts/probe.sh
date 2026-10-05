#!/usr/bin/env bash
# Boot a one-board system for N virtual seconds, then run extra monitor commands.
#   scripts/probe.sh <systems/x.yaml> <elf> <seconds> "<cmd1>" "<cmd2>" ...
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
system=$1; elf=$(realpath "$2"); secs=$3; shift 3
board=$(python3 -c "from vhil.system import System; print(*System('$system').boards)")
script=$(mktemp --suffix=.resc)
python3 -m vhil.system render "$system" --firmware "$board=$elf" -o "$script"
cmds=$(printf '%s\n' "$@")
# The monitor is this console: -P -1 keeps Renode from also opening its
# monitor port, which listens on every interface (vhil/renode.py).
"$renode" --disable-gui --plain --console -P -1 -e "
include @$script
emulation RunFor \"$secs\"
$cmds
quit
" 2>&1 | grep -v -E 'usb1:|CAN_CCU|not preselected|will be transmitted'
