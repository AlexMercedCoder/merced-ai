from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MERCED_AI_BROWSER_E2E") != "1",
    reason="browser E2E is enabled in the dedicated Chromium CI step",
)


def test_group_ui_end_to_end(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import expect, sync_playwright
    from uvicorn import Config, Server

    from merced_ai.bots import create_bot
    from merced_ai.profiles import create_profile
    from merced_ai.webui_server import create_web_app

    fake_codex = Path(__file__).parent / "fixtures" / "fake_codex.py"
    monkeypatch.setenv("MERCED_AI_CODEX_PATH", str(fake_codex))
    monkeypatch.setenv("MERCED_AI_FAKE_CODEX_VERSION_DELAY", "0.5")
    for name, description in (
        ("reviewer", "Reviews correctness and concrete risks."),
        ("builder", "Proposes practical implementation steps."),
        ("tester", "Designs focused validation scenarios."),
    ):
        create_profile(
            name,
            description,
            f"Act as the {name} in a product council.",
            workspace,
            edit_permission="deny",
            shell_permission="deny",
        )
        create_bot(name, name, "codex", (), workspace)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = Server(
        Config(
            create_web_app(workspace, "browser-token"),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started

    screenshot_root = Path(os.environ.get("MERCED_AI_SCREENSHOT_DIR", "/tmp"))
    screenshot_root.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 980})
            browser_errors: list[str] = []
            page.on("console", lambda message: browser_errors.append(f"console: {message.text}"))
            page.on("pageerror", lambda error: browser_errors.append(f"pageerror: {error}"))
            page.on(
                "requestfailed",
                lambda request: browser_errors.append(
                    f"requestfailed: {request.method} {request.url} {request.failure}"
                ),
            )
            opened_at = time.monotonic()
            page.goto(f"http://127.0.0.1:{port}/#token=browser-token")
            try:
                expect(page.locator("#workspace-name")).not_to_have_text("Loading…", timeout=30_000)
            except AssertionError as error:
                page.screenshot(path=screenshot_root / "merced-ai-browser-failure.png")
                details = "; ".join(browser_errors) or "no browser errors captured"
                raise AssertionError(f"{error}\nBrowser diagnostics: {details}") from error
            assert time.monotonic() - opened_at < 2
            expect(page.locator("#harness-detection-state")).to_contain_text("Detecting")
            expect(page.locator("#harness-detection-state")).to_have_text(
                "Detection complete", timeout=30_000
            )
            expect(page.locator("#healthy-count")).to_have_text(re.compile(r"^[1-9]\d* ready$"))
            page.locator("#refresh-harnesses").click()
            expect(page.locator("#refresh-harnesses")).to_be_disabled()
            expect(page.locator("#harness-detection-state")).to_contain_text("Detecting")
            expect(page.locator("#harness-detection-state")).to_have_text(
                "Detection complete", timeout=30_000
            )

            page.locator("#new-group").click()
            expect(page.locator("#group-dialog")).to_be_visible()
            expect(page.locator('#group-options input[type="checkbox"]:checked')).to_have_count(2)
            page.locator("#group-title").fill("Release readiness council")
            page.locator('#group-options input[value="tester"]').check()
            expect(page.locator("#group-selected .group-selection")).to_have_count(3)
            page.locator("#group-submit").click()
            expect(page.locator("#group-dialog")).not_to_be_visible()
            expect(page.locator("#conversation-title")).to_have_text("Release readiness council")
            expect(page.locator("#participant-list .participant-chip")).to_have_count(3)

            page.locator("#dispatch-select").select_option("all")
            page.locator("#message-input").fill("Assess the release candidate independently.")
            page.locator("#send-message").click()
            expect(page.locator(".message.assistant")).to_have_count(3, timeout=15_000)
            expect(page.locator("#message-input")).to_be_enabled(timeout=15_000)
            expect(page.locator(".pending-response")).to_have_count(0)
            expect(page.locator(".message-speaker")).to_have_count(3)
            expect(page.locator(".activity")).to_have_count(3)

            page.locator("#message-input").fill("@te")
            expect(page.locator("#mention-menu")).to_be_visible()
            expect(page.locator('#mention-menu [data-mention="tester"]')).to_be_visible()
            page.locator('#mention-menu [data-mention="tester"]').click()
            expect(page.locator("#message-input")).to_have_value("@tester ")

            page.screenshot(
                path=screenshot_root / "merced-ai-group-desktop.jpg",
                type="jpeg",
                quality=88,
                full_page=True,
            )
            page.locator("#derive-group").click()
            expect(page.locator("#group-dialog-title")).to_have_text(
                "Start with different participants"
            )
            page.locator("#group-dialog").locator(".group-cancel").last.click()

            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            mobile.goto(f"http://127.0.0.1:{port}/#token=browser-token")
            expect(mobile.locator("#workspace-name")).not_to_have_text("Loading…", timeout=30_000)
            expect(mobile.locator("#conversation-title")).to_have_text("Release readiness council")
            expect(mobile.locator("#composer-status")).to_have_text("")
            mobile.locator(".message.assistant").last.scroll_into_view_if_needed()
            mobile.evaluate("document.activeElement?.blur()")
            mobile.screenshot(
                path=screenshot_root / "merced-ai-group-mobile.jpg",
                type="jpeg",
                quality=88,
                full_page=True,
            )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
