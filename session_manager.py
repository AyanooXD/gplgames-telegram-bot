"""
Session Manager - Handles persistent login sessions via browser cookies.
Only stores cookies for session persistence. NO credentials or payment data.
"""

import json
from pathlib import Path

from config import SESSION_DIR
from secure_log import get_logger

logger = get_logger("SessionManager")


class SessionManager:
    """Manages browser cookie persistence for login sessions."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.session_dir = Path(SESSION_DIR)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.session_dir / f"user_{user_id}_cookies.json"

    def save_cookies(self, cookies: list[dict]) -> None:
        """Save browser cookies to disk for session persistence."""
        try:
            with open(self.cookie_file, "w") as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"Session cookies saved for user {self.user_id}")
        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")

    def load_cookies(self) -> list[dict] | None:
        """Load previously saved cookies. Returns None if no session exists."""
        try:
            if not self.cookie_file.exists():
                logger.info(f"No existing session for user {self.user_id}")
                return None
            with open(self.cookie_file, "r") as f:
                cookies = json.load(f)
            logger.info(f"Session cookies loaded for user {self.user_id}")
            return cookies
        except Exception as e:
            logger.error(f"Failed to load cookies: {e}")
            return None

    def has_session(self) -> bool:
        """Check if a persistent session exists."""
        return self.cookie_file.exists()

    def delete_session(self) -> None:
        """Delete saved session (logout)."""
        try:
            if self.cookie_file.exists():
                self.cookie_file.unlink()
                logger.info(f"Session deleted for user {self.user_id}")
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")

    def save_local_storage(self, storage: list[dict]) -> None:
        """Save localStorage for session persistence."""
        storage_file = self.session_dir / f"user_{self.user_id}_storage.json"
        try:
            with open(storage_file, "w") as f:
                json.dump(storage, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save localStorage: {e}")

    def load_local_storage(self) -> list[dict] | None:
        """Load previously saved localStorage."""
        storage_file = self.session_dir / f"user_{self.user_id}_storage.json"
        try:
            if not storage_file.exists():
                return None
            with open(storage_file, "r") as f:
                return json.load(f)
        except Exception:
            return None