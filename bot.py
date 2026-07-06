"""
GPL Games Automation Telegram Bot
==================================
Automates login, cart, and payment flow on gplgames.net.

SECURITY: Credit card details are NEVER saved, logged, or persisted.
They exist only in memory during the payment step and are immediately
overwritten with zeros after use.

Usage:
    1. Set your BOT_TOKEN in config.py
    2. Install dependencies: pip install -r requirements.txt
    3. Install browsers: python -m playwright install chromium
    4. Run: python -m bot
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN
from handlers import router as handlers_router
from secure_log import SecureLogFilter, get_logger
from live_log import setup_live_logging

# Set up live issue logging FIRST so we capture errors from the very start.
# - logs/live_issues.log  : WARNING+ live feed (tail -f this)
# - logs/bot_full.log     : DEBUG+ for post-mortem
setup_live_logging(level=logging.INFO)
# Apply secure logging filter to ALL loggers (filters CC data globally)
logging.root.addFilter(SecureLogFilter())


async def main() -> None:
    """Start the Telegram bot."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("ERROR: Bot token not set!")
        print("=" * 50)
        print()
        print("Steps to get your bot token:")
        print("  1. Open Telegram and search for @BotFather")
        print("  2. Send /newbot")
        print("  3. Choose a name for your bot")
        print("  4. Choose a username (must end in 'bot')")
        print("  5. Copy the token BotFather gives you")
        print("  6. Set it as an env var:")
        print('       export BOT_TOKEN="your-token-here"')
        print("     Or paste it in config.py -> BOT_TOKEN")
        print()
        print("Then run this script again.")
        sys.exit(1)

    logger = get_logger("Main")
    logger.info("Starting GPL Games Automation Bot...")

    # Create bot instance
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Create dispatcher and register handlers
    dp = Dispatcher()
    dp.include_router(handlers_router)

    # Start polling
    logger.info("Bot is running! Press Ctrl+C to stop.")
    print()
    print("🤖 GPL Games Automation Bot is running!")
    print("   Send /start in Telegram to begin.")
    print("   Press Ctrl+C to stop.")
    print("   Live issue log: tail -f logs/live_issues.log")
    print()

    # Install global exception handlers so unhandled errors in async tasks
    # (e.g. inside Playwright callbacks) still get written to the live log.
    def _on_unhandled_exception(loop, context):
        msg = context.get("message", "Unhandled exception in event loop")
        exc = context.get("exception")
        if exc is not None:
            logger.error(f"{msg}: {exc!r}", exc_info=exc)
        else:
            logger.error(msg)
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_on_unhandled_exception)

    # Also catch sync exceptions in threads (Playwright uses some)
    def _on_threading_exception(args):
        logger.error(f"Thread exception in {args.thread.name}: {args.exc_value!r}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading_excepthook = getattr(threading, "excepthook", None)
    if threading_excepthook:
        threading.excepthook = _on_threading_exception

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        logger.info("Shutting down...")
        # Cleanup: close all active browser engines
        from handlers import _active_engines
        for user_id, engine in list(_active_engines.items()):
            try:
                await engine.close()
            except Exception as e:
                logger.error(f"Error closing engine for user {user_id}: {e}")
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    import threading  # noqa: E402 (late import so module-level code stays clean)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        # Make sure fatal errors are logged to the live log too
        logging.getLogger("Main").critical(f"Fatal error: {e}", exc_info=True)
        print(f"Fatal error: {e}")
        sys.exit(1)