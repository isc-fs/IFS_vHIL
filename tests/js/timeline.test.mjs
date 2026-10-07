// The Scenario tab's timeline layout (src/vhil/timeline.js) under node's
// test runner: lanes, marks, ticks and zoom.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  fitScale, laneOf, lanesOf, marksOf, tickLabel, tickStep, ticks, zoomAround,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/timeline.js";

const DOC = {
  virtual_ms: 7000,
  stimuli: [
    { kind: "can_periodic", name: "vcu", at_ms: 2500, bus: "can_acu", id: 0x100, data: "", period_ms: 10 },
    { kind: "gpio", at_ms: 5500, board: "ams", pin: "PF9", level: true },
    { kind: "stop_periodic", at_ms: 6500, periodic: "vcu" },
    { kind: "can_send", at_ms: 3000, bus: "can_dash", id: 0x7e0, data: "deadbeef" },
    { kind: "analog", at_ms: 100, board: "ams", pin: "PF7", volts: 1.5 },
  ],
  watch: [{ kind: "pin", board: "ams", pin: "PB5" }],
  expect: [
    { check: "eventually", at_ms: 5600, until_ms: 6200, signal: "frame:can_acu.AMS_status.fsm_state", value: 1 },
    { check: "always", at_ms: 6800, signal: "pin:ams.PB5", value: "high", name: "air" },
  ],
};

test("a lane per bus and board, and for what a row names that the system lacks", () => {
  assert.deepEqual(lanesOf(DOC, { buses: ["can_acu"], boards: ["ams"] }).map((l) => l.id),
                   ["bus:can_acu", "board:ams", "bus:can_dash"]);
  assert.equal(laneOf("watch", DOC.watch[0]), null);
  assert.equal(laneOf("expect", DOC.expect[1]), "board:ams");
  assert.equal(laneOf("stimuli", DOC.stimuli[3]), "bus:can_dash");
});

test("marks: diamonds, bars to their stop, steps, expect windows", () => {
  const marks = Object.fromEntries(marksOf(DOC, 7000).map((m) => [m.key, m]));
  assert.equal("stimuli[2]" in marks, false);                // a stop is its periodic's end
  assert.deepEqual([marks["stimuli[0]"].shape, marks["stimuli[0]"].t0, marks["stimuli[0]"].t1],
                   ["bar", 2500, 6500]);
  assert.equal(marks["stimuli[0]"].label, "0x100 /10 ms");
  assert.deepEqual([marks["stimuli[1]"].shape, marks["stimuli[1]"].label, marks["stimuli[1]"].up],
                   ["step", "PF9 ↑", true]);
  assert.equal(marks["stimuli[3]"].shape, "diamond");
  assert.equal(marks["stimuli[4]"].label, "PF7 1.5 V");
  assert.deepEqual([marks["expect[0]"].t0, marks["expect[0]"].t1, marks["expect[0]"].label],
                   [5600, 6200, "eventually"]);
  assert.deepEqual([marks["expect[1]"].t1, marks["expect[1]"].label], [7000, "air"]);
});

test("ticks stay apart, and read in ms or s", () => {
  assert.equal(tickStep(0.1), 1000);
  assert.equal(tickStep(1), 100);
  assert.deepEqual(ticks(3000, 0.1), [0, 1000, 2000, 3000]);
  assert.equal(tickLabel(1500), "1.5 s");
  assert.equal(tickLabel(250), "250 ms");
});

test("zoom keeps the time under the pointer where it was", () => {
  const z = zoomAround(0.1, 2, 4000, 300);
  assert.equal(z.pxPerMs, 0.2);
  assert.equal(z.scrollLeft, 4000 * 0.2 - 300);
  assert.equal(zoomAround(10, 10, 0, 0).pxPerMs, 20);       // clamped
  assert.equal(fitScale(7000, 700), 0.1);
});
