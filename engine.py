"""
Site Engine — Playwright-based automation for gplgames.net.

The site uses LiteSpeed Bot Verification + reCAPTCHA v2, blocking all
non-browser HTTP clients. Strategy:
  - ALL page interactions go through Playwright (real Chromium browser).
  - First visit triggers LiteSpeed reCAPTCHA verification page.
  - We solve it via Nopecha API (reCAPTCHA v2 token solving).
  - After solving, cookies are cached → subsequent visits bypass verification.
  - Cookie import from user's browser is also supported as a fallback.

CC details are handled ONLY in memory and zeroed after use.
"""

import re
import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from session_manager import SessionManager
from secure_log import get_logger
from config import (
    SITE_URL, LOGIN_URL, CHECKOUT_URL, CART_URL,
    RECAPTCHA_SITE_KEY, NOPECHA_API_KEY, HEADLESS, BROWSER_TIMEOUT,
)

logger = get_logger("SiteEngine")

NOPECHA_BASE = "https://api.nopecha.com"


@dataclass
class CreditCard:
    """Credit card details - EXISTS ONLY IN MEMORY."""
    number: str = ""
    expiry: str = ""
    cvv: str = ""

    def secure_wipe(self) -> None:
        self.number = "\x00" * len(self.number)
        self.expiry = "\x00" * len(self.expiry)
        self.cvv = "\x00" * len(self.cvv)
        self.number = ""
        self.expiry = ""
        self.cvv = ""
        logger.info("Credit card data securely wiped from memory")


