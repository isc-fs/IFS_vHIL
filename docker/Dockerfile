# The vHIL toolchain in one image: what CI installs on ubuntu-latest, pinned
# to the same versions (CLAUDE.md), for amd64 and arm64 hosts.
#
#   docker build -t ifs-vhil docker/        (scripts/vhil-docker.sh does this)
#
# Firmware sources, build output and the IFS_HIL checkout live in a volume
# mounted at /vhil, not in the image: they change per run.
FROM python:3.11-bookworm

ARG TARGETARCH
ARG RENODE_VERSION=1.17.0
ARG ARM_GNU_VERSION=14.2.rel1
ARG CAN_FLASHER_VERSION=3.1.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build git iproute2 can-utils xz-utils dosfstools mtools \
        libicu72 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Renode portable: linux-portable is x86_64, linux-arm64-portable aarch64.
RUN case "$TARGETARCH" in \
        amd64) asset=linux-portable ;; \
        arm64) asset=linux-arm64-portable ;; \
        *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/renode.tar.gz \
        "https://github.com/renode/renode/releases/download/v${RENODE_VERSION}/renode-${RENODE_VERSION}.${asset}.tar.gz" \
    && mkdir -p /opt/renode \
    && tar xzf /tmp/renode.tar.gz -C /opt/renode --strip-components=1 \
    && rm /tmp/renode.tar.gz

# Arm GNU toolchain, pinned like IFS_HIL's firmware recipes.
RUN case "$TARGETARCH" in \
        amd64) host=x86_64 ;; \
        arm64) host=aarch64 ;; \
    esac \
    && curl -fsSL -o /tmp/gcc.tar.xz \
        "https://developer.arm.com/-/media/Files/downloads/gnu/${ARM_GNU_VERSION}/binrel/arm-gnu-toolchain-${ARM_GNU_VERSION}-${host}-arm-none-eabi.tar.xz" \
    && mkdir -p /opt/arm-gnu \
    && tar xJf /tmp/gcc.tar.xz -C /opt/arm-gnu --strip-components=1 \
    && rm /tmp/gcc.tar.xz

# can-flasher (`cf`, isc-fs/can-flasher): the host side of the CAN
# bootloader, which IFS_HIL's flashing tests drive over SocketCAN.
RUN case "$TARGETARCH" in \
        amd64) triple=x86_64-unknown-linux-gnu ;; \
        arm64) triple=aarch64-unknown-linux-gnu ;; \
    esac \
    && curl -fsSL "https://github.com/isc-fs/can-flasher/releases/download/v${CAN_FLASHER_VERSION}/can-flasher-v${CAN_FLASHER_VERSION}-${triple}.tar.gz" \
       | tar xz -C /tmp \
    && install -m 0755 "/tmp/can-flasher-v${CAN_FLASHER_VERSION}-${triple}/can-flasher" /usr/local/bin/can-flasher \
    && rm -rf /tmp/can-flasher-*

# vHIL + renode-test (Renode 1.17's tests/requirements.txt) + IFS_HIL's core
# dependencies (its pyproject; the Pi-only [bench] extras stay out).
RUN pip install --no-cache-dir \
        "pyyaml==6.0.*" "ruamel.yaml==0.18.*" "jsonschema>=4" pytest \
        "robotframework==6.1" "robotframework-retryfailed==0.2.0" \
        "psutil==5.9.4" "telnetlib3==2.0.*" \
        "typer>=0.9" "pytest-html>=4.0" "python-can>=4.0" "pyserial>=3.5"

ENV PATH=/opt/arm-gnu/bin:/opt/renode:$PATH \
    RENODE=/opt/renode/renode \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work
