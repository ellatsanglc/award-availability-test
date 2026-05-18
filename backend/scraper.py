"""
Cathay Pacific award flight scraper using Playwright.

Flow per search:
  1. Navigate to Cathay homepage
  2. Toggle "Redeem with miles"
  3. Fill origin, destination, trip type, cabin, dates
  4. Submit search
  5. Extract results from the results page

Session state (cookies) is saved to session_state.json after the first login
so subsequent runs skip the login step.
"""

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

from models import FlightResult, FlightOption, CABIN_LABELS, SearchRequest

# Load credentials from backend/.env (never committed to git)
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

CATHAY_HOME = "https://www.cathaypacific.com/cx/en_HK.html"  # login lives here
BOOKING_URL = "https://flights.cathaypacific.com/en_HK.html"  # award search lives here
PROFILE_DIR = Path(__file__).parent / "browser_profile"  # legacy single-user profile
SESSION_FILE = Path(__file__).parent / "session_state.json"  # legacy session snapshot
SESSION_PROFILES_DIR = Path(__file__).parent / "session_profiles"  # per-user profiles
AIRPORTS_CACHE = Path(__file__).parent / "destinations.json"
SEARCH_DELAY_SECONDS = 3

# True when running on Railway (or any cloud environment without a display)
IS_CLOUD = "RAILWAY_ENVIRONMENT" in os.environ

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-default-apps",
    # Required in Docker/cloud — Chromium won't start without these
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    # HTTP/2 can cause ERR_HTTP2_PROTOCOL_ERROR in containerised environments
    "--disable-http2",
    "--disable-quic",
]

_airports_lock: Optional[asyncio.Lock] = None

# In-memory map of search_id → active Playwright Page (for screenshot/interact endpoints)
active_pages: dict = {}


def get_session_profile_dir(session_id: str) -> Path:
    """Return (and create) a per-user browser profile directory."""
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id or 'default')[:64] or 'default'
    d = SESSION_PROFILES_DIR / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def get_screenshot_b64(page) -> str:
    """Capture the current page as a base64 JPEG for the virtual browser panel."""
    buf = await page.screenshot(type="jpeg", quality=70)
    return base64.b64encode(buf).decode()


# ---------------------------------------------------------------------------
# Destination airport scraper (one-time, cached)
# ---------------------------------------------------------------------------

# Multi-word country/region names that appear at the end of Cathay airport names.
# Single-word countries are handled by taking the last word of the name.
# Phrases in airport names that indicate a non-airport transport connection
# (China Railway, bus terminals, etc.) that Cathay sometimes lists.
_NON_AIRPORT_PHRASES = [
    "railway station", "train station", "rail station",
    "bus terminal", "coach terminal", "ferry terminal",
    "heliport",
]


def _is_real_airport(name: str) -> bool:
    nl = name.lower()
    return not any(phrase in nl for phrase in _NON_AIRPORT_PHRASES)


_MULTI_WORD_COUNTRIES = [
    "The Chinese Mainland", "Hong Kong", "Macau SAR", "Taiwan",
    "New Zealand", "United States", "United Kingdom",
    "Saudi Arabia", "United Arab Emirates", "South Korea",
    "Sri Lanka", "South Africa", "New Caledonia", "Papua New Guinea",
]
# Words that are part of airport names, not country names — skip as last-word fallback.
_AIRPORT_WORDS = {"Airport", "Int'l", "International", "Terminal", "Field", "Base", "Hub"}


def _extract_country_from_name(name: str) -> str:
    """
    Extract the country/region from a Cathay airport name string.
    Format is typically: 'City, Airport Name Country'
    """
    for country in _MULTI_WORD_COUNTRIES:
        if name.endswith(country):
            return country
    # Strip parenthesised suffixes like '(China)' → 'China'
    import re as _re
    m = _re.search(r'\(([^)]+)\)\s*$', name)
    if m:
        return m.group(1).strip()
    # Last word of the name — filter out words that are airport descriptors
    last_word = name.rsplit(None, 1)[-1] if name else "Other"
    return last_word if last_word not in _AIRPORT_WORDS else "Other"


async def scrape_all_destinations() -> List[dict]:
    """
    Scrape every destination airport Cathay offers from the booking autocomplete
    with the 'Redeem with miles' toggle enabled so only award destinations appear.
    Results are cached to destinations.json; delete the file to force a re-scrape.
    Cache is also invalidated automatically when it uses the old format (no country field).
    Thread-safe via asyncio lock.
    """
    if AIRPORTS_CACHE.exists():
        cached = json.loads(AIRPORTS_CACHE.read_text())
        # Reject cache if it's empty, missing country fields, or all entries landed as 'Other'
        # (which means a previous scrape ran without the toggle or failed mid-way)
        has_real_countries = cached and "country" in cached[0] and any(
            a.get("country", "Other") != "Other" for a in cached
        )
        if has_real_countries:
            return cached
        logger.info("scrape_destinations: cache is stale or has no country data — re-scraping")
        AIRPORTS_CACHE.unlink()

    global _airports_lock
    if _airports_lock is None:
        _airports_lock = asyncio.Lock()

    async with _airports_lock:
        if AIRPORTS_CACHE.exists():  # another coroutine beat us to it
            return json.loads(AIRPORTS_CACHE.read_text())

        airports: dict = {}
        logger.info("scrape_destinations: starting — will open a browser window briefly")

        with tempfile.TemporaryDirectory() as tmp_dir:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=tmp_dir,
                    headless=IS_CLOUD,
                    channel=None if IS_CLOUD else "chrome",
                    viewport={"width": 1280, "height": 800},
                    args=CHROMIUM_ARGS,
                    ignore_https_errors=True,
                )
                ctx.on("page", lambda popup: asyncio.ensure_future(popup.close()))
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    await page.goto(BOOKING_URL, timeout=30000)
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    # Fresh profiles load slower and may have cookie/promo overlays —
                    # wait longer and dismiss in two passes before touching the toggle.
                    await asyncio.sleep(4)
                    await dismiss_overlays(page)
                    await asyncio.sleep(2)
                    await dismiss_overlays(page)  # second pass in case a delayed overlay appeared

                    # Enable award/miles mode so only redeemable destinations appear.
                    # Retry up to 3 times in case the toggle wasn't ready yet.
                    for attempt in range(3):
                        await toggle_redeem_with_miles(page)
                        await asyncio.sleep(2)
                        # Verify toggle is ON by checking aria-checked state
                        is_on = await page.evaluate(r"""() => {
                            for (const el of document.querySelectorAll('[role="switch"],[role="checkbox"],input[type="checkbox"]')) {
                                const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
                                if (lbl.includes('redeem') || lbl.includes('miles')) {
                                    return el.checked || el.getAttribute('aria-checked') === 'true';
                                }
                            }
                            return null;
                        }""")
                        if is_on:
                            logger.info("scrape_destinations: miles toggle confirmed ON (attempt %d)", attempt + 1)
                            break
                        logger.warning("scrape_destinations: toggle not confirmed ON — retrying (attempt %d)", attempt + 1)
                        await asyncio.sleep(1)

                    await wait_for_booking_form(page)

                    # Find the destination input
                    dest_inp = None
                    for ph in DEST_PLACEHOLDERS:
                        loc = page.locator(f'input[placeholder="{ph}"]')
                        if await loc.count() > 0:
                            dest_inp = loc.first
                            break
                    if dest_inp is None:
                        logger.error("scrape_destinations: destination input not found")
                        return []

                    # Open the autocomplete panel
                    await dest_inp.locator("..").click()
                    await asyncio.sleep(0.5)

                    # Type each letter a–z and collect all offered options with country grouping
                    for letter in "abcdefghijklmnopqrstuvwxyz":
                        try:
                            # Clear via React setter so controlled input resets
                            await dest_inp.evaluate("""el => {
                                const setter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, '');
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.focus();
                            }""")
                            await asyncio.sleep(0.1)
                            await page.keyboard.type(letter, delay=60)
                            await asyncio.sleep(0.8)

                            # Extract options and their country groups in one JS pass
                            new_opts = await page.evaluate(r"""() => {
                                const result = [];
                                const seen = new Set();
                                for (const opt of document.querySelectorAll('[role="option"]')) {
                                    const text = (opt.innerText || opt.textContent || '').trim();
                                    const m = text.match(/\(([A-Z]{3})\)/);
                                    if (!m) continue;
                                    const code = m[1];
                                    if (seen.has(code)) continue;
                                    seen.add(code);
                                    const name = text.replace(/\s*,?\s*\([A-Z]{3}\)/, '').trim();
                                    // Walk up ancestors to find the nearest [role="group"]
                                    let country = null;
                                    let el = opt.parentElement;
                                    for (let i = 0; i < 10; i++) {
                                        if (!el || el === document.body) break;
                                        if (el.getAttribute('role') === 'group') {
                                            const lbl = el.getAttribute('aria-label');
                                            if (lbl) { country = lbl.trim(); break; }
                                            const hd = el.querySelector('[role="heading"],h2,h3,h4,h5');
                                            if (hd) { country = (hd.innerText || hd.textContent || '').trim(); break; }
                                            // First non-IATA line in the group's text
                                            const firstLine = (el.innerText || '').split('\n')
                                                .find(l => !l.match(/\([A-Z]{3}\)/) && l.trim().length > 2);
                                            if (firstLine) { country = firstLine.trim(); break; }
                                        }
                                        el = el.parentElement;
                                    }
                                    result.push({ code, name, country: country || 'Other' });
                                }
                                return result;
                            }""")

                            for item in new_opts:
                                code = item["code"]
                                if code not in airports:
                                    # If ARIA group detection didn't find a country, extract from name
                                    if item.get("country") == "Other":
                                        item["country"] = _extract_country_from_name(item["name"])
                                    airports[code] = item
                            logger.info("scrape_destinations: '%s' → %d total so far", letter, len(airports))
                        except Exception as e:
                            logger.warning("scrape_destinations: letter '%s' failed: %s", letter, e)
                finally:
                    await ctx.close()

        result = sorted(
            [a for a in airports.values() if _is_real_airport(a["name"])],
            key=lambda x: (x.get("country") or "Other", x["code"]),
        )
        AIRPORTS_CACHE.write_text(json.dumps(result, indent=2))
        logger.info("scrape_destinations: done — %d airports cached", len(result))
        return result


# ---------------------------------------------------------------------------
# Browser / session helpers
# ---------------------------------------------------------------------------

async def dismiss_overlays(page: Page):
    """Quietly close cookie banners or any modal that might block clicks."""
    for selector in [
        'button[aria-label="Close"]',
        '#onetrust-accept-btn-handler',
        'button:has-text("Accept")',
        '.close-button',
    ]:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                await el.click()
                await asyncio.sleep(0.4)
                break
        except Exception:
            pass


async def is_logged_in(page: Page) -> str:
    """
    Check login state.  Returns one of:
      'logged_in'     – header is loaded and shows no sign-in button
      'not_logged_in' – "Sign in / up" button is visibly present
      'unknown'       – page not fully loaded yet; caller should retry
    """
    try:
        return await page.evaluate("""() => {
            const header = document.querySelector('header');
            if (!header) return 'unknown';

            // Definitive NOT-logged-in: "Sign in / up" or "Sign in" is visible
            const allEls = [...document.querySelectorAll('a, button')];
            const signInBtn = allEls.find(el => {
                const t = (el.textContent || '').trim().toLowerCase();
                return t === 'sign in / up' || t === 'sign in';
            });
            if (signInBtn) {
                const r = signInBtn.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) return 'not_logged_in';
            }

            // Definitive LOGGED-IN signals: member name, miles balance, or avatar
            const ht = header.innerText || '';
            if (ht.includes('Welcome,')) return 'logged_in';
            // Header contains a member-area element (bell icon, avatar, account button)
            const hasMemberEl = !!header.querySelector(
                '[class*="member" i], [class*="avatar" i], [class*="account" i], ' +
                '[aria-label*="member" i], [aria-label*="account" i], [aria-label*="profile" i]'
            );
            if (hasMemberEl) return 'logged_in';

            // Header has interactive links but no sign-in — probably logged in
            if (header.querySelectorAll('a, button').length > 3) return 'logged_in';

            return 'unknown';  // header exists but not enough info yet
        }""")
    except Exception:
        return 'unknown'


async def wait_for_login(page: Page, login_event: asyncio.Event, timeout_seconds: int = 300, session_file=None):
    """
    Auto-detect login by polling the page every 2 seconds.
    Also unblocks immediately if the user clicks the Continue button (fallback).
    """
    logger.info("Waiting for login (auto-detect, up to %ds)…", timeout_seconds)
    deadline = asyncio.get_event_loop().time() + timeout_seconds

    while asyncio.get_event_loop().time() < deadline:
        # Unblock immediately if user clicked Continue
        if login_event.is_set():
            logger.info("Login confirmed via Continue button")
            return True
        # Auto-detect: poll for logged-in state
        if await is_logged_in(page) == 'logged_in':
            logger.info("Login auto-detected — proceeding")
            login_event.set()
            try:
                _sf = session_file or SESSION_FILE
                await page.context.storage_state(path=str(_sf))
                logger.info("wait_for_login: session state saved to %s", _sf.name)
            except Exception as _e:
                logger.warning("wait_for_login: could not save session state: %s", _e)
            return True
        await asyncio.sleep(2)

    logger.warning("Login timed out after %ds", timeout_seconds)
    return False


