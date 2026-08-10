#!/usr/bin/env python3
"""
wiki.py
-------
Shared Wikipedia client helpers used by the display-only ingest modules
(``national.py`` for USMNT/USWNT, ``competitions.py`` for cup tournaments).

Both pull article HTML from the Wikipedia REST API and read plain text out of
HTML fragments, so those two primitives live here and share a single throttled
client — keeping request spacing sane when several articles are fetched in one
``update`` run.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/html"

# HTTP (throttled + retried).
_THROTTLE_S = 0.4
_last_call = [0.0]
_HEADERS = {"User-Agent": "fbsim/1.0 (football fixtures + results; contact via repo)"}


def article_html(article: str, tries: int = 3) -> str:
    """An article's full HTML via the Wikipedia REST API (Parsoid markup)."""
    url = f"{WIKI_REST}/{urllib.parse.quote(article, safe='')}"
    last_exc: Exception | None = None
    for attempt in range(tries):
        wait = _THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:  # noqa: BLE001 - retried below
            last_exc = exc
        finally:
            _last_call[0] = time.monotonic()
        if attempt < tries - 1:
            time.sleep(2 * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def text(fragment: str) -> str:
    """HTML fragment -> plain text (strip tags, refs, entities, extra space)."""
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    fragment = re.sub(r"\[\d+\]", "", fragment)        # [1] ref marks
    return re.sub(r"\s+", " ", fragment).strip()
