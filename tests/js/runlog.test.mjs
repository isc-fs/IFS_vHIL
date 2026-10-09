// src/vhil/runlog.js under node's test runner: a run's log records as the
// Log tab's lines (time, source, level, text) and its source filter.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  EDITOR, RUN, levelOf, logEntry, shownEntries, sourceOf, sourcesOf, textOf, toggled, when,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/runlog.js";

test("a record becomes a line with its time, source and level", () => {
  const e = logEntry({ kind: "log", t_us: 2_150_000, text: "iwdg: Watchdog reset triggered!",
                       source: "renode", level: "warning", board: "ams" });
  assert.equal(e.source, "renode");
  assert.equal(e.level, "warning");
  assert.equal(e.text, "    2.150 s  renode       WARN   iwdg: Watchdog reset triggered!");
  const u = logEntry({ kind: "log", t_us: 300_000, text: "$PMTK220,100*2F", source: "ecu.USART10" });
  assert.equal(u.level, "info");
  assert.match(u.text, /0\.300 s {2}ecu\.USART10 {2}info {3}\$PMTK220/);
});

test("a record before power-on shows its wall time", () => {
  const wall = new Date(2026, 9, 10, 9, 5, 7).getTime() / 1000;
  assert.equal(when({ t_us: 0, wall_s: wall }), "09:05:07");
  assert.equal(when({ t_us: 0 }), "0.000 s");
  assert.match(logEntry({ t_us: 0, wall_s: wall, text: "[ecu] cmake", source: "build" }).text,
    /^ {3}09:05:07 {2}build/);
});

test("an old record with no source or level is the run's, at info", () => {
  const e = logEntry({ kind: "log", t_us: 0, text: "worker w1" });
  assert.equal(e.source, RUN);
  assert.equal(e.level, "info");
  assert.equal(logEntry({ t_us: 0, text: "x", level: "loud" }).level, "info");
});

test("the editor's own messages are plain strings", () => {
  assert.equal(sourceOf("Opened ams"), EDITOR);
  assert.equal(levelOf("Opened ams"), "info");
  assert.equal(textOf("Opened ams"), "Opened ams");
  assert.equal(textOf({ text: "x" }), "x");
  assert.equal(sourceOf(null), EDITOR);
});

const ENTRIES = [
  "run 4 queued",
  logEntry({ t_us: 0, text: "building ams", source: "build", wall_s: 1 }),
  logEntry({ t_us: 0, text: "worker w", source: "worker" }),
  logEntry({ t_us: 100_000, text: "ams: Machine started.", source: "renode" }),
  logEntry({ t_us: 200_000, text: "$GPGGA", source: "ecu.USART10" }),
  logEntry({ t_us: 300_000, text: "stimulus gpio", source: "scenario" }),
  logEntry({ t_us: 400_000, text: "boot", source: "ams.USART2" }),
];

test("sources come in a stable order: editor, the run's, then the UARTs", () => {
  assert.deepEqual(sourcesOf(ENTRIES),
    ["editor", "worker", "build", "renode", "scenario", "ams.USART2", "ecu.USART10"]);
  assert.deepEqual(sourcesOf([]), []);
});

test("hidden sources' lines are filtered out", () => {
  assert.equal(shownEntries(ENTRIES, []), ENTRIES);
  const shown = shownEntries(ENTRIES, ["renode", "editor"]);
  assert.deepEqual(shown.map(sourceOf), ["build", "worker", "ecu.USART10", "scenario", "ams.USART2"]);
});

test("a chip toggles its source in a new, sorted list", () => {
  const h = toggled([], "renode");
  assert.deepEqual(h, ["renode"]);
  assert.deepEqual(toggled(h, "build"), ["build", "renode"]);
  assert.deepEqual(toggled(["build", "renode"], "renode"), ["build"]);
  assert.deepEqual(h, ["renode"]); // not changed in place
  assert.deepEqual(toggled(undefined, "x"), ["x"]);
});
