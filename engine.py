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
        Generate realistic random Indian billing details for any fields that
        are NOT already in `existing`. Used when the user doesn't provide
        billing details manually.

        Returns a dict with billing_* keys ready to be passed to
        fill_and_submit_checkout().
        """
        import random
        import string

        existing = existing or {}
        out = {}

        # Indian first names (mix of common Hindu/Muslim/Sikh/Christian names)
        first_names = [
            "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh",
            "Ayaan", "Krishna", "Ishaan", "Rahul", "Amit", "Suresh", "Rajesh",
            "Ananya", "Aadhya", "Aaradhya", "Saanvi", "Priya", "Pooja", "Kavya",
            "Diya", "Anika", "Navya", "Myra", "Anjali", "Deepa", "Sneha",
        ]
        last_names = [
            "Sharma", "Verma", "Gupta", "Patel", "Singh", "Kumar", "Reddy",
            "Nair", "Iyer", "Mehta", "Joshi", "Agarwal", "Bhat", "Rao",
            "Das", "Banerjee", "Chatterjee", "Mukherjee", "Khan", "Ali",
        ]

        # Indian cities with their states and pincodes (real, valid combos)
        locations = [
            ("Mumbai",    "MH", "400001"),  # Fort, Mumbai
            ("Delhi",     "DL", "110001"),  # Connaught Place
            ("Bengaluru", "KA", "560001"),  # MG Road
            ("Hyderabad", "TG", "500001"),  # Charminar
            ("Chennai",   "TN", "600001"),  # Parry's Corner
            ("Kolkata",   "WB", "700001"),  # BBD Bagh
            ("Pune",      "MH", "411001"),  # Pune City
            ("Ahmedabad", "GJ", "380001"),  # Kolkata
            ("Jaipur",    "RJ", "302001"),  # Jaipur City
            ("Lucknow",   "UP", "226001"),  # Hazratganj
            ("Chandigarh","CH", "160001"),  # Sector 1
            ("Indore",    "MP", "452001"),  # Indore City
            ("Surat",     "GJ", "395001"),  # Surat City
            ("Nagpur",    "MH", "440001"),  # Sitabuldi
        ]

        # Street names (realistic Indian address format)
        street_names = [
            "MG Road", "Station Road", "Civil Lines", "Model Town",
            "Rajaji Marg", "Jawahar Lane", "Gandhi Nagar", "Nehru Street",
            "Patel Marg", "Subhash Road", "Indira Colony", "Shastri Nagar",
        ]

        # Generate a stable random email handle so first + last name + email match
        first = existing.get("billing_first_name") or random.choice(first_names)
        last = existing.get("billing_last_name") or random.choice(last_names)
        city, state, pincode = random.choice(locations)
        street = random.choice(street_names)
        house_num = random.randint(1, 999)
        phone = "9" + "".join(random.choices(string.digits, k=9))  # Indian mobile
        email_handle = (first + last).lower() + str(random.randint(100, 9999))
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
            out["billing_address_1"] = f"{house_num}, {street}"
        if "billing_city" not in existing:
            out["billing_city"] = city
        if "billing_state" not in existing:
            out["billing_state"] = state
        if "billing_postcode" not in existing:
            out["billing_postcode"] = pincode
        if "billing_country" not in existing:
            out["billing_country"] = "IN"

        logger.info(
            f"Generated random billing for missing fields: "
            f"{first} {last}, {city} {state} {pincode}, {email}"
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
                    for (const el of document.querySelectorAll('input, button, [role="textbox"], iframe')) {
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
                        });
                    }
                    return out;
                }"""
            )
            for inp in inputs:
                logger.info(f"    {inp}")
        except Exception as e:
            logger.info(f"    (could not read frame: {e})")


