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
- `pipeline_manager/frontend/src/vhil/`: vHIL's own frontend code (the workspace, theme,
  validators, log view, below); `src/vhil/shell/` is filled by the image
  build and `src/vhil/validators/` by `build-validators.mjs`, neither in git.

## Changed

1. **bus-per-instance** (`pipeline_manager/frontend/src/core/NodeFactory.js`,
   `createBaklavaInterfaces`). Each node instance gets its own copy of an
   interface's `bus` object (and its `stubs`). Upstream copies the type's
   interface with `Object.assign`, so every node of a type shared one `bus`
   and held the stubs of the last one loaded: a graph with two CAN buses lost
   all but the last bus's stubs ("Missing dst s:<bus>:0"). Not fixed upstream
   as of v0.5.2 / main. Was `docker/pm/bus-per-instance.patch`.
2. **Theme on the vHIL design tokens** (step 4 of the
   [editor workspace plan](../../docs/architecture/editor-workspace.md)).
   - `pipeline_manager/frontend/styles/_variables.scss`: the colour variables
     are `var(--<token>, <dark value>)` on `vhil/server/static/tokens.css`
     instead of compile-time hex, so a theme switch recolours the editor at
     run time. Upstream → token: `$white` #ffffff → `--fg`; `$gray-100`
     #999999 → `--fg-muted`; `$gray-200` #6f6f6f → `--border-control`;
     `$gray-300` #606060 → `--fg-faint`; `$gray-400` #4a4a4a → 12 % `--fg`
     over `--bg-3`; `$gray-500` #343434 → `--line`; `$gray-600` #1d1d1d →
     `--bg-2`; `$gray-700` #151515 → `--bg-1`; `$black` #0f0f0f → `--bg-0`;
     `$gold` → `--status-warn`; `$red`, `$red-dark` → `--status-failed`;
     `$green` #00E58D → `--accent`; new `$focus` → `--focus`. The fallbacks
     are the dark values (`tests/unit/test_editor_theme.py` holds them to
     tokens.css). The token names are tokens.css's own (`--bg-0`, `--fg`…),
     not a `--vhil-` alias of them: one name per token, and Pipeline Manager
     uses none of them.
   - Sass colour maths can't take a `var()`: `color.adjust($green, $alpha)`
     (`styles/_editor.scss`), `#{$gray-600}E6` / `80` hex alpha suffixes
     (`styles/_node.scss`, `styles/_markdown_style.scss`,
     `components/GraphDetails.vue`, `Settings.vue`, `LoadingScreen.vue`,
     `menu/ParentMenu.vue`) and `rgba($…)` (`menu/WelcomeMenu.vue`) are
     `color-mix(in srgb, … N%, transparent)` with the same alpha.
   - `resources/schemas/metadata_schema.json`: the canvas `backgroundColor`
     default is `var(--bg-0, #0f1115)` (was `#0f0f0f`), so the canvas follows
     the theme.
   - `src/vhil/theme.css`: on the light theme, the SVG icons' hard-coded
     white `fill`/`stroke` attributes take `--fg`.
3. **Self-hosted fonts.** `styles/style.scss` drops the Google Fonts
   `@import` (Montserrat, Roboto, Roboto Mono); `$montserrat`/`$roboto` are
   `--font-ui` (Inter) and `$roboto-mono` is `--font-mono` (JetBrains Mono),
   the woff2 files the shell serves, bundled into `dist/fonts/`.
4. **Focus rings.** `styles/_global.scss`: `:focus-visible` draws a 2 px
   `--focus` outline. The `outline: none` rules on the sidebar's and the
   menus' close buttons (`styles/_node_sidebar.scss`,
   `components/menu/ParentMenu.vue`) and on the group-name input
   (`custom/RectangleGrouping.vue`) are gone; node property inputs
   (`styles/_editor.scss`) ring on `:focus-visible` in `--focus` (was
   `:focus` in green).
