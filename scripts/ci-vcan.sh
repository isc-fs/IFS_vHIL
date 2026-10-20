#!/usr/bin/env bash
# vcan can0..can2 on a GitHub-hosted Ubuntu runner, plus the apt packages the
# job names (e.g. can-utils). Used by .github/workflows/ifs-hil-ecu.yml.
#
#   scripts/ci-vcan.sh [package ...]
#
# Where vcan.ko lives depends on the runner's kernel: up to the 6.x azure
# kernels it is in linux-modules-extra-$(uname -r); from 7.0.0-1017-azure
# (runner image ubuntu24/20261018) it is in linux-modules-$(uname -r), which
# the image has installed, and no linux-modules-extra package exists for that
# kernel at all, so asking apt for it failed the job (nightly 2026-10-20).
# Load vcan first; only if that fails install the extra modules.
set -euo pipefail
echo "kernel $(uname -r)"
apt_install() {
    # The image's package lists can lag the archive: update once and retry.
    sudo apt-get install -y -qq "$@" >/dev/null \
        || { sudo apt-get update -qq && sudo apt-get install -y -qq "$@" >/dev/null; }
}
[ $# -gt 0 ] && apt_install "$@"
if ! sudo modprobe vcan 2>/dev/null; then
    apt_install "linux-modules-extra-$(uname -r)"
    sudo modprobe vcan
fi
for i in 0 1 2; do
    sudo ip link add "can$i" type vcan
    sudo ip link set "can$i" txqueuelen 1000 up
done
ip -br link show type vcan
