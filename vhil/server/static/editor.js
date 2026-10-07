// The shell's routes into the editor: the editor is the vHIL workspace
// (docs/architecture/editor-workspace.md), a page of its own on this origin
// under /editor/ (Pipeline Manager, editor/pipeline-manager/), with its own
// top bar, Systems and Runs, Run, Commit and PR, and REPLAY of a run. Old
// links redirect there (app.js): #/editor[/<system>] and #/systems[/<system>]
// open a system, #/runs the Runs view, #/runs/<id>[/<tab>] REPLAY of the run.
// What Run sends stays in editor-run.js, which the workspace's build copies
// (docker/editor.Dockerfile); the default run length is the server's
// run_virtual_ms (GET /api/config), which the workspace reads.

/** The workspace's URL: opening `system`, replaying `run`, or showing `view`. */
export function editorUrl(system, { run, view } = {}) {
  const u = new URL("/editor/", location.href);
  if (system) u.searchParams.set("system", system);
  if (run) u.searchParams.set("run", String(run));
  if (view) u.searchParams.set("view", view);
  return u.pathname + u.search;
}

export function editorPage(_view, system, opts) {
  location.replace(editorUrl(system, opts));
}
