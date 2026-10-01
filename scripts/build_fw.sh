#!/usr/bin/env bash
# Build a DUT firmware image exactly as IFS_HIL's recipe does
# (configs/firmware/<dut>.yaml): Arm GNU 14.2.Rel1, root CMake project.
#   scripts/build_fw.sh <firmware-checkout> [toolchain-bin-dir]
set -euo pipefail
src=$1
tc=${2:-${ARM_TOOLCHAIN_BIN:-}}
[ -n "$tc" ] && export PATH="$tc:$PATH"
cd "$src"
cmake -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build -j"$(nproc)"
ls -la build/*.elf
