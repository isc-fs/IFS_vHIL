#!/usr/bin/env bash
# Start the system editor: Antmicro's Pipeline Manager (UI on :5000) and the
# vHIL backend it talks to (python -m vhil.editor serve, TCP :9000).
#
#   scripts/editor.sh            # then open http://localhost:5000
#
# Expects (docs/development/setup.md, "System editor"):
#   PM_DIR    Pipeline Manager checkout, built with `./build server-app`
#             (default ~/vhil-tools/kenning-pipeline-manager)
#   PM_VENV   its Python venv (default ~/vhil-tools/pm-venv)
#   NODE_DIR  Node.js >= 20.18 (default ~/vhil-tools/node)
#   VHIL_<FIRMWARE>_ELF  images Run uses, e.g. VHIL_ECU_ELF, VHIL_AMS_ELF
# Logs: $LOG_DIR/pm.log and $LOG_DIR/backend.log (default ~/vhil/editor).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
PM_DIR=${PM_DIR:-$HOME/vhil-tools/kenning-pipeline-manager}
PM_VENV=${PM_VENV:-$HOME/vhil-tools/pm-venv}
NODE_DIR=${NODE_DIR:-$HOME/vhil-tools/node}
LOG_DIR=${LOG_DIR:-$HOME/vhil/editor}
mkdir -p "$LOG_DIR"
export PATH="$NODE_DIR/bin:$PATH"

( source "$PM_VENV/bin/activate"
  cd "$PM_DIR"
  exec ./run --backend-host 127.0.0.1 --backend-port 5000 --tcp-server-port 9000 ) \
  > "$LOG_DIR/pm.log" 2>&1 &
pm=$!
trap 'kill $pm 2>/dev/null' EXIT

# The backend connects to Pipeline Manager's TCP server; give it time to bind.
for _ in $(seq 1 30); do
  grep -q "Application startup complete" "$LOG_DIR/pm.log" 2>/dev/null && break
  sleep 1
done
echo "editor: http://localhost:5000  (logs in $LOG_DIR)"
cd "$here"
python -m vhil.editor serve --port 9000 2>&1 | tee "$LOG_DIR/backend.log"
