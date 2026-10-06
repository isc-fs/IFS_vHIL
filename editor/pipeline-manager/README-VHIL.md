# Pipeline Manager, vendored

Antmicro's [Kenning Pipeline Manager](https://github.com/antmicro/kenning-pipeline-manager),
the system editor's frontend and its server, carried in this repo
([editor workspace plan](../../docs/architecture/editor-workspace.md)).

- **Upstream:** tag `v0.5.2`, commit `04613679deecea5eab0de43c7229b8075e4da912`.
- **Licence:** Apache-2.0, upstream's [`LICENSE`](LICENSE), unchanged. Upstream
  ships no `NOTICE` file.
- **Our changes** are made in place and every one is listed in
  [`CHANGELOG-VHIL.md`](CHANGELOG-VHIL.md).
- **Built by** `docker/editor.Dockerfile` (`scripts/vhil-docker.sh image`).

## How it was imported

A plain copy of `git archive 04613679` (not `git subtree`), so the paths left
out below never enter this repo's history; a subtree, even squashed, would
store upstream's 20 MB `examples/` for good. Left out, none needed to build or
run the editor:

| Path | Why |
|---|---|
| `examples/` | 20 MB of sample graphs (19 MB is one specification); used only by upstream's own test suites |
| `docs/`, `img/` | upstream's Sphinx documentation and README images |
| `.github/`, `.ci.yml`, `.pre-commit-config.yaml` | upstream's CI and hooks |

Upstream's Python tests (`pipeline_manager/tests/`) and Playwright specs
(`pipeline_manager/frontend/tests/`) are kept as source but need `examples/`
to run: fetch it from upstream at the same commit.

## Moving to a new upstream release

Only onto tagged releases, bumping `FORMAT_VERSION` in `vhil/editor.py` with
it. From a clone of upstream:

```sh
git diff v0.5.2 <new-tag> -- . ':!examples' ':!docs' ':!img' ':!.github' \
  | git -C <IFS_vHIL> apply -3 --directory=editor/pipeline-manager
```

then resolve conflicts against `CHANGELOG-VHIL.md`, and update the commit
above and `PM_REF`/`PM_COMMIT` in `docker/editor.Dockerfile`.
