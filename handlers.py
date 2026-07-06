"""
Telegram Bot Handlers — All command and message handlers.

Uses aiogram FSM for step-by-step automation flow.
Engine uses Playwright for all operations (site blocks non-browser clients).

Login methods:
  1. /login  — email + password (Nopecha auto-solves captcha)
  2. /cookies — paste cookie string from browser (free, no API needed)
"""

import re
import html
from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from engine import SiteEngine
from config import SITE_URL
from secure_log import get_logger

logger = get_logger("Handlers")
router = Router()


class BotStates(StatesGroup):
    """FSM States for the automation flow."""
    WAITING_EMAIL = State()
    WAITING_PASSWORD = State()
    WAITING_COOKIES = State()
    WAITING_URL = State()
    WAITING_QUANTITY = State()
    WAITING_BILLING = State()
    WAITING_CC_NUMBER = State()
    WAITING_CC_EXPIRY = State()
    WAITING_CVV = State()


_active_engines: dict[int, SiteEngine] = {}


def _get_engine(user_id: int) -> SiteEngine | None:
    return _active_engines.get(user_id)


def _set_engine(user_id: int, engine: SiteEngine) -> None:
    _active_engines[user_id] = engine


def _remove_engine(user_id: int) -> None:
    _active_engines.pop(user_id, None)


async def _send_status(message: Message, text: str) -> None:
    """Edit the status message in-place. Falls back to answer() on conflict."""
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        try:
            await message.answer(text, parse_mode="HTML")
        except Exception:
            pass


# ============================================================
# /start
# ============================================================
@router.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🤖 <b>GPL Games Automation Bot</b>\n\n"
        f"🎯 Target: <code>{SITE_URL}</code>\n"
        "💳 Payment: <b>RazorPay</b>\n"
        "🛡️ Captcha: <b>Nopecha</b> (auto-solved)\n\n"
        "📋 <b>Commands:</b>\n"
        "🔑 <code>/login</code> — Login with email + password\n"
        "🍪 <code>/cookies</code> — Login via browser cookies (free!)\n"
        "🔗 <code>/seturl</code> — Set product URL\n"
        "🔄 <code>/status</code> — Check login\n"
        "🛑 <code>/cancel</code> — Cancel current action\n"
        "🚪 <code>/logout</code> — Logout & clear session\n\n"
        "🔒 CC details are <b>NEVER</b> saved or logged.",
        parse_mode="HTML"
    )


# ============================================================
# /cookies — Cookie-based login (FREE, no API needed)
# ============================================================
@router.message(F.text == "/cookies")
async def cmd_cookies(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BotStates.WAITING_COOKIES)
    await message.answer(
        "🍪 <b>Login via Browser Cookies</b>\n\n"
        "This is the <b>free</b> method — no captcha needed!\n\n"
        "<b>How to get your cookies:</b>\n"
        "1. Open gplgames.net in your browser (Chrome/Firefox)\n"
        "2. Login to your account\n"
        "3. Press <b>F12</b> → go to <b>Console</b> tab\n"
        "4. Type: <code>document.cookie</code> and press Enter\n"
        "5. Copy the entire output and paste it here\n\n"
        "📌 Paste your cookie string below:",
        parse_mode="HTML"
    )


