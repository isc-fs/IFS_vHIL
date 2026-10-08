// Where the shell's old links go now. The workspace (/editor/: Pipeline
// Manager, editor/pipeline-manager/; docs/architecture/editor-workspace.md)
// replaced every page the shell had: its run page (REPLAY with the Bus,
// Signals, Log and Artifacts tabs), its runs list and pytest form (Runs, and
// Tests… in the top bar), its systems pages (Systems). Its links keep
// working: app.js sends each one here. What Run sends stays in
// editor-run.js, which the workspace's build copies (docker/editor.Dockerfile).

/** The workspace's URL: opening `system`, replaying `run` on dock tab `tab`,
 *  or showing sidebar `view`. */
export function editorUrl(system, { run, view, tab } = {}) {
  const q = new URLSearchParams();
  if (system) q.set("system", system);
  if (run) q.set("run", String(run));
  if (tab) q.set("tab", tab);
  if (view) q.set("view", view);
  const s = q.toString();
  return `/editor/${s ? `?${s}` : ""}`;
}

// The run page's tabs, as the dock's.
const RUN_TABS = { frames: "bus", signals: "signals", edges: "signals", log: "log", artifacts: "artifacts" };

/**
 * The workspace URL an old shell link (its hash) stands for:
 * #/[classic/]runs/<id>[/<tab>] replays the run (on that tab), #/[classic/]runs
 * the Runs view, #/[classic/]systems/<id> and #/editor/<id> open the system,
 * anything else the Systems view.
 */
export function redirectFor(hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean)
    .map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  if (parts[0] === "classic") parts.shift();
  const [page, arg, sub] = parts;
  if (page === "runs") {
    return /^\d+$/.test(arg || "") ? editorUrl("", { run: Number(arg), tab: RUN_TABS[sub] })
      : editorUrl("", { view: "runs" });
  }
  if ((page === "systems" || page === "editor") && arg) return editorUrl(arg);
  return page ? editorUrl("", { view: "systems" }) : editorUrl("");
}
