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
        )

        # Remove webdriver detection
        await self.context.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
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

    async def close(self) -> None:
        """Save cookies and close browser."""
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

    async def _solve_captcha_and_navigate(self, page: Page, url: str) -> bool:
        """
        If page shows LiteSpeed Bot Verification, solve reCAPTCHA via Nopecha
        and inject the token to get past it. Then navigate to the actual URL.
        Returns True if we end up on the real page (not verification).
        """
        content = await page.content()
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

        # Inject the token into the verification form
        await page.evaluate(
            """(token) => {
                const ta = document.getElementById('g-recaptcha-response');
                if (ta) {
                    ta.value = token;
                    ta.style.display = 'block';
                }
            }""",
            token,
        )
        await asyncio.sleep(1)

        # Submit the LiteSpeed verification form
        try:
            await page.evaluate(
                """() => {
                    const form = document.getElementById('lsrecaptcha-form');
                    if (form) form.submit();
                }"""
            )
        except Exception:
            pass

        # Wait for navigation after form submit
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            await asyncio.sleep(5)

        # Check if verification passed
        new_content = await page.content()
        if "Bot Verification" not in new_content:
            await self.on_status("✅ Verification passed!")
            return True

        # If still on verification page, the token might have been rejected.
        # Try navigating directly to the target URL with our new cookies.
        await self.on_status("🔄 Navigating to target page...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            final_content = await page.content()
            if "Bot Verification" not in final_content:
                return True
        except Exception:
            pass

        await self.on_status("❌ Verification failed. Try /cookies instead.")
        return False

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

            content = await self.page.content()
            if "Bot Verification" in content:
                return await self._solve_captcha_and_navigate(self.page, url)

            return True
        except Exception as e:
            logger.error(f"Page load error for {url}: {e}")
            return False

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

            content = await self.page.content()
            is_logged = "log out" in content.lower() or "logout" in content.lower()
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

            content = await self.page.content()
            if "woocommerce-login-nonce" not in content:
                await self.on_status("❌ Login page did not load properly.")
                return False

            await self.on_status("🔐 Filling credentials...")

            email_input = self.page.locator('input#username')
            await email_input.fill(email)

            pass_input = self.page.locator('input#password')
            await pass_input.fill(password)

            await asyncio.sleep(0.5)

            login_btn = self.page.locator('button[name="login"]')
            await login_btn.click()

            try:
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                await asyncio.sleep(3)

            content = await self.page.content()
            is_logged = "log out" in content.lower() or "logout" in content.lower()

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
                    await self.on_status(f"❌ Login failed: {error_text[:100]}")
                else:
                    await self.on_status("❌ Login failed. Check your credentials.")
                return False

        except Exception as e:
            logger.error(f"Login error: {e}")
            await self.on_status(f"❌ Login error: {str(e)[:100]}")
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

            pid_match = re.search(r"[?&]p=(\d+)", url)
            if pid_match:
                # Validate by briefly loading the page
                product_id = int(pid_match.group(1))
                product_url = f"{SITE_URL}/?post_type=product&p={product_id}"
                ok = await self._ensure_page_loaded(product_url)
                if not ok:
                    return False
                content = await self.page.content()
                if "add_to_cart" not in content and "add-to-cart" not in content:
                    await self.on_status("❌ URL has product ID but page is not a valid product.")
                    return False
                self.product_id = product_id
                await self.on_status(f"✅ Valid product! (ID: {self.product_id})")
                return True

            ok = await self._ensure_page_loaded(url)
            if not ok:
                return False

            content = await self.page.content()
            if "add_to_cart" not in content and "add-to-cart" not in content:
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

            add_btn = self.page.locator('button.single_add_to_cart_button[name="add-to-cart"]')
            if await add_btn.count() > 0:
                await add_btn.click()
                await asyncio.sleep(3)

                content = await self.page.content()
                if "has been added to your cart" in content.lower() or "added to cart" in content.lower():
                    await self.on_status("✅ Product added to cart!")
                    return True

                # Verify by checking cart page (through captcha-safe loader)
                ok = await self._ensure_page_loaded(CART_URL)
                if not ok:
                    await self.on_status("❌ Could not verify cart (captcha block).")
                    return False
                await asyncio.sleep(2)
                cart_content = await self.page.content()
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

            content = await self.page.content()

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

    async def fill_and_submit_checkout(self, billing: dict, nonce: str) -> dict:
        """Fill billing fields and submit checkout via Playwright."""
        await self.on_status("📦 Filling billing details...")

        try:
            field_map = {
                "billing_first_name": "billing_first_name",
                "billing_last_name": "billing_last_name",
                "billing_email": "billing_email",
                "billing_phone": "billing_phone",
                "billing_address_1": "billing_address_1",
                "billing_city": "billing_city",
                "billing_state": "billing_state",
                "billing_postcode": "billing_postcode",
            }

            for key, selector_id in field_map.items():
                if key in billing:
                    field = self.page.locator(f"#{selector_id}")
                    if await field.count() > 0:
                        await field.fill(billing[key])
                        await asyncio.sleep(0.2)

            # Select country
            country_select = self.page.locator("#billing_country")
            if await country_select.count() > 0:
                try:
                    await country_select.select_option("IN")
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
            content = await self.page.content()

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
async def _fill_razorpay_modal(engine: SiteEngine) -> dict:
    """Fill RazorPay modal that's already open in the page."""
    await engine.on_status("💳 RazorPay modal found! Entering card details...")
    cc = engine._cc
    filled = False

    for frame in engine.page.frames:
        if "razorpay" not in (frame.url or "").lower():
            continue
        try:
            await asyncio.sleep(2)

            # Click "Card" tab if visible
            try:
                card_tab = frame.locator('text="Card"')
                if await card_tab.count() > 0:
                    await card_tab.first.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            # Card number — use type() for custom inputs that ignore fill()
            card_input = frame.locator('input[name="card[number]"]')
            if await card_input.count() > 0 and await card_input.is_visible(timeout=5000):
                await card_input.click()
                await asyncio.sleep(0.3)
                await card_input.fill("")
                await card_input.type(cc.number, delay=30)
                await asyncio.sleep(0.5)

            # Expiry
            exp_input = frame.locator('input[name="card[expiry]"]')
            if await exp_input.count() > 0 and await exp_input.is_visible(timeout=3000):
                await exp_input.click()
                await asyncio.sleep(0.3)
                await exp_input.fill("")
                await exp_input.type(cc.expiry, delay=30)
                await asyncio.sleep(0.5)

            # CVV
            cvv_input = frame.locator('input[name="card[cvv]"]')
            if await cvv_input.count() > 0 and await cvv_input.is_visible(timeout=3000):
                await cvv_input.click()
                await asyncio.sleep(0.3)
                await cvv_input.fill("")
                await cvv_input.type(cc.cvv, delay=30)
                await asyncio.sleep(0.5)

            filled = True
            await engine.on_status("⏳ Submitting payment to RazorPay...")

            # Click pay button
            pay_btn = frame.locator('button[class*="pay"]')
            if await pay_btn.count() > 0 and await pay_btn.is_visible(timeout=3000):
                await pay_btn.click()
            break
        except Exception as e:
            logger.error(f"Error filling RazorPay frame: {e}")
            continue

    if not filled:
        return {"status": "error", "message": "Could not fill card details in RazorPay."}

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
        content = await self.page.content()
        return "log out" in content.lower() or "logout" in content.lower()
    except Exception:
        return False


async def _logout(self) -> None:
    """Logout and clear session."""
    try:
        ok = await self._ensure_page_loaded(f"{SITE_URL}/my-account/")
        if ok:
            content = await self.page.content()
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