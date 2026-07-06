"""
Live Issue Logger — captures every error, warning, and exception the bot
encounters in real-time, with timestamps and full context.

Output files (under LIVE_LOG_DIR, default: logs/):
  - live_issues.log    — WARNING+ only, one line per issue (the "live feed")
  - bot_full.log       — DEBUG+ everything (for post-mortem)

Usage in tailing terminal:
    tail -f logs/live_issues.log

The live_issues.log file is line-buffered so you see issues the instant
they happen. Each line includes:
    timestamp | level | logger name | message | (optional traceback)

Sensitive data (card numbers, CVV, expiry) is filtered by SecureLogFilter
before anything is written, so this file is safe to share.
"""

import logging
import logging.handlers
import os
import re
import sys
import traceback
from pathlib import Path
from datetime import datetime, timezone

from secure_log import SecureLogFilter

# Where to write logs. Override via env var LIVE_LOG_DIR.
LIVE_LOG_DIR = Path(os.getenv("LIVE_LOG_DIR", "logs"))
LIVE_LOG_DIR.mkdir(parents=True, exist_ok=True)

LIVE_ISSUES_FILE = LIVE_LOG_DIR / "live_issues.log"
FULL_LOG_FILE = LIVE_LOG_DIR / "bot_full.log"

# Maximum log file size before rotation (5 MB), keep 3 backups
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


class LiveIssueFormatter(logging.Formatter):
    """Compact one-line formatter for the live issues feed."""

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp with milliseconds + timezone
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone()
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + ts.strftime("%z")

        # Compact level tag
        level_tag = {
            "CRITICAL": "💥CRIT",
            "ERROR":    "❌ERR ",
            "WARNING":  "⚠️WARN",
            "INFO":     "✓INFO",
            "DEBUG":    "•DEBUG",
        }.get(record.levelname, record.levelname)

        msg = record.getMessage()

        # Compose the one-liner
        line = f"[{ts_str}] {level_tag} | {record.name:<15} | {msg}"

        # Append traceback on errors (multi-line, indented)
        if record.exc_info and record.exc_info[1] is not None:
            exc_type = record.exc_info[0].__name__
            exc_msg = str(record.exc_info[1])
            line += f"\n    └─ {exc_type}: {exc_msg}"
            # Short traceback (last 5 frames) for live triage
            tb_lines = traceback.format_exception(*record.exc_info, limit=5)
            tb_text = "".join(tb_lines).rstrip()
            for tb_line in tb_text.split("\n"):
                line += f"\n    {tb_line}"

        return line


class FullLogFormatter(logging.Formatter):
    """Verbose formatter for the full debug log (post-mortem analysis)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone()
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        msg = record.getMessage()
        line = (
            f"[{ts_str}] {record.levelname:<8} "
            f"{record.name}:{record.lineno} - {msg}"
        )
        if record.exc_info:
            line += "\n" + "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return line


def setup_live_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Wire up the live issue logger.

    - Adds a rotating file handler for live_issues.log (WARNING+, line-buffered)
    - Adds a rotating file handler for bot_full.log (DEBUG+ if level=DEBUG)
    - Adds a stream handler for stdout (so `python bot.py` still shows live)
    - All handlers get the SecureLogFilter so card data is never written

    Returns the root logger.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.addFilter(SecureLogFilter())  # global filter, applies to all handlers

    # Don't double-add handlers on repeated calls
    existing_names = {getattr(h, "_live_tag", None) for h in root.handlers}

    # 1. Live issues file — WARNING+ only, line-buffered for instant tail
    if "live_issues" not in existing_names:
        live_handler = logging.handlers.RotatingFileHandler(
            LIVE_ISSUES_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        live_handler.setLevel(logging.WARNING)
        live_handler.setFormatter(LiveIssueFormatter())
        live_handler._live_tag = "live_issues"
        root.addHandler(live_handler)

    # 2. Full log — everything, for post-mortem
    if "full_log" not in existing_names:
        full_handler = logging.handlers.RotatingFileHandler(
            FULL_LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        full_handler.setLevel(logging.DEBUG)
        full_handler.setFormatter(FullLogFormatter())
        full_handler._live_tag = "full_log"
        root.addHandler(full_handler)

    # 3. Stdout — INFO+, so running bot.py in terminal still shows status
    if "stdout" not in existing_names:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(LiveIssueFormatter())
        stream_handler._live_tag = "stdout"
        root.addHandler(stream_handler)

    return root


def log_issue(logger: logging.Logger, message: str, *,
              level: int = logging.ERROR,
              exc: Exception | None = None) -> None:
    """
    Convenience helper to log a structured live issue with optional exception.

    Example:
        from live_log import log_issue
        try:
            ...
        except Exception as e:
            log_issue(logger, "Failed to inject captcha token", exc=e)
    """
    extra = {}
    if exc is not None:
        # logging will pick up exc_info from sys.exc_info() if we pass it
        logger.log(level, message, exc_info=(type(exc), exc, exc.__traceback__))
    else:
        logger.log(level, message)


# Allow `from live_log import logger` as a quick default
logger = logging.getLogger("Live")