async def auto_login(page: Page, queue=None, otp_event=None, otp_holder=None) -> bool:
    """
    Attempt automated login using credentials from the .env file.
    Returns True if login succeeded, False if credentials are missing or login failed.

    Cathay's login flow:
      1. Click "Sign in / up" (or similar) in the top nav
      2. Enter phone number (country code may be pre-filled as +852)
      3. Click Next
      4. Enter password
      5. Click Sign in
    """
    phone_full = os.environ.get("CATHAY_PHONE", "").strip()
    password = os.environ.get("CATHAY_PASSWORD", "").strip()
    if not phone_full or not password:
        logger.info("auto_login: no credentials in .env — skipping")
        return False

    # Strip the +852 prefix — Cathay's phone field expects the local number only
    phone_local = phone_full.lstrip("+")
    if phone_local.startswith("852"):
        phone_local = phone_local[3:]  # "63851430"

    logger.info("auto_login: attempting login")
    try:
        # --- Step 1: click the Sign in button ---
        sign_in_clicked = False
        for sel in [
            'a:has-text("Sign in")',
            'button:has-text("Sign in")',
            '[href*="sign-in"]',
            '[href*="login"]',
            'text="Sign in / up"',
            'text="Sign in"',
            '[aria-label*="sign in" i]',
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                    await btn.click()
                    sign_in_clicked = True
                    logger.info("auto_login: clicked sign-in via %s", sel)
                    break
            except Exception:
                continue

        if not sign_in_clicked:
            logger.warning("auto_login: could not find Sign in button")
            await screenshot(page, "login_FAIL_no_button")
            return False

        await asyncio.sleep(2.0)
        await screenshot(page, "login_01_modal")

        # --- Step 2: fill phone number ---
        # Cathay may pre-select "+852" country code, so try the local number first.
        # Fall back to the full number if that doesn't work.
        phone_filled = False
        for sel in [
            'input[type="tel"]',
            'input[placeholder*="phone" i]',
            'input[placeholder*="mobile" i]',
            'input[placeholder*="number" i]',
            'input[name*="phone" i]',
            'input[name*="mobile" i]',
            'input[autocomplete*="tel" i]',
        ]:
            field = page.locator(sel).first
            try:
                if await field.count() > 0 and await field.is_visible(timeout=2000):
                    await field.click()
                    await field.fill(phone_local)
                    logger.info("auto_login: filled phone (%s) in %s", phone_local, sel)
                    phone_filled = True
                    break
            except Exception:
                continue

        if not phone_filled:
            logger.warning("auto_login: could not find phone input")
            await screenshot(page, "login_FAIL_no_phone")
            return False

        await asyncio.sleep(0.5)

        # --- Step 3: click Next / Continue ---
        for btn_text in ["Next", "Continue", "Proceed", "Sign in"]:
            btn = page.locator(f'button:has-text("{btn_text}")').first
            try:
                if await btn.count() > 0 and await btn.is_visible(timeout=1500):
                    await btn.click()
                    logger.info("auto_login: clicked '%s' after phone", btn_text)
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

        await screenshot(page, "login_02_password")

        # --- Step 4: fill password ---
        password_filled = False
        for sel in [
            'input[type="password"]',
            'input[placeholder*="password" i]',
            'input[name*="password" i]',
            'input[autocomplete="current-password"]',
        ]:
            field = page.locator(sel).first
            try:
                if await field.count() > 0 and await field.is_visible(timeout=3000):
                    await field.click()
                    await field.fill(password)
                    logger.info("auto_login: filled password in %s", sel)
                    password_filled = True
                    break
            except Exception:
                continue

        if not password_filled:
            logger.warning("auto_login: could not find password input")
            await screenshot(page, "login_FAIL_no_password")
            return False

        await asyncio.sleep(0.5)

        # --- Step 5: submit ---
        for btn_text in ["Sign in", "Log in", "Login", "Submit", "Continue"]:
            btn = page.locator(f'button:has-text("{btn_text}")').first
            try:
                if await btn.count() > 0 and await btn.is_visible(timeout=1500):
                    await btn.click()
                    logger.info("auto_login: submitted via '%s'", btn_text)
                    break
            except Exception:
                continue

        # Wait for the post-login redirect to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            await asyncio.sleep(5)

        await screenshot(page, "login_03_post_submit")

        # --- OTP 2FA detection ---
        if queue is not None and otp_event is not None and await is_logged_in(page) != 'logged_in':
            otp_page = await page.evaluate("""() => {
                const bodyText = document.body.innerText || '';
                const hasHeading = /verification code/i.test(bodyText) || /one.time/i.test(bodyText) || /enter.*code/i.test(bodyText);
                const singleInputs = [...document.querySelectorAll('input[type="text"],input[type="tel"],input[type="number"]')]
                    .filter(el => {
                        const maxlen = el.getAttribute('maxlength');
                        return maxlen && parseInt(maxlen) === 1 && el.getBoundingClientRect().width > 0;
                    });
                return hasHeading || singleInputs.length >= 4;
            }""")

            if otp_page:
                logger.info("auto_login: OTP page detected — requesting code from user")
                await screenshot(page, "login_04_otp_page")
                otp_event.clear()
                if otp_holder is not None:
                    otp_holder.clear()
                await queue.put({
                    "type": "otp",
                    "message": "Cathay sent a 6-digit verification code. Check your phone or email and enter it in the popup.",
                })
                try:
                    await asyncio.wait_for(otp_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("auto_login: OTP timed out after 120s")
                    return False

                code = (otp_holder[0] if otp_holder else "").strip()
                logger.info("auto_login: received OTP code (%d chars)", len(code))

                if len(code) == 6 and code.isdigit():
                    # Fill individual 1-char input boxes (React-friendly: set via nativeInput setter)
                    filled = await page.evaluate("""(code) => {
                        const inputs = [...document.querySelectorAll('input[type="text"],input[type="tel"],input[type="number"]')]
                            .filter(el => {
                                const maxlen = el.getAttribute('maxlength');
                                return maxlen && parseInt(maxlen) === 1 && el.getBoundingClientRect().width > 0;
                            }).slice(0, 6);
                        if (inputs.length < 6) return false;
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        inputs.forEach((inp, i) => {
                            inp.focus();
                            nativeSetter.call(inp, code[i]);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                        });
                        return true;
                    }""", code)

                    if not filled:
                        # Fallback: single OTP input field
                        for sel in ['input[autocomplete*="one-time"]', 'input[name*="otp" i]', 'input[name*="code" i]']:
                            field = page.locator(sel).first
                            if await field.count() > 0:
                                await field.fill(code)
                                break

                    await asyncio.sleep(0.5)
                    await screenshot(page, "login_04_otp_filled")

                    # Submit OTP form
                    for btn_text in ["Sign in", "Verify", "Submit", "Confirm", "Continue"]:
                        btn = page.locator(f'button:has-text("{btn_text}")').first
                        try:
                            if await btn.count() > 0 and await btn.is_visible(timeout=1500):
                                await btn.click()
                                logger.info("auto_login: submitted OTP via '%s' button", btn_text)
                                break
                        except Exception:
                            continue

                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        await asyncio.sleep(5)

                    await screenshot(page, "login_05_post_otp")

        if await is_logged_in(page) == 'logged_in':
            logger.info("auto_login: succeeded")
            try:
                await page.context.storage_state(path=str(SESSION_FILE))
                logger.info("auto_login: session state saved to %s", SESSION_FILE.name)
            except Exception as _e:
                logger.warning("auto_login: could not save session state: %s", _e)
            return True

        logger.warning("auto_login: submitted but not logged in — may need manual login")
        await screenshot(page, "login_FAIL_no_welcome")
        return False

    except Exception as exc:
        logger.warning("auto_login failed: %s", exc)
        await screenshot(page, "login_FAIL")
        return False


# ---------------------------------------------------------------------------
# Form interaction helpers
# ---------------------------------------------------------------------------

DEBUG_SCREENSHOTS = True  # set False to stop saving screenshots
SCREENSHOT_DIR = Path(__file__).parent / "debug_screenshots"


async def screenshot(page: Page, name: str):
    """Save a debug screenshot (only when DEBUG_SCREENSHOTS is True)."""
    if not DEBUG_SCREENSHOTS:
        return
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(path))
        logger.info("Screenshot: %s", path)
    except Exception:
        pass


async def toggle_redeem_with_miles(page: Page):
    """Switch the booking form to award/miles mode via JavaScript DOM walk."""
    result = await page.evaluate(r"""() => {
        // 1. Try elements with role="switch" or role="checkbox" whose aria-label mentions redeem/miles
        for (const el of document.querySelectorAll('[role="switch"],[role="checkbox"],input[type="checkbox"]')) {
            const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
            if (lbl.includes('redeem') || lbl.includes('miles')) {
                const on = el.checked || el.getAttribute('aria-checked') === 'true';
                if (!on) el.click();
                return on ? 'already_on' : 'clicked_switch';
            }
        }
        // 2. Walk every text node looking for the phrase "redeem with miles"
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (node.textContent.trim().toLowerCase() !== 'redeem with miles') continue;
            // Walk up ancestors to find the first clickable element
            let el = node.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!el || el === document.body) break;
                const tag = el.tagName.toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                if (tag === 'button' || tag === 'label' || tag === 'a'
                    || role === 'switch' || role === 'checkbox' || role === 'button') {
                    el.click();
                    return 'clicked_ancestor:' + tag + '/' + role;
                }
                el = el.parentElement;
            }
            // Nothing clickable found — click the text's direct parent anyway
            node.parentElement.click();
            return 'clicked_parent:' + node.parentElement.tagName;
        }
        // 3. Last resort: dump what switches exist for diagnostics
        const info = [...document.querySelectorAll('[role="switch"],[role="checkbox"]')]
            .map(e => e.getAttribute('aria-label') + '|' + e.tagName);
        return 'not_found:' + info.join(', ');
    }""")
    logger.info("toggle_redeem_with_miles: %s", result)
    await asyncio.sleep(2)  # wait for form to re-render in miles mode


ORIGIN_PLACEHOLDERS = ["Select a departure city", "Leaving from", "Departure city"]
DEST_PLACEHOLDERS   = ["Select a destination",   "Going to",     "Destination"]


async def wait_for_booking_form(page: Page):
    """Wait until the booking widget comboboxes are fully rendered."""
    all_placeholders = ORIGIN_PLACEHOLDERS + DEST_PLACEHOLDERS
    selector = ", ".join(f'input[placeholder="{p}"]' for p in all_placeholders)
    await page.wait_for_selector(selector, state="visible", timeout=20000)
    await asyncio.sleep(0.5)


async def find_airport_input(page: Page, placeholders: list):
    """Return a Locator for the airport input matching any placeholder (visibility not required)."""
    for placeholder in placeholders:
        loc = page.locator(f'input[placeholder="{placeholder}"]')
        if await loc.count() > 0:
            return loc.first
    return page.locator(f'input[placeholder="{placeholders[0]}"]').first


async def fill_airport(page: Page, placeholder: str, airport_code: str):
    """
    Clear an airport combobox, type the IATA code, and select the matching option.
    Cathay shows options like "Tokyo Narita, (NRT)".
    `placeholder` is the canonical name; we also try known aliases automatically.
    """
    code = airport_code.upper()

    alias_map = {
        "Select a departure city": ORIGIN_PLACEHOLDERS,
        "Select a destination":    DEST_PLACEHOLDERS,
        "Leaving from":            ORIGIN_PLACEHOLDERS,
        "Going to":                DEST_PLACEHOLDERS,
    }
    candidates = alias_map.get(placeholder, [placeholder])
    inp = await find_airport_input(page, candidates)

    await inp.scroll_into_view_if_needed()
    await asyncio.sleep(0.2)

    # Click the input's parent container — handles the common pattern where a
    # "selected value" overlay sits on top of the hidden <input>, making the
    # input itself unclickable.  Clicking the wrapper re-opens the combobox.
    await inp.locator("..").click()
    await asyncio.sleep(0.4)

    # Clear any existing value using the React-native input setter so React's
    # controlled state actually resets (plain .value= assignment is ignored).
    await inp.evaluate("""el => {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, '');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.focus();
    }""")
    await asyncio.sleep(0.2)

    # Type character-by-character so autocomplete fires on each keystroke
    await page.keyboard.type(code, delay=100)

    # Wait specifically for an option containing this airport code — waiting for
    # any [role="option"] picks up stale options from the previous field (origin)
    # that are still in the DOM but hidden, causing a 15s timeout.
    specific_option = page.locator(f'[role="option"]:has-text("({code})")')
    try:
        await specific_option.first.wait_for(state="visible", timeout=15000)
    except Exception:
        logger.warning("Option (%s) didn't appear. URL: %s", code, page.url)
        raise
    await asyncio.sleep(0.2)

    # Use bounding_box() + page.mouse.click() for a real human-like click
    bbox = await specific_option.first.bounding_box()
    if bbox:
        cx = bbox['x'] + bbox['width'] / 2
        cy = bbox['y'] + bbox['height'] / 2
        logger.info("Mouse-clicking option (%s) at (%.0f, %.0f)", code, cx, cy)
        await page.mouse.click(cx, cy)
    else:
        await specific_option.first.click()

    await asyncio.sleep(0.5)


async def set_trip_type(page: Page, trip_type: str):
    """Switch Trip type to 'One way' or 'Return'."""
    label = "One way" if trip_type == "one_way" else "Return"

    # Find the trip-type control — try multiple selector strategies
    clicked = False
    for selector in [
        'select[aria-label*="Trip" i]',                            # native select
        '[role="combobox"][aria-label*="Trip" i]',                  # aria combobox
        'button[aria-label*="Trip" i]',                            # button variant
    ]:
        els = page.locator(selector)
        if await els.count() > 0 and await els.first.is_visible():
            tag = await els.first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await els.first.select_option(label=label)
                clicked = True
                break
            await els.first.click()
            clicked = True
            break

    if not clicked:
        # Fallback: find any visible combobox-like element near "Trip type" text
        trip_label = page.locator('text="Trip type"')
        if await trip_label.count() > 0:
            # Click the parent container which should be the trigger
            await trip_label.first.locator("..").click()
            clicked = True

    if not clicked:
        logger.warning("Could not find Trip type control — skipping (will use page default)")
        return

    await asyncio.sleep(0.5)

    # Select the option (may be in a listbox or native dropdown)
    option = page.locator(f'[role="option"]:has-text("{label}"), option:has-text("{label}")')
    if await option.count() > 0:
        await option.first.click()
    await asyncio.sleep(0.5)


async def set_cabin_class(page: Page, cabin_class: str):
    """Open the cabin+passengers panel and select the desired cabin class."""
    label = CABIN_LABELS[cabin_class]
    if label == "Economy":
        return  # Economy is the default

    await screenshot(page, f"cabin_00_before_{cabin_class}")

    # Step 1: Open the panel (button shows "Economy, 1 Adult")
    clicked = False
    for selector in [
        'select[aria-label*="Cabin" i]',
        '[role="combobox"][aria-label*="Cabin" i]',
        '[role="combobox"]:has-text("Adult")',
        'button:has-text("Adult")',
        '[aria-label*="cabin" i]',
        '[aria-label*="passenger" i]',
    ]:
        els = page.locator(selector)
        if await els.count() > 0 and await els.first.is_visible():
            tag = await els.first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await els.first.select_option(label=label)
                logger.info("set_cabin_class: native select, chose %s", label)
                return
            await els.first.click()
            clicked = True
            logger.info("set_cabin_class: opened panel via '%s'", selector)
            break

    if not clicked:
        logger.warning("set_cabin_class: could not find panel button — skipping")
        return

    await asyncio.sleep(1.5)  # wait for panel to render
    await screenshot(page, f"cabin_01_panel_open_{cabin_class}")

    # Step 2: Select the cabin class WITHIN the panel.
    # The panel is a floating overlay. All interactions MUST be scoped to the panel's
    # bounding box — otherwise "Economy", "Business" etc. match promo links in the
    # background (e.g. "Economy from HKG $1,767") and cause page navigations.
    #
    # Approach:
    #   A) Get the panel's bounding box by finding its title text
    #   B) Within that box: open the class sub-dropdown (click the "Economy" trigger)
    #   C) Within that box: click the target cabin option
    #   D) If no panel found, fall back to native <select> only (safe, won't navigate)

    selected = False

    panel_box = await page.evaluate("""() => {
        // Find the panel by its title. The title contains "cabin class" but is short
        // (< 200 chars) to avoid matching the entire page body.
        let panel = null;
        for (const el of document.querySelectorAll('*')) {
            const txt = (el.textContent || '').trim();
            if (txt.length < 200 && txt.length > 10 &&
                (txt.toLowerCase().includes('cabin class') || txt.toLowerCase().includes('select cabin'))) {
                if (el.offsetParent) {
                    panel = el;
                    break;
                }
            }
        }
        if (!panel) return null;
        // Walk up to find the containing box that's big enough to be the whole panel
        let el = panel;
        for (let i = 0; i < 8; i++) {
            const r = el.getBoundingClientRect();
            if (r.width > 150 && r.height > 80) {
                return {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
            }
            if (!el.parentElement || el.parentElement === document.body) break;
            el = el.parentElement;
        }
        return null;
    }""")
    logger.info("set_cabin_class: panel box → %s", panel_box)

    if panel_box:
        # 2a: Click the target cabin option.
        # The Class sub-dropdown may already be expanded (all options visible) OR collapsed
        # (showing only the current "Economy" label). Check for the target option first —
        # if visible, click it directly. Only click the Economy trigger to expand the list
        # if the target option isn't already showing.
        sub_result = await page.evaluate("""({box, label}) => {
            const inBox = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0
                    && r.left >= box.left - 30 && r.right <= box.right + 30
                    && r.top >= box.top - 30 && r.bottom <= box.bottom + 200;
            };

            const findOpt = () => {
                const opts = [...document.querySelectorAll('*')].filter(el => {
                    if (!inBox(el)) return false;
                    if (el.querySelectorAll('*').length > 10) return false;
                    const txt = (el.innerText || '').trim();
                    return txt === label || txt.startsWith(label);
                });
                opts.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
                return opts[0] || null;
            };

            // Step A: if target already visible, click it immediately
            const direct = findOpt();
            if (direct) {
                direct.click();
                return 'ok:direct/' + (direct.innerText || '').trim().substring(0, 30);
            }

            // Step B: target not visible — open the Class sub-dropdown by clicking its trigger.
            // The trigger shows the currently selected class (e.g. "Economy"). Prefer the
            // LARGEST matching element (the trigger row) to avoid toggling the closed dropdown
            // via the small label text.
            const subTriggers = [...document.querySelectorAll('*')].filter(el => {
                if (!inBox(el)) return false;
                if (el.querySelectorAll('*').length > 6) return false;
                const txt = (el.innerText || '').trim();
                return txt === 'Economy' || txt === 'Business' ||
                       txt === 'Premium Economy' || txt === 'First';
            });
            if (subTriggers.length > 0) {
                subTriggers.sort((a, b) => {
                    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                    return (rb.width * rb.height) - (ra.width * ra.height); // largest first
                });
                subTriggers[0].click();
            }

            // Step C: wait for sub-dropdown animation then click target
            return new Promise(resolve => {
                setTimeout(() => {
                    const opt = findOpt();
                    if (!opt) return resolve('not_found');
                    opt.click();
                    resolve('ok:' + opt.tagName + '/' + (opt.innerText || '').trim().substring(0, 30));
                }, 400);
            });
        }""", {"box": panel_box, "label": label})
        logger.info("set_cabin_class: panel-scoped select → %s", sub_result)
        if sub_result.startswith('ok:'):
            selected = True

    # 2b: Playwright locator fallback — the JS approach may have expanded the dropdown
    # but timed out before clicking. The options are now visible; use Playwright's
    # native click (dispatches real mouse events, works with React synthetic events).
    if not selected:
        for opt_sel in [
            f'[role="option"]:has-text("{label}")',
            f'li:has-text("{label}")',
            f'[class*="option"]:has-text("{label}")',
            f'[class*="item"]:has-text("{label}")',
        ]:
            opt_loc = page.locator(opt_sel)
            if await opt_loc.count() > 0:
                try:
                    await opt_loc.first.click(timeout=2000)
                    logger.info("set_cabin_class: Playwright click → %s via '%s'", label, opt_sel)
                    selected = True
                    break
                except Exception as e:
                    logger.debug("set_cabin_class: Playwright click '%s' failed: %s", opt_sel, e)

    # 2c: Native <select> last-resort
    if not selected:
        sels = page.locator('select')
        for i in range(await sels.count()):
            try:
                await sels.nth(i).select_option(label=label)
                logger.info("set_cabin_class: native select #%d succeeded", i)
                selected = True
                break
            except Exception as e:
                logger.debug("set_cabin_class: select #%d failed: %s", i, e)

    if not selected:
        logger.warning("set_cabin_class: could not select cabin '%s' — proceeding anyway", label)

    await asyncio.sleep(0.3)

    await asyncio.sleep(0.5)
    await screenshot(page, f"cabin_02_after_select_{cabin_class}")

    # Step 3: Close the panel
    done_closed = False
    for done_sel in ['button:has-text("Done")', 'button:has-text("Apply")', 'button:has-text("Confirm")']:
        btn = page.locator(done_sel).first
        if await btn.count() > 0:
            try:
                await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(force=True, timeout=3000)
                logger.info("set_cabin_class: panel closed via '%s'", done_sel)
                done_closed = True
                break
            except Exception:
                pass
    if not done_closed:
        await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)


