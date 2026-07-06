"""
Configuration for GPL Games Automation Bot.
"""

# ============================================================
# BOT TOKEN
# ============================================================
BOT_TOKEN = "8782707772:AAE_BGWdVRwr6luC82TIQUvDbOZ-YXbehKM"

# Target site
SITE_URL = "https://gplgames.net"
LOGIN_URL = "https://gplgames.net/my-account/"
CHECKOUT_URL = "https://gplgames.net/checkout/"
CART_URL = "https://gplgames.net/cart/"

# WooCommerce AJAX endpoints
WC_AJAX_ADD_TO_CART = f"{SITE_URL}/?wc-ajax=add_to_cart"
WC_AJAX_CHECKOUT = f"{SITE_URL}/?wc-ajax=checkout"
WC_AJAX_FRAGMENTS = f"{SITE_URL}/?wc-ajax=get_refreshed_fragments"

# Session storage path (cookies only — NO credentials or CC stored here)
SESSION_DIR = "sessions"

# ============================================================
# reCAPTCHA — Correct key from LiteSpeed verification page
# ============================================================
RECAPTCHA_SITE_KEY = "6LewU34UAAAAAHvXqFOcQlm8z1MP1xpGAZCYEeZY"

# ============================================================
# Nopecha — reCAPTCHA solving service (https://nopecha.com)
# ============================================================
NOPECHA_API_KEY = "sub_1TljWwCRwBwvt6ptc021urVr"

# Payment gateway
PAYMENT_GATEWAY = "razorpay"

# Browser settings (Playwright)
HEADLESS = True
BROWSER_TIMEOUT = 60000