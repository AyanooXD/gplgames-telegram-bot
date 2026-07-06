"""
Telegram Bot Handlers — All command and message handlers.

Uses aiogram FSM for step-by-step automation flow.
Engine uses Playwright for all operations (site blocks non-browser clients).

Login methods:
  1. /login  — email + password (Nopecha auto-solves captcha)
  2. /cookies — paste cookie string from browser (free, no API needed)
"""

import re
import time
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

# Track last status edit per message — prevents duplicate edits + spam
_last_status_text: dict[int, str] = {}
_last_edit_time: dict[int, float] = {}


def _get_engine(user_id: int) -> SiteEngine | None:
    return _active_engines.get(user_id)


def _set_engine(user_id: int, engine: SiteEngine) -> None:
    _active_engines[user_id] = engine


def _remove_engine(user_id: int) -> None:
    _active_engines.pop(user_id, None)


async def _send_status(message: Message, text: str) -> None:
    """
    Edit the status message in place. NEVER falls back to answer() —
    this prevents the message spam that was happening before.

    Includes:
    - Duplicate text detection (skip if same as last edit)
    - Throttling (max 1 edit per 0.8s per message)
    - Silent failure (don't create new messages on error)
    """
    msg_id = message.message_id

    # Skip if text hasn't changed
    if _last_status_text.get(msg_id) == text:
        return

    # Throttle: don't edit more than once per 0.8 seconds
    now = time.time()
    if now - _last_edit_time.get(msg_id, 0) < 0.8:
        return

    try:
        await message.edit_text(text, parse_mode="HTML")
        _last_status_text[msg_id] = text
        _last_edit_time[msg_id] = now
    except Exception:
        # Silently skip — NEVER send a new message
        pass


def _clear_status_tracking(message: Message) -> None:
    """Clear tracking for a message so the next edit always goes through."""
    msg_id = message.message_id
    _last_status_text.pop(msg_id, None)
    _last_edit_time.pop(msg_id, None)


# ============================================================
# /start
# ============================================================
@router.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎮 <b>GPL Games Automation Bot</b>\n\n"
        "⚡ Automated checkout for <code>gplgames.net</code>\n\n"

        "📋 <b>Commands</b>\n"
        "🔑 <code>/login</code> — Sign in (captcha auto-solved)\n"
        "🍪 <code>/cookies</code> — Free login via browser cookies\n"
        "🔗 <code>/seturl</code> — Set product URL\n"
        "📊 <code>/status</code> — Check login status\n"
        "🚪 <code>/logout</code> — Sign out\n"
        "❌ <code>/cancel</code> — Cancel current action\n\n"

        "🔒 <b>Security</b>\n"
        "• Card details: never saved, auto-deleted from chat\n"
        "• Wiped from memory after payment\n"
        "• Billing: auto-generated (non-India)\n\n"

        "<i>Send any command to begin.</i>",
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
        "Free method — no captcha solving needed.\n\n"

        "📋 <b>How to get cookies:</b>\n"
        "1️⃣ Open gplgames.net in Chrome/Firefox\n"
        "2️⃣ Log in to your account\n"
        "3️⃣ Press <code>F12</code> → Console tab\n"
        "4️⃣ Type <code>document.cookie</code> + Enter\n"
        "5️⃣ Copy the output and paste here\n\n"

        "<i>👇 Paste your cookie string below</i>",
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
        await message.answer("⚠️ Invalid cookie string. Use <code>/cookies</code> to try again.", parse_mode="HTML")
        return

    processing = await message.answer("⏳ <b>Importing cookies...</b>", parse_mode="HTML")
    _clear_status_tracking(processing)

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
                "✅ <b>Login Successful</b>\n\n"
                "Session is persistent — you won't need to log in again.\n\n"
                "👉 <b>Next:</b> Send <code>/seturl</code> with a product link",
                parse_mode="HTML"
            )
        else:
            await engine.close()
            _remove_engine(message.from_user.id)
            await processing.edit_text(
                "❌ <b>Cookie Login Failed</b>\n\n"
                "Cookies may be expired or invalid.\n\n"
                "💡 Make sure you're logged in on the site, then copy fresh cookies.\n"
                "Type <code>/cookies</code> to try again.",
                parse_mode="HTML"
            )
    except Exception as e:
        _remove_engine(message.from_user.id)
        await processing.edit_text(f"❌ <b>Error:</b> <code>{html.escape(str(e)[:150])}</code>", parse_mode="HTML")


