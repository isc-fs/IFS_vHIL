# The system editor (M5, #13) on top of the vHIL image: Antmicro's Pipeline
# Manager (UI on :5000) and the backend library vhil.editor serves through.
# Built from the repository root, which the editor's sources live in
# (editor.Dockerfile.dockerignore sends only those):
#
#   docker build -t ifs-vhil-editor --build-arg BASE=ifs-vhil -f docker/editor.Dockerfile .
#
# Pipeline Manager is vendored in editor/pipeline-manager/ (README-VHIL.md
# there: upstream v0.5.2, 04613679; CHANGELOG-VHIL.md: our changes), at a
# release whose specification/dataflow format matches vhil/editor.py's
# FORMAT_VERSION; bump them together.
#
# Node's tarball is checked against the SHA-256 in nodejs.org's published
# https://nodejs.org/dist/v<ver>/SHASUMS256.txt (re-fetch when bumping).
# In production BASE is the base image by digest (docs/deploy.md, "Images").
ARG BASE=ifs-vhil

# --- build: Node, the frontend (npm ci on the lockfile) and the venv ---------
FROM ${BASE} AS build
ARG TARGETARCH
ARG NODE_VERSION=22.23.3
ARG NODE_SHA256_AMD64=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
ARG NODE_SHA256_ARM64=a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f
ARG PM_VERSION=0.5.2
ARG PM_COMM_REF=e26894d9bdd97da49ad4d04f1ffd00c8de9024ca

RUN case "$TARGETARCH" in \
        amd64) a=x64; sum=$NODE_SHA256_AMD64 ;; \
        arm64) a=arm64; sum=$NODE_SHA256_ARM64 ;; \
        *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${a}.tar.xz" \
    && echo "$sum  /tmp/node.tar.xz" | sha256sum -c - \
    && tar xJf /tmp/node.tar.xz -C /opt && rm /tmp/node.tar.xz \
    && mv "/opt/node-v${NODE_VERSION}-linux-${a}" /opt/node
ENV PATH=/opt/node/bin:$PATH

# The frontend's dependencies first, from the lockfile alone, so a source
# change doesn't refetch them. postinstall runs patch-package on patches/.
COPY editor/pipeline-manager/pipeline_manager/frontend/package.json \
     editor/pipeline-manager/pipeline_manager/frontend/package-lock.json \
     /opt/pm/pipeline_manager/frontend/
COPY editor/pipeline-manager/pipeline_manager/frontend/patches/ \
     /opt/pm/pipeline_manager/frontend/patches/
RUN cd /opt/pm/pipeline_manager/frontend && npm ci --no-audit --no-fund \
    && npm cache clean --force

# Pipeline Manager in its own venv: it pins old dependency versions. Its
# version comes from setuptools-scm, which has no git history here. The
# frontend is built by ./build below, not by pip.
COPY editor/pipeline-manager/ /opt/pm/
# The shell's design tokens, self-hosted fonts, what a run sends
# (editor-run.js, the workspace's Run), and its virtual-table window and
# frame decoder (vtable.js, decode.js: the Bus tab's), from their one source
# (vhil/server/static/, which the shell serves too), into the frontend's
# src/vhil/shell/ (not in git; the editor CI job compares the image's copies
# with the checkout); the fonts checked against the hashes they were pinned
# with.
COPY vhil/server/static/tokens.css vhil/server/static/editor-run.js \
     vhil/server/static/vtable.js vhil/server/static/decode.js \
     /opt/pm/pipeline_manager/frontend/src/vhil/shell/
COPY vhil/server/static/fonts/ /opt/pm/pipeline_manager/frontend/src/vhil/shell/fonts/
RUN cd /opt/pm/pipeline_manager/frontend/src/vhil/shell/fonts && sha256sum -c --quiet SHA256SUMS
RUN python -m venv /opt/pm-venv \
    && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PIPELINE_MANAGER="$PM_VERSION" \
       PIPELINE_MANAGER_SKIP_FRONTEND_BUILD=1 \
       /opt/pm-venv/bin/pip install --no-cache-dir -e /opt/pm \
    && /opt/pm-venv/bin/pip install --no-cache-dir \
        "git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git@${PM_COMM_REF}" \
    && cd /opt/pm && PATH=/opt/pm-venv/bin:$PATH ./build server-app --skip-install-deps
# The built UI carries the copy: every token tokens.css defines, and the
# fonts, served from the editor's own origin.
RUN cd /opt/pm/pipeline_manager/frontend \
    && for t in $(grep -o -- '--[a-z0-9-]*:' src/vhil/shell/tokens.css | sort -u); do \
        grep -qF -- "$t" dist/css/*.css || { echo "dist/css lacks $t" >&2; exit 1; }; done \
    && ls dist/fonts/Inter-Regular.*.woff2 dist/fonts/JetBrainsMono-Regular.*.woff2 >/dev/null \
    && ! grep -l 'fonts.googleapis' -r dist

# What runs: the Python package with the built UI (frontend/dist) and what
# ./validate loads the frontend's code with (src, node_modules, Node; the
# lockfile and patches/ too, so a ./validate without --skip-install-deps
# finds nothing to install). Not the tests, the upstream tooling or the
# npm cache.
RUN mkdir -p /out/pm/pipeline_manager/frontend \
    && cd /opt/pm && cp -a run validate pyproject.toml setup.py LICENSE \
        README.md README-VHIL.md CHANGELOG-VHIL.md /out/pm/ \
    && tar -C /opt/pm -cf - --exclude=pipeline_manager/frontend \
        --exclude=pipeline_manager/tests --exclude=__pycache__ pipeline_manager \
        | tar -C /out/pm -xf - \
    && cd pipeline_manager/frontend && cp -a dist src node_modules patches validator.js \
        package.json package-lock.json tsconfig.json __init__.py \
        /out/pm/pipeline_manager/frontend/

# --- the editor image -------------------------------------------------------
FROM ${BASE}
ARG PM_COMM_REF=e26894d9bdd97da49ad4d04f1ffd00c8de9024ca
COPY --from=build /opt/node /opt/node
COPY --from=build /opt/pm-venv /opt/pm-venv
COPY --from=build /out/pm /opt/pm
ENV PATH=/opt/node/bin:$PATH

# The JSON-RPC library both ends use: Pipeline Manager's server (in its venv,
# above) and vhil.editor (in the image's Python, next to vhil).
RUN pip install --no-cache-dir \
        "git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git@${PM_COMM_REF}"

ENV PM_DIR=/opt/pm PM_VENV=/opt/pm-venv NODE_DIR=/opt/node LOG_DIR=/vhil/editor
EXPOSE 5000
