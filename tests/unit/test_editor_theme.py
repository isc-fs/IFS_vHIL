"""The vendored Pipeline Manager's theme stays on the shell's design tokens
(editor/pipeline-manager/CHANGELOG-VHIL.md, docs/architecture/editor-workspace.md
step 4): tokens.css is the one source, and the copies of its values the editor
needs as fallbacks match it."""
import json
import re
from pathlib import Path

from vhil.system import REPO

TOKENS = REPO / "vhil/server/static/tokens.css"
PM = REPO / "editor/pipeline-manager/pipeline_manager"
FRONTEND = PM / "frontend"

VAR = re.compile(r"var\(\s*(--[\w-]+)\s*,\s*([^()]+?)\s*\)")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace('"', "'")).strip().lower()


def _dark_tokens() -> dict[str, str]:
    """Every custom property the default (dark) theme defines: the :root
    blocks outside the light theme's rules."""
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.S)
    css = css.split("@media (prefers-color-scheme: light)")[0]
    tokens = {}
    for block in re.findall(r"(?<![\w\]]):root\s*\{(.*?)\}", css, flags=re.S):
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block):
            tokens[name] = _norm(value)
    return tokens


def _fallbacks(text: str) -> list[tuple[str, str]]:
    return [(n, _norm(v)) for n, v in VAR.findall(text)]


def test_scss_variables_fall_back_to_the_dark_tokens():
    tokens = _dark_tokens()
    found = _fallbacks((FRONTEND / "styles/_variables.scss").read_text())
    assert {n for n, _ in found} >= {"--fg", "--bg-0", "--accent", "--focus", "--font-ui",
                                     "--font-mono"}
    for name, fallback in found:
        assert name in tokens, f"{name} is not a token in tokens.css"
        assert fallback == tokens[name], f"{name}: fallback {fallback} != token {tokens[name]}"


def test_canvas_background_defaults_to_the_page_token():
    tokens = _dark_tokens()
    schema = json.loads((PM / "resources/schemas/metadata_schema.json").read_text())
    [(name, fallback)] = _fallbacks(schema["properties"]["backgroundColor"]["default"])
    assert (name, fallback) == ("--bg-0", tokens["--bg-0"])


def test_set_theme_procedure_matches_the_theme_module():
    spec = json.loads((PM / "resources/api_specification/specification.json").read_text())
    theme = spec["frontend_endpoints"]["vhil_set_theme"]["params"]["properties"]["theme"]
    ts = (FRONTEND / "src/vhil/theme.ts").read_text()
    themes = re.search(r"THEMES = \[(.*?)\] as const", ts).group(1)
    assert theme["enum"] == re.findall(r"'(\w+)'", themes)
    assert "export function vhil_set_theme" in (
        FRONTEND / "src/core/communication/remoteProcedures.ts").read_text()


def test_no_remote_fonts_and_a_focus_ring():
    for path in [*(FRONTEND / "styles").glob("*.scss"), FRONTEND / "index.html"]:
        assert "fonts.googleapis" not in path.read_text(), path
    assert re.search(r":focus-visible\s*\{\s*outline: 2px solid \$focus",
                     (FRONTEND / "styles/_global.scss").read_text())