@router.message(BotStates.WAITING_COOKIES)
async def process_cookies(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    cookie_string = (message.text or "").strip()
    await state.clear()

    if not cookie_string or "=" not in cookie_string:
        await message.answer("⚠️ Invalid cookie string. Use /cookies to try again.")
        return

    processing = await message.answer("⏳ <b>Importing cookies...</b>", parse_mode="HTML")

    try:
        old = _get_engine(message.from_user.id)
        if old:
            await old.close()
            _remove_engine(message.from_user.id)

        engine = SiteEngine(
            user_id=message.from_user.id,
            on_status=lambda text: _send_status(processing, text)
        )
        _set_engine(message.from_user.id, engine)

        await engine.init_session()
        success = await engine.import_cookies_from_string(cookie_string)

        if success:
            await processing.edit_text(
                "✅ <b>Logged in via cookies!</b>\n\n"
                "Session is persistent. Use <code>/seturl</code> to choose a product.",
                parse_mode="HTML"
            )
        else:
            await engine.close()
            _remove_engine(message.from_user.id)
            await processing.edit_text(
                "❌ Cookie login failed. Cookies may be expired or invalid.\n\n"
                "Make sure you're logged in on the site, then copy fresh cookies.\n"
                "Type /cookies to try again.",
                parse_mode="HTML"
            )
    except Exception as e:
        _remove_engine(message.from_user.id)
        await processing.edit_text(f"❌ Error: {str(e)[:150]}")


# ============================================================
# /login — Email + Password (Nopecha auto-solves captcha)
# ============================================================
@router.message(F.text == "/login")
async def cmd_login(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BotStates.WAITING_EMAIL)
    await message.answer(
        "🔐 <b>Login to GPL Games</b>\n\n"
        "Captcha will be solved automatically via Nopecha.\n\n"
        "Send your <b>email/username</b>:",
        parse_mode="HTML"
    )


@router.message(BotStates.WAITING_EMAIL)
async def process_email(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    await state.update_data(email=message.text or "")
    await state.set_state(BotStates.WAITING_PASSWORD)
    await message.answer(
        "✉️ Now send your <b>password</b>:",
        parse_mode="HTML"
    )


@router.message(BotStates.WAITING_PASSWORD)
async def process_password(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    data = await state.get_data()
    email = data.get("email", "")
    password = message.text or ""
    await state.clear()

    processing = await message.answer("⏳ <b>Logging in...</b>", parse_mode="HTML")

    try:
        old = _get_engine(message.from_user.id)
        if old:
            await old.close()
            _remove_engine(message.from_user.id)

        engine = SiteEngine(
            user_id=message.from_user.id,
            on_status=lambda text: _send_status(processing, text)
        )
        _set_engine(message.from_user.id, engine)

        await engine.init_session()
        success = await engine.login(email, password)

        if not success:
            # Clear stale cookies so they don't interfere with the next attempt.
            # Failed login often leaves half-baked cookies (e.g. captcha-passed
            # but not logged-in) that cause the next /login to skip captcha
            # but fail at the actual credential step.
            try:
                from session_manager import SessionManager
                SessionManager(message.from_user.id).delete_session()
                logger.info(f"Cleared stale session for user {message.from_user.id} after failed login")
            except Exception as clear_err:
                logger.warning(f"Could not clear stale session: {clear_err}")

            await engine.close()
            _remove_engine(message.from_user.id)
            await processing.edit_text(
                "❌ Login failed.\n\n"
                "💡 Tip: Use <code>/cookies</code> for free login (no captcha).\n"
                "Or type /login to retry.",
                parse_mode="HTML"
            )
        else:
            await processing.edit_text(
                "✅ <b>Logged in!</b> Session is persistent.\n\n"
                "Next: <code>/seturl</code> to choose a product.",
                parse_mode="HTML"
            )
    except Exception as e:
        _remove_engine(message.from_user.id)
        await processing.edit_text(f"❌ Error: {str(e)[:150]}")


# ============================================================
# /seturl
# ============================================================
@router.message(F.text.startswith("/seturl"))
async def cmd_seturl(message: Message, state: FSMContext) -> None:
    parts = message.text.split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""

    if not url:
        await state.set_state(BotStates.WAITING_URL)
        await message.answer(
            "🔗 Send the <b>product URL</b> from gplgames.net:",
            parse_mode="HTML"
        )
        return

    await _process_url(message, state, url)


@router.message(BotStates.WAITING_URL)
async def process_url(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    await _process_url(message, state, message.text or "")


async def _process_url(message: Message, state: FSMContext, url: str) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer("❌ Not logged in! Use <code>/login</code> or <code>/cookies</code> first.", parse_mode="HTML")
        return

    processing = await message.answer("🔍 Verifying URL...", parse_mode="HTML")
    engine.on_status = lambda text: _send_status(processing, text)

    valid = await engine.verify_url(url)
    if valid:
        await state.set_state(BotStates.WAITING_QUANTITY)
        await processing.edit_text(
            "✅ Valid product!\n\n"
            "📦 Send quantity (e.g., <code>1</code>):",
            parse_mode="HTML"
        )
    else:
        await processing.edit_text("❌ Invalid URL. Try /seturl again.")


# ============================================================
# QUANTITY → Add to Cart → Checkout
# ============================================================
@router.message(BotStates.WAITING_QUANTITY)
async def process_quantity(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1 or int(text) > 999:
        await message.answer("⚠️ Send a valid number between 1 and 999:")
        return

    quantity = int(text)
    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer("❌ Session lost. /login or /cookies again.")
        await state.clear()
        return

    processing = await message.answer(f"🛒 Adding to cart (qty: {quantity})...", parse_mode="HTML")
    engine.on_status = lambda text: _send_status(processing, text)

    added = await engine.add_to_cart(quantity)
    if not added:
        await state.clear()
        return

    checkout_data = await engine.get_checkout_page()

    if checkout_data.get("error"):
        await processing.edit_text(f"❌ {checkout_data['error']}")
        await state.clear()
        return

    total = checkout_data.get("total", "N/A")
    nonce = checkout_data.get("nonce", "")

    if not nonce:
        await processing.edit_text("❌ Could not get checkout nonce. Try again.")
        await state.clear()
        return

    await state.update_data(quantity=quantity, checkout_nonce=nonce, order_total=total)

    await state.set_state(BotStates.WAITING_BILLING)
    await processing.edit_text(
        f"🛒 <b>In Cart — Total: ₹{total}</b>\n\n"
        "📋 <b>Billing Details</b>\n\n"
        "Send in this format:\n"
        "<code>First Name, Last Name, Email, Phone, Address, City, State, Pincode</code>\n\n"
        "<b>Example:</b>\n"
        "<code>Rahul, Sharma, rahul@email.com, 9876543210, 123 Main St, Mumbai, MH, 400001</code>",
        parse_mode="HTML"
    )


# ============================================================
# BILLING DETAILS
# ============================================================
@router.message(BotStates.WAITING_BILLING)
async def process_billing(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    text = (message.text or "").strip()
    parts = [p.strip() for p in text.split(",")]

    if len(parts) < 8:
        await message.answer(
            "⚠️ Need 8 fields separated by commas:\n\n"
            "<code>Name, Last, Email, Phone, Address, City, State, Pincode</code>"
        )
        return

    if "@" not in parts[2]:
        await message.answer("⚠️ Invalid email. Try again:")
        return

    billing = {
        "billing_first_name": parts[0],
        "billing_last_name": parts[1],
        "billing_email": parts[2],
        "billing_phone": parts[3],
        "billing_address_1": parts[4],
        "billing_city": parts[5],
        "billing_state": parts[6],
        "billing_postcode": parts[7],
    }

    data = await state.get_data()
    nonce = data.get("checkout_nonce", "")

    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer("❌ Session lost. /login or /cookies again.")
        await state.clear()
        return

    processing = await message.answer("📦 Submitting checkout...", parse_mode="HTML")
    engine.on_status = lambda text: _send_status(processing, text)

    result = await engine.fill_and_submit_checkout(billing, nonce)

    if result.get("result") == "failure":
        error_msg = re.sub(r"<[^>]*>", "", result.get("messages", "Unknown error")).strip()
        # Escape to prevent HTML injection from site-controlled error text
        await processing.edit_text(f"❌ Checkout failed: {html.escape(error_msg[:200])}")
        await state.clear()
        return

    await state.update_data(checkout_result=result)
    await state.set_state(BotStates.WAITING_CC_NUMBER)
    await processing.edit_text(
        "✅ Checkout submitted!\n\n"
        "💳 Send your <b>Card Number</b>:\n"
        "Example: <code>4242424242424242</code>\n\n"
        "🔒 <i>Card details are NEVER saved.</i>",
        parse_mode="HTML"
    )


# ============================================================
# CC NUMBER
# ============================================================
@router.message(BotStates.WAITING_CC_NUMBER)
async def process_cc_number(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    cc_number = (message.text or "").replace(" ", "").replace("-", "")
    if not cc_number.isdigit() or len(cc_number) < 13 or len(cc_number) > 19:
        await message.answer("⚠️ Invalid card number (13-19 digits):")
        return

    # Delete the user's message so the card number doesn't linger in chat history
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(cc_number=cc_number)
    await state.set_state(BotStates.WAITING_CC_EXPIRY)
    await message.answer(
        "✅ Card number received (deleted for privacy).\n\n"
        "📅 Send card <b>Expiry</b> as <code>MM/YY</code> (e.g., <code>12/28</code>):",
        parse_mode="HTML"
    )


# ============================================================
# CC EXPIRY
# ============================================================
@router.message(BotStates.WAITING_CC_EXPIRY)
async def process_cc_expiry(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    expiry = (message.text or "").strip()
    expiry_clean = re.sub(r"[^\d]", "", expiry)
    if len(expiry_clean) != 4:
        await message.answer("⚠️ Invalid. Send <code>MM/YY</code> (e.g., <code>12/28</code>):")
        return

    month = int(expiry_clean[:2])
    if month < 1 or month > 12:
        await message.answer("⚠️ Invalid month (01-12):")
        return

    # Delete the user's message so expiry doesn't linger in chat history
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(cc_expiry=f"{expiry_clean[:2]}/{expiry_clean[2:]}")
    await state.set_state(BotStates.WAITING_CVV)
    await message.answer(
        "✅ Expiry received (deleted for privacy).\n\n"
        "🔒 Send your card <b>CVV</b> (3 or 4 digits):",
        parse_mode="HTML"
    )


# ============================================================
# CVV → PROCESS PAYMENT → SHOW RESULT
# ============================================================
@router.message(BotStates.WAITING_CVV)
async def process_cvv(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return

    cvv = (message.text or "").strip()
    if not cvv.isdigit() or len(cvv) < 3 or len(cvv) > 4:
        await message.answer("⚠️ Invalid CVV (3-4 digits):")
        return

    # Delete the user's CVV message immediately for privacy
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    cc_number = data.get("cc_number", "")
    cc_expiry = data.get("cc_expiry", "")
    checkout_result = data.get("checkout_result", {})

    await state.clear()

    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer("❌ Session lost. /login or /cookies again.")
        return

    processing = await message.answer(
        "🔒 <b>Processing payment...</b>\n\n"
        "💳 Entering card in RazorPay...\n"
        "Please wait...",
        parse_mode="HTML"
    )
    engine.on_status = lambda text: _send_status(processing, text)

    result = await engine.process_razorpay_payment(
        cc_number, cc_expiry, cvv, checkout_result
    )

    status = result.get("status", "error")
    msg = result.get("message", "Unknown result")

    if status == "success":
        response_text = f"✅ <b>Payment Completed!</b>\n\n💬 {html.escape(msg)}"
        url = result.get("url", "")
        if url:
            response_text += f"\n\n🔗 <code>{html.escape(url)}</code>"
    elif status == "needs_review":
        response_text = (
            f"⚠️ <b>Payment Submitted — Verify</b>\n\n"
            f"💬 {html.escape(msg)}\n\n"
            f"Check your email/WhatsApp for order confirmation."
        )
    else:
        response_text = (
            f"❌ <b>Payment Issue</b>\n\n"
            f"💬 {html.escape(msg)}\n\n"
            "Possible reasons:\n"
            "• Invalid card details\n"
            "• Card declined\n"
            "• RazorPay gateway issue\n\n"
            "Type /seturl to try again."
        )

    await processing.edit_text(response_text, parse_mode="HTML")


# ============================================================
# /status
# ============================================================
@router.message(F.text == "/status")
async def cmd_status(message: Message, state: FSMContext) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)

    if not engine:
        await message.answer(
            "🟡 Not logged in.\n\nUse <code>/cookies</code> (free) or <code>/login</code>.",
            parse_mode="HTML"
        )
        return

    processing = await message.answer("🔍 Checking...", parse_mode="HTML")
    try:
        is_logged = await engine.check_login_status()
        if is_logged:
            await processing.edit_text(
                "🟢 <b>Logged in</b> ✅\n\nSession persistent. <code>/seturl</code> to buy.",
                parse_mode="HTML"
            )
        else:
            await processing.edit_text(
                "🔴 Session expired.\n\nUse <code>/cookies</code> or <code>/login</code> again.",
                parse_mode="HTML"
            )
    except Exception:
        await processing.edit_text("⚠️ Could not check. /cookies or /login again.")


# ============================================================
# /cancel
# ============================================================
@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛑 Cancelled.\n\n<code>/start</code> for commands.", parse_mode="HTML")


# ============================================================
# /logout
# ============================================================
@router.message(F.text == "/logout")
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)
    if engine:
        try:
            processing = await message.answer("🚪 Logging out...", parse_mode="HTML")
            engine.on_status = lambda text: _send_status(processing, text)
            await engine.logout()
            await engine.close()
        except Exception:
            pass
        finally:
            _remove_engine(message.from_user.id)
    await message.answer("✅ Logged out. Session cleared.", parse_mode="HTML")


# ============================================================
# /help
# ============================================================
@router.message(F.text == "/help")
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Help</b>\n\n"
        "🚀 <b>Flow:</b>\n"
        "1. <code>/login</code> — email + password (captcha auto-solved)\n"
        "   OR <code>/cookies</code> — paste browser cookies (free!)\n"
        "2. <code>/seturl</code> — product link\n"
        "3. Send quantity (e.g., 1)\n"
        "4. Send billing: <code>Name, Last, Email, Phone, Address, City, State, Pincode</code>\n"
        "5. Send card number\n"
        "6. Send expiry (MM/YY)\n"
        "7. Send CVV\n"
        "8. Bot completes payment → shows result\n\n"
        "🔒 CC data: in-memory only, wiped after use.\n"
        "💳 Gateway: RazorPay\n"
        "🛡️ Captcha: Nopecha (auto-solved)",
        parse_mode="HTML"
    )


# ============================================================
# FALLBACK — catch non-text / unrecognized messages
# ============================================================
@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    """Catch-all for messages that don't match any command or expected state input.

    - If a state is active, remind the user what's expected.
    - Otherwise nudge them to /start.
    """
    current_state = await state.get_state()
    if current_state is not None:
        hints = {
            BotStates.WAITING_EMAIL:        "your <b>email/username</b>",
            BotStates.WAITING_PASSWORD:     "your <b>password</b>",
            BotStates.WAITING_COOKIES:      "the <b>cookie string</b> from your browser",
            BotStates.WAITING_URL:          "the <b>product URL</b> from gplgames.net",
            BotStates.WAITING_QUANTITY:     "a <b>quantity</b> (e.g., 1)",
            BotStates.WAITING_BILLING:      "<b>billing details</b> (comma-separated, 8 fields)",
            BotStates.WAITING_CC_NUMBER:    "your <b>card number</b> (13-19 digits)",
            BotStates.WAITING_CC_EXPIRY:    "your <b>card expiry</b> as MM/YY",
            BotStates.WAITING_CVV:          "your <b>CVV</b> (3-4 digits)",
        }
        hint = hints.get(current_state, "the expected input")
        await message.answer(
            f"⚠️ I'm waiting for {hint}.\n"
            f"Send <code>/cancel</code> to abort.",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🤖 Unknown command. Send <code>/start</code> to see available commands.",
            parse_mode="HTML"
        )