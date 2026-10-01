#!/usr/bin/env bash
# Boot a .resc for N virtual seconds, then run extra monitor commands.
#   scripts/probe.sh <script.resc> <elf> <seconds> "<cmd1>" "<cmd2>" ...
set -euo pipefail
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
resc=$(realpath "$1"); elf=$2; secs=$3; shift 3
cmds=$(printf '%s\n' "$@")
"$renode" --disable-gui --plain --console -e "
\$elf=@$elf
include @$resc
emulation RunFor \"$secs\"
$cmds
quit
" 2>&1 | grep -v -E 'usb1:|CAN_CCU|not preselected|will be transmitted'
