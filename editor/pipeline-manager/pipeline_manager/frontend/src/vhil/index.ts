/*
 * vHIL: what the editor loads before Pipeline Manager renders
 * (CHANGELOG-VHIL.md): the shell's design tokens and self-hosted fonts, and
 * the theme the URL asks for.
 *
 * shell/ is not in git: docker/editor.Dockerfile copies it from
 * vhil/server/static/ (tokens.css and fonts/), the one source the shell uses
 * too, and checks the copy (docs/development/setup.md for a native build).
 */
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import './shell/tokens.css';
import './theme.css';
import { setTheme, themeFromQuery } from './theme';

setTheme(themeFromQuery(window.location.search));
