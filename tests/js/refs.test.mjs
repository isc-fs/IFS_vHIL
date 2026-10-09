// What a board's in-node firmware and bootloader dropdowns list
// (editor/.../src/vhil/refs.js, VhilBoardControls.vue) under node's test runner.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  SHOW_ACTIVE, SHOW_ALL, detailText, listingNote, notBuilt, refDetail, refLabel, refOptions, shownKinds,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/refs.js";

const NOW = Date.parse("2026-10-09T12:00:00Z");
const day = (n) => new Date(NOW - n * 86400e3).toISOString();

const fw = { id: "ams", repo: "isc-fs/IFS08-CE-AMS", ref: "dev" };
const app = (ref = "") => ({ what: "app", refProp: "firmware_ref", kinds: ["branches", "tags"], fwId: "ams", fw, ref });
const boot = (ref = "") => ({
  what: "bootloader", refProp: "bootloader_ref", kinds: ["tags"], fwId: "can-bootloader",
  fw: { id: "can-bootloader", repo: "isc-fs/IFS-BL", ref: "v1.6.2" }, ref,
});

// As GET /api/firmware/ams/refs gives it (newest first), and with ?all=1.
const ACTIVE = {
  details: "github", active_days: 30, hidden: 7,
  branches: [
    { name: "feat/x", sha: "1a2b3c4d5e6f7a8b", date: day(3), author: "raul", prs: [{ number: 12 }], built: false },
    { name: "dev", sha: "aaaaaaaabbbbbbbb", date: day(5), author: "raul", prs: [], built: true },
  ],
  tags: [{ name: "v3.0.2", sha: "cccccccc", built: true }],
};
const EVERY = { ...ACTIVE, hidden: 0, branches: [...ACTIVE.branches, { name: "old/y", sha: "dddddddd", date: day(90), built: false }] };

const values = (groups) => groups.map((g) => [g.label, g.options.map((o) => o.value)]);

test("a ref reads its age, author, PRs and whether it is built", () => {
  assert.equal(refLabel("feat/x", ACTIVE.branches[0], NOW), "feat/x · 3 d ago · raul · #12 · not built");
  assert.equal(refLabel("dev", ACTIVE.branches[1], NOW), "dev · 5 d ago · raul");
  assert.equal(refLabel("gone", undefined, NOW), "gone");
});

test("the app lists the catalogue's ref, the active branches newest first, then show all", () => {
  const groups = refOptions(app(), { refs: ACTIVE, showAll: false }, NOW);
  assert.deepEqual(values(groups), [["", [""]], ["active branches", ["feat/x"]], ["", [SHOW_ALL]]]);
  assert.equal(groups[0].options[0].label, "dev · 5 d ago · raul (catalogue)");
  assert.equal(groups[2].options[0].label, "Show all branches and tags (7 more)…");
});

test("show all lists every branch and the tags, and offers the active ones back", () => {
  const groups = refOptions(app(), { refs: ACTIVE, every: EVERY, showAll: true }, NOW);
  assert.deepEqual(values(groups), [["", [""]], ["branches", ["feat/x", "old/y"]], ["tags", ["v3.0.2"]],
    ["", [SHOW_ACTIVE]]]);
  // Until every branch has come, the active listing stands in.
  assert.deepEqual(values(refOptions(app(), { refs: ACTIVE, showAll: true }, NOW))[1], ["branches", ["feat/x"]]);
});

test("a picked ref the listing doesn't show is kept and says why", () => {
  assert.deepEqual(refOptions(app("old/y"), { refs: ACTIVE, every: EVERY, showAll: false }, NOW)[0].options[1],
    { value: "old/y", label: "old/y (not active)" });
  assert.deepEqual(refOptions(app("nope"), { refs: ACTIVE, showAll: false }, NOW)[0].options[1],
    { value: "nope", label: "nope (not found)" });
  assert.deepEqual(refOptions(app("feat/x"), { showAll: false }, NOW)[0].options[1],
    { value: "feat/x", label: "feat/x (loading…)" });
  // Listed: not repeated.
  assert.equal(refOptions(app("feat/x"), { refs: ACTIVE, showAll: false }, NOW)[0].options.length, 1);
});

test("the bootloader lists tags only, with no show-all toggle", () => {
  const tags = { ...ACTIVE, tags: [{ name: "v1.6.2", sha: "eeee", built: true }, { name: "v1.6.1", sha: "ffff", built: false }] };
  assert.deepEqual(shownKinds(boot(), false), ["tags"]);
  const groups = refOptions(boot(), { refs: tags, showAll: false }, NOW);
  assert.deepEqual(values(groups), [["", [""]], ["tags", ["v1.6.1"]]]);
  assert.equal(groups[1].options[0].label, "v1.6.1 · not built");
});

test("the line under the app: commit, age, author; built or not", () => {
  const lists = { refs: ACTIVE, showAll: false };
  assert.equal(detailText(app("feat/x"), lists, NOW), "1a2b3c4d · 3 d ago · raul");
  assert.equal(notBuilt(app("feat/x"), lists), true);
  assert.equal(refDetail(app(), lists).name, "dev"); // empty: the catalogue's
  assert.equal(notBuilt(app(), lists), false);
  assert.equal(detailText(app(), { showAll: false }, NOW), "loading refs…");
});

test("without the GitHub API every branch is listed, and the note says why", () => {
  const plain = { ...ACTIVE, details: "ls-remote", note: "branch dates and PRs unavailable (rate limit)" };
  assert.equal(listingNote(app(), { refs: plain, showAll: false }), plain.note);
  assert.equal(refOptions(app(), { refs: plain, showAll: false }, NOW)[1].label, "branches");
  assert.equal(listingNote(app(), { refs: ACTIVE, showAll: false }), "");
  assert.equal(listingNote(boot(), { refs: plain, showAll: false }), "");
});
