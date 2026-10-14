# The vHIL toolchain in one image: what CI installs on ubuntu-latest, pinned
# to the same versions (CLAUDE.md), for amd64 and arm64 hosts.
#
#   docker build -t ifs-vhil docker/        (scripts/vhil-docker.sh does this)
#
# Firmware sources, build output and the IFS_HIL checkout live in a volume
# mounted at /vhil, not in the image: they change per run.
#
# Renode is built from source in the renode-build stage below: the release
# tag plus the vHIL's patch queue (docker/renode/: README.md, pins.env,
# patches/), as a portable headless package in /opt/renode, the release
# tarball's layout. `--target renode-package` exports just the package (CI's
# setup-renode action).
#
# Supply chain: the base images are pinned by digest and every download is
# checked against a SHA-256 hard-coded here before it is unpacked; Renode's
# sources against the commits in docker/renode/pins.env and submodules.txt.
# Where each hash comes from (re-fetch them when bumping a version, in the
# same PR):
#   Arm GNU      Arm's published <tarball>.sha256asc next to each download
#   can-flasher  GitHub's asset digest for the release:
#                gh api repos/isc-fs/can-flasher/releases/tags/v<ver> --jq '.assets[]|.name+" "+.digest'
#   base images  docker buildx imagetools inspect <image>:<tag> (the index digest):
#                python:3.11-bookworm, mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim
#
# The user: every service of deploy/compose.prod.yaml runs as `vhil` (uid/gid
# 10001), never root. The image's default user stays root for the dev wrapper
# (scripts/vhil-docker.sh creates vcan links); compose sets `user:`.

