# Renode from source, plus what its own test runner needs (renode-test:
# robot framework, NUnit through `dotnet test`). Same SDK image, by digest, as
# docker/Dockerfile's renode-build stage.
#   docker build -t renode-up-builder -f builder.Dockerfile .
FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim@sha256:ec9c0a0dc5f60adc2065762050638dd5ed5facfc53716d4580a42d86027c8e80
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build build-essential git python3 python3-pip python3-dev automake autoconf libtool \
        pkg-config zip xz-utils file rsync procps time \
    && rm -rf /var/lib/apt/lists/*
# renode/tests/requirements.txt (with its two -r includes) at upstream master, 2026-10-12
RUN pip3 install --no-cache-dir --break-system-packages \
        robotframework==6.1 robotframework-retryfailed==0.2.0 psutil==5.9.4 'pyyaml==6.0.*' \
        'telnetlib3==2.0.*' construct==2.10.68 pyelftools==0.30
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
WORKDIR /src
