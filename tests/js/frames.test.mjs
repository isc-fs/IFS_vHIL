// The workspace's Bus tab store and monitor (src/vhil/frames.js,
// monitor.js) under node's test runner.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  FrameStore, filterIndices, hexBytes, lastAtOrBefore,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/frames.js";
import { Monitor, meanPeriod } from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/monitor.js";

const frame = (t_us, bus, id, data, extra = {}) => ({ kind: "frame", t_us, bus, id, ext: false, data, ...extra });

test("frames are held in time order, across buses within a slice", () => {
  const s = new FrameStore(16);
  // A slice's records come bus by bus (vhil/worker.py): not in time order.
  s.append([frame(300, "can_acu", 0x100, "01"), frame(500, "can_acu", 0x100, "02"),
            frame(200, "can_inv", 0x200, "aa"), frame(400, "can_inv", 0x200, "bb")]);
  assert.deepEqual([0, 1, 2, 3].map((i) => s.tAt(i)), [200, 300, 400, 500]);
  assert.deepEqual(s.buses, ["can_inv", "can_acu"]);  // as first seen
  assert.equal(s.countUpTo(399), 2);
  assert.equal(s.countUpTo(400), 3);
  assert.equal(s.countUpTo(0), 0);
  assert.equal(hexBytes(s.frame(0).bytes), "aa");
});

test("a full ring drops the oldest frames", () => {
  const s = new FrameStore(3);
  s.append([1, 2, 3, 4, 5].map((t) => frame(t, "b", t, "00")));
  assert.equal(s.length, 3);
  assert.equal(s.dropped, 2);
  assert.deepEqual([0, 1, 2].map((i) => s.frame(i).id), [3, 4, 5]);
});

test("payloads: classic, empty and longer than 8 bytes; flags", () => {
  const s = new FrameStore(8);
  const long = "00112233445566778899aabbccddeeff";
  s.append([frame(1, "b", 1, ""), frame(2, "b", 2, "0102030405060708"),
            frame(3, "b", 3, long), frame(4, "b", 0x18ff50e5, "ff", { ext: true, src: "stimulus" })]);
  assert.equal(s.frame(0).bytes.length, 0);
  assert.equal(hexBytes(s.frame(1).bytes), "01 02 03 04 05 06 07 08");
  assert.equal(hexBytes(s.frame(2).bytes).replace(/ /g, ""), long);
  const f = s.frame(3);
  assert.equal(f.ext, true);
  assert.equal(f.src, true);
  assert.equal(f.id, 0x18ff50e5);
});

test("the monitor keeps one row per (bus, id) as of a time", () => {
  const s = new FrameStore(64);
  s.append([frame(0, "can_acu", 0x100, "0000"), frame(100, "can_acu", 0x100, "0001"),
            frame(150, "can_acu", 0x101, "ff"), frame(200, "can_acu", 0x100, "0001"),
            frame(250, "can_inv", 0x100, "aa")]);
  const m = new Monitor(s);
  let rows = m.update(120);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 2);
  assert.equal(rows[0].changed, 0b10, "byte 1 changed in the last frame");
  assert.equal(rows[0].changedAt, 100);
  rows = m.update(1000);
  assert.deepEqual(rows.map((r) => `${r.bus}/${r.id.toString(16)}/${r.count}`),
                   ["can_acu/100/3", "can_acu/101/1", "can_inv/100/1"]);
  const r100 = rows[0];
  assert.equal(r100.changed, 0, "the same data again");
  assert.equal(r100.changedAt, 100);
  assert.equal(meanPeriod(r100), 100);
  assert.equal(meanPeriod(rows[1]), null);
  // Back in time: folded again from the start.
  rows = m.update(50);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 1);
});

test("the trace view's filter and cursor", () => {
  const s = new FrameStore(64);
  s.append([frame(10, "a", 1, "00"), frame(20, "b", 2, "00"), frame(30, "a", 3, "00"),
            frame(40, "a", 1, "00")]);
  const onA = filterIndices(s, (i) => s.busName(i) === "a");
  assert.deepEqual(onA, [0, 2, 3]);
  assert.equal(lastAtOrBefore(s, onA, 5), -1);
  assert.equal(lastAtOrBefore(s, onA, 30), 1);
  assert.equal(lastAtOrBefore(s, onA, 35), 1);
  assert.equal(lastAtOrBefore(s, onA, 1e9), 2);
});

test("a long run folds fast: 5k frames/s for 20 s", () => {
  const s = new FrameStore(1 << 17);
  const recs = [];
  for (let i = 0; i < 100000; i += 1) {
    recs.push(frame(i * 200, i % 3 ? "can_acu" : "can_inv", 0x100 + (i % 40), "0102030405060708"));
  }
  s.append(recs);
  const m = new Monitor(s);
  const t0 = performance.now();
  for (let t = 0; t <= 20e6; t += 20e6 / 200) m.update(t); // a scrub forward, 200 steps
  const forward = performance.now() - t0;
  assert.equal(m.update(20e6).length, 80);
  assert.ok(forward < 2000, `forward scrub took ${forward} ms`);
});