async def _find_card_input_in_frame(frame, field_type: str = "number") -> any:
    """
    Find a card input element in a RazorPay frame using multiple strategies.

    Modern RazorPay uses separate iframes per field (card_number, card_expiry,
    card_cvv). Each iframe has a single <input> with no name/id, or with
    field-specific attributes.

    field_type: 'number', 'expiry', or 'cvv'
    """
    # Strategy 1: Traditional named inputs (old RazorPay UI)
    name_patterns = {
        "number":  ["card[number]", "card_number", "cardNumber", "cc-number"],
        "expiry":  ["card[expiry]", "card_expiry", "cardExpiry", "cc-exp"],
        "cvv":     ["card[cvv]", "card_cvv", "cardCvv", "cc-csc", "cc-cvv"],
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

    # Strategy 6: Placeholder-based
    placeholder_patterns = {
        "number":  ["card number", "0000 0000", "1234 5678", "•"],
        "expiry":  ["mm/yy", "MM/YY", "expiry", "MMYY"],
        "cvv":     ["cvv", "cvc", "123"],
    }
    for ph in placeholder_patterns.get(field_type, []):
        try:
            loc = frame.locator(f'input[placeholder*="{ph}" i]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    return None


async def _fill_razorpay_modal(engine: SiteEngine) -> dict:
    """Fill RazorPay modal that's already open in the page.

    Handles both old (single iframe with all fields) and new (separate
    iframes per card field) RazorPay UI layouts.
    """
    await engine.on_status("💳 RazorPay modal found! Entering card details...")
    cc = engine._cc

    # Wait for modal to fully load — RazorPay loads iframes asynchronously
    await asyncio.sleep(3)

    # Diagnostic dump
    await _dump_razorpay_frames(engine)

    filled_card = False
    filled_exp = False
    filled_cvv = False

    # First, try the OLD layout: single iframe with all card fields
    razorpay_frames = [f for f in engine.page.frames if "razorpay" in (f.url or "").lower()]
    logger.info(f"Found {len(razorpay_frames)} RazorPay frame(s)")

    for frame in razorpay_frames:
        if filled_card and filled_exp and filled_cvv:
            break
        try:
            # Click "Card" tab if visible (some RazorPay UIs show payment method tabs)
            try:
                card_tab = frame.locator('text="Card"')
                if await card_tab.count() > 0:
                    await card_tab.first.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

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
                "Could not find the card number field in RazorPay. "
                "RazorPay may have updated their UI. "
                "Please check /logs/live_issues.log for the frame dump."
            ),
        }

    if not filled_exp or not filled_cvv:
        logger.warning(f"Missing: expiry={not filled_exp}, cvv={not filled_cvv}")
        # Try one more time with a longer wait — iframes may still be loading
        await asyncio.sleep(2)
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

    # Click pay button — search ALL frames, not just the ones we filled
    pay_clicked = False
    for frame in engine.page.frames:
        if "razorpay" not in (frame.url or "").lower():
            continue
        for selector in [
            'button[class*="pay"]',
            'button#pay-button',
            'button:has-text("Pay")',
            'button:has-text("pay")',
            'input[type="submit"][value*="Pay"]',
            'button[type="submit"]',
            '#checkout-pay',
            'button.btn-primary',
        ]:
            try:
                pay_btn = frame.locator(selector)
                if await pay_btn.count() > 0:
                    if await pay_btn.first.is_visible(timeout=2000):
                        await pay_btn.first.click()
                        pay_clicked = True
                        logger.info(f"Pay button clicked in frame {frame.url} with selector {selector}")
                        break
            except Exception:
                continue
        if pay_clicked:
            break

    if not pay_clicked:
        logger.error("Could not find/click RazorPay pay button in any frame")

    await asyncio.sleep(8)
    try:
        final_url = engine.page.url
        final_text = await engine.page.inner_text("body")
        if "thank" in final_text.lower() or "order-received" in final_url:
            return {"status": "success", "message": "Payment completed successfully!", "url": final_url}
        return {
            "status": "needs_review",
            "message": "Payment submitted. Check email/WhatsApp for confirmation.",
            "url": final_url,
            "page_text": final_text[:300],
        }
    except Exception:
        return {"status": "needs_review", "message": "Payment submitted. Verify manually."}


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
        final_url = engine.page.url
        final_text = await engine.page.inner_text("body")

        if "thank" in final_text.lower() or "order-received" in final_url:
            return {"status": "success", "message": "Payment completed!", "url": final_url}

        return {
            "status": "needs_review",
            "message": "Payment submitted. Check email for confirmation.",
            "url": final_url,
        }
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