# vHIL changes to Pipeline Manager

Every divergence of this tree from upstream v0.5.2 (`04613679`), newest last.
Paths are relative to this directory. Keep it complete: it is what a move to
a new upstream release is checked against ([README-VHIL.md](README-VHIL.md)).

## Left out of the import

`examples/`, `docs/`, `img/`, `.github/`, `.ci.yml`,
`.pre-commit-config.yaml` (README-VHIL.md says why). Nothing else differs
from `git archive 04613679` except as listed below.

## Added

- `README-VHIL.md`, `CHANGELOG-VHIL.md`: this file and its neighbour.

## Changed

1. **bus-per-instance** (`pipeline_manager/frontend/src/core/NodeFactory.js`,
   `createBaklavaInterfaces`). Each node instance gets its own copy of an
   interface's `bus` object (and its `stubs`). Upstream copies the type's
   interface with `Object.assign`, so every node of a type shared one `bus`
   and held the stubs of the last one loaded: a graph with two CAN buses lost
   all but the last bus's stubs ("Missing dst s:<bus>:0"). Not fixed upstream
   as of v0.5.2 / main. Was `docker/pm/bus-per-instance.patch`.
