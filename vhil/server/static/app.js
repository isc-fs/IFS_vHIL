// The app shell: hash routes over the JSON API (docs/architecture/m5-web-app.md).
import { renderRun } from "./inspect.js";
import { renderRuns } from "./runs.js";

const view = document.getElementById("view");

// Every request goes through here (fetch options in `opts`; `as: "text"` for
// a non-JSON body).
async function api(path, opts = {}) {
  const { as = "json", ...init } = opts;
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return as === "text" ? r.text() : r.json();
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const routes = {
  async systems() {
    const systems = await api("/api/systems");
    view.innerHTML = `<h2>Systems</h2><table><tr><th>System</th><th>Boards</th><th>Description</th></tr>${
      systems.map((s) => `<tr><td><a href="#/systems/${esc(s.id)}">${esc(s.id)}</a></td>
        <td>${esc(s.boards.join(", "))}</td><td class="muted">${esc(s.description)}</td></tr>`).join("")}</table>`;
  },
  async system(id) {
    const s = await api(`/api/systems/${encodeURIComponent(id)}`);
    view.innerHTML = `<h2>${esc(s.id)}</h2>
      <p class="muted">${esc(s.path)} @ ${esc(s.ref.slice(0, 8) || "working tree")}</p>
      ${s.errors.length ? `<p class="error">${esc(s.errors.join("; "))}</p>` : ""}
      <pre>${esc(s.yaml)}</pre>`;
  },
  async runs() { await renderRuns(view, { api, esc }); },
  async editor() { view.innerHTML = `<h2>Editor</h2><p class="muted">Coming with #116.</p>`; },
};

async function route() {
  const [, page = "systems", arg, sub] = location.hash.split("/");
  try {
    if (page === "systems" && arg) await routes.system(decodeURIComponent(arg));
    else if (page === "runs" && /^\d+$/.test(arg || "")) await renderRun(view, { api, esc }, Number(arg), sub);
    else await (routes[page] || routes.systems)();
  } catch (e) {
    view.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}

window.addEventListener("hashchange", route);
api("/api/health").then((h) => {
  document.getElementById("status").textContent = `${h.version} · auth ${h.auth} · ${h.workspace_ref.slice(0, 8)}`;
}).catch(() => {});
route();
