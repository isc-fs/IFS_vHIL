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
- `pipeline_manager/frontend/src/vhil/`: vHIL's own frontend code (theme,
  below); `src/vhil/shell/` is filled by the image build, not in git.

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

## Left for later

- **Light theme polish** (owner decision: defined now, polished later).
  About twenty components hard-code `white` in their styles (`fill: white`,
  `color: white`: the sidebar tabs, several icons, the terminal), unreadable
  on the light surfaces; `src/vhil/theme.css` covers only SVG attributes.
  The wire, header and pill colours in the specification's metadata
  (`vhil/editor.py`) are the dark theme's hex values (its canvas
  `backgroundColor` is `var(--bg-0, #0f1115)` and follows the theme).
- **CSP, step 6.** Ajv still compiles the RPC and specification schemas at
  run time (`new Function`: needs `'unsafe-eval'`) until they are
  precompiled (`core/EditorManager.js`, `core/communication/rpcCommunication.ts`);
  hterm (`src/third-party/hterm_all.js`, the terminal) writes inline styles
  (needs `style-src 'unsafe-inline'`) until it is replaced;
  `core/communication/externalApp/frontend.ts` still answers postMessage with
  `targetOrigin '*'`, and `backend/fastapi.py` sends no CSP and allows any
  CORS origin. This step adds no inline script or style: tokens, fonts and
  the theme code are bundled files.
- **Screenshot baselines.** The editor image has Playwright's library
  (`node_modules`) but no browser; the dark/light × 1366×768/1920×1080
  baselines and the axe scan wait for a pinned browser in CI.
