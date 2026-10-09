// src/vhil/stable.js under node's test runner: what keeps a native <select>'s
// open popup from being re-patched (Chrome and Safari on macOS drop the pick
// when its options are). A computed that recomputes to the same data keeps
// its old value, so the component reading it doesn't re-render; v-pick writes
// a select's value only when it differs.
import assert from "node:assert/strict";
import { test } from "node:test";
import { keep, same, vPick } from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/stable.js";

const chips = () => [
  { id: "n:ams", name: "ams", fw: { id: "ams", repo: "isc-fs/IFS08-CE-AMS", ref: "dev" }, label: "dev", picked: false },
];

test("plain data compares by content", () => {
  assert.equal(same(chips(), chips()), true);
  assert.equal(same({ a: [1, { b: "x" }] }, { a: [1, { b: "x" }] }), true);
  assert.equal(same({ a: 1 }, { a: 1, b: undefined }), false);
  assert.equal(same([1, 2], { 0: 1, 1: 2 }), false);
  assert.equal(same({ a: null }, { a: {} }), false);
  assert.equal(same(NaN, NaN), true);
  const changed = chips();
  changed[0].label = "feat/x";
  assert.equal(same(chips(), changed), false);
});

test("a recompute to the same data keeps the old value: no re-render", () => {
  const old = chips();
  assert.equal(keep(old, chips()), old);
  assert.equal(keep(undefined, old), old); // the first computation
  const empty = [];
  assert.equal(keep(empty, []), empty);
});

test("a recompute to new data is the new value", () => {
  const next = chips();
  next[0].picked = true;
  assert.equal(keep(chips(), next), next);
});

// A select whose every value write is counted.
const select = (value = "") => {
  const el = { writes: 0, current: value };
  Object.defineProperty(el, "value", {
    get: () => el.current,
    set: (v) => { el.writes += 1; el.current = v; },
  });
  return el;
};

test("v-pick sets the value on mount", () => {
  const el = select();
  vPick.mounted(el, { value: "tsms-precharge-run" });
  assert.equal(el.value, "tsms-precharge-run");
  const none = select("x");
  vPick.mounted(none, { value: null });
  assert.equal(none.value, "");
});

test("v-pick leaves the select alone when a re-render keeps its value", () => {
  const el = select("tsms-precharge-run");
  vPick.updated(el, { value: "tsms-precharge-run" });
  vPick.updated(el, { value: "tsms-precharge-run" });
  assert.equal(el.writes, 0);
  const empty = select("");
  vPick.updated(empty, { value: undefined });
  assert.equal(empty.writes, 0);
});

test("v-pick writes a value that changed", () => {
  const el = select("a");
  vPick.updated(el, { value: "b" });
  assert.equal(el.value, "b");
  assert.equal(el.writes, 1);
  vPick.updated(el, { value: 3 }); // a number property: the DOM holds strings
  assert.equal(el.value, "3");
});
