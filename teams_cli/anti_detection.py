"""Anti-detection layer for Teams API requests.

Adds jitter, full browser headers, and rate limiting
to avoid triggering Microsoft's bot detection.
"""
from __future__ import annotations

import os
import random
import time

import httpx


class BrowserSession:
    """HTTP session wrapper with anti-detection features."""

    def __init__(
        self,
        read_jitter_base: float = 0.3,
        write_jitter_base: float = 2.0,
        proxy: str | None = None,
        timeout: int | float = 30,
    ):
        self._read_base = read_jitter_base
        self._write_base = write_jitter_base
        self._last_request_time: float = 0

        transport_kwargs: dict = {}
        proxy_url = proxy or os.environ.get("TEAMS_PROXY")
        if proxy_url:
            transport_kwargs["proxy"] = proxy_url

        # Allow env override for timeout
        env_timeout = os.environ.get("TEAMS_TIMEOUT")
        actual_timeout = int(env_timeout) if env_timeout else timeout

        self._client = httpx.Client(
            timeout=actual_timeout,
            follow_redirects=True,
            **transport_kwargs,
        )

    @property
    def client(self) -> httpx.Client:
        return self._client

    def jitter(self, is_write: bool = False) -> None:
        """Apply random delay before request."""
        base = self._write_base if is_write else self._read_base
        delay = base * random.uniform(0.7, 1.5)

        # Ensure minimum gap between requests
        elapsed = time.time() - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)

        self._last_request_time = time.time()

    def browser_headers(self, token: str, extra: dict | None = None) -> dict:
        """Build full browser-like headers for Teams API."""
        from .constants import IC3_HEADERS, USER_AGENT

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            **IC3_HEADERS,
        }
        if extra:
            headers.update(extra)
        return headers

    def close(self) -> None:
        self._client.close()
