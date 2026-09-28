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
    assert parser.dialogs == 7
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
        "#derive-group",
        "#rename-session",
        "#composer",
        "#cancel-run",
        "#bot-select",
        "#harness-select",
        "#refresh-harnesses",
        "#dispatch-select",
        "#group-form",
        "#mention-menu",
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
    assert ".status-dot.detecting" in styles


def test_first_run_token_and_draft_contracts() -> None:
    """UI-3: never drop a draft, guide a workspace with no bots, explain a missing token."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "styles.css").read_text(encoding="utf-8")

    # Text entry never depends on having a bot; only Send does, with an inline reason.
    assert '$("#message-input").disabled = Boolean(state.activeRun);' in script
    assert '$("#message-input").disabled = !bot' not in script
    assert 'id="composer-hint"' in html and "Your draft stays here." in script
    assert "renderComposerHint({ attempted: true });  // Keep the draft" in script
    # First run: detected harnesses, one-click default bot, docs link, inspector call to action.
    assert 'id="first-run"' in html and 'id="inspector-create-bot"' in html
    assert "Create your first bot" in script and "createFirstBot" in script
    assert 'edit_permission: "ask"' in script and 'shell_permission: "ask"' in script
    # A 401 gets its own screen with a token field instead of the app shell.
    assert 'id="connect-screen"' in html and 'id="connect-token"' in html
    assert "error.status === 401" in script and "showConnectScreen" in script
    # Shortcut chip stays on one line; conversation-only actions hide without a conversation.
    assert ".shortcut { display: inline-flex;" in styles and "white-space: nowrap" in styles
    assert '"#delete-session", "#derive-group"]) $(id).hidden = !session;' in script
    assert ".danger-button:disabled" in styles


GENERIC_FAMILIES = {
    "serif",
    "sans-serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "ui-serif",
    "ui-sans-serif",
    "ui-monospace",
    "ui-rounded",
    "emoji",
    "math",
    "fangsong",
}


def _font_stacks(css: str) -> list[str]:
    import re

    without_faces = re.sub(r"@font-face\s*{[^}]*}", "", css)
    stacks = re.findall(r"font-family:\s*([^;}]+)", without_faces)
    for value in re.findall(r"(?<![-\w])font:\s*([^;}]+)", without_faces):
        if value.strip() not in {"inherit", "initial", "unset", "revert"}:
            stacks.append(value)
    return stacks


def test_every_font_stack_ends_in_a_generic_family() -> None:
    """UI-5: the layout must not depend on a font the user may not have installed."""
    css = (ROOT / "styles.css").read_text(encoding="utf-8")
    stacks = _font_stacks(css)
    assert len(stacks) >= 5
    for stack in stacks:
        last = stack.split(",")[-1].split()[-1].strip().strip("\"'")
        assert last in GENERIC_FAMILIES, f"font stack without a generic fallback: {stack!r}"
    # The linter itself catches a missing fallback.
    bad = _font_stacks(".x { font-family: Inter; } .y { font: 600 12px/1 Georgia; }")
    assert all(item.split(",")[-1].split()[-1] not in GENERIC_FAMILIES for item in bad)


def test_web_fonts_are_self_hosted_licensed_and_swapped() -> None:
    import re

    css = (ROOT / "styles.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*{([^}]*)}", css)
    assert {re.search(r'font-family:\s*"([^"]+)"', face)[1] for face in faces} == {
        "Inter Variable",
        "Source Serif 4 Variable",
    }
    for face in faces:
        assert "font-display: swap" in face
        for url in re.findall(r'url\("([^"]+)"\)', face):
            assert not url.startswith(("http:", "https:", "//")), url  # CSP: font-src 'self'
            assert (ROOT / url).is_file(), url
            assert url.endswith(".woff2") and "latin" in url
    for license_file in ("OFL-Inter.txt", "OFL-SourceSerif4.txt"):
        text = (ROOT / "fonts" / license_file).read_text(encoding="utf-8")
        assert "SIL Open Font License" in text
