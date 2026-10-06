# IFS_vHIL — virtual hardware-in-the-loop

Virtual hardware-in-the-loop bench: runs emulated ISC boards so HIL suites can
run in CI without physical hardware.

The emulated boards run the same firmware images
[IFS_HIL](https://github.com/isc-fs/IFS_HIL) flashes to the real ones,
bootloader included. A test that passes here has run the car's software, not a
special build. Every push gets HIL-style testing, in parallel, with no hardware
attached. Work that needs real physics stays on the physical bench: analog
accuracy, real power, flash wear and bus electrics.

**What it does today**

- **Systems as data.** A system file places boards, gives each its role and
  firmware, and wires them together over CAN buses and pins.
- **Tests in virtual time.** Native test suites, plus IFS_HIL's own suites run
  unmodified against the virtual boards.
- **A web app.** Compose a system in the editor, save it to a branch, run it,
  and inspect the result: decoded CAN frames, plots, logs and artifacts.

**Where it's going:** a shared tool where engineers compose whole cars from the
team's boards, point each one at a branch of its firmware, and test the result
before it reaches a bench. Hardware combinations are bounded by what is
modelled, not by a PCB. See [`docs/vision.md`](docs/vision.md) for the roadmap.

**Contributing:** `dev` is the trunk and `main` is release-only. Branch
`feat/` `fix/` `docs/` `chore/` `test/` off `dev` and open a PR back into it.
See [`docs/development/setup.md`](docs/development/setup.md), and
[`CLAUDE.md`](CLAUDE.md) for the operating model.

## Try it

Everything runs in Docker; nothing is installed on the host. On macOS the
wrapper starts a small VM for it.

```sh
scripts/vhil-docker.sh vm            # once, on macOS: the VM
scripts/vhil-docker.sh image         # once: the images
scripts/vhil-docker.sh fw ecu ams    # build the firmware each system declares
scripts/vhil-docker.sh unit          # host-only checks; validates every system
scripts/vhil-docker.sh smoke ecu     # boot a board and check what it says on CAN
scripts/vhil-docker.sh sim           # the native test suites, in virtual time
scripts/vhil-docker.sh ifs-hil       # IFS_HIL's own suite against the virtual boards
scripts/vhil-docker.sh server        # the web app on http://localhost:8080
```

Pinned tool versions, running without Docker, and every job the wrapper runs
are in [`docs/development/setup.md`](docs/development/setup.md). Deploying the
web app for the team is in [`docs/deploy.md`](docs/deploy.md).

A system file is the source of truth: edit `systems/*.yaml`, never a generated
script. `python -m vhil.system validate <system>` checks one against the schema
and the catalogue.

Tests the virtual bench can't pass yet are listed in
[`configs/gaps.yaml`](configs/gaps.yaml) with the reason and issue; they are
reported as skips, never silently dropped. If the firmware touches hardware the
bench doesn't model, and that isn't explained in
[`configs/peripherals.yaml`](configs/peripherals.yaml), the run fails even when
every test passed.

## Layout

```
systems/                  Systems as data: boards, their roles and firmware, the buses between them
catalog/platforms/        Emulated MCUs
catalog/boards/           Boards: a platform, its connectors and pins, and the roles it can take
catalog/firmware/         Firmware sources: repo, default ref, build recipe
catalog/models/           Devices that attach to a board: battery monitors, sensors, SD cards
schemas/                  The schema every catalogue entry and system is validated against
platforms/, models/       The emulator-side descriptions of those MCUs and devices
vhil/                     Generator, simulation API, virtual broker, web app server and worker
tests/unit/               Host-only checks of the catalogue, systems, generator and web app
tests/sim/                Native test suites, run in virtual time
tests/*_smoke.robot       Smoke checks per system
configs/gaps.yaml         IFS_HIL tests the virtual bench can't pass yet, and why
configs/peripherals.yaml  Hardware the firmware may touch that isn't modelled, and why
scripts/                  Docker wrapper and helpers
docker/, deploy/          Images, local stack, and the deployment of the web app
docs/                     Vision, design, development, deployment, backplane references
.github/workflows/        CI: unit, smoke, native suites, IFS_HIL suites
```
