import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_app_titlebar_no_longer_contains_tps_chip():
    assert 'id="tpsStat"' not in INDEX_HTML


def _css_rule_declarations(selector):
    """Declarations of the first top-level rule for *selector*, as {property: value}."""
    match = re.search(r"(?m)^\s*" + re.escape(selector) + r"\{([^}]*)\}", STYLE_CSS)
    assert match, f"{selector} rule not found in static/style.css"
    decls = {}
    for part in match.group(1).split(";"):
        if ":" in part:
            prop, value = part.split(":", 1)
            decls[prop.strip()] = value.strip()
    return decls


def test_app_titlebar_returns_to_centered_desktop_layout():
    # Assert the centered-layout declarations, not the rule's exact text: other declarations
    # (e.g. -webkit-app-region for desktop webviews, #6059) may be added without changing the layout.
    bar = _css_rule_declarations(".app-titlebar")
    assert bar.get("display") == "flex"
    assert bar.get("align-items") == "center"
    assert bar.get("justify-content") == "center"
    inner = _css_rule_declarations(".app-titlebar-inner")
    for prop, value in {
        "display": "flex",
        "align-items": "center",
        "gap": "8px",
        "min-width": "0",
        "max-width": "100%",
        "justify-content": "center",
    }.items():
        assert inner.get(prop) == value, (prop, inner.get(prop))


def test_app_titlebar_subtitle_shows_message_count_again():
    assert "subText = String(vis.length);" in PANELS_JS


def test_queue_updates_do_not_hijack_app_titlebar_subtitle():
    assert "_syncQueueTitlebar" not in UI_JS