# --- Renode 1.17.0 + docker/renode/patches, from source ----------------------
# The .NET 8 SDK and a C toolchain (tlib), per Renode's build docs; the
# package bundles its own .NET runtime. One RUN fetches, checks, patches,
# builds and deletes the tree, so the layer is the package alone (~180 MB).
FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim@sha256:ec9c0a0dc5f60adc2065762050638dd5ed5facfc53716d4580a42d86027c8e80 AS renode-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build build-essential git python3 automake autoconf libtool \
        pkg-config zip xz-utils file \
    && rm -rf /var/lib/apt/lists/*
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
COPY renode/ /vhil-renode/
RUN /vhil-renode/build.sh /opt/renode && rm -rf /root/.nuget /root/.local /tmp/*

# The package alone: `docker buildx build --target renode-package --output
# type=local,dest=<dir> docker/` (CI, .github/actions/setup-renode).
FROM scratch AS renode-package
COPY --from=renode-build /opt/renode/ /

# --- the vHIL image ----------------------------------------------------------
FROM python:3.11-bookworm@sha256:3fd8382c50bae84184460a6e1a8ff4eeac0e3ca6a560691cb56bd84d2b6ec9ca

ARG TARGETARCH
ARG ARM_GNU_VERSION=14.2.rel1
ARG ARM_GNU_SHA256_AMD64=62a63b981fe391a9cbad7ef51b17e49aeaa3e7b0d029b36ca1e9c3b2a9b78823
ARG ARM_GNU_SHA256_ARM64=87330bab085dd8749d4ed0ad633674b9dc48b237b61069e3b481abd364d0a684
ARG CAN_FLASHER_VERSION=3.1.1
ARG CAN_FLASHER_SHA256_AMD64=8e3412b846a3ced9abd34b9a2a5665232c2fb72f089fcf55fdcccb5635c87b5e
ARG CAN_FLASHER_SHA256_ARM64=06c731c37f2842c74d7595392e9bce2ae5f956a807c4ece1db98da24bf699549

# libncurses5: the Arm GNU toolchain's arm-none-eabi-gdb links it (libncurses.so.5);
# the live session's debugger runs it (vhil/gdb.py, docs/debugger.md).
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build git iproute2 can-utils xz-utils dosfstools mtools \
        libicu72 libgtk-3-0 libncurses5 \
    && rm -rf /var/lib/apt/lists/*

# Renode (the stage above): /opt/renode/VHIL_RENODE_BUILD names the release
# and the patch set; vhil.renode logs it when it starts Renode.
COPY --from=renode-build /opt/renode/ /opt/renode/

# Arm GNU toolchain, pinned like IFS_HIL's firmware recipes.
RUN case "$TARGETARCH" in \
        amd64) host=x86_64; sum=$ARM_GNU_SHA256_AMD64 ;; \
        arm64) host=aarch64; sum=$ARM_GNU_SHA256_ARM64 ;; \
        *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/gcc.tar.xz \
        "https://developer.arm.com/-/media/Files/downloads/gnu/${ARM_GNU_VERSION}/binrel/arm-gnu-toolchain-${ARM_GNU_VERSION}-${host}-arm-none-eabi.tar.xz" \
    && echo "$sum  /tmp/gcc.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/arm-gnu \
    && tar xJf /tmp/gcc.tar.xz -C /opt/arm-gnu --strip-components=1 \
    && rm /tmp/gcc.tar.xz

# can-flasher (`cf`, isc-fs/can-flasher): the host side of the CAN
# bootloader, which IFS_HIL's flashing tests drive over SocketCAN.
RUN case "$TARGETARCH" in \
        amd64) triple=x86_64-unknown-linux-gnu; sum=$CAN_FLASHER_SHA256_AMD64 ;; \
        arm64) triple=aarch64-unknown-linux-gnu; sum=$CAN_FLASHER_SHA256_ARM64 ;; \
        *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/can-flasher.tar.gz \
        "https://github.com/isc-fs/can-flasher/releases/download/v${CAN_FLASHER_VERSION}/can-flasher-v${CAN_FLASHER_VERSION}-${triple}.tar.gz" \
    && echo "$sum  /tmp/can-flasher.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/can-flasher.tar.gz -C /tmp && rm /tmp/can-flasher.tar.gz \
    && install -m 0755 "/tmp/can-flasher-v${CAN_FLASHER_VERSION}-${triple}/can-flasher" /usr/local/bin/can-flasher \
    && rm -rf /tmp/can-flasher-*

# vHIL + renode-test (Renode 1.17's tests/requirements.txt) + IFS_HIL's core
# dependencies (its pyproject; the Pi-only [bench] extras stay out) + the web
# app's API service (vhil.server, M5; cryptography signs the GitHub App's
# RS256 JWT, vhil/server/github_app.py).
RUN pip install --no-cache-dir \
        "pyyaml==6.0.*" "ruamel.yaml==0.18.*" "jsonschema>=4" pytest \
        "robotframework==6.1" "robotframework-retryfailed==0.2.0" \
        "psutil==5.9.4" "telnetlib3==2.0.*" \
        "typer>=0.9" "pytest-html>=4.0" "python-can>=4.0" "pyserial>=3.5" \
        "fastapi==0.115.*" "uvicorn[standard]==0.32.*" "httpx==0.28.*" \
        "cryptography==50.0.*"

# The unprivileged user, and the mount points of deploy/compose.prod.yaml's
# volumes owned by it: Docker seeds an empty named volume with the image
# directory's owner and mode, so a fresh deployment's volumes are writable by
# `vhil` without a chown (the stack's `volumes` one-shot fixes older ones).
RUN groupadd -g 10001 vhil \
    && useradd -u 10001 -g vhil -d /home/vhil -m -s /usr/sbin/nologin vhil \
    && install -d -o vhil -g vhil -m 0750 /workspace /data /data/db /data/runs /vhil /vhil/fw \
    && install -d -o vhil -g vhil -m 0700 /backups

ENV PATH=/opt/arm-gnu/bin:/opt/renode:$PATH \
    RENODE=/opt/renode/renode \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work
