// The web app's front page (/): a redirect into the workspace (/editor/),
// which replaced the shell's own pages (step 18 of
// docs/architecture/editor-workspace.md). Old links (#/runs/<id>,
// #/classic/runs/<id>/signals, #/systems/<id>…) land where their page's
// content is now (editor.js redirectFor). Right after a logout
// (vhil/server/auth.py) it stays here: the workspace's first request would
// send the browser back to the login.
import { redirectFor } from "./editor.js";

const view = document.getElementById("view");

if (new URLSearchParams(location.search).has("logged_out")) {
  const p = document.createElement("p");
  const a = document.createElement("a");
  a.href = "/auth/login";
  a.textContent = "Log in with GitHub";
  p.append("Logged out. ", a);
  view.replaceChildren(p);
} else {
  location.replace(redirectFor(location.hash));
}
