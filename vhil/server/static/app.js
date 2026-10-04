// The app shell: hash routes over the JSON API (docs/architecture/m5-web-app.md).
import { editorPage } from "./editor.js";

const view = document.getElementById("view");

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.json();
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
      <p class="muted">${esc(s.path)} @ ${esc(s.ref.slice(0, 8) || "working tree")} · <a href="#/editor/${esc(s.id)}">edit</a></p>
      ${s.errors.length ? `<p class="error">${esc(s.errors.join("; "))}</p>` : ""}
      <pre>${esc(s.yaml)}</pre>`;
  },
  async runs() { view.innerHTML = `<h2>Runs</h2><p class="muted">Coming with #114.</p>`; },
  async editor(id) { await editorPage(view, id); },
};

async function route() {
  const [, page = "systems", arg] = location.hash.split("/");
  document.body.dataset.page = page;
  try {
    if (page === "systems" && arg) await routes.system(decodeURIComponent(arg));
    else await (routes[page] || routes.systems)(arg && decodeURIComponent(arg));
  } catch (e) {
    view.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}

window.addEventListener("hashchange", route);
api("/api/health").then((h) => {
  document.getElementById("status").textContent = `${h.version} · auth ${h.auth} · ${h.workspace_ref.slice(0, 8)}`;
}).catch(() => {});
route();