class SiteEngine:
    """
    Playwright-based automation engine for gplgames.net.
    Every page load goes through a real Chromium browser.
    The first load solves LiteSpeed reCAPTCHA via Nopecha; cookies are then persisted.
    """

    def __init__(self, user_id: int, on_status: Callable[[str], Awaitable[None]]):
        self.user_id = user_id
        self.on_status = on_status
        self.session_manager = SessionManager(user_id)
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.product_id: int | None = None
        self._cc: CreditCard | None = None
        self.nopecha_key: str = NOPECHA_API_KEY

    # ================================================================
    # BROWSER LIFECYCLE
    # ================================================================
    async def init_session(self) -> None:
        """Launch browser and restore cookies."""
        await self.on_status("🔗 Launching browser...")

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                # Use the NEW headless mode (Chrome's real headless) instead
                # of the legacy --headless=chrome flag. The old headless mode
                # identifies itself as "HeadlessChrome" in sec-ch-ua, which
                # Cloudflare/LiteSpeed WAFs detect and use to return 403 on
                # form submits. The new mode reports as regular Chrome.
                "--headless=new",
                # Disable automation info bar and other giveaways
                "--disable-infobars",
                "--disable-extensions",
                "--password-store=basic",
                "--use-mock-keychain",
            ],
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            # Override sec-ch-ua headers — Playwright's headless Chromium
            # normally sends '"HeadlessChrome";v="X"' here, which the WAF
            # uses to detect and block automation. Force real-Chrome values.
            extra_http_headers={
                "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "accept-language": "en-US,en;q=0.9",
            },
        )

        # Remove webdriver detection + override navigator.userAgentData
        # (Playwright leaks "HeadlessChrome" brand via navigator.userAgentData.brands)
        await self.context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

            // Override navigator.userAgentData to report regular Chrome brands.
            // We preserve all original methods (getHighEntropyValues etc.) by
            // creating a fresh object with the same prototype chain.
            if (navigator.userAgentData) {
                const origUAD = navigator.userAgentData;
                const fakeBrands = [
                    {brand: 'Chromium', version: '131'},
                    {brand: 'Not_A Brand', version: '24'},
                    {brand: 'Google Chrome', version: '131'}
                ];
                const fakeUAD = Object.create(Object.getPrototypeOf(origUAD));
                Object.defineProperties(fakeUAD, {
                    brands:       {get: () => fakeBrands, configurable: true},
                    mobile:       {get: () => false, configurable: true},
                    platform:     {get: () => 'Windows', configurable: true},
                    getHighEntropyValues: {
                        value: async (hints) => ({
                            architecture: 'x86',
                            bitness: '64',
                            brands: fakeBrands,
                            mobile: false,
                            model: '',
                            platform: 'Windows',
                            platformVersion: '15.0.0',
                            uaFullVersion: '131.0.0.0',
                            fullWidth: '131.0.6778.85',
                        }),
                        configurable: true,
                    },
                    toJSON: {
                        value: () => ({brands: fakeBrands, mobile: false, platform: 'Windows'}),
                        configurable: true,
                    },
                });
                Object.defineProperty(navigator, 'userAgentData', {
                    get: () => fakeUAD,
                    configurable: true,
                });
            }

            // Override permissions API (headless Chrome reports differently)
            const origQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (origQuery) {
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : origQuery(parameters)
                );
            }

            // Plugins — headless Chrome has no plugins, real Chrome has 5
            try {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                    configurable: true,
                });
            } catch (e) {}

            // Languages — make sure en-US is first
            try {
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                    configurable: true,
                });
            } catch (e) {}
            """
        )

        # Restore saved cookies
        saved = self.session_manager.load_cookies()
        if saved:
            pw_cookies = []
            for c in saved:
                pw_cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".gplgames.net"),
                    "path": c.get("path", "/"),
                })
            await self.context.add_cookies(pw_cookies)
            logger.info("Restored session cookies")

        self.page = await self.context.new_page()
        self.page.set_default_timeout(BROWSER_TIMEOUT)

        # Restore localStorage after the page exists. We must navigate to the
        # site first because localStorage is origin-scoped.
        saved_storage = self.session_manager.load_local_storage()
        if saved_storage:
            try:
                await self.page.goto(SITE_URL, wait_until="domcontentloaded", timeout=15000)
                await self.page.evaluate(
                    """(entries) => {
                        for (const {key, value} of entries) {
                            try { window.localStorage.setItem(key, value); } catch (e) {}
                        }
                    }""",
                    saved_storage,
                )
                logger.info(f"Restored {len(saved_storage)} localStorage entries")
            except Exception as e:
                logger.warning(f"Could not restore localStorage: {e}")

    async def close(self) -> None:
        """Save cookies and localStorage, then close browser."""
        # 1. Save cookies first (separate try so browser close still happens)
        try:
            if self.context:
                cookies = await self.context.cookies()
                cookie_list = [
                    {"name": c["name"], "value": c["value"],
                     "domain": c.get("domain", ""), "path": c.get("path", "/")}
                    for c in cookies
                ]
                if cookie_list:
                    self.session_manager.save_cookies(cookie_list)
                    logger.info("Session cookies saved")
        except Exception as e:
            logger.error(f"Error saving cookies on close: {e}")

        # 1b. Save localStorage (origin-scoped, requires being on the site)
        try:
            if self.page:
                # Make sure we're on the site origin before reading storage
                if "gplgames.net" not in (self.page.url or ""):
                    try:
                        await self.page.goto(SITE_URL, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                storage_entries = await self.page.evaluate(
                    """() => {
                        const out = [];
                        for (let i = 0; i < window.localStorage.length; i++) {
                            const k = window.localStorage.key(i);
                            try { out.push({key: k, value: window.localStorage.getItem(k)}); } catch (e) {}
                        }
                        return out;
                    }"""
                )
                if storage_entries:
                    self.session_manager.save_local_storage(storage_entries)
                    logger.info(f"Saved {len(storage_entries)} localStorage entries")
        except Exception as e:
            logger.error(f"Error saving localStorage on close: {e}")

        # 2. Wipe CC
        if self._cc:
            self._cc.secure_wipe()
            self._cc = None

        # 3. Close browser resources
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error(f"Error stopping playwright: {e}")

        # 4. Null out refs to prevent use-after-close
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    # ================================================================
    # NOPECHA CAPTCHA SOLVING (raw API — reliable)
    # ================================================================
    async def _solve_recaptcha_nopecha(self, page_url: str) -> str | None:
        """
        Solve reCAPTCHA v2 via Nopecha API.
        POST to create a task, GET to poll for the token.
        Returns the reCAPTCHA token string, or None on failure.
        """
        if not self.nopecha_key:
            return None

        try:
            # Step 1: Create task
            async with aiohttp.ClientSession() as http:
                post_body = {
                    "key": self.nopecha_key,
                    "type": "recaptcha2",
                    "sitekey": RECAPTCHA_SITE_KEY,
                    "url": page_url,
                }

                async with http.post(
                    f"{NOPECHA_BASE}/token/",
                    json=post_body,
                    headers={"Authorization": f"Key {self.nopecha_key}"},
                ) as resp:
                    post_data = await resp.json()

                if "data" not in post_data:
                    logger.error(f"Nopecha POST failed: {post_data}")
                    return None

                job_id = post_data["data"]
                logger.info(f"Nopecha job created: {job_id}")

                # Step 2: Poll for result (up to 180 seconds)
                await self.on_status("⏳ Solving captcha (~60-90s)...")
                for attempt in range(180):
                    await asyncio.sleep(1)

                    async with http.get(
                        f"{NOPECHA_BASE}/token/",
                        params={"key": self.nopecha_key, "id": job_id}
                    ) as resp:
                        result = await resp.json()

                    if "data" in result:
                        token = result["data"]
                        if isinstance(token, str) and len(token) > 50:
                            logger.info(f"Captcha solved (attempt {attempt + 1})")
                            return token

                    # Show progress every 15 seconds
                    if attempt > 0 and attempt % 15 == 0:
                        await self.on_status(f"⏳ Still solving... ({attempt}s)")

                    # Check for errors (except IncompleteJob which means still solving)
                    if "error" in result:
                        error_code = result.get("error")
                        # Handle both int (14) and string ("14" / "IncompleteJob")
                        if str(error_code) == "14":
                            continue  # IncompleteJob — keep polling
                        else:
                            logger.error(f"Nopecha error: {result}")
                            return None

                logger.error("Nopecha timeout after 180s")
                return None

        except Exception as e:
            logger.error(f"Nopecha solve error: {e}")
            return None

    async def _inject_token_and_wait(self, page: Page, token: str, url: str) -> bool:
        """
        Core injection logic — tries multiple methods to submit the reCAPTCHA
        token to LiteSpeed. Returns True if verification passed.
        """

        # ------------------------------------------------------------------
        # METHOD 1: Find and call the reCAPTCHA data-callback directly.
        # This is the CORRECT way — LiteSpeed registers a JS callback that
        # POSTs the token to its verification endpoint, sets cookies, then
        # reloads.  Just setting the textarea + form.submit() bypasses this.
        # ------------------------------------------------------------------
        callback_result = await page.evaluate(
            """(token) => {
                // 1a. Look for data-callback on the .g-recaptcha div
                const rcDiv = document.querySelector('.g-recaptcha') ||
                              document.querySelector('[data-sitekey]') ||
                              document.querySelector('[data-callback]');
                if (rcDiv) {
                    const cbName = rcDiv.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {
                        // Inject token into textarea first (callback may read it)
                        const ta = document.getElementById('g-recaptcha-response');
                        if (ta) { ta.value = token; ta.style.display = 'block'; }
                        try { window[cbName](token); return 'callback:' + cbName; }
                        catch(e) { return 'callback_error:' + e.message; }
                    }
                }

                // 1b. Check grecaptcha's internal client config for callback
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {
                        for (const cid in cfg.clients) {
                            const cl = cfg.clients[cid];
                            if (cl) {
                                // Check all properties for a function callback
                                for (const prop of ['callback', 'onSuccess', 'successCb']) {
                                    if (typeof cl[prop] === 'function') {
                                        const ta = document.getElementById('g-recaptcha-response');
                                        if (ta) { ta.value = token; ta.style.display = 'block'; }
                                        try { cl[prop](token); return 'cfg:' + prop; }
                                        catch(e) { /* continue */ }
                                    }
                                }
                            }
                        }
                    }
                } catch(e) { /* not available */ }

                return null;
            }""",
            token,
        )

        if (callback_result and callback_result.startswith("callback:")) or (
            callback_result and callback_result.startswith("cfg:")
        ):
            method_name = callback_result.split(":")[0] + " " + callback_result.split(":", 1)[1]
            await self.on_status(f"🔄 Triggered {method_name}, waiting for redirect...")

            # Wait for the callback to do its thing (POST + reload)
            try:
                async with page.expect_navigation(timeout=15000, wait_until="domcontentloaded"):
                    pass
            except Exception:
                # expect_navigation timed out — callback may have failed silently
                await asyncio.sleep(2)

            if "Bot Verification" not in await self._safe_page_content(page):
                await self.on_status("✅ Verification passed!")
                return True

        elif callback_result and "error" in callback_result:
            logger.warning(f"Callback invocation failed: {callback_result}")

        # ------------------------------------------------------------------
        # METHOD 2: Inject + dispatch events + form submit (old approach,
        # improved with events so React/Vue/jQuery pick up the change).
        # ------------------------------------------------------------------
        await self.on_status("🔄 Trying form submission...")
        await page.evaluate(
            """(token) => {
                const ta = document.getElementById('g-recaptcha-response');
                if (ta) {
                    ta.value = token;
                    ta.style.display = 'block';
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            token,
        )
        await asyncio.sleep(0.5)

        form_found = await page.evaluate("""() => {
            // Try multiple form selectors
            const selectors = [
                '#lsrecaptcha-form',
                'form[action*="verify"]',
                'form[action*="lscache"]',
                'form[action*="captcha"]',
                'form'
            ];
            for (const sel of selectors) {
                const form = document.querySelector(sel);
                if (form) {
                    try { form.submit(); return sel; }
                    catch(e) { /* try next */ }
                }
            }
            return null;
        }""")

        if form_found:
            await self.on_status(f"🔄 Submitted form ({form_found}), waiting...")
            try:
                async with page.expect_navigation(timeout=12000, wait_until="domcontentloaded"):
                    pass
            except Exception:
                await asyncio.sleep(3)

            if "Bot Verification" not in await self._safe_page_content(page):
                await self.on_status("✅ Verification passed!")
                return True

        # ------------------------------------------------------------------
        # METHOD 3: Manually POST the token to common LiteSpeed verify
        # endpoints using the browser's fetch (same origin, cookies sent).
        # ------------------------------------------------------------------
        await self.on_status("🔄 Trying direct token POST...")
        posted = await page.evaluate(
            """(token) => {
                const endpoints = [
                    '/__lscache/verify',
                    '/wp-content/plugins/litespeed-cache/guest.vary.php',
                    '/?lscache_verify=1'
                ];
                // Try each endpoint
                for (const ep of endpoints) {
                    try {
                        const fd = new FormData();
                        fd.append('g-recaptcha-response', token);
                        const xhr = new XMLHttpRequest();
                        xhr.open('POST', ep, false); // synchronous
                        xhr.send(fd);
                        if (xhr.status === 200) return ep + ' -> ' + xhr.status;
                    } catch(e) { /* try next */ }
                }
                return null;
            }""",
            token,
        )

        if posted:
            await self.on_status(f"🔄 POSTed token ({posted}), reloading...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
            except Exception:
                await asyncio.sleep(3)

            if "Bot Verification" not in await self._safe_page_content(page):
                await self.on_status("✅ Verification passed!")
                return True

        # ------------------------------------------------------------------
        # METHOD 4: Navigate to target URL — maybe the callback already
        # set the verification cookie and a simple navigation works.
        # ------------------------------------------------------------------
        await self.on_status("🔄 Navigating to target page...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
            if "Bot Verification" not in await self._safe_page_content(page):
                await self.on_status("✅ Verification passed!")
                return True
        except Exception:
            pass

        # All methods failed
        return False

    async def _solve_captcha_and_navigate(self, page: Page, url: str) -> bool:
        """
        If page shows LiteSpeed Bot Verification, solve reCAPTCHA via Nopecha
        and inject the token to get past it. Then navigate to the actual URL.
        Returns True if we end up on the real page (not verification).
        """
        content = await self._safe_page_content(page)
        if "Bot Verification" not in content:
            return True  # No verification needed

        await self.on_status("🤖 Bot verification detected — solving via Nopecha...")

        # Get a fresh token from Nopecha
        token = await self._solve_recaptcha_nopecha(page.url)
        if not token:
            await self.on_status(
                "❌ Nopecha failed to solve captcha.\n\n"
                "Possible fixes:\n"
                "• Check your Nopecha credits\n"
                "• Try again in a moment\n"
                "• Use /cookies for free login instead"
            )
            return False

        await self.on_status("✅ Captcha solved! Injecting token...")

        passed = await self._inject_token_and_wait(page, token, url)

        if not passed:
            await self.on_status("❌ Verification failed after all attempts.\n\n💡 Use /cookies for free login (no captcha needed).")
            return False

        # Save cookies immediately so subsequent loads skip captcha entirely.
        try:
            if self.context:
                cookies = await self.context.cookies()
                cookie_list = [
                    {"name": c["name"], "value": c["value"],
                     "domain": c.get("domain", ""), "path": c.get("path", "/")}
                    for c in cookies
                ]
                if cookie_list:
                    self.session_manager.save_cookies(cookie_list)
                    logger.info("Post-captcha cookies saved")
        except Exception as e:
            logger.error(f"Error saving post-captcha cookies: {e}")

        return True

    async def _ensure_page_loaded(self, url: str) -> bool:
        """
        Navigate to URL, solve captcha if needed.
        Returns True if the actual page loaded (not verification page).
        """
        if not self.page:
            logger.error("Browser page not initialized")
            await self.on_status("❌ Browser not ready. Try again.")
            return False
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            content = await self._safe_get_content()
            if "Bot Verification" in content:
                passed = await self._solve_captcha_and_navigate(self.page, url)
                if not passed:
                    return False

                # CRITICAL: After captcha solving, the page may be on a
                # different URL (verification success page, homepage, or
                # mid-redirect). Re-navigate to the originally requested URL
                # so the caller gets a stable page on the right URL.
                try:
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    # Confirm we're not back on the verification page
                    post_content = await self._safe_get_content()
                    if "Bot Verification" in post_content:
                        logger.error("Still on verification page after re-navigation")
                        await self.on_status("❌ Captcha solved but page redirected back. Try /cookies instead.")
                        return False
                except Exception as e:
                    logger.error(f"Post-captcha navigation to {url} failed: {e}")
                    await self.on_status("❌ Could not reach target page after captcha. Try /cookies instead.")
                    return False

            return True
        except Exception as e:
            logger.error(f"Page load error for {url}: {e}")
            return False

    async def _safe_get_content(self, max_retries: int = 4) -> str:
        """
        Get page.content() with retries — handles the case where the page is
        mid-navigation and Playwright throws
        "Unable to retrieve content because the page is navigating and changing the content."

        This is the #1 cause of false "Login failed" errors after captcha solving.
        """
        return await self._safe_page_content(self.page, max_retries)

    @staticmethod
    async def _safe_page_content(page: Page, max_retries: int = 4) -> str:
        """Static helper — same as _safe_get_content but for an arbitrary page."""
        last_exc = None
        for attempt in range(max_retries):
            try:
                # Give any in-flight navigation time to settle first
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    # load state wait may itself time out during a slow redirect —
                    # fall through and try content() anyway
                    pass
                return await page.content()
            except Exception as e:
                last_exc = e
                msg = str(e).lower()
                if "navigating" in msg or "changing the content" in msg:
                    logger.debug(f"page.content() retry {attempt + 1}/{max_retries}: {e}")
                    await asyncio.sleep(1.5)
                    continue
                # Different error — re-raise immediately
                raise
        # Exhausted retries — raise the last navigation error
        raise last_exc

    # ================================================================
    # COOKIE IMPORT
    # ================================================================
    async def import_cookies_from_string(self, cookie_string: str) -> bool:
        """Import cookies from a browser cookie string: 'name1=val1; name2=val2'"""
        try:
            cookies = []
            for part in cookie_string.split(";"):
                part = part.strip()
                if "=" in part:
                    name, value = part.split("=", 1)
                    cookies.append({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".gplgames.net",
                        "path": "/",
                    })

            if not cookies:
                return False

            if not self.context:
                await self.init_session()

            await self.context.add_cookies(cookies)
            self.session_manager.save_cookies(
                [{"name": c["name"], "value": c["value"],
                  "domain": c.get("domain", ""), "path": c.get("path", "/")}
                 for c in cookies]
            )

            # Verify by checking my-account page
            await self.on_status("🔍 Verifying session...")
            ok = await self._ensure_page_loaded(LOGIN_URL)
            if not ok:
                return False

            content = await self._safe_get_content()
            is_logged = self._is_logged_in(content, self.page.url)
            return is_logged

        except Exception as e:
            logger.error(f"Cookie import error: {e}")
            return False

    # ================================================================
    # LOGIN (email + password via Playwright)
    # ================================================================
    async def login(self, email: str, password: str) -> bool:
        """Login via Playwright on the actual login page."""
        await self.on_status("🔐 Loading login page...")

        try:
            ok = await self._ensure_page_loaded(LOGIN_URL)
            if not ok:
                return False

            current_url = self.page.url
            logger.info(f"Login page URL after ensure_loaded: {current_url}")
            content = await self._safe_get_content()
            content_lower = content.lower()

            # Log what we actually got — helps diagnose stale-cookie redirects
            title_match = re.search(r"<title>(.*?)</title>", content, re.I | re.DOTALL)
            page_title = title_match.group(1).strip() if title_match else "(no title)"
            logger.info(f"Login page title: {page_title!r}, content length: {len(content)}")

            # Check if we're already logged in (e.g. valid cookies from before)
            already_logged = self._is_logged_in(content, current_url)
            if already_logged:
                logger.info("Already logged in (cookies valid) — skipping credential entry")
                await self.on_status("✅ Already logged in! Session is persistent.")
                return True

            if "woocommerce-login-nonce" not in content:
                # Save a snippet for debugging
                snippet = re.sub(r"\s+", " ", content_lower[:500])
                logger.error(
                    f"Login page did not load properly. URL={current_url}, "
                    f"title={page_title!r}, snippet={snippet!r}"
                )
                await self.on_status(
                    f"❌ Login page did not load properly.\n"
                    f"URL: {current_url}\n"
                    f"Title: {page_title}\n"
                    f"Try /cookies for free login instead."
                )
                return False

            await self.on_status("🔐 Filling credentials...")

            email_input = self.page.locator('input#username')
            username_count = await email_input.count()
            logger.info(f"Username input found: {username_count} element(s)")
            if username_count == 0:
                logger.error("Username input not found on page")
                await self.on_status("❌ Could not find username field. Page may have changed.")
                return False
            await email_input.fill(email)

            pass_input = self.page.locator('input#password')
            await pass_input.fill(password)

            await asyncio.sleep(0.5)

            login_btn = self.page.locator('button[name="login"]')
            btn_count = await login_btn.count()
            logger.info(f"Login button found: {btn_count} element(s)")
            if btn_count == 0:
                logger.error("Login button not found on page")
                await self.on_status("❌ Could not find login button. Page may have changed.")
                return False
            await login_btn.click()
            logger.info("Login button clicked, waiting for navigation...")

            # Wait for the post-login navigation. Don't use networkidle —
            # gplgames.net has continuous polling that prevents it from firing.
            # Use domcontentloaded (fires once after HTML is parsed) + a sleep
            # so any post-login redirect can complete.
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # CRITICAL: gplgames.net sometimes throws a SECONDARY bot verification
            # challenge when the login form is submitted (different from the
            # initial visit captcha). Detect it and solve it before checking
            # login status. May need to solve + resubmit up to 2 times.
            max_login_attempts = 3
            for attempt in range(max_login_attempts):
                post_url = self.page.url
                post_content = await self._safe_get_content()

                # Already logged in? Done.
                if self._is_logged_in(post_content, post_url):
                    logger.info(f"Logged in after attempt {attempt + 1}")
                    break

                # Not logged in and no captcha visible? Either login failed
                # with an error message, or the form is still showing.
                if "Bot Verification" not in post_content:
                    logger.info(
                        f"Attempt {attempt + 1}: no captcha, not logged in "
                        f"(URL={post_url}, title detected separately)"
                    )
                    break

                # Captcha present — solve it.
                logger.warning(
                    f"Secondary captcha triggered after login submit "
                    f"(attempt {attempt + 1}/{max_login_attempts}, URL={post_url}). Solving..."
                )
                await self.on_status(
                    f"🤖 Login triggered another captcha — solving (attempt {attempt + 1})..."
                )

                captcha_passed = await self._solve_captcha_and_navigate(self.page, LOGIN_URL)
                if not captcha_passed:
                    logger.error(f"Secondary captcha (attempt {attempt + 1}) failed")
                    await self.on_status(
                        "❌ Login captcha failed.\n"
                        "💡 Try /cookies for free login (no captcha)."
                    )
                    return False

                # After captcha, re-navigate to login URL to check status
                try:
                    await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Post-captcha navigation to login URL failed: {e}")
                    await self.on_status("❌ Could not reach my-account after captcha.")
                    return False

                # Check if we're now on the login FORM (meaning we need to
                # re-submit credentials) or already logged in.
                content_after_captcha = await self._safe_get_content()
                if self._is_logged_in(content_after_captcha, self.page.url):
                    logger.info(f"Logged in after captcha (attempt {attempt + 1})")
                    break

                # If login form is showing again, the captcha cleared but we
                # need to re-fill and re-submit credentials (gplgames does
                # this: captcha resets the session, then you re-login).
                if "woocommerce-login-nonce" in content_after_captcha:
                    logger.info(f"Re-submitting credentials after captcha (attempt {attempt + 1})")
                    await self.on_status("🔄 Re-submitting credentials after captcha...")

                    email_input = self.page.locator('input#username')
                    if await email_input.count() > 0:
                        await email_input.fill(email)
                    pass_input = self.page.locator('input#password')
                    if await pass_input.count() > 0:
                        await pass_input.fill(password)
                    await asyncio.sleep(0.5)

                    login_btn = self.page.locator('button[name="login"]')
                    if await login_btn.count() > 0:
                        await login_btn.click()
                        logger.info("Re-clicked login button after captcha")

                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    # Loop continues to next attempt — will check status again
                else:
                    # Neither logged in nor login form — unusual state, give up
                    logger.error(
                        f"After captcha (attempt {attempt + 1}): unexpected page state. "
                        f"URL={self.page.url}"
                    )
                    break

            # Final status check after all attempts
            post_url = self.page.url
            content = await self._safe_get_content()
            is_logged = self._is_logged_in(content, post_url)
            logger.info(f"Logged-in check after submit: {is_logged} (URL={post_url})")

            if is_logged:
                await self.on_status("✅ Login successful! Session is persistent.")
                return True
            else:
                errors = re.findall(
                    r'<ul[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</ul>',
                    content, re.DOTALL | re.I
                )
                if errors:
                    error_text = re.sub(r"<[^>]*>", "", errors[0]).strip()
                    logger.warning(f"WooCommerce error shown: {error_text[:200]}")
                    await self.on_status(f"❌ Login failed: {error_text[:100]}")
                else:
                    # Log diagnostic info so we can debug "silent" failures
                    post_title_match = re.search(r"<title>(.*?)</title>", content, re.I | re.DOTALL)
                    post_title = post_title_match.group(1).strip() if post_title_match else "(no title)"
                    snippet = re.sub(r"\s+", " ", content.lower()[:400])
                    logger.warning(
                        f"Login failed silently. URL={post_url}, title={post_title!r}, "
                        f"snippet={snippet!r}"
                    )
                    await self.on_status(
                        "❌ Login failed. Check your credentials.\n"
                        "💡 Try /cookies for free login (no captcha)."
                    )
                return False

        except Exception as e:
            logger.error(f"Login error: {e}", exc_info=True)
            await self.on_status(f"❌ Login error: {str(e)[:100]}")
            return False

    def _is_logged_in(self, content: str, current_url: str = "") -> bool:
        """
        Robust logged-in detection. Checks multiple signals:
        - "log out" / "logout" / "sign out" text (case-insensitive)
        - A hyperlink whose href contains 'logout' or 'customer-logout'
        - WooCommerce my-account dashboard markers (only when login form is gone)

        Returns True if a strong signal indicates the user is logged in.
        """
        content_lower = content.lower()

        # Must NOT be on the bot verification page — that page has no login
        # form but the user is definitely NOT logged in.
        if "bot verification" in content_lower:
            return False

        # Signal 1: logout text anywhere on page (strongest signal)
        if "log out" in content_lower or "logout" in content_lower or "sign out" in content_lower:
            return True

        # Signal 2: logout/logout link href
        if re.search(r'href=["\'][^"\']*(?:logout|customer-logout)[^"\']*["\']', content_lower):
            return True

        # Signal 3: WooCommerce account dashboard markers + no login form.
        # This catches themes that put the logout link behind a dropdown.
        has_login_form = "woocommerce-login-nonce" in content_lower
        has_account_dashboard = (
            "woocommerce-my-account" in content_lower
            or "woocommerce-account" in content_lower
            or "account-content" in content_lower
            or "account-navigation" in content_lower
        )
        if has_account_dashboard and not has_login_form:
            return True

        return False

    # ================================================================
    # URL VERIFICATION
    # ================================================================
    async def verify_url(self, url: str) -> bool:
        """Verify if URL is a valid gplgames.net product page."""
        await self.on_status("🔍 Verifying product URL...")

        try:
            if "gplgames.net" not in url:
                await self.on_status("❌ URL must be from gplgames.net")
                return False

            # Block obvious non-product URLs early (homepage, shop, cart, etc.)
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            path = (parsed.path or "").rstrip("/")
            qs = parse_qs(parsed.query)

            # If ?p=N is present, it's a product ID link — trust it after validation
            pid_match = re.search(r"[?&]p=(\d+)", url)
            if pid_match:
                product_id = int(pid_match.group(1))
                product_url = f"{SITE_URL}/?post_type=product&p={product_id}"
                ok = await self._ensure_page_loaded(product_url)
                if not ok:
                    return False
                content = await self._safe_get_content()

                # Require BOTH a "post_type=product" hint AND an add-to-cart
                # control to avoid false positives from category/shop pages.
                has_add_to_cart = (
                    "add_to_cart" in content
                    or "add-to-cart" in content
                    or 'name="add-to-cart"' in content
                )
                has_product_marker = (
                    "single-product" in content
                    or "woocommerce-product" in content
                    or 'class="product ' in content
                    or "product_id" in content
                )
                if not (has_add_to_cart and has_product_marker):
                    await self.on_status("❌ URL has product ID but page is not a valid product.")
                    return False
                self.product_id = product_id
                await self.on_status(f"✅ Valid product! (ID: {self.product_id})")
                return True

            # Non-?p= URL — load and inspect
            ok = await self._ensure_page_loaded(url)
            if not ok:
                return False

            content = await self._safe_get_content()

            # Reject obvious non-product paths up front
            non_product_paths = ("/shop", "/cart", "/checkout", "/my-account", "/product-category")
            if any(path == np or path.startswith(np) for np in non_product_paths):
                if "post_type=product" not in (parsed.query or ""):
                    await self.on_status("❌ Not a product page (looks like a listing/account page).")
                    return False

            # Require both an add-to-cart control and a product marker
            has_add_to_cart = (
                "add_to_cart" in content
                or "add-to-cart" in content
                or 'name="add-to-cart"' in content
            )
            has_product_marker = (
                "single-product" in content
                or "woocommerce-product" in content
                or 'class="product ' in content
            )
            if not (has_add_to_cart and has_product_marker):
                await self.on_status("❌ Not a valid product page.")
                return False

            pid_match = re.search(r'name="add-to-cart"\s+value="(\d+)"', content)
            if not pid_match:
                pid_match = re.search(r'data-product_id="(\d+)"', content)
            if not pid_match:
                pid_match = re.search(r"\?add-to-cart=(\d+)", content)

            if pid_match:
                self.product_id = int(pid_match.group(1))
                await self.on_status(f"✅ Valid product page! (ID: {self.product_id})")
                return True
            else:
                await self.on_status("❌ Could not find product ID on the page.")
                return False

        except Exception as e:
            logger.error(f"URL verification error: {e}")
            await self.on_status(f"❌ URL error: {str(e)[:100]}")
            return False

    # ================================================================
    # ADD TO CART
    # ================================================================
    async def add_to_cart(self, quantity: int) -> bool:
        """Add product to cart via Playwright."""
        await self.on_status(f"🛒 Adding to cart (qty: {quantity})...")

        try:
            product_url = f"{SITE_URL}/?post_type=product&p={self.product_id}"
            ok = await self._ensure_page_loaded(product_url)
            if not ok:
                return False

            qty_input = self.page.locator('input.qty[name="quantity"]')
            if await qty_input.count() > 0:
                await qty_input.fill(str(quantity))
                await asyncio.sleep(0.3)

            # Try multiple selectors — WooCommerce themes use <button> OR <a>
            # with class single_add_to_cart_button, sometimes with different
            # name attributes.
            add_btn = None
            for selector in [
                'button.single_add_to_cart_button[name="add-to-cart"]',
                'button.single_add_to_cart_button',
                'a.single_add_to_cart_button',
                'button[name="add-to-cart"]',
                'input[name="add-to-cart"]',
            ]:
                candidate = self.page.locator(selector).first
                try:
                    if await candidate.count() > 0 and await candidate.is_visible(timeout=2000):
                        add_btn = candidate
                        break
                except Exception:
                    continue

            if add_btn is not None:
                await add_btn.click()
                await asyncio.sleep(3)

                content = await self._safe_get_content()
                lowered = content.lower()
                if "has been added to your cart" in lowered or "added to cart" in lowered:
                    await self.on_status("✅ Product added to cart!")
                    return True

                # Verify by checking cart page (through captcha-safe loader)
                ok = await self._ensure_page_loaded(CART_URL)
                if not ok:
                    await self.on_status("❌ Could not verify cart (captcha block).")
                    return False
                await asyncio.sleep(2)
                cart_content = await self._safe_get_content()
                if "your cart is currently empty" not in cart_content.lower():
                    await self.on_status("✅ Product added to cart!")
                    return True
                else:
                    await self.on_status("❌ Cart appears empty after adding.")
                    return False
            else:
                await self.on_status("❌ Could not find 'Add to Cart' button.")
                return False

        except Exception as e:
            logger.error(f"Add to cart error: {e}")
            await self.on_status(f"❌ Error adding to cart: {str(e)[:100]}")
            return False

    # ================================================================
    # CHECKOUT
    # ================================================================
    async def get_checkout_page(self) -> dict:
        """Fetch checkout page via Playwright and extract details."""
        await self.on_status("📄 Loading checkout page...")

        try:
            ok = await self._ensure_page_loaded(CHECKOUT_URL)
            if not ok:
                return {"error": "Could not load checkout (captcha block)", "nonce": "", "total": "", "html": ""}

            content = await self._safe_get_content()

            if "Your cart is currently empty" in content:
                return {"error": "Cart is empty", "nonce": "", "total": "", "html": ""}

            nonce_match = re.search(
                r'name="woocommerce-process-checkout-nonce"\s+value="([^"]+)"',
                content
            )
            nonce = nonce_match.group(1) if nonce_match else ""

            total_match = re.search(
                r'order-total[^>]*>.*?<span[^>]*>.*?([\d,]+\.?\d*)',
                content, re.DOTALL
            )
            total = "N/A"
            if total_match:
                num_match = re.search(r"([\d,]+\.?\d*)", total_match.group(0))
                if num_match:
                    total = num_match.group(1)

            return {"nonce": nonce, "total": total, "html": content, "error": None}

        except Exception as e:
            logger.error(f"Checkout page error: {e}")
            return {"error": str(e), "nonce": "", "total": "", "html": ""}

    async def _read_existing_billing_fields(self) -> dict:
        """
        Read pre-filled billing values from the checkout form. Returns a dict
        with whatever fields are already populated (empty values are skipped).

        Works for both <input> fields (read .value) and <select> fields
        (read the selected option's value).
        """
        try:
            values = await self.page.evaluate(
                """() => {
                    const out = {};
                    const fieldIds = [
                        'billing_first_name', 'billing_last_name', 'billing_email',
                        'billing_phone', 'billing_address_1', 'billing_city',
                        'billing_state', 'billing_postcode', 'billing_country'
                    ];
                    for (const id of fieldIds) {
                        const el = document.getElementById(id);
                        if (!el) continue;
                        let val = '';
                        if (el.tagName.toLowerCase() === 'select') {
                            val = el.value || (el.options[el.selectedIndex] ? el.options[el.selectedIndex].value : '');
                        } else {
                            val = el.value || '';
                        }
                        val = (val || '').trim();
                        if (val) out[id] = val;
                    }
                    return out;
                }"""
            )
            return values if values else {}
        except Exception as e:
            logger.warning(f"Could not read existing billing fields: {e}")
            return {}

    def _generate_random_billing(self, existing: dict | None = None) -> dict:
        """
        Generate realistic random billing details for any fields that
        are NOT already in `existing`. Uses NON-INDIA countries only
        (US, UK, Canada, Australia, Germany, Singapore, UAE) per user request.

        Returns a dict with billing_* keys ready to be passed to
        fill_and_submit_checkout().
        """
        import random
        import string

        existing = existing or {}
        out = {}

        # International first names
        first_names = [
            "James", "John", "Robert", "Michael", "David", "William", "Thomas",
            "Daniel", "Matthew", "Christopher", "Andrew", "Joseph", "Ryan",
            "Sarah", "Emily", "Jessica", "Ashley", "Amanda", "Megan", "Lauren",
            "Sophie", "Olivia", "Hannah", "Grace", "Chloe", "Tyler", "Kevin",
            "Eric", "Jason", "Justin", "Marcus", "Marcus", "Dylan", "Logan",
        ]
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
            "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Wilson",
            "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
            "Thompson", "White", "Harris", "Clark", "Lewis", "Walker", "Hall",
            "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams",
        ]

        # Non-India countries with real city/state/postcode combos
        # Format: (country_code, [(city, state, postcode), ...], phone_prefix, phone_len)
        countries = [
            ("US", [
                ("New York",     "NY", "10001"),
                ("Los Angeles",  "CA", "90001"),
                ("Chicago",      "IL", "60601"),
                ("Houston",      "TX", "77001"),
                ("Phoenix",      "AZ", "85001"),
                ("Philadelphia", "PA", "19101"),
                ("San Diego",    "CA", "92101"),
                ("Dallas",       "TX", "75201"),
                ("Seattle",      "WA", "98101"),
                ("Boston",       "MA", "02101"),
            ], "+1", 10),
            ("GB", [
                ("London",     "ENG", "SW1A 1AA"),
                ("Manchester", "ENG", "M1 1AE"),
                ("Birmingham", "ENG", "B1 1AA"),
                ("Leeds",      "ENG", "LS1 1AA"),
                ("Glasgow",    "SCT", "G1 1AA"),
                ("Liverpool",  "ENG", "L1 1AA"),
                ("Bristol",    "ENG", "BS1 1AA"),
                ("Edinburgh",  "SCT", "EH1 1AA"),
            ], "+44", 10),
            ("CA", [
                ("Toronto",    "ON", "M5H 2N2"),
                ("Vancouver",  "BC", "V6B 1A1"),
                ("Montreal",   "QC", "H3A 1A1"),
                ("Calgary",    "AB", "T2P 1A1"),
                ("Ottawa",     "ON", "K1A 1A1"),
                ("Edmonton",   "AB", "T5J 1A1"),
            ], "+1", 10),
            ("AU", [
                ("Sydney",     "NSW", "2000"),
                ("Melbourne",  "VIC", "3000"),
                ("Brisbane",   "QLD", "4000"),
                ("Perth",      "WA",  "6000"),
                ("Adelaide",   "SA",  "5000"),
                ("Gold Coast", "QLD", "4217"),
            ], "+61", 9),
            ("DE", [
                ("Berlin",    "BE", "10115"),
                ("Munich",    "BY", "80331"),
                ("Hamburg",   "HH", "20095"),
                ("Cologne",   "NW", "50667"),
                ("Frankfurt", "HE", "60311"),
                ("Stuttgart", "BW", "70173"),
            ], "+49", 10),
            ("SG", [
                ("Singapore", "Singapore", "018989"),
                ("Singapore", "Singapore", "048583"),
                ("Singapore", "Singapore", "238801"),
            ], "+65", 8),
            ("AE", [
                ("Dubai",     "Dubai",     "00000"),
                ("Abu Dhabi", "Abu Dhabi", "00000"),
                ("Sharjah",   "Sharjah",   "00000"),
            ], "+971", 9),
        ]

        # Pick a random non-India country
        country_code, locations, phone_prefix, phone_len = random.choice(countries)
        city, state, postcode = random.choice(locations)

        # Street names
        street_names = [
            "Main St", "High St", "King St", "Queen St", "Church St",
            "Park Ave", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd",
            "Elm St", "Washington St", "Lincoln Ave", "Victoria Rd",
            "Albert St", "James St", "George St", "Mill Lane", "Station Rd",
        ]

        # Generate values
        first = existing.get("billing_first_name") or random.choice(first_names)
        last = existing.get("billing_last_name") or random.choice(last_names)
        street = random.choice(street_names)
        house_num = random.randint(1, 999)
        phone_digits = "".join(random.choices(string.digits, k=phone_len))
        phone = f"{phone_prefix}{phone_digits}"
        email_handle = (first + last).lower().replace(" ", "") + str(random.randint(100, 9999))
        email = f"{email_handle}@gmail.com"

        # Only fill fields that aren't already in `existing`
        if "billing_first_name" not in existing:
            out["billing_first_name"] = first
        if "billing_last_name" not in existing:
            out["billing_last_name"] = last
        if "billing_email" not in existing:
            out["billing_email"] = email
        if "billing_phone" not in existing:
            out["billing_phone"] = phone
        if "billing_address_1" not in existing:
            out["billing_address_1"] = f"{house_num} {street}"
        if "billing_city" not in existing:
            out["billing_city"] = city
        if "billing_state" not in existing:
            out["billing_state"] = state
        if "billing_postcode" not in existing:
            out["billing_postcode"] = postcode
        if "billing_country" not in existing:
            out["billing_country"] = country_code

        logger.info(
            f"Generated billing: {first} {last}, {city}, {country_code} "
            f"{postcode}, {email}, {phone}"
        )
        return out

    async def fill_and_submit_checkout(self, billing: dict | None = None, nonce: str = "") -> dict:
        """
        Fill billing fields and submit checkout via Playwright.

        billing: dict of billing fields, OR None to auto-detect existing values
                on the page and fill missing ones with random data.
        nonce:   checkout nonce from get_checkout_page(). May be empty — we'll
                try to re-extract it from the page.
        """
        await self.on_status("📦 Loading checkout form...")

        try:
            # If we're not already on the checkout page, navigate to it.
            if "checkout" not in (self.page.url or "").lower():
                ok = await self._ensure_page_loaded(CHECKOUT_URL)
                if not ok:
                    return {"result": "failure", "messages": "Could not load checkout page", "redirect": ""}

            # ----------------------------------------------------------------
            # STEP 1: Read existing (pre-filled) billing values from the form.
            # ----------------------------------------------------------------
            existing_billing = await self._read_existing_billing_fields()
            logger.info(f"Pre-filled billing fields: {list(existing_billing.keys())}")

            # ----------------------------------------------------------------
            # STEP 2: Decide which billing dict to use.
            #   - If caller passed a billing dict → merge with existing
            #     (caller's values override existing, existing overrides random)
            #   - If caller passed None → use existing + generate missing
            # ----------------------------------------------------------------
            if billing is None:
                # Auto mode: use existing values, fill gaps with random data
                needed = self._generate_random_billing(existing_billing)
                merged = {**needed, **existing_billing}  # existing wins
                if existing_billing:
                    await self.on_status(
                        f"📋 Using pre-filled billing ({len(existing_billing)} fields). "
                        f"Filling {len(needed)} missing..."
                    )
                else:
                    await self.on_status("📋 Generating billing details automatically...")
                billing = merged
            else:
                # Caller provided values — merge: caller > existing > random
                needed = self._generate_random_billing({**billing, **existing_billing})
                billing = {**needed, **existing_billing, **billing}

            logger.info(f"Final billing fields to fill: {list(billing.keys())}")

            # ----------------------------------------------------------------
            # STEP 3: Fill the form fields.
            # ----------------------------------------------------------------
            await self.on_status("📝 Filling billing form...")

            # Text input fields — use fill()
            text_field_map = {
                "billing_first_name": "billing_first_name",
                "billing_last_name": "billing_last_name",
                "billing_email": "billing_email",
                "billing_phone": "billing_phone",
                "billing_address_1": "billing_address_1",
                "billing_city": "billing_city",
                "billing_postcode": "billing_postcode",
            }

            for key, selector_id in text_field_map.items():
                if key in billing and billing[key]:
                    field = self.page.locator(f"#{selector_id}")
                    if await field.count() > 0:
                        await field.fill(billing[key])
                        await asyncio.sleep(0.2)

            # billing_state is usually a <select> for IN/US/etc, but a text
            # input for countries without a predefined state list. Try both.
            state_value = billing.get("billing_state", "")
            if state_value:
                state_select = self.page.locator("#billing_state")
                if await state_select.count() > 0:
                    tag = await state_select.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        # Try exact value match first, then label-based match
                        try:
                            await state_select.select_option(value=state_value)
                        except Exception:
                            try:
                                await state_select.select_option(label=state_value)
                            except Exception:
                                # Last resort: try matching by first 2 chars (state code)
                                try:
                                    await state_select.select_option(value=state_value.upper()[:2])
                                except Exception as e:
                                    logger.warning(f"Could not select state '{state_value}': {e}")
                    else:
                        # It's an input — use fill
                        try:
                            await state_select.fill(state_value)
                        except Exception as e:
                            logger.warning(f"Could not fill state input: {e}")

            # Select country — default to IN if not provided (gplgames is India-focused)
            country = billing.get("billing_country", "IN")
            country_select = self.page.locator("#billing_country")
            if await country_select.count() > 0:
                try:
                    await country_select.select_option(country)
                    await asyncio.sleep(0.5)  # State dropdown may reload on country change
                except Exception as e:
                    logger.warning(f"Could not select country '{country}': {e}")
                    # If country select failed, try IN as a fallback
                    if country != "IN":
                        try:
                            await country_select.select_option("IN")
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass

            # Ensure RazorPay is selected
            rp_radio = self.page.locator('input#payment_method_razorpay[value="razorpay"]')
            if await rp_radio.count() > 0:
                await rp_radio.check(force=True)
                await asyncio.sleep(0.5)

            # Check terms
            terms = self.page.locator('input[name="terms"]')
            if await terms.count() > 0:
                await terms.check(force=True)
                await asyncio.sleep(0.3)

            await self.on_status("📦 Submitting checkout...")

            place_order = self.page.locator('button#place_order')
            if await place_order.count() > 0:
                await place_order.click()
            else:
                await self.page.evaluate(
                    "() => { document.querySelector('form.checkout').submit(); }"
                )

            await asyncio.sleep(5)

            current_url = self.page.url
            content = await self._safe_get_content()

            if "order-received" in current_url:
                return {"result": "success", "redirect": current_url, "messages": ""}

            # Check for RazorPay modal in any frame
            has_rzp = False
            for frame in self.page.frames:
                if "razorpay" in (frame.url or "").lower():
                    has_rzp = True
                    break

            if has_rzp:
                return {
                    "result": "success",
                    "redirect": "",
                    "messages": "RAZORPAY_MODAL_OPEN",
                    "current_url": current_url,
                }

            # Check for WooCommerce errors
            errors = re.findall(
                r'<ul[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</ul>',
                content, re.DOTALL | re.I
            )
            if errors:
                error_text = re.sub(r"<[^>]*>", "", errors[0]).strip()
                return {"result": "failure", "messages": error_text, "redirect": ""}

            notices = re.findall(
                r'<div[^>]*class="[^"]*woocommerce-notice[^"]*"[^>]*>(.*?)</div>',
                content, re.DOTALL | re.I
            )
            if notices:
                notice_text = re.sub(r"<[^>]*>", "", notices[0]).strip()
                return {"result": "failure", "messages": notice_text, "redirect": ""}

            return {
                "result": "unknown",
                "messages": f"Checkout submitted. URL: {current_url}",
                "redirect": current_url,
            }

        except Exception as e:
            logger.error(f"Checkout submit error: {e}")
            return {"result": "failure", "messages": str(e), "redirect": ""}

    # ================================================================
    # RAZORPAY PAYMENT
    # ================================================================
    async def process_razorpay_payment(
        self, cc_number: str, cc_expiry: str, cc_cvv: str, checkout_result: dict
    ) -> dict:
        """
        Handle RazorPay payment in the already-open browser.
        CC details exist ONLY in memory and are wiped immediately.
        """
        self._cc = CreditCard(number=cc_number, expiry=cc_expiry, cvv=cc_cvv)
        await self.on_status("🔒 Processing RazorPay payment...")

        try:
            messages = checkout_result.get("messages", "")

            if "RAZORPAY_MODAL_OPEN" in messages:
                return await _fill_razorpay_modal(self)

            if checkout_result.get("result") == "success" and checkout_result.get("redirect"):
                return {
                    "status": "success",
                    "message": "Payment completed successfully!",
                    "url": checkout_result["redirect"],
                }

            # Wait and check for RazorPay frame
            await asyncio.sleep(3)
            for frame in self.page.frames:
                if "razorpay" in (frame.url or "").lower():
                    return await _fill_razorpay_in_frame(self, frame)

            return {
                "status": "needs_review",
                "message": f"Could not find RazorPay modal. {messages[:200]}",
            }

        except Exception as e:
            logger.error(f"Payment error: {e}")
            return {"status": "error", "message": f"Payment error: {str(e)[:200]}"}
        finally:
            if self._cc:
                self._cc.secure_wipe()
                self._cc = None


# ================================================================
# RAZORPAY MODAL HELPERS
# ================================================================
async def _dump_razorpay_frames(engine: SiteEngine) -> None:
    """Diagnostic: log every frame URL and the input elements inside it.
    Helps debug 'Could not fill card number' errors when RazorPay changes
    their iframe structure."""
    logger.info(f"=== RAZORPAY FRAME DUMP (total frames: {len(engine.page.frames)}) ===")
    for i, frame in enumerate(engine.page.frames):
        url = frame.url or "(no url)"
        logger.info(f"  Frame[{i}] URL: {url}")
        if "razorpay" not in url.lower():
            continue
        try:
            inputs = await frame.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll('input, button, [role="textbox"], iframe, [role="radio"], label, div')) {
                        // Only log elements with useful info to avoid spam
                        const txt = (el.textContent || '').trim().substring(0, 60);
                        if (!el.name && !el.id && !el.placeholder && !el.type && !txt && !el.getAttribute('aria-label')) continue;
                        out.push({
                            tag: el.tagName.toLowerCase(),
                            type: el.type || '',
                            name: el.name || '',
                            id: el.id || '',
                            placeholder: el.placeholder || '',
                            value: el.value ? '(has value)' : '',
                            visible: el.offsetParent !== null,
                            ariaLabel: el.getAttribute('aria-label') || '',
                            autocomplete: el.autocomplete || '',
                            text: txt,
                            role: el.getAttribute('role') || '',
                            cls: (el.className || '').toString().substring(0, 80),
                        });
                    }
                    return out;
                }"""
            )
            for inp in inputs:
                logger.info(f"    {inp}")
        except Exception as e:
            logger.info(f"    (could not read frame: {e})")


async def _click_card_payment_method(engine: SiteEngine) -> bool:
    """
    On RazorPay's initial screen, click the 'Card' payment method option
    to reveal the card number/expiry/CVV input fields.

    Returns True if the Card option was found and clicked.
    """
    razorpay_frames = [f for f in engine.page.frames if "razorpay" in (f.url or "").lower()]
    logger.info(f"Looking for 'Card' payment method in {len(razorpay_frames)} frame(s)")

    for frame in razorpay_frames:
        try:
            # Strategy 1: Click by text "Card" (most reliable)
            # RazorPay shows payment methods as clickable labels/buttons
            for text in ["Card", "CARD", "Credit Card", "Debit Card", "Credit/Debit Card"]:
                try:
                    card_btn = frame.locator(f'text="{text}"')
                    count = await card_btn.count()
                    if count > 0:
                        logger.info(f"Found '{text}' payment method ({count} matches), clicking first visible...")
                        for i in range(count):
                            try:
                                el = card_btn.nth(i)
                                if await el.is_visible(timeout=1500):
                                    await el.click()
                                    logger.info(f"Clicked '{text}' payment method (match #{i})")
                                    await asyncio.sleep(2)  # Wait for card form to slide in
                                    return True
                            except Exception:
                                continue
                except Exception:
                    pass

            # Strategy 2: Click the radio button whose label/sibling says "Card"
            try:
                # Look for labels containing "Card" and click them
                card_label = frame.locator('label:has-text("Card"), [role="radio"]:has-text("Card")')
                count = await card_label.count()
                if count > 0:
                    for i in range(count):
                        try:
                            el = card_label.nth(i)
                            if await el.is_visible(timeout=1500):
                                await el.click()
                                logger.info(f"Clicked Card label/radio (match #{i})")
                                await asyncio.sleep(2)
                                return True
                        except Exception:
                            continue
            except Exception:
                pass

            # Strategy 3: Click by aria-label
            try:
                card_aria = frame.locator('[aria-label*="Card" i], [aria-label*="card" i]')
                count = await card_aria.count()
                if count > 0:
                    for i in range(count):
                        try:
                            el = card_aria.nth(i)
                            if await el.is_visible(timeout=1500):
                                await el.click()
                                logger.info(f"Clicked Card aria-labeled element (match #{i})")
                                await asyncio.sleep(2)
                                return True
                        except Exception:
                            continue
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"Error searching for Card method in frame: {e}")
            continue

    logger.error("Could not find 'Card' payment method in any RazorPay frame")
    return False


async def _find_card_input_in_frame(frame, field_type: str = "number") -> any:
    """
    Find a card input element in a RazorPay frame using multiple strategies.

    Modern RazorPay uses separate iframes per field (card_number, card_expiry,
    card_cvv). Each iframe has a single <input> with no name/id, or with
    field-specific attributes.

    field_type: 'number', 'expiry', or 'cvv'
    """
    # Strategy 1: Traditional named inputs (old + new RazorPay UI)
    # Old UI uses bracket notation: card[number], card[expiry], card[cvv]
    # New Tata Neu UI uses dot notation: card.number, card.expiry, card.cvv
    name_patterns = {
        "number":  ["card[number]", "card_number", "cardNumber", "cc-number",
                    "card.number", "card.number.input", "card-number-input"],
        "expiry":  ["card[expiry]", "card_expiry", "cardExpiry", "cc-exp",
                    "card.expiry", "card.expiry.input", "card-expiry-input"],
        "cvv":     ["card[cvv]", "card_cvv", "cardCvv", "cc-csc", "cc-cvv",
                    "card.cvv", "card.cvv.input", "card-cvv-input"],
    }
    for name in name_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'input[name="{name}"]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Strategy 2: ID-based selectors
    id_patterns = {
        "number":  ["card-number", "card_number", "cardNumber", "cc-number"],
        "expiry":  ["card-expiry", "card_expiry", "cardExpiry", "cc-exp"],
        "cvv":     ["card-cvv", "card_cvv", "cardCvv", "cc-cvv", "cvv"],
    }
    for id_val in id_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'#{id_val}')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Strategy 3: Autocomplete attribute (modern RazorPay uses this)
    autocomplete_patterns = {
        "number":  ["cc-number", "cc-csc"],  # cc-csc is CVV, filter below
        "expiry":  ["cc-exp"],
        "cvv":     ["cc-csc", "cc-cvv"],
    }
    for ac in autocomplete_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'input[autocomplete="{ac}"]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Strategy 4: aria-label based
    aria_patterns = {
        "number":  ["card number", "card_number", "Card Number"],
        "expiry":  ["expiry", "mm/yy", "MM/YY", "Expiry"],
        "cvv":     ["cvv", "cvc", "CVV", "CVC"],
    }
    for aria in aria_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'[aria-label*="{aria}" i]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Strategy 5: If the frame URL contains the field type, grab the first
    # visible input (modern RazorPay: one iframe per field, URL contains hint)
    try:
        url = (frame.url or "").lower()
        url_hints = {
            "number":  ["card_number", "card-number", "number"],
            "expiry":  ["card_expiry", "card-expiry", "expiry", "exp"],
            "cvv":     ["card_cvv", "card-cvv", "cvv", "csc", "security"],
        }
        hints = url_hints.get(field_type, [])
        if any(h in url for h in hints):
            loc = frame.locator('input, [role="textbox"]')
            count = await loc.count()
            for j in range(count):
                try:
                    el = loc.nth(j)
                    if await el.is_visible(timeout=1000):
                        return el
                except Exception:
                    continue
    except Exception:
        pass

    # Strategy 6: Placeholder-based (exact placeholders seen in Tata Neu UI)
    placeholder_patterns = {
        "number":  ["card number", "0000 0000", "1234 5678", "•", "0000"],
        "expiry":  ["mm / yy", "mm/yy", "MM/YY", "MMYY", "expiry", "mm / yy"],
        "cvv":     ["cvv", "cvc", "123", "•••"],
    }
    for ph in placeholder_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'input[placeholder*="{ph}" i]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Strategy 7: aria-label based (Tata Neu UI uses aria-label="Card Number", "MM / YY", "CVV")
    aria_label_patterns = {
        "number":  ["card number", "Card Number"],
        "expiry":  ["mm / yy", "MM / YY", "expiry", "Expiry"],
        "cvv":     ["cvv", "CVV", "cvc", "CVC", "security code"],
    }
    for al in aria_label_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'input[aria-label="{al}"], input[aria-label="{al.upper()}"]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    return None


async def _fill_razorpay_modal(engine: SiteEngine) -> dict:
    """Fill RazorPay modal that's already open in the page.

    Handles both old (single iframe with all fields) and new (separate
    iframes per card field) RazorPay UI layouts.

    RazorPay's flow:
    1. Modal opens with payment method selection (Card, UPI, Netbanking, etc.)
       — only radio buttons + labels are visible, NO card input fields
    2. User clicks "Card" → card input fields slide in (may be in new iframes)
    3. User fills card number, expiry, CVV
    4. User clicks Pay
    """
    await engine.on_status("💳 RazorPay modal found! Entering card details...")
    cc = engine._cc

    # Wait for modal to fully load — RazorPay loads iframes asynchronously
    await asyncio.sleep(3)

    # Initial diagnostic dump (shows payment method selection state)
    await _dump_razorpay_frames(engine)

    # ------------------------------------------------------------------
    # STEP 1: Click the "Card" payment method to reveal card input fields.
    # On RazorPay's initial screen, you see radio buttons for Card/UPI/etc
    # but NO card input fields. We must click "Card" first.
    # ------------------------------------------------------------------
    await engine.on_status("💳 Selecting Card payment method...")

    # Check if card input fields already exist (skip Card click if so)
    card_input_already_visible = False
    for frame in engine.page.frames:
        if "razorpay" not in (frame.url or "").lower():
            continue
        if await _find_card_input_in_frame(frame, "number") is not None:
            card_input_already_visible = True
            logger.info("Card input already visible — skipping Card method click")
            break

    if not card_input_already_visible:
        clicked = await _click_card_payment_method(engine)
        if not clicked:
            # Maybe the modal already shows card fields but we couldn't detect
            # them, OR the UI is different. Try to fill anyway.
            logger.warning("Could not click Card payment method — trying to fill anyway")

        # Wait for card form to load (new iframes may be created)
        await asyncio.sleep(3)

        # Re-dump frames AFTER clicking Card — this shows the actual card
        # input fields (which appear in new iframes after Card is selected)
        logger.info("=== RAZORPAY FRAME DUMP AFTER CLICKING CARD ===")
        await _dump_razorpay_frames(engine)

    # ------------------------------------------------------------------
    # STEP 2: Fill the card number, expiry, CVV fields.
    # ------------------------------------------------------------------
    filled_card = False
    filled_exp = False
    filled_cvv = False

    razorpay_frames = [f for f in engine.page.frames if "razorpay" in (f.url or "").lower()]
    logger.info(f"Found {len(razorpay_frames)} RazorPay frame(s) for filling")

    for frame in razorpay_frames:
        if filled_card and filled_exp and filled_cvv:
            break
        try:
            # Try to fill card number
            if not filled_card:
                card_input = await _find_card_input_in_frame(frame, "number")
                if card_input is not None:
                    try:
                        await card_input.click(timeout=3000)
                        await asyncio.sleep(0.3)
                        await card_input.fill("")
                        await card_input.type(cc.number, delay=30)
                        await asyncio.sleep(0.5)
                        filled_card = True
                        logger.info(f"Card number filled in frame: {frame.url}")
                    except Exception as e:
                        logger.warning(f"Card number fill attempt failed: {e}")

            # Try to fill expiry
            if not filled_exp:
                exp_input = await _find_card_input_in_frame(frame, "expiry")
                if exp_input is not None:
                    try:
                        await exp_input.click(timeout=3000)
                        await asyncio.sleep(0.3)
                        await exp_input.fill("")
                        # RazorPay expects MM/YY format
                        exp_val = cc.expiry if "/" in cc.expiry else f"{cc.expiry[:2]}/{cc.expiry[2:]}"
                        await exp_input.type(exp_val, delay=30)
                        await asyncio.sleep(0.5)
                        filled_exp = True
                        logger.info(f"Expiry filled in frame: {frame.url}")
                    except Exception as e:
                        logger.warning(f"Expiry fill attempt failed: {e}")

            # Try to fill CVV
            if not filled_cvv:
                cvv_input = await _find_card_input_in_frame(frame, "cvv")
                if cvv_input is not None:
                    try:
                        await cvv_input.click(timeout=3000)
                        await asyncio.sleep(0.3)
                        await cvv_input.fill("")
                        await cvv_input.type(cc.cvv, delay=30)
                        await asyncio.sleep(0.5)
                        filled_cvv = True
                        logger.info(f"CVV filled in frame: {frame.url}")
                    except Exception as e:
                        logger.warning(f"CVV fill attempt failed: {e}")

        except Exception as e:
            logger.error(f"Error processing RazorPay frame {frame.url}: {e}")
            continue

    logger.info(f"Fill status: card={filled_card}, expiry={filled_exp}, cvv={filled_cvv}")

    if not filled_card:
        # Card number is mandatory — can't proceed without it
        return {
            "status": "error",
            "message": (
                "Could not find the card number field in RazorPay even after "
                "clicking the Card payment method. RazorPay may have updated "
                "their UI. Please check logs/bot_full.log for the frame dump."
            ),
        }

    # Retry pass for expiry/CVV if not filled (iframes may still be loading)
    if not filled_exp or not filled_cvv:
        logger.warning(f"Missing: expiry={not filled_exp}, cvv={not filled_cvv}")
        await asyncio.sleep(2)
        # Refresh frame list (new iframes may have appeared)
        razorpay_frames = [f for f in engine.page.frames if "razorpay" in (f.url or "").lower()]
        for frame in razorpay_frames:
            if not filled_exp:
                exp_input = await _find_card_input_in_frame(frame, "expiry")
                if exp_input is not None:
                    try:
                        await exp_input.click(timeout=3000)
                        await exp_input.fill("")
                        exp_val = cc.expiry if "/" in cc.expiry else f"{cc.expiry[:2]}/{cc.expiry[2:]}"
                        await exp_input.type(exp_val, delay=30)
                        filled_exp = True
                        logger.info(f"Expiry filled on retry in frame: {frame.url}")
                    except Exception:
                        pass
            if not filled_cvv:
                cvv_input = await _find_card_input_in_frame(frame, "cvv")
                if cvv_input is not None:
                    try:
                        await cvv_input.click(timeout=3000)
                        await cvv_input.fill("")
                        await cvv_input.type(cc.cvv, delay=30)
                        filled_cvv = True
                        logger.info(f"CVV filled on retry in frame: {frame.url}")
                    except Exception:
                        pass

    await engine.on_status("⏳ Submitting payment to RazorPay...")

    # ------------------------------------------------------------------
    # STEP 3: Inject JS to capture RazorPay success/failure events.
    # RazorPay's handler flow fires `handler(response)` on success and
    # `rzp1.on('payment.failed', ...)` on decline. We register listeners
    # BEFORE clicking Pay so we capture the authoritative event.
    # ------------------------------------------------------------------
    await _inject_razorpay_event_listeners(engine)

    # Click pay button — search ALL frames, not just the ones we filled
    # RazorPay UI variants use different button text: "Pay", "Pay Now", "Continue"
    pay_clicked = False
    for frame in engine.page.frames:
        if "razorpay" not in (frame.url or "").lower():
            continue
        for selector in [
            # Class/ID based
            'button[class*="pay"]',
            'button#pay-button',
            '#checkout-pay',
            'button.btn-primary',
            # Type + name based (Tata Neu uses name="button" type="submit")
            'button[type="submit"][name="button"]',
            'button[type="submit"]:not([class*="hidden"])',
            # Text based — "Pay", "Pay Now", "Continue" (Tata Neu uses "Continue")
            'button:has-text("Pay")',
            'button:has-text("Pay Now")',
            'button:has-text("Pay now")',
            'button:has-text("CONTINUE")',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Submit")',
            'input[type="submit"][value*="Pay"]',
            'input[type="submit"][value*="Continue"]',
        ]:
            try:
                pay_btn = frame.locator(selector)
                if await pay_btn.count() > 0:
                    # Try each match — first visible one
                    for i in range(min(await pay_btn.count(), 5)):
                        try:
                            el = pay_btn.nth(i)
                            if await el.is_visible(timeout=2000):
                                # Skip offer/promo buttons (they contain "Offers", "View all", "Refresh", "Using as")
                                try:
                                    btn_text = await el.inner_text(timeout=500)
                                    btn_text_lower = btn_text.lower().strip()
                                    skip_phrases = ["offers", "view all", "refresh", "using as", "privacy", "edit preferences"]
                                    if any(skip in btn_text_lower for skip in skip_phrases):
                                        logger.debug(f"Skipping non-pay button: {btn_text!r}")
                                        continue
                                except Exception:
                                    pass
                                await el.click()
                                pay_clicked = True
                                logger.info(f"Pay button clicked in frame {frame.url} with selector {selector} (match #{i})")
                                break
                        except Exception:
                            continue
                    if pay_clicked:
                        break
            except Exception:
                continue
        if pay_clicked:
            break

    if not pay_clicked:
        logger.error("Could not find/click RazorPay pay button in any frame")
        # Dump all visible submit buttons for debugging
        try:
            for frame in engine.page.frames:
                if "razorpay" not in (frame.url or "").lower():
                    continue
                btns = await frame.evaluate(
                    """() => {
                        const out = [];
                        for (const b of document.querySelectorAll('button, input[type="submit"]')) {
                            if (b.offsetParent !== null) {
                                out.push({
                                    tag: b.tagName,
                                    type: b.type,
                                    name: b.name,
                                    text: (b.textContent || b.value || '').trim().substring(0, 50),
                                    cls: (b.className || '').substring(0, 60),
                                });
                            }
                        }
                        return out;
                    }"""
                )
                if btns:
                    logger.info(f"Visible buttons in {frame.url[:80]}: {btns}")
        except Exception:
            pass

    # Wait for payment to process — RazorPay needs time to call the bank,
    # get a response, and either close the modal (success) or show an error
    await asyncio.sleep(10)

    # Check if our injected listeners captured any events
    captured_events = await _read_captured_razorpay_events(engine)
    logger.info(f"Captured RazorPay events: {captured_events}")

    # Parse the payment result from the final page + captured events
    # Pass pay_clicked so the parser knows whether payment was actually submitted
    result = await _parse_payment_result(engine, captured_events, pay_clicked)
    return result


async def _inject_razorpay_event_listeners(engine: SiteEngine) -> None:
    """
    Inject JS into the main page to capture RazorPay checkout events.

    RazorPay's Standard Checkout fires:
    - handler(response) on success → response has razorpay_payment_id,
      razorpay_order_id, razorpay_signature
    - rzp1.on('payment.failed', ...) on decline → response.error has
      code, description, source, step, reason, metadata

    We store these in window.__rzp_events so we can read them later.
    """
    try:
        await engine.page.evaluate(
            """() => {
                // Storage for captured events
                window.__rzp_events = {
                    success: null,
                    failure: null,
                    dismiss: null,
                };

                // Try to find the RazorPay instance and hook into its events.
                // RazorPay creates a global `rzp1` or similar instance.
                const hookRzp = (rzp) => {
                    try {
                        if (rzp && typeof rzp.on === 'function') {
                            rzp.on('payment.failed', function(resp) {
                                window.__rzp_events.failure = {
                                    code: resp && resp.error && resp.error.code || '',
                                    description: resp && resp.error && resp.error.description || '',
                                    source: resp && resp.error && resp.error.source || '',
                                    step: resp && resp.error && resp.error.step || '',
                                    reason: resp && resp.error && resp.error.reason || '',
                                    order_id: resp && resp.error && resp.error.metadata && resp.error.metadata.order_id || '',
                                    payment_id: resp && resp.error && resp.error.metadata && resp.error.metadata.payment_id || '',
                                    timestamp: Date.now(),
                                };
                                console.log('[RZP] payment.failed captured:', window.__rzp_events.failure);
                            });
                        }
                        if (rzp && typeof rzp.on === 'function') {
                            rzp.on('payment.success' in rzp ? 'payment.success' : 'payment.authorized', function(resp) {
                                window.__rzp_events.success = {
                                    payment_id: resp && (resp.razorpay_payment_id || resp.id) || '',
                                    order_id: resp && (resp.razorpay_order_id || resp.order_id) || '',
                                    signature: resp && resp.razorpay_signature || '',
                                    timestamp: Date.now(),
                                };
                                console.log('[RZP] payment.success captured:', window.__rzp_events.success);
                            });
                        }
                        // Modal dismiss
                        if (rzp && rzp.options && typeof rzp.options.modal === 'object') {
                            const origDismiss = rzp.options.modal.ondismiss;
                            rzp.options.modal.ondismiss = function(reason) {
                                window.__rzp_events.dismiss = {
                                    reason: typeof reason === 'string' ? reason : 'unknown',
                                    timestamp: Date.now(),
                                };
                                console.log('[RZP] modal dismissed:', reason);
                                if (typeof origDismiss === 'function') return origDismiss(reason);
                            };
                        }
                        return true;
                    } catch (e) {
                        console.log('[RZP] hook error:', e);
                        return false;
                    }
                };

                // Try common global variable names
                const candidates = ['rzp1', 'rzp', 'razorpay', 'Razorpay'];
                for (const name of candidates) {
                    if (window[name]) {
                        if (hookRzp(window[name])) {
                            console.log('[RZP] hooked instance:', name);
                            return;
                        }
                    }
                }

                // If no global instance found, watch for it via Object.defineProperty
                let hooked = false;
                for (const name of candidates) {
                    let _val = window[name];
                    try {
                        Object.defineProperty(window, name, {
                            configurable: true,
                            get() { return _val; },
                            set(v) {
                                _val = v;
                                if (!hooked && v) {
                                    hooked = hookRzp(v);
                                    if (hooked) console.log('[RZP] hooked late instance:', name);
                                }
                            },
                        });
                    } catch (e) {}
                }

                console.log('[RZP] event listeners injected');
            }"""
        )
        logger.info("RazorPay event listeners injected")
    except Exception as e:
        logger.warning(f"Could not inject RazorPay event listeners: {e}")


async def _read_captured_razorpay_events(engine: SiteEngine) -> dict:
    """Read events captured by _inject_razorpay_event_listeners."""
    try:
        events = await engine.page.evaluate(
            "() => window.__rzp_events || {success: null, failure: null, dismiss: null}"
        )
        return events or {"success": null, "failure": null, "dismiss": null}
    except Exception as e:
        logger.warning(f"Could not read captured RazorPay events: {e}")
        return {"success": null, "failure": null, "dismiss": null}


# RazorPay error reason codes → user-friendly messages
# Based on official RazorPay error documentation (~90 codes)
RZP_REASON_MESSAGES = {
    # Card declines
    "card_declined":              "Card was declined by the bank",
    "payment_declined":           "Payment was declined",
    "payment_declined_due_to_high_traffic": "Payment declined due to high traffic — try again",
    "debit_declined":             "Debit transaction was declined",
    "credit_limit_exceeded":      "Credit limit exceeded on the card",
    "credit_not_permitted":       "Credit transaction not permitted on this card",
    "authorisation_declined_by_psp": "Authorization declined by payment processor",
    "issuer_technical_error":     "Card-issuing bank had a technical error",
    "gateway_technical_error":    "Payment gateway had a technical error",
    "invalid_response_from_gateway": "Invalid response from payment gateway",
    # Card detail errors
    "card_expired":               "Card has expired",
    "card_number_invalid":        "Invalid card number",
    "card_type_invalid":          "This card type is not supported",
    "card_not_enrolled":          "Card is not enrolled for this transaction",
    "incorrect_card_details":     "Incorrect card details entered",
    "incorrect_card_expiry_date": "Incorrect card expiry date",
    "incorrect_cardholder_name":  "Incorrect cardholder name",
    "incorrect_cvv":              "Incorrect CVV",
    "debit_instrument_blocked":   "This debit instrument is blocked",
    # Authentication failures
    "authentication_failed":      "3D Secure authentication failed",
    "incorrect_otp":              "Incorrect OTP entered",
    "otp_attempts_exceeded":      "Too many wrong OTP attempts",
    "otp_expired":                "OTP expired — try again",
    "payment_authentication":     "Payment authentication failed",
    # Funds / limits
    "insufficient_funds":         "Insufficient funds in account",
    "transaction_daily_limit_exceeded": "Daily transaction limit exceeded",
    "transaction_limit_exceeded": "Transaction limit exceeded",
    # Processing errors
    "payment_failed":             "Payment processing failed",
    "payment_cancelled":          "Payment was cancelled",
    "payment_risk_check_failed":  "Payment blocked by risk check",
    "payment_timed_out":          "Payment timed out — try again",
    "payment_pending":            "Payment is pending at the bank",
    "payment_session_expired":    "Payment session expired — try again",
    "request_timed_out":          "Request timed out",
    "server_error":               "RazorPay server error — try again",
    # Bank errors
    "bank_technical_error":       "Bank had a technical error",
    "bank_account_invalid":       "Invalid bank account",
    # Validation
    "input_validation_failed":    "Input validation failed",
    "invalid_order_id":           "Invalid order ID",
    "order_already_paid":         "Order was already paid",
    "live_mode_not_enabled":      "Live mode not enabled on account",
}

# Error code → status mapping
RZP_ERROR_CODE_STATUS = {
    "BAD_REQUEST_ERROR": "failed",
    "GATEWAY_ERROR":     "failed",
    "SERVER_ERROR":      "failed",
}


async def _parse_payment_result(engine: SiteEngine, captured_events: dict = None, pay_clicked: bool = True) -> dict:
    """
    Parse the final page after payment to extract order/payment details AND
    determine the authoritative payment status.

    Uses multiple detection signals (in priority order):
    1. Captured RazorPay JS events (most authoritative — from handler/payment.failed)
    2. RazorPay checkout modal state (still open = declined, closed = approved)
    3. Final URL patterns (order-received = success)
    4. Page content markers (thank you, payment failed, etc.)
    5. RazorPay payment ID presence (pay_XXX = success signal)
    6. WooCommerce error/notices

    Extracts: order ID, order key, payment ID, amount, status, decline reason.

    pay_clicked: whether the Pay/Continue button was actually clicked. If False,
                 "modal still open" should NOT be interpreted as a decline —
                 it means payment was never submitted.
    """
    if captured_events is None:
        captured_events = {"success": None, "failure": None, "dismiss": None}

    result = {
        "status": "unknown",
        "status_text": "",
        "order_id": "",
        "order_key": "",
        "payment_id": "",
        "amount": "",
        "currency": "",
        "url": "",
        "message": "",
        "decline_reason": "",
        "decline_code": "",
        "error_source": "",
        "error_step": "",
    }

    try:
        final_url = engine.page.url or ""
        result["url"] = final_url
        logger.info(f"Parsing payment result from URL: {final_url}")

        # Extract order ID + key from URL
        order_match = re.search(r'order-(?:received|pay)/(\d+)', final_url, re.I)
        if order_match:
            result["order_id"] = order_match.group(1)

        key_match = re.search(r'key=(wc_order_\w+)', final_url, re.I)
        if key_match:
            result["order_key"] = key_match.group(1)

        # Also check URL query params for razorpay_payment_id (redirect flow)
        rzp_url_match = re.search(r'[?&]razorpay_payment_id=(pay_\w+)', final_url, re.I)
        if rzp_url_match and not result["payment_id"]:
            result["payment_id"] = rzp_url_match.group(1)

        # Get page content + visible text
        content = await engine._safe_get_content()
        try:
            text = await engine.page.inner_text("body")
        except Exception:
            text = content

        text_lower = text.lower()
        content_lower = content.lower()

        # Extract RazorPay payment ID from page (pay_XXX format, 10-14 chars after prefix)
        if not result["payment_id"]:
            rzp_match = re.search(r'\b(pay_[A-Za-z0-9]{10,14})\b', content)
            if rzp_match:
                result["payment_id"] = rzp_match.group(1)
        if not result["payment_id"]:
            rzp_match = re.search(r'\b(pay_[A-Za-z0-9]{10,14})\b', text)
            if rzp_match:
                result["payment_id"] = rzp_match.group(1)

        # Extract amount (₹, Rs, INR, $, etc.)
        amount_match = re.search(r'(?:₹|Rs\.?\s*|INR\s*|\$|€|£)\s*([\d,]+\.?\d*)', text)
        if amount_match:
            result["amount"] = amount_match.group(1)

        # Extract WooCommerce order number from text (if not from URL)
        if not result["order_id"]:
            order_num_match = re.search(r'Order\s*(?:No\.?|Number|#)\s*:?\s*(\d+)', text, re.I)
            if order_num_match:
                result["order_id"] = order_num_match.group(1)

        # ==================================================================
        # SIGNAL 1: Captured RazorPay JS events (MOST AUTHORITATIVE)
        # ==================================================================
        if captured_events.get("failure"):
            # payment.failed event fired — definitive decline
            fail = captured_events["failure"]
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            result["decline_code"] = fail.get("code", "")
            result["decline_reason"] = fail.get("reason", "")
            result["error_source"] = fail.get("source", "")
            result["error_step"] = fail.get("step", "")
            if fail.get("payment_id"):
                result["payment_id"] = result["payment_id"] or fail["payment_id"]
            # Map reason code to user-friendly message
            reason = fail.get("reason", "")
            desc = fail.get("description", "")
            if reason in RZP_REASON_MESSAGES:
                result["message"] = RZP_REASON_MESSAGES[reason]
            elif desc:
                result["message"] = desc
            else:
                result["message"] = f"Payment declined ({reason or 'unknown reason'})"
            logger.info(f"Payment DECLINED via JS event: code={result['decline_code']}, reason={reason}")
            return _finalize_result(result)

        if captured_events.get("success"):
            # handler(response) fired — definitive success
            succ = captured_events["success"]
            result["status"] = "success"
            result["status_text"] = "Payment Approved"
            if succ.get("payment_id"):
                result["payment_id"] = result["payment_id"] or succ["payment_id"]
            if succ.get("order_id") and not result["order_id"]:
                result["order_id"] = succ["order_id"]
            result["message"] = "Payment completed successfully."
            logger.info(f"Payment APPROVED via JS event: payment_id={result['payment_id']}")
            return _finalize_result(result)

        # ==================================================================
        # SIGNAL 2: RazorPay modal state (still open = likely declined)
        # ==================================================================
        modal_still_open = False
        try:
            modal_check = await engine.page.evaluate(
                """() => {
                    // RazorPay modal selectors
                    const selectors = [
                        '#razorpay-payment-container',
                        '.razorpay-container',
                        'iframe[name*="razorpay"]',
                        'iframe[src*="checkout.razorpay.com"]',
                        'iframe[src*="api.razorpay.com/v1/checkout"]',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) return {open: true, selector: sel};
                    }
                    return {open: false, selector: null};
                }"""
            )
            modal_still_open = modal_check.get("open", False) if modal_check else False
            if modal_still_open:
                logger.info(f"RazorPay modal still open: {modal_check.get('selector')}")
        except Exception as e:
            logger.warning(f"Could not check modal state: {e}")

        # Look for in-modal error message if modal is still open
        in_modal_error = ""
        if modal_still_open:
            try:
                # Search all RazorPay frames for error text
                for frame in engine.page.frames:
                    if "razorpay" not in (frame.url or "").lower():
                        continue
                    try:
                        err_text = await frame.evaluate(
                            """() => {
                                // RazorPay shows error in elements with these patterns
                                const errorEls = document.querySelectorAll(
                                    '[class*="error"], [class*="Error"], [role="alert"], ' +
                                    '.text-red, .text-danger, .rzp-error, [data-error]'
                                );
                                for (const el of errorEls) {
                                    const t = (el.textContent || '').trim();
                                    if (t && t.length > 5 && t.length < 200 && el.offsetParent !== null) {
                                        return t;
                                    }
                                }
                                return '';
                            }"""
                        )
                        if err_text:
                            in_modal_error = err_text
                            logger.info(f"In-modal error text: {err_text}")
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # ==================================================================
        # SIGNAL 3: Final URL patterns
        # ==================================================================
        url_success = "order-received" in final_url.lower()

        # ==================================================================
        # SIGNAL 4: WooCommerce + RazorPay plugin HTML markers
        # (Based on actual RazorPay WooCommerce plugin source code:
        #  github.com/razorpay/razorpay-woocommerce — woo-razorpay.php)
        # ==================================================================

        # Check for WooCommerce thank-you page HTML classes (most reliable)
        has_order_container = "woocommerce-order" in content_lower
        has_success_notice = (
            "woocommerce-thankyou-order-received" in content_lower
            or "woocommerce-notice--success" in content_lower
        )
        has_failed_notice = (
            "woocommerce-thankyou-order-failed" in content_lower
            or "woocommerce-notice--error" in content_lower
        )

        # The RazorPay plugin overrides WooCommerce's default text with:
        # "Thank you for shopping with us. Your account has been charged and
        #  your transaction is successful. We will be processing your order soon."
        # (Source: DEFAULT_SUCCESS_MESSAGE constant in woo-razorpay.php line 221)
        #
        # Additionally, the success page shows "Payment Successful" as a
        # banner/heading (user-confirmed). Including all known variants.
        razorpay_success_phrases = [
            "your transaction is successful",
            "your account has been charged",
            "we will be processing your order soon",
            "thank you for shopping with us",
            "payment successful",        # User-confirmed: appears on success page
            "payment success",
            "transaction successful",
        ]
        razorpay_success_hits = sum(1 for p in razorpay_success_phrases if p in text_lower)

        # WooCommerce default text (if RazorPay filter is disabled)
        wc_default_success = "thank you. your order has been received" in text_lower

        # WooCommerce failure text
        wc_failed_phrases = [
            "unfortunately your order cannot be processed",
            "originating bank/merchant has declined your transaction",
            "please attempt your purchase again",
        ]
        wc_failed_hits = sum(1 for p in wc_failed_phrases if p in text_lower)

        # RazorPay-specific decline text shown inside the modal
        rzp_modal_failure_phrases = [
            "payment failed", "transaction failed", "payment declined",
            "card declined", "payment unsuccessful", "transaction declined",
            "payment was declined", "your card was declined",
            "payment could not be processed", "transaction cannot be processed",
            "insufficient funds", "authentication failed",
            "incorrect cvv", "invalid cvv", "card expired",
            "payment timed out", "payment cancelled",
        ]
        rzp_failure_hits = sum(1 for p in rzp_modal_failure_phrases if p in text_lower)

        # Pending markers
        pending_phrases = [
            "payment pending", "transaction pending", "awaiting confirmation",
            "payment is being processed",
        ]
        pending_hits = sum(1 for p in pending_phrases if p in text_lower)

        # ==================================================================
        # SIGNAL 5: Extract order details from WooCommerce order-overview
        # (These elements only appear on the success/thank-you page)
        # ==================================================================
        try:
            order_overview = await engine.page.evaluate(
                """() => {
                    const result = {};
                    // Order number: <li class="woocommerce-order-overview__order order">
                    const orderEl = document.querySelector('.woocommerce-order-overview__order strong, .woocommerce-order-overview__order .value');
                    if (orderEl) result.order_number = orderEl.textContent.trim();
                    // Total: <li class="woocommerce-order-overview__total total">
                    const totalEl = document.querySelector('.woocommerce-order-overview__total strong, .woocommerce-order-overview__total .value');
                    if (totalEl) result.total = totalEl.textContent.trim();
                    // Payment method: <li class="woocommerce-order-overview__payment-method method">
                    const methodEl = document.querySelector('.woocommerce-order-overview__payment-method strong, .woocommerce-order-overview__payment-method .value');
                    if (methodEl) result.payment_method = methodEl.textContent.trim();
                    // Date
                    const dateEl = document.querySelector('.woocommerce-order-overview__date strong, .woocommerce-order-overview__date .value');
                    if (dateEl) result.date = dateEl.textContent.trim();
                    // Email
                    const emailEl = document.querySelector('.woocommerce-order-overview__email strong, .woocommerce-order-overview__email .value');
                    if (emailEl) result.email = emailEl.textContent.trim();
                    // Main success message text
                    const noticeEl = document.querySelector('.woocommerce-thankyou-order-received, .woocommerce-notice--success');
                    if (noticeEl) result.notice_text = noticeEl.textContent.trim();
                    return result;
                }"""
            )
            if order_overview:
                logger.info(f"WooCommerce order overview: {order_overview}")
                if order_overview.get("order_number") and not result["order_id"]:
                    result["order_id"] = order_overview["order_number"]
                if order_overview.get("total") and not result["amount"]:
                    # Extract numeric amount from "₹99.00" or "$99.00" etc.
                    amt_match = re.search(r'([\d,]+\.?\d*)', order_overview["total"])
                    if amt_match:
                        result["amount"] = amt_match.group(1)
                if order_overview.get("notice_text"):
                    result["message"] = order_overview["notice_text"][:300]
        except Exception as e:
            logger.warning(f"Could not extract WooCommerce order overview: {e}")

        # ==================================================================
        # DECISION LOGIC — combine all signals (priority order)
        # ==================================================================

        # Priority 1: WooCommerce + RazorPay HTML markers (most authoritative
        # for the thank-you page — these only appear after payment processing)
        if has_success_notice and not has_failed_notice:
            result["status"] = "success"
            result["status_text"] = "Payment Approved"
            # Use the RazorPay plugin's actual success message if present
            if razorpay_success_hits >= 2:
                result["message"] = (
                    "Thank you for shopping with us. Your account has been charged "
                    "and your transaction is successful. We will be processing your "
                    "order soon."
                )
            elif not result["message"]:
                result["message"] = "Payment completed successfully."
            logger.info("Payment APPROVED via WooCommerce success notice marker")
        elif has_failed_notice:
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            if wc_failed_hits > 0:
                result["message"] = (
                    "Unfortunately your order cannot be processed as the originating "
                    "bank/merchant has declined your transaction. Please attempt your "
                    "purchase again."
                )
            elif not result["message"]:
                result["message"] = "Payment was declined or failed."
            logger.info("Payment DECLINED via WooCommerce failure notice marker")

        # Priority 2: URL pattern + order overview present
        elif url_success and has_order_container:
            result["status"] = "success"
            result["status_text"] = "Payment Approved"
            if razorpay_success_hits >= 2 and not result["message"]:
                result["message"] = (
                    "Thank you for shopping with us. Your account has been charged "
                    "and your transaction is successful. We will be processing your "
                    "order soon."
                )
            elif not result["message"]:
                result["message"] = "Payment completed successfully."
            logger.info("Payment APPROVED via URL pattern + order container")

        # Priority 3: RazorPay plugin success text detected
        elif razorpay_success_hits >= 2 or wc_default_success:
            result["status"] = "success"
            result["status_text"] = "Payment Approved"
            if razorpay_success_hits >= 2:
                result["message"] = (
                    "Thank you for shopping with us. Your account has been charged "
                    "and your transaction is successful. We will be processing your "
                    "order soon."
                )
            else:
                result["message"] = "Thank you. Your order has been received."
            logger.info("Payment APPROVED via RazorPay/WooCommerce success text")

        # Priority 4: WooCommerce failed text
        elif wc_failed_hits > 0:
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            result["message"] = (
                "Unfortunately your order cannot be processed as the originating "
                "bank/merchant has declined your transaction. Please attempt your "
                "purchase again."
            )
            logger.info("Payment DECLINED via WooCommerce failure text")

        # Priority 5: RazorPay modal failure phrases
        elif rzp_failure_hits > 0:
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            # Try to find which phrase matched for a better message
            for phrase in rzp_modal_failure_phrases:
                if phrase in text_lower:
                    result["message"] = phrase.capitalize() + "."
                    # Map to reason code
                    reason_map = {
                        "card declined":              "card_declined",
                        "your card was declined":     "card_declined",
                        "payment was declined":       "payment_declined",
                        "payment declined":           "payment_declined",
                        "insufficient funds":         "insufficient_funds",
                        "authentication failed":      "authentication_failed",
                        "incorrect cvv":              "incorrect_cvv",
                        "invalid cvv":                "incorrect_cvv",
                        "card expired":               "card_expired",
                        "payment timed out":          "payment_timed_out",
                        "payment cancelled":          "payment_cancelled",
                    }
                    for pattern, reason in reason_map.items():
                        if pattern in text_lower:
                            result["decline_reason"] = reason
                            result["message"] = RZP_REASON_MESSAGES.get(reason, result["message"])
                            break
                    break
            logger.info("Payment DECLINED via RazorPay failure phrase")

        # Priority 6: Pending markers
        elif pending_hits > 0:
            result["status"] = "pending"
            result["status_text"] = "Payment Pending"
            result["message"] = "Payment is pending. Check your bank."
            logger.info("Payment PENDING via pending markers")

        # Priority 7: Modal still open + in-modal error (only if pay was clicked)
        elif modal_still_open and in_modal_error and pay_clicked:
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            result["message"] = in_modal_error
            err_lower = in_modal_error.lower()
            reason_map = {
                "card_declined":              ["card declined", "card was declined", "your card was declined"],
                "insufficient_funds":         ["insufficient funds", "insufficient balance"],
                "authentication_failed":      ["authentication failed", "3d secure", "3ds"],
                "incorrect_cvv":              ["incorrect cvv", "invalid cvv", "wrong cvv"],
                "card_expired":               ["card expired", "expired card"],
                "payment_timed_out":          ["timed out", "timeout"],
                "payment_cancelled":          ["cancelled", "canceled"],
            }
            for reason, patterns in reason_map.items():
                if any(p in err_lower for p in patterns):
                    result["decline_reason"] = reason
                    result["message"] = RZP_REASON_MESSAGES.get(reason, in_modal_error)
                    break
            logger.info("Payment DECLINED via modal still open + error text")

        # Priority 8: Modal still open (silent failure) — only if pay was clicked
        elif modal_still_open and pay_clicked:
            result["status"] = "failed"
            result["status_text"] = "Payment Declined"
            result["message"] = "Payment was declined or cancelled. Modal remained open."
            logger.info("Payment DECLINED via modal still open (no error text)")

        # Priority 8b: Modal still open but pay was NEVER clicked — different error
        elif modal_still_open and not pay_clicked:
            result["status"] = "error"
            result["status_text"] = "Pay Button Not Found"
            result["message"] = (
                "Could not find or click the Pay/Continue button in RazorPay. "
                "Payment was never submitted. This is a bot issue, not a card decline. "
                "Check logs/bot_full.log for the button dump."
            )
            logger.error("Pay button was never clicked — payment not submitted")

        # Priority 9: Payment ID present (success signal)
        elif result["payment_id"]:
            result["status"] = "success"
            result["status_text"] = "Payment Approved"
            if not result["message"]:
                result["message"] = "Payment completed successfully."
            logger.info("Payment APPROVED via payment ID presence")

        # Priority 10: Fallback — check WooCommerce error notices
        else:
            errors = re.findall(
                r'<ul[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</ul>',
                content, re.DOTALL | re.I
            )
            if errors:
                error_text = re.sub(r"<[^>]*>", "", errors[0]).strip()
                result["status"] = "failed"
                result["status_text"] = "Payment Error"
                result["message"] = error_text[:300]
            else:
                notices = re.findall(
                    r'<div[^>]*class="[^"]*woocommerce-notice[^"]*"[^>]*>(.*?)</div>',
                    content, re.DOTALL | re.I
                )
                if notices:
                    notice_text = re.sub(r"<[^>]*>", "", notices[0]).strip()
                    result["message"] = notice_text[:300]
                    if "success" in notice_text.lower() or "received" in notice_text.lower():
                        result["status"] = "success"
                        result["status_text"] = "Payment Approved"
                    else:
                        result["status"] = "needs_review"
                        result["status_text"] = "Needs Review"
                else:
                    result["status"] = "needs_review"
                    result["status_text"] = "Needs Review"
                    result["message"] = "Payment submitted. Verify manually."

        logger.info(
            f"Payment result: status={result['status']}, order_id={result['order_id']}, "
            f"payment_id={result['payment_id']}, amount={result['amount']}, "
            f"decline_reason={result['decline_reason']}"
        )

    except Exception as e:
        logger.error(f"Error parsing payment result: {e}", exc_info=True)
        result["status"] = "needs_review"
        result["status_text"] = "Parse Error"
        result["message"] = f"Could not parse payment result: {str(e)[:100]}"

    return _finalize_result(result)


def _finalize_result(result: dict) -> dict:
    """Ensure all required keys are present in the result dict."""
    required_keys = [
        "status", "status_text", "order_id", "order_key", "payment_id",
        "amount", "currency", "url", "message", "decline_reason",
        "decline_code", "error_source", "error_step",
    ]
    for key in required_keys:
        if key not in result:
            result[key] = ""
    return result


async def _fill_razorpay_in_frame(engine: SiteEngine, frame) -> dict:
    """Fill RazorPay in a specific frame."""
    cc = engine._cc
    await engine.on_status("💳 Entering card details...")
    try:
        await asyncio.sleep(2)

        # Click "Card" tab first (same as _fill_razorpay_modal)
        try:
            card_tab = frame.locator('text="Card"')
            if await card_tab.count() > 0:
                await card_tab.first.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        # Card number — type() for custom inputs
        card_input = frame.locator('input[name="card[number]"]')
        if await card_input.count() > 0:
            await card_input.click()
            await asyncio.sleep(0.3)
            await card_input.fill("")
            await card_input.type(cc.number, delay=30)
            await asyncio.sleep(0.3)

        exp_input = frame.locator('input[name="card[expiry]"]')
        if await exp_input.count() > 0:
            await exp_input.click()
            await asyncio.sleep(0.3)
            await exp_input.fill("")
            await exp_input.type(cc.expiry, delay=30)
            await asyncio.sleep(0.3)

        cvv_input = frame.locator('input[name="card[cvv]"]')
        if await cvv_input.count() > 0:
            await cvv_input.click()
            await asyncio.sleep(0.3)
            await cvv_input.fill("")
            await cvv_input.type(cc.cvv, delay=30)
            await asyncio.sleep(0.3)

        pay_btn = frame.locator('button[class*="pay"]')
        if await pay_btn.count() > 0:
            await pay_btn.click()

        await asyncio.sleep(8)

        # Parse the payment result from the final page
        return await _parse_payment_result(engine)
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


# ================================================================
# UTILITY METHODS (attached to class below)
# ================================================================
async def _check_login_status(self) -> bool:
    """Check if currently logged in."""
    try:
        ok = await self._ensure_page_loaded(LOGIN_URL)
        if not ok:
            return False
        content = await self._safe_get_content()
        return self._is_logged_in(content, self.page.url)
    except Exception:
        return False


async def _logout(self) -> None:
    """Logout and clear session."""
    try:
        ok = await self._ensure_page_loaded(f"{SITE_URL}/my-account/")
        if ok:
            content = await self._safe_get_content()
            logout_match = re.search(r'href="([^"]*logout[^"]*)"', content)
            if logout_match:
                logout_url = logout_match.group(1)
                if logout_url.startswith("/"):
                    logout_url = f"{SITE_URL}{logout_url}"
                await self.page.goto(logout_url, timeout=15000)
                await asyncio.sleep(2)
    except Exception:
        pass
    finally:
        self.session_manager.delete_session()
        await self.on_status("✅ Logged out. Session cleared.")


SiteEngine.check_login_status = _check_login_status
SiteEngine.logout = _logout