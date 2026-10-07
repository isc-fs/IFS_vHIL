// The app shell: hash routes over the JSON API (docs/architecture/m5-web-app.md).
import { editorPage, editorUrl } from "./editor.js";
import { renderRun } from "./inspect.js";
import { renderRuns } from "./runs.js";

const view = document.getElementById("view");

const cookie = (name) => document.cookie.split("; ").find((c) => c.startsWith(`${name}=`))?.slice(name.length + 1);
const loggedOut = new URLSearchParams(location.search).has("logged_out");

// Every request goes through here (fetch options in `opts`; `as: "text"` for
// a non-JSON body, `as: "response"` for the Response itself, e.g. to read a
// header). Mutating requests carry the session's CSRF token
// (vhil/server/auth.py); a 401 in github mode goes to the GitHub login and
// comes back to the same page (not right after a logout).
async function api(path, opts = {}) {
  const { as = "json", ...init } = opts;
  const method = (init.method || "GET").toUpperCase();
  const headers = { ...(init.headers || {}) };
  const csrf = cookie("vhil_csrf");
  if (method !== "GET" && method !== "HEAD" && csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf);
  const r = await fetch(path, { ...init, method, headers });
  if (r.status === 401) {
    if (!loggedOut) location.href = `/auth/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
    throw new Error("login required");
  }
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return as === "text" ? r.text() : as === "response" ? r : r.json();
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// The shell's own pages, under #/classic/ (step 9 of
// docs/architecture/editor-workspace.md): the editor's workspace replaces
// them, and their old links redirect there. A run's page stays reachable
// (#/classic/runs/<id>) while REPLAY shows only its frames and log, not its
// signals and artifacts; the runs list keeps the pytest runs the workspace
// can't start yet.
const routes = {
  async systems() {
    const systems = await api("/api/systems");
    view.innerHTML = `<h2>Systems</h2><table><tr><th>System</th><th>Boards</th><th>Description</th></tr>${
      systems.map((s) => `<tr><td><a href="#/classic/systems/${esc(s.id)}">${esc(s.id)}</a></td>
        <td>${esc(s.boards.join(", "))}</td><td class="muted">${esc(s.description)}</td></tr>`).join("")}</table>`;
  },
  async system(id) {
    const s = await api(`/api/systems/${encodeURIComponent(id)}`);
    view.innerHTML = `<h2>${esc(s.id)}</h2>
      <p class="muted">${esc(s.path)} @ ${esc(s.ref.slice(0, 8) || "working tree")} · <a href="${esc(editorUrl(s.id))}">edit</a></p>
      ${s.errors.length ? `<p class="error">${esc(s.errors.join("; "))}</p>` : ""}
      <pre>${esc(s.yaml)}</pre>`;
  },
  async runs() { await renderRuns(view, { api, esc }); },
};

// Old links into the workspace: #/systems[/<id>], #/runs[/<id>[/<tab>]] and
// #/editor[/<id>] (and the shell's front page, #/systems by default).
const redirects = {
  systems: (arg) => editorPage(view, arg),
  editor: (arg) => editorPage(view, arg),
  runs: (arg) => (/^\d+$/.test(arg || "") ? editorPage(view, "", { run: Number(arg) })
    : editorPage(view, "", { view: "runs" })),
};

async function route() {
  const parts = location.hash.split("/");
  if (loggedOut && parts[1] !== "classic") {
    // Right after a logout (auth.py): no redirect into the workspace, whose
    // first request would send the browser back to the login.
    view.innerHTML = `<p>Logged out. <a href="/auth/login">Log in with GitHub</a></p>`;
    return;
  }
  if (parts[1] !== "classic") {
    const [, page = "systems", arg] = parts;
    (redirects[page] || redirects.systems)(arg && decodeURIComponent(arg));
    return;
  }
  const [, , page = "runs", arg, sub] = parts;
  document.body.dataset.page = page;
  try {
    if (page === "systems" && arg) await routes.system(decodeURIComponent(arg));
    else if (page === "runs" && /^\d+$/.test(arg || "")) await renderRun(view, { api, esc }, Number(arg), sub);
    else await (routes[page] || routes.runs)(arg && decodeURIComponent(arg));
  } catch (e) {
    view.innerHTML = loggedOut ? `<p>Logged out. <a href="/auth/login">Log in with GitHub</a></p>`
      : `<p class="error">${esc(e.message)}</p>`;
  }
}

window.addEventListener("hashchange", route);
api("/api/health").then((h) => {
  document.getElementById("status").textContent = `${h.version} · auth ${h.auth} · ${h.workspace_ref.slice(0, 8)}`;
}).catch(() => {});
api("/api/me").then((u) => {
  document.getElementById("user").innerHTML = `${u.avatar_url ? `<img src="${esc(u.avatar_url)}" alt="">` : ""}
    <span title="${esc(u.name)}">${esc(u.login)}</span>${u.auth === "github" ? ' <a href="/auth/logout">Log out</a>' : ""}`;
}).catch(() => {});
route();
