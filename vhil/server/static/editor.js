// The Editor route: the editor is the vHIL workspace now (step 7 of
// docs/architecture/editor-workspace.md), a page of its own on this origin
// under /editor/ (Pipeline Manager, editor/pipeline-manager/), with its own
// top bar, Run, Commit and PR. Old #/editor[/<system>] links redirect there.
// What Run sends stays in editor-run.js, which the workspace's build copies
// (docker/editor.Dockerfile); the default run length is the server's
// run_virtual_ms (GET /api/config), which the workspace reads.

/** The workspace's URL, opening `system` if given. */
export function editorUrl(system) {
  const u = new URL("/editor/", location.href);
  if (system) u.searchParams.set("system", system);
  return u.pathname + u.search;
}

export function editorPage(_view, system) {
  location.replace(editorUrl(system));
}