async def pick_calendar_date(page: Page, field_hint: str, target_date: date, calendar_already_open: bool = False):
    """
    Open a date picker and click the target date.

    calendar_already_open=True skips the trigger click — used for the return date
    in a return search where Cathay uses a range picker: clicking the departure date
    keeps the calendar open in end-date selection mode, so clicking the "Returning"
    trigger would disrupt the state rather than help.
    """
    if not calendar_already_open:
        trigger = page.locator(
            f'button:has-text("{field_hint}"), [aria-label*="{field_hint}"]'
        ).first
        await trigger.scroll_into_view_if_needed()
        await asyncio.sleep(0.2)
        await trigger.click()
        await asyncio.sleep(0.8)
    else:
        # Calendar already open in range-selection mode.
        # Wait for the calendar to finish transitioning from departure-mode to end-date-mode.
        # 0.5s was too short — the range picker animates and date cell positions shift.
        await asyncio.sleep(2.0)

    day = target_date.day
    month_label = target_date.strftime("%B %Y")  # e.g. "May 2026"

    for attempt in range(24):
        result = await page.evaluate(f"""() => {{
            const day = {day};
            const monthYear = '{month_label}';

            // Each Cathay date cell shows a number + a seat-availability icon below it.
            // The cell's total textContent is NOT just the number — it includes the icon's
            // text/title content. So we find LEAF NODES (no children) whose text is exactly
            // the day number, then walk UP to the actual clickable cell.

            // Only return true when a small calendar header element shows exactly monthYear
            // (avoids false positive from monthYear appearing in the departure field text)
            const monthVisible = [...document.querySelectorAll('*')].some(el => {{
                if (el.children.length > 3) return false;
                const r = el.getBoundingClientRect();
                if (r.width < 50 || r.height < 10) return false;
                return (el.textContent || '').trim() === monthYear;
            }});

            // STEP 1: find all leaf nodes with text exactly matching the day number
            const leafs = [...document.querySelectorAll('*')].filter(el => {{
                if (el.children.length > 0) return false;   // not a leaf
                if (el.textContent.trim() !== String(day)) return false;
                const bbox = el.getBoundingClientRect();
                return bbox.width > 5 && bbox.height > 5;  // must be visible
            }});

            if (leafs.length === 0) {{
                // Extended diagnostic: what ARE the text nodes near the calendar?
                const calendarTexts = [...document.querySelectorAll('*')]
                    .filter(el => {{
                        if (el.children.length > 0) return false;
                        const t = el.textContent.trim();
                        return /^\d{{1,2}}$/.test(t);  // any 1-2 digit number leaf
                    }})
                    .map(el => el.tagName + '(' + el.textContent.trim() + ')[' + (el.className || '') + ']')
                    .slice(0, 20);
                return {{debug: 'no_leafs', monthVisible, calendarTexts: calendarTexts.join(', ')}};
            }}

            // STEP 2: for each leaf, walk UP to find the clickable cell parent
            // Prefer the cell whose ancestor contains the target monthYear (handles 2-month calendars)
            const CLICKABLE = new Set(['button', 'td', 'a']);
            const CLICKABLE_ROLES = new Set(['button', 'gridcell', 'option']);

            function findClickableParent(leaf) {{
                let el = leaf.parentElement;
                for (let i = 0; i < 8; i++) {{
                    if (!el || el === document.body) break;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (CLICKABLE.has(tag) || CLICKABLE_ROLES.has(role)) return el;
                    el = el.parentElement;
                }}
                return leaf;  // fall back to the leaf itself
            }}

            // Build list of (leaf, clickableCell, hasRealParent) triples.
            // hasRealParent=true means findClickableParent found a real button/td/a.
            // Range-display "24" nodes have no clickable parent and fall back to the leaf —
            // those are deprioritised so we always prefer actual date grid cells.
            const pairs = leafs.map(leaf => {{
                const cell = findClickableParent(leaf);
                return {{ leaf, cell, hasRealParent: cell !== leaf }};
            }});

            // Filter to cells that are actually visible
            const visible = pairs.filter(p => {{
                const r = p.cell.getBoundingClientRect();
                return r.width > 10 && r.height > 10;
            }});

            if (visible.length === 0) {{
                return {{debug: 'leafs_found_but_cells_not_visible', monthVisible, count: leafs.length}};
            }}

            // STEP 3: disambiguate by finding the one inside the target month's section
            function inTargetMonth(cell) {{
                let el = cell.parentElement;
                for (let i = 0; i < 15; i++) {{
                    if (!el || el === document.body) break;
                    if ((el.textContent || '').includes(monthYear)) return true;
                    el = el.parentElement;
                }}
                return false;
            }}

            // Rank candidates by HOW CLOSE their first ancestor containing monthYear is.
            // A cell in the correct month section has a shallower (smaller depth) ancestor
            // with monthYear than a cell in an adjacent month whose only qualifying ancestor
            // is the outer calendar container (which shows both months' text).
            function depthToMonth(cell) {{
                let el = cell.parentElement, d = 0;
                while (el && el !== document.body) {{
                    if ((el.textContent || '').includes(monthYear)) return d;
                    el = el.parentElement;
                    d++;
                }}
                return 999;
            }}

            const withDepth = visible.map(p => ({{...p, depth: depthToMonth(p.cell)}}));
            const reachable = withDepth.filter(p => p.depth < 999);

            if (reachable.length === 0) {{
                return {{debug: 'day_not_in_target_month', monthVisible, count: visible.length}};
            }}

            // Sort: real clickable parents first (actual grid cells), then by depth ascending.
            // This prevents range-display text nodes (no real parent) from beating the actual cell.
            reachable.sort((a, b) => {{
                if (a.hasRealParent !== b.hasRealParent) return a.hasRealParent ? -1 : 1;
                return a.depth - b.depth;
            }});
            const chosen = reachable[0].cell;
            chosen.scrollIntoView({{block: 'center'}});
            chosen.click();

            const r = chosen.getBoundingClientRect();
            return {{
                x: r.left + r.width / 2,
                y: r.top + r.height / 2,
                via: 'month-disambig',
                totalLeafs: leafs.length,
                inMonth: reachable.length,
                chosenDepth: reachable[0].depth,
            }};
        }}""")

        if result and 'x' in result:
            logger.info("Clicking calendar date %s (attempt %d, via=%s, leafs=%s, inMonth=%s, depth=%s)",
                        target_date, attempt, result.get('via'), result.get('totalLeafs'), result.get('inMonth'), result.get('chosenDepth'))
            # JS already fired a click on the element.
            # Wait for the calendar to process the click before checking result.
            # Do NOT fire a backup mouse.click at stale (x,y) coordinates — after the
            # JS click the calendar may scroll/shift, causing the backup to land on the
            # wrong date (e.g. Jun 17 click shows range preview, layout shifts, backup
            # lands on Jun 10 instead).
            await asyncio.sleep(0.8)

            # Check if the departure date placeholder is still showing.
            # Use .first to avoid strict-mode crash (there are multiple date fields on page).
            still_empty = await page.locator('.c-date-input-text-ellipsis').first.is_visible()
            if not still_empty:
                logger.info("Calendar date %s confirmed selected", target_date)
            else:
                logger.warning("Calendar: click fired but placeholder still visible — proceeding anyway")
            # Always return here regardless — never let the loop continue after a click
            # attempt or it will advance through every month clicking the same day number.
            return

        debug = result or {}
        logger.info("Calendar attempt %d: %s | monthVisible=%s | calendarTexts=%s",
                    attempt, debug.get('debug'), debug.get('monthVisible'), debug.get('calendarTexts', debug.get('leafs')))

        # Only navigate to next month if the target month genuinely isn't shown yet
        if debug.get('monthVisible'):
            logger.warning("Calendar: month %s visible but day %d still not clickable — check selectors. Debug: %s",
                           month_label, day, debug)
            await screenshot(page, f"CAL_DEBUG_{target_date}")
            break

        next_clicked = False
        for btn in await page.locator(
            'button[aria-label="Next"], button[aria-label*="next month" i], '
            'button[aria-label*="Next month" i]'
        ).all():
            if await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
                next_clicked = True
                break

        if not next_clicked:
            logger.warning("Calendar: no visible Next button on attempt %d", attempt)
            break

    logger.warning("Could not click calendar date %s", target_date)


# ---------------------------------------------------------------------------
# Results extraction
# ---------------------------------------------------------------------------

