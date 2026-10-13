# The vHIL image with a source-built (patched) Renode in place of the release
# package: same layout (/opt/renode, RENODE=/opt/renode/renode).
#   docker build -t ifs-vhil-renode-fork -f fork.Dockerfile <dir with renode-fork.tar.gz>
FROM ifs-vhil:latest
COPY renode-fork.tar.gz /tmp/
RUN rm -rf /opt/renode && mkdir -p /opt/renode \
    && tar xzf /tmp/renode-fork.tar.gz -C /opt/renode --strip-components=1 && rm /tmp/renode-fork.tar.gz
