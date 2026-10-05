# Deploying the web app on a host

How to run the shared web app (M5, [architecture](architecture/m5-web-app.md),
[login and credentials](development/web-app.md)) on a Linux host for the team:
[`deploy/compose.prod.yaml`](../deploy/compose.prod.yaml) behind a TLS proxy,
with its own git clone, persistent state and backups. Issue #118. The host
itself is still an open decision (owner: Raul); everything here was tested
locally (see [Local test](#local-test)).

## The stack

```
            :443  ┌──────────── proxy (Caddy 2.11.6) ────────────┐  :5443
browser ─────────▶│ VHIL_SITE          VHIL_EDITOR_SITE          │◀──────── editor iframe
                  │   │                  │ forward_auth /api/me  │
                  └───┼──────────────────┼───────────────────────┘
          [frontend]  ▼        [editor]  ▼  (internal)
                     api                editor (Pipeline Manager + vhil.editor)
                      │  SQLite (WAL)
          volume db ──┤                                        [jobs] (internal)
          volume runs ┤◀── worker × VHIL_WORKERS (Renode) ──┐
                      │         └── volume fw              ├── egress ──▶ github.com only
       volume workspace ◀──────── workspace (one-shot) ─────┘  [egress]
                      └── backup (no network; DB + saved branches → backups)
```

| Service | Image | What it does |
|---|---|---|
| `volume-owner` | `ifs-vhil` | One-shot, first on every `up`: chowns to uid 10001 whatever in the volumes isn't yet (volumes made when the services still ran as root); a no-op after |
| `egress` | `ifs-vhil` | [`deploy/egress_proxy.py`](../deploy/egress_proxy.py): the workers' and the workspace's only way out, CONNECT to `VHIL_EGRESS_ALLOW` |
| `workspace` | `ifs-vhil` | One-shot, runs before the others on every `up`: clones IFS_vHIL into the `workspace` volume if empty, `git fetch`es, checks out `VHIL_WORKSPACE_REF` detached ([`deploy/workspace.sh`](../deploy/workspace.sh)) |
| `api` | `ifs-vhil` | `python -m vhil.server`: the shell, systems read/save, runs, inspect, login. Healthcheck `GET /api/health` |
| `worker` | `ifs-vhil` | `python -m vhil.worker`, `VHIL_WORKERS` replicas: claims queued runs, runs Renode, builds missing firmware into `fw` |
| `editor` | `ifs-vhil-editor` | Pipeline Manager + `vhil.editor`, embedded by the shell |
| `proxy` | `caddy:2.11.6-alpine` (by digest) | TLS, security headers, the only published ports: 80, 443 (app), 5443 (editor) |
| `backup` | `ifs-vhil` | Snapshots the DB and the saved branches every `VHIL_BACKUP_INTERVAL_H` ([`deploy/backup.py`](../deploy/backup.py)) |

Every long-running service has `restart: unless-stopped`, CPU/memory/pids
limits (`VHIL_*_CPUS` / `VHIL_*_MEMORY`, `VHIL_WORKER_PIDS`) and json-file
logs rotated at 10 MB × 5; all of them run hardened ([Hardening](#hardening)).

**The code comes from the workspace clone, not the image.** The images carry
the toolchain (Renode, Arm GCC, Python deps, Pipeline Manager) as CI's runners
do; `vhil/`, `catalog/`, `schemas/` and `systems/` are the workspace's working
tree at `VHIL_WORKSPACE_REF`. That keeps code, catalogue and systems from the
same commit, as in CI. The operator's checkout only provides
`compose.prod.yaml`, the `Caddyfile` and the scripts in `deploy/`.

**Workers need no privileges.** Run scenarios and `tests/sim` use Renode's
in-process CAN hubs, not vcan, so workers run unprivileged, each in its own
network namespace: concurrent runs can't collide on Renode monitor ports or
CAN links. If a scenario ever bridges to SocketCAN
(`--vhil-socketcan`, IFS_HIL suites), give the worker `cap_add: [NET_ADMIN]`
so it creates its vcan links in its own namespace; that needs the `vcan` module
loaded on the host (a container can't load modules), but not `privileged`.

**The editor** has no login of its own. Caddy serves it on a second port of
the app's host and checks every request (WebSockets included) against the
API's `/api/me` first: a signed-in org member (or anyone in dev mode) passes,
everyone else gets 401. The session cookie reaches the gate because cookies
are scoped to the host, not the port; Caddy cuts the app's `vhil_*` cookies
from what it passes on to Pipeline Manager. The editor sits on an internal
network shared with the proxy only, so the gate is the only way in.

## Hardening

What every service gets (`x-hardened` in the compose file), and why:

| Control | Setting | Notes |
|---|---|---|
| Non-root | `user: "10001:10001"` | The images carry a `vhil` user (10001) and the volume mount points owned by it, so new volumes are writable; `volume-owner` fixes older ones. The images' default user stays root for `scripts/vhil-docker.sh`, which creates vcan links |
| No capabilities | `cap_drop: [ALL]` | `volume-owner` alone adds `CHOWN` + `DAC_READ_SEARCH`, as root, with no network |
| No privilege gain | `security_opt: [no-new-privileges:true]` | Caddy's binary carries a file capability the kernel won't exec under this, so the proxy runs a plain copy of it from its `/tmp`; binding 80/443 needs nothing, Docker sets `net.ipv4.ip_unprivileged_port_start=0` in a container |
| Read-only root | `read_only: true` + `tmpfs: /tmp` | `HOME=/tmp`. The worker's and editor's `/tmp` are `exec`: Renode unpacks its CPU library there and dlopens it. tmpfs counts against the memory limit (`VHIL_WORKER_TMP`, default 1 GB) |
| Limits | CPU, memory, pids on every service | pids: worker `VHIL_WORKER_PIDS` (2048: Renode's threads, `cmake --build -j`), others 64–512 |
| Minimal mounts | per service | worker: workspace **ro**, db, runs, fw, **no secrets**; api: workspace (saves), db, runs **ro**, fw **ro**; editor: workspace **ro**; backup: db, workspace **ro**, backups; no service mounts the Docker socket |

### Networks

| Network | Internal | Members | Why |
|---|---|---|---|
| `frontend` | no | proxy, api | the proxy reaches the API; the API reaches GitHub (OAuth, pushes, `ls-remote`) |
| `editor` | yes | proxy, editor | the editor is reachable only through the proxy's auth gate, and reaches nothing |
| `jobs` | yes | worker, workspace, egress | no route off the host; the workers don't share a network with the API or the editor, so they can't reach their ports |
| `egress` | no | egress | the egress proxy's own way out |

Workers talk to the API only through the DB file and the `runs` volume, never
over the network. They reach the outside only through `egress`
([`deploy/egress_proxy.py`](../deploy/egress_proxy.py), standard library
only): an HTTP CONNECT proxy that tunnels TLS to `VHIL_EGRESS_ALLOW`
(default `github.com,.githubusercontent.com`: firmware clones and the
workspace fetch) on `VHIL_EGRESS_PORTS` (443), refuses every other host and
plain HTTP, and refuses an allowed name that resolves to a private,
loopback, link-local, multicast or reserved address, so DNS can't aim it at
the metadata service or the host. git finds it through `https_proxy`. With no
route at all on `jobs`, `169.254.169.254` is unreachable from a worker,
whatever it runs. A firmware recipe that fetches from another host (CMake
`FetchContent`, a submodule elsewhere) needs that host added to
`VHIL_EGRESS_ALLOW`.

**Host firewall (defence in depth).** The `frontend` network (the API) has a
normal route out, and a misconfiguration could give one to a worker. On a
cloud host, block the metadata address for every container in Docker's
`DOCKER-USER` chain (it runs before Docker's own rules), and persist it with
your distribution's mechanism:

```sh
# iptables (Docker's default backend)
sudo iptables -I DOCKER-USER -d 169.254.169.254/32 -j REJECT
sudo ip6tables -I DOCKER-USER -d fd00:ec2::254/128 -j REJECT    # AWS IPv6 IMDS
# nftables hosts, same effect, ahead of Docker's own rules
sudo nft add table inet vhil-guard
sudo nft add chain inet vhil-guard fwd '{ type filter hook forward priority -10; }'
sudo nft add rule inet vhil-guard fwd ip daddr 169.254.169.254 reject
```

To restrict the API's egress too (GitHub, the registry and Let's Encrypt are
the proxy's), add per-bridge rules in `DOCKER-USER` keyed on the
`frontend` bridge interface (`docker network inspect ifs-vhil-prod_frontend`
gives its id; the bridge is `br-<first 12 chars>`), allowing only
established traffic and TCP 443, and resolve GitHub's ranges from
`https://api.github.com/meta`; that list changes, so it is not set up by
default.

### Secrets

Secrets are files, never environment values or command-line arguments:

| File in `VHIL_SECRETS_DIR` | Mounted as (compose secret) | Read through | Used by |
|---|---|---|---|
| `session_secret` | `/run/secrets/session_secret` | `VHIL_SESSION_SECRET_FILE` | api |
| `github_client_secret` | `/run/secrets/github_client_secret` | `VHIL_GITHUB_CLIENT_SECRET_FILE` | api |
| `github-app.pem` (`VHIL_GITHUB_APP_KEY_FILE`) | `/run/secrets/github_app_key` | `VHIL_GITHUB_APP_KEY_FILE` | api |
| `github_token` | `/run/secrets/github_token` | `VHIL_GITHUB_TOKEN_FILE` | api, workspace |

Every file must exist (compose refuses a missing one); an unused one is
empty, which the app treats as unset. Compose bind-mounts them as they are
on the host, so the host file's owner and mode are what the container sees:
`chown 10001:10001` and `chmod 0400` them. The app reads `<NAME>_FILE` before
`<NAME>` (`vhil/server/config.py`, `env_secret`), so the plain variables
still work for a local checkout, but the production compose file no longer
passes any. A token never goes on git's command line (every process on the
host can read argv): the API and `deploy/workspace.sh` hand it to git as an
`http.extraHeader` in git's environment (`GIT_CONFIG_COUNT`, readable only
by the same user and root), and it is never written into the clone's config.

### Limits

Runs are bounded by the API ([`vhil/server/runs.py`](../vhil/server/runs.py),
`Limits`) and the worker, from these variables (defaults in brackets):

| Variable | Bounds | Refused with |
|---|---|---|
| `VHIL_MAX_VIRTUAL_MS` [600000] | a run's virtual time | 422 |
| `VHIL_MAX_STIMULI` [1000] / `VHIL_MAX_WATCHES` [100] | a run scenario's stimuli / watches | 422 |
| `VHIL_MAX_QUEUED` [50] | active (queued + running) runs, everyone | 429 |
| `VHIL_MAX_QUEUED_PER_USER` [10] | active runs per login | 429 |
| `VHIL_MAX_TRACE_MB` [512] | a run's `trace.jsonl` (worker) | the run ends `error` |
| `VHIL_MAX_OUTPUT_MB` [64] | a pytest run's `pytest.txt` (worker) | the run ends `error` |

A pytest run is also bounded by its own `timeout_s` (at most 6 h), Caddy
caps request bodies at 8 MB, and each container by its CPU, memory and pids
limits.

### Residual risk

- **Workers share the run database with the API.** A worker needs to claim,
  heartbeat and finish runs, so it writes `vhil.db`; a compromised worker
  (a malicious firmware build or test, a Renode escape) can rewrite any run's
  state, summary or `owner`, or queue runs that bypass the API's
  limits. It can't reach the API, the editor, the secrets or (but for GitHub)
  the outside. Narrowing this needs the workers to talk to an API endpoint
  instead of the file (a claim/heartbeat/finish RPC with a worker token);
  not done here, it changes the queue's design (M5.2).
- **Workers share `runs` and `fw`.** One run can read or overwrite another's
  traces and the firmware builds others will run. Builds come only from
  repositories on the allowlist, at refs `vhil.system build` resolves.
- **The workspace is the code.** Services run the code at
  `VHIL_WORKSPACE_REF`: whoever can push that ref (or a tag it names) to the
  remote controls the deployment. Pin a commit for the strongest guarantee.
- **The API has a normal route out** (GitHub login and pushes): see the
  host firewall above.
- **Python packages** in the images are pinned by version, not hash, and the
  Pipeline Manager frontend build pulls npm packages from its lock file.


## State

| Volume | Mounted at | Holds | Backed up |
|---|---|---|---|
| `workspace` | `/workspace` (api rw; worker, editor, backup ro) | the app's IFS_vHIL clone; its local branches are the systems users saved | local branches, as a git bundle |
| `db` | `/data/db` | `vhil.db` (WAL): run queue + history | yes, `sqlite3` backup API |
| `runs` | `/data/runs` | per-run traces, JUnit, snapshots, logs | no (large; copy by hand if wanted) |
| `fw` | `/vhil/fw` | firmware sources, builds, `built.txt` | no (rebuilt from source) |
| `backups` or `VHIL_BACKUP_DIR` | `/backups` | snapshots | it is the backup |
| `caddy-data`, `caddy-config` | Caddy | certificates, ACME account | no (re-issued), but keep across upgrades: Let's Encrypt rate-limits |

Volume names are prefixed with the project, `ifs-vhil-prod_`.

### The workspace clone

The app has its own clone instead of the operator's checkout: a save from the
browser writes a commit and a branch ref into the workspace
(`vhil/server/gitstore.py`, plumbing only), and those must not land in
someone's working copy. [`deploy/workspace.sh`](../deploy/workspace.sh), on every
`docker compose up`:

1. clones `VHIL_WORKSPACE_REMOTE` (default `https://github.com/isc-fs/IFS_vHIL.git`)
   into the empty volume (refuses a non-empty directory that is not a clone);
2. `git fetch --tags origin`: remote branches move under `refs/remotes/origin/*`
   (saves start from the fresh `origin/dev`), tags follow;
3. checks out `VHIL_WORKSPACE_REF` (a branch of origin, a tag or a commit)
   **detached**, with `--force`: only HEAD and the working tree move, and the
   working tree belongs to the deployment;
4. `git gc --auto`.

**Local branches are never touched**: no reset, no prune, no delete (except
right after the first clone, where the copy of the default branch the clone
made is dropped so only user branches are local). A saved branch that was
never pushed survives every upgrade, and the backup bundles it.

IFS_vHIL and the firmware repos are public, so clone, fetch and firmware
builds need no credentials. **Pushes** of saved branches and PRs are the
API's: with the GitHub App configured it uses an installation token for
`VHIL_GITHUB_REPO` (contents + pull requests write, minted per push), else
`VHIL_GITHUB_TOKEN` (a fine-grained token, interim). The token is never
written into the clone's config. For a private remote, `VHIL_GITHUB_TOKEN`
also authenticates the workspace's clone and fetch (sent as a header for that
run only).

## Images

Two images, tagged together: `<VHIL_IMAGE>:<VHIL_TAG>` and
`<VHIL_IMAGE>-editor:<VHIL_TAG>` (default `ghcr.io/isc-fs/ifs-vhil`,
`ghcr.io/isc-fs/ifs-vhil-editor`). They hold only the toolchain, so they
change when `docker/Dockerfile` or `docker/editor.Dockerfile` change, not on
every app change. The API needs `cryptography` (the GitHub App's JWT), which
`docker/Dockerfile` installs since M5.5: an image built before that can't
start with `VHIL_GITHUB_APP_ID` set.

Tag with the date and the commit of `docker/` (and `latest`):

```sh
tag=$(date +%Y.%m.%d)-$(git rev-parse --short HEAD)
img=ghcr.io/isc-fs/ifs-vhil
# once: a token with write:packages, owned by an isc-fs member
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
docker buildx build --platform linux/amd64,linux/arm64 \
    -t "$img:$tag" -t "$img:latest" --push docker/
docker buildx build --platform linux/amd64,linux/arm64 \
    --build-arg BASE="$img:$tag" -f docker/editor.Dockerfile \
    -t "$img-editor:$tag" -t "$img-editor:latest" --push docker/
```

Then `VHIL_TAG=$tag` in `deploy/.env`, and the digests the push printed (or
`docker buildx imagetools inspect "$img:$tag"`, the index digest) as
`VHIL_DIGEST=@sha256:…` and `VHIL_EDITOR_DIGEST=@sha256:…`: a tag can be
moved, a digest can't, so the host runs exactly what was built. Pin a tag
and digest in production rather than `latest`, so an upgrade is a
deliberate edit. Build the editor with `--build-arg BASE="$img:$tag@sha256:…"`
to pin its base the same way.

The images' inputs are pinned too: `python:3.11-bookworm` by digest, and
every download in `docker/Dockerfile` and `docker/editor.Dockerfile`
(Renode, the Arm GNU toolchain, can-flasher, Node) is checked against a
SHA-256 hard-coded there (`sha256sum -c`), from the publisher's checksum file
or GitHub's release asset digest; Pipeline Manager must be the pinned commit.
Bumping a version means fetching its new hash the same way (the Dockerfiles
say where). Without a registry, build on the
host itself (`VHIL_DOCKER_CONTEXT=default scripts/vhil-docker.sh image` gives
`ifs-vhil:latest` and `ifs-vhil-editor:latest`; set `VHIL_IMAGE=ifs-vhil`,
`VHIL_TAG=latest`). GHCR packages of a public repo can be made public; if
they stay private, `docker login ghcr.io` on the host with a `read:packages`
token.

## Host requirements

- **Linux, x86_64 or arm64**, with Docker Engine ≥ 25 and the Compose plugin
  ≥ 2.24 (healthcheck `start_interval`, `service_completed_successfully`).
- **CPU/RAM**: a run is one Renode per board; as a rough budget, 2–4 cores
  and up to 4 GB per concurrent run (`VHIL_WORKERS`; the default limits), plus
  ~2 GB for api + editor. 8 cores / 16 GB carries two workers comfortably. Disk: ~10 GB
  for images, plus traces (`runs`) and firmware builds (`fw`, ~1 GB per
  firmware ref built).
- **vcan** (recommended, not required today): `sudo modprobe vcan`, persisted
  with `echo vcan | sudo tee /etc/modules-load.d/vcan.conf`. Ubuntu cloud
  images ship it in `linux-modules-extra-$(uname -r)`. Needed only when a
  worker gets `NET_ADMIN` for SocketCAN scenarios (above).
- **Ports**: 80 and 443 (TCP; 443/UDP for HTTP/3) and 5443 (editor) open to
  the users. 80 must reach Caddy from the internet for Let's Encrypt's HTTP
  challenge, or 443 for TLS-ALPN; behind a firewall with no inbound access
  from the internet, use a DNS challenge (Caddy plugin) or an internal CA.
- **DNS**: an A/AAAA record for the domain (e.g. `vhil.<team-domain>`) to the
  host.
- **Outbound**: github.com (clone, fetch, push, firmware sources, OAuth),
  api.github.com, the image registry, Let's Encrypt. Workers and the
  workspace one-shot reach only `VHIL_EGRESS_ALLOW`, through `egress`.

## First-time setup

```sh
git clone https://github.com/isc-fs/IFS_vHIL ~/ifs-vhil-deploy   # for deploy/ only
cd ~/ifs-vhil-deploy
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
$EDITOR deploy/.env          # every variable is described there; no secrets in it
mkdir -p deploy/secrets
cp ~/github-app.pem deploy/secrets/github-app.pem
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > deploy/secrets/session_secret
$EDITOR deploy/secrets/github_client_secret           # the App's client secret
touch deploy/secrets/github_token                     # empty unless no App yet
sudo chown -R 10001:10001 deploy/secrets
sudo chmod 0500 deploy/secrets && sudo chmod 0400 deploy/secrets/*
docker compose -f deploy/compose.prod.yaml up -d
docker compose -f deploy/compose.prod.yaml ps          # all healthy, workspace exited 0
docker compose -f deploy/compose.prod.yaml logs workspace
curl -fsS https://vhil.example.org/api/health
```

`deploy/.env` is read automatically (it sits next to the compose file) and is
git-ignored, as is `deploy/secrets/`. In `deploy/.env` at least:
`VHIL_SITE`, `VHIL_EDITOR_SITE`, `VHIL_PUBLIC_URL`, `VHIL_EDITOR_URL`,
`VHIL_GITHUB_CLIENT_ID`, `VHIL_GITHUB_APP_ID`, `VHIL_WORKSPACE_REF` (a tag
or commit; the example's placeholder fails the workspace service) and
`VHIL_TAG` (+ `VHIL_DIGEST` / `VHIL_EDITOR_DIGEST`). The secrets are the
files above ([Secrets](#secrets)). The API refuses to start in github mode
without the OAuth client or with a short session secret.

The first run of a system whose firmware isn't built yet makes the worker
build it (minutes). To warm `fw` up front, do what the worker does on demand
(`vhil/worker.py`, `FirmwareResolver`):

```sh
docker compose -f deploy/compose.prod.yaml run --rm --no-deps worker bash -c \
    'python -m vhil.system build systems/ecu-ams.yaml --workdir /vhil/fw >> /vhil/fw/built.txt'
```

### The GitHub App (hand-off to an isc-fs org admin)

One App does login (its OAuth client) and pushes (its installation tokens).
Steps: [docs/development/web-app.md, "Creating the GitHub App"](development/web-app.md#creating-the-github-app-needs-an-isc-fs-org-admin),
with **Callback URL** `https://<domain>/auth/callback` (`VHIL_PUBLIC_URL` +
`/auth/callback`). The admin hands over, out of band (never in git, chat or
an issue): the App ID, the Client ID, a client secret, and the private key
`.pem`. They go into `deploy/.env` and `deploy/secrets/github-app.pem` and `deploy/secrets/github_client_secret`.

Until the App exists: `VHIL_AUTH=github` with an OAuth App for login (scope
`read:org`) and a fine-grained token (contents + pull requests write on
IFS_vHIL only) in `deploy/secrets/github_token` for pushes.

## Upgrade

```sh
cd ~/ifs-vhil-deploy && git pull               # compose/Caddyfile/scripts changes
$EDITOR deploy/.env                            # VHIL_WORKSPACE_REF / VHIL_TAG, if pinned
docker compose -f deploy/compose.prod.yaml pull
docker compose -f deploy/compose.prod.yaml up -d --force-recreate
```

`--force-recreate` re-runs `workspace` (fetch + checkout of the new ref) and
then restarts every service on that code: `up -d` alone re-runs `workspace`
but keeps the API's old process if its own config didn't change. A worker
stopped mid-run ends that run as `error` (SIGTERM, 30 s grace): check
`GET /api/runs?state=running` is empty first. Saved branches, the DB, traces,
firmware builds and certificates live in volumes and carry over. To roll
back, set `VHIL_WORKSPACE_REF` to the previous commit and recreate again.

`VHIL_WORKSPACE_REF` is required and should be a tag or commit: with a
branch, an upgrade (or any `up`) deploys whatever the branch is at that
moment.

**Upgrading from before the hardening** (services as root, secrets in
`deploy/.env`): move `VHIL_GITHUB_CLIENT_SECRET`, `VHIL_SESSION_SECRET` and
`VHIL_GITHUB_TOKEN` out of `deploy/.env` into the files of
[Secrets](#secrets) (the compose file no longer passes them), set
`VHIL_WORKSPACE_REF` to a tag or commit, and `up -d --force-recreate`:
`volume-owner` hands the existing volumes (and a host `VHIL_BACKUP_DIR`) to
uid 10001 on its first run.

## Backups and restore

The `backup` service writes a snapshot every `VHIL_BACKUP_INTERVAL_H` (24)
hours into `VHIL_BACKUP_DIR` (a host directory, or the `backups` volume) and
keeps the newest `VHIL_BACKUP_KEEP` (14):

```
<VHIL_BACKUP_DIR>/20261004T160832Z/vhil.db          SQLite backup API copy, integrity-checked
<VHIL_BACKUP_DIR>/20261004T160832Z/branches.bundle  the workspace's local branches
```

The DB is copied with SQLite's online backup API while the api and workers
keep writing: a raw copy of `vhil.db` without its `-wal` can lose or tear the
latest commits. A snapshot directory appears only once complete. Snapshot
directories are 0700 and their files 0600, owned by uid 10001: they hold
every run's scenario and summary and unpushed branches.

### Off-host copies

Copy `VHIL_BACKUP_DIR` off the host, **encrypted**, from root's cron, e.g.
with restic (client-side encryption; the repository password lives only on
this host and in the team's password manager):

```sh
export RESTIC_REPOSITORY=sftp:backup@backup-host:/srv/restic/ifs-vhil
export RESTIC_PASSWORD_FILE=/root/.restic-ifs-vhil      # 0400 root
restic backup /var/backups/ifs-vhil && restic forget --keep-daily 14 --keep-monthly 6 --prune
```

or `tar -C /var/backups/ifs-vhil -c . | age -r <recipient public key> > ifs-vhil-$(date +%F).tar.age`
and ship the `.age` file. Test a restore from the copy now and then. Traces
(`runs` volume) aren't included; copy them the same way if history matters.

```sh
dc="docker compose -f deploy/compose.prod.yaml"
$dc exec backup python /deploy/backup.py once      # a snapshot now
$dc exec backup python /deploy/backup.py list      # newest first
```

**Restore the DB** (the current one is kept as `vhil.db.before-restore-<time>`):

```sh
$dc stop api worker
$dc exec backup python /deploy/backup.py restore 20261004T160832Z
$dc start api worker
```

`start` re-runs `workspace` (a fetch) first; that's harmless. Runs created
after the snapshot are gone; their result directories stay in `runs`. When a
new run reuses one of their ids, the worker moves the old directory to
`/data/runs/.orphaned/<id>-<time>` first, so the new run starts empty; delete
`.orphaned` when you no longer need them.

**Restore saved branches** (after losing the `workspace` volume, or one
branch):

```sh
$dc run --rm --no-deps -v ifs-vhil-prod_backups:/backups:ro --entrypoint git api \
    -C /workspace fetch /backups/20261004T160832Z/branches.bundle 'refs/heads/*:refs/heads/*'
```

(With a host `VHIL_BACKUP_DIR`, mount that path instead of the volume.) A
branch that exists and has diverged from the bundle's copy is refused rather
than overwritten; add `+` to the refspec only when you mean it.

## Local test

[`deploy/smoke.sh`](../deploy/smoke.sh) brings the stack up in local mode and
checks it end to end, against the Colima VM on macOS
(`VHIL_DOCKER_CONTEXT=colima-vhil`, the default, as `scripts/vhil-docker.sh`)
or a Linux host (`VHIL_DOCKER_CONTEXT=default`):

```sh
scripts/vhil-docker.sh image       # local ifs-vhil / ifs-vhil-editor
scripts/vhil-docker.sh fw ecu      # once: ELFs it copies into the stack (no build)
deploy/smoke.sh [workspace-ref]    # default dev
```

It uses project `ifs-vhil-smoke`, plain HTTP on `127.0.0.1:18080` (app) and
`:15443` (editor), dev auth, empty secret files and one worker, then:
`/api/health` through the proxy, the editor through its auth gate, the
hardening at runtime (every service uid 10001 with a read-only root and no
capabilities; no secrets in the worker; the worker reaches neither the API,
the editor, the metadata address nor anything off the host but GitHub through
`egress`; the security headers), a save of `systems/ecu.yaml` to a
branch that survives the workspace service running again, a 500 ms run of
`systems/ecu.yaml` queued through the API and executed by the worker (passed,
CAN frames in its trace), a snapshot (0700/0600), one more run, a restore
(the later run gone), and the saved branch restored from the bundle. The
services run the workspace ref's code, so to test changes to `vhil/` push the
branch and pass it as the ref. `down -v` at the end
(`SMOKE_KEEP=1` leaves it up). About 2 minutes after a first clone.

Other local modes: `VHIL_SITE=localhost` / `VHIL_EDITOR_SITE=localhost:5443`
give HTTPS from Caddy's internal CA (`curl -k`, or trust the root in
`caddy-data`); `VHIL_AUTH=github` with a throwaway OAuth client checks the
gate (`/api/me` and the editor answer 401 without a session).

No CI job runs it: it needs the toolchain images (~4 GB to build), a clone of
GitHub and a firmware build or copy, minutes per run, for a stack that changes
rarely. Run it locally when `deploy/` or `docker/` change.

## Exit criterion check (M5, #13)

On the deployed app, in a browser, signed in as an isc-fs member:

1. **Compose**: open a system in the editor, change it, **Save** to a new
   branch (e.g. `feat/<you>-try`). The API validates it as
   `python -m vhil.system validate` does and commits `systems/<id>.yaml` on
   that branch in the workspace; **Open PR** pushes it with the App token.
2. **Run**: queue a run of the system and watch it live.
3. **Inspect**: the run page: frames with decoding, plots, artifacts.
4. **CI path**: on the pushed branch, the saved file runs unchanged:
   ```sh
   git fetch origin feat/<you>-try && git checkout FETCH_HEAD
   python -m vhil.system validate systems/<id>.yaml
   gh workflow run sim.yml --ref feat/<you>-try     # or the PR's own sim check
   ```
   and the `sim` job on the PR is green.

Today a run uses the system as checked out in the workspace
(`VHIL_WORKSPACE_REF`), not the copy saved on a branch (`POST /api/runs`
accepts only the workspace's ref, `vhil/server/runs.py`). So step 2 on the
edited system works once the branch is merged and the deployment follows it;
until runs at a saved branch are supported, run the system as deployed in
step 2 and let CI (step 4) run the edited copy.
