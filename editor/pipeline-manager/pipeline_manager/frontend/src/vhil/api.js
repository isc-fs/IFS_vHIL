/*
 * vHIL: the web app's JSON API, from the workspace (CHANGELOG-VHIL.md).
 *
 * The editor is served on the app's origin under /editor/ (deploy/Caddyfile),
 * so /api is the same origin: the session cookie goes with every request, and
 * writes carry the session's CSRF token from the vhil_csrf cookie, as the
 * shell's app.js does (vhil/server/auth.py). A 401 goes to the GitHub login
 * and comes back to this page.
 */

const cookie = (name) => document.cookie.split('; ')
    .find((c) => c.startsWith(`${name}=`))?.slice(name.length + 1);

/** A refused request: `errors` lists what the server said, `status` its code. */
export class ApiError extends Error {
    constructor(errors, status, detail) {
        super(errors.join('; '));
        this.errors = errors;
        this.status = status;
        this.detail = detail;
    }
}

export async function call(method, path, body) {
    const headers = body ? { 'Content-Type': 'application/json' } : {};
    const csrf = cookie('vhil_csrf');
    if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf);
    const r = await fetch(path, {
        method, headers, body: body ? JSON.stringify(body) : undefined, credentials: 'same-origin',
    });
    if (r.status === 401) {
        const here = window.location.pathname + window.location.search;
        window.location.href = `/auth/login?next=${encodeURIComponent(here)}`;
        throw new ApiError(['login required'], 401);
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
        const d = data.detail;
        let errors;
        if (d?.errors) errors = d.errors;
        else if (Array.isArray(d)) errors = d.map((e) => `${e.loc.join('.')}: ${e.msg}`);
        else errors = [typeof d === 'string' ? d : `${r.status} ${r.statusText}`];
        throw new ApiError(errors, r.status, d);
    }
    return data;
}

/** The shell's page for a run (its frames, plots, log). */
export const runPage = (id) => `/#/runs/${id}`;
