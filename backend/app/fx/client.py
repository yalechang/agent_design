"""HTTP fetch of the two authorized ECB reference-rate series. Nothing else in the app touches the network for FX."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.fx.series import ALLOWED_URLS

USER_AGENT = "procurement-qa-prototype/1.0 (ECB reference rates; contact: finance analytics)"
ALLOWED_HOST = "data-api.ecb.europa.eu"


class RateFetchError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    fetched_at: datetime
    body: bytes


def fetch(url: str, *, timeout: float = 20.0, retries: int = 2, backoff: float = 1.0,
          transport: httpx.BaseTransport | None = None) -> FetchResult:
    if url not in ALLOWED_URLS:
        raise RateFetchError(f"URL not in the ECB allow-list: {url}")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport,
                              headers={"User-Agent": USER_AGENT, "Accept": "text/csv"}) as client:
                r = client.get(url)
            if urlparse(str(r.url)).hostname != ALLOWED_HOST:
                raise RateFetchError(f"redirected off the ECB data API: {r.url}")
            if r.status_code != 200:
                raise RateFetchError(f"HTTP {r.status_code} from {url}")
            return FetchResult(url, str(r.url), r.status_code, datetime.now(timezone.utc), r.content)
        except RateFetchError as e:
            last_err = e
            if "allow-list" in str(e) or "redirected" in str(e):
                raise
        except httpx.HTTPError as e:
            last_err = e
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    raise RateFetchError(f"failed to fetch {url}: {last_err}")