async def extract_results(
    page: Page,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: Optional[date],
    cabin_class: str,
) -> FlightResult:
    """
    Parse the results page after a search.
    NOTE: Selectors here may need calibration on first run — the scraper will
    print the page URL and title to help debug if nothing is extracted.
    """
    base = FlightResult(
        origin=origin,
        destination=destination,
        departure_date=str(departure_date),
        return_date=str(return_date) if return_date else None,
        cabin_class=cabin_class,
    )

    logger.info("Results page URL: %s", page.url)
    await screenshot(page, f"07_results_{destination}_{departure_date}")

    CABIN_NAME_MAP = {
        "economy":         "Economy",
        "premium_economy": "Premium Economy",
        "business":        "Business",
        "first":           "First",
    }
    cabin_label = CABIN_NAME_MAP.get(cabin_class, "Economy")

    # Parse per-flight cards from body.innerText.
    # Each card is delimited by "Flight details" link text.
    # We pass cabin_label as an argument to avoid f-string escaping in the JS.
    data = await page.evaluate("""(cabinLabel) => {
        const bodyText = document.body.innerText || '';

        if (/no flights available|sorry.*no.*flight/i.test(bodyText)) {
            return {flights: [], pageAvailable: false};
        }

        const sections = bodyText.split('Flight details');
        if (sections.length <= 1) {
            // No flight cards found — try cabin tab "FROM X,XXX" fallback
            const cabinIdx = bodyText.indexOf(cabinLabel);
            if (cabinIdx !== -1) {
                const snippet = bodyText.slice(cabinIdx, cabinIdx + 100);
                const m = snippet.match(/FROM[^\\d]*(\\d[\\d,]+)/);
                if (m) {
                    const miles = parseInt(m[1].replace(/,/g, ''));
                    return {flights: [{flight_numbers: [], departure_time: null,
                                       arrival_time: null, duration: null,
                                       stops: null, miles, available: true}],
                            pageAvailable: true};
                }
            }
            return {flights: [], pageAvailable: null};
        }

        const flights = [];
        for (let i = 1; i < sections.length; i++) {
            const prevSec = sections[i - 1];
            const thisSec = sections[i];

            // Flight numbers sit at the end of the previous section
            const prevLines = prevSec.split('\\n').map(l => l.trim()).filter(Boolean);
            const lookback  = prevLines.slice(-5).join(' ');
            const flightNums = [...lookback.matchAll(/\\b([A-Z]{2}\\d{2,4})\\b/g)].map(m => m[1]);

            // Times HH:MM
            const times  = thisSec.match(/\\b(\\d{2}:\\d{2})\\b/g) || [];
            const depTime = times[0] || null;
            const arrTime = times[1] || null;

            // Duration e.g. "3h 0m"
            const durM   = thisSec.match(/(\\d+h\\s*\\d+m)/);
            const duration = durM ? durM[1].replace(/\\s+/, ' ') : null;

            // Stops e.g. "1 stop, DOH"
            const stopM = thisSec.match(/(\\d+\\s*stop[^\\n]*)/i);
            const stops = stopM ? stopM[1].trim() : 'Direct';

            // Availability
            const available = !/no redemption seats/i.test(thisSec);

            // Miles: Asia Miles A-icon renders as letter "A" in innerText → "A9,000"
            let miles = null;
            for (const mm of thisSec.matchAll(/\\bA(\\d{1,3}(?:,\\d{3})+)/g)) {
                const n = parseInt(mm[1].replace(/,/g, ''));
                if (n >= 1000 && n < 1000000) { miles = n; break; }
            }
            if (miles === null) {
                // Fallback: comma-formatted number (e.g. "9,000")
                for (const mm of thisSec.matchAll(/\\b(\\d{1,3}(?:,\\d{3})+)/g)) {
                    const n = parseInt(mm[1].replace(/,/g, ''));
                    if (n >= 1000 && n < 1000000) { miles = n; break; }
                }
            }
            if (miles === null) {
                // Last fallback: plain 4-6 digit number (e.g. "9000" without comma)
                for (const mm of thisSec.matchAll(/\\b(\\d{4,6})\\b/g)) {
                    const n = parseInt(mm[1]);
                    if (n >= 1000 && n < 1000000) { miles = n; break; }
                }
            }

            if (flightNums.length > 0 || depTime) {
                flights.push({flight_numbers: flightNums, departure_time: depTime,
                              arrival_time: arrTime, duration, stops, miles, available});
            }
        }

        return {flights, pageAvailable: flights.some(f => f.available && f.miles !== null)};
    }""", cabin_label)

    logger.info("extract_results: %d flight(s) found, pageAvailable=%s",
                len(data.get('flights', [])), data.get('pageAvailable'))

    raw_flights = data.get('flights', [])
    if not raw_flights:
        base.available = False
        return base

    flight_options = [
        FlightOption(
            flight_numbers=f.get('flight_numbers') or [],
            departure_time=f.get('departure_time'),
            arrival_time=f.get('arrival_time'),
            duration=f.get('duration'),
            stops=f.get('stops'),
            miles=f.get('miles'),
            available=f.get('available', False),
        )
        for f in raw_flights
    ]

    base.flights = flight_options
    available_flights = [f for f in flight_options if f.available and f.miles]
    base.available = bool(available_flights)

    if available_flights:
        cheapest = min(available_flights, key=lambda f: f.miles)
        base.outbound = cheapest
        base.total_miles = cheapest.miles

    return base


# ---------------------------------------------------------------------------
# Inbound (return-leg) scraper
# ---------------------------------------------------------------------------

# Flight-card parsing JS shared between outbound and inbound extraction.
# Splits body.innerText on "Flight details" landmarks and parses each card.
_PARSE_FLIGHTS_JS = r"""() => {
    const bodyText = document.body.innerText || '';
    const sections = bodyText.split('Flight details');
    if (sections.length <= 1) return [];
    const flights = [];
    for (let i = 1; i < sections.length; i++) {
        const prevSec = sections[i - 1];
        const thisSec = sections[i];
        const prevLines = prevSec.split('\n').map(l => l.trim()).filter(Boolean);
        const lookback  = prevLines.slice(-5).join(' ');
        const flightNums = [...lookback.matchAll(/\b([A-Z]{2}\d{2,4})\b/g)].map(m => m[1]);
        const times   = thisSec.match(/\b(\d{2}:\d{2})\b/g) || [];
        const depTime = times[0] || null;
        const arrTime = times[1] || null;
        const durM    = thisSec.match(/(\d+h\s*\d+m)/);
        const duration = durM ? durM[1].replace(/\s+/, ' ') : null;
        const stopM   = thisSec.match(/(\d+\s*stop[^\n]*)/i);
        const stops   = stopM ? stopM[1].trim() : 'Direct';
        const available = !/no redemption seats/i.test(thisSec);
        let miles = null;
        for (const mm of thisSec.matchAll(/\bA(\d{1,3}(?:,\d{3})+)/g)) {
            const n = parseInt(mm[1].replace(/,/g, ''));
            if (n >= 1000 && n < 1000000) { miles = n; break; }
        }
        if (miles === null) {
            for (const mm of thisSec.matchAll(/\b(\d{1,3}(?:,\d{3})+)/g)) {
                const n = parseInt(mm[1].replace(/,/g, ''));
                if (n >= 1000 && n < 1000000) { miles = n; break; }
            }
        }
        if (miles === null) {
            for (const mm of thisSec.matchAll(/\b(\d{4,6})\b/g)) {
                const n = parseInt(mm[1]);
                if (n >= 1000 && n < 1000000) { miles = n; break; }
            }
        }
        if (flightNums.length > 0 || depTime) {
            flights.push({flight_numbers: flightNums, departure_time: depTime,
                          arrival_time: arrTime, duration, stops, miles, available});
        }
    }
    return flights;
}"""


async def _select_outbound_and_get_inbound(
    page: Page,
    destination: str,
    outbound_miles: int,
) -> List["FlightOption"]:
    """
    Click the cheapest available outbound flight card on the results page,
    wait for the inbound (return-leg) results to render, and parse them.
    Returns a list of FlightOption for the inbound leg.
    """
    from models import FlightOption  # avoid circular at module level
    try:
        await screenshot(page, f"08_pre_inbound_select_{destination}")

        # Use the "Flight details" anchor as a structural landmark for each flight card.
        # Walk up from it to find the card container, skip unavailable cards
        # ("no redemption seats"), then click the price panel (leaf element with
        # miles text, e.g. "A9,000") inside the first available card.
        click_result = await page.evaluate(r"""() => {
            // Use TreeWalker on raw text nodes — avoids innerText icon/arrow issues.
            // NOTE: the "A" in "A9,000" is a CSS ::before pseudo-element, so
            // card.innerText shows "9,000" not "A9,000". All number checks omit the A prefix.
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const fdParents = [];
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.trim() === 'Flight details') {
                    // Only include visible elements (filters out hidden/template copies)
                    const rect = node.parentElement.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        fdParents.push(node.parentElement);
                    }
                }
            }

            if (fdParents.length === 0) {
                return 'no_fd_text_nodes:body_has=' + document.body.innerText.includes('Flight details');
            }

            for (const el of fdParents) {
                let card = el.parentElement;
                for (let i = 0; i < 12; i++) {
                    if (!card || card === document.body) break;
                    const ct = card.innerText || '';
                    if (ct.length > 1500) break;
                    // Card must have times AND a miles-range number (no "A" prefix — it's CSS)
                    if (ct.match(/\d{2}:\d{2}/) && ct.match(/\d[\d,]{3,}/)) {
                        if (/no redemption seats/i.test(ct)) break;
                        // Find the price element: pure number in miles range (5000–999999)
                        const candidates = [...card.querySelectorAll('*')].filter(e => {
                            const txt = (e.innerText || '').trim();
                            if (!/^\d[\d,]+$/.test(txt)) return false;
                            const n = parseInt(txt.replace(/,/g, ''));
                            return n >= 5000 && n < 1000000
                                && e.getBoundingClientRect().height > 5;
                        });
                        if (candidates.length > 0) {
                            candidates[0].click();
                            return 'price_el:' + candidates[0].innerText.trim();
                        }
                        card.click();
                        return 'card_click';
                    }
                    card = card.parentElement;
                }
            }
            const sizes = fdParents.slice(0, 2).map(el => {
                let c = el.parentElement; const lens = [];
                for (let i = 0; i < 10; i++) {
                    if (!c || c === document.body) break;
                    const ct = c.innerText || '';
                    lens.push(ct.length + (ct.match(/\d{2}:\d{2}/) ? 'T' : '') + (ct.match(/\d[\d,]{3,}/) ? 'M' : ''));
                    c = c.parentElement;
                }
                return lens.join(',');
            });
            return 'not_found:fd=' + fdParents.length + ':sizes=' + sizes.join('|');
        }""")

        logger.info("_select_outbound: click result → %s", click_result)
        # Only proceed if we actually clicked something
        if not (click_result.startswith('price_el:') or click_result == 'card_click'):
            logger.warning("_select_outbound: could not click any outbound flight (%s) — skipping inbound", click_result)
            return []

        # After selecting the outbound flight, Cathay shows a confirmation bar +
        # a sticky "Continue" button in the footer. Click it to load the inbound page.
        await asyncio.sleep(2)
        continue_result = await page.evaluate(r"""() => {
            const btns = [...document.querySelectorAll('button, a[role="button"], [role="button"]')];
            const labels = ['continue', 'confirm', 'proceed', 'next', 'select return', 'done'];
            for (const label of labels) {
                const btn = btns.find(b => (b.innerText || b.textContent || '').trim().toLowerCase().includes(label));
                if (btn && btn.getBoundingClientRect().width > 0) {
                    btn.click();
                    return 'continue:' + (btn.innerText || btn.textContent || '').trim().substring(0, 40);
                }
            }
            // Fallback: click the last large visible button (usually the CTA in sticky footer)
            const visible = btns.filter(b => {
                const r = b.getBoundingClientRect();
                return r.width > 80 && r.height > 30;
            });
            if (visible.length > 0) {
                const last = visible[visible.length - 1];
                last.click();
                return 'last_btn:' + (last.innerText || last.textContent || '').trim().substring(0, 40);
            }
            return 'no_continue_btn';
        }""")
        logger.info("_select_outbound: continue click → %s", continue_result)

        # Wait for inbound results page to render
        await asyncio.sleep(4)
        for _ in range(15):
            tlen = await page.locator('body').evaluate("el => el.innerText.trim().length")
            if tlen > 200:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(2)
        await screenshot(page, f"09_inbound_{destination}")

        raw = await page.evaluate(_PARSE_FLIGHTS_JS)
        logger.info("_select_outbound: %d inbound flight(s) found", len(raw))
        return [
            FlightOption(
                flight_numbers=f.get('flight_numbers') or [],
                departure_time=f.get('departure_time'),
                arrival_time=f.get('arrival_time'),
                duration=f.get('duration'),
                stops=f.get('stops'),
                miles=f.get('miles'),
                available=f.get('available', False),
            )
            for f in raw
        ]
    except Exception as exc:
        logger.warning("_select_outbound_and_get_inbound failed: %s", exc)
        try:
            await screenshot(page, f"FAIL_inbound_{destination}")
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Date strip navigation helpers
# ---------------------------------------------------------------------------

