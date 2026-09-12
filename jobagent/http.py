"""Shared HTTP access.

Every outbound request goes through here so retries, timeouts and per-host
politeness delays are applied uniformly. Being a well-behaved client is what
keeps this tool usable long term.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config, load_config
from .tls import enable_system_trust

log = logging.getLogger(__name__)

_last_request_at: dict[str, float] = {}
# One lock per host, and a small lock guarding the registry of those locks.
# Politeness has to be per host: a single global lock held across the sleep
# would serialise every source behind the slowest one, which is exactly what
# makes an interactive search take minutes instead of seconds.
_host_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()

RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


class HttpError(RuntimeError):
    def __init__(self, status_code: int, url: str):
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url


class HttpClient:
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        if self.config.http.use_system_certs:
            enable_system_trust(self.config.http.ca_bundle)
        self._client = httpx.Client(
            timeout=self.config.http.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.config.http.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def _throttle(self, url: str) -> None:
        """Space out requests to the same host, without blocking other hosts."""
        host = urlparse(url).netloc
        min_delay = self.config.http.min_delay_seconds
        with _registry_lock:
            lock = _host_locks.setdefault(host, threading.Lock())
        # Held across the sleep on purpose: that is what serialises this host.
        with lock:
            last = _last_request_at.get(host)
            now = time.monotonic()
            if last is not None:
                wait = min_delay + random.uniform(0, min_delay * 0.4) - (now - last)
                if wait > 0:
                    time.sleep(wait)
            _last_request_at[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        self._throttle(url)

        @retry(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(self.config.http.max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=15),
            reraise=True,
        )
        def _do() -> httpx.Response:
            return self._client.get(url, **kwargs)

        response = _do()
        # 404 is normal here: it usually means a board slug no longer exists.
        if response.status_code >= 400:
            raise HttpError(response.status_code, url)
        return response

    def post(self, url: str, **kwargs) -> httpx.Response:
        """POST with the same throttling and retry policy as GET.

        Workday and Jooble expose their job search as a JSON POST rather than a
        query string, so this is not optional.
        """
        self._throttle(url)

        @retry(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(self.config.http.max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=15),
            reraise=True,
        )
        def _do() -> httpx.Response:
            return self._client.post(url, **kwargs)

        response = _do()
        if response.status_code >= 400:
            raise HttpError(response.status_code, url)
        return response

    def get_json(self, url: str, **kwargs) -> dict | list:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, **kwargs) -> dict | list:
        return self.post(url, **kwargs).json()

    def get_text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
