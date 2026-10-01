#!/usr/bin/env bash
# Exploratory headless run: boot a .resc for N virtual seconds and dump the log.
#   scripts/explore.sh <script.resc> <elf> [seconds]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
renode=${RENODE:-$HOME/vhil-tools/renode_1.17.0-portable/renode}
"$renode" --disable-gui --plain --console -e "
\$elf=@$2
include @$(realpath "$1")
logLevel 0 sysbus.fdcan1
logLevel 0 sysbus.fdcan2
logLevel 0 sysbus.fdcan3
emulation RunFor \"${3:-3}\"
cpu PC
quit
"