async def _navigate_date_strip(page: Page, target_date: date) -> bool:
    """
    Click target_date on the horizontal date strip at the top of the results page.
    The strip shows 7 days like "TUE 12 / WED 13 / FRI 15 (selected) / ...".
    Returns True if a date tab was clicked, False if the date isn't reachable.
    """
    day_num = target_date.day
    day_name = target_date.strftime("%a").upper()  # MON, TUE, WED ...

    # Scroll to top so the date strip is in the viewport
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.3)

    for attempt in range(4):  # up to 4 strip-scroll attempts before giving up
        clicked = await page.evaluate(f"""() => {{
            const day = {day_num};
            const name = '{day_name}';

            // Date strip sits in the top ~200px. Each tab is a small box with
            // the 3-letter day name + date number (e.g. "FRI\\n15").
            const candidates = [];
            for (const el of document.querySelectorAll('*')) {{
                const rect = el.getBoundingClientRect();
                if (rect.top < 0 || rect.top > 250) continue;
                if (rect.width < 20 || rect.width > 250 || rect.height < 20) continue;
                // Avoid huge containers — only leaf-ish nodes
                if (el.querySelectorAll('*').length > 20) continue;
                const txt = (el.innerText || el.textContent || '').trim().toUpperCase().replace(/\\s+/g, ' ');
                if (txt.includes(String(day)) && txt.includes(name)) {{
                    candidates.push(el);
                }}
            }}
            if (candidates.length === 0) return 'not_found';

            // Prefer the element with the smallest child count (most specific)
            candidates.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
            candidates[0].click();
            return 'ok:' + (candidates[0].innerText || '').trim().replace(/\\n/g, '|').substring(0, 20);
        }}""")

        if clicked.startswith("ok:"):
            logger.info("_navigate_date_strip: %s clicked → %s", target_date, clicked)
            return True

        # Date not in strip window — try clicking the right/forward arrow
        arrow = await page.evaluate("""() => {
            // Find arrow buttons in the date strip area (top 150px, narrow width)
            const strip_buttons = [...document.querySelectorAll(
                'button, a, [role="button"], [tabindex]'
            )].filter(el => {
                const r = el.getBoundingClientRect();
                return r.top >= 0 && r.top < 150 && r.width > 0 && r.width < 80 && r.height > 0;
            });

            // Prefer right/next — look for text, class, or aria-label cues
            const rightBtns = strip_buttons.filter(el => {
                const txt = (el.textContent || '').trim().toLowerCase();
                const cls = (el.className || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                return txt.includes('>') || txt.includes('›') || txt.includes('next') ||
                       cls.includes('next') || cls.includes('right') || cls.includes('forward') ||
                       aria.includes('next') || aria.includes('forward') || aria.includes('right');
            });

            if (rightBtns.length > 0) {
                rightBtns[0].click();
                return 'right:' + rightBtns[0].textContent.trim().substring(0, 10);
            }

            // Fallback: click the rightmost narrow button in the strip area
            if (strip_buttons.length > 0) {
                const rightmost = strip_buttons.reduce((a, b) =>
                    a.getBoundingClientRect().x > b.getBoundingClientRect().x ? a : b
                );
                rightmost.click();
                return 'rightmost:x=' + Math.round(rightmost.getBoundingClientRect().x);
            }
            return 'no_arrow';
        }""")

        logger.info("_navigate_date_strip: attempt %d, strip scroll → %s", attempt + 1, arrow)
        if "no_arrow" in arrow:
            break
        await asyncio.sleep(1.2)

    logger.info("_navigate_date_strip: %s (%s) not found in strip", target_date, day_name)
    return False


# ---------------------------------------------------------------------------
# Side-by-side calendar layout helpers
# ---------------------------------------------------------------------------

async def _is_side_by_side(page: Page) -> bool:
    """
    Returns True when Cathay shows Depart + Return calendar strips simultaneously
    (instead of the normal sequential outbound-then-inbound flight-list flow).
    """
    return await page.evaluate("""() => {
        const hasHeading = label => [...document.querySelectorAll('*')].some(el => {
            const t = (el.textContent || '').trim();
            return t.toLowerCase() === label.toLowerCase()
                && el.querySelectorAll('*').length < 5
                && el.getBoundingClientRect().width > 0;
        });
        return hasHeading('Depart') && hasHeading('Return');
    }""")


async def _navigate_side_strip_to_available(
    page: Page, side: str, preferred_date: "date"
) -> Optional["date"]:
    """
    Within the Depart or Return section of the side-by-side layout, click the first
    visible date cell that does NOT show "Not available". Prefers dates >= preferred_date.
    No arrow navigation — works with whatever is currently visible in the strip.
    """
    import calendar as _cal
    result = await page.evaluate("""({side, preferredDay}) => {
        // Find the section heading
        const heading = [...document.querySelectorAll('*')].find(el => {
            const t = (el.textContent || '').trim();
            return t.toLowerCase() === side.toLowerCase()
                && el.querySelectorAll('*').length < 5
                && el.getBoundingClientRect().width > 0;
        });
        if (!heading) return {status: 'no_heading'};

        const hr = heading.getBoundingClientRect();

        // Collect all date cells in this section.
        // Width 40-160px targets individual date columns (~90px each).
        // No children-count limit — available cells have more nested children
        // (empty flight-card placeholders) than "Not available" cells.
        const cells = [...document.querySelectorAll('*')].filter(el => {
            const r = el.getBoundingClientRect();
            if (r.top < hr.bottom - 10) return false;
            if (r.left < hr.left - 80 || r.right > hr.right + 700) return false;
            if (r.width < 40 || r.width > 160 || r.height < 40 || r.height > 130) return false;
            const t = (el.innerText || '').trim();
            return /\\b\\d{1,2}\\b/.test(t) && t.length < 100;
        });

        // Available = no "Not available" text AND contains a day number
        const available = cells.filter(el => {
            const t = (el.innerText || '').trim().toLowerCase();
            return !t.includes('not available') && /\\b\\d{1,2}\\b/.test(t);
        });

        if (available.length === 0) return {status: 'none_available', total: cells.length};

        // Prefer the first available date >= preferredDay (sorted by x position = date order)
        available.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);

        const after = available.find(el => {
            const t = el.innerText || '';
            const m = t.match(/\\b(\\d{1,2})\\b/);
            return m && parseInt(m[1]) >= preferredDay;
        });

        const target = after || available[0];
        const text = target.innerText.trim();
        target.click();
        return {status: 'clicked', text};
    }""", {"side": side, "preferredDay": preferred_date.day})

    logger.info("_navigate_side_strip_to_available [%s]: %s", side, result)

    if result.get("status") == "clicked":
        text = result.get("text", "")
        day_m = re.search(r'\b(\d{1,2})\b', text)
        month_m = re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', text, re.I)
        year_m = re.search(r'\b(20\d{2})\b', text)
        if day_m:
            month_num = (
                list(_cal.month_abbr).index(month_m.group(1).capitalize())
                if month_m else preferred_date.month
            )
            year = int(year_m.group(1)) if year_m else preferred_date.year
            try:
                from datetime import date as _date
                found = _date(year, month_num, int(day_m.group(1)))
                logger.info("_navigate_side_strip_to_available [%s]: clicked %s", side, found)
                return found
            except Exception:
                pass
        return preferred_date  # clicked something but couldn't parse date

    logger.warning("_navigate_side_strip_to_available [%s]: %s", side, result.get("status"))
    return None


async def _handle_side_by_side(
    page: Page, dep_date: "date", ret_date: Optional["date"]
) -> dict:
    """
    Check whether the currently loaded page is the side-by-side calendar layout.
    If it is, navigate any unavailable requested dates to their nearest available
    alternative in the respective strip.
    Returns {dep_used, ret_used, note}.  note is None when nothing needed navigating.
    """
    if not await _is_side_by_side(page):
        return {"dep_used": dep_date, "ret_used": ret_date, "note": None}

    logger.info("_handle_side_by_side: side-by-side layout detected")
    await screenshot(page, "sbs_detected")

    result: dict = {"dep_used": dep_date, "ret_used": ret_date, "note": None}
    notes = []

    for side, requested_date, key in [
        ("Depart", dep_date, "dep_used"),
        ("Return", ret_date, "ret_used"),
    ]:
        if requested_date is None:
            continue

        day = requested_date.day

        # Strip cells show only day-name + day-number (no month text), so match by
        # word-boundary day number only, within the section's bounding box.
        status = await page.evaluate("""({side, day}) => {
            const heading = [...document.querySelectorAll('*')].find(el => {
                const t = (el.textContent || '').trim();
                return t.toLowerCase() === side.toLowerCase()
                    && el.querySelectorAll('*').length < 5
                    && el.getBoundingClientRect().width > 0;
            });
            if (!heading) return {found: false};

            const hr = heading.getBoundingClientRect();
            const dayRe = new RegExp('\\\\b' + day + '\\\\b');
            const cells = [...document.querySelectorAll('*')].filter(el => {
                const r = el.getBoundingClientRect();
                if (r.top < hr.bottom - 10) return false;
                if (r.left < hr.left - 80 || r.right > hr.right + 700) return false;
                if (r.width < 30 || r.height < 20 || r.height > 130) return false;
                if (el.querySelectorAll('*').length > 10) return false;
                const t = (el.innerText || el.textContent || '').trim();
                return dayRe.test(t);
            });

            if (cells.length === 0) return {found: false, debug: 'no cells matched day=' + day};
            const t = (cells[0].innerText || '').toLowerCase();
            return {found: true, unavailable: t.includes('not available'), text: cells[0].innerText.trim()};
        }""", {"side": side, "day": day})

        logger.info("_handle_side_by_side [%s %s]: %s", side, requested_date, status)

        if not status.get("found") or not status.get("unavailable"):
            continue  # available or strip not found — leave as-is

        notes.append(f"{side} {requested_date} not available")
        found = await _navigate_side_strip_to_available(page, side, requested_date)
        if found:
            result[key] = found
            notes[-1] += f" — data for {found}"
        else:
            notes[-1] += " — no nearby available date found"

    result["note"] = "; ".join(notes) if notes else None
    if notes:
        await asyncio.sleep(2)
        await screenshot(page, "sbs_navigated")

    return result


async def _navigate_back_to_outbound(page: Page, outbound_url: Optional[str] = None) -> bool:
    """
    Ensure we're on the outbound results page.
    - If already on outbound: returns True immediately (no navigation needed).
    - If outbound_url given: navigates directly to that URL (most reliable for SPA).
    - Fallback: page.go_back() — unreliable after SPA navigation, kept as last resort.
    """
    try:
        is_inbound = await page.evaluate("""() => {
            const top = document.body.innerText.substring(0, 300);
            return /^\\s*Return\\b/m.test(top);
        }""")

        if not is_inbound:
            ok = await page.evaluate("""() => {
                const t = document.body.innerText;
                if (t.trim().length < 200) return false;
                return t.includes('Flight details')
                    || /\\b\\d{2}:\\d{2}\\b/.test(t)
                    || /\\bCX\\d+\\b/.test(t);
            }""")
            logger.info("_navigate_back_to_outbound: already on outbound page (ok=%s)", ok)
            return bool(ok)

        # Try direct URL navigation first — go_back() throws "Execution context was
        # destroyed" after Cathay's SPA "Continue" navigation.
        if outbound_url:
            await page.goto(outbound_url, wait_until="domcontentloaded", timeout=20000)
            # Poll up to 20s for the SPA to re-render flight cards
            for _ in range(20):
                tlen = await page.locator("body").evaluate("el => el.innerText.trim().length")
                if tlen > 200:
                    body_text = await page.locator("body").inner_text()
                    if "Flight details" in body_text:
                        break
                await asyncio.sleep(1)
            await asyncio.sleep(1)  # extra render buffer
            ok = await page.evaluate("""() => {
                const t = document.body.innerText;
                if (t.trim().length < 200) return false;
                return t.includes('Flight details')
                    || /\\b\\d{2}:\\d{2}\\b/.test(t)
                    || /\\bCX\\d+\\b/.test(t);
            }""")
            logger.info("_navigate_back_to_outbound: url_goto → %s", "ok" if ok else "failed")
            if ok:
                return True

        # Fallback: page.go_back().
        # go_back() sometimes throws "Execution context was destroyed" during SPA
        # navigation even though the browser DID go back. Catch the error and check
        # the page content anyway — if the navigation worked we can continue.
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception as go_exc:
            logger.info("_navigate_back_to_outbound: go_back threw (%s) — checking page anyway", go_exc)
            await asyncio.sleep(3)
        else:
            await asyncio.sleep(2)
        try:
            ok = await page.evaluate("""() => {
                const t = document.body.innerText;
                if (t.trim().length < 200) return false;
                return t.includes('Flight details')
                    || /\\b\\d{2}:\\d{2}\\b/.test(t)
                    || /\\bCX\\d+\\b/.test(t);
            }""")
            logger.info("_navigate_back_to_outbound: go_back → %s", "ok" if ok else "failed")
            return bool(ok)
        except Exception:
            logger.warning("_navigate_back_to_outbound: evaluate after go_back failed")
            return False
    except Exception as exc:
        logger.warning("_navigate_back_to_outbound: %s", exc)
        return False