# ============================================================
# /login — Email + Password (Nopecha auto-solves captcha)
# ============================================================
@router.message(F.text == "/login")
async def cmd_login(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BotStates.WAITING_EMAIL)
    await message.answer(
        "🔐 <b>Login to GPL Games</b>\n\n"
        "Captcha is auto-solved — just send your credentials.\n\n"

        "📝 <b>Step 1 of 2:</b> Send your <b>email or username</b>\n\n"

        "💡 <i>Tip: Use <code>/cookies</code> for free login (no captcha)</i>",
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
        "✉️ <b>Step 2 of 2:</b> Send your <b>password</b>\n\n"
        "<i>🔒 Password is never stored</i>",
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

    # Delete the password message for privacy
    try:
        await message.delete()
    except Exception:
        pass

    processing = await message.answer("⏳ <b>Logging in...</b>\n\n<i>Captcha solving takes 60-90s</i>", parse_mode="HTML")
    _clear_status_tracking(processing)

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
            # Clear stale cookies so they don't interfere with the next attempt
            try:
                from session_manager import SessionManager
                SessionManager(message.from_user.id).delete_session()
                logger.info(f"Cleared stale session for user {message.from_user.id} after failed login")
            except Exception as clear_err:
                logger.warning(f"Could not clear stale session: {clear_err}")

            await engine.close()
            _remove_engine(message.from_user.id)
            await processing.edit_text(
                "❌ <b>Login Failed</b>\n\n"
                "💡 <b>Alternatives:</b>\n"
                "• <code>/cookies</code> — free login, no captcha\n"
                "• <code>/login</code> — try again",
                parse_mode="HTML"
            )
        else:
            await processing.edit_text(
                "✅ <b>Login Successful</b>\n\n"
                "Session is persistent — you won't need to log in again.\n\n"
                "👉 <b>Next:</b> Send <code>/seturl</code> with a product link",
                parse_mode="HTML"
            )
    except Exception as e:
        _remove_engine(message.from_user.id)
        await processing.edit_text(f"❌ <b>Error:</b> <code>{html.escape(str(e)[:150])}</code>", parse_mode="HTML")


# ============================================================
# /seturl
# ============================================================
@router.message(F.text.startswith("/seturl"))
async def cmd_seturl(message: Message, state: FSMContext) -> None:
    # Ignore edited messages to prevent duplicate processing
    if message.edit_date is not None:
        return

    parts = message.text.split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""

    if not url:
        await state.set_state(BotStates.WAITING_URL)
        await message.answer(
            "🔗 <b>Set Product URL</b>\n\n"
            "Send a product link from gplgames.net\n\n"
            "<i>Example: <code>https://gplgames.net/?p=12345</code></i>",
            parse_mode="HTML"
        )
        return

    await _process_url(message, state, url)


@router.message(BotStates.WAITING_URL)
async def process_url(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    # Ignore edited messages
    if message.edit_date is not None:
        return
    await _process_url(message, state, message.text or "")


async def _process_url(message: Message, state: FSMContext, url: str) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer(
            "❌ <b>Not logged in!</b>\n\n"
            "Use <code>/login</code> or <code>/cookies</code> first.",
            parse_mode="HTML"
        )
        return

    processing = await message.answer("🔍 <b>Verifying product URL...</b>", parse_mode="HTML")
    _clear_status_tracking(processing)
    engine.on_status = lambda text: _send_status(processing, text)

    valid = await engine.verify_url(url)
    if valid:
        await state.set_state(BotStates.WAITING_QUANTITY)
        await processing.edit_text(
            "✅ <b>Product Verified</b>\n\n"
            f"🆔 <b>Product ID:</b> <code>{engine.product_id}</code>\n\n"
            "📦 <b>Next:</b> Send the <b>quantity</b> (e.g., <code>1</code>)",
            parse_mode="HTML"
        )
    else:
        await processing.edit_text(
            "❌ <b>Invalid URL</b>\n\n"
            "Make sure it's a valid gplgames.net product page.\n"
            "Type <code>/seturl</code> to try again.",
            parse_mode="HTML"
        )


# ============================================================
# QUANTITY → Add to Cart → Checkout
# ============================================================
@router.message(BotStates.WAITING_QUANTITY)
async def process_quantity(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    # Ignore edited messages
    if message.edit_date is not None:
        return

    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1 or int(text) > 999:
        await message.answer("⚠️ Send a valid number between <b>1</b> and <b>999</b>:", parse_mode="HTML")
        return

    quantity = int(text)
    engine = _get_engine(message.from_user.id)
    if not engine:
        await message.answer("❌ <b>Session lost.</b> Use <code>/login</code> or <code>/cookies</code> again.", parse_mode="HTML")
        await state.clear()
        return

    processing = await message.answer(f"🛒 <b>Processing order...</b>\n\n📦 Quantity: <code>{quantity}</code>", parse_mode="HTML")
    _clear_status_tracking(processing)
    engine.on_status = lambda text: _send_status(processing, text)

    added = await engine.add_to_cart(quantity)
    if not added:
        await state.clear()
        return

    checkout_data = await engine.get_checkout_page()

    if checkout_data.get("error"):
        await processing.edit_text(f"❌ <b>Error:</b> <code>{html.escape(checkout_data['error'])}</code>", parse_mode="HTML")
        await state.clear()
        return

    total = checkout_data.get("total", "N/A")
    nonce = checkout_data.get("nonce", "")

    if not nonce:
        await processing.edit_text("❌ <b>Could not get checkout nonce.</b> Try again.", parse_mode="HTML")
        await state.clear()
        return

    await state.update_data(quantity=quantity, checkout_nonce=nonce, order_total=total)

    # Auto-fill billing (non-India) and submit checkout
    await processing.edit_text(
        f"🛒 <b>Order Summary</b>\n\n"
        f"💰 <b>Total:</b> <code>₹{total}</code>\n"
        f"📦 <b>Quantity:</b> <code>{quantity}</code>\n\n"
        "📋 <b>Auto-filling billing details...</b>\n"
        "<i>Generating international billing address</i>",
        parse_mode="HTML"
    )

    result = await engine.fill_and_submit_checkout(billing=None, nonce=nonce)

    if result.get("result") == "failure":
        error_msg = re.sub(r"<[^>]*>", "", result.get("messages", "Unknown error")).strip()
        await processing.edit_text(
            f"❌ <b>Checkout Failed</b>\n\n"
            f"💬 <code>{html.escape(error_msg[:200])}</code>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    await state.update_data(checkout_result=result)
    await state.set_state(BotStates.WAITING_CC_NUMBER)
    await processing.edit_text(
        "✅ <b>Checkout Submitted</b>\n\n"
        f"💰 <b>Amount:</b> <code>₹{total}</code>\n"
        "📋 <b>Billing:</b> Auto-filled ✓\n\n"
        "💳 <b>Step 1 of 3:</b> Send your <b>Card Number</b>\n"
        "<i>13-19 digits, spaces/dashes OK</i>\n\n"
        "🔒 <i>Card details are never saved and auto-deleted from chat</i>",
        parse_mode="HTML"
    )


# ============================================================
# BILLING DETAILS (kept for manual override, but auto-fill is default)
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
            "⚠️ Need <b>8 fields</b> separated by commas:\n\n"
            "<code>Name, Last, Email, Phone, Address, City, State, Pincode</code>",
            parse_mode="HTML"
        )
        return

    if "@" not in parts[2]:
        await message.answer("⚠️ Invalid email. Try again:", parse_mode="HTML")
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
        await message.answer("❌ <b>Session lost.</b> Use <code>/login</code> or <code>/cookies</code> again.", parse_mode="HTML")
        await state.clear()
        return

    processing = await message.answer("📦 <b>Submitting checkout...</b>", parse_mode="HTML")
    _clear_status_tracking(processing)
    engine.on_status = lambda text: _send_status(processing, text)

    result = await engine.fill_and_submit_checkout(billing, nonce)

    if result.get("result") == "failure":
        error_msg = re.sub(r"<[^>]*>", "", result.get("messages", "Unknown error")).strip()
        await processing.edit_text(
            f"❌ <b>Checkout Failed</b>\n\n"
            f"💬 <code>{html.escape(error_msg[:200])}</code>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    await state.update_data(checkout_result=result)
    await state.set_state(BotStates.WAITING_CC_NUMBER)
    await processing.edit_text(
        "✅ <b>Checkout Submitted</b>\n\n"
        "💳 <b>Step 1 of 3:</b> Send your <b>Card Number</b>\n"
        "<i>13-19 digits, spaces/dashes OK</i>\n\n"
        "🔒 <i>Card details are never saved</i>",
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
        await message.answer("⚠️ Invalid card number. Send <b>13-19 digits</b>:", parse_mode="HTML")
        return

    # Delete the user's message so the card number doesn't linger in chat history
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(cc_number=cc_number)
    await state.set_state(BotStates.WAITING_CC_EXPIRY)
    await message.answer(
        "✅ <b>Card number received</b>\n"
        "🗑️ <i>Message deleted for privacy</i>\n\n"
        "📅 <b>Step 2 of 3:</b> Send <b>Expiry</b> as <code>MM/YY</code>\n"
        "<i>Example: <code>12/28</code></i>",
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
        await message.answer("⚠️ Invalid. Send <code>MM/YY</code> (e.g., <code>12/28</code>):", parse_mode="HTML")
        return

    month = int(expiry_clean[:2])
    if month < 1 or month > 12:
        await message.answer("⚠️ Invalid month (01-12):", parse_mode="HTML")
        return

    # Delete the user's message so expiry doesn't linger in chat history
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(cc_expiry=f"{expiry_clean[:2]}/{expiry_clean[2:]}")
    await state.set_state(BotStates.WAITING_CVV)
    await message.answer(
        "✅ <b>Expiry received</b>\n"
        "🗑️ <i>Message deleted for privacy</i>\n\n"
        "🔒 <b>Step 3 of 3:</b> Send <b>CVV</b>\n"
        "<i>3-4 digits from card back</i>",
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
        await message.answer("⚠️ Invalid CVV. Send <b>3-4 digits</b>:", parse_mode="HTML")
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
        await message.answer("❌ <b>Session lost.</b> Use <code>/login</code> or <code>/cookies</code> again.", parse_mode="HTML")
        return

    processing = await message.answer(
        "🔒 <b>Processing Payment</b>\n\n"
        "💳 Entering card details in RazorPay...\n"
        "⏳ <i>Please wait — do not send any messages</i>",
        parse_mode="HTML"
    )
    _clear_status_tracking(processing)
    engine.on_status = lambda text: _send_status(processing, text)

    result = await engine.process_razorpay_payment(
        cc_number, cc_expiry, cvv, checkout_result
    )

    status = result.get("status", "error")
    msg = result.get("message", "Unknown result")
    order_id = result.get("order_id", "")
    order_key = result.get("order_key", "")
    payment_id = result.get("payment_id", "")
    amount = result.get("amount", "")
    status_text = result.get("status_text", "")
    url = result.get("url", "")

    # Build detailed payment result message
    if status == "success":
        details_lines = []
        if order_id:
            details_lines.append(f"🆔 <b>Order ID:</b> <code>{html.escape(order_id)}</code>")
        if order_key:
            details_lines.append(f"🔑 <b>Order Key:</b> <code>{html.escape(order_key)}</code>")
        if payment_id:
            details_lines.append(f"💳 <b>Payment ID:</b> <code>{html.escape(payment_id)}</code>")
        if amount:
            details_lines.append(f"💰 <b>Amount:</b> <code>₹{html.escape(amount)}</code>")
        if status_text:
            details_lines.append(f"📊 <b>Status:</b> <b>{html.escape(status_text)}</b>")

        details_block = "\n".join(details_lines) if details_lines else "✅ Payment confirmed"
        url_line = f"\n\n🔗 <a href=\"{html.escape(url)}\">View Order</a>" if url else ""

        response_text = (
            "✅ <b>Payment Completed</b>\n\n"
            f"{details_block}"
            f"{url_line}\n\n"
            "<i>📧 Check your email for confirmation.</i>"
        )
    elif status == "needs_review":
        details_lines = []
        if order_id:
            details_lines.append(f"🆔 <b>Order ID:</b> <code>{html.escape(order_id)}</code>")
        if payment_id:
            details_lines.append(f"💳 <b>Payment ID:</b> <code>{html.escape(payment_id)}</code>")
        if amount:
            details_lines.append(f"💰 <b>Amount:</b> <code>₹{html.escape(amount)}</code>")

        details_block = "\n".join(details_lines) if details_lines else ""
        details_block = "\n" + details_block + "\n" if details_block else ""
        url_line = f"\n🔗 <a href=\"{html.escape(url)}\">Check order status</a>\n" if url else ""

        response_text = (
            "⚠️ <b>Payment Submitted — Verify Manually</b>\n\n"
            f"💬 {html.escape(msg)}"
            f"{details_block}"
            f"{url_line}\n"
            "<i>📧 Check your email/WhatsApp for confirmation.</i>"
        )
    elif status == "pending":
        response_text = (
            "⏳ <b>Payment Pending</b>\n\n"
            f"💬 {html.escape(msg)}\n\n"
            "<i>Check your bank app or email for updates.</i>"
        )
    else:
        response_text = (
            "❌ <b>Payment Failed</b>\n\n"
            f"💬 {html.escape(msg)}\n\n"
            "💡 <b>Possible reasons:</b>\n"
            "• Invalid card details\n"
            "• Card declined by bank\n"
            "• RazorPay gateway issue\n\n"
            "<i>Send <code>/seturl</code> to try again</i>"
        )

    try:
        await processing.edit_text(response_text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await message.answer(response_text, parse_mode="HTML", disable_web_page_preview=True)


# ============================================================
# /status
# ============================================================
@router.message(F.text == "/status")
async def cmd_status(message: Message, state: FSMContext) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)

    if not engine:
        await message.answer(
            "🔴 <b>Not Logged In</b>\n\n"
            "Send <code>/cookies</code> (free) or <code>/login</code> to start.",
            parse_mode="HTML"
        )
        return

    processing = await message.answer("🔍 <b>Checking session...</b>", parse_mode="HTML")
    _clear_status_tracking(processing)
    try:
        is_logged = await engine.check_login_status()
        if is_logged:
            await processing.edit_text(
                "🟢 <b>Logged In</b>\n\n"
                "Session is active and persistent.\n\n"
                "👉 Send <code>/seturl</code> to buy a product",
                parse_mode="HTML"
            )
        else:
            await processing.edit_text(
                "🔴 <b>Session Expired</b>\n\n"
                "Send <code>/cookies</code> or <code>/login</code> again.",
                parse_mode="HTML"
            )
    except Exception:
        await processing.edit_text(
            "⚠️ <b>Could not check status.</b>\n\n"
            "Send <code>/cookies</code> or <code>/login</code> again.",
            parse_mode="HTML"
        )


# ============================================================
# /cancel
# ============================================================
@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🛑 <b>Cancelled</b>\n\n"
        "Send <code>/start</code> to see commands.",
        parse_mode="HTML"
    )


# ============================================================
# /logout
# ============================================================
@router.message(F.text == "/logout")
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    engine = _get_engine(message.from_user.id)
    if engine:
        try:
            processing = await message.answer("🚪 <b>Logging out...</b>", parse_mode="HTML")
            _clear_status_tracking(processing)
            engine.on_status = lambda text: _send_status(processing, text)
            await engine.logout()
            await engine.close()
        except Exception:
            pass
        finally:
            _remove_engine(message.from_user.id)
    await message.answer(
        "✅ <b>Logged Out</b>\n\n"
        "Session cleared.\n\n"
        "Send <code>/login</code> or <code>/cookies</code> to start again.",
        parse_mode="HTML"
    )


# ============================================================
# /help
# ============================================================
@router.message(F.text == "/help")
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Help & Quick Start</b>\n\n"

        "🚀 <b>Quick Flow:</b>\n"
        "1️⃣ <code>/login</code> or <code>/cookies</code> — sign in\n"
        "2️⃣ <code>/seturl</code> — paste product link\n"
        "3️⃣ Send quantity (e.g., <code>1</code>)\n"
        "4️⃣ Billing auto-fills (international)\n"
        "5️⃣ Send card → expiry → CVV\n"
        "6️⃣ Payment completes automatically\n\n"

        "🔒 <b>Security:</b>\n"
        "• Card data: in-memory only, wiped after use\n"
        "• Card messages: auto-deleted from chat\n"
        "• Billing: auto-generated, non-India\n"
        "• Gateway: RazorPay\n"
        "• Captcha: Nopecha (auto-solved)\n\n"

        "📋 <b>Other Commands:</b>\n"
        "📊 <code>/status</code> — check login\n"
        "🚪 <code>/logout</code> — sign out\n"
        "❌ <code>/cancel</code> — abort current action",
        parse_mode="HTML"
    )


# ============================================================
# FALLBACK — catch non-text / unrecognized messages
# ============================================================
@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    """Catch-all for messages that don't match any command or expected state input."""
    current_state = await state.get_state()
    if current_state is not None:
        hints = {
            BotStates.WAITING_EMAIL:        "your <b>email/username</b>",
            BotStates.WAITING_PASSWORD:     "your <b>password</b>",
            BotStates.WAITING_COOKIES:      "the <b>cookie string</b> from your browser",
            BotStates.WAITING_URL:          "the <b>product URL</b> from gplgames.net",
            BotStates.WAITING_QUANTITY:     "a <b>quantity</b> (e.g., <code>1</code>)",
            BotStates.WAITING_BILLING:      "<b>billing details</b> (comma-separated, 8 fields)",
            BotStates.WAITING_CC_NUMBER:    "your <b>card number</b> (13-19 digits)",
            BotStates.WAITING_CC_EXPIRY:    "your <b>card expiry</b> as <code>MM/YY</code>",
            BotStates.WAITING_CVV:          "your <b>CVV</b> (3-4 digits)",
        }
        hint = hints.get(current_state, "the expected input")
        await message.answer(
            f"⚠️ <b>Waiting for {hint}</b>\n\n"
            f"Send <code>/cancel</code> to abort.",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🤖 <b>Unknown command</b>\n\n"
            "Send <code>/start</code> to see available commands.",
            parse_mode="HTML"
        )
