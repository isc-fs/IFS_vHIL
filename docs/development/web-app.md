# Web app: login and GitHub credentials

The shared web app (`python -m vhil.server`, M5, [architecture](../architecture/m5-web-app.md))
has two auth modes, picked by `VHIL_AUTH`:

| Mode | Who can use it | For |
|---|---|---|
| `github` (default) | members of `VHIL_GITHUB_ORG` (default `isc-fs`), after GitHub login | the shared deployment |
| `dev` | anyone who reaches the port, as the fixed user `dev` | a local checkout: `scripts/vhil-docker.sh server`, `docker compose -f docker/compose.yaml up` |

Unset `VHIL_AUTH` means `github`: the server fails closed, and without the
GitHub settings below it refuses to start. Dev mode needs `VHIL_AUTH=dev`, and
`python -m vhil.server` then refuses to listen on anything but loopback
(`--host 127.0.0.1`, the default) unless `VHIL_ALLOW_DEV_ON_NETWORK=1` says the
port is reachable from this machine only some other way: a container whose
port is published on the host's `127.0.0.1` (what `vhil-docker.sh server` and
`docker/compose.yaml` do), or a test. It logs a warning whenever dev mode
starts. Never set it on a host.

## How it works

- **Login** (`vhil/server/auth.py`): `/auth/login` sends the browser to
  GitHub's authorize page with a random `state` and a PKCE challenge, both
  kept in a short-lived signed cookie. `/auth/callback` checks the state,
  exchanges the code (with the PKCE verifier), reads `GET /user` and
  `GET /user/memberships/orgs/{org}` with the user's token, and accepts only an
  `active` membership (anything else: 403). The user's GitHub token is used for
  that check and discarded. `/auth/logout` clears the session.
- **Session**: a cookie `vhil_session` holding the user's login, name and
  avatar, signed with HMAC-SHA256 under `VHIL_SESSION_SECRET` (HttpOnly,
  SameSite=Lax, Secure when `VHIL_PUBLIC_URL` is https). It lasts 12 h; after
  that the user logs in again, which re-checks org membership. Changing
  `VHIL_SESSION_SECRET` logs everyone out.
- **Enforcement**: one ASGI middleware over the whole app. Every request under
  `/api/` and every WebSocket needs a session, except `/api/health`; the
  shell (`/`, `/static/`) stays open and goes to the login on a 401. A new
  route is covered without doing anything. To exempt one:
  `auth.allow_anonymous(app, "/api/thing")` (exact path) or `"/api/thing/"`
  (prefix). Routes read the user with `Depends(auth.current_user)`.
- **CSRF**: POST/PUT/PATCH/DELETE under `/api/` are refused when the browser
  marks them cross-site (`Origin` not this site, or `Sec-Fetch-Site:
  cross-site`), in both modes. In github mode they must also carry
  `X-CSRF-Token`: a token derived from the session, in the `vhil_csrf` cookie
  and in `GET /api/me`. The shell's `api(path, {method, ...})` helper in
  `static/app.js` adds it; use that helper for writes.
