#!/usr/bin/env bash
# Start the system editor: Antmicro's Pipeline Manager (UI on :5000) and the
# vHIL backend it talks to (python -m vhil.editor serve, TCP :9000).
#
#   scripts/editor.sh
#
# The browser reaches it on the web app's origin, under /editor/, through
# the proxy (deploy/Caddyfile: docker/compose.yaml locally, deploy/ on a
# host), which strips the prefix; the UI's socket.io follows the page there.
#
# Expects (docs/development/setup.md, "System editor"):
#   PM_DIR    Pipeline Manager checkout, built with `./build server-app`
#             (default ~/vhil-tools/kenning-pipeline-manager)
#   PM_VENV   its Python venv (default ~/vhil-tools/pm-venv)
#   NODE_DIR  Node.js >= 20.18 (default ~/vhil-tools/node)
#   PM_HOST   address the UI listens on (default 127.0.0.1; 0.0.0.0 in Docker)
#   EDITOR_URL  where the UI is reached from the browser (default http://localhost:8080/editor/)
#   PM_CSP    the Content-Security-Policy Pipeline Manager sends (default:
#             vhil/server/security.py's EDITOR_CSP)
#   PM_CSP_REPORT_ONLY  1: send it as Content-Security-Policy-Report-Only
#             (violations reported in the browser console, not enforced)
# Logs: $LOG_DIR/pm.log and $LOG_DIR/backend.log (default ~/vhil/editor).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
PM_DIR=${PM_DIR:-$HOME/vhil-tools/kenning-pipeline-manager}
PM_VENV=${PM_VENV:-$HOME/vhil-tools/pm-venv}
NODE_DIR=${NODE_DIR:-$HOME/vhil-tools/node}
PM_HOST=${PM_HOST:-127.0.0.1}
LOG_DIR=${LOG_DIR:-$HOME/vhil/editor}
mkdir -p "$LOG_DIR"
export PATH="$NODE_DIR/bin:$PATH"
PM_CSP=${PM_CSP:-$(python -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["EDITOR_CSP"])' \
    "$here/vhil/server/security.py")}
export PM_CSP PM_CSP_REPORT_ONLY=${PM_CSP_REPORT_ONLY:-1}

( source "$PM_VENV/bin/activate"
  cd "$PM_DIR"
  exec ./run --backend-host "$PM_HOST" --backend-port 5000 --tcp-server-port 9000 ) \
  > "$LOG_DIR/pm.log" 2>&1 &
pm=$!
trap 'kill $pm 2>/dev/null' EXIT

# The backend connects to Pipeline Manager's TCP server; give it time to bind.
for _ in $(seq 1 30); do
  grep -q "Application startup complete" "$LOG_DIR/pm.log" 2>/dev/null && break
  sleep 1
done
echo "editor: ${EDITOR_URL:-http://localhost:8080/editor/}  (logs in $LOG_DIR)"
cd "$here"
python -m vhil.editor serve --port 9000 2>&1 | tee "$LOG_DIR/backend.log"
