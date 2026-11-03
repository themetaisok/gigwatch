"""Source protocol and HTTP helper with retries."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import requests

if TYPE_CHECKING:
    from datetime import datetime

    from gigwatch.config import Config
    from gigwatch.models import Event

log = logging.getLogger("gigwatch")
USER_AGENT = "gigwatch/0.1 (personal gig alerts)"


class SourceError(Exception):
    pass


class Source(Protocol):
    name: str
    priority: int

    def enabled(self, env: Mapping[str, str]) -> bool: ...

    def fetch(self, cfg: "Config", now: "datetime") -> list["Event"]: ...


def request_with_retry(session: requests.Session, url: str, *, params: Mapping[str, Any] | None = None,
                       headers: Mapping[str, str] | None = None, timeout: float = 30,
                       retries: int = 3, sleep: Callable[[float], None] = time.sleep
                       ) -> requests.Response:
    """GET with retry on 429/5xx/connection errors. Errors never include the URL query (API keys)."""
    host = urlsplit(url).netloc
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    last = "unknown error"
    for attempt in range(retries + 1):
        wait = float(2 ** attempt)
        try:
            r = session.get(url, params=params, headers=hdrs, timeout=timeout)
        except requests.RequestException as e:
            last = f"{type(e).__name__} fetching {host}"
        else:
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code} from {host}"
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", "")
                if ra.isdigit():
                    wait = float(ra)
            elif r.status_code < 500:
                raise SourceError(last)
        if attempt < retries:
            sleep(wait)
    raise SourceError(last)