- **Ownership**: a run records who started it (`owner`: the login; `dev` in
  dev mode), shown in the history and on the run page. Only its owner or an
  admin (`VHIL_ADMINS`) may cancel it (403 otherwise; an admin's cancel is
  logged). A save records its saver as a `Vhil-User: <login>` trailer on the
  commit (`vhil/server/gitstore.py`); a save that would move a branch whose
  tip someone else saved (its trailer, else its GitHub noreply author; a tip
  with neither counts as someone else's) is 409 `{errors, owner, takeover:
  true}` unless the saver is an admin or sends `takeover: true`, which is
  logged and recorded as a `Vhil-Takeover-From` trailer. The Editor asks before
  it retries with `takeover`. Dev mode has one user and no ownership checks.
- **Headers** (`vhil/server/security.py`): every response carries a strict
  Content-Security-Policy (`default-src 'self'`, no inline script or style,
  `connect-src` this site and its WebSocket, `frame-src` this site and
  `VHIL_EDITOR_URL`'s origin, `frame-ancestors 'self'`), `X-Frame-Options:
  SAMEORIGIN`, `X-Content-Type-Options: nosniff` and `Referrer-Policy:
  same-origin`. So the shell's code has no inline `<script>`, `style=""` or
  `on*=` handlers: set styles through `el.style` and handlers with
  `addEventListener`. Run artifacts (`/api/runs/{id}/artifacts/…`) are never
  rendered: text as `text/plain; charset=utf-8`, anything else as an
  attachment, with `Content-Security-Policy: sandbox`.
- **GitHub App** (`vhil/server/github_app.py`): firmware clones and
  system-file pushes / PRs use the App, never a person's token.
  `app.state.github_app.token_for("isc-fs/IFS08-CE-ECU")` returns an
  installation token narrowed to that repository with contents read;
  `token_for("isc-fs/IFS_vHIL", write=True)` one with contents + pull
  requests write. Tokens (1 h) are cached and renewed 5 min before they
  expire. `app.state.github_app` is `None` when the App isn't configured.

## Environment

On a host these come from `deploy/.env` ([`docs/deploy.md`](../deploy.md),
[`deploy/.env.example`](../../deploy/.env.example)), and the secrets from
files ([docs/deploy.md, "Secrets"](../deploy.md#secrets)): each secret below
is also read from the file named by `<NAME>_FILE`, which wins over `<NAME>`.

| Variable | Mode | Meaning |
|---|---|---|
| `VHIL_AUTH` | both | `github` (default) or `dev` |
| `VHIL_ALLOW_DEV_ON_NETWORK` | dev | `1`: allow dev mode off loopback (a container published on 127.0.0.1 only; tests) |
| `VHIL_ADMINS` | github | comma-separated GitHub logins that may cancel any run and save over any branch |
| `VHIL_GITHUB_ORG` | github | org whose members may log in, and where the App is installed (default `isc-fs`) |
| `VHIL_GITHUB_CLIENT_ID` | github | OAuth client ID (from the GitHub App, or an OAuth App) |
| `VHIL_GITHUB_CLIENT_SECRET` (`_FILE`) | github | its client secret |
| `VHIL_SESSION_SECRET` (`_FILE`) | github | ≥ 32 random characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `VHIL_PUBLIC_URL` | github | the URL users open, e.g. `https://vhil.example.org` (no trailing `/`): builds the callback URL, the allowed `Origin` and the cookies' Secure flag. Unset: taken from the request (fine on localhost) |
| `VHIL_GITHUB_APP_ID` | either | the GitHub App's ID; unset = no App tokens |
| `VHIL_GITHUB_APP_KEY_FILE` | either | path to the App's private key `.pem` (`VHIL_GITHUB_APP_KEY`, the older name, still works) |
| `VHIL_GITHUB_TOKEN` (`_FILE`) | either | interim push token until the App exists |

The server refuses to start in github mode without the client ID/secret or
with a short session secret. Keep secrets and the `.pem` out of git: files
mounted into the container (compose secrets), not environment values.

## Creating the GitHub App (needs an isc-fs org admin)

One GitHub App does both jobs: users log in through it, and the server uses
its installation tokens. (A separate OAuth App for login works too; then the
GitHub App only needs the repository permissions.)

1. isc-fs → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Name `IFS vHIL`; Homepage URL = `VHIL_PUBLIC_URL`; **Callback URL** =
   `VHIL_PUBLIC_URL/auth/callback` (add `http://localhost:8080/auth/callback`
   too for local github-mode testing). Leave "Request user authorization
   during installation" off. **Webhook**: off (not used).
3. Permissions:
   - Repository → **Contents: Read and write**. Tokens for firmware repos are
     narrowed to read when issued; write is only ever requested for IFS_vHIL.
   - Repository → **Pull requests: Read and write** (system-file PRs on
     IFS_vHIL).
   - Repository → **Metadata: Read** (mandatory).
   - Organization → **Members: Read** (the login's membership check).
4. "Where can this GitHub App be installed?": **Only on this account**.
   Create.
5. On the App's page: note the **App ID** (`VHIL_GITHUB_APP_ID`) and the
   **Client ID** (`VHIL_GITHUB_CLIENT_ID`); **Generate a new client secret**
   (`VHIL_GITHUB_CLIENT_SECRET`); **Generate a private key** and store the
   `.pem` on the host (`VHIL_GITHUB_APP_KEY`).
6. **Install App** → isc-fs → **Only select repositories**: IFS_vHIL and the
   firmware repositories systems build from (today `IFS08-CE-ECU`,
   `IFS08-CE-AMS`, `stm32-can-bootloader`; see `systems/*.yaml` and
   `configs/firmware/`). Add a repository here when a system starts using it.

With an OAuth App for login instead: callback URL as above, and the server
asks for scope `read:org` (needed for the membership check); the GitHub App
can then drop Members: Read.

## Trying github mode locally

```sh
export VHIL_AUTH=github VHIL_GITHUB_CLIENT_ID=… VHIL_GITHUB_CLIENT_SECRET=… \
       VHIL_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
scripts/vhil-docker.sh server     # http://localhost:8080 → GitHub login
```

`vhil-docker.sh server` passes the variables above into the container when
they are set. The container sees the repository at `/work` and the
`vhil-data` volume at `/vhil`, so `VHIL_GITHUB_APP_KEY` must be a path under
one of them (e.g. a git-ignored `/work/results/app.pem`), not a host path.

`tests/unit/test_auth.py` covers the flow with GitHub mocked; nothing in the
test suite talks to GitHub.
