# The system editor (M5, #13) on top of the vHIL image: Antmicro's Pipeline
# Manager (UI on :5000) and the backend library vhil.editor serves through.
#
#   docker build -t ifs-vhil-editor --build-arg BASE=ifs-vhil -f docker/editor.Dockerfile docker/
#
# Pipeline Manager is pinned to a release whose specification/dataflow
# format matches vhil/editor.py's FORMAT_VERSION; bump them together.
ARG BASE=ifs-vhil
FROM ${BASE}

ARG TARGETARCH
ARG NODE_VERSION=22.23.3
ARG PM_REF=v0.5.2
ARG PM_COMM_REF=e26894d9bdd97da49ad4d04f1ffd00c8de9024ca

RUN case "$TARGETARCH" in amd64) a=x64 ;; arm64) a=arm64 ;; esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${a}.tar.xz" \
       | tar xJ -C /opt \
    && mv "/opt/node-v${NODE_VERSION}-linux-${a}" /opt/node
ENV PATH=/opt/node/bin:$PATH

# Pipeline Manager in its own venv: it pins old dependency versions. Our
# patches (docker/pm/) go on before the frontend is built:
#   bus-per-instance  every node of a type shared one `bus` object, so a
#                     graph with two CAN buses lost all but the last bus's
#                     stubs on load ("Missing dst s:<bus>:0"). Not fixed
#                     upstream as of v0.5.2 / main.
COPY pm/ /tmp/pm-patches/
RUN git clone -q --depth 1 -b "$PM_REF" https://github.com/antmicro/kenning-pipeline-manager /opt/pm \
    && git -C /opt/pm apply /tmp/pm-patches/*.patch \
    && python -m venv /opt/pm-venv \
    && /opt/pm-venv/bin/pip install --no-cache-dir -e /opt/pm \
    && cd /opt/pm && PATH=/opt/pm-venv/bin:$PATH ./build server-app

# The JSON-RPC library both ends use: Pipeline Manager's server and
# vhil.editor (in the image's Python, next to vhil).
RUN for py in /opt/pm-venv/bin/pip pip; do $py install --no-cache-dir \
        "git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git@${PM_COMM_REF}"; \
    done

ENV PM_DIR=/opt/pm PM_VENV=/opt/pm-venv NODE_DIR=/opt/node LOG_DIR=/vhil/editor
EXPOSE 5000