async def _click_cabin_tab(page: Page, cabin_label: str) -> bool:
    """
    Click a cabin class tab on the results page (Economy / Premium Economy / Business / First).
    The tabs sit below the date strip — their y-position varies by browser zoom / viewport,
    so we try role=tab selectors first, then fall back to a wider geometry scan.
    Returns True if a tab was clicked.
    """
    try:
        # Scroll to top so the cabin tabs are in the viewport
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.3)

        # Primary: use Playwright role/text selectors (most reliable, not position-dependent)
        for sel in [
            f'[role="tab"]:has-text("{cabin_label}")',
            f'[role="tab"] >> text="{cabin_label}"',
            f'button:has-text("{cabin_label}")',
        ]:
            locs = page.locator(sel)
            n = await locs.count()
            for i in range(n):
                loc = locs.nth(i)
                try:
                    box = await loc.bounding_box()
                    # Must be in upper portion of page and wide enough to be a tab
                    if box and box["y"] < 700 and box["width"] > 60 and box["height"] > 15:
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        logger.info("_click_cabin_tab: %s → clicked via '%s'", cabin_label, sel)
                        return True
                except Exception:
                    continue

        # Fallback: geometry-based scan with expanded y-range (was 350, now 700)
        result = await page.evaluate(f"""() => {{
            const label = '{cabin_label}';
            const candidates = [];
            for (const el of document.querySelectorAll('*')) {{
                const rect = el.getBoundingClientRect();
                if (rect.top < 60 || rect.top > 700 || rect.width < 80 || rect.height < 20) continue;
                if (el.querySelectorAll('*').length > 15) continue;
                const txt = (el.innerText || '').trim();
                if (txt === label || txt.startsWith(label + '\\n')) {{
                    candidates.push(el);
                }}
            }}
            if (candidates.length === 0) return 'not_found';
            candidates.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
            candidates[0].click();
            return 'ok:' + (candidates[0].innerText || '').trim().replace(/\\n/g, '|').substring(0, 40);
        }}""")

        if result.startswith('ok:'):
            logger.info("_click_cabin_tab: %s → %s (geometry fallback)", cabin_label, result)
            return True
        logger.info("_click_cabin_tab: %s not found in either pass", cabin_label)
        return False
    except Exception as exc:
        logger.warning("_click_cabin_tab: %s", exc)
        return False


async def _get_visible_cabin_tabs(page: Page) -> set:
    """
    Return the set of cabin class keys ('economy', 'business', etc.) that have
    a visible tab on the current results page.  An empty set means we couldn't
    determine tab visibility — caller should not skip searches in that case.
    """
    CABIN_TAB_MAP = {
        "Economy":         "economy",
        "Premium Economy": "premium_economy",
        "Business":        "business",
        "First":           "first",
    }
    labels = await page.evaluate("""() => {
        const cabinNames = ['Economy', 'Premium Economy', 'Business', 'First'];
        const found = new Set();
        // Cathay uses large clickable div-cards for cabin selection, not standard
        // <button> or role="tab" elements. Scan all visible elements in the upper
        // portion of the page and match by text content.
        for (const el of document.querySelectorAll('*')) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 60 || rect.height < 15 || rect.top < 10 || rect.top > 700) continue;
            if (rect.width > 600) continue;  // skip full-width containers
            const txt = (el.innerText || el.textContent || '').trim();
            for (const name of cabinNames) {
                // Match exact name or name followed by whitespace/newline (e.g. "Economy\nFROM...")
                if (txt === name || txt.startsWith(name + '\\n') || txt.startsWith(name + '\\r') || txt.startsWith(name + ' ')) {
                    found.add(name);
                }
            }
        }
        return [...found];
    }""")
    result = set()
    for lbl in labels:
        key = CABIN_TAB_MAP.get(lbl)
        if key:
            result.add(key)
    if result:
        logger.info("_get_visible_cabin_tabs: %s", sorted(result))
    else:
        logger.info("_get_visible_cabin_tabs: no cabin tabs detected (raw labels=%s)", labels)
    return result


# ---------------------------------------------------------------------------
# Single-search orchestrator
# ---------------------------------------------------------------------------

