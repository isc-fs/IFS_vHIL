# The editor workspace — frontend plan

Decision record for the frontend refresh after M5
([architecture](m5-web-app.md)). The owner approved the direction below; each
frontend PR names the step of the [phased plan](#phased-plan) it implements
and follows the tokens, layout and rules here. Change this file when a
decision changes, in the same PR.

## Direction

One product, not a shell around a foreign app. Pipeline Manager (PM), carried
in this repo, becomes the whole vHIL workspace, built around the graph like an
IDE (VS Code's activity bar and docks, Simulink's canvas plus scopes):

- **The canvas is the centre and the selection.** Clicking a board opens its
  state; clicking a bus filters the bus monitor. Everything else is a dock
  around it: Scenario, State, Bus, Signals, Log, Debug, Problems.
- **Dark first, quiet and dense:** an engineering tool, not a dashboard.
  Neutral cool near-black surfaces, one cool blue for links, selection and
  focus, IFS red only for the brand mark and primary buttons.
- **Status is never colour alone.** Every fault and state carries text and a
  glyph; pins read HIGH/LOW. All numbers are tabular (monospace in data views).
- **Buses look like buses:** thin amber rails, wires coloured and patterned
  by interface type. Boards are 520 px two-column cards with a role band;
  devices are compact dashed cards; live state shows on nodes as text pills.
- **Four modes, one component tree:** DESIGN (topology editable), LIVE,
  REPLAY, PAUSED, named by a mode pill in a single 40 px top bar. PM's 60 px
  navbar, the shell header, the "Editor" heading, the ed-bar and the Save/PR
  aside all go.

What PM v0.5.2 (commit `04613679`, `PM_COMMIT` in `docker/editor.Dockerfile`)
offers without changes, checked against its
`resources/schemas/metadata_schema.json`: `metadata.interfaces`
(`interfaceColor`, `interfaceConnectionColor`, `interfaceConnectionPattern`),
`styles` (`color`, `icon`, `pill`), `welcome`, `newGraphNode`, `newNodeType`,
`backgroundColor`, `backgroundSize`, `movementStep`, `hideHud`,
`collapseSidebar`, `navbarItems`. Everything else (docks, theme variables,
node shapes) needs changes to PM itself: its `Home.vue` is only NavBar, Editor
and TerminalPanel, and its colours are compile-time SCSS.

## Owner decisions

| Question | Decision |
|---|---|
| Feature order | **Scenario builder → state panel → live session → debugger** |
| Where PM lives | **In this repo** (see [Pipeline Manager in the repo](#pipeline-manager-in-the-repo)), not a separate fork repository |
| Editor origin | Served **same-origin under `/editor/`** with the shell (one CSP, no second port) |
| Old shell pages | **Retired** once the workspace replaces them; old links redirect |
| PM's log terminal (hterm) | **Replaced** (xterm.js with its CSS file, or a plain virtualised log), so the editor runs under `style-src 'self'` |
| Themes | **Dark first**; the light theme is defined and AA-checked now, polished later |
| Scenario tests in CI | **Advisory** at first, not a gate on firmware PRs |
| Live sessions | **One live session per run**; other tabs on the same run are view-only |
| Brand | **IFS red**: `#e5243b` fills in dark, `#c8102e` in light |

## Pipeline Manager in the repo

PM v0.5.2 (commit `04613679`) is vendored under `editor/pipeline-manager/`
as a plain copy of `git archive`, not a subtree: a subtree, even squashed,
would keep upstream's 20 MB `examples/` in history, which the editor never
uses. It keeps its Apache-2.0 `LICENSE` (upstream has no `NOTICE`);
`README-VHIL.md` there names the commit, what was left out and how to move to
a new release.

- vHIL's changes are made in place, and every divergence from upstream is
  listed in `editor/pipeline-manager/CHANGELOG-VHIL.md`.
- The existing `docker/pm/bus-per-instance.patch` becomes the first commit on
  top of the vendored tree; `docker/editor.Dockerfile` builds from that
  directory instead of cloning and `git apply`ing.
- Keep the change surface small, about 8 upstream files; everything new goes
  in `frontend/src/vhil/`:
  - `components/Home.vue`: VhilTopBar in place of NavBar, VhilRail,
    VhilInspector, and VhilDock wrapping TerminalPanel as its Log tab.
  - `styles/_variables.scss`: each `$colour` becomes `var(--<token>, fallback)`
    on `tokens.css`, with its own names (`--bg-0`, `--fg`…), no `--vhil-`
    alias layer; `styles/style.scss` drops its Google Fonts `@import`.
  - `custom/CustomNode.vue`: a template branch on `additionalData.vhil.kind`
    (board, bus, device) plus a badge slot; `:focus-visible` instead of the
    `outline: none` rules.
  - `core/communication/remoteProcedures.ts` and `resources/api_specification`:
    only `vhil_set_theme`, `vhil_select`, `vhil_mode`. Live data never goes
    through PM's RPC.
  - `EditorManager.js`, `rpcCommunication.ts`: precompiled (standalone) Ajv
    validators, so `script-src 'self'` holds.
  - `externalApp/frontend.ts`: a real `targetOrigin` instead of `'*'` (or
    removed once same-origin); `backend/fastapi.py`: CSP and narrowed CORS.
- Rebase onto upstream only at tagged releases, bumping `FORMAT_VERSION` in
  `vhil/editor.py` with it. The image build is multi-stage and copies only
  `dist` and the Python package.
- The shell's `vtable.js`, `plot.js` and `decode.js` stay the one copy: the
  editor build takes them from `vhil/server/static/`, never a fork of them.

Renode needs no changes for the UI; the debugger uses its built-in per-machine
GDB server.

## Tokens

Defined once in `vhil/server/static/tokens.css`; the shell reads them now and
PM's SCSS will (step 4). `tests/js/tokens.test.mjs` holds both themes to WCAG
AA: 4.5:1 for text tokens on every surface, 3:1 for control borders, focus,
wires, roles and plot series.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg-0` | `#0f1115` | `#f6f7f9` | canvas, page |
| `--bg-1` | `#161a20` | `#ffffff` | panels, docks, sidebar |
| `--bg-2` | `#1d222a` | `#ffffff` | raised: node body, code |
| `--bg-3` | `#262c36` | `#eef0f4` | hover, node header |
| `--line` | `#2a3038` | `#dde1e7` | dividers |
| `--border-control` | `#5f6876` | `#80878f` | inputs, buttons (≥ 3:1) |
| `--fg` / `--fg-muted` | `#e6e9ee` / `#9aa3b1` | `#15181d` / `#5b6270` | text |
| `--fg-faint` | `#6b7380` | `#8a919c` | non-text only |
| `--brand` | `#e5243b` | `#c8102e` | fills only: mark, primary button, active tab |
| `--brand-text` | `#ff6b7f` | `#b00e28` | red text, if ever needed |
| `--accent` | `#7aa7ff` | `#2457c5` | links, interactive text |
| `--focus` | `#4c8dff` | `#2f6fe0` | focus ring, selection, plot cursor |

Brand, accent and focus are three jobs and three tokens: brand red never marks
a link, a selection or an error.

**Status** (always with a text label and a glyph):

| Token | Dark | Light | Glyph |
|---|---|---|---|
| `--status-queued` | `#959ca6` | `#5b6270` | hollow ring |
| `--status-building` | `#b197fc` | `#6741d9` | spinner |
| `--status-running` (LIVE) | `#4dabf7` | `#1864ab` | pulsing dot, static under reduced motion |
| `--status-ok` (passed, nominal, closed) | `#40c057` | `#237032` | check, filled square |
| `--status-warn` (transitional, PAUSED) | `#fcc419` | `#8a6500` | half-filled |
| `--status-failed` (test failed, latched fault) | `#ff6b6b` | `#c92a2a` | cross, 14 % tint, 3 px left bar |
| `--status-error` (infrastructure failed) | `#ff922b` | `#b8400d` | warning triangle |
| `--status-cancelled` | `#9aa3b1` | `#5b6270` | slash |
| `--status-stale` (no heartbeat) | `#959ca6` | `#5b6270` | dashed outline, "stale 1.2 s" |
| `--status-replay` | `#7aa7ff` | `#2457c5` | |

`error` (the run broke) and `failed` (the test said no) are different colours
on purpose.

**Wires** by interface type (set on the canvas through
`specification.metadata.interfaces` in `vhil/editor.py`):

| Type | Colour (dark) | Pattern |
|---|---|---|
| CAN | `#f59f00` amber | solid, 3 px (the backbone) |
| SPI, isoSPI | `#4dabf7` blue | solid, 2 px |
| I2C | `#3bc9db` cyan | solid, 2 px |
| SDMMC | `#9775fa` violet | solid, 2 px |
| UART | `#e599f7` | solid |
| GPIO | `#adb5bd` grey | dotted, 1.5 px |
| analog | `#20c997` teal | dashed, 2 px (not "ok" green) |

With more than one CAN bus, each takes `--can-1..4` (`#f59f00 #f783ac #63e6be
#ffd43b`) in system order, and its name is always written on the rail.
Widths, hover tooltips ("can_acu · 500 kbit/s · 3 nodes") and the LIVE dash
march need PM changes (step 8).

**Roles:** a 4 px band on the board node and a role chip, always with the
role as text: ECU `#e5243b`, AMS `#fab005`, uDV `#4dabf7`.

**Series** (plots only): `#4dabf7 #ff922b #69db7c #e599f7 #ffd43b #3bc9db`
(dark), `#1c7ed6 #b8400d #237032 #ae3ec9 #b08800 #0c8599` (light).

**Type.** Inter for the UI, JetBrains Mono for CAN IDs, payloads, addresses,
git refs and property values, both self-hosted as woff2 in
`vhil/server/static/fonts/` (the CSP has `font-src 'self'`, and the track has
no network). 13 px base, 12 px dense tables and dock rows, 11 px legends,
15 px/600 section titles, 18 px/600 the FSM state. Numeric columns use
`tabular-nums` and are right-aligned; cells don't wrap (ellipsis plus the
full value as a title).

**Spacing:** a 4 px grid (4/8/12/16/24/32). Radii: 4 px controls, 6 px board
nodes and panels, 10 px device cards, full for rails and pills. Rows 22 px
dense, 26 px comfortable.

## Layout

One app, same-origin under `/editor/`; every old hash route redirects in.
Minimum target 1366×768 at 125 % zoom with no horizontal page scroll; dock and
sidebar sizes are kept per viewer in `localStorage` (wrapped in try/catch).

1. **Top run bar, 40 px:** IFS mark; "system @ branch" with a dirty dot; one
   firmware chip per board (opens the ref picker that replaces the firmware
   aside); scenario dropdown; duration; Run (F5), Pause (Space), Stop
   (Shift+F5), Restart; mode pill; virtual clock "t=1.234 s · RTF 0.8x";
   Commit… and Open PR as dialogs; theme toggle; avatar.
2. **Activity rail, 44 px, and a 260 px sidebar (Ctrl+B):** Palette (PM's
   node tree), Systems (open one on the canvas, "View YAML" read-only),
   Scenarios, Runs (history of the open system; opening one enters REPLAY),
   Tests (scenario tests with JUnit results).
3. **Canvas:** board nodes with role band, mono sub-line "node 0x2 · FDCAN1",
   grouped pins and a live state pill; bus rails with frames/s in LIVE,
   dimmed when silent; device cards with a count pill. Topology is editable
   only in DESIGN; values stay editable in the other modes. Context menus:
   Watch symbol…, Set pin…, Debug this board; Set analog value on a model.
4. **Inspector, 320 px (an overlay below 1400 px):** DESIGN — properties,
   role, firmware, bootloader, write-protect; LIVE/REPLAY — the board's State
   card, watches, faults; PAUSED — registers, locals, stack; a bus — bitrate,
   netdev, IDs seen with their senders.
5. **Bottom dock (Ctrl+J, Ctrl+1..7), 38 % by default:** Scenario, State, Bus
   (Monitor, Trace, Transmit), Signals (uPlot: analog, state and digital
   lanes on one axis), Log (PM's terminal, moved here, plus UART/Renode log),
   Debug, Problems (validate output, click to jump to the node); Artifacts and
   Tests when the run has them. Collapsed, it is a 28 px strip that still
   shows the fault count.
6. **Status strip, 24 px:** virtual vs wall time, emulation speed, WS lag,
   frames/s and drops (`aria-live` polite, 1 Hz), active periodic stimuli
   with "stop all".

**Data plane.** Docks talk straight to the vHIL API on the same origin
(`/api/runs`, `/runs/{id}/live`, `/trace`, `/contract`, `/enums`, and a new
`/runs/{id}/session` WebSocket). PM's JSON-RPC relay carries only graph,
specification, selection and theme; high-rate data never goes through it or
through Ajv.

## Features

In the owner's order.

1. **Scenario builder** (Scenario dock tab; Scenarios sidebar view). A
   timeline on the Signals time axis (Ctrl+wheel zoom, drag with 1/5/10 ms
   snap): lanes per bus for CAN and per board for GPIO/analog — a diamond per
   `can_send`, a hatched bar per `can_periodic`, a step per gpio/analog — and
   a gutter of watches. Below it the row table: t | action (send, periodic,
   gpio, analog, watch, expect) | target | value. Frames are edited as
   decoded signals from the `.def` contracts (enums as selects, scalars with
   units and limits), with a raw hex fallback; each edit is validated
   server-side against the stimulus schema `vhil/worker.py` executes.
   `expect` rows ("by 800 ms AMS state == Precharge", "0x100 period < 120 ms")
   make a scenario a test. Scenario YAML lives next to the system and goes
   through the same Commit/PR dialogs; a pytest collector and a CI job run
   every scenario as the vHIL's own suite (advisory at first). After a run the
   lanes overlay the result: expected and actual together.
2. **State panel.** A per-firmware "state view" block in the catalogue, next
   to `can.contract`: `{label, source}` with source `frame:<msg>.<field>`,
   `symbol:<elf>` or `pin:<board.pin>`. Enum labels from the `.def` values
   and a new `GET /api/firmware/<id>/enums` (DWARF via `vhil/elf.py`,
   cached). The trace stays raw, so replay decodes as live does. Shown in
   three places: the State dock (a 340×420 card per board — FSM state at
   18 px with "for 312 ms" and the previous state, transition history,
   AIR+/AIR-/PRE/SDC as square pills with text, active faults with age,
   latched-cleared ones outlined, 4–8 key values with unit, sparkline and
   limit, staleness after 3× the period), the inspector, and the canvas node
   pill ("ECU · R2D") with a fault ring. No DWARF enum: the raw value with a
   "no enum" hint.
3. **Live session.** LIVE mode of the same workspace, on a normal
   `/api/runs` run with a bidirectional `/runs/{id}/session` WebSocket. Its
   ops are exactly the scenario stimulus kinds (`can_send`, `can_periodic`,
   `stop_periodic`, `gpio`, `analog`, `watch`); the worker applies them at the
   next slice boundary and echoes each into the trace as
   `{src: "stimulus", op_id}`, with a `{kind: "clock"}` heartbeat per slice
   (live poll 200 → 50 ms). One live session per run; other tabs view only.
   Bus Monitor (default): one row per (bus, id) with sender, dlc, per-byte
   cells that flash on change, count, measured vs contract period, jitter,
   age; rows ok, late or missing; stimulus frames marked. Trace: virtualised
   chronological log on a typed-array ring buffer, one render per frame, only
   visible rows decoded, follow mode that pauses on scroll-up. Transmit:
   message from the contract or raw id, generated field editors, hex synced
   both ways, once or every N ms, "+ to scenario". A 1 ms periodic asks for
   confirmation. Pins are `role=switch` with HIGH/LOW text. Every manual op is
   recorded with its virtual time, and "Save session as scenario" makes a
   bench exploration repeatable.
4. **Debugger** (Debug dock tab, following the canvas selection). Renode's
   per-machine GDB server; vhil runs gdb-multiarch in MI mode and proxies it
   over `/runs/{id}/gdb/{board}`. Source/disassembly, breakpoint gutter, a
   text-labelled continue/step/next/finish/break toolbar; registers, locals
   and stack in the inspector; watches shared with the State panel. Machines
   run in lockstep, so a breakpoint pauses all: the top bar reads
   "PAUSED · breakpoint in ams (bms_task.c:212)", Bus and Signals freeze with
   a banner, and timeouts that would have fired on hardware are listed on
   resume. The raw GDB console is xterm.js or a plain log, not hterm.

## Phased plan

1. **Canvas metadata** (no PM changes). `specification()` metadata in
   `vhil/editor.py`: wire colours and patterns per type, `styles` per
   category, `backgroundColor` `#0f1115`, `backgroundSize` 24,
   `movementStep` 8, `welcome`/`newGraphNode`/`newNodeType` off.
2. **Shell tokens.** `tokens.css` with both themes; `app.css` onto it;
   self-hosted Inter and JetBrains Mono; tabular numbers; the contrast test.
3. **Vendor PM into the repo** (no behaviour change). PM v0.5.2 at
   `04613679` under `editor/pipeline-manager/` with its LICENSE/NOTICE,
   bus-per-instance as the first commit on top, `CHANGELOG-VHIL.md`, the
   Dockerfile building from the directory (multi-stage, `dist` and the
   Python package only); a CI job that builds the editor image and runs
   `vhil.editor check` when the editor's inputs change.
4. **PM theme and accessibility base.** `_variables.scss` on `var(--<token>)`
   from `tokens.css`; no Google Fonts; `:focus-visible` rings; `?theme=` and
   `vhil_set_theme`. Playwright baselines (dark/light × 1366×768/1920×1080)
   and an axe scan of shell and editor.
5. **One run model.** The editor's Run creates a normal `POST /api/runs` run
   and follows `/live`; the in-process 2 s `EditorMethods.dataflow_run` (on
   `$VHIL_*_ELF`, no history) goes.
6. **Same origin and CSP.** PM under `/editor/` behind Caddy with socket.io
   proxied; precompiled Ajv; narrowed CORS; a real `targetOrigin`;
   `default-src`/`script-src`/`style-src 'self'` staged through Report-Only,
   with a CSP-violation test. hterm replaced.
7. **Workspace layout.** Top bar, activity rail and sidebar (Palette,
   Systems), inspector, bottom dock (Log, Problems), status strip; Save and PR
   as dialogs; the shell's editor grid, header and heading go; keyboard map
   (F5, Ctrl+B, Ctrl+J, Ctrl+1..7, `?` overlay).
8. **Node shapes.** Board card with role band and mono sub-line, bus rail,
   dashed device card, badge slot; wire widths and hover tooltip.
9. **Runs, REPLAY, Bus tab foundation.** Runs sidebar view; REPLAY from
   `/trace` with the clock as scrubber; `#/runs` and `#/systems` redirect in.
   Bus Monitor and Trace on `vtable.js` with a ring buffer, rAF batching, row
   reuse, visible-only decode; perf test: 5k frames/s replay, p95 frame time
   under 16 ms.
10. **Scenario model and table** (feature 1a). Shared stimulus/expect schema
    (the same ops that drive live), the table with contract-driven editors,
    server-side validation, YAML through Commit/PR.
11. **Timeline and vHIL tests** (feature 1b). Lanes, drag/snap, watch gutter,
    expected-vs-actual overlay; the worker evaluates expects; a pytest
    collector and CI job (advisory) with JUnit into the Tests view, green for
    AMS and ECU before going on.
12. **State sources** (feature 2a). Catalogue state-view blocks for AMS and
    ECU; `GET /api/firmware/<id>/enums`; the mid-run `watch` op.
13. **State UI** (feature 2b). State cards, inspector card, node pill and
    fault ring; FSM and digital lanes in `plot.js`.
14. **Session channel** (feature 3a). `/runs/{id}/session`, acks echoed into
    the trace, slice-boundary application, clock heartbeat, 50 ms poll; tests
    for mid-run ops and the determinism of a recorded session.
15. **Live UI** (feature 3b). LIVE mode with topology locked; Transmit with
    periodic management and "stop all"; pin switches and analog set; wire
    frames/s and a throttled dash march; Record → scenario.
16. **GDB plumbing** (feature 4a). Renode GDB server per machine on demand,
    an MI proxy per board, lockstep pause in the clock heartbeat and top bar.
17. **Debug tab** (feature 4b). Source, breakpoints, stepping, inspector
    registers/locals/stack, shared watches, frozen-bus banner, list of
    would-have-fired timeouts.
18. **Cleanup before M8.** Old shell pages go (`index.html` redirects);
    `CHANGELOG-VHIL.md` complete; the session, scenario and trace schemas
    documented as the stable contract for external simulation platforms.

## Avoid

- High-rate frames, state deltas or GDB events through PM's socket.io → TCP
  relay with per-message Ajv. Live data uses direct same-origin WebSockets.
- Two run paths: `dataflow_run` must not live alongside `/api/runs`.
- Building features in the old iframe/aside layout and restyling later: the
  theme and layout steps land first.
- Patch piles in the Dockerfile, or changes scattered over PM's core; keep
  the few seams and put new code in `frontend/src/vhil/`.
- A second UI framework next to Vue, or Vue rewrites of `vtable.js`,
  `plot.js`, `decode.js`.
- Brand red for links, selection or errors; colour-only status (bare LED dots,
  the same green for analog wires and "ok").
- Inline `<style>` or script, `'unsafe-inline'`, `'unsafe-eval'`.
- Removing focus outlines, icon-only buttons without labels, blinking or
  shaking fault animations; ignoring `prefers-reduced-motion`.
- Re-rendering the frame table per WebSocket message, or unbounded live
  arrays.
- Manual live actions not recorded with virtual timestamps.
- Google Fonts or CDN assets.
- Breaking `#/runs/N/frames` and `#/systems` links: redirect them.
- Letting the frontend work delay the vHIL's own scenario test suite.
