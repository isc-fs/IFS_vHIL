#!/usr/bin/env bash
# Build and load vcan for the running WSL2 kernel.
#
# Microsoft's WSL2 kernel ships the CAN core (can.ko, can-raw.ko, can-dev.ko)
# but not vcan.ko, so `ip link add can0 type vcan` fails and the SocketCAN
# bridge has nothing to attach to. This builds vcan.ko from Microsoft's source
# for exactly the running kernel and running config.
#
#   scripts/wsl-vcan.sh          build (once per kernel version, ~10-15 min)
#   scripts/wsl-vcan.sh --load   build if needed, then load it and create
#                                can0..can2 (asks for your sudo password)
#
# The kernel has CONFIG_MODVERSIONS=y: a module's imported symbols carry CRCs
# that must match the kernel's. They are reproduced by building vmlinux from
# the same source and config, so the first build compiles the kernel once.
# BTF is disabled for that build only. The kernel allows BTF mismatch, and
# BTF does not affect the CRCs, so this avoids needing pahole.
#
# Rebuild after `wsl --update` changes the kernel version (uname -r).
set -euo pipefail

kver=$(uname -r)                          # e.g. 6.6.87.2-microsoft-standard-WSL2
case "$kver" in
    *microsoft-standard-WSL2*) ;;
    *) echo "Not a WSL2 kernel ($kver). On a normal distro: install"
       echo "linux-modules-extra-\$(uname -r) and 'sudo modprobe vcan'."; exit 1 ;;
esac

base=${kver%%-*}                          # 6.6.87.2
tag=linux-msft-wsl-$base
work=${VHIL_TOOLS:-$HOME/vhil-tools}/wsl-kernel
src=$work/WSL2-Linux-Kernel-$tag
ko=$work/vcan-$kver.ko
jobs=$(nproc)

if [ ! -f "$ko" ]; then
    mkdir -p "$work"
    if [ ! -d "$src" ]; then
        echo "Downloading $tag (~250 MB)..."
        curl -fL --progress-bar -o "$work/$tag.tar.gz" \
            "https://github.com/microsoft/WSL2-Linux-Kernel/archive/refs/tags/$tag.tar.gz"
        tar xzf "$work/$tag.tar.gz" -C "$work"
        rm "$work/$tag.tar.gz"
    fi

    missing=()
    for t in gcc make flex bison bc perl; do command -v "$t" >/dev/null || missing+=("$t"); done
    [ -f /usr/include/gelf.h ] || missing+=("libelf-dev")
    [ -f /usr/include/openssl/ssl.h ] || missing+=("libssl-dev")
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Missing build dependencies: ${missing[*]}"
        echo "Install them with:"
        echo "  sudo apt-get install -y build-essential flex bison bc libelf-dev libssl-dev"
        exit 1
    fi

    cd "$src"
    zcat /proc/config.gz > .config
    scripts/config --module CAN_VCAN --disable DEBUG_INFO_BTF
    make olddefconfig >/dev/null
    echo "Building vmlinux once for the symbol CRCs ($jobs jobs)..."
    make -j"$jobs" LOCALVERSION= vmlinux >"$work/build.log" 2>&1 \
        || { tail -30 "$work/build.log"; exit 1; }
    make -j"$jobs" LOCALVERSION= M=drivers/net/can modules >>"$work/build.log" 2>&1 \
        || { tail -30 "$work/build.log"; exit 1; }
    cp drivers/net/can/vcan.ko "$ko"
fi

vm=$(modinfo -F vermagic "$ko")
case "$vm" in
    "$kver "*) echo "Built $ko (vermagic: $vm)" ;;
    *) echo "vermagic mismatch: module '$vm' vs kernel '$kver'"; exit 1 ;;
esac

if [ "${1:-}" = "--load" ]; then
    sudo modprobe can_dev
    lsmod | grep -q '^vcan ' || sudo insmod "$ko"
    for i in 0 1 2; do
        ip link show "can$i" >/dev/null 2>&1 || sudo ip link add "can$i" type vcan
        sudo ip link set "can$i" txqueuelen 1000 up
    done
    ip -br link show type vcan
else
    echo "Load it with: $0 --load"
fi
