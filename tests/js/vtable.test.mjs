// static/vtable.js windowing under node's test runner.
import assert from "node:assert/strict";
import { test } from "node:test";
import { scrollFor, windowFor } from "../../vhil/server/static/vtable.js";

test("only the visible rows plus overscan are in the window", () => {
  // 400 px viewport, 20 px rows: 21 rows visible (one partial), 10 overscan each side
  assert.deepEqual(windowFor(0, 400, 20, 100000, 10), { start: 0, end: 31 });
  assert.deepEqual(windowFor(20000, 400, 20, 100000, 10), { start: 990, end: 1031 });
  const w = windowFor(1e6, 600, 22, 1e6);
  assert.ok(w.end - w.start < 60, "window size is bounded by the viewport, not the count");
});

test("the window is clamped to the rows that exist", () => {
  assert.deepEqual(windowFor(0, 400, 20, 5), { start: 0, end: 5 });
  assert.deepEqual(windowFor(99999, 400, 20, 50), { start: 50, end: 50 });
  assert.deepEqual(windowFor(-30, 400, 20, 0), { start: 0, end: 0 });
});

test("scrollFor brings a row into view only when it is not", () => {
  assert.equal(scrollFor(5, 0, 400, 20), 0);            // already visible
  assert.equal(scrollFor(50, 0, 400, 20), 620);         // below: its bottom at the viewport's
  assert.equal(scrollFor(2, 200, 400, 20), 40);         // above: its top at the viewport's
  assert.equal(scrollFor(50, 0, 400, 20, true), 810);   // centred
});
