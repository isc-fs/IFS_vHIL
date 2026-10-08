// The debugger's model (src/vhil/debug.js) under node's test runner: what a
// session's debug records leave (breakpoints, watches, the stop each board is
// held at), how a stop reads, and what a breakpoint field's text means.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DebugState, heldText, isExpression, parseLocation, sameFile, sourceWindow, whereText,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/debug.js";

const FILE = "/vhil/fw/ecu@dev/Core/Src/app/control.cpp";
const dbg = (t, board, event, extra = {}) => ({ kind: "debug", t_us: t, board, event, ...extra });

test("the debug records fold into each board's breakpoints and stop", () => {
  const s = new DebugState();
  s.add(dbg(2_000_000, "ecu", "attached"));
  s.add(dbg(2_050_000, "ecu", "breakpoints", {
    breakpoints: [{ number: 1, file: FILE, line: 35, func: "ecu::Controller::step" }],
    watches: ["now_ms"],
  }));
  assert.deepEqual(s.held(), []);
  const stop = dbg(2_100_000, "ecu", "stopped", {
    reason: "breakpoint-hit", frame: { func: "ecu::Controller::step", file: FILE, line: 35 },
  });
  s.add(stop);
  s.add({ kind: "frame", t_us: 2_100_000 });
  assert.deepEqual(s.held(), ["ecu"]);
  assert.equal(s.board("ecu").stop, stop);
  assert.deepEqual([...s.linesOf("ecu", "app/control.cpp")], [[35, 1]]);
  assert.deepEqual([...s.linesOf("ecu", "other.c")], []);
  s.add(dbg(2_100_000, "", "running"));
  assert.deepEqual(s.held(), []);
  assert.equal(s.stopAt(2_099_999), null);
  assert.equal(s.stopAt(3_000_000), stop);
  s.add(dbg(2_200_000, "ecu", "detached"));
  assert.equal(s.boards.has("ecu"), false);
  assert.equal(s.stops.length, 1, "a REPLAY still knows where it stopped");
});

test("a stop reads as the top bar says it", () => {
  assert.equal(whereText({ file: FILE, line: 35 }), "control.cpp:35");
  assert.equal(whereText({ func: "??", addr: "0x8" }), "0x8");
  assert.equal(heldText({ board: "ams", reason: "breakpoint-hit", file: "/x/bms_task.c", line: 212 }),
    "breakpoint in ams (bms_task.c:212)");
  assert.equal(heldText({ board: "ecu", reason: "end-stepping-range", func: "main" }), "step in ecu (main)");
  assert.equal(heldText(null), "");
});

test("a breakpoint field reads a function, a file:line or an address", () => {
  assert.deepEqual(parseLocation("ecu::Controller::step"), { function: "ecu::Controller::step" });
  assert.deepEqual(parseLocation(" control.cpp:35 "), { file: "control.cpp", line: 35 });
  assert.deepEqual(parseLocation("Core/Src/app/control.cpp:35"), { file: "Core/Src/app/control.cpp", line: 35 });
  assert.deepEqual(parseLocation("*0x0802310a"), { address: "0x0802310a" });
  for (const bad of ["", "f()", "x = 1", "a b", "$_shell", "c.c:"]) assert.equal(parseLocation(bad), null, bad);
});

test("an expression is a variable path, nothing that runs or writes", () => {
  for (const ok of ["now_ms", "in.apps1_raw", "this->state_", "cells[3].v", "*p", "ns::g.x"]) {
    assert.ok(isExpression(ok), ok);
  }
  for (const bad of ["x=1", "f()", "$pc", "(int)x", "a+b", "a[i]"]) assert.ok(!isExpression(bad), bad);
});

test("files match by suffix, and a source shows a window around the line", () => {
  assert.ok(sameFile(FILE, "app/control.cpp"));
  assert.ok(sameFile("control.cpp", FILE));
  assert.ok(!sameFile(FILE, "ontrol.cpp"));
  assert.deepEqual(sourceWindow(35, 1000, 10), [25, 45]);
  assert.deepEqual(sourceWindow(3, 1000, 10), [1, 13]);
  assert.deepEqual(sourceWindow(999, 1000, 10), [989, 1000]);
});