5. **Theme selection.** `src/vhil/` (new): `index.ts` loads the tokens and
   fonts (`src/vhil/shell/`, copied from `vhil/server/static/` by
   `docker/editor.Dockerfile`, not in git) and applies `?theme=dark|light|auto`
   from the URL, dark by default; `theme.ts` sets `data-theme` on `<html>`
   (`auto`: none, so prefers-color-scheme picks, as in the shell).
   `src/main.js` imports it first. A new frontend procedure,
   `vhil_set_theme {theme}` (`core/communication/remoteProcedures.ts`,
   `resources/api_specification/specification.json`), switches it at run
   time (the shell's Editor page calls it over postMessage): the page passes
   its theme in `?theme=` and calls it when the theme changes.
6. **Served under a path** (step 6 of the editor workspace plan: the editor
   is on the web app's origin, under `/editor/`, behind its proxy, which
   strips the prefix). `core/communication/externalApp/backend.ts`: the
   socket.io client's `path` is `socket.io` next to the page
   (`new URL('socket.io', document.baseURI)`: `/editor/socket.io` there,
   upstream's `/socket.io` at the root) when the backend is the page's own
   origin. Asset URLs were relative already (`publicPath: ''`).
7. **Precompiled validators** (CSP `script-src 'self'`: Ajv compiles with
   `new Function`). `src/vhil/build-validators.mjs` (new) compiles, with
   upstream's two Ajv configurations, every schema the editor checks against
   into standalone code, `src/vhil/validators/{editor,rpc}.js` with their
   `.d.ts` (TypeScript's checker overflows its stack inferring the generated
   code): the specification, dataflow, metadata, graph and message schemas
   with each of their `$defs`, and every JSON-RPC endpoint's params and
   returns. `package.json`'s `build-server-app` and `build-static-html` run
   it first. `core/validate-json.js` takes the precompiled validators and
   looks one up by the schema's `$id` and the reference (upstream: an Ajv
   instance, compiling), and uses `JSON.stringify` for Ajv's `stringify`;
   `core/EditorManager.js` `validateJSONWithSchema` and
   `core/communication/rpcCommunication.ts` construct no Ajv (its
   `additionalAjvOptions` parameter, which nothing passed, is gone, and the
   endpoint schemas' compile check moved to the build script). The bundle
   keeps only Ajv's runtime helpers.
8. **The terminal is a plain log** (CSP `style-src 'self'`: hterm wrote
   inline styles; owner decision). `components/Terminal.vue` renders
   `src/vhil/LogView.vue` (new): a virtualised list of plain-text lines
   (only the rows in view are in the DOM; it follows the end unless
   scrolled up; at most 20 000 lines; escape sequences dropped), with an
   input line for a writable terminal (Enter sends the line through
   `requestTerminalRead`, where hterm sent keystrokes). Entries are no
   longer separated by a blank line. `components/TerminalPanel.vue` focuses
   that input instead of `#hterm-terminal`. `src/third-party/hterm_all.js`
   is gone.
9. **postMessage on one origin.** `custom/Editor.vue` drops messages from
   any origin but its own (the shell's Editor page, now the same origin)
   and replies to `event.origin`; `core/communication/externalApp/frontend.ts`
   posts to `window.location.origin`. Upstream accepted any origin and
   replied to `'*'`.
10. **CORS and CSP on the server.** `backend/fastapi.py`: CORS only for the
    origins `PM_ALLOWED_ORIGINS` names (comma-separated; none by default:
    upstream allowed `*`), and `backend/socketio.py`'s handshake the same
    (same origin by default; upstream `*`). A new middleware sends `PM_CSP`
    as the `Content-Security-Policy` on every response, or
    `-Report-Only` with `PM_CSP_REPORT_ONLY=1`; unset, none (upstream).
    vHIL's policy is `EDITOR_CSP` in `vhil/server/security.py`, which
    `scripts/editor.sh` passes.
11. **No style attribute in a node's title** (CSP `style-src 'self'`, found
    by the Report-Only rollout: one report per node render).
    `custom/CustomNode.vue` built the subtitle as
    `<pre class="subtitle" style="overflow: hidden; …">`, set with `v-html`;
    it is the class `subtitle-ellipsis` now, styled in `styles/_node.scss`.
12. **The vHIL workspace** (step 7 of the editor workspace plan: the layout).
    `components/Home.vue` lays out the vHIL workspace (`src/vhil/`, new)
    instead of `NavBar`, the canvas alone and `TerminalPanel`: a 40 px top
    bar (`VhilTopBar.vue`: the mark, "system @ branch" with a dot for unsaved
    edits, a firmware chip per board opening the ref picker
    `VhilRefPicker.vue`, the run's duration, Run (F5) and Stop (Shift+F5),
    the mode pill, Commit… and Open PR, the theme toggle), the activity rail
    and sidebar (`VhilRail.vue`, Ctrl+B: Palette, Systems, Runs), the canvas,
    the inspector (`VhilInspector.vue`, resizable), the bottom dock
    (`VhilDock.vue`, Ctrl+J, Ctrl/Alt+1..7: Log, Problems, and empty
    Scenario, State, Bus, Signals and Debug tabs for later steps), the status
    strip (`VhilStatus.vue`) and the dialogs (`VhilDialogs.vue`: Commit, PR,
    the `?` keyboard map). What they do (`workspace.js`, `graph.js`, `api.js`,
    `shortcuts.js`) is what the shell's Editor page did around the editor:
    the web app's API on the same origin (`/api`, with the session's CSRF
    token), and Run as `vhil/server/static/editor-run.js` says (copied into
    `src/vhil/shell/` by the image build, as the tokens are). The graph is
    read and edited in place (`saveDataflow`, a property's `value`) where the
    shell went through postMessage JSON-RPC; the URL's `?system=&branch=`
    opens a system, and the layout and theme are kept per viewer in
    `localStorage`.
    - **Gone from the page:** NavBar (its file menu, node search, graph
      details, settings, notifications panel, fullscreen and backend-status
      buttons, and the backend's navbar actions, so `Validate system`: Check
      replaces it) and TerminalPanel (its tab strip and resizer: the dock's
      Log tab renders `components/Terminal.vue` for the main terminal, and
      takes `terminalStore.manager`, so `terminal_show` opens the Log). The
      files are unchanged. Toasts still show; the node sidebar
      (`custom/CustomSidebar.vue`) is mounted on the canvas, with the
      `hoveredOver` NavBar provided.
    - **The palette** (`components/Palette.vue`) is rendered in the canvas's
      `palette` slot as before, inside a `<Teleport defer>` to the sidebar's
      `#vhil-palette-host`: it keeps the canvas's `editorEl` injection, so
      dragging a node onto the canvas places it as before. `workspace.css`
      takes away its absolute position and slide transform.
    - **A board's properties are in the inspector, not on its node.**
      `custom/CustomNode.vue` shows no property rows on a node whose type's
      `additionalData.vhil.kind` is `board` (the role, firmware, refs and
      bootloader: five rows that made the board taller than wide). The
      inspector edits the same property objects, so the node still tells the
      backend of a role change (`properties_on_change`) and the backend
      relabels its pins as before. A property's watcher skipped its first
      change (its control settling as the node mounts); a board's
      properties have no control on the node, so theirs skips nothing, or
      the user's first role change would be lost.
    - **Moved by CSS** (`workspace.css`): the zoom buttons
      (`components/Zoom.vue`, fixed to the window's corner, where the dock
      is now) sit in the canvas's corner.
    - **Layout CSS** is `src/vhil/workspace.css`, on the tokens. Script sets
      only the grid's sizes, as custom properties through the CSSOM (Vue's
      `:style` objects), which `style-src 'self'` allows.
13. **Node shapes and wires** (step 8 of the editor workspace plan).
    - `custom/CustomNode.vue`: a node whose type's `additionalData.vhil.kind`
      is `board`, `bus` or `model` draws its own head (`src/vhil/shapes.js`
      says what it reads, `src/vhil/nodes.css` draws it, on the tokens) in
      place of the title label, header icon and pill, and takes the classes
      `vhil-node --vhil-<kind>` plus `--role-<role>` or `--can-<n>`. A board
      is a card with a 4 px band and a chip in its role's colour (the role as
      text), a mono sub-line from the specification's `role_info` ("node 0x2"), its pin columns captioned (devices and analog left, CAN and
      digital right) and an empty `vhil-node-state` slot for the live state
      pill (step 15); a bus is a thin pill-ended rail (8 px, width `auto`) in
      its tint, `--can-1..4` by its place among the graph's buses, with its
      name and bitrate written along it (`bitrate` from the specification);
      a device is a compact (232 px) dashed card with a count pill (`×10`).
      The role select relabels the card as it relabels the pins. Every vHIL
      kind keeps its properties in the inspector (step 7 did so for boards):
      a bus's netdev, a device's count and parameters.
    - `custom/connection/ConnectionView.vue`: a wire is classed `--t-<type>`
      by its interface type, a CAN wire on a bus takes the bus's tint
      (`--color: var(--can-<n>, …)`), and hovering one shows a tooltip, one
      element placed through the CSSOM (`src/vhil/shapes.js`): "can_acu ·
      500 kbit/s · 2 nodes", or the type and both ends ("SPI · ams.SPI1 ↔
      isospi.spi"). `styles/_connection.scss`: width by type (CAN 3 px; SPI,
      I2C, SDMMC, UART, analog 2 px; GPIO 1.5 px, round dots) where upstream
      drew every wire 5 px; hovered, a wire thickens in its own colour
      (upstream: 6 px green).
    - `icons/Plus.vue`, `Minus.vue`, `Crosshair.vue` (the zoom buttons) stroke
      `$white` (`--fg`) instead of a scoped `#ffffff`, white on the light
      theme's buttons.
    - `core/communication/ExternalApplicationManager.js` no longer asks the
      backend for its app capabilities (navbar buttons): nothing shows them,
      and `vhil.editor` offers none now (its `Validate system` went; Check
      validates). An empty list can't be returned anyway: the backend
      library's `send_jsonrpc_message_with_sid` replaces a falsy result with
      `{}`, which fails the `navbar_items` schema.
    - `src/vhil/workspace.css`: the node sidebar (`custom/CustomSidebar.vue`)
      is pinned to the canvas's right edge and hidden while closed; on a wide
      canvas (the inspector hidden) the closed sidebar showed as an empty
      panel.
    - The specification's wire, port, header and pill colours
      (`vhil/editor.py` `CANVAS_METADATA`) are token `var()`s with the dark
      value as fallback, so the light theme recolours them.
