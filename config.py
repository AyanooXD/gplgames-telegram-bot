"""
Configuration for GPL Games Automation Bot.

All sensitive values (BOT_TOKEN, NOPECHA_API_KEY) are read from environment
variables first, with hardcoded fallbacks ONLY for local development.

For production:
    export BOT_TOKEN="your-bot-token"
    export NOPECHA_API_KEY="your-nopecha-key"
"""

import os

# ============================================================
# BOT TOKEN — from env var, fallback to placeholder
# ============================================================
# Set via:  export BOT_TOKEN="123:ABC..."
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ============================================================
# Target site
# ============================================================
SITE_URL = "https://gplgames.net"
LOGIN_URL = "https://gplgames.net/my-account/"
CHECKOUT_URL = "https://gplgames.net/checkout/"
CART_URL = "https://gplgames.net/cart/"

# WooCommerce AJAX endpoints
WC_AJAX_ADD_TO_CART = f"{SITE_URL}/?wc-ajax=add_to_cart"
WC_AJAX_CHECKOUT = f"{SITE_URL}/?wc-ajax=checkout"
WC_AJAX_FRAGMENTS = f"{SITE_URL}/?wc-ajax=get_refreshed_fragments"

# Session storage path (cookies only — NO credentials or CC stored here)
SESSION_DIR = os.getenv("SESSION_DIR", "sessions")

# ============================================================
# reCAPTCHA — site key from LiteSpeed verification page
# ============================================================
RECAPTCHA_SITE_KEY = "6LewU34UAAAAAHvXqFOcQlm8z1MP1xpGAZCYEeZY"

# ============================================================
# Nopecha — reCAPTCHA solving service (https://nopecha.com)
# Set via:  export NOPECHA_API_KEY="sub_..."
# ============================================================
# Prefer env var. Fallback to legacy hardcoded value (will be removed in future).
NOPECHA_API_KEY = os.getenv("NOPECHA_API_KEY", "sub_1TljWwCRwBwvt6ptc021urVr")

# Payment gateway
PAYMENT_GATEWAY = "razorpay"

# Browser settings (Playwright)
# Set HEADLESS=0 in env to run with visible browser window (debugging)
HEADLESS = os.getenv("HEADLESS", "1") == "1"
BROWSER_TIMEOUT = 60000
