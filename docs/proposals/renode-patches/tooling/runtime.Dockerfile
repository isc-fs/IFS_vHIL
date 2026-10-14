# The vHIL image plus perf, for profiling Renode variants mounted at /opt/renode-variants/<name>.
FROM ifs-vhil:latest
RUN apt-get update && apt-get install -y --no-install-recommends linux-perf procps && rm -rf /var/lib/apt/lists/*