14. **Runs, REPLAY and the Bus tab** (step 9 of the editor workspace plan).
    All in `src/vhil/` but for the image build; no Pipeline Manager file
    changes.
    - **REPLAY** (`replay.js`, `workspace.js`): opening a run from Runs
      (`VhilRail.vue`: each row replays its run; `↗` opens its shell page),
      or `?run=<id>`, opens the run's system as saved on its branch, loads
      its record, its CAN contract (`/api/runs/<id>/contract`) and its trace
      (`/api/runs/<id>/trace?kinds=frame,log`, 50 000 records a page,
      following `X-Trace-Cursor`: `api.js` `tracePage`), and sets the mode
      to REPLAY. The top bar (`VhilTopBar.vue`) trades the run duration for
      the run's clock: a range over its virtual time, `t=` and the end, the
      run's page link and Exit; the mode pill takes `--status-replay`. The
      run's log records go to the Log. With no system open, Runs lists every
      system's runs. Opening a system, or Run, leaves REPLAY.
    - **Frames** (`frames.js`, `monitor.js`, pure; `tests/js/frames.test.mjs`
      under node): `FrameStore` keeps frames in typed arrays, a ring of
      2^20 that drops the oldest, sorted by virtual time (the worker writes a
      slice bus by bus); `Monitor` folds them into one row per (bus, id) as
      of a time, only the frames since the last call while time moves
      forward.
    - **The Bus tab** (`VhilBus.vue`, `bus.css`; `VhilDock.vue` renders it):
      Monitor (one row per (bus, id): last data with the bytes changed in
      the last 250 ms marked, fields decoded, count, mean period, age) and
      Trace (every frame, filtered by bus and id with the shell's id filter,
      the frame at the scrubber marked and centred while following, later
      ones dimmed; a click moves the scrubber). Both are `rowtable.js`: the
      shell's `vtable.js` window (`windowFor`, `scrollFor`) over a fixed pool
      of rows made once and filled through `textContent` and classes, drawn
      once per animation frame; only the rows in view are decoded, with the
      shell's `decode.js`, and cached. `window.vhilBusTimings` keeps the last
      600 draw times (ms), for the performance check.
    - **One source:** `docker/editor.Dockerfile` copies `vtable.js` and
      `decode.js` from `vhil/server/static/` into `src/vhil/shell/` with the
      tokens and `editor-run.js`; the editor CI job compares the image's
      copies with the checkout.
    - **The shell** (`vhil/server/static/app.js`, `editor.js`): `#/runs/<id>`
      redirects to `/editor/?run=<id>`, `#/runs` to the Runs view,
      `#/systems[/<id>]` to the workspace, as `#/editor` did. Its own pages
      stay under `#/classic/` (`api.js` `runPage`): a run's page keeps its
      signals and artifacts, which REPLAY doesn't show yet, and links to
      REPLAY; the runs list keeps its pytest runs.

