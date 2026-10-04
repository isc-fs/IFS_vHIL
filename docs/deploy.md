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
                      ▼                  ▼
                     api ─────────────  editor (Pipeline Manager + vhil.editor)
                      │  SQLite (WAL)          │
          volume db ──┤                        │
          volume runs ┤◀── worker × VHIL_WORKERS (Renode, unprivileged)
                      │         └── volume fw (firmware builds)
       volume workspace (the app's IFS_vHIL clone) ◀── workspace (one-shot: clone/fetch/checkout)
                      └── backup (DB + saved branches → backups)
```

| Service | Image | What it does |
|---|---|---|
| `workspace` | `ifs-vhil` | One-shot, runs before the others on every `up`: clones IFS_vHIL into the `workspace` volume if empty, `git fetch`es, checks out `VHIL_WORKSPACE_REF` detached ([`deploy/workspace.sh`](../deploy/workspace.sh)) |
| `api` | `ifs-vhil` | `python -m vhil.server`: the shell, systems read/save, runs, inspect, login. Healthcheck `GET /api/health` |
| `worker` | `ifs-vhil` | `python -m vhil.worker`, `VHIL_WORKERS` replicas: claims queued runs, runs Renode, builds missing firmware into `fw` |
| `editor` | `ifs-vhil-editor` | Pipeline Manager + `vhil.editor`, embedded by the shell |
| `proxy` | `caddy:2.11.6-alpine` | TLS, the only published ports: 80, 443 (app), 5443 (editor) |
| `backup` | `ifs-vhil` | Snapshots the DB and the saved branches every `VHIL_BACKUP_INTERVAL_H` ([`deploy/backup.py`](../deploy/backup.py)) |

Every long-running service has `restart: unless-stopped`, CPU/memory limits
(`VHIL_*_CPUS` / `VHIL_*_MEMORY`) and json-file logs rotated at 10 MB × 5.

**The code comes from the workspace clone, not the image.** The images carry
the toolchain (Renode, Arm GCC, Python deps, Pipeline Manager) as CI's runners
do; `vhil/`, `catalog/`, `schemas/` and `systems/` are the workspace's working
tree at `VHIL_WORKSPACE_REF`. That keeps code, catalogue and systems from the
same commit, as in CI. The operator's checkout only provides
`compose.prod.yaml`, the `Caddyfile` and the scripts in `deploy/`.

**Workers need no privileges.** Run scenarios and `tests/sim` use Renode's
in-process CAN hubs, not vcan, so workers run unprivileged on the default
bridge network, each in its own network namespace: concurrent runs can't
collide on Renode monitor ports or CAN links (with the dev compose's host
network they share one namespace). If a scenario ever bridges to SocketCAN
(`--vhil-socketcan`, IFS_HIL suites), give the worker `cap_add: [NET_ADMIN]`
so it creates its vcan links in its own namespace; that needs the `vcan` module
loaded on the host (a container can't load modules), but not `privileged`.

**The editor** has no login of its own. Caddy serves it on a second port of
the app's host and checks every request (WebSockets included) against the
API's `/api/me` first: a signed-in org member (or anyone in dev mode) passes,
everyone else gets 401. The session cookie reaches it because cookies are
scoped to the host, not the port.

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

Then `VHIL_TAG=$tag` in `deploy/.env`. Pin a tag in production rather than
`latest`, so an upgrade is a deliberate edit. Without a registry, build on the
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
  api.github.com, the image registry, Let's Encrypt.

## First-time setup

```sh
git clone https://github.com/isc-fs/IFS_vHIL ~/ifs-vhil-deploy   # for deploy/ only
cd ~/ifs-vhil-deploy
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
$EDITOR deploy/.env          # every variable is described there
mkdir -p deploy/secrets && chmod 700 deploy/secrets
cp ~/github-app.pem deploy/secrets/github-app.pem && chmod 600 deploy/secrets/github-app.pem
docker compose -f deploy/compose.prod.yaml up -d
docker compose -f deploy/compose.prod.yaml ps          # all healthy, workspace exited 0
docker compose -f deploy/compose.prod.yaml logs workspace
curl -fsS https://vhil.example.org/api/health
```

`deploy/.env` is read automatically (it sits next to the compose file) and is
git-ignored, as is `deploy/secrets/`. In `deploy/.env` at least:
`VHIL_SITE`, `VHIL_EDITOR_SITE`, `VHIL_PUBLIC_URL`, `VHIL_EDITOR_URL`,
`VHIL_GITHUB_CLIENT_ID`, `VHIL_GITHUB_CLIENT_SECRET`, `VHIL_SESSION_SECRET`,
`VHIL_GITHUB_APP_ID` (or, until the App exists, `VHIL_GITHUB_TOKEN`), and
`VHIL_TAG`. The API refuses to start in github mode without the OAuth client
or with a short session secret.

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
`.pem`. They go into `deploy/.env` and `deploy/secrets/github-app.pem`.

Until the App exists: `VHIL_AUTH=github` with an OAuth App for login (scope
`read:org`) and `VHIL_GITHUB_TOKEN` (fine-grained, contents + pull requests
write on IFS_vHIL only) for pushes.

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

With `VHIL_WORKSPACE_REF=dev` an upgrade deploys whatever `dev` is at that
moment; pin a tag or commit for a deployment that only changes on purpose.

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
latest commits. A snapshot directory appears only once complete. Copy
`VHIL_BACKUP_DIR` off the host (rsync/restic from cron); traces (`runs`
volume) aren't included, copy them the same way if history matters.

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
after the snapshot are gone; their trace directories stay in `runs` and are
overwritten when the IDs are reused (delete `/data/runs/<id>` for ids above
the restored maximum if that matters).

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

It uses project `ifs-vhil-smoke`, plain HTTP on `localhost:18080` (app) and
`:15443` (editor), dev auth and one worker, then: `/api/health` through the
proxy, the editor through its auth gate, a save of `systems/ecu.yaml` to a
branch that survives the workspace service running again, a 500 ms run of
`systems/ecu.yaml` queued through the API and executed by the worker (passed,
CAN frames in its trace), a snapshot, one more run, a restore (the later run
gone), and the saved branch restored from the bundle. `down -v` at the end
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
