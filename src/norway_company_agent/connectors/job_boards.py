"""Read documented public recruitment APIs for company-linked boards."""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit


def board_endpoint(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    segments = [part for part in parsed.path.split("/") if part]
    tenant = segments[0] if segments else ""
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        tenant = parse_qs(parsed.query).get("for", [tenant])[0]
        prefix = "https://boards-api.greenhouse.io/v1/boards/"
        suffix = "/jobs?content=true"
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        prefix = "https://api.eu.lever.co/v0/postings/" if host == "jobs.eu.lever.co" else "https://api.lever.co/v0/postings/"
        suffix = "?mode=json&limit=100"
    elif host == "jobs.ashbyhq.com":
        prefix = "https://api.ashbyhq.com/posting-api/job-board/"
        suffix = ""
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant):
        return None
    return prefix + quote(tenant, safe="") + suffix


def listed_adverts(board_url: str, read: Callable[[str], bytes]) -> dict[str, dict]:
    """Return a bounded set of adverts from the exact linked board."""
    endpoint = board_endpoint(board_url)
    if not endpoint:
        return {}
    try:
        raw = read(endpoint)
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    rows = data if isinstance(data, list) else data.get("jobs", []) if isinstance(data, dict) else []
    adverts = []
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict) or row.get("isListed") is False:
            continue
        url = str(row.get("absolute_url") or row.get("hostedUrl") or row.get("jobUrl") or "")
        if board_endpoint(url) != endpoint:
            continue
        categories = row.get("categories") or {}
        location = row.get("location") or (categories.get("location") if isinstance(categories, dict) else "") or ""
        if isinstance(location, dict):
            location = location.get("name", "")
        adverts.append((url, "norway" not in str(location).casefold() and "norge" not in str(location).casefold()))
    adverts.sort(key=lambda item: item[1])
    return {url: {
        "source_url": endpoint,
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "advert_url": url,
    } for url in list(dict.fromkeys(url for url, _ in adverts))[:30]}
