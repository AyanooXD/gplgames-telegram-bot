"""
Secure Logger - Filters out all sensitive data (CC, CVV, expiry).
Card details are NEVER written to any log, file, or storage.
"""

import logging
import re


class SecureLogFilter(logging.Filter):
    """Filters sensitive payment data from all log messages."""

    SENSITIVE_PATTERNS = [
        # 13-19 digit card numbers (word-bounded)
        (re.compile(r"\b\d{13,19}\b"), "[REDACTED-CARD]"),
        # MM/YY or MM/YYYY expiry patterns
        (re.compile(r"\b(?:0[1-9]|1[0-2])\s*[/\s]\s*\d{2,4}\b"), "[REDACTED-EXP]"),
        # 3-4 digit CVV only when preceded by "cvv" or "cvc" context (avoids false positives)
        (re.compile(r"(?i)(?:cvv|cvc)\s*:?\s*\d{3,4}\b"), "[REDACTED-CVV]"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        # getMessage() returns `msg % args` if args are present. We need to
        # redact BOTH the format string and the args, then clear args so the
        # handler doesn't try to format again (which would fail because the
        # redacted msg has no %s placeholders left).
        msg = str(record.getMessage())
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            msg = pattern.sub(replacement, msg)
        record.msg = msg
        record.args = None  # critical: prevent double-formatting crash
        return True


def get_logger(name: str) -> logging.Logger:
    """Get a logger with secure filtering enabled."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(name)s - %(levelname)s - %(message)s")
        )
        handler.addFilter(SecureLogFilter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger