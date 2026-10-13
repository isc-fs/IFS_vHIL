# Renode 1.17.0 from source: .NET 8 SDK + cmake + gcc (upstream build docs).
FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build build-essential git python3 python3-pip automake autoconf libtool \
        pkg-config zip xz-utils file rsync linux-perf procps \
    && rm -rf /var/lib/apt/lists/*
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
WORKDIR /src