async def search_one_flight(
    page: Page,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: Optional[date],
    cabin_class: str,
) -> FlightResult:
    """Run one search on Cathay's site and return the result."""
    try:
        # Navigate to the award booking engine.
        # Use "load" not "networkidle" — Cathay has persistent background requests
        # that prevent networkidle from ever firing.
        await page.goto(BOOKING_URL, wait_until="load", timeout=45000)
        await asyncio.sleep(2)  # let JS finish rendering the booking widget

        # If we got redirected to sign-in, fall back to www home
        if "sign-in" in page.url or "cathaypacific.com" not in page.url:
            logger.warning("BOOKING_URL redirected to %s — falling back to CATHAY_HOME", page.url)
            await page.goto(CATHAY_HOME, wait_until="load", timeout=45000)
            await asyncio.sleep(2)

        await dismiss_overlays(page)
        await screenshot(page, f"01_home_{destination}_{departure_date}")

        await toggle_redeem_with_miles(page)
        await wait_for_booking_form(page)
        await screenshot(page, f"02_toggled_{destination}_{departure_date}")

        trip_type = "return" if return_date else "one_way"
        await set_trip_type(page, trip_type)
        await screenshot(page, f"03_trip_type_{destination}_{departure_date}")

        await fill_airport(page, "Select a departure city", origin)
        await screenshot(page, f"04_origin_{destination}_{departure_date}")
        await fill_airport(page, "Select a destination", destination)
        await screenshot(page, f"05_airports_{destination}_{departure_date}")

        await set_cabin_class(page, cabin_class)
        await pick_calendar_date(page, "Departing", departure_date)
        if return_date:
            # Departure date is saved to the form field immediately on click, so pressing
            # Escape here only closes the picker without clearing the selection.
            # Opening a fresh picker for the return date is more reliable than assuming
            # the range picker stayed open after the departure click.
            await asyncio.sleep(1.0)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            await pick_calendar_date(page, "Returning", return_date)

        # Close the calendar with the "Done" button.
        # The button sits at the bottom of the calendar and is often partially hidden
        # by the viewport edge or the cookie banner — so use JS click which bypasses
        # overlay intercepts and visibility restrictions.
        # Click the Done button using Playwright's locator — it handles scrolling
        # and clicking atomically, avoiding the race where the page scrolls back
        # up between JS scrollIntoView and a separate mouse click.
        # force=True bypasses visibility/overlap checks (cookie banner can sit on top).
        # Close the calendar via JS click (bypasses visibility/overlap entirely).
        # scroll_into_view_if_needed was timing out because the Done button is
        # partially hidden by the cookie banner or viewport edge.
        done_clicked = await page.evaluate("""() => {
            const labels = ['done', 'apply', 'confirm'];
            const btns = [...document.querySelectorAll('button')];
            for (const label of labels) {
                const btn = btns.slice().reverse().find(b =>
                    (b.innerText || b.textContent || '').trim().toLowerCase() === label
                );
                if (btn) {
                    btn.click();
                    return 'js:' + (btn.innerText || '').trim();
                }
            }
            return null;
        }""")
        if done_clicked:
            logger.info("Calendar closed via JS click: %s", done_clicked)
            await asyncio.sleep(0.8)
        else:
            logger.warning("Done button not found — pressing Escape to close calendar")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

        await screenshot(page, f"06_dates_{destination}_{departure_date}")

        # Click the search/redeem button — text varies depending on miles mode
        search_btn = page.locator(
            'button:has-text("Redeem flights"), '
            'button:has-text("Search flights"), '
            'button:has-text("Search miles"), '
            'button.search-btn'
        ).first
        await search_btn.click(timeout=15000)

        # Wait for the results SPA to render.
        # The booking engine fetches availability via background AJAX calls,
        # so networkidle fires too early (or never). Poll for visible body text instead.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        for _ in range(30):
            text_len = await page.locator('body').evaluate("el => el.innerText.trim().length")
            if text_len > 100:
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)  # extra buffer for React to finish rendering cards

        # Handle Cathay's side-by-side calendar layout (return trips only).
        # When either the dep or ret date shows "Not available", navigate the strip
        # to the nearest available date so flight cards can be extracted.
        sbs_note = None
        actual_dep, actual_ret = departure_date, return_date
        if return_date:
            sbs = await _handle_side_by_side(page, departure_date, return_date)
            if sbs.get("note"):
                actual_dep = sbs["dep_used"]
                actual_ret = sbs["ret_used"]
                sbs_note = sbs["note"]

        result = await extract_results(
            page, origin, destination, actual_dep, actual_ret, cabin_class
        )
        if sbs_note:
            result.note = sbs_note

        # For return trips, click the cheapest available outbound flight to get inbound options
        if return_date and result.flights:
            available_out = [f for f in result.flights if f.available and f.miles]
            if available_out:
                cheapest_out = min(available_out, key=lambda f: f.miles)
                outbound_url = page.url
                inbound = await _select_outbound_and_get_inbound(page, destination, cheapest_out.miles)
                if inbound:
                    result.inbound_flights = inbound
                    available_in = [f for f in inbound if f.available and f.miles]
                    if available_in:
                        best_in = min(available_in, key=lambda f: f.miles)
                        result.inbound = best_in
                        result.total_miles = (cheapest_out.miles or 0) + (best_in.miles or 0)
                await _navigate_back_to_outbound(page, outbound_url)

        return result

    except Exception as exc:
        logger.exception("Search failed: %s→%s %s", origin, destination, departure_date)
        try:
            await screenshot(page, f"FAIL_{destination}_{departure_date}")
        except Exception:
            pass  # page may be closed; don't mask the real error
        return FlightResult(
            origin=origin,
            destination=destination,
            departure_date=str(departure_date),
            return_date=str(return_date) if return_date else None,
            cabin_class=cabin_class,
            available=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Main job runner (called as a background task from main.py)
# ---------------------------------------------------------------------------

async def run_search_job(request: SearchRequest, queue: asyncio.Queue, login_event: asyncio.Event, otp_event: asyncio.Event = None, otp_holder: list = None, search_id: str = None):
    """
    Orchestrates the full search: login check → iterate combinations → stream results.
    Puts dicts onto `queue` with a 'type' key:
        progress  – {current, total, message}
        login     – {message}   (user needs to log in manually)
        result    – FlightResult as dict
        complete  – {}
        error     – {message}
    """
    logger.info("run_search_job: starting — IS_CLOUD=%s", IS_CLOUD)
    combos = request.get_combinations()
    total = len(combos)
    logger.info("run_search_job: %d combinations to search", total)

    async with async_playwright() as pw:
        logger.info("run_search_job: launching browser (headless=%s)", IS_CLOUD)
        # Persistent context = real browser profile on disk.
        # - Cathay's site won't detect automation (no AutomationControlled flag)
        # - Login is remembered across runs (stored in PROFILE_DIR)
        session_id = getattr(request, 'session_id', None) or 'default'
        profile_dir = get_session_profile_dir(session_id)
        session_file = profile_dir / "session_state.json"
        ctx: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=IS_CLOUD,
            channel=None if IS_CLOUD else "chrome",
            viewport={"width": 1280, "height": 800},
            args=CHROMIUM_ARGS,
            ignore_https_errors=True,
        )

        # Auto-close any popup tabs opened by Cathay's tracking scripts (e.g. doubleclick.net).
        def _close_popup(popup):
            asyncio.ensure_future(popup.close())

        ctx.on("page", _close_popup)

        # Restore session cookies saved from the previous run.
        # Chromium's persistent profile only persists cookies that have an explicit
        # expiry date — Cathay's auth token is a session cookie (no Expires), so it
        # is cleared every time the browser closes. storage_state.json captures ALL
        # cookies (including session cookies) and re-injects them here before navigation.
        if session_file.exists():
            try:
                saved = json.loads(session_file.read_text())
                cookies = saved.get("cookies", [])
                if cookies:
                    await ctx.add_cookies(cookies)
                    logger.info("run_search_job: restored %d cookies from %s", len(cookies), session_file.name)
            except Exception as _e:
                logger.warning("run_search_job: could not restore cookies: %s", _e)

        # Use the first open page or create one
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if search_id:
            active_pages[search_id] = page

        # --- Login check ---
        # Navigate to the main Cathay page and poll for auth state.
        # React hydrates the auth state asynchronously after networkidle, so a single
        # check fires too early and incorrectly triggers auto_login every run even when
        # the session cookie is still valid.
        await page.goto(CATHAY_HOME, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            await asyncio.sleep(4)

        needs_login = True
        for _attempt in range(15):  # poll up to 15 s for a definitive auth state
            state = await is_logged_in(page)
            logger.info("Login state check (attempt %d): %s", _attempt + 1, state)
            if state == 'logged_in':
                needs_login = False
                # Refresh saved state while session is live so next run gets fresh cookies
                try:
                    await ctx.storage_state(path=str(SESSION_FILE))
                    logger.info("run_search_job: session state refreshed (already logged in)")
                except Exception:
                    pass
                break
            if state == 'not_logged_in':
                break
            await asyncio.sleep(1)

        if needs_login:
            # Try automated login with credentials from .env first
            auto_logged_in = await auto_login(page, queue=queue, otp_event=otp_event, otp_holder=otp_holder)

            if not auto_logged_in:
                # Fall back to virtual browser panel — user logs in directly on Cathay site
                await queue.put({
                    "type": "login",
                    "message": "Please log in to your Asia Miles account using the panel below.",
                })
                logged_in = await wait_for_login(page, login_event, timeout_seconds=300, session_file=session_file)
                if not logged_in:
                    if search_id:
                        active_pages.pop(search_id, None)
                    await queue.put({"type": "error", "message": "Login timed out after 5 minutes."})
                    await ctx.close()
                    return
            (profile_dir / "logged_in.flag").touch()
            await queue.put({"type": "login_complete"})
        else:
            # Already logged in from saved session — refresh the flag
            (profile_dir / "logged_in.flag").touch()

        # --- Run searches ---
        # Sort by (destination, nights, departure_date, cabin_order) so that:
        # • All cabins for a given date/route are adjacent → cabin tab switches within a date
        # • Consecutive dates for the same route are adjacent → date strip between dates
        # Cabin order matches the user's selection order (only selected cabins appear).
        def _nights(c):
            return (c["return_date"] - c["departure_date"]).days if c["return_date"] else -1

        cabin_order = {c: i for i, c in enumerate(request.cabin_classes)}

        combos_ordered = sorted(
            combos,
            key=lambda c: (
                c["destination"],
                _nights(c),
                c["departure_date"],
                cabin_order.get(c["cabin_class"], 99),
            ),
        )

        on_outbound_page = False
        results_route = None   # (dest, nights_val) currently shown on page
        results_date = None    # departure date currently shown
        results_cabin = None   # cabin class key currently shown
        skipped_keys: set = set()  # (dest, dep, ret, cabin) already handled via tab inspection

        for i, combo in enumerate(combos_ordered, start=1):
            dest = combo["destination"]
            dep = combo["departure_date"]
            ret = combo["return_date"]
            cabin = combo["cabin_class"]
            nights_val = _nights(combo)
            route = (dest, nights_val)
            combo_key = (dest, dep, ret, cabin)

            # Skip combos already resolved via cabin-tab inspection
            if combo_key in skipped_keys:
                unavail = FlightResult(
                    origin=request.origin, destination=dest,
                    departure_date=str(dep),
                    return_date=str(ret) if ret else None,
                    cabin_class=cabin, available=False,
                    error=f"{CABIN_LABELS[cabin]} tab not shown on results page — no seats this date",
                )
                await queue.put({"type": "progress", "current": i, "total": total,
                    "message": f"Skipping {CABIN_LABELS[cabin]} {dep} — tab absent on page"})
                await queue.put({"type": "result", "data": unavail.model_dump()})
                continue

            await queue.put({
                "type": "progress",
                "current": i,
                "total": total,
                "message": (
                    f"Searching {request.origin}→{dest}  "
                    f"{dep}{f' → {ret}' if ret else ''}  "
                    f"({CABIN_LABELS[cabin]})"
                ),
            })

            result = None

            if on_outbound_page and route == results_route:
                # ── Fast path A: same route + same date + different cabin ────────────
                if dep == results_date and cabin != results_cabin:
                    tab_ok = await _click_cabin_tab(page, CABIN_LABELS[cabin])
                    if tab_ok:
                        try:
                            await asyncio.sleep(2)
                            for _ in range(8):
                                tlen = await page.locator("body").evaluate("el => el.innerText.trim().length")
                                if tlen > 100:
                                    break
                                await asyncio.sleep(0.5)
                            result = await extract_results(page, request.origin, dest, dep, ret, cabin)
                            results_cabin = cabin
                            on_outbound_page = True  # cabin tab never leaves the outbound page
                        except Exception as exc:
                            if "TargetClosedError" in type(exc).__name__ or "Target page" in str(exc):
                                await queue.put({"type": "error", "message": "Browser window was closed — search stopped."})
                                return
                            logger.warning("Cabin tab search failed (%s) — falling back", exc)
                            result = None
                            on_outbound_page = False

                # ── Fast path B: same route + different date ─────────────────────────
                elif dep != results_date:
                    strip_ok = await _navigate_date_strip(page, dep)
                    if strip_ok:
                        try:
                            await asyncio.sleep(3)
                            for _ in range(15):
                                tlen = await page.locator("body").evaluate("el => el.innerText.trim().length")
                                if tlen > 100:
                                    break
                                await asyncio.sleep(1)
                            await asyncio.sleep(2)
                            results_date = dep

                            # Date strip keeps current cabin; switch if the combo needs a different one
                            if cabin != results_cabin:
                                tab_ok = await _click_cabin_tab(page, CABIN_LABELS[cabin])
                                if tab_ok:
                                    await asyncio.sleep(2)
                                    results_cabin = cabin
                                else:
                                    # Can't switch cabin after strip — fall back to full search
                                    on_outbound_page = False
                                    result = None  # trigger full search below

                            if result is None and cabin != results_cabin:
                                pass  # already marked for full search
                            else:
                                result = await extract_results(page, request.origin, dest, dep, ret, cabin)
                                on_outbound_page = True  # date strip never leaves the outbound page
                        except Exception as exc:
                            if "TargetClosedError" in type(exc).__name__ or "Target page" in str(exc):
                                await queue.put({"type": "error", "message": "Browser window was closed — search stopped."})
                                return
                            logger.warning("Date strip search failed (%s) — falling back", exc)
                            result = None
                            on_outbound_page = False

            # ── Full search (first in route group, or fast path failed) ─────────────
            if result is None:
                try:
                    result = await search_one_flight(page, request.origin, dest, dep, ret, cabin)
                except Exception as exc:
                    if "TargetClosedError" in type(exc).__name__ or "Target page" in str(exc):
                        logger.warning("run_search_job: browser closed — stopping search")
                        await queue.put({"type": "error", "message": "Browser window was closed — search stopped."})
                        return
                    raise

                results_route = route
                results_date = dep
                results_cabin = cabin
                if ret:
                    on_outbound_page = await _navigate_back_to_outbound(page)
                else:
                    on_outbound_page = True

            # Always sync state (fast paths may have partially updated these).
            # Use the result's actual departure_date (may differ from dep when SBS
            # adjusted the date), so the next combo's date strip navigation uses the
            # correct baseline.
            results_route = route
            try:
                results_date = date.fromisoformat(result.departure_date) if result.departure_date else dep
            except ValueError:
                results_date = dep
            results_cabin = cabin

            # If the SBS calendar jumped to a different departure date than requested,
            # emit an unavailable placeholder for the originally requested date first
            # so the frontend shows "no flights for 21 Jun" rather than showing 23 Jun
            # results mislabelled as 21 Jun.
            if result.note and result.departure_date and result.departure_date != str(dep):
                placeholder = FlightResult(
                    origin=request.origin,
                    destination=dest,
                    departure_date=str(dep),
                    return_date=str(ret) if ret else None,
                    cabin_class=cabin,
                    available=False,
                    error=f"No award flights on this date — nearest available: {result.departure_date}",
                )
                await queue.put({"type": "result", "data": placeholder.model_dump()})

            await queue.put({"type": "result", "data": result.model_dump()})

            # After any successful extraction on the results page, inspect visible
            # cabin tabs. Upcoming combos for the same (dest, dep, ret) whose cabin
            # tab is absent have no seats — queue them as unavailable and skip later.
            if on_outbound_page and result is not None:
                visible_tabs = await _get_visible_cabin_tabs(page)
                if visible_tabs:
                    for fc in combos_ordered[i:]:  # look ahead at remaining combos
                        if (fc["destination"] == dest
                                and fc["departure_date"] == dep
                                and fc["return_date"] == ret
                                and fc["cabin_class"] not in visible_tabs):
                            fkey = (fc["destination"], fc["departure_date"], fc["return_date"], fc["cabin_class"])
                            if fkey not in skipped_keys:
                                skipped_keys.add(fkey)
                                logger.info(
                                    "Tab inspection: %s absent for %s %s — will skip",
                                    fc["cabin_class"], dest, dep,
                                )

            if i < total:
                await asyncio.sleep(SEARCH_DELAY_SECONDS)

        if search_id:
            active_pages.pop(search_id, None)
        await ctx.close()

    await queue.put({"type": "complete"})
