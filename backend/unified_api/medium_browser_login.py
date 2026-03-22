"""
Automated Medium.com sign-in via Google account email/password using Playwright.

Callers pass email/password from the shared encrypted store (see ``google_browser_login_credentials``);
optional env overrides behavior:
  MEDIUM_BROWSER_HEADLESS   — default 0 (headed); set 1/true for headless Chromium
  MEDIUM_BROWSER_TIMEOUT_MS — default 180000

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_ENV_HEADLESS = "MEDIUM_BROWSER_HEADLESS"
_ENV_TIMEOUT_MS = "MEDIUM_BROWSER_TIMEOUT_MS"


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _google_page(context, main_page, timeout_ms: int):
    """Return the Playwright page that is on accounts.google.com (popup or same tab)."""
    import time

    end = time.monotonic() + min(45.0, timeout_ms / 1000.0)
    while time.monotonic() < end:
        for p in context.pages:
            try:
                if "accounts.google.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        try:
            main_page.wait_for_url(re.compile(r".*accounts\.google\.com.*"), timeout=800)
            return main_page
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("Did not reach Google sign-in (accounts.google.com). Medium UI may have changed.")


def _click_first_visible(page, selectors: list[str], timeout_each: int = 2500) -> bool:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0:
                loc.click(timeout=timeout_each)
                return True
        except Exception:
            continue
    return False


def _open_medium_sign_in(page) -> None:
    page.goto("https://medium.com/m/signin", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)


def _medium_click_google_button(page, candidates: list[str]) -> None:
    if _click_first_visible(page, candidates, timeout_each=4000):
        return
    try:
        page.get_by_role("button", name=re.compile(r"google", re.I)).first.click(timeout=8000)
    except Exception:
        page.get_by_text(re.compile(r"continue with google", re.I)).first.click(timeout=8000)


def _click_medium_google(page, context, timeout_ms: int) -> None:
    """Click Medium's Google sign-in once; wait for accounts.google.com in any page or main tab."""
    import time

    candidates = [
        'button:has-text("Continue with Google")',
        'div[role="button"]:has-text("Google")',
        'button:has-text("Google")',
        '[data-testid*="google" i]',
        'a:has-text("Sign in with Google")',
        'span:has-text("Sign in with Google")',
    ]
    _medium_click_google_button(page, candidates)
    end = time.monotonic() + min(20.0, timeout_ms / 1000.0)
    while time.monotonic() < end:
        for p in context.pages:
            try:
                if "accounts.google.com" in (p.url or ""):
                    p.wait_for_load_state("domcontentloaded", timeout=5000)
                    return
            except Exception:
                continue
        try:
            if "accounts.google.com" in (page.url or ""):
                return
        except Exception:
            pass
        time.sleep(0.2)


def _google_fill_email(google_page, email: str, timeout_ms: int) -> None:
    google_page.wait_for_load_state("domcontentloaded")
    box = google_page.locator('input#identifierId, input[type="email"], input[name="identifier"]').first
    box.wait_for(state="visible", timeout=min(20000, timeout_ms))
    box.fill(email)
    for name in ("Next", "Continue"):
        try:
            google_page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I)).first.click(
                timeout=8000,
            )
            return
        except Exception:
            continue
    google_page.locator("#identifierNext").first.click(timeout=8000)


def _google_fill_password(google_page, password: str, timeout_ms: int) -> None:
    pwd = google_page.locator('input[name="Passwd"], input[type="password"]').first
    pwd.wait_for(state="visible", timeout=min(25000, timeout_ms))
    pwd.fill(password)
    for label in ("Next", "Sign in", "Continue"):
        try:
            google_page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).first.click(
                timeout=10000,
            )
            return
        except Exception:
            continue
    for sel in ("#passwordNext", "#submit"):
        try:
            google_page.locator(sel).first.click(timeout=8000)
            return
        except Exception:
            continue
    raise RuntimeError("Could not submit Google password step (button not found).")


def _wait_signed_into_medium(context, main_page, timeout_ms: int) -> None:
    """Wait until some Medium page shows a logged-in state or /me loads."""
    import time

    deadline = time.monotonic() + min(timeout_ms / 1000.0, 120.0)
    medium_re = re.compile(r"https?://([^/]+\.)?medium\.com/.*")
    while time.monotonic() < deadline:
        for p in context.pages:
            try:
                url = p.url or ""
            except Exception:
                continue
            if not medium_re.match(url):
                continue
            if "accounts.google.com" in url or "google.com/oauth" in url:
                continue
            if "/m/signin" not in url:
                p.wait_for_load_state("domcontentloaded", timeout=5000)
                return
        time.sleep(0.35)
    raise RuntimeError(
        "Timed out waiting for Medium after Google login. If Google showed 2FA or a challenge, "
        "use headed mode (MEDIUM_BROWSER_HEADLESS=0) and MEDIUM_BROWSER_TIMEOUT_MS.",
    )


def perform_medium_google_browser_login(
    email: str,
    password: str,
    *,
    headless: bool | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """
    Launch Chromium, open Medium sign-in, use Google with the given email/password, return storage_state dict.
    """
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        raise RuntimeError("Medium Google browser login requires a stored email and password.")

    if headless is None:
        headless = _env_truthy(_ENV_HEADLESS, "0")
    if timeout_ms is None:
        try:
            timeout_ms = int(os.environ.get(_ENV_TIMEOUT_MS, "180000").strip())
        except ValueError:
            timeout_ms = 180000
    timeout_ms = max(30000, min(int(timeout_ms), 600000))

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install with: pip install playwright && playwright install chromium",
        ) from e

    logger.info("Starting Medium browser Google login (headless=%s)", headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
            main_page = context.new_page()
            main_page.set_default_timeout(timeout_ms)

            _open_medium_sign_in(main_page)
            _click_medium_google(main_page, context, timeout_ms)

            google_page = _google_page(context, main_page, timeout_ms)
            _google_fill_email(google_page, email, timeout_ms)
            google_page.wait_for_timeout(800)
            _google_fill_password(google_page, password, timeout_ms)

            _wait_signed_into_medium(context, main_page, timeout_ms)

            state = context.storage_state()
            if not isinstance(state, dict):
                raise RuntimeError("Playwright returned an invalid storage state.")
            return state
        except PlaywrightTimeoutError as e:
            raise RuntimeError(
                "Playwright timed out during Medium/Google login. Try MEDIUM_BROWSER_HEADLESS=0, "
                f"increase {_ENV_TIMEOUT_MS}, or sign in manually once and import storage_state.",
            ) from e
        finally:
            browser.close()


def perform_medium_google_browser_login_json(email: str, password: str) -> str:
    """Run login and return minified JSON string suitable for set_medium_session_storage_state_json."""
    state = perform_medium_google_browser_login(email, password)
    return json.dumps(state, separators=(",", ":"))
