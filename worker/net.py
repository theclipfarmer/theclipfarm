"""Tiny HTTP helper on top of urllib (no third-party deps)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class HTTPError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")
        self.status = status
        self.body = body


def request(method: str, url: str, *, headers: dict | None = None, data: Any = None,
            raw: bytes | None = None, timeout: int = 120, retries: int = 3,
            want_headers: bool = False):
    """Send a request. `data` is JSON-encoded; `raw` is sent as-is.
    Returns parsed JSON (or text / bytes), optionally with response headers."""
    hdrs = dict(headers or {})
    body = None
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype and content:
                    out = json.loads(content)
                elif ctype.startswith(("text/", "application/xml", "application/atom")):
                    out = content.decode("utf-8", "replace")
                else:
                    out = content
                return (out, dict(resp.headers)) if want_headers else out
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            # Retry only on rate limits / server errors.
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                last_err = HTTPError(e.code, text, url)
                continue
            raise HTTPError(e.code, text, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise last_err  # pragma: no cover