15. **Scenario model and table** (step 10 of the editor workspace plan). All
    in `src/vhil/` but for the image build; no Pipeline Manager file changes.
    - **Scenarios** (`scenarios.js`): the open system's scenario files
      (`systems/<id>.scenarios/<name>.yaml`; `docs/scenarios.md`), listed on
      its branch with their last runs, one selected for Run and the Scenario
      tab, its firmware's CAN contract without a run
      (`GET /api/systems/<id>/contract`, with the graph's firmware refs), and
      its check on the server, 400 ms after each edit
      (`POST .../scenarios/<name>/preview`). It imports nothing of
      `workspace.js`, which binds the open system, branch and refs to it.
    - **The model** (`scenario.js`, pure; `tests/js/scenario.test.mjs` under
      node): a scenario's `stimuli`, `watch` and `expect` lists as one table
      of rows sorted by time, each pointing at its item, edited in place;
      what a row targets and sets; the server's messages and a run's expect
      results by row.
    - **The Scenario tab** (`VhilScenario.vue`, the row editor `VhilScenarioRow.vue`, `scenario.css`;
      `VhilDock.vue` renders it): the scenario pick and `+ New`, its
      description and virtual time, `+ Add row…` (send, periodic, stop,
      gpio, analog, watch, expect), the state (errors, warnings, unsaved, the
      last run's expects), the rows `t | action | target | value | result`,
      and an editor for the selected one. A frame is edited as its message's
      decoded fields from the contract: a value table as a select, a bit as
      false/true, a scalar as a number with its unit, range and step, the hex
      in sync (the shell's `decode.js`, which gains `encodeField`,
      `rawFromPhysical`, `fieldRange` and `bytesToHex`: one copy); with no
      contract, raw hex. An expect's signal is built from a source (frame
      field, frame, symbol, pin), its owner and item, and its value is a
      label select where the field has a value table.
    - **The top bar** (`VhilTopBar.vue`): the scenario Run runs, next to the
      run's duration, which is the scenario's own while one is selected.
      Run (`workspace.js`) sends its rows and name with the run
      (`shell/editor-run.js` `runRequest`'s `scenario`), and refuses while
      the scenario has errors; when the run ends, its expect results are on
      the rows, in the Log and in Problems.
    - **Commit…** (`VhilDialogs.vue`) commits the system file, the selected
      scenario or both, on one branch (the scenario on the system's new
      commit). `?scenario=` in the URL selects one.
    - **Problems** (`VhilDock.vue`, `VhilStatus.vue`) list the scenario's
      check (errors, warnings) and its last run's failed expects with the
      system's; a click shows the row.

16. **Timeline and the Tests view** (step 11 of the editor workspace plan).
    All in `src/vhil/`; no Pipeline Manager file changes.
    - **The timeline** (`VhilTimeline.vue`, laid out by `timeline.js`, pure,
      `tests/js/timeline.test.mjs`; drawn in `scenario.css`), above the
      Scenario tab's table, on the virtual-time axis REPLAY's scrubber uses:
      a lane per bus and per board (and per bus or board a row names that the
      system lacks), a diamond per `can_send`, a hatched bar per
      `can_periodic` to its stop, a step per gpio/analog set, a bracket per
      expect over its window; the watches are the gutter below. A mark is
      dragged with a 1, 5 or 10 ms snap (checked on the server when it is
      dropped), or moved with ←/→ when focused; Ctrl+wheel zooms around the
      pointer, − / fit / + by buttons. SVG geometry is attributes and colour
      classes, so `style-src 'self'` holds.
    - **Expected against actual.** When a scenario's run ends, the workspace
      replays it on the Scenario tab (`workspace.js` `openRun(id, {tab})`;
      opening any run that ran a scenario selects it): each expect's result
      sits on its lane at its evidence time, ✓ or ✕ (a click moves the
      scrubber there, replaying the run first if needed), behind the marks
      the frames and pin edges the scenario names (REPLAY now loads `edge`
      records too: `replay.js` `replay.edges`), and the scrubber's time is a
      line; clicking the axis scrubs.
    - **Tests** (`VhilRail.vue`): a sidebar view of the open system's
      scenarios (every system's with none open: `GET /api/scenarios`), each
      with its last run's state and expects (passed, failed); opening one
      selects it and replays that run on the Scenario tab, ▶ runs it.

17. **State sources in the Scenario tab** (step 12 of the editor workspace
    plan; [state view](../../docs/state-view.md)). All in `src/vhil/`; no
    Pipeline Manager file changes.
    - **Enum labels** (`VhilScenarioRow.vue`): an expect's value is a select
      of labels for a symbol too, from the contract's `labels` (the image's
      DWARF enum or the state view's table), with a field's value table
      over them: `symbol:ams.g_state_telemetry == Precharge`.
    - **The `watch` stimulus** (`scenario.js`, `timeline.js`,
      `VhilScenarioRow.vue`): a watch that starts mid-run is a timed row
      (a symbol or a pin, as the `watch` rows), a diamond on its board's
      lane.

18. **The state panel** (step 13 of the editor workspace plan;
    [state view](../../docs/state-view.md)).
    - **The model** (`state.js`, pure; `tests/js/state.test.mjs` under node):
      `StateTrace` keeps each state-view signal's observations as a time
      series of raw values, fed record by record (samples, edges, frames
      decoded with the shell's `decode.js`): a replayed run's trace now, a
      live session's stream later. `cardAt(board, t)` is a board's FSM state
      (label, for how long, the one before, its transitions), relays,
      active and latched-cleared faults with reason and age, key values,
      and staleness (a source silent for three periods); `pillAt` the
      canvas pill; `lanesOf` the history lanes.
    - **REPLAY** (`replay.js`) loads the trace's `sample` records too and
      builds the run's `StateTrace` once its contract (with each board's
      state view) and trace are in.
    - **The State tab** (`VhilState.vue`, `VhilStateCard.vue`, `state.css`;
      `VhilDock.vue` renders it): a 340 px card per board at the scrubber's
      time: the FSM state at 18 px with "for N ms" and the previous state;
      contactors and relays as square pills with text (■ closed, filled;
      □ open); active faults as ✕ pills with reason and age, cleared ones
      outlined (○); key values with units; "stale" and a dashed card when
      the state went silent; the history: the FSM state's lane (its states
      written on it) and each relay's digital lane over the run, and the
      transitions; a click on either moves the scrubber. Segment positions
      are set through the CSSOM (`:style` objects). The FSM and digital
      lanes live here until the Signals tab exists.
    - **The inspector** (`VhilInspector.vue`): in REPLAY, the selected
      board's card (without the history) above its properties.
    - **The node pill** (`custom/CustomNode.vue`, the board's
      `vhil-node-state` slot from step 8: `VhilNodeState.vue`; `nodes.css`):
      "AMS · Precharge" at the scrubber's time; while a fault is active it
      reads "✕ fault" and is ringed, and so is the board's card (`:has()`);
      "stale" and dashed when its state went silent.

19. **LIVE** (step 15 of the editor workspace plan; [live
    sessions](../../docs/live-session.md)).
    - **The session** (`session.js`): a live run's two WebSockets, its trace
      as it is written (`/api/runs/<id>/live`: frames, samples, edges and
      logs into the REPLAY store and model, `replay.js` `beginLive` and
      `feedLive`; its `op` and `clock` records into the session's state) and
      its channel (`/api/runs/<id>/session`: ops out, acks and control in).
      Records are flushed every 50 ms at most. Every tab on the run folds the
      same `op` records (`live.js` `SessionState`, pure,
      `tests/js/live.test.mjs`), so the one that controls the session and
      the ones that watch show the same periodic senders and pin levels.
    - **The workspace** (`workspace.js`): ● Live starts a live session of the
      saved system (`startLive`, refused as Run is with unsaved edits);
      opening a running live run (Runs, `?run=`) joins it, as its controller
      if the server gives control, else view only. The mode pill reads LIVE,
      PAUSED while paused; the topology is locked (Pipeline Manager's own
      read-only mode, `graph.js` `setLocked`); when the session ends, what it
      streamed stays as a REPLAY of the run. "Save as scenario…"
      (`VhilDialogs.vue`) opens the session's recording
      (`GET /api/runs/<id>/session/scenario`) as a new scenario in the
      Scenario tab, which Commit… saves.
    - **The top bar** (`VhilTopBar.vue`): in LIVE, the run, the clock
      ("t=12.345 s · RTF 0.98×"), who controls it (Take control once it is
      free), the idle countdown in its last five minutes with Keep alive,
      Pause/Resume (Space) and Stop (Shift+F5), and Leave.
    - **The Bus tab** (`VhilBus.vue`): Monitor and Trace stream (a live
      batch bumps `replay.tick`: the Monitor folds the new frames, the
      Trace's index grows, nothing is rebuilt); a Send view (`VhilSend.vue`)
      composes a frame from the contract's decoded fields or raw hex and
      sends it once or every N ms as a named periodic (a period under 5 ms
      asks first), and lists the senders running with Stop and Stop all; a
      banner says while any runs.
    - **Inputs** (`VhilInputs.vue`): a board's GPIO inputs as `role="switch"`
      buttons reading HIGH/LOW, its analog inputs as a voltage with Set, from
      the contract's `inputs` (`vhil/stateview.py`); in the inspector, and
      from the board's context menu ("Inputs and analog…",
      `custom/CustomNode.vue`, a panel `VhilInputsMenu.vue`).
    - **The canvas**: a bus rail reads its frames a second
      (`custom/CustomNode.vue`), and a CAN wire whose bus carried frames in
      the last second marches, four steps a second, still under
      prefers-reduced-motion (`custom/connection/ConnectionView.vue`
      `--traffic`, `live.css`). The State tab, the inspector's card and the
      node pill follow the session's clock.
    - **The bootloader's window** (`state.js`): before 2 s a card says "in
      bootloader" and the pill "AMS · bootloader", not the app's first state
      read from RAM that is still 0.
    - **The status strip** (`VhilStatus.vue`): the clock, each bus's frames a
      second, the periodic senders running and Stop all.

20. **The debugger** (step 17 of the editor workspace plan;
    [debugger](../../docs/debugger.md)).
    - **The model** (`debug.js`, pure, `tests/js/debug.test.mjs`): the
      trace's `debug` records folded into each board's breakpoints, watches
      and the stop it is held at (stack, locals, registers, watch values);
      every stop, for a REPLAY; how a stop reads ("breakpoint in ecu
      (control.cpp:35)") and what a breakpoint field's text means (a
      function, file:line or an address). `replay.js` loads the `debug`
      records with the trace and feeds a live session's as they come;
      `session.js` reads a paused clock record's `debug` (`live.held`) and
      sends an op with `request`, resolving with its ack (a query's result).
    - **The Debug tab** (`VhilDebug.vue`, `debugui.js`), following the
      canvas selection (else the held board): a step toolbar with labelled
      buttons and their keys (Continue F5, Step over F10, Step into F11, Step
      out Shift+F11, Break, Detach), breakpoints added by function, file:line
      or address and removed from a list; the firmware source at the stop,
      read only (`GET /api/runs/<id>/debug/source`, from the image's
      checkout in the fw volume), a window of lines around the current one
      (▶, highlighted, scrolled to) with a breakpoint gutter (● buttons that
      set or clear one); a Disassembly toggle around the pc; the watch list.
      A REPLAY says it can't be debugged and lists where the session
      stopped (a click scrubs there). No list of the timeouts that would
      have fired: the contracts give periods, not receivers' timeouts.
    - **The frozen-bus banner** (`VhilHeld.vue`), in the Debug and Bus tabs:
      "Bus frozen: all boards paused at t=… · breakpoint in ecu (…)". The top
      bar adds where the system is held next to PAUSED; the Debug tab comes
      up at a stop.
    - **The inspector** (`VhilDebugInspect.vue`): a held board's call stack
      (a click picks the frame the source and locals show), its locals and
      its registers; with nothing selected, the held board's.
    - **Watches** (`VhilWatches.vue`): a board's list, kept by the worker
      (the `watches` op), in the Debug tab and the State tab alike, each with
      its value at the board's stop.
    - **Keys** (`shortcuts.js`): held at a stop and in control, F5
      continues, F10 steps over, F11 steps into, Shift+F11 steps out.

## Left for later

- **Light theme polish** (owner decision: defined now, polished later).
  About twenty components hard-code `white` in their styles (`fill: white`,
  `color: white`: the sidebar tabs, several icons, the terminal), unreadable
  on the light surfaces; `src/vhil/theme.css` covers only SVG attributes.
  (The canvas itself follows the theme since step 8: nodes, wires and the
  zoom buttons are on the tokens.)
- **HTML from the specification** (node titles and pills in
  `custom/CustomNode.vue`, palette entries: `v-html` through DOMPurify): a
  `style` attribute in it is refused by the CSP. vHIL's specification puts
  none there.
- **Screenshot baselines.** The editor image has Playwright's library
  (`node_modules`) but no browser; the dark/light × 1366×768/1920×1080
  baselines and the axe scan wait for a pinned browser in CI.
