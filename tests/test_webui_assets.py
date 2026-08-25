from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "merced_ai" / "webui"


class AccessibilityParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.landmarks: set[str] = set()
        self.dialogs = 0
        self.live_regions = 0
        self.html_language = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"] or "")
        if tag == "html":
            self.html_language = values.get("lang") or ""
        if tag in {"main", "nav", "aside"}:
            self.landmarks.add(tag)
        if tag == "dialog":
            self.dialogs += 1
        if values.get("aria-live"):
            self.live_regions += 1
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1


def test_webui_assets_have_accessible_secure_structure() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    parser = AccessibilityParser()
    parser.feed(html)

    assert parser.html_language == "en"
    assert parser.landmarks == {"main", "nav", "aside"}
    assert parser.dialogs == 2
    assert parser.live_regions >= 3
    assert parser.inline_scripts == 0
    assert parser.inline_styles == 0
    assert len(parser.ids) == len(set(parser.ids))
    assert 'class="skip-link"' in html
    assert 'aria-label="Message"' in html


def test_webui_javascript_wires_primary_product_controls() -> None:
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    for control in (
        "#new-thread",
        "#new-group",
        "#composer",
        "#cancel-run",
        "#bot-select",
        "#harness-select",
        "#dispatch-select",
        "#management-action",
        "#session-search",
        "#open-navigation",
        "#export-session",
        "#theme-toggle",
    ):
        assert f"$({control!r})".replace("'", '"') in script
    assert "window.location.hash.slice(1)" in script
    assert "history.replaceState" in script
    assert "/api/auth" in script
    assert "approval_required" in script
    assert "participant_error" in script
    assert "tool_event" in script
    assert "navigator.clipboard.writeText" in script
    assert "localStorage.setItem" in script
    assert "retry-run" in script
    assert "escapeHtml(turn.content)" not in script  # Markdown renderer escapes before formatting.


def test_webui_styles_include_responsive_and_reduced_motion_contracts() -> None:
    styles = (ROOT / "styles.css").read_text(encoding="utf-8")

    assert "@media (max-width: 720px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ".sidebar.open" in styles
    assert ":focus-visible" in styles
    assert ".sr-only" in styles
