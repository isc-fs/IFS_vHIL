// The app shell: hash routes over the JSON API (docs/architecture/m5-web-app.md).
const view = document.getElementById("view");

const cookie = (name) => document.cookie.split("; ").find((c) => c.startsWith(`${name}=`))?.slice(name.length + 1);
const loggedOut = new URLSearchParams(location.search).has("logged_out");

// fetch() over the API. Mutating requests carry the session's CSRF token
// (vhil/server/auth.py); a 401 in github mode goes to the GitHub login and
// comes back to the same page (not right after a logout).
async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = { ...(opts.headers || {}) };
  const csrf = cookie("vhil_csrf");
  if (method !== "GET" && method !== "HEAD" && csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf);
  const r = await fetch(path, { ...opts, method, headers });
  if (r.status === 401) {
    if (!loggedOut) location.href = `/auth/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
    throw new Error("login required");
  }
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
      <p class="muted">${esc(s.path)} @ ${esc(s.ref.slice(0, 8) || "working tree")}</p>
      ${s.errors.length ? `<p class="error">${esc(s.errors.join("; "))}</p>` : ""}
      <pre>${esc(s.yaml)}</pre>`;
  },
  async runs() { view.innerHTML = `<h2>Runs</h2><p class="muted">Coming with #114.</p>`; },
  async editor() { view.innerHTML = `<h2>Editor</h2><p class="muted">Coming with #116.</p>`; },
};

async function route() {
  const [, page = "systems", arg] = location.hash.split("/");
  try {
    if (page === "systems" && arg) await routes.system(decodeURIComponent(arg));
    else await (routes[page] || routes.systems)();
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
