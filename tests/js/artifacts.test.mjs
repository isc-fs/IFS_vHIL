// The Artifacts tab's model (src/vhil/artifacts.js) and the front page's
// redirects (vhil/server/static/editor.js) under node's test runner.
import assert from "node:assert/strict";
import { test } from "node:test";
import { editorUrl, redirectFor } from "../../vhil/server/static/editor.js";
import {
  artifactUrl, firmwareText, junitCounts, parseJunit, scenarioText, snapshotsFor, snapshotsOf, tail, wallText,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/artifacts.js";

// As pytest writes it (junit_family xunit2), shortened.
const JUNIT = `<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="12.5">
<testcase classname="tests.sim.test_ams_boot" name="test_boots" time="3.250" />
<testcase classname="tests.sim.test_ams_can" name="test_period[0x4A0]" time="4.000"><failure message="assert 600 &lt; 550">E   assert 600 &lt; 550
E    +  where 600 = gap</failure></testcase>
<testcase classname="tests.sim.test_ams_can" name="test_dlc" time="0.100"><error message="fixture &quot;sim&quot; failed"><![CDATA[boom & bust]]></error></testcase>
<testcase classname="tests.sim.test_x" name="test_skip" time="0"><skipped type="pytest.skip" message="no firmware" /></testcase>
</testsuite></testsuites>`;

test("JUnit cases with their outcome, message and text", () => {
  const cases = parseJunit(JUNIT);
  assert.deepEqual(cases.map((c) => [c.name, c.outcome, c.time]), [
    ["test_boots", "passed", 3.25], ["test_period[0x4A0]", "failed", 4], ["test_dlc", "error", 0.1],
    ["test_skip", "skipped", 0]]);
  assert.equal(cases[1].message, "assert 600 < 550");
  assert.match(cases[1].text, /^E {3}assert 600 < 550\nE {4}\+ {2}where 600 = gap$/);
  assert.equal(cases[2].message, 'fixture "sim" failed');
  assert.equal(cases[2].text, "boom & bust");
  assert.equal(cases[3].message, "no firmware");
  assert.deepEqual(junitCounts(cases), { passed: 1, failed: 1, error: 1, skipped: 1 });
  assert.deepEqual(parseJunit("<testsuites/>"), []);
});

test("failure snapshots, grouped, and found for a failed case", () => {
  const names = ["junit.xml", "sim-logs/failures/tests_sim_test_ams_can.py_test_period_0x4A0_-call/renode.log",
                 "sim-logs/failures/tests_sim_test_ams_can.py_test_period_0x4A0_-call/frames.txt",
                 "sim-logs/failures/tests_sim_test_other.py_test_y-setup/renode.log"];
  const snaps = snapshotsOf(names);
  assert.deepEqual(snaps.map((s) => [s.name, s.files.length]),
                   [["tests_sim_test_ams_can.py_test_period_0x4A0_-call", 2], ["tests_sim_test_other.py_test_y-setup", 1]]);
  const [, failed] = parseJunit(JUNIT);
  assert.deepEqual(snapshotsFor(failed, snaps).map((s) => s.name), ["tests_sim_test_ams_can.py_test_period_0x4A0_-call"]);
  assert.deepEqual(snapshotsFor(parseJunit(JUNIT)[0], snaps), []);   // passed: none
});

test("a run's record as text", () => {
  assert.equal(artifactUrl(7, "sim-logs/a b/x.log"), "/api/runs/7/artifacts/sim-logs/a%20b/x.log");
  assert.equal(tail("abcdef", 3), "… (3 characters cut; the file has all of it)\ndef");
  assert.equal(tail("abc", 3), "abc");
  assert.equal(firmwareText({ summary: { firmware: { ams: "/vhil/fw/ams@dev/build/AMS.elf" } } }), "ams: ams@dev");
  assert.equal(firmwareText({ firmware: { ams: null } }), "ams@default");
  assert.equal(wallText({ started: "2026-10-08T10:00:00Z", finished: "2026-10-08T10:03:07Z" }), "3 min 7 s");
  assert.equal(wallText({}), "–");
  assert.equal(scenarioText({ scenario: { kind: "pytest", select: "tests/sim/test_x.py" } }), "pytest tests/sim/test_x.py");
  assert.equal(scenarioText({ scenario: { kind: "run", virtual_ms: 3000 } }), "run 3000 ms");
  assert.equal(scenarioText({ scenario: { kind: "run", live: true } }), "live session");
});

test("the shell's old links land in the workspace", () => {
  assert.equal(redirectFor(""), "/editor/");
  assert.equal(redirectFor("#/"), "/editor/");
  assert.equal(redirectFor("#/runs/12"), "/editor/?run=12");
  assert.equal(redirectFor("#/classic/runs/12/signals"), "/editor/?run=12&tab=signals");
  assert.equal(redirectFor("#/classic/runs/12/edges"), "/editor/?run=12&tab=signals");
  assert.equal(redirectFor("#/runs/12/frames"), "/editor/?run=12&tab=bus");
  assert.equal(redirectFor("#/classic/runs/12/artifacts"), "/editor/?run=12&tab=artifacts");
  assert.equal(redirectFor("#/runs"), "/editor/?view=runs");
  assert.equal(redirectFor("#/classic/runs"), "/editor/?view=runs");
  assert.equal(redirectFor("#/systems/ecu-ams"), "/editor/?system=ecu-ams");
  assert.equal(redirectFor("#/classic/systems/ams"), "/editor/?system=ams");
  assert.equal(redirectFor("#/editor/ecu"), "/editor/?system=ecu");
  assert.equal(redirectFor("#/systems"), "/editor/?view=systems");
  assert.equal(redirectFor("#/nonsense/x"), "/editor/?view=systems");
  assert.equal(editorUrl("a b"), "/editor/?system=a+b");
});
