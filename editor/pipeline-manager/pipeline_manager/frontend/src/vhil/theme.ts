/*
 * vHIL: the editor's theme (CHANGELOG-VHIL.md).
 *
 * The colours are the vHIL design tokens (shell/tokens.css, copied from
 * vhil/server/static/ by the image build): dark on :root, light under
 * data-theme="light" on <html>, and with no data-theme the browser's
 * prefers-color-scheme picks. The editor starts dark unless its URL says
 * ?theme=light|auto, and the shell switches it with the frontend procedure
 * vhil_set_theme (postMessage or the backend), so both change together.
 *
 * No DOM access at import: validator.js loads this module under Node.
 */

export const THEMES = ['dark', 'light', 'auto'] as const;
export type Theme = typeof THEMES[number];

export const DEFAULT_THEME: Theme = 'dark';

/** The theme a URL query asks for (`?theme=`), else the default. */
export function themeFromQuery(search: string): Theme {
    const asked = new URLSearchParams(search).get('theme');
    return (THEMES as readonly string[]).includes(asked ?? '') ? asked as Theme : DEFAULT_THEME;
}

/** Applies a theme to the document: 'auto' follows prefers-color-scheme. */
export function setTheme(theme: Theme): void {
    const root = document.documentElement;
    if (theme === 'auto') delete root.dataset.theme;
    else root.dataset.theme = theme;
}
