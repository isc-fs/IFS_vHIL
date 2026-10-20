# Sourced by bench.sh and vhil-ab.sh: one emulation on the host at a time.
# wait_idle blocks until no container runs except the long-lived vHIL stack
# (names matching $IDLE_IGNORE); contended prints 1 if another one is running
# now (checked after a run: that run may have shared the CPUs).
IDLE_IGNORE=${IDLE_IGNORE:-ifs-vhil-smoke-}
_others() { docker ps --format '{{.Names}}' | grep -v -E "$IDLE_IGNORE" | grep -v -x -F "${1:-}" ; }
wait_idle() { while [ -n "$(_others)" ]; do sleep 5; done; }
contended() { [ -n "$(_others)" ] && echo true || echo false; }
