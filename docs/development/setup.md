# Development setup and workflow

## Environment

Linux, or WSL2 on Windows. The Renode SocketCAN bridge used from Phase 1 is
Linux-only.

| Tool | Version | Where |
|---|---|---|
| Renode | 1.17.0 portable | `renode-1.17.0.linux-portable.tar.gz` from the [Renode releases](https://github.com/renode/renode/releases) |
| Arm GNU Toolchain | 14.2.Rel1 | developer.arm.com (pinned by IFS_HIL's recipes) |
| Python | ≥ 3.10 | venv with `robotframework==6.1 robotframework-retryfailed==0.2.0 psutil==5.9.4 "pyyaml==6.0.*" "telnetlib3==2.0.*"` (Renode 1.17's own `tests/requirements.txt`) |
| CMake | ≥ 3.22 | distro package |

CI ([`.github/workflows/ecu-smoke.yml`](../../.github/workflows/ecu-smoke.yml))
installs exactly these on `ubuntu-latest`. It is the reference setup.

## Branching

`main` is the release branch. Feature work lands on `dev` first, and `dev`
merges to `main` when a release is cut. Same model as IFS_HIL.

Branch names are `<type>/<kebab-slug>`: `feat/`, `fix/`, `docs/`, `chore/`,
`test/`. Always branch **from `dev`**; PRs target `dev`.

## Commits

- One commit per concern.
- First line ≤ 72 chars, imperative mood, area prefix:
  `feat(platform): …`, `fix(ecu): …`, `docs: …`.
- Body wrapped at 72, explaining the *why*.
- No AI co-author lines.

## Issues

Work is planned and tracked in issues. Each phase in
[`proposal.md`](../proposal.md#5-plan) has one issue with a checklist and an
exit criterion (#1–#4). Bugs and model gaps get their own issue, labelled
`bug` or `enhancement`.

- A PR says which issue it advances: `Part of #1`.
- PRs merge into `dev`, so GitHub's `Closes #N` does not fire. Close the
  issue by hand once its exit criterion is met, linking the PRs.
- A test that passes virtually but fails on the physical bench is a
  **model gap**. Open an issue for it; don't skip the test to make it
  green.

## Pull requests

- **Summary** (1–3 bullets) and **Test plan** (`- [ ]` / `- [x]`).
- Small and thematic. Draft PRs are fine for blocked work.
- Green CI before merge; merge commit, not squash; delete the branch
  after merge.

## Release process

`dev` → `main` via a merge PR once a phase's exit criterion is met. Tag the
merge commit (`v0.1.0` = Phase 1, …).
